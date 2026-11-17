import copy
from types import SimpleNamespace

import httpx
import pytest

from frontend import server
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.model_route_recovery import (
    USER_DIFFICULT_ROUTE, authorized_difficulty_route, bind_declared_backup,
)
from mewcode.model_registry import ModelRef
from tests.test_capability_executor_slot import profile_value


TARGET = ModelRef("chosen-provider", "gpt-6.1-sol", "xhigh")


@pytest.fixture
def run_env(tmp_path):
    store, run_id = ExpertRunStore(tmp_path / "runs"), "f" * 32
    flow = {"flow_id": "difficulty", "finalizer_node_id": "auditor",
            "nodes": [{"node_id": role, "role": role, "label": role}
                      for role in ("manager", "executor", "auditor")],
            "edges": [{"from_node": "manager", "to_node": "executor"},
                      {"from_node": "executor", "to_node": "auditor"}]}
    store.create(run_id=run_id, conversation_id="owner", task="original task", attachments=[],
                 profile=profile_value(), strategy="stable", flow=flow)
    store.mutate(run_id, lambda s: s.update(
        node_states={"manager": {"status": "needs_attention", "handoff": {"blocker_kind": "capability_missing"}}},
        gate_confirmations={"gate": {"answer": None}}, loop_states={"loop": {"tokens": 91}},
        recovery_checkpoints={"manager": {"retry_same_checkpoint": 1}}, audit_protocols={"audit": {"round": 2}}))
    store.transition(run_id, "paused", reason="capability missing")
    return store, run_id


class Registry:
    def resolve(self, ref):
        return SimpleNamespace(ref=ref, effort=ref.effort, capabilities=("chat", "tools", "vision"))


def configure(store, run_id, **overrides):
    values = dict(conversation_id="owner", model=TARGET.as_dict(),
        node_ids=["manager", "executor"], apply_node_ids=["manager"],
        expected_updated_at=store.load(run_id)["updated_at"], command_id="difficulty-1",
        reason="用户要求困难节点切换 GPT-6.1 SOL 极高")
    values.update(overrides)
    return store.set_difficult_node_model(run_id, **values)


def test_explicit_paused_upgrade_is_durable_and_preserves_blockers_progress_and_contract(run_env):
    store, run_id = run_env
    original = store.load(run_id)
    changed = configure(store, run_id)
    assert changed["locked_models"]["manager"] == TARGET.as_dict()
    assert changed["status"] == "paused"
    for field in ("node_states", "locked_flow", "locked_profile", "gate_confirmations", "loop_states",
                  "audit_protocols", "recovery_checkpoints", "flow_fingerprint", "attempt", "usage"):
        assert changed.get(field) == original.get(field), field
    assert configure(store, run_id, expected_updated_at=original["updated_at"]) == changed
    assert ExpertRunStore(store.root).load(run_id)["locked_models"]["manager"] == TARGET.as_dict()
    assert len(changed["difficult_node_model_changes"]) == 1


@pytest.mark.parametrize("overrides", [
    {"conversation_id": "another"}, {"expected_updated_at": "stale"},
    {"apply_node_ids": ["unknown"]}, {"node_ids": ["manager"], "apply_node_ids": ["executor"]},
])
def test_upgrade_rejects_stale_foreign_and_out_of_scope_requests_without_mutation(run_env, overrides):
    store, run_id = run_env
    original = store.load(run_id)
    with pytest.raises(ValueError):
        configure(store, run_id, **overrides)
    assert store.load(run_id) == original


def test_upgrade_does_not_rebind_completed_nodes(run_env):
    store, run_id = run_env
    store.mutate(run_id, lambda s: s["node_states"]["manager"].update(status="completed"))
    original = store.load(run_id)
    with pytest.raises(ValueError):
        configure(store, run_id)
    assert store.load(run_id) == original


def test_difficult_route_requires_current_host_authorization_and_exact_capabilities(run_env):
    store, run_id = run_env
    snapshot = configure(store, run_id, apply_node_ids=[])
    node = SimpleNamespace(node_id="executor")
    current = ModelRef("old", "different-model", "high")
    target, reason = authorized_difficulty_route(snapshot, node, current, Registry())
    assert target == TARGET and reason == USER_DIFFICULT_ROUTE
    for field, value in (("authorized_by", "model"), ("flow_fingerprint", "stale")):
        altered = copy.deepcopy(snapshot)
        altered["difficult_node_model_policy"][field] = value
        assert authorized_difficulty_route(altered, node, current, Registry())[0] is None

    class Downgraded(Registry):
        def resolve(self, ref):
            value = super().resolve(ref)
            if ref == TARGET:
                value.effort = "high"
                value.ref = ModelRef(TARGET.provider_key, TARGET.model_id, "high")
            return value
    assert authorized_difficulty_route(snapshot, node, current, Downgraded())[0] is None
    assert authorized_difficulty_route(snapshot, node, TARGET, Registry())[0] is None


def test_automatic_route_switch_is_bound_to_user_choice_and_input(run_env):
    store, run_id = run_env
    snapshot = configure(store, run_id, apply_node_ids=[])
    current = ModelRef("old", "different-model", "high")
    snapshot["locked_models"] = {"executor": current.as_dict()}
    snapshot["node_states"]["executor"] = {"input_revision": "v1", "tools": {}}
    receipt = bind_declared_backup(snapshot, node_id="executor", role="executor", expected_current=current,
        backup=TARGET, input_revision="v1", flow_fingerprint=snapshot["flow_fingerprint"],
        receipt_id="one-switch", reason=USER_DIFFICULT_ROUTE)
    assert receipt["reason"] == USER_DIFFICULT_ROUTE
    assert snapshot["locked_models"]["executor"] == TARGET.as_dict()


def test_resume_preflight_validates_actual_override_and_reports_invalid_saved_binding(run_env):
    from mewcode.expert_web import expert_flow_model_error
    store, run_id = run_env
    flow = store.load(run_id)["locked_flow"]
    flow["nodes"][0]["model"] = {"provider_key": "unavailable", "model_id": "old"}
    class ExactRegistry(Registry):
        def resolve(self, ref):
            if ref != TARGET:
                raise ValueError("unavailable old provider")
            return super().resolve(ref)
    assert expert_flow_model_error(ExactRegistry(), flow, node_ids={"manager"},
                                   locked_models={"manager": TARGET.as_dict()}) == ""
    error = expert_flow_model_error(ExactRegistry(), flow, node_ids={"manager"},
                                   locked_models={"manager": {"model_id": "missing-provider"}})
    assert "manager" in error and "Provider" in error


@pytest.mark.parametrize("kind", ["vision", "unavailable", "effort"])
def test_user_selected_difficult_model_never_loses_capabilities_or_silently_falls_back(run_env, kind):
    store, run_id = run_env
    snapshot = configure(store, run_id, apply_node_ids=[])
    class Restricted(Registry):
        def resolve(self, ref):
            if ref == TARGET and kind == "unavailable":
                raise ValueError("chosen provider unavailable")
            value = super().resolve(ref)
            if ref == TARGET and kind == "vision":
                value.capabilities = ("chat", "tools")
            if ref == TARGET and kind == "effort":
                value.effort = "high"
            return value
    original = copy.deepcopy(snapshot)
    chosen, reason = authorized_difficulty_route(snapshot, SimpleNamespace(node_id="executor"),
        ModelRef("old", "vision-model", "high"), Restricted())
    assert chosen is None and reason
    assert snapshot == original


@pytest.mark.asyncio
async def test_api_uses_exact_route_without_discovery_call_or_implicit_resume(run_env, tmp_path, monkeypatch):
    store, run_id = run_env
    project = str(tmp_path / "project")
    monkeypatch.setattr(server, "SESSION", SimpleNamespace(work_dir=project,
        session=SimpleNamespace(session_id="owner"), chat_busy=lambda cid: False))
    monkeypatch.setattr(server, "EXPERT_RUNS", store)
    monkeypatch.setattr(server, "MODEL_REGISTRY", Registry())
    async def emit(event):
        assert event["snapshot"]["status"] == "paused"
    monkeypatch.setattr(server.WS_EVENTS, "send_json", emit)
    body = dict(conversation_id="owner", project=project, model=TARGET.as_dict(),
        node_ids=["manager", "executor"], apply_node_ids=["manager"],
        expected_updated_at=store.load(run_id)["updated_at"], command_id="api-difficulty")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.post(f"/api/expert-runs/{run_id}/difficult-node-model", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["snapshot"]["difficult_node_model"] == TARGET.as_dict()
    assert store.load(run_id)["locked_models"]["manager"] == TARGET.as_dict()
    assert store.load(run_id)["locked_profile"] == profile_value()


def test_node_only_change_preserves_existing_backup_policy_and_blocks_unknown_calls(run_env):
    store, run_id = run_env
    previous = configure(store, run_id, apply_node_ids=[])
    changed = configure(store, run_id, node_ids=['manager'], apply_node_ids=['manager'],
        command_id='node-only', record_backup=False)
    assert changed['difficult_node_model_policy'] == previous['difficult_node_model_policy']
    assert changed['locked_models']['manager'] == TARGET.as_dict()
    store.mutate(run_id, lambda s: s.update(kernel_calls={'unfinished': {'node_id':'executor', 'state':'unknown'}}))
    before = store.load(run_id)
    with pytest.raises(ValueError, match='未结算'):
        configure(store, run_id, node_ids=['executor'], apply_node_ids=['executor'], command_id='unsettled', record_backup=False)
    assert store.load(run_id) == before


def test_expanded_node_binding_preserves_certificates_gates_and_lineage_usage(tmp_path):
    from tests.test_autonomous_publication import prepared
    from tests.test_collaboration_mode import RUN_ID, accept
    store, record, _, publication = prepared(tmp_path, publish=True)
    children = publication['fragment']['child_ids']
    accept(record['collaboration'], children[1])
    store.mutate(RUN_ID, lambda s: s.update(record), durable=True)
    store.transition(RUN_ID, 'paused', reason='用户暂停换绑')
    before = store.load(RUN_ID)
    changed = configure(store, RUN_ID, conversation_id='conversation', node_ids=[children[0]],
        apply_node_ids=[children[0]], record_backup=False)
    assert changed['locked_models'][children[0]] == TARGET.as_dict()
    assert changed['collaboration'] == before['collaboration']
    assert changed['status'] == 'paused' and changed['locked_flow'] == before['locked_flow']
    assert changed['collaboration_model_changes'][-1]['source'] == 'user'
    for identity, message in [(children[1], '已完成'), ('work', '汇合')]:
        saved = store.load(RUN_ID)
        with pytest.raises(ValueError, match=message):
            configure(store, RUN_ID, conversation_id='conversation', node_ids=[identity],
                apply_node_ids=[identity], command_id='blocked-'+identity, record_backup=False)
        assert store.load(RUN_ID) == saved


@pytest.mark.asyncio
async def test_node_model_api_reads_exact_binding_and_can_replace_unavailable_old_provider(run_env, tmp_path, monkeypatch):
    store, run_id = run_env
    project = str(tmp_path / 'project')
    old = ModelRef('missing-old-provider', 'old-model', 'high')
    store.mutate(run_id, lambda s: s.update(locked_models={'manager':old.as_dict()}))
    monkeypatch.setattr(server, 'SESSION', SimpleNamespace(work_dir=project,
        session=SimpleNamespace(session_id='owner'), chat_busy=lambda cid: False))
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    class OnlyChosen(Registry):
        def resolve(self, ref):
            if ref != TARGET:
                raise ValueError('旧模型不可用')
            return super().resolve(ref)
    monkeypatch.setattr(server, 'MODEL_REGISTRY', OnlyChosen())
    async def emit(event):
        assert event['snapshot']['status'] == 'paused'
    monkeypatch.setattr(server.WS_EVENTS, 'send_json', emit)
    scope = dict(project=project, conversation_id='owner', node_id='manager')
    before = store.load(run_id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        read = await client.get(f'/api/expert-runs/{run_id}/node-model', params=scope)
        assert read.status_code == 200, read.text
        assert read.json()['model'] == old.as_dict() and read.json()['editable'] is True
        assert store.load(run_id) == before
        foreign = await client.get(f'/api/expert-runs/{run_id}/node-model', params={**scope,'conversation_id':'foreign'})
        assert foreign.status_code == 409
        body = dict(project=project, conversation_id='owner', model=TARGET.as_dict(), node_ids=['manager'],
            apply_node_ids=['manager'], record_backup=False, expected_updated_at=read.json()['updated_at'], command_id='node-choice')
        response = await client.post(f'/api/expert-runs/{run_id}/difficult-node-model', json=body)
        assert response.status_code == 200, response.text
        assert (await client.get(f'/api/expert-runs/{run_id}/node-model', params=scope)).json()['model'] == TARGET.as_dict()
    changed = store.load(run_id)
    assert not changed.get('difficult_node_model_policy')
    for key in ('node_states', 'locked_flow', 'locked_profile', 'attempt', 'usage', 'status'):
        assert changed.get(key) == before.get(key)


def test_unavailable_current_is_only_ignored_for_explicit_user_replacement():
    from mewcode.experts.model_route_recovery import validate_difficulty_model
    class OnlyChosen(Registry):
        def resolve(self, ref):
            if ref != TARGET:
                raise ValueError('旧模型不可用')
            return super().resolve(ref)
    old = ModelRef('missing', 'old')
    with pytest.raises(ValueError, match='旧模型不可用'):
        validate_difficulty_model(OnlyChosen(), TARGET, old)
    validate_difficulty_model(OnlyChosen(), TARGET, old, allow_unavailable_current=True)
