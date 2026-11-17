import base64
import json

import pytest

from mewcode.teams.responsibility_coordinator import Coordinator, VerificationRegistry
from mewcode.teams.execution_pool import ExecutionPool, ModelReply
from mewcode.teams.responsibility import MissionService
from mewcode.teams.tool_broker import ToolBroker
from mewcode.teams.store_v2 import TeamStateError
from test_team_runtime_v2 import runtime_case
from test_team_responsibility_v2 import initialize
from test_team_dynamic_plans_v2 import planner
from test_team_execution_pool_v2 import SyntheticAdapter, candidate


def checked(payload):
    valid = all(base64.b64decode(a['content_b64']) == b'fixture result' for a in payload['artifacts'])
    return dict(accepted=valid, scores=[dict(criterion_id=c['criterion_id'], score=10000 if valid else 0,
                evidence='Compared exact immutable bytes with the test fixture oracle') for c in payload['task']['contract']['acceptance']],
                detail='Registered fixture validator read the actual candidate bytes')


def coordinator(case, tmp_path, adapter=None, *, root=True):
    _, runtime, owner, _, active, _ = case
    initialize(runtime, owner, active)
    MissionService(runtime).configure(owner, active['team_run_id'], policy=dict(autonomous=True, automatic_receipts=True,
                         automatic_verification=True, root_validator='fixture-root' if root else None),
                         expected_version=1, control_epoch=1, command_id='policy')
    pool = ExecutionPool(runtime, ToolBroker(runtime, tmp_path))
    pool.register(owner, dict(provider_id='grok-build', model_id='grok-4.6', ready=True, reason='Synthetic test adapter'),
                   adapter or SyntheticAdapter(), command_id='adapter')
    validators = VerificationRegistry()
    validators.register('software-check/v1', candidate=checked, input_check=lambda p: dict(accepted=base64.b64decode(p['artifact']['content_b64']) == b'fixture result',
                                                   reason='Exact fixture bytes are compatible with this next-stage input'))
    validators.register_goal('fixture-root', lambda p: dict(accepted=len(p['artifacts']) == 2,
                          scores=[dict(criterion_id='delivery', score=10000 if len(p['artifacts']) == 2 else 0,
                                       evidence='Independently checked the two-stage fixture result set')], detail='All current required fixture stages and root goal checked'))
    return Coordinator(planner(runtime, owner).service, pool, validators)


@pytest.mark.asyncio
async def test_full_two_stage_loop_receipts_independent_checks_and_root_closure(runtime_case, tmp_path):
    _, runtime, owner, _, active, _ = runtime_case
    host = coordinator(runtime_case, tmp_path)
    result = await host.run_until_idle(active['team_run_id'], control_epoch=1)
    assert result['completed'], result
    assert len(result['steps']) == 2
    assert all(t['state'] == 'completed' for t in runtime.snapshot(owner, active['team_run_id'])['tasks'])
    assert runtime.db.execute('SELECT COUNT(*) FROM team_v2_goal_reviews WHERE accepted=1').fetchone()[0] == 1
    assert runtime.db.execute('SELECT COUNT(*) FROM team_v2_receipts WHERE accepted=1').fetchone()[0] == 1


@pytest.mark.asyncio
async def test_root_acceptance_is_not_inferred_from_all_children_passing(runtime_case, tmp_path):
    _, runtime, _, _, active, _ = runtime_case
    host = coordinator(runtime_case, tmp_path, root=False)
    result = await host.run_until_idle(active['team_run_id'], control_epoch=1)
    assert not result['completed'] and result['state'] == 'verifying'
    assert result['steps'][-1]['root']['reason'] == 'ROOT_REVIEW_REQUIRED'
    assert runtime.db.execute('SELECT COUNT(*) FROM team_v2_goal_reviews').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_repair_stays_with_owner_and_uses_verified_dispute_feedback(runtime_case, tmp_path):
    _, runtime, owner, _, active, _ = runtime_case
    first = True
    async def repair(context):
        nonlocal first
        value = json.loads(candidate(context))
        if first:
            first = False
            value['submission']['artifacts'][0]['content_b64'] = base64.b64encode(b'incorrect fixture').decode()
        elif context['contract']['task_key'] == 'produce':
            assert context['verified_disputes'] and context['verified_disputes'][0]['evidence'][0]['verified_by'] == 'owner'
        return ModelReply(json.dumps(value), 20)
    host = coordinator(runtime_case, tmp_path, SyntheticAdapter(repair))
    result = await host.run_until_idle(active['team_run_id'], control_epoch=1)
    assert result['completed'], result
    disputes = host.disputes.inbox(owner, active['team_run_id'])
    assert len(disputes) == 1 and disputes[0]['state'] == 'resolved'
    assert disputes[0]['owner_role'] == 'producer'
    assert runtime.db.execute('SELECT COUNT(*) FROM team_v2_attempts').fetchone()[0] == 3
    assert runtime.db.execute('SELECT COUNT(*) FROM team_v2_questions').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_disabled_autonomy_never_starts_a_model(runtime_case, tmp_path):
    _, runtime, owner, _, active, _ = runtime_case
    host = coordinator(runtime_case, tmp_path)
    MissionService(runtime).configure(owner, active['team_run_id'], policy={}, expected_version=2, control_epoch=1, command_id='manual')
    with pytest.raises(TeamStateError, match='AUTONOMY_NOT_GRANTED'):
        await host.step(active['team_run_id'], control_epoch=1, command_id='denied')
    assert runtime.db.execute('SELECT COUNT(*) FROM team_v2_attempts').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_coordinator_step_replay_does_not_repeat_model_execution(runtime_case, tmp_path):
    _, runtime, _, _, active, _ = runtime_case
    adapter = SyntheticAdapter()
    host = coordinator(runtime_case, tmp_path, adapter)
    first = await host.step(active['team_run_id'], control_epoch=1, command_id='same-step')
    replay = await host.step(active['team_run_id'], control_epoch=1, command_id='same-step')
    assert first == replay and len(adapter.contexts) == 1


@pytest.mark.asyncio
async def test_validator_cannot_accept_with_invented_criterion(runtime_case, tmp_path):
    _, runtime, owner, _, active, _ = runtime_case
    host = coordinator(runtime_case, tmp_path)
    host.validators.register('software-check/v1', candidate=lambda p: dict(accepted=True, scores=[dict(criterion_id='invented', score=10000, evidence='Untrusted criteria')], detail='Invalid fixture validator'))
    result = await host.step(active['team_run_id'], control_epoch=1, command_id='bad-validator')
    assert not result['completed'] and result['reason'] == 'REVIEW_CRITERIA_MISMATCH'
    assert runtime.task(owner, active['task_ids']['produce'])['state'] == 'verifying'
