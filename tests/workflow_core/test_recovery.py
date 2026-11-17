import copy

import pytest

from mewcode.workflow_core import recovery
from mewcode.workflow_core.runtime import Core, MemoryBackend
from mewcode.workflow_core.state import fresh, start_attempt, finish_attempt
from tests.workflow_core.test_controls import LegacyLimitCore
from tests.workflow_core.test_runtime import graph


REQUEST = '检查为什么卡住并继续'


@pytest.mark.parametrize('text', [REQUEST, '请排查任务阻塞，修复后从检查点继续',
    '修复当前编排的运行问题', '检查流程为什么卡住，然后继续', 'diagnose the stuck workflow and resume'])
def test_direct_recovery_requests(text):
    assert recovery.chat_intent(text) == 'recover'


@pytest.mark.parametrize('text', ['看看为什么卡住', '检查任务阻塞原因，不要继续',
    'diagnose the blocked workflow, do not resume'])
def test_inspection_does_not_imply_execution(text):
    assert recovery.chat_intent(text) == 'inspect'


@pytest.mark.parametrize('text', ['能否在工作台检查为什么卡住并继续？', '实现检查为什么卡住并继续这个功能',
    '说明“检查为什么卡住并继续”的作用', '帮我检查这份运行文档的错别字', '继续安装这个技能'])
def test_other_chat_is_not_recovery(text):
    assert not recovery.chat_intent(text)


def report(state, **kwargs):
    return recovery.diagnose(dict(run_id='a' * 32, workflow_core=state), **kwargs)


@pytest.mark.asyncio
async def test_recover_keeps_deliveries_context_unknown_receipts_and_unaffected_work():
    backend = MemoryBackend(fresh(graph(['source', 'work', 'store', 'other'], [
        ('source', 'work', 'always'), ('work', 'store', 'always')])))
    calls = []
    async def execute(ctx):
        calls.append(ctx.node_id)
        if ctx.node_id == 'work' and calls.count('work') == 1:
            await ctx.publish('checkpoint', messages=[dict(role='assistant', content='已写入原材料，等待导出结果')],
                pending_tools={'export': dict(name='Bash', status='unknown')})
            return dict(status='blocked', reason='业务结果需要核对', text='已处理一半')
        if ctx.node_id == 'work':
            assert ctx.checkpoint['messages'][0]['content'] == '已写入原材料，等待导出结果'
            assert ctx.checkpoint['pending_tools']['export']['status'] == 'unknown'
            assert ctx.recovery['request'] == REQUEST
            assert any(row['kind'] == 'unknown_tools' for row in ctx.recovery['findings'])
        return dict(text='已完成 ' + ctx.node_id)
    first = await Core(backend, execute).run()
    original = copy.deepcopy(first['attempts'])
    deliveries = copy.deepcopy(first['deliveries'])
    graph_before = copy.deepcopy(first['graph'])
    observed = report(first, live={'scheduler_active': False})
    receipt = backend.change(lambda state: recovery.resume(state, observed, request_id='repair', text=REQUEST))
    after = backend.read()
    assert backend.change(lambda state: recovery.resume(state, observed, request_id='repair', text=REQUEST)) == receipt
    assert backend.read() == after
    result = await Core(backend, execute).run()
    assert result['status'] == 'completed'
    assert calls.count('source') == calls.count('other') == calls.count('store') == 1
    assert calls.count('work') == 2
    assert result['graph'] == graph_before
    assert all(result['attempts'][key] == value for key, value in original.items())
    assert all(result['deliveries'][key] == value for key, value in deliveries.items())
    assert result['recovery_requests']['repair']['diagnosis']['retained']['attempts'] == len(original)


@pytest.mark.asyncio
async def test_recover_loop_limit_enters_next_round_without_resetting_totals(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('mewcode.workflow_core.runtime.time.time', lambda: clock[0])
    backend = MemoryBackend(fresh(graph([dict(id='loop', kind='loop', config=dict(mode='count', count=2,
        limits={'seconds': 1}, graph=graph(['work'])))])))
    calls = []
    async def execute(ctx):
        calls.append(ctx.iteration)
        clock[0] += 2
        return dict(text='done')
    first = await LegacyLimitCore(backend, execute).run()
    saved = copy.deepcopy(first['nodes']['loop']['loop']['results'])
    observed = report(first)
    assert any(item['kind'] == 'loop_limit' for item in observed['findings'])
    backend.change(lambda state: recovery.resume(state, observed, request_id='continue', text=REQUEST))
    result = await Core(backend, execute).run()
    assert result['status'] == 'completed' and calls == [[0], [1]]
    assert result['nodes']['loop']['loop']['results'][0] == saved[0]
    assert result['nodes']['loop']['loop']['elapsed'] == 4


@pytest.mark.asyncio
async def test_manual_input_is_preserved_while_other_branch_continues():
    backend = MemoryBackend(fresh(graph([dict(id='choice', kind='judge', config={'manual': True}), 'work'])))
    def pause(state):
        choice = start_attempt(state, 'choice', {}, [])
        finish_attempt(state, choice['id'], dict(status='needs_input', reason='请提供真实编号'))
        work = start_attempt(state, 'work', {}, [])
        finish_attempt(state, work['id'], dict(status='paused', text=''))
        state['status'] = 'blocked'
    backend.change(pause)
    saved = backend.read()['nodes']['choice']
    observed = report(backend.read())
    backend.change(lambda state: recovery.resume(state, observed, request_id='continue', text=REQUEST))
    called = []
    async def execute(ctx):
        called.append(ctx.node_id)
        return dict(text='done')
    result = await Core(backend, execute).run()
    assert called == ['work']
    assert result['nodes']['choice'] == saved
    assert result['nodes']['work']['status'] == 'completed'


def test_live_wait_is_not_a_failure_or_restart_trigger():
    state = fresh(graph(['work']))
    attempt = start_attempt(state, 'work', {}, [])
    attempt.update(status='suspended', waiting_on='reply')
    state['status'] = 'running'
    state['node_requests'] = {'reply': dict(id='reply', node_id='work', status='running')}
    observed = report(state, live=dict(scheduler_active=True, active_node_ids=[]))
    before = copy.deepcopy(state['nodes'])
    receipt = recovery.resume(state, observed, request_id='check', text=REQUEST)
    assert receipt['status'] == 'already_running'
    assert state['nodes'] == before
    assert state['attempts'][attempt['id']]['status'] == 'suspended'


def test_completed_run_is_not_restarted_and_request_id_cannot_change_content():
    state = fresh(graph(['work']))
    attempt = start_attempt(state, 'work', {}, [])
    finish_attempt(state, attempt['id'], dict(status='completed', text='done'))
    state['status'] = 'completed'
    before = copy.deepcopy(state['nodes'])
    observed = report(state)
    receipt = recovery.resume(state, observed, request_id='check', text=REQUEST)
    assert receipt['status'] == 'completed' and state['status'] == 'completed'
    assert state['nodes'] == before
    with pytest.raises(ValueError, match='不同消息'):
        recovery.resume(state, observed, request_id='check', text=REQUEST + 'x')


@pytest.mark.asyncio
async def test_failed_supplement_resumes_saved_dialogue_without_redoing_primary(tmp_path):
    from mewcode.workflow_core import dialogue, revisions
    partial, _ = revisions.prepare(tmp_path, 'run', {'id': 'revise'}, {}, tmp_path / 'snapshots')
    (partial / 'chapter.tex').write_text('未发布的 v2 修订', encoding='utf-8')
    backend = MemoryBackend(fresh(graph(['work'])))
    def setup(state):
        original = start_attempt(state, 'work', {}, [])
        finish_attempt(state, original['id'], dict(status='completed', text='v1'))
        request = dialogue.enqueue(state, dict(node_id='work', kind='revise', message='补全末尾', request_id='revise'))
        _, reply, _ = dialogue.start_request(state, state['node_requests'][request['id']])
        state['attempts'][reply['id']]['messages'] = [dict(role='assistant', content='v2 已改，导出尚未完成')]
        state['attempts'][reply['id']]['pending_tools'] = {'export': dict(name='Bash', status='unknown')}
        dialogue.finish_request(state, reply['id'], dict(status='blocked', text='', reason='导出连接断开'))
        state['status'] = 'completed'  # Primary DAG is complete; the supplemental conversation is not.
    backend.change(setup)
    original = backend.read()
    observed = report(original)
    assert observed['retry_requests'] == ['revise'] and not observed['unfinished']
    assert '导出连接断开' in recovery.describe(observed)
    backend.change(lambda state: recovery.resume(state, observed, request_id='repair-reply', text=REQUEST))
    called = []
    async def execute(ctx):
        called.append(ctx.request['id'])
        assert ctx.request['parent_request_id'] == 'revise'
        assert ctx.checkpoint['messages'][0]['content'] == 'v2 已改，导出尚未完成'
        assert ctx.checkpoint['pending_tools']['export']['status'] == 'unknown'
        assert ctx.recovery['request'] == REQUEST
        restored, _ = revisions.prepare(tmp_path, 'run', ctx.request, {}, tmp_path / 'snapshots')
        assert restored == partial
        assert (restored / 'chapter.tex').read_text(encoding='utf-8') == '未发布的 v2 修订'
        return dict(text='v2 已补全')
    result = await Core(backend, execute).run()
    assert result['status'] == 'completed' and len(called) == 1
    assert result['node_requests']['revise'] == original['node_requests']['revise']
    assert all(result['attempts'][key] == row for key, row in original['attempts'].items())
    assert all(result['deliveries'][key] == row for key, row in original['deliveries'].items())
    assert len(result['deliveries']) == 2


def test_historical_failed_reply_and_cancelled_request_are_not_replayed():
    from mewcode.workflow_core import dialogue
    state = fresh(graph(['work']))
    original = start_attempt(state, 'work', {}, [])
    finish_attempt(state, original['id'], dict(status='completed', text='v1'))
    request = dialogue.enqueue(state, dict(node_id='work', kind='revise', message='old request', request_id='old'))
    _, reply, _ = dialogue.start_request(state, state['node_requests'][request['id']])
    dialogue.finish_request(state, reply['id'], dict(status='failed', text='old failure'))
    dialogue.enqueue(state, dict(node_id='work', kind='ask', message='later cancelled', request_id='cancelled'))
    dialogue.cancel(state, 'cancelled')
    observed = report(state)
    assert not observed['retry_requests'] and not observed['pending_requests']
