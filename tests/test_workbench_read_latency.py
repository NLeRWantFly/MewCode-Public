"""Workbench reads must not occupy the host's tool-dispatch loop."""
import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest

from frontend import server
from frontend.workbench_api import Workbench
from tests.test_task_design_entry import task_api_env


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['view', 'tasks'])
async def test_slow_display_read_keeps_host_loop_responsive(task_api_env, monkeypatch, method):
    _, _, _, owner, _ = task_api_env
    started, release = threading.Event(), threading.Event()
    owner_thread, reader_threads = threading.get_ident(), []
    def read(values):
        reader_threads.append(threading.get_ident())
        assert values['project'] == owner['project']
        started.set()
        assert release.wait(timeout=3)
        return {'unchanged': 'display result'}
    monkeypatch.setattr(server.WORKBENCH, method, read)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        pending = asyncio.create_task(client.get('/api/workbench/'+method, params=owner))
        try:
            assert await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=2)
            assert not pending.done(), 'A blocked display read must leave the host loop runnable'
            assert reader_threads == [reader_threads[0]] and reader_threads[0] != owner_thread
        finally:
            release.set()
            result = await asyncio.wait_for(pending, timeout=3)
    assert result.status_code == 200 and result.json() == {'unchanged': 'display result'}
    assert result.headers['cache-control'] == 'no-store'


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['view', 'tasks'])
async def test_switch_during_display_read_cannot_publish_old_conversation(task_api_env, monkeypatch, method):
    _, _, _, owner, _ = task_api_env
    current = [owner['conversation_id']]
    monkeypatch.setattr(server, '_active_conversation_id', lambda: current[0])
    started, release = threading.Event(), threading.Event()
    def read(values):
        started.set()
        assert release.wait(timeout=3)
        return {'private': 'previous conversation content'}
    monkeypatch.setattr(server.WORKBENCH, method, read)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        pending = asyncio.create_task(client.get('/api/workbench/'+method, params=owner))
        try:
            assert await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=2)
            current[0] = 'another-conversation'
        finally:
            release.set()
            result = await asyncio.wait_for(pending, timeout=3)
    assert result.status_code == 409
    assert 'previous conversation content' not in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['view', 'tasks'])
async def test_team_display_keeps_connection_owner_thread(task_api_env, monkeypatch, method):
    _, _, _, owner, _ = task_api_env
    owner_thread = threading.get_ident()
    threads = []
    monkeypatch.setattr(server.WORKBENCH, 'team_host', lambda *args: object())
    def read(values):
        threads.append(threading.get_ident())
        return {'team': True}
    monkeypatch.setattr(server.WORKBENCH, method, read)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        result = await client.get('/api/workbench/'+method, params=owner)
    assert result.status_code == 200 and threads == [owner_thread]


@pytest.mark.parametrize('record', [None, {'conversation_id': 'other'}, {'conversation_id': 'current', 'run_id': 'bound'}])
def test_default_page_reads_only_its_bound_run(record):
    reads = []
    def selected(run_id):
        reads.append(run_id)
        return record
    def forbidden(*args):
        raise AssertionError('Bound page enumerated historical runs')
    wb = Workbench(SimpleNamespace(EXPERT_RUNS=SimpleNamespace(
        read_workbench=selected, query_summaries=forbidden)))
    context = ('project', 'current', {}, {'task_id': 'task', 'run_id': 'bound'})
    wb.context = lambda values: context
    result = wb.select({}, read_only=True)
    assert result[:4] == context and reads == ['bound']
    assert result[-1] == (record if record and record['conversation_id'] == 'current' else None)


def test_explicit_task_selection_keeps_existing_history_lookup():
    record = {'run_id': 'historical', '_workbench_view': {'task': {'id': 'selected'}}}
    wb = Workbench(SimpleNamespace())
    wb.context = lambda values: ('project', 'current', {}, {'task_id': 'new', 'run_id': 'bound'})
    wb.records = lambda cid: [record]
    assert wb.select({'task_id': 'selected'}, read_only=True)[-1] is record


def test_task_list_does_not_load_chat_history():
    reads = []
    def read(run_id):
        reads.append(run_id)
        return {'run_id': run_id}
    wb = Workbench(SimpleNamespace(EXPERT_RUNS=SimpleNamespace(
        query_run_ids=lambda cid: ['first', 'second'], read_workbench=read)))
    assert wb.records('current') == [{'run_id': 'first'}, {'run_id': 'second'}]
    assert reads == ['first', 'second']
