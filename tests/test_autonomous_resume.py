"""Runtime child identity remains usable through the existing host resume API."""
import copy

import pytest

from mewcode.kernel.collaboration_graph import effective_flow
from tests.test_autonomous_publication import prepared
from tests.test_collaboration_mode import RUN_ID


def test_resume_accepts_published_child_but_refuses_replacing_runtime_graph(tmp_path):
    store, record, attempt, publication = prepared(tmp_path, publish=True)
    child = publication['fragment']['child_ids'][0]
    record.update(status='blocked', node_states={child: {'status': 'failed', 'node_id': child}}, active_node_ids=[])
    store.mutate(RUN_ID, lambda saved: saved.update(record), durable=True)
    resumed = store.resume(RUN_ID, strategy='stable', retry_node_id=child, flow=record['locked_flow'])
    assert child in effective_flow(resumed).node_map
    assert resumed['locked_flow'] == record['locked_flow']
    assert resumed['collaboration']['runtime_flow'] == record['collaboration']['runtime_flow']
    saved = store.load(RUN_ID)
    changed = copy.deepcopy(record['locked_flow'])
    changed['nodes'][0]['responsibility'] = '未经发布的新任务'
    with pytest.raises(ValueError, match='已发布子图'):
        store.resume(RUN_ID, strategy='stable', flow=changed)
    assert store.load(RUN_ID) == saved
