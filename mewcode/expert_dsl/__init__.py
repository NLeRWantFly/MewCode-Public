"""Orchestration DSL for the expert team.

The planner expresses the execution flow as a bounded program of primitive
ops; Runtime interprets it and delegates the real work to a Host.
"""

from mewcode.expert_dsl.runtime import (
    DEFAULT_MAX_STEPS,
    KNOWN_OPS,
    OP_BRANCH,
    OP_ESCALATE,
    OP_FANOUT,
    OP_SPAWN,
    OP_VERIFY,
    OP_WAIT,
    STEP_LIMIT_ERROR,
    TIMEOUT_ERROR,
    DslError,
    Host,
    Program,
    Runtime,
    Step,
    StepLimitExceeded,
    validate_program,
)

__all__ = [
    "DEFAULT_MAX_STEPS",
    "KNOWN_OPS",
    "OP_BRANCH",
    "OP_ESCALATE",
    "OP_FANOUT",
    "OP_SPAWN",
    "OP_VERIFY",
    "OP_WAIT",
    "STEP_LIMIT_ERROR",
    "TIMEOUT_ERROR",
    "DslError",
    "Host",
    "Program",
    "Runtime",
    "Step",
    "StepLimitExceeded",
    "validate_program",
]
