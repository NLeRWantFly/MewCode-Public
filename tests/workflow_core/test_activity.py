import copy
import errno
from types import SimpleNamespace

import pytest

from mewcode.orchestration.task_view import finish
from mewcode.workflow_core.activity import observe_node_detail, observe_view
from mewcode.workflow_core.projection import node_detail, view
from mewcode.workflow_core.runtime import Core, FAILURES, LIVE, MemoryBackend, observation
from mewcode.workflow_core.state import fresh, start_attempt, finish_attempt
from tests.workflow_core.test_runtime import graph


def active_view(stamps=(1000,)):
    state = fresh(graph([f'work{i}' for i in range(len(stamps))]))
    state['status'] = 'running'
    for i, stamp in enumerate(stamps):
        attempt = start_attempt(state, f'work{i}', {}, [])
        attempt.update(started_at=stamp, last_activity_at=stamp)
        state['events'][-1]['at'] = stamp
    # A record refresh is intentionally recent; it must not reset progress.
    return view(dict(run_id='r' * 32, workflow_core=state, updated_at=9999))


def test_ended_conversation_is_blocked_immediately_even_after_recent_progress():
    original = active_view()
    for live in ({'scheduler_active': False}, {'scheduler_active': True, 'active_node_ids': []}):
        result = finish(observe_view(copy.deepcopy(original), live=live))
        assert result['task']['status'] == '已阻塞'
        assert result['task']['execution_status'] == 'blocked'
        assert result['task']['last_activity_at'] == 1000
        assert result['plan'][0]['steps'][0]['status'] == '已阻塞'
        assert result['structure']['nodes'][0]['status'] == '已阻塞'
        assert '对话已结束' in result['task']['reason']
    assert original['task']['execution_status'] == 'running'


def test_active_conversation_stays_running_regardless_of_silence_duration():
    original = active_view()
    assert observe_view(copy.deepcopy(original), live={'scheduler_active': False})['task']['status'] == '已阻塞'
    result = finish(observe_view(copy.deepcopy(original),
        live={'scheduler_active': True, 'active_node_ids': ['work0']}))
    assert result['task']['status'] == '进行中'
    assert result['plan'][0]['steps'][0]['status'] == '进行中'
    assert result['plan'][0]['steps'][0]['detail']['last_activity_at'] == 1000


def test_ended_branch_does_not_relabel_independent_conversation():
    result = finish(observe_view(active_view((1690, 1000)),
        live={'scheduler_active': True, 'active_node_ids': ['work1']}))
    assert result['task']['status'] == '部分受阻'
    assert result['task']['blocked_branches'] == 1
    assert [step['status'] for step in result['plan'][0]['steps']] == ['已阻塞', '进行中']


def test_manual_pause_and_completed_delivery_do_not_become_stalled():
    for status in ('paused', 'completed'):
        original = active_view()
        original['task'].update(execution_status=status, status='已暂停' if status == 'paused' else '已完成')
        assert observe_view(copy.deepcopy(original), live={'scheduler_active': False}) == original


def test_idle_manual_judgment_shows_required_action_without_claiming_model_stopped():
    state = fresh(graph(['sample', dict(id='confirm', kind='judge', config={'manual': True}), 'batch'],
                        [('sample', 'confirm', 'always'), ('confirm', 'batch', 'pass')]))
    sample = start_attempt(state, 'sample', {}, [])
    finish_attempt(state, sample['id'], dict(status='completed', text='样章已交付'))
    confirm = start_attempt(state, 'confirm', {}, [])
    finish_attempt(state, confirm['id'], dict(status='needs_input', reason='请查看样章并选择通过或不通过'))
    state['status'] = 'blocked'
    before = copy.deepcopy(state)
    record = dict(run_id='manual-wait', workflow_core=state, status_reason='请查看样章并选择通过或不通过')
    projected = finish(observe_view(view(record), live=dict(scheduler_active=False)))
    assert projected['task']['status'] == '等你处理'
    assert projected['task']['execution_status'] == 'needs_input'
    assert projected['task']['waiting_node_ids'] == ['confirm']
    assert 'resume' not in projected['task']['controls']
    assert projected['task']['reason'] == record['status_reason']
    assert projected['structure']['nodes'][0]['status'] == '已完成'
    assert state == before
    # Older persisted projections must get the same fix on their read path.
    cached = copy.deepcopy(projected)
    cached['task'].update(status='已阻塞', execution_status='blocked', reason='模型对话已结束，节点尚未交付。')
    cached['task'].pop('waiting_node_ids')
    restored = finish(observe_view(cached, live=dict(scheduler_active=False)))
    assert restored['task'] == projected['task']
    failed = observe_view(view(record), live=dict(scheduler_active=False), fault={'reason': '磁盘写入失败'})
    assert failed['task']['status'] == '已阻塞' and failed['task']['reason'] == '磁盘写入失败'


def test_stopped_scheduler_preserves_specific_failure_reason():
    state = fresh(graph(['work']))
    attempt = start_attempt(state, 'work', {}, [])
    finish_attempt(state, attempt['id'], dict(status='blocked', reason='模型接口返回 503'))
    state['status'] = 'blocked'
    record = dict(run_id='specific-failure', workflow_core=state, status_reason='模型接口返回 503')
    result = finish(observe_view(view(record), live=dict(scheduler_active=False)))
    assert result['task']['status'] == '已阻塞'
    assert result['task']['reason'] == '模型接口返回 503'


def test_node_history_and_request_show_ended_conversation_without_changing_saved_status():
    detail = dict(node_id='work', attempts=[dict(id='a', status='running', started_at=1000)],
                  requests=[dict(id='q', status='running', attempt_id='a')])
    result = observe_node_detail(detail, live={'scheduler_active': False})
    assert result['attempts'][0]['status'] == 'running'
    assert result['attempts'][0]['display_status'] == 'blocked'
    assert result['requests'][0]['display_status'] == 'blocked'


@pytest.mark.parametrize('waiting_on', ['upstream-supplement', None])
def test_yielded_conversation_is_not_a_stopped_conversation(waiting_on):
    state = fresh(graph(['work']))
    state['status'] = 'running'
    attempt = start_attempt(state, 'work', {}, [])
    attempt.update(status='suspended', waiting_on=waiting_on)
    record = dict(run_id='waiting-test', workflow_core=state)
    live = dict(scheduler_active=True, active_node_ids=[], active_attempt_ids=[])
    projected = finish(observe_view(view(record), live=live))
    assert projected['task']['status'] == '进行中'
    assert projected['task']['blocked_branches'] == 0
    node = projected['structure']['nodes'][0]
    assert node['status'] == '进行中'
    assert node['execution']['phase'] == ('waiting_node' if waiting_on else 'yielded')
    assert not node['execution']['active']
    detail = observe_node_detail(node_detail(record, 'work'), live=live)
    assert detail['attempts'][0]['status'] == 'suspended'
    assert detail['attempts'][0]['waiting_on'] == waiting_on
    assert not detail['attempts'][0].get('display_status')
    # Losing the scheduler or encountering an actual fault still surfaces it.
    for stopped_live, fault in [(dict(scheduler_active=False), None),
                                (live, dict(node_ids=['work'], reason='磁盘写入失败'))]:
        assert observe_view(view(record), live=stopped_live, fault=fault)['task']['status'] == '已阻塞'
        assert observe_node_detail(node_detail(record, 'work'), live=stopped_live,
                                   fault=fault)['attempts'][0]['display_status'] == 'blocked'


def test_waiting_followup_preserves_delivery_and_request_state():
    state = fresh(graph(['work']))
    state['status'] = 'running'
    primary = start_attempt(state, 'work', {}, [])
    primary['status'] = state['nodes']['work']['status'] = 'completed'
    reply = copy.deepcopy(primary)
    reply.update(id='reply', status='suspended', request_id='question', waiting_on='dependency')
    state['attempts']['reply'] = reply
    state['node_requests'] = {'question': dict(id='question', node_id='work', sender_node_id='',
        status='waiting', attempt_id='reply')}
    record = dict(run_id='waiting-followup', workflow_core=state)
    live = dict(scheduler_active=True, active_node_ids=[], active_attempt_ids=[])
    projected = observe_view(view(record), live=live)
    assert projected['task']['status'] == '已完成'
    assert projected['task']['workflow_status'] == 'completed'
    assert projected['task']['node_dialogue']['waiting'] == 1
    assert projected['structure']['nodes'][0]['status'] == '已完成'
    assert projected['structure']['nodes'][0]['execution']['phase'] == 'waiting_node'
    detail = observe_node_detail(node_detail(record, 'work'), live=live)
    assert detail['requests'][0]['status'] == 'waiting'
    assert not detail['requests'][0].get('display_status')


@pytest.mark.asyncio
async def test_storage_failure_keeps_checkpoint_and_surfaces_blocked_even_when_writes_fail():
    class FullDisk(MemoryBackend):
        def change(self, callback):
            raise OSError(errno.ENOSPC, 'No space left on device')
    state = fresh(graph(['work']))
    state['status'] = 'running'
    attempt = start_attempt(state, 'work', {}, [])
    attempt['messages'] = [{'role': 'assistant', 'content': 'Saved page 185'}]
    backend = FullDisk(state)
    run_id = 'activity-full-disk'
    core = Core(backend, None, run_id=run_id)
    try:
        with pytest.raises(OSError):
            await core.run()
        assert run_id not in LIVE
        live, fault = observation(run_id)
        assert '磁盘空间已满' in fault['reason']
        assert backend.read()['attempts'][attempt['id']]['messages'] == attempt['messages']
        assert observe_view(active_view(), live=live, fault=fault)['task']['status'] == '已阻塞'
    finally:
        FAILURES.pop(run_id, None)


@pytest.mark.asyncio
async def test_stream_activity_is_observed_without_writing_a_snapshot_per_token():
    backend = MemoryBackend(fresh(graph(['work'])))
    core = Core(backend, None)
    before = backend.read()
    core.observe('attempt', 'work')
    await core.publish('attempt', 'activity', activity='reasoning')
    assert core.activity_snapshot()['nodes']['work'] > 0
    assert backend.read() == before


@pytest.mark.asyncio
async def test_phase_transitions_refresh_projection_without_token_writes_or_stale_activity():
    state = fresh(graph(['work']))
    state['status'] = 'running'
    attempt = start_attempt(state, 'work', {}, [])
    backend, events = MemoryBackend(state), []
    core = Core(backend, None, emit=events.append)
    core.tasks[attempt['id']] = SimpleNamespace(done=lambda: False)
    core.observe(attempt['id'], 'work')

    def projected():
        return finish(observe_view(view(dict(run_id='phase-test', workflow_core=backend.read())),
                                   live=core.activity_snapshot()))

    await core.publish(attempt['id'], 'activity', activity='reasoning')
    first = projected()
    for _ in range(100):
        await core.publish(attempt['id'], 'activity', activity='reasoning')
    assert len(events) == 1 and backend.read() == state
    assert projected()['version'] == first['version']
    await core.publish(attempt['id'], 'activity', activity='tool_running', tool='ReadFile', tool_id='read')
    second = projected()
    assert second['version'] != first['version']
    execution = second['structure']['nodes'][0]['execution']
    assert execution['text'] == '正在调用工具 · ReadFile'
    assert second['plan'][0]['steps'][0]['detail']['execution'] == execution
    await core.publish(attempt['id'], 'activity', activity='tool_error', tool='ReadFile', outcome='failed')
    await core.publish(attempt['id'], 'activity', activity='waiting_model')
    assert projected()['structure']['nodes'][0]['execution']['text'] == '等待模型响应 · ReadFile 返回错误'
    assert projected()['task']['execution_status'] == 'running'
    core.tasks.clear()
    stopped = projected()['structure']['nodes'][0]
    assert stopped['execution']['phase'] == 'blocked' and not stopped['execution']['active']


def test_terminal_and_paused_nodes_do_not_keep_a_tool_spinner():
    from mewcode.workflow_core.activity import execution_fallback
    previous = {'execution': {'phase': 'tool_running', 'text': '正在调用工具 · Bash', 'active': True}}
    for status in ('completed', 'blocked', 'paused', 'terminated'):
        assert not execution_fallback(status, attempt=previous)['active']
    assert execution_fallback('running', attempt=previous, run_status='paused')['phase'] == 'paused'


@pytest.mark.asyncio
async def test_tool_result_cannot_revive_an_ended_conversation():
    state = fresh(graph(['work0']))
    attempt = start_attempt(state, 'work0', {}, [])
    backend = MemoryBackend(state)
    core = Core(backend, None)
    core.observe(attempt['id'], 'work0')
    await core.publish(attempt['id'], 'tool_result', name='LateToolResult', output='Completed')
    result = observe_view(active_view(), live=core.activity_snapshot())
    assert result['task']['status'] == '已阻塞'


def test_reply_notification_reads_frozen_archive_without_copying_it(tmp_path, monkeypatch):
    from mewcode.workflow_core import revisions
    project, artifacts = tmp_path / 'project', tmp_path / 'runs'
    frozen = artifacts / 'run' / 'source.tar'
    frozen.parent.mkdir(parents=True)
    frozen.write_bytes(b'Original OCR archive')
    monkeypatch.setattr(revisions.shutil, 'copyfile', lambda *_: pytest.fail('Notification copied an old archive'))
    root, mapping = revisions.prepare(project, 'run', dict(id='reply', kind='update'),
        {'artifacts': [dict(name='source.tar', stored_path=str(frozen), source_path=str(project / 'source.tar'))]}, artifacts)
    assert root.is_dir() and not (root / 'source.tar').exists()
    assert mapping[str(root / 'source.tar')] == str(project / 'source.tar')
    assert frozen.read_bytes() == b'Original OCR archive'
