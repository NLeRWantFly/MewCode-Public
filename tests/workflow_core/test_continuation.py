import asyncio
import copy
import json

import pytest

from mewcode.workflow_core import continuation, dialogue
from mewcode.workflow_core.activity import observe_view
from mewcode.workflow_core.projection import view
from mewcode.workflow_core.runtime import Core, MemoryBackend
from mewcode.workflow_core.state import fresh, start_attempt, finish_attempt
from tests.workflow_core.test_runtime import graph


@pytest.mark.asyncio
async def test_transient_failure_continues_own_checkpoint_and_keeps_other_work():
    backend = MemoryBackend(fresh(graph(['source','work','other','join'],[
        ('source','work','always'),('work','join','always'),('other','join','always')])))
    calls, release = [], asyncio.Event()
    messages = [dict(role='assistant',content='已写入半成品，继续未完成部分')]
    async def execute(ctx):
        calls.append(ctx.node_id)
        if ctx.node_id=='other':
            await asyncio.wait_for(release.wait(),3)
        if ctx.node_id=='work':
            if calls.count('work')==1:
                await ctx.publish('checkpoint',messages=messages,pending_tools={'check':dict(name='Bash',status='unknown')})
                return dict(status='blocked',reason='Network error: Request timed out.')
            assert ctx.checkpoint['messages']==messages
            assert ctx.checkpoint['pending_tools']['check']['status']=='unknown'
            assert ctx.recovery['automatic_continuation']['kind']=='transport'
            release.set()
        return dict(text='交付')
    result=await Core(backend,execute,concurrency=2,retry_delay=0).run()
    assert result['status']=='completed'
    assert calls.count('source')==calls.count('other')==calls.count('join')==1
    assert calls.count('work')==2
    failed=next(a for a in result['attempts'].values() if a['status']=='blocked')
    assert failed['messages']==messages and failed['result']['reason'].startswith('Network')


@pytest.mark.asyncio
async def test_context_failure_preserves_full_archive_but_starts_with_bounded_note():
    backend=MemoryBackend(fresh(graph(['work'])))
    original=[dict(role='user',content='very large history '*10000)]
    calls=[]
    async def execute(ctx):
        calls.append(ctx)
        if len(calls)==1:
            await ctx.publish('checkpoint',messages=original,pending_tools={})
            return dict(status='blocked',reason='上下文压缩未能腾出足够空间；压缩仅减少约191 tokens，未达到最低收益')
        assert continuation.restart_context(ctx.checkpoint,ctx.recovery)
        assert ctx.checkpoint['messages']==original
        assert len(continuation.context_note(ctx.checkpoint))<20000
        assert 'ReadNodeCheckpoint' in continuation.context_note(ctx.checkpoint)
        return dict(text='已接续')
    result=await Core(backend,execute,retry_delay=0).run()
    assert result['status']=='completed' and len(calls)==2


@pytest.mark.asyncio
async def test_manual_pause_stops_pending_automatic_recovery():
    backend=MemoryBackend(fresh(graph(['work'])))
    failed=asyncio.Event()
    async def emit(event):
        if event['type']=='workflow_node_finished':
            failed.set()
    async def execute(ctx):
        return dict(status='blocked',reason='Network error: timeout')
    core=Core(backend,execute,emit=emit,retry_delay=30)
    task=asyncio.create_task(core.run())
    await asyncio.wait_for(failed.wait(),3)
    core.control(dict(operation='pause',request_id='user-pause'))
    result=await asyncio.wait_for(task,3)
    assert result['status']=='paused' and len(result['attempts'])==1
    assert continuation.pending(result)==[]
    continuation.advance(result,now=10**12)
    assert len(result['attempts'])==1 and result['nodes']['work']['status']=='blocked'


@pytest.mark.asyncio
async def test_pause_after_admission_prevents_any_model_dispatch():
    backend=MemoryBackend(fresh(graph(['work'])))
    called=[]
    async def execute(ctx):
        called.append(ctx.node_id)
        return dict(text='should not run')
    core=Core(backend,execute)
    entries=backend.change(core.tick)
    core.control(dict(operation='pause',request_id='pause-before-dispatch'))
    await core.dispatch(entries)
    await asyncio.gather(*core.tasks.values())
    assert not called
    assert backend.read()['status']=='paused'


@pytest.mark.asyncio
async def test_individually_paused_branch_is_never_automatically_restarted():
    backend=MemoryBackend(fresh(graph(['work','other'])))
    async def execute(ctx):
        if ctx.node_id=='work':
            return dict(status='blocked',reason='Request timed out')
        return dict(text='其他工作已交付')
    core=None
    async def emit(event):
        if event['type']=='workflow_node_finished' and event['node_id']=='work':
            core.control(dict(operation='pause',node_id='work',request_id='pause-branch'))
    core=Core(backend,execute,emit=emit,retry_delay=30)
    result=await core.run()
    assert result['status']=='paused'
    assert result['nodes']['other']['status']=='completed'
    assert sum(a['node_id']=='work' for a in result['attempts'].values())==1


@pytest.mark.asyncio
@pytest.mark.parametrize('status,reason',[
    ('paused','用户暂停'),('needs_input','需要用户提供输入'),('blocked','API error (400): invalid model'),
    ('failed','业务条件不满足')])
async def test_control_input_and_business_failures_are_not_blindly_retried(status,reason):
    async def execute(ctx):
        return dict(status=status,reason=reason)
    result=await Core(MemoryBackend(fresh(graph(['work']))),execute,retry_delay=0).run()
    assert len(result['attempts'])==1
    assert not result.get('automatic_continuations')


@pytest.mark.asyncio
async def test_failed_followup_continues_working_copy_without_repeating_primary_delivery():
    backend=MemoryBackend(fresh(graph(['producer'])))
    async def initial(ctx):
        return dict(text='原交付')
    await Core(backend,initial).run()
    original=copy.deepcopy(backend.read()['deliveries'])
    backend.change(lambda state:dialogue.enqueue(state,dict(node_id='producer',kind='revise',message='定向补交',request_id='request-a')))
    calls=[]
    async def revise(ctx):
        assert ctx.request['kind']=='revise'
        calls.append(ctx)
        if len(calls)==1:
            await ctx.publish('checkpoint',messages=[dict(role='assistant',content='已修改一半')],pending_tools={})
            return dict(status='blocked',reason='connection reset')
        assert ctx.request['working_copy_id']=='request-a'
        assert ctx.checkpoint['messages'][0]['content']=='已修改一半'
        return dict(text='定向补交完成')
    result=await Core(backend,revise,retry_delay=0).run()
    assert result['status']=='completed' and len(calls)==2
    assert all(result['deliveries'][key]==value for key,value in original.items())
    assert result['node_requests']['request-a']['status']=='failed'
    assert result['attempts'][result['node_requests']['request-a']['attempt_id']]['status']=='blocked'


def interrupted_state():
    state=fresh(graph(['interrupted','active']))
    state['status']='running'
    failure=start_attempt(state,'interrupted',{},[])
    failure['messages']=[dict(role='assistant',content='已保存原文件')]
    finish_attempt(state,failure['id'],dict(status='blocked',reason='Network error: timeout'))
    start_attempt(state,'active',{},[])
    return state,failure


def test_partial_failure_and_recovery_labels_match_live_branch_state():
    state,failure=interrupted_state()
    current=observe_view(view(dict(run_id='r',workflow_core=state)),
        live=dict(scheduler_active=True,active_node_ids=['active']))
    from mewcode.orchestration.task_view import finish
    assert finish(current)['task']['status']=='部分受阻'
    continuation.schedule(state,failure['id'],delay=30)
    current=finish(observe_view(view(dict(run_id='r',workflow_core=state)),
        live=dict(scheduler_active=True,active_node_ids=['active'])))
    assert current['task']['status']=='恢复中'
    assert current['task']['controls']==['pause','cancel']
    assert current['plan'][0]['steps'][0]['status']=='恢复中'
    state.update(status='paused',stop_requested=True)
    assert view(dict(run_id='r',workflow_core=state))['task']['status']=='已暂停'


def test_archived_checkpoint_is_paged_and_only_readable_by_its_own_node():
    state,source=interrupted_state()
    current=start_attempt(state,'interrupted',{},[])
    page=continuation.checkpoint_page(state,current['id'],attempt_id=source['id'],section='messages',limit=12)
    rest=continuation.checkpoint_page(state,current['id'],attempt_id=source['id'],section='messages',offset=12)
    assert json.loads(page['text']+rest['text'])==source['messages'][0]
    other=state['nodes']['active']['attempt_id']
    with pytest.raises(ValueError,match='当前节点'):
        continuation.checkpoint_page(state,current['id'],attempt_id=other)


def test_historical_failure_and_old_graph_generation_cannot_be_replayed():
    state,failed=interrupted_state()
    continuation.schedule(state,failed['id'],delay=0)
    state['nodes']['interrupted']['generation']+=1
    continuation.advance(state,now=10**12)
    assert state['nodes']['interrupted']['status']=='blocked'
    assert continuation.pending(state)==[]


def test_dialogue_on_paused_graph_does_not_schedule_primary_recovery():
    state, failed = interrupted_state()
    state['dialogue_only'] = True
    assert not continuation.schedule(state, failed['id'], delay=0)
    assert state['nodes']['interrupted']['attempt_id'] == failed['id']
    assert state['nodes']['interrupted']['status'] == 'blocked'


def test_graph_replacement_clears_already_admitted_recovery_hint():
    from mewcode.workflow_core.state import invalidate
    state, failed = interrupted_state()
    continuation.schedule(state, failed['id'], delay=0)
    continuation.advance(state, now=10**12)
    assert state['nodes']['interrupted']['automatic_continuation']
    invalidate(state, {'interrupted'})
    assert not state['nodes']['interrupted'].get('automatic_continuation')
    assert not state['nodes']['interrupted'].get('resume_checkpoint')


def test_latest_request_uses_creation_time_after_storage_reorders_keys():
    state, _ = interrupted_state()
    old = dialogue.enqueue(state, dict(node_id='active', kind='ask', message='旧问题', request_id='z-old'))
    _, attempt, _ = dialogue.start_request(state, state['node_requests'][old['id']])
    dialogue.finish_request(state, attempt['id'], dict(status='blocked', reason='Request timed out'))
    new = dialogue.enqueue(state, dict(node_id='active', kind='ask', message='新问题', request_id='a-new'))
    state['node_requests'][old['id']]['created_at'] = 1
    state['node_requests'][new['id']]['created_at'] = 2
    state['node_requests'] = dict(sorted(state['node_requests'].items()))
    assert not continuation.schedule(state, attempt['id'], delay=0)
