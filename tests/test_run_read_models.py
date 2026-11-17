import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from frontend import server
from frontend.workbench_notify import WorkbenchNotifier
from mewcode.expert_runs import ExpertRunStore
from mewcode.kernel.events import EventJournal
from mewcode.kernel.sqlite_state import RunDatabase
from mewcode.orchestration.task_design import TaskDesign, execution_scope_error
from tests.test_task_design_entry import task_api_env


RUN = 'f' * 32


def sql_run(root, cid='c'):
    store = ExpertRunStore(root, storage='sqlite')
    store.create(run_id=RUN, conversation_id=cid, task='task', attachments=[], profile={}, strategy='stable')
    return store


def forbidden(*args, **kwargs):
    raise AssertionError('query loaded full execution state')


@pytest.mark.parametrize('storage', ['legacy', 'sqlite'])
def test_deleted_conversation_leaves_no_query_rows(tmp_path, storage):
    store = ExpertRunStore(tmp_path, storage=storage)
    for run_id, cid in ((RUN, 'removed'), ('a' * 32, 'retained')):
        store.create(run_id=run_id, conversation_id=cid, task='task', attachments=[], profile={}, strategy='stable')
    assert store.query_summaries('removed')
    assert store.delete_conversation('removed') == 1
    assert store.query_summaries('removed') == []
    assert len(store.query_summaries('retained')) == 1
    assert store.load(RUN) is None
    with RunDatabase(tmp_path, RUN).connection() as db:
        for table in ('history_index', 'cells', 'transactions', 'migrations', 'run_views', 'runs'):
            assert db.execute('SELECT count(*) FROM ' + table + ' WHERE run_id=?', (RUN,)).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_workbench_api_and_notify_never_load_full_execution_state(task_api_env, tmp_path, monkeypatch):
    _, _, _, owner, _ = task_api_env
    store = sql_run(tmp_path / 'runs', owner['conversation_id'])
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    monkeypatch.setattr(store, 'load', forbidden)
    monkeypatch.setattr(store, '_read_disk', forbidden)
    monkeypatch.setattr(store, 'list_for_conversation', forbidden)
    monkeypatch.setattr(EventJournal, 'load', forbidden)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        for path in ('/api/workbench/view', '/api/workbench/tasks', '/api/expert-runs/' + RUN,
                     '/api/expert-runs/' + RUN + '/history'):
            response = await client.get(path, params=dict(owner, run_id=RUN))
            assert response.status_code == 200, response.text
    notifier = WorkbenchNotifier(server.WORKBENCH)
    try:
        event = dict(kind='run', conversation_id=owner['conversation_id'], run_id=RUN,
                     task_id='run:' + RUN, version='run:1')
        monkeypatch.setattr(store, 'read_workbench', forbidden)
        assert notifier.read(event)[0]['version'] == 'run:1'
    finally:
        notifier.close()


@pytest.mark.asyncio
async def test_old_paused_workbench_read_is_normalized_without_loading_or_writing_run(task_api_env, tmp_path, monkeypatch):
    from mewcode.kernel import run_views
    from mewcode.kernel.events import digest
    _, _, _, owner, _ = task_api_env
    store = sql_run(tmp_path / 'runs', owner['conversation_id'])
    store.mutate(RUN, lambda row: row.update(status='paused'), durable=True)
    cached = store.read_workbench(RUN)
    # Simulate a view materialized before paused-node presentation was added.
    cached['_workbench_view']['task'].pop('execution_status', None)
    cached['_workbench_view']['plan'] = [{'id': 'g', 'status': '进行中', 'steps': [
        {'id': 'n', 'name': '执行节点', 'status': '进行中'}]}]
    cached['_workbench_view']['structure']['nodes'] = [{'id': 'n', 'status': '进行中'}]
    before, cache_before = digest(store.load(RUN)), digest(cached)
    with monkeypatch.context() as guarded:
        guarded.setattr(server, 'EXPERT_RUNS', store)
        guarded.setattr(store, 'read_workbench', lambda run_id: copy.deepcopy(cached))
        guarded.setattr(store, 'load', forbidden)
        guarded.setattr(store, '_persist', forbidden)
        guarded.setattr(store, 'mutate', forbidden)
        guarded.setattr(run_views, 'build', forbidden)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
            response = await client.get('/api/workbench/view', params={**owner, 'run_id': RUN})
            assert response.status_code == 200, response.text
            view = response.json()
            assert view['plan'][0]['steps'][0]['status'] == '已暂停'
            assert view['structure']['nodes'][0]['status'] == '已暂停'
    assert digest(store.load(RUN)) == before and digest(cached) == cache_before


def test_history_page_reads_only_selected_receipts_and_retains_older_window(tmp_path, monkeypatch):
    store = sql_run(tmp_path)
    store.append_sequenced_events(RUN, [dict(seq=i + 1, type='expert_step', node_id='n', label=str(i))
                                        for i in range(150)], return_snapshot=False)
    observed = []
    original = RunDatabase._read
    def read(self, db, space, parts=()):
        observed.append((space, parts))
        return original(self, db, space, parts)
    monkeypatch.setattr(RunDatabase, '_read', read)
    first = store.history_page(RUN, limit=25)
    assert [row['seq'] for row in first['items']] == list(range(1, 26))
    assert first['next_cursor'] == 25
    assert len(observed) == 25 and all(parts[0] == 'events' for _, parts in observed)
    second = store.history_page(RUN, after=first['next_cursor'], limit=25)
    assert second['items'][0]['seq'] == 26
    assert not set(row['seq'] for row in first['items']) & set(row['seq'] for row in second['items'])


def test_sqlite_run_does_not_write_full_checkpoints_or_legacy_events(tmp_path, monkeypatch):
    store = sql_run(tmp_path)
    monkeypatch.setattr(store, '_persist', forbidden)
    monkeypatch.setattr(store, '_append_event_log', forbidden)
    store.append_event(RUN, dict(type='expert_step', label='next'))
    store.mutate(RUN, lambda value: value.update(status_reason='updated'), durable=True)
    assert not list(tmp_path.glob('*.json'))
    assert not list((tmp_path / 'kernel-events' / RUN).glob('[0-9]*.json'))
    assert ExpertRunStore(tmp_path).load(RUN)['status_reason'] == 'updated'


@pytest.mark.parametrize('text', ['只证明验证设计已经保存', '技术说明：只证明设计已经保存，不代表业务完成。',
                                  '说明中写着“不要执行”，这是引用的错误文本。'])
def test_technical_prose_does_not_revoke_user_execution_choice(tmp_path, text):
    service = TaskDesign(tmp_path, 'c')
    before = service.start('execute')
    after = service.record_turn(text)
    assert after['intent_authority'] == before['intent_authority']
    assert execution_scope_error(after) == ''


@pytest.mark.asyncio
async def test_cancel_waits_for_durable_worker_and_loop_stays_responsive():
    import threading
    from mewcode.kernel.async_commit import durable_call
    entered, release, committed = threading.Event(), threading.Event(), threading.Event()
    def write():
        entered.set()
        assert release.wait(5)
        committed.set()
    task = asyncio.create_task(durable_call(write))
    await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(.01)
    assert not task.done() and not committed.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert committed.is_set()
