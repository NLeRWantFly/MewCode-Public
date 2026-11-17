"""Revision publication is fenced by the actual journal's operation receipts."""
import copy

import pytest

from mewcode.kernel import collaboration as ledger
from mewcode.orchestration.collab_coordinator import CollaborationCoordinator
from tests.test_collaboration_mode import CATALOG, RUN_ID, accept, store_for


def proposal(store):
    def prepare(record):
        value = ledger.state(record)
        value['entries']['method'] = dict(id='method', kind='requirement', body='first method',
            version=1, origin='model', strength='soft', sources=[], data={})
        value['contracts']['work']['requirement_ids'].append('method')
        other = copy.deepcopy(value['contracts']['work'])
        other.update(id='independent', node_id='independent', depends_on=[], requirement_ids=['goal'])
        value['contracts']['independent'] = other
        replacement = {**value['entries']['method'], 'body': 'revised method'}
        ledger.proposal(value, proposal_id='revision', changes={'method': replacement}, evidence=['observation'],
            actor='coordinator', base_version=value['spec_version'], epoch=value['coordinator_epoch'])
    store.mutate(RUN_ID, prepare, durable=True)


def test_proposal_holds_affected_successors_but_preserves_unrelated_branch(tmp_path):
    store = store_for(tmp_path)
    proposal(store)
    value = ledger.state(store.load(RUN_ID))
    assert ledger.affected_contracts(value, ['method']) == {'work', 'deliver'}
    for key in ('work', 'deliver'):
        with pytest.raises(ValueError, match='待处理修订'):
            ledger.dispatch(value, key)
    assert ledger.dispatch(value, 'independent')


@pytest.mark.asyncio
@pytest.mark.parametrize('timing', ['before_review', 'during_review'])
async def test_unknown_transitive_effect_prevents_revision_publication(tmp_path, timing):
    store = store_for(tmp_path)
    proposal(store)
    invoked = []
    def unknown():
        store.mutate(RUN_ID, lambda record: record.update(kernel_calls={
            'operation': {'node_id': 'deliver::child__check', 'state': 'unknown'}}), durable=True)
    if timing == 'before_review':
        unknown()
    async def review(*args):
        invoked.append(True)
        unknown()
        return {'passed': True, 'evidence': ['reviewed specification']}
    with pytest.raises(ValueError, match='结果不明'):
        await CollaborationCoordinator(store, RUN_ID, CATALOG, ask=review).review_proposal('revision')
    value = ledger.state(store.load(RUN_ID))
    assert len(invoked) == (timing == 'during_review')
    assert value['spec_version'] == 1 and value['plan_version'] == 1
    assert value['proposals']['revision']['status'] == 'proposed'
    assert value['entries']['method']['body'] == 'first method'
    assert value['lineage_budget']['root']['used']['revisions'] == 0
    assert store.load(RUN_ID)['kernel_calls']['operation']['state'] == 'unknown'


@pytest.mark.asyncio
async def test_unrelated_completion_and_unknown_effect_do_not_stale_revision(tmp_path):
    store = store_for(tmp_path)
    proposal(store)
    async def review(*args):
        def independent(record):
            accept(ledger.state(record), 'independent')
            record['kernel_calls'] = {'other': {'node_id': 'independent', 'state': 'unknown'}}
        store.mutate(RUN_ID, independent, durable=True)
        return {'passed': True, 'evidence': ['reviewed specification']}
    await CollaborationCoordinator(store, RUN_ID, CATALOG, ask=review).review_proposal('revision')
    value = ledger.state(store.load(RUN_ID))
    assert value['spec_version'] == 2
    assert ledger.valid_certificate(value, 'independent')
    assert not ledger.revision_pending(value, 'work')
    assert ledger.dispatch(value, 'work')


@pytest.mark.asyncio
async def test_related_change_at_final_commit_rejects_old_review(tmp_path, monkeypatch):
    store = store_for(tmp_path)
    proposal(store)
    coordinator = CollaborationCoordinator(store, RUN_ID, CATALOG)
    async def reviewed(**kwargs):
        store.mutate(RUN_ID, lambda record: record['collaboration']['contracts']['deliver'].update(version=2), durable=True)
        return {'passed': True, 'evidence': ['old review']}
    monkeypatch.setattr(coordinator, 'call', reviewed)
    with pytest.raises(ValueError, match='复核后变化'):
        await coordinator.review_proposal('revision')
    assert ledger.state(store.load(RUN_ID))['spec_version'] == 1
