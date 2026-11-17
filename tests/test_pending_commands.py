import copy
from dataclasses import replace

import pytest

from mewcode.expert_group import ExpertGroupOrchestrator, ExpertRoleResult
from mewcode.experts.command_receipts import request_receipts
from mewcode.experts.pending_commands import restore_pending_commands
from mewcode.experts.stage_context import input_revision
from mewcode.model_registry import ModelRef
from tests.test_command_request_receipts import blocked_result, command_request, verification
from tests.test_expert_flow import _profile
from tests.test_file_escalation import grok_catalog
from tests.test_retry_memory import retry_flow


def empty_request_result():
    result = blocked_result()
    result.handoff.update(command_requests=[], blockers=["原探测尚未启动，等待宿主新回执"])
    return result


@pytest.mark.parametrize("case", ["safe", "unknown", "completed", "stale", "question", "denied",
                                  "tampered", "new_request", "later_unknown", "model_claim"])
def test_restore_requires_current_host_unstarted_identity(case):
    rows = request_receipts("work", "current", [command_request()], status="not_started")
    result = empty_request_result()
    if case in {"unknown", "completed"}:
        rows[0]["status"] = "outcome_unknown" if case == "unknown" else case
    elif case == "stale":
        rows[0]["input_revision"] = "old"
    elif case == "question":
        result.handoff["question"] = "等待授权"
    elif case == "denied":
        result = replace(result, error_kind="permission_denied")
    elif case == "tampered":
        rows[0]["request"]["command"] = "changed-command"
    elif case == "new_request":
        result.handoff["command_requests"] = [command_request("new-command")]
    elif case == "later_unknown":
        rows.append({**rows[0], "request_id": "verification-id", "base_request_id": rows[0]["request_id"],
                     "status": "outcome_unknown"})
    elif case == "model_claim":
        rows[0]["evidence_source"] = "model"
    before = copy.deepcopy((rows, result))
    actual = restore_pending_commands("work", "current", rows, result)
    assert (rows, result) == before
    if case == "safe":
        assert actual.handoff["command_requests"] == [command_request()]
    else:
        assert actual is result


@pytest.mark.asyncio
@pytest.mark.parametrize("admitted", [True, False])
@pytest.mark.parametrize("error_kind", ["capability_missing", "environment_fault"])
async def test_resume_rehydrates_event_then_dispatches_without_model_resubmission(tmp_path, admitted, error_kind):
    flow = retry_flow()
    flow = replace(flow, nodes=tuple(replace(node, role="planner") if node.node_id == "work" else node
                                     for node in flow.nodes))
    profile = replace(_profile(), capability_executor=ModelRef("desktop:grok-main", "grok-4.6", "high"))
    revision = input_revision(sources={}, flow=flow, node=flow.nodes[0], model=profile.planner)
    rows = request_receipts("work", revision, [command_request()], status="not_started")
    rows[0].update(evidence_source="host_execution", dispatch_id="original-dispatch", tool_id="original-tool")
    checkpoint = {"node_states": {"work": {"input_revision": revision, "status": "needs_attention",
        "command_receipts": [], "handoff": empty_request_result().handoff}},
        "events": [{"node_id": "work", "command_receipts": rows}]}
    before = copy.deepcopy(checkpoint)
    calls = []

    async def runner(node, model, prompt, policy, member=None):
        calls.append(node.node_id)
        if node.node_id == "work__command_probe":
            assert policy.command_allowlist == (command_request()["command"],)
            return ExpertRoleResult("receipt", status="completed", handoff={"status": "completed"},
                                    verification_records=[verification()])
        if node.node_id == "work" and calls.count("work") == 1:
            return replace(empty_request_result(), error_kind=error_kind)
        if node.node_id == "work":
            assert policy.stage_inputs["work__command_probe"]["handoff"]["command_receipts"][0]["exit_code"] == 0
        return ExpertRoleResult("done", status="completed", handoff={"status": "completed"})

    orchestrator = ExpertGroupOrchestrator(runner=None, node_runner=runner, work_dir=tmp_path,
        archive_root=tmp_path, command_admission=lambda _: None if admitted else {"allowed": False, "reason": "gate closed"})
    actual = await orchestrator.run("synthetic", profile, grok_catalog(), strategy="stable", flow=flow,
                                    checkpoint=checkpoint)
    assert checkpoint == before
    assert calls == (["work", "work__command_probe", "work", "done"] if admitted else ["work"])
    assert (actual.status == "completed") is admitted


@pytest.mark.parametrize("status", ["completed", "outcome_unknown", "in_flight"])
@pytest.mark.parametrize("stage_has_refusal", [False, True])
def test_event_rehydration_cannot_erase_previous_effect_with_later_refusal(status, stage_has_refusal):
    from mewcode.experts.command_receipts import reconcile_run_command_receipts
    protected = request_receipts("work", "current", [command_request()], status=status)
    refused = request_receipts("work", "current", [command_request()], status="not_started")
    snapshot = {"node_states": {"work": {"input_revision": "current",
        "command_receipts": refused if stage_has_refusal else []}}, "events": [
            {"node_id": "work", "command_receipts": protected},
            {"node_id": "work", "command_receipts": refused}]}
    reconcile_run_command_receipts(snapshot)
    rows = snapshot["node_states"]["work"]["command_receipts"]
    assert rows[0]["status"] == status
    result = empty_request_result()
    assert restore_pending_commands("work", "current", rows, result) is result


@pytest.mark.asyncio
async def test_web_terminal_stage_does_not_overwrite_host_receipts(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from mewcode import expert_web
    from mewcode.conversation import ConversationManager
    from mewcode.expert_group import ExpertRunResult
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.experts.flow import ExpertFlowStore
    from mewcode.orchestration.plan_session import PlanSessionStore

    rows = request_receipts("work", "current", [command_request()], status="completed")
    rows[0].update(command_started=True, exit_code=0, successful=True, evidence_source="host_execution")

    class FakeOrchestrator:
        def __init__(self, **kwargs):
            self.emit = kwargs["event_sink"]

        async def run(self, *args, **kwargs):
            base = {"run_id": kwargs["run_id"], "node_id": "work", "role": "planner",
                    "status": "needs_attention", "input_revision": "current"}
            await self.emit({**base, "type": "expert_node_completed", "command_receipts": rows})
            await self.emit({**base, "type": "expert_stage", "command_receipts": []})
            await self.emit({**base, "type": "expert_stage", "command_receipts": request_receipts(
                "work", "current", [command_request()], status="not_dispatched")})
            return ExpertRunResult(kwargs["run_id"], "needs_attention", "stable", "", "", "", "")

    monkeypatch.setattr(expert_web, "ExpertGroupOrchestrator", FakeOrchestrator)
    project = tmp_path / "project"
    project.mkdir()
    flow = retry_flow()
    runs, flows = ExpertRunStore(tmp_path / "runs"), ExpertFlowStore(tmp_path / "flows.json")
    flows.put(project, flow)
    events = []
    session = SimpleNamespace(work_dir=str(project), conversation_id="receipt-test", multi_agent=False,
        session=SimpleNamespace(meta=SimpleNamespace(message_count=0), append=lambda *args: None,
                                append_expert_run=lambda *args, **kwargs: None),
        conversation=ConversationManager(), expert_run_id="", expert_control_action="pause",
        context=SimpleNamespace(get=lambda key, default=None: default, slots=SimpleNamespace(resolve=lambda key: {})),
        task_manager=SimpleNamespace(list_tasks=lambda: [], mark_notified=lambda ids: None),
        expert_child_event_callback=None, status_payload=lambda: {}, tasks_payload=lambda: [])
    await expert_web.run_expert_chat(SimpleNamespace(send_json=AsyncMock(side_effect=events.append)),
        "synthetic", session=session, confirmed_new_flow=flow,
        registry_service=SimpleNamespace(catalog=AsyncMock(return_value=grok_catalog()), resolve=lambda _: None),
        profile_store=SimpleNamespace(get=lambda _: _profile()), flow_store=flows,
        plan_store=PlanSessionStore(tmp_path / "plans.json"), run_store=runs)
    assert not [event for event in events if event.get("type") == "error"]
    snapshot = runs.load(next(event["run_id"] for event in events if event.get("run_id")))
    assert snapshot["node_states"]["work"]["command_receipts"] == rows
