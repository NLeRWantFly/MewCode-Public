"""Real maintenance ledger/agent/answer routes behind the workbench projection."""
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi import Request

from frontend import server
from frontend.workbench_api import Workbench
from frontend.workbench_answers import install_answers
from frontend.workbench_maintenance import install, project
from mewcode.orchestration.maintenance import MaintenanceStore, SCHEMA
from mewcode.orchestration.plan_session import build_plan_session
from mewcode.orchestration.task_design import TaskDesign
from mewcode.orchestration.workbench_events import subscribe
from tests.test_orchestration_maintenance import maintenance_env, update_proposal, poll
from tests.test_maintenance_agent import interactive_builder, wait_question


def setup(maintenance_env, monkeypatch):
    app, owner, current, durable, spec = maintenance_env
    design = TaskDesign(owner['project'], owner['conversation_id']).start('plan')
    spec = replace(spec, meta={**spec.meta, 'task_design':{'task_id':design['task_id']}})
    server._conversation_plan_store(owner['conversation_id']).put(owner['project'], build_plan_session(spec))
    monkeypatch.setattr(server, 'app', app)
    wb = Workbench(server)
    install_answers(app, wb); install(app, wb)
    @app.get('/api/workbench/view')
    async def read(request: Request):
        return wb.view(dict(request.query_params))
    return app, {**owner,'task_id':design['task_id'],'run_id':''}, spec, wb


@pytest.mark.asyncio
async def test_maintenance_question_only_answers_matching_live_request(maintenance_env, monkeypatch):
    app, owner, spec, wb = setup(maintenance_env, monkeypatch)
    interactive_builder(monkeypatch, Path(owner['project']))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        opened = await client.post('/api/workbench/maintenance', json={**owner,'action':'open','mode':'fix'},headers={'X-Mew-Workbench':'1'})
        assert opened.status_code==200, opened.text
        state=opened.json()
        sent=await client.post('/api/workbench/maintenance',json={**owner,'action':'send','id':state['id'],'revision':state['revision'],'message':'调查后按回答处理'},headers={'X-Mew-Workbench':'1'})
        assert sent.status_code==200,sent.text
        await wait_question(client,owner,state['id'])
        view=(await client.get('/api/workbench/view',params=owner)).json()
        card=next(row for row in view['inbox'] if row['type']=='maintenance_question')
        assert card['mandatory'] and card['questions'][0]['name']=='approach'
        assert not (Path(owner['project'])/'repair.txt').exists()
        answer_path='/api/workbench/inbox/'+card['id']+'/answer'
        answered=await client.post(answer_path,json={**owner,'answers':{'approach':'写入修复说明'}},headers={'X-Mew-Workbench':'1'})
        assert answered.status_code==200,answered.text
        stale=await client.post(answer_path,json={**owner,'answers':{'approach':'只调查'}},headers={'X-Mew-Workbench':'1'})
        assert stale.status_code==409 and '已经处理或已过期' in stale.json()['error']
        await poll(client,owner,state)
        assert (Path(owner['project'])/'repair.txt').read_text(encoding='utf-8')=='用户回答后才写入'
        refreshed=wb.view(owner)
        assert not any(row['type']=='maintenance_question' for row in refreshed['inbox'])
        assert refreshed['maintenance'][0]['messages']


@pytest.mark.asyncio
async def test_maintenance_decline_is_durable_and_does_not_apply_graph(maintenance_env,monkeypatch):
    app,owner,spec,wb=setup(maintenance_env,monkeypatch)
    ledger=server.MAINTENANCE_STORE
    state=ledger.open(owner['project'],owner['conversation_id'],spec.as_dict(),mode='update',node_ids=['work'])
    running=ledger.begin(owner['project'],owner['conversation_id'],state['id'],revision=state['revision'],message='加核验',base=spec.as_dict())
    ready=ledger.complete(owner['project'],owner['conversation_id'],state['id'],call_id=running['call_id'],base=spec.as_dict(),response={'schema':SCHEMA,'reply':'等你确认','proposal':update_proposal()})
    view=wb.view(owner);card=next(row for row in view['inbox'] if row['type']=='maintenance_proposal')
    assert 'spec' not in card['context']['proposal']
    before=ledger.path.read_bytes()
    assert MaintenanceStore.read_states(owner['project'],owner['conversation_id'],ledger.path)
    assert ledger.path.read_bytes()==before
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        response=await client.post('/api/workbench/inbox/'+card['id']+'/answer',json={**owner,'answers':{'action':'decline'}},headers={'X-Mew-Workbench':'1'})
        assert response.status_code==200,response.text
    saved=MaintenanceStore(ledger.path).get(owner['project'],owner['conversation_id'],state['id'])
    assert saved['proposal'] is None and saved['messages'][-1]['kind']=='decision'
    assert server._conversation_plan_store(owner['conversation_id']).get(owner['project']).spec.as_dict()==spec.as_dict()
    assert not wb.view(owner)['inbox']
    with pytest.raises(ValueError,match='不属于当前任务'):
        from frontend.workbench_maintenance import check_owner
        check_owner(wb,{**owner,'id':'0'*32})


def test_maintenance_notifications_and_unrelated_scope(tmp_path):
    events=[];unsubscribe=subscribe(events.append)
    try:
        missing=tmp_path/'absent.sqlite'
        assert MaintenanceStore.read_states(tmp_path,'chat',missing)==[] and not missing.exists()
        store=MaintenanceStore(tmp_path/'maintenance.sqlite')
        store.open(tmp_path,'chat',{'spec_id':'s'},mode='fix')
        assert events[-1]['kind']=='maintenance' and events[-1]['conversation_id']=='chat'
        assert MaintenanceStore.read_states(tmp_path,'other',store.path)==[]
    finally:unsubscribe()
