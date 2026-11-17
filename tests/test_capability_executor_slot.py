from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from threading import Event

import httpx
import pytest

from frontend import server
from mewcode.expert_group import (
    ExpertProfile, ExpertProfileStore, relocate_expert_profile,
    select_capability_executor_model,
)
from mewcode.expert_runs import ExpertRunStore
from mewcode.model_registry import ModelRef


REF = ModelRef("chosen-provider", "chosen-model", "medium")


def profile_value():
    # Existing role models need not be currently available to change only the
    # helper. Their bindings and unrelated extension data must be preserved.
    role = {"provider_key": "old-roles", "model_id": "old-role-model", "effort": "high"}
    return {name: dict(role) for name in ("manager", "planner", "executor", "auditor")} | {
        "planner_routes": {"backend": [dict(role)]}, "extension": {"keep": [1, 2]},
    }


def catalog_value(**overrides):
    details = {"provider_key": REF.provider_key, "model_id": REF.model_id,
               "capabilities": ["chat", "tools"], "tools": True, "efforts": ["medium", "high"]}
    details.update(overrides)
    return {"models": [details], "providers": [
        {"key": REF.provider_key, "models": [REF.model_id]},
        {"key": "grok", "models": ["grok-4.6"], "brand": "xai"},
    ]}


@pytest.mark.parametrize("details", [
    {"tools": False}, {"review_only": True}, {"protocol": "typesafe"},
    {"capabilities": ["chat"]}, {"efforts": ["low"]},
])
def test_slot_rejects_known_missing_tool_capability_or_effort(details):
    selected, _ = select_capability_executor_model(catalog_value(**details), REF)
    assert selected is None


def test_explicit_helper_is_not_relocated_when_other_roles_are_healed():
    profile = replace(ExpertProfile.from_value(profile_value()), capability_executor=REF)
    calls = []

    def relocate(ref):
        calls.append(ref)
        return ModelRef("replacement", ref.model_id, ref.effort)

    changed = relocate_expert_profile(SimpleNamespace(relocate_model_ref=relocate), profile)
    assert changed.capability_executor == REF
    assert REF not in calls
    assert changed.executor.provider_key == "replacement"


def test_project_slot_save_preserves_raw_role_extensions_and_other_projects(tmp_path):
    store = ExpertProfileStore(tmp_path / "profiles.json")
    project, other = tmp_path / "project", tmp_path / "other"
    store.put(project, profile_value())
    store.put(other, profile_value())
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    from mewcode.expert_group import normalize_project_path
    raw[normalize_project_path(project)]["extension"] = {"keep": [1, 2]}
    store.path.write_text(json.dumps(raw), encoding="utf-8")
    original = copy.deepcopy(raw)
    saved = store.set_capability_executor(project, REF)
    assert saved.capability_executor == REF
    after = json.loads(store.path.read_text(encoding="utf-8"))
    assert after[normalize_project_path(project)].pop("capability_executor") == REF.as_dict()
    assert after == original
    store.set_capability_executor(project, None)
    assert json.loads(store.path.read_text(encoding="utf-8")) == original


@pytest.fixture
def run_env(tmp_path):
    store = ExpertRunStore(tmp_path / "runs")
    run_id = "f" * 32
    store.create(run_id=run_id, conversation_id="owner", task="original task", attachments=[],
                 profile=profile_value(), strategy="stable")

    def progress(value):
        value.update(node_states={"manager": {"status": "failed", "text": "old provider evidence"}},
                     loop_states={"loop": {"round": 2, "tokens": 19}},
                     gate_confirmations={"gate": {"answer": None}},
                     recovery_checkpoints={"manager": {"retry_same_checkpoint": 1}},
                     audit_protocols={"audit": {"round": 2}},
                     handoff={"escalation_receipt": {"error": "HTTP 426"}})

    store.mutate(run_id, progress)
    store.transition(run_id, "paused", reason="provider failure")
    return store, run_id


def bind(store, run_id, **overrides):
    request = dict(conversation_id="owner", model=REF.as_dict(),
                   expected_updated_at=store.load(run_id)["updated_at"],
                   command_id="command-1", reason="user selected helper")
    request.update(overrides)
    return store.set_capability_executor(run_id, **request)


def protected(snapshot):
    value = copy.deepcopy(snapshot)
    for key in ("updated_at", "last_seq", "events", "capability_executor_changes"):
        value.pop(key, None)
    value["locked_profile"].pop("capability_executor", None)
    return value


def test_paused_binding_is_durable_audited_idempotent_and_does_not_resume(run_env):
    store, run_id = run_env
    original = store.load(run_id)
    changed = bind(store, run_id)
    assert protected(changed) == protected(original)
    assert changed["status"] == "paused" and changed["attempt"] == original["attempt"]
    assert changed["locked_profile"]["capability_executor"] == REF.as_dict()
    change = changed["capability_executor_changes"][0]
    assert change["previous_model"] is None and change["authorized_by"] == "user"
    assert change["checkpoint_id"]
    repeated = bind(store, run_id, expected_updated_at=original["updated_at"])
    assert repeated == changed
    restored = ExpertRunStore(store.root).load(run_id)
    assert restored["locked_profile"]["capability_executor"] == REF.as_dict()
    assert restored["capability_executor_changes"] == changed["capability_executor_changes"]
    public = store.public(run_id)
    assert public["capability_executor"] == REF.as_dict()
    assert "locked_profile" not in public and "capability_executor_changes" not in public
    assert any(e["type"] == "expert_capability_executor_changed" for e in public["events"])
    resumed = store.resume(run_id, strategy="stable")
    assert resumed["locked_profile"]["capability_executor"] == REF.as_dict()
    assert resumed["locked_profile"]["executor"] == original["locked_profile"]["executor"]


@pytest.mark.parametrize("override", [
    {"conversation_id": "wrong"}, {"expected_updated_at": "stale"},
    {"expected_updated_at": ""}, {"command_id": ""}, {"reason": ""},
    {"model": {"provider_key": "p", "model_id": "codex-auto-review"}},
])
def test_rejected_binding_has_no_partial_cached_changes(run_env, override):
    store, run_id = run_env
    original = store.load(run_id)
    with pytest.raises(ValueError):
        bind(store, run_id, **override)
    assert store.load(run_id) == original
    assert store.checkpoint_archive.list(run_id) == []


@pytest.mark.parametrize("status,active", [("running", []), ("completed", []), ("paused", ["manager"])])
def test_binding_rejects_running_completed_or_active_nodes(run_env, status, active):
    store, run_id = run_env
    store.mutate(run_id, lambda s: s.update(status=status, active_node_ids=active))
    if active:
        # Normalized stopped snapshots remove stale active IDs. Exercise the
        # explicit guard against a transient live-cache worker registration.
        with store._lock:
            store._live[run_id]["active_node_ids"] = active
    original = store.load(run_id)
    with pytest.raises(ValueError):
        bind(store, run_id)
    assert store.load(run_id) == original


def test_command_ids_cannot_be_reused_for_different_selection(run_env):
    store, run_id = run_env
    original = bind(store, run_id)
    with pytest.raises(ValueError):
        bind(store, run_id, model=replace(REF, effort="high").as_dict())
    assert store.load(run_id) == original


def test_competing_bindings_use_the_checkpoint_cas(run_env):
    store, run_id = run_env
    stamp = store.load(run_id)["updated_at"]

    def attempt(effort):
        try:
            bind(store, run_id, expected_updated_at=stamp, command_id=effort,
                 model=replace(REF, effort=effort).as_dict())
            return "accepted"
        except ValueError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["medium", "high"]))
    assert sorted(results) == ["accepted", "rejected"]
    assert len(store.load(run_id)["capability_executor_changes"]) == 1


def test_same_command_retries_failed_persistence_before_acknowledging(run_env, monkeypatch):
    store, run_id = run_env
    persist = store._persist
    calls = []

    def fail_once(snapshot):
        calls.append(snapshot["run_id"])
        if len(calls) == 1:
            raise OSError("simulated full disk")
        persist(snapshot)

    monkeypatch.setattr(store, "_persist", fail_once)
    with pytest.raises(OSError):
        bind(store, run_id)
    changed = bind(store, run_id)
    reloaded = ExpertRunStore(store.root).load(run_id)
    assert len(calls) == 2
    assert reloaded["locked_profile"]["capability_executor"] == REF.as_dict()
    assert reloaded["capability_executor_changes"] == changed["capability_executor_changes"]
    assert len(changed["capability_executor_changes"]) == 1


def test_concurrent_same_command_waits_for_persistence(run_env, monkeypatch):
    store, run_id = run_env
    persist, enqueue = store._persist, store._enqueue_write
    entered, release, duplicate_waiting = Event(), Event(), Event()
    enqueues = []

    def delayed(snapshot):
        entered.set()
        assert release.wait(5)
        persist(snapshot)

    def observed(run, *, durable):
        enqueues.append(run)
        if len(enqueues) == 2:
            duplicate_waiting.set()
        enqueue(run, durable=durable)

    monkeypatch.setattr(store, "_persist", delayed)
    monkeypatch.setattr(store, "_enqueue_write", observed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(bind, store, run_id)
        try:
            assert entered.wait(5)
            second = pool.submit(bind, store, run_id)
            assert duplicate_waiting.wait(5)
            assert not first.done() and not second.done()
        finally:
            release.set()
        assert first.result() == second.result()
    reloaded = ExpertRunStore(store.root).load(run_id)
    assert len(reloaded["capability_executor_changes"]) == 1


@pytest.fixture
def api_env(tmp_path, monkeypatch, run_env):
    store, run_id = run_env
    project = str(tmp_path / "project")
    profiles = ExpertProfileStore(tmp_path / "profiles.json")
    profiles.put(project, profile_value())
    busy = {"value": False}
    session = SimpleNamespace(work_dir=project, session=SimpleNamespace(session_id="owner"),
                              chat_busy=lambda cid: busy["value"])
    catalog = catalog_value()
    resolved = []
    events = []
    hooks = {}

    class Registry:
        async def catalog(self):
            if "catalog" in hooks:
                hooks["catalog"]()
            return catalog

        def resolve(self, value):
            ref = ModelRef.from_value(value)
            resolved.append(ref)
            if "resolved" in hooks:
                return hooks["resolved"](ref)
            return SimpleNamespace(ref=ref, config=SimpleNamespace(protocol="openai"),
                                   capabilities=("chat", "tools"))

        def relocate_model_ref(self, value):
            raise AssertionError("an explicit helper provider must not be relocated")

    async def emit(event):
        events.append(event)

    monkeypatch.setattr(server, "SESSION", session)
    monkeypatch.setattr(server, "EXPERT_RUNS", store)
    monkeypatch.setattr(server, "EXPERT_PROFILES", profiles)
    monkeypatch.setattr(server, "MODEL_REGISTRY", Registry())
    monkeypatch.setattr(server.WS_EVENTS, "send_json", emit)
    return SimpleNamespace(store=store, run_id=run_id, project=project, profiles=profiles,
                           session=session, busy=busy, catalog=catalog, events=events,
                           hooks=hooks, resolved=resolved)


async def post_run(env, **overrides):
    body = {"conversation_id": "owner", "project": env.project, "model": REF.as_dict(),
            "command_id": "api-1", "expected_updated_at": env.store.load(env.run_id)["updated_at"]}
    body.update(overrides)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        return await client.post(f"/api/expert-runs/{env.run_id}/capability-executor", json=body)


@pytest.mark.asyncio
async def test_api_saves_project_and_current_run_independently(api_env):
    env = api_env
    before = env.store.load(env.run_id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.put("/api/expert-profile/capability-executor", json={
            "conversation_id": "owner", "project": env.project, "model": REF.as_dict()})
    assert response.status_code == 200
    assert env.profiles.get(env.project).capability_executor == REF
    assert env.store.load(env.run_id) == before
    response = await post_run(env)
    assert response.status_code == 200
    assert response.json()["snapshot"]["capability_executor"] == REF.as_dict()
    assert protected(env.store.load(env.run_id)) == protected(before)
    assert env.resolved == [REF, REF]
    assert env.events[0]["snapshot"]["status"] == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["busy", "conversation", "project", "stale", "switch-during-catalog",
                                  "busy-during-catalog", "missing-model", "no-tools", "unsupported-effort",
                                  "resolved-effort", "resolved-provider", "resolved-no-tools"])
async def test_api_rejects_unsafe_changes_without_mutating_the_run(api_env, mode):
    env = api_env
    before = env.store.load(env.run_id)
    body = {}
    if mode == "busy":
        env.busy["value"] = True
    elif mode == "conversation":
        body["conversation_id"] = "wrong"
    elif mode == "project":
        body["project"] = env.project + "-wrong"
    elif mode == "stale":
        body["expected_updated_at"] = "old"
    elif mode == "switch-during-catalog":
        env.hooks["catalog"] = lambda: setattr(env.session.session, "session_id", "other")
    elif mode == "busy-during-catalog":
        env.hooks["catalog"] = lambda: env.busy.update(value=True)
    elif mode == "missing-model":
        body["model"] = replace(REF, provider_key="missing").as_dict()
    elif mode == "no-tools":
        env.catalog["models"][0].update(review_only=True, protocol="typesafe", tools=False)
    elif mode == "unsupported-effort":
        body["model"] = replace(REF, effort="ultra").as_dict()
    elif mode.startswith("resolved-"):
        def altered(ref):
            return SimpleNamespace(
                ref=replace(ref, effort="") if mode == "resolved-effort" else
                    replace(ref, provider_key="another-provider") if mode == "resolved-provider" else ref,
                config=SimpleNamespace(protocol="openai"),
                capabilities=("chat",) if mode == "resolved-no-tools" else ("chat", "tools"),
            )
        env.hooks["resolved"] = altered
    response = await post_run(env, **body)
    assert response.status_code in {400, 409}
    assert env.store.load(env.run_id) == before
    assert not env.events


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", ["conversation", "project"])
async def test_project_selection_rejects_scope_change_during_discovery(api_env, switch):
    env = api_env
    original = env.profiles.path.read_bytes()
    env.hooks["catalog"] = (lambda: setattr(env.session.session, "session_id", "other")) if switch == "conversation" \
        else (lambda: setattr(env.session, "work_dir", env.project + "-other"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.put("/api/expert-profile/capability-executor", json={
            "conversation_id": "owner", "project": env.project, "model": REF.as_dict()})
    assert response.status_code == 409
    assert env.profiles.path.read_bytes() == original


@pytest.mark.asyncio
async def test_disabling_a_paused_slot_does_not_select_grok_or_start_work(api_env):
    env = api_env
    bind(env.store, env.run_id)
    response = await post_run(env, model=None)
    assert response.status_code == 200
    assert response.json()["snapshot"]["capability_executor"] is None
    assert response.json()["snapshot"]["status"] == "paused"
    assert env.resolved == []
