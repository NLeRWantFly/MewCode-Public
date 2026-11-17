from mewcode.experts.node_ledger import ledger_for_run
from mewcode.kernel.escalation import decide_and_record, ensure_owners
from mewcode.kernel.calls import propose, transition
from mewcode.kernel.reconcile import file_probe, apply_probe
import pytest


def test_each_rung_is_used_once_and_model_progress_cannot_replenish_it():
    record = {'node_states': {'N03': {'handoff': {'summary': 'I changed the method'}}}}
    ledger_for_run(record).record('N03', 'blocker', {'kind': 'capability_missing'})
    decisions = [decide_and_record(record, 'N03', trigger='failure', made_progress=True, identity=str(i))
                 for i in range(6)]
    assert [item.level for item in decisions] == ['retry_same_method', 'alternate_route', 'replan_node', 'human', 'human', 'human']
    assert record['node_states']['N03']['blocker_resolution'][0]['owner'] == 'human'
    assert record['node_states']['N03']['blocker_resolution'][0]['remaining_opportunities'] == 0


def test_all_open_blockers_have_host_resolution_owner():
    record = {}
    ledger_for_run(record).record('N03', 'blocker', {'kind': 'safety_review_format'})
    ensure_owners(record)
    assert record['node_states']['N03']['blocker_resolution'][0]['owner'] == 'reviewer'


def test_productive_unfinished_batches_do_not_consume_failure_recovery():
    record = {'node_states': {'N03': {}}}
    ledger = ledger_for_run(record)
    ledger.record('N03', 'blocker', {'kind': 'internal_work'})
    for index in range(8):
        record['node_states']['N03']['handoff'] = {'inline_outputs': [
            {'name': f'page-{index}', 'content': f'parsed-content-{index}'}]}
        choice = decide_and_record(record, 'N03', trigger='internal_work', identity=str(index))
        assert choice.action == 'progress_continue' and choice.level is None
    # Repeating a saved result is not new progress.
    stalled = [decide_and_record(record, 'N03', trigger='internal_work', identity=f'stall-{index}')
               for index in range(4)]
    assert [item.level for item in stalled] == [
        'retry_same_method', 'alternate_route', 'replan_node', 'human']


@pytest.mark.parametrize('blocker', ['environment_fault', 'safety_review_unavailable',
                                   'permission', 'unknown_effect'])
def test_new_content_cannot_clear_unrelated_blocker(blocker):
    record = {'node_states': {'N03': {}}}
    ledger = ledger_for_run(record)
    ledger.record('N03', 'blocker', {'kind': 'internal_work'})
    ledger.record('N03', 'blocker', {'kind': blocker})
    record['node_states']['N03']['handoff'] = {'inline_outputs': [{'name': 'page', 'content': 'new data'}]}
    choice = decide_and_record(record, 'N03', trigger='internal_work', identity='new-page')
    assert choice.level is not None
    assert any(f.payload['kind'] == blocker for f in ledger.view('N03').active_blockers)


def test_old_evidence_cannot_buy_progress_again_after_regression():
    record = {'node_states': {'N03': {}}, 'review_queues': {'N03': {'records': {'item': {'state': 'complete'}}}}}
    first = decide_and_record(record, 'N03', trigger='failure', identity='first')
    assert first.level is None
    record['review_queues'] = {}
    decide_and_record(record, 'N03', trigger='failure', identity='second')
    record['review_queues'] = {'N03': {'records': {'item': {'state': 'complete'}}}}
    replayed = decide_and_record(record, 'N03', trigger='failure', identity='third')
    assert replayed.level == 'alternate_route'


def test_unknown_effect_gets_one_probe_attempt_then_human():
    record = {}
    ledger_for_run(record).record('N03', 'blocker', {'kind': 'unknown_effect'})
    first = decide_and_record(record, 'N03', trigger='interrupted', identity='first')
    second = decide_and_record(record, 'N03', trigger='interrupted', identity='second')
    assert first.assistance_kind == 'effect_verification'
    assert second.action == 'ask_human'


@pytest.mark.parametrize('boundary', ['none', 'started', 'receipt', 'helper', 'epoch',
                                    'permission', 'business', 'different_request', 'completed'])
def test_only_an_unstarted_matching_assistance_reservation_can_resume(boundary):
    from mewcode.kernel.escalation import unstarted_assistance_decision, run_assist_count
    request = {'request_id': 'explicit-request', 'node_id': 'work', 'epoch': 0,
               'status': 'pending', 'kind': 'diagnosis'}
    record = {'node_states': {'work': {'assistance_request': request}}}
    facts = ledger_for_run(record)
    facts.record('work', 'assistance', {**request, 'status': 'requested'}, source='node')
    reserved = decide_and_record(record, 'work', trigger='assistance_requested', identity='reserved')
    stage = record['node_states']['work']
    if boundary == 'started':
        stage['assistance_checkpoint'] = {'request_id': request['request_id']}
    elif boundary == 'receipt':
        stage['assistance_receipts'] = [{'request_id': request['request_id']}]
    elif boundary == 'helper':
        record['node_states']['work__assist_' + request['request_id'][:12]] = {'status': 'failed'}
    elif boundary == 'epoch':
        request['epoch'] = 1
    elif boundary in {'permission', 'business'}:
        facts.record('work', 'blocker', {'kind': 'permission' if boundary == 'permission' else 'contract_conflict'})
    elif boundary == 'different_request':
        request['request_id'] = 'new-request'
    elif boundary == 'completed':
        request['status'] = 'completed'
    assert unstarted_assistance_decision(record, 'work') == (reserved if boundary == 'none' else None)
    assert run_assist_count(record) == 1


def test_readonly_probe_confirms_only_matching_workspace_effect(tmp_path):
    record = {}
    row = propose(record, 'N03', 'v1', {'tool': 'WriteFile', 'arguments': {'file_path': 'output.txt', 'content': 'done\n'}})
    transition(row, 'reviewed')
    transition(row, 'dispatched', evidence={'executor_accepted': True})
    transition(row, 'unknown')
    unresolved = file_probe(row, tmp_path)
    assert unresolved['state'] == 'unknown'
    (tmp_path / 'output.txt').write_text('done\n', encoding='utf-8')
    probe = file_probe(row, tmp_path)
    assert probe['state'] == 'succeeded'
    apply_probe(record, row['call_id'], probe)
    assert row['state'] == 'succeeded'
