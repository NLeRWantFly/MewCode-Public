"""Startup decisions survive reload and feed the real TaskDesign continuation."""
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from frontend import server, workbench_startup as startup
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.task_design import TaskDesign
from tests.test_task_design_entry import task_api_env


def setup(task_api_env, monkeypatch):
    _, _, _, owner, _ = task_api_env
    design = TaskDesign(owner['project'], owner['conversation_id'])
    current = design.start('execute')
    current = design.transition(current, 'paused', build_request={'id':'saved', 'epoch':1, 'state':'saved'})
    spec = {'meta':{'task_design':{'task_id':current['task_id']}}, 'goal_contract': {'run_inputs': [
        {'name':'sources','label':'输入文件','type':'list','required':True},
        {'name':'later','label':'稍后信息','collect_at':'work','required':True}]}}
    plan = SimpleNamespace(spec=SimpleNamespace(as_dict=lambda: copy.deepcopy(spec)))
    monkeypatch.setattr(server, '_conversation_plan_store', lambda cid: SimpleNamespace(get=lambda project: plan))
    return owner, design, current, spec


@pytest.mark.asyncio
async def test_startup_answer_reload_and_continuation(task_api_env, monkeypatch):
    owner, design, current, spec = setup(task_api_env, monkeypatch)
    model = AsyncMock(side_effect=AssertionError('startup answers must not call a provider'))
    monkeypatch.setattr(server.MODEL_REGISTRY, 'catalog', model)
    calls = []
    async def confirm(request):
        calls.append(await request.json())
        return {'started':True}
    monkeypatch.setattr(server, 'api_orchestration_confirm', confirm)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        view = (await client.get('/api/workbench/view', params=owner)).json()
        card = next(row for row in view['inbox'] if row['type']=='run_inputs')
        assert [row['name'] for row in card['questions']] == ['sources']
        path = '/api/workbench/inbox/'+card['id']+'/answer'
        body = {**owner,'task_id':current['task_id'],'answers':{'sources':['explicit.csv']}}
        invalid = await client.post(path, json={**body,'answers':{'invented':'x'}}, headers={'X-Mew-Workbench':'1'})
        assert invalid.status_code==409
        good = await client.post(path, json=body, headers={'X-Mew-Workbench':'1'})
        assert good.status_code==200, good.text
        assert not good.json()['execution_started'] and not calls
        again = await client.post(path, json=body, headers={'X-Mew-Workbench':'1'})
        assert again.status_code==409 and '已经处理或已过期' in again.json()['error']
        fresh = (await client.get('/api/workbench/view', params=owner)).json()
        assert not any(row['type']=='run_inputs' for row in fresh['inbox'])
        assert startup.execution_values(server, owner['project'], owner['conversation_id'], design.read(), spec) == {'sources':['explicit.csv'],'later':''}
        resumed = await client.post('/api/orchestration/task-design', json={**owner,'action':'resume',
            'expected_task_id':current['task_id'],'expected_revision':current['revision']})
        assert resumed.status_code==200, resumed.text
        assert len(calls)==1 and calls[0]['run_inputs']=={'sources':['explicit.csv'],'later':''}
    model.assert_not_awaited()


def test_startup_receipt_rejects_new_spec_and_task_revision(task_api_env, monkeypatch):
    owner, design, current, spec = setup(task_api_env, monkeypatch)
    state = DraftStore.read_state(owner['project'], owner['conversation_id'])
    task = {'id':current['task_id'],'conversation_id':owner['conversation_id'],'run_id':''}
    card = startup.collect(server, owner['project'], owner['conversation_id'], task, state)[0]
    changed = design.transition(current, 'paused', '更新要求')
    with pytest.raises(ValueError, match='已经处理或已过期'):
        startup.save(server, owner['project'], owner['conversation_id'], current['task_id'], card['context'], {'sources':['a']})
    card = startup.collect(server, owner['project'], owner['conversation_id'], task, DraftStore.read_state(owner['project'],owner['conversation_id']))[0]
    startup.save(server, owner['project'], owner['conversation_id'], current['task_id'], card['context'], {'sources':['a']})
    spec['goal_contract']['run_inputs'][0]['label'] = '新的输入范围'
    assert startup.execution_values(server, owner['project'], owner['conversation_id'], changed, spec)=={}
    assert startup.collect(server, owner['project'], owner['conversation_id'], task, DraftStore.read_state(owner['project'],owner['conversation_id']))
