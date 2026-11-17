"""Ordinary independent review must not depend on a repair queue existing."""
import asyncio
import copy
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from mewcode.expert_group import ExpertRoleResult
from mewcode.experts.continuation import checkpoint_prompt, turn_guidance
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.experts.review_protocol import bind_review_protocol, resolve_review_protocol
from mewcode.experts.workflow_context import project_working_context
from mewcode.tools import ToolRegistry
from mewcode.tools.capabilities import inventory
from mewcode.tools.submit_handoff import HandoffHolder, SubmitHandoff


def review_flow():
    return ExpertFlowSpec.from_value({'flow_id': 'independent-review', 'finalizer_node_id': 'check',
        'nodes': [{'node_id': 'work', 'role': 'executor', 'label': '整理交付'},
                  {'node_id': 'first', 'role': 'auditor', 'label': '第一路复查'},
                  {'node_id': 'second', 'role': 'auditor', 'label': '第二路复查'},
                  {'node_id': 'check', 'role': 'auditor', 'label': '判定'}],
        'edges': [{'from_node': 'work', 'to_node': 'first', 'condition': 'pass'},
                  {'from_node': 'work', 'to_node': 'second', 'condition': 'always'},
                  {'from_node': 'first', 'to_node': 'check', 'condition': 'always'},
                  {'from_node': 'second', 'to_node': 'check', 'condition': 'always'}]})


CONTEXT = {'contract_hash': 'frozen', 'reviewer_node_ids': ['first', 'second', 'check']}


@pytest.mark.parametrize('initialized', [[], ['first'], ['first', 'second']])
def test_graph_routes_remain_available_without_submitted_batches(initialized):
    snapshot = {'review_queues': {key: {'contract_hash': 'frozen', 'items': [], 'records': {}}
                                 for key in initialized}}
    before = copy.deepcopy(snapshot)
    protocol = resolve_review_protocol(review_flow(), 'work', snapshot, CONTEXT)
    assert snapshot == before
    routes = protocol['verification_routes']
    assert {row['node_id'] for row in routes} == {'first', 'second'}
    assert all(row['request_tool'] == 'SubmitHandoff' and row['requires'] == 'current_stage_handoff'
               for row in routes)
    assert {row['node_id'] for row in routes if row.get('batch_handoff')} == set(initialized)
    assert not any('batch_ids' in row for row in routes)


def test_plain_dag_also_exposes_scheduled_independent_review():
    protocol = resolve_review_protocol(review_flow(), 'work', {})
    assert len(protocol['verification_routes']) == 2
    assert protocol['batch_reviewer_node_ids'] == []
    decision = resolve_review_protocol(review_flow(), 'first', {}, submission_tool='SubmitVerdict')
    assert decision['verification_routes'][0]['request_tool'] == 'SubmitVerdict'


@pytest.mark.parametrize('queues', [[], {'first': {}}, {'first': {'contract_hash': 'old'}}, {'first': 'unknown'}])
def test_invalid_or_stale_queue_cannot_silently_become_ordinary_review(queues):
    with pytest.raises(ValueError):
        resolve_review_protocol(review_flow(), 'work', {'review_queues': queues}, CONTEXT)


def test_capability_probe_does_not_report_review_missing_when_batch_tool_absent():
    registry = ToolRegistry()
    registry.register(SubmitHandoff(HandoffHolder()))
    protocol = resolve_review_protocol(review_flow(), 'work', {}, CONTEXT)
    agent = SimpleNamespace(registry=registry, review_protocol=protocol,
                            verification_routes=protocol['verification_routes'])
    facts = inventory(agent)
    assert facts['verification_routes']
    assert all(row['request_tool_enabled'] for row in facts['verification_routes'])
    assert not registry.is_enabled('RequestVerification')


def legacy_stage(*, queued=False):
    stage = {'node_id': 'work', 'node_attempt': 8, 'epoch': 1, 'input_revision': 'current', 'status': 'running',
             'handoff': {'status': 'needs_attention', 'open_items': ['先补齐冻结工作单才能继续复核']},
             'tools': {'failed-method': {'name': 'RepairWorkBatch', 'tool_id': 'failed-method', 'args': {'action': 'plan'},
                 'status': 'failed', 'is_error': True, 'node_attempt': 8, 'epoch': 1,
                 'input_revision': 'current', 'declared_at': 1, 'output': '尚无工作单'}}}
    snapshot = {'review_queues': {'first': {'contract_hash': 'frozen'}}} if queued else {}
    protocol = resolve_review_protocol(review_flow(), 'work', snapshot, CONTEXT)
    stage['review_protocol'] = bind_review_protocol(protocol, stage)
    return stage


def test_resumed_summary_uses_current_protocol_instead_of_old_batch_instructions():
    stage = legacy_stage()
    before = copy.deepcopy(stage)
    current = project_working_context(stage, binding={})
    assert current['waiting_for'] == 'agent'
    assert '普通阶段交接' in current['next_action']
    assert current['next_items'] == []
    assert current['review_protocol']['input_kind'] == 'stage_handoff'
    restored = checkpoint_prompt(stage, node_id='work')
    assert '普通阶段交接' in restored and '@review_protocol' in restored
    assert stage == before
    assert stage['handoff']['status'] == 'needs_attention'


@pytest.mark.parametrize('change', [{'node_attempt': 9}, {'epoch': 2}, {'input_revision': 'other'}])
def test_stale_protocol_binding_cannot_change_current_work(change):
    stage = legacy_stage()
    stage.update(change)
    current = project_working_context(stage, binding={})
    assert 'review_protocol' not in current
    assert current['next_action'] == stage['handoff']['open_items'][0]


@pytest.mark.parametrize('boundary', ['queue', 'question', 'permission', 'assistance', 'unknown_result',
    'handoff_blocker', 'ledger_handoff', 'ledger_permission', 'ledger_unknown', 'safety_review', 'completed'])
def test_protocol_recovery_preserves_real_queues_and_unsettled_boundaries(boundary):
    stage = legacy_stage(queued=boundary == 'queue')
    if boundary == 'question':
        stage['pending_interaction'] = {'request_id': 'answer', 'question': '选择业务范围'}
    elif boundary == 'permission':
        stage['pending_permission'] = {'tool_id': 'permission'}
    elif boundary == 'assistance':
        stage['assistance_request'] = {'status': 'pending'}
    elif boundary == 'unknown_result':
        stage['tools']['unknown'] = {'tool_id': 'unknown', 'name': 'Bash', 'status': 'outcome_unknown',
                                    'node_attempt': 8, 'epoch': 1, 'input_revision': 'current', 'declared_at': 2}
    elif boundary == 'handoff_blocker':
        stage['handoff'].update(blocker_kind='permission_denied', blockers=['真实授权仍缺失'])
    elif boundary == 'ledger_handoff':
        stage['ledger_view'] = {'last_handoff': {'payload': {'blockers': ['真实外部依赖未解除']}}}
        stage.pop('handoff')
    elif boundary in {'ledger_permission', 'ledger_unknown'}:
        stage['ledger_view'] = {'active_blockers': [{'fact_id': 'unresolved', 'payload': {
            'kind': 'permission' if boundary == 'ledger_permission' else 'unknown_effect'}}]}
    elif boundary == 'safety_review':
        stage['safety_review_state'] = {'pending_call_ref': 'review-required'}
    elif boundary == 'completed':
        stage['status'] = 'completed'
    current = project_working_context(stage, binding={})
    assert '旧批次工具失败不是' not in current['next_action']
    if boundary in {'question', 'permission'}:
        assert current['waiting_for'] == 'user'


def test_current_protocol_correction_is_once_per_failed_method_not_once_per_read():
    stage, guidance = legacy_stage(), {}
    assert turn_guidance(stage, guidance)['kind'] == 'correct_review_protocol'
    stage['tools']['read'] = {'tool_id': 'read', 'name': 'ReadFile', 'status': 'completed', 'is_error': False,
                            'node_attempt': 8, 'epoch': 1, 'input_revision': 'current', 'declared_at': 2}
    assert turn_guidance(stage, guidance) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('queue, invalid', [(None, False), ({}, True), ({'contract_hash': 'old'}, True)])
async def test_empty_queue_response_preserves_protocol_and_invalid_queue_boundaries(queue, invalid):
    import json
    from mewcode.tools.review_work_queue import ReadReviewQueue, ReadReviewQueueParams
    store = SimpleNamespace(load=lambda _: {'review_queues': {'first': queue}})
    tool = ReadReviewQueue(store, 'run', ['first'], 'frozen')
    result = await tool.execute(ReadReviewQueueParams(reviewer_node_id='first'))
    assert result.is_error is invalid
    if not invalid:
        value = json.loads(result.output)
        assert value['queue_initialized'] is False and value['input_kind'] == 'stage_handoff'
        assert value['items'] == []


@pytest.mark.asyncio
async def test_unchanged_deliverable_still_reaches_review_after_a_failed_quality_check(tmp_path):
    from mewcode.tools.submit_handoff import SubmitHandoffParams
    from tests.test_generic_loops import report
    from tests.test_loop_checkpoint_liveness import execute, flow_case
    flow, contract = flow_case()
    artifact = tmp_path / 'candidate.txt'
    artifact.write_text('existing candidate', encoding='utf-8')
    original = artifact.read_bytes()
    calls = []

    async def run(node, incoming, policy, prompt):
        calls.append(node.node_id)
        if node.node_id == 'work':
            holder = HandoffHolder()
            result = await SubmitHandoff(holder, work_dir=tmp_path).execute(
                SubmitHandoffParams(summary='交付当前版本，等待下游复查', artifacts=[{'path': artifact.name}]))
            assert not result.is_error, result.output
            return ExpertRoleResult(text='existing candidate', handoff=holder.read())
        assert incoming[0]['handoff']['artifacts'][0]['path'] == artifact.name
        verdict = report(policy.loop_context, 'FAIL') if calls.count('check') == 1 else report(policy.loop_context)
        if calls.count('check') == 1:
            verdict['repair_node_ids'] = ['work']
        return ExpertRoleResult(text='actual independent review', loop_decision=verdict)

    _, result = await execute(flow, contract, {}, run)
    assert result.status == 'completed' and calls == ['work', 'check', 'work', 'check']
    assert artifact.read_bytes() == original


@pytest.mark.asyncio
async def test_ordinary_review_resume_keeps_completed_upstream_and_no_batch_receipt():
    from tests.test_generic_loops import report
    from tests.test_loop_checkpoint_liveness import execute, flow_case
    flow, contract = flow_case()
    states, calls = {}, []

    async def first(node, incoming, policy, prompt):
        calls.append(node.node_id)
        if node.node_id == 'check':
            raise asyncio.CancelledError()
        return ExpertRoleResult(text='retained ordinary delivery', handoff={'status': 'completed',
            'inline_outputs': [{'name': '候选', 'content': '当前固定版本'}]})

    with pytest.raises(asyncio.CancelledError):
        await execute(flow, contract, states, first)
    before = copy.deepcopy(states['improve']['round_results']['work'])

    async def resumed(node, incoming, policy, prompt):
        calls.append(node.node_id)
        assert node.node_id == 'check' and incoming[0]['handoff'] == before['handoff']
        return ExpertRoleResult(text='reviewed', loop_decision=report(policy.loop_context))

    _, result = await execute(flow, contract, states, resumed)
    assert result.status == 'completed' and calls == ['work', 'check', 'check']
    assert states['improve']['round_results']['work'] == before
    assert not asdict(result).get('verification_request')


@pytest.mark.asyncio
async def test_real_web_adapter_runs_two_reviews_without_a_repair_queue(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from mewcode import expert_web
    from mewcode.agent import Agent, StreamText, ToolResultEvent, ToolUseEvent, TurnComplete
    from mewcode.conversation import ConversationManager
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.experts.flow import ExpertFlowStore
    from mewcode.orchestration.convert import to_expert_flow
    from mewcode.orchestration.plan_session import PlanSessionStore
    from mewcode.orchestration.spec import OrchestrationSpec
    from mewcode.permissions.policy import build_permission_checker
    from mewcode.tools.read_stage_result import ReadStageResultParams
    from mewcode.tools.submit_handoff import SubmitHandoffParams
    from mewcode.tools.submit_loop_decision import SubmitLoopDecisionParams
    from tests.test_expert_children import FakeSession, make_registry
    from tests.test_agent import MockLLMClient
    from tests.test_expert_flow import _profile
    from tests.test_generic_loops import report, spec_for

    monkeypatch.setattr('mewcode.permissions.review_client.load_review_model_settings', lambda: {})
    raw = spec_for('只补当前版本的两路复核').as_dict()
    raw['nodes'][2:2] = [{'node_id': key, 'kind': 'decision', 'label': key, 'goal': '核对当前固定交付'}
                         for key in ('first', 'second')]
    for node in raw['nodes']:
        node['capabilities'] = {'write_paths': ['candidate.txt']} if node['node_id'] == 'work' else {}
    raw['edges'] = [{'from': a, 'to': b} for a, b in (
        ('prepare', 'work'), ('work', 'first'), ('first', 'second'), ('second', 'check'))] + [
        {'from': 'check', 'to': 'work', 'condition': 'loop'},
        {'from': 'check', 'to': 'done', 'condition': 'pass'}]
    raw['meta']['loops'][0].update(node_ids=['work', 'first', 'second', 'check'],
        reviewer_node_ids=['first', 'second', 'check'], control_mode='team',
        requires_repair=True, repair_node_ids=['work'])
    flow = to_expert_flow(OrchestrationSpec.from_value(raw))
    flows = ExpertFlowStore(tmp_path / 'flows.json')
    flows.put(tmp_path, flow)
    store = ExpertRunStore(tmp_path / 'runs', storage='sqlite')
    artifact = tmp_path / 'candidate.txt'
    artifact.write_text('现有融合稿保持原样，独立核验当前版本', encoding='utf-8')
    original = artifact.read_bytes()
    calls, events = [], []

    class OfflineAgent(Agent):
        def __init__(self, role, policy, holder):
            self.policy = policy
            host = FakeSession(make_registry())
            host.work_dir = str(tmp_path)
            registry = expert_web._role_registry(host, role, policy, holder)
            super().__init__(MockLLMClient([]), registry, 'openai', str(tmp_path),
                             permission_checker=build_permission_checker(tmp_path))

        def set_review_authorization_context(self, context):
            assert '【队列候选确定性预检】' not in context
            assert '先用RepairWorkBatch.plan' not in context

        async def run(self, conversation):
            key = self.policy.execution_node_id
            calls.append(key)
            assert self.registry.get('RepairWorkBatch') is None
            assert self.registry.get('RequestVerification') is None
            assert self.registry.get('VerifyArtifacts') is None
            assert self.review_protocol['input_kind'] == 'stage_handoff'
            source = await self.registry.get('ReadStageResult').execute(ReadStageResultParams(node_id='@review_protocol'))
            assert not source.is_error and 'stage_handoff' in source.output
            if key in {'first', 'second', 'check'}:
                value = report(self.policy.loop_context)
                tool, params = 'SubmitLoopDecision', SubmitLoopDecisionParams(**value)
            else:
                tool = 'SubmitHandoff'
                params = SubmitHandoffParams(summary='交付当前固定版本，独立复查仍由下游执行',
                    artifacts=[{'path': artifact.name}], inline_outputs=[{'name': '待复核范围', 'content': '当前版本'}])
            yield ToolUseEvent(tool, key + '-delivery', params.model_dump())
            result = await self.registry.get(tool).execute(params)
            assert not result.is_error, result.output
            yield ToolResultEvent(key + '-delivery', tool, result.output, False, .001)
            yield TurnComplete(1)
            yield StreamText('当前节点已提交实际结果')

    monkeypatch.setattr(expert_web, '_build_role_agent', lambda _s, _r, role, _m, policy, holder: (
        OfflineAgent(role, policy, holder), None,
        SimpleNamespace(conversation=ConversationManager(), aclose=AsyncMock())))
    session = SimpleNamespace(work_dir=str(tmp_path), conversation_id='normal-two-reviewers', multi_agent=False,
        session=SimpleNamespace(meta=SimpleNamespace(message_count=0), append=lambda *a: None,
            append_expert_run=lambda *a, **kw: None), conversation=ConversationManager(), expert_run_id='',
        expert_control_action='pause', context=SimpleNamespace(get=lambda key, default=None: default,
            slots=SimpleNamespace(resolve=lambda key: {})),
        task_manager=SimpleNamespace(list_tasks=lambda: [], mark_notified=lambda ids: None),
        expert_child_event_callback=None, status_payload=lambda: {}, tasks_payload=lambda: [])

    async def send(event):
        events.append(copy.deepcopy(event))

    await asyncio.wait_for(expert_web.run_expert_chat(SimpleNamespace(send_json=send), '复核当前已有成果',
        session=session, registry_service=SimpleNamespace(catalog=AsyncMock(return_value={'providers': []}), resolve=lambda _: None),
        profile_store=SimpleNamespace(get=lambda _: _profile()), flow_store=flows,
        plan_store=PlanSessionStore(tmp_path / 'plans.json'), run_store=store, confirmed_new_flow=flow), 180)
    run_id = next(row['run_id'] for row in events if row.get('run_id'))
    saved = store.load(run_id)
    assert saved['status'] == 'completed', {key: row.get('text') for key, row in saved['node_states'].items()}
    assert calls == ['prepare', 'work', 'first', 'second', 'check', 'done']
    assert not saved.get('review_queues') and artifact.read_bytes() == original
    assert all(saved['node_states'][key]['review_protocol']['input_kind'] == 'stage_handoff'
               for key in ('work', 'first', 'second', 'check'))
