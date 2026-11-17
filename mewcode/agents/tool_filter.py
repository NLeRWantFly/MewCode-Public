from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mewcode.tools import ToolRegistry, expand_tool_aliases

if TYPE_CHECKING:
    from mewcode.agents.parser import AgentDef
    from mewcode.teams.manager import TeamManager

ALL_AGENT_DISALLOWED_TOOLS: frozenset[str] = frozenset({
    "TaskOutput",
    "ExitPlanMode",
    "EnterPlanMode",
    "Agent",
    "AskUserQuestion",
    "human_oncall",
    "TaskStop",
    "TaskRetry",
    "Workflow",
    # These instances bind the lead's live session; children use dispatch
    # isolation='worktree' instead of mutating their parent's workspace.
    "EnterWorktree",
    "ExitWorktree",
})

CUSTOM_AGENT_DISALLOWED_TOOLS: frozenset[str] = frozenset({
    "TaskOutput",
    "ExitPlanMode",
    "EnterPlanMode",
    "Agent",
    "AskUserQuestion",
    "human_oncall",
    "TaskStop",
    "Workflow",
})

ASYNC_AGENT_ALLOWED_TOOLS: frozenset[str] = frozenset({
    "ReadFile",
    "WebSearch",
    "web_search",
    "TodoWrite",
    "Grep",
    "WebFetch",
    "web_fetch",
    "Browser",
    "browser",
    "Glob",
    "Bash",
    "EditFile",
    "WriteFile",
    "NotebookEdit",
    "Skill",
    "LoadSkill",
    "SyntheticOutput",
    "ToolSearch",
    "AgentStatus",
    "ProbeCapabilities",
    "DecideNextStep",
    "InspectToolHistory",
    "FindSkills",
    "InspectGitState",
    "RunVerification",
    "TerminalStart",
    "TerminalRead",
    "TerminalInput",
})

TEAMMATE_COORDINATION_TOOLS: frozenset[str] = frozenset({
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskUpdate",
    "SendMessage",
    "TeamRoster",
})

# 注意：不给 teammate CronCreate/CronDelete——定时任务的生命周期超过团队
# 本身，等于给了临时工一把能在离职后继续生效的钥匙
IN_PROCESS_TEAMMATE_ALLOWED_TOOLS: frozenset[str] = (
    ASYNC_AGENT_ALLOWED_TOOLS | TEAMMATE_COORDINATION_TOOLS
)

COORDINATOR_MODE_ALLOWED_TOOLS: frozenset[str] = frozenset({
    "Agent",
    "TaskStop",
    "SendMessage",
    "SyntheticOutput",
    "TeamCreate",
    "TeamDelete",
    # lead 需要共享任务板来派发/追踪工作（绑定实例在 TeamCreate 时注册）
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskUpdate",
    "TeamRoster",
})


def _is_mcp_tool(name: str) -> bool:
    return name.startswith(("mcp__", "mcp_"))


def resolve_agent_tools(
    parent_registry: ToolRegistry,
    definition: AgentDef,
    is_background: bool = False,
) -> ToolRegistry:
    all_tools = {t.name: t for t in parent_registry.list_tools() if parent_registry.is_enabled(t.name)}

    # 第 1 层：全局禁用工具
    for name in ALL_AGENT_DISALLOWED_TOOLS:
        all_tools.pop(name, None)

    # 第 2 层：自定义 agent 额外限制
    if definition.source in ("project", "user", "plugin"):
        for name in CUSTOM_AGENT_DISALLOWED_TOOLS:
            all_tools.pop(name, None)

    # 第 3 层：后台任务白名单
    if is_background:
        all_tools = {
            name: tool
            for name, tool in all_tools.items()
            if name in ASYNC_AGENT_ALLOWED_TOOLS or _is_mcp_tool(name) or getattr(tool, "interaction_kind", "") == "browser"
        }

    # 第 4 层：按 agent 定义中的禁用/允许列表过滤
    if definition.disallowed_tools:
        for name in expand_tool_aliases(set(definition.disallowed_tools)):
            all_tools.pop(name, None)
        if "*" in definition.disallowed_tools:
            all_tools.clear()

    if definition.tools:
        allowed_set = expand_tool_aliases(set(definition.tools))
        all_tools = {
            name: tool
            for name, tool in all_tools.items()
            if "*" in allowed_set or name in allowed_set
        }

    filtered = ToolRegistry()
    for tool in all_tools.values():
        filtered.register(tool)
        if parent_registry.is_discovered(tool.name):
            filtered.mark_discovered(tool.name)
    return filtered


def build_teammate_tools(
    parent_registry: ToolRegistry,
    team_manager: TeamManager,
    team_name: str,
    agent_id: str,
    agent_name: str,
    backend_type: str,
    definition: AgentDef | None = None,
) -> ToolRegistry:
    from mewcode.teams.models import BackendType
    from mewcode.tools.send_message import SendMessageTool
    from mewcode.tools.task_create import TaskCreateTool
    from mewcode.tools.task_get import TaskGetTool
    from mewcode.tools.task_list import TaskListTool
    from mewcode.tools.task_update import TaskUpdateTool
    from mewcode.tools.team_roster import TeamRosterTool

    if backend_type == BackendType.IN_PROCESS.value:
        all_tools = {t.name: t for t in parent_registry.list_tools() if parent_registry.is_enabled(t.name)}
        filtered = {
            name: tool
            for name, tool in all_tools.items()
            if name in IN_PROCESS_TEAMMATE_ALLOWED_TOOLS or _is_mcp_tool(name) or getattr(tool, "interaction_kind", "") == "browser"
        }
    else:
        filtered = {t.name: t for t in parent_registry.list_tools() if parent_registry.is_enabled(t.name)}
        filtered.pop("TeamCreate", None)
        filtered.pop("TeamDelete", None)
    filtered.pop("TaskRetry", None)  # Never inherit the lead's dispatch authority.
    filtered.pop("EnterWorktree", None)
    filtered.pop("ExitWorktree", None)

    # 应用 agent 定义中的工具限制
    if definition is not None:
        if definition.disallowed_tools:
            for name in expand_tool_aliases(set(definition.disallowed_tools)):
                filtered.pop(name, None)
            if "*" in definition.disallowed_tools:
                filtered.clear()
        if definition.tools:
            allowed_set = expand_tool_aliases(set(definition.tools)) | TEAMMATE_COORDINATION_TOOLS
            filtered = {
                name: tool
                for name, tool in filtered.items()
                if "*" in allowed_set or name in allowed_set
            }

    coordination_tools = [
        TaskCreateTool(team_manager, team_name, agent_name),
        TaskGetTool(team_manager, team_name),
        TaskListTool(team_manager, team_name),
        TaskUpdateTool(team_manager, team_name, agent_name),
        SendMessageTool(team_manager, team_name, agent_id, agent_name),
        TeamRosterTool(team_manager, team_name),
    ]

    registry = ToolRegistry()
    for tool in filtered.values():
        registry.register(tool)
        if parent_registry.is_discovered(tool.name):
            registry.mark_discovered(tool.name)
    for tool in coordination_tools:
        if not definition or not ({tool.name, "*"} & set(definition.disallowed_tools)):
            if parent_registry.get(tool.name) is None or parent_registry.is_enabled(tool.name):
                registry.register(tool)

    return registry


def apply_coordinator_filter(registry: ToolRegistry) -> ToolRegistry:
    all_tools = {t.name: t for t in registry.list_tools() if registry.is_enabled(t.name)}
    filtered = ToolRegistry()
    for name, tool in all_tools.items():
        if name in COORDINATOR_MODE_ALLOWED_TOOLS:
            filtered.register(tool)
    return filtered
