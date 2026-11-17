"""External transcript polling cannot stall the host or expose partial scans."""
import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest

from mewcode.external_agents import ExternalAgentStore, ExternalConversation, ExternalMessage


@pytest.mark.asyncio
async def test_watcher_scan_leaves_loop_free_and_stops_before_delivery(monkeypatch):
    store = ExternalAgentStore({'codex': []})
    entered, release, stop = threading.Event(), threading.Event(), asyncio.Event()
    def refresh():
        entered.set()
        assert release.wait(3)
        return [{'external_id': 'old'}]
    monkeypatch.setattr(store, 'refresh', refresh)
    callback = AsyncMock()
    task = asyncio.create_task(store.watch(callback, stop))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 3)
        stop.set()
    finally:
        release.set()
        await asyncio.wait_for(task, 3)
    callback.assert_not_called()


def test_refresh_publishes_complete_snapshot_and_serializes_scans(tmp_path, monkeypatch):
    store = ExternalAgentStore({'codex': [tmp_path]})
    path = tmp_path/'one.jsonl'
    path.write_text('{}', encoding='utf-8')
    entered, release = threading.Event(), threading.Event()
    old = ExternalConversation(source='codex', external_id='old', path='old', title='old')
    store._conversations[('codex', 'old')] = old
    published = store._conversations
    parsed = []
    def parse(_path):
        parsed.append(_path)
        entered.set()
        assert release.wait(3)
        return ExternalConversation(source='codex', external_id='new', path=str(path),
            title='new', messages=[ExternalMessage(id='u', role='user', text='new')])
    monkeypatch.setattr('mewcode.external_agents.parse_codex_transcript', parse)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(store.refresh)
        try:
            assert entered.wait(2)
            second = pool.submit(store.refresh)
            assert store.get('codex', 'new') is None
            assert [row['external_id'] for row in store.list()] == ['old']
        finally:
            release.set()
        assert len(first.result()) == 1
        assert second.result() == []
    assert len(parsed) == 1
    assert set(published) == {('codex', 'old')}
    assert store.get('codex', 'new')['title'] == 'new'


@pytest.mark.asyncio
@pytest.mark.parametrize('detail', [False, True])
async def test_external_read_api_scans_off_loop(monkeypatch, detail):
    from frontend import server
    store = ExternalAgentStore({'codex': []})
    entered, release = threading.Event(), threading.Event()
    owner = threading.get_ident()
    def refresh():
        assert threading.get_ident() != owner
        entered.set()
        assert release.wait(3)
        return []
    monkeypatch.setattr(store, 'refresh', refresh)
    monkeypatch.setattr(server, 'EXTERNAL_AGENTS', store)
    task = asyncio.create_task(server.api_external_agent_conversation('codex', 'missing')
        if detail else server.api_external_agents('codex'))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 3)
    finally:
        release.set()
        result = await asyncio.wait_for(task, 3)
    assert result.status_code == (404 if detail else 200)
    if not detail:
        assert json.loads(result.body) == {'items': []}
