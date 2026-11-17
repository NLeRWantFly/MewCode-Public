"""Conversation-bound, human-confirmed exact-file access. Never executes a tool."""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

from mewcode.permissions.action_review import ActionReviewBinding
from mewcode.permissions.modes import PermissionMode
from mewcode.permissions.sensitive_paths import classify_sensitive_read_path
from mewcode.tools.base import ToolResult


async def request_file_access(agent, tc, params):
    from mewcode.agent import AskUserRequest
    from mewcode.tools.read_file import ReadFile
    from mewcode.tools.write_file import WriteFile

    checker = agent.permission_checker
    if checker is None or checker.mode not in {
        PermissionMode.DEFAULT, PermissionMode.ACCEPT_EDITS,
        PermissionMode.AUTO, PermissionMode.STRICT_ISOLATION,
        PermissionMode.CUSTOM,
    }:
        yield ToolResult("FILE_ACCESS_UNAVAILABLE: 当前权限模式不支持对话授权。", is_error=True)
        return
    sandbox = checker.sandbox
    writable = params.access == "read_write"
    candidate = Path(params.file_path).expanduser()
    probe = WriteFile() if writable else ReadFile()
    arguments = {"file_path": str(candidate)}

    def target():
        if not candidate.is_absolute():
            raise ValueError("请提供一个文件的绝对路径")
        resolved = candidate.resolve(strict=False)
        if resolved.is_dir() or (resolved.exists() and not resolved.is_file()):
            raise ValueError("只能申请单个普通文件，不能申请目录或设备")
        if not resolved.exists() and not writable:
            raise ValueError("只读授权需要现有文件")
        for path in (candidate, resolved):
            sensitive = classify_sensitive_read_path(path, workspace_root=sandbox.project_root)
            if sensitive.sensitive:
                raise ValueError("敏感位置不能通过文件授权放行：" + sensitive.reason)
        decision = checker.check(probe, arguments)
        if decision.effect == "deny":
            raise ValueError(decision.reason)
        return resolved

    try:
        resolved = target()
        if sandbox.check(str(candidate), read_only=not writable)[0]:
            yield ToolResult("FILE_ACCESS_ALREADY_AVAILABLE: 目标文件已具备所需路径访问权限，无需再次询问。具体操作仍须正常权限检查及适用的安全审查。")
            return
        binding = ActionReviewBinding.capture(agent, tc.tool_name, tc.arguments)
    except (OSError, RuntimeError, ValueError) as exc:
        yield ToolResult("FILE_ACCESS_BLOCKED: " + str(exc), is_error=True)
        return

    access = "读写（可创建或修改）" if writable else "只读"
    questions = [{"type": "radio", "name": "file_access", "title": "文件访问授权",
        "message": f"是否允许在当前会话运行时内{access}访问此文件？\n{resolved}",
        "context": f"用途：{params.reason}\n仅此文件，不包含所属目录、同级文件或子目录。重启后失效；具体操作仍须正常权限检查及适用的安全审查。",
        "allow_other": False,
        "options": [{"value": "grant_file", "label": "授权此文件", "description": f"允许{access}访问上述单个文件"},
                    {"value": "deny_file", "label": "暂不授权", "description": "保持当前权限，不执行受阻操作"}]}]
    future = asyncio.get_running_loop().create_future()
    request = AskUserRequest(request_id=tc.tool_id, tool_name=tc.tool_name,
        questions=questions, future=future, host_file_access={
            "nonce": uuid.uuid4().hex, "file_path": str(resolved), "access": params.access})
    try:
        yield request
        answers = await future
        if request.resolved_answers is not None:
            answers = request.resolved_answers
    finally:
        if not future.done():
            future.cancel()
    allowed = isinstance(answers, dict) and answers.get("file_access") == "grant_file"
    if allowed:
        try:
            if (agent.permission_checker is not checker or checker.sandbox is not sandbox
                    or target() != resolved or not binding.is_current(agent, tc.tool_name, tc.arguments)):
                raise ValueError("等待期间路径、权限或任务已变化，此次确认已失效，请根据当前状态重新申请")
        except (OSError, RuntimeError, ValueError) as exc:
            yield ToolResult("FILE_ACCESS_CONTEXT_CHANGED: " + str(exc), is_error=True)
            return
        # Commit the frozen exact path directly; never call a directory-capable
        # grant helper, and never downgrade an existing writable grant.
        key = os.path.normcase(str(resolved))
        sandbox.file_grants[key] = bool(writable or sandbox.file_grants.get(key))
    agent._audit_permission(tc, tool_name=tc.tool_name, params_summary=str(resolved),
        decision="user_allow" if allowed else "user_deny", source_layer="human_file_access",
        user_choice="grant_file" if allowed else "deny_file",
        authorization_scope="conversation_exact_file" if allowed else "", reason=params.reason)
    if allowed:
        yield ToolResult(f"FILE_ACCESS_GRANTED: 已授予{access}访问：{resolved}。仅此文件，当前会话运行时有效，重启后失效。操作尚未执行；请重新提交所需操作，仍须正常权限检查及适用的安全审查。旧审查引用不会恢复有效。",
            host_observation={"file_access_grant": {"request_id": tc.tool_id,
                "file_path": str(resolved), "access": params.access,
                "scope": "conversation_exact_file", "execution_authorized": False}})
    else:
        yield ToolResult("FILE_ACCESS_DENIED: 用户未授权此文件，权限未扩大。保留已完成工作或使用已授权范围，不得重复施压或绕过限制。", is_error=True)
