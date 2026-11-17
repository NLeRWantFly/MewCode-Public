import pytest

from mewcode.experts.flow import ExpertFlowSpec
from mewcode.kernel.capabilities import call_gap, compile_manifest, declaration, resolver_diagnostics
from mewcode.orchestration.convert import from_expert_flow, to_expert_flow


def flow():
    return ExpertFlowSpec.from_value({'flow_id': 'fixture', 'label': 'fixture', 'finalizer_node_id': 'N03',
        'nodes': [{'node_id': 'N03', 'role': 'executor', 'capabilities': {
            'command_execution': True, 'command_prefixes': [['python', 'check.py']],
            'write_paths': ['outputs'], 'network_hosts': ['example.test'],
            'possible_blockers': ['capability_missing'], 'resolvers': {'capability_missing': 'missing'}}}],
        'edges': []})


def test_manifest_roundtrip_and_exact_scope(tmp_path):
    original = flow()
    restored = to_expert_flow(from_expert_flow(original))
    assert restored.nodes[0].capabilities == original.nodes[0].capabilities
    manifest = compile_manifest(restored, workspace=str(tmp_path), approved=True)
    assert call_gap(manifest, 'N03', 'WriteFile', {'file_path': 'outputs/ok.txt'}) is None
    assert call_gap(manifest, 'N03', 'WriteFile', {'file_path': 'outputs/../outside.txt'})
    assert call_gap(manifest, 'N03', 'WebFetch', {'url': 'https://example.test/page'}) is None
    assert call_gap(manifest, 'N03', 'WebFetch', {'url': 'https://example.test.attacker/page'})
    assert call_gap(manifest, 'N03', 'Bash', {'command': 'python check.py'}) is None
    assert call_gap(manifest, 'N03', 'Bash', {'command': 'python other.py'})


def test_unreachable_resolver_reports_node_and_blocker():
    diagnostic, = resolver_diagnostics(flow())
    assert diagnostic['entry_ids'] == ['N03']
    assert diagnostic['blocker'] == 'capability_missing'


def test_missing_declaration_is_not_an_execution_grant(tmp_path):
    manifest = compile_manifest(ExpertFlowSpec.from_value({'flow_id': 'legacy', 'nodes': [
        {'node_id': 'N03', 'role': 'executor'}], 'edges': [], 'finalizer_node_id': 'N03'}),
        workspace=str(tmp_path), approved=True)
    assert call_gap(manifest, 'N03', 'Bash', {'command': 'python check.py'})
    with pytest.raises(ValueError):
        declaration({'command_execution': 'yes'})


def test_actual_manifest_guard_persists_specific_authorization_blocker(tmp_path):
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.kernel.capabilities import ManifestGuard
    from mewcode.experts.node_ledger import ledger_for_run
    store, run_id = ExpertRunStore(tmp_path / 'runs'), 'b' * 32
    store.create(run_id=run_id, conversation_id='fixture', task='fixture', attachments=[], profile={}, strategy='stable')
    store.mutate(run_id, lambda record: record.update(authorization_manifest=compile_manifest(
        flow(), workspace=str(tmp_path), approved=True)), durable=True)
    assert ManifestGuard(store, run_id, 'N03').check('WriteFile', {'file_path': 'outside.txt', 'content': 'fixture'})
    blocker, = ledger_for_run(store.load(run_id)).view('N03').active_blockers
    assert blocker.payload['kind'] == 'missing_authorization'
    assert blocker.payload['requested_call']['tool'] == 'WriteFile'


def test_snapshot_guard_uses_parent_declaration_without_widening_writes(tmp_path):
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.kernel.capabilities import ManifestGuard
    source, isolated = tmp_path / 'source', tmp_path / 'isolated'
    source.mkdir()
    isolated.mkdir()
    store, run_id = ExpertRunStore(tmp_path / 'runs'), 'c' * 32
    store.create(run_id=run_id, conversation_id='fixture', task='fixture', attachments=[], profile={}, strategy='stable')
    store.mutate(run_id, lambda record: record.update(authorization_manifest=compile_manifest(
        flow(), workspace=str(source), approved=True)), durable=True)
    guard = ManifestGuard(store, run_id, 'N03', execution_workspace=str(isolated))
    assert guard.check('WriteFile', {'file_path': str(isolated / 'outputs' / 'ok.txt')}) is None
    assert guard.check('WriteFile', {'file_path': str(isolated / 'outside.txt')})
    assert guard.check('WriteFile', {'file_path': str(source / 'outputs' / 'escape.txt')})
