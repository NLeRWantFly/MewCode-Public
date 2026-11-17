"""Task polling never owns recovery or reads execution history."""
from types import SimpleNamespace

import httpx
import pytest

from frontend import server
from mewcode.expert_runs import ExpertRunStore
from mewcode.kernel.events import EventJournal
from mewcode.kernel.sqlite_state import RunDatabase
from mewcode.orchestration.task_design import TaskDesign
from mewcode.orchestration.task_view import collaboration_projection
from tests.test_collaboration_mode import flow, PROFILE, RUN_ID
from tests.test_task_design_entry import task_api_env


def forbidden(*args, **kwargs):
    raise AssertionError('task query touched execution/recovery')


def bound_run(root, owner, storage='sqlite'):
    design = TaskDesign(owner['project'], owner['conversation_id'])
    current = design.start('execute', orchestration_mode='collaborative')
    spec = flow()
    spec.meta['collaboration_spec']['task_id'] = current['task_id']
    store = ExpertRunStore(root, storage=storage)
    store.create(run_id=RUN_ID, conversation_id=owner['conversation_id'], task='synthetic task',
                 attachments=[], profile=PROFILE.as_dict(), strategy='stable', flow=spec)
    design.transition(current, 'running', run_id=RUN_ID)
    return design, store


@pytest.mark.asyncio
@pytest.mark.parametrize('storage', ['legacy', 'sqlite'])
async def test_bound_get_reads_committed_projection_only(task_api_env, tmp_path, monkeypatch, storage):
    _, _, _, owner, _ = task_api_env
    design, store = bound_run(tmp_path / 'runs', owner, storage)
    before = design.read()
    expected = collaboration_projection(store.load(RUN_ID))
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    for name in ('load', '_read_disk', '_commit', 'transition', 'mutate'):
        monkeypatch.setattr(store, name, forbidden)
    for name in ('control', 'transition', 'bind_run', 'record_execution_not_started'):
        monkeypatch.setattr(TaskDesign, name, forbidden)
    monkeypatch.setattr(EventJournal, 'load', forbidden)
    monkeypatch.setattr(server, 'recover_task_design_on_session_load', forbidden)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        for _ in range(2):
            response = await client.get('/api/orchestration/task-design', params=owner)
            assert response.status_code == 200, response.text
            current = response.json()['task_design']
            assert current['collaboration'] == expected
            assert current['revision'] == before['revision'] and current['status'] == 'running'
    assert design.read() == before


@pytest.mark.asyncio
@pytest.mark.parametrize('problem', ['missing', 'version', 'task', 'conversation'])
async def test_missing_or_wrong_binding_fails_closed_without_rebuild(task_api_env, tmp_path, monkeypatch, problem):
    _, _, _, owner, _ = task_api_env
    _, store = bound_run(tmp_path / 'runs', owner)
    database = RunDatabase(store.root, RUN_ID)
    with database.connection(write=True) as db:
        db.execute('BEGIN IMMEDIATE')
        if problem == 'missing':
            database._remove(db, 'design', ())
        else:
            key = {'version': 'version', 'task': 'task_id', 'conversation': 'conversation_id'}[problem]
            database._set(db, 'design', (key,), 'wrong')
        db.commit()
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    monkeypatch.setattr(store, 'load', forbidden)
    monkeypatch.setattr(store, '_read_disk', forbidden)
    monkeypatch.setattr(store, 'refresh_public_summaries', forbidden)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        response = await client.get('/api/orchestration/task-design', params=owner)
    assert response.status_code == 409, response.text
    assert ('未就绪' if problem in {'missing', 'version'} else '不一致') in response.json()['error']


@pytest.mark.parametrize('storage', ['legacy', 'sqlite'])
def test_startup_upgrades_missing_card_without_changing_authority(task_api_env, tmp_path, monkeypatch, storage):
    _, _, _, owner, _ = task_api_env
    _, store = bound_run(tmp_path / 'runs', owner, storage)
    before = store.load(RUN_ID)
    database = RunDatabase(store.root, RUN_ID)
    with database.connection(write=True) as db:
        db.execute('DELETE FROM cells WHERE run_id=? AND space=?', (RUN_ID, 'design'))
    restarted = ExpertRunStore(store.root)
    assert restarted.read_design(RUN_ID) is None
    assert restarted.refresh_public_summaries() == 1
    assert restarted.read_design(RUN_ID)['collaboration'] == collaboration_projection(before)
    assert restarted.load(RUN_ID) == before
    monkeypatch.setattr(restarted, '_read_disk', forbidden)
    assert restarted.refresh_public_summaries() == 0
    with database.connection() as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_card_query_cost_does_not_grow_with_execution_history(task_api_env, tmp_path, monkeypatch):
    _, _, _, owner, _ = task_api_env
    _, store = bound_run(tmp_path / 'runs', owner)
    original_read = RunDatabase._read
    counts = []
    def measured(self, db, space, parts=()):
        assert space == 'design' and not parts
        count = [0]
        def tick():
            count[0] += 1
        db.set_progress_handler(tick, 1)
        try:
            return original_read(self, db, space, parts)
        finally:
            db.set_progress_handler(None, 0)
            counts.append(count[0])
    with monkeypatch.context() as query:
        query.setattr(RunDatabase, '_read', measured)
        baseline = store.read_design(RUN_ID)
    store.append_sequenced_events(RUN_ID, [dict(seq=i+1, type='expert_step', node_id='work', label='step')
                                         for i in range(5000)], return_snapshot=False)
    with monkeypatch.context() as query:
        query.setattr(RunDatabase, '_read', measured)
        query.setattr(store, 'load', forbidden)
        query.setattr(store, '_read_disk', forbidden)
        expanded = store.read_design(RUN_ID)
    assert expanded['collaboration'] == baseline['collaboration']
    assert counts[1] == counts[0], 'same card size must visit the same number of SQL instructions'
    store.append_sequenced_events(RUN_ID, [dict(seq=5001, type='expert_step', node_id='work', label='step')],
                                  return_snapshot=False)
    with RunDatabase(store.root, RUN_ID).connection() as db:
        written = db.execute('SELECT logical_bytes FROM transactions WHERE run_id=? '
                             'ORDER BY revision DESC LIMIT 1', (RUN_ID,)).fetchone()[0]
        assert written < 10 * 1024, 'adding the card must retain bounded append writes'
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['designing', 'queued', 'building'])
async def test_unbound_get_never_recovers_an_orphan(task_api_env, monkeypatch, status):
    _, _, _, owner, _ = task_api_env
    design = TaskDesign(owner['project'], owner['conversation_id'])
    current = design.start('plan')
    current = design.record_turn('synthetic goal')
    current = design.transition(current, status)
    monkeypatch.setattr(TaskDesign, 'control', forbidden)
    monkeypatch.setattr(TaskDesign, 'transition', forbidden)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        response = await client.get('/api/orchestration/task-design', params=owner)
    assert response.status_code == 200, response.text
    assert response.json()['task_design']['status'] == status
    assert design.read()['revision'] == current['revision']


@pytest.mark.parametrize('created', [False, True])
def test_cold_recovery_resolves_claim_without_creating_another_run(task_api_env, tmp_path, monkeypatch, created):
    _, _, _, owner, _ = task_api_env
    design = TaskDesign(owner['project'], owner['conversation_id'])
    current = design.start('execute', orchestration_mode='collaborative')
    ready = design.transition(current, 'ready', build_request={'state': 'saved', 'id': 'build', 'epoch': 1})
    claim = design.claim_execution(ready)
    run_id = design.execution_run_id(claim)
    store = ExpertRunStore(tmp_path / 'runs', storage='sqlite')
    if created:
        spec = flow()
        spec.meta['collaboration_spec']['task_id'] = claim['task_id']
        store.create(run_id=run_id, conversation_id=owner['conversation_id'], task='synthetic',
                     attachments=[], profile=PROFILE.as_dict(), strategy='stable', flow=spec)
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    monkeypatch.setattr(server.SESSION, 'chat_busy', lambda *_: False)
    monkeypatch.setattr(store, 'create', forbidden)
    recovered = server.recover_task_design_on_session_load(owner['project'], owner['conversation_id'])
    assert recovered['status'] == 'paused'
    assert recovered.get('run_id') == (run_id if created else None)
    assert recovered['task_id'] == claim['task_id']
    again = server.recover_task_design_on_session_load(owner['project'], owner['conversation_id'])
    assert again['revision'] == recovered['revision']


@pytest.mark.parametrize('failure', [False, True])
def test_cold_session_load_invokes_recovery_once(task_api_env, monkeypatch, failure):
    _, _, _, owner, _ = task_api_env
    calls = []
    def recover(*args):
        calls.append(args)
        if failure:
            raise ValueError('synthetic binding mismatch')
    monkeypatch.setattr(server, 'recover_task_design_on_session_load', recover)
    monkeypatch.setattr(server, 'continuation_state', lambda *_: {})
    monkeypatch.setattr(server, '_save_web_state', lambda *_: None)
    monkeypatch.setattr(server.EXPERT_RUNS, 'public_for_conversation', lambda *_: [])
    session = SimpleNamespace(session_id=owner['conversation_id'])
    fake = SimpleNamespace(session=None, work_dir=owner['project'], agent=None, web_state={}, scope='project',
        runtime_for=lambda *_: None, chat_busy=lambda *_: False,
        session_manager=lambda *_: SimpleNamespace(resume=lambda *_: SimpleNamespace(session=session, messages=[])),
        _release_active_conversation=lambda: None,
        new_conversation_store=lambda: SimpleNamespace(restore_history=lambda *_: None, history=[]),
        _ensure_file_history=lambda: None, _restore_conversation_settings=lambda *_: None,
        _ensure_active_agent=lambda: None, _remember_conversation=lambda *_: None,
        _display_entries=lambda *_: [], pipeline_events=lambda *_: [])
    loaded = server.WebSession.load_conversation(fake, 'project', session.session_id)
    assert loaded['running'] is False
    assert loaded['task_recovery_error'] == ('synthetic binding mismatch' if failure else '')
    assert calls == [(owner['project'], session.session_id)]
    assert server.WebSession.load_conversation(fake, 'project', session.session_id)['running'] is False
    assert len(calls) == 1, 'polling/opening the already active session must not recover again'
