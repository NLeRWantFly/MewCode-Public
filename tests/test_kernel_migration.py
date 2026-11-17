import copy
import json
from pathlib import Path
import zipfile
import pytest

from mewcode.expert_runs import ExpertRunStore
from mewcode.kernel.migration import migrate


def legacy_fixture():
    trace = json.loads((Path(__file__).parent / 'fixtures/paradigm/capability_missing_scheduler_hold.json').read_text(encoding='utf-8'))
    # The sanitized trace intentionally contains only N03. These completed
    # upstream artifacts are synthetic additions, never claims about live data.
    states = {'N01': {'status': 'completed', 'handoff': {'status': 'completed', 'inline_outputs': [
        {'name': 'synthetic-N01', 'content': 'fixture input inventory'}]}},
        'N02': {'status': 'completed', 'handoff': {'status': 'completed', 'inline_outputs': [
            {'name': 'synthetic-N02', 'content': 'fixture selected scope'}]}},
        **copy.deepcopy(trace['node_states'])}
    return {'run_id': trace['source']['run_snapshot']['run_id'], 'conversation_id': 'fixture',
            'status': 'paused', 'version': 1, 'locked_flow': trace['locked_flow'], 'node_states': states,
            'fixture_provenance': {'N03': 'sanitized source', 'N01/N02': 'synthetic completed prerequisites'}}


def test_legacy_import_is_read_only_idempotent_reuses_completed_artifacts_and_starts_n03(tmp_path):
    store, source = ExpertRunStore(tmp_path / 'runs'), legacy_fixture()
    original = copy.deepcopy(source)
    store._path(source['run_id']).write_text(json.dumps(source, ensure_ascii=False), encoding='utf-8')
    before = store._path(source['run_id']).read_bytes()
    assert store.load(source['run_id']) == source
    result = migrate(store, source, workspace=str(tmp_path))
    assert result['stage_cursor'] == 'N03'
    assert result['migration']['model_calls'] == 0
    assert result['migration']['reused_node_ids'] == ['N01', 'N02']
    assert result['node_states']['N01']['handoff'] == source['node_states']['N01']['handoff']
    assert source == original and store._path(source['run_id']).read_bytes() == before
    assert migrate(store, source, workspace=str(tmp_path))['run_id'] == result['run_id']
    with zipfile.ZipFile(result['migration']['archive']) as archive:
        assert json.loads(archive.read('source.json')) == original
        assert archive.read('original/' + source['run_id'] + '.json') == before
    assert store.public(source['run_id'])['migration_required']
    assert store._path(source['run_id']).read_bytes() == before
    with pytest.raises(ValueError, match='只读'):
        store.mutate(source['run_id'], lambda value: value.update(task='forbidden'))


def test_imported_host_definite_call_is_reused_under_unchanged_inputs(tmp_path):
    from mewcode.kernel.calls import propose
    source = legacy_fixture()
    source['node_states']['N03']['command_receipts'] = [{
        'request_id': 'host-fixture', 'status': 'completed', 'command_started': True, 'exit_code': 0,
        'input_revision': 'old-revision', 'request': {'tool': 'Bash', 'arguments': {'command': 'echo fixture'}},
        'stdout': 'fixture'}]
    store = ExpertRunStore(tmp_path / 'runs')
    record = migrate(store, source, workspace=str(tmp_path))
    imported = next(iter(record['kernel_calls'].values()))
    row = propose(record, 'N03', 'new-revision', {'tool': 'Bash', 'arguments': {'command': 'echo fixture'}})
    assert row['call_id'] == imported['call_id'] and row['state'] == 'succeeded'
    record['run_inputs']['changed'] = 'new context'
    assert propose(record, 'N03', 'changed-revision', row['invocation'])['state'] == 'proposed'
