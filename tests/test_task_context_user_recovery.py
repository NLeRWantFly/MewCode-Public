import copy
import json

import pytest

from mewcode.orchestration.task_context import ContextBudgetExceeded, build_task_context
from tests.test_autonomous_context import attempt, make_ledger


def recovered_value():
    value = make_ledger()
    value['human_decisions'] = {
        'current': {'id': 'current', 'source': 'user', 'spec_version': 2, 'node_ids': ['work'],
                    'payload': {'action': 'retry', 'reason': 'Read scripts/repair_reference/README; reuse accepted intake.'}},
        'unrelated': {'id': 'unrelated', 'source': 'user', 'spec_version': 2, 'node_ids': ['deliver'],
                      'payload': {'action': 'retry', 'reason': 'UNRELATED'}}}
    value['timeline'] = [
        {'kind': 'retry_authorized', 'decision_id': 'current', 'contracts': ['work']},
        {'kind': 'retry_authorized', 'decision_id': 'unrelated', 'contracts': ['deliver']}]
    return value


def test_ordinary_retry_keeps_complete_scoped_user_reason_without_mutating_ledger():
    value = recovered_value()
    before = copy.deepcopy(value)
    context = build_task_context(value, 'work')
    assert context['objective']['user_recovery'] == value['human_decisions']['current']
    assert 'UNRELATED' not in json.dumps(context)
    assert value == before
    assert context == build_task_context(value, 'work')


@pytest.mark.parametrize('field,new', [('source', 'model'), ('spec_version', 1), ('node_ids', ['deliver'])])
def test_stale_or_non_user_or_wrong_node_decisions_are_not_promoted(field, new):
    value = recovered_value()
    value['human_decisions']['current'][field] = new
    assert 'user_recovery' not in build_task_context(value, 'work')['objective']


def test_required_recovery_reason_cannot_be_silently_trimmed_to_fit():
    value = recovered_value()
    value['human_decisions']['current']['payload']['reason'] = '不可丢弃的恢复依据' * 1000
    with pytest.raises(ContextBudgetExceeded):
        build_task_context(value, 'work', char_budget=4000)


def failed_value():
    value = make_ledger()
    row = attempt('work', 1, 'rejected')
    row['report']['artifacts'] = [{'kind': 'inline', 'text': 'Old delivery'}]
    row['report']['checks'] = [
        {'id': 'keep', 'passed': True, 'evidence': {'detail': 'Already accepted'}},
        {'id': 'repair', 'passed': False, 'evidence': {'issues': [
            {'id': 'ENV-01', 'description': 'Reject oversized single pages before POST.'}]}}]
    value['attempts'][row['id']] = row
    return value


def test_inline_retry_keeps_complete_failed_checks_when_optional_evidence_is_dropped():
    value = failed_value()
    before = copy.deepcopy(value)
    context = build_task_context(value, 'work', evidence=[{'huge': 'x' * 50000}])
    failure = context['objective']['prior_failed_checks']
    assert failure['attempt_id'] == 'work-1'
    assert failure['checks'] == value['attempts']['work-1']['report']['checks'][1:]
    assert context['reported_evidence']['items'] == []
    assert value == before
    assert context == build_task_context(value, 'work', evidence=[{'huge': 'x' * 50000}])
    failure['checks'][0]['evidence']['issues'].clear()
    assert value == before


def test_required_failed_checks_cannot_be_silently_trimmed_to_fit():
    value = failed_value()
    value['attempts']['work-1']['report']['checks'][1]['evidence']['issues'][0]['acceptance_test'] = 'x' * 50000
    with pytest.raises(ContextBudgetExceeded):
        build_task_context(value, 'work')


def test_optional_failure_narration_is_replaced_by_an_explicit_full_report_reference():
    value = failed_value()
    original = value['attempts']['work-1']['report']['checks'][1]['evidence']
    original['detail'] = 'x' * 50000
    context = build_task_context(value, 'work')
    evidence = context['objective']['prior_failed_checks']['checks'][0]['evidence']
    assert evidence['issues'] == original['issues']
    assert 'previous:work' in evidence['details']
    assert original['detail'] == 'x' * 50000


@pytest.mark.parametrize('field,new', [('contract_id', 'deliver'), ('spec_version', 1), ('superseded', True)])
def test_failed_checks_from_unrelated_stale_or_superseded_attempts_are_excluded(field, new):
    value = failed_value()
    value['attempts']['work-1'][field] = new
    assert 'prior_failed_checks' not in build_task_context(value, 'work')['objective']


def test_later_accepted_attempt_does_not_resurrect_old_failed_checks():
    value = failed_value()
    accepted = attempt('work', 2, 'accepted')
    accepted['report'] = {'passed': True, 'checks': []}
    value['attempts'][accepted['id']] = accepted
    assert 'prior_failed_checks' not in build_task_context(value, 'work')['objective']


def test_compact_context_budget_counts_actual_json_and_preserves_all_required_fields():
    value = failed_value()
    value.update({k: v for k, v in recovered_value().items() if k in {'human_decisions', 'timeline'}})
    standard = build_task_context(value, 'work')
    compact = build_task_context(value, 'work', compact=True)
    encoded = json.dumps(compact, ensure_ascii=False, separators=(',', ':'))
    assert compact['budget']['used'] == len(encoded)
    assert compact['objective'] == standard['objective']
    assert compact['budget']['used'] < standard['budget']['used']
    bounded = build_task_context(value, 'work', compact=True, char_budget=len(encoded))
    assert len(json.dumps(bounded, ensure_ascii=False, separators=(',', ':'))) <= len(encoded)
    assert bounded['objective'] == standard['objective']
