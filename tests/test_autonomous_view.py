"""Projection replays real publication versions without mutating the journal."""
import copy

from mewcode.kernel import collaboration as ledger
from mewcode.orchestration.task_view import from_record
from tests.test_autonomous_context_runtime import retry_store
from tests.test_autonomous_rolling_runtime import rolling_flow, prepared, publish
from tests.test_collaboration_mode import RUN_ID


def test_future_milestone_and_published_children_are_truthful_pure_projections(tmp_path):
    flow = rolling_flow()
    store = retry_store(tmp_path, flow)
    record, fragment = prepared(tmp_path)
    store.mutate(RUN_ID, lambda saved: saved.update(collaboration=record['collaboration']), durable=True)
    saved = store.load(RUN_ID)
    before = copy.deepcopy(saved)
    future = from_record(saved)
    assert saved == before
    assert future['plan'][1]['status'] == '待展开'
    assert future['plan'][1]['steps'] == []
    assert future['plan'][1]['checks'][0]['criteria'] == ['后续报告符合目标']
    assert publish(saved, fragment)
    before = copy.deepcopy(saved)
    view = from_record(saved)
    assert from_record(saved) == view and saved == before
    versions = view['structure']['versions']
    assert [row['version'] for row in versions] == [1, 2]
    assert len(versions[0]['nodes']) == 2 and len(versions[1]['nodes']) == 5
    assert versions[0]['edges'] != versions[1]['edges']
    steps = view['plan'][1]['steps']
    child = next(row for row in steps if row['id'] in fragment['child_ids'])
    assert child['detail']['parent_label'] == '撰写后续报告'
    assert child['detail']['expanded_from']['contract_version'] == 1
    assert child['detail']['lineage_id'] == 'milestone::m2'
    assert next(row for row in child['detail']['budget'] if row['kind'] == 'expansions')['used'] == 1
    parent = next(row for row in steps if row['id'] == 'milestone::m2')
    assert {row['id'] for row in parent['detail']['join_children']} == set(fragment['child_ids'])
