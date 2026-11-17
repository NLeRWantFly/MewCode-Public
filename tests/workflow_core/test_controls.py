import asyncio
import copy

import pytest

from mewcode.workflow_core.runtime import Core, MemoryBackend
from mewcode.workflow_core.state import command, fresh, edit, start_attempt
from mewcode.workflow_core.schema import walk
from tests.workflow_core.test_runtime import graph


class LegacyLimitCore(Core):
    """Create a first-round resource pause as written by the previous host."""
    def tick(self, state):
        for path, node in walk(state['graph']):
            row = state['nodes'].get(path, {})
            loop = row.get('loop') or {}
            limits = node['config'].get('limits') or {}
            if limits and row.get('status') == 'running' and loop.get('round') == 1 and not loop.get('active'):
                dimension = next(iter(limits))
                reason = {'iterations': '达到设定循环次数', 'seconds': '达到设定循环时限',
                          'tokens': '达到设定循环 Token 上限'}[dimension]
                row.update(status='paused', reason=reason)
                loop['limit_pause'] = dict(dimension=dimension, limit=limits[dimension])
        return super().tick(state)


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', ['iterations', 'seconds', 'tokens', 'legacy_seconds'])
async def test_continue_old_loop_limit_preserves_results_without_another_resource_stop(tmp_path, monkeypatch, limit):
    from mewcode.workflow_core import runtime
    clock = [100.0]
    monkeypatch.setattr(runtime.time, 'time', lambda: clock[0])
    key = 'seconds' if limit == 'legacy_seconds' else limit
    flow = graph([dict(id='loop', kind='loop', config=dict(mode='count', count=3,
        limits={key: 1}, graph=graph(['make']))), 'next'], [('loop', 'next', 'always')])
    backend, calls = MemoryBackend(fresh(flow)), []
    async def execute(ctx):
        calls.append((ctx.node_id, ctx.iteration))
        clock[0] += 2
        return dict(text='delivered', tokens=2)
    first = await LegacyLimitCore(backend, execute).run()
    assert first['status'] == 'paused'
    assert first['nodes']['loop']['loop']['round'] == 1
    history = copy.deepcopy(first['attempts'])
    first_result = copy.deepcopy(first['nodes']['loop']['loop']['results'][0])
    if limit == 'legacy_seconds':
        backend.change(lambda s: s['nodes']['loop']['loop'].pop('limit_pause'))
    body = dict(operation='resume', request_id='resume-1')
    backend.change(lambda s: command(s, body))
    resumed = backend.read()
    assert all(resumed['attempts'][identity] == old for identity, old in history.items())
    backend.change(lambda s: command(s, body))
    assert backend.read() == resumed
    final = await Core(backend, execute).run()
    assert final['status'] == 'completed'
    assert final['nodes']['loop']['loop']['results'][0] == first_result
    assert all(final['attempts'][identity] == old for identity, old in history.items())
    assert calls == [('loop/make', [0]), ('loop/make', [1]), ('loop/make', [2]), ('next', [])]
    assert final['nodes']['loop']['loop']['tokens'] == 6
    assert final['attempts'][final['nodes']['loop']['attempt_id']]['status'] == 'completed'


@pytest.mark.asyncio
async def test_model_decision_ends_long_loop_despite_all_old_resource_limits(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('mewcode.workflow_core.runtime.time.time', lambda: clock[0])
    flow = graph([dict(id='loop', kind='loop', config=dict(mode='until',
        limits=dict(iterations=1, seconds=1, tokens=1), until={'path': 'results.check.value', 'equals': True},
        graph=graph(['make', dict(id='check', kind='judge')], [('make', 'check', 'always')]))), 'next'],
        [('loop', 'next', 'always')])
    calls = []
    async def execute(ctx):
        calls.append((ctx.node_id, ctx.iteration))
        clock[0] += 86400
        return dict(text='done', tokens=1000000, value=ctx.iteration == [2])
    result = await Core(MemoryBackend(fresh(flow)), execute).run()
    assert result['status'] == 'completed'
    assert result['nodes']['loop']['loop']['round'] == 3
    assert result['nodes']['loop']['loop']['tokens'] == 6000000
    assert result['nodes']['loop']['loop']['elapsed'] >= 6 * 86400
    assert calls == [(f'loop/{name}', [round_]) for round_ in range(3) for name in ['make', 'check']] + [('next', [])]
    assert not result['nodes']['loop']['loop'].get('limit_pause')


@pytest.mark.asyncio
@pytest.mark.parametrize('marker', [True, False])
async def test_reattach_retires_old_resource_stop_without_resetting_a_finished_round(marker):
    backend = MemoryBackend(fresh(graph([dict(id='loop', kind='loop', config=dict(mode='count', count=2,
        limits={'seconds': 1}, graph=graph(['make'])))])))
    calls = []
    async def execute(ctx):
        calls.append(ctx.iteration)
        return dict(text='done', tokens=10)
    first = await LegacyLimitCore(backend, execute).run()
    if not marker:
        backend.change(lambda state: state['nodes']['loop']['loop'].pop('limit_pause'))
    result = await Core(backend, execute).run()
    assert result['status'] == 'completed' and calls == [[0], [1]]
    assert result['nodes']['loop']['loop']['results'][0] == first['nodes']['loop']['loop']['results'][0]
    assert all(result['attempts'][key] == old for key, old in first['attempts'].items())
    assert any(event['kind'] == 'loop_limit_retired' for event in result['events'])


def test_manual_pause_does_not_reset_loop_resource_window():
    state = fresh(graph([dict(id='loop', kind='loop', config=dict(graph=graph(['work']), until=True))]))
    start_attempt(state, 'loop', {}, [])
    state['nodes']['loop'].update(status='paused', loop=dict(inputs={}, round=2, elapsed=10, tokens=20,
                                                           limit_baseline={'seconds': 3}))
    command(state, dict(operation='resume', request_id='resume'))
    assert state['nodes']['loop']['loop']['limit_baseline'] == {'seconds': 3}


@pytest.mark.parametrize('node_id', ['work', ''])
def test_resume_with_existing_node_messages_keeps_control_id_and_scope(node_id):
    from mewcode.workflow_core import dialogue
    from mewcode.workflow_core.state import finish_attempt

    state = fresh(graph(['work', 'source']))
    work = start_attempt(state, 'work', {}, [])
    work['messages'] = [{'role': 'assistant', 'content': 'Saved work checkpoint'}]
    finish_attempt(state, work['id'], dict(status='paused', text=''))
    source = start_attempt(state, 'source', {}, [])
    finish_attempt(state, source['id'], dict(status='completed', text='Original delivery'))
    message = dialogue.enqueue(state, dict(node_id='source', kind='ask', message='Explain', request_id='question'))
    message = state['node_requests'][message['id']]
    dialogue.start_request(state, message)
    message['status'] = 'paused'
    reply_attempt = state['attempts'][message['attempt_id']]
    reply_attempt['status'] = 'paused'

    body = dict(operation='resume', node_id=node_id, request_id='resume-control')
    result = command(state, body)
    assert command(state, body) == result
    assert state['commands']['resume-control']['result'] == result
    assert state['nodes']['work']['previous_attempt_id'] == work['id']
    assert work['messages'][0]['content'] == 'Saved work checkpoint'
    assert state['nodes']['source']['status'] == 'completed'
    assert state['node_requests']['question']['status'] == ('paused' if node_id else 'waiting')
    assert reply_attempt['status'] == ('paused' if node_id else 'suspended')


@pytest.mark.asyncio
async def test_continue_inside_loop_keeps_current_item_and_prior_deliveries():
    flow = graph([dict(id='pages', kind='loop', config=dict(mode='foreach', items=['p1', 'p2', 'p3'],
        graph=graph(['prepare', 'ocr', 'store'], [('prepare', 'ocr', 'always'), ('ocr', 'store', 'always')])))])
    backend, calls = MemoryBackend(fresh(flow)), []
    async def execute(ctx):
        calls.append((ctx.node_id, ctx.inputs['item']))
        if ctx.node_id.endswith('/ocr') and ctx.inputs['item'] == 'p2' and calls.count((ctx.node_id, 'p2')) == 1:
            return dict(status='failed', reason='connection lost')
        return dict(text='done')
    await Core(backend, execute).run()
    backend.change(lambda s: command(s, dict(operation='continue_from', node_id='pages/ocr', request_id='resume-item')))
    result = await Core(backend, execute).run()
    assert result['status'] == 'completed'
    assert calls.count(('pages/prepare', 'p2')) == 1
    assert calls.count(('pages/store', 'p1')) == 1
    assert calls.count(('pages/ocr', 'p2')) == 2
    assert calls[-1] == ('pages/store', 'p3')


@pytest.mark.asyncio
async def test_manual_judgment_and_skip_bound_file_need_no_model():
    flow = graph(['source', dict(id='check', kind='judge', config={'manual': True}), 'store'], [
        ('source', 'check', 'always'), ('check', 'store', 'pass')])
    backend = MemoryBackend(fresh(flow))
    backend.change(lambda s: command(s, dict(operation='skip', node_id='source', request_id='skip',
        inputs={'file': {'path': 'existing.txt'}})))
    async def execute(ctx):
        if ctx.node_id == 'check':
            assert ctx.inputs['source']['file']['path'] == 'existing.txt'
            return dict(status='needs_input', reason='请确认')
        assert ctx.node_id == 'store'
        return dict(text='stored')
    await Core(backend, execute).run()
    waiting = backend.read()
    backend.change(lambda s: command(s, dict(operation='resume', request_id='resume-before-answer')))
    await Core(backend, execute).run()
    assert backend.read()['nodes']['check'] == waiting['nodes']['check']
    assert backend.read()['attempts'] == waiting['attempts']
    backend.change(lambda s: command(s, dict(operation='judge_answer', node_id='check', request_id='answer', value=True)))
    result = await Core(backend, execute).run()
    assert result['status'] == 'completed' and result['nodes']['source']['status'] == 'skipped'


@pytest.mark.asyncio
async def test_unknown_tool_resume_keeps_checkpoint_but_explicit_rerun_starts_fresh():
    state = fresh(graph(['work']))
    old = start_attempt(state, 'work', {}, [])
    old['messages'] = [{'role': 'user', 'content': 'old'}]
    old['pending_tools'] = {'call': {'status': 'unknown'}}
    state['nodes']['work']['status'] = 'unknown'
    old['status'] = 'unknown'
    backend = MemoryBackend(state)
    calls = []
    async def execute(ctx):
        calls.append(ctx)
        if len(calls) == 1:
            assert ctx.checkpoint['id'] == old['id']
            assert ctx.checkpoint['pending_tools']['call']['status'] == 'unknown'
        else:
            assert not ctx.checkpoint
        return dict(text='verified and delivered')
    backend.change(lambda s: command(s, dict(operation='resume', request_id='resume')))
    await Core(backend, execute).run()
    assert len(calls) == 1
    backend.change(lambda s: command(s, dict(operation='rerun', node_id='work', request_id='repeat')))
    assert (await Core(backend, execute).run())['status'] == 'completed'
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_pending_apply_recovers_without_replaying_delivered_unaffected_branch():
    backend = MemoryBackend(fresh(graph(['a', 'b', 'other'], [('a', 'b', 'always')])))
    calls = []
    async def execute(ctx):
        calls.append(ctx.node_id)
        return dict(text='done')
    await Core(backend, execute).run()
    changed = backend.change(lambda s: edit(s, dict(action='insert', kind='node', id='check',
        edge_id='e_0_a_b', fields={'kind': 'judge', 'config': {'expression': True}})))
    body = dict(operation='apply', request_id='apply', expected_revision=changed['revision'])
    backend.change(lambda s: command(s, body))
    recovered = MemoryBackend(backend.read())
    result = await Core(recovered, execute).run()
    assert result['status'] == 'completed'
    assert calls == ['a', 'other', 'b', 'b']
    assert recovered.change(lambda s: command(s, body))['applied']


@pytest.mark.asyncio
async def test_stop_unbounded_loop_and_pause_only_one_branch():
    backend = MemoryBackend(fresh(graph([dict(id='repeat', kind='loop', config=dict(
        graph=graph(['work']), until=False)), 'other'])))
    arrived = asyncio.Event()
    async def execute(ctx):
        if ctx.node_id == 'repeat/work' and ctx.iteration[0] == 10:
            arrived.set()
            await asyncio.Event().wait()
        return dict(text='done')
    core = Core(backend, execute)
    task = asyncio.create_task(core.run())
    await asyncio.wait_for(arrived.wait(), 3)
    core.control(dict(operation='pause', node_id='repeat', request_id='pause-loop'))
    result = await asyncio.wait_for(task, 3)
    assert result['status'] == 'paused' and result['nodes']['other']['status'] == 'completed'
    assert result['nodes']['repeat']['loop']['round'] == 10


@pytest.mark.asyncio
async def test_apply_returns_interrupted_external_effect_to_node_for_reconciliation():
    backend = MemoryBackend(fresh(graph(['write'])))
    started, calls = asyncio.Event(), []
    async def execute(ctx):
        calls.append(ctx.attempt_id)
        if len(calls) > 1:
            assert ctx.checkpoint['id'] == calls[0]
            assert ctx.checkpoint['pending_tools']['external']['status'] == 'unknown'
            assert ctx.node['task'] == 'new task'
            return dict(text='Existing external output checked and reused')
        await ctx.publish('checkpoint', pending_tools={'external': {'status': 'running'}})
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await ctx.publish('checkpoint', pending_tools={'external': {'status': 'unknown'}})
    core = Core(backend, execute)
    running = asyncio.create_task(core.run())
    await started.wait()
    changed = backend.change(lambda s: edit(s, dict(action='upsert', kind='node', id='write', fields={'task': 'new task'})))
    core.control(dict(operation='apply', expected_revision=changed['revision'], request_id='apply'))
    result = await asyncio.wait_for(running, 3)
    assert result['graph']['revision'] == 2 and result['nodes']['write']['status'] == 'completed'
    assert len(calls) == 2
    assert result['attempts'][calls[0]]['pending_tools']['external']['status'] == 'unknown'
