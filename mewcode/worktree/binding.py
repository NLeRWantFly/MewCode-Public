"""Bind a worktree session to one Agent without changing process cwd."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mewcode.permissions.policy import build_permission_checker


class WorktreeSessionBinding:
    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self._original_work_dir: str | None = None
        self._original_checker: Any = None

    def validate_write_target(self, path: str) -> None:
        checker = self.agent.permission_checker
        if checker is None:
            raise ValueError("Worktree session requires an existing permission policy")
        if getattr(checker, "native_publication_mode", "publish") == "discard":
            raise ValueError("Read-only runtime cannot create or enter a writable worktree")
        sandbox = checker.sandbox
        scoped = getattr(sandbox, "allows_policy_write", None)
        allowed = scoped(path) if callable(scoped) else sandbox.check(path)[0]
        if not allowed:
            raise ValueError(f"Worktree directory exceeds the current writable scope: {path}")

    def prepare(self, path: str) -> Any:
        self.validate_write_target(path)
        parent = self.agent.permission_checker
        checker = build_permission_checker(path, parent_policy=parent,
            profile="worktree", writable_scope=[path])
        if hasattr(parent, "native_publication_mode"):
            checker.native_publication_mode = parent.native_publication_mode
        return checker

    def activate(self, path: str, checker: Any) -> None:
        if self._original_work_dir is None:
            self._original_work_dir = self.agent.work_dir
            self._original_checker = self.agent.permission_checker
        self.agent.work_dir = str(Path(path).resolve())
        self.agent.permission_checker = checker
        self._publish_checker(checker)

    def restore(self) -> None:
        if self._original_work_dir is None:
            return
        current, original = self.agent.permission_checker, self._original_checker
        # Task changes and revocations made while in the worktree remain current.
        for name in ("mode", "task_id", "task_authorization", "plan_file_path"):
            setattr(original, name, getattr(current, name))
        if hasattr(current, "native_publication_mode"):
            original.native_publication_mode = current.native_publication_mode
        self.agent.work_dir = self._original_work_dir
        self.agent.permission_checker = original
        self._publish_checker(original)
        self._original_work_dir = None
        self._original_checker = None

    def _publish_checker(self, checker: Any) -> None:
        context = getattr(self.agent, "runtime_context", None)
        if context is not None:
            from mewcode.plugins.services import PERMISSIONS
            context.provide(PERMISSIONS, checker, replace=True)
