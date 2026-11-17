"""Approved recovery execution scope for permission fast-paths."""
from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any

_SCOPE: ContextVar[dict[str, Any] | None] = ContextVar(
    "mewcode_approved_recovery_scope", default=None
)


def bind_approved_revision(scope: dict[str, Any] | None) -> Token:
    return _SCOPE.set(scope if isinstance(scope, dict) else None)


def reset_approved_revision(token: Token) -> None:
    _SCOPE.reset(token)


def current_approved_revision() -> dict[str, Any] | None:
    return _SCOPE.get()
