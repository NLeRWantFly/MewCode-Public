"""Worktree tools use the desktop's shared runtime and its live permission policy."""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from mewcode.agents.parser import AgentDef
from mewcode.agents.tool_filter import resolve_agent_tools, build_teammate_tools
from mewcode.config import ProviderConfig
from mewcode.permissions import PermissionMode
from mewcode.permissions.policy import build_permission_checker
from mewcode.plugins import ApplicationHost
from mewcode.plugins.features import MultiAgentOptions, multi_agent_plugin
from mewcode.plugins.runtime import AgentRuntimeRequest
from mewcode.plugins.services import AGENT_FACTORY, TASKS, WORKTREES, PERMISSIONS
from mewcode.tools.enter_worktree import EnterWorktreeParams, EnterWorktreeTool
from mewcode.tools.exit_worktree import ExitWorktreeParams, ExitWorktreeTool
from mewcode.tools.write_file import WriteFile, Params as WriteFileParams
from mewcode.worktree.binding import WorktreeSessionBinding
from tests.test_worktree import _init_git_repo
from tests.test_task_interrupt_lifecycle import _Agent, _Runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("multi_agent", [True, False])
async def test_shared_runtime_registers_worktree_tools_and_scopes_relative_writes(tmp_path, multi_agent):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    provider = ProviderConfig(name="test", protocol="openai-compat", base_url="http://localhost", model="test-model")
    client = SimpleNamespace(model="test-model", base_url="http://localhost", set_usage_recorder=lambda _: None)
    host = ApplicationHost(str(repo), discover_plugins=False)
    await host.astart()
    runtime = await host.context.get(AGENT_FACTORY).acreate(AgentRuntimeRequest(
        provider=provider, work_dir=str(repo), client=client, auxiliary_client=client,
        memory_manager=SimpleNamespace(load_always_on=lambda: "")))
    runtime.install(multi_agent_plugin(MultiAgentOptions(provider=provider, enabled=multi_agent)))
    original_checker = runtime.agent.permission_checker
    cwd = os.getcwd()
    try:
        enter, leave = runtime.registry.get("EnterWorktree"), runtime.registry.get("ExitWorktree")
        assert enter is not None and leave is not None
        assert runtime.registry.is_enabled("EnterWorktree")
        assert bool(runtime.registry.is_enabled("Agent")) == multi_agent
        result = await enter.execute(EnterWorktreeParams(name="isolated"))
        assert not result.is_error, result.output
        target = Path(runtime.agent.work_dir)
        assert target != repo and target.is_relative_to(repo)
        assert os.getcwd() == cwd
        assert runtime.agent.permission_checker.policy_profile == "worktree"
        assert runtime.context.get(PERMISSIONS) is runtime.agent.permission_checker
        assert runtime.agent.permission_checker.check(WriteFile(), {
            "file_path": str(repo / "leak.txt"), "content": "forbidden"}).effect == "deny"
        write = await WriteFile().run(WriteFileParams(file_path="only-in-tree.txt", content="isolated"),
                                      work_dir=runtime.agent.work_dir)
        assert not write.is_error, write.output
        assert (target / "only-in-tree.txt").read_text() == "isolated"
        assert not (repo / "only-in-tree.txt").exists()
        role = await host.context.get(AGENT_FACTORY).acreate(AgentRuntimeRequest(
            provider=provider, work_dir=str(target), client=client, auxiliary_client=client,
            permission_checker=runtime.agent.permission_checker,
            memory_manager=SimpleNamespace(load_always_on=lambda: ""),
            include_default_tools=False, inherit_parent_tools=False,
            extra_tools=(enter, leave)))
        try:
            role_manager = role.context.get(WORKTREES)
            assert role_manager is not runtime.context.get(WORKTREES)
            assert role_manager.session_binding.agent is role.agent
            assert role.registry.get("ExitWorktree")._manager.get_current_session() is None
            assert runtime.context.get(WORKTREES).get_current_session() is not None
        finally:
            await role.aclose()
        runtime.agent.permission_checker.task_id = "new-task"
        runtime.agent.permission_checker.task_authorization = None
        runtime.agent.set_permission_mode(PermissionMode.PLAN)
        result = await leave.execute(ExitWorktreeParams(action="keep"))
        assert not result.is_error, result.output
        assert runtime.agent.work_dir == str(repo)
        assert runtime.agent.permission_checker is original_checker
        assert runtime.context.get(PERMISSIONS) is original_checker
        assert original_checker.task_id == "new-task"
        assert original_checker.mode is PermissionMode.PLAN
        assert target.exists() and os.getcwd() == cwd
    finally:
        await runtime.aclose()
        await host.aclose()


@pytest.mark.asyncio
async def test_runtime_close_waits_for_owned_children_to_settle(tmp_path):
    provider = ProviderConfig(name="test", protocol="openai-compat", base_url="http://localhost", model="test-model")
    client = SimpleNamespace(model="test-model", base_url="http://localhost", set_usage_recorder=lambda _: None)
    host = ApplicationHost(str(tmp_path), discover_plugins=False)
    await host.astart()
    runtime = await host.context.get(AGENT_FACTORY).acreate(AgentRuntimeRequest(
        provider=provider, work_dir=str(tmp_path), client=client, auxiliary_client=client,
        memory_manager=SimpleNamespace(load_always_on=lambda: "")))
    runtime.install(multi_agent_plugin(MultiAgentOptions(provider=provider)))
    gate = asyncio.Event()
    child_scope = _Runtime(cleanup_gate=gate)
    child = _Agent(runtime=child_scope)
    manager = runtime.context.get(TASKS)
    task_id = manager.launch(child, "owned child")
    await child.started.wait()
    closing = asyncio.create_task(runtime.aclose())
    await child_scope.cleanup_started.wait()
    try:
        assert not closing.done()
        assert manager.get(task_id).cancellation_pending
    finally:
        gate.set()
        await closing
        await host.aclose()
    assert child_scope.closed and manager.get(task_id).status == "cancelled"


def test_child_and_teammate_never_inherit_lead_session_switches(tmp_path):
    from mewcode.tools import ToolRegistry
    from mewcode.worktree.manager import WorktreeManager
    registry = ToolRegistry()
    manager = WorktreeManager(str(tmp_path))
    registry.register(EnterWorktreeTool(manager))
    registry.register(ExitWorktreeTool(manager))
    definition = AgentDef(agent_type="test", when_to_use="test")
    for background in (False, True):
        child = resolve_agent_tools(registry, definition, background)
        assert child.get("EnterWorktree") is None and child.get("ExitWorktree") is None
    for backend in ("in-process", "tmux"):
        child = build_teammate_tools(registry, SimpleNamespace(), "team", "id", "child", backend)
        assert child.get("EnterWorktree") is None and child.get("ExitWorktree") is None


@pytest.mark.asyncio
async def test_unauthorized_worktree_target_is_rejected_before_git_or_writes(tmp_path):
    from mewcode.worktree.manager import WorktreeManager
    repo, allowed = tmp_path / "repo", tmp_path / "allowed"
    repo.mkdir()
    allowed.mkdir()
    agent = SimpleNamespace(work_dir=str(repo), permission_checker=build_permission_checker(
        repo, writable_scope=[allowed]))
    manager = WorktreeManager(str(repo))
    manager.session_binding = WorktreeSessionBinding(agent)
    result = await EnterWorktreeTool(manager).execute(EnterWorktreeParams(name="blocked"))
    assert result.is_error and "writable scope" in result.output
    assert not (repo / ".mewcode").exists()
    assert agent.work_dir == str(repo)
    assert manager.current_session is None
