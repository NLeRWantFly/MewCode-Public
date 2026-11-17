import asyncio
import copy
from pathlib import Path

import pytest

from mewcode.workflow_core import dialogue, files
from mewcode.workflow_core.runtime import Core, MemoryBackend, YieldExecution
from mewcode.workflow_core.state import fresh, start_attempt
from tests.workflow_core.test_runtime import graph


@pytest.mark.asyncio
async def test_completed_node_answers_in_context_without_replaying_any_graph_node():
    backend = MemoryBackend(fresh(graph(['a', 'b', 'other'], [('a', 'b', 'always')])))
    calls = []
    async def execute(ctx):
        calls.append((ctx.node_id, ctx.request.get('kind')))
        if ctx.request:
            assert ctx.checkpoint['messages'][-1]['content'] == 'a original context'
            await ctx.publish('checkpoint', messages=ctx.checkpoint['messages'] + [
                {'role': 'assistant', 'content': 'explanation'}])
            return dict(text='This explains the original result')
        await ctx.publish('checkpoint', messages=[{'role': 'assistant', 'content': ctx.node_id + ' original context'}])
        return dict(text=ctx.node_id + ' delivered')
    await Core(backend, execute).run()
    before = backend.read()
    core = Core(backend, execute)
    core.send_message(dict(node_id='a', kind='ask', message='Why?', request_id='human-1'))
    result = await core.run()
    assert calls == [('a', None), ('other', None), ('b', None), ('a', 'ask')]
    assert result['nodes'] == before['nodes']
    assert result['node_requests']['human-1']['reply']['text'].startswith('This explains')
    assert len(result['deliveries']) == len(before['deliveries'])
    assert result['status'] == 'completed'


@pytest.mark.asyncio
async def test_peer_revision_releases_slot_and_keeps_other_consumers_on_frozen_input(tmp_path):
    flow = graph(['source', 'requester', 'other'], [('source', 'requester', 'always'), ('source', 'other', 'always')])
    backend = MemoryBackend(fresh(flow))
    original = tmp_path / 'page.txt'
    fixing, other_done = asyncio.Event(), asyncio.Event()
    calls, seen = [], []
    async def execute(ctx):
        calls.append((ctx.node_id, bool(ctx.request)))
        if ctx.node_id == 'source':
            if ctx.request:
                assert ctx.checkpoint['messages'][0]['content'] == 'source context'
                fixing.set()
                await asyncio.wait_for(other_done.wait(), 2)
                original.write_text('corrected formula', encoding='utf-8')
                artifact = files.capture(tmp_path / 'runs', 'run', ctx.attempt_id, original)
                await ctx.publish('artifact', artifact=artifact)
                return dict(text='Page 53 corrected')
            original.write_text('missing formula', encoding='utf-8')
            await ctx.publish('artifact', artifact=files.capture(tmp_path / 'runs', 'run', ctx.attempt_id, original))
            await ctx.publish('checkpoint', messages=[{'role': 'assistant', 'content': 'source context'}])
            return dict(text='First delivery')
        if ctx.node_id == 'other':
            await asyncio.wait_for(fixing.wait(), 2)
            assert Path(ctx.inputs['source']['artifacts'][0]['path']).read_text() == 'missing formula'
            other_done.set()
            return dict(text='Unaffected part completed')
        messages = ctx.poll_messages()
        if messages:
            seen.extend(messages)
            assert ctx.inputs['source']['delivery_version'] == 2
            assert Path(ctx.inputs['source']['artifacts'][0]['path']).read_text() == 'corrected formula'
            return dict(text='Continued using corrected page')
        receipt = ctx.send_message(node_id='source', kind='revise', message='Fix missing formula',
            references=['page:53'], request_id='peer-1')
        assert receipt == ctx.send_message(node_id='source', kind='revise', message='Fix missing formula',
            references=['page:53'], request_id='peer-1')
        await ctx.publish('checkpoint', messages=[{'role': 'assistant', 'content': 'Other questions already saved'}])
        ctx.wait_for_reply(receipt['id'])
        pytest.fail('the pending reply must release this slot')
    result = await asyncio.wait_for(Core(backend, execute, concurrency=2).run(), 5)
    assert result['status'] == 'completed'
    assert result['node_requests']['peer-1']['status'] == 'completed'
    assert seen and calls.count(('source', False)) == 1
    assert calls.count(('requester', False)) == 2
    # The completed sibling may receive a separate local-update turn, but its
    # initial graph execution never resets. It has no dependency wait imposed.
    assert calls.count(('other', False)) == 1
    source_versions = [d for d in result['deliveries'].values() if d['node_id'] == 'source']
    assert len(source_versions) == 2
    assert Path(source_versions[0]['result']['artifacts'][0]['stored_path']).read_text() == 'missing formula'


@pytest.mark.asyncio
async def test_single_slot_wait_can_run_previously_completed_producer():
    backend = MemoryBackend(fresh(graph(['a', 'b'], [('a', 'b', 'always')])))
    async def execute(ctx):
        if ctx.request:
            return dict(text='Answer')
        if ctx.node_id == 'a':
            return dict(text='A')
        if ctx.poll_messages():
            return dict(text='B')
        receipt = ctx.send_message(node_id='a', message='Question', request_id='b-question')
        ctx.wait_for_reply(receipt['id'])
    result = await asyncio.wait_for(Core(backend, execute, concurrency=1).run(), 2)
    assert result['status'] == 'completed'
    assert result['node_requests']['b-question']['status'] == 'completed'


@pytest.mark.asyncio
async def test_running_node_services_inbox_at_checkpoint_then_resumes_original_work():
    backend = MemoryBackend(fresh(graph(['a', 'other'])))
    started, proceed = asyncio.Event(), asyncio.Event()
    rounds = []
    async def execute(ctx):
        if ctx.node_id == 'other':
            return dict(text='independent')
        rounds.append(ctx.request.get('kind', 'main'))
        if ctx.request:
            assert ctx.checkpoint['messages'][0]['content'] == 'saved work'
            await ctx.publish('checkpoint', messages=ctx.checkpoint['messages'] + [
                {'role': 'assistant', 'content': 'answer without losing work'}])
            return dict(text='answer')
        if ctx.checkpoint:
            assert ctx.checkpoint['messages'][-1]['content'] == 'answer without losing work'
            return dict(text='original work finished')
        await ctx.publish('checkpoint', messages=[{'role': 'assistant', 'content': 'saved work'}])
        started.set()
        await proceed.wait()
        ctx.cooperate()
        pytest.fail('inbox work should be scheduled at this boundary')
    core = Core(backend, execute, concurrency=2)
    task = asyncio.create_task(core.run())
    await started.wait()
    core.send_message(dict(node_id='a', message='Explain the partial result', request_id='live-question'))
    proceed.set()
    result = await asyncio.wait_for(task, 3)
    assert result['status'] == 'completed' and rounds == ['main', 'ask', 'main']
    assert len([a for a in result['attempts'].values() if a['node_id'] == 'a' and not a.get('request_id')]) == 1


@pytest.mark.asyncio
async def test_requests_for_same_node_are_serial_and_retain_previous_reply():
    backend = MemoryBackend(fresh(graph(['a'])))
    async def original(ctx):
        await ctx.publish('checkpoint', messages=[{'role': 'user', 'content': 'original'}])
        return dict(text='done')
    await Core(backend, original).run()
    active, count = 0, 0
    async def answer(ctx):
        nonlocal active, count
        active += 1
        assert active == 1
        count += 1
        assert len(ctx.checkpoint['messages']) == count
        await asyncio.sleep(0)
        await ctx.publish('checkpoint', messages=ctx.checkpoint['messages'] + [{'role': 'assistant', 'content': str(count)}])
        active -= 1
        return dict(text=str(count))
    core = Core(backend, answer, concurrency=4)
    for index in range(3):
        core.send_message(dict(node_id='a', message=str(index), request_id='question-' + str(index)))
    result = await core.run()
    assert count == 3 and result['nodes']['a']['status'] == 'completed'


@pytest.mark.asyncio
async def test_failed_request_does_not_remove_prior_delivery_or_fail_workflow():
    backend = MemoryBackend(fresh(graph(['a'])))
    async def execute(ctx):
        if ctx.request:
            raise OSError('remote tool unavailable')
        return dict(text='existing usable output')
    await Core(backend, execute).run()
    before = backend.read()['nodes']['a'].copy()
    core = Core(backend, execute)
    core.send_message(dict(node_id='a', kind='revise', message='Supplement it', request_id='failed-revision'))
    result = await core.run()
    assert result['nodes']['a'] == before
    assert result['node_requests']['failed-revision']['status'] == 'failed'
    assert result['status'] == 'completed'


def test_circular_wait_returns_feedback_instead_of_holding_all_slots():
    state = fresh(graph(['a', 'b']))
    a, b = [start_attempt(state, key, {}, []) for key in ['a', 'b']]
    first = dialogue.enqueue(state, dict(node_id='b', message='B please', request_id='to-b'), sender=a)
    second = dialogue.enqueue(state, dict(node_id='a', message='A please', request_id='to-a'), sender=b)
    assert dialogue.wait_for(state, a['id'], first['id'])['status'] == 'waiting'
    with pytest.raises(ValueError, match='循环'):
        dialogue.wait_for(state, b['id'], second['id'])
    assert 'waiting_on' not in b and state['status'] == 'running'


@pytest.mark.asyncio
async def test_restart_resumes_interrupted_reply_checkpoint_without_replaying_main(tmp_path):
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.workflow_core.store import ensure
    from mewcode.workflow_core.web import envelope
    flow = graph(['a'])
    rid = 'c' * 32
    store = ExpertRunStore(tmp_path / 'runs', storage='sqlite')
    store.create(run_id=rid, conversation_id='restart', task='persist replies', attachments=[],
                 profile={}, strategy='stable', flow=envelope(flow))
    backend = ensure(store, rid, flow)
    async def initial(ctx):
        return dict(text='original')
    await Core(backend, initial).run()
    backend.change(lambda state: dialogue.enqueue(state, dict(node_id='a', message='more', request_id='resume-reply')))
    def interrupted(state):
        _, attempt, _ = dialogue.start_request(state, state['node_requests']['resume-reply'])
        saved = state['attempts'][attempt['id']]
        saved.update(messages=[{'role': 'assistant', 'content': 'partial reply'}],
                     pending_tools={'external': {'status': 'unknown'}})
        dialogue.checkpoint(state, saved)
    backend.change(interrupted)
    async def resumed(ctx):
        assert ctx.request['id'] == 'resume-reply'
        assert ctx.checkpoint['messages'][-1]['content'] == 'partial reply'
        assert ctx.checkpoint['pending_tools']['external']['status'] == 'unknown'
        return dict(text='reconciled and answered')
    reopened = ensure(ExpertRunStore(tmp_path / 'runs', storage='sqlite'), rid)
    result = await Core(reopened, resumed).run()
    assert result['status'] == 'completed'
    assert len(result['attempts']) == 2


@pytest.mark.asyncio
async def test_old_loop_item_can_answer_without_changing_current_item_delivery():
    flow = graph([dict(id='pages', kind='loop', config=dict(mode='foreach', items=['first', 'second'], graph=graph(['work'])))])
    backend = MemoryBackend(fresh(flow))
    async def execute(ctx):
        if ctx.request:
            assert ctx.iteration == [0]
            assert ctx.checkpoint['messages'][0]['content'] == 'first'
            return dict(text='first item explained')
        await ctx.publish('checkpoint', messages=[{'role': 'user', 'content': ctx.inputs['item']}])
        return dict(text=ctx.inputs['item'])
    await Core(backend, execute).run()
    before = backend.read()
    first = next(a for a in before['attempts'].values() if a['node_id'] == 'pages/work' and a['iteration'] == [0])
    core = Core(backend, execute)
    core.send_message(dict(node_id='pages/work', target_attempt_id=first['id'], message='First item?', request_id='old-item'))
    result = await core.run()
    assert result['nodes'] == before['nodes']
    assert result['node_requests']['old-item']['status'] == 'completed'


@pytest.mark.asyncio
async def test_cancel_only_one_request_keeps_original_completed_result():
    backend = MemoryBackend(fresh(graph(['a'])))
    async def initial(ctx):
        return dict(text='existing')
    await Core(backend, initial).run()
    core = Core(backend, initial)
    core.send_message(dict(node_id='a', message='more', request_id='cancel-me'))
    core.control(dict(operation='cancel_request', message_id='cancel-me'))
    result = await core.run()
    assert result['node_requests']['cancel-me']['status'] == 'cancelled'
    assert len(result['attempts']) == 1
    assert result['nodes']['a']['result']['text'] == 'existing'


@pytest.mark.asyncio
async def test_waiting_amendment_keeps_next_amendment_serial():
    backend = MemoryBackend(fresh(graph(['a', 'b'])))
    calls = []
    async def execute(ctx):
        if not ctx.request:
            return dict(text='initial')
        identity = ctx.request['id']
        calls.append(identity)
        if identity == 'first' and not ctx.poll_messages():
            receipt = ctx.send_message(node_id='b', message='Need a fact', request_id='fact')
            ctx.wait_for_reply(receipt['id'])
        return dict(text='answered')
    await Core(backend, execute).run()
    core = Core(backend, execute, concurrency=1)
    core.send_message(dict(node_id='a', kind='revise', message='First correction', request_id='first'))
    core.send_message(dict(node_id='a', kind='revise', message='Second correction', request_id='second'))
    result = await asyncio.wait_for(core.run(), 3)
    assert result['status'] == 'completed'
    assert calls == ['first', 'fact', 'first', 'second']


@pytest.mark.asyncio
async def test_original_work_gets_a_turn_between_queued_questions():
    backend = MemoryBackend(fresh(graph(['a'])))
    started, proceed = asyncio.Event(), asyncio.Event()
    calls = []
    async def execute(ctx):
        if ctx.request:
            calls.append(ctx.request['id'])
            return dict(text='reply')
        if ctx.checkpoint:
            calls.append('original-resumed')
            return dict(text='delivered')
        await ctx.publish('checkpoint', messages=[{'role': 'assistant', 'content': 'work saved'}])
        started.set()
        await proceed.wait()
        ctx.cooperate()
    core = Core(backend, execute, concurrency=1)
    task = asyncio.create_task(core.run())
    await started.wait()
    for index in range(3):
        core.send_message(dict(node_id='a', message=str(index), request_id='q' + str(index)))
    proceed.set()
    result = await asyncio.wait_for(task, 3)
    assert result['status'] == 'completed'
    assert calls == ['q0', 'original-resumed', 'q1', 'q2']


def test_reply_remains_pending_until_its_conversation_checkpoint_is_acknowledged():
    state = fresh(graph(['a', 'b']))
    a, b = [start_attempt(state, key, {}, []) for key in ['a', 'b']]
    request = dialogue.enqueue(state, dict(node_id='a', message='fact', request_id='pending-reply'), sender=b)
    _, attempt, _ = dialogue.start_request(state, state['node_requests'][request['id']])
    dialogue.finish_request(state, attempt['id'], dict(text='42'))
    first = dialogue.receive(state, b['id'], ack=False)
    restarted = copy.deepcopy(state)
    assert dialogue.receive(restarted, b['id'], ack=False) == first
    dialogue.acknowledge(restarted, b['id'], [item['id'] for item in first])
    assert dialogue.receive(restarted, b['id'], ack=False) == []


@pytest.mark.asyncio
async def test_requester_that_delivers_before_reply_receives_a_local_continuation():
    backend = MemoryBackend(fresh(graph(['a', 'b'], [('a', 'b', 'always')])))
    delivered = asyncio.Event()
    calls = []
    async def execute(ctx):
        calls.append((ctx.node_id, ctx.request.get('kind')))
        if ctx.node_id == 'a':
            if ctx.request:
                await delivered.wait()
                return dict(text='Requested explanation')
            return dict(text='Original source')
        if ctx.request:
            messages = ctx.poll_messages()
            assert messages[0]['reply']['text'] == 'Requested explanation'
            return dict(text='Used late explanation; unaffected results retained')
        ctx.send_message(node_id='a', message='Explain this', request_id='late-answer')
        delivered.set()
        return dict(text='Finished independent parts; question remains pending')
    result = await asyncio.wait_for(Core(backend, execute, concurrency=2).run(), 3)
    assert result['status'] == 'completed'
    assert calls == [('a', None), ('b', None), ('a', 'ask'), ('b', 'update')]
    assert result['nodes']['b']['result']['text'].startswith('Finished independent parts')
    initial_b = next(d for d in result['deliveries'].values() if d['node_id'] == 'b')
    assert initial_b['unresolved_requests'] == ['late-answer']
    assert initial_b['input_versions']['a']
    assert result['node_requests']['reply-update:late-answer']['reply']['text'].startswith('Used late')
