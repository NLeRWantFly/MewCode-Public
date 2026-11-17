"""Stable mother contract: a batch of independent typed questions, not a winner."""
from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["choice", "noul", "score"]
    instructions: str = Field(min_length=1, max_length=8000)
    criteria: dict | list[str] | None = None

    @model_validator(mode="after")
    def typed_criteria(self):
        if self.type == "choice" and (not isinstance(self.criteria, dict) or not 2 <= len(self.criteria) <= 64):
            raise ValueError("Choice requires 2–64 explicitly named candidates")
        if self.type == "score" and (not isinstance(self.criteria, list) or not 2 <= len(self.criteria) <= 10):
            raise ValueError("Score requires 2–10 ordered descriptions")
        if self.type == "noul" and self.criteria is not None:
            raise ValueError("Noul has no competing candidates")
        return self


class JudgmentSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str = Field(pattern=r"^[a-z0-9-]{1,64}$")
    version: str = "1"
    mode: Literal["advisory", "shadow"] = "advisory"
    failure_policy: Literal["return_to_caller", "preserve"] = "return_to_caller"
    max_state_bytes: int = Field(default=18000, ge=256, le=24000)
    max_request_bytes: int = Field(default=28000, ge=1024, le=28000)
    max_questions_per_batch: int = Field(default=12, ge=1, le=32)
    max_batches: int = Field(default=4, ge=1, le=8)
    concurrency: int = Field(default=2, ge=1, le=2)
    timeout_seconds: float = Field(default=12, gt=0, le=30)
    max_rounds: int = Field(default=2, ge=1, le=2)
    max_reservations: int = Field(default=8, ge=1, le=16)
    max_reserved_bytes: int = Field(default=240000, ge=1024, le=240000)


class JudgmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec: str = Field(pattern=r"^[a-z0-9-]{1,64}$")
    issue: str = Field(min_length=1, max_length=160)
    state: dict
    questions: dict[str, Question] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def question_ids(self):
        import re
        if any(not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", key) for key in self.questions):
            raise ValueError("Use stable, non-secret question IDs")
        return self


def validate_answers(questions: dict, answers: object) -> dict:
    """Validate adapters as well as native clients. Missing never means false."""
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise ValueError("Judgment answers must exactly match the submitted IDs")

    def probability(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("Invalid judgment probability")
        return value

    validated = {}
    for key, question in questions.items():
        answer = answers[key]
        if not isinstance(answer, dict) or answer.get("type") != question["type"]:
            raise ValueError("Judgment type mismatch")
        if question["type"] == "noul":
            probability(answer.get("noul"))
            validated[key] = {"type": "noul", "noul": answer["noul"]}
            continue
        criteria = question["criteria"]
        expected = criteria if question["type"] == "choice" else {str(i): v for i, v in enumerate(criteria)}
        values = answer.get("probabilities")
        if not isinstance(values, dict) or set(values) != set(expected):
            raise ValueError("Incomplete judgment probabilities")
        for value in values.values():
            probability(value)
        if abs(sum(values.values()) - 1) > .01:
            raise ValueError("Invalid probability sum")
        probability(answer.get("confidence"))
        if question["type"] == "choice":
            choice = answer.get("choice")
            if choice not in values or values[choice] != max(values.values()):
                raise ValueError("Choice contradicts probabilities")
        else:
            score = answer.get("score")
            if (answer.get("legend") != expected or isinstance(score, bool)
                    or not isinstance(score, (int, float)) or not math.isfinite(score)
                    or abs(score - sum(int(k) * v for k, v in values.items())) > .02):
                raise ValueError("Score contradicts its legend/probabilities")
        fields = ("type", "choice", "confidence", "probabilities") if question["type"] == "choice" else (
            "type", "score", "confidence", "probabilities", "legend")
        validated[key] = {field: answer[field] for field in fields}
    return validated
