"""Admission tests use synthetic workers only: no native helper or ACL changes."""
import asyncio
from types import SimpleNamespace

import pytest

from mewcode.runtime_scheduler import (
    RuntimeScheduler, ResourceQueueTimeout, bind_runtime_scheduler, reset_runtime_scheduler,
)
import mewcode.runtime_scheduler as clocks
from mewcode.sandbox import lpac_runtime as lpac, staged_execution, windows
from mewcode.sandbox.receipt import NativeExecutionReceipt
from mewcode.usage.service import usage_trace_scope


@pytest.fixture
def harness(tmp_path, monkeypatch):
    scheduler = RuntimeScheduler(limit=2, kind_limits={"tool": 2})
    token = bind_runtime_scheduler(scheduler)
    now = [100.0]
    monkeypatch.setattr(clocks, "time", SimpleNamespace(monotonic=lambda: now[0]))
    scope = SimpleNamespace(workspace=tmp_path.resolve(), current=lambda: True)
    monkeypatch.setattr(windows, "native_runtime_ready", lambda: True)
    monkeypatch.setattr(lpac, "find_runtime_helper", lambda **_: tmp_path / "unused.exe")
    entered, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls, reconciles = [], []
    active = [False]
    outcome = [[]]

    def reconcile(_scope):
        assert not active[0], "normal active execution must not enter recovery"
        reconciles.append(True)
        return outcome[0]

    async def execute(command, **kwargs):
        assert not active[0]
        active[0] = True
        calls.append(command)
        try:
            if command == "A":
                entered.set()
                await release.wait()
        finally:
            active[0] = False
            cleaned.set()
        return NativeExecutionReceipt("exited", exit_code=0, cleanup_confirmed=True)

    monkeypatch.setattr(staged_execution, "reconcile", reconcile)
    monkeypatch.setattr(staged_execution, "execute_staged", execute)

    async def run(name):
        with usage_trace_scope(agent_id=name, session_id="fixture"):
            # Production Tool.run owns an ordinary permit before native routing.
            async with scheduler.slot(kind="tool", label="Bash", agent_id=name):
                return await lpac.execute_lpac(name, scope=scope, work_dir=str(tmp_path), timeout=2)

    yield SimpleNamespace(**locals())
    reset_runtime_scheduler(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_first", [False, True])
async def test_native_sibling_queues_before_reconcile_and_runs_once_after_cleanup(harness, cancel_first):
    h = harness
    a = asyncio.create_task(h.run("A"))
    await h.entered.wait()
    b = asyncio.create_task(h.run("B"))
    try:
        await asyncio.sleep(0)
        assert h.scheduler.queue_position(agent_id="B") == 1
        assert not h.scheduler.agent_runtime("B")["active"]
        h.now[0] += 30
        assert h.scheduler.agent_runtime("B")["elapsed"] == 0
        assert h.calls == ["A"] and len(h.reconciles) == 1
        # A queued sibling doesn't monopolize the remaining tool permit.
        async with h.scheduler.slot(kind="tool", agent_id="structured-read"):
            assert h.scheduler.agent_runtime("A")["active"]
        if cancel_first:
            a.cancel()
        else:
            h.release.set()
        await asyncio.gather(a, return_exceptions=True)
        assert h.cleaned.is_set()
        assert not (await asyncio.wait_for(b, 1)).is_error
        assert h.calls == ["A", "B"] and len(h.reconciles) == 2
        assert h.scheduler.snapshot()["running"] == h.scheduler.snapshot()["queued"] == 0
    finally:
        h.release.set()
        await asyncio.gather(a, b, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_queued_sibling_never_runs_or_recovers_owner(harness):
    h = harness
    a = asyncio.create_task(h.run("A"))
    await h.entered.wait()
    b = asyncio.create_task(h.run("B"))
    try:
        await asyncio.sleep(0)
        b.cancel()
        with pytest.raises(asyncio.CancelledError):
            await b
        assert not a.done() and h.scheduler.agent_runtime("A")["active"]
        assert h.calls == ["A"] and len(h.reconciles) == 1
        assert h.scheduler.queue_position(agent_id="B") == 0
    finally:
        h.release.set()
        await a


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["cleanup_unknown", "authority_changed", "gate_closed", "external_busy"])
async def test_queue_admission_rechecks_authority_gate_and_unsettled_outcome(harness, monkeypatch, change):
    h = harness
    a = asyncio.create_task(h.run("A"))
    await h.entered.wait()
    b = asyncio.create_task(h.run("B"))
    try:
        await asyncio.sleep(0)
        if change == "cleanup_unknown":
            h.outcome[0] = [{"state": "recovery_required"}]
        elif change == "external_busy":
            h.outcome[0] = [{"state": "busy"}]
        elif change == "authority_changed":
            h.scope.current = lambda: False
        else:
            monkeypatch.setattr(windows, "native_runtime_ready", lambda: False)
            monkeypatch.setattr("mewcode.sandbox.native_gate.native_gate_status",
                lambda: SimpleNamespace(state=SimpleNamespace(value="disabled"), reason="test"))
        h.release.set()
        await a
        result = await b
        assert result.is_error and h.calls == ["A"]
        if change == "external_busy":
            assert "busy" in result.detail and "reconciliation" not in result.detail
        if change == "cleanup_unknown":
            assert "reconciliation" in result.detail
    finally:
        h.release.set()
        await asyncio.gather(a, b, return_exceptions=True)


@pytest.mark.asyncio
async def test_resource_queue_timeout_does_not_leak_or_start_work():
    scheduler = RuntimeScheduler(limit=2)
    holder = await scheduler.acquire(kind="tool", resource_key="workspace")
    try:
        async with scheduler.slot(kind="tool", agent_id="waiting"):
            with pytest.raises(ResourceQueueTimeout):
                async with scheduler.serialized("workspace", wait_timeout=0.01):
                    pytest.fail("queued command must not start")
        assert scheduler.snapshot()["queued"] == 0
        assert scheduler.snapshot()["running"] == 1
    finally:
        await scheduler.release(holder)


@pytest.mark.asyncio
async def test_direct_calls_and_inherited_child_context_do_not_borrow_parent_permit():
    scheduler = RuntimeScheduler(limit=2)
    async with scheduler.slot(kind="tool", agent_id="parent"):
        async def child():
            async with scheduler.serialized("workspace", agent_id="child"):
                assert scheduler.agent_runtime("parent")["active"]
                assert scheduler.agent_runtime("child")["active"]
        await asyncio.create_task(child())
        assert scheduler.agent_runtime("parent")["active"]
        assert not scheduler.agent_runtime("child")["active"]
    assert scheduler.snapshot()["running"] == 0


@pytest.mark.asyncio
async def test_workspace_fifo_does_not_block_unrelated_workspace():
    scheduler = RuntimeScheduler(limit=4)
    holder = await scheduler.acquire(kind="tool", resource_key="one")
    order = []
    async def worker(name, key):
        async with scheduler.serialized(key, agent_id=name):
            order.append(name)
    b = asyncio.create_task(worker("B", "one"))
    c = asyncio.create_task(worker("C", "one"))
    d = asyncio.create_task(worker("D", "two"))
    try:
        await asyncio.wait_for(d, 1)
        assert order == ["D"]
        assert scheduler.queue_position(agent_id="B") < scheduler.queue_position(agent_id="C")
        await scheduler.release(holder)
        await asyncio.gather(b, c)
        assert order == ["D", "B", "C"]
    finally:
        await scheduler.release(holder)
        await asyncio.gather(b, c, d, return_exceptions=True)


@pytest.mark.parametrize("winerror, expected", [(5, "recovery_required"), (32, "busy"), (33, "busy")])
def test_only_exclusive_claim_conflict_is_busy_not_access_denial(tmp_path, monkeypatch, winerror, expected):
    from mewcode.sandbox.transaction_registry import TransactionRegistry
    registry = TransactionRegistry(tmp_path / "private")
    scope = SimpleNamespace(workspace=tmp_path, task_id="task")
    record = registry.begin(tmp_path, task_id="task", policy_revision="test")
    def denied(*args, **kwargs):
        error = OSError("synthetic claim error")
        error.winerror = winerror
        raise error
    monkeypatch.setattr("mewcode.sandbox.publication_io.PinnedPath", denied)
    result = staged_execution.reconcile(scope, registry)
    assert result[0]["state"] == expected
    assert registry.get(record["id"])["state"] == "preparing"
