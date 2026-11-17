"""Tool boundary writes stay durable without traversing unrelated run history."""
import asyncio
import copy
import threading
from types import SimpleNamespace

import pytest

from mewcode.kernel import events
from mewcode.kernel.calls import CallGateway
from mewcode.kernel.sqlite_state import RunDatabase
from mewcode.tools.base import ToolResult
from tests.test_tool_performance import RUN, assert_views_match, receipt, running_store


def tool_call():
    return SimpleNamespace(tool_name='WriteFile', tool_id='write-1',
                           arguments={'file_path': 'fixture.txt', 'content': 'durable output'})


def test_boundaries_preserve_history_without_copying_it(tmp_path):
    store = running_store(tmp_path)
    def seed(row):
        row['kernel_attempt_archives'] = []
        row['node_states']['executor']['messages'] = []
        row['node_states']['previous'] = {'status': 'completed', 'messages': []}
    store.mutate(RUN, seed)
    store.append_sequenced_events(RUN, [receipt(1)], return_snapshot=False)
    history = store.history_page(RUN)['items']
    frozen = RunDatabase(tmp_path, RUN).load()
    class Uncopyable(list):
        def __deepcopy__(self, memo):
            raise AssertionError('tool boundary copied unrelated execution context')
    private = events.EventJournal(tmp_path, RUN)._sql_load(detached=False)
    for tree in (private, store._live[RUN]):
        tree['kernel_attempt_archives'] = Uncopyable()
        tree['node_states']['executor']['messages'] = Uncopyable()
        tree['node_states']['previous']['messages'] = Uncopyable()
    gateway = CallGateway(store, RUN, 'executor', 'v1')
    call = gateway.prepare(tool_call())
    assert call['state'] == 'proposed'
    first_revision = RunDatabase(tmp_path, RUN).revision()[0]
    assert gateway.prepare(tool_call()) == call
    assert RunDatabase(tmp_path, RUN).revision()[0] == first_revision
    gateway.update(call['call_id'], 'reviewed')
    gateway.update(call['call_id'], 'dispatched', evidence={'executor_accepted': True})
    gateway.update(call['call_id'], 'succeeded', result=ToolResult('written'))
    gateway.unstarted(tool_call(), ToolResult('written'))
    saved = assert_views_match(tmp_path)
    assert saved['kernel_calls'][call['call_id']]['history'] == [
        'proposed', 'reviewed', 'dispatched', 'succeeded']
    assert saved['node_states']['previous'] == frozen['node_states']['previous']
    assert saved['kernel_attempt_archives'] == frozen['kernel_attempt_archives']
    assert store.history_page(RUN)['items'] == history
    assert private['node_ledgers'] == frozen['node_ledgers']
    assert private.get('kernel_calls') == frozen.get('kernel_calls')


def test_tool_boundary_callback_and_return_values_are_detached(tmp_path):
    store = running_store(tmp_path)
    retained = []
    def change(scope):
        retained.append(scope)
        scope['authorization_manifest'] = {'schema_version': 2, 'approved': False}
        return scope['authorization_manifest']
    returned = store.mutate_tool_boundary(RUN, 'executor', change, fields=('authorization_manifest',))
    returned['approved'] = True
    retained[0]['authorization_manifest']['approved'] = True
    retained[0]['node_ledgers'].clear()
    committed = RunDatabase(tmp_path, RUN).load()
    private = events.EventJournal(tmp_path, RUN)._sql_load(detached=False)
    assert not committed['authorization_manifest']['approved']
    assert private == committed
    assert store._live[RUN]['authorization_manifest'] == committed['authorization_manifest']
    gateway = CallGateway(store, RUN, 'executor', 'v1')
    row = gateway.prepare(tool_call())
    row['state'] = 'succeeded'
    detached = store.read_kernel_call(RUN, row['call_id'])
    assert detached['state'] == 'proposed'
    detached['state'] = 'failed'
    assert store.read_kernel_call(RUN, row['call_id'])['state'] == 'proposed'


def test_tool_boundary_failure_rolls_back_state_and_projections(tmp_path, monkeypatch):
    store = running_store(tmp_path)
    gateway = CallGateway(store, RUN, 'executor', 'v1')
    call = gateway.prepare(tool_call())
    before = RunDatabase(tmp_path, RUN).load()
    live, cache = store._live[RUN], store._read_model_cache[RUN]
    def fail(*args, **kwargs):
        raise OSError('injected display write failure')
    monkeypatch.setattr(RunDatabase, '_models', fail)
    with pytest.raises(OSError, match='injected'):
        gateway.update(call['call_id'], 'reviewed')
    assert RunDatabase(tmp_path, RUN).load() == before
    assert events.EventJournal(tmp_path, RUN)._sql_load() == before
    assert store._live[RUN] is live and store._read_model_cache[RUN] is cache


def test_unknown_call_keeps_identity_after_input_change_and_restart(tmp_path):
    from mewcode.expert_runs import ExpertRunStore
    store = running_store(tmp_path)
    gateway = CallGateway(store, RUN, 'executor', 'v1')
    call = gateway.prepare(tool_call())
    gateway.update(call['call_id'], 'reviewed')
    gateway.update(call['call_id'], 'dispatched', evidence={'executor_accepted': True})
    gateway.update(call['call_id'], 'unknown')
    restored = ExpertRunStore(tmp_path, storage='sqlite')
    retry = CallGateway(restored, RUN, 'executor', 'v2').prepare(tool_call())
    assert retry['call_id'] == call['call_id'] and retry['state'] == 'unknown'
    with pytest.raises(ValueError, match='illegal'):
        gateway.update(call['call_id'], 'dispatched', evidence={'executor_accepted': True})
    saved = assert_views_match(tmp_path)
    blockers = saved['node_states']['executor']['ledger_view']['active_blockers']
    assert any(row['payload']['kind'] == 'unknown_effect' for row in blockers)
    assert saved['kernel_calls'][call['call_id']]['state'] == 'unknown'


def test_boundary_cannot_edit_other_node_or_execution_fields(tmp_path):
    store = running_store(tmp_path)
    before = copy.deepcopy(store._live[RUN])
    def invalid(scope):
        scope['node_states']['executor']['messages'] = ['not owned by tool boundary']
    with pytest.raises(ValueError, match='unrelated'):
        store.mutate_tool_boundary(RUN, 'executor', invalid, fields=('kernel_calls',))
    assert store._live[RUN] == before
    with pytest.raises(ValueError, match='unsupported'):
        store.mutate_tool_boundary(RUN, 'executor', lambda scope: None, fields=('authorizations',))


def test_tool_metadata_keeps_preview_private_and_records_detached(tmp_path):
    store = running_store(tmp_path)
    file = {'id': 'file-1', 'path': '/fixture.txt', 'name': 'fixture.txt', 'preview_type': 'text',
            'preview': 'private source bytes', 'node_id': 'executor', 'tool_id': 'write-1'}
    check = {'tool_id': 'check-1', 'command': 'python --version', 'is_error': False, 'output': 'Python'}
    store.record_tool_metadata(RUN, 'executor', process_file=file, test_record=check)
    saved = assert_views_match(tmp_path)
    assert saved['process_files'] == [file] and saved['test_records'] == [check]
    assert 'preview' not in saved['process_file_receipts'][0]
    before = RunDatabase(tmp_path, RUN).revision()[0]
    store.record_tool_metadata(RUN, 'executor', process_file=file)
    assert RunDatabase(tmp_path, RUN).revision()[0] == before
    file['preview'], check['output'] = 'caller mutation', 'caller mutation'
    assert events.EventJournal(tmp_path, RUN)._sql_load() == saved
    assert store._live[RUN]['process_files'] == saved['process_files']


@pytest.mark.asyncio
@pytest.mark.parametrize('endpoint', ['run', 'history'])
async def test_expert_run_web_reads_do_not_block_event_loop(monkeypatch, endpoint):
    from frontend import server
    threads = []
    def read(*args, **kwargs):
        threads.append(threading.get_ident())
        return {'conversation_id': 'chat', 'events': []}
    def history(*args, **kwargs):
        threads.append(threading.get_ident())
        return {'items': [], 'next_cursor': None}
    def owner(*args):
        threads.append(threading.get_ident())
        return 'chat'
    monkeypatch.setattr(server, 'EXPERT_RUNS', SimpleNamespace(read_public=read, read_owner=owner, history_page=history))
    monkeypatch.setattr(server, '_active_conversation_id', lambda: 'chat')
    if endpoint == 'run':
        response = await server.api_expert_run(RUN, role='', node_id='', attempt=0, node_attempt=0, member_id='')
    else:
        response = await server.api_expert_run_history(RUN, after=-1, limit=50, node_id='',
                                                     attempt=0, node_attempt=0, member_id=None)
    assert response.status_code == 200 and threads
    assert all(identity != threading.get_ident() for identity in threads)


@pytest.mark.asyncio
async def test_history_read_rechecks_conversation_after_worker_returns(monkeypatch):
    from frontend import server
    active = ['chat']
    def history(*args, **kwargs):
        active[0] = 'another-chat'
        return {'items': [{'output': 'private receipt'}]}
    monkeypatch.setattr(server, 'EXPERT_RUNS', SimpleNamespace(
        read_owner=lambda *a, **k: 'chat', history_page=history))
    monkeypatch.setattr(server, '_active_conversation_id', lambda: active[0])
    response = await server.api_expert_run_history(RUN, after=-1, limit=50, node_id='',
                                                 attempt=0, node_attempt=0, member_id=None)
    assert response.status_code == 404 and b'private receipt' not in response.body


@pytest.mark.asyncio
@pytest.mark.parametrize('during_effect', [False, True])
async def test_cancelled_durable_dispatch_never_loses_effect_state(tmp_path, monkeypatch, during_effect):
    from mewcode.agent import Agent
    store = running_store(tmp_path)
    agent = Agent.__new__(Agent)
    agent._safety_call_ref = lambda *args: 'fixture'
    agent._prepare_rollback_evidence = lambda *args: None
    agent._review_recovery = SimpleNamespace(begin_effect=lambda *args: {}, finish_effect=lambda *args: None)
    agent.work_dir, agent.permission_checker = str(tmp_path), None
    gateway = agent.kernel_gateway = CallGateway(store, RUN, 'executor', 'v1')
    entered, release = threading.Event(), threading.Event()
    effect_entered = asyncio.Event()
    worker_threads, effects = [], []
    original = gateway.update
    def update(call_id, state, **kwargs):
        if state == 'dispatched' and not during_effect:
            worker_threads.append(threading.get_ident())
            entered.set()
            assert release.wait(5)
        return original(call_id, state, **kwargs)
    monkeypatch.setattr(gateway, 'update', update)
    async def effect(*args, **kwargs):
        effects.append(True)
        effect_entered.set()
        await asyncio.Future()
    tool = SimpleNamespace(category='write', run=effect)
    task = asyncio.create_task(agent._run_recorded_tool(tool, {}, tool_call()))
    try:
        if during_effect:
            await asyncio.wait_for(effect_entered.wait(), 5)
        else:
            assert await asyncio.to_thread(entered.wait, 5)
            assert worker_threads == [worker_threads[0]] and worker_threads[0] != threading.get_ident()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    call = next(iter(RunDatabase(tmp_path, RUN).load()['kernel_calls'].values()))
    assert call['state'] == ('unknown' if during_effect else 'not_started')
    assert len(effects) == int(during_effect)
    if not during_effect:
        assert call['evidence'][-1]['host_verified'] is True
        assert call['evidence'][-1]['command_started'] is False
