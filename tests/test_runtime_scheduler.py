import asyncio

import pytest
from pydantic import BaseModel

import mewcode.client as client_module
import mewcode.tools.base as tool_base_module
from mewcode.client import scheduled_client_stream
from mewcode.conversation import ConversationManager
from mewcode.runtime_scheduler import RuntimeScheduler
from mewcode.tools.base import StreamEnd, Tool, ToolResult


@pytest.mark.asyncio
async def test_scheduler_caps_concurrency_and_preserves_fifo():
    scheduler = RuntimeScheduler(limit=4)
    active = 0
    peak = 0
    started: list[int] = []
    gate = asyncio.Event()

    async def worker(index: int) -> None:
        nonlocal active, peak
        async with scheduler.slot(kind="model", label=str(index)):
            active += 1
            peak = max(peak, active)
            started.append(index)
            await gate.wait()
            active -= 1

    tasks = [asyncio.create_task(worker(index)) for index in range(12)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert scheduler.snapshot()["running"] == 4
    assert scheduler.snapshot()["queued"] == 8
    assert started == [0, 1, 2, 3]
    gate.set()
    await asyncio.gather(*tasks)
    assert peak == 4
    assert started == list(range(12))


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_consume_capacity():
    scheduler = RuntimeScheduler(limit=1)
    holder = await scheduler.acquire(kind="tool", label="holder")
    blocked = asyncio.create_task(scheduler.acquire(kind="tool", label="cancel"))
    await asyncio.sleep(0)
    blocked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked
    assert scheduler.snapshot()["queued"] == 0
    await scheduler.release(holder)
    assert scheduler.snapshot()["running"] == 0


@pytest.mark.asyncio
async def test_agent_execution_clock_does_not_start_while_waiting_for_slot():
    scheduler = RuntimeScheduler(limit=1)
    holder = await scheduler.acquire(kind="model", agent_id="other")
    pending = asyncio.create_task(scheduler.acquire(kind="model", agent_id="child"))
    await asyncio.sleep(0)
    assert scheduler.queue_position(agent_id="child") == 1
    assert scheduler.agent_runtime("child") == {
        "started": False, "active": False, "active_slots": 0, "elapsed": 0.0,
    }
    await scheduler.release(holder)
    admitted = await pending
    assert scheduler.agent_runtime("child")["started"] is True
    assert scheduler.agent_runtime("child")["active"] is True
    assert scheduler.agent_runtime("child")["active_slots"] == 1
    await asyncio.sleep(0.01)
    active = scheduler.agent_runtime("child")["elapsed"]
    assert active > 0
    await scheduler.release(admitted)
    frozen = scheduler.agent_runtime("child")["elapsed"]
    assert scheduler.agent_runtime("child")["active"] is False
    assert scheduler.agent_runtime("child")["active_slots"] == 0
    await asyncio.sleep(0.01)
    assert scheduler.agent_runtime("child")["elapsed"] == frozen


@pytest.mark.asyncio
async def test_agent_runtime_reports_concurrent_slots_for_one_agent():
    scheduler = RuntimeScheduler(limit=2)
    model_lease = await scheduler.acquire(kind="model", agent_id="child")
    tool_lease = await scheduler.acquire(kind="tool", agent_id="child")

    runtime = scheduler.agent_runtime("child")
    assert runtime["active"] is True
    assert runtime["active_slots"] == 2

    await scheduler.release(model_lease)
    assert scheduler.agent_runtime("child")["active_slots"] == 1
    await scheduler.release(tool_lease)
    assert scheduler.agent_runtime("child")["active_slots"] == 0


@pytest.mark.asyncio
async def test_background_work_never_jumps_normal_waiters():
    scheduler = RuntimeScheduler(limit=1)
    holder = await scheduler.acquire(kind="model")
    order: list[str] = []

    async def wait(name: str, priority: int) -> None:
        async with scheduler.slot(kind="usage", label=name, priority=priority):
            order.append(name)

    background = asyncio.create_task(wait("usage", 10))
    await asyncio.sleep(0)
    normal = asyncio.create_task(wait("chat", 0))
    await asyncio.sleep(0)
    await scheduler.release(holder)
    await asyncio.gather(background, normal)
    assert order == ["chat", "usage"]


@pytest.mark.asyncio
async def test_kind_limits_prevent_model_and_tool_starvation():
    scheduler = RuntimeScheduler(
        limit=4,
        kind_limits={"model": 2, "tool": 2},
    )
    first_model = await scheduler.acquire(kind="model", label="model-1")
    second_model = await scheduler.acquire(kind="model", label="model-2")
    queued_model = asyncio.create_task(
        scheduler.acquire(kind="model", label="model-3")
    )
    await asyncio.sleep(0)

    tool = await asyncio.wait_for(
        scheduler.acquire(kind="tool", label="tool-1"), timeout=1
    )
    snapshot = scheduler.snapshot()
    assert snapshot["running_by_kind"] == {"model": 2, "tool": 1}
    assert snapshot["kind_limits"] == {"model": 2, "tool": 2}
    assert not queued_model.done()

    await scheduler.release(first_model)
    admitted_model = await asyncio.wait_for(queued_model, timeout=1)
    await scheduler.release(second_model)
    await scheduler.release(tool)
    await scheduler.release(admitted_model)


class _NoParams(BaseModel):
    pass


@pytest.mark.asyncio
async def test_model_and_tool_leaf_work_share_one_four_slot_limit(monkeypatch):
    scheduler = RuntimeScheduler(limit=4)
    monkeypatch.setattr(client_module, "GLOBAL_RUNTIME_SCHEDULER", scheduler)
    monkeypatch.setattr(tool_base_module, "GLOBAL_RUNTIME_SCHEDULER", scheduler)
    gate = asyncio.Event()
    active = 0
    peak = 0

    async def block() -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await gate.wait()
        finally:
            active -= 1

    class Client:
        async def stream(self, _conversation, system="", tools=None):
            await block()
            yield StreamEnd(stop_reason="end")

    class BlockingTool(Tool):
        name = "Blocking"
        description = "test"
        params_model = _NoParams

        async def execute(self, _params):
            await block()
            return ToolResult(output="ok")

    async def model_work() -> None:
        async for _ in scheduled_client_stream(Client(), ConversationManager()):
            pass

    tool = BlockingTool()
    tasks = [asyncio.create_task(model_work()) for _ in range(6)] + [
        asyncio.create_task(tool.run(_NoParams())) for _ in range(6)
    ]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert scheduler.snapshot()["running"] == 4
    assert scheduler.snapshot()["queued"] == 8
    assert active == 4
    gate.set()
    await asyncio.gather(*tasks)
    assert peak == 4
    assert scheduler.snapshot()["running"] == 0


@pytest.mark.asyncio
async def test_harness_does_not_hold_a_slot_while_waiting_for_child_model(monkeypatch):
    scheduler = RuntimeScheduler(limit=1)
    monkeypatch.setattr(client_module, "GLOBAL_RUNTIME_SCHEDULER", scheduler)
    monkeypatch.setattr(tool_base_module, "GLOBAL_RUNTIME_SCHEDULER", scheduler)

    class Client:
        async def stream(self, _conversation, system="", tools=None):
            yield StreamEnd(stop_reason="end")

    class Harness(Tool):
        name = "Harness"
        description = "test"
        params_model = _NoParams
        category = "harness"

        async def execute(self, _params):
            async for _ in scheduled_client_stream(Client(), ConversationManager()):
                pass
            return ToolResult(output="ok")

    result = await asyncio.wait_for(Harness().run(_NoParams()), timeout=1)
    assert result.output == "ok"
    assert scheduler.snapshot()["running"] == 0
