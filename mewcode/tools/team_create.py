from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING, Literal

from pydantic import BaseModel

from mewcode.tools.base import Tool, ToolResult

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from mewcode.agent import Agent
    from mewcode.teams.manager import TeamManager


class TeamCreateParams(BaseModel):
    team_name: str
    description: str = ""
    protocol_version: Literal[1, 2] = 1
    team_spec: dict[str, Any] | None = None
    objective: str | None = None
    budget_ref: str | None = None
    command_id: str | None = None
    existing_run_id: str | None = None


class TeamCreateTool(Tool):
    name = "TeamCreate"
    description = (
        "Create a new team for coordinating multiple agents.\n\n"
        "## When to Use\n\n"
        "Use this tool proactively whenever:\n"
        "- The user explicitly asks to use a team, swarm, or group of agents\n"
        "- The user mentions wanting agents to work together, coordinate, or collaborate\n"
        "- A task requires sequential or parallel collaboration between multiple agents\n\n"
        "When in doubt about whether a task warrants a team, prefer spawning a team.\n\n"
        "## Team Workflow\n\n"
        "1. **Create a team** with TeamCreate\n"
        "2. **Spawn teammates** using the Agent tool with team_name and name parameters "
        "— this is REQUIRED to create long-running team members\n"
        "3. Teammates work independently and communicate via **SendMessage**\n"
        "4. When a teammate finishes, it sends its result to \"lead\" via SendMessage, then goes idle\n"
        "5. The lead collects and synthesizes all teammate results\n\n"
        "## CRITICAL: Spawning Teammates\n\n"
        "To add a member to a team, you MUST pass both team_name and name to the Agent tool:\n"
        "```\nAgent({\n"
        '  "team_name": "<team name from step 1>",\n'
        '  "name": "<member name, e.g. reviewer>",\n'
        '  "prompt": "...",\n'
        '  "description": "..."\n'
        "})\n```\n"
        "Without team_name, the agent runs as a one-shot sub-agent that blocks and returns inline "
        "— it will NOT be a team member.\n\n"
        "## Teammate Idle State\n\n"
        "Teammates go idle after every turn — this is completely normal. "
        "Sending a message to an idle teammate wakes it up. If a teammate "
        "stays idle with no messages for several minutes it exits; sending "
        "to an exited teammate returns an error telling you to respawn it.\n\n"
        "## Communication\n\n"
        "- Use SendMessage to talk to teammates by name; teammates reach you "
        "by messaging 'lead'\n"
        "- Messages from teammates arrive as system reminders at the start of each turn\n"
        "- Messages are delivered automatically — you do NOT need to manually check your inbox"
    )
    params_model = TeamCreateParams
    category = "command"
    is_concurrency_safe = False


    def __init__(
        self,
        team_manager: TeamManager,
        parent_agent: Agent,
        teammate_mode: str = "",
        is_interactive: bool = True,
        enable_coordinator_mode: bool = False,
        tool_installer: Any = None,
    ) -> None:
        self._team_manager = team_manager
        self._parent_agent = parent_agent
        self._teammate_mode = teammate_mode
        self._is_interactive = is_interactive
        self._enable_coordinator_mode = enable_coordinator_mode
        self._tool_installer = tool_installer
        if getattr(team_manager, '_v2_service', None) is not None:
            self.description = ('Create a v2 draft team with protocol_version=2, a complete team_spec, objective, '
                                'budget_ref and stable command_id. This only plans; it never starts members. '
                                'Inspect the draft, then explicitly activate with host-authorized role bindings, '
                                'immutable input versions and a bounded budget through TeamCommand. '
                                'Only the external Grok worker executes; review stays with the host.')


    async def execute(self, params: BaseModel) -> ToolResult:
        p: TeamCreateParams = params  # type: ignore[assignment]

        service = getattr(self._team_manager, '_v2_service', None)
        if p.protocol_version == 2 or service is not None:
            if p.protocol_version != 2 or service is None:
                return ToolResult(output='PROTOCOL_VERSION_MISMATCH: v2 requires explicit host configuration and protocol_version=2', is_error=True)
            if p.existing_run_id:
                try:
                    import json
                    handle = self._team_manager.attach_v2_run(service, p.existing_run_id)
                    self._register_lead_tools(handle)
                    return ToolResult(output=json.dumps(dict(scope=handle, snapshot=service.repository.runtime.snapshot(service.actor, p.existing_run_id)), ensure_ascii=False))
                except Exception as exc:
                    return ToolResult(output=str(exc), is_error=True)
            if not all((p.team_spec, p.objective, p.budget_ref, p.command_id)):
                return ToolResult(output='CONTRACT_REQUIRED: team_spec, objective, budget_ref and command_id are required', is_error=True)
            import json
            try:
                compiled = service.validate_spec(p.team_spec)
                service.register_spec(p.team_spec, command_id=p.command_id + '-spec')
                team = service.create_team(compiled.spec.ref, name=p.team_name, command_id=p.command_id + '-team')
                run = service.plan_run(team['team_id'], objective=p.objective, budget_ref=p.budget_ref,
                                       expected_version=team['state_version'], control_epoch=team['control_epoch'], command_id=p.command_id + '-run')
                handle = self._team_manager.attach_v2_run(service, run['team_run_id'])
                self._register_lead_tools(handle)
                return ToolResult(output=json.dumps(dict(scope=handle, team=team, run=run, execution_started=False), ensure_ascii=False))
            except Exception as exc:
                return ToolResult(output=str(exc), is_error=True)

        from mewcode.teams.backend_detect import BackendDetectionError

        try:
            backend = self._team_manager.detect_backend(
                self._teammate_mode, self._is_interactive
            )
        except BackendDetectionError as e:
            return ToolResult(output=str(e), is_error=True)

        try:
            team = self._team_manager.create_team(
                name=p.team_name,
                lead_agent_id=self._parent_agent.agent_id,
                description=p.description,
                teammate_mode=self._teammate_mode,
                is_interactive=self._is_interactive,
            )
        except Exception as e:
            return ToolResult(output=f"Failed to create team: {e}", is_error=True)

        # lead 必须拥有与 teammate 同款的协调工具，否则提示词教它做的事
        # （发消息、建任务、派任务）全部会命中 unknown tool
        self._register_lead_tools(team.name)

        coordinator_note = ""
        from mewcode.teams.coordinator import is_coordinator_mode
        if is_coordinator_mode(self._enable_coordinator_mode):
            from mewcode.agents.tool_filter import (
                COORDINATOR_MODE_ALLOWED_TOOLS,
                apply_coordinator_filter,
            )
            self._parent_agent.coordinator_mode = True
            self._parent_agent._team_manager = self._team_manager
            self._parent_agent._full_registry = self._parent_agent.registry
            self._parent_agent.registry = apply_coordinator_filter(self._parent_agent.registry)
            coordinator_note = "\nCoordinator Mode activated: tools narrowed to dispatch-only."

            # 契约校验：coordinator 提示词/白名单宣称的工具必须真实存在，
            # 缺失说明注册链路又断了——宁可在日志里炸响也不让模型撞 unknown tool
            registered = {t.name for t in self._parent_agent.registry.list_tools()}
            missing = COORDINATOR_MODE_ALLOWED_TOOLS - registered
            if missing:
                log.warning(
                    "Coordinator whitelist tools missing from registry: %s",
                    ", ".join(sorted(missing)),
                )

        return ToolResult(
            output=(
                f"Team '{team.name}' created successfully.\n"
                f"Backend: {backend.value}\n"
                f"Config: {team.config_path}\n"
                f"Use Agent tool with team_name='{team.name}' to spawn teammates.\n"
                f"You now have SendMessage / TaskCreate / TaskGet / TaskList / "
                f"TaskUpdate bound to this team."
                f"{coordinator_note}"
            )
        )

    def _register_lead_tools(self, team_name: str) -> None:
        from mewcode.tools.send_message import SendMessageTool
        from mewcode.tools.task_create import TaskCreateTool
        from mewcode.tools.task_get import TaskGetTool
        from mewcode.tools.task_list import TaskListTool
        from mewcode.tools.task_update import TaskUpdateTool
        from mewcode.tools.team_roster import TeamRosterTool

        lead_tools = [
            SendMessageTool(
                self._team_manager,
                team_name,
                self._parent_agent.agent_id,
                "lead",
            ),
            TaskCreateTool(self._team_manager, team_name, "lead"),
            TaskGetTool(self._team_manager, team_name),
            TaskListTool(self._team_manager, team_name),
            TaskUpdateTool(self._team_manager, team_name, "lead"),
            TeamRosterTool(self._team_manager, team_name),
        ]
        if team_name in getattr(self._team_manager, '_v2_scopes', {}):
            from mewcode.tools.team_command import TeamCommandTool
            lead_tools.append(TeamCommandTool(self._team_manager._v2_scopes[team_name]))
        registries = [self._parent_agent.registry]
        full = getattr(self._parent_agent, "_full_registry", None)
        if full is not None and full is not self._parent_agent.registry:
            registries.append(full)
        for registry in registries:
            for tool in lead_tools:
                if registry is self._parent_agent.registry and self._tool_installer:
                    self._tool_installer(tool)
                else:
                    registry.register(tool)
