"""Graph design is a workflow; local probes use the selected execution policy."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from frontend import server
from mewcode.orchestration.task_design import TaskDesign
from mewcode.orchestration.workflow_blocks import WorkflowBlocks
from mewcode.permissions import PermissionChecker, PermissionMode, PathSandbox, DangerousCommandDetector, RuleEngine, Rule
from mewcode.tools.base import ToolCallComplete, ToolResult
from mewcode.tools.bash import Bash
from mewcode.tools.task_design import AdvanceTaskDesign
from mewcode.tools.workflow_blocks import SubmitWorkflowBlocks
from tests.test_permission_three_levels import _runtime_session
from tests.test_web_slash_commands import _session


@pytest.mark.parametrize("mode", ["default", "auto", "bypassPermissions"])
def test_design_preserves_selected_policy_in_session_agent_and_runtime(monkeypatch, tmp_path, mode):
    session, runtime = _runtime_session(monkeypatch, tmp_path)
    session.set_permission_mode(mode)
    assert session.set_orchestration_planning(True) is None
    assert session.permission_mode.value == runtime.agent.permission_mode.value == mode
    assert runtime.agent.permission_checker.mode.value == mode
    assert runtime.orchestration_planning and runtime.agent.orchestration_planning
    assert not session.status_payload()["planning"]
    assert not runtime.status_payload()["planning"]
    assert session.set_orchestration_planning(False) is None
    assert runtime.agent.permission_mode.value == mode
    session._close_session()


@pytest.mark.parametrize("stored,expected", [
    ({"permission_before_orch": "auto", "permission_before_plan": "auto"}, "auto"),
    ({"permission_before_plan": "default"}, "default"),
    ({}, "default"),
])
def test_old_design_plan_overlay_migrates_from_its_own_saved_conversation(monkeypatch, tmp_path, stored, expected):
    session = _session(monkeypatch, tmp_path)
    cid = session.session.session_id
    session.permission_before_orch = "bypassPermissions"
    session.web_state["permission_before_orch"] = "bypassPermissions"
    session.conversation_settings[cid] = {"permission_mode": "plan", "orchestration_planning": True, **stored}
    session._restore_conversation_settings(cid)
    assert session.permission_mode.value == expected
    assert session.orchestration_planning and session.permission_before_orch == ""
    assert session.conversation_settings[cid]["permission_mode"] == expected
    session._sync_orchestration_flag()
    assert session.permission_mode.value == expected
    session._close_session()


def test_live_legacy_runtime_migrates_but_later_permission_choice_is_preserved(monkeypatch, tmp_path):
    session, runtime = _runtime_session(monkeypatch, tmp_path)
    session.permission_mode = PermissionMode.PLAN
    runtime.agent.set_permission_mode(PermissionMode.PLAN)
    session.orchestration_planning = True
    session.permission_before_orch = "auto"
    session._sync_orchestration_flag()
    assert session.permission_mode == runtime.agent.permission_mode == PermissionMode.AUTO
    session.set_permission_mode("default")
    session._sync_orchestration_flag()
    session.set_orchestration_planning(False)
    assert session.permission_mode == runtime.agent.permission_mode == PermissionMode.DEFAULT
    session._close_session()


@pytest.mark.parametrize("entry", ["setter", "slash", "runtime_slash"])
def test_explicit_plan_selects_separate_readonly_workflow(monkeypatch, tmp_path, entry):
    session, runtime = _runtime_session(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "SESSION", session)
    session.set_orchestration_planning(True)
    design = TaskDesign(session.work_dir, runtime.conversation_id)
    current = design.start("execute")
    queued = design.transition(current, "queued", build_request={"sequence": 1})
    if entry == "setter":
        assert session.set_permission_mode("plan") is None
    elif entry == "slash":
        assert session.execute_web_command("plan")["ok"]
    else:
        assert session.execute_web_command_for(runtime.conversation_id, "plan")["ok"]
    session._sync_orchestration_flag()
    assert session.permission_mode == runtime.agent.permission_mode == PermissionMode.PLAN
    assert not session.orchestration_planning and not runtime.orchestration_planning
    assert design.read()["status"] == "paused"
    assert design.read()["build_request"]["state"] == "paused"
    assert design.read()["build_request"]["epoch"] > queued["build_request"].get("epoch", 0)
    with pytest.raises(ValueError):
        design.claim_build(queued)
    server.task_design_after_turn(runtime)
    assert design.read()["status"] == "paused"
    denied = runtime.agent.permission_checker.check(Bash(), {"command": "python -c 'print(1)'"})
    assert denied.effect == "deny" and denied.source == "plan_mode"
    session.set_orchestration_planning(True)
    assert runtime.agent.permission_mode == PermissionMode.DEFAULT
    session._close_session()


@pytest.mark.asyncio
async def test_explicit_plan_cancels_inflight_design_without_applying_or_activating(monkeypatch, tmp_path):
    session, runtime = _runtime_session(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "SESSION", session)
    session.set_orchestration_planning(True)
    runtime.starting = False
    design = TaskDesign(session.work_dir, runtime.conversation_id)
    queued = design.transition(design.start("execute"), "queued", build_request={"sequence": 1})
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def build(*args, **kwargs):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    apply = AsyncMock(side_effect=AssertionError("Plan must not apply queued designs"))
    confirm = AsyncMock(side_effect=AssertionError("Plan must not activate queued designs"))
    monkeypatch.setattr(server, "task_design_build_graph", build)
    monkeypatch.setattr(server, "task_design_apply_graph", apply)
    monkeypatch.setattr(server, "api_orchestration_confirm", confirm)
    monkeypatch.setattr(server.WS_EVENTS, "send_json", AsyncMock())
    server.task_design_after_turn(runtime)
    await asyncio.wait_for(entered.wait(), 3)
    assert session.execute_web_command_for(runtime.conversation_id, "plan")["ok"]
    await asyncio.wait_for(cancelled.wait(), 3)
    release.set()
    assert design.read()["status"] == "paused"
    assert design.read()["build_request"]["state"] == "paused"
    assert design.read()["build_request"]["epoch"] > queued["build_request"].get("epoch", 0)
    with pytest.raises(ValueError):
        design.claim_build(queued)
    apply.assert_not_awaited()
    confirm.assert_not_awaited()
    session._close_session()


@pytest.mark.asyncio
async def test_interpreter_probe_can_run_after_design_entry_under_existing_permission(monkeypatch, tmp_path):
    session, runtime = _runtime_session(monkeypatch, tmp_path)
    command = "python -c 'print(1)'"
    checker = runtime.agent.permission_checker
    checker.rule_engine.append_session_rule(Rule("Bash", command, "allow"))
    session.set_permission_mode("plan")
    assert checker.check(Bash(), {"command": command}).effect == "deny"
    session.set_orchestration_planning(True)
    runner = AsyncMock(return_value=ToolResult("1"))
    runtime.agent.registry.get("Bash").run = runner
    result = await runtime.agent._execute_tool_noninteractive(ToolCallComplete("probe", "Bash", {"command": command}))
    assert result.output == "1" and not result.is_error
    runner.assert_awaited_once()
    # Removing the Plan overlay never overrides explicit operation restrictions.
    checker.rule_engine.append_session_rule(Rule("Bash", "restricted-probe", "deny"))
    assert checker.check(Bash(), {"command": "restricted-probe"}).effect == "deny"
    session._close_session()


@pytest.mark.parametrize("mode", [PermissionMode.DEFAULT, PermissionMode.AUTO, PermissionMode.PLAN])
@pytest.mark.parametrize("tool_class", [SubmitWorkflowBlocks, AdvanceTaskDesign])
def test_local_graph_state_remains_host_scoped_without_extra_permission_cards(tmp_path, mode, tool_class):
    blocks = WorkflowBlocks(tmp_path, "fixture")
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(tmp_path)), RuleEngine(), mode)
    tool = tool_class(blocks)
    arguments = ({"action": "build"} if tool_class is AdvanceTaskDesign else
        {"use_jev": False, "expected_sequence": 0, "source_hash": "", "base_graph_hash": ""})
    assert checker.check(tool, arguments).effect == "allow"
    wrong = tool_class(WorkflowBlocks(tmp_path / "other", "fixture"))
    denied = checker.check(wrong, arguments)
    assert denied.effect == "deny" and denied.source == "host_tool_scope"


def test_design_state_fastpath_keeps_ask_rules_and_paid_advice_on_normal_policy(tmp_path):
    blocks = WorkflowBlocks(tmp_path, "fixture")
    rules = RuleEngine()
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(tmp_path)), rules, PermissionMode.DEFAULT)
    tool = SubmitWorkflowBlocks(blocks)
    arguments = {"expected_sequence": 0, "source_hash": "", "base_graph_hash": ""}
    rules.append_session_rule(Rule(tool.name, "*", "ask"))
    assert checker.check(tool, {**arguments, "use_jev": False}).effect == "ask"
    checker.rule_engine = RuleEngine()
    tool = SubmitWorkflowBlocks(blocks, judgment_runtime=SimpleNamespace())
    decision = checker.check(tool, {**arguments, "use_jev": True})
    assert decision.effect != "allow" and decision.source != "workflow_design"


@pytest.mark.asyncio
async def test_real_task_entry_keeps_graph_only_intent_without_plan_permission(monkeypatch, tmp_path):
    session = _session(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "SESSION", session)
    session.set_permission_mode("auto")
    session.execute_web_command("plan")
    owner = {"project": session.work_dir, "conversation_id": session.session.session_id}
    launch = AsyncMock(side_effect=AssertionError("design entry must not start business execution"))
    monkeypatch.setattr(server, "start_runtime_turn", launch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.post("/api/orchestration/task-design", json={**owner, "action": "start", "intent": "plan"})
    assert response.status_code == 200, response.text
    assert session.permission_mode == PermissionMode.AUTO and session.orchestration_planning
    assert TaskDesign(owner["project"], owner["conversation_id"]).read()["intent"] == "plan"
    launch.assert_not_awaited()
    session._close_session()
