from __future__ import annotations

from enum import Enum
from typing import Literal

from mewcode.tools.base import ToolCategory


DecisionEffect = Literal["allow", "deny", "ask", "review"]


class PermissionMode(str, Enum):
    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    PLAN = "plan"
    AUTO = "auto"
    STRICT_ISOLATION = "strictIsolation"
    BYPASS = "bypassPermissions"
    # Legacy/internal modes remain readable alongside the public options.
    CUSTOM = "custom"
    DONT_ASK = "dontAsk"


EXECUTION_PERMISSION_MODES = (PermissionMode.DEFAULT, PermissionMode.AUTO, PermissionMode.BYPASS)
PUBLIC_PERMISSION_MODES = (PermissionMode.DEFAULT, PermissionMode.PLAN, PermissionMode.AUTO, PermissionMode.BYPASS)
PERMISSION_MODE_LABELS = {
    PermissionMode.DEFAULT: "授权运行",
    PermissionMode.PLAN: "计划（Plan）",
    PermissionMode.AUTO: "Auto模式",
    PermissionMode.BYPASS: "完全放行【危险操作停止】",
}


def public_permission_mode(value: object, *, allow_plan: bool = False) -> PermissionMode:
    """Migrate retired user levels conservatively, without changing child contracts."""
    try:
        mode = PermissionMode(value)
    except (TypeError, ValueError):
        return PermissionMode.DEFAULT
    if mode in EXECUTION_PERMISSION_MODES or (allow_plan and mode == PermissionMode.PLAN):
        return mode
    return PermissionMode.DEFAULT


_MODE_MATRIX: dict[PermissionMode, dict[str, DecisionEffect]] = {
    PermissionMode.DEFAULT: {"read": "allow", "write": "ask", "command": "ask", "harness": "ask"},
    PermissionMode.ACCEPT_EDITS: {"read": "allow", "write": "allow", "command": "ask", "harness": "ask"},
    PermissionMode.PLAN: {"read": "allow", "write": "deny", "command": "deny", "harness": "deny"},
    # Ambiguous Auto commands require review; isolation is an execution backend.
    PermissionMode.AUTO: {"read": "allow", "write": "allow", "command": "ask", "harness": "ask"},
    PermissionMode.STRICT_ISOLATION: {"read": "allow", "write": "allow", "command": "ask", "harness": "ask"},
    PermissionMode.BYPASS: {"read": "allow", "write": "allow", "command": "allow", "harness": "allow"},
    PermissionMode.CUSTOM: {"read": "ask", "write": "ask", "command": "ask", "harness": "ask"},
    PermissionMode.DONT_ASK: {"read": "allow", "write": "allow", "command": "allow", "harness": "allow"},
}


def mode_decide(mode: PermissionMode, category: ToolCategory | str) -> DecisionEffect:
    matrix = _MODE_MATRIX[mode]
    if category in matrix:
        return matrix[category]
    if mode == PermissionMode.PLAN:
        return "deny"
    if mode in {PermissionMode.BYPASS, PermissionMode.DONT_ASK}:
        return "allow"
    if mode in {PermissionMode.AUTO, PermissionMode.STRICT_ISOLATION}:
        return "ask"
    return "ask"
