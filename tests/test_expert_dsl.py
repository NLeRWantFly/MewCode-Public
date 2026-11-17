from __future__ import annotations

from typing import Any

import pytest

from mewcode.expert_dsl import (
    DEFAULT_MAX_STEPS,
    STEP_LIMIT_ERROR,
    DslError,
    Runtime,
    StepLimitExceeded,
    validate_program,
)


class RecordingHost:
    """In-memory host that records calls and answers from canned data."""

    def __init__(self, conditions: dict[str, bool] | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.conditions = dict(conditions or {})

    async def spawn(self, role: str, task: str) -> str:
        self.calls.append(("spawn", (role, task)))
        return f"handle:{role}:{task}"

    async def fanout(self, role: str, tasks: list[str]) -> list[str]:
        self.calls.append(("fanout", (role, tasks)))
        return [f"handle:{role}:{task}" for task in tasks]

    async def wait(self) -> list[str]:
        self.calls.append(("wait", ()))
        return ["done"]

    async def escalate(self, level: str, issue: str) -> str:
        self.calls.append(("escalate", (level, issue)))
        return f"escalated:{level}"

    async def verify(self, scope: str) -> dict[str, str]:
        self.calls.append(("verify", (scope,)))
        return {"verdict": "PASS", "scope": scope}

    async def evaluate(self, key: str) -> bool:
        self.calls.append(("evaluate", (key,)))
        if key not in self.conditions:
            raise AssertionError(f"unexpected condition: {key}")
        return self.conditions[key]


async def run_program(
    program: Any,
    conditions: dict[str, bool] | None = None,
    **runtime_kwargs: Any,
) -> tuple[RecordingHost, Runtime, list[Any]]:
    host = RecordingHost(conditions)
    runtime = Runtime(host, **runtime_kwargs)
    results = await runtime.run(program)
    return host, runtime, results


@pytest.mark.asyncio
async def test_linear_program_delegates_each_op() -> None:
    host, runtime, results = await run_program([
        {"op": "spawn", "role": "executor", "task": "build"},
        {"op": "verify", "scope": "build"},
    ])
    assert [call[0] for call in host.calls] == ["spawn", "verify"]
    assert runtime.steps_run == 2
    assert results[0] == "handle:executor:build"
    assert results[1] == {"verdict": "PASS", "scope": "build"}


@pytest.mark.asyncio
async def test_fanout_and_wait() -> None:
    host, _, results = await run_program([
        {"op": "fanout", "role": "executor", "tasks": ["a", "b", "c"]},
        {"op": "wait"},
    ])
    assert [call[0] for call in host.calls] == ["fanout", "wait"]
    assert results[0] == [
        "handle:executor:a",
        "handle:executor:b",
        "handle:executor:c",
    ]


@pytest.mark.asyncio
async def test_branch_follows_condition() -> None:
    program = [
        {
            "op": "branch",
            "on": "audit_passed",
            "then": [{"op": "verify", "scope": "final"}],
            "else": [{"op": "escalate", "level": "manager", "issue": "failed"}],
        }
    ]
    host, _, _ = await run_program(program, conditions={"audit_passed": True})
    assert [call[0] for call in host.calls] == ["evaluate", "verify"]

    host2, _, _ = await run_program(program, conditions={"audit_passed": False})
    assert [call[0] for call in host2.calls] == ["evaluate", "escalate"]


def test_unknown_op_is_rejected_with_name() -> None:
    with pytest.raises(DslError, match="teleport"):
        validate_program([{"op": "teleport", "role": "x"}])


def test_invalid_params_are_rejected() -> None:
    with pytest.raises(DslError):
        validate_program([{"op": "spawn", "role": "executor"}])
    with pytest.raises(DslError):
        validate_program([{"op": "fanout", "role": "x", "tasks": []}])
    with pytest.raises(DslError):
        validate_program([{"op": "fanout", "role": "x", "tasks": [1]}])
    with pytest.raises(DslError):
        validate_program([{"op": "branch", "on": "k"}])
    with pytest.raises(DslError):
        validate_program([{"op": "escalate", "level": "", "issue": "x"}])


def test_program_must_be_a_list() -> None:
    with pytest.raises(DslError):
        validate_program({"op": "verify", "scope": "s"})


@pytest.mark.asyncio
async def test_step_limit_is_enforced() -> None:
    program = [{"op": "verify", "scope": "s"}] * 5
    _, runtime, _ = await run_program(program, max_steps=5)
    assert runtime.steps_run == 5

    with pytest.raises(StepLimitExceeded, match=STEP_LIMIT_ERROR):
        host = RecordingHost()
        await Runtime(host, max_steps=3).run(program)


@pytest.mark.asyncio
async def test_nested_branch_steps_count_towards_limit() -> None:
    program = [
        {
            "op": "branch",
            "on": "go",
            "then": [
                {"op": "verify", "scope": "a"},
                {"op": "verify", "scope": "b"},
            ],
        }
    ]
    _, runtime, _ = await run_program(
        program, conditions={"go": True}, max_steps=3
    )
    assert runtime.steps_run == 3

    with pytest.raises(StepLimitExceeded):
        host = RecordingHost({"go": True})
        await Runtime(host, max_steps=2).run(program)


def test_default_step_limit_is_64() -> None:
    assert DEFAULT_MAX_STEPS == 64
