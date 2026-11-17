"""One deadline for every attempt of an automatic approval review.

Adapted from OpenAI Codex guardian-reviewer/src/retry.rs at
afb436df8b70bb5bc57b86d9a3e829968988cd21 (Apache-2.0).
Copyright 2025 OpenAI. See third_party/codex/NOTICE and LICENSE.

Changes: Python monotonic budget, asyncio cancellation supplied by the caller,
and MewCode's existing two-attempt and provider-cooldown policy. This budget
never retries the action being reviewed or treats service failure as consent.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time


@dataclass(frozen=True)
class ReviewBudget:
    deadline: float
    max_attempts: int = 2

    @classmethod
    def start(cls, seconds: float, *, max_attempts: int = 2) -> "ReviewBudget":
        if not math.isfinite(seconds) or seconds <= 0 or max_attempts < 1:
            raise ValueError("review budget must have a finite positive deadline and attempts")
        return cls(time.monotonic() + seconds, max_attempts)

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def can_retry(self, completed_attempts: int, *, delay: float = 0.0) -> bool:
        # A provider cooldown is never shortened merely to fit our budget.
        return (completed_attempts < self.max_attempts and math.isfinite(delay)
                and delay >= 0 and self.remaining() > delay)
