"""Responsibility-layer contracts, separate from sealed v2 task definitions.

Keeping these in side tables preserves the hashes of existing TeamSpecs.
Declarations describe intent; only authenticated host commands confer authority.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .spec import AcceptanceCriterion, ContractModel, Identifier, Text


class MissionContract(ContractModel):
    objective: Text
    hard_constraints: tuple[Text, ...] = ()
    success_criteria: Annotated[tuple[AcceptanceCriterion, ...], Field(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def unique_criteria(self):
        keys = [c.criterion_id for c in self.success_criteria]
        if len(keys) != len(set(keys)):
            raise ValueError("mission criterion IDs must be unique")
        return self


class Responsibility(ContractModel):
    task_key: Identifier
    owner_role: Identifier
    goal_criteria: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    parent_task_key: Identifier | None = None
    decision_scope: tuple[Literal["investigate", "propose_plan", "request_help"], ...] = ("investigate", "request_help")


class CoordinationPolicy(ContractModel):
    autonomous: bool = False
    internal_replan: bool = False
    automatic_receipts: bool = False
    automatic_verification: bool = False
    max_parallel: Annotated[int, Field(strict=True, ge=1, le=32)] = 1
    max_tool_rounds: Annotated[int, Field(strict=True, ge=1, le=64)] = 12
    stagnation_rounds: Annotated[int, Field(strict=True, ge=1, le=32)] = 3
    root_validator: Text | None = None
    max_attempts_per_revision: Annotated[int, Field(strict=True, ge=1, le=8)] = 2
    plan_before_execution: bool = False


class ModelPreference(ContractModel):
    mode: Literal["fixed", "preferred", "auto"] = "fixed"
    candidates: Annotated[tuple[Text, ...], Field(min_length=1, max_length=64)]
    instances: Annotated[int, Field(strict=True, ge=1, le=32)] = 1
    effort: Text | None = None

    @model_validator(mode="after")
    def allowed_candidates(self):
        if len(set(self.candidates)) != len(self.candidates):
            raise ValueError("model candidates must be unique")
        if self.mode == "fixed" and len(self.candidates) != 1:
            raise ValueError("fixed assignment requires exactly one model")
        if any(value == "codex-auto-review" or "/" not in value for value in self.candidates):
            raise ValueError("use provider/model IDs, never approval route labels")
        return self


class DisputeSubmission(ContractModel):
    task_id: Text
    revision: Annotated[int, Field(strict=True, ge=1)]
    criterion_id: Identifier
    artifact_version: Text | None = None
    problem: Text
    reproduction: Text
    owner_role: Identifier
    # A stable semantic key, not the wording of a repeated complaint.
    issue_key: Identifier


class DisputeEvidence(ContractModel):
    kind: Literal["observation", "test", "repair", "ruled_out", "contradiction"]
    source_ref: Text
    source_hash: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    detail: Text
    task_revision: Annotated[int, Field(strict=True, ge=1)]
    verification: Text


class Intervention(ContractModel):
    intent: Literal["question", "information", "change_goal", "change_task", "pause", "cancel", "auto"]
    message: Text
    task_id: Text | None = None
    expected_goal_version: Annotated[int, Field(strict=True, ge=1)]
    expected_plan_revision: Annotated[int, Field(strict=True, ge=1)]
    task_revision: Annotated[int, Field(strict=True, ge=1)] | None = None
    mission: MissionContract | None = None
    contract: dict | None = None
    assignments: tuple[Responsibility, ...] | None = None
    # An explicit change command still needs current-state and policy checks.
    confirmed: bool = False


class ResourceGrant(ContractModel):
    resource_id: Text
    kind: Literal["workspace", "artifact", "source"]
    locator: Text
    tools: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    writable: bool = False


class ToolInvocation(ContractModel):
    call_id: Identifier
    name: Identifier
    resource_id: Text
    arguments: dict = Field(default_factory=dict)
