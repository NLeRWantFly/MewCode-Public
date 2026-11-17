"""Retired dialogue review must not precede the current host resume policy."""
from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.conversation import ConversationManager, Message
from mewcode.memory.session import SessionManager
from tests.test_chat_task_routing import ingress  # noqa: F401 - shared real WS fixture


class Store:
    def __init__(self, snapshot, root):
        self.value = copy.deepcopy(snapshot)
        self.root = root
        self.writes = 0

    def load(self, identity):
        return copy.deepcopy(self.value) if identity == self.value["run_id"] else None

    def mutate(self, identity, fn, **_kwargs):
        assert identity == self.value["run_id"]
        candidate = copy.deepcopy(self.value)
        fn(candidate)
        self.value = candidate
        self.writes += 1
        return self.load(identity)


def old_review():
    return {"action": "hold", "relation": "needs_user", "evidence_incomplete": True,
            "summary": "62 条硬性对账记录；原编排保持暂停",
            "handoff_review_progress": {"source_id": "a" * 64, "total": 171,
                "classified": 170, "reviewed": 0, "deterministic_hold_records": 62,
                "review_status": "hard_reconciliation_required"}}


def snapshot():
    return {"run_id": "a" * 32, "conversation_id": "chat-a", "status": "paused",
            "updated_at": "v1", "attempt": 9, "task": "original task", "attachments": [],
            "active_node_ids": [], "locked_models": {"executor": "original-route"},
            "locked_profile": {"token_budget": 123}, "review_queues": {},
            "node_states": {"done": {"status": "completed", "text": "preserved"},
                "work": {"status": "needs_attention", "tools": {
                    "unknown": {"name": "Bash", "status": "outcome_unknown"}},
                    "native_cleanup_error": "unconfirmed", "retry_count": 7}},
            "pending_dialogue_handoff": old_review(),
            "pause_dialogue_anchor": {"record_offset": 0, "attempt": 9}}


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [False, True])
async def test_old_171_fragment_gate_reaches_host_resume_without_semantic_call(tmp_path, monkeypatch, authorized):
    from frontend import server

    saved = snapshot()
    if authorized:
        saved["handoff_review_grant"] = {"provider_key": "removed-typesafe", "model_id": "jev-latest",
            "grant_id": "old-grant", "max_tokens": 105000, "source_id": "a" * 64}
        saved["handoff_review_grants"] = [copy.deepcopy(saved["handoff_review_grant"])]
    session = SessionManager(str(tmp_path)).create()
    session.append(Message(role="user", content="unrelated historical conversation" * 1500))
    store = Store(saved, tmp_path / "runs")
    runtime = SimpleNamespace(session=session, conversation_id="chat-a", work_dir=str(tmp_path),
        conversation=ConversationManager(), status_payload=lambda: {}, tasks_payload=lambda: [])
    events = []
    socket = SimpleNamespace(send_json=AsyncMock(side_effect=events.append))
    model = SimpleNamespace(resolve=lambda *_: pytest.fail("retired semantic route must not be resolved"),
                            records=lambda: [])
    monkeypatch.setattr(server, "EXPERT_RUNS", store)
    monkeypatch.setattr(server, "MODEL_REGISTRY", model)
    review = AsyncMock(side_effect=AssertionError("retired dialogue review must not run"))
    monkeypatch.setattr(server, "_run_takeover_dialogue", review)
    resume = AsyncMock()
    monkeypatch.setattr(server, "run_expert_chat", resume)
    try:
        await server._resume_expert_after_paused_dialogue(socket, runtime, saved,
            supplemental="explicit supplement", retry_node_id="work")
    finally:
        session.close()

    resume.assert_awaited_once()
    review.assert_not_awaited()
    kwargs = resume.await_args.kwargs
    assert kwargs["resume_run_id"] == saved["run_id"]
    assert kwargs["retry_node_id"] == "work" and kwargs["supplemental"] == "explicit supplement"
    assert "pending_dialogue_handoff" not in store.value
    assert "handoff_review_grant" not in store.value
    assert store.value["node_states"] == saved["node_states"]
    assert store.value["locked_profile"] == saved["locked_profile"]
    assert store.value["locked_models"] == saved["locked_models"]
    assert store.value["status"] == "paused" and store.value["attempt"] == 9
    assert not any(event.get("handoff_review_progress") for event in events)
    archives = store.value["retired_paused_handoff_reviews"]
    assert archives[0]["status"] == "retired_not_approved"
    assert archives[0]["decision"] == saved["pending_dialogue_handoff"]
    if authorized:
        assert archives[0]["grant"] == saved["handoff_review_grant"]
        assert store.value["handoff_review_grants"] == saved["handoff_review_grants"]


@pytest.mark.parametrize("kind", ["fragment_review", "route_error", "no_decision", "interrupted"])
def test_retirement_is_idempotent_and_does_not_approve_effects(kind):
    from mewcode.orchestration.retired_handoff import retire_paused_handoff_review

    saved = snapshot()
    if kind == "route_error":
        saved["pending_dialogue_handoff"]["handoff_review_progress"] = {
            "error_kind": "handoff_review_route_unavailable", "provider_key": "gone"}
    elif kind == "no_decision":
        saved["pending_dialogue_handoff"] = {"action": "hold", "relation": "needs_user",
            "summary": "接力分析没有形成有效决定，原编排保持暂停。"}
    elif kind == "interrupted":
        saved["pending_dialogue_handoff"] = {"action": "hold", "relation": "needs_user",
            "retryable": True, "failure_kind": "interrupted", "summary": "old model interrupted"}
    original = copy.deepcopy(saved)
    assert retire_paused_handoff_review(saved)
    retired = copy.deepcopy(saved)
    assert not retire_paused_handoff_review(saved)
    assert saved == retired
    for key in ("node_states", "review_queues", "locked_models", "locked_profile", "attempt", "status"):
        assert saved[key] == original[key]


@pytest.mark.parametrize("pending", [
    {"action": "hold", "relation": "plan_changed", "summary": "approve changed plan"},
    {"action": "hold", "relation": "needs_user", "summary": "confirm scope"},
    {"action": "hold", "relation": "context_changed", "summary": "completed node invalid",
     "invalidated_completed_node_ids": ["done"], "handoff_review_progress": {"source_id": "a" * 64}},
])
def test_real_plan_user_and_acceptance_holds_survive_retirement(pending):
    from mewcode.orchestration.retired_handoff import retire_paused_handoff_review

    saved = snapshot()
    saved["pending_dialogue_handoff"] = copy.deepcopy(pending)
    retire_paused_handoff_review(saved)
    assert saved["pending_dialogue_handoff"] == pending


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["conversation", "version", "active", "completed"])
async def test_resume_entry_checks_owner_version_and_idle_checkpoint(tmp_path, monkeypatch, changed):
    from frontend import server

    saved = snapshot()
    store = Store(saved, tmp_path / "runs")
    if changed == "conversation":
        store.value["conversation_id"] = "other"
    elif changed == "version":
        store.value["updated_at"] = "v2"
    elif changed == "active":
        store.value["active_node_ids"] = ["work"]
    else:
        store.value["status"] = "completed"
    runtime = SimpleNamespace(conversation_id="chat-a", status_payload=lambda: {}, tasks_payload=lambda: [])
    monkeypatch.setattr(server, "EXPERT_RUNS", store)
    resume = AsyncMock()
    monkeypatch.setattr(server, "run_expert_chat", resume)
    await server._resume_expert_after_paused_dialogue(SimpleNamespace(send_json=AsyncMock()), runtime, saved)
    resume.assert_not_awaited()
    assert store.writes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["handoff_review_authorize", "handoff_review_escalate",
                                  "handoff_review_reconcile_page"])
async def test_stale_client_cannot_reactivate_removed_review(ingress, monkeypatch, action):
    from frontend import server

    saved = snapshot()
    saved["conversation_id"] = ingress.runtime.conversation_id
    store = Store(saved, ingress.runs.root)
    original = copy.deepcopy(store.value)
    monkeypatch.setattr(server, "EXPERT_RUNS", store)
    monkeypatch.setattr(server, "MODEL_REGISTRY", SimpleNamespace(
        resolve=lambda *_: pytest.fail("deprecated action resolved a model")))
    socket = ingress.Socket([{"type": action, "run_id": saved["run_id"],
        "model_ref": {"provider_key": "typesafe", "model_id": "jev-latest"},
        "max_tokens": 105000, "confirmed": True, "offset": 0}])
    await server.ws_endpoint(socket)
    assert any(event.get("error_code") == "retired_paused_handoff_review"
               for event in socket.messages)
    assert store.value == original and store.writes == 0
    assert not ingress.prompts


def test_retirement_archive_is_private_in_public_run_snapshot():
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.orchestration.retired_handoff import retire_paused_handoff_review

    saved = snapshot()
    retire_paused_handoff_review(saved)
    for details in (False, True):
        public = ExpertRunStore._public_value(saved, include_details=details)
        assert "retired_paused_handoff_reviews" not in public


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "terminated"])
async def test_rerun_after_completed_anchor_preserves_existing_resume_options(tmp_path, monkeypatch, status):
    from frontend import server

    saved = snapshot()
    saved["status"] = status
    store = Store(saved, tmp_path / "runs")
    runtime = SimpleNamespace(conversation_id="chat-a", status_payload=lambda: {}, tasks_payload=lambda: [])
    monkeypatch.setattr(server, "EXPERT_RUNS", store)
    resume = AsyncMock()
    monkeypatch.setattr(server, "run_expert_chat", resume)
    options = {"resume_after_node": "done", "recovery_action": "approve",
               "recovery_info": "explicit action", "grant_extra_budget": True, "sync_canvas": True}
    await server._resume_expert_after_paused_dialogue(SimpleNamespace(send_json=AsyncMock()), runtime, saved, **options)
    resume.assert_awaited_once()
    for key, value in options.items():
        assert resume.await_args.kwargs[key] == value
    assert store.value["node_states"] == saved["node_states"]


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["initial", "returning"])
async def test_cancelled_continue_does_not_dispatch_original_run(tmp_path, monkeypatch, boundary):
    from frontend import server

    saved = snapshot()
    store = Store(saved, tmp_path / "runs")
    runtime = SimpleNamespace(conversation_id="chat-a", cancel_requested=boundary == "initial",
                              status_payload=lambda: {}, tasks_payload=lambda: [])
    def on_event(event):
        if boundary == "returning" and event.get("phase") == "returning":
            runtime.cancel_requested = True
    monkeypatch.setattr(server, "EXPERT_RUNS", store)
    resume = AsyncMock()
    monkeypatch.setattr(server, "run_expert_chat", resume)
    await server._resume_expert_after_paused_dialogue(
        SimpleNamespace(send_json=AsyncMock(side_effect=on_event)), runtime, saved)
    resume.assert_not_awaited()
    assert store.value["node_states"] == saved["node_states"]


@pytest.mark.asyncio
async def test_retirement_survives_real_store_reload_and_repeated_continue(tmp_path, monkeypatch):
    from frontend import server
    from mewcode.expert_runs import ExpertRunStore

    saved = snapshot()
    store = ExpertRunStore(tmp_path / "runs")
    store.create(run_id=saved["run_id"], conversation_id="chat-a", task=saved["task"],
                 attachments=[], profile={}, strategy="stable")
    store.mutate(saved["run_id"], lambda value: value.update(saved), durable=True)
    runtime = SimpleNamespace(conversation_id="chat-a", status_payload=lambda: {}, tasks_payload=lambda: [])
    monkeypatch.setattr(server, "EXPERT_RUNS", store)
    resume = AsyncMock()
    monkeypatch.setattr(server, "run_expert_chat", resume)
    socket = SimpleNamespace(send_json=AsyncMock())
    await server._resume_expert_after_paused_dialogue(socket, runtime, store.load(saved["run_id"]))
    reloaded = ExpertRunStore(tmp_path / "runs")
    checkpoint = reloaded.load(saved["run_id"])
    assert checkpoint["node_states"] == saved["node_states"]
    assert checkpoint["retired_paused_handoff_reviews"][0]["decision"] == saved["pending_dialogue_handoff"]
    assert "pending_dialogue_handoff" not in checkpoint
    monkeypatch.setattr(server, "EXPERT_RUNS", reloaded)
    await server._resume_expert_after_paused_dialogue(socket, runtime, checkpoint)
    assert resume.await_count == 2
    assert reloaded.load(saved["run_id"]) == checkpoint
