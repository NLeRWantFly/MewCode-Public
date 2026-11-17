import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from frontend import server
from mewcode.kernel import collaboration as ledger
from mewcode.model_registry import ModelRef
from tests.test_collaboration_mode import store_for, RUN_ID, CATALOG, accept
from tests.test_task_design_entry import task_api_env

TARGET = ModelRef('selected-review', 'gpt-6.1-sol', 'xhigh')
POOL = {'models': [*CATALOG['models'], {**TARGET.as_dict(), 'available': True, 'tools': True,
                                      'provider_brand': 'review-vendor', 'efforts': ['high','xhigh']}]}


def paused(tmp_path):
    store = store_for(tmp_path)
    def seed(row):
        row['status'] = 'paused'
        accept(row['collaboration'], 'work')
        row['collaboration']['acceptance_model_overrides'] = {'deliver': CATALOG['models'][1]}
    store.mutate(RUN_ID, seed, durable=True)
    return store


def apply(store, **overrides):
    values = dict(conversation_id='conversation', model=TARGET.as_dict(), catalog=POOL,
                  expected_updated_at=store.load(RUN_ID)['updated_at'], command_id='user-choice')
    values.update(overrides)
    return store.set_verifier_model(RUN_ID, **values)


def test_verifier_change_is_durable_idempotent_and_preserves_all_existing_work(tmp_path):
    store=paused(tmp_path)
    before=store.load(RUN_ID)
    after=apply(store)
    assert after['locked_profile']['auditor'] == after['locked_profile']['role_models']['verifier'] == TARGET.as_dict()
    assert after['role_models']['roles']['verifier'] == TARGET.as_dict()
    assert 'verifier' in after['role_models']['explicit_roles']
    for key in ('collaboration','locked_models','locked_flow','node_states','attempt','usage','status'):
        assert after.get(key)==before.get(key), key
    assert ledger.valid_certificate(after['collaboration'],'work')
    assert apply(store, expected_updated_at=before['updated_at']) == after
    assert len(after['verifier_model_changes'])==1 and after['verifier_model_changes'][0]['checkpoint_id']


@pytest.mark.parametrize('case', ['foreign','stale','running','unresolved','command_reuse'])
def test_invalid_verifier_changes_leave_run_untouched(tmp_path, case):
    store=paused(tmp_path)
    args={}
    if case=='foreign':args['conversation_id']='other'
    elif case=='stale':args['expected_updated_at']='old'
    elif case=='running':store.mutate(RUN_ID,lambda row:row.update(status='running'),durable=True)
    elif case=='unresolved':
        store.mutate(RUN_ID,lambda row:row['collaboration']['calls'].update({'open-call':
            {'status':'dispatched','kind':'acceptance','basis':{'deliver':{}},'node_id':'deliver'}}),durable=True)
    else:
        apply(store)
        args['model']={**TARGET.as_dict(),'effort':'high'}
    before=store.load(RUN_ID)
    with pytest.raises(ValueError):apply(store,**args)
    assert store.load(RUN_ID)==before


@pytest.mark.asyncio
async def test_verifier_api_accepts_explicit_replacement_of_missing_old_provider(task_api_env,tmp_path,monkeypatch):
    _,_,_,owner,_=task_api_env
    store=paused(tmp_path)
    store.mutate(RUN_ID,lambda row:row.update(conversation_id=owner['conversation_id']),durable=True)
    monkeypatch.setattr(server,'EXPERT_RUNS',store)
    def resolve(ref):
        if ref!=TARGET:raise ValueError('old provider removed')
        return SimpleNamespace(ref=ref,effort=ref.effort,capabilities={'chat','tools','vision'})
    monkeypatch.setattr(server.MODEL_REGISTRY,'resolve',resolve)
    catalog=AsyncMock(return_value=POOL)
    monkeypatch.setattr(server.MODEL_REGISTRY,'catalog',catalog)
    monkeypatch.setattr(server.WS_EVENTS,'send_json',AsyncMock())
    before=store.load(RUN_ID)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://test') as client:
        result=await client.post('/api/expert-runs/'+RUN_ID+'/verifier-model',json={**owner,
            'model':TARGET.as_dict(),'expected_updated_at':before['updated_at'],'command_id':'explicit-api'})
    assert result.status_code==200,result.text
    after=store.load(RUN_ID)
    assert after['role_models']['roles']['verifier']==TARGET.as_dict()
    assert after['collaboration']==before['collaboration'] and after['status']=='paused'
    catalog.assert_awaited_once_with(local_only=True)
