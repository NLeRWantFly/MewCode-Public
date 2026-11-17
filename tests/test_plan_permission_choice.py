"""Plan is selectable, read-only, and distinct from execution authorization."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from frontend import server
from mewcode.app import MewCodeApp, _MODE_CYCLE
from mewcode.commands.handlers.permission import handle_permission
from mewcode.commands.handlers.do import handle_do
from mewcode.permissions import PermissionMode, Rule
from mewcode.permissions.modes import EXECUTION_PERMISSION_MODES
from mewcode.tools.bash import Bash
from mewcode.tools.read_file import ReadFile
from mewcode.tools.run_verification import RunVerification
from mewcode.tools.write_file import WriteFile
from tests.test_permission_three_levels import _runtime_session


@pytest.mark.parametrize("base", EXECUTION_PERMISSION_MODES)
def test_public_plan_choice_preserves_and_restores_execution_base(base, monkeypatch, tmp_path):
    session, runtime = _runtime_session(monkeypatch, tmp_path)
    session.set_permission_mode(base.value)
    for _ in range(2):
        assert session.set_permission_mode("plan") is None
    assert session.permission_mode == PermissionMode.PLAN
    assert session.permission_before_plan == runtime.permission_before_plan == base.value
    assert session.status_payload()["permission_mode"] == "plan"
    assert session.status_payload()["permission_level"] == base.value
    checker = runtime.agent.permission_checker
    assert checker.check(ReadFile(), {"file_path": str(tmp_path / "source.txt")}).effect == "allow"
    assert checker.check(WriteFile(), {"file_path": str(tmp_path / "source.txt"), "content": "change"}).effect == "deny"
    assert checker.check(Bash(), {"command": "git status"}).effect == "deny"
    assert session.execute_web_command_for(runtime.conversation_id, "do")["ok"]
    assert runtime.agent.permission_mode == session.permission_mode == base
    session._close_session()


@pytest.mark.parametrize("tool,args", [
    (WriteFile(), {"file_path": "source.txt", "content": "change"}),
    (Bash(), {"command": "git status"}),
    (RunVerification(), {"command": "python -m pytest"}),
])
def test_execution_allow_rules_cannot_override_plan_readonly(tool, args, monkeypatch, tmp_path):
    session, runtime = _runtime_session(monkeypatch, tmp_path)
    checker = runtime.agent.permission_checker
    checker.rule_engine.append_session_rule(Rule(tool.name, "*", "allow"))
    checker.grant_call(tool.name, args)
    session.set_permission_mode("plan")
    assert checker.check(tool, args).effect == "deny"
    session._close_session()


def test_plan_write_exception_is_bound_to_exact_current_file(monkeypatch, tmp_path):
    session, runtime = _runtime_session(monkeypatch, tmp_path)
    checker = runtime.agent.permission_checker
    session.set_permission_mode("plan")
    current = tmp_path / ".mewcode/plans/current.md"
    args = lambda path: {"file_path": str(path), "content": "plan"}
    assert checker.check(WriteFile(), args(current)).effect == "deny"
    checker.plan_file_path = str(current)
    assert checker.check(WriteFile(), args(current)).effect == "allow"
    assert checker.check(WriteFile(), args(current.parent / "another.md")).effect == "deny"
    assert checker.check(WriteFile(), args(tmp_path / "other/current.md")).effect == "deny"
    session._close_session()


@pytest.mark.parametrize("base", EXECUTION_PERMISSION_MODES)
def test_confirm_plan_choice_resolves_only_execution_base(base, monkeypatch):
    monkeypatch.setattr(server.SESSION, "permission_mode", PermissionMode.PLAN)
    monkeypatch.setattr(server.SESSION, "permission_before_plan", base.value)
    assert server._orchestration_permission_mode({"permission_mode": "plan"}) == base.value
    monkeypatch.setattr(server.SESSION, "permission_before_plan", "plan")
    assert server._orchestration_permission_mode({"permission_mode": "plan"}) == "default"


def _tui(initial):
    agent = SimpleNamespace(permission_mode=initial, permission_checker=None)
    agent.set_permission_mode = lambda mode: setattr(agent, "permission_mode", mode)
    ui = SimpleNamespace(agent=agent, _update_mode_label=lambda: None, refresh_status=lambda: None,
                         add_system_message=lambda text: None)
    ui.set_plan_mode = lambda enabled: MewCodeApp.set_plan_mode(ui, enabled)
    return agent, ui


def test_tui_mode_cycle_enters_and_exits_plan():
    agent, ui = _tui(PermissionMode.DEFAULT)
    sequence = []
    for _ in range(4):
        MewCodeApp.action_cycle_mode(ui)
        sequence.append(agent.permission_mode)
    assert sequence == [PermissionMode.PLAN, PermissionMode.AUTO, PermissionMode.BYPASS, PermissionMode.DEFAULT]
    assert ui._pre_plan_mode == PermissionMode.DEFAULT
    assert list(_MODE_CYCLE) == [PermissionMode.DEFAULT, PermissionMode.PLAN, PermissionMode.AUTO, PermissionMode.BYPASS]


@pytest.mark.asyncio
@pytest.mark.parametrize("base", EXECUTION_PERMISSION_MODES)
async def test_permission_command_uses_plan_transition_and_do_restores_base(base):
    agent, ui = _tui(base)
    context = SimpleNamespace(args="mode plan", agent=agent, ui=ui)
    await handle_permission(context)
    await handle_permission(context)
    assert agent.permission_mode == PermissionMode.PLAN and ui._pre_plan_mode == base
    await handle_do(context)
    assert agent.permission_mode == base


def test_cli_plan_flag_remains_plan(monkeypatch, tmp_path):
    import mewcode.__main__ as cli
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["mewcode", "--mode", "plan", "-p", "制定计划"])
    monkeypatch.setattr(cli.logging, "basicConfig", lambda **kwargs: None)
    monkeypatch.setattr(cli, "load_config", lambda: SimpleNamespace(raw_hooks=[]))
    monkeypatch.setattr(cli, "load_hooks", lambda hooks: [])
    run = AsyncMock()
    monkeypatch.setattr(cli, "_run_prompt", run)
    cli.main()
    assert run.await_args.args[1] == PermissionMode.PLAN
