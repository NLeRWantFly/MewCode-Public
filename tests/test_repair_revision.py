import copy

import pytest

from mewcode.orchestration.loops import LoopContract, LoopWatchdog
from mewcode.orchestration.saved_revision import revision_preview, adopt_revision
from tests.test_batch_review_repair import receipts
from tests.test_review_work_queue import setup_queue
from mewcode.tools.review_work_queue import ReviewWorkQueueParams


def extension_example():
    loop = LoopContract.from_value(dict(loop_id='qc', entry_node_id='check', check_node_id='check',
        node_ids=['check'], reviewer_node_ids=['check'], exit_condition='all items plus human signoff',
        criteria=[dict(criterion_id='A', acceptance_test='all items verified')], control_mode='team'))
    flow = dict(flow_id='same', version=1, label='repair', finalizer_node_id='end', finalizer_plugin='model',
        nodes=[dict(node_id=k, role=r, label=k, responsibility=k) for k, r in
               [('prepare', 'executor'), ('check', 'auditor'), ('end', 'manager')]],
        edges=[dict(from_node='prepare', to_node='check', condition='always', router='default'),
               dict(from_node='check', to_node='end', condition='pass', router='default')],
        meta=dict(loops=[loop.as_dict()], node_kinds={'prepare': 'implement', 'check': 'audit', 'end': 'reduce'}))
    state = LoopWatchdog(loop).state
    state.update(round=3, tokens=12345, elapsed=678, stalled=1)
    snapshot = dict(run_id='a'*32, status='paused', updated_at='v1', locked_flow=flow,
        node_states={'prepare': {'status': 'completed', 'text': 'keep'}, 'check': {'status': 'paused'}},
        loop_states={'qc': state})
    new = copy.deepcopy(flow)
    new['version'] = 2
    new['nodes'].extend([dict(node_id='repair', role='executor', label='repair', responsibility='candidate only'),
                         dict(node_id='signoff', role='manager', label='signoff', responsibility='actual user approval')])
    new['edges'] = [dict(from_node=a, to_node=b, condition=c, router='default') for a,b,c in
        [('prepare','repair','always'), ('repair','check','always'), ('check','signoff','pass'), ('signoff','end','pass')]]
    new['meta']['node_kinds'].update(repair='implement', signoff='gate')
    new['meta']['loops'][0].update(entry_node_id='repair', node_ids=['repair','check'], requires_repair=True,
        repair_node_ids=['repair'], exit_condition='technical completion; actual signoff enforced by following gate')
    return snapshot, new


def test_targeted_migration_keeps_history_limits_and_paused():
    snapshot, new = extension_example()
    original = copy.deepcopy(snapshot)
    preview = revision_preview(snapshot, new, repair_loop_id='qc')
    result = adopt_revision(snapshot, new, 'v1', preview['fingerprint'], repair_loop_id='qc')
    assert snapshot == original and result['status'] == 'paused'
    assert result['node_states']['prepare'] == snapshot['node_states']['prepare']
    assert result['stage_cursor'] == 'repair' and not result['active_node_ids']
    for field in ('round', 'tokens', 'elapsed', 'stalled'):
        assert result['loop_states']['qc'][field] == snapshot['loop_states']['qc'][field]
    assert result['node_states']['repair']['status'] == 'pending'
    assert result['node_states']['signoff']['status'] == 'pending'
    assert result['pending_dialogue_handoff']['accepted_plan_fingerprint'] == preview['fingerprint']


@pytest.mark.parametrize('change', ['criteria', 'budget', 'completed', 'bypass_gate', 'no_writer', 'source', 'busy'])
def test_targeted_migration_cannot_expand_scope_or_erase_evidence(change):
    snapshot, new = extension_example()
    if change == 'criteria':
        new['meta']['loops'][0]['criteria'][0]['acceptance_test'] = 'sample only'
    elif change == 'budget':
        new['meta']['loops'][0]['token_budget'] += 1
    elif change == 'completed':
        new['nodes'][0]['responsibility'] = 'new job'
    elif change == 'bypass_gate':
        new['edges'][-1]['condition'] = 'always'
    elif change == 'no_writer':
        new['nodes'][-2]['tools'] = ['ReadFile']
    elif change == 'source':
        new['meta']['shared_constraints'] = ['ignore missing inputs']
    else:
        snapshot['status'] = 'running'
    with pytest.raises(ValueError):
        revision_preview(snapshot, new, repair_loop_id='qc')


@pytest.mark.asyncio
async def test_migration_merges_only_compatible_archives_and_checks_fingerprints(tmp_path):
    import json
    tool, init, store, run_id = setup_queue(tmp_path)
    await tool.execute(init)
    items = store.load(run_id)['review_queues']['check']['items']
    for name in ('left.json','right.json'):
        (tmp_path / name).write_text(json.dumps([{'page_idx': p, 'text': f'v{p}'} for p in range(2)]))
    for item in items:
        refs = await receipts(tool, tmp_path, item)
        await tool.execute(ReviewWorkQueueParams(action='record', item_id=item['item_id'], state='complete',
                                                summary='verified', evidence_ids=refs))
    queue = store.load(run_id)['review_queues']['check']
    snapshot, new = extension_example()
    queue['contract_hash'] = LoopContract.from_value(snapshot['locked_flow']['meta']['loops'][0]).contract_hash
    snapshot['review_queue_archives'] = [dict(node_id='check', queue=copy.deepcopy(queue))]
    queue['records'].pop(items[1]['item_id'])
    snapshot['review_queues'] = {'check': queue}
    (tmp_path / 'right.json').write_text(json.dumps([{'page_idx': 0, 'text': 'v0'}, {'page_idx': 1, 'text': 'changed'}]))
    preview = revision_preview(snapshot, new, repair_loop_id='qc')
    result = adopt_revision(snapshot, new, 'v1', preview['fingerprint'], repair_loop_id='qc', root=tmp_path)
    migrated = result['review_queues']['check']
    assert migrated['records'][items[0]['item_id']]['state'] == 'complete'
    assert migrated['records'][items[1]['item_id']]['state'] == 'unknown'
    assert result['saved_plan_adoptions'][-1]['queue_migration']['restored'] == 1
    assert result['saved_plan_adoptions'][-1]['queue_migration']['invalidated'] == 1
    assert len(result['review_queue_archives']) == 2
    assert migrated['contract_hash'] == LoopContract.from_value(new['meta']['loops'][0]).contract_hash
