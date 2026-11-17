"""Public levels, recovery and command routing; never call real providers."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from frontend import server
from mewcode.agent import Agent, PermissionRequest
from mewcode.config import AppConfig, _merge_config, _load_single_file
from mewcode.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, Rule, RuleEngine
from mewcode.permissions.modes import PUBLIC_PERMISSION_MODES, PERMISSION_MODE_LABELS, public_permission_mode
from mewcode.permissions.policy import build_permission_checker
from mewcode.permissions.reviewer import SafetyReviewResult
from mewcode.experts.node_expansion import ExpansionPathSandbox
from mewcode.tools.base import ToolCallComplete, ToolResult
from mewcode.tools.bash import Bash
from mewcode.tools import create_default_registry
from mewcode.tools.run_verification import RunVerification
from tests.test_permissions import MockLLMClient
from tests.test_web_slash_commands import _session


@pytest.mark.parametrize("old", ["acceptEdits", "custom", "dontAsk", "strictIsolation", "plan", "bad", None])
def test_retired_main_levels_migrate_conservatively(old):
    assert public_permission_mode(old) == PermissionMode.DEFAULT
    assert public_permission_mode("plan", allow_plan=True) == PermissionMode.PLAN
    assert len(PUBLIC_PERMISSION_MODES) == 4
    assert list(PERMISSION_MODE_LABELS.values()) == ["授权运行", "计划（Plan）", "Auto模式", "完全放行【危险操作停止】"]


@pytest.mark.parametrize("mode", PUBLIC_PERMISSION_MODES)
@pytest.mark.parametrize("command", ["rm -rf /", "format C:"])
def test_danger_stops_even_with_explicit_allow(mode, command, tmp_path):
    rules = RuleEngine()
    rules.append_session_rule(Rule("Bash", command, "allow"))
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(tmp_path)), rules, mode)
    assert checker.check(Bash(), {"command": command}).effect == "deny"


@pytest.mark.parametrize("mode,expected", [(PermissionMode.DEFAULT, "ask"),
    (PermissionMode.PLAN, "deny"), (PermissionMode.AUTO, "review"), (PermissionMode.BYPASS, "allow")])
@pytest.mark.parametrize("command", ["git push --force origin main", "terraform destroy", "DROP DATABASE production"])
def test_authorizable_commands_follow_mode_without_becoming_invariants(mode, expected, command, tmp_path):
    rules = RuleEngine()
    rules.append_session_rule(Rule("Bash", command, "allow"))
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(tmp_path)), rules, mode)
    decision = checker.check(Bash(), {"command": command})
    assert decision.effect == expected
    if mode == PermissionMode.AUTO:
        assert decision.risk["specific_authority_required"]


@pytest.mark.parametrize("tool", [Bash(), RunVerification()])
def test_unsettled_transaction_blocks_host_commands(tool, tmp_path, monkeypatch):
    monkeypatch.setattr("mewcode.sandbox.transaction_registry.workspace_write_block", lambda _: "unsettled transaction")
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(tmp_path)), RuleEngine(), PermissionMode.BYPASS)
    decision = checker.check(tool, {"command": "python -m pytest"})
    assert (decision.effect, decision.source) == ("deny", "native_transaction_recovery")


@pytest.mark.parametrize("expansion", [False, True])
def test_child_contract_cannot_turn_into_host_shell(expansion, tmp_path, monkeypatch):
    monkeypatch.setattr("mewcode.sandbox.windows.native_runtime_ready", lambda: False)
    parent = build_permission_checker(tmp_path, mode=PermissionMode.AUTO)
    writable = tmp_path / "child-output"
    writable.mkdir()
    checker = build_permission_checker(tmp_path, parent_policy=parent, writable_scope=[writable])
    if expansion:
        checker.sandbox = ExpansionPathSandbox(parent.sandbox, [str(tmp_path / "only.txt")])
    assert checker.check(Bash(), {"command": "python -m pytest"}).effect == "deny"


@pytest.mark.asyncio
@pytest.mark.parametrize("interactive", [True, False])
@pytest.mark.parametrize("tool_name", ["Bash", "RunVerification"])
@pytest.mark.parametrize("allowed", [True, False])
async def test_auto_reviews_then_uses_host_only_if_allowed(interactive, tool_name, allowed, tmp_path, monkeypatch):
    from mewcode.tools.command_sandbox import native_auto_scope, sandbox_scope
    monkeypatch.setattr("mewcode.sandbox.windows.native_runtime_ready", lambda: False)
    monkeypatch.setattr("mewcode.sandbox.windows.native_fastpath_ready", lambda: False)
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(tmp_path)), RuleEngine(), PermissionMode.AUTO)
    registry = create_default_registry()

    async def host_only(*args, **kwargs):
        assert not native_auto_scope() and not sandbox_scope()
        return ToolResult("reviewed host command")

    runner = AsyncMock(side_effect=host_only)
    registry.get(tool_name).run = runner
    agent = Agent(MockLLMClient([]), registry, "anthropic", work_dir=str(tmp_path), permission_checker=checker)
    reviewer = AsyncMock(return_value=SafetyReviewResult("allow" if allowed else "deny", "medium", "fixture", inconclusive=not allowed))
    agent._review_tool = reviewer
    call = ToolCallComplete("reviewed-command", tool_name, {"command": "python -m pytest"})
    if interactive:
        events = [event async for event in agent._execute_tool(call)]
        assert not any(isinstance(event, PermissionRequest) for event in events)
        results = [event[0] for event in events if isinstance(event, tuple)]
        assert results and results[-1].is_error is not allowed
    else:
        result = await agent._execute_tool_noninteractive(call)
        assert result.is_error is not allowed
    reviewer.assert_awaited_once()
    assert runner.await_count == (1 if allowed else 0)


def test_config_explicit_default_overrides_global_full_permission(tmp_path):
    path = tmp_path / "config.yaml"
    provider = "providers:\n  - name: fixture\n    protocol: openai\n    base_url: https://example.com/v1\n    model: fixture\n"
    path.write_text(provider + "permission_mode: default\n", encoding="utf-8")
    override = _load_single_file(path)
    assert override.permission_mode_explicit
    merged = _merge_config(AppConfig(providers=[], permission_mode="bypassPermissions"), override)
    assert merged.permission_mode == "default"
    path.write_text(provider + "permission_mode: strictIsolation\n", encoding="utf-8")
    assert public_permission_mode(_load_single_file(path).permission_mode) == PermissionMode.DEFAULT


@pytest.mark.parametrize("old", ["acceptEdits", "custom", "dontAsk", "strictIsolation"])
def test_saved_conversation_does_not_restore_retired_level(old, monkeypatch, tmp_path):
    session = _session(monkeypatch, tmp_path)
    session.conversation_settings[session.session.session_id] = {"permission_mode": old}
    session._restore_conversation_settings(session.session.session_id)
    assert session.permission_mode == PermissionMode.DEFAULT
    assert session.set_permission_mode(old) is not None
    session._close_session()


def test_missing_plan_restore_fields_cannot_inherit_full_permission(monkeypatch, tmp_path):
    session = _session(monkeypatch, tmp_path)
    session.permission_before_plan = "bypassPermissions"
    session.permission_before_orch = "bypassPermissions"
    session.conversation_settings[session.session.session_id] = {"permission_mode": "plan"}
    session._restore_conversation_settings(session.session.session_id)
    assert session.permission_before_plan == "default"
    assert session.permission_before_orch == ""
    assert session.status_payload()["permission_level"] == "default"
    assert session.execute_web_command("do")["ok"]
    assert session.permission_mode == PermissionMode.DEFAULT
    session._close_session()


def test_orchestration_exit_ignores_other_conversation_global_restore(monkeypatch, tmp_path):
    session = _session(monkeypatch, tmp_path)
    session.web_state["permission_before_orch"] = "bypassPermissions"
    session.permission_mode = PermissionMode.PLAN
    session.orchestration_planning = True
    session.permission_before_plan = "default"
    session.permission_before_orch = ""
    assert session.set_orchestration_planning(False) is None
    assert session.permission_mode == PermissionMode.DEFAULT
    session._close_session()


def test_plan_keeps_base_level_and_public_setter_accepts_plan(monkeypatch, tmp_path):
    session = _session(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "SESSION", session)
    assert session.set_permission_mode("default") is None
    assert session.set_permission_mode("plan") is None
    session.execute_web_command("plan")
    session.execute_web_command("plan")
    status = session.status_payload()
    assert status["permission_level"] == "default" and status["planning"]
    assert server._orchestration_permission_mode({}) == public_permission_mode(server.SESSION.permission_mode).value
    session.execute_web_command("do")
    assert session.permission_mode == PermissionMode.DEFAULT
    session._close_session()


def _runtime_session(monkeypatch, tmp_path):
    session = _session(monkeypatch, tmp_path)
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(tmp_path)), RuleEngine(), PermissionMode.DEFAULT)
    session.client = MockLLMClient([])
    session.agent = Agent(session.client, create_default_registry(), "anthropic", work_dir=session.work_dir, permission_checker=checker)
    session.agent_runtime = SimpleNamespace(close=lambda: None)
    runtime = session.capture_conversation_runtime()
    runtime.starting = True
    return session, runtime


@pytest.mark.parametrize("orchestration", [False, True])
def test_plan_base_is_synchronized_with_captured_runtime(orchestration, monkeypatch, tmp_path):
    session, runtime = _runtime_session(monkeypatch, tmp_path)
    session.permission_before_plan = "bypassPermissions"
    runtime.permission_before_plan = "bypassPermissions"
    assert session.set_permission_mode("default") is None
    if orchestration:
        assert session.set_orchestration_planning(True) is None
        assert runtime.permission_before_orch == ""
        assert runtime.agent.permission_mode == PermissionMode.DEFAULT
    else:
        assert session.execute_web_command_for(runtime.conversation_id, "plan")["ok"]
    assert session.permission_before_plan == runtime.permission_before_plan == "default"
    assert session.status_payload()["permission_level"] == "default"
    assert runtime.status_payload()["permission_level"] == "default"
    record = session.conversation_settings[runtime.conversation_id]
    assert record["permission_before_plan"] == "default"
    if orchestration:
        assert record["permission_before_orch"] == ""
        session.permission_before_orch = runtime.permission_before_orch
        assert session.set_orchestration_planning(False) is None
    else:
        assert session.execute_web_command_for(runtime.conversation_id, "do")["ok"]
    assert runtime.agent.permission_mode == session.permission_mode == PermissionMode.DEFAULT
    session._close_session()


@pytest.mark.parametrize("command", ["git diff --ext-diff", "git show --textconv", "git status"])
def test_auto_host_commands_require_review_even_with_readonly_prefix(command, tmp_path, monkeypatch):
    monkeypatch.setattr("mewcode.sandbox.windows.native_runtime_ready", lambda: False)
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(tmp_path)), RuleEngine(), PermissionMode.AUTO)
    assert checker.check(Bash(), {"command": command}).effect == "review"


@pytest.mark.asyncio
@pytest.mark.parametrize("interactive", [True, False])
async def test_child_fails_closed_when_native_readiness_is_lost(interactive, tmp_path, monkeypatch):
    from mewcode.tools.command_sandbox import native_auto_scope
    parent = build_permission_checker(tmp_path, mode=PermissionMode.AUTO)
    writable = tmp_path / "child-output"
    writable.mkdir()
    checker = build_permission_checker(tmp_path, parent_policy=parent, writable_scope=[writable])
    from mewcode.permissions.network_policy import NetworkPolicy
    checker.network_policy = NetworkPolicy(False)
    # Child isolation stays required even if readiness disappears during check.
    readiness = iter([True, False])
    monkeypatch.setattr("mewcode.sandbox.windows.native_runtime_ready", lambda: next(readiness, False))
    command = "python -m pytest"
    checker.rule_engine.append_session_rule(Rule("Bash", command, "allow"))
    registry = create_default_registry()
    async def native_only(*args, **kwargs):
        assert native_auto_scope()
        return ToolResult("native required")
    runner = AsyncMock(side_effect=native_only)
    registry.get("Bash").run = runner
    agent = Agent(MockLLMClient([]), registry, "anthropic", work_dir=str(tmp_path), permission_checker=checker)
    call = ToolCallComplete("child-command", "Bash", {"command": command})
    if interactive:
        results = [event[0] async for event in agent._execute_tool(call) if isinstance(event, tuple)]
        assert results[-1].is_error and "AUTO_BOUNDARY_DENIED" in results[-1].output
    else:
        result = await agent._execute_tool_noninteractive(call)
        assert result.is_error and "AUTO_BOUNDARY_DENIED" in result.output
    runner.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("interactive", [True, False])
async def test_readonly_backend_becoming_ready_never_executes_unreviewed_host(interactive, tmp_path, monkeypatch):
    from mewcode.tools.command_sandbox import native_auto_scope
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(tmp_path)), RuleEngine(), PermissionMode.AUTO)
    readiness = iter([False, True])
    monkeypatch.setattr("mewcode.sandbox.windows.native_runtime_ready", lambda: next(readiness, True))
    registry = create_default_registry()
    async def reviewed_host(*args, **kwargs):
        assert not native_auto_scope()
        return ToolResult("reviewed host selected")
    runner = AsyncMock(side_effect=reviewed_host)
    registry.get("Bash").run = runner
    agent = Agent(MockLLMClient([]), registry, "anthropic", work_dir=str(tmp_path), permission_checker=checker)
    agent._review_tool = AsyncMock(return_value=SafetyReviewResult("allow", "medium", "fixture"))
    call = ToolCallComplete("git-driver-command", "Bash", {"command": "git diff --ext-diff"})
    if interactive:
        results = [event[0] async for event in agent._execute_tool(call) if isinstance(event, tuple)]
        assert results[-1].output == "reviewed host selected"
    else:
        assert (await agent._execute_tool_noninteractive(call)).output == "reviewed host selected"
    agent._review_tool.assert_awaited_once()
    runner.assert_awaited_once()
