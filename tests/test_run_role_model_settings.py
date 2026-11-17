"""Exact task role replacement preserves deliveries, spending and user gates."""
import copy
from unittest.mock import AsyncMock

import httpx
import pytest

from frontend import server
from mewcode.kernel import collaboration as ledger
from mewcode.kernel.models import ROLES, RoleModels
from mewcode.orchestration.task_design import TaskDesign
from tests.test_collaboration_mode import CATALOG, PROFILE, RUN_ID, accept, store_for
from tests.test_task_design_entry import task_api_env


TARGET = {'provider_key': 'configured-magpie', 'model_id': 'desktop:example_provider_4/deepseek-v4.1-flash', 'effort': 'max'}
POOL = {'models': [*CATALOG['models'], {**TARGET, 'available': True, 'tools': True,
    'provider_brand': 'deepseek', 'efforts': ['high', 'max']}]}


def stopped(tmp_path):
    store = store_for(tmp_path)
    def seed(row):
        row['status'] = 'blocked'
        row['role_models'] = RoleModels(POOL).bind(PROFILE)
        accept(row['collaboration'], 'work')
    store.mutate(RUN_ID, seed, durable=True)
    return store


def apply(store, **overrides):
    values = dict(conversation_id='conversation', models={role: TARGET for role in ROLES}, catalog=POOL,
        expected_updated_at=store.load(RUN_ID)['updated_at'], command_id='user-exact-route',
        reason='User selected the configured DeepSeek route for all roles')
    values.update(overrides)
    return store.set_role_models(RUN_ID, **values)


def test_all_role_bindings_change_atomically_without_starting_or_rewriting_prior_work(tmp_path):
    store = stopped(tmp_path)
    before = store.load(RUN_ID)
    after = apply(store)
    assert after['role_models']['roles'] == {role: TARGET for role in ROLES}
    assert set(after['role_models']['explicit_roles']) == set(ROLES)
    assert after['locked_profile']['role_models'] == {role: TARGET for role in ROLES}
    assert after['collaboration']['coordinator'] == TARGET
    assert after['collaboration']['coordinator_epoch'] == before['collaboration']['coordinator_epoch'] + 1
    assert not after['role_models']['verification_independent']
    for key in ('locked_flow', 'locked_models', 'node_states', 'attempt', 'usage', 'status', 'authorizations'):
        assert after.get(key) == before.get(key), key
    assert after['collaboration']['certificates'] == before['collaboration']['certificates']
    assert after['collaboration']['lineage_budget']['grants'] == before['collaboration']['lineage_budget']['grants']
    assert ledger.valid_certificate(after['collaboration'], 'work')
    assert after['role_model_binding_changes'][0]['checkpoint_id']
    assert apply(store, expected_updated_at=before['updated_at']) == after


@pytest.mark.parametrize('fault', ['foreign', 'stale', 'running', 'active', 'unknown_call',
    'unavailable', 'bad_effort', 'unknown_role', 'command_reuse'])
def test_invalid_role_replacements_leave_every_role_and_run_fact_unchanged(tmp_path, fault):
    store = stopped(tmp_path)
    args = {}
    if fault == 'foreign':
        args['conversation_id'] = 'foreign'
    elif fault == 'stale':
        args['expected_updated_at'] = 'obsolete'
    elif fault == 'running':
        store.mutate(RUN_ID, lambda row: row.update(status='running'), durable=True)
    elif fault == 'active':
        store.mutate(RUN_ID, lambda row: row.update(status='running', active_node_ids=['deliver']), durable=True)
    elif fault == 'unknown_call':
        store.mutate(RUN_ID, lambda row: row['collaboration']['calls'].update({
            'unfinished': {'status': 'unknown', 'kind': 'acceptance', 'node_id': 'deliver'}}), durable=True)
    elif fault == 'unavailable':
        args['models'] = {'planner': TARGET, 'verifier': {**TARGET, 'provider_key': 'missing'}}
    elif fault == 'bad_effort':
        args['models'] = {'planner': TARGET, 'verifier': {**TARGET, 'effort': 'not-published'}}
    elif fault == 'unknown_role':
        args['models'] = {'approval': TARGET}
    else:
        apply(store)
        args['models'] = {'verifier': {**TARGET, 'effort': 'high'}}
    before = copy.deepcopy(store.load(RUN_ID))
    with pytest.raises(ValueError):
        apply(store, **args)
    assert store.load(RUN_ID) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['', 'unconfirmed', 'task', 'revision'])
async def test_task_role_api_requires_the_current_explicit_host_selection(task_api_env, tmp_path, monkeypatch, fault):
    _, _, _, owner, _ = task_api_env
    store = stopped(tmp_path)
    design = TaskDesign(owner['project'], owner['conversation_id'])
    task = design.start('execute', orchestration_mode='collaborative')
    task = design.transition(task, 'paused', run_id=RUN_ID)
    def bind(row):
        row['conversation_id'] = owner['conversation_id']
        row['collaboration']['task_id'] = task['task_id']
    store.mutate(RUN_ID, bind, durable=True)
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    monkeypatch.setattr(server.MODEL_REGISTRY, 'catalog', AsyncMock(return_value=POOL))
    monkeypatch.setattr(server.WS_EVENTS, 'send_json', AsyncMock())
    before = store.load(RUN_ID)
    body = {**owner, 'confirm_model_change': fault != 'unconfirmed', 'expected_task_id': task['task_id'],
        'expected_revision': task['revision'], 'expected_run_id': RUN_ID,
        'expected_updated_at': before['updated_at'], 'models': {role: TARGET for role in ROLES},
        'command_id': 'explicit-task-route', 'reason': 'User selected this exact route'}
    if fault == 'task':
        body['expected_task_id'] = 'other-task'
    if fault == 'revision':
        body['expected_revision'] -= 1
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        result = await client.post('/api/orchestration/task-design/role-models', json=body)
    assert result.status_code == (409 if fault else 200), result.text
    if fault:
        assert store.load(RUN_ID) == before
    else:
        assert result.json()['started'] is False
        assert store.load(RUN_ID)['role_models']['roles'] == {role: TARGET for role in ROLES}
