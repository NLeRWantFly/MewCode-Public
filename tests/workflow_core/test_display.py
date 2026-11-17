import copy
import json
from pathlib import Path
import subprocess

import pytest
import pytest_asyncio

from mewcode.orchestration.task_view import finish
from mewcode.workflow_core import continuation, dialogue
from mewcode.workflow_core.activity import observe_view
from mewcode.workflow_core.projection import view
from mewcode.workflow_core.runtime import MemoryBackend
from mewcode.workflow_core.state import fresh, start_attempt, finish_attempt
from tests.workflow_core.test_controls import LegacyLimitCore
from tests.workflow_core.test_runtime import graph


async def make_paused_graph():
    flow = graph([
        dict(id='pilot', label='样章', kind='loop', config=dict(mode='count', count=1, graph=graph(['make']))),
        dict(id='content', label='正文', kind='loop', config=dict(mode='count', count=2,
            limits={'iterations': 1}, graph=graph(['make', 'check'], [('make', 'check', 'always')]))),
        dict(id='layout', label='排版', kind='loop', config=dict(mode='count', count=1, graph=graph(['compile'])))],
        [('pilot', 'content', 'always'), ('content', 'layout', 'always')])
    backend = MemoryBackend(fresh(flow))
    async def execute(_):
        return dict(text='已交付')
    result = await LegacyLimitCore(backend, execute).run()
    assert result['status'] == 'paused'
    def reply(state):
        request = dialogue.enqueue(state, dict(node_id='content/check', kind='ask', message='核对来源', request_id='ask'))
        dialogue.start_request(state, state['node_requests'][request['id']])
        dialogue.enqueue(state, dict(node_id='content/make', kind='ask', message='核对补交', request_id='next'))
    backend.change(reply)
    return backend.read()


@pytest_asyncio.fixture
async def paused_graph():
    return await make_paused_graph()


def projected(state, active=None):
    return finish(observe_view(view(dict(run_id='run', workflow_core=state)),
                              live=dict(scheduler_active=True, active_node_ids=active or ['content/check'])))


@pytest.mark.asyncio
async def test_loop_pause_and_active_followups_have_separate_statuses(paused_graph):
    before = copy.deepcopy(paused_graph)
    shown = projected(paused_graph)
    task = shown['task']
    assert task['execution_status'] == 'running'
    assert task['workflow_status'] == 'paused' and task['status'] == '已暂停'
    assert task['node_dialogue'] == dict(running=1, waiting=0, queued=1)
    assert task['controls'] == ['resume', 'pause', 'cancel']
    assert '主流程已暂停：达到设定循环次数' in task['current_activity']
    assert '节点对话：1 条进行中，1 条排队' in task['current_activity']
    assert {g['id']: g['status'] for g in shown['plan']} == {
        'workflow': '已暂停', 'pilot': '已完成', 'content': '已暂停', 'layout': '未开始'}
    loop = next(node for node in shown['structure']['nodes'] if node['id'] == 'content')
    assert loop['status'] == '已暂停' and loop['execution']['phase'] == 'paused'
    assert paused_graph == before
    assert finish(shown) == shown


@pytest.mark.asyncio
async def test_independent_graph_work_stays_running_beside_a_paused_loop(paused_graph):
    paused_graph['graph']['nodes'].append(dict(id='independent', label='独立任务', kind='work',
        task='', inputs=[], outputs=[], model=None, config={}))
    start_attempt(paused_graph, 'independent', {}, [])
    shown = projected(paused_graph, ['content/check', 'independent'])
    assert shown['task']['workflow_status'] == 'running'
    assert shown['task']['controls'] == ['pause', 'cancel']
    assert shown['task']['paused_branches'] == 1
    assert '1 个分支已暂停' in shown['task']['current_activity']
    assert next(g for g in shown['plan'] if g['id'] == 'content')['status'] == '已暂停'


@pytest.mark.asyncio
async def test_explicit_pause_does_not_claim_background_calls_are_running(paused_graph):
    paused_graph['status'] = 'paused'
    shown = projected(paused_graph)
    assert shown['task']['status'] == '已暂停'
    assert shown['task']['node_dialogue']['running'] == 0
    assert '节点对话' not in shown['task']['current_activity']
    assert shown['task']['controls'] == ['resume', 'cancel']


@pytest.mark.asyncio
async def test_legacy_client_adapter_matches_host_display_and_preserves_raw_state(paused_graph, tmp_path):
    shown = projected(paused_graph)
    old = copy.deepcopy(shown)
    for field in ('workflow_status', 'node_dialogue', 'paused_branches'):
        old['task'].pop(field)
    old['task'].update(status='进行中', controls=['pause', 'cancel'], current_activity='正在推进当前计划')
    for group in old['plan']:
        group['status'] = '进行中'
    fixtures = tmp_path / 'display.json'
    fixtures.write_text(json.dumps(dict(old=old, expected=shown), ensure_ascii=False), encoding='utf-8')
    root = Path(__file__).resolve().parents[2]
    checked = subprocess.run(['node', str(Path(__file__).with_name('status_alignment.cjs')), str(fixtures)],
        cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert 'status alignment passed' in checked.stdout


@pytest.mark.parametrize('recovering', [False, True])
def test_nested_interruption_and_recovery_match_client_display(tmp_path, recovering):
    state = fresh(graph([dict(id='book', kind='loop', config=dict(mode='count', count=1,
        graph=graph(['review', 'other'])))]))
    state['status'] = 'running'
    start_attempt(state, 'book', {}, [])
    failed = start_attempt(state, 'book/review', {}, [0])
    finish_attempt(state, failed['id'], dict(status='blocked', reason='Provider disconnected'))
    start_attempt(state, 'book/other', {}, [0])
    if recovering:
        continuation.schedule(state, failed['id'], delay=30)
    shown = projected(state, ['book/other'])
    label = '恢复中' if recovering else '部分受阻'
    assert shown['task']['status'] == label
    assert shown['plan'][0]['steps'][0]['status'] == label
    assert finish(copy.deepcopy(shown)) == shown
    old = copy.deepcopy(shown)
    for field in ('workflow_status', 'node_dialogue', 'paused_branches', 'blocked_branches'):
        old['task'].pop(field)
    old['task'].update(status='进行中', controls=['pause', 'cancel'], current_activity='正在推进当前计划')
    old['plan'][0]['steps'][0]['status'] = '进行中'
    fixtures = tmp_path / 'display.json'
    fixtures.write_text(json.dumps(dict(old=old, expected=shown), ensure_ascii=False), encoding='utf-8')
    checked = subprocess.run(['node', str(Path(__file__).with_name('status_alignment.cjs')), str(fixtures)],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert checked.returncode == 0, checked.stdout + checked.stderr
