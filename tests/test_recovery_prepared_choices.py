from __future__ import annotations

import copy

import pytest

from mewcode.experts.case_runtime import answer_case
from mewcode.experts.problem_dossier import incident_report
from mewcode.experts.recovery_case import apply_event, open_case, save_case


def _record():
    return {
        'node_states': {'work': {'input_revision': 'rev', 'status': 'paused'}},
        'node_ledgers': {},
    }


def _option(option_id: str):
    return {
        'option_id': option_id,
        'method': f'执行 {option_id}',
        'strategy': {'kind': 'probe', 'tools': ['ProbeCapabilities'], 'target': option_id},
        'needs': {'capabilities': [], 'authority': [], 'user_input': []},
        'risk': 'low', 'reversible': True, 'changes_acceptance': False,
        'verification': {
            'probe': '核验宿主回执', 'expected_evidence': '宿主证据',
            'tool': 'ProbeCapabilities', 'arguments': {},
        },
    }


def _plan(case, *options):
    return {
        'case_id': case['case_id'], 'round': len(case['rounds']) + 1,
        'root_cause': {'class': 'contract_defect', 'summary': '宿主合同阻塞',
                       'evidence_refs': ['dossier:' + case['case_id']], 'confidence': 'high'},
        'options': list(options), 'recommended_option_id': options[0]['option_id'],
        'not_recommended': [],
    }


def test_reports_keep_model_options_for_fundamental_and_routed_causes():
    record = _record()
    case = open_case(record, 'work')
    plan = _plan(case, _option('first'), _option('second'))
    case['plan'] = copy.deepcopy(plan)
    save_case(record, case)

    for root in ('input_defect', 'contract_defect', 'business_decision', 'capability_absent'):
        report = incident_report(record, case, conclusion='需要选择恢复方式。', root_cause=root)
        assert [row['value'] for row in report['recovery_options']] == ['first', 'second']
        assert {row['value'] for row in report['options']} >= {'first', 'second'}
        assert report['recovery_plan']['options'][1]['option_id'] == 'second'


def test_direct_recovery_option_answer_preserves_and_selects_saved_plan():
    record = _record()
    case = open_case(record, 'work')
    plan = _plan(case, _option('first'), _option('second'))
    case['plan'] = copy.deepcopy(plan)
    save_case(record, case)
    report = incident_report(record, case, conclusion='需要选择恢复方式。', root_cause='contract_defect')
    case = apply_event(record, case['case_id'], {'kind': 'report', 'identity': 'report', 'report': report})

    answered = answer_case(record, case['case_id'], round=0, choice='second')
    assert answered['status'] == 'open'
    assert answered['plan']['recommended_option_id'] == 'second'
    assert answered['plan']['round'] == 1
    assert answered['selected_recovery_option']['option_id'] == 'second'
    assert answered['pending'] == {}
    assert not answered.get('closed_by_user')


def _prepared_pending(*, name='next', choices=None, input_definitions=None):
    return {
        'kind': 'input', 'round': 0, 'question': '请选择',
        'input_definitions': input_definitions or [],
        'questions': copy.deepcopy(input_definitions or []),
        'choice_bundle': {
            'id': 'bundle-1', 'version': 'version-1', 'status': 'ready',
            'questions': [{'name': name, 'choices': choices or []}],
        },
        'options': [{'value': choice['id']} for choice in choices or []]
                   + [{'value': 'close'}],
    }


def test_prepared_value_binds_typed_list_and_keeps_custom_note_context_only():
    record = _record()
    record.update({
        'run_inputs': {},
        'locked_flow': {'nodes': [{'node_id': 'work'}], 'edges': [], 'meta': {
            'run_input_contract': [{'name': 'sources', 'type': 'list', 'required': True,
                                    'collect_at': 'work'}],
        }},
    })
    case = open_case(record, 'work')
    choices = [
        {'id': 'sources-candidate', 'kind': 'value', 'value': ['a.pdf', 'b.pdf'],
         'requires_input': False},
        {'id': 'manual', 'kind': 'action', 'action': 'manual', 'requires_input': True},
    ]
    pending = _prepared_pending(name='sources', choices=choices,
                                input_definitions=[{'name': 'sources', 'type': 'list',
                                                    'required': True, 'collect_at': 'work'}])
    case = apply_event(record, case['case_id'], {'kind': 'wait', 'identity': 'wait', 'pending': pending})

    answered = answer_case(record, case['case_id'], round=0, choice='sources-candidate',
                           bundle_id='bundle-1', version='version-1', note='仅作背景说明')
    assert answered['status'] == 'open'
    assert record['run_inputs'] == {'sources': ['a.pdf', 'b.pdf']}
    assert not record.get('authorizations', {}).get('entries')
    assert next(iter(record['case_inputs'].values()))['context_note'] == '仅作背景说明'


def test_prepared_actions_are_saved_for_dispatch_without_grant_or_completion():
    record = _record()
    case = open_case(record, 'work')
    choices = [
        {'id': 'manual', 'kind': 'action', 'action': 'manual', 'requires_input': True},
        {'id': 'defer', 'kind': 'action', 'action': 'defer', 'requires_input': False},
    ]
    pending = _prepared_pending(choices=choices)
    case = apply_event(record, case['case_id'], {'kind': 'wait', 'identity': 'wait', 'pending': pending})
    before = copy.deepcopy(record.get('authorizations', {}).get('entries', []))

    answered = answer_case(record, case['case_id'], round=0, choice='manual',
                           bundle_id='bundle-1', version='version-1',
                           choice_inputs={'manual': '走人工维护路径'}, note='不要把这条当参数')
    assert answered['status'] == 'open'
    assert answered['investigation_requested']['instruction'] == '走人工维护路径'
    assert answered['pending'] == {}
    assert answered['plan'] == {}
    assert 'pending_choice_action' not in answered
    assert not answered.get('closed_by_user')
    assert record.get('authorizations', {}).get('entries', []) == before
    assert record['node_states']['work']['status'] == 'pending'
    assert 'pending_choice_action' not in record['node_states']['work']


def test_defer_keeps_card_but_close_stops_it():
    record = _record()
    case = open_case(record, 'work')
    pending = {'kind': 'incident', 'round': 0, 'question': '稍后处理',
               'options': [{'value': 'defer'}, {'value': 'close'}]}
    case = apply_event(record, case['case_id'], {'kind': 'wait', 'identity': 'wait', 'pending': pending})
    deferred = answer_case(record, case['case_id'], round=0, choice='defer')
    assert deferred['status'] == 'awaiting_user'
    assert deferred['pending'] and not deferred.get('closed_by_user')
    stopped = answer_case(record, case['case_id'], round=0, choice='close')
    assert stopped['status'] == 'reported'
    assert stopped['pending'] == {}
    assert stopped['closed_by_user'] is True


def test_stale_prepared_bundle_is_rejected_atomically():
    record = _record()
    case = open_case(record, 'work')
    choices = [{'id': 'defer', 'kind': 'action', 'action': 'defer', 'requires_input': False}]
    pending = _prepared_pending(choices=choices)
    case = apply_event(record, case['case_id'], {'kind': 'wait', 'identity': 'wait', 'pending': pending})
    before = copy.deepcopy(record)
    with pytest.raises(ValueError, match='版本'):
        answer_case(record, case['case_id'], round=0, choice='defer',
                    bundle_id='bundle-1', version='stale', choice_inputs={})
    assert record == before


def test_legacy_close_control_is_not_reinterpreted_as_manual_value():
    record = _record()
    case = open_case(record, 'work')
    pending = _prepared_pending(
        choices=[
            {'id': 'manual', 'kind': 'action', 'action': 'manual', 'requires_input': True},
        ]
    )
    pending['options'].append({'value': 'close'})
    case = apply_event(record, case['case_id'], {'kind': 'wait', 'identity': 'wait', 'pending': pending})

    stopped = answer_case(
        record, case['case_id'], round=0, choice='close',
        bundle_id='bundle-1', version='version-1',
    )
    assert stopped['status'] == 'reported'
    assert stopped['closed_by_user'] is True
    assert stopped['pending'] == {}
