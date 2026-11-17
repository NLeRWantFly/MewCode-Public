"""T46: a host review failure cannot turn an unlaunched command into an effect."""
from mewcode.agent import Agent
from mewcode.experts.command_receipts import observe_requests, request_receipts, execution_observation
from mewcode.experts.recovery_state import blockers


def test_pre_dispatch_guard_stamps_non_execution_without_exit_code():
    result = Agent._operation_blocked_result('AUTO_REVIEW_INCONCLUSIVE: malformed review')
    host = result.host_observation
    assert host['phase'] == 'before_dispatch'
    assert host['command_execution']['command_started'] is False
    assert host['command_execution']['exit_code'] is None
    row = request_receipts('N03', 'revision', [{'command': 'fixture-command'}])[0]
    record = {'name': 'Bash', 'args': row['request'], 'status': 'outcome_unknown',
              'host_observation': host, 'output': result.output}
    actual = observe_requests([row], [record])[0]
    assert actual['status'] == 'not_started'
    assert all(row['kind'] != 'unknown_effect' for row in blockers({'tools': {'call': record}}))
    assert request_receipts('N03', 'revision', [row['request']])[0]['request_id'] == actual['request_id']


def test_no_dispatch_receipt_is_not_unknown_but_a_started_timeout_is():
    row = request_receipts('N03', 'revision', [{'command': 'fixture-command'}])[0]
    assert observe_requests([row], [])[0]['status'] == 'not_started'
    record = {'name': 'Bash', 'args': row['request'], 'status': 'failed', 'host_observation': {
        'command_execution': execution_observation('timed_out')}}
    assert observe_requests([row], [record])[0]['status'] == 'outcome_unknown'


def test_later_refusal_never_erases_an_earlier_started_unknown_effect():
    row = request_receipts('N03', 'revision', [{'command': 'fixture-command'}])[0]
    records = [{'name': 'Bash', 'args': row['request'], 'status': 'failed', 'host_observation': {
        'command_execution': execution_observation('timed_out')}},
        {'name': 'Bash', 'args': row['request'], 'status': 'failed', 'host_observation': {
            'operation_blocked': True}}]
    assert observe_requests([row], records)[0]['status'] == 'outcome_unknown'
