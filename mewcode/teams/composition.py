"""Bounded static composition of compiled teams into one typed task DAG.

Components retain explicit task/role prefixes and source snapshot references.
There is one runtime and one budget ledger, never recursive agent spawning.
"""
from __future__ import annotations

from dataclasses import dataclass

from .spec import CompiledTeam, Connection, PortEndpoint, PortSpec, TeamSpec, SpecRef, PolicyLimits
from .spec_compiler import CompilationContext, SpecCompilationError, compile_team_spec, validate_compiled_snapshot, _policy_intersection


@dataclass(frozen=True)
class ComponentLink:
    source_component: str | None
    source_port: str
    target_component: str | None
    target_port: str


@dataclass(frozen=True)
class CompositionPlan:
    compiled: CompiledTeam
    # (component key, pinned spec ref, compiled hash); kept by callers as
    # provenance. These fields confer no additional authority.
    sources: tuple[tuple[str, SpecRef, str], ...]


def compose_team(*, spec_id: str, version: int, components: dict[str, CompiledTeam],
                 inputs: tuple[PortSpec, ...], outputs: tuple[PortSpec, ...],
                 links: tuple[ComponentLink, ...], policy: PolicyLimits,
                 context: CompilationContext, max_components: int = 16) -> CompositionPlan:
    if not components or len(components) > min(max_components, 64):
        raise SpecCompilationError("COMPOSITION_LIMIT", "invalid component count")
    checked = {key: validate_compiled_snapshot(value) for key, value in components.items()}
    # Conservative common deployment constraints; never widen a child model,
    # resource, action, or self-review policy to make a composition compile.
    effective = _policy_intersection(policy, context.user_grant, context.project_policy,
                                     *(value.effective_policy for value in checked.values()))
    roles, tasks, raw_edges, schemas = [], [], [], {}
    def node(kind, component, port):
        return (kind, component, port)
    for port in inputs:
        schemas[node("root-in", None, port.port_id)] = port
    for port in outputs:
        schemas[node("root-out", None, port.port_id)] = port
    for key, compiled in checked.items():
        if not key or not key.replace("_", "").isalnum() or not key[0].isalpha() or len(key) > 24:
            raise SpecCompilationError("INVALID_COMPONENT", key)
        prefix = key + "__"
        for role in compiled.spec.roles:
            roles.append(role.model_copy(update={"role_id": prefix + role.role_id,
                                                  "independent_from": tuple(prefix + other for other in role.independent_from)}))
        for task in compiled.spec.tasks:
            tasks.append(task.model_copy(update={"task_key": prefix + task.task_key,
                                                  "role_id": prefix + task.role_id, "reviewer_role": prefix + task.reviewer_role,
                                                  "verification": task.verification.model_copy(update={"ref": prefix + task.verification.ref})
                                                  if task.verification.kind == "human_review" else task.verification}))
            for port in task.inputs:
                schemas[node("task-in", prefix + task.task_key, port.port_id)] = port
            for port in task.outputs:
                schemas[node("task-out", prefix + task.task_key, port.port_id)] = port
        for port in compiled.spec.inputs:
            schemas[node("component-in", key, port.port_id)] = port
        for port in compiled.spec.outputs:
            schemas[node("component-out", key, port.port_id)] = port
        for edge in compiled.spec.connections:
            source = node("task-out", prefix + edge.source.task_key, edge.source.port_id) if edge.source.task_key else node("component-in", key, edge.source.port_id)
            target = node("task-in", prefix + edge.target.task_key, edge.target.port_id) if edge.target.task_key else node("component-out", key, edge.target.port_id)
            raw_edges.append((source, target))
    for link in links:
        source = node("component-out", link.source_component, link.source_port) if link.source_component else node("root-in", None, link.source_port)
        target = node("component-in", link.target_component, link.target_port) if link.target_component else node("root-out", None, link.target_port)
        raw_edges.append((source, target))
    incoming = {}
    for source, target in raw_edges:
        if source not in schemas or target not in schemas:
            raise SpecCompilationError("UNKNOWN_PORT", str((source, target)))
        if schemas[source].schema_ref != schemas[target].schema_ref:
            raise SpecCompilationError("SCHEMA_MISMATCH", str((source, target)))
        if schemas[target].required and not schemas[source].required:
            raise SpecCompilationError("OPTIONAL_REQUIRED_MISMATCH", str(target))
        if target in incoming:
            raise SpecCompilationError("MULTIPLE_INPUT_SOURCES", str(target))
        incoming[target] = source
    for key, port in schemas.items():
        if key[0] in {"component-in", "component-out"} and port.required and key not in incoming:
            raise SpecCompilationError("UNBOUND_INPUT", str(key))
    flattened = []
    for target, source in incoming.items():
        if target[0] not in {"task-in", "root-out"}:
            continue
        seen = set()
        while source[0] in {"component-in", "component-out"}:
            if source in seen:
                raise SpecCompilationError("COMPOSITION_CYCLE", str(source))
            seen.add(source)
            if source not in incoming:
                break
            source = incoming[source]
        if source[0] not in {"task-out", "root-in"}:
            if schemas[target].required:
                raise SpecCompilationError("UNBOUND_INPUT", str(target))
            continue
        flattened.append(Connection(source=PortEndpoint(task_key=source[1], port_id=source[2]),
                                    target=PortEndpoint(task_key=target[1], port_id=target[2])))
    definition = TeamSpec(spec_id=spec_id, version=version, inputs=inputs, outputs=outputs,
                          roles=tuple(roles), tasks=tuple(tasks), connections=tuple(flattened), policy=effective)
    compiled = compile_team_spec(definition, context=context)
    return CompositionPlan(compiled, tuple((key, value.spec.ref, value.resolved_spec_hash) for key, value in checked.items()))


def map_team(component: CompiledTeam, item_ids: tuple[str, ...], *, spec_id: str,
             policy: PolicyLimits, context: CompilationContext) -> CompositionPlan:
    if len(item_ids) != len(set(item_ids)):
        raise SpecCompilationError("DUPLICATE_ITEM", "map items must have stable unique identities")
    inputs, outputs, links = [], [], []
    for item in item_ids:
        for port in component.spec.inputs:
            inputs.append(port.model_copy(update={"port_id": item + "__" + port.port_id}))
            links.append(ComponentLink(None, item + "__" + port.port_id, item, port.port_id))
        for port in component.spec.outputs:
            outputs.append(port.model_copy(update={"port_id": item + "__" + port.port_id}))
            links.append(ComponentLink(item, port.port_id, None, item + "__" + port.port_id))
    return compose_team(spec_id=spec_id, version=1, components={key: component for key in item_ids},
                        inputs=tuple(inputs), outputs=tuple(outputs), links=tuple(links), policy=policy, context=context)
