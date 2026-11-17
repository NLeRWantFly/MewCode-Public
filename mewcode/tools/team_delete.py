from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from mewcode.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from mewcode.agent import Agent
    from mewcode.teams.manager import TeamManager


class TeamDeleteParams(BaseModel):
    team_name: str
    force: bool = False


class TeamDeleteTool(Tool):
    name = "TeamDelete"
    description = (
        "Delete an Agent Team. Terminates all pane processes, removes worktrees "
        "(worktrees with uncommitted changes are kept and reported), "
        "cleans up mailbox and team directory. Requires all members to be idle "
        "unless force=true (use force to clean up a stale team left behind by "
        "a crashed session)."
    )
    params_model = TeamDeleteParams
    category = "command"
    is_concurrency_safe = False


    def __init__(self, team_manager: TeamManager, parent_agent: Agent | None = None) -> None:
        self._team_manager = team_manager
        self._parent_agent = parent_agent


    async def execute(self, params: BaseModel) -> ToolResult:
        p: TeamDeleteParams = params  # type: ignore[assignment]

        from mewcode.teams.tool_adapter_v2 import route_team_tool
        routed = route_team_tool(self._team_manager, p.team_name, "TeamDelete", p)
        if routed is not None:
            return routed

        from mewcode.teams.manager import TeamError

        try:
            warnings = self._team_manager.delete_team(p.team_name, force=p.force)
        except TeamError as e:
            return ToolResult(output=str(e), is_error=True)
        except Exception as e:
            return ToolResult(output=f"Failed to delete team: {e}", is_error=True)

        coordinator_note = ""
        if self._parent_agent and self._parent_agent.coordinator_mode:
            full_registry = getattr(self._parent_agent, '_full_registry', None)
            if full_registry is not None:
                self._parent_agent.registry = full_registry
                self._parent_agent._full_registry = None
            self._parent_agent.coordinator_mode = False
            coordinator_note = "\nCoordinator Mode deactivated: full tools restored."

        warning_note = ""
        if warnings:
            warning_note = "\n" + "\n".join(warnings)
        return ToolResult(
            output=f"Team '{p.team_name}' deleted successfully."
            f"{warning_note}{coordinator_note}"
        )
