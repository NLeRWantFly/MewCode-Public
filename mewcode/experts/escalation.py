"""Permission escalation chain: subagent → executor → planner → manager.

Models the v3 rule: a permission request travels up the hierarchy. A deny at
any level blocks it, a defer passes it up, an approve grants it. The manager
(top level) is the final authority and cannot defer. Every decision is
recorded for traceability, and the chain has a fixed depth (the levels tuple).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 上报链的四级，顺序即深度上限。
ESCALATION_LEVELS: tuple[str, ...] = ("subagent", "executor", "planner", "manager")

DECISION_APPROVE = "approve"
DECISION_DENY = "deny"
DECISION_DEFER = "defer"

OUTCOME_GRANTED = "granted"
OUTCOME_REJECTED = "rejected"


class EscalationError(ValueError):
    """Raised when the chain is misused (bad level, late decision, etc.)."""


@dataclass(frozen=True)
class EscalationDecision:
    """One level's recorded decision - the traceability unit."""

    level: str
    outcome: str
    reason: str = ""
    by: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "level": self.level,
            "outcome": self.outcome,
            "reason": self.reason,
            "by": self.by,
        }


@dataclass
class EscalationChain:
    """The permission chain with a hard depth cap and a full decision trail."""

    levels: tuple[str, ...] = ESCALATION_LEVELS
    decisions: list[EscalationDecision] = field(default_factory=list)
    outcome: str | None = None

    def decide(
        self,
        level: str,
        outcome: str,
        *,
        reason: str = "",
        by: str = "",
    ) -> None:
        """Record one level's decision.

        approve → granted; deny → rejected; defer → stays open (passes up).
        The manager (top level) cannot defer - it is the final authority.
        """
        if self.outcome is not None:
            raise EscalationError("上报链已终结，不能再做决策")
        if level not in self.levels:
            raise EscalationError(f"未知层级: {level!r}")
        if outcome not in {DECISION_APPROVE, DECISION_DENY, DECISION_DEFER}:
            raise EscalationError(f"非法决策: {outcome!r}")
        if outcome == DECISION_DEFER and level == self.levels[-1]:
            raise EscalationError("管理专家是最终裁决者，不能继续上报")
        self.decisions.append(EscalationDecision(level, outcome, reason, by))
        if outcome == DECISION_APPROVE:
            self.outcome = OUTCOME_GRANTED
        elif outcome == DECISION_DENY:
            self.outcome = OUTCOME_REJECTED

    def next_level(self, level: str) -> str | None:
        """The level above the given one, or None at the top (depth cap)."""
        if level not in self.levels:
            raise EscalationError(f"未知层级: {level!r}")
        index = self.levels.index(level)
        if index + 1 >= len(self.levels):
            return None
        return self.levels[index + 1]

    @property
    def is_terminated(self) -> bool:
        return self.outcome is not None

    @property
    def any_denied(self) -> bool:
        return any(item.outcome == DECISION_DENY for item in self.decisions)

    @property
    def trace(self) -> tuple[dict[str, str], ...]:
        return tuple(item.as_dict() for item in self.decisions)
