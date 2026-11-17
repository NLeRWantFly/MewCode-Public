"""A new extracted graph must not reactivate the old process in its conversation."""
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from frontend import server
from mewcode import expert_web
from mewcode.conversation import ConversationManager
from mewcode.agent import StreamText
from mewcode.expert_runs import ExpertRunStore
from mewcode.orchestration import OrchestrationSpec
from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.revisions import can_resume_confirmed_flow
from tests.test_expert_flow import _profile
from tests.test_orchestration_confirmation_recovery import confirm
from tests.test_orchestration_start_run import confirm_env


OLD_IDS = ["inventory_draft", "prepare_templates", "rewrite_contributions", "confirm_contributions",
           "mock_review", "revise_abstract_intro", "confirm_abstract_intro", "revise_related_work",
           "confirm_related_work", "revise_method", "confirm_method", "revise_experiments", "stage_ws0kln"]
NEW_IDS = ["inspect_handoff", "collect_evidence", "check_interfaces", "confirm_scope", "revise_methods",
           "revise_experiments", "repair_figure_five", "assemble_package", "audit_package", "human_handoff", "summarize"]


def graph(ids, version):
    return OrchestrationSpec.from_value({
        "spec_id": "orchestration", "label": "当前阶段图", "version": version,
        "goal": f"按 v{version} 当前阶段图执行", "require_confirm_gate": False,
        "nodes": [{"node_id": nid, "label": nid, "kind": "reduce" if i == len(ids)-1 else "implement",
                   "model": {"provider_key": "p", "model_id": "m-test"}, "goal": nid}
                  for i, nid in enumerate(ids)],
        "edges": [{"from_node": left, "to_node": right} for left, right in zip(ids, ids[1:])],
        "finalizer_node_id": ids[-1],
    })


def old_checkpoint(store):
    run_id = "a" * 32
    store.create(run_id=run_id, conversation_id="confirm-test", task="original task", attachments=[],
                 profile=_profile().as_dict(), strategy="stable", flow=to_expert_flow(graph(OLD_IDS, 107)))
    return store.mutate(run_id, lambda s: s.update(
        status="blocked", stage_cursor="stage_ws0kln",
        node_states={nid: {"status": "completed", "text": "retained artifact"} for nid in OLD_IDS[:-1]}
                    | {"stage_ws0kln": {"status": "budget_exhausted"}},
    ))


def test_default_flow_id_and_overlapping_node_are_not_enough_to_resume(tmp_path):
    runs = ExpertRunStore(tmp_path / "runs")
    old = old_checkpoint(runs)
    assert can_resume_confirmed_flow(old, to_expert_flow(graph(OLD_IDS, 108)))
    assert not can_resume_confirmed_flow(old, to_expert_flow(graph(NEW_IDS, 138)))
    # Removing only the interrupted node is also a different continuation.
    assert not can_resume_confirmed_flow(old, to_expert_flow(graph(OLD_IDS[:-1], 109)))
    before = runs.load(old["run_id"])
    with pytest.raises(ValueError, match="保护进度"):
        runs.resume(old["run_id"], strategy="stable", flow=to_expert_flow(graph(NEW_IDS, 138)))
    assert runs.load(old["run_id"]) == before


def acceptance_checkpoint():
    from tests.test_collaboration_mode import flow
    spec = flow()
    review_id = "work__acceptance_012345abcdef"
    snapshot = {"locked_flow": spec.as_dict(), "node_states": {
        "work": {"status": "completed"}, review_id: {"status": "failed"}},
        "stage_cursor": review_id,
        "collaboration": {"format": "mewcode.collaboration.v2", "calls": {
            "host-review": {"kind": "acceptance", "status": "finished",
                            "result": {"recovery_node_id": review_id, "passed": False}}}}}
    return snapshot, spec, review_id


def test_host_acceptance_stage_resumes_its_owning_business_node():
    snapshot, spec, _ = acceptance_checkpoint()
    before = copy.deepcopy(snapshot)
    assert can_resume_confirmed_flow(snapshot, spec)
    assert snapshot == before


@pytest.mark.parametrize("change", ["missing_receipt", "wrong_kind", "lookalike", "missing_parent", "real_node"])
def test_acceptance_stage_mapping_does_not_hide_removed_or_unproven_nodes(change):
    snapshot, spec, review_id = acceptance_checkpoint()
    current = spec.as_dict()
    if change == "missing_receipt":
        snapshot["collaboration"]["calls"].clear()
    elif change == "wrong_kind":
        snapshot["collaboration"]["calls"]["host-review"]["kind"] = "semantic_review"
    elif change == "lookalike":
        snapshot["node_states"]["work__acceptance_unproven"] = {"status": "failed"}
    elif change == "missing_parent":
        current["nodes"] = [node for node in current["nodes"] if node["node_id"] != "work"]
        current["edges"] = []
    else:
        # Even a name matching the helper convention remains a real business
        # node if it was declared in the locked graph.
        snapshot["locked_flow"]["nodes"].append({**snapshot["locked_flow"]["nodes"][0], "node_id": review_id})
    assert not can_resume_confirmed_flow(snapshot, current)


@pytest.mark.asyncio
async def test_confirm_replacement_executes_only_new_nodes_and_preserves_old_bytes(confirm_env, tmp_path, monkeypatch):
    runs = ExpertRunStore(tmp_path / "runs")
    old = old_checkpoint(runs)
    original_bytes = (runs.root / f'{old["run_id"]}.json').read_bytes()
    monkeypatch.setattr(server, "EXPERT_RUNS", runs)
    calls, events = [], []

    class FakeAgent:
        client = SimpleNamespace(expose_reasoning=False)
        total_input_tokens = 1
        total_output_tokens = 1

        def set_review_authorization_context(self, _context):
            pass

        async def run(self, conversation):
            calls.append(copy.deepcopy(conversation.history))
            yield StreamText(text="completed fixture node")

    permission_ids = []

    def build_role(runtime, *_args):
        from mewcode.sandbox.lpac_runtime import NativeScope
        checker = expert_web._expert_permission_checker(runtime, runtime.work_dir)
        scope = NativeScope.from_checker(checker)
        assert scope.task_id and scope.current()
        permission_ids.append(scope.task_id)
        return FakeAgent(), None, SimpleNamespace(conversation=ConversationManager(), aclose=AsyncMock())

    monkeypatch.setattr(expert_web, "_build_role_agent", build_role)
    durable = SimpleNamespace(meta=SimpleNamespace(message_count=0), append=lambda m: None,
                              append_expert_run=lambda *a, **kw: None)
    session = SimpleNamespace(work_dir=server.SESSION.work_dir, conversation_id="confirm-test", multi_agent=False,
        session=durable, conversation=ConversationManager(),
        context=SimpleNamespace(get=lambda key, default=None: default, slots=SimpleNamespace(resolve=lambda key: {})),
        task_manager=SimpleNamespace(list_tasks=lambda: [], mark_notified=lambda ids: None),
        expert_run_id="", expert_control_action="pause", expert_child_event_callback=None,
        status_payload=lambda: {}, tasks_payload=lambda: [])
    from mewcode.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine
    parent_checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(session.work_dir),
                                       RuleEngine(), PermissionMode.PLAN)
    session.agent = SimpleNamespace(permission_checker=parent_checker, permission_mode=PermissionMode.PLAN)
    socket = SimpleNamespace(send_json=AsyncMock(side_effect=lambda e: events.append(e)))

    async def start(runtime, text, *, resume_run_id="", confirmed_new_flow=None):
        assert not resume_run_id and confirmed_new_flow.version == 138
        await expert_web.run_expert_chat(socket, text, session=session, run_store=runs,
            registry_service=SimpleNamespace(
                catalog=AsyncMock(return_value={"providers": []}),
                resolve=lambda _ref: SimpleNamespace(),
            ),
            profile_store=SimpleNamespace(get=lambda p: _profile()),
            plan_store=server.get_plan_session_store(), confirmed_new_flow=confirmed_new_flow)

    monkeypatch.setattr(server, "start_runtime_turn", start)
    response = await confirm(graph(NEW_IDS, 138).as_dict())
    assert response.status_code == 200, response.text
    assert response.json()["execution_mode"] == "new_plan"
    assert response.json()["preserved_run_id"] == old["run_id"]
    assert not response.json()["resume_run_id"]
    assert len(calls) == len(NEW_IDS)
    assert len(permission_ids) == len(NEW_IDS) and len(set(permission_ids)) == 1
    assert parent_checker.task_id == permission_ids[0] and parent_checker.task_authorization is None
    started_nodes = [e["node_id"] for e in events if e["type"] == "expert_node_started"]
    assert started_nodes == NEW_IDS
    assert (runs.root / f'{old["run_id"]}.json').read_bytes() == original_bytes
    new_id = next(e["run_id"] for e in events if e["type"] == "done")
    assert new_id != old["run_id"]
    assert runs.load(new_id)["status"] == "completed"
    assert set(runs.load(new_id)["node_states"]) == set(NEW_IDS)
    assert runs.latest_resumable("confirm-test") is None  # Do not resurrect v107 after v138 finishes.


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["preflight", "launch"])
async def test_failed_confirmation_does_not_leave_execution_mode_enabled(confirm_env, monkeypatch, failure):
    monkeypatch.setattr(server.SESSION, "expert_mode", False)
    monkeypatch.setattr(server.SESSION, "orchestration_planning", True)
    monkeypatch.setattr(server.SESSION, "permission_mode", server.PermissionMode.PLAN)
    confirm_env.runtime.expert_mode = False

    def fail_capture():
        raise RuntimeError("fixture preflight failure")

    async def fail_start(*args, **kwargs):
        raise ValueError("fixture launch failure")

    if failure == "preflight":
        monkeypatch.setattr(server.SESSION, "capture_conversation_runtime", fail_capture)
    else:
        monkeypatch.setattr(server, "start_runtime_turn", fail_start)
    response = await confirm(graph(NEW_IDS, 138).as_dict())
    assert response.status_code == 400
    assert not server.SESSION.expert_mode and server.SESSION.orchestration_planning
    assert not confirm_env.runtime.expert_mode


def test_latest_task_selection_does_not_revive_older_blocked_tasks(tmp_path):
    runs = ExpertRunStore(tmp_path / "runs")
    old = old_checkpoint(runs)
    new_id = "b" * 32
    runs.create(run_id=new_id, conversation_id="confirm-test", task="new task", attachments=[],
                profile=_profile().as_dict(), strategy="stable", flow=to_expert_flow(graph(NEW_IDS, 138)))
    runs.transition(new_id, "paused")
    assert runs.latest_resumable("confirm-test")["run_id"] == new_id
    runs.transition(new_id, "completed")
    # A bookkeeping update on old history must not make it the selected task.
    runs.mutate(old["run_id"], lambda s: s.update(status_reason="history bookkeeping"))
    assert runs.latest_resumable("confirm-test") is None
    # Explicitly selecting the old history by ID is still supported.
    runs.resume(old["run_id"], strategy="stable")
    runs.transition(old["run_id"], "paused")
    assert runs.latest_resumable("confirm-test")["run_id"] == old["run_id"]
