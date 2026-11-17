"""Reporting position and business problems never settles work or authority."""
import copy

import pytest
from pydantic import ValidationError

from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.work_reporting import current_report, project_work_board
from mewcode.experts.workflow_context import project_working_context
from mewcode.kernel import events, run_views
from mewcode.kernel.sqlite_state import RunDatabase
from mewcode.orchestration import task_view
from mewcode.tools.report_step import ReportStepParams, ReportStepTool, WorkProgress
from tests.test_loop_checkpoint_preflight import checkpoint_flow
from tests.test_tool_performance import RUN, assert_views_match


def store_for(root):
    store = ExpertRunStore(root, storage='sqlite')
    store.create(run_id=RUN, conversation_id='book-chat', task='逐页核对一本书', attachments=[],
        profile={}, strategy='stable', flow=checkpoint_flow())
    def seed(record):
        record.update(status='running', active_node_ids=['check'], stage_cursor='check',
            node_states={key: {'node_id': key, 'status': 'running', 'node_attempt': 1,
                'epoch': 0, 'input_revision': 'v1', 'tools': {}} for key in ('work', 'check', 'done')},
            loop_states={'L': {'status': 'running', 'round': 3, 'active_nodes': ['check']}})
    store.mutate(RUN, seed)
    return store


async def report(store, node='check', *, tool_id=None, **fields):
    last = store.read_execution_fields(RUN, 'last_seq')['last_seq']
    tool_id = tool_id or f'report-{last + 1}'
    params = ReportStepParams(label='核对第5页公式', **fields)
    result = await ReportStepTool().execute(params)
    declared = dict(type='expert_tool', seq=last + 1, node_id=node, node_attempt=1,
        id=tool_id, name='report_step', args=params.model_dump())
    completed = dict(type='expert_tool_result', seq=last + 2, node_id=node, id=tool_id,
        is_error=False, output=result.output, host_observation=result.host_observation)
    store.append_sequenced_events(RUN, [declared, completed], return_snapshot=False)
    return completed


PROGRESS = {'scope': '电路基础', 'unit': '页', 'current': 5, 'total': 110, 'completed': 4}
ISSUE = {'id': 'page5-formula2', 'scope': '电路基础', 'location': '第5页第二个公式',
    'message': '公式缺少分母，暂时不能确认计算结果。', 'state': 'open', 'next_step': '对照原书补齐后再复核。'}


@pytest.mark.parametrize('counts', [
    {'current': 0}, {'current': True}, {'current': '5'}, {'current': 111, 'total': 110},
    {'completed': -1}, {'completed': 111, 'total': 110}, {'total': 110}, {'current': 1, 'total': 0},
])
def test_progress_rejects_inconsistent_or_guessed_counts(counts):
    with pytest.raises(ValidationError):
        WorkProgress(scope='电路基础', unit='页', **counts)


def test_position_can_revisit_a_page_and_total_can_be_unknown():
    assert WorkProgress(scope='电路基础', current=5).total is None
    assert WorkProgress(scope='电路基础', current=5, completed=20, total=110).current == 5


@pytest.mark.asyncio
async def test_receipt_persists_current_report_and_read_models_without_accepting_work(tmp_path, monkeypatch):
    store = store_for(tmp_path)
    before = RunDatabase(tmp_path, RUN).load()
    builds = []
    original = run_views.build
    def tracked(*a, **kw):
        builds.append(True)
        return original(*a, **kw)
    monkeypatch.setattr(run_views, 'build', tracked)
    await report(store, progress=PROGRESS, issues=[ISSUE], summary='读到第5页，复核发现公式缺少一项。')
    assert not builds, 'A progress report must not rebuild the entire execution history.'
    state = assert_views_match(tmp_path)
    assert state['node_states']['check']['status'] == 'running'
    for field in ('authorizations', 'kernel_progress', 'node_ledgers', 'loop_states'):
        assert state.get(field) == before.get(field)
    view = store.read_workbench(RUN)['_workbench_view']
    region = view['structure']['loops'][0]
    assert region['progress'] == PROGRESS and region['round'] == 3
    assert region['report']['node_id'] == 'check'
    assert region['issues'][0]['message'] == ISSUE['message']
    assert all(step['acceptance'] is None for group in view['plan'] for step in group['steps'])
    events._SQL_CACHE.clear()
    reloaded = ExpertRunStore(tmp_path, storage='sqlite').read_workbench(RUN)
    assert reloaded['_workbench_view']['structure']['loops'] == view['structure']['loops']


@pytest.mark.asyncio
async def test_issue_updates_from_later_node_and_scopes_are_never_summed(tmp_path):
    store = store_for(tmp_path)
    await report(store, node='check', progress=PROGRESS, issues=[ISSUE])
    await report(store, node='work', progress=dict(PROGRESS, current=5),
        issues=[dict(ISSUE, state='fixing')], next_step='补齐公式中的分母。')
    view = task_view.from_record(store.load(RUN))
    assert view['structure']['loops'][0]['issues'][0]['state'] == 'fixing'
    assert view['structure']['loops'][0]['issues'][0]['node_id'] == 'work'
    assert view['structure']['loops'][0]['issues'][0]['origin_node_id'] == 'check'
    await report(store, node='work', issues=[dict(ISSUE, state='resolved')])
    view = task_view.from_record(store.load(RUN))
    assert view['structure']['loops'][0]['issues'][0]['state'] == 'resolved'
    assert view['structure']['loops'][0]['progress'] == PROGRESS
    await report(store, node='work', progress={'scope': '第二本书', 'unit': '页', 'current': 2, 'total': 30})
    region = task_view.from_record(store.load(RUN))['structure']['loops'][0]
    assert region['progress']['scope'] == '第二本书' and region['progress']['total'] == 30
    assert region['progress']['current'] == 2  # Not 5+2, and never round 3.


@pytest.mark.asyncio
async def test_new_input_or_attempt_hides_stale_reports_and_late_receipts(tmp_path):
    store = store_for(tmp_path)
    completed = await report(store, progress=PROGRESS, issues=[ISSUE])
    saved = store.load(RUN)['node_states']['check']
    for field, changed in [('input_revision', 'v2'), ('node_attempt', 2), ('epoch', 1)]:
        stage = copy.deepcopy(saved)
        stage[field] = changed
        assert current_report(stage) is None
    store.mutate(RUN, lambda record: record['node_states']['check'].update(input_revision='v2'))
    last = store.read_execution_fields(RUN, 'last_seq')['last_seq']
    store.append_sequenced_events(RUN, [dict(completed, seq=last + 1)], return_snapshot=False)
    view = task_view.from_record(store.load(RUN))
    assert view['structure']['loops'][0]['progress'] is None
    assert view['structure']['loops'][0]['issues'] == []


@pytest.mark.asyncio
async def test_unknown_or_failed_tool_result_cannot_publish_progress(tmp_path):
    store = store_for(tmp_path)
    await report(store, progress=PROGRESS)
    state = store.load(RUN)
    saved = copy.deepcopy(state['node_states']['check']['work_report'])
    last = state['last_seq']
    store.append_sequenced_events(RUN, [dict(seq=last+1, type='expert_tool_result', node_id='check',
        id='unknown-tool', name='report_step', is_error=False,
        host_observation={'work_report': {'schema_version': 1, 'progress': PROGRESS}})], return_snapshot=False)
    assert store.load(RUN)['node_states']['check']['work_report'] == saved


@pytest.mark.asyncio
async def test_out_of_order_reports_merge_issues_but_keep_newer_position_and_resolution(tmp_path):
    store = store_for(tmp_path)
    observations = []
    for index in range(3):
        issue = dict(ISSUE, id='formula-2' if index else 'caption-1',
                     state='resolved' if index == 2 else 'open')
        params = ReportStepParams(label=f'汇报{index}', progress=dict(PROGRESS, current=5 + index), issues=[issue])
        result = await ReportStepTool().execute(params)
        observations.append((params, result))
    def declared(index, seq):
        return dict(seq=seq, type='expert_tool', node_id='check', node_attempt=1, id=f'p{index}',
            name='report_step', declared_at=100 + index, args=observations[index][0].model_dump())
    def completed(index, seq):
        result = observations[index][1]
        return dict(seq=seq, type='expert_tool_result', node_id='check', id=f'p{index}',
            is_error=False, host_observation=result.host_observation, output=result.output)
    first = store.read_execution_fields(RUN, 'last_seq')['last_seq'] + 1
    sequence = [declared(0, first), declared(1, first+1), completed(1, first+2), completed(0, first+3),
                declared(2, first+4), completed(2, first+5), completed(1, first+6)]
    for event in sequence:
        store.append_sequenced_events(RUN, [event], return_snapshot=False)
        assert_views_match(tmp_path)
    saved = store.load(RUN)['node_states']['check']['work_report']
    assert saved['progress']['current'] == 7
    issues = {row['id']: row for row in saved['issues']}
    assert issues['caption-1']['state'] == 'open'
    assert issues['formula-2']['state'] == 'resolved'
    assert saved['label'] == '汇报2'


@pytest.mark.asyncio
async def test_projection_keeps_other_read_models_immutable_and_context_reports_bounded(tmp_path):
    store = store_for(tmp_path)
    await report(store, progress=PROGRESS, issues=[ISSUE])
    state = store.load(RUN)
    view = task_view.from_record(state)
    before = copy.deepcopy(view)
    shallow = dict(view)
    project_work_board(shallow, state)
    assert view == before
    stage = state['node_states']['check']
    context = project_working_context(stage, binding={})
    assert context['reported_work']['progress'] == PROGRESS
    assert context['reported_work']['issues'][0]['location'] == ISSUE['location']
    # Public reports themselves remain excluded from genuine work progress.
    from mewcode.experts.recovery_state import progress_marker
    marker = progress_marker(state, 'check')
    assert not marker['write_receipts'] and not marker['submitted_items']


@pytest.mark.asyncio
async def test_real_role_adapter_keeps_agent_report_and_streams_it_to_workbench(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from mewcode import expert_web
    from mewcode.agent import Agent, StreamText, ToolResultEvent, ToolUseEvent, TurnComplete
    from mewcode.conversation import ConversationManager
    from mewcode.experts.flow import ExpertFlowSpec, ExpertFlowStore
    from mewcode.orchestration.plan_session import PlanSessionStore
    from mewcode.permissions.policy import build_permission_checker
    from mewcode.tools import ToolRegistry
    from mewcode.tools.submit_handoff import SubmitHandoff, SubmitHandoffParams
    from tests.test_agent import MockLLMClient
    from tests.test_expert_flow import _profile

    project = tmp_path / 'project'
    project.mkdir()
    flows = ExpertFlowStore(tmp_path / 'flows.json')
    flow = ExpertFlowSpec.from_value({'flow_id': 'book', 'finalizer_node_id': 'read', 'edges': [],
        'nodes': [{'node_id': 'read', 'role': 'manager', 'label': '逐页阅读', 'capabilities': {}}]})
    flows.put(project, flow)
    store = ExpertRunStore(tmp_path / 'runs', storage='sqlite')
    class FakeAgent(Agent):
        def __init__(self, policy):
            super().__init__(MockLLMClient([]), ToolRegistry(), 'openai', str(project))
            self.registry.register(ReportStepTool())
            self.registry.register(SubmitHandoff(policy.handoff_holder))
        def set_review_authorization_context(self, context):
            pass
        async def run(self, conversation):
            params = ReportStepParams(label='阅读到第5页', summary='第5页公式正在修正。', progress=PROGRESS, issues=[ISSUE])
            yield ToolUseEvent('report_step', 'progress', params.model_dump())
            result = await self.registry.get('report_step').execute(params)
            yield ToolResultEvent('progress', 'report_step', result.output, False, .001, host_observation=result.host_observation)
            delivery = SubmitHandoffParams(summary='已记录当前阅读进度', inline_outputs=[{'name': '本次记录', 'content': '第5页公式待修正。'}])
            yield ToolUseEvent('SubmitHandoff', 'delivery', delivery.model_dump())
            result = await self.registry.get('SubmitHandoff').execute(delivery)
            yield ToolResultEvent('delivery', 'SubmitHandoff', result.output, result.is_error, .001, host_observation=result.host_observation)
            yield TurnComplete(1)
            conversation.add_assistant_message('已汇报当前处理位置。')
            yield StreamText('已汇报当前处理位置。')
    monkeypatch.setattr(expert_web, '_build_role_agent', lambda *args: (
        FakeAgent(args[4]), None, SimpleNamespace(conversation=ConversationManager(), aclose=AsyncMock())))
    session = SimpleNamespace(work_dir=str(project), conversation_id='book-chat', multi_agent=False,
        agent=SimpleNamespace(client=SimpleNamespace(), permission_checker=build_permission_checker(project)),
        session=SimpleNamespace(meta=SimpleNamespace(message_count=0), append=lambda *a: None, append_expert_run=lambda *a, **kw: None),
        conversation=ConversationManager(), expert_run_id='', expert_control_action='pause',
        context=SimpleNamespace(get=lambda key, default=None: default, slots=SimpleNamespace(resolve=lambda key: {})),
        task_manager=SimpleNamespace(list_tasks=lambda: [], mark_notified=lambda ids: None),
        expert_child_event_callback=None, status_payload=lambda: {}, tasks_payload=lambda: [])
    sent = []
    async def send(event):
        sent.append(event)
    await asyncio.wait_for(expert_web.run_expert_chat(SimpleNamespace(send_json=send), '逐页阅读这本书', session=session,
        registry_service=SimpleNamespace(catalog=AsyncMock(return_value={'providers': []}), resolve=lambda model: None),
        profile_store=SimpleNamespace(get=lambda _: _profile()), flow_store=flows,
        plan_store=PlanSessionStore(tmp_path / 'plans.json'), run_store=store, confirmed_new_flow=flow), 90)
    run_id = next(row['run_id'] for row in sent if row.get('run_id'))
    state = store.load(run_id)
    assert state['node_states']['read']['work_report']['progress'] == PROGRESS
    live = next(row for row in sent if row.get('type') == 'expert_step' and row.get('progress'))
    assert live['progress'] == PROGRESS and live['issues'][0]['message'] == ISSUE['message']
    view = store.read_workbench(run_id)['_workbench_view']
    assert view['work_board']['progress'] == PROGRESS
    assert view['work_board']['issues'][0]['state'] == 'open'
