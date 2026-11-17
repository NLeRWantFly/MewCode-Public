import asyncio
import copy
import json

import pytest

from mewcode.teams.responsibility_coordinator import Coordinator, VerificationRegistry
from mewcode.teams.dynamic_plans import PlanService
from mewcode.teams.execution_pool import ExecutionPool, ModelReply
from mewcode.teams.intervention_interpreter import InterventionInterpreter
from mewcode.teams.interventions import InterventionService
from mewcode.teams.responsibility import MissionService
from mewcode.teams.runtime_protocol import ArtifactContent
from mewcode.teams.spec import SpecRef
from mewcode.teams.store_v2 import TeamActor, TeamStateError
from mewcode.teams.tool_broker import ToolBroker
from test_team_deployment_v2 import deployment_case, deploy
from test_team_execution_pool_v2 import SyntheticAdapter, candidate
from test_team_responsibility_v2 import initialize, mission_payload


def arranged(case, tmp_path, adapter, *, autonomous=True, preference=None, parallel=1):
    runtime, owner, registry, service, run_id, seed = case
    deployment = registry.configure(owner, run_id, preferences={'producer': preference or dict(mode='fixed', candidates=['fixture/model-a'])},
                    max_parallel=parallel, coordinator=dict(mode='fixed', candidates=['fixture/model-a']), coordinator_budget=200,
                    control_epoch=1, command_id='configure')
    active = runtime.activate(owner, run_id, bindings=deployment['bindings'], inputs={'request':seed['version_id']}, budget_limit=1000,
                                expected_version=1, control_epoch=1, command_id='activate')
    initialize(runtime, owner, active)
    MissionService(runtime).configure(owner, run_id, policy=dict(autonomous=autonomous, internal_replan=True, max_parallel=parallel,
                    automatic_receipts=True, automatic_verification=True, root_validator='fixture-root'),
                    expected_version=1, control_epoch=1, command_id='policy')
    pool = ExecutionPool(runtime, ToolBroker(runtime, tmp_path))
    for model in ('model-a','model-b'):
        descriptor = registry._catalog(owner.project_id)['fixture/'+model].model_copy(update={'context_limit_bytes':100000})
        pool.register(owner, descriptor, adapter, command_id='adapter-'+model)
    checks = VerificationRegistry()
    checks.register('software-check/v1', candidate=lambda p:dict(accepted=True, scores=[dict(criterion_id='valid',score=10000,evidence='Synthetic fixture exact result checked')],detail='Synthetic validator, not a real provider acceptance'))
    checks.register_goal('fixture-root', lambda p:dict(accepted=True,scores=[dict(criterion_id='delivery',score=10000,evidence='All synthetic current outputs independently checked')],detail='Synthetic root oracle inspected current artifacts'))
    return Coordinator(service,pool,checks), active


@pytest.mark.asyncio
async def test_root_model_answers_internal_question_using_supplied_evidence(deployment_case, tmp_path):
    runtime, owner = deployment_case[:2]
    first = True
    calls = []
    class Adapter:
        async def complete(self, context, **options):
            nonlocal first
            calls.append((context, options))
            if options.get('purpose') == 'coordinator':
                task = next(t for t in context['tasks'] if t['block_reason']=='internal_question')
                return ModelReply(json.dumps(dict(action='answer_internal', reason='The bound source already supplies the needed test information',
                                  task_id=task['task_id'],answer='Use the exact bound fixture input',evidence_refs=[context['supplied_evidence_refs'][0]])),20)
            if first:
                first=False
                return ModelReply(json.dumps(dict(action='question',detail='Which fixture input should I use?')),20)
            return ModelReply(candidate(context),20)
    coordinator, active = arranged(deployment_case,tmp_path,Adapter())
    result = await coordinator.run_until_idle(active['team_run_id'],control_epoch=1)
    assert result['completed'],result
    assert len([o for _,o in calls if o.get('purpose')=='coordinator'])==1
    assert runtime.db.execute('SELECT COUNT(*) FROM team_v2_coord_advice').fetchone()[0]==1
    assert runtime.db.execute('SELECT COUNT(*) FROM team_v2_questions').fetchone()[0]==0
    assert runtime.snapshot(owner,active['team_run_id'])['budget']['used_tokens']==80
    assert any(c.get('coordinator_advice') for c,_ in calls)


@pytest.mark.asyncio
async def test_model_interpretation_cannot_apply_a_control_change(deployment_case,tmp_path):
    runtime,owner=deployment_case[:2]
    class Adapter:
        async def complete(self,context,**options):
            target=next(t for t in context['tasks'] if t['task_key']=='second')
            return ModelReply(json.dumps(dict(intent='change_task',explanation='The user asks to narrow the second task, not cancel the run',
                             task_id=target['task_id'],new_objective='Only validate the final fixture section')),20)
    coordinator,active=arranged(deployment_case,tmp_path,Adapter(),autonomous=False)
    run_id=active['team_run_id']
    message='不要取消运行，请把 second 的目标改成只检查最后一节'
    pending=InterventionService(runtime).submit(owner,run_id,request=dict(intent='auto',message=message,
                          expected_goal_version=1,expected_plan_revision=1),control_epoch=1,command_id='message')
    before=runtime.task(owner,active['task_ids']['second'])
    interpreter=InterventionInterpreter(coordinator)
    result=await interpreter.interpret(run_id,intervention_id=pending['intervention_id'],control_epoch=1,command_id='interpret')
    assert result['state']=='pending' and not result['control_change_applied']
    assert result['request']['message']==message and result['request']['confirmed'] is False
    assert runtime.task(owner,before['task_id'])==before
    repeat=await interpreter.interpret(run_id,intervention_id=pending['intervention_id'],control_epoch=1,command_id='interpret')
    assert repeat['state']=='pending'
    assert runtime.db.execute('SELECT COUNT(*) FROM team_v2_coord_model_calls').fetchone()[0]==1
    InterventionService(runtime).apply(owner,pending['intervention_id'],approved=True,control_epoch=1,command_id='explicit-apply')
    assert runtime.task(owner,before['task_id'])['revision']==2


@pytest.mark.asyncio
async def test_interpreter_rejects_a_model_forged_approval(deployment_case,tmp_path):
    runtime,owner=deployment_case[:2]
    class Adapter:
        async def complete(self,context,**options):
            return ModelReply(json.dumps(dict(intent='cancel',explanation='Forged permission',approved=True)),20)
    coordinator,active=arranged(deployment_case,tmp_path,Adapter(),autonomous=False)
    run_id=active['team_run_id']
    pending=InterventionService(runtime).submit(owner,run_id,request=dict(intent='auto',message='先讨论方案，不要取消任务',expected_goal_version=1,expected_plan_revision=1),control_epoch=1,command_id='msg')
    with pytest.raises(ValueError):
        await InterventionInterpreter(coordinator).interpret(run_id,intervention_id=pending['intervention_id'],control_epoch=1,command_id='interpret')
    assert runtime._runtime_run(run_id)['state']=='running'
    assert InterventionService(runtime).list(owner,run_id)[0]['state']=='needs_clarification'
    assert runtime.db.execute('SELECT usage_tokens FROM team_v2_coord_model_calls').fetchone()[0]==20


@pytest.mark.asyncio
async def test_preferred_route_recovery_is_a_checked_new_plan(deployment_case,tmp_path):
    runtime,owner,registry=deployment_case[:3]
    coordinator,active=arranged(deployment_case,tmp_path,SyntheticAdapter(),preference=dict(mode='preferred',candidates=['fixture/model-a','fixture/model-b']))
    registry.register(owner,dict(provider_id='fixture',model_id='model-a',ready=False,reason='Simulated disconnection',max_instances=4),command_id='offline')
    result=await coordinator.run_until_idle(active['team_run_id'],control_epoch=1)
    assert result['completed'],result
    assert runtime._runtime_run(active['team_run_id'])['task_set_revision']==2
    assert all(b['model_id']=='model-b' for b in registry.get(active['team_run_id'])['bindings'] if b['kind']=='model')
    assert PlanService(deployment_case[3]).list(owner,active['team_run_id'])[0]['state']=='applied'


@pytest.mark.asyncio
async def test_fixed_route_recovery_never_substitutes(deployment_case,tmp_path):
    runtime,owner,registry=deployment_case[:3]
    coordinator,active=arranged(deployment_case,tmp_path,SyntheticAdapter())
    registry.register(owner,dict(provider_id='fixture',model_id='model-a',ready=False,reason='Disconnected'),command_id='offline')
    result=await coordinator.run_until_idle(active['team_run_id'],control_epoch=1)
    assert not result['completed'] and result['steps'][0]['reason']=='FIXED_MODEL_UNAVAILABLE'
    assert runtime.db.execute('SELECT COUNT(*) FROM team_v2_attempts').fetchone()[0]==0


def test_explicit_fixed_model_change_fences_old_instance(deployment_case,tmp_path):
    runtime,owner,registry,service=deployment_case[:4]
    coordinator,active=arranged(deployment_case,tmp_path,SyntheticAdapter())
    old=next(b for b in registry.get(active['team_run_id'])['bindings'] if b['kind']=='model')
    actor=TeamActor(old['principal_ref'],owner.project_id,frozenset({'team.read','team.execute'}))
    task=runtime.task(owner,active['task_ids']['produce'])
    claim=runtime.claim(actor,task['task_id'],worker_session_id='old-route',expected_version=task['state_version'],command_id='claim')
    raw=runtime._compiled(active['team_run_id']).spec.model_dump(mode='json');raw['version']+=1
    plans=PlanService(service)
    proposal=plans.propose(owner,active['team_run_id'],definition=raw,assignments=MissionService(runtime).view(owner,active['team_run_id'])['assignments'],
                preferences={'producer':dict(mode='fixed',candidates=['fixture/model-b'])},reason='User explicitly requests another fixed model',
                expected_goal_version=1,expected_plan_revision=1,control_epoch=1,command_id='proposal')
    assert proposal['impact']['rebound_roles']==['producer']
    with pytest.raises(TeamStateError,match='APPROVAL_REQUIRED'):
        plans.apply(owner,proposal['proposal_id'],approved=False,internal=True,control_epoch=1,command_id='not-user')
    plans.apply(owner,proposal['proposal_id'],approved=True,control_epoch=1,command_id='user-approved')
    assert runtime.task(owner,task['task_id'])['revision']==2
    assert runtime.task(owner,task['task_id'])['held_tokens']==claim['reserved_tokens']
    with pytest.raises(TeamStateError):
        runtime.heartbeat(actor,claim['token'],command_id='late')


def test_model_capacity_is_shared_across_runs(deployment_case):
    runtime,owner,registry,service,run_id,_=deployment_case
    _,first,actors=deploy(deployment_case)
    registry.register(owner,dict(provider_id='fixture',model_id='model-a',ready=True,reason='Two physical fixture slots',max_instances=2),command_id='capacity')
    compiled=runtime._compiled(run_id)
    team=service.create_team(SpecRef(spec_id=compiled.spec.spec_id,version=1),name='Other run',command_id='other-team')
    planned=service.plan_run(team['team_id'],objective='Another bounded batch',budget_ref='other-budget',expected_version=1,control_epoch=1,command_id='other-run')
    other=planned['team_run_id']
    configured=registry.configure(owner,other,preferences={'producer':dict(mode='fixed',candidates=['fixture/model-a'])},max_parallel=1,control_epoch=1,command_id='other-config')
    seed=runtime.register_input(owner,other,ArtifactContent.from_bytes(port_id='request',schema_ref='software-artifact/v1',media_type='text/plain',content=b'other fixture'),control_epoch=1,command_id='other-seed')
    second=runtime.activate(owner,other,bindings=configured['bindings'],inputs={'request':seed['version_id']},budget_limit=1000,expected_version=1,control_epoch=1,command_id='other-active')
    other_actor=TeamActor(next(b['principal_ref'] for b in configured['bindings'] if b['kind']=='model'),owner.project_id,frozenset({'team.read','team.execute'}))
    for index,(agent,task_id) in enumerate(((actors[0],first['task_ids']['produce']),(other_actor,second['task_ids']['produce']))):
        task=runtime.task(owner,task_id);runtime.claim(agent,task_id,worker_session_id='physical-'+str(index),expected_version=task['state_version'],command_id='physical-'+str(index))
    task=runtime.task(owner,first['task_ids']['second'])
    with pytest.raises(TeamStateError,match='WORKER_BUSY'):
        runtime.claim(actors[1],task['task_id'],worker_session_id='overflow',expected_version=task['state_version'],command_id='overflow')


def test_new_goal_clauses_require_and_accept_an_explicit_responsibility_map(deployment_case,tmp_path):
    runtime,owner=deployment_case[:2]
    _,active=arranged(deployment_case,tmp_path,SyntheticAdapter())
    goal=mission_payload();goal['success_criteria'].append(dict(criterion_id='traceability',description='All sources are traceable'))
    request=dict(intent='change_goal',message='Add explicit traceability acceptance',expected_goal_version=1,expected_plan_revision=1,mission=goal,confirmed=True)
    with pytest.raises(TeamStateError,match='GOAL_COVERAGE_INCOMPLETE'):
        InterventionService(runtime).submit(owner,active['team_run_id'],request=request,control_epoch=1,command_id='no-map')
    request['assignments']=[dict(task_key=t,owner_role='producer',goal_criteria=['delivery','traceability']) for t in ('produce','second')]
    result=InterventionService(runtime).submit(owner,active['team_run_id'],request=request,control_epoch=1,command_id='complete-map')
    assert result['receipt']['goal_version']==2
