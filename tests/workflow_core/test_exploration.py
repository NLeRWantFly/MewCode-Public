import asyncio
import copy

import pytest

from mewcode.workflow_core import continuation, dialogue, presentation
from mewcode.workflow_core.exploration import GoalCore, PROFILE, tasks_ready, update
from mewcode.workflow_core.recipes import RecipeStore, crystallize, instantiate
from mewcode.workflow_core.runtime import MemoryBackend, YieldExecution
from mewcode.workflow_core.state import command, fresh, start_attempt, finish_attempt, invalidate
from tests.workflow_core.test_runtime import graph

MODEL = dict(provider_key='fixture', model_id='gpt-6.1-sol', effort='max')


def goal_state():
    flow = graph([dict(id='goal', kind='work', label='行测章节目标', task='完成行测章节试跑',
        model=MODEL, config=dict(exploration_owner=True, presentation=True))])
    flow.update(execution_profile=PROFILE, concurrency=3, run_parameters=['source', 'output_dir'])
    state = fresh(flow)
    state['inputs'] = dict(source='source-chapter.json', output_dir='original-output')
    return state


def item(identity, dependencies=()):
    return dict(id=identity, label=identity, task='使用 source-chapter.json，写入 original-output/' + identity,
                depends_on=list(dependencies), inputs={})


@pytest.mark.asyncio
async def test_live_fanout_releases_parent_and_fast_successor_does_not_wait_for_slow_branch():
    backend = MemoryBackend(goal_state())
    release, calls, events = asyncio.Event(), [], []
    async def execute(ctx):
        calls.append(ctx.node_id)
        if ctx.node_id == 'goal':
            if not ctx.checkpoint:
                ctx.channel('update_tasks', request_id='first', items=[item('fast'), item('slow'), item('next', ['fast'])])
                await ctx.publish('checkpoint', messages=[dict(role='assistant', content='已安排当前工作')], pending_tools={})
                ctx.channel('wait_tasks', node_ids=['fast', 'slow', 'next'])
                raise YieldExecution()
            assert all(row['status'] == 'completed' for row in ctx.inputs['workflow_tasks'])
            return dict(text='正常交付')
        if ctx.node_id == 'slow':
            await asyncio.wait_for(release.wait(), 3)
        if ctx.node_id == 'next':
            assert 'slow' in calls and backend.read()['nodes']['slow']['status'] == 'running'
            release.set()
        return dict(text=ctx.node_id + '已交付')
    core = GoalCore(backend, execute, concurrency=3, emit=events.append)
    result = await asyncio.wait_for(core.run(), 5)
    assert result['status'] == 'completed' and len(result['attempts']) == 4
    assert calls.count('goal') == 2 and all(calls.count(name) == 1 for name in ('fast', 'slow', 'next'))
    assert result['execution_metrics']['peak_active'] >= 2
    assert len([event for event in events if event['type'] == 'workflow_node_started']) == 5


@pytest.mark.asyncio
async def test_parent_waits_through_child_transport_recovery_and_keeps_checkpoint():
    backend, calls = MemoryBackend(goal_state()), []
    async def execute(ctx):
        calls.append(ctx.node_id)
        if ctx.node_id == 'goal':
            if not ctx.checkpoint:
                ctx.channel('update_tasks', request_id='first', items=[item('work')])
                await ctx.publish('checkpoint', messages=[dict(role='assistant', content='等待实际交付')], pending_tools={})
                ctx.channel('wait_tasks', node_ids=['work'])
                raise YieldExecution()
            assert ctx.inputs['workflow_tasks'][0]['status'] == 'completed'
            return dict(text='最终交付')
        if calls.count('work') == 1:
            await ctx.publish('checkpoint', messages=[dict(role='assistant', content='已保存部分内容')], pending_tools={})
            return dict(status='blocked', reason='Provider disconnected')
        assert ctx.checkpoint['messages'][0]['content'] == '已保存部分内容'
        return dict(text='剩余工作完成')
    result = await asyncio.wait_for(GoalCore(backend, execute, retry_delay=0).run(), 5)
    assert result['status'] == 'completed' and calls == ['goal', 'work', 'work', 'goal']
    assert any(attempt['status'] == 'blocked' for attempt in result['attempts'].values())


@pytest.mark.asyncio
async def test_pause_and_new_scheduler_resume_only_unfinished_work():
    backend, calls = MemoryBackend(goal_state()), []
    slow_started = asyncio.Event()
    core = None
    async def emit(event):
        if event['type'] == 'workflow_node_finished' and event['node_id'] == 'fast':
            core.control(dict(operation='pause', request_id='user-pause'))
    async def execute(ctx):
        calls.append(ctx.node_id)
        if ctx.node_id == 'goal':
            if not ctx.checkpoint:
                ctx.channel('update_tasks', request_id='first', items=[item('fast'), item('slow')])
                await ctx.publish('checkpoint', messages=[dict(role='assistant', content='已保存目标上下文')], pending_tools={})
                ctx.channel('wait_tasks', node_ids=['fast', 'slow'])
                raise YieldExecution()
            return dict(text='接续交付')
        if ctx.node_id == 'slow' and calls.count('slow') == 1:
            await ctx.publish('checkpoint', messages=[dict(role='assistant', content='已有未完成成果')], pending_tools={})
            slow_started.set()
            await asyncio.Event().wait()
        if ctx.node_id == 'fast':
            await asyncio.wait_for(slow_started.wait(), 3)
        if ctx.node_id == 'slow':
            assert ctx.checkpoint['messages'][0]['content'] == '已有未完成成果'
        return dict(text='完成')
    core = GoalCore(backend, execute, emit=emit)
    paused = await asyncio.wait_for(core.run(), 5)
    assert paused['status'] == 'paused' and calls.count('fast') == 1
    renewed = MemoryBackend(paused)
    renewed.change(lambda state:command(state, dict(operation='resume', request_id='user-resume')))
    done = await asyncio.wait_for(GoalCore(renewed, execute).run(), 5)
    assert done['status'] == 'completed' and calls.count('fast') == 1 and calls.count('slow') == 2


def running_owner():
    state = goal_state()
    state['status'] = 'running'
    actor = start_attempt(state, 'goal', {}, [])
    return state, actor


def test_incremental_updates_are_idempotent_and_preserve_started_work():
    state, actor = running_owner()
    first = update(state, actor['id'], request_id='same', items=[item('first')])
    assert update(state, actor['id'], request_id='same', items=[item('first')]) == first
    work = start_attempt(state, 'first', {}, [])
    snapshot = copy.deepcopy(state['nodes']['first'])
    update(state, actor['id'], request_id='next', items=[item('second', ['first'])])
    assert state['nodes']['first'] == snapshot
    assert state['attempts'][work['id']]['status'] == 'running'
    with pytest.raises(ValueError, match='已有工作项'):
        update(state, actor['id'], request_id='rewrite', items=[dict(item('first'), task='重写已有工作')])
    with pytest.raises(ValueError, match='当前目标主对话'):
        update(state, actor['id'], request_id='deadlock', items=[item('third', ['goal'])])
    assert all(node['model'] == MODEL for node in state['graph']['nodes'])


def test_obsolete_recovery_ticket_does_not_leave_parent_waiting_forever():
    state, actor = running_owner()
    update(state, actor['id'], request_id='new', items=[item('work')])
    attempt = start_attempt(state, 'work', {}, [])
    finish_attempt(state, attempt['id'], dict(status='blocked', reason='timeout'))
    continuation.schedule(state, attempt['id'], delay=30)
    actor['waiting_tasks'] = ['work']
    assert not tasks_ready(state, actor)
    current = start_attempt(state, 'work', {}, [])
    finish_attempt(state, current['id'], dict(text='人工接续已正常交付'))
    assert tasks_ready(state, actor)


def test_replanning_pending_work_and_replacing_failure_preserves_completed_upstream():
    state, actor = running_owner()
    update(state, actor['id'], request_id='first', items=[item('source'),item('bad',['source']),item('next',['bad'])])
    source = start_attempt(state,'source',{},[])
    finish_attempt(state,source['id'],dict(text='上游有效成果'))
    original = copy.deepcopy(state['nodes']['source'])
    update(state,actor['id'],request_id='revise',items=[dict(item('next',['bad']),task='尚未开始的新要求')])
    failed = start_attempt(state,'bad',{},[])
    finish_attempt(state,failed['id'],dict(status='blocked',reason='当前方法不可用'))
    update(state,actor['id'],request_id='replace',items=[dict(item('alternate',['source']),replaces=['bad'])])
    assert state['nodes']['source'] == original
    assert state['nodes']['bad']['status'] == 'skipped' and state['nodes']['bad']['superseded_by'] == 'alternate'
    assert state['attempts'][failed['id']]['status'] == 'blocked'
    assert any(edge['source']=='alternate' and edge['target']=='next' for edge in state['graph']['edges'])
    assert not any(edge['source']=='bad' for edge in state['graph']['edges'])
    with pytest.raises(ValueError, match='已交付'):
        update(state,actor['id'],request_id='bad-replace',items=[dict(item('overwrite'),replaces=['source'])])


def test_recipe_executes_saved_graph_with_new_parameters_and_versions(tmp_path):
    state, actor = running_owner()
    update(state, actor['id'], request_id='first', items=[item('first')])
    result = start_attempt(state, 'first', {}, [])
    finish_attempt(state, result['id'], dict(text='已交付'))
    finish_attempt(state, actor['id'], dict(text='目标已交付'))
    state['status'] = 'completed'
    store = RecipeStore(tmp_path)
    recipe = store.save(crystallize(state, source_run_id='real-source'))
    assert [node['id'] for node in recipe['graph']['nodes']] == ['first']
    assert '{{run_inputs.source}}' in recipe['graph']['nodes'][0]['task']
    recipe['graph']['nodes'][0]['task'] = '保存版本的实际内容 {{run_inputs.output_dir}}'
    second = store.save(recipe)
    flow, bindings = instantiate(store.read(recipe['id']), dict(source='new-chapter.json', output_dir='new-output'))
    assert second['version'] == 2 and store.read(recipe['id'], 1)['version'] == 1
    assert flow['nodes'][0]['task'] == '保存版本的实际内容 new-output'
    assert flow['recipe']['version'] == 2 and bindings == {'first': {}}
    assert len(store.list()) == 1
    with pytest.raises(ValueError, match='全部参数'):
        instantiate(recipe, dict(source='new-chapter.json'))


def test_visual_blocks_survive_continuation_but_stale_attempt_cannot_overwrite():
    state, actor = running_owner()
    block = dict(block_id='structure', slot='output', kind='diagram', title='章节关系',
                 nodes=[dict(id='a', label='材料'), dict(id='b', label='输出')], edges=[dict(source='a', target='b')])
    presentation.publish(state, actor['id'], **block)
    newer = start_attempt(state, 'goal', {}, [])
    assert presentation.visible(state, newer)[0]['title'] == '章节关系'
    with pytest.raises(ValueError, match='新代次'):
        presentation.publish(state, actor['id'], **dict(block, title='迟到覆盖'))
    invalidate(state, {'goal'})
    fresh_attempt = start_attempt(state, 'goal', {}, [])
    assert presentation.visible(state, fresh_attempt) == []


@pytest.mark.parametrize('block', [
    dict(kind='chart', labels=['a'], series=[dict(name='count', values=[float('nan')])]),
    dict(kind='table', columns=['a', 'b'], rows=[[1]]),
    dict(kind='diagram', nodes=[dict(id='a',label='a')], edges=[dict(source='a',target='missing')]),
    dict(kind='image', artifact_id='not-registered'),
])
def test_invalid_visual_feedback_stays_inside_node(block):
    state, actor = running_owner()
    with pytest.raises(ValueError):
        presentation.publish(state, actor['id'], block_id='bad', slot='output', title='测试', **block)
    assert state['status'] == state['nodes']['goal']['status'] == 'running'


@pytest.mark.asyncio
async def test_recipe_storage_failure_does_not_reject_normal_delivery():
    class UnavailableStore:
        def save(self, _):
            raise OSError('storage unavailable')
    backend = MemoryBackend(goal_state())
    async def execute(ctx):
        if ctx.node_id == 'goal' and not ctx.checkpoint:
            ctx.channel('update_tasks', request_id='first', items=[item('work')])
        return dict(text='正常交付')
    state = await GoalCore(backend, execute, recipe_store=UnavailableStore()).run()
    assert state['status'] == 'completed' and 'storage unavailable' in state['recipe_error']


@pytest.mark.asyncio
async def test_scoped_material_tools_only_read_bound_input_and_write_trial_output(tmp_path):
    from mewcode.tools.read_file import ReadFile, Params as ReadParams
    from mewcode.tools.write_file import WriteFile, Params as WriteParams
    from mewcode.workflow_core.scoped_io import ScopedFileTool
    source, output = tmp_path/'math', tmp_path/'trial'
    source.mkdir()
    output.mkdir()
    (source/'chapter.txt').write_text('数学材料',encoding='utf-8')
    outside = tmp_path/'outside.txt'
    outside.write_text('不属于试跑材料',encoding='utf-8')
    reader = ScopedFileTool(ReadFile(),output,[source,output],[output])
    writer = ScopedFileTool(WriteFile(),output,[source,output],[output])
    assert not (await reader.execute(ReadParams(file_path=str(source/'chapter.txt')))).is_error
    assert (await reader.execute(ReadParams(file_path=str(outside)))).is_error
    assert (await writer.execute(WriteParams(file_path=str(source/'chapter.txt'),content='改写'))).is_error
    assert not (await writer.execute(WriteParams(file_path=str(output/'result.txt'),content='试跑结果'))).is_error
    assert (source/'chapter.txt').read_text(encoding='utf-8') == '数学材料'
