from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

VALID_STATUSES = {"pending", "in_progress", "completed", "blocked"}


class TaskConflictError(Exception):
    """认领冲突 / 非法状态迁移 / 依赖未满足——调用方应把消息原样反馈给 agent。"""


@dataclass
class SharedTask:
    id: str
    title: str
    description: str = ""
    status: str = "pending"  # pending | in_progress | completed | blocked
    assignee: str = ""
    blocks: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    created_by: str = ""


    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SharedTask:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class SharedTaskStore:


    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._next_id = 1
        self._tasks: dict[str, SharedTask] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            # 半写/损坏的任务板不隔离的话，所有 Task 工具会永久抛错
            from mewcode.teams.fsio import quarantine_corrupt_file
            backup = quarantine_corrupt_file(self._path)
            log.warning(
                "Corrupt task board %s quarantined to %s; starting empty",
                self._path, backup,
            )
            self._tasks.clear()
            self._next_id = 1
            return
        # 全量替换而非增量合并：其他实例删除的任务不应在本实例复活
        self._tasks.clear()
        self._next_id = data.get("next_id", 1)
        for t in data.get("tasks", []):
            task = SharedTask.from_dict(t)
            self._tasks[task.id] = task

    def _save(self) -> None:
        from mewcode.teams.fsio import atomic_write_text
        data = {
            "next_id": self._next_id,
            "tasks": [t.to_dict() for t in self._tasks.values()],
        }
        atomic_write_text(
            self._path, json.dumps(data, indent=2, ensure_ascii=False)
        )

    def create(
        self,
        title: str,
        description: str = "",
        assignee: str = "",
        blocks: list[str] | None = None,
        blocked_by: list[str] | None = None,
        created_by: str = "",
    ) -> SharedTask:
        # 与 get/list/update 同样先重读磁盘，否则多实例下会覆写他人任务并复用 id
        self._load()

        unknown = [
            bid for bid in (blocks or []) + (blocked_by or [])
            if bid not in self._tasks
        ]
        if unknown:
            raise TaskConflictError(
                f"Unknown dependency task id(s): {', '.join(unknown)}"
            )

        task_id = str(self._next_id)
        self._next_id += 1
        task = SharedTask(
            id=task_id,
            title=title,
            description=description,
            assignee=assignee,
            blocks=blocks or [],
            blocked_by=blocked_by or [],
            created_by=created_by,
        )
        self._tasks[task_id] = task
        # 依赖关系双向落库，blocks/blocked_by 不再靠调用方手工维护一致
        for bid in task.blocks:
            other = self._tasks[bid]
            if task_id not in other.blocked_by:
                other.blocked_by.append(task_id)
        for bid in task.blocked_by:
            other = self._tasks[bid]
            if task_id not in other.blocks:
                other.blocks.append(task_id)
        self._save()
        return task

    def get(self, task_id: str) -> SharedTask | None:
        self._load()
        return self._tasks.get(task_id)


    def list_tasks(
        self,
        status: str | None = None,
        assignee: str | None = None,
    ) -> list[SharedTask]:
        self._load()
        result = list(self._tasks.values())
        if status:
            result = [t for t in result if t.status == status]
        if assignee:
            result = [t for t in result if t.assignee == assignee]
        return result


    def update(
        self,
        task_id: str,
        status: str | None = None,
        assignee: str | None = None,
        description: str | None = None,
        add_blocks: list[str] | None = None,
        add_blocked_by: list[str] | None = None,
        expected_status: str | None = None,
        expected_assignee: str | None = None,
    ) -> SharedTask | None:
        self._load()
        task = self._tasks.get(task_id)
        if task is None:
            return None

        # CAS 前置条件：expected_* 不匹配即冲突，杜绝"两人都认领成功"
        if expected_status is not None and task.status != expected_status:
            raise TaskConflictError(
                f"Task {task_id} status is '{task.status}', "
                f"expected '{expected_status}'."
            )
        if expected_assignee is not None and task.assignee != expected_assignee:
            holder = task.assignee or "(unassigned)"
            raise TaskConflictError(
                f"Task {task_id} is held by {holder}, "
                f"expected '{expected_assignee or '(unassigned)'}'."
            )

        if status is not None:
            self._check_transition(task, status)
        if status is not None:
            task.status = status
        if assignee is not None:
            task.assignee = assignee
        if description is not None:
            task.description = description
        if add_blocks:
            for bid in add_blocks:
                if bid not in self._tasks:
                    raise TaskConflictError(f"Unknown task id '{bid}' in blocks")
                if bid not in task.blocks:
                    task.blocks.append(bid)
                other = self._tasks[bid]
                if task_id not in other.blocked_by:
                    other.blocked_by.append(task_id)
        if add_blocked_by:
            for bid in add_blocked_by:
                if bid not in self._tasks:
                    raise TaskConflictError(f"Unknown task id '{bid}' in blocked_by")
                if bid not in task.blocked_by:
                    task.blocked_by.append(bid)
                other = self._tasks[bid]
                if task_id not in other.blocks:
                    other.blocks.append(task_id)
        self._save()
        return task

    def _check_transition(self, task: SharedTask, new_status: str) -> None:
        if new_status not in VALID_STATUSES:
            raise TaskConflictError(
                f"Invalid status '{new_status}'. "
                f"Must be one of: {', '.join(sorted(VALID_STATUSES))}"
            )
        if new_status == task.status:
            return
        # completed 是终态，只允许重开回 in_progress
        if task.status == "completed" and new_status != "in_progress":
            raise TaskConflictError(
                f"Task {task.id} is completed; only reopening to "
                f"'in_progress' is allowed."
            )
        # 开工/完成前，所有上游依赖必须已完成——依赖图从注释变成硬约束
        if new_status in ("in_progress", "completed"):
            pending_deps = [
                bid for bid in task.blocked_by
                if bid in self._tasks and self._tasks[bid].status != "completed"
            ]
            if pending_deps:
                raise TaskConflictError(
                    f"Task {task.id} is blocked by unfinished task(s): "
                    f"{', '.join(pending_deps)}."
                )

    def completed_unlocks(self, task_id: str) -> list[SharedTask]:
        """task_id 完成后，返回因此而不再被阻塞的下游任务。"""
        self._load()
        unlocked: list[SharedTask] = []
        for t in self._tasks.values():
            if task_id not in t.blocked_by or t.status == "completed":
                continue
            if all(
                self._tasks[bid].status == "completed"
                for bid in t.blocked_by
                if bid in self._tasks
            ):
                unlocked.append(t)
        return unlocked

    def init_empty(self) -> None:
        self._tasks.clear()
        self._next_id = 1
        self._save()
