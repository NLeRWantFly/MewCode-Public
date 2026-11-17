from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.agents.task_manager import BackgroundTask, TaskManager
from mewcode.agents.tool_filter import resolve_agent_tools
from mewcode.agents.parser import AgentDef
from mewcode.tools import create_default_registry
from mewcode.tools.agent_status import AgentStatus, BackgroundTasks, TaskRetry
from mewcode.tools.base import ToolResult
from mewcode.tools.terminal import TerminalSessions, TerminalStart, TerminalRead, TerminalInput


def parent(tmp_path):
    registry = create_default_registry()
    registry.register(SimpleNamespace(name="Agent"))
    return SimpleNamespace(session_id="owner", work_dir=str(tmp_path), registry=registry, permission_mode="auto")


def record(tmp_path, **kw):
    return BackgroundTask(id="old", name="verify", agent=None, task="check",
                          owner_session_id="owner", owner_store_dir=str(tmp_path), **kw)


@pytest.mark.asyncio
async def test_diagnostics_show_disabled_not_missing(tmp_path):
    agent = parent(tmp_path)
    agent.registry.disable("Agent")
    tool = AgentStatus(agent, TaskManager())
    data = json.loads((await tool.execute(tool.params_model())).output)
    assert data["tools"]["Agent"] is False
    assert data["tools"]["TerminalStart"] is True
    assert data["model_availability"] == "not_probed"


@pytest.mark.asyncio
async def test_status_exposes_live_configuration_without_secrets_or_probes(tmp_path):
    agent = parent(tmp_path)
    agent.client = SimpleNamespace(model="gpt-6-luna", effort="max",
        _usage_provider_name="codex-custom", api_key="never-expose-secret",
        base_url="https://secret-endpoint.invalid", list_models=AsyncMock())
    tool = AgentStatus(agent)
    data = json.loads((await tool.execute(tool.params_model())).output)
    assert data["host_work_dir"] == str(tmp_path.resolve())
    assert data["runtime_route"] == {
        "provider_name": "codex-custom", "configured_model": "gpt-6-luna", "effort": "max",
        "source": "active_client_configuration", "live_verified": False,
    }
    assert "MewWork:" in data["workspace_note"]
    assert "never-expose" not in json.dumps(data)
    assert "secret-endpoint" not in json.dumps(data)
    agent.client.list_models.assert_not_awaited()
    agent.client = SimpleNamespace(model="gpt-6-sol", effort="xhigh", _usage_provider_name="other")
    updated = json.loads((await tool.execute(tool.params_model())).output)
    assert updated["runtime_route"]["configured_model"] == "gpt-6-sol"
    assert updated["runtime_route"]["effort"] == "xhigh"
    assert updated["runtime_route"]["provider_name"] == "other"


@pytest.mark.asyncio
async def test_status_does_not_invent_route_or_host_directory(tmp_path):
    agent = parent(tmp_path)
    agent.work_dir = "."
    tool = AgentStatus(agent)
    data = json.loads((await tool.execute(tool.params_model())).output)
    assert data["host_work_dir"] is None
    assert data["runtime_route"]["configured_model"] is None
    assert data["runtime_route"]["provider_name"] is None
    assert data["runtime_route"]["effort"] is None
    assert data["model_availability"] == "not_probed"


@pytest.mark.asyncio
async def test_wait_does_not_cancel_and_result_ack_is_explicit(tmp_path):
    agent, manager = parent(tmp_path), TaskManager()
    task = record(tmp_path)
    manager._tasks[task.id] = task
    event = asyncio.Event()
    handle = asyncio.create_task(event.wait())
    manager._async_tasks[task.id] = handle
    tool = BackgroundTasks(agent, manager)
    try:
        result = await tool.execute(tool.params_model(task_id="old", wait_seconds=.01, acknowledge=True))
        assert json.loads(result.output)["status"] == "running"
        assert not handle.cancelled() and not task.notified
        task.status, task.result = "completed", "PASS"
        result = await tool.execute(tool.params_model(task_id="old", acknowledge=True))
        assert json.loads(result.output)["result"] == "PASS"
        assert task.notified
    finally:
        event.set()
        await handle


@pytest.mark.asyncio
async def test_retry_preserves_request_and_rejects_duplicates(tmp_path):
    manager = TaskManager(tmp_path / "tasks.json")
    spec = dict(prompt="check files", description="verify", subagent_type="Verification",
                model="chosen-model", provider_key="chosen-provider", effort="high", run_in_background=True)
    old = record(tmp_path, status="running", recovery_spec=spec)
    manager._tasks[old.id] = old
    manager._persist_state()
    restored = TaskManager(tmp_path / "tasks.json")
    assert restored.get("old").status == "failed"
    delegate = SimpleNamespace(execute=AsyncMock(return_value=ToolResult("Task ID: abcd1234")))
    tool = TaskRetry(parent(tmp_path), restored, delegate)
    assert (await tool.execute(tool.params_model(task_id="old"))).is_error
    result = await tool.execute(tool.params_model(task_id="old", side_effects_checked=True))
    assert not result.is_error
    assert delegate.execute.call_args.args[0].model == "chosen-model"
    assert restored.get("old").retry_task_id == "abcd1234"
    assert (await tool.execute(tool.params_model(task_id="old", side_effects_checked=True))).is_error
    assert delegate.execute.await_count == 1
    assert TaskManager(tmp_path / "tasks.json").get("old").retry_task_id == "abcd1234"


def test_restart_keeps_only_actual_execution_time_for_new_background_task(tmp_path):
    import time

    path = tmp_path / "tasks.json"
    manager = TaskManager(path)
    old_spawn = time.monotonic() - 3540
    task = record(tmp_path, status="running", start_time=old_spawn,
                  duration_basis="active_runtime", duration_quality="measured",
                  execution_elapsed_seconds=2.5)
    manager._tasks[task.id] = task
    manager._persist_state()
    saved = json.loads(path.read_text(encoding="utf-8"))["tasks"][0]
    assert saved["duration_basis"] == "active_runtime"
    assert saved["elapsed"] == 2.5
    restored = TaskManager(path)
    assert restored.get("old").status == "failed"
    assert restored.task_payload("old")["elapsed"] == 2.5
    assert restored.task_payload("old")["duration_quality"] == "lower_bound"
    assert "web service restarted" in restored.get("old").result
    twice = TaskManager(path)
    assert twice.task_payload("old")["duration_quality"] == "lower_bound"


def test_legacy_background_wall_time_is_not_labeled_as_execution_time(tmp_path):
    import time

    manager = TaskManager(tmp_path / "tasks.json")
    task = record(tmp_path, status="completed", start_time=time.monotonic() - 3540,
                  end_time=time.monotonic())
    manager._tasks[task.id] = task
    manager._persist_state()
    restored = TaskManager(tmp_path / "tasks.json")
    payload = restored.task_payload("old")
    assert payload["duration_basis"] == "legacy_wall"
    assert payload["duration_quality"] == "unknown"
    assert payload["elapsed"] is None
    assert payload["legacy_wall_elapsed"] > 3500


@pytest.mark.asyncio
async def test_task_access_is_scoped_and_disabled_retry_rejected(tmp_path):
    manager = TaskManager()
    manager._tasks["old"] = record(tmp_path, status="failed", recovery_spec={"prompt": "x"})
    agent = parent(tmp_path)
    agent.session_id = "other"
    tool = BackgroundTasks(agent, manager)
    assert (await tool.execute(tool.params_model(task_id="old"))).is_error
    empty = json.loads((await tool.execute(tool.params_model())).output)
    assert empty["task_count"] == 0 and empty["tasks"] == []
    agent.session_id = "owner"
    agent.registry.disable("Agent")
    retry = TaskRetry(agent, manager, SimpleNamespace(execute=AsyncMock()))
    assert (await retry.execute(retry.params_model(task_id="old", side_effects_checked=True))).is_error


@pytest.mark.asyncio
async def test_terminal_real_process_incremental_output_and_stdin(tmp_path, monkeypatch):
    # Test the session transport independently of platform shell quoting.
    monkeypatch.setattr("mewcode.tools.terminal._shell_command", lambda _: (sys.executable, ["-u", "-c", "print('ready', flush=True); print(input(), flush=True)"]))
    sessions = TerminalSessions()
    start, read, send = TerminalStart(sessions), TerminalRead(sessions), TerminalInput(sessions)
    try:
        result = await start.run(start.params_model(command="test", lifetime_seconds=10), work_dir=str(tmp_path))
        assert not result.is_error, result.output
        key = json.loads(result.output)["session_id"]
        result = await send.run(send.params_model(session_id=key, text="hello\n"), work_dir=str(tmp_path))
        assert not result.is_error
        data = json.loads((await read.run(read.params_model(session_id=key, wait_seconds=5), work_dir=str(tmp_path))).output)
        assert "ready" in data["output"] and "hello" in data["output"]
        assert data["exit_code"] == 0
        second = json.loads((await read.run(read.params_model(session_id=key, offset=data["next_offset"]), work_dir=str(tmp_path))).output)
        assert second["output"] == ""
        assert (await read.run(read.params_model(session_id=key), work_dir=str(tmp_path / "other"))).is_error
    finally:
        await sessions.aclose()


@pytest.mark.asyncio
async def test_terminal_lifetime_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr("mewcode.tools.terminal._shell_command", lambda _: (sys.executable, ["-c", "import time; time.sleep(20)"]))
    sessions = TerminalSessions()
    start, read = TerminalStart(sessions), TerminalRead(sessions)
    try:
        result = await start.run(start.params_model(command="test", lifetime_seconds=1), work_dir=str(tmp_path))
        assert not result.is_error, result.output
        key = json.loads(result.output)["session_id"]
        data = json.loads((await read.run(read.params_model(session_id=key, wait_seconds=10), work_dir=str(tmp_path))).output)
        assert data["timed_out"] and data["exit_code"] is not None
    finally:
        await sessions.aclose()


@pytest.mark.asyncio
async def test_verification_reports_nonzero_and_workspace(tmp_path, monkeypatch):
    from mewcode.tools.run_verification import RunVerification
    seen = {}
    async def run(*argv, **kw):
        seen.update(kw)
        seen["argv"] = argv
        return SimpleNamespace(returncode=1, communicate=AsyncMock(return_value=(b"assertion failed", None)))
    monkeypatch.setattr("mewcode.tools.run_verification.asyncio.create_subprocess_exec", run)
    tool = RunVerification()
    result = await tool.run(tool.params_model(command="pytest tests"), work_dir=str(tmp_path))
    data = json.loads(result.output)
    assert result.is_error and data["status"] == "FAIL" and data["exit_code"] == 1
    assert seen["cwd"] == str(tmp_path)
    assert seen["argv"][-1] == "tests"
    assert data["output"] == "assertion failed"
    assert tool.category == "command"


def test_background_filter_keeps_browser_and_validation():
    registry = create_default_registry()
    registry.register(SimpleNamespace(name="browser_snapshot", interaction_kind="browser"))
    registry.register(SimpleNamespace(name="mcp_server_read", interaction_kind=""))
    definition = AgentDef(agent_type="Tester", when_to_use="test", system_prompt="test")
    filtered = resolve_agent_tools(registry, definition, is_background=True)
    for name in ("browser_snapshot", "mcp_server_read", "RunVerification", "InspectGitState", "TerminalRead"):
        assert filtered.is_enabled(name)


def test_multi_toggle_restores_all_control_tools():
    from frontend.server import _set_multi_agent_tools
    agent = SimpleNamespace(registry=create_default_registry())
    for name in ("Agent", "TaskStop", "TaskRetry", "TeamCreate", "TeamDelete"):
        agent.registry.register(SimpleNamespace(name=name))
    _set_multi_agent_tools(agent, False)
    assert not agent.registry.is_enabled("TaskStop")
    _set_multi_agent_tools(agent, True)
    assert agent.registry.is_enabled("TaskStop") and agent.registry.is_enabled("TaskRetry")


def test_delivery_skills_discoverable(tmp_path):
    from mewcode.skills.loader import SkillLoader
    skills = SkillLoader(str(tmp_path)).load_all()
    for name in ("debug-regression", "multi-agent-delivery", "model-api-acceptance", "safe-service-recovery", "document-evidence-audit"):
        assert name in skills and skills[name].prompt_body and skills[name].description


def test_terminal_input_is_not_mistaken_for_readonly_command(tmp_path):
    from mewcode.permissions import PermissionChecker, PermissionMode, DangerousCommandDetector, PathSandbox, RuleEngine
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(tmp_path)), RuleEngine(), PermissionMode.AUTO)
    tool = TerminalInput(TerminalSessions())
    decision = checker.check(tool, {"session_id": "test", "text": "git status"})
    assert decision.effect == "deny"


@pytest.mark.asyncio
async def test_browser_reconnect_available_after_initial_failure(monkeypatch):
    from mewcode.cordis import Context
    from mewcode.plugins.features import mcp_plugin
    from mewcode.plugins.services import TOOLS, TOOL_SLOT
    from mewcode.tools import ToolRegistry
    from mewcode.plugins.runtime import _bind_tool_slot
    context = Context()
    context.slots.declare(TOOL_SLOT, kind="keyed", owner="test")
    registry = ToolRegistry()
    context.provide(TOOLS, registry)
    _bind_tool_slot(context, registry)
    monkeypatch.setattr("mewcode.mcp.manager.MCPManager.register_all_tools", AsyncMock(return_value=["connection failed"]))
    # Invoke plugin apply directly, including its registered disposers.
    cleanup = await mcp_plugin([]).apply(context)
    try:
        assert registry.is_enabled("browser_session")
        reconnect = AsyncMock(return_value=[])
        monkeypatch.setattr("mewcode.mcp.manager.MCPManager.replace_server", reconnect)
        tool = registry.get("browser_session")
        result = await tool.execute(tool.params_model(mode="ephemeral"))
        assert not result.is_error
        reconnect.assert_awaited_once()
    finally:
        await cleanup()
        await context.aclose()
