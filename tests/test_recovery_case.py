import copy
from dataclasses import asdict
import pytest
from mewcode.experts.recovery_case import (
    open_case, apply_event, get_case, cases, CaseRound, supersede_cases,
    needs_case_card_migration, restore_case_cards,
)
from mewcode.experts.node_ledger import ledger_for_run, bump_run_epoch
from mewcode.kernel.escalation import automatic_continuations, context_for, decide
from mewcode.experts.case_runtime import answer_case
from mewcode.experts.problem_dossier import incident_report
from mewcode.orchestration.maintenance import _preview_run_actions
from mewcode.orchestration.maintenance_revision import apply_run_actions
from tests.test_graph_lint import _spec, _node


def record():
    return {'node_states': {'work': {'input_revision': 'rev', 'status': 'paused'}}, 'node_ledgers': {}}


def test_case_events_replay_boundaries_new_issue_does_not_erase_history():
    r = record()
    c = open_case(r, 'work')
    event = {'kind': 'finish', 'identity': 'round:1', 'round': asdict(CaseRound(1, 'a', 'method-a', 'transient', verification={'result': 'resolved'}))}
    finished = apply_event(r, c['case_id'], event)
    assert apply_event(r, c['case_id'], event) == finished
    for n in range(2):
        c = open_case(r, 'work')
        apply_event(r, c['case_id'], {**event, 'identity': f'new:{n}'})
    assert len(cases(r)) == 3
    new = open_case(r, 'work')
    assert new['status'] == 'open' and len(cases(r)) == 4


def test_epoch_and_input_revision_supersede_old_open_cases_keep_rounds():
    r = record()
    c = open_case(r, 'work')
    bump_run_epoch(r, 'work', reason='maintenance_applied', source_ref='confirmed:fix')
    assert get_case(r, c['case_id'])['status'] == 'superseded'
    assert r['node_states']['work']['recovery_case']['status'] == 'superseded'
    new = open_case(r, 'work')
    r['node_states']['work']['input_revision'] = 'different'
    supersede_cases(r, 'work')
    assert get_case(r, new['case_id'])['status'] == 'superseded'


def waiting_case(r):
    c = open_case(r, 'work')
    return apply_event(r, c['case_id'], {'kind': 'wait', 'identity': 'wait', 'pending': {
        'kind': 'elevation', 'authority': [], 'options': [{'value': 'approve'}, {'value': 'alternative'}, {'value': 'close'}]}})


def test_stale_or_unavailable_answers_are_atomic():
    r = record()
    c = waiting_case(r)
    original = copy.deepcopy(r)
    with pytest.raises(ValueError, match='备选'):
        answer_case(r, c['case_id'], round=0, choice='alternative')
    assert r == original
    r['node_states']['work']['input_revision'] = 'new'
    with pytest.raises(ValueError, match='代际'):
        answer_case(r, c['case_id'], round=0, choice='approve')


def test_progress_never_resets_six_auto_continuations_without_clearance():
    r = record()
    r['recovery_case_enabled'] = True
    ledger = ledger_for_run(r)
    for n in range(6):
        ledger.record('work', 'progress', {'made_progress': True}, identity=f'progress:{n}')
        ledger.record('work', 'maintenance', {'recovery_action': 'progress_continue'}, identity=f'continue:{n}')
    assert automatic_continuations(ledger.view('work')) == 6
    assert decide(context_for(r, 'work', trigger='internal_work', made_progress=True)).action == 'open_case'
    ledger.record('work', 'clearance', {'blocker_fact_id': 'cleared', 'kind': 'host'}, identity='fresh-clearance')
    assert automatic_continuations(ledger.view('work')) == 0


def test_phase_history_only_reports_self_help_when_host_recorded_it():
    r = record()
    assert open_case(r, 'work')['phases'] == ['发现问题']
    r = record()
    ledger_for_run(r).record('work', 'maintenance', {'recovery_action': 'self_diagnose'})
    assert open_case(r, 'work')['phases'] == ['发现问题', '节点自救']


def test_fix_case_actions_require_confirmation_for_concrete_direction_not_quota():
    r = record()
    c = waiting_case(r)
    spec = _spec([_node('work')])
    actions = _preview_run_actions(spec.as_dict(), 'fix', [{'action': 'extend_rounds', 'node_id': 'work', 'case_id': c['case_id'], 'direction': '核对已有环境'}], r)
    assert actions[0]['requires_confirmation']
    unchanged = apply_run_actions(r, actions, [], maintenance_id='fix:1')['snapshot']
    assert get_case(unchanged, c['case_id'])['status'] == 'awaiting_user'
    result = apply_run_actions(r, actions, [actions[0]['id']], maintenance_id='fix:1')['snapshot']
    assert get_case(result, c['case_id'])['continuation_direction'] == '核对已有环境'
    assert not result.get('authorizations', {}).get('entries')


def test_conditional_input_requirements_reopen_the_same_card_until_complete():
    r = record()
    r.update({'status': 'paused', 'active_node_ids': ['work'], 'run_inputs': {},
        'locked_flow': {'nodes': [{'node_id': 'work'}], 'edges': [], 'meta': {
            'run_input_contract': [
                {'name': 'enable', 'required': True, 'type': 'text'},
                {'name': 'details', 'required': False, 'type': 'text', 'collect_at': 'work',
                 'required_when': {'input': 'enable', 'equals': 'true'}},
            ]}},
    })
    case = open_case(r, 'work')
    report = incident_report(r, case, conclusion='请补充当前字段。', root_cause='input_defect')
    case = apply_event(r, case['case_id'], {'kind': 'report', 'identity': 'input-report', 'report': report})
    assert [item['name'] for item in case['pending']['input_definitions']] == ['enable']

    case = answer_case(r, case['case_id'], round=0, choice='input', answer='{"enable":"true"}')
    assert case['status'] == 'awaiting_user'
    assert [item['name'] for item in case['pending']['input_definitions']] == ['details']
    assert r['run_inputs'] == {'enable': 'true'}

    case = answer_case(r, case['case_id'], round=0, choice='input', answer='{"details":"ready"}')
    assert case['status'] == 'open' and case['pending'] == {}
    assert r['run_inputs'] == {'enable': 'true', 'details': 'ready'}


def test_internal_input_defect_migrates_once_without_reopening_empty_question():
    r = record()
    r.update({'status': 'paused', 'active_node_ids': [], 'run_inputs': {},
        'locked_flow': {'nodes': [{'node_id': 'work'}], 'edges': [], 'meta': {}},
    })
    case = open_case(r, 'work')
    report = incident_report(r, case, conclusion='当前没有可提交字段。', root_cause='input_defect')
    case = apply_event(r, case['case_id'], {'kind': 'report', 'identity': 'incident-report', 'report': report})
    assert report['card_projection']['version'] == 1
    assert needs_case_card_migration(r)

    case['report'].pop('card_projection')
    r['node_states']['work']['recovery_case'] = copy.deepcopy(case)
    assert needs_case_card_migration(r)
    rounds = copy.deepcopy(case['rounds'])
    restore_case_cards(r)
    assert not needs_case_card_migration(r)
    migrated = get_case(r, case['case_id'])
    assert migrated['rounds'] == rounds
    assert migrated['handling']['owner'] == 'system'
    assert migrated['handling']['handling_state'] == 'blocked'
    assert migrated['pending'] == {}
    before = copy.deepcopy(r)
    restore_case_cards(r)
    assert r == before

    r['run_inputs'] = {'changed': 'value'}
    assert not needs_case_card_migration(r)
