from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mewcode.mcp.coord_protocol import APPROVAL_MODEL_ID, CoordinationError
from mewcode.mcp.coord_store import CoordinationStore


def _deadline() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")


def _manifest(tools: list[str] | None = None) -> dict[str, object]:
    return {
        "agent_id": "grok-executor-01",
        "model_id": "grok-4.6",
        "role": "executor",
        "tools": tools or ["ping", "pytest"],
        "skills": ["read-only-audit"],
        "mcp_servers": [{"name": "mewcode-coordination", "version": "1"}],
    }


@pytest.fixture
def store(tmp_path: Path):
    value = CoordinationStore(tmp_path / "state.sqlite3", tmp_path / "capabilities")
    for agent_id, model_id, role in (
        ("codex-planner-01", APPROVAL_MODEL_ID, "planner"),
        ("grok-executor-01", "grok-4.6", "executor"),
        ("codex-verifier-01", APPROVAL_MODEL_ID, "verifier"),
    ):
        value.register_agent(agent_id=agent_id, model_id=model_id, role=role, session_id=f"session-{role}")
    yield value
    value.close()


def _task(store: CoordinationStore, **overrides: object) -> dict[str, object]:
    snapshot = store.sync_capabilities(agent_id="grok-executor-01", manifest=_manifest())
    args: dict[str, object] = {
        "creator_id": "codex-planner-01",
        "title": "bounded ping",
        "description": "protocol smoke test",
        "target_agent_id": "grok-executor-01",
        "capabilities_hash_value": snapshot["capabilities_hash"],
        "allowed_tools": ["ping"],
        "allowed_skills": [],
        "allowed_paths": [],
        "forbidden_paths": [],
        "write_mode": "read_only",
        "acceptance_criteria": ["result contains pong"],
        "max_turns": 1,
        "deadline": _deadline(),
    }
    args.update(overrides)
    return store.create_task(**args)


def test_protocol_smoke_and_idempotent_messages(store: CoordinationStore) -> None:
    task = _task(store)
    claimed = store.claim_task(task_id=task["task_id"], agent_id="grok-executor-01")
    assert claimed["state"] == "claimed"
    assert store.heartbeat(agent_id="grok-executor-01", task_id=task["task_id"], progress="started", progress_made=True)["task"]["state"] == "running"
    message = store.post_message(task_id=task["task_id"], sender_id="grok-executor-01", recipient_id="codex-verifier-01", kind="progress", payload={"ok": True}, message_id="fixed-message")
    duplicate = store.post_message(task_id=task["task_id"], sender_id="grok-executor-01", recipient_id="codex-verifier-01", kind="progress", payload={"ok": True}, message_id="fixed-message")
    assert message["sequence"] == 1
    assert duplicate["duplicate"] is True
    assert len(store.poll_messages(recipient_id="codex-verifier-01", task_id=task["task_id"])) == 1
    store.ack_message(message_id="fixed-message", agent_id="codex-verifier-01")
    assert store.poll_messages(recipient_id="codex-verifier-01", task_id=task["task_id"]) == []
    store.submit_result(task_id=task["task_id"], agent_id="grok-executor-01", result={"pong": True}, evidence={"source": "smoke"})
    assert store.close_task(task_id=task["task_id"], verifier_id="codex-verifier-01", accepted=True, evidence={"checked": True})["state"] == "completed"


def test_two_workers_cannot_claim_same_task(store: CoordinationStore) -> None:
    task = _task(store)
    calls = [(task["task_id"], "grok-executor-01"), (task["task_id"], "grok-executor-01")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda pair: _claim(store, *pair), calls))
    assert sum(result is None for result in results) == 1


def _claim(store: CoordinationStore, task_id: str, agent_id: str) -> dict[str, object] | None:
    try:
        return store.claim_task(task_id=task_id, agent_id=agent_id)
    except CoordinationError:
        return None


def test_capability_change_invalidates_old_task(store: CoordinationStore) -> None:
    task = _task(store)
    changed = store.sync_capabilities(agent_id="grok-executor-01", manifest=_manifest(["ping", "pytest", "new-tool"]))
    assert changed["changed"] is True
    assert changed["target"] == "capabilities"
    with pytest.raises(CoordinationError, match="mismatch"):
        store.claim_task(task_id=task["task_id"], agent_id="grok-executor-01")


def test_expired_lease_can_be_reclaimed(store: CoordinationStore) -> None:
    task = _task(store)
    store.claim_task(task_id=task["task_id"], agent_id="grok-executor-01")
    with store._conn:
        store._conn.execute("UPDATE tasks SET lease_until=? WHERE task_id=?", ("2000-01-01T00:00:00Z", task["task_id"]))
    assert store.claim_task(task_id=task["task_id"], agent_id="grok-executor-01")["state"] == "claimed"


def test_create_task_rejects_a_deadline_in_the_past(store: CoordinationStore) -> None:
    with pytest.raises(CoordinationError, match="deadline must be in the future"):
        _task(store, deadline="2000-01-01T00:00:00Z")


def test_create_task_rejects_an_unready_managed_worker(store: CoordinationStore) -> None:
    store.register_agent(
        agent_id="grok-executor-01",
        model_id="grok-4.6",
        role="executor",
        session_id="worker",
        metadata={
            "runner": "mewcode-coord-worker",
            "readiness": "auth_required",
            "readiness_reason": "Grok authentication is required",
        },
    )
    with pytest.raises(CoordinationError, match=r"not ready \(auth_required\)"):
        _task(store)


def test_acceptance_needs_evidence(store: CoordinationStore) -> None:
    task = _task(store)
    store.claim_task(task_id=task["task_id"], agent_id="grok-executor-01")
    store.submit_result(task_id=task["task_id"], agent_id="grok-executor-01", result={"ok": True}, evidence={"run": "x"})
    with pytest.raises(CoordinationError, match="verification evidence"):
        store.close_task(task_id=task["task_id"], verifier_id="codex-verifier-01", accepted=True)


def test_verifier_child_task_is_required_when_created(store: CoordinationStore) -> None:
    parent = _task(store)
    child = _task(
        store,
        creator_id="codex-verifier-01",
        title="bounded verification",
        description="run the explicitly assigned check",
        kind="verification",
        parent_task_id=parent["task_id"],
    )
    store.claim_task(task_id=parent["task_id"], agent_id="grok-executor-01")
    store.submit_result(task_id=parent["task_id"], agent_id="grok-executor-01", result={"ok": True}, evidence={"run": "execution"})
    with pytest.raises(CoordinationError, match="child task"):
        store.close_task(task_id=parent["task_id"], verifier_id="codex-verifier-01", accepted=True, evidence={"checked": True})
    store.claim_task(task_id=child["task_id"], agent_id="grok-executor-01")
    store.submit_result(task_id=child["task_id"], agent_id="grok-executor-01", result={"ok": True}, evidence={"run": "verification"})
    store.close_task(task_id=child["task_id"], verifier_id="codex-verifier-01", accepted=True, evidence={"checked": True})
    assert store.close_task(task_id=parent["task_id"], verifier_id="codex-verifier-01", accepted=True, evidence={"checked": True})["state"] == "completed"
