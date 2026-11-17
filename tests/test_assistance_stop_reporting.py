"""Actual host dispatch and durable recovery stops, with no live model calls."""
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.expert_group import ExpertRoleResult
from mewcode.experts.node_ledger import ledger_for_run
from mewcode.experts.recovery_prompt_migration import retire_failed_assistance_prompt
from mewcode.experts.waits import BranchWaitPaused
from mewcode.permissions.effects import effect_signature
from mewcode.tools.base import ToolCallComplete
from mewcode.tools.decide_next_step import DecideNextStep
from mewcode.tools.inspect_tool_history import InspectToolHistory
from mewcode.tools.read_stage_result import ReadStageResult
from mewcode.tools.request_assistance import SubmitAssistanceReceipt
from mewcode.tools.submit_handoff import HandoffHolder, SubmitHandoff
from tests.test_effect_recovery_integration import _make_agent, _invoke, _write, _deny
from tests.test_scheduler_recovery import controller, decide
from tests.test_team_loop_control import execute, report


@pytest.mark.asyncio
async def test_denied_write_does_not_prevent_reporting_blocked_handoff(tmp_path, monkeypatch):
    agent, checker, reviewer = _make_agent(tmp_path, monkeypatch, [_deny()])
    checker.review_workspace_writes = True
    target = tmp_path / 'denied.txt'
    await _invoke(agent, _write(target))
    holder = HandoffHolder()
    tool = SubmitHandoff(holder, work_dir=tmp_path)
    agent.registry.register(tool)
    agent.registry.mark_discovered(tool.name)
    params = {'summary': '调查未完成，保留已核实证据。', 'status': 'blocked',
              'blocker_kind': 'capability_missing', 'blockers': ['像素观察工具尚不可用']}
    result, _ = await _invoke(agent, ToolCallComplete('handoff', tool.name, params))
    assert not result[0].is_error and holder.value['status'] == 'blocked'
    assert reviewer._ask_model.await_count == 1
    assert agent._review_recovery.total_denials == 1 and not target.exists()
    assert len(agent._review_recovery.requests) == 1


@pytest.mark.parametrize('tool,args', [
    (SubmitHandoff(HandoffHolder()), {'command_requests': [{'command': 'never execute this'}]}),
    (SubmitAssistanceReceipt({}), {'patch': {'never': 'apply here'}}),
    (ReadStageResult({'N01': {}}), {'node_id': 'N01'}),
    (InspectToolHistory(SimpleNamespace()), {'action': 'report'}),
])
def test_known_host_controls_are_disjoint_from_execution_denials(tmp_path, tool, args):
    effect = effect_signature(tool.name, args, work_dir=tmp_path, tool=tool)
    denied = effect_signature('Bash', {'command': 'opaque script'}, work_dir=tmp_path)
    assert effect.kind == 'read' and not effect.uncertain and not effect.overlaps(denied)
    spoof = SimpleNamespace(name=tool.name, category='read')
    assert effect_signature(tool.name, args, work_dir=tmp_path, tool=spoof).kind == 'exec_unbounded'


@pytest.mark.parametrize('tool,args', [
    (DecideNextStep(SimpleNamespace()), {}),
    (InspectToolHistory(SimpleNamespace()), {'action': 'sample'}),
])
def test_auxiliary_model_calls_keep_network_and_cost_review(tmp_path, monkeypatch, tool, args):
    effect = effect_signature(tool.name, args, work_dir=tmp_path, tool=tool)
    _, checker, _ = _make_agent(tmp_path, monkeypatch)
    assert effect.kind == 'net_egress' and effect.blast == 'external'
    assert tool.permission_risk(args).network and tool.permission_risk(args).cost
    assert checker.check(tool, args).effect == 'review'


@pytest.mark.asyncio
@pytest.mark.parametrize('available', [False, True])
async def test_technical_assistance_stop_reports_actual_result_without_business_prompt(tmp_path, monkeypatch, available):
    handler, flow, _, store, _, _, events = controller(tmp_path, monkeypatch)
    if available:
        handler.primary_repair = AsyncMock(return_value={'action': 'human', 'source': 'primary_model_assistance',
            'assistance_receipt': {'next_step': 'ask_human', 'notes': 'SubmitHandoff 被审查拒绝，交接未记录。'},
            'chosen': {'next_step': 'SubmitHandoff 被审查拒绝，交接未记录。'}})
    with pytest.raises(BranchWaitPaused) as stopped:
        await decide(handler, flow, result=ExpertRoleResult('需要协助', status='needs_attention',
            error_kind='assistance_requested'), host_facts={'trigger': 'assistance_requested'})
    assert stopped.value.error_kind == 'system_recovery_blocked'
    assert stopped.value.reason
    state = store.load(handler.run_id)['node_states']['extract']['recovery_case']
    assert state['status'] == 'reported' and not state['pending']
    assert state['handling']['owner'] == 'system'
    assert state['report']['kind'] == 'incident' and state['report']['options']
    if available:
        assert 'SubmitHandoff' in state['report']['reason']
    assert not any(e['type'] == 'ask_user' for e in events)
    assert events[-1]['type'] == 'recovery_case' and events[-1]['phase'] == '系统受阻'


@pytest.mark.asyncio
async def test_concrete_assistance_question_is_presented_without_substituting_generic_direction(tmp_path, monkeypatch):
    handler, flow, _, store, _, _, events = controller(tmp_path, monkeypatch)
    handler.primary_repair = AsyncMock(return_value={'action': 'human', 'source': 'primary_model_assistance',
        'human_question': '这四本书使用哪个分类名称？', 'chosen': {'next_step': '实际分类尚未确认'}})
    with pytest.raises(BranchWaitPaused):
        await decide(handler, flow, result=ExpertRoleResult('需要协助', status='needs_attention',
            error_kind='assistance_requested'), host_facts={'trigger': 'assistance_requested'})
    prompt = next(e for e in events if e['type'] == 'ask_user')['questions'][0]['message']
    assert '这四本书使用哪个分类名称？' in prompt
    assert '请补充业务方向' not in prompt and "'action':" not in prompt
    assert store.load(handler.run_id)['node_states']['extract']['pending_interaction']['tool'] == 'SchedulerRecovery'


@pytest.mark.asyncio
async def test_loop_preserves_technical_pause_reason_instead_of_waiting_for_business_decision(tmp_path, monkeypatch):
    handler, flow, contract, _, _, _, events = controller(tmp_path, monkeypatch)
    handler.primary_repair = AsyncMock(return_value={'action': 'human', 'source': 'primary_model_assistance',
        'chosen': {'next_step': 'SubmitHandoff 被拒，协助没有完成。'}})
    async def runner(node, incoming, policy, prompt):
        if node.node_id == 'extract':
            return ExpertRoleResult('需要协助', status='needs_attention', error_kind='assistance_requested')
        return ExpertRoleResult('已完成', loop_decision=report(policy.loop_context) if node.node_id == 'check' else None)
    _, result = await execute(flow, contract, {}, runner, recover_node=handler)
    assert result.status == 'paused' and result.error_kind == 'system_recovery_blocked'
    assert result.text == 'SubmitHandoff 被拒，协助没有完成。'
    assert not any(e['type'] == 'ask_user' for e in events)


def obsolete_prompt():
    reason = '调用主模型协助核对证据并换一种方法。'
    questions = [{'type': 'text', 'name': 'direction', 'message': f'节点「调查四个PDF」需要具体决定：{reason} 已尝试：[]。请补充业务方向、必要材料或所需授权的处理方式。'}]
    receipt = {'request_id': 'assist1', 'next_step': 'ask_human', 'notes': '缺少结构化回执'}
    stage = {'handoff': {}, 'assistance_request': {'request_id': 'assist1', 'status': 'completed'},
        'scheduler_recovery': {'phase': 'awaiting_input', 'decisions': [{'action': 'human',
            'source': 'primary_model_assistance', 'controller': {'action': 'assist', 'reason': reason},
            'assistance_receipt': receipt}]},
        'pending_interaction': {'request_id': 'q1', 'tool': 'RecoveryDecision', 'questions': questions}}
    record = {'node_states': {'N01': stage}}
    ledger = ledger_for_run(record)
    fact = ledger.record('N01', 'question', {'request_id': 'q1', 'tool': 'RecoveryDecision', 'questions': questions})
    stage['pending_interaction']['question_fact_id'] = fact.fact_id
    ledger.record('N01', 'blocker', {'kind': 'effect_unknown', 'request_id': 'command1'})
    ledger.record('N01', 'receipt', {'request_id': 'command1', 'status': 'outcome_unknown'})
    return record, stage, ledger, fact


def test_only_obsolete_technical_question_retires_without_clearing_unknown_effect():
    record, stage, ledger, fact = obsolete_prompt()
    assert retire_failed_assistance_prompt(record, 'N01')
    assert 'pending_interaction' not in stage and not stage['interaction_history'][-1]['answered']
    assert stage['scheduler_recovery']['phase'] == 'stopped'
    assert ledger.view('N01').pending_question is None
    assert ledger.view('N01').active_blockers[0].payload['kind'] == 'unknown_effect'
    assert ledger.view('N01').current_receipts[0].payload['status'] == 'outcome_unknown'
    assert not retire_failed_assistance_prompt(record, 'N01')


@pytest.mark.parametrize('case', ['answered', 'ledger_answer', 'permission', 'question', 'wrong_receipt', 'wrong_prompt', 'no_fact', 'no_ledger'])
def test_real_answers_permissions_and_other_questions_never_retire(case):
    record, stage, ledger, fact = obsolete_prompt()
    if case == 'answered':
        stage['interaction_history'] = [{'pending': {'request_id': 'q1'}, 'answered': True}]
    elif case == 'ledger_answer':
        ledger.record('N01', 'answer', {'question_fact_id': fact.fact_id}, source='user')
    elif case == 'permission':
        stage['pending_permission'] = {'request_id': 'permission1'}
    elif case == 'question':
        stage['handoff']['question'] = '允许哪些 PDF 内容外发？'
    elif case == 'wrong_receipt':
        stage['assistance_request']['request_id'] = 'another'
    elif case == 'wrong_prompt':
        stage['pending_interaction']['questions'] = [{'type': 'text', 'name': 'direction', 'message': '真实问题'}]
    elif case == 'no_fact':
        stage['pending_interaction']['question_fact_id'] = 'missing'
    else:
        record.pop('node_ledgers')
    before = copy.deepcopy(record)
    assert not retire_failed_assistance_prompt(record, 'N01')
    assert record == before
