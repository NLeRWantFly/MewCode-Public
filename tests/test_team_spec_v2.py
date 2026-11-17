"""Pure Agent Team v2 compilation: no providers, processes, or runtime stores."""
from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from mewcode.teams.spec import CompiledTeam, PolicyLimits, RuntimeBinding, TeamRun, TeamSpec
from mewcode.teams.spec_compiler import (
    CompilationContext, SpecCompilationError, compile_team_spec, validate_runtime_bindings,
    validate_compiled_snapshot,
)


def team_definition(domain="software"):
    """Same topology and role contracts; only domain data and validation differ."""
    return {
        "spec_id": domain + "-delivery", "version": 1,
        "inputs": [{"port_id": "request", "schema_ref": "request/v1"}],
        "outputs": [{"port_id": "delivery", "schema_ref": domain + "-artifact/v1"}],
        "roles": [
            {"role_id": "producer", "responsibility": "Produce a scoped candidate",
             "allowed_actions": ["read", "write_candidate"],
             "required_capabilities": ["artifact.read"]},
            {"role_id": "reviewer", "responsibility": "Review exact candidate evidence",
             "allowed_actions": ["read", "verify"], "independent_from": ["producer"]},
        ],
        "tasks": [{
            "task_key": "produce", "role_id": "producer", "reviewer_role": "reviewer",
            "objective": f"Produce a {domain} deliverable", "input_scope": ["source"],
            "forbidden_changes": ["Do not modify authoritative sources"],
            "inputs": [{"port_id": "request", "schema_ref": "request/v1"}],
            "outputs": [{"port_id": "result", "schema_ref": domain + "-artifact/v1"}],
            "required_actions": ["read", "write_candidate"],
            "verification": {"kind": "registered_validator", "ref": domain + "-check/v1"},
            "acceptance": [{"criterion_id": "valid", "description": "All required checks pass"}],
            "token_budget": 100,
        }],
        "connections": [
            {"source": {"port_id": "request"},
             "target": {"task_key": "produce", "port_id": "request"}},
            {"source": {"task_key": "produce", "port_id": "result"},
             "target": {"port_id": "delivery"}},
        ],
        "policy": {
            "allowed_actions": ["read", "write_candidate", "verify"],
            "allowed_resources": ["source"],
            "allowed_models": ["external/executor", "host/reviewer"],
            "max_tasks": 16, "max_inheritance_depth": 8, "token_budget": 1000,
        },
    }


def compile_context():
    policy = PolicyLimits.model_validate(team_definition()["policy"])
    return CompilationContext(
        user_grant=policy, project_policy=policy,
        registered_schemas=frozenset({"request/v1", "software-artifact/v1", "data-artifact/v1"}),
        registered_validators=frozenset({"software-check/v1", "data-check/v1"}),
    )


def compile_definition(definition=None, **kwargs):
    return compile_team_spec(definition or team_definition(), context=kwargs.pop("context", compile_context()),
                             **kwargs)


def assert_error(code, definition, **kwargs):
    with pytest.raises(SpecCompilationError) as error:
        compile_definition(definition, **kwargs)
    assert error.value.code == code


def binding(role="producer", **overrides):
    values = dict(binding_id=role, team_id="team-a", team_run_id="run-a", role_id=role,
                  principal_ref=role, kind="model", provider_id="external", model_id="executor",
                  capabilities=("artifact.read",), policy_version="policy-1")
    values.update(overrides)
    return RuntimeBinding(**values)


def test_software_and_data_use_identical_core():
    software = compile_definition()
    data = compile_definition(team_definition("data"))
    assert software.task_order == data.task_order == ("produce",)
    assert software.required_task_keys == data.required_task_keys
    assert software.spec.roles == data.spec.roles
    assert software.resolved_spec_hash != data.resolved_spec_hash
    assert "worktree" not in TeamSpec.model_fields
    assert "model_id" not in TeamSpec.model_fields
    assert "paper" not in TeamSpec.model_fields


def test_deterministic_frozen_roundtrip_and_caller_isolation():
    raw = team_definition()
    compiled = compile_definition(raw)
    assert compile_definition(deepcopy(raw)) == compiled
    raw["tasks"][0]["acceptance"][0]["minimum_score"] = 0
    assert compiled.spec.tasks[0].acceptance[0].minimum_score == 10000
    assert CompiledTeam.model_validate_json(compiled.model_dump_json()) == compiled
    with pytest.raises(ValidationError):
        compiled.spec.tasks[0].objective = "changed"
    with pytest.raises(ValidationError):
        compiled.spec.tasks[0].acceptance[0].minimum_score = 0


@pytest.mark.parametrize("field,value", [("version", True), ("version", "latest"),
                                           ("spec_id", "../escape"), ("unknown", "value")])
def test_invalid_contract_fields_rejected(field, value):
    raw = team_definition()
    raw[field] = value
    with pytest.raises(ValidationError):
        compile_definition(raw)


@pytest.mark.parametrize("location,field", [("roles", "role_id"), ("tasks", "task_key"),
                                             ("inputs", "port_id"), ("outputs", "port_id")])
def test_duplicate_ids_rejected(location, field):
    raw = team_definition()
    raw[location].append(deepcopy(raw[location][0]))
    assert_error("DUPLICATE_KEY", raw)


@pytest.mark.parametrize("mutation,code", [
    (lambda d: d["tasks"][0].update(role_id="missing"), "INVALID_ROLE"),
    (lambda d: d["tasks"][0].update(reviewer_role="producer"), "INVALID_ROLE"),
    (lambda d: d["tasks"][0].update(input_scope=["not-granted"]), "PERMISSION_REQUIRED"),
    (lambda d: d["tasks"][0].update(required_actions=["publish"]), "PERMISSION_REQUIRED"),
    (lambda d: d["tasks"][0]["verification"].update(ref="unknown/v1"), "UNKNOWN_VALIDATOR"),
    (lambda d: d["outputs"][0].update(schema_ref="unknown/v1"), "UNKNOWN_SCHEMA"),
    (lambda d: d["tasks"][0].update(token_budget=1001), "BUDGET_EXCEEDED"),
    (lambda d: d["connections"].pop(0), "UNBOUND_INPUT"),
    (lambda d: d["connections"].pop(), "UNBOUND_INPUT"),
    (lambda d: d["connections"][0]["source"].update(port_id="missing"), "UNKNOWN_PORT"),
    (lambda d: d["connections"][0]["source"].update(task_key="missing"), "UNKNOWN_TASK"),
    (lambda d: d["outputs"][0].update(schema_ref="request/v1"), "SCHEMA_MISMATCH"),
    (lambda d: d["inputs"][0].update(required=False), "OPTIONAL_REQUIRED_MISMATCH"),
    (lambda d: d["tasks"][0].update(required=False), "OPTIONAL_REQUIRED_MISMATCH"),
    (lambda d: d["connections"].append(deepcopy(d["connections"][0])), "MULTIPLE_INPUT_SOURCES"),
])
def test_preflight_fail_closed(mutation, code):
    raw = team_definition()
    mutation(raw)
    assert_error(code, raw)


def test_self_acceptance_forbidden_at_role_and_principal_levels():
    raw = team_definition()
    raw["roles"][0]["allowed_actions"].append("verify")
    raw["tasks"][0]["reviewer_role"] = "producer"
    assert_error("SELF_ACCEPTANCE", raw)
    with pytest.raises(SpecCompilationError, match="SELF_ACCEPTANCE"):
        validate_runtime_bindings(compile_definition(),
                                  [binding(), binding("reviewer", principal_ref="producer")],
                                  team_id="team-a", team_run_id="run-a")


def test_policy_intersection_does_not_expand_scope_or_budget():
    context = compile_context()
    grant = context.user_grant.model_copy(update={"token_budget": 150,
                                                 "allowed_models": ("external/executor",)})
    result = compile_definition(context=CompilationContext(
        user_grant=grant, project_policy=context.project_policy,
        registered_schemas=context.registered_schemas, registered_validators=context.registered_validators))
    assert result.effective_policy.token_budget == 150
    assert result.effective_policy.allowed_models == ("external/executor",)


def test_empty_grants_deny_instead_of_wildcard():
    context = compile_context()
    grant = context.user_grant.model_copy(update={"allowed_resources": ()})
    assert_error("PERMISSION_REQUIRED", team_definition(), context=CompilationContext(
        user_grant=grant, project_policy=context.project_policy,
        registered_schemas=context.registered_schemas, registered_validators=context.registered_validators))


def inherited_definition():
    parent = team_definition()
    child = deepcopy(parent)
    child.update(spec_id="specialized", extends_ref={"spec_id": parent["spec_id"], "version": 1})
    child["tasks"][0]["forbidden_changes"].append("Do not publish externally")
    child["policy"]["token_budget"] = 500
    return TeamSpec.model_validate(parent), child


def test_pinned_inheritance_and_lineage():
    parent, child = inherited_definition()
    compiled = compile_definition(child, catalog=[parent])
    assert tuple(ref.spec_id for ref in compiled.lineage) == ("software-delivery", "specialized")
    assert compiled.effective_policy.token_budget == 500
    assert compiled.spec.tasks[0].forbidden_changes[-1] == "Do not publish externally"
    assert compiled.definitions[0] == parent


def test_narrow_child_can_fit_a_smaller_grant_than_parent():
    parent, child = inherited_definition()
    child["tasks"][0]["token_budget"] = 50
    child["policy"]["token_budget"] = 50
    context = compile_context()
    context = CompilationContext(
        user_grant=context.user_grant.model_copy(update={"token_budget": 50}),
        project_policy=context.project_policy, registered_schemas=context.registered_schemas,
        registered_validators=context.registered_validators)
    compiled = compile_definition(child, catalog=[parent], context=context)
    assert compiled.effective_policy.token_budget == 50


@pytest.mark.parametrize("mutation,code", [
    (lambda d: d["policy"]["allowed_actions"].append("publish"), "POLICY_ESCALATION"),
    (lambda d: d["policy"]["allowed_resources"].append("secret"), "POLICY_ESCALATION"),
    (lambda d: d["policy"]["allowed_models"].append("other/model"), "POLICY_ESCALATION"),
    (lambda d: d["policy"].update(token_budget=1001), "POLICY_ESCALATION"),
    (lambda d: d["policy"].update(self_acceptance_forbidden=False), "POLICY_ESCALATION"),
    (lambda d: d["roles"][1].update(independent_from=[]), "POLICY_ESCALATION"),
    (lambda d: d["tasks"][0]["acceptance"][0].update(minimum_score=0), "ACCEPTANCE_WEAKENED"),
    (lambda d: d["tasks"][0]["acceptance"][0].update(description="Easier rule"), "ACCEPTANCE_WEAKENED"),
    (lambda d: d["tasks"][0]["verification"].update(ref="data-check/v1"), "INCOMPATIBLE_INHERITANCE"),
    (lambda d: d["tasks"][0].update(forbidden_changes=["Different boundary"]), "INCOMPATIBLE_INHERITANCE"),
    (lambda d: d["inputs"].append({"port_id": "extra", "schema_ref": "request/v1"}), "INCOMPATIBLE_INHERITANCE"),
])
def test_inheritance_cannot_weaken_contract(mutation, code):
    parent, child = inherited_definition()
    mutation(child)
    assert_error(code, child, catalog=[parent])


def test_unknown_base_and_conflicting_version_rejected():
    parent, child = inherited_definition()
    assert_error("UNKNOWN_BASE_SPEC", child)
    conflicting = parent.model_dump()
    conflicting["tasks"][0]["objective"] = "A different definition under same ID"
    assert_error("SPEC_VERSION_CONFLICT", child,
                 catalog=[parent, TeamSpec.model_validate(conflicting)])


def test_inheritance_cycle_and_depth_limit():
    parent, child = inherited_definition()
    parent_data = parent.model_dump()
    parent_data["extends_ref"] = {"spec_id": "specialized", "version": 1}
    assert_error("INHERITANCE_CYCLE", child, catalog=[TeamSpec.model_validate(parent_data)])
    child["policy"]["max_inheritance_depth"] = 1
    assert_error("INHERITANCE_LIMIT", child, catalog=[parent])


def test_ancestor_content_is_part_of_snapshot_hash():
    parent, child = inherited_definition()
    before = compile_definition(child, catalog=[parent])
    data = parent.model_dump()
    data["roles"][0]["responsibility"] = "Clarified responsibility"
    after = compile_definition(child, catalog=[TeamSpec.model_validate(data)])
    assert before.resolved_spec_hash != after.resolved_spec_hash


def graph_definition(cycle=False):
    raw = team_definition()
    first = raw["tasks"][0]
    first["inputs"][0]["schema_ref"] = "software-artifact/v1"
    second = deepcopy(first)
    second["task_key"] = "second"
    raw["tasks"].append(second)
    raw["inputs"][0]["schema_ref"] = "software-artifact/v1"
    raw["connections"] = [
        {"source": {"task_key": "produce", "port_id": "result"},
         "target": {"task_key": "second", "port_id": "request"}},
        {"source": {"task_key": "second", "port_id": "result"},
         "target": {"port_id": "delivery"}},
        {"source": ({"task_key": "second", "port_id": "result"} if cycle else {"port_id": "request"}),
         "target": {"task_key": "produce", "port_id": "request"}},
    ]
    return raw


def test_dependency_cycle_and_task_limit():
    assert_error("DEPENDENCY_CYCLE", graph_definition(cycle=True))
    raw = graph_definition()
    raw["policy"]["max_tasks"] = 1
    assert_error("TASK_LIMIT", raw)


def test_explicit_dag_order_is_independent_of_declaration_order():
    raw = graph_definition()
    raw["tasks"].reverse()
    assert compile_definition(raw).task_order == ("produce", "second")


def test_fanout_and_join_bind_every_required_port():
    raw = graph_definition()
    branch = deepcopy(raw["tasks"][0])
    branch["task_key"] = "branch"
    raw["tasks"].append(branch)
    raw["tasks"][1]["inputs"].append({"port_id": "other", "schema_ref": "software-artifact/v1"})
    raw["connections"].extend([
        {"source": {"port_id": "request"}, "target": {"task_key": "branch", "port_id": "request"}},
        {"source": {"task_key": "branch", "port_id": "result"},
         "target": {"task_key": "second", "port_id": "other"}},
    ])
    assert compile_definition(raw).task_order == ("branch", "produce", "second")
    raw["connections"].pop()
    assert_error("UNBOUND_INPUT", raw)


def test_binding_model_policy_and_separate_principals():
    compiled = compile_definition()
    values = [binding(), binding("reviewer", kind="host", provider_id="host", model_id="reviewer")]
    assert validate_runtime_bindings(compiled, values, team_id="team-a", team_run_id="run-a") == tuple(values)


@pytest.mark.parametrize("replacement,code", [
    ({"model_id": "unapproved"}, "MODEL_NOT_ALLOWED"),
    ({"capabilities": ()}, "CAPABILITY_MISMATCH"),
    ({"team_id": "team-b"}, "TEAM_SCOPE_MISMATCH"),
    ({"team_run_id": "run-b"}, "TEAM_SCOPE_MISMATCH"),
    ({"role_id": "missing"}, "INVALID_ROLE"),
])
def test_invalid_runtime_bindings(replacement, code):
    with pytest.raises(SpecCompilationError) as error:
        validate_runtime_bindings(compile_definition(), [binding(**replacement), binding("reviewer")],
                                  team_id="team-a", team_run_id="run-a")
    assert error.value.code == code


def test_missing_role_and_duplicate_member_bindings():
    with pytest.raises(SpecCompilationError, match="ROLE_UNBOUND"):
        validate_runtime_bindings(compile_definition(), [binding()], team_id="team-a", team_run_id="run-a")
    with pytest.raises(SpecCompilationError, match="DUPLICATE_KEY"):
        validate_runtime_bindings(compile_definition(), [binding(), binding()], team_id="team-a", team_run_id="run-a")


def test_approval_route_is_never_a_model_id():
    with pytest.raises(ValidationError, match="approval route"):
        binding(model_id="codex-auto-review")
    value = binding("reviewer", kind="host", provider_id=None, model_id=None,
                    approval_route="codex-auto-review")
    assert value.approval_route == "codex-auto-review" and value.model_id is None


def test_human_review_and_readonly_without_model_or_git():
    raw = team_definition("data")
    raw["roles"][0]["allowed_actions"] = ["read"]
    raw["tasks"][0]["required_actions"] = ["read"]
    raw["tasks"][0]["verification"] = {"kind": "human_review", "ref": "reviewer"}
    compiled = compile_definition(raw)
    human = binding("reviewer", kind="human", provider_id=None, model_id=None)
    validate_runtime_bindings(compiled, [binding(), human], team_id="team-a", team_run_id="run-a")
    with pytest.raises(SpecCompilationError, match="BINDING_KIND_MISMATCH"):
        validate_runtime_bindings(compiled, [binding(), binding("reviewer")],
                                  team_id="team-a", team_run_id="run-a")


def test_team_run_has_separate_identity_and_no_side_effects(monkeypatch):
    import subprocess
    def forbidden(*args, **kwargs):
        pytest.fail("compilation attempted to spawn execution")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    compiled = compile_definition()
    run = TeamRun(team_run_id="run-a", team_id="team-a", project_id="project-a",
                  resolved_spec_hash=compiled.resolved_spec_hash, objective="A defined outcome",
                  budget_ref="approved-budget")
    assert run.state == "draft"
    assert run.team_id != run.team_run_id
    assert run.task_set_revision == run.control_epoch == 1


def test_unsafe_model_copy_is_revalidated():
    unsafe = TeamSpec.model_validate(team_definition()).model_copy(update={"version": -1})
    with pytest.raises(ValidationError):
        compile_definition(unsafe)


@pytest.mark.parametrize("field,value", [("required_task_keys", []), ("task_order", []),
                                          ("definitions", []), ("resolved_spec_hash", "sha256:" + "0" * 64)])
def test_compiled_snapshot_corruption_is_detected(field, value):
    raw = compile_definition().model_dump()
    raw[field] = value
    with pytest.raises(SpecCompilationError, match="SNAPSHOT_CORRUPT"):
        validate_compiled_snapshot(raw)


def test_context_freezes_host_catalog_and_revalidates_policy():
    context = compile_context()
    schemas = set(context.registered_schemas)
    frozen = CompilationContext(context.user_grant, context.project_policy, schemas)
    schemas.clear()
    assert frozen.registered_schemas
    with pytest.raises(ValidationError):
        CompilationContext(context.user_grant.model_copy(update={"token_budget": -1}),
                           context.project_policy, frozenset())
