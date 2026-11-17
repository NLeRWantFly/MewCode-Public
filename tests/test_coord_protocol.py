from __future__ import annotations

import pytest

from mewcode.mcp.coord_protocol import (
    CoordinationError,
    capabilities_hash,
    normalize_capabilities,
    validate_task_limits,
)


def test_capability_hash_is_order_independent() -> None:
    first = {
        "agent_id": "grok-executor-01",
        "model_id": "grok-4.6",
        "role": "executor",
        "tools": [{"name": "b"}, {"name": "a"}],
        "skills": ["audit"],
        "mcp_servers": [],
    }
    second = {**first, "tools": [{"name": "a"}, {"name": "b"}]}
    assert capabilities_hash(first) == capabilities_hash(second)


def test_capabilities_require_fixed_identity() -> None:
    with pytest.raises(CoordinationError, match="agent_id and model_id"):
        normalize_capabilities({"role": "executor", "tools": []})


def test_execution_turn_limit() -> None:
    assert validate_task_limits(3) == 3
    with pytest.raises(CoordinationError, match="cannot exceed"):
        validate_task_limits(4)

