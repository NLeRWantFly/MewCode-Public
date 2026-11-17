"""Real pipe semantics and host task projection; no ACL or live-run changes."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from mewcode.agents.task_manager import TaskManager
from mewcode.sandbox import execution_progress as progress, lpac_runtime as lpac
from mewcode.tools.agent_status import BackgroundTasks, BackgroundParams
from tests.test_lpac_runtime import envelope, _FakeCancelEvent
from tests.test_native_workspace_queue import harness


class PipeProcess:
    pid = 1234

    def __init__(self):
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.stdin = self
        self.request = None
        self.fed = asyncio.Event()
        self.exited = asyncio.Event()
        self.returncode = None
        self.killed = False

    def write(self, payload):
        self.request = payload

    async def drain(self):
        pass

    def close(self):
        self.fed.set()

    async def wait(self):
        await self.exited.wait()
        return self.returncode

    def finish(self, *, clean=True):
        data = envelope()
        if not clean:
            data["receipt"]["job_cleanup_ok"] = False
        self.stdout.feed_data(json.dumps(data).encode())
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.returncode = 0
        self.exited.set()

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.exited.set()


async def start(monkeypatch, process, events, cancel=None):
    async def create(*args, **kwargs):
        return process
    monkeypatch.setattr(lpac.asyncio, "create_subprocess_exec", create)

    async def run():
        token = progress.observer.set(events.append)
        try:
            with progress.operation():
                return await lpac.invoke_helper(Path("synthetic-helper.exe"),
                    {"request_id": "id", "timeout_ms": 10000}, cancel or _FakeCancelEvent())
        finally:
            progress.observer.reset(token)

    task = asyncio.create_task(run())
    await asyncio.wait_for(process.fed.wait(), 2)
    return task


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_fragmented_startup_is_visible_before_final_output(monkeypatch):
    process, events = PipeProcess(), []
    task = await start(monkeypatch, process, events)
    try:
        assert events[-1]["phase"] == "starting"
        assert events[-1]["process_started"] is False
        process.stderr.feed_data(b"MEWCODE_CHILD_")
        await asyncio.sleep(0)
        assert not any(e["process_started"] for e in events)
        process.stderr.feed_data(b"PID=4567\r\n")
        await until(lambda: events[-1]["phase"] == "running")
        event = events[-1]
        assert not task.done()
        assert event["child_pid"] == 4567 and event["helper_pid"] == 1234
        assert event["request_id"] == "id"
        assert event["request_sha256"] == hashlib.sha256(process.request).hexdigest()
        assert event["start_observed_at"]
        process.stderr.feed_data(b"MEWCODE_CHILD_PID=9876\n")
        await asyncio.sleep(0)
        assert events[-1]["child_pid"] == 4567
    finally:
        process.finish()
        result = await task
    assert result.exit_code == 0
    assert events[-1]["phase"] == "finished" and events[-1]["cleanup_confirmed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [b"MEWCODE_CHILD_PID=0\n", b"MEWCODE_CHILD_PID=-1\n",
    b"MEWCODE_CHILD_PID=99999999999\n", b"prefix MEWCODE_CHILD_PID=45\n",
    b"MEWCODE_CHILD_PID=45", b"x" * 8000 + b"MEWCODE_CHILD_PID=45\n"])
async def test_unrecognized_helper_text_is_not_start_evidence(monkeypatch, raw):
    process, events = PipeProcess(), []
    task = await start(monkeypatch, process, events)
    process.stderr.feed_data(raw)
    process.finish()
    await task
    assert not any(e["process_started"] for e in events)


@pytest.mark.asyncio
async def test_child_output_marker_is_not_start_evidence(monkeypatch):
    process, events = PipeProcess(), []
    task = await start(monkeypatch, process, events)
    data = envelope()
    data["receipt"]["stdout"] = "MEWCODE_CHILD_PID=999\n"
    data["receipt"]["stderr"] = "MEWCODE_CHILD_PID=888\n"
    process.stdout.feed_data(json.dumps(data).encode())
    process.stdout.feed_eof()
    process.stderr.feed_eof()
    process.returncode = 0
    process.exited.set()
    await task
    assert not any(e["process_started"] for e in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("clean", [True, False])
async def test_cancel_waits_for_cleanup_without_claiming_early_success(monkeypatch, clean):
    process, events, cancel = PipeProcess(), [], _FakeCancelEvent()
    task = await start(monkeypatch, process, events, cancel)
    process.stderr.feed_data(b"MEWCODE_CHILD_PID=4567\n")
    await until(lambda: events[-1]["phase"] == "running")
    task.cancel()
    await asyncio.wait_for(cancel.signalled.wait(), 2)
    assert events[-1]["phase"] == "cleaning"
    assert events[-1]["cleanup_confirmed"] is None
    assert not task.done()
    process.finish(clean=clean)
    with pytest.raises(asyncio.CancelledError if clean else RuntimeError):
        await task
    assert events[-1]["phase"] == ("finished" if clean else "cleanup_unknown")
    assert events[-1]["cleanup_confirmed"] is clean


@pytest.mark.asyncio
async def test_real_subprocess_stderr_is_streamed_before_exit():
    # A synthetic Python helper exercises actual OS pipes, not the native boundary.
    script = ("import sys,time; sys.stdin.buffer.read(); "
              "sys.stderr.write('MEWCODE_CHILD_PID=12345\\n'); sys.stderr.flush(); "
              "time.sleep(0.5); sys.stdout.write('final')")
    child = await asyncio.create_subprocess_exec(sys.executable, "-c", script,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    events = []
    token = progress.observer.set(events.append)
    try:
        with progress.operation():
            task = asyncio.create_task(lpac._capture_helper_progress(child, b"request", "id"))
            await until(lambda: any(e.get("process_started") for e in events))
            assert not task.done()
            assert (await task)[0] == b"final"
            assert child.returncode == 0
    finally:
        progress.observer.reset(token)
        if child.returncode is None:
            child.kill()
        await child.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("registered", [False, True])
async def test_task_tool_sidebar_and_panel_use_same_progress_and_parent(tmp_path, registered):
    from frontend import server

    class Agent:
        agent_id = "child"
        team_name = ""
        total_input_tokens = total_output_tokens = total_tool_calls = 0
        runtime_scheduler = None
        async def run_to_completion(self, *args, **kwargs):
            with progress.operation():
                progress.emit("running", process_started=True, child_pid=4567,
                              request_id="real-host-request")
                started.set()
                await release.wait()
                progress.emit("finished", cleanup_confirmed=True)
            return "done"

    manager, agent = TaskManager(tmp_path / "tasks.json"), Agent()
    started, release = asyncio.Event(), asyncio.Event()
    task_id = manager.launch(agent, "synthetic", owner_task_id="actual-parent",
                            owner_session_id="session", owner_store_dir=str(tmp_path))
    session = SimpleNamespace(task_manager=manager, session=SimpleNamespace(session_id="session"),
        scope="project", _conversation_store_dir=lambda: str(tmp_path), _agent_tool=None)
    if registered:
        session._agent_tool = SimpleNamespace(runs={"child": dict(agent=agent, name="child",
            status="running", start=0, end=None, task_id=task_id, mode="bg", agent_type="test", task="synthetic",
            owner_session_id="session", owner_store_dir=str(tmp_path),
            owner_task_id="stale-parent", owner_task_title="stale", team="")})
    try:
        await asyncio.wait_for(started.wait(), 2)
        caller = SimpleNamespace(session_id="session", work_dir=str(tmp_path))
        tool = await BackgroundTasks(caller, manager).execute(BackgroundParams(task_id=task_id))
        detail = json.loads(tool.output)
        sidebar = server.WebSession.tasks_payload(session)[0]
        panel = server.WebSession.agent_tasks_payload(session)
        row = panel["groups"][0]["rows"][0]
        for payload in (detail, sidebar, row):
            assert payload["parent_task_id"] == "actual-parent"
            assert payload["native_execution"]["child_pid"] == 4567
            assert payload["native_execution"]["phase"] == "running"
        assert panel["groups"][0]["key"] == "task:actual-parent"
        persisted = TaskManager(tmp_path / "tasks.json").task_payload(task_id)
        assert persisted["native_execution"]["phase"] == "interrupted"
        assert persisted["native_execution"]["cleanup_confirmed"] is None
    finally:
        release.set()
        await manager._async_tasks[task_id]
    assert manager.task_payload(task_id)["native_execution"]["phase"] == "finished"


@pytest.mark.asyncio
async def test_parallel_operations_do_not_share_start_facts():
    events_a, events_b = [], []
    entered, release = asyncio.Event(), asyncio.Event()

    async def run(events, started):
        token = progress.observer.set(events.append)
        try:
            with progress.operation():
                if started:
                    progress.emit("running", process_started=True, child_pid=123)
                    entered.set()
                await release.wait()
        finally:
            progress.observer.reset(token)

    a = asyncio.create_task(run(events_a, True))
    await entered.wait()
    b = asyncio.create_task(run(events_b, False))
    await asyncio.sleep(0)
    assert events_a[-1]["process_started"] is True
    assert events_b[-1]["process_started"] is False and "child_pid" not in events_b[-1]
    assert events_a[-1]["execution_id"] != events_b[-1]["execution_id"]
    release.set()
    await asyncio.gather(a, b)


@pytest.mark.asyncio
async def test_queued_native_operation_does_not_claim_process_start(harness):
    h = harness
    events = {"A": [], "B": []}

    async def run(name):
        token = progress.observer.set(events[name].append)
        try:
            return await h.run(name)
        finally:
            progress.observer.reset(token)

    a = asyncio.create_task(run("A"))
    await h.entered.wait()
    b = asyncio.create_task(run("B"))
    try:
        await until(lambda: bool(events["B"]))
        assert events["B"][-1]["phase"] == "queued"
        assert events["B"][-1]["process_started"] is False
        assert h.calls == ["A"]
    finally:
        h.release.set()
        await asyncio.gather(a, b)
    assert h.calls == ["A", "B"]
    # Successful mocked execution without a helper marker must stay unconfirmed.
    assert not any(e["process_started"] for e in events["B"])


@pytest.mark.asyncio
async def test_marker_flows_through_real_task_manager_without_sibling_leak(tmp_path, monkeypatch):
    processes = {"A": PipeProcess(), "B": PipeProcess()}
    cancels = {"A": _FakeCancelEvent(), "B": _FakeCancelEvent()}

    async def create(helper, *args, **kwargs):
        return processes[helper]
    monkeypatch.setattr(lpac.asyncio, "create_subprocess_exec", create)

    class Agent:
        team_name = ""
        total_input_tokens = total_output_tokens = total_tool_calls = 0
        runtime_scheduler = None
        def __init__(self, name):
            self.agent_id = name
        async def run_to_completion(self, *args, **kwargs):
            with progress.operation():
                result = await lpac.invoke_helper(Path(self.agent_id),
                    {"request_id": "id", "timeout_ms": 10000}, cancels[self.agent_id])
            return result.render()

    manager = TaskManager(tmp_path / "tasks.json")
    a = manager.launch(Agent("A"), "wait", owner_task_id="parent-A")
    b = manager.launch(Agent("B"), "wait", owner_task_id="parent-B")
    handles = list(manager._async_tasks.values())
    try:
        await asyncio.wait_for(asyncio.gather(*(p.fed.wait() for p in processes.values())), 2)
        processes["A"].stderr.feed_data(b"MEWCODE_CHILD_PID=5678\n")
        await until(lambda: manager.task_payload(a)["native_execution"].get("process_started"))
        assert manager.task_payload(a)["native_execution"]["child_pid"] == 5678
        assert manager.task_payload(b)["native_execution"]["process_started"] is False
        assert manager.task_payload(b)["parent_task_id"] == "parent-B"
        assert manager.cancel(a)
        await asyncio.wait_for(cancels["A"].signalled.wait(), 2)
        assert manager.task_payload(a)["native_execution"]["phase"] == "cleaning"
        assert manager.task_payload(b)["native_execution"]["phase"] == "starting"
        assert not cancels["B"].signalled.is_set()
    finally:
        for process in processes.values():
            process.finish()
        await asyncio.gather(*handles)
    assert manager.task_payload(a)["status"] == "cancelled"
    assert manager.task_payload(b)["status"] == "completed"
