import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from mewcode.expert_group import ExpertExecutionPolicy, ExpertGroupOrchestrator, ExpertRoleResult
from mewcode.expert_web import _command_capability_admission
from mewcode.experts.command_receipts import (
    execution_observation, observe_requests, persist_command_receipts, request_receipts,
)
from mewcode.experts.delivery import prior_handoff_source
from mewcode.experts.file_escalation import resolve_capability_requests
from mewcode.experts.flow import ExpertNodeSpec
from mewcode.permissions.modes import PermissionMode
from mewcode.tools.read_stage_result import ReadStageResult, ReadStageResultParams
from mewcode.tools.submit_handoff import SubmitHandoffParams
from tests.test_expert_flow import _profile
from tests.test_file_escalation import command_request, grok_catalog
from tests.test_retry_memory import retry_flow


def blocked_result(requests=None):
    return ExpertRoleResult("等待回执", status="needs_attention", error_kind="capability_missing",
        handoff=SubmitHandoffParams(summary="synthetic request", status="blocked",
            blocker_kind="capability_missing", command_requests=requests or [command_request()]).model_dump())


def verification(command="python --version", *, status="exited", exit_code=0, stdout="observed"):
    return {"name": "Bash", "tool_id": "host-tool-1", "args": {"command": command},
            "status": "completed" if exit_code == 0 else "failed", "is_error": exit_code != 0,
            "output": "child says EXIT CODE: 0", "host_observation": {
                "command_execution": execution_observation(status, stdout=stdout,
                    stderr="", exit_code=exit_code, reason="host reason")}}


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["native", "deny", "readonly", "auditor", "budget"])
async def test_host_refusals_return_each_request_without_dispatch(tmp_path, gate):
    requests = [command_request(f"synthetic-probe-{i}") for i in range(5)]
    result = blocked_result(requests)
    policy = ExpertExecutionPolicy("stable", False, 0, input_revision="revision",
        deny_tools=("Bash",) if gate == "deny" else (), parallel_readonly=gate == "readonly",
        token_budget=10 if gate == "budget" else None)
    events = []

    async def dispatch(*_):
        raise AssertionError("no helper/model/command may start")

    async def emit(event):
        events.append(event)

    actual = await resolve_capability_requests(
        ExpertNodeSpec("N03", "auditor" if gate == "auditor" else "executor", "N03"),
        result, policy, work_dir=tmp_path, dispatch=dispatch, emit=emit,
        command_admission=lambda _: {"allowed": False, "reason": "native gate closed"} if gate == "native" else None)
    assert len(actual.command_receipts) == 5
    assert actual.status == "needs_attention"
    assert len({row["request_id"] for row in actual.command_receipts}) == 5
    assert all(row["command_started"] is False and row["exit_code"] is None
               and row["stdout"] is None for row in actual.command_receipts)
    assert events[-1]["durable_checkpoint"]
    assert all(row["status"] == ("not_started" if gate == "native" else "not_dispatched")
               for row in actual.command_receipts)


@pytest.mark.asyncio
async def test_executor_receipt_returns_structured_output_and_scope(tmp_path):
    events, calls = [], []

    async def dispatch(policy, prompt):
        calls.append(policy)
        assert policy.command_allowlist == ("python --version",)
        return ExpertRoleResult("receipt", status="completed", handoff={"status": "completed"},
                                verification_records=[verification(stdout="x" * 12000)])

    async def emit(event):
        events.append(copy.deepcopy(event))

    actual = await resolve_capability_requests(ExpertNodeSpec("N03", "executor", "N03"), blocked_result(),
        ExpertExecutionPolicy("stable", False, 0, input_revision="current"), work_dir=tmp_path,
        dispatch=dispatch, emit=emit)
    assert len(calls) == 1 and actual.error_kind == "capability_resolved"
    row = actual.command_receipts[0]
    assert row["status"] == "completed" and row["exit_code"] == 0
    assert row["stdout"] == "x" * 12000 and row["stderr"] == ""
    assert row["input_revision"] == "current" and row["evidence_source"] == "host_execution"
    assert events[0]["command_receipts"][0]["status"] == "in_flight"
    assert events[-1]["command_receipts"] == actual.command_receipts
    assert actual.handoff["capability_results"]["command_receipts"] == actual.command_receipts


@pytest.mark.parametrize("status,exit_code,expected", [
    ("exited", 7, "completed"), ("denied", None, "not_started"),
    ("timed_out", None, "outcome_unknown"), ("spawn_failed", None, "not_started")])
def test_host_observation_wins_over_child_success_markers(status, exit_code, expected):
    rows = request_receipts("N03", "current", [command_request()])
    observed = observe_requests(rows, [verification(status=status, exit_code=exit_code)])
    assert observed[0]["status"] == expected and observed[0]["exit_code"] == exit_code
    assert not observed[0]["successful"]


def test_repeated_results_keep_evidence_for_all_eight_commands():
    from mewcode.experts.attempt_memory import collect_verification_records
    tools = {str(i): verification(command=f"synthetic-{i}") for i in range(8)}
    tools.update({f"repeat-{i}": verification(command="synthetic-7") for i in range(12)})
    records = collect_verification_records(tools)
    assert {row["args"]["command"] for row in records} == {f"synthetic-{i}" for i in range(8)}


def test_request_identity_uses_same_outer_whitespace_normalization_as_execution_guard():
    original = command_request()
    spaced = command_request("  python --version  ")
    assert request_receipts("N03", "same", [original])[0]["request_id"] == request_receipts("N03", "same", [spaced])[0]["request_id"]


@pytest.mark.asyncio
async def test_prose_only_tool_result_cannot_complete_request(tmp_path):
    async def dispatch(*_):
        return ExpertRoleResult("claimed done", status="completed", handoff={"status": "completed"},
            verification_records=[{"name": "Bash", "args": {"command": "python --version"},
                                   "status": "completed", "output": "test passed", "is_error": False}])

    async def emit(_):
        pass

    actual = await resolve_capability_requests(ExpertNodeSpec("N03", "executor", "N03"), blocked_result(),
        ExpertExecutionPolicy("stable", False, 0), work_dir=tmp_path, dispatch=dispatch, emit=emit)
    assert actual.error_kind == "capability_missing" and actual.command_receipts[0]["status"] == "outcome_unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_status", ["completed", "in_flight", "outcome_unknown"])
async def test_changed_explanation_and_route_cannot_repeat_observed_requests(tmp_path, saved_status):
    old = request_receipts("N03", "same", [command_request()], status=saved_status)
    old[0]["successful"] = saved_status == "completed"
    changed = command_request()
    changed["purpose"] = "different wording"
    changed["deliverables"] = ["same execution with a new explanation"]

    async def dispatch(*_):
        raise AssertionError("must reuse/protect original request")

    async def emit(_):
        pass

    actual = await resolve_capability_requests(ExpertNodeSpec("N03", "executor", "N03"),
        blocked_result([changed]), ExpertExecutionPolicy("stable", False, 0, input_revision="same"),
        work_dir=tmp_path, dispatch=dispatch, emit=emit, prior_command_receipts=old,
        executor_route={"provider": "different", "model": "different"})
    assert actual.command_receipts == old
    assert actual.error_kind == ("capability_resolved" if saved_status == "completed" else "capability_missing")


@pytest.mark.asyncio
async def test_request_identity_does_not_reuse_changed_inputs(tmp_path):
    old = request_receipts("N03", "old", [command_request()], status="completed")
    old[0]["successful"] = True
    calls = []

    async def dispatch(*_):
        calls.append(1)
        return ExpertRoleResult("no result", status="needs_attention")

    async def emit(_):
        pass

    actual = await resolve_capability_requests(ExpertNodeSpec("N03", "executor", "N03"), blocked_result(),
        ExpertExecutionPolicy("stable", False, 0, input_revision="new"), work_dir=tmp_path,
        dispatch=dispatch, emit=emit, prior_command_receipts=old)
    assert calls == [1] and actual.command_receipts[0]["status"] == "outcome_unknown"
    assert actual.command_receipts[0]["request_id"] != old[0]["request_id"]


@pytest.mark.asyncio
async def test_cancellation_durably_keeps_unknown_result_for_no_replay(tmp_path):
    events = []

    async def dispatch(*_):
        raise asyncio.CancelledError

    async def emit(event):
        events.append(event)

    with pytest.raises(asyncio.CancelledError):
        await resolve_capability_requests(ExpertNodeSpec("N03", "executor", "N03"), blocked_result(),
            ExpertExecutionPolicy("stable", False, 0), work_dir=tmp_path, dispatch=dispatch, emit=emit)
    assert events[-1]["command_receipts"][0]["status"] == "outcome_unknown"
    assert events[-1]["durable_checkpoint"]


@pytest.mark.asyncio
async def test_empty_later_handoff_preserves_legacy_command_only_source():
    stage = {"status": "needs_attention", "input_revision": "current", "handoff": {"status": "blocked"},
        "tools": {"accepted": {"name": "SubmitHandoff", "status": "completed", "args": blocked_result().handoff}}}
    source = prior_handoff_source("N03", stage, input_revision="current")
    assert source["command_receipts"][0]["status"] == "outcome_unknown"
    assert source["command_receipts"][0]["exit_code"] is None
    assert prior_handoff_source("N03", stage, input_revision="new") is None
    result = await ReadStageResult({"N03": source}).execute(ReadStageResultParams(node_id="N03"))
    assert "command_receipts" in result.output and "python --version" in result.output
    assert (await ReadStageResult({"N03": source}).execute(ReadStageResultParams(node_id="other"))).is_error


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [12000, 24000])
async def test_long_receipt_pages_survive_real_agent_truncation_without_skipping(limit, tmp_path):
    from mewcode.agent import Agent
    from mewcode.tools import create_default_registry
    from tests.test_task_status_host_observation import MockLLMClient
    reader = ReadStageResult({"N03": {"command_receipts": [{"stdout": "x" * 25000 + "LAST_OBSERVED_OUTPUT",
                                                          "stderr": "", "exit_code": 7}]}})
    agent = Agent(MockLLMClient([]), create_default_registry(), "anthropic", work_dir=str(tmp_path))
    offset, parts = 0, []
    while True:
        result = await reader.execute(ReadStageResultParams(node_id="N03", offset=offset, limit=limit))
        visible = agent._maybe_persist_or_truncate("synthetic-page", result.output)
        assert visible == result.output and len(visible) <= 10000
        parts.append(visible.split("\n", 5)[5])
        cursor = result.host_observation["read_cursor"]
        offset = cursor["next_offset"]
        if not cursor["more"]:
            break
    assert "".join(parts) == reader.sources["N03"]


def test_later_request_does_not_hide_earlier_five_unresolved_requests():
    from mewcode.experts.command_receipts import stage_command_receipts
    original = [command_request(f"original-{i}") for i in range(5)]
    later = [command_request("later-request")]
    stage = {"status": "needs_attention", "input_revision": "current",
        "tools": {"original": {"name": "SubmitHandoff", "status": "completed", "args": blocked_result(original).handoff}},
        "handoff": blocked_result(later).handoff,
        "command_receipts": request_receipts("N03", "current", later, status="not_started")}
    rows = stage_command_receipts("N03", stage)
    assert len(rows) == 6
    assert {row["request"]["command"] for row in rows} == {"later-request", *(f"original-{i}" for i in range(5))}
    assert rows[-1]["status"] == "not_started"


def test_host_ledger_is_not_erased_by_followup_without_commands():
    value = {}
    rows = request_receipts("N03", "current", [command_request()], status="in_flight")
    persist_command_receipts(value, {"node_id": "N03", "command_receipts": rows})
    persist_command_receipts(value, {"node_id": "N03", "command_receipts": []})
    assert value["node_states"]["N03"]["command_receipts"] == rows


@pytest.mark.parametrize("status", ["completed", "outcome_unknown", "in_flight"])
def test_new_refusal_cannot_erase_existing_execution_receipt(status):
    value = {}
    rows = request_receipts("N03", "current", [command_request()], status=status)
    persist_command_receipts(value, {"node_id": "N03", "command_receipts": rows})
    denied = request_receipts("N03", "current", [command_request()], status="not_started")
    persist_command_receipts(value, {"node_id": "N03", "command_receipts": denied})
    assert value["node_states"]["N03"]["command_receipts"] == rows


def test_closed_native_gate_preflight_does_not_resolve_model(monkeypatch):
    import mewcode.expert_web as web
    monkeypatch.setattr(web, "_expert_permission_checker", lambda *_: SimpleNamespace(
        mode=PermissionMode.AUTO, requires_native_commands=lambda: True))
    monkeypatch.setattr("mewcode.sandbox.windows.native_runtime_ready", lambda: False)
    policy = ExpertExecutionPolicy("stable", False, 0)
    assert _command_capability_admission(SimpleNamespace(work_dir="synthetic"), policy)["allowed"] is False
    monkeypatch.setattr("mewcode.sandbox.windows.native_runtime_ready", lambda: True)
    assert _command_capability_admission(SimpleNamespace(work_dir="synthetic"), policy) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["blocked", "receipt"])
async def test_executor_request_stops_retry_three_or_receives_one_followup(tmp_path, outcome):
    from mewcode.model_registry import ModelRef
    flow = retry_flow()
    calls, events, revisions = [], [], []

    async def runner(node, model, prompt, policy, member=None):
        calls.append(node.node_id)
        if node.node_id == "work":
            revisions.append(policy.input_revision)
        if node.node_id == "work__command_probe":
            return ExpertRoleResult("host result", status="completed", handoff={"status": "completed"},
                                    verification_records=[verification()])
        if node.node_id == "work" and calls.count("work") == 1:
            return blocked_result()
        if node.node_id == "work":
            assert policy.stage_inputs["work__command_probe"]["handoff"]["command_receipts"][0]["exit_code"] == 0
        return ExpertRoleResult("done", status="completed", handoff={"status": "completed"})

    orchestrator = ExpertGroupOrchestrator(runner=None, node_runner=runner, work_dir=tmp_path,
        archive_root=tmp_path, event_sink=events.append, command_admission=lambda _: (
            {"allowed": False, "reason": "native gate closed"} if outcome == "blocked" else None))
    profile = replace(_profile(), capability_executor=ModelRef("desktop:grok-main", "grok-4.6", "high"))
    result = await orchestrator.run("synthetic only", profile, grok_catalog(), strategy="stable", flow=flow)
    assert calls == (["work"] if outcome == "blocked" else ["work", "work__command_probe", "work", "done"])
    assert (result.status == "completed") == (outcome == "receipt")
    assert not any(event.get("phase") == "retry" for event in events)
    if outcome == "receipt":
        assert revisions[0] == revisions[1]


@pytest.mark.asyncio
async def test_gate_clearance_can_dispatch_previously_unstarted_request(tmp_path):
    old = request_receipts("N03", "same", [command_request()], status="not_started")
    calls = []

    async def dispatch(*_):
        calls.append(1)
        return ExpertRoleResult("done", status="completed", handoff={"status": "completed"},
                                verification_records=[verification()])

    async def emit(_):
        pass

    actual = await resolve_capability_requests(ExpertNodeSpec("N03", "executor", "N03"), blocked_result(),
        ExpertExecutionPolicy("stable", False, 0, input_revision="same"), work_dir=tmp_path,
        dispatch=dispatch, emit=emit, prior_command_receipts=old, command_admission=lambda _: None)
    assert calls == [1] and actual.error_kind == "capability_resolved"


@pytest.mark.asyncio
async def test_legacy_failure_without_individual_receipts_is_not_repeated_for_new_wording(tmp_path):
    prior = blocked_result().handoff
    prior["escalation_receipt"] = {"status": "needs_attention", "text": "no execution observation",
                                 "executor_route": {"provider": "chosen", "model": "chosen"}}
    changed = command_request()
    changed["purpose"] = "same command with a different explanation"

    async def dispatch(*_):
        raise AssertionError("legacy unknown result must not be repeated")

    async def emit(_):
        pass

    actual = await resolve_capability_requests(ExpertNodeSpec("N03", "executor", "N03"), blocked_result([changed]),
        ExpertExecutionPolicy("stable", False, 0), work_dir=tmp_path, dispatch=dispatch, emit=emit,
        prior_handoff=prior, executor_route={"provider": "chosen", "model": "chosen"})
    assert actual.command_receipts[0]["status"] == "outcome_unknown"


@pytest.mark.asyncio
async def test_partial_batch_is_not_replayed_after_route_change(tmp_path):
    requests = [command_request(f"synthetic-{i}") for i in range(5)]
    saved = request_receipts("N03", "same", requests, status="not_started")
    saved[0].update(status="completed", successful=True, exit_code=0)
    saved[1]["status"] = "outcome_unknown"

    async def dispatch(*_):
        raise AssertionError("completed and unknown commands cannot repeat")

    async def emit(_):
        pass

    actual = await resolve_capability_requests(ExpertNodeSpec("N03", "executor", "N03"), blocked_result(requests),
        ExpertExecutionPolicy("stable", False, 0, input_revision="same"), work_dir=tmp_path,
        dispatch=dispatch, emit=emit, prior_command_receipts=saved,
        executor_route={"provider": "changed", "model": "changed"})
    assert actual.command_receipts == saved and actual.error_kind == "capability_missing"


@pytest.mark.asyncio
async def test_bash_wrapper_reports_host_exit_and_full_streams_without_launch(monkeypatch):
    from unittest.mock import AsyncMock
    from mewcode.tools.bash import Bash, Params
    process = SimpleNamespace(returncode=7, communicate=AsyncMock(return_value=(b"x" * 12000, b"error")))
    monkeypatch.setattr("mewcode.tools.command_sandbox.native_auto_scope", lambda: False)
    monkeypatch.setattr("mewcode.tools.command_sandbox.sandbox_scope", lambda: False)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    result = await Bash().execute(Params(command="synthetic-never-launched"))
    host = result.host_observation["command_execution"]
    assert result.is_error and host["exit_code"] == 7 and host["command_started"] is True
    assert host["stdout"] == "x" * 12000 and host["stderr"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [False, True])
async def test_real_agent_event_keeps_host_metadata_after_text_truncation(tmp_path, parallel):
    from mewcode.agent import Agent, ToolResultEvent
    from mewcode.client import StreamEnd, TextDelta, ToolCallComplete
    from mewcode.conversation import ConversationManager
    from mewcode.tools import create_default_registry
    from tests.test_task_status_host_observation import HugeOutput, MockLLMClient
    tool = HugeOutput()
    tool.is_concurrency_safe = parallel
    registry = create_default_registry()
    registry.register(tool)
    client = MockLLMClient([[ToolCallComplete("big", "HugeOutput", {}), StreamEnd("tool_use")],
                            [TextDelta("done"), StreamEnd("end_turn")]])
    conversation = ConversationManager()
    conversation.add_user_message("synthetic tool")
    agent = Agent(client, registry, "anthropic", work_dir=str(tmp_path))
    events = [event async for event in agent.run(conversation)]
    event = next(event for event in events if isinstance(event, ToolResultEvent))
    assert event.host_observation == {"k": 1}


@pytest.mark.asyncio
async def test_web_host_preserves_unstarted_receipts_through_bounded_kernel_retries(tmp_path, monkeypatch):
    # Synthetic registry fixtures must not read the user's configured observer.
    monkeypatch.setenv("MEWCODE_USER_DATA_DIR", str(tmp_path / "isolated-profile"))
    # All actors are synthetic; exercise downstream admission/receipt handling
    # without inspecting or invoking the installed operating-system runtime.
    monkeypatch.setattr("mewcode.kernel.preflight.inspect", lambda *args, **kwargs: [])
    from unittest.mock import AsyncMock
    from mewcode import expert_web
    from mewcode.agent import StreamText, ToolResultEvent, ToolUseEvent
    from mewcode.conversation import ConversationManager
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.experts.flow import ExpertFlowStore
    from mewcode.model_registry import ModelRef
    from mewcode.orchestration.plan_session import PlanSessionStore

    project = tmp_path / "project"
    project.mkdir()
    runs, flows = ExpertRunStore(tmp_path / "runs"), ExpertFlowStore(tmp_path / "flows.json")
    flows.put(project, retry_flow())
    requests = [command_request(f"synthetic-{i}") for i in range(5)]
    built, events = [], []

    class FakeAgent:
        client = SimpleNamespace(expose_reasoning=False)
        total_input_tokens = total_output_tokens = 1

        def __init__(self, policy):
            self.policy = policy

        def set_review_authorization_context(self, context):
            self.node_id = context.split("STAGE: ", 1)[1].split(" ", 1)[0]

        async def run(self, conversation):
            assert self.node_id == "work"
            self.policy.handoff_holder.value = blocked_result(requests).handoff
            yield ToolUseEvent("SubmitHandoff", "request", self.policy.handoff_holder.value)
            yield ToolResultEvent("request", "SubmitHandoff", "accepted", False, .1)
            conversation.add_assistant_message("等待回执")
            yield StreamText(text="等待回执")

    def build(session, registry, role, model, policy, verdict):
        built.append(model)
        return FakeAgent(policy), None, SimpleNamespace(conversation=ConversationManager(), aclose=AsyncMock())

    monkeypatch.setattr(expert_web, "_build_role_agent", build)
    monkeypatch.setattr(expert_web, "_command_capability_admission", lambda *_: {
        "allowed": False, "reason": "native gate closed"})
    session = SimpleNamespace(work_dir=str(project), conversation_id="receipt-test", multi_agent=False,
        session=SimpleNamespace(meta=SimpleNamespace(message_count=0), append=lambda *args: None,
                                append_expert_run=lambda *args, **kwargs: None),
        conversation=ConversationManager(), expert_run_id="", expert_control_action="pause",
        context=SimpleNamespace(get=lambda key, default=None: default, slots=SimpleNamespace(resolve=lambda key: {})),
        task_manager=SimpleNamespace(list_tasks=lambda: [], mark_notified=lambda ids: None),
        expert_child_event_callback=None, status_payload=lambda: {}, tasks_payload=lambda: [])

    async def send(event):
        events.append(copy.deepcopy(event))

    profile = replace(_profile(), capability_executor=ModelRef("desktop:grok-main", "grok-4.6", "high"))
    await asyncio.wait_for(expert_web.run_expert_chat(SimpleNamespace(send_json=send), "synthetic task", session=session,
        confirmed_new_flow=flows.get(project), registry_service=SimpleNamespace(
            catalog=AsyncMock(return_value=grok_catalog()), resolve=lambda _: None),
        profile_store=SimpleNamespace(get=lambda _: profile), flow_store=flows,
        plan_store=PlanSessionStore(tmp_path / "plans.json"), run_store=runs), 10)
    snapshot = ExpertRunStore(runs.root).load(next(event["run_id"] for event in events if event.get("run_id")))
    assert snapshot["status"] == "paused" and 1 <= len(built) <= 3
    assert all(model == _profile().executor for model in built)
    assert not snapshot.get("recovery_case_enabled")
    stage = snapshot["node_states"]["work"]
    assert len(stage["command_receipts"]) == 5
    assert all(row["status"] == "not_started" and row["exit_code"] is None for row in stage["command_receipts"])
    source = prior_handoff_source("work", stage, input_revision=stage["input_revision"])
    assert source["command_receipts"] == stage["command_receipts"]
