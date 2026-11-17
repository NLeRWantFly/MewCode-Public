"""Desktop child clocks must observe the scheduler that admits their work."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from frontend import server
from mewcode.agents.task_manager import TaskManager
from mewcode.plugins import ApplicationHost
from mewcode.plugins.services import RUNTIME_SCHEDULER
from mewcode.runtime_scheduler import RuntimeScheduler
import mewcode.runtime_scheduler as scheduler_module


def test_nested_hosts_share_scheduler_but_independent_hosts_do_not(tmp_path):
    root = ApplicationHost(str(tmp_path), discover_plugins=False).start()
    child = other = None
    try:
        child = ApplicationHost(str(tmp_path), context=root.context.scope("desktop-session"),
                                discover_plugins=False).start()
        other = ApplicationHost(str(tmp_path), discover_plugins=False).start()
        scheduler = root.context.get(RUNTIME_SCHEDULER)
        assert child.context.get(RUNTIME_SCHEDULER) is scheduler
        assert other.context.get(RUNTIME_SCHEDULER) is not scheduler
        child.close()
        child = None
        assert root.context.get(RUNTIME_SCHEDULER) is scheduler
        assert scheduler.snapshot()["running"] == 0
    finally:
        if child is not None:
            child.close()
        if other is not None:
            other.close()
        root.close()


class ClockAgent:
    """An in-memory worker; no provider, Shell, native process or network."""
    def __init__(self, scheduler, identity):
        self.runtime_scheduler = scheduler
        self.agent_id = identity
        self.team_name = ""
        self.total_input_tokens = self.total_output_tokens = 0
        self.total_tool_calls = 1
        self.started = asyncio.Event()
        self.finish = asyncio.Event()

    async def run_to_completion(self, _task, event_callback):
        async with self.runtime_scheduler.slot(kind="tool", agent_id=self.agent_id):
            event_callback({"type": "tool_use", "toolName": "SyntheticWait"})
            self.started.set()
            await self.finish.wait()
        return "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("registered", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
async def test_child_clock_across_queue_active_terminal_and_reload(
    tmp_path, monkeypatch, registered, cancel,
):
    now = [100.0]
    monkeypatch.setattr(scheduler_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    actual, unrelated = RuntimeScheduler(limit=2), RuntimeScheduler(limit=2)
    monkeypatch.setattr(server, "APP_RUNTIME_SCHEDULER", unrelated)
    manager = TaskManager(tmp_path / "tasks.json")
    manager.runtime_scheduler = unrelated
    agent = ClockAgent(actual, "child-A")
    sibling = ClockAgent(actual, "child-B")
    # Fill both slots: registration age must not appear as execution time.
    holders = [await actual.acquire(kind="tool", agent_id=f"holder-{i}") for i in range(2)]
    owner = dict(owner_session_id="session", owner_store_dir=str(tmp_path), owner_task_id="parent")
    task_id = manager.launch(agent, "wait", **owner)
    sibling_id = manager.launch(sibling, "control", **owner)
    session = SimpleNamespace(
        task_manager=manager, session=SimpleNamespace(session_id="session"), scope="project",
        _conversation_store_dir=lambda: str(tmp_path), _agent_tool=None,
    )
    if registered:
        session._agent_tool = SimpleNamespace(runs={agent.agent_id: {
            "agent": agent, "name": "child-A", "agent_type": "test", "mode": "bg",
            "status": "running", "task": "wait", "task_id": task_id, "team": "",
            "start": 0, "end": None, **owner,
        }})

    def rows():
        sidebar = next(row for row in server.WebSession.tasks_payload(session) if row["id"] == task_id)
        panel = next(row for group in server.WebSession.agent_tasks_payload(session)["groups"]
                     for row in group["rows"] if row["task_id"] == task_id)
        return manager.task_payload(task_id), sidebar, panel

    try:
        await asyncio.sleep(0)
        now[0] += 3540
        detail, sidebar, panel = rows()
        assert detail["elapsed"] == sidebar["elapsed"] == panel["elapsed"] == 0
        assert sidebar["status"] == panel["status"] == "queued"
        assert not sidebar["runtime_started"] and not panel["runtime_active"]
        for holder in holders:
            await actual.release(holder)
        holders.clear()
        await asyncio.wait_for(agent.started.wait(), 1)
        await asyncio.wait_for(sibling.started.wait(), 1)
        now[0] += 7
        detail, sidebar, panel = rows()
        assert detail["elapsed"] == sidebar["elapsed"] == panel["elapsed"] == 7
        assert sidebar["runtime_active"] and panel["runtime_active"]
        assert panel["runtime_active_slots"] == 1
        assert sidebar["owner_task_id"] == "parent"
        if cancel:
            assert manager.cancel(task_id)
        else:
            agent.finish.set()
        assert await manager.settle_tasks([task_id], timeout=1) == []
        now[0] += 13
        detail, sidebar, panel = rows()
        assert detail["elapsed"] == sidebar["elapsed"] == panel["elapsed"] == 7
        assert detail["status"] == ("cancelled" if cancel else "completed")
        assert detail["cleanup_confirmed"] is True
        assert not sidebar["runtime_active"] and not panel["runtime_active"]
        assert manager.get(sibling_id).status == "running"
        assert actual.agent_runtime(sibling.agent_id)["active"]
        restored = TaskManager(tmp_path / "tasks.json").task_payload(task_id)
        assert restored["elapsed"] == 7
        assert restored["duration_quality"] == "measured"
    finally:
        for holder in holders:
            await actual.release(holder)
        agent.finish.set()
        sibling.finish.set()
        await manager.settle_tasks([task_id, sibling_id], timeout=1)


@pytest.mark.parametrize("status", ["completed", "cancelled", "running"])
def test_historical_zero_clock_with_work_is_unknown_not_unstarted(tmp_path, status):
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps({"tasks": [{
        "id": "old", "status": status, "elapsed": 0.0,
        "duration_basis": "active_runtime", "duration_quality": "measured",
        "progress": {"tool_call_count": 1, "input_tokens": 100},
    }]}), encoding="utf-8")
    detail = TaskManager(path).task_payload("old")
    assert detail["duration_quality"] == "unknown"
    assert detail["elapsed"] is None
    # A second load must not turn missing evidence into measured zero again.
    assert TaskManager(path).task_payload("old")["duration_quality"] == "unknown"
