"""Pure validation and deterministic compilation of explicit team DAGs.

Inheritance is a complete substitutable child definition, not a permissive
deep-merge. Schemas and validator references must be supplied by the host's
trusted catalog. No code, models, stores, or files are loaded by this module.
"""
from __future__ import annotations

import hashlib
import heapq
import json
from dataclasses import dataclass
from typing import Iterable

from .spec import CompiledTeam, PolicyLimits, PortEndpoint, PortSpec, RuntimeBinding, TeamSpec


class SpecCompilationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _fail(code: str, message: str) -> None:
    raise SpecCompilationError(code, message)


@dataclass(frozen=True)
class CompilationContext:
    user_grant: PolicyLimits
    project_policy: PolicyLimits
    registered_schemas: frozenset[str]
    registered_validators: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for field in ("user_grant", "project_policy"):
            policy = getattr(self, field)
            object.__setattr__(self, field, PolicyLimits.model_validate(policy.model_dump()))
        object.__setattr__(self, "registered_schemas", frozenset(self.registered_schemas))
        object.__setattr__(self, "registered_validators", frozenset(self.registered_validators))


def _index(values: Iterable, field: str, location: str) -> dict:
    result = {}
    for value in values:
        key = getattr(value, field)
        if key in result:
            _fail("DUPLICATE_KEY", f"{location}: {key}")
        result[key] = value
    return result


def _unique(values: tuple, location: str) -> None:
    if len(values) != len(set(values)):
        _fail("DUPLICATE_KEY", location)


def _policy_intersection(*policies: PolicyLimits) -> PolicyLimits:
    def common(field: str) -> tuple:
        values = set(getattr(policies[0], field))
        for policy in policies[1:]:
            values.intersection_update(getattr(policy, field))
        return tuple(sorted(values))

    return PolicyLimits(
        allowed_actions=common("allowed_actions"),
        allowed_resources=common("allowed_resources"),
        allowed_models=common("allowed_models"),
        max_tasks=min(p.max_tasks for p in policies),
        max_inheritance_depth=min(p.max_inheritance_depth for p in policies),
        token_budget=min(p.token_budget for p in policies),
        self_acceptance_forbidden=any(p.self_acceptance_forbidden for p in policies),
    )


def _check_policy_refinement(parent: PolicyLimits, child: PolicyLimits) -> None:
    for field in ("allowed_actions", "allowed_resources", "allowed_models"):
        if not set(getattr(child, field)) <= set(getattr(parent, field)):
            _fail("POLICY_ESCALATION", field)
    for field in ("max_tasks", "max_inheritance_depth", "token_budget"):
        if getattr(child, field) > getattr(parent, field):
            _fail("POLICY_ESCALATION", field)
    if parent.self_acceptance_forbidden and not child.self_acceptance_forbidden:
        _fail("POLICY_ESCALATION", "self acceptance")


def _check_ports(parent: tuple[PortSpec, ...], child: tuple[PortSpec, ...], *,
                 inputs: bool, location: str) -> None:
    previous = _index(parent, "port_id", location)
    current = _index(child, "port_id", location)
    # Conservatively preserve even optional inherited ports. No inferred schema
    # subtyping: conversions must be explicit tasks with registered schemas.
    for key, before in previous.items():
        after = current.get(key)
        if after is None or after.schema_ref != before.schema_ref:
            _fail("INCOMPATIBLE_INHERITANCE", f"{location}.{key}: removed or changed schema")
        if inputs and not before.required and after.required:
            _fail("INCOMPATIBLE_INHERITANCE", f"{location}.{key}: input became required")
        if not inputs and before.required and not after.required:
            _fail("INCOMPATIBLE_INHERITANCE", f"{location}.{key}: required output weakened")
    if inputs and any(p.required for key, p in current.items() if key not in previous):
        _fail("INCOMPATIBLE_INHERITANCE", f"{location}: new required input")


def _check_inheritance(parent: TeamSpec, child: TeamSpec) -> None:
    _check_policy_refinement(parent.policy, child.policy)
    _check_ports(parent.inputs, child.inputs, inputs=True, location="team.inputs")
    _check_ports(parent.outputs, child.outputs, inputs=False, location="team.outputs")
    roles = _index(child.roles, "role_id", "roles")
    for before in parent.roles:
        after = roles.get(before.role_id)
        if after is None:
            _fail("INCOMPATIBLE_INHERITANCE", f"missing role {before.role_id}")
        if (not set(after.allowed_actions) <= set(before.allowed_actions)
                or not set(before.independent_from) <= set(after.independent_from)
                or not set(before.required_capabilities) <= set(after.required_capabilities)
                or after.max_members > before.max_members):
            _fail("POLICY_ESCALATION", f"role {before.role_id}")
    tasks = _index(child.tasks, "task_key", "tasks")
    for before in parent.tasks:
        after = tasks.get(before.task_key)
        if after is None:
            _fail("INCOMPATIBLE_INHERITANCE", f"missing task {before.task_key}")
        if (after.role_id != before.role_id or after.reviewer_role != before.reviewer_role
                or (before.required and not after.required)
                or after.verification != before.verification
                or after.token_budget > before.token_budget
                or not set(after.input_scope) <= set(before.input_scope)
                or not set(before.forbidden_changes) <= set(after.forbidden_changes)
                or not set(after.required_actions) <= set(before.required_actions)):
            _fail("INCOMPATIBLE_INHERITANCE", f"task {before.task_key} changes its contract boundary")
        _check_ports(before.inputs, after.inputs, inputs=True, location=before.task_key + ".inputs")
        _check_ports(before.outputs, after.outputs, inputs=False, location=before.task_key + ".outputs")
        criteria = _index(after.acceptance, "criterion_id", "acceptance")
        for criterion in before.acceptance:
            replacement = criteria.get(criterion.criterion_id)
            if (replacement is None or replacement.description != criterion.description
                    or replacement.minimum_score < criterion.minimum_score):
                _fail("ACCEPTANCE_WEAKENED", f"{before.task_key}.{criterion.criterion_id}")
    # Rewiring an inherited target could silently replace trusted evidence.
    inherited_edges = {(e.source, e.target) for e in parent.connections}
    if not inherited_edges <= {(e.source, e.target) for e in child.connections}:
        _fail("INCOMPATIBLE_INHERITANCE", "inherited connections changed")


def _validate(spec: TeamSpec, context: CompilationContext, policy: PolicyLimits) -> tuple[str, ...]:
    tasks = _index(spec.tasks, "task_key", "tasks")
    roles = _index(spec.roles, "role_id", "roles")
    if len(tasks) > policy.max_tasks:
        _fail("TASK_LIMIT", str(len(tasks)))
    if sum(t.token_budget for t in tasks.values()) > policy.token_budget:
        _fail("BUDGET_EXCEEDED", "task reservations exceed effective team budget")
    for field in ("allowed_actions", "allowed_resources", "allowed_models"):
        _unique(getattr(spec.policy, field), "policy." + field)
    for role in roles.values():
        _unique(role.allowed_actions, role.role_id + ".allowed_actions")
        _unique(role.independent_from, role.role_id + ".independent_from")
        _unique(role.required_capabilities, role.role_id + ".required_capabilities")
        if not set(role.allowed_actions) <= set(policy.allowed_actions):
            _fail("PERMISSION_REQUIRED", f"role {role.role_id} actions")
        if role.role_id in role.independent_from or not set(role.independent_from) <= roles.keys():
            _fail("INVALID_ROLE", f"{role.role_id} independence")

    team_inputs = _index(spec.inputs, "port_id", "team.inputs")
    team_outputs = _index(spec.outputs, "port_id", "team.outputs")
    inputs = {key: _index(t.inputs, "port_id", key + ".inputs") for key, t in tasks.items()}
    outputs = {key: _index(t.outputs, "port_id", key + ".outputs") for key, t in tasks.items()}
    all_ports = list(spec.inputs) + list(spec.outputs)
    for task in tasks.values():
        all_ports.extend(task.inputs + task.outputs)
        _index(task.acceptance, "criterion_id", task.task_key + ".acceptance")
        _unique(task.input_scope, task.task_key + ".input_scope")
        _unique(task.required_actions, task.task_key + ".required_actions")
        if task.role_id not in roles or task.reviewer_role not in roles:
            _fail("INVALID_ROLE", task.task_key)
        if "verify" not in roles[task.reviewer_role].allowed_actions:
            _fail("INVALID_ROLE", f"{task.reviewer_role} cannot verify")
        if policy.self_acceptance_forbidden and task.role_id == task.reviewer_role:
            _fail("SELF_ACCEPTANCE", task.task_key)
        if not set(task.required_actions) <= set(roles[task.role_id].allowed_actions):
            _fail("PERMISSION_REQUIRED", task.task_key + " actions")
        if not set(task.input_scope) <= set(policy.allowed_resources):
            _fail("PERMISSION_REQUIRED", task.task_key + " resource grants")
        if (task.verification.kind == "registered_validator"
                and task.verification.ref not in context.registered_validators):
            _fail("UNKNOWN_VALIDATOR", task.verification.ref)
        if (task.verification.kind == "human_review"
                and task.verification.ref != task.reviewer_role):
            _fail("INVALID_ROLE", "human review must reference the declared reviewer")
    for port in all_ports:
        if port.schema_ref not in context.registered_schemas:
            _fail("UNKNOWN_SCHEMA", port.schema_ref)

    connected = set()
    dependencies = {key: set() for key in tasks}
    successors = {key: set() for key in tasks}
    for edge in spec.connections:
        if edge.target in connected:
            _fail("MULTIPLE_INPUT_SOURCES", str(edge.target))
        connected.add(edge.target)
        if edge.source.task_key is not None and edge.source.task_key not in tasks:
            _fail("UNKNOWN_TASK", str(edge.source.task_key))
        if edge.target.task_key is not None and edge.target.task_key not in tasks:
            _fail("UNKNOWN_TASK", str(edge.target.task_key))
        source_ports = team_inputs if edge.source.task_key is None else outputs[edge.source.task_key]
        target_ports = team_outputs if edge.target.task_key is None else inputs[edge.target.task_key]
        source = source_ports.get(edge.source.port_id)
        target = target_ports.get(edge.target.port_id)
        if source is None or target is None:
            _fail("UNKNOWN_PORT", str(edge))
        if source.schema_ref != target.schema_ref:
            _fail("SCHEMA_MISMATCH", str(edge))
        if target.required and (not source.required or (
                edge.source.task_key is not None and not tasks[edge.source.task_key].required)):
            _fail("OPTIONAL_REQUIRED_MISMATCH", str(edge))
        if edge.source.task_key is not None and edge.target.task_key is not None:
            dependencies[edge.target.task_key].add(edge.source.task_key)
            successors[edge.source.task_key].add(edge.target.task_key)

    for key, ports in [(None, team_outputs), *inputs.items()]:
        for port in ports.values():
            if port.required and PortEndpoint(task_key=key, port_id=port.port_id) not in connected:
                _fail("UNBOUND_INPUT", f"{key or 'team.output'}.{port.port_id}")
    ready = [key for key in tasks if not dependencies[key]]
    heapq.heapify(ready)
    order = []
    while ready:
        key = heapq.heappop(ready)
        order.append(key)
        for successor in sorted(successors[key]):
            dependencies[successor].remove(key)
            if not dependencies[successor]:
                heapq.heappush(ready, successor)
    if len(order) != len(tasks):
        _fail("DEPENDENCY_CYCLE", "task graph must be a DAG")
    return tuple(order)


def compile_team_spec(spec: TeamSpec | dict, *, catalog: Iterable[TeamSpec] = (),
                      context: CompilationContext) -> CompiledTeam:
    """Validate a complete definition and seal its exact resolved snapshot."""
    # Revalidate even model instances: callers may have used model_copy/update.
    spec = TeamSpec.model_validate(spec.model_dump() if isinstance(spec, TeamSpec) else spec)
    definitions = {}
    for entry in (*tuple(catalog), spec):
        entry = TeamSpec.model_validate(entry.model_dump())
        if entry.ref in definitions and definitions[entry.ref] != entry:
            _fail("SPEC_VERSION_CONFLICT", str(entry.ref))
        definitions[entry.ref] = entry
    lineage = []
    seen = set()
    current = spec
    limit = min(context.user_grant.max_inheritance_depth,
                context.project_policy.max_inheritance_depth, spec.policy.max_inheritance_depth)
    while True:
        if current.ref in seen:
            _fail("INHERITANCE_CYCLE", str(current.ref))
        seen.add(current.ref)
        lineage.append(current)
        if len(lineage) > limit:
            _fail("INHERITANCE_LIMIT", str(limit))
        if current.extends_ref is None:
            break
        parent = definitions.get(current.extends_ref)
        if parent is None:
            _fail("UNKNOWN_BASE_SPEC", str(current.extends_ref))
        current = parent
    lineage.reverse()
    for index, entry in enumerate(lineage):
        # Validate ancestor contracts too; a child cannot hide a broken base.
        # Ancestors are templates, not requested executions. A narrower child
        # can legitimately fit a grant that would not admit its wider parent.
        base_policy = _policy_intersection(*[item.policy for item in lineage[:index + 1]])
        _validate(entry, context, base_policy)
        if index:
            _check_inheritance(lineage[index - 1], entry)
    effective = _policy_intersection(context.user_grant, context.project_policy,
                                     *[item.policy for item in lineage])
    order = _validate(spec, context, effective)
    payload = {
        "compiler_version": 1,
        "definitions": [entry.model_dump(mode="json") for entry in lineage],
        "effective_policy": effective.model_dump(mode="json"),
        "task_order": order,
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), allow_nan=False)
    digest = "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    required = {task.task_key for task in spec.tasks if task.required}
    return CompiledTeam(spec=spec, definitions=tuple(lineage),
                        lineage=tuple(entry.ref for entry in lineage),
                        effective_policy=effective, task_order=order,
                        required_task_keys=tuple(key for key in order if key in required),
                        resolved_spec_hash=digest)


def validate_runtime_bindings(compiled: CompiledTeam, bindings: Iterable[RuntimeBinding], *,
                              team_id: str, team_run_id: str) -> tuple[RuntimeBinding, ...]:
    """Check binding feasibility only; runtime must recheck live authority."""
    compiled = validate_compiled_snapshot(compiled)
    bindings = tuple(RuntimeBinding.model_validate(binding.model_dump()) for binding in bindings)
    _index(bindings, "binding_id", "runtime bindings")
    roles = {role.role_id: role for role in compiled.spec.roles}
    assigned = {role_id: [] for role_id in roles}
    for binding in bindings:
        if binding.team_id != team_id or binding.team_run_id != team_run_id:
            _fail("TEAM_SCOPE_MISMATCH", binding.binding_id)
        role = roles.get(binding.role_id)
        if role is None:
            _fail("INVALID_ROLE", binding.role_id)
        if not set(role.required_capabilities) <= set(binding.capabilities):
            _fail("CAPABILITY_MISMATCH", binding.binding_id)
        if binding.model_id and f"{binding.provider_id}/{binding.model_id}" not in compiled.effective_policy.allowed_models:
            _fail("MODEL_NOT_ALLOWED", binding.model_id)
        assigned[binding.role_id].append(binding)
    for role_id, values in assigned.items():
        if len(values) > roles[role_id].max_members:
            _fail("MEMBER_LIMIT", role_id)
        if len({value.principal_ref for value in values}) != len(values):
            _fail("DUPLICATE_MEMBER", role_id)
    needed = {key for task in compiled.spec.tasks for key in (task.role_id, task.reviewer_role)}
    if any(not assigned[key] for key in needed):
        _fail("ROLE_UNBOUND", ",".join(sorted(key for key in needed if not assigned[key])))
    pairs = {(role.role_id, other) for role in roles.values() for other in role.independent_from}
    if compiled.effective_policy.self_acceptance_forbidden:
        pairs.update((task.role_id, task.reviewer_role) for task in compiled.spec.tasks)
    for first, second in pairs:
        if {b.principal_ref for b in assigned[first]} & {b.principal_ref for b in assigned[second]}:
            _fail("SELF_ACCEPTANCE", f"{first} / {second}")
    for task in compiled.spec.tasks:
        if task.verification.kind == "human_review" and any(
                binding.kind != "human" for binding in assigned[task.reviewer_role]):
            _fail("BINDING_KIND_MISMATCH", task.reviewer_role)
    return bindings


def validate_compiled_snapshot(value: CompiledTeam | dict) -> CompiledTeam:
    """Verify serialization integrity, not the current host's authorization."""
    compiled = CompiledTeam.model_validate(value.model_dump() if isinstance(value, CompiledTeam) else value)
    if (not compiled.definitions or compiled.definitions[-1] != compiled.spec
            or tuple(entry.ref for entry in compiled.definitions) != compiled.lineage):
        _fail("SNAPSHOT_CORRUPT", "definition lineage mismatch")
    payload = {
        "compiler_version": compiled.compiler_version,
        "definitions": [entry.model_dump(mode="json") for entry in compiled.definitions],
        "effective_policy": compiled.effective_policy.model_dump(mode="json"),
        "task_order": compiled.task_order,
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), allow_nan=False)
    digest = "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    tasks = {task.task_key: task for task in compiled.spec.tasks}
    if (digest != compiled.resolved_spec_hash or len(compiled.task_order) != len(tasks)
            or set(compiled.task_order) != tasks.keys()
            or compiled.required_task_keys != tuple(
                key for key in compiled.task_order if tasks[key].required)):
        _fail("SNAPSHOT_CORRUPT", "compiled hash or task manifest mismatch")
    return compiled
