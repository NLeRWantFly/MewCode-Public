"""Built-in feature plugins installed into an AgentRuntime scope."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mewcode.cordis import Context, PluginSpec
from mewcode.plugins.services import (
    AGENT,
    AGENT_CATALOG,
    TASKS,
    TASK_FACTORY,
    TEAMS,
    TOOL_SLOT,
    TRACES,
    TRACE_FACTORY,
    WORKTREES,
)


@dataclass
class MultiAgentOptions:
    provider: Any
    task_state_path: str | Path | None = None
    provider_registry: Any = None
    enable_fork: bool = False
    worktree_config: Any = None
    teammate_permission_mode: str = ""
    teammate_mode: str = "in-process"
    is_interactive: bool = False
    enable_coordinator_mode: bool = False
    enabled: bool = True
    child_event_callback: Any = None
    task_origin: str = "chat"


def multi_agent_plugin(
    options: MultiAgentOptions,
    *,
    name: str = "runtime.multi_agent",
) -> PluginSpec:
    def apply(ctx: Context) -> None:
        from mewcode.agents.task_manager import TaskManager
        from mewcode.agents.trace import TraceManager
        from mewcode.config import WorktreeConfig
        from mewcode.teams.manager import TeamManager
        from mewcode.tools.agent_tool import AgentTool
        from mewcode.tools.task_stop import TaskStopTool
        from mewcode.tools.agent_status import AgentStatus, BackgroundTasks, TaskRetry
        from mewcode.tools.team_create import TeamCreateTool
        from mewcode.tools.team_delete import TeamDeleteTool
        from mewcode.worktree import WorktreeManager
        from mewcode.worktree.binding import WorktreeSessionBinding
        from mewcode.tools.enter_worktree import EnterWorktreeTool
        from mewcode.tools.exit_worktree import ExitWorktreeTool

        agent = ctx.get(AGENT)
        task_factory = ctx.get(TASK_FACTORY, None)
        trace_factory = ctx.get(TRACE_FACTORY, None)
        task_manager = (
            task_factory.create(options.task_state_path)
            if task_factory is not None
            else TaskManager(options.task_state_path)
        )
        trace_manager = (
            trace_factory.create()
            if trace_factory is not None
            else TraceManager()
        )
        wt_cfg = options.worktree_config or WorktreeConfig()
        worktree_manager = WorktreeManager(
            repo_root=agent.work_dir,
            symlink_directories=wt_cfg.symlink_directories,
        )
        worktree_manager.session_binding = WorktreeSessionBinding(agent)
        for tool in (EnterWorktreeTool(worktree_manager), ExitWorktreeTool(worktree_manager)):
            ctx.slots.register(TOOL_SLOT, tool, item_key=tool.name)
        team_manager = TeamManager(
            worktree_manager=worktree_manager,
            trace_manager=trace_manager,
        )
        # v2 is explicitly opted in per fixed workspace, never inferred from
        # a model request or from another project's settings.
        from mewcode.teams.host import TeamHost
        from pathlib import Path
        team_host = None
        fixed_team_root = Path(__file__).resolve().parents[2]
        if isinstance(agent.work_dir, (str, Path)) and Path(agent.work_dir).resolve() == fixed_team_root:
            candidate = TeamHost(fixed_team_root)
            if candidate.enabled:
                team_host = candidate
                team_manager.configure_v2(team_host.service)
        ctx.provide(TASKS, task_manager)
        ctx.provide(TRACES, trace_manager)
        ctx.provide(WORKTREES, worktree_manager)
        ctx.provide(TEAMS, team_manager)

        agent_tool = AgentTool(
            agent_loader=ctx.get(AGENT_CATALOG),
            task_manager=task_manager,
            trace_manager=trace_manager,
            parent_agent=agent,
            enable_fork=options.enable_fork,
            provider_config=options.provider,
            provider_registry=options.provider_registry,
            worktree_manager=worktree_manager,
            team_manager=team_manager,
            teammate_permission_mode=options.teammate_permission_mode,
            child_event_callback=options.child_event_callback,
            task_origin=options.task_origin,
        )
        tools = (
            agent_tool,
            TaskStopTool(task_manager),
            TaskRetry(agent, task_manager, agent_tool),
            TeamCreateTool(
                team_manager=team_manager,
                parent_agent=agent,
                teammate_mode=options.teammate_mode,
                is_interactive=options.is_interactive,
                enable_coordinator_mode=options.enable_coordinator_mode,
                tool_installer=agent._install_runtime_tool,
            ),
            TeamDeleteTool(team_manager=team_manager, parent_agent=agent),
        )
        for tool in tools:
            ctx.slots.register(TOOL_SLOT, tool, item_key=tool.name)
        if not options.enabled:
            for tool in tools:
                agent.registry.disable(tool.name)

        for tool in (AgentStatus(agent, task_manager), BackgroundTasks(agent, task_manager)):
            ctx.slots.register(TOOL_SLOT, tool, item_key=tool.name)

        def notifications() -> list[str]:
            return team_manager.drain_lead_mailbox()

        agent.notification_fn = notifications

        async def cleanup() -> None:
            agent.notification_fn = None
            try:
                await task_manager.settle_tasks(list(task_manager._async_tasks), cancel=True)
            finally:
                if team_host is not None:
                    team_host.close()

        ctx.effect(cleanup)
        ctx.provide("multi_agent.tool", agent_tool)

    return PluginSpec(
        name,
        apply,
        purpose="Subagent, task, team, trace, and worktree runtime",
        source="builtin",
    )


def mcp_plugin(
    configs: list[Any],
    *,
    name: str = "runtime.mcp",
    include_builtin: bool = True,
) -> PluginSpec:
    async def apply(ctx: Context) -> Any:
        from mewcode.mcp.manager import MCPManager
        from mewcode.mcp.playwright_browser import (
            PLAYWRIGHT_SERVER_NAME,
            merge_mcp_server_configs,
        )
        from mewcode.plugins.services import TOOLS
        from mewcode.tools.browser_session import BrowserSessionTool

        merged, builtin = (
            merge_mcp_server_configs(configs)
            if include_builtin
            else (list(configs or []), False)
        )
        manager = MCPManager()
        manager.attach_context(ctx)
        manager.load_configs(merged)
        registry = ctx.get(TOOLS)
        errors = await manager.register_all_tools(registry)
        if (
            builtin
            and registry.get("browser_session") is None
        ):
            ctx.slots.register(
                TOOL_SLOT,
                BrowserSessionTool(manager, registry),
                item_key="browser_session",
            )
        ctx.provide("mcp", manager)
        ctx.provide("mcp.errors", errors)
        ctx.provide("mcp.builtin_playwright", builtin)
        return manager.shutdown

    return PluginSpec(
        name,
        apply,
        purpose="MCP clients and dynamically discovered tools",
        source="builtin",
    )


__all__ = ["MultiAgentOptions", "mcp_plugin", "multi_agent_plugin"]
