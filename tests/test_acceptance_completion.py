import asyncio
import copy
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock

from mewcode.expert_group import ExpertGroupOrchestrator, ExpertRoleResult
from mewcode.expert_runs import ExpertRunStore, is_resumable_snapshot
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.experts.human_gate import await_gate_decision, gate_scope_hash
from mewcode.experts.outcome import acceptance_recheck_nodes, effective_run_status
from tests.test_expert_flow import _profile, _pass_verdict, _needs_attention


def branch_flow(kind="audit"):
    return ExpertFlowSpec.from_value({
        "flow_id": "acceptance", "label": "交付验收", "finalizer_node_id": "done",
        "nodes": [{"node_id": "work", "role": "executor", "label": "实现"},
                  {"node_id": "audit", "role": "auditor", "label": "整包验收"},
                  {"node_id": "handoff", "role": "manager", "label": "人工交接"},
                  {"node_id": "done", "role": "manager", "label": "交付结果"}],
        "edges": [{"from": "work", "to": "audit"},
                  {"from": "audit", "to": "done", "condition": "pass"},
                  {"from": "audit", "to": "handoff", "condition": "fail"},
                  {"from": "handoff", "to": "done"}],
        "meta": {"node_kinds": {"audit": kind, "handoff": "gate", "done": "reduce"}},
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,status", [("audit", "needs_attention"), ("decision", "completed")])
async def test_summary_cannot_override_acceptance_but_decisions_can_branch(tmp_path, kind, status):
    started = []

    async def runner(node, *_args, **_kwargs):
        started.append(node.node_id)
        return ExpertRoleResult(text="NEEDS_ATTENTION" if node.node_id == "done" else "output",
                                verdict=_needs_attention() if node.node_id == "audit" else None)

    orchestrator = ExpertGroupOrchestrator(runner=runner, node_runner=runner, archive_root=tmp_path / "archive")
    result = await orchestrator.run("deliver", _profile(), {"providers": []}, strategy="stable", flow=branch_flow(kind))
    assert started == ["work", "audit", "handoff", "done"]
    assert result.status == status  # Never infer a verdict by parsing the summary prose.


def old_false_completion(tmp_path):
    runs = ExpertRunStore(tmp_path / "runs")
    run_id = "b" * 32
    runs.create(run_id=run_id, conversation_id="c", task="deliver", attachments=[], profile=_profile().as_dict(),
                strategy="stable", flow=branch_flow())
    runs.mutate(run_id, lambda s: s.update(status="completed", flow_state={"phase": "done", "status": "completed"},
        node_states={node: {"status": "completed", "passed": node != "audit", "text": "kept " + node,
                           **({"verdict": _needs_attention()} if node == "audit" else {})}
                     for node in ["work", "audit", "handoff", "done"]}))
    return runs, run_id


def test_old_false_completion_is_corrected_only_in_read_projection(tmp_path):
    runs, run_id = old_false_completion(tmp_path)
    disk = runs.root / (run_id + ".json")
    before = disk.read_bytes()
    assert runs.load(run_id)["status"] == "completed"
    for public in [runs.public(run_id), runs.public(run_id, role="work"), runs.public_for_conversation("c")[0]]:
        assert public["status"] == "needs_attention"
        assert public["recorded_status"] == "completed"
        assert public["unresolved_audit_node_ids"] == ["audit"]
        assert public["activated_at"] > 0
    assert is_resumable_snapshot(runs.load(run_id))
    assert runs.latest_resumable("c")["run_id"] == run_id
    assert disk.read_bytes() == before


def test_continue_cannot_silently_restart_finished_failed_acceptance(tmp_path):
    runs, run_id = old_false_completion(tmp_path)
    before = runs.load(run_id)
    with pytest.raises(ValueError, match="明确重试"):
        runs.resume(run_id, strategy="stable")
    assert runs.load(run_id) == before


def test_explicit_resume_rechecks_acceptance_not_successful_work(tmp_path):
    runs, run_id = old_false_completion(tmp_path)
    before = runs.load(run_id)
    resumed = runs.resume(run_id, strategy="stable", retry_node_id="audit")
    assert resumed["node_states"]["work"] == before["node_states"]["work"]
    for node in ("audit", "handoff", "done"):
        assert resumed["node_states"][node]["status"] == "pending"
        assert resumed["node_states"][node]["attempt_history"]
    assert resumed["stage_cursor"] == "audit"
    assert resumed["acceptance_rechecks"][0]["previous"]["done"] == before["node_states"]["done"]
    assert "acceptance_rechecks" not in runs.public(run_id)


def test_acceptance_recheck_never_partially_resets_a_downstream_loop():
    flow = branch_flow().as_dict()
    flow["meta"]["loops"] = [{"node_ids": ["handoff"]}]
    assert acceptance_recheck_nodes(flow, {"audit": {"status": "completed", "passed": False}}) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,approved", [({"choice": "continue"}, True), ({"choice": "pause"}, False), (None, False)])
async def test_gate_waits_for_current_human_response(tmp_path, answer, approved):
    runs, run_id = old_false_completion(tmp_path)
    session = SimpleNamespace(_human_oncall_request=None)
    events = []
    ready = asyncio.Event()

    async def emit(event):
        events.append(event)
        if event["type"] == "ask_user":
            session._ask_user_request.future.set_result({"requirements": "没有补充"})
        if event["type"] == "human_oncall":
            ready.set()

    flow = branch_flow()
    task = asyncio.create_task(await_gate_decision(node=flow.node_map["handoff"], flow=flow,
        session=session, store=runs, run_id=run_id, emit=emit))
    await asyncio.wait_for(ready.wait(), 2)
    assert not task.done() and session._human_oncall_request is not None
    session._human_oncall_request.future.set_result(answer)
    if answer is None:
        from mewcode.experts.waits import BranchWaitPaused
        with pytest.raises(BranchWaitPaused):
            await asyncio.wait_for(task, 2)
    else:
        assert await asyncio.wait_for(task, 2) is approved
    assert session._human_oncall_request is None
    confirmation = runs.load(run_id).get("gate_confirmations", {}).get("handoff")
    if approved:
        assert confirmation["scope_hash"] == gate_scope_hash(flow.node_map["handoff"], flow.meta)
    else:
        assert confirmation is None
    assert effective_run_status(runs.load(run_id)) == "needs_attention"


@pytest.mark.asyncio
@pytest.mark.parametrize("coordination_limit", [False, True])
@pytest.mark.parametrize("passes,answer,status", [(False, "continue", "needs_attention"),
                                                  (False, "pause", "blocked"),
                                                  (True, None, "completed")])
async def test_web_gate_and_acceptance_end_to_end(tmp_path, monkeypatch, passes, answer, status, coordination_limit):
    from mewcode import expert_web
    from mewcode.agent import StreamText, ErrorEvent
    from mewcode.conversation import ConversationManager
    from mewcode.experts.flow import ExpertFlowStore
    from mewcode.orchestration.plan_session import PlanSessionStore

    project = tmp_path / "project"
    project.mkdir()
    runs = ExpertRunStore(tmp_path / "runs")
    flows = ExpertFlowStore(tmp_path / "flows.json")
    flows.put(project, branch_flow())
    started, events = [], []

    class FakeAgent:
        client = SimpleNamespace(expose_reasoning=False)
        total_input_tokens = total_output_tokens = 1
        max_iterations = 2

        def set_review_authorization_context(self, context):
            self.node_id = context.split("STAGE: ", 1)[1].split(" ", 1)[0]

        async def run(self, conversation):
            started.append(self.node_id)
            if self.node_id == "done" and not passes:
                assert "本次确认不替代业务验收" in str(conversation.get_messages())
            conversation.add_assistant_message("output " + self.node_id)
            yield StreamText(text="output " + self.node_id)
            if self.node_id == "handoff" and coordination_limit:
                yield ErrorEvent("方案整理达到本段上限", kind="max_iterations")

    def build(*args):
        if args[-1] is not None:
            args[-1].submit(_pass_verdict() if passes else _needs_attention())
        return FakeAgent(), None, SimpleNamespace(conversation=ConversationManager(), aclose=AsyncMock())

    monkeypatch.setattr(expert_web, "_build_role_agent", build)
    durable = SimpleNamespace(meta=SimpleNamespace(message_count=0), append=lambda *args: None,
                              append_expert_run=lambda *args, **kwargs: None)
    session = SimpleNamespace(work_dir=str(project), conversation_id="c", multi_agent=False,
        session=durable, conversation=ConversationManager(), expert_run_id="", expert_control_action="pause",
        context=SimpleNamespace(get=lambda key, default=None: default, slots=SimpleNamespace(resolve=lambda key: {})),
        task_manager=SimpleNamespace(list_tasks=lambda: [], mark_notified=lambda ids: None),
        expert_child_event_callback=None, status_payload=lambda: {}, tasks_payload=lambda: [])

    async def send(event):
        events.append(event)
        if event["type"] == "ask_user":
            raise AssertionError("Intermediate results must not require generic user input")
        if event["type"] == "human_oncall":
            assert not passes and "done" not in started
            assert not session._human_oncall_request.future.done()
            session._human_oncall_request.future.set_result({"choice": answer})

    kwargs = dict(session=session,
        registry_service=SimpleNamespace(catalog=AsyncMock(return_value={"providers": []}), resolve=lambda model: None),
        profile_store=SimpleNamespace(get=lambda p: _profile()), flow_store=flows,
        plan_store=PlanSessionStore(tmp_path / "plans.json"), run_store=runs)
    await asyncio.wait_for(expert_web.run_expert_chat(SimpleNamespace(send_json=send), "交付验收", confirmed_new_flow=flows.get(project), **kwargs), 10)
    snapshot = runs.load(next(event["run_id"] for event in events if event.get("run_id")))
    assert snapshot["status"] == status
    assert started == (["work", "audit", "handoff"] if answer == "pause" else
                       ["work", "audit", "done"] if passes else ["work", "audit", "handoff", "done"])
    assert sum(event["type"] == "human_oncall" for event in events) == (0 if passes else 1)
    assert not any(event["type"] == "recovery_case" for event in events)
    if not passes and answer == "continue":
        started.clear()
        passes = True
        await asyncio.wait_for(expert_web.run_expert_chat(SimpleNamespace(send_json=send), "继续",
            resume_run_id=snapshot["run_id"], retry_node_id="audit", **kwargs), 10)
        assert started == ["audit", "done"]
        assert runs.load(snapshot["run_id"])["status"] == "completed"
