"""Run-control previews cannot mutate state before an inbox confirmation."""
from unittest.mock import AsyncMock
from types import SimpleNamespace

import httpx
import pytest

from frontend import server
from tests.test_expert_retry import blocked_run
from tests.test_task_design_entry import task_api_env


@pytest.mark.asyncio
async def test_stop_active_investigation_requires_confirmation_and_keeps_scope(task_api_env,tmp_path,monkeypatch):
    from mewcode.experts.recovery_case import open_case,save_case
    _,_,_,owner,_=task_api_env
    store,run_id,_=blocked_run(tmp_path)
    def running(record):
        record.update(conversation_id=owner['conversation_id'],status='running')
        case=open_case(record,'repair',error='只读调查')
        case['investigation_requested']={'question':'核对目录'}
        save_case(record,case)
    store.mutate(run_id,running)
    before=store.load(run_id);case=before['node_states']['repair']['recovery_case']
    monkeypatch.setattr(server,'EXPERT_RUNS',store)
    monkeypatch.setattr(server,'_workbench_run_actions',{},raising=False)
    monkeypatch.setattr(server.SESSION,'runtime_for',lambda cid:None)
    original=AsyncMock(return_value=dict(ok=True))
    monkeypatch.setattr(server,'api_recovery_case_answer',original)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://test') as client:
        values={**owner,'run_id':run_id}
        view=(await client.get('/api/workbench/view',params=values)).json();values['task_id']=view['task']['id']
        prepared=await client.post('/api/workbench/run-action',headers={'X-Mew-Workbench':'1'},json={**values,
            'operation':'stop_repair','case_id':case['case_id'],'expected_updated_at':before['updated_at']})
        assert prepared.status_code==200,prepared.text
        assert store.load(run_id)==before and original.await_count==0
        view=(await client.get('/api/workbench/view',params=values)).json();card=next(row for row in view['inbox'] if row['type']=='run_action')
        response=await client.post('/api/workbench/inbox/'+card['id']+'/answer',headers={'X-Mew-Workbench':'1'},json={**values,'answers':{'action':'accept'}})
        assert response.status_code==200,response.text
        sent=original.await_args.args[0]
        assert (sent.case_id,sent.choice,sent.run_id,sent.conversation_id)==(case['case_id'],'close',run_id,owner['conversation_id'])


@pytest.mark.asyncio
async def test_checkpoint_preview_confirm_and_second_tab(task_api_env, tmp_path, monkeypatch):
    _,_,_,owner,_=task_api_env
    store,run_id,_=blocked_run(tmp_path)
    store.mutate(run_id,lambda row: row.update(conversation_id=owner['conversation_id']))
    point=store.save_turn_checkpoint(run_id)
    original=store.load(run_id)['node_states']['repair']['text']
    store.mutate(run_id,lambda row: row['node_states']['repair'].update(text='later output'))
    before=store.load(run_id)
    monkeypatch.setattr(server,'EXPERT_RUNS',store)
    monkeypatch.setattr(server.SESSION,'runtime_for',lambda cid:None)
    monkeypatch.setattr(server.WS_EVENTS,'send_json',AsyncMock())
    monkeypatch.setattr(server,'_workbench_run_actions',{},raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://test') as client:
        values={**owner,'run_id':run_id}
        view=(await client.get('/api/workbench/view',params=values)).json()
        assert not view['legacy']
        values['task_id']=view['task']['id']
        request={**values,'operation':'checkpoint','checkpoint_id':point['checkpoint_id'],'expected_updated_at':before['updated_at']}
        stale=await client.post('/api/workbench/run-action',json={**request,'expected_updated_at':'old'},headers={'X-Mew-Workbench':'1'})
        assert stale.status_code==409
        prepared=await client.post('/api/workbench/run-action',json=request,headers={'X-Mew-Workbench':'1'})
        assert prepared.status_code==200,prepared.text
        assert store.load(run_id)==before
        current=(await client.get('/api/workbench/view',params=values)).json()
        card=next(row for row in current['inbox'] if row['type']=='run_action')
        assert card['mandatory'] and card['context']['checkpoint_id']==point['checkpoint_id']
        path='/api/workbench/inbox/'+card['id']+'/answer'
        confirmed=await client.post(path,json={**values,'answers':{'action':'accept'}},headers={'X-Mew-Workbench':'1'})
        assert confirmed.status_code==200,confirmed.text
        assert store.load(run_id)['node_states']['repair']['text']==original
        assert store.load(run_id)['status']!='running'
        second=await client.post(path,json={**values,'answers':{'action':'accept'}},headers={'X-Mew-Workbench':'1'})
        assert second.status_code==409 and '已经处理或已过期' in second.json()['error']
        assert len(store.load(run_id)['checkpoint_restorations'])==1


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,node_id,argument',[('retry_node','repair','retry_node_id'),('resume_after_node','prepare','resume_after_node')])
async def test_step_resume_uses_original_dispatch_only_after_confirmation(task_api_env,tmp_path,monkeypatch,operation,node_id,argument):
    _,_,_,owner,_=task_api_env
    store,run_id,_=blocked_run(tmp_path)
    store.mutate(run_id,lambda row:row.update(conversation_id=owner['conversation_id']))
    before=store.load(run_id);scheduled=[]
    runtime=SimpleNamespace(conversation_id=owner['conversation_id'],starting=False)
    continuation=AsyncMock()
    monkeypatch.setattr(server,'EXPERT_RUNS',store)
    monkeypatch.setattr(server,'_workbench_run_actions',{},raising=False)
    monkeypatch.setattr(server.SESSION,'runtime_for',lambda cid:None)
    monkeypatch.setattr(server.SESSION,'capture_conversation_runtime',lambda:runtime)
    monkeypatch.setattr(server.SESSION,'begin_conversation_turn',lambda value:None)
    monkeypatch.setattr(server.SESSION,'attach_conversation_task',lambda value,task:scheduled.append(task))
    monkeypatch.setattr(server,'_resume_expert_after_paused_dialogue',continuation)
    monkeypatch.setattr(server.WS_EVENTS,'send_json',AsyncMock())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://test') as client:
        values={**owner,'run_id':run_id}
        view=(await client.get('/api/workbench/view',params=values)).json();values['task_id']=view['task']['id']
        request={**values,'operation':operation,'node_id':node_id,'expected_updated_at':before['updated_at']}
        invalid=await client.post('/api/workbench/run-action',json={**request,'node_id':'missing'},headers={'X-Mew-Workbench':'1'})
        assert invalid.status_code==409
        prepared=await client.post('/api/workbench/run-action',json=request,headers={'X-Mew-Workbench':'1'})
        assert prepared.status_code==200,prepared.text
        assert store.load(run_id)==before and not scheduled
        view=(await client.get('/api/workbench/view',params=values)).json()
        card=next(row for row in view['inbox'] if row['type']=='run_action')
        path='/api/workbench/inbox/'+card['id']+'/answer'
        response=await client.post(path,json={**values,'answers':{'action':'accept'}},headers={'X-Mew-Workbench':'1'})
        assert response.status_code==200,response.text
        assert response.json()['execution_started']
        assert len(scheduled)==1
        await scheduled[0]
        assert continuation.await_args.kwargs[argument]==node_id
        assert continuation.await_args.args[2]['run_id']==run_id
        second=await client.post(path,json={**values,'answers':{'action':'accept'}},headers={'X-Mew-Workbench':'1'})
        assert second.status_code==409 and len(scheduled)==1
        assert store.load(run_id)==before
