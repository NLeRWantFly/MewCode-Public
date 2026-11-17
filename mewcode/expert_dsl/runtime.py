"""Orchestration DSL: a bounded, interpretable execution-flow language.

The planner expresses the expert-team execution flow as a small program built
from a fixed set of primitive ops (spawn / fanout / wait / branch / escalate /
verify). This module validates such a program and interprets it with hard
bounds: a step limit and an optional deadline.

The runtime itself performs no real work. Every primitive is delegated to a
Host, which is how the interpreter stays testable without real agents - the
orchestrator (a later task) plugs the real agent / audit / permission logic
into that interface.
"""

from __future__ import annotations

import time
from typing import Any, Protocol, Sequence

# --- op names -----------------------------------------------------------------

OP_SPAWN = "spawn"
OP_FANOUT = "fanout"
OP_WAIT = "wait"
OP_BRANCH = "branch"
OP_ESCALATE = "escalate"
OP_VERIFY = "verify"

KNOWN_OPS: frozenset[str] = frozenset(
    {OP_SPAWN, OP_FANOUT, OP_WAIT, OP_BRANCH, OP_ESCALATE, OP_VERIFY}
)

# --- bounds and error texts (pinned in checklist) ------------------------------

DEFAULT_MAX_STEPS = 64

STEP_LIMIT_ERROR = "编排步数超限"
TIMEOUT_ERROR = "编排执行超时"


class DslError(ValueError):
    """Raised when a program is structurally invalid or misbehaves."""


class StepLimitExceeded(DslError):
    """Raised when a program exceeds its step budget."""


# --- program shape -------------------------------------------------------------

# A program is a list of steps. A step is a dict with an "op" key plus
# op-specific fields; branch steps nest two sub-programs under then / else.
Step = dict[str, Any]
Program = list[Step]


def _require_str(step: Step, key: str, path: str) -> str:
    value = step.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DslError(f"{path} {key} 必须是非空字符串")
    return value


def _validate_step(step: Any, path: str) -> Step:
    if not isinstance(step, dict):
        raise DslError(f"{path} 必须是对象")
    op = step.get("op")
    if op not in KNOWN_OPS:
        raise DslError(f"{path} 出现未定义原语: {op!r}")
    if op == OP_SPAWN:
        _require_str(step, "role", path)
        _require_str(step, "task", path)
    elif op == OP_FANOUT:
        _require_str(step, "role", path)
        tasks = step.get("tasks")
        if (
            not isinstance(tasks, list)
            or not tasks
            or not all(isinstance(item, str) and item.strip() for item in tasks)
        ):
            raise DslError(f"{path} fanout.tasks 必须是非空字符串列表")
    elif op == OP_WAIT:
        pass
    elif op == OP_BRANCH:
        _require_str(step, "on", path)
        if "then" not in step:
            raise DslError(f"{path} branch 需要 then 子步骤")
        _validate_subprogram(step["then"], f"{path}.then")
        if "else" in step and step["else"] is not None:
            _validate_subprogram(step["else"], f"{path}.else")
    elif op == OP_ESCALATE:
        _require_str(step, "level", path)
        _require_str(step, "issue", path)
    elif op == OP_VERIFY:
        _require_str(step, "scope", path)
    return step


def _validate_subprogram(value: Any, path: str) -> Program:
    if not isinstance(value, list):
        raise DslError(f"{path} 必须是步骤列表")
    validated: Program = []
    for index, item in enumerate(value):
        validated.append(_validate_step(item, f"{path}[{index}]"))
    return validated


def validate_program(program: Any) -> Program:
    """Validate a whole program up front; raise DslError on the first problem."""
    if not isinstance(program, list):
        raise DslError("DSL 程序必须是步骤列表")
    validated: Program = []
    for index, item in enumerate(program):
        validated.append(_validate_step(item, f"steps[{index}]"))
    return validated


# --- host ----------------------------------------------------------------------

class Host(Protocol):
    """The real-work side of the DSL: agents, audit, permission escalations."""

    async def spawn(self, role: str, task: str) -> Any: ...

    async def fanout(self, role: str, tasks: list[str]) -> list[Any]: ...

    async def wait(self) -> list[Any]: ...

    async def escalate(self, level: str, issue: str) -> Any: ...

    async def verify(self, scope: str) -> Any: ...

    async def evaluate(self, key: str) -> bool: ...


# --- runtime -------------------------------------------------------------------

class Runtime:
    """Interprets a validated DSL program with hard bounds.

    Step accounting is dynamic: each op execution counts one step, and nested
    branch sub-programs count their own steps too. Exactly max_steps passes;
    one more raises StepLimitExceeded. The optional deadline is checked before
    every step.
    """

    def __init__(
        self,
        host: Host,
        *,
        max_steps: int = DEFAULT_MAX_STEPS,
        timeout_seconds: float | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps 必须为正数")
        self._host = host
        self._max_steps = max_steps
        self._timeout_seconds = timeout_seconds
        self._steps = 0
        self._deadline: float | None = None

    @property
    def steps_run(self) -> int:
        return self._steps

    async def run(self, program: Any) -> list[Any]:
        validated = validate_program(program)
        self._steps = 0
        self._deadline = (
            time.monotonic() + self._timeout_seconds
            if self._timeout_seconds is not None
            else None
        )
        return await self._run_seq(validated)

    async def _run_seq(self, steps: Sequence[Step]) -> list[Any]:
        results: list[Any] = []
        for step in steps:
            results.append(await self._run_step(step))
        return results

    async def _run_step(self, step: Step) -> Any:
        self._tick()
        op = step["op"]
        if op == OP_SPAWN:
            return await self._host.spawn(step["role"], step["task"])
        if op == OP_FANOUT:
            return await self._host.fanout(step["role"], list(step["tasks"]))
        if op == OP_WAIT:
            return await self._host.wait()
        if op == OP_ESCALATE:
            return await self._host.escalate(step["level"], step["issue"])
        if op == OP_VERIFY:
            return await self._host.verify(step["scope"])
        if op == OP_BRANCH:
            condition = await self._host.evaluate(step["on"])
            sub = step["then"] if condition else step.get("else") or []
            return await self._run_seq(sub)
        # Unreachable after validation; kept as a hard stop.
        raise DslError(f"出现未定义原语: {op!r}")

    def _tick(self) -> None:
        self._steps += 1
        if self._steps > self._max_steps:
            raise StepLimitExceeded(STEP_LIMIT_ERROR)
        if self._deadline is not None and time.monotonic() > self._deadline:
            raise DslError(TIMEOUT_ERROR)
