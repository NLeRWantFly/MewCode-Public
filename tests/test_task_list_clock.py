import asyncio
import time
from types import SimpleNamespace

import pytest

from frontend import server
from mewcode.agents.task_manager import BackgroundTask, TaskManager
from mewcode.runtime_scheduler import RuntimeScheduler


def make_session(tmp_path, manager):
    return SimpleNamespace(
        session=SimpleNamespace(session_id="clock-test"), scope="project",
        _conversation_store_dir=lambda: str(tmp_path), task_manager=manager,
    )


def task_for(tmp_path, **changes):
    values = dict(id="row", name="clock", task="local fixture", agent=None,
                  start_time=time.monotonic() - 3540, owner_session_id="clock-test",
                  owner_scope="project", owner_store_dir=str(tmp_path))
    values.update(changes)
    return BackgroundTask(**values)


def test_sidebar_clock_matches_detail_and_never_counts_queue_wait(tmp_path):
    manager = TaskManager()
    task = task_for(tmp_path, duration_basis="active_runtime", duration_quality="measured",
                    execution_elapsed_seconds=12.5, status="cancelled", end_time=time.monotonic())
    manager._tasks[task.id] = task
    row = server.WebSession.tasks_payload(make_session(tmp_path, manager))[0]
    assert row["elapsed"] == manager.task_payload(task.id)["elapsed"] == 12.5
    assert row["runtime_started"] is True
    assert row["status"] == "cancelled"


def test_sidebar_legacy_time_is_unknown_and_not_3540_execution_seconds(tmp_path):
    manager = TaskManager()
    task = task_for(tmp_path, status="completed", end_time=time.monotonic())
    manager._tasks[task.id] = task
    row = server.WebSession.tasks_payload(make_session(tmp_path, manager))[0]
    assert row["elapsed"] is None
    assert row["duration_basis"] == "legacy_wall"
    assert row["legacy_wall_elapsed"] >= 3540


def test_sidebar_excludes_other_conversation_scope_and_store(tmp_path):
    manager = TaskManager()
    for task in [task_for(tmp_path, id="other-conversation", owner_session_id="other"),
                 task_for(tmp_path, id="other-scope", owner_scope="user"),
                 task_for(tmp_path, id="other-store", owner_store_dir=str(tmp_path / "other"))]:
        manager._tasks[task.id] = task
    assert server.WebSession.tasks_payload(make_session(tmp_path, manager)) == []


@pytest.mark.asyncio
async def test_sidebar_reports_actual_queue_and_active_slot(tmp_path):
    manager = TaskManager()
    scheduler = RuntimeScheduler(limit=1)
    manager.runtime_scheduler = scheduler
    agent = SimpleNamespace(agent_id="queue-clock")
    task = task_for(tmp_path, agent=agent, duration_basis="active_runtime",
                    duration_quality="measured", execution_elapsed_seconds=0)
    manager._tasks[task.id] = task
    session = make_session(tmp_path, manager)
    holder = await scheduler.acquire(kind="model", agent_id="holder")
    waiting = asyncio.create_task(scheduler.acquire(kind="model", agent_id=agent.agent_id))
    try:
        await asyncio.sleep(0)
        row = server.WebSession.tasks_payload(session)[0]
        assert row["status"] == "queued"
        assert row["queue_position"] == 1
        assert row["elapsed"] == 0
        assert row["runtime_active"] is False
        await scheduler.release(holder)
        holder = None
        lease = await waiting
        try:
            row = server.WebSession.tasks_payload(session)[0]
            assert row["status"] == "running"
            assert row["runtime_started"] is True
            assert row["runtime_active"] is True
        finally:
            await scheduler.release(lease)
    finally:
        if holder is not None:
            await scheduler.release(holder)
        if not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
