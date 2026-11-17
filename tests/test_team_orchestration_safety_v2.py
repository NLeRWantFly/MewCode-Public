"""Adversarial lifecycle, concurrency and evidence checks; no live provider."""
import asyncio
import copy
import json
import threading

import pytest

from mewcode.teams.disputes import DisputeService
from mewcode.teams.execution_pool import ModelReply
from mewcode.teams.responsibility import MissionService
from mewcode.teams.responsibility_coordinator import VerificationResult
from mewcode.teams.runtime_lock import runtime_locked
from mewcode.teams.store_v2 import TeamStateError
from team_responsibility_fixture import FixtureAdapter, make_host, make_run
from test_team_responsibility_v2 import runtime_case, initialize, open_dispute, evidence


@pytest.mark.asyncio
async def test_background_sql_obeys_shared_lock_while_snapshots_are_read(tmp_path,monkeypatch):
    host,_=make_host(tmp_path);run,_=make_run(host,third=True)
    extension=host.responsibility;runtime=host.runtime
    raw=runtime.db
    class CheckedConnection:
        def execute(self,*args,**kwargs):
            assert runtime.lock._is_owned(),'database operation escaped the shared transaction lock'
            return raw.execute(*args,**kwargs)
        def __getattr__(self,name):return getattr(raw,name)
    checked=CheckedConnection()
    for obj in (runtime,extension.broker,extension.pool,extension.pool.registry,extension.views,extension.coordinator,
                extension.coordinator.missions,extension.coordinator.disputes,extension.coordinator.plans):
        monkeypatch.setattr(obj,'db',checked)
    errors=[];stop=threading.Event()
    def observer():
        try:
            while not stop.is_set():
                snapshot=host.snapshot(run)
                assert len(snapshot['tasks'])==3
                assert snapshot['run']['task_set_revision']==1
                extension.views.events(host.actor,run,limit=8)
                stop.wait(.001)
        except BaseException as exc:errors.append(exc)
    watcher=threading.Thread(target=observer);watcher.start()
    try:
        job=extension.start(run,control_epoch=1,max_steps=8,command_id='start')
        await asyncio.wait_for(extension.jobs[job['job_id']],15)
        assert not errors,errors
        snapshot=host.snapshot(run)
        assert snapshot['run']['state']=='completed',snapshot['orchestration']['jobs']
    finally:
        stop.set();watcher.join(timeout=5)
        await host.aclose()


def test_runtime_lock_cannot_wrap_an_awaiting_function():
    async def wrong(self):pass
    with pytest.raises(TypeError,match='synchronous'):
        runtime_locked(wrong)


@pytest.mark.asyncio
async def test_initial_model_plan_is_checked_before_any_task_execution(tmp_path):
    class Planner(FixtureAdapter):
        async def complete(self,context,**options):
            if options.get('purpose')=='coordinator':
                self.contexts.append(context)
                assert context['planning_stage']=='initial_goal_decomposition'
                definition=copy.deepcopy(context['definition']);definition['version']+=1
                definition['tasks'][0]['objective']='先提取有版本依据的字段，再提交独立检查。'
                return ModelReply(json.dumps(dict(action='propose_plan',reason='Refine the authorized first-stage approach',definition=definition,assignments=context['mission']['assignments'])),20)
            assert any(c.get('planning_stage') for c in self.contexts),'execution preceded initial decomposition'
            return await super().complete(context,**options)
    host,adapter=make_host(tmp_path,adapter=Planner());run,_=make_run(host)
    m=MissionService(host.runtime).view(host.actor,run)
    MissionService(host.runtime).configure(host.actor,run,policy=dict(m['policy'],plan_before_execution=True),expected_version=m['state_version'],control_epoch=1,command_id='initial')
    try:
        result=await host.responsibility.coordinator.run_until_idle(run,control_epoch=1)
        assert result['completed'],result
        view=host.snapshot(run)
        assert view['run']['task_set_revision']==2
        assert len(adapter.contexts)==3
        assert view['orchestration']['plan_proposals'][0]['state']=='applied'
    finally:await host.aclose()


@pytest.mark.asyncio
async def test_rewording_validator_output_is_not_new_progress(tmp_path):
    host,_=make_host(tmp_path);run,_=make_run(host)
    try:
        await host.responsibility.pool.run_ready(host.actor,run)
        task=next(t for t in host.snapshot(run)['tasks'] if t['task_key']=='prepare')
        verdict=lambda detail:VerificationResult(accepted=False,scores=[dict(criterion_id=c['criterion_id'],score=0,evidence=detail) for c in task['contract']['acceptance']],detail='Exact same independent check')
        coordinator=host.responsibility.coordinator
        first=task['handoffs'][0]
        coordinator._verification_evidence(run,task,first,verdict('First phrasing of the same failure'),1)
        # A different handoff ID alone and reworded prose are not new bytes.
        coordinator._verification_evidence(run,task,dict(first,handoff_id=first['handoff_id']+'-retry'),verdict('Second phrasing; same actual result'),1)
        for dispute in coordinator.disputes.inbox(host.actor,run):
            assert len(dispute['evidence'])==1
            assert dispute['no_progress_rounds']==1
    finally:await host.aclose()


def test_same_evidence_needs_current_reattestation_without_counting_as_new_progress(runtime_case):
    _,runtime,owner,worker,active,_=runtime_case
    initialize(runtime,owner,active);service=DisputeService(runtime)
    dispute=open_dispute(runtime,owner,active)['dispute_id']
    item=service.add_evidence(worker,dispute,evidence=evidence(),expected_version=1,command_id='evidence')
    service.verify_evidence(owner,dispute,evidence_id=item['evidence_id'],expected_version=2,control_epoch=1,command_id='verify')
    service.record_round(owner,dispute,evidence_ids=[item['evidence_id']],action_taken='Original independent check',result='New source evidence',expected_version=3,control_epoch=1,command_id='round')
    task=runtime.task(owner,active['task_ids']['produce'])
    runtime.revise(owner,task['task_id'],inputs={},expected_version=task['state_version'],control_epoch=1,reason='Recheck the exact source under a new task revision',approved=True,command_id='revise')
    with pytest.raises(TeamStateError,match='CURRENT_EVIDENCE_REQUIRED'):
        service.resolve(owner,dispute,outcome='resolved',evidence_ids=[item['evidence_id']],explanation='Old verification is insufficient',expected_version=4,control_epoch=1,command_id='old-close')
    service.verify_evidence(owner,dispute,evidence_id=item['evidence_id'],expected_version=4,control_epoch=1,observed_revision=2,
                            verification='Independently checked that the same exact source still proves the current task criterion',command_id='reattest')
    recorded=service.record_round(owner,dispute,evidence_ids=[item['evidence_id']],action_taken='Verify applicability to revision 2',result='Same bytes, no new diagnostic fact',expected_version=5,control_epoch=1,command_id='same-round')
    assert not recorded['progress']
    result=service.resolve(owner,dispute,outcome='resolved',evidence_ids=[item['evidence_id']],explanation='Current independent attestation is recorded',expected_version=6,control_epoch=1,command_id='close')
    assert result['state']=='resolved' and len(service.get(owner,dispute)['evidence'])==1


@pytest.mark.asyncio
async def test_existing_grok_worker_receives_goal_and_data_only_context(runtime_case,tmp_path):
    from mewcode.teams.runtime_binding import GrokTeamExecutor
    from test_team_binding_v2 import TeamInvoker
    from test_coord_worker import FakeInvoker
    from test_team_execution_pool_v2 import candidate
    from mewcode.teams.execution_pool import SYSTEM
    from dataclasses import replace
    _,runtime,owner,_,active,_=runtime_case
    initialize(runtime,owner,active)
    class ResponsibilityInvoker(TeamInvoker):
        async def invoke_async(self,request):
            self.data_only_preflight(request.cwd)
            assert request.data_only and not request.allowed_tools
            context=json.loads(request.prompt.removeprefix(SYSTEM+'\n'))
            result=await FakeInvoker.invoke_async(self,request)
            return replace(result,parsed_output=dict(result=candidate(context),usage=dict(total_tokens=42)))
    invoker=ResponsibilityInvoker()
    result=await GrokTeamExecutor(runtime,invoker,root=tmp_path,session_id='fixture-legacy-mission').run_once()
    assert result and result[0]['state']=='verifying',result
    context=json.loads(runtime.db.execute('SELECT context_json FROM team_v2_attempt_contexts').fetchone()[0])
    assert context['mission']['goal_version']==1 and context['authority']=='host_contract_only'
    assert runtime.db.execute('SELECT COUNT(*) FROM team_v2_model_turns').fetchone()[0]==1
