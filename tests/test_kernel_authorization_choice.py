"""A manifest gap has a concrete answer, and answering it records an authorization."""
import copy

from mewcode.experts.authorization_ledger import covers, record_authorization
from mewcode.experts.node_ledger import ledger_for_run
from mewcode.kernel.escalation import (apply_authorization_choice, authorization_choice_options,
                                       authorization_request_for)
from mewcode.kernel.capabilities import compile_manifest, ManifestGuard
from mewcode.experts.flow import ExpertFlowSpec

WRITE_CALL = {'tool': 'WriteFile', 'arguments': {'file_path': 'kb/00_inventory/README.md',
                                                'content': '# 四书清点说明\n'}}
BASH_CALL = {'tool': 'Bash', 'arguments': {
    'command': 'cd "C:/proj" && ls -la && ls -la kb'}}


class Store:
    """Minimal run store: load/mutate over one dict, matching the real protocol."""

    def __init__(self, record):
        self.record = record

    def load(self, _run_id):
        return copy.deepcopy(self.record)

    def mutate(self, _run_id, apply, durable=False):
        value = copy.deepcopy(self.record)
        apply(value)
        self.record = value
        return copy.deepcopy(value)


def blocker(record, node_id):
    for fact in ledger_for_run(copy.deepcopy(record)).view(node_id).active_blockers:
        if fact.payload.get('kind') in {'missing_authorization', 'capability_missing'}:
            return fact
    return None


def flow():
    return ExpertFlowSpec.from_value({'flow_id': 'f', 'finalizer_node_id': 'N01', 'edges': [],
        'nodes': [{'node_id': 'N01', 'role': 'executor'}]})


def test_write_gap_becomes_an_exact_file_request(tmp_path):
    request = authorization_request_for(type('B', (), {'payload': {'requested_call': WRITE_CALL}})(),
                                        workspace=str(tmp_path))
    assert request == {'operation_class': 'write_outside_workspace',
                       'scope': {'paths': [(tmp_path / 'kb' / '00_inventory' / 'README.md').as_posix()]}}
    assert authorization_request_for(type('B', (), {'payload': {'requested_call': {
        'tool': 'WriteFile', 'arguments': {'file_path': '../escape.md'}}}})(),
        workspace=str(tmp_path)) is None


def test_bash_gap_becomes_a_prefix_request_without_redirect():
    assert authorization_request_for(type('B', (), {'payload': {'requested_call': BASH_CALL}})()) is None
    request = authorization_request_for(type('B', (), {'payload': {'requested_call': {
        'tool': 'Bash', 'arguments': {'command': 'python check.py'}}}})())
    assert request is not None and request['operation_class'] == 'run_command_prefix'
    assert request['scope']['command_prefix'][:1]
    assert authorization_request_for(type('B', (), {'payload': {'requested_call': {
        'tool': 'Bash', 'arguments': {'command': 'echo hi > out.txt'}}}})()) is None


def test_an_uncovered_gap_is_offered_as_an_explicit_choice(tmp_path):
    record = {'authorization_manifest': compile_manifest(flow(), workspace=str(tmp_path), approved=True)}
    store = Store(record)
    ManifestGuard(store, 'run', 'N01').check('WriteFile', WRITE_CALL['arguments'])
    fact = blocker(store.record, 'N01')
    assert fact is not None and fact.payload['kind'] == 'missing_authorization'

    options, pending = authorization_choice_options(store.record, 'N01')
    assert len(pending) == 1
    assert options[0]['value'] == 'allow:' + fact.fact_id
    assert 'WriteFile' in options[0]['label']
    assert options[-1]['value'] == 'deny_scope'


def test_choosing_allow_records_the_authorization_and_clears_the_gap(tmp_path):
    record = {'authorization_manifest': compile_manifest(flow(), workspace=str(tmp_path), approved=True)}
    store = Store(record)
    ManifestGuard(store, 'run', 'N01').check('WriteFile', WRITE_CALL['arguments'])
    fact = blocker(store.record, 'N01')
    _, pending = authorization_choice_options(store.record, 'N01')

    apply_authorization_choice(store.record, node_id='N01', blocked=pending,
                              choice='allow:' + fact.fact_id, source_ref='req-1')
    request = pending[0][1]
    assert covers(store.record, request).status == 'covered'
    assert blocker(store.record, 'N01') is None
    assert ManifestGuard(store, 'run', 'N01').check('WriteFile', WRITE_CALL['arguments']) is None

    # The recorded grant is scoped: a sibling write outside it is still uncovered.
    wider = {**request, 'scope': {'paths': [str(tmp_path / 'kb' / '01_other')]}}
    assert covers(store.record, wider).status != 'covered'


def test_the_same_gap_is_not_offered_twice_once_covered(tmp_path):
    record = {'authorization_manifest': compile_manifest(flow(), workspace=str(tmp_path), approved=True)}
    store = Store(record)
    assert ManifestGuard(store, 'run', 'N01').check('WriteFile', WRITE_CALL['arguments'])
    fact = blocker(store.record, 'N01')
    _, pending = authorization_choice_options(store.record, 'N01')
    apply_authorization_choice(store.record, node_id='N01', blocked=pending,
                              choice='allow:' + fact.fact_id, source_ref='req-1')

    assert ManifestGuard(store, 'run', 'N01').check('WriteFile', WRITE_CALL['arguments']) is None
    reopened = blocker(store.record, 'N01')
    assert reopened is None
    options, _ = authorization_choice_options(store.record, 'N01')
    assert options == []


def test_deny_records_no_authorization(tmp_path):
    record = {'authorization_manifest': compile_manifest(flow(), workspace=str(tmp_path), approved=True)}
    store = Store(record)
    ManifestGuard(store, 'run', 'N01').check('WriteFile', WRITE_CALL['arguments'])
    _, pending = authorization_choice_options(store.record, 'N01')
    before = copy.deepcopy(store.record.get('authorizations'))
    apply_authorization_choice(store.record, node_id='N01', blocked=pending,
                              choice='deny_scope', source_ref='req-2')
    assert store.record.get('authorizations') == before
    assert blocker(store.record, 'N01') is not None


def test_a_covered_scope_is_bound_without_a_user_question(tmp_path):
    """The defect: the model asked for a normal workspace write and got a user question."""
    record = {'authorization_manifest': compile_manifest(flow(), workspace=str(tmp_path), approved=True)}
    record_authorization(record, kind='gate_answer',
        scope={'operation_classes': ['write_outside_workspace'],
               'paths': [(tmp_path / 'kb').as_posix()]},
        quote='允许写 kb', source_ref='gate:1', host_verified=True)
    store = Store(record)
    assert ManifestGuard(store, 'run', 'N01').check('WriteFile', WRITE_CALL['arguments']) is None
    fact = blocker(store.record, 'N01')
    assert fact is None
    options, pending = authorization_choice_options(store.record, 'N01')
    assert options == [] and pending == []
