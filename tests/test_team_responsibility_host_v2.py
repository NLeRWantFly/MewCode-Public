import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from frontend.team_v2_api import install_team_v2_api
from mewcode.teams.execution_pool import ConfiguredModelAdapter, ModelNotStarted
from mewcode.teams.interventions import InterventionService
from mewcode.teams.store_v2 import TeamActor, TeamStateError
from mewcode.tools.base import StreamEnd, TextDelta, ThinkingDelta
from team_responsibility_fixture import FixtureAdapter, make_host, make_run


@pytest.fixture
def http_case(tmp_path):
    host, adapter = make_host(tmp_path)
    app = FastAPI()
    install_team_v2_api(app, host, authorize=lambda req:req.headers.get('authorization')=='Bearer fixture', static_dir=Path(__file__).resolve().parents[1]/'frontend'/'static')
    with TestClient(app, headers={'authorization':'Bearer fixture','X-Team-V2':'1'}) as client:
        yield host, adapter, client
    host.close()


def test_goal_http_is_idempotent_and_never_starts_inference(http_case):
    host, adapter, client = http_case
    payload = dict(objective='Prepare a traceable delivery',criteria=['Exact result','Traceable sources'],constraints=['Read only original files'],command_id='goal-draft')
    response = client.post('/api/team-v2/goal-drafts',json=payload)
    assert response.status_code==200,response.text
    repeat=client.post('/api/team-v2/goal-drafts',json=payload)
    assert repeat.json()==response.json()
    run=response.json()['run']['team_run_id']
    view=client.get('/api/team-v2/runs/'+run).json()
    assert view['orchestration']['mission']['owner_ref']==host.actor.principal_ref
    assert not response.json()['plan_generated_by_model']
    assert adapter.contexts==[] and view['tasks']==[]
    assert 'unused-local-fixture-key' not in json.dumps(view)
    assert client.get('/api/team-v2/models').json()[0]['adapter_installed']


def test_new_write_routes_preserve_desktop_and_csrf_guards(http_case):
    host,adapter,client=http_case
    run,_=make_run(host)
    for endpoint in ('coordinate','coordinate/stop','interpret','commands'):
        url=f'/api/team-v2/runs/{run}/{endpoint}'
        for headers in ({'X-Team-V2':''},{'authorization':''},{'origin':'https://untrusted.example'},{'sec-fetch-site':'cross-site'}):
            assert client.post(url,json={},headers=headers).status_code==403
    assert not adapter.contexts


def test_commands_cannot_reuse_other_run_objects(http_case):
    host,_,client=http_case
    first,_=make_run(host,suffix='first');second,_=make_run(host,suffix='second')
    receipt=InterventionService(host.runtime).submit(host.actor,first,request=dict(intent='pause',message='Pause this run',expected_goal_version=1,expected_plan_revision=1),control_epoch=1,command_id='pause')
    response=client.post(f'/api/team-v2/runs/{second}/commands',json=dict(command='apply_intervention',arguments=dict(intervention_id=receipt['intervention_id'],approved=True,control_epoch=1,command_id='foreign')))
    assert response.status_code==409 and 'RUN_NOT_ACCESSIBLE' in response.text
    assert host.runtime._runtime_run(first)['state']=='running'


def test_explicit_http_job_completes_with_real_checks_and_incremental_history(http_case):
    host,adapter,client=http_case
    adapter.use_tools=True
    run,_=make_run(host)
    response=client.post(f'/api/team-v2/runs/{run}/coordinate',json=dict(control_epoch=1,max_steps=16,command_id='start'))
    assert response.status_code==202,response.text
    for _ in range(200):
        view=client.get('/api/team-v2/runs/'+run).json()
        if view['orchestration']['jobs'][0]['state'] not in {'running','queued'}:
            break
        time.sleep(.02)
    assert view['run']['state']=='completed',view['orchestration']['jobs']
    assert len(adapter.contexts)==4
    cursor, sequences = 0, []
    while True:
        page=client.get(f'/api/team-v2/runs/{run}/events?after={cursor}&limit=10').json()
        sequences.extend(e['sequence'] for e in page['events'])
        cursor=page['next_cursor']
        if not page['has_more']:
            break
    assert sequences==sorted(set(sequences)) and sequences
    history=client.get('/api/team-v2/tasks/'+view['tasks'][0]['task_id']+'/history').json()
    attempt=history['revisions'][0]['attempts'][0]
    assert attempt['tools'][0]['result']['result']['source_version']=='v7'
    assert 'lease_token' not in json.dumps(history) and 'signature' not in json.dumps(history)
    assert client.post(f'/api/team-v2/runs/{run}/coordinate',json=dict(control_epoch=1,max_steps=65,command_id='bad')).status_code==422


@pytest.mark.asyncio
async def test_stop_before_first_coroutine_turn_does_not_leave_queued_job(tmp_path):
    host,adapter=make_host(tmp_path);run,_=make_run(host)
    try:
        result=host.responsibility.start(run,control_epoch=1,max_steps=8,command_id='start')
        stopped=await host.responsibility.stop(run,job_id=result['job_id'],control_epoch=1,command_id='stop')
        assert stopped['state']=='interrupted' and not adapter.contexts
        assert not host.runtime.db.execute("SELECT 1 FROM team_v2_host_jobs WHERE state IN ('queued','running')").fetchone()
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_shutdown_drains_active_callbacks_and_preserves_unknown_usage(tmp_path):
    started=asyncio.Event()
    class Slow(FixtureAdapter):
        async def complete(self,context,**options):
            started.set();await asyncio.Event().wait()
    host,_=make_host(tmp_path,adapter=Slow());run,_=make_run(host)
    host.responsibility.start(run,control_epoch=1,max_steps=8,command_id='start')
    await asyncio.wait_for(started.wait(),5)
    with pytest.raises(TeamStateError,match='HOST_EXECUTION_ACTIVE'):
        host.close()
    await host.aclose()
    assert host._store is None
    # Re-open the same explicit fixture store and inspect, never infer completion.
    view=host.snapshot(run)
    assert view['run']['state']!='completed'
    assert next(t for t in view['tasks'] if t['task_key']=='prepare')['held_tokens']==600
    assert view['orchestration']['jobs'][0]['state']=='interrupted'
    await host.aclose()


@pytest.mark.asyncio
async def test_detached_host_stop_revokes_original_live_loop(tmp_path):
    started,release=asyncio.Event(),asyncio.Event()
    class Slow(FixtureAdapter):
        async def complete(self,context,**options):
            started.set();await release.wait();return await super().complete(context,**options)
    host,_=make_host(tmp_path,adapter=Slow());run,_=make_run(host)
    job=host.responsibility.start(run,control_epoch=1,max_steps=8,command_id='start')
    await asyncio.wait_for(started.wait(),5)
    from mewcode.teams.orchestration_host import ResponsibilityHost
    other=ResponsibilityHost(host)
    await other.stop(run,job_id=job['job_id'],control_epoch=1,command_id='other-stop')
    release.set();await host.responsibility.jobs[job['job_id']]
    view=host.snapshot(run)
    assert not any(t['handoffs'] for t in view['tasks'])
    assert view['orchestration']['jobs'][0]['state']=='interrupted'
    assert next(t for t in view['tasks'] if t['task_key']=='prepare')['held_tokens']==600
    await host.aclose()


@pytest.mark.asyncio
async def test_information_freezes_per_attempt_and_delivery_is_real(tmp_path):
    started,release=asyncio.Event(),asyncio.Event()
    class Slow(FixtureAdapter):
        async def complete(self,context,**options):
            if not self.contexts:
                started.set();await release.wait()
            return await super().complete(context,**options)
    host,adapter=make_host(tmp_path,adapter=Slow());run,_=make_run(host)
    try:
        pending=asyncio.create_task(host.responsibility.pool.run_ready(host.actor,run))
        await asyncio.wait_for(started.wait(),5)
        message=InterventionService(host.runtime).submit(host.actor,run,request=dict(intent='information',message='New data, not instructions',expected_goal_version=1,expected_plan_revision=1),control_epoch=1,command_id='info')
        release.set();await pending
        assert not adapter.contexts[0]['user_information']
        result=await host.responsibility.coordinator.run_until_idle(run,control_epoch=1)
        assert result['completed'],result
        assert adapter.contexts[-1]['user_information'][0]['intervention_id']==message['intervention_id']
        receipt=InterventionService(host.runtime).list(host.actor,run)[0]
        assert len(receipt['deliveries'])==1
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_configured_adapter_uses_sdk_usage_and_never_stores_thinking(monkeypatch):
    from mewcode.config import ProviderConfig
    seen=[]
    class Client:
        def __init__(self):self._client=self;self.closed=False
        async def close(self):self.closed=True
        async def stream(self,conversation,**kwargs):
            seen.append((conversation,kwargs))
            yield ThinkingDelta('private fixture reasoning')
            yield TextDelta('{"public":"result","usage_tokens":999}')
            yield StreamEnd('end_turn',input_tokens=20,output_tokens=5,cache_read=10,cache_creation=2)
    client=Client();monkeypatch.setattr('mewcode.client.create_client',lambda config:client)
    adapter=ConfiguredModelAdapter(ProviderConfig(name='fixture',protocol='openai',base_url='http://127.0.0.1:9',model='fixture-model',api_key='fixture'))
    result=await adapter.complete({'scoped':'data'},max_tokens=100)
    assert result.usage_tokens==37 and 'private fixture reasoning' not in result.text
    assert seen[0][1]['tools']==[] and client.closed
    adapter.config.model='codex-auto-review'
    with pytest.raises(ModelNotStarted,match='INVALID_MODEL'):
        await adapter.complete({},max_tokens=100)


def test_restarting_a_host_reattests_its_installed_routes(tmp_path):
    host,adapter=make_host(tmp_path)
    registry=host.responsibility.pool.registry
    descriptor=registry._catalog(host.actor.project_id)['fixture/model-a'].model_copy(update={'ready':False,'reason':'Fixture previous host disconnected'})
    registry.register(host.actor,descriptor,command_id='offline')
    host.close()
    try:
        restored=host.status()
        assert next(r for r in restored['model_routes'] if r['route_id']=='fixture/model-a')['ready']
        assert not adapter.contexts
    finally:host.close()


@pytest.mark.asyncio
async def test_mcp_extension_only_executes_at_explicit_entry_point(tmp_path):
    from mewcode.mcp.coord_server import build_server
    host,adapter=make_host(tmp_path);run,_=make_run(host)
    try:
        server=build_server(host._store,team_host=host)
        tool=lambda name:server._tool_manager.get_tool(name).fn
        assert tool('coord_team_v2_snapshot')(run)['run']['state']=='running'
        assert tool('coord_team_v2_models')() and not adapter.contexts
        result=await tool('coord_team_v2_execute')(run,1,'mcp-start',8)
        await host.responsibility.jobs[result['job_id']]
        assert len(adapter.contexts)==2
        assert host.snapshot(run)['run']['state']=='completed'
        stopped=await tool('coord_team_v2_stop_execution')(run,result['job_id'],1,'mcp-stop')
        assert stopped['state']=='completed' and not stopped['completion_inferred']
    finally:await host.aclose()
