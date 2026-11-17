import copy

import pytest

from mewcode.kernel.calls import propose, transition
from mewcode.kernel.repairs import adjudicate, extend_manifest
from mewcode.experts.node_ledger import ledger_for_run


def unknown():
    record = {'node_states': {'N03': {}}}
    invocation = {'tool': 'WriteFile', 'arguments': {'file_path': 'output.txt', 'content': 'fixture'}}
    row = propose(record, 'N03', 'revision', invocation)
    transition(row, 'reviewed')
    transition(row, 'dispatched', evidence={'executor_accepted': True})
    transition(row, 'unknown')
    ledger_for_run(record).record('N03', 'blocker', {'kind': 'unknown_effect', 'ref': row['call_id']}, identity='unknown')
    return record, row, invocation


@pytest.mark.parametrize('disposition', ['assume_not_applied', 'assume_applied'])
def test_explicit_disposition_preserves_unknown_history_without_claiming_verification(disposition):
    record, original, invocation = unknown()
    history = copy.deepcopy(original['history'])
    result = adjudicate(record, node_id='N03', call_id=original['call_id'], disposition=disposition, source_ref='user-fixture')
    assert not result['effect_verified']
    assert original['state'] == 'unknown' and original['history'] == history
    assert not ledger_for_run(record).view('N03').active_blockers
    proposed = propose(record, 'N03', 'revision', invocation)
    if disposition == 'assume_not_applied':
        assert proposed['state'] == 'proposed' and proposed['call_id'] != original['call_id']
        assert propose(record, 'N03', 'revision', invocation)['call_id'] == proposed['call_id']
    else:
        assert proposed['call_id'] == original['call_id']
    assert adjudicate(record, node_id='N03', call_id=original['call_id'], disposition=disposition,
                      source_ref='user-fixture')['status'] == 'no_effect'


def test_authorization_extension_is_exact_scope_and_same_extension_has_no_effect(tmp_path):
    record = {'node_states': {'N03': {}}, 'authorization_manifest': {
        'schema_version': 2, 'workspace': str(tmp_path), 'nodes': {}, 'approved': True}}
    result = extend_manifest(record, node_id='N03', capabilities={'write_paths': ['outputs']}, source_ref='user-fixture')
    assert result['status'] == 'authorization_extended'
    assert record['authorization_manifest']['nodes']['N03']['write_paths'] == ['outputs']
    assert extend_manifest(record, node_id='N03', capabilities={'write_paths': ['outputs']},
                           source_ref='user-fixture')['status'] == 'no_effect'
