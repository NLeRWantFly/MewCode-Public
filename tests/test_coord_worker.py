from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mewcode.mcp.coord_protocol import APPROVAL_MODEL_ID, CoordinationError, TaskState
from mewcode.mcp.coord_store import CoordinationStore
from mewcode.mcp.coord_worker import EXECUTOR_ID, GrokCoordWorker, build_worker
from mewcode.mcp.external_model import GrokCLIInvoker, ModelCallStatus, ModelInvocationResult


def _deadline() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")


class FakeInvoker(GrokCLIInvoker):
    def __init__(
        self,
        result_status: ModelCallStatus = ModelCallStatus.COMPLETED,
        probe_status: ModelCallStatus = ModelCallStatus.COMPLETED,
    ) -> None:
        super().__init__(executable="fake-grok")
        self.result_status = result_status
        self.probe_status = probe_status
        self.calls = 0
        self.requests = []

    def probe_auth(self, *, timeout_seconds: int = 20) -> ModelInvocationResult:
        return ModelInvocationResult(
            provider_id="grok-build",
            model_id="grok-4.6",
            task_id="preflight",
            status=self.probe_status,
            exit_code=0 if self.probe_status is ModelCallStatus.COMPLETED else 1,
            stdout="",
            stderr="Not signed in" if self.probe_status is ModelCallStatus.AUTH_REQUIRED else "",
            command=("fake-grok", "models"),
            started_at="2026-01-01T00:00:00Z",
            completed_at="2026-01-01T00:00:01Z",
        )

    async def invoke_async(self, request):
        self.calls += 1
        self.requests.append(request)
        return ModelInvocationResult(
            provider_id="grok-build",
            model_id="grok-4.6",
            task_id=request.task_id,
            status=self.result_status,
            exit_code=0 if self.result_status is ModelCallStatus.COMPLETED else 1,
            stdout='{"text":"pong"}' if self.result_status is ModelCallStatus.COMPLETED else "",
            stderr="" if self.result_status is ModelCallStatus.COMPLETED else "Not signed in",
            command=("fake-grok",),
            started_at="2026-01-01T00:00:00Z",
            completed_at="2026-01-01T00:00:01Z",
            parsed_output={"text": "pong"} if self.result_status is ModelCallStatus.COMPLETED else None,
        )


def _worker(
    tmp_path: Path,
    status: ModelCallStatus = ModelCallStatus.COMPLETED,
    probe_status: ModelCallStatus = ModelCallStatus.COMPLETED,
):
    store = CoordinationStore(tmp_path / "coordination.sqlite3", tmp_path / "capabilities")
    store.register_agent(agent_id="codex-planner-01", model_id=APPROVAL_MODEL_ID, role="planner", session_id="planner")
    store.register_agent(agent_id="codex-verifier-01", model_id=APPROVAL_MODEL_ID, role="verifier", session_id="verifier")
    manifest = {"agent_id": EXECUTOR_ID, "model_id": "grok-4.6", "role": "executor", "tools": ["ping"], "skills": [], "mcp_servers": []}
    worker = GrokCoordWorker(
        root=tmp_path,
        store=store,
        invoker=FakeInvoker(status, probe_status),
        capabilities=manifest,
        heartbeat_seconds=60,
    )
    return store, worker


def _create_task(store: CoordinationStore, worker: GrokCoordWorker, reasoning_effort: str | None = None):
    snapshot = worker.ensure_executor()
    metadata = {"prompt": "Reply with pong."}
    if reasoning_effort is not None:
        metadata["reasoning_effort"] = reasoning_effort
    return store.create_task(
        creator_id="codex-planner-01",
        title="automatic ping",
        description="respond with pong",
        target_agent_id=EXECUTOR_ID,
        capabilities_hash_value=snapshot["capabilities_hash"],
        allowed_tools=["ping"],
        allowed_skills=[],
        allowed_paths=[],
        forbidden_paths=[],
        write_mode="read_only",
        acceptance_criteria=["pong"],
        max_turns=1,
        deadline=_deadline(),
        metadata=metadata,
    )


def test_worker_reuses_healthy_capability_registration(tmp_path: Path) -> None:
    store, worker = _worker(tmp_path)
    try:
        before = store._conn.execute(
            "SELECT count(*) FROM audit_events WHERE event_type IN ('agent_registered', 'capabilities_synced')"
        ).fetchone()[0]
        first = worker.ensure_executor()
        second = worker.ensure_executor()
        assert second["capabilities_hash"] == first["capabilities_hash"]
        events = store._conn.execute(
            "SELECT event_type FROM audit_events WHERE event_type IN ('agent_registered', 'capabilities_synced')"
        ).fetchall()
        assert len(events) - before == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_worker_automatically_claims_and_submits_result(tmp_path: Path) -> None:
    store, worker = _worker(tmp_path)
    try:
        task = _create_task(store, worker)
        results = await worker.run_once()
        assert len(results) == 1
        assert results[0]["state"] == TaskState.VERIFYING.value
        assert worker.invoker.calls == 1
        assert worker.invoker.requests[0].reasoning_effort == "high"
        messages = store.poll_messages(recipient_id="codex-verifier-01", task_id=task["task_id"])
        assert len(messages) == 1
        assert messages[0]["kind"] == "result"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_worker_does_not_starve_live_work_behind_expired_queue_rows(tmp_path: Path) -> None:
    store, worker = _worker(tmp_path)
    try:
        expired = [_create_task(store, worker) for _ in range(10)]
        with store._conn:
            for task in expired:
                store._conn.execute(
                    "UPDATE tasks SET deadline=? WHERE task_id=?",
                    ("2000-01-01T00:00:00Z", task["task_id"]),
                )
        live = _create_task(store, worker)

        results = await worker.run_once()

        assert [result["task_id"] for result in results] == [live["task_id"]]
        assert worker.invoker.calls == 1
        assert store.get_task(live["task_id"])["state"] == TaskState.VERIFYING.value
    finally:
        store.close()


@pytest.mark.asyncio
async def test_worker_preserves_task_reasoning_effort_override(tmp_path: Path) -> None:
    store, worker = _worker(tmp_path)
    try:
        _create_task(store, worker, reasoning_effort="low")
        await worker.run_once()
        assert worker.invoker.requests[0].reasoning_effort == "low"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_worker_does_not_claim_work_until_authentication_is_ready(tmp_path: Path) -> None:
    store, worker = _worker(tmp_path)
    try:
        task = _create_task(store, worker)
        worker.invoker.probe_status = ModelCallStatus.AUTH_REQUIRED
        worker._last_auth_check = 0
        assert await worker.run_once() == []
        assert store.get_task(task["task_id"])["state"] == TaskState.QUEUED.value
        assert worker.invoker.calls == 0
        assert store.get_agent(EXECUTOR_ID)["metadata_json"]
    finally:
        store.close()


def test_build_worker_uses_one_grok_home_for_inspect_and_invocation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    grok_home = tmp_path / "shared-grok-home"

    def fake_load(executable: str, root: Path, selected_home: Path):
        captured["inspect_home"] = selected_home
        return {"agent_id": EXECUTOR_ID, "model_id": "grok-4.6", "role": "executor", "tools": [], "skills": [], "mcp_servers": []}

    monkeypatch.setattr("mewcode.mcp.coord_worker.project_root", lambda: tmp_path)
    monkeypatch.setattr("mewcode.mcp.coord_worker.token_file", lambda root: tmp_path / ".bridge-token")
    monkeypatch.setattr("mewcode.mcp.coord_worker._load_capabilities", fake_load)
    worker = build_worker(root=tmp_path, state_dir=tmp_path / ".mewcode" / "coordination", grok_home=grok_home)
    try:
        assert captured["inspect_home"] == grok_home
        assert worker.invoker.grok_home == grok_home
    finally:
        worker.close()


def _patch_build_worker_dependencies(monkeypatch: pytest.MonkeyPatch, root: Path, load=None) -> None:
    monkeypatch.setattr("mewcode.mcp.coord_worker.project_root", lambda: root)
    monkeypatch.setattr("mewcode.mcp.coord_worker.token_file", lambda value: root / ".bridge-token")
    monkeypatch.setattr(
        "mewcode.mcp.coord_worker._load_capabilities",
        load or (lambda executable, fixed_root, selected_home: {"agent_id": EXECUTOR_ID, "model_id": "grok-4.6", "role": "executor", "tools": [], "skills": [], "mcp_servers": []}),
    )


def test_build_worker_rejects_second_worker_for_same_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_build_worker_dependencies(monkeypatch, tmp_path)
    state = tmp_path / ".mewcode" / "coordination"
    first = build_worker(root=tmp_path, state_dir=state)
    try:
        with pytest.raises(CoordinationError, match="another MewCode coordinator worker is already running"):
            build_worker(root=tmp_path, state_dir=state)
    finally:
        first.close()


def test_build_worker_releases_lock_on_close_and_startup_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_build_worker_dependencies(monkeypatch, tmp_path)
    state = tmp_path / ".mewcode" / "coordination"
    first = build_worker(root=tmp_path, state_dir=state)
    first.close()
    second = build_worker(root=tmp_path, state_dir=state)
    second.close()

    def fail_load(executable, fixed_root, selected_home):
        raise RuntimeError("inspect failed")

    _patch_build_worker_dependencies(monkeypatch, tmp_path, fail_load)
    with pytest.raises(RuntimeError, match="inspect failed"):
        build_worker(root=tmp_path, state_dir=state)

    _patch_build_worker_dependencies(monkeypatch, tmp_path)
    recovered = build_worker(root=tmp_path, state_dir=state)
    recovered.close()
