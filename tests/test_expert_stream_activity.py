"""A reconnect exposes live metadata while keeping reasoning private."""
import json
import pytest

from mewcode.expert_runs import ExpertRunStore
from tests.test_audit_protocol import flow_spec


@pytest.mark.parametrize('storage', ['legacy', 'sqlite'])
def test_stream_flush_restores_liveness_without_private_reasoning(tmp_path, storage):
    store = ExpertRunStore(tmp_path / 'runs', storage=storage)
    run_id = 'b' * 32
    store.create(run_id=run_id, conversation_id='c', task='stream fixture', attachments=[], profile={},
                 strategy='stable', flow=flow_spec().as_dict())
    store.mutate(run_id, lambda row: row.update(status='running', node_states={
        'executor': {'role': 'executor', 'status': 'running', 'started_at': 100}}))
    activity = {"phase": "processing", "updated_at": 123.5}
    store.flush_stream(run_id, "executor", text="Public answer", reasoning="PRIVATE_REASONING_SENTINEL",
                       messages=[], stream_activity=activity, return_snapshot=False)
    raw = store.load(run_id)
    assert raw['node_states']['executor']['stream_activity'] == activity
    public = ExpertRunStore(tmp_path / 'runs', storage=storage).read_public(run_id)
    stage = next(e for e in public['events'] if e['type'] == 'expert_stage' and e['node_id'] == 'executor')
    assert stage['stream_activity'] == activity
    assert 'PRIVATE_REASONING_SENTINEL' not in json.dumps(public)
    assert any(e['type'] == 'expert_delta' and e['text'] == 'Public answer' for e in public['events'])
