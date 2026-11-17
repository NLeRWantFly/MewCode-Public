from __future__ import annotations

import pytest

from mewcode.teams.store_v2 import TeamStateError
from mewcode.teams.runtime_protocol import ArtifactContent
from test_team_runtime_v2 import runtime_case, submit_candidate, review_candidate, runtime_actor


def test_unknown_usage_is_not_zero_and_resume_requires_reconciliation(runtime_case):
    _, runtime, owner, worker, active, schema = runtime_case
    task_id = active["task_ids"]["produce"]
    task = runtime.task(owner, task_id)
    claimed = runtime.claim(worker, task_id, worker_session_id="one", expected_version=task["state_version"], command_id="claim")
    runtime.clock = lambda: 10**12
    runtime.reap_expired(worker, active["team_run_id"], command_id="reap")
    expired = runtime.task(owner, task_id)
    assert expired["held_tokens"] == 300
    with pytest.raises(TeamStateError, match="USAGE_UNKNOWN"):
        runtime.resume(owner, task_id, expected_version=expired["state_version"], control_epoch=1, command_id="resume")
    runtime.resolve_usage(owner, claimed["token"]["attempt_id"], usage_tokens=40,
                          evidence="Verified provider record", control_epoch=1, command_id="usage")
    expired = runtime.task(owner, task_id)
    assert expired["spent_tokens"] == 40 and expired["held_tokens"] == 0
    with pytest.raises(TeamStateError, match="NEEDS_RECONCILIATION"):
        runtime.resume(owner, task_id, expected_version=expired["state_version"], control_epoch=1, command_id="resume")
    runtime.resume(owner, task_id, expected_version=expired["state_version"], control_epoch=1,
                   effect_evidence="Read-only call, no external writes", command_id="resume")
    fresh = runtime.task(owner, task_id)
    runtime.clock = __import__("time").time
    next_attempt = runtime.claim(worker, task_id, worker_session_id="two", expected_version=fresh["state_version"], command_id="next")
    assert next_attempt["reserved_tokens"] == 260
    with pytest.raises(TeamStateError, match="STALE_FENCE"):
        runtime.heartbeat(worker, claimed["token"], command_id="late")


def test_question_identity_survives_reopen_and_does_not_hold_slot(runtime_case):
    store, runtime, owner, worker, active, _ = runtime_case
    task_id = active["task_ids"]["produce"]
    claimed = runtime.claim(worker, task_id, worker_session_id="one", expected_version=runtime.task(owner, task_id)["state_version"], command_id="claim")
    blocked = runtime.block_attempt(worker, claimed["token"], reason_code="missing_input", detail="Which format?",
                                    usage_tokens=10, needs_answer=True, command_id="block")
    question_id = blocked["question"]["question_id"]
    assert store._conn.execute("SELECT COUNT(*) FROM team_v2_attempts WHERE state='running'").fetchone()[0] == 0
    assert runtime.task(owner, task_id)["question"]["question_id"] == question_id
    task = runtime.task(owner, task_id)
    with pytest.raises(TeamStateError, match="QUESTION_REQUIRED"):
        runtime.resume(owner, task_id, expected_version=task["state_version"], control_epoch=1, command_id="answer",
                       question_id="wrong", answer="JSON")
    runtime.resume(owner, task_id, expected_version=task["state_version"], control_epoch=1, command_id="answer",
                   question_id=question_id, answer="JSON")
    assert runtime.task(owner, task_id)["question"]["state"] == "answered"


def test_input_revision_invalidates_only_actual_dependents_and_keeps_history(runtime_case):
    store, runtime, owner, worker, active, schema = runtime_case
    first, second = active["task_ids"]["produce"], active["task_ids"]["second"]
    _, _, result = submit_candidate(runtime, worker, first, schema)
    review_candidate(runtime, owner, result)
    runtime.accept_input(owner, second, "request", result["artifacts"][0]["version_id"], revision=1, accepted=True,
                         reason="Compatible", command_id="accept", control_epoch=1)
    new_input = runtime.register_input(owner, active["team_run_id"], ArtifactContent.from_bytes(
        port_id="request", schema_ref=schema, media_type="application/json", content=b'{"source":2}'), control_epoch=1, command_id="input2")
    first_task = runtime.task(owner, first)
    changed = runtime.revise(owner, first, inputs={"request": new_input["version_id"]}, expected_version=first_task["state_version"],
                             control_epoch=1, reason="Adopt updated source", approved=True, command_id="revise")
    assert set(changed["affected_task_ids"]) == {first, second}
    assert runtime.task(owner, first)["revision"] == runtime.task(owner, second)["revision"] == 2
    assert runtime.task(owner, second)["inputs"][0]["version_id"] is None
    assert store._conn.execute("SELECT state FROM team_v2_task_revisions WHERE task_id=? AND revision=1", (first,)).fetchone()[0] == "completed"
    assert runtime.artifact(owner, result["artifacts"][0]["version_id"])["state"] == "published"
    with pytest.raises(TeamStateError, match="INPUT_REVISION_MISMATCH"):
        runtime.accept_input(owner, second, "request", result["artifacts"][0]["version_id"], revision=1, accepted=True,
                             reason="Old receipt", command_id="late", control_epoch=1)


def test_suspend_fences_worker_and_takeover_changes_control_epoch(runtime_case):
    store, runtime, owner, worker, active, _ = runtime_case
    task_id = active["task_ids"]["produce"]
    claimed = runtime.claim(worker, task_id, worker_session_id="one", expected_version=runtime.task(owner, task_id)["state_version"], command_id="claim")
    team = store.teams_v2.get_team(owner, active["team_id"])
    suspended = runtime.suspend(owner, team["team_id"], expected_version=team["state_version"], control_epoch=1, command_id="suspend")
    with pytest.raises(TeamStateError, match="STALE_FENCE"):
        runtime.heartbeat(worker, claimed["token"], command_id="old-worker")
    next_owner = runtime_actor("new-owner")
    current = runtime.take_over(next_owner, team["team_id"], expected_version=suspended["state_version"],
                                control_epoch=2, reason="Explicit host recovery", command_id="takeover")
    assert current["control_epoch"] == 3 and current["owner_ref"] == "new-owner"
    with pytest.raises(TeamStateError, match="TEAM_NOT_ACCESSIBLE"):
        runtime.cancel_run(owner, active["team_run_id"], control_epoch=1, reason="old owner", command_id="old-cancel")


def test_messages_are_durable_and_ack_does_not_accept_inputs(runtime_case):
    _, runtime, owner, worker, active, _ = runtime_case
    sent = runtime.send_message(owner, active["team_run_id"], recipient="producer", body="Discuss the task", command_id="send")
    assert sent["execution_started"] is False
    messages = runtime.messages(worker, active["team_run_id"])
    assert messages[0]["body"] == "Discuss the task"
    ack = runtime.ack_message(worker, sent["message_ids"][0], command_id="ack")
    assert ack["business_acceptance"] is False
    assert runtime.task(owner, active["task_ids"]["second"])["state"] == "waiting_inputs"


def test_finalize_requires_exact_terminal_outputs_then_close_keeps_artifacts(runtime_case):
    store, runtime, owner, worker, active, schema = runtime_case
    first, second = active["task_ids"]["produce"], active["task_ids"]["second"]
    _, _, result = submit_candidate(runtime, worker, first, schema)
    review_candidate(runtime, owner, result)
    runtime.accept_input(owner, second, "request", result["artifacts"][0]["version_id"], revision=1, accepted=True,
                         reason="Verified input", control_epoch=1, command_id="accept")
    _, _, last = submit_candidate(runtime, worker, second, schema, "second")
    review_candidate(runtime, owner, last, "second")
    snapshot = runtime.snapshot(owner, active["team_run_id"])
    finalized = runtime.finalize(owner, active["team_run_id"], expected_version=snapshot["run"]["state_version"],
                                  control_epoch=1, evidence="Validated export manifest", command_id="final")
    assert finalized["manifest"]["exports"]["delivery"] == last["artifacts"][0]["version_id"]
    team = store.teams_v2.get_team(owner, active["team_id"])
    runtime.close_team(owner, team["team_id"], expected_version=team["state_version"], control_epoch=1, command_id="close")
    assert runtime.artifact(owner, last["artifacts"][0]["version_id"])["size"] > 0
