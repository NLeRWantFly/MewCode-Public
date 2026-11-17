"""One host-task identity contract for binding, freezing and admission."""
from __future__ import annotations


def require_task_identity(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError("A host task identity is required")
    return value
