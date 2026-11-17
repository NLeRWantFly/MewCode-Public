"""Bind a run's durable identity before any role or command can be dispatched."""
from __future__ import annotations

import os
from pathlib import Path

from mewcode.orchestration.chat_routing import (
    bind_task_permission_scope, dialogue_focus, set_dialogue_focus,
)
from mewcode.sandbox.task_identity import require_task_identity

SCHEMA = "mewcode.execution-scope.v1"


def prepare_orchestration_scope(runtime, *, task: str, run_id: str, checkpoint=None) -> dict:
    """Restore this run's host identity, never infer or create an authorization."""
    conversation_id = str(getattr(runtime, "conversation_id", "") or "")
    durable_id = str(getattr(getattr(runtime, "session", None), "session_id", "") or "")
    if not conversation_id or (durable_id and durable_id != conversation_id):
        raise ValueError("编排与持久化会话身份不一致，未派发节点。")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("编排缺少明确的 run ID，未派发节点。")
    workspace = str(Path(runtime.work_dir).resolve())
    previous = (checkpoint or {}).get("host_execution_scope")
    task_id = None
    if previous is not None:
        if (not isinstance(previous, dict) or previous.get("schema") != SCHEMA
                or previous.get("conversation_id") != conversation_id or previous.get("run_id") != run_id
                or os.path.normcase(str(previous.get("workspace") or "")) != os.path.normcase(workspace)):
            raise ValueError("保存的编排执行身份或工作目录不匹配，未派发节点。")
        task_id = require_task_identity(previous.get("task_id"))
    elif checkpoint:
        from mewcode.orchestration.draft_store import DraftStore
        from mewcode.orchestration.draft_sources import digest
        design = (DraftStore.read_state(workspace, conversation_id) or {}).get("task_design") or {}
        locked = checkpoint.get("locked_flow") or {}
        exact_flow = (design.get("status") in {"ready", "running"}
                      and design.get("execution_flow_hash") == digest(locked))
        bound_native_run = (bool(checkpoint.get("workflow_core")) and checkpoint.get("run_id") == run_id
                            and design.get("run_id") == run_id)
        if (design.get("intent") == "execute" and (exact_flow or bound_native_run)
                and (locked.get("meta", {}).get("task_design") or {}).get("task_id") == design.get("task_id")):
            task_id = require_task_identity(design["task_id"])
    checker = getattr(getattr(runtime, "agent", None), "permission_checker", None)
    if checker is not None:
        sandbox = getattr(checker, "sandbox", None)
        root = getattr(sandbox, "project_root", None)
        if root is None or os.path.normcase(str(Path(root).resolve())) != os.path.normcase(workspace):
            raise ValueError("编排权限检查器与工作目录不一致，未派发节点。")
    set_dialogue_focus(runtime, "orchestration", task=task, run_id=run_id, task_id=task_id)
    task_id = require_task_identity(dialogue_focus(runtime).get("task_id"))
    authorization = bind_task_permission_scope(runtime, task_id=task_id)
    mode = getattr(checker, "mode", None)
    return {"schema": SCHEMA, "conversation_id": conversation_id, "run_id": run_id,
            "task_id": task_id, "workspace": workspace,
            "permission_mode": getattr(mode, "value", str(mode or "")),
            "authorization_version": authorization.version if authorization else "",
            "permission_scope_bound": checker is not None and checker.task_id == task_id}
