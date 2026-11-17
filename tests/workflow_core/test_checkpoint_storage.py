import asyncio
import copy
import sqlite3

import pytest

from mewcode.expert_runs import ExpertRunStore
from mewcode.kernel.events import EventJournal, _SQL_CACHE
from mewcode.kernel.sqlite_state import RunDatabase
from mewcode.workflow_core.state import start_attempt, finish_attempt
from mewcode.workflow_core.runtime import Core
from mewcode.workflow_core.store import ensure
from mewcode.workflow_core.web import envelope
from tests.workflow_core.test_runtime import graph


def saved_run(tmp_path):
    store = ExpertRunStore(tmp_path / 'runs', storage='sqlite')
    rid = 'a' * 32
    flow = graph([dict(id=name, label=name, kind='work', task='处理数学材料') for name in ('archive', 'a', 'b')])
    store.create(run_id=rid, conversation_id='chat', task='行测章节', attachments=[],
                 profile={}, strategy='stable', flow=envelope(flow))
    backend = ensure(store, rid, flow)
    ids = {}
    def setup(state):
        state['status'] = 'running'
        for name in ('archive', 'a', 'b'):
            attempt = start_attempt(state, name, {}, [])
            ids[name] = attempt['id']
            if name == 'archive':
                attempt['messages'] = [dict(role='user', content='历史原文' * 2000) for _ in range(20)]
                attempt['pending_tools'] = {'unknown': dict(name='write', status='unknown')}
                finish_attempt(state, attempt['id'], dict(text='原有交付'))
    backend.change(setup)
    return store, backend, rid, ids


@pytest.mark.asyncio
async def test_parallel_checkpoints_preserve_archive_receipts_and_reload(tmp_path):
    store, backend, rid, ids = saved_run(tmp_path)
    old = backend.read()
    history = copy.deepcopy(old['attempts'][ids['archive']])
    journal = EventJournal(store.root, rid)
    revision = store._live[rid]['kernel_revision']
    core = Core(backend, None)
    for _ in range(3):
        assert core.channel(ids['a'], 'receive', ack=False) == []
        assert core.channel(ids['a'], 'should_yield') is False
        assert core.channel(ids['a'], 'inputs') == {}
    assert not core.wake.is_set() and store._live[rid]['kernel_revision'] == revision
    async def update(name):
        messages = [dict(role='assistant', content=name + '的实际进度')]
        await asyncio.to_thread(backend.publish, ids[name], 'checkpoint',
            dict(messages=messages, pending_tools={'t': dict(status='unknown')}, partial_text=name), stamp=10)
        messages[0]['content'] = '外部随后修改'
        await asyncio.to_thread(backend.publish, ids[name], 'progress', dict(text=name + '已保存'), stamp=11)
    await asyncio.gather(update('a'), update('b'))
    # A cold database read proves that concurrency committed both checkpoints,
    # and that publication did not rewrite the large archived conversation.
    _SQL_CACHE.clear()
    restored = journal.load()['workflow_core']
    assert restored['attempts'][ids['archive']] == history
    for name in ('a', 'b'):
        attempt = restored['attempts'][ids[name]]
        assert attempt['messages'][0]['content'] == name + '的实际进度'
        assert attempt['pending_tools']['t']['status'] == 'unknown'
        assert attempt['progress'][-1]['text'] == name + '已保存'
        assert restored['node_sessions'][attempt['session_id']]['head_attempt_id'] == attempt['id']
    with sqlite3.connect(store.root / 'runs.sqlite3') as database:
        written = database.execute('SELECT SUM(logical_bytes) FROM transactions WHERE run_id=? AND revision>?',
                                   (rid, revision)).fetchone()[0]
    assert written < 128 * 1024
    assert old['attempts'][ids['archive']] == history
    assert old['attempts'][ids['a']]['messages'] == []


def test_checkpoint_write_failure_preserves_live_and_durable_state(tmp_path, monkeypatch):
    store, backend, rid, ids = saved_run(tmp_path)
    old = backend.read()
    revision = store._live[rid]['kernel_revision']
    def fail(*args, **kwargs):
        raise OSError('disk write failed')
    with monkeypatch.context() as scoped:
        scoped.setattr(RunDatabase, 'commit', fail)
        with pytest.raises(OSError, match='disk write failed'):
            backend.publish(ids['a'], 'checkpoint', dict(messages=[dict(role='user', content='未提交')]), stamp=1)
    assert backend.read() == old
    _SQL_CACHE.clear()
    restored = EventJournal(store.root, rid).load()
    assert restored['kernel_revision'] == revision and restored['workflow_core'] == old


def test_idle_scheduler_pass_keeps_revision_and_archive_unchanged(tmp_path, monkeypatch):
    store, backend, rid, ids = saved_run(tmp_path)
    old = backend.read()
    revision = store._live[rid]['kernel_revision']
    def unexpected_commit(*args, **kwargs):
        raise AssertionError('an idle scheduler must not write another transaction')
    with monkeypatch.context() as scoped:
        scoped.setattr(RunDatabase, 'commit', unexpected_commit)
        assert backend.change(lambda state: []) == []
        assert backend.change(lambda state: state['attempts'][ids['archive']]['status']) == 'completed'
    assert store._live[rid]['kernel_revision'] == revision
    assert backend.read() == old


def test_failed_control_transition_keeps_live_and_durable_checkpoint(tmp_path, monkeypatch):
    store, backend, rid, ids = saved_run(tmp_path)
    old = backend.read()
    revision = store._live[rid]['kernel_revision']
    def fail(*args, **kwargs):
        raise OSError('control transaction failed')
    with monkeypatch.context() as scoped:
        scoped.setattr(RunDatabase, 'commit', fail)
        with pytest.raises(OSError, match='control transaction failed'):
            backend.change(lambda state: state['nodes']['a'].update(reason='uncommitted'))
    assert backend.read() == old
    _SQL_CACHE.clear()
    restored = EventJournal(store.root, rid).load()
    assert restored['kernel_revision'] == revision and restored['workflow_core'] == old
