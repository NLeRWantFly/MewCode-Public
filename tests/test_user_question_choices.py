import copy
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from mewcode.expert_group import ExpertRoleResult
from mewcode.experts.node_ledger import ledger_for_run
from mewcode.experts.recovery_prompt_migration import retire_obsolete_question_blockers
from mewcode.experts.waits import BranchWaitPaused
from mewcode.tools.ask_user import AskUserParams, QuestionItem
from mewcode.tools.submit_handoff import SubmitHandoffParams
from tests.test_scheduler_recovery import controller, decide


OPTIONS = [{'id': 'book', 'label': '按书目分类', 'description': '保留四本书各自的章节树。', 'recommended': True},
           {'value': 'topic', 'label': '按专题分类', 'description': '合并相同主题。'}]


def test_question_schema_preserves_metadata_and_stable_ids():
    params = AskUserParams(purpose='user_input', questions=[{'type': 'radio', 'name': 'category',
        'message': '采用哪种分类？', 'title': '选择分类方式', 'context': '四本书已核验。',
        'details': '诊断记录', 'options': OPTIONS, 'allow_other': True}])
    question = params.model_dump()['questions'][0]
    assert question['options'][0] == {'value': 'book', 'label': '按书目分类',
        'description': '保留四本书各自的章节树。', 'recommended': True}
    assert question['context'] == '四本书已核验。' and question['allow_other']
    assert QuestionItem(type='select', name='legacy', message='选择环境', options=['dev', 'prod']).options == ['dev', 'prod']


@pytest.mark.parametrize('options', [[OPTIONS[0], OPTIONS[0]], ['', 'other'],
                                   [OPTIONS[0], {**OPTIONS[1], 'recommended': True}]])
def test_ambiguous_choice_ids_or_recommendations_rejected(options):
    with pytest.raises(ValidationError):
        QuestionItem(type='radio', name='direction', message='选择', options=options)


def test_handoff_options_need_a_concrete_question():
    with pytest.raises(ValidationError):
        SubmitHandoffParams(summary='缺少信息', status='needs_attention', question_options=OPTIONS)
    handoff = SubmitHandoffParams(summary='缺少分类', status='needs_attention', blocker_kind='user_input',
        question='采用哪种分类？', question_options=OPTIONS, question_context='保留原始来源。').model_dump()
    assert handoff['question_options'][0]['value'] == 'book'


@pytest.mark.asyncio
async def test_structured_choices_remain_compatible_with_advisory_decisions(tmp_path):
    import json
    from mewcode.decisions.clarification import before_ask
    from tests.test_decision_runtime import harness
    runtime, _, _ = harness(tmp_path, chosen='option_0')
    params = AskUserParams(purpose='decision', decision_issue='category', questions=[
        {'type': 'radio', 'name': 'category', 'message': '选择分类', 'options': OPTIONS}])
    result = json.loads((await before_ask(runtime.agent, params)).output)
    assert result['suggested_option']['value'] == 'book'
    assert result['source'] == 'jev_advisory_not_user_answer'


@pytest.mark.asyncio
async def test_assistance_receipt_preserves_question_choices_and_background(tmp_path):
    from mewcode.expert_group import ExpertExecutionPolicy
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.experts.assistance import AssistanceCoordinator, AssistanceRequest, RunContext
    from tests.test_retry_memory import retry_flow
    flow = retry_flow()
    store = ExpertRunStore(tmp_path / 'runs')
    store.create(run_id='b' * 32, conversation_id='c', task='task', attachments=[], profile={}, strategy='stable', flow=flow)
    store.mutate('b' * 32, lambda r: r['node_states'].setdefault('work', {}))
    handoff = SubmitHandoffParams(summary='缺少分类', status='needs_attention', blocker_kind='user_input',
        question='采用哪种分类？', question_options=OPTIONS, question_context='保留原始来源。').model_dump()
    async def dispatch(helper, prompt, policy):
        policy.assistance_holder.update(next_step='ask_human', notes='需要分类偏好', clearances=[])
        return ExpertRoleResult('需要分类偏好', status='needs_attention', handoff=handoff)
    coordinator = AssistanceCoordinator(store=store, run_id='b' * 32, dispatch=dispatch, emit=AsyncMock(),
        before_resume=AsyncMock(), task='task', work_dir=str(tmp_path), model={})
    receipt = await coordinator.run(AssistanceRequest('r', 'work', 0, 'diagnosis', 'check', (), (), 'node'),
        RunContext(flow.node_map['work'], ExpertExecutionPolicy('stable', False, 0)))
    assert receipt.question == handoff['question'] and receipt.question_context == '保留原始来源。'
    assert receipt.question_options[0]['value'] == 'book'
    persisted = store.load('b' * 32)['node_states']['work']['assistance_receipts'][-1]
    assert list(persisted['question_options']) == handoff['question_options']


@pytest.mark.asyncio
async def test_real_assistance_choices_reach_ui_and_selected_id_returns_as_context(tmp_path, monkeypatch):
    handler, flow, _, store, _, _, events = controller(tmp_path, monkeypatch)
    async def emit(event):
        events.append(event)
        if event['type'] == 'ask_user':
            handler.session._ask_user_request.future.set_result({'direction': 'book'})
    handler.emit = emit
    handler.primary_repair = AsyncMock(return_value={'action': 'human', 'source': 'primary_model_assistance',
        'human_question': '采用哪种分类？', 'human_question_options': OPTIONS,
        'human_question_context': '四本书保留原始来源。'})
    result = await decide(handler, flow, result=ExpertRoleResult('需要协助', status='needs_attention',
        error_kind='assistance_requested'), host_facts={'trigger': 'assistance_requested'})
    prompt = next(e for e in events if e['type'] == 'ask_user')['questions'][0]
    assert prompt['type'] == 'radio' and prompt['options'][0]['value'] == 'book'
    assert prompt['message'] == '采用哪种分类？' and '已尝试' not in prompt['message']
    assert prompt['context'].endswith('四本书保留原始来源。') and prompt['details'].startswith('已尝试')
    assert result['user_answer'] == 'book' and result['chosen']['next_step'].startswith('按书目分类:')
    history = store.load(handler.run_id)['node_states']['extract']['interaction_history'][-1]
    assert history['pending']['questions'] == [prompt] and history['answered']
    state = store.load(handler.run_id)['node_states']['extract']['scheduler_recovery']
    assert state['phase'] == 'continuing' and state['decisions'][-1]['execution_status'] == '待执行'
    await handler.record_result({'node_id': 'extract', 'role': 'executor', 'status': 'completed'})
    assert store.load(handler.run_id)['node_states']['extract']['scheduler_recovery']['phase'] == 'finished'


@pytest.mark.asyncio
async def test_generic_recovery_stop_does_not_invent_a_business_question(tmp_path, monkeypatch):
    handler, flow, _, store, _, _, events = controller(tmp_path, monkeypatch)
    def seed(record):
        ledger_for_run(record).record('extract', 'blocker', {'kind': 'business_ambiguity', 'ref': 'no-contract'})
    store.mutate(handler.run_id, seed, durable=True)
    with pytest.raises(BranchWaitPaused) as caught:
        await decide(handler, flow)
    assert caught.value.error_kind == 'system_recovery_blocked'
    assert not any(e['type'] == 'ask_user' for e in events)
    stage = store.load(handler.run_id)['node_states']['extract']
    assert stage['status'] == 'needs_attention' and stage['recovery_case']['status'] == 'reported'
    assert not stage['recovery_case']['pending']
    assert stage['recovery_case']['handling']['owner'] == 'system'
    assert any(f.payload.get('kind') == 'business_ambiguity' for f in ledger_for_run(store.load(handler.run_id)).view('extract').active_blockers)


@pytest.mark.asyncio
async def test_recommended_business_choice_never_auto_answers_on_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv('MEWCODE_EXPERT_HITL_SOFT_TIMEOUT', '0.01')
    from mewcode.experts.iteration_recovery import CONTINUE
    handler, flow, _, store, _, _, events = controller(tmp_path, monkeypatch)
    async def emit(event):
        events.append(event)
    handler.emit = emit
    handler.primary_repair = AsyncMock(return_value={'action': 'human', 'source': 'primary_model_assistance',
        'human_question': '采用哪种分类？', 'human_question_options': [
            {'value': CONTINUE, 'label': '沿用原分类', 'recommended': True}, {'value': 'new', 'label': '另行分类'}]})
    with pytest.raises(BranchWaitPaused):
        await decide(handler, flow, result=ExpertRoleResult('需要协助', status='needs_attention',
            error_kind='assistance_requested'), host_facts={'trigger': 'assistance_requested'})
    pending = store.load(handler.run_id)['node_states']['extract']['pending_interaction']
    assert pending['questions'][0]['requires_user_answer']
    assert not any(f.kind == 'answer' for f in ledger_for_run(store.load(handler.run_id)).view('extract').history)


def test_retired_prompt_cannot_leave_a_derived_business_blocker():
    record = {'node_states': {'N01': {}}}
    ledger = ledger_for_run(record)
    question = ledger.record('N01', 'question', {'request_id': 'old', 'tool': 'RecoveryDecision'})
    ledger.record('N01', 'blocker', {'kind': 'human_question', 'ref': 'old'})
    actual = ledger.record('N01', 'blocker', {'kind': 'human_question', 'ref': 'real'})
    unknown = ledger.record('N01', 'blocker', {'kind': 'effect_unknown', 'ref': 'operation'})
    retired = ledger.record('N01', 'maintenance', {'reason': 'technical_assistance_prompt', 'question_fact_id': question.fact_id})
    ledger.supersede(question.fact_id, retired.fact_id)
    assert retire_obsolete_question_blockers(record, 'N01') == 1
    assert {f.fact_id for f in ledger.view('N01').active_blockers} == {actual.fact_id, unknown.fact_id}
    assert not any(f.kind in {'answer', 'clearance'} for f in ledger.view('N01').history)
    assert retire_obsolete_question_blockers(record, 'N01') == 0


@pytest.mark.parametrize('reason,source', [('host_facts_revalidated', 'host'), ('technical_assistance_prompt', 'user')])
def test_other_retirements_are_not_used_to_clear_questions(reason, source):
    record = {'node_states': {'N01': {}}}
    ledger = ledger_for_run(record)
    question = ledger.record('N01', 'question', {'request_id': 'real'})
    ledger.record('N01', 'blocker', {'kind': 'human_question', 'ref': 'real'})
    replacement = ledger.record('N01', 'maintenance', {'reason': reason, 'question_fact_id': question.fact_id}, source=source)
    ledger.supersede(question.fact_id, replacement.fact_id)
    before = copy.deepcopy(record)
    assert retire_obsolete_question_blockers(record, 'N01') == 0 and record == before
