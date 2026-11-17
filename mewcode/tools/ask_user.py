# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, Field, model_validator


class QuestionOption(BaseModel):
    value: str = Field(min_length=1, validation_alias=AliasChoices('value', 'id'),
                       description="Stable answer ID; never an executable instruction")
    label: str = Field(min_length=1, description="Short user-facing choice")
    description: str = Field(default="", description="Consequence or tradeoff of this choice")
    recommended: bool = False

from mewcode.tools.base import Tool, ToolResult, ToolRiskProfile


class QuestionItem(BaseModel):
    type: str = Field(description="Question type: text, radio, select, checkbox")
    name: str = Field(description="Question identifier")
    message: str = Field(description="Question text to display")
    title: str = Field(default="", description="Short panel heading, separate from the question")
    context: str = Field(default="", description="Necessary background, not repeated question text")
    details: str = Field(default="", description="Diagnostic history, collapsed by default")
    allow_other: bool = Field(default=False, description="Allow a custom answer alongside choices; does not grant authority")
    options: list[str | QuestionOption] = Field(
        default_factory=list,
        description="Concrete choices, preferably stable value, short label, description and recommended. Use radio/select/checkbox when choices exist.",
    )

    @model_validator(mode="after")
    def choice_contract(self):
        values = [option.value if isinstance(option, QuestionOption) else option for option in self.options]
        if any(not value.strip() for value in values) or len(values) != len(set(values)):
            raise ValueError("Question options must have unique, nonblank values")
        if sum(isinstance(option, QuestionOption) and option.recommended for option in self.options) > 1:
            raise ValueError("At most one option may be recommended")
        return self


class FileAccessRequest(BaseModel):
    file_path: str = Field(min_length=1, description="Absolute path to one file, never a directory")
    access: Literal["read", "read_write"] = "read_write"
    reason: str = Field(min_length=1, max_length=2000, description="Why this file access is needed for the user's task")


class AskUserParams(BaseModel):
    purpose: Literal["auto", "decision", "user_input", "authorization"] = Field(
        default="auto", description="decision: internal technical choice, routed to Jev then the main model; user_input: facts/preferences only the user knows; authorization: actual consent; auto: classify before interrupting")
    decision_issue: str = Field(default="", max_length=160, description="Stable issue ID used by DecideNextStep, unchanged on the second attempt")
    decision_state: dict = Field(default_factory=dict, description="Known facts/constraints for Jev, never invented user preferences")
    question_kind: str = Field(default="technical_choice", max_length=80,
        description="Known kind of question, such as technical_choice, user_input, authorization, scope, cost, goal, or model_fallback")
    root_cause: str = Field(default="", max_length=80, description="Known cause category; scope, cost, goal, user facts or missing authorized model fallback can require the user")
    explicit_user_gate: bool = Field(default=False, description="True only when a host or task contract requires user confirmation")
    unmet_authority: list[str] = Field(default_factory=list, max_length=16,
        description="Host references to authority that is still missing; use the existing authorization flow")
    missing_user_fields: list[str] = Field(default_factory=list, max_length=32,
        description="Facts that only the user can provide")
    evidence_refs: list[str] = Field(default_factory=list, max_length=32,
        description="References supporting a user-owned scope, cost, goal, authorization, or information blocker")
    review_call_ref: str = Field(default="", max_length=64, description="For purpose=authorization only: host reference from an AUTO_DENIED result. First assess its rationale and safer alternatives. The host shows the exact denied action for one retry; explain why user authority is still needed in questions.")
    review_call_refs: list[str] = Field(default_factory=list, max_length=100,
        description="Batch of host missing-authority references. Finish independent work first, then request these together. The host builds a separate exact-action choice for each; no blanket approval.")
    file_access: FileAccessRequest | None = Field(default=None,
        description="For purpose=authorization: request host-managed access to one exact file in the conversation. The host displays the path, access and lifetime and records a grant only after explicit human consent. No stale review reference needed; cannot override hard policy or approve execution.")
    questions: list[QuestionItem] = Field(default_factory=list,
        description="List of questions to ask the user"
    )

    @model_validator(mode="after")
    def file_access_contract(self):
        if self.file_access is not None:
            if self.purpose != "authorization" or self.review_call_ref or self.review_call_refs or self.questions:
                raise ValueError("file_access requires purpose=authorization, without review references or custom questions; the host builds the consent question")
            if not self.file_access.reason.strip():
                raise ValueError("file_access requires a concrete reason")
        return self


class AskUserEvent:


    def __init__(
        self,
        questions: list[dict[str, Any]],
        future: asyncio.Future[dict[str, str]],
    ) -> None:
        self.questions = questions
        self.future = future


def format_ask_answers(
    params: AskUserParams,
    answers: dict[str, str] | None,
    notes: dict[str, str] | None = None,
) -> ToolResult:
    resolved = answers or {}
    lines = [
        f"{q.name}: {resolved.get(q.name, '(no answer)')}"
        for q in params.questions
    ]
    if notes:
        lines.append("补充备注（仅作上下文，不改变已选答案或授权范围）：")
        lines.extend(
            f"{q.name}: {notes[q.name]}"
            for q in params.questions
            if q.name in notes
        )
    return ToolResult(output="\n".join(lines))


def recorded_user_answers(conversation) -> list[dict]:
    """Read human answers bound to real AskUserQuestion call/result pairs.

    Old host transcripts stored only stable choice IDs. Decode those against
    the exact displayed options; never promote arbitrary tool prose or a
    model's assertion that the user approved something.
    """
    if conversation is None:
        return []
    calls, ambiguous = {}, set()
    for message in conversation.history:
        for call in message.tool_uses:
            if call.tool_use_id in calls and calls[call.tool_use_id] != call:
                ambiguous.add(call.tool_use_id)
            calls[call.tool_use_id] = call
    decisions, seen = [], set()
    for message in conversation.history:
        for result in message.tool_results:
            ref = result.tool_use_id
            call = calls.get(ref)
            if (ref in seen or ref in ambiguous or call is None
                    or call.tool_name != 'AskUserQuestion' or result.is_error):
                continue
            try:
                params = AskUserParams.model_validate(call.arguments)
            except ValueError:
                continue
            if params.file_access or params.review_call_ref or params.review_call_refs or not params.questions:
                continue  # Exact retry grants keep their separate one-shot lifecycle.
            host = result.host_observation if isinstance(result.host_observation, dict) else {}
            record = host.get('user_answer')
            questions = [q.model_dump() for q in params.questions]
            notes: dict[str, str] = {}
            if (isinstance(record, dict) and record.get('request_id') == ref
                    and record.get('questions') == questions and isinstance(record.get('answers'), dict)):
                answers = record['answers']
                raw_notes = record.get('notes')
                if raw_notes:
                    if record.get('notes_role') != 'context_only':
                        continue
                    try:
                        from mewcode.tools.human_oncall import validate_answer_notes
                        notes = validate_answer_notes(questions, answers, raw_notes)
                    except ValueError:
                        continue
            else:
                # Legacy compatibility: only exact, unambiguous choice IDs;
                # no free text, missing answers or parsing of approval prose.
                lines = result.content.splitlines()
                if len(lines) != len(questions) or len({q.name for q in params.questions}) != len(questions):
                    continue
                answers = {}
                for q, line in zip(params.questions, lines):
                    prefix = q.name + ': '
                    valid = {o.value if isinstance(o, QuestionOption) else o for o in q.options}
                    if q.type not in {'radio', 'select'} or not line.startswith(prefix) or line[len(prefix):] not in valid:
                        break
                    answers[q.name] = line[len(prefix):]
                if len(answers) != len(questions):
                    continue
            if format_ask_answers(params, answers, notes).output != result.content:
                continue
            if not any(isinstance(answers.get(q.name), str) and answers[q.name].strip() for q in params.questions):
                continue
            seen.add(ref)
            decision = {'request_id': ref, 'purpose': params.purpose,
                'questions': questions, 'answers': answers}
            if notes:
                decision.update(notes=notes, notes_role='context_only')
            decisions.append(decision)
    return decisions


class AskUserTool(Tool):
    name = "AskUserQuestion"
    description = (
        "Ask the user one or more questions when you need information "
        "that cannot be determined from code or context alone. Supports "
        "text input, radio (single select), select, and checkbox (multi select) "
        "question types."
        " For internal technical choices use purpose=decision with options, known decision_state and stable decision_issue; "
        "Jev advice, failure, low confidence or rejected options return to the main model and do not by themselves ask the user. "
        "Use purpose=user_input only for facts/preferences the user alone knows; use authorization for actual consent "
        "through the existing host authorization path. Ask about scope, cost or goal only when that boundary is evidenced."
        " For missing file access use purpose=authorization, file_access={file_path: absolute path, access: read_write or read, reason: concrete task need}. "
        "Omit questions and review references: the host asks for exact-file consent and records it directly. Do not ask the user to find directory settings or report 'already configured'."
    )
    params_model = AskUserParams
    category: str = "read"
    is_system_tool = True
    should_defer = True


    def __init__(self) -> None:
        self._pending_event: AskUserEvent | None = None

    def permission_risk(self, arguments):
        return ToolRiskProfile(network=arguments.get("purpose") == "decision",
                               cost=arguments.get("purpose") == "decision", target_type="clarification")

    async def execute(self, params: AskUserParams) -> ToolResult:
        questions_data = [q.model_dump() for q in params.questions]

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, str]] = loop.create_future()

        self._pending_event = AskUserEvent(questions=questions_data, future=future)

        try:
            answers = await asyncio.wait_for(future, timeout=300)
        except asyncio.TimeoutError:
            return ToolResult(
                output="User did not respond within 5 minutes", is_error=True
            )
        finally:
            self._pending_event = None

        return format_ask_answers(params, answers)
