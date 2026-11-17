"""Permission decisions stay concrete and do not consume unanswered materials."""
import copy
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from mewcode.experts.authorization_ledger import record_authorization
from mewcode.experts.case_runtime import answer_case
from mewcode.experts.recovery_case import cases, open_case, save_case
from mewcode.experts.waits import BranchWaitPaused
from tests.test_recovery_case_e2e import AUTH, RUN, harness, option, plan, run


@pytest.mark.asyncio
async def test_typed_authority_precedes_missing_facts_and_approval_rechecks_them(tmp_path):
    question = '这次样本页调用的费用怎样处理？'
    def options(n):
        selected = option(n, authority=True)
        if not h.store.load(RUN).get('case_inputs'):
            selected['needs']['user_input'] = [question]
        return [selected]
    h = harness(tmp_path, make_options=options)
    with pytest.raises(BranchWaitPaused):
        await run(h)
    case = cases(h.store.load(RUN))[0]
    pending = case['pending']
    assert pending['kind'] == 'elevation'
    assert [(request['operation_class'], request['scope']) for request in pending['authority']] == [(AUTH['operation_class'], AUTH['scope'])]
    assert pending['input_requirements'] == [question]
    assert not h.store.load(RUN).get('authorizations')
    assert not any(call[2].assistance_session for call in h.calls)
    h.store.mutate(RUN, lambda record: answer_case(record, case['case_id'], round=0, choice='approve'))
    approved = h.store.load(RUN)
    assert cases(approved)[0]['plan'] == {}
    assert approved['authorizations']['entries'][0]['scope']['operation_classes'] == ['external_send']
    assert 'spend_over_cap' not in approved['authorizations']['entries'][0]['scope']['operation_classes']
    assert approved['node_states']['work']['status'] != 'completed'
    with pytest.raises(BranchWaitPaused):
        await run(h)
    pending = cases(h.store.load(RUN))[0]['pending']
    assert pending['kind'] == 'input' and pending['questions'] == [question]
    assert not any(call[2].assistance_session for call in h.calls)
    h.store.mutate(RUN, lambda record: answer_case(record, case['case_id'], round=0, choice='input',
        answer='仅使用已付费套餐，不允许额外收费。'))
    await run(h)
    assert cases(h.store.load(RUN))[0]['status'] == 'resolved'
    assert len([call for call in h.calls if call[2].assistance_session]) == 1


def old_input_card(h):
    def prepare(record):
        record['status'] = 'paused'
        case = open_case(record, 'work')
        selected = option(authority=True)
        selected['needs']['user_input'] = ['确认上述外发范围，没有AUTH或ST时不调用。']
        current = plan({'case_id': case['case_id'], 'round': 1}, [selected], 'authority_gap')
        case.update(status='awaiting_user', plan=current, pending={'kind': 'input', 'round': 0,
            'plan': copy.deepcopy(current), 'option_id': selected['option_id'],
            'question': selected['needs']['user_input'][0],
            'options': [{'value': 'input', 'label': '提交补充'}, {'value': 'close', 'label': '停止本案件'}]})
        save_case(record, case)
        record['node_states']['work']['status'] = 'paused'
    h.store.mutate(RUN, prepare)


@pytest.mark.parametrize('summary', [False, True])
def test_existing_input_only_card_migrates_without_authorizing_or_running(tmp_path, summary):
    h = harness(tmp_path)
    old_input_card(h)
    before = h.store.load(RUN)
    old = cases(before)[0]
    snapshot = h.store.public_for_conversation('conv')[0] if summary else h.store.public(RUN)
    case = snapshot['node_states']['work']['recovery_case']
    assert case['pending']['kind'] == 'elevation'
    assert case['pending']['authority'] == [AUTH]
    assert case['pending']['input_requirements'] == old['plan']['options'][0]['needs']['user_input']
    assert case['card_migrations'][0]['original_pending'] == old['pending']
    assert case['plan'] == old['plan'] and not case['rounds']
    after = h.store.load(RUN)
    assert after.get('authorizations') == before.get('authorizations')
    assert after['node_states']['work']['messages'] == before['node_states']['work']['messages']
    assert not h.calls
    h.store.public(RUN)
    assert h.store.load(RUN) == after
    with pytest.raises(ValueError, match='选项不匹配'):
        h.store.mutate(RUN, lambda record: answer_case(record, case['case_id'], round=0, choice='input', answer='继续'))
    assert h.store.load(RUN) == after
    h.store.mutate(RUN, lambda record: answer_case(record, case['case_id'], round=0, choice='approve'))
    assert cases(h.store.load(RUN))[0]['plan'] == {} and not h.calls


@pytest.mark.parametrize('guard', ['denied', 'covered', 'revision_changed', 'terminated'])
def test_migration_does_not_reask_denied_covered_or_obsolete_requests(tmp_path, guard):
    h = harness(tmp_path)
    old_input_card(h)
    def prepare(record):
        if guard in {'denied', 'covered'}:
            record_authorization(record, kind='denial' if guard == 'denied' else 'elevation_answer',
                scope={**AUTH['scope'], 'operation_classes': ['external_send']}, quote='original decision',
                source_ref='test:original', host_verified=guard == 'covered')
        elif guard == 'revision_changed':
            record['node_states']['work']['input_revision'] = 'new-revision'
        else:
            record['status'] = 'terminated'
    h.store.mutate(RUN, prepare)
    before = h.store.load(RUN)
    h.store.public(RUN)
    assert h.store.load(RUN) == before and not h.calls
