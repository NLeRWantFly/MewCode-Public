from __future__ import annotations

import json
from abc import abstractmethod
from typing import Any

from pydantic import BaseModel, Field, model_validator

from mewcode.tools.base import Tool, ToolResult


class HumanOnCallOption(BaseModel):
    id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
        description="Stable identifier returned in the tool result",
    )
    label: str = Field(
        min_length=1,
        max_length=120,
        description="Short user-facing option label",
    )
    description: str = Field(
        default="",
        max_length=500,
        description="One sentence explaining the option's impact or trade-off",
    )
    recommended: bool = Field(
        default=False,
        description="Whether this is the agent's recommended option",
    )


class HumanOnCallParams(BaseModel):
    question: str = Field(
        min_length=1,
        max_length=500,
        description="A concrete decision the user needs to make",
    )
    context: str = Field(
        default="",
        max_length=2000,
        description="Why the decision is needed and what it changes",
    )
    options: list[HumanOnCallOption] = Field(
        min_length=2,
        max_length=6,
        description="Two to six mutually exclusive choices",
    )
    allow_other: bool = Field(
        default=True,
        description="Allow the user to provide a custom answer",
    )

    @model_validator(mode="after")
    def validate_options(self) -> HumanOnCallParams:
        option_ids = [option.id for option in self.options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("option ids must be unique")
        if sum(option.recommended for option in self.options) > 1:
            raise ValueError("at most one option may be recommended")
        return self


class HumanInputTool(Tool):
    """Base contract for tools that suspend the Agent for human input."""

    category = "read"
    is_concurrency_safe = False
    is_system_tool = True
    interaction_kind = "human_oncall"

    @abstractmethod
    def build_questions(self, params: BaseModel) -> list[dict[str, Any]]: ...

    @abstractmethod
    def format_answers(
        self,
        params: BaseModel,
        answers: dict[str, str] | None,
        notes: dict[str, str] | None = None,
    ) -> ToolResult: ...

    async def execute(self, params: BaseModel) -> ToolResult:
        return ToolResult(
            output=(
                "Human input is unavailable in this non-interactive execution "
                "context. Continue with a safe reversible default or report the blocker."
            ),
            is_error=True,
        )


class HumanOnCallTool(HumanInputTool):
    name = "human_oncall"
    description = (
        "Pause the current task and ask the user to make one material choice. "
        "Use only when the answer cannot be discovered from available context and "
        "different choices would materially change the result. Provide 2-6 concise "
        "options, mark at most one recommendation, and continue from the structured "
        "tool result. Do not use for routine status updates or reversible defaults."
    )
    params_model = HumanOnCallParams

    def build_questions(self, params: BaseModel) -> list[dict[str, Any]]:
        assert isinstance(params, HumanOnCallParams)
        return [
            {
                "name": "choice",
                "header": "需要你的决定",
                "question": params.question,
                "context": params.context,
                "options": [
                    {
                        "value": option.id,
                        "label": option.label,
                        "description": option.description,
                        "recommended": option.recommended,
                    }
                    for option in params.options
                ],
                "allowOther": params.allow_other,
                "required": True,
            }
        ]

    def format_answers(
        self,
        params: BaseModel,
        answers: dict[str, str] | None,
        notes: dict[str, str] | None = None,
    ) -> ToolResult:
        assert isinstance(params, HumanOnCallParams)
        if answers is None:
            return ToolResult(
                output=json.dumps(
                    {"status": "cancelled", "reason": "user_cancelled"},
                    ensure_ascii=False,
                )
            )

        answer = str(answers.get("choice", "")).strip()
        by_id = {option.id: option for option in params.options}
        if answer in by_id:
            option = by_id[answer]
            payload = {
                "status": "answered",
                "choice": {
                    "id": option.id,
                    "label": option.label,
                    "is_other": False,
                },
            }
            if notes:
                payload["notes"] = dict(notes)
                payload["notes_role"] = "context_only"
            return ToolResult(output=json.dumps(payload, ensure_ascii=False))

        if params.allow_other and answer:
            payload = {
                "status": "answered",
                "choice": {
                    "id": "other",
                    "label": answer,
                    "is_other": True,
                },
            }
            if notes:
                payload["notes"] = dict(notes)
                payload["notes_role"] = "context_only"
            return ToolResult(output=json.dumps(payload, ensure_ascii=False))

        return ToolResult(
            output=json.dumps(
                {"status": "invalid", "reason": "answer_not_in_options"},
                ensure_ascii=False,
            ),
            is_error=True,
        )


def validate_human_answers(
    questions: list[dict[str, Any]],
    answers: dict[str, Any] | None,
) -> dict[str, str] | None:
    """Validate UI responses before they are returned to the waiting Agent."""

    if answers is None:
        return None
    if not isinstance(answers, dict):
        raise ValueError("answers must be an object")

    normalized: dict[str, str] = {}
    for question in questions:
        name = str(question.get("name", "")).strip()
        if not name:
            raise ValueError("question is missing a name")
        raw = answers.get(name, "")
        value = str(raw).strip()
        allowed = {
            str(option.get("value", option.get("label", ""))).strip()
            if isinstance(option, dict)
            else str(option).strip()
            for option in question.get("options", [])
        }
        allowed.discard("")
        allow_other = bool(question.get("allowOther", True))
        required = bool(question.get("required", False))
        multi_select = bool(question.get("multiSelect", False))

        if multi_select and value:
            values = [item.strip() for item in value.split(",") if item.strip()]
            if not allow_other and any(item not in allowed for item in values):
                raise ValueError(f"invalid answer for question '{name}'")
        elif value and value not in allowed and not allow_other:
            raise ValueError(f"invalid answer for question '{name}'")
        elif required and not value:
            raise ValueError(f"question '{name}' requires an answer")

        normalized[name] = value

    return normalized


def validate_answer_notes(
    questions: list[dict[str, Any]],
    answers: dict[str, Any] | None,
    notes: dict[str, Any] | None,
) -> dict[str, str]:
    """Validate optional per-question context without changing the answer.

    Notes are deliberately keyed to the questions shown in the current card.
    They are context for the model only: an empty answer cannot be accompanied
    by a meaningful note, and a note can never introduce another field or
    option.  The returned mapping is a small detached copy suitable for
    storing on the waiting request.
    """

    if notes is None:
        return {}
    if not isinstance(notes, dict):
        raise ValueError("notes must be an object")
    displayed = {
        str(question.get("name") or "")
        for question in questions
        if isinstance(question, dict) and str(question.get("name") or "")
    }
    if not isinstance(answers, dict):
        raise ValueError("answers must be an object when notes are supplied")
    invalid = [str(key) for key in notes if not isinstance(key, str) or key not in displayed]
    if invalid:
        raise ValueError("notes contain a question that was not displayed")
    validated: dict[str, str] = {}
    for key, value in notes.items():
        name = str(key)
        answer = answers.get(key)
        if answer is None and key != name:
            answer = answers.get(name)
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError(f"notes require a non-empty answer for question '{name}'")
        if not isinstance(value, str):
            raise ValueError(f"note for question '{name}' must be text")
        if len(value) > 2000:
            raise ValueError(f"note for question '{name}' is too long")
        if "\x00" in value:
            raise ValueError(f"note for question '{name}' contains an invalid NUL")
        validated[name] = value
    return validated
