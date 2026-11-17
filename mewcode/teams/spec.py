"""Provider-neutral, immutable contracts for opt-in Agent Team v2.

These are definitions, not execution authority. Parsing them never creates a
team, imports a validator, starts a process, or calls a model. Legacy team
models deliberately remain unchanged.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,95}$")]
Text = Annotated[str, Field(min_length=1, max_length=16000)]
SchemaRef = Annotated[str, Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_./-]*/v[1-9][0-9]*$")]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]
Action = Literal["read", "plan", "write_candidate", "verify", "publish"]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True,
                              allow_inf_nan=False)


class SpecRef(ContractModel):
    spec_id: Identifier
    version: PositiveInt


class PortSpec(ContractModel):
    port_id: Identifier
    schema_ref: SchemaRef
    required: bool = True


class RoleSpec(ContractModel):
    role_id: Identifier
    responsibility: Text
    required_capabilities: tuple[Text, ...] = ()
    allowed_actions: tuple[Action, ...] = ("read",)
    independent_from: tuple[Identifier, ...] = ()
    max_members: PositiveInt = 1


class AcceptanceCriterion(ContractModel):
    criterion_id: Identifier
    description: Text
    # Integer basis points avoid float ambiguity in acceptance snapshots.
    minimum_score: Annotated[int, Field(strict=True, ge=0, le=10000)] = 10000


class VerificationSpec(ContractModel):
    kind: Literal["command", "registered_validator", "human_review"]
    ref: Text


class PolicyLimits(ContractModel):
    """Exact allowlists. Resource IDs are grants, never filesystem globs.

    Empty sets mean deny, not unrestricted. The compiler does not interpret
    paths or mint grants; runtime must resolve and recheck them before use.
    """

    allowed_actions: tuple[Action, ...]
    allowed_resources: tuple[Text, ...]
    allowed_models: tuple[Text, ...]
    max_tasks: Annotated[int, Field(strict=True, ge=1, le=10000)] = 128
    max_inheritance_depth: Annotated[int, Field(strict=True, ge=1, le=32)] = 8
    token_budget: Annotated[int, Field(strict=True, ge=0)] = 0
    self_acceptance_forbidden: bool = True


class TaskTemplate(ContractModel):
    task_key: Identifier
    role_id: Identifier
    reviewer_role: Identifier
    objective: Text
    input_scope: tuple[Text, ...]
    forbidden_changes: Annotated[tuple[Text, ...], Field(min_length=1)]
    inputs: tuple[PortSpec, ...] = ()
    outputs: Annotated[tuple[PortSpec, ...], Field(min_length=1)]
    required_actions: Annotated[tuple[Action, ...], Field(min_length=1)] = ("read",)
    verification: VerificationSpec
    acceptance: Annotated[tuple[AcceptanceCriterion, ...], Field(min_length=1)]
    required: bool = True
    token_budget: Annotated[int, Field(strict=True, ge=0)] = 0


class PortEndpoint(ContractModel):
    # None refers to the team's external input (source) or output (target).
    task_key: Identifier | None = None
    port_id: Identifier


class Connection(ContractModel):
    source: PortEndpoint
    target: PortEndpoint


class TeamSpec(ContractModel):
    schema_version: Literal[2] = 2
    spec_id: Identifier
    version: PositiveInt
    extends_ref: SpecRef | None = None
    inputs: tuple[PortSpec, ...] = ()
    outputs: Annotated[tuple[PortSpec, ...], Field(min_length=1)]
    roles: Annotated[tuple[RoleSpec, ...], Field(min_length=1)]
    tasks: Annotated[tuple[TaskTemplate, ...], Field(min_length=1)]
    connections: tuple[Connection, ...] = ()
    policy: PolicyLimits

    @property
    def ref(self) -> SpecRef:
        return SpecRef(spec_id=self.spec_id, version=self.version)


class RuntimeBinding(ContractModel):
    binding_id: Identifier
    team_id: Text
    team_run_id: Text
    role_id: Identifier
    principal_ref: Text
    kind: Literal["model", "host", "human", "tool"]
    provider_id: Text | None = None
    model_id: Text | None = None
    effort: Text | None = None
    approval_route: Text | None = None
    capabilities: tuple[Text, ...] = ()
    policy_version: Text
    worker_session_id: Text | None = None

    @model_validator(mode="after")
    def validate_model_route(self) -> RuntimeBinding:
        if self.model_id == "codex-auto-review":
            raise ValueError("approval route cannot be a provider model ID")
        if self.kind == "model" and (not self.provider_id or not self.model_id):
            raise ValueError("model binding requires provider_id and model_id")
        if self.kind in {"human", "tool"} and (self.provider_id or self.model_id or self.effort):
            raise ValueError("human/tool bindings cannot contain model routing")
        if bool(self.provider_id) != bool(self.model_id):
            raise ValueError("provider_id and model_id must be specified together")
        return self


class TeamRun(ContractModel):
    team_run_id: Text
    team_id: Text
    project_id: Text
    resolved_spec_hash: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    objective: Text
    task_set_revision: PositiveInt = 1
    control_epoch: PositiveInt = 1
    state_version: PositiveInt = 1
    state: Literal["draft", "ready", "running", "waiting", "verifying",
                   "completed", "failed", "cancelled"] = "draft"
    budget_ref: Text


class CompiledTeam(ContractModel):
    """Sealed, serializable planning result; NOT permission to execute."""

    compiler_version: Literal[1] = 1
    spec: TeamSpec
    definitions: tuple[TeamSpec, ...]
    lineage: tuple[SpecRef, ...]
    effective_policy: PolicyLimits
    task_order: tuple[Identifier, ...]
    required_task_keys: tuple[Identifier, ...]
    resolved_spec_hash: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
