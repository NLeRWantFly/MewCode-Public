from unittest.mock import AsyncMock

import pytest

from mewcode.expert_runs import ExpertRunStore
from mewcode.kernel.arbitration import Arbitration


def instance(tmp_path, response):
    store = ExpertRunStore(tmp_path)
    run_id = 'a' * 32
    store.create(run_id=run_id, conversation_id='fixture', task='task', attachments=[], profile={}, strategy='stable')
    return Arbitration(store, run_id, AsyncMock(return_value=response))


@pytest.mark.asyncio
async def test_each_epoch_arbitrates_once_and_reuses_journaled_result(tmp_path):
    arbiter = instance(tmp_path, {'decision': 'accept_verifier', 'confidence': .9, 'reason': 'host evidence supports verifier', 'evidence_refs': ['proof']})
    first = await arbiter.resolve('N03', {'pass': True}, {'pass': False}, [{'id': 'proof'}])
    second = await arbiter.resolve('N03', {'different': True}, {'pass': False}, [{'id': 'proof'}])
    assert first == second and first['decision'] == 'accept_verifier'
    arbiter.ask.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['authority', 'business', 'low_confidence', 'invented_evidence'])
async def test_authority_business_and_unreliable_judgments_require_human(tmp_path, boundary):
    arbiter = instance(tmp_path, {'decision': 'accept_executor', 'confidence': .4 if boundary == 'low_confidence' else .9,
        'reason': 'fixture judgment', 'evidence_refs': ['invented'] if boundary == 'invented_evidence' else ['proof']})
    result = await arbiter.resolve('N03', {'pass': True}, {'pass': False}, [{'id': 'proof'}],
        authority=boundary == 'authority', business=boundary == 'business')
    assert result['decision'] == 'human'
    if boundary in {'authority', 'business'}:
        arbiter.ask.assert_not_awaited()
