from types import SimpleNamespace

from mewcode.kernel.capabilities import compile_manifest
from mewcode.kernel.preflight import inspect, interpreter_gap
from mewcode.experts.flow import ExpertFlowSpec


def test_preflight_reports_native_and_quote_failures_with_affected_nodes(tmp_path):
    flow = ExpertFlowSpec.from_value({'flow_id': 'fixture', 'finalizer_node_id': 'N03', 'edges': [],
        'nodes': [{'node_id': 'N03', 'role': 'executor', 'capabilities': {
            'command_execution': True, 'network_hosts': ['example.test']}}]})
    manifest = compile_manifest(flow, workspace=str(tmp_path), approved=True)
    findings = inspect(manifest, native_probe=lambda: SimpleNamespace(ready=False, reason='isolator missing'),
                       quote_probe=lambda: (False, 'quotes lost'), probe_network=True,
                       network_probe=lambda host: (False, 'fixture unreachable'))
    assert {item['code'] for item in findings if item['severity'] == 'error'} == {
        'isolated_executor', 'powershell_quotes', 'network_scope'}
    assert all(item['entry_ids'] == ['N03'] for item in findings)


def test_non_ascii_venv_missing_base_interpreter_is_reported_without_repair(tmp_path):
    root = tmp_path / '用户路径'
    interpreter = root / '.venv' / 'Scripts' / 'python.exe'
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b'fixture')
    config = interpreter.parent.parent / 'pyvenv.cfg'
    config.write_text('executable = ' + str(root / '基础解释器缺失.exe'), encoding='utf-8')
    before = config.read_bytes()
    assert '基础解释器' in interpreter_gap(root, str(interpreter))
    assert config.read_bytes() == before


def test_auto_preflight_allows_model_to_repair_technical_gaps(tmp_path):
    flow = flow_with({'node_id': 'work', 'role': 'executor', 'capabilities': {
        'command_execution': True, 'interpreters': ['missing-python.exe'], 'network_hosts': ['example.test']}})
    manifest = compile_manifest(flow, workspace=str(tmp_path), approved=True)
    def unused_native():
        raise AssertionError('host execution must not probe an unused native boundary')
    findings = inspect(manifest, command_backend='host_reviewed', native_probe=unused_native,
        quote_probe=lambda: (False, 'fix quoting'), probe_network=True,
        network_probe=lambda host: (False, 'temporary connectivity failure'))
    assert not any(row['severity'] == 'error' for row in findings)
    assert {row['code'] for row in findings if row['severity'] == 'warning'} == {
        'base_interpreter', 'network_scope', 'powershell_quotes'}


def flow_with(*nodes):
    return ExpertFlowSpec.from_value({'flow_id': 'fixture', 'finalizer_node_id': nodes[-1]['node_id'],
        'edges': [], 'nodes': list(nodes)})


def test_approved_manifest_with_undeclared_nodes_cannot_start(tmp_path):
    """The real defect: approved=true while every node scope is null."""
    flow = flow_with({'node_id': 'N01', 'role': 'planner'}, {'node_id': 'N02', 'role': 'executor'})
    manifest = compile_manifest(flow, workspace=str(tmp_path), approved=True)
    findings = inspect(manifest)
    blocking = [item for item in findings if item['severity'] == 'error']
    assert [item['code'] for item in blocking] == ['undeclared_capabilities', 'undeclared_capabilities']
    assert [item['entry_ids'] for item in blocking] == [['N01'], ['N02']]
    assert all('不能启动' in item['message'] for item in blocking)


def test_unapproved_manifest_still_reports_undeclared_nodes_as_advisory(tmp_path):
    flow = flow_with({'node_id': 'N01', 'role': 'planner'})
    manifest = compile_manifest(flow, workspace=str(tmp_path), approved=False)
    findings = inspect(manifest)
    assert [item['severity'] for item in findings] == ['warning']


def test_a_fully_declared_approved_manifest_reports_no_capability_gap(tmp_path):
    flow = flow_with({'node_id': 'N01', 'role': 'executor',
        'capabilities': {'command_execution': False, 'write_paths': ['kb']}})
    manifest = compile_manifest(flow, workspace=str(tmp_path), approved=True)
    findings = inspect(manifest)
    assert [item['code'] for item in findings] == []
