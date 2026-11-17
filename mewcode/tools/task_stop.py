from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from mewcode.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from mewcode.agents.task_manager import TaskManager


class TaskStopParams(BaseModel):
    task_id: str


class TaskStopTool(Tool):
    name = "TaskStop"
    description = (
        "Stop a running background task (including teammates spawned via the "
        "Agent tool). Pass the Task ID reported when the task was launched. "
        "A stopped teammate is marked idle; its mailbox and worktree are kept."
    )
    params_model = TaskStopParams
    category = "command"
    is_concurrency_safe = False

    def __init__(self, task_manager: TaskManager) -> None:
        self._task_manager = task_manager

    async def execute(self, params: BaseModel) -> ToolResult:
        p: TaskStopParams = params  # type: ignore[assignment]

        bg = self._task_manager.get(p.task_id)
        if bg is None:
            return ToolResult(
                output=f"Unknown task ID '{p.task_id}'.", is_error=True
            )
        if bg.status != "running":
            return ToolResult(
                output=f"Task '{p.task_id}' is not running (status: {bg.status}).",
                is_error=True,
            )
        if self._task_manager.cancel(p.task_id):
            return ToolResult(
                output=f"Task '{p.task_id}' ({bg.name}) stop requested."
            )
        return ToolResult(
            output=f"Failed to stop task '{p.task_id}'.", is_error=True
        )
