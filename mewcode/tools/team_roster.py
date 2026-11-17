from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from mewcode.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from mewcode.teams.manager import TeamManager


class TeamRosterParams(BaseModel):
    pass


class TeamRosterTool(Tool):
    name = "TeamRoster"
    description = (
        "List the members of your team: name, agent type, and whether they are "
        "currently active. Use this to discover teammates before SendMessage."
    )
    params_model = TeamRosterParams
    category = "read"
    is_concurrency_safe = True

    def __init__(self, team_manager: TeamManager, team_name: str) -> None:
        self._team_manager = team_manager
        self._team_name = team_name

    async def execute(self, params: BaseModel) -> ToolResult:
        from mewcode.teams.tool_adapter_v2 import route_team_tool
        routed = route_team_tool(self._team_manager, self._team_name, 'TeamRoster', params)
        if routed is not None:
            return routed
        team = self._team_manager.get_team(self._team_name)
        if team is None:
            return ToolResult(
                output=f"Team '{self._team_name}' not found", is_error=True
            )

        lines = [f"Team '{team.name}' roster:"]
        lines.append("- lead (address messages to 'lead')")
        if not team.members:
            lines.append("- (no teammates spawned yet)")
        for m in team.members:
            if m.is_active is False:
                state = "idle"
            elif m.is_active:
                state = "active"
            else:
                state = "unknown"
            lines.append(f"- {m.name} ({m.agent_type or 'worker'}, {state})")
        return ToolResult(output="\n".join(lines))
