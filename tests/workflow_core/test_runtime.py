import asyncio
import copy

import pytest

from mewcode.workflow_core.runtime import Core, MemoryBackend
from mewcode.workflow_core.schema import normalize
from mewcode.workflow_core.state import command, edit, fresh


def graph(nodes, edges=()):
    return normalize(dict(nodes=[dict(id=node, kind='work') if isinstance(node, str) else node for node in nodes],
                          edges=[dict(source=a, target=b, port=port) for a, b, port in edges]))


@pytest.mark.asyncio
async def test_ocr_fanout_joins_only_after_both_deliveries():
    flow = graph(['environment', 'materials', 'ocr_a', 'ocr_b', 'database'], [
        ('environment', 'materials', 'always'), ('materials', 'ocr_a', 'always'),
        ('materials', 'ocr_b', 'always'), ('ocr_a', 'database', 'always'), ('ocr_b', 'database', 'always')])
    backend = MemoryBackend(fresh(flow))
    arrived, calls = set(), []
    both = asyncio.Event()
    async def execute(ctx):
        calls.append(ctx.node_id)
        if ctx.node_id.startswith('ocr_'):
            arrived.add(ctx.node_id)
            if len(arrived) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 2)
        if ctx.node_id == 'database':
            assert ctx.inputs['ocr_a']['text'] == 'ocr_a delivered'
            assert ctx.inputs['ocr_b']['text'] == 'ocr_b delivered'
        return dict(text=ctx.node_id + ' delivered')
    result = await Core(backend, execute, concurrency=2).run()
    assert result['status'] == 'completed'
    assert calls[:2] == ['environment', 'materials'] and calls[-1] == 'database'
    assert len(result['attempts']) == 5  # No implicit verifier or finalizer calls.


@pytest.mark.asyncio
async def test_unselected_branch_does_not_block_join():
    flow = graph([dict(id='judge', kind='judge', config={'expression': True}), 'yes', 'no', 'join'], [
        ('judge', 'yes', 'pass'), ('judge', 'no', 'fail'), ('yes', 'join', 'always'), ('no', 'join', 'always')])
    calls = []
    async def execute(ctx):
        calls.append(ctx.node_id)
        return dict(text='done')
    result = await Core(MemoryBackend(fresh(flow)), execute).run()
    assert result['status'] == 'completed'
    assert calls == ['yes', 'join']
    assert result['nodes']['no']['status'] == 'skipped'


@pytest.mark.asyncio
async def test_failure_only_holds_dependent_branch():
    flow = graph(['broken', 'dependent', 'independent'], [('broken', 'dependent', 'always')])
    calls = []
    async def execute(ctx):
        calls.append(ctx.node_id)
        return dict(status='failed' if ctx.node_id == 'broken' else 'completed', text='result')
    result = await Core(MemoryBackend(fresh(flow)), execute).run()
    assert result['status'] == 'blocked'
    assert set(calls) == {'broken', 'independent'}


@pytest.mark.asyncio
async def test_nested_foreach_and_unlimited_until():
    inner = graph([dict(id='repeat', kind='loop', config=dict(mode='count', count=2, graph=graph(['work'])))])
    flow = graph([dict(id='pages', kind='loop', config=dict(mode='foreach', items=list(range(7)), graph=inner))])
    calls = []
    async def execute(ctx):
        calls.append((ctx.node_id, ctx.iteration))
        return dict(text='recognized')
    result = await Core(MemoryBackend(fresh(flow)), execute).run()
    assert result['status'] == 'completed' and len(calls) == 14
    assert calls[-1][1] == [6, 1]
    body = graph([dict(id='check', kind='judge')])
    flow = graph([dict(id='retry', kind='loop', config=dict(graph=body,
        until={'path': 'results.check.value', 'equals': True}))])
    async def judge(ctx):
        return dict(text='measured', value=ctx.iteration[0] >= 8)
    result = await Core(MemoryBackend(fresh(flow)), judge).run()
    assert result['status'] == 'completed'
    assert result['nodes']['retry']['loop']['round'] == 9


@pytest.mark.asyncio
async def test_edit_running_branch_preserves_unaffected_work_and_archives_late_result():
    flow = graph(['a', 'b', 'independent'], [('a', 'b', 'always')])
    backend = MemoryBackend(fresh(flow))
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def execute(ctx):
        calls.append(ctx.node_id)
        if ctx.node_id == 'a' and calls.count('a') == 1:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                # Simulate a provider reporting success as cancellation arrives.
                return dict(text='old result')
        if ctx.node_id == 'independent':
            await release.wait()
        return dict(text='new result')
    core = Core(backend, execute)
    running = asyncio.create_task(core.run())
    await started.wait()
    changed = backend.change(lambda state: edit(state, dict(action='upsert', kind='node', id='a', fields={'task': 'new task'})))
    core.control(dict(operation='apply', request_id='apply-1', expected_revision=changed['revision']))
    release.set()
    result = await asyncio.wait_for(running, 3)
    assert result['status'] == 'completed'
    assert calls.count('independent') == 1 and calls.count('a') == 2
    assert result['nodes']['a']['result']['text'] == 'new result'
    assert len(result['graph_history']) == 2


@pytest.mark.asyncio
async def test_rerun_single_holds_successors_and_command_is_idempotent():
    backend = MemoryBackend(fresh(graph(['a', 'b'], [('a', 'b', 'always')])))
    calls = []
    async def execute(ctx):
        calls.append(ctx.node_id)
        return dict(text='done')
    await Core(backend, execute).run()
    body = dict(operation='rerun', node_id='a', request_id='once')
    result = backend.change(lambda state: command(state, body))
    before = backend.read()
    assert backend.change(lambda state: command(state, body)) == result
    assert before == backend.read()
    result = await Core(backend, execute).run()
    assert calls == ['a', 'b', 'a']
    assert result['nodes']['b']['status'] == 'paused'
    backend.change(lambda state: command(state, dict(operation='resume', request_id='continue')))
    result = await Core(backend, execute).run()
    assert calls[-1] == 'b' and result['status'] == 'completed'


@pytest.mark.asyncio
async def test_resume_unknown_tool_returns_checkpoint_to_executor_for_reconciliation():
    state = fresh(graph(['work']))
    from mewcode.workflow_core.state import start_attempt
    attempt = start_attempt(state, 'work', {}, [])
    attempt['pending_tools'] = {'call': {'status': 'running'}}
    attempt['messages'] = [{'role': 'user', 'content': 'existing task'}]
    async def execute(ctx):
        assert ctx.checkpoint['id'] == attempt['id']
        assert ctx.checkpoint['pending_tools'] == attempt['pending_tools']
        assert ctx.checkpoint['messages'] == attempt['messages']
        return dict(text='Existing output inspected and reused')
    result = await Core(MemoryBackend(state), execute).run()
    assert result['status'] == 'completed'
    assert result['attempts'][attempt['id']]['status'] == 'unknown'
    assert len(result['attempts']) == 2


@pytest.mark.asyncio
async def test_normal_delivery_advances_successor_despite_earlier_tool_timeout():
    calls = []
    async def execute(ctx):
        calls.append(ctx.node_id)
        if ctx.node_id == 'deploy':
            await ctx.publish('checkpoint', pending_tools={'clone': {'status': 'unknown'}})
            await ctx.publish('tool_result', tool_id='health', name='RemoteSSH', is_error=False, output='healthy')
            return dict(text='Deployment recovered and smoke test passed')
        assert ctx.inputs['deploy']['text'] == 'Deployment recovered and smoke test passed'
        return dict(text='OCR delivered')
    result = await Core(MemoryBackend(fresh(graph(['deploy', 'ocr'], [('deploy', 'ocr', 'always')]))), execute).run()
    assert result['status'] == 'completed' and calls == ['deploy', 'ocr']
    attempt = result['attempts'][result['nodes']['deploy']['attempt_id']]
    assert attempt['pending_tools']['clone']['status'] == 'unknown'
    assert attempt['diagnostics'][0]['output'] == 'healthy'


def test_layout_only_apply_does_not_invalidate_deliveries_and_undo_restores_graph():
    state = fresh(graph(['a', 'b'], [('a', 'b', 'always')]))
    from mewcode.workflow_core.state import current, apply_pending
    current(state, 'a').update(status='completed', result={'text': 'retain'})
    update = edit(state, dict(action='positions', positions={'a': {'x': 100, 'y': 20}}))
    assert update['impact']['affected'] == []
    command(state, dict(operation='apply', request_id='layout', expected_revision=update['revision']))
    apply_pending(state)
    assert state['nodes']['a']['result']['text'] == 'retain'
    edit(state, dict(action='delete', kind='node', id='b'))
    edit(state, dict(action='undo'))
    assert len(state['draft']['nodes']) == 2 and len(state['draft']['edges']) == 1


def test_batch_insert_is_atomic_and_no_half_edge_saved():
    state = fresh(graph(['a', 'b'], [('a', 'b', 'always')]))
    before = copy.deepcopy(state)
    backend = MemoryBackend(state)
    with pytest.raises(ValueError):
        backend.change(lambda value: edit(value, dict(action='batch', operations=[
            dict(action='upsert', kind='node', id='c', fields={'label': 'check'}),
            dict(action='upsert', kind='edge', id='bad', fields={'source': 'c', 'target': 'missing'})])))
    assert backend.read() == before
