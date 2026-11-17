import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.expert_group import ExpertExecutionPolicy
from mewcode.experts.command_receipts import observe_requests, persist_command_receipts, reconcile_helper_receipts, reconcile_run_command_receipts, request_receipts
from mewcode.experts.file_escalation import resolve_capability_requests
from mewcode.experts.flow import ExpertNodeSpec
from mewcode.experts.review_context import freeze_review_source, read_review_source, source_reference
from mewcode.permissions.reviewer import SafetyReviewer
from tests.test_command_request_receipts import blocked_result, command_request, verification


def interrupted():
    return {"name": "Bash", "tool_id": "pending", "args": {"command": "python --version"},
            "status": "interrupted_before_execution"}


def test_only_bound_host_interruption_can_correct_unknown():
    rows = request_receipts("N03", "rev", [command_request()], status="outcome_unknown")
    rows[0]["dispatch_id"] = "dispatch-a"
    value = {"node_states": {"N03": {"command_receipts": copy.deepcopy(rows)}}}
    observed = observe_requests(rows, [interrupted()])
    persist_command_receipts(value, {"node_id": "N03", "command_receipts": observed})
    assert value["node_states"]["N03"]["command_receipts"][0]["status"] == "not_started"
    helper = {"input_revision": "rev", "command_dispatch_id": "dispatch-a", "tools": {"pending": interrupted()}}
    assert reconcile_helper_receipts(rows, helper)[0]["status"] == "not_started"
    helper["command_dispatch_id"] = "another"
    assert reconcile_helper_receipts(rows, helper) == rows
    assert observe_requests(rows, [verification(status="timed_out", exit_code=None), interrupted()])[0]["status"] == "outcome_unknown"
    from mewcode.experts.attempt_memory import collect_verification_records
    records = collect_verification_records({"started": verification(status="timed_out", exit_code=None),
                                            "pending": interrupted()})
    assert observe_requests(rows, records)[0]["status"] == "outcome_unknown"


@pytest.mark.asyncio
async def test_cancelled_helper_preserves_proven_unstarted_receipt(tmp_path):
    events = []
    async def dispatch(policy, prompt):
        assert policy.timeout_seconds == 480  # no accidental 120s whole-agent deadline
        policy.command_receipt_context["records"] = [interrupted()]
        raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await resolve_capability_requests(ExpertNodeSpec("N03", "executor", "N03"), blocked_result(),
            ExpertExecutionPolicy("stable", False, 0, timeout_seconds=480), work_dir=tmp_path,
            dispatch=dispatch, emit=AsyncMock(side_effect=lambda event: events.append(copy.deepcopy(event))))
    assert events[-1]["command_receipts"][0]["status"] == "not_started"
    assert events[-1]["command_receipts"][0]["dispatch_id"] == events[0]["command_receipts"][0]["dispatch_id"]


@pytest.mark.asyncio
async def test_reviewer_reads_real_frozen_source_after_compressed_evidence_gap(tmp_path):
    request = command_request()
    full_prompt = "背景" * 8000 + json.dumps(request) + "禁止安装依赖"
    ref, source = freeze_review_source(tmp_path, overall_task="只读探测", stage_prompt=full_prompt,
        node_id="N03__command_probe", role="executor", command_context={"source_node_id": "N03", "requests": [request]})
    context = source_reference(ref, source)
    assert request["command"] in context and all(item in context for item in request["input_scope"])
    assert source["stage_prompt"] == json.loads(read_review_source(ref))["stage_prompt"]
    reviewer = SafetyReviewer(SimpleNamespace(), str(tmp_path))
    reviewer.authorization_context = context
    reviewer.authorization_source = ref
    reviewer._ask_model = AsyncMock(side_effect=[json.dumps({"decision": "deny", "risk": "low", "reason": "missing", "blocker_kind": "missing_authorization"}),
        json.dumps({"decision": "allow", "risk": "low", "reason": "verified source"})])
    result = await reviewer.review(tool_name="Bash", arguments={"command": request["command"]}, risk={}, conversation=None)
    assert result.allowed and reviewer._ask_model.await_count == 2
    assert "禁止安装依赖" in reviewer._ask_model.call_args.kwargs["task_context"]
    from pathlib import Path
    Path(ref["path"]).write_text("tampered", encoding="utf-8")
    assert read_review_source(ref) == ""


def test_legacy_recovery_requires_unique_bound_host_dispatch_interval():
    row = request_receipts("N03", "rev", [command_request()], status="in_flight")[0]
    end_row = {**row, "status": "outcome_unknown"}
    start = {"node_id": "N03", "phase": "capability_escalation", "status": "running",
             "attempt": 14, "seq": 10, "server_time": 100, "command_receipts": [row]}
    end = {**start, "status": "needs_attention", "seq": 20, "server_time": 130, "command_receipts": [end_row]}
    value = {"events": [start, end], "node_states": {
        "N03": {"input_revision": "rev", "command_receipts": []},
        "N03__command_probe": {"attempt": 14, "input_revision": "rev", "started_at": 101,
            "completed_at": 129, "tools": {"pending": {**interrupted(), "attempt": 14}}}}}
    ambiguous = copy.deepcopy(value)
    ambiguous["events"].insert(0, {**start, "seq": 1})
    assert reconcile_run_command_receipts(ambiguous)["unresolved"]
    wrong_attempt = copy.deepcopy(value)
    wrong_attempt["node_states"]["N03__command_probe"]["attempt"] = 15
    assert reconcile_run_command_receipts(wrong_attempt)["unresolved"]
    assert reconcile_run_command_receipts(value) == {"corrected": 1, "unresolved": []}
    assert value["node_states"]["N03"]["command_receipts"][0]["tool_id"] == "pending"
    assert reconcile_run_command_receipts(value) == {"corrected": 0, "unresolved": []}


@pytest.mark.asyncio
async def test_whitelist_and_result_reader_do_not_require_explicit_work_dir(tmp_path):
    from dataclasses import replace
    from mewcode.expert_web import _role_registry
    from mewcode.tools import ToolRegistry
    from mewcode.tools.bash import Bash, Params as BashParams
    registry = ToolRegistry()
    bash = Bash()
    bash.execute = AsyncMock()
    registry.register(bash)
    session = SimpleNamespace(agent=SimpleNamespace(registry=registry), work_dir=str(tmp_path))
    policy = ExpertExecutionPolicy("stable", False, 0, command_allowlist=("python --version",))
    tools = _role_registry(session, "executor", policy)
    result = await tools.get("Bash").execute(BashParams(command="other-command"))
    assert result.is_error
    bash.execute.assert_not_awaited()
    reader_policy = replace(policy, command_allowlist=(), node_tools=("ReadFile", "Glob", "Grep"),
                            stage_inputs={"upstream": {"handoff": {"summary": "complete result"}}})
    assert _role_registry(session, "planner", reader_policy).get("ReadStageResult") is not None
    assert _role_registry(session, "planner", replace(reader_policy, deny_tools=("ReadStageResult",))).get("ReadStageResult") is None


@pytest.mark.asyncio
async def test_planner_plain_text_does_not_release_downstream(tmp_path, monkeypatch):
    from dataclasses import replace
    from mewcode import expert_web
    from mewcode.agent import StreamText
    from mewcode.conversation import ConversationManager
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.experts.flow import ExpertFlowStore
    from mewcode.orchestration.plan_session import PlanSessionStore
    from mewcode.tools import ToolRegistry
    from tests.test_expert_flow import _profile
    from tests.test_file_escalation import grok_catalog
    from tests.test_retry_memory import retry_flow

    flow = retry_flow()
    flow = replace(flow, nodes=tuple(replace(node, role="planner") if node.node_id == "work" else node for node in flow.nodes))
    project = tmp_path / "project"
    project.mkdir()
    runs, flows = ExpertRunStore(tmp_path / "runs"), ExpertFlowStore(tmp_path / "flows.json")
    flows.put(project, flow)
    built, events = [], []
    class FakeAgent:
        registry = ToolRegistry()
        client = SimpleNamespace(expose_reasoning=False)
        total_input_tokens = total_output_tokens = 1
        def set_review_authorization_context(self, context):
            self.node_id = context.split("STAGE: ", 1)[1].split(" ", 1)[0]
        async def run(self, conversation):
            assert self.node_id == "work"
            yield StreamText(text="调查尚未完成，缺少证据")
    def build(session, registry, role, model, policy, verdict):
        built.append(role)
        return FakeAgent(), None, SimpleNamespace(conversation=ConversationManager(), aclose=AsyncMock())
    monkeypatch.setattr(expert_web, "_build_role_agent", build)
    session = SimpleNamespace(work_dir=str(project), conversation_id="planner-test", multi_agent=False,
        session=SimpleNamespace(meta=SimpleNamespace(message_count=0), append=lambda *args: None,
                                append_expert_run=lambda *args, **kwargs: None),
        conversation=ConversationManager(), expert_run_id="", expert_control_action="pause",
        context=SimpleNamespace(get=lambda key, default=None: default, slots=SimpleNamespace(resolve=lambda key: {})),
        task_manager=SimpleNamespace(list_tasks=lambda: [], mark_notified=lambda ids: None),
        expert_child_event_callback=None, status_payload=lambda: {}, tasks_payload=lambda: [])
    await expert_web.run_expert_chat(SimpleNamespace(send_json=AsyncMock(side_effect=lambda e: events.append(e))),
        "调查再实施", session=session, confirmed_new_flow=flow,
        registry_service=SimpleNamespace(catalog=AsyncMock(return_value=grok_catalog()), resolve=lambda _: None),
        profile_store=SimpleNamespace(get=lambda _: _profile()), flow_store=flows,
        plan_store=PlanSessionStore(tmp_path / "plans.json"), run_store=runs)
    snapshot = runs.load(next(event["run_id"] for event in events if event.get("run_id")))
    assert snapshot["node_states"]["work"]["status"] != "completed"
    assert all(role == "planner" for role in built)
