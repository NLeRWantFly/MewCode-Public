"""A host-approved coordinator recovery unblocks the existing real scheduler."""
import copy

import pytest

from mewcode.kernel import collaboration as ledger
from mewcode.orchestration.collab_service import user_recovery
from mewcode.orchestration import inbox, task_view
from tests.test_autonomous_context_runtime import retry_flow, retry_store, orchestrate, run_flow
from tests.test_collaboration_mode import RUN_ID, CATALOG, EXECUTOR, REVIEWER, delivery


@pytest.mark.asyncio
async def test_failed_coordinator_waits_for_explicit_same_model_recovery(tmp_path):
    flow = retry_flow()
    store = retry_store(tmp_path, flow)
    executions, calls = [], []

    async def runner(node, *args):
        executions.append(node.node_id)
        return delivery('固定交付')

    async def review(model, payload, context):
        good = context['node_id'] != 'work' or executions.count('work') > 1
        return {'passed': good, 'evidence': ['宿主固定检查'], 'reason': '' if good else '缺少依据'}

    async def coordinate(model, payload, system):
        calls.append(model)
        raise ConnectionError('协调服务暂不可用')

    result = await run_flow(orchestrate(store, tmp_path, runner, review, coordinate), flow, store)
    assert result.status == 'blocked' and executions == ['work'] and len(calls) == 1
    store.transition(RUN_ID, 'blocked', reason=result.final_summary)
    record = store.load(RUN_ID)
    value = ledger.state(record)
    epoch = value['coordinator_epoch']
    gate = next(row for row in task_view.collaboration_projection(record)['recovery_gates'] if row['kind'] == 'coordinator_unavailable')
    card = next(row for row in inbox.collect(task_view.from_record(record)['task'], record=record) if row['type'] == 'collaboration_recovery')
    options = {row['value'] for row in card['questions'][0]['options']}
    assert options == {'resume_coordinator', 'switch_coordinator', 'revise'}
    before = copy.deepcopy(value['lineage_budget'])
    user_recovery(store, record, gate_id=gate['id'], expected_version=value['spec_version'], request_id='recover',
                  action='resume_coordinator', reason='用户决定恢复原协调服务', work_dir=tmp_path, catalog=CATALOG)
    recovered = store.load(RUN_ID)
    assert recovered['collaboration']['coordinator_epoch'] == epoch + 1
    assert recovered['collaboration']['lineage_budget'] == before
    assert recovered['collaboration']['coordinator'] == EXECUTOR.as_dict()
    store.resume(RUN_ID, strategy='stable')
    result = await run_flow(orchestrate(store, tmp_path, runner, review, coordinate), flow, store)
    assert result.status == 'completed', result.final_summary
    assert executions == ['work', 'work', 'deliver'] and len(calls) == 1


def test_coordinator_takeover_releases_only_unpublished_reservations(tmp_path):
    from tests.test_autonomous_publication import prepared
    store, record, attempt, publication = prepared(tmp_path)
    value = ledger.state(record)
    before_used = copy.deepcopy(value['lineage_budget']['root']['used'])
    ledger.takeover(value, EXECUTOR.as_dict(), source_ref='host-choice')
    assert not value['plan_holds']
    assert value['graph_publications'][publication['id']]['status'] == 'rejected'
    assert value['lineage_budget']['root']['reserved']['expansions'] == 0
    assert value['lineage_budget']['root']['used'] == before_used
    with pytest.raises(ValueError, match='旧尝试|旧协调者|版本|代际'):
        ledger.check_attempt(value, attempt['id'])
