"""Real handoff tools and scheduler; no provider calls or manuscript writes."""
import copy

import pytest
from pydantic import ValidationError

from mewcode.expert_group import ExpertExecutionPolicy, ExpertRoleResult
from mewcode.expert_web import _role_registry
from mewcode.experts.delivery import delivery_prompt, handoff_error_kind, prior_handoff_source, recovery_action
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.experts.stage_context import collect_stage_inputs, stage_inputs_prompt
from mewcode.tools.read_stage_result import ReadStageResult, ReadStageResultParams
from mewcode.tools.submit_handoff import HandoffHolder, SubmitHandoff, SubmitHandoffParams
from tests.test_expert_children import FakeSession, make_registry
from tests.test_generic_loops import report
from tests.test_team_loop_control import chain_flow, execute, legacy_poisoned_run, grant


def analysis_flow():
    flow, contract = chain_flow()
    raw = flow.as_dict()
    next(node for node in raw['nodes'] if node['node_id'] == 'analyze')['role'] = 'manager'
    return ExpertFlowSpec.from_value(raw), contract


def analysis_outputs():
    return [{'name': name, 'content': f'原稿 P01：{name}；证据 E01，保留事实。'}
            for name in ['写法模式表', '逐段差距清单', '逐段改写建议']]


@pytest.mark.asyncio
async def test_readonly_manager_delivers_saved_content_without_file_capability(tmp_path):
    holder = HandoffHolder()
    policy = ExpertExecutionPolicy('stable', False, 0, handoff_holder=holder,
                                   node_tools=('ReadFile', 'ReadStageResult'))
    registry = _role_registry(FakeSession(make_registry('ReadFile', 'WriteFile', 'EditFile')), 'manager', policy)
    assert registry.get('WriteFile') is None and registry.get('EditFile') is None
    assert registry.get('RequestAssistance') is not None and registry.get('ReadStageResult') is not None
    result = await registry.get('SubmitHandoff').execute(SubmitHandoffParams(
        summary='analysis delivered', inline_outputs=analysis_outputs()))
    assert not result.is_error and holder.value['status'] == 'completed'
    assert holder.value['artifacts'] == [] and list(tmp_path.iterdir()) == []
    sources = collect_stage_inputs([{'from_node': 'analyze', 'handoff': holder.value, 'status': 'completed'}], {}, {})
    brief = stage_inputs_prompt(sources)
    assert 'inline_outputs' in brief and '不必寻找独立文件' in brief
    output = await ReadStageResult(sources).execute(ReadStageResultParams(node_id='analyze'))
    assert all(item['content'] in output.output for item in analysis_outputs())


@pytest.mark.asyncio
async def test_inline_content_cannot_replace_a_declared_missing_file(tmp_path):
    holder = HandoffHolder()
    result = await SubmitHandoff(holder, work_dir=tmp_path).execute(SubmitHandoffParams(
        summary='analysis', inline_outputs=analysis_outputs(), artifacts=[{'path': 'required.md'}]))
    assert result.is_error and '不存在' in result.output and holder.read() is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('extra', [dict(status='completed', blocker_kind='capability_missing'),
    dict(status='completed', question='Approve?'), dict(status='needs_attention', blocker_kind='user_input'),
    dict(status='needs_attention', blocker_kind='delivery_format', question='Approve?')])
async def test_inconsistent_or_unactionable_handoff_is_rejected(extra):
    holder = HandoffHolder()
    result = await SubmitHandoff(holder).execute(SubmitHandoffParams(summary='analysis', **extra))
    assert result.is_error and holder.read() is None


@pytest.mark.parametrize('outputs', [[{'name': 'table', 'content': ' '}],
    [{'name': ' ', 'content': 'data'}], [{'name': str(i), 'content': 'x' * 20000} for i in range(7)]])
def test_empty_or_oversized_content_is_rejected_without_silent_truncation(outputs):
    with pytest.raises(ValidationError):
        SubmitHandoffParams(summary='analysis', inline_outputs=outputs)


@pytest.mark.asyncio
async def test_team_corrects_format_once_then_hands_complete_content_to_writer():
    flow, contract = analysis_flow()
    calls, states = [], {}
    async def runner(node, incoming, policy, prompt):
        calls.append(node.node_id)
        handoff = None
        if node.node_id == 'analyze':
            holder = HandoffHolder()
            first = calls.count('analyze') == 1
            params = SubmitHandoffParams(summary='content already prepared', inline_outputs=analysis_outputs(),
                status='needs_attention' if first else 'completed',
                blocker_kind='delivery_format' if first else '', blockers=['Unsure how to deliver the table'] if first else [])
            assert not (await SubmitHandoff(holder).execute(params)).is_error
            handoff = holder.read()
            if not first:
                assert policy.resume_messages and '一次交付形式补交' in prompt
                assert states['improve']['recovery_history'][-1]['results']['analyze']['handoff']['inline_outputs'] == analysis_outputs()
        if node.node_id == 'rewrite':
            sources = collect_stage_inputs(incoming, {}, {})
            complete = await ReadStageResult(sources).execute(ReadStageResultParams(node_id='analyze'))
            assert all(item['content'] in complete.output for item in analysis_outputs())
            assert incoming[0]['status'] == 'completed'
        return ExpertRoleResult(text='stage result', handoff=handoff, billable_tokens=5,
            loop_decision=report(policy.loop_context) if node.node_id == 'check' else None)
    _, result = await execute(flow, contract, states, runner)
    assert result.status == 'completed'
    assert calls == ['retrieve', 'extract', 'analyze', 'analyze', 'rewrite', 'check']
    assert states['improve']['tokens'] == 30
    assert states['improve']['recovery_history'][0]['reason'] == 'handoff_format_correction'


@pytest.mark.asyncio
async def test_repeated_format_problem_stops_without_false_acceptance_or_unlimited_retry():
    flow, contract = analysis_flow()
    calls, states = [], {}
    async def runner(node, *args):
        calls.append(node.node_id)
        return ExpertRoleResult(text='still incomplete', billable_tokens=2,
            handoff={'status': 'needs_attention', 'blocker_kind': 'delivery_format', 'summary': 'format'}
            if node.node_id == 'analyze' else None)
    _, result = await execute(flow, contract, states, runner)
    assert calls == ['retrieve', 'extract', 'analyze', 'analyze']
    assert result.status == 'needs_attention' and result.error_kind == 'handoff_format'
    assert states['improve']['status'] == 'blocked' and states['improve']['tokens'] == 8
    assert not states['improve'].get('reports')
    await execute(flow, contract, states, runner)
    assert len(calls) == 4  # Reload does not authorize another correction.


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,question,expected', [('capability_missing', '', 'capability_missing'),
    ('user_input', 'Which authorized directory?', 'awaiting_input'), ('external_dependency', '', 'external_dependency'),
    ('delivery_format', 'Authorize a source edit?', 'awaiting_input'), ('', '', 'node_failure')])
async def test_real_blockers_and_legacy_unspecified_blockers_are_not_auto_accepted(kind, question, expected):
    flow, contract = analysis_flow()
    calls = []
    async def runner(node, *args):
        calls.append(node.node_id)
        return ExpertRoleResult(text='cause', handoff={'status': 'needs_attention',
            'blocker_kind': kind, 'question': question, 'blockers': ['cause']} if node.node_id == 'analyze' else None)
    _, result = await execute(flow, contract, {}, runner)
    assert calls == ['retrieve', 'extract', 'analyze']
    assert result.error_kind == expected and result.failed_node_id == 'analyze'
    if expected == 'capability_missing':
        assert '原样重试不会补齐工具' in result.text
    if question:
        assert question in result.text


@pytest.mark.asyncio
async def test_existing_checkpoint_keeps_complete_analysis_as_history_after_explicit_continue(tmp_path):
    store, run_id, flow, contract, snapshot = legacy_poisoned_run(tmp_path)
    decisions = [f'分析建议 {i}' for i in range(23)]
    def update(value):
        state = value['loop_states']['improve']
        state['round_results']['extract']['status'] = 'completed'
        state['round_results']['analyze'].update(status='needs_attention', handoff={
            'status': 'needs_attention', 'decisions': decisions, 'blockers': ['Cannot write files']})
        state['round_results'].pop('rewrite')
        value['node_states'] = copy.deepcopy(state['round_results'])
    snapshot = store.mutate(run_id, update)
    grant(store, run_id, snapshot)
    resumed = store.resume(run_id, strategy='stable')
    assert resumed['stage_cursor'] == 'analyze'
    assert list(resumed['loop_states']['improve']['round_results']) == ['retrieve', 'extract']
    stage = resumed['node_states']['analyze']
    source = prior_handoff_source('analyze', stage)
    assert source['handoff']['decisions'] == decisions
    assert source['record_kind'] == 'previous_attempt_not_current_acceptance'
    assert 'messages' not in source and source['status'] != 'completed'
    reader = ReadStageResult({'analyze': source})
    result = await reader.execute(ReadStageResultParams(node_id='analyze'))
    assert '分析建议 22' in result.output
    assert (await reader.execute(ReadStageResultParams(node_id='unrelated'))).is_error
    source['handoff']['decisions'].clear()
    assert stage['handoff']['decisions'] == decisions


def test_capability_prompt_distinguishes_inline_content_from_explicit_files():
    prompt = delivery_prompt('manager', ['ReadFile', 'SubmitHandoff'])
    assert '本地写入与命令工具：无' in prompt
    assert '未要求独立文件时，内容完成可提交 completed 且 artifacts 为空' in prompt
    assert '明确要求文件或执行操作时，提供对应实际产物和回执' in prompt
    assert '补充说明' not in recovery_action('capability_missing')
    assert handoff_error_kind({'status': 'needs_attention', 'blocker_kind': 'delivery_format', 'question': 'Approve?'}) == 'awaiting_input'


@pytest.mark.parametrize('change', [{'input_revision': 'different-input'}, {'status': 'completed'},
                                  {'handoff': {'status': 'completed', 'inline_outputs': analysis_outputs()}}])
def test_prior_delivery_does_not_leak_finished_items_or_changed_inputs(change):
    stage = {'status': 'needs_attention', 'input_revision': 'same-input',
             'handoff': {'status': 'needs_attention', 'inline_outputs': analysis_outputs()}}
    assert prior_handoff_source('analyze', stage, input_revision='same-input') is not None
    stage.update(change)
    assert prior_handoff_source('analyze', stage, input_revision='same-input') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('error_kind', ['permission_denied', 'effect_unknown'])
async def test_hard_error_wins_over_partial_status_or_format_marker(error_kind):
    flow, contract = analysis_flow()
    calls = []
    async def runner(node, *args):
        calls.append(node.node_id)
        return ExpertRoleResult(text='real nonretryable cause', status='budget_exhausted', error_kind=error_kind,
            handoff={'status': 'needs_attention', 'blocker_kind': 'delivery_format'})
    _, result = await execute(flow, contract, {}, runner)
    assert calls == ['retrieve'] and result.error_kind == error_kind
    assert result.status != 'completed'


@pytest.mark.asyncio
async def test_provider_error_retries_once_and_requires_completed_response():
    flow, contract = analysis_flow()
    calls, states = [], {}

    async def runner(node, incoming, policy, prompt):
        calls.append(node.node_id)
        if node.node_id == 'retrieve' and calls.count('retrieve') == 1:
            return ExpertRoleResult(text='partial provider response', status='budget_exhausted',
                error_kind='provider_error',
                handoff={'status': 'needs_attention', 'blocker_kind': 'delivery_format'})
        if node.node_id == 'retrieve':
            assert '当前节点已自动接续一次' in prompt
            return ExpertRoleResult(text='verified retrieval result')
        return ExpertRoleResult(text='complete ' + node.node_id,
            loop_decision=report(policy.loop_context) if node.node_id == 'check' else None)

    _, result = await execute(flow, contract, states, runner)
    assert result.status == 'completed'
    assert calls == ['retrieve', 'retrieve', 'extract', 'analyze', 'rewrite', 'check']
    first_attempt = states['improve']['recovery_history'][0]['results']['retrieve']
    assert first_attempt['status'] == 'budget_exhausted'
    assert first_attempt['error_kind'] == 'provider_error'


@pytest.mark.asyncio
async def test_web_reads_own_prior_delivery_and_persists_inline_result(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from mewcode import expert_web
    from mewcode.agent import StreamText
    from mewcode.conversation import ConversationManager
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.experts.flow import ExpertFlowStore
    from mewcode.orchestration.plan_session import PlanSessionStore
    from tests.test_expert_flow import _profile

    project = tmp_path / 'project'
    project.mkdir()
    flows, runs = ExpertFlowStore(tmp_path / 'flows.json'), ExpertRunStore(tmp_path / 'runs')
    flow = ExpertFlowSpec.from_value({'flow_id': 'delivery', 'label': 'Analysis delivery',
        'nodes': [{'node_id': 'analyze', 'role': 'manager', 'label': 'Analyze', 'tools': ['ReadFile']},
                  {'node_id': 'done', 'role': 'manager', 'label': 'Read analysis', 'tools': ['ReadFile']}],
        'edges': [{'from': 'analyze', 'to': 'done'}], 'finalizer_node_id': 'done'})
    flows.put(project, flow)
    calls, events = [], []

    class FakeAgent:
        client = SimpleNamespace(expose_reasoning=False)
        total_input_tokens = total_output_tokens = 1

        def __init__(self, role, policy):
            self.registry = _role_registry(FakeSession(make_registry('ReadFile', 'WriteFile')), role, policy)

        def set_review_authorization_context(self, context):
            self.node_id = context.split('STAGE: ', 1)[1].split(' ', 1)[0]

        async def run(self, conversation):
            calls.append(self.node_id)
            prompt = str(conversation.get_messages())
            assert '交付形式与实际能力' in prompt
            assert self.registry.get('WriteFile') is None
            assert self.registry.get('RequestAssistance') is not None
            tool = self.registry.get('SubmitHandoff')
            if self.node_id == 'analyze' and calls.count('analyze') == 1:
                # Legacy-style delivery with complete text but no typed reason.
                params = SubmitHandoffParams(summary='analysis prepared', status='needs_attention',
                    blockers=['Cannot write independent analysis files'], decisions=[f'建议 {i}' for i in range(23)])
            else:
                reader = self.registry.get('ReadStageResult')
                record = await reader.execute(ReadStageResultParams(node_id='analyze'))
                assert not record.is_error
                if self.node_id == 'analyze':
                    assert '本节点上次完整交接' in prompt and '建议 22' in record.output
                    assert 'previous_attempt_not_current_acceptance' in record.output
                else:
                    assert all(item['content'] in record.output for item in analysis_outputs())
                assert (await reader.execute(ReadStageResultParams(node_id='unrelated'))).is_error
                params = SubmitHandoffParams(summary='analysis delivered', inline_outputs=analysis_outputs())
            assert not (await tool.execute(params)).is_error
            conversation.add_assistant_message('analysis result')
            yield StreamText(text='analysis result')

    def build(_session, _registry, role, _model, policy, _verdict):
        return FakeAgent(role, policy), None, SimpleNamespace(conversation=ConversationManager(), aclose=AsyncMock())

    monkeypatch.setattr(expert_web, '_build_role_agent', build)
    session = SimpleNamespace(work_dir=str(project), conversation_id='delivery-test', multi_agent=False,
        session=SimpleNamespace(meta=SimpleNamespace(message_count=0), append=lambda *a: None,
            append_expert_run=lambda *a, **kw: None), conversation=ConversationManager(), expert_run_id='',
        expert_control_action='pause', context=SimpleNamespace(get=lambda key, default=None: default,
            slots=SimpleNamespace(resolve=lambda key: {})),
        task_manager=SimpleNamespace(list_tasks=lambda: [], mark_notified=lambda ids: None),
        expert_child_event_callback=None, status_payload=lambda: {}, tasks_payload=lambda: [])
    async def send(event):
        events.append(copy.deepcopy(event))
    kwargs = dict(session=session,
        registry_service=SimpleNamespace(catalog=AsyncMock(return_value={'providers': []}), resolve=lambda model: None),
        profile_store=SimpleNamespace(get=lambda _: _profile()), flow_store=flows,
        plan_store=PlanSessionStore(tmp_path / 'plans.json'), run_store=runs)
    ws = SimpleNamespace(send_json=send)
    await asyncio.wait_for(expert_web.run_expert_chat(ws, '分析并交接', confirmed_new_flow=flows.get(project), **kwargs), 10)
    run_id = next(event['run_id'] for event in events if event.get('run_id'))
    assert calls == ['analyze'] and runs.load(run_id)['status'] != 'completed'
    await asyncio.wait_for(expert_web.run_expert_chat(ws, '继续', resume_run_id=run_id, **kwargs), 10)
    assert calls == ['analyze', 'analyze', 'done']
    saved = runs.load(run_id)
    assert saved['status'] == 'completed'
    assert saved['node_states']['analyze']['handoff']['inline_outputs'] == analysis_outputs()
    # The web runtime may save its hidden review-context cache; inline delivery
    # must not create user-facing workspace files.
    assert [path.name for path in project.iterdir() if path.name != '.mewcode'] == []
