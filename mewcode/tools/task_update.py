from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from mewcode.teams.manager import TeamManager


class TaskUpdateParams(BaseModel):
    task_id: str
    status: str | None = None
    assignee: str | None = None
    description: str | None = None
    add_blocks: list[str] | None = None
    add_blocked_by: list[str] | None = None
    expected_status: str | None = Field(
        default=None,
        description="Optimistic lock: fail if the task's current status differs.",
    )
    expected_assignee: str | None = Field(
        default=None,
        description=(
            "Optimistic lock: fail if the task's current assignee differs. "
            "Pass '' to claim a task only if it is unassigned."
        ),
    )


VALID_STATUSES = {"pending", "in_progress", "completed", "blocked"}


class TaskUpdateTool(Tool):
    name = "TaskUpdate"
    description = (
        "Update a shared task's status, assignee, description, or dependencies. "
        "Use add_blocks/add_blocked_by to add dependency relations (kept in "
        "sync bidirectionally). To claim a task safely, pass assignee=<you> "
        "with expected_assignee='' — the update fails with the current holder "
        "if someone claimed it first. Starting or completing a task requires "
        "all its blocked_by dependencies to be completed; completing a task "
        "automatically notifies assignees of tasks it unblocks."
    )
    params_model = TaskUpdateParams
    category = "command"
    is_concurrency_safe = True

    def __init__(
        self,
        team_manager: TeamManager,
        team_name: str,
        agent_name: str = "",
    ) -> None:
        self._team_manager = team_manager
        self._team_name = team_name
        self._agent_name = agent_name

    async def execute(self, params: BaseModel) -> ToolResult:
        p: TaskUpdateParams = params  # type: ignore[assignment]

        from mewcode.teams.tool_adapter_v2 import route_team_tool
        routed = route_team_tool(self._team_manager, self._team_name, "TaskUpdate", p)
        if routed is not None:
            return routed

        if p.status and p.status not in VALID_STATUSES:
            return ToolResult(
                output=f"Invalid status '{p.status}'. Must be one of: {', '.join(sorted(VALID_STATUSES))}",
                is_error=True,
            )

        store = self._team_manager.get_task_store(self._team_name)
        if store is None:
            return ToolResult(output=f"Task store not found for team '{self._team_name}'", is_error=True)

        from mewcode.teams.shared_task import TaskConflictError

        try:
            task = store.update(
                task_id=p.task_id,
                status=p.status,
                assignee=p.assignee,
                description=p.description,
                add_blocks=p.add_blocks,
                add_blocked_by=p.add_blocked_by,
                expected_status=p.expected_status,
                expected_assignee=p.expected_assignee,
            )
        except TaskConflictError as e:
            return ToolResult(output=f"Task update rejected: {e}", is_error=True)

        if task is None:
            return ToolResult(output=f"Task '{p.task_id}' not found", is_error=True)

        changes: list[str] = []
        if p.status:
            changes.append(f"status → {p.status}")
        if p.assignee is not None:
            changes.append(f"assignee → {p.assignee or '(unassigned)'}")
        if p.description is not None:
            changes.append("description updated")
        if p.add_blocks:
            changes.append(f"blocks += {', '.join(p.add_blocks)}")
        if p.add_blocked_by:
            changes.append(f"blocked_by += {', '.join(p.add_blocked_by)}")

        unlock_note = ""
        if p.status == "completed":
            unlock_note = self._notify_unlocked(store, task.id)

        return ToolResult(
            output=(
                f"Task {task.id} updated: "
                f"{'; '.join(changes) if changes else 'no changes'}{unlock_note}"
            )
        )

    def _notify_unlocked(self, store, task_id: str) -> str:
        """任务完成时通知被解锁的下游——让依赖图真正驱动协作。"""
        try:
            unlocked = store.completed_unlocks(task_id)
        except Exception:
            return ""
        if not unlocked:
            return ""

        from mewcode.teams.mailbox import create_message
        from mewcode.teams.registry import AgentNameRegistry

        mailbox = self._team_manager.get_mailbox(self._team_name)
        registry = AgentNameRegistry.instance()
        notified: list[str] = []
        if mailbox is not None:
            for t in unlocked:
                recipient = registry.resolve(t.assignee) if t.assignee else None
                if recipient is None:
                    recipient = registry.resolve("lead")
                if recipient is None:
                    continue
                msg = create_message(
                    from_agent="task-board",
                    to_agent=recipient,
                    content=(
                        f"Task {t.id} '{t.title}' is now unblocked "
                        f"(all dependencies completed)."
                    ),
                    summary=f"task {t.id} unblocked",
                )
                mailbox.write(recipient, msg)
                notified.append(t.id)
        if notified:
            return f"\nUnblocked task(s) notified: {', '.join(notified)}"
        return ""
