from __future__ import annotations

import pytest

from mewcode.mcp.coord_store import CoordinationStore
from mewcode.teams.service import TeamService
from mewcode.teams.spec import PolicyLimits, RuntimeBinding, SpecRef
from mewcode.teams.spec_compiler import CompilationContext
from mewcode.teams.store_v2 import TeamActor, TeamStateError
from mewcode.teams.runtime_protocol import ArtifactContent, HandoffSubmission, Evidence
from test_team_spec_v2 import graph_definition


def runtime_actor(principal="owner"):
    return TeamActor(principal, "project-a", frozenset(
        {"team.read", "team.execute"} if principal == "grok-executor-01" else
        {"team.read", "team.plan", "team.manage", "team.review", "team.recover"}))


def runtime_fixture(store, domain="software"):
    raw = graph_definition()
    raw["policy"]["allowed_models"] = ["grok-build/grok-4.6", "host/gpt-6-astra"]
    for task in raw["tasks"]:
        task["token_budget"] = 300
    if domain == "data":
        raw["spec_id"] = "data-delivery"
        for port in raw["inputs"] + raw["outputs"]:
            port["schema_ref"] = "data-artifact/v1"
        for task in raw["tasks"]:
            task["objective"] = "Normalize a batch and retain quality evidence"
            task["verification"]["ref"] = "data-check/v1"
            for port in task["inputs"] + task["outputs"]:
                port["schema_ref"] = "data-artifact/v1"
    policy = PolicyLimits.model_validate(raw["policy"])
    context = CompilationContext(policy, policy, frozenset({"software-artifact/v1", "data-artifact/v1"}),
                                 frozenset({"software-check/v1", "data-check/v1"}))
    owner = runtime_actor()
    service = TeamService(store, actor=owner, compilation_context=context)
    service.register_spec(raw, command_id=domain + "-spec")
    team = service.create_team(SpecRef(spec_id=raw["spec_id"], version=1), name=domain, command_id=domain + "-team")
    run = service.plan_run(team["team_id"], objective="Two stages with reviewed handoffs", budget_ref=domain + "-budget",
                           expected_version=1, control_epoch=1, command_id=domain + "-plan")
    runtime = store.teams_v2.runtime
    schema = domain + "-artifact/v1"
    seed = runtime.register_input(owner, run["team_run_id"], ArtifactContent.from_bytes(
        port_id="request", schema_ref=schema, media_type="application/json", content=b'{"source":1}'),
        command_id=domain + "-seed", control_epoch=1)
    bindings = [RuntimeBinding(binding_id="producer", team_id=team["team_id"], team_run_id=run["team_run_id"],
                               role_id="producer", principal_ref="grok-executor-01", kind="model", provider_id="grok-build",
                               model_id="grok-4.6", capabilities=("artifact.read",), policy_version="project-v1"),
                RuntimeBinding(binding_id="reviewer", team_id=team["team_id"], team_run_id=run["team_run_id"],
                               role_id="reviewer", principal_ref="owner", kind="host", provider_id="host",
                               model_id="gpt-6-astra", effort="high", policy_version="project-v1")]
    active = runtime.activate(owner, run["team_run_id"], bindings=bindings, inputs={"request": seed["version_id"]},
                              budget_limit=1000, expected_version=1, control_epoch=1, command_id=domain + "-activate")
    return runtime, owner, runtime_actor("grok-executor-01"), active, schema


@pytest.fixture
def runtime_case(tmp_path):
    with CoordinationStore(tmp_path / "coord.sqlite", enable_team_v2=True) as store:
        yield store, *runtime_fixture(store)


def submit_candidate(runtime, worker, task_id, schema, prefix="one"):
    task = runtime.task(worker, task_id)
    claimed = runtime.claim(worker, task_id, worker_session_id="worker-one", expected_version=task["state_version"], command_id=prefix + "-claim")
    submission = HandoffSubmission(summary="Candidate for independent review", artifacts=(ArtifactContent.from_bytes(
        port_id="result", schema_ref=schema, media_type="application/json", content=b'{"result":2}'),),
        evidence=(Evidence(check_id="valid", status="observed", detail="Inspect the fixed result before accepting"),))
    result = runtime.submit(worker, claimed["token"], submission, usage_tokens=30, command_id=prefix + "-submit")
    return claimed, submission, result


def review_candidate(runtime, owner, result, prefix="one"):
    return runtime.review(owner, result["handoff_id"], accepted=True,
                           scores=[{"criterion_id": "valid", "score": 10000, "evidence": "Verified fixed candidate"}],
                           expected_revision=1, control_epoch=1, command_id=prefix + "-review")


def test_two_stage_handoff_is_not_a_message_ack(runtime_case):
    store, runtime, owner, worker, active, schema = runtime_case
    first, second = active["task_ids"]["produce"], active["task_ids"]["second"]
    assert runtime.task(owner, first)["state"] == "queued"
    assert runtime.task(owner, second)["state"] == "waiting_inputs"
    _, _, result = submit_candidate(runtime, worker, first, schema)
    assert runtime.task(owner, first)["state"] == "verifying"
    review_candidate(runtime, owner, result)
    pending = runtime.task(owner, second)
    assert pending["state"] == "waiting_inputs"
    assert pending["inputs"][0]["receipt"] == "pending"
    runtime.accept_input(owner, second, "request", result["artifacts"][0]["version_id"], revision=1,
                         accepted=True, reason="Compatible with second-stage contract", command_id="accept", control_epoch=1)
    assert runtime.task(owner, second)["state"] == "queued"
    _, _, second_result = submit_candidate(runtime, worker, second, schema, "two")
    review_candidate(runtime, owner, second_result, "two")
    assert runtime._runtime_run(active["team_run_id"])["state"] == "verifying"
    assert store.list_tasks() == []
    with pytest.raises(Exception, match="PROTOCOL_VERSION_MISMATCH"):
        store.get_task(first)


def test_data_uses_same_runtime(tmp_path):
    with CoordinationStore(tmp_path / "coord.sqlite", enable_team_v2=True) as store:
        runtime, owner, worker, active, schema = runtime_fixture(store, "data")
        _, _, result = submit_candidate(runtime, worker, active["task_ids"]["produce"], schema)
        review_candidate(runtime, owner, result)
        assert runtime.task(owner, active["task_ids"]["produce"])["state"] == "completed"


def test_submit_replay_and_failed_acceptance(runtime_case):
    _, runtime, owner, worker, active, schema = runtime_case
    claimed, submission, result = submit_candidate(runtime, worker, active["task_ids"]["produce"], schema)
    assert runtime.submit(worker, claimed["token"], submission, usage_tokens=30, command_id="one-submit") == result
    with pytest.raises(TeamStateError, match="ACCEPTANCE_FAILED"):
        runtime.review(owner, result["handoff_id"], accepted=True,
                       scores=[{"criterion_id": "valid", "score": 0, "evidence": "Not sufficient"}],
                       expected_revision=1, control_epoch=1, command_id="bad-review")


def test_expired_lease_cannot_heartbeat_or_submit(runtime_case):
    _, runtime, owner, worker, active, schema = runtime_case
    task = runtime.task(owner, active["task_ids"]["produce"])
    claimed = runtime.claim(worker, task["task_id"], worker_session_id="one", expected_version=task["state_version"], command_id="claim")
    runtime.clock = lambda: 10**12
    with pytest.raises(TeamStateError, match="LEASE_EXPIRED"):
        runtime.heartbeat(worker, claimed["token"], command_id="hb")


def test_worker_cannot_review_its_own_result(runtime_case):
    _, runtime, owner, worker, active, schema = runtime_case
    _, _, result = submit_candidate(runtime, worker, active["task_ids"]["produce"], schema)
    with pytest.raises(TeamStateError, match="PERMISSION_REQUIRED"):
        review_candidate(runtime, worker, result)
