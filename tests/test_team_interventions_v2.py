import copy

import pytest

from mewcode.teams.interventions import InterventionService, dependency_closure
from mewcode.teams.responsibility import MissionService
from mewcode.teams.store_v2 import TeamStateError
from test_team_runtime_v2 import runtime_case
from test_team_responsibility_v2 import initialize, mission_payload


def request(intent, **overrides):
    return dict(intent=intent, message='Please apply this scoped instruction', expected_goal_version=1,
                expected_plan_revision=1, **overrides)


def test_question_returns_persisted_facts_without_changing_work(runtime_case):
    _, runtime, owner, _, active, _ = runtime_case
    initialize(runtime, owner, active)
    run_id = active['team_run_id']
    before = runtime.snapshot(owner, run_id)
    result = InterventionService(runtime).submit(owner, run_id, request=request('question'), control_epoch=1, command_id='question')
    assert result['state'] == 'answered'
    assert result['receipt']['answer']['tasks'] == 2
    assert result['receipt']['effective'] is False
    assert runtime.snapshot(owner, run_id)['tasks'] == before['tasks']


def test_unknown_text_is_not_treated_as_a_control_command(runtime_case):
    _, runtime, owner, _, active, _ = runtime_case
    initialize(runtime, owner, active)
    values = request('auto', confirmed=True)
    values['message'] = 'Do not cancel the task; continue analyzing its risks'
    result = InterventionService(runtime).submit(owner, active['team_run_id'], request=values, control_epoch=1, command_id='ambiguous')
    assert result['state'] == 'needs_clarification' and not result['receipt']['effective']
    assert runtime._runtime_run(active['team_run_id'])['state'] == 'running'


def test_information_is_data_not_authority_and_is_task_scoped(runtime_case):
    _, runtime, owner, worker, active, _ = runtime_case
    initialize(runtime, owner, active)
    service = InterventionService(runtime)
    task_id = active['task_ids']['produce']
    result = service.submit(owner, active['team_run_id'], request=request('information', task_id=task_id, task_revision=1),
                             control_epoch=1, command_id='info')
    assert result['state'] == 'available'
    values = service.information_for_task(worker, task_id)
    assert len(values) == 1 and values[0]['trust'] == 'data_only'
    assert service.information_for_task(worker, active['task_ids']['second']) == []


def test_local_change_preserves_unrelated_work_and_rejects_stale_apply(runtime_case):
    _, runtime, owner, _, active, _ = runtime_case
    initialize(runtime, owner, active)
    service = InterventionService(runtime)
    first, second = active['task_ids']['produce'], active['task_ids']['second']
    before = runtime.task(owner, first)
    contract = copy.deepcopy(runtime.task(owner, second)['contract'])
    contract['objective'] = 'Only revise the final section'
    result = service.submit(owner, active['team_run_id'], request=request('change_task', task_id=second, task_revision=1,
                             contract=contract), control_epoch=1, command_id='change')
    assert result['state'] == 'pending'
    assert result['receipt']['affected_task_ids'] == [second]
    applied = service.apply(owner, result['intervention_id'], approved=True, control_epoch=1, command_id='apply')
    assert applied['receipt']['effective'] is True
    assert runtime.task(owner, second)['revision'] == 2
    assert runtime.task(owner, first) == before
    with pytest.raises(TeamStateError, match='INTERVENTION_STALE'):
        service.submit(owner, active['team_run_id'], request=request('information', task_id=second, task_revision=1),
                        control_epoch=1, command_id='stale-info')


def test_dependency_impact_includes_unmaterialized_downstream(runtime_case):
    _, runtime, _, _, active, _ = runtime_case
    assert dependency_closure(runtime, active['team_run_id'], active['task_ids']['produce']) == [
        active['task_ids']['produce'], active['task_ids']['second']]


def test_changed_task_fences_running_attempt_and_keeps_unknown_usage(runtime_case):
    _, runtime, owner, worker, active, _ = runtime_case
    initialize(runtime, owner, active)
    task_id = active['task_ids']['produce']
    task = runtime.task(owner, task_id)
    claimed = runtime.claim(worker, task_id, worker_session_id='before-change', expected_version=task['state_version'], command_id='claim')
    contract = copy.deepcopy(task['contract'])
    contract['objective'] = 'A narrower deliverable'
    result = InterventionService(runtime).submit(owner, active['team_run_id'], request=request('change_task', task_id=task_id,
                    task_revision=1, contract=contract, confirmed=True), control_epoch=1, command_id='intervene')
    assert result['state'] == 'applied'
    assert runtime.task(owner, task_id)['held_tokens'] == claimed['reserved_tokens']
    assert runtime.task(owner, task_id)['block_reason'] == 'effect_unknown'
    assert runtime.task(owner, active['task_ids']['second'])['revision'] == 2
    with pytest.raises(TeamStateError, match='STALE_FENCE'):
        runtime.heartbeat(worker, claimed['token'], command_id='late-heartbeat')


def test_acceptance_cannot_be_weakened_by_local_redirection(runtime_case):
    _, runtime, owner, _, active, _ = runtime_case
    initialize(runtime, owner, active)
    task_id = active['task_ids']['produce']
    contract = copy.deepcopy(runtime.task(owner, task_id)['contract'])
    contract['acceptance'][0]['minimum_score'] = 0
    with pytest.raises(TeamStateError, match='ACCEPTANCE_WEAKENED'):
        InterventionService(runtime).submit(owner, active['team_run_id'], request=request('change_task', task_id=task_id,
                    task_revision=1, contract=contract, confirmed=True), control_epoch=1, command_id='weaken')
    assert InterventionService(runtime).list(owner, active['team_run_id']) == []


def test_goal_change_creates_history_and_expires_old_input_directives(runtime_case):
    _, runtime, owner, worker, active, _ = runtime_case
    initialize(runtime, owner, active)
    service, run_id = InterventionService(runtime), active['team_run_id']
    service.submit(owner, run_id, request=request('information'), control_epoch=1, command_id='old-info')
    goal = mission_payload()
    goal['objective'] = 'A newly authorized objective'
    result = service.submit(owner, run_id, request=request('change_goal', mission=goal, confirmed=True), control_epoch=1, command_id='new-goal')
    assert result['receipt']['goal_version'] == 2
    mission = MissionService(runtime).view(owner, run_id)
    assert [r['version'] for r in mission['history']] == [1, 2]
    assert service.information_for_task(worker, active['task_ids']['produce']) == []
    assert runtime.snapshot(owner, run_id)['run']['objective'] == goal['objective']


def test_goal_change_without_exact_contract_is_atomic(runtime_case):
    _, runtime, owner, _, active, _ = runtime_case
    initialize(runtime, owner, active)
    with pytest.raises(TeamStateError, match='GOAL_CHANGE_REQUIRED'):
        InterventionService(runtime).submit(owner, active['team_run_id'], request=request('change_goal', confirmed=True),
                                             control_epoch=1, command_id='missing-contract')
    assert MissionService(runtime).view(owner, active['team_run_id'])['version'] == 1


def test_pending_intervention_can_be_dismissed_explicitly(runtime_case):
    _, runtime, owner, _, active, _ = runtime_case
    initialize(runtime, owner, active)
    service = InterventionService(runtime)
    pending = service.submit(owner, active['team_run_id'], request=request('pause'), control_epoch=1, command_id='pause-request')
    result = service.dismiss(owner, pending['intervention_id'], reason='Keep the current direction', control_epoch=1, command_id='dismiss')
    assert result['state'] == 'dismissed' and result['receipt']['effective'] is False


def test_worker_cannot_change_goal(runtime_case):
    _, runtime, owner, worker, active, _ = runtime_case
    initialize(runtime, owner, active)
    with pytest.raises(TeamStateError, match='PERMISSION_REQUIRED'):
        InterventionService(runtime).submit(worker, active['team_run_id'], request=request('change_goal', confirmed=True,
                                               mission=mission_payload()), control_epoch=1, command_id='worker-goal')
