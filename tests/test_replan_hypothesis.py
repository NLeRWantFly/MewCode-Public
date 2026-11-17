import json

import httpx
import pytest

from mewcode.tools.evaluate_replan_hypothesis import EvaluateReplanHypothesis, EvaluateHypothesisParams
from tests.test_adaptive_replan import setup
from tests.test_typesafe_review import make_client


@pytest.mark.asyncio
@pytest.mark.parametrize('confidence', [.1, .95])
async def test_optional_advice_not_permission_and_one_paid_batch(tmp_path, monkeypatch, confidence):
    handler, _, store, _, _, _, node, policy, _ = setup(tmp_path, monkeypatch)
    calls, charges = [], []
    def respond(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return httpx.Response(200, json={'model': 'jev-test-version', 'usage': {'input_tokens': 50, 'output_tokens': 10},
            'answers': {key: {'type': 'choice', 'choice': 'insufficient', 'confidence': confidence,
                'probabilities': {'supported': .2, 'contradicted': .2, 'insufficient': .6}}
                for key in payload['questions']}})
    tool = EvaluateReplanHypothesis(store=store, run_id=handler.run_id, node=node,
        client=make_client(respond), policy=lambda: policy, charge=charges.append)
    params = EvaluateHypothesisParams(hypotheses=[{'claim': 'Digest scopes differ', 'evidence_refs': ['r']}])
    result = json.loads((await tool.execute(params)).output)
    assert result['status'] == 'advisory' and result['result']['actual_model'] == 'jev-test-version'
    assert 'permission' in result['instruction']
    assert charges == [60] and len(calls) == 1
    assert set(calls[0]['state']) == {'hypotheses', 'evidence'}
    assert not store.load(handler.run_id)['node_states'][node.node_id].get('pending_interaction')
    await tool.execute(params)
    assert charges == [60] and len(calls) == 1


@pytest.mark.asyncio
async def test_unavailable_jev_never_falls_back_or_becomes_approval(tmp_path, monkeypatch):
    handler, _, store, primary, _, _, node, policy, _ = setup(tmp_path, monkeypatch)
    charges = []
    client = make_client(lambda request: httpx.Response(503, json={'error': 'synthetic outage'}))
    # Avoid HTTP transport retry semantics: simulate an uncertain lost receipt.
    async def unavailable(state, questions):
        raise ConnectionError('private-provider-body')
    client.evaluate = unavailable
    tool = EvaluateReplanHypothesis(store=store, run_id=handler.run_id, node=node,
        client=client, policy=lambda: policy, charge=charges.append)
    params = EvaluateHypothesisParams(hypotheses=[{'claim': 'Test', 'evidence_refs': ['r']}])
    result = await tool.execute(params)
    assert json.loads(result.output)['status'] == 'unavailable'
    assert 'private-provider-body' not in result.output
    assert not primary.calls and len(charges) == 1 and charges[0] > 0
    await tool.execute(params)
    assert len(charges) == 1
