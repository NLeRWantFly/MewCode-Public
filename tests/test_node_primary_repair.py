import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.expert_group import ExpertExecutionPolicy, ExpertRoleResult
from mewcode.expert_runs import ExpertRunStore
from mewcode.expert_web import _role_registry, _expert_execution_mode
from mewcode.experts.command_receipts import request_receipts
from mewcode.experts.primary_repair import PrimaryRepairCoordinator
from mewcode.kernel.escalation import KernelScheduler as SchedulerRecovery
from mewcode.permissions.modes import PermissionMode
from tests.test_command_request_receipts import command_request
from tests.test_expert_children import make_registry
from tests.test_retry_memory import retry_flow


@pytest.mark.parametrize("role", ["planner", "manager"])
def test_role_uses_host_tools_without_changing_mode_or_delegation(role, tmp_path):
    registry = make_registry("ReadFile", "Bash", "WriteFile", "EditFile", "browser_navigate", "Agent", "TeamCreate")
    session = SimpleNamespace(agent=SimpleNamespace(registry=registry, permission_mode=PermissionMode.PLAN),
                              work_dir=str(tmp_path))
    policy = ExpertExecutionPolicy("stable", False, 0)
    actual = _role_registry(session, role, policy)
    assert all(actual.get(name) is not None for name in ("Bash", "WriteFile", "EditFile", "browser_navigate"))
    assert actual.get("Agent") is None and actual.get("TeamCreate") is None
    assert _expert_execution_mode(session) == PermissionMode.PLAN
    assert _role_registry(session, role, replace(policy, node_tools=("ReadFile",))).get("Bash") is None
    assert _role_registry(session, role, replace(policy, deny_tools=("Bash",))).get("Bash") is None
    assert _role_registry(session, role, replace(policy, parallel_readonly=True)).get("WriteFile") is None


def setup_repair(tmp_path, dispatch):
    store, run_id = ExpertRunStore(tmp_path / "runs"), "b" * 32
    flow = retry_flow()
    node = replace(flow.node_map["work"], role="planner")
    store.create(run_id=run_id, conversation_id="primary-repair", task="inspect local data", attachments=[],
                 profile={}, strategy="stable", flow=flow)
    store.mutate(run_id, lambda value: value["node_states"].setdefault("work", {}).update(
        status="needs_attention", input_revision="current", tools={}, available_tools=["ReadFile", "Bash"],
        handoff={"status": "blocked", "blocker_kind": "capability_missing", "blockers": ["probe unavailable"]}))
    events = []
    coordinator = PrimaryRepairCoordinator(store=store, run_id=run_id, dispatch=dispatch,
        emit=AsyncMock(side_effect=events.append), before_resume=AsyncMock(), task="inspect local data",
        work_dir=str(tmp_path), model={"provider_key": "selected-primary", "model_id": "primary-model", "effort": "high"})
    request = dict(node=node, result=ExpertRoleResult("blocked", status="needs_attention", error_kind="capability_missing"),
        policy=ExpertExecutionPolicy("stable", False, 0, token_budget=24000, timeout_seconds=300),
        allowed=["continue_checkpoint", "diagnose_path"], host_facts={})
    return coordinator, store, request, events


def repair_result():
    return ExpertRoleResult("checked runtime", status="completed", billable_tokens=123,
        handoff={"status": "completed", "summary": "runtime checked; continue original probe"},
        verification_records=[{"name": "ReadFile", "status": "completed", "is_error": False}])


@pytest.mark.asyncio
async def test_primary_tool_loop_returns_structured_receipt_preserving_checkpoint(tmp_path):
    async def dispatch(helper, prompt, policy):
        assert helper.node_id.startswith("work__assist_") and helper.role == "executor"
        assert policy.node_tools == ("*",) and policy.assistance_session
        assert policy.input_revision == "current" and not policy.allow_delegation
        assert policy.token_budget == 24000 and policy.timeout_seconds == 300
        policy.assistance_holder.update(next_step="resume", notes="runtime checked")
        return repair_result()
    coordinator, store, request, _ = setup_repair(tmp_path, dispatch)
    original = copy.deepcopy(store.load(coordinator.run_id)["locked_flow"])
    actual = await coordinator(**request)
    assert actual["source"] == "primary_model_assistance" and actual["action"] == "diagnose_path"
    assert actual["decision_tokens"] == 123
    receipt = actual["assistance_receipt"]
    assert receipt["next_step"] == "resume"
    snapshot = store.load(coordinator.run_id)
    assert snapshot["locked_flow"] == original
    assert snapshot["node_states"]["work"]["assistance_receipts"][-1] == receipt


@pytest.mark.asyncio
async def test_unknown_effects_keep_global_tools_and_require_verification(tmp_path):
    async def dispatch(helper, prompt, policy):
        assert not policy.parallel_readonly and policy.node_tools == ("*",)
        policy.assistance_holder.update(next_step="resume", notes="unsupported claim")
        return repair_result()
    coordinator, store, request, _ = setup_repair(tmp_path, dispatch)
    rows = request_receipts("work", "current", [command_request("echo changed > output.txt")], status="outcome_unknown")
    from mewcode.experts.node_ledger import ledger_for_run
    def uncertain(value):
        value["node_states"]["work"]["command_receipts"] = rows
        ledger_for_run(value).record("work", "blocker", {"kind": "effect_unknown", "request_id": rows[0]["request_id"]})
    store.mutate(coordinator.run_id, uncertain)
    actual = await coordinator(**request)
    assert actual["action"] == "human" and actual["decision_tokens"] == 123
    assert store.load(coordinator.run_id)["node_states"]["work"]["command_receipts"][0]["status"] == "outcome_unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["narrative_only", "stale_input", "stale_flow", "new_unknown", "unresolved_handoff"])
async def test_primary_claim_cannot_authorize_resume_without_current_safe_evidence(tmp_path, case):
    async def dispatch(helper, prompt, policy):
        if case != "narrative_only":
            policy.assistance_holder.update(next_step="resume", notes="claimed safe")
        if case == "stale_input":
            store.mutate(coordinator.run_id, lambda v: v["node_states"]["work"].update(input_revision="changed"))
        elif case == "stale_flow":
            store.mutate(coordinator.run_id, lambda v: v["locked_flow"].update(version=99))
        elif case == "new_unknown":
            store.mutate(coordinator.run_id, lambda v: v["node_states"]["work"].update(
                tools={"x": {"name": "Bash", "status": "outcome_unknown"}}))
        result = repair_result()
        if case == "unresolved_handoff":
            result = replace(result, handoff={"status": "completed", "blockers": ["still missing"]})
        return result
    coordinator, store, request, _ = setup_repair(tmp_path, dispatch)
    if case.startswith("stale_"):
        with pytest.raises(ValueError, match="Node changed"):
            await coordinator(**request)
    else:
        actual = await coordinator(**request)
        assert actual["action"] == "human"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["question", "permission", "budget", "deadline"])
async def test_actual_user_boundaries_and_exhausted_resources_do_not_launch_fix(tmp_path, case):
    dispatch = AsyncMock(return_value=repair_result())
    coordinator, store, request, _ = setup_repair(tmp_path, dispatch)
    if case == "question":
        request["result"] = replace(request["result"], handoff={"question": "choose scope"})
    elif case == "permission":
        store.mutate(coordinator.run_id, lambda v: v["node_states"]["work"].update(pending_permission={"tool_id": "x"}))
    elif case == "budget":
        request["policy"] = replace(request["policy"], token_budget=100)
    elif case == "deadline":
        request["host_facts"] = {"hard_boundary": "global deadline"}
    assert await coordinator(**request) is None
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["parallel", "auditor"])
async def test_all_roles_can_request_assistance(tmp_path, case):
    dispatch = AsyncMock(return_value=repair_result())
    coordinator, store, request, _ = setup_repair(tmp_path, dispatch)
    if case == "parallel":
        request["policy"] = replace(request["policy"], parallel_readonly=True)
    else:
        request["node"] = replace(request["node"], role="auditor")
    await coordinator(**request)
    dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_primary_fix_is_durable_and_not_relaunched(tmp_path):
    dispatch = AsyncMock(side_effect=asyncio.CancelledError)
    coordinator, store, request, _ = setup_repair(tmp_path, dispatch)
    with pytest.raises(asyncio.CancelledError):
        await coordinator(**request)
    assert store.load(coordinator.run_id)["node_states"]["work"]["assistance_request"]["status"] == "interrupted"
    assert (await coordinator(**request))["action"] == "human"
    dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_scheduler_consults_primary_for_explicit_assistance(tmp_path):
    async def dispatch(helper, prompt, policy):
        policy.assistance_holder.update(next_step="resume", notes="checked")
        return repair_result()
    coordinator, store, request, _ = setup_repair(tmp_path, dispatch)
    store.mutate(coordinator.run_id, lambda r: r["node_states"]["work"].update(
        assistance_request={"status": "pending", "request_id": "r", "kind": "diagnosis", "need": "check"}))
    handler = SchedulerRecovery(session=SimpleNamespace(), registry=None, store=store,
        run_id=coordinator.run_id, emit=AsyncMock(), before_resume=AsyncMock(), task="inspect local data",
        adaptive_methods=True, primary_repair=coordinator)
    account = AsyncMock()
    actual = await handler(**request, account=account)
    assert actual["action"] == "diagnose_path" and actual["source"] == "primary_model_assistance"
    account.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_account", [False, True])
async def test_assistance_preserves_paid_usage(tmp_path, monkeypatch, with_account):
    from tests.test_scheduler_recovery import controller
    handler, flow, _, store, _, _, _ = controller(tmp_path, monkeypatch)
    handler.primary_repair = AsyncMock(return_value={
        "action": "diagnose_path", "decision_tokens": 16000, "decision_seconds": 240.0})
    account = AsyncMock() if with_account else None
    actual = await handler(node=flow.node_map["extract"],
        result=ExpertRoleResult("change method", status="needs_attention", error_kind="supervisor_method_change"),
        policy=ExpertExecutionPolicy("stable", False, 0, token_budget=24000, timeout_seconds=300),
        allowed=["continue_checkpoint", "diagnose_path"], host_facts={}, account=account)
    assert actual["decision_tokens"] == 16000 and actual["decision_seconds"] == 240.0
    if account is not None:
        account.assert_awaited_once_with(16000, 240.0)
    saved = store.load(handler.run_id)["node_states"]["extract"]["scheduler_recovery"]["decisions"][-1]
    assert saved["decision_tokens"] == 16000


@pytest.mark.asyncio
@pytest.mark.parametrize('late_receipt', [False, True])
@pytest.mark.parametrize('legacy_analysis', [False, True])
async def test_web_primary_model_uses_real_tools_then_original_node_verifies(tmp_path, monkeypatch, late_receipt, legacy_analysis):
    from mewcode import expert_web
    from mewcode.agent import Agent
    from mewcode.conversation import ConversationManager
    from mewcode.experts.flow import ExpertFlowStore
    from mewcode.orchestration.plan_session import PlanSessionStore
    from mewcode.permissions.policy import build_permission_checker
    from mewcode.tools import ToolRegistry
    from mewcode.tools.base import ToolCallComplete, StreamEnd
    from mewcode.tools.read_file import ReadFile
    from tests.test_agent import MockLLMClient
    from tests.test_expert_flow import _profile
    from tests.test_file_escalation import grok_catalog

    flow = retry_flow()
    flow = replace(flow, nodes=tuple(replace(n, role="planner") if n.node_id == "work" else n for n in flow.nodes))
    project = tmp_path / "project"
    project.mkdir()
    # Recovery now verifies the original failed operation. Begin with an
    # actually missing input instead of requesting help without any failure.
    runs, flows = ExpertRunStore(tmp_path / "runs"), ExpertFlowStore(tmp_path / "flows.json")
    flows.put(project, flow)
    base_tools = ToolRegistry()
    base_tools.register(ReadFile())
    built, events = [], []
    session = SimpleNamespace(work_dir=str(project), conversation_id="primary-tools", multi_agent=False,
        agent=SimpleNamespace(registry=base_tools, client=MockLLMClient([]), permission_mode=PermissionMode.AUTO),
        slot=SimpleNamespace(key="selected-primary", provider=SimpleNamespace(model="primary-model")),
        _effective_effort=lambda: "high",
        session=SimpleNamespace(meta=SimpleNamespace(message_count=0), append=lambda *args: None,
                                append_expert_run=lambda *args, **kwargs: None),
        conversation=ConversationManager(), expert_run_id="", expert_control_action="pause",
        context=SimpleNamespace(get=lambda key, default=None: default, slots=SimpleNamespace(resolve=lambda key: {})),
        task_manager=SimpleNamespace(list_tasks=lambda: [], mark_notified=lambda ids: None),
        expert_child_event_callback=None, status_payload=lambda: {}, tasks_payload=lambda: [])

    def call(name, arguments):
        return [ToolCallComplete(tool_id=name, tool_name=name, arguments=arguments),
                StreamEnd(stop_reason="tool_use", input_tokens=10, output_tokens=10)]

    def build(session, registry, role, model, policy, verdict=None):
        built.append((role, model, policy))
        analysis = policy.recovery_analysis
        if analysis:
            if legacy_analysis:
                def seed_old_context(record):
                    stage = record['node_states'].setdefault('work__analyst__' + analysis['case_id'], {})
                    stage.update(messages=[{'role': 'assistant', 'content': '已达到本循环剩余 Token 预算。' * 6}],
                        text='old analysis display', partial_text='已达到本循环剩余 Token 预算。' * 6,
                        analysis_context_version=1)
                runs.mutate(policy.stage_inputs['@run']['run_id'], seed_old_context)
            from tests.test_recovery_case_e2e import option, plan
            selected = option(analysis['round'])
            selected['strategy'] = {'kind': 'read', 'tools': ['ReadFile'], 'target': str(project / 'data.txt')}
            selected['verification'].update(tool='ReadFile', arguments={'file_path': str(project / 'data.txt')})
            responses = [call('SubmitRecoveryPlan', plan(analysis, [selected]))]
            # The host must return the accepted plan immediately: a second
            # model/tool turn is unrelated to delivering this analysis.
            responses.append(call('ReadFile', {'file_path': str(project / 'unrelated-after-plan.txt')}))
        elif policy.execution_recovery:
            (project / "data.txt").write_text("probe input available", encoding="utf-8")
            responses = [call("ReadFile", {"file_path": str(project / "data.txt")}) for _ in range(7 if late_receipt else 1)] + [
                call("SubmitAssistanceReceipt", {"next_step": "resume", "notes": "primary confirmed input available"}),
                call("SubmitHandoff", {"status": "completed", "summary": "primary confirmed input available"})]
        elif role == "planner" and sum(r == "planner" for r, _, _ in built) == 1:
            responses = [call("ReadFile", {"file_path": str(project / "data.txt")}),
                call("RequestAssistance", {"kind": "capability", "need": "read input", "requested_tools": ["ReadFile"]})]
        elif role == "planner":
            responses = [call("ReadFile", {"file_path": str(project / "data.txt")}),
                call("SubmitHandoff", {"status": "completed", "summary": "original node verified input"})]
        else:
            responses = [call("SubmitHandoff", {"status": "completed", "summary": "done"})]
        responses.append([StreamEnd(stop_reason="end_turn", input_tokens=1, output_tokens=1)])
        tools = _role_registry(session, role, policy)
        if analysis:
            from mewcode.tools.submit_recovery_plan import SubmitRecoveryPlan
            tools.register(SubmitRecoveryPlan(analysis['holder'], analysis['case_id'], analysis['round']))
        agent = Agent(MockLLMClient(responses), tools, "anthropic",
            work_dir=str(project), permission_checker=build_permission_checker(project, mode=PermissionMode.AUTO),
            stall_policy=False)
        if policy.execution_recovery and late_receipt:
            agent.max_iterations = 10
        return agent, None, SimpleNamespace(conversation=ConversationManager(), aclose=AsyncMock())

    monkeypatch.setattr(expert_web, "_build_role_agent", build)
    await expert_web.run_expert_chat(SimpleNamespace(send_json=AsyncMock(side_effect=events.append)),
        "inspect local data", session=session, confirmed_new_flow=flow,
        registry_service=SimpleNamespace(catalog=AsyncMock(return_value=grok_catalog()), resolve=lambda _: None),
        profile_store=SimpleNamespace(get=lambda _: _profile()), flow_store=flows,
        plan_store=PlanSessionStore(tmp_path / "plans.json"), run_store=runs)
    snapshot = runs.load(next(e["run_id"] for e in events if e.get("run_id")))
    assert snapshot["status"] == "completed", [(e.get("type"), e.get("error")) for e in events]
    fixes = [(model, policy) for _, model, policy in built if policy.execution_recovery]
    assert len(fixes) == 1 and fixes[0][0].as_dict() == {
        "provider_key": "selected-primary", "model_id": "primary-model", "effort": "high"}
    parent = snapshot["node_states"]["work"]
    assert parent["status"] == "completed" and parent["passed"]
    assert parent['recovery_case']['failed_step']['tool'] == 'ReadFile'
    assert parent["assistance_receipts"][-1]["next_step"] == "resume"
    helper = next(s for k, s in snapshot["node_states"].items() if k.startswith("work__assist_"))
    reminders = [m for m in helper['messages'] if '80%' in str(m.get('content', ''))]
    assert len(reminders) == int(late_receipt)
    assert any(t["name"] == "ReadFile" and t["status"] == "completed" for t in helper["tools"].values())
    assert any(t["name"] == "ReadFile" and "probe input available" in t.get("output", "")
               for t in parent["tools"].values())
    assert not any(e.get("type") == "ask_user" for e in events)
    analyst = next(s for k, s in snapshot['node_states'].items() if '__analyst__' in k)
    from mewcode.experts.recovery_analyst import ANALYSIS_CONTEXT_VERSION
    assert analyst['analysis_context_version'] == ANALYSIS_CONTEXT_VERSION
    assert not any(t.get('name') == 'ReadFile' for t in analyst['tools'].values())
    assert '已达到本循环' not in str(analyst['messages']) + analyst.get('partial_text', '')
    archives = analyst.get('analysis_context_archives', [])
    assert len(archives) == int(legacy_analysis)
    if legacy_analysis:
        assert '已达到本循环' in str(archives[0]['messages'])
        assert archives[0]['partial_text'] == '已达到本循环剩余 Token 预算。' * 6
