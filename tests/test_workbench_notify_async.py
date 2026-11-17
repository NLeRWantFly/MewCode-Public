"""Slow display projections must not stall the host or accumulate refreshes."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from frontend import server
from frontend.workbench_notify import Coalescer, WorkbenchNotifier
from tests.test_collaboration_mode import RUN_ID, store_for
from tests.test_task_design_entry import task_api_env


def timer_recorder():
    callbacks = []
    def later(delay, callback):
        handle = SimpleNamespace(cancelled=False)
        handle.cancel = lambda: setattr(handle, 'cancelled', True)
        callbacks.append((callback, handle))
        return handle
    return callbacks, later


def notifier_for_reader(reader):
    class DisplayNotifier(WorkbenchNotifier):
        def read(self, event):
            return reader(event)
    send = AsyncMock()
    notifier = DisplayNotifier(SimpleNamespace(srv=SimpleNamespace(WS_EVENTS=SimpleNamespace(send_json=send))))
    _, notifier.coalescer.call_later = timer_recorder()
    return notifier, send


@pytest.mark.asyncio
async def test_run_projection_keeps_the_event_loop_responsive():
    started, release = threading.Event(), threading.Event()
    owner_thread = threading.get_ident()
    reader_thread = []
    def read(event):
        reader_thread.append(threading.get_ident())
        started.set()
        release.wait(timeout=2)
        return [dict(conversation_id='c', task_id='t', run_id='r', version='current')]
    notifier, send = notifier_for_reader(read)
    notifier.coalescer.touch('task', {'kind': 'run'})
    task = asyncio.create_task(notifier.coalescer.flush('task'))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=2)
        assert not task.done(), 'The host loop must run while the display read is still blocked'
        assert len(reader_thread) == 1 and reader_thread[0] != owner_thread
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=3)
        notifier.close()
    assert send.call_args.args[0]['version'] == 'current'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['team', 'draft'])
async def test_non_run_projection_keeps_its_owner_thread(kind):
    owner_thread = threading.get_ident()
    threads = []
    def read(event):
        threads.append(threading.get_ident())
        return [dict(conversation_id='c', task_id='t', run_id='r', version=event['kind'])]
    notifier, send = notifier_for_reader(read)
    try:
        notifier.coalescer.touch('task', {'kind': kind})
        await notifier.coalescer.flush('task')
        assert threads == [owner_thread] and send.call_args.args[0]['version'] == kind
    finally:
        notifier.close()


@pytest.mark.asyncio
async def test_inflight_refresh_keeps_only_the_latest_pending_change():
    callbacks, later = timer_recorder()
    coroutines, reads, gates, tasks, sent = [], [], {}, [], []
    def read(event):
        version = event['version']
        reads.append(version)
        return [dict(conversation_id='c', task_id='t', run_id='r', version=version)]
    async def send(payload):
        version = payload['version']
        gate = gates[version] = asyncio.get_running_loop().create_future()
        await gate
        sent.append(version)
    queue = Coalescer(read, send, call_later=later, spawn=coroutines.append)
    try:
        queue.touch('task', {'version': 'v1'})
        callbacks.pop(0)[0]()
        tasks.append(asyncio.create_task(coroutines.pop(0)))
        await asyncio.sleep(0)
        assert reads == ['v1']
        queue.touch('task', {'version': 'v2'})
        queue.touch('task', {'version': 'v3'})
        assert not callbacks, 'A running projection must not start a second read'
        gates['v1'].set_result(None)
        await tasks[0]
        assert len(callbacks) == 1
        callbacks.pop(0)[0]()
        tasks.append(asyncio.create_task(coroutines.pop(0)))
        await asyncio.sleep(0)
        assert reads == ['v1', 'v3']
        gates['v3'].set_result(None)
        await tasks[1]
        assert sent == ['v1', 'v3'] and not callbacks
    finally:
        queue.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_closed_refresh_cannot_publish_into_a_new_generation():
    callbacks, later = timer_recorder()
    release = asyncio.get_running_loop().create_future()
    sent = []
    async def read(event):
        if event['version'] == 'old':
            await release
        return [dict(conversation_id='c', task_id='t', run_id='r', version=event['version'])]
    async def send(payload):
        sent.append(payload['version'])
    queue = Coalescer(lambda event: [], send, async_read=read, call_later=later)
    queue.touch('task', {'version': 'old'})
    old = asyncio.create_task(queue.flush('task'))
    try:
        await asyncio.sleep(0)
        queue.close()
        queue.touch('task', {'version': 'new'})
        await queue.flush('task')
        release.set_result(None)
        await old
        assert sent == ['new']
        assert queue.versions == {('c', 't', 'r'): 'new'}
    finally:
        queue.close()
        old.cancel()
        await asyncio.gather(old, return_exceptions=True)


@pytest.mark.asyncio
async def test_threaded_run_refresh_matches_the_authoritative_workbench(task_api_env, tmp_path, monkeypatch):
    _, _, _, owner, _ = task_api_env
    store = store_for(tmp_path / 'run', selected='classic')
    cid = owner['conversation_id']
    store.mutate(RUN_ID, lambda record: record.update(conversation_id=cid), durable=True)
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    runtime = SimpleNamespace(expert_run_id=RUN_ID, conversation_id=cid, work_dir=owner['project'])
    monkeypatch.setattr(server.SESSION, 'runtime_for', lambda identity: runtime if identity == cid else None)
    notifier = WorkbenchNotifier(server.WORKBENCH)
    send = AsyncMock()
    notifier.coalescer.send = send
    notifier.attach_loop()
    try:
        notifier.schedule(dict(kind='run', root=str(store.root), conversation_id=cid, run_id=RUN_ID))
        await notifier.coalescer.flush((cid, RUN_ID))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
            view = (await client.get('/api/workbench/view', params=dict(owner, run_id=RUN_ID))).json()
        assert send.await_count == 1 and send.call_args.args[0]['version'] == view['version']
    finally:
        notifier.close()
