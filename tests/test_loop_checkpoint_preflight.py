"""A host checkpoint can judge current evidence without gaining capabilities."""
import copy

import pytest

from mewcode.experts.flow import ExpertFlowSpec
from mewcode.kernel.capabilities import call_gap, declaration
from mewcode.kernel.preflight import inspect_node
from mewcode.kernel.preflight_binding import prepare_manifest


def checkpoint_flow(role='auditor', capabilities=None):
    return ExpertFlowSpec.from_value({
        'flow_id': 'checkpoint-preflight', 'finalizer_node_id': 'done',
        'nodes': [
            {'node_id': 'work', 'role': 'executor', 'capabilities': {}},
            {'node_id': 'check', 'role': role, 'capabilities': capabilities},
            {'node_id': 'done', 'role': 'executor', 'capabilities': {}},
        ],
        'edges': [{'from_node': 'work', 'to_node': 'check'},
                  {'from_node': 'check', 'to_node': 'done'}],
        'meta': {'loops': [{'loop_id': 'L', 'entry_node_id': 'work',
            'check_node_id': 'check', 'node_ids': ['work', 'check'],
            'reviewer_node_ids': ['check'], 'exit_condition': 'Evidence complete',
            'criteria': [{'criterion_id': 'C', 'acceptance_test': 'Evidence complete'}]}]},
    })


def test_legacy_loop_checkpoint_preflight_and_tool_scope(tmp_path):
    from mewcode.expert_group import ExpertExecutionPolicy
    from mewcode.expert_web import _role_registry
    from tests.test_expert_children import FakeSession, make_registry

    flow = checkpoint_flow()
    original = copy.deepcopy(flow.as_dict())
    record = {}
    manifest = prepare_manifest(record, flow, tmp_path)
    assert manifest['nodes']['check'] == declaration({})
    assert not any(row['severity'] == 'error' for row in inspect_node(manifest, 'check'))
    assert flow.as_dict() == original
    assert 'authorizations' not in record

    session = FakeSession(make_registry('ReadFile', 'WriteFile', 'EditFile', 'Bash', 'WebFetch'))
    session.work_dir = str(tmp_path)
    policy = ExpertExecutionPolicy(strategy='fast', allow_delegation=False, max_direct_agents=0,
        loop_context={'reviewer': True})
    registry = _role_registry(session, 'auditor', policy)
    names = {tool.name for tool in registry.list_tools()}
    assert {'ReadFile', 'ReadAuditArtifact', 'SubmitLoopDecision'} <= names
    assert not names & {'WriteFile', 'EditFile', 'Bash', 'WebFetch', 'RunVerification'}
    assert call_gap(manifest, 'check', 'ReadFile', {'file_path': 'report.json'}) is None
    assert call_gap(manifest, 'check', 'SubmitLoopDecision', {}) is None
    for tool, args in [
        ('WriteFile', {'file_path': 'report.json', 'content': 'changed'}),
        ('Bash', {'command': 'python worker.py'}),
        ('WebFetch', {'url': 'https://example.test'}),
        ('InspectVisualEvidence', {'file_path': 'page.png'}),
    ]:
        assert call_gap(manifest, 'check', tool, args)


@pytest.mark.parametrize('role', ['executor', 'planner', 'manager'])
def test_checkpoint_name_does_not_declare_a_non_auditor(tmp_path, role):
    manifest = prepare_manifest({}, checkpoint_flow(role), tmp_path)
    assert manifest['nodes']['check'] is None
    assert any(row['code'] == 'undeclared_capabilities' and row['severity'] == 'error'
               for row in inspect_node(manifest, 'check'))


def test_checkpoint_preserves_explicit_scope_and_does_not_match_an_unbound_auditor(tmp_path):
    scope = {'network_hosts': ['example.test']}
    manifest = prepare_manifest({}, checkpoint_flow(capabilities=scope), tmp_path)
    assert manifest['nodes']['check'] == declaration(scope)
    value = checkpoint_flow().as_dict()
    value['meta'] = {}
    assert prepare_manifest({}, value, tmp_path)['nodes']['check'] is None
