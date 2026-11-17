from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from frontend import server
from mewcode.agents.task_manager import BackgroundTask, TaskManager
from mewcode.runtime_scheduler import RuntimeScheduler


class _Runtime:
    def __init__(
        self,
        *,
        cleanup_gate: asyncio.Event | None = None,
        cleanup_error: BaseException | None = None,
    ) -> None:
        self.cleanup_gate = cleanup_gate
        self.cleanup_error = cleanup_error
        self.cleanup_started = asyncio.Event()
        self.closed = False

    async def aclose(self) -> None:
        self.cleanup_started.set()
        if self.cleanup_gate is not None:
            await self.cleanup_gate.wait()
        if self.cleanup_error is not None:
            raise self.cleanup_error
        self.closed = True


class _Agent:
    def __init__(
        self,
        *,
        agent_id: str = "child",
        runtime: _Runtime | None = None,
        started: asyncio.Event | None = None,
    ) -> None:
        self.agent_id = agent_id
        self._cordis_runtime = runtime or _Runtime()
        self.started = started or asyncio.Event()
        self.total_input_tokens = 3
        self.total_output_tokens = 5
        self.total_tool_calls = 0
        self.session_id = ""
        self._conversation_scope = "project"
        self._conversation_store_dir = ""
        self.work_dir = ""
        self.team_name = ""

    async def run_to_completion(self, *_args, **_kwargs) -> str:
        self.started.set()
        await asyncio.Event().wait()
        return "unreachable"


class _FakeSession:
    def __init__(
        self,
        manager: TaskManager,
        *,
        active_id: str,
        active_store: Path,
    ) -> None:
        self.task_manager = manager
        self.session = SimpleNamespace(session_id=active_id)
        self.scope = "project"
        self.store = str(active_store)
        self.conversation_runs = {}

    def _conversation_store_dir(self, _scope: str | None = None) -> str:
        return self.store

    def interrupt_conversation(self, conversation_id: str, *, action: str) -> dict:
        return {
            "conversation_id": conversation_id,
            "status": "not_running",
            "cancelled": False,
        }

    def tasks_payload(self) -> list[dict]:
        return []


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_error", [False, True])
async def test_watchdog_waits_for_inner_execution_to_stop_before_closing_runtime(cleanup_error):
    manager = TaskManager()
    cleanup_started, cleanup_gate = asyncio.Event(), asyncio.Event()

    class Child(_Agent):
        async def run_to_completion(self, *_args, **_kwargs):
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await cleanup_gate.wait()
                if cleanup_error:
                    raise RuntimeError("execution cleanup failed")

    child = Child()
    task_id = manager.launch(child, "work", progress=SimpleNamespace(
        last_event_at=time.monotonic(), status="running"))
    await child.started.wait()
    manager.cancel(task_id)
    await cleanup_started.wait()
    try:
        assert await manager.settle_tasks([task_id], timeout=0.01) == [task_id]
        assert not child._cordis_runtime.closed
        assert not manager.get(task_id).lifecycle_finalized
    finally:
        cleanup_gate.set()
        await manager.settle_tasks([task_id], timeout=1)
    assert manager.get(task_id).status == ("failed" if cleanup_error else "cancelled")
    assert manager.get(task_id).cleanup_confirmed is not cleanup_error


@pytest.mark.asyncio
async def test_interrupt_uses_captured_runtime_manager_after_switching_chat(monkeypatch, tmp_path):
    original, visible = TaskManager(), TaskManager()
    child, other = _Agent(), _Agent(agent_id="other")
    child_id = original.launch(child, "original", owner_session_id="original",
                               owner_scope="project", owner_store_dir=str(tmp_path))
    other_id = visible.launch(other, "visible", owner_session_id="visible",
                              owner_scope="project", owner_store_dir=str(tmp_path))
    await asyncio.gather(child.started.wait(), other.started.wait())
    session = _FakeSession(visible, active_id="visible", active_store=tmp_path)
    session.conversation_runs["original"] = SimpleNamespace(
        scope="project", work_dir=str(tmp_path), task_manager=original)

    async def send_json(_payload):
        pass

    monkeypatch.setattr(server, "SESSION", session)
    monkeypatch.setattr(server, "WS_EVENTS", SimpleNamespace(send_json=send_json))
    try:
        result = await server.request_chat_interrupt("original")
        assert result["child_task_cancellation_requested"] == [child_id]
        assert original.get(child_id).status == "cancelled"
        assert visible.get(other_id).status == "running"
    finally:
        await original.settle_tasks([child_id], cancel=True, timeout=1)
        await visible.settle_tasks([other_id], cancel=True, timeout=1)


@pytest.mark.asyncio
async def test_cli_interrupt_cancels_owned_background_children_without_streaming(tmp_path):
    from mewcode.app import MewCodeApp
    manager = TaskManager()
    child, other = _Agent(), _Agent(agent_id="other")
    child.agent_id = ""  # Incomplete legacy identity must not bind unrelated children.
    child.parent_id, other.parent_id = "parent", ""
    child_id, other_id = manager.launch(child, "child"), manager.launch(other, "other")
    await asyncio.gather(child.started.wait(), other.started.wait())
    messages = []
    app = SimpleNamespace(_streaming=False, _agent_task=None, _subagent_task=None,
        agent=SimpleNamespace(agent_id="parent", session_id="session", work_dir=str(tmp_path)),
        task_manager=manager, _finish_streaming=lambda: None,
        _show_system_message=messages.append,
        query_one=lambda *_args: (_ for _ in ()).throw(LookupError()))
    try:
        await MewCodeApp._interrupt_streaming(app)
        assert manager.get(child_id).status == "cancelled"
        assert child._cordis_runtime is None
        assert manager.get(other_id).status == "running"
        assert "interrupted" in messages[-1]
    finally:
        await manager.settle_tasks([child_id, other_id], cancel=True, timeout=1)


@pytest.mark.asyncio
async def test_repeated_cli_interrupt_does_not_interrupt_root_cleanup(tmp_path):
    from mewcode.app import MewCodeApp
    started, cleaning, gate = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def root():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await gate.wait()

    task = asyncio.create_task(root())
    await started.wait()
    app = SimpleNamespace(_streaming=True, _agent_task=task, _subagent_task=None,
        agent=SimpleNamespace(agent_id="parent", session_id="session", work_dir=str(tmp_path)),
        task_manager=TaskManager(), _finish_streaming=lambda: None,
        _show_system_message=lambda _: None,
        query_one=lambda *_args: (_ for _ in ()).throw(LookupError()))
    first = asyncio.create_task(MewCodeApp._interrupt_streaming(app))
    await cleaning.wait()
    second = asyncio.create_task(MewCodeApp._interrupt_streaming(app))
    await asyncio.sleep(0)
    try:
        assert task.cancelling() == 1 and not task.done()
    finally:
        gate.set()
        await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_cancel_before_worker_starts_uses_host_completion_path():
    manager = TaskManager()
    runtime = _Runtime()
    agent = _Agent(runtime=runtime)
    task_id = manager.launch(agent, "work")

    assert manager.cancel(task_id) is True
    pending = await manager.settle_tasks([task_id], timeout=1.0)

    task = manager.get(task_id)
    assert pending == []
    assert task is not None
    assert task.status == "cancelled"
    assert task.cancellation_pending is False
    assert task.end_time is not None
    assert task.lifecycle_finalized is True
    assert runtime.closed is True
    assert [item.id for item in manager.poll_completed()] == [task_id]


@pytest.mark.asyncio
async def test_cancel_waits_for_async_cleanup_and_preserves_partial_result():
    manager = TaskManager()
    cleanup_gate = asyncio.Event()
    runtime = _Runtime(cleanup_gate=cleanup_gate)
    agent = _Agent(runtime=runtime)
    task_id = manager.adopt_running(agent, "continue work", partial_result="partial output")
    await agent.started.wait()

    assert manager.cancel(task_id) is True
    handle = manager._async_tasks[task_id]
    assert manager.cancel(task_id) is True
    assert handle.cancelling() == 1
    await runtime.cleanup_started.wait()

    task = manager.get(task_id)
    assert task is not None
    assert task.status == "running"
    assert task.cancellation_pending is True
    assert task.end_time is None
    assert manager.task_payload(task_id)["cancellation_pending"] is True

    pending = await manager.settle_tasks([task_id], timeout=0.01)
    assert pending == [task_id]
    cleanup_gate.set()
    assert await manager.settle_tasks([task_id], timeout=1.0) == []

    task = manager.get(task_id)
    assert task is not None
    assert task.status == "cancelled"
    assert task.result == "partial output"
    assert task.end_time is not None
    assert runtime.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cleanup_error",
    [RuntimeError("close failed"), asyncio.CancelledError()],
)
async def test_unconfirmed_cleanup_blocks_await_all_settlement(cleanup_error):
    manager = TaskManager()
    runtime = _Runtime(cleanup_error=cleanup_error)
    agent = _Agent(runtime=runtime)
    task_id = manager.adopt_running(agent, "continue work", partial_result="partial")
    await agent.started.wait()

    with pytest.raises(RuntimeError, match="Task cleanup was not confirmed"):
        await manager.settle_tasks([task_id], cancel=True)

    task = manager.get(task_id)
    assert task.status == "failed"
    assert task.lifecycle_finalized is True
    assert task.cleanup_confirmed is False
    assert "cleanup" in task.cleanup_error.lower()
    assert "partial" in task.result
    assert task.cancellation_pending is False


@pytest.mark.asyncio
async def test_settle_tasks_does_not_wait_for_or_cancel_its_current_worker():
    manager = TaskManager()

    class SelfSettlingAgent(_Agent):
        def __init__(self):
            super().__init__()
            self.task_id = ""
            self.pending = None
            self.status_during_settle = ""

        async def run_to_completion(self, *_args, **_kwargs):
            self.started.set()
            self.pending = await manager.settle_tasks(
                [self.task_id], cancel=True
            )
            self.status_during_settle = manager.get(self.task_id).status
            return "finished"

    agent = SelfSettlingAgent()
    task_id = manager.launch(agent, "self settle")
    agent.task_id = task_id
    await agent.started.wait()
    await manager.settle_tasks([task_id])

    task = manager.get(task_id)
    assert agent.pending == [task_id]
    assert agent.status_during_settle == "running"
    assert task.status == "completed"
    assert task.cancellation_pending is False


@pytest.mark.asyncio
async def test_adopt_running_uses_scheduler_runtime_not_registration_age():
    class Scheduler:
        def agent_runtime(self, agent_id: str) -> dict:
            assert agent_id == "child"
            return {
                "started": False,
                "active": False,
                "active_slots": 0,
                "elapsed": 0.0,
            }

    manager = TaskManager()
    manager.runtime_scheduler = Scheduler()
    agent = _Agent()
    agent._chat_task_id = "parent-task"
    agent._chat_task_title = "Parent task title"
    task_id = manager.adopt_running(agent, "resume me")
    task = manager.get(task_id)
    task.start_time -= 3600

    await asyncio.sleep(0)
    assert task.owner_task_id == "parent-task"
    assert task.owner_task_title == "Parent task title"
    assert task.duration_basis == "active_runtime"
    assert task.duration_quality == "measured"
    assert manager._execution_elapsed(task) == 0.0

    manager.cancel(task_id)
    await manager.settle_tasks([task_id], timeout=1.0)


def test_legacy_task_duration_remains_unknown_after_restart(tmp_path: Path):
    state_path = tmp_path / "tasks.json"
    state_path.write_text(json.dumps({
        "tasks": [{
            "id": "legacy",
            "name": "legacy task",
            "task": "old task",
            "status": "completed",
            "elapsed": 1234.5,
        }],
    }), encoding="utf-8")

    task = TaskManager(state_path).get("legacy")

    assert task is not None
    assert task.duration_basis == "legacy_wall"
    assert task.duration_quality == "unknown"
    assert TaskManager(state_path).task_payload("legacy")["elapsed"] is None


@pytest.mark.asyncio
async def test_interrupt_cancels_only_children_for_exact_conversation_and_store(
    monkeypatch, tmp_path: Path,
):
    store_a = tmp_path / "project-a"
    store_b = tmp_path / "project-b"
    store_a.mkdir()
    store_b.mkdir()
    manager = TaskManager()
    agents: dict[str, _Agent] = {}
    task_ids: dict[str, str] = {}

    async def launch(task_id: str, owner_id: str, store: Path, scope: str = "project"):
        agent = _Agent(agent_id=task_id)
        agent.session_id = owner_id
        agent._conversation_scope = scope
        agent._conversation_store_dir = str(store)
        task_ids[task_id] = manager.launch(
            agent, task_id, owner_session_id=owner_id, owner_scope=scope,
            owner_store_dir=str(store),
        )
        agents[task_id] = agent

    await launch("target", "conversation-a", store_a)
    await launch("same-id-other-store", "conversation-a", store_b)
    await launch("other-conversation", "conversation-b", store_a)
    await launch("unknown-owner", "", store_a)
    await asyncio.gather(*(agent.started.wait() for agent in agents.values()))

    fake_session = _FakeSession(
        manager, active_id="conversation-a", active_store=store_a
    )
    sent: list[dict] = []

    async def send_json(payload: dict) -> None:
        sent.append(payload)

    monkeypatch.setattr(server, "SESSION", fake_session)
    monkeypatch.setattr(server, "WS_EVENTS", SimpleNamespace(send_json=send_json))
    result = await server.request_chat_interrupt("conversation-a")

    assert result["child_task_cancellation_requested"] == [task_ids["target"]]
    assert "child_task_cancellation_pending" not in result
    assert manager.get(task_ids["target"]).status == "cancelled"
    assert manager.get(task_ids["same-id-other-store"]).status == "running"
    assert manager.get(task_ids["other-conversation"]).status == "running"
    assert manager.get(task_ids["unknown-owner"]).status == "running"
    assert sent[-1]["conversation_id"] == "conversation-a"

    remaining = [
        task_ids["same-id-other-store"],
        task_ids["other-conversation"],
        task_ids["unknown-owner"],
    ]
    await manager.settle_tasks(remaining, cancel=True, timeout=1.0)


@pytest.mark.asyncio
async def test_orphan_child_is_cancelled_when_parent_runtime_has_ended(
    monkeypatch, tmp_path: Path,
):
    target_store = tmp_path / "captured-project"
    active_store = tmp_path / "active-project"
    target_store.mkdir()
    active_store.mkdir()
    manager = TaskManager()
    child = _Agent(agent_id="orphan")
    child.session_id = "ended-conversation"
    child._conversation_scope = "project"
    child._conversation_store_dir = str(target_store)
    task_id = manager.launch(
        child, "orphan child", owner_session_id="ended-conversation",
        owner_scope="project", owner_store_dir=str(target_store),
    )
    await child.started.wait()

    fake_session = _FakeSession(
        manager, active_id="another-conversation", active_store=active_store
    )
    async def send_json(_payload: dict) -> None:
        return None

    monkeypatch.setattr(server, "SESSION", fake_session)
    monkeypatch.setattr(server, "WS_EVENTS", SimpleNamespace(send_json=send_json))
    result = await server.request_chat_interrupt("ended-conversation")

    assert result["conversation_id"] == "ended-conversation"
    assert result["child_task_cancellation_requested"] == [task_id]
    assert manager.get(task_id).status == "cancelled"


@pytest.mark.asyncio
async def test_interrupt_reports_child_cleanup_failure_to_ui(monkeypatch, tmp_path: Path):
    store = tmp_path / "project"
    store.mkdir()
    manager = TaskManager()
    child = _Agent(runtime=_Runtime(cleanup_error=RuntimeError("close failed")))
    child.session_id = "conversation"
    child._conversation_store_dir = str(store)
    task_id = manager.launch(
        child, "child", owner_session_id="conversation",
        owner_scope="project", owner_store_dir=str(store),
    )
    await child.started.wait()
    fake_session = _FakeSession(manager, active_id="conversation", active_store=store)
    async def send_json(_payload: dict) -> None:
        return None

    monkeypatch.setattr(server, "SESSION", fake_session)
    monkeypatch.setattr(server, "WS_EVENTS", SimpleNamespace(send_json=send_json))

    result = await server.request_chat_interrupt("conversation")

    task = manager.get(task_id)
    assert result["child_task_cancellation_requested"] == [task_id]
    assert result["child_task_cleanup_failed"] == [task_id]
    assert "child_task_cancellation_pending" not in result
    assert task.status == "failed"
    assert task.cleanup_confirmed is False


@pytest.mark.asyncio
async def test_cancel_endpoint_reports_pending_cleanup(monkeypatch, tmp_path: Path):
    store = tmp_path / "project"
    store.mkdir()
    task = BackgroundTask(
        id="pending", name="pending", agent=None, task="work",
        owner_session_id="conversation", owner_scope="project",
        owner_store_dir=str(store),
    )

    async def settle(task_ids, *, timeout=None):
        assert task_ids == [task.id]
        assert timeout == 1.0
        return [task.id]

    def cancel(task_id: str) -> bool:
        assert task_id == task.id
        task.cancellation_pending = True
        return True

    manager = SimpleNamespace(
        get=lambda task_id: task if task_id == task.id else None,
        cancel=cancel,
        settle_tasks=settle,
    )
    fake_session = SimpleNamespace(
        task_manager=manager,
        session=SimpleNamespace(session_id="conversation"),
        scope="project",
        _conversation_store_dir=lambda: str(store),
    )
    monkeypatch.setattr(server, "SESSION", fake_session)

    response = await server.api_cancel_task(task.id)

    payload = json.loads(response.body)
    assert response.status_code == 200
    assert payload["status"] == "running"
    assert payload["cleanup_pending"] is True
    assert payload["cleanup_failed"] is False


@pytest.mark.asyncio
async def test_orphan_owner_inference_refuses_ambiguous_or_unknown_store(
    monkeypatch, tmp_path: Path,
):
    store_a = tmp_path / "a"
    store_b = tmp_path / "b"
    store_a.mkdir()
    store_b.mkdir()
    manager = TaskManager()
    tasks = []
    for task_id, store in (("a", store_a), ("b", store_b)):
        agent = _Agent(agent_id=task_id)
        agent.session_id = "duplicate-conversation"
        agent._conversation_store_dir = str(store)
        tasks.append(manager.launch(
            agent, task_id, owner_session_id="duplicate-conversation",
            owner_scope="project", owner_store_dir=str(store),
        ))
    unknown = _Agent(agent_id="unknown")
    unknown.session_id = "unowned-conversation"
    tasks.append(manager.launch(
        unknown, "unknown", owner_session_id="unowned-conversation",
        owner_scope="project", owner_store_dir="",
    ))
    await asyncio.gather(*(
        manager.get(task_id).agent.started.wait() for task_id in tasks
    ))

    fake_session = _FakeSession(
        manager, active_id="another-conversation", active_store=tmp_path
    )
    async def send_json(_payload: dict) -> None:
        return None

    monkeypatch.setattr(server, "SESSION", fake_session)
    monkeypatch.setattr(server, "WS_EVENTS", SimpleNamespace(send_json=send_json))
    ambiguous = await server.request_chat_interrupt("duplicate-conversation")
    unknown_owner = await server.request_chat_interrupt("unowned-conversation")

    assert "child_task_cancellation_requested" not in ambiguous
    assert "child_task_cancellation_requested" not in unknown_owner
    assert all(manager.get(task_id).status == "running" for task_id in tasks)
    await manager.settle_tasks(tasks, cancel=True, timeout=1.0)


@pytest.mark.asyncio
async def test_agent_task_payload_reports_conversation_queue_and_active_slots(
    monkeypatch, tmp_path: Path,
):
    scheduler = RuntimeScheduler(limit=1)
    monkeypatch.setattr(server, "APP_RUNTIME_SCHEDULER", scheduler)
    manager = TaskManager()
    agent = SimpleNamespace(
        agent_id="queued-agent", total_input_tokens=0,
        total_output_tokens=0, total_tool_calls=0,
    )
    fake_session = SimpleNamespace(
        session=SimpleNamespace(session_id="conversation"),
        _conversation_store_dir=lambda: str(tmp_path),
        _agent_tool=SimpleNamespace(runs={"queued-agent": {
            "agent": agent, "name": "queued", "agent_type": "Verification",
            "mode": "bg", "status": "running", "task": "review",
            "task_id": "", "team": "", "start": time.monotonic() - 3600,
            "end": None, "owner_session_id": "conversation",
            "owner_store_dir": str(tmp_path),
        }}),
        task_manager=manager,
    )
    holder = await scheduler.acquire(kind="model", agent_id="holder")
    pending = asyncio.create_task(scheduler.acquire(kind="model", agent_id="queued-agent"))
    await asyncio.sleep(0)

    queued = server.WebSession.agent_tasks_payload(fake_session)
    queued_row = queued["groups"][0]["rows"][0]
    assert queued["conversation_id"] == "conversation"
    assert queued_row["status"] == "queued"
    assert queued_row["queue_position"] == 1
    assert queued_row["runtime_active"] is False
    assert queued_row["runtime_active_slots"] == 0
    assert queued_row["elapsed"] == 0

    await scheduler.release(holder)
    lease = await pending
    active = server.WebSession.agent_tasks_payload(fake_session)
    active_row = active["groups"][0]["rows"][0]
    assert active_row["status"] == "running"
    assert active_row["runtime_active"] is True
    assert active_row["runtime_active_slots"] == 1
    await scheduler.release(lease)


@pytest.mark.asyncio
async def test_fallback_task_rows_also_show_queue_position_and_runtime_activity(
    monkeypatch, tmp_path: Path,
):
    scheduler = RuntimeScheduler(limit=1)
    monkeypatch.setattr(server, "APP_RUNTIME_SCHEDULER", scheduler)
    agent = SimpleNamespace(agent_id="fallback", total_input_tokens=0, total_output_tokens=0)
    manager = TaskManager()
    manager._tasks["fallback-task"] = BackgroundTask(
        id="fallback-task", name="fallback", agent=agent, task="review",
        owner_session_id="conversation", owner_store_dir=str(tmp_path),
        duration_basis="active_runtime", execution_elapsed_seconds=0.0,
        duration_quality="measured",
    )
    fake_session = SimpleNamespace(
        session=SimpleNamespace(session_id="conversation"),
        _conversation_store_dir=lambda: str(tmp_path),
        _agent_tool=None,
        task_manager=manager,
    )
    holder = await scheduler.acquire(kind="tool", agent_id="holder")
    pending = asyncio.create_task(scheduler.acquire(kind="model", agent_id="fallback"))
    await asyncio.sleep(0)

    queued = server.WebSession.agent_tasks_payload(fake_session)
    row = queued["groups"][0]["rows"][0]
    assert row["status"] == "queued"
    assert row["queue_position"] == 1
    assert row["runtime_active"] is False
    assert row["elapsed"] == 0

    await scheduler.release(holder)
    lease = await pending
    active = server.WebSession.agent_tasks_payload(fake_session)
    row = active["groups"][0]["rows"][0]
    assert row["runtime_active"] is True
    assert row["runtime_active_slots"] == 1
    await scheduler.release(lease)


@pytest.mark.asyncio
async def test_active_agent_with_queued_leaf_request_stays_running(
    monkeypatch, tmp_path: Path,
):
    scheduler = RuntimeScheduler(limit=2)
    monkeypatch.setattr(server, "APP_RUNTIME_SCHEDULER", scheduler)
    agent = SimpleNamespace(agent_id="mixed", total_input_tokens=0, total_output_tokens=0)
    manager = TaskManager()
    manager.runtime_scheduler = scheduler
    manager._tasks["mixed-task"] = BackgroundTask(
        id="mixed-task", name="mixed", agent=agent, task="review",
        owner_session_id="conversation", owner_store_dir=str(tmp_path),
        duration_basis="active_runtime", execution_elapsed_seconds=0.0,
        duration_quality="measured",
    )
    fake_session = SimpleNamespace(
        session=SimpleNamespace(session_id="conversation"),
        _conversation_store_dir=lambda: str(tmp_path),
        _agent_tool=None,
        task_manager=manager,
    )

    active_lease = await scheduler.acquire(kind="model", agent_id="mixed")
    other_lease = await scheduler.acquire(kind="tool", agent_id="other")
    queued_lease = asyncio.create_task(
        scheduler.acquire(kind="tool", agent_id="mixed")
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0.12)

    payload = server.WebSession.agent_tasks_payload(fake_session)
    row = payload["groups"][0]["rows"][0]
    assert row["status"] == "running"
    assert row["queue_position"] == 1
    assert row["runtime_active"] is True
    assert row["runtime_active_slots"] == 1
    assert row["elapsed"] >= 0.1

    await scheduler.release(other_lease)
    second_lease = await queued_lease
    payload = server.WebSession.agent_tasks_payload(fake_session)
    row = payload["groups"][0]["rows"][0]
    assert row["status"] == "running"
    assert row["runtime_active_slots"] == 2

    await scheduler.release(active_lease)
    await scheduler.release(second_lease)
