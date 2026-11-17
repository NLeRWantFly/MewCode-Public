import copy
import json

import pytest

from mewcode.mcp.coord_store import CoordinationStore
from mewcode.teams.disputes import DisputeService
from mewcode.teams.responsibility import MissionService
from mewcode.teams.runtime_protocol import byte_hash
from mewcode.teams.store_v2 import TeamActor, TeamStateError
from test_team_runtime_v2 import runtime_case, runtime_fixture, submit_candidate, review_candidate


def mission_payload():
    return dict(objective='Deliver two verified stages', hard_constraints=['Keep original sources', 'Do not invent facts'],
                success_criteria=[dict(criterion_id='delivery', description='Both stages satisfy the supplied request')])


def assignments():
    return [dict(task_key=key, owner_role='producer', goal_criteria=['delivery']) for key in ('produce', 'second')]


def initialize(runtime, owner, active):
    return MissionService(runtime).initialize(owner, active['team_run_id'], contract=mission_payload(),
                                             assignments=assignments(), control_epoch=1, command_id='mission')


def open_dispute(runtime, owner, active):
    return DisputeService(runtime).open(owner, active['team_run_id'], submission=dict(
        task_id=active['task_ids']['produce'], revision=1, criterion_id='valid', owner_role='producer',
        issue_key='unit-mismatch', problem='Unit differs from the source', reproduction='Compare the source unit and output unit'),
        command_id='dispute')


def evidence(source='source:units', detail='Source uses milliseconds'):
    return dict(kind='observation', source_ref=source, source_hash=byte_hash(source.encode()), detail=detail,
                task_revision=1, verification='Read the exact source bytes independently')


def test_mission_preserves_frozen_spec_and_scopes_context(runtime_case):
    _, runtime, owner, worker, active, _ = runtime_case
    compiled_before = runtime._compiled(active['team_run_id']).model_dump_json()
    initialize(runtime, owner, active)
    service = MissionService(runtime)
    view = service.view(owner, active['team_run_id'])
    assert view['owner_ref'] == 'owner' and view['version'] == 1
    assert len(view['assignments']) == 2
    assert runtime._compiled(active['team_run_id']).model_dump_json() == compiled_before
    context = service.context(worker, active['task_ids']['produce'])
    assert context['responsibility']['task_key'] == 'produce'
    assert 'history' not in context and 'assignments' not in context
    with pytest.raises(TeamStateError, match='PERMISSION_REQUIRED'):
        service.view(worker, active['team_run_id'])


@pytest.mark.parametrize('change,code', [
    (lambda a: a.pop(), 'RESPONSIBILITY_INCOMPLETE'),
    (lambda a: a[0].update(owner_role='reviewer'), 'RESPONSIBILITY_ROLE_MISMATCH'),
    (lambda a: a[0].update(goal_criteria=['invented']), 'GOAL_CRITERION_MISMATCH'),
    (lambda a: a[0].update(parent_task_key='produce'), 'RESPONSIBILITY_CYCLE'),
])
def test_invalid_responsibility_is_atomic(runtime_case, change, code):
    _, runtime, owner, _, active, _ = runtime_case
    values = assignments()
    change(values)
    with pytest.raises(TeamStateError, match=code):
        MissionService(runtime).initialize(owner, active['team_run_id'], contract=mission_payload(), assignments=values,
                                           control_epoch=1, command_id='invalid-mission')
    assert MissionService(runtime).view(owner, active['team_run_id']) is None


def test_mission_policy_change_is_cas_and_not_execution(runtime_case):
    _, runtime, owner, _, active, _ = runtime_case
    initialize(runtime, owner, active)
    service = MissionService(runtime)
    result = service.configure(owner, active['team_run_id'], policy={'max_parallel': 4}, expected_version=1,
                               control_epoch=1, command_id='policy')
    assert result['execution_started'] is False
    assert service.policy(active['team_run_id']).max_parallel == 4
    with pytest.raises(TeamStateError, match='STATE_VERSION_CONFLICT'):
        service.configure(owner, active['team_run_id'], policy={}, expected_version=1, control_epoch=1, command_id='stale')


def test_dispute_dedup_keeps_responsibility_and_task_state(runtime_case):
    _, runtime, owner, worker, active, _ = runtime_case
    initialize(runtime, owner, active)
    first = open_dispute(runtime, owner, active)
    service = DisputeService(runtime)
    submission = service.get(owner, first['dispute_id'])['submission']
    submission['problem'] = 'The same mismatch, worded differently'
    again = service.open(worker, active['team_run_id'], submission=submission, command_id='dispute-again')
    assert again['duplicate'] and again['dispute_id'] == first['dispute_id']
    assert runtime.task(owner, active['task_ids']['produce'])['state'] == 'queued'
    submission['owner_role'] = 'reviewer'
    with pytest.raises(TeamStateError, match='RESPONSIBILITY_TRANSFER_DENIED'):
        service.open(worker, active['team_run_id'], submission=submission, command_id='kick-back')


def test_rephrasing_and_unverified_evidence_are_not_progress(runtime_case):
    _, runtime, owner, worker, active, _ = runtime_case
    initialize(runtime, owner, active)
    dispute = open_dispute(runtime, owner, active)
    service, key = DisputeService(runtime), dispute['dispute_id']
    added = service.add_evidence(worker, key, evidence=evidence(), expected_version=1, command_id='ev')
    duplicate = service.add_evidence(worker, key, evidence=evidence(detail='Rephrasing the same observation'),
                                     expected_version=2, command_id='ev-again')
    assert duplicate['duplicate'] and duplicate['evidence_id'] == added['evidence_id']
    one = service.record_round(owner, key, evidence_ids=[added['evidence_id']], action_taken='Compare the input',
                                result='An unverified assertion', expected_version=2, control_epoch=1, command_id='round-1')
    assert one['progress'] is False and one['ask_user'] is False
    with pytest.raises(TeamStateError, match='PERMISSION_REQUIRED'):
        service.verify_evidence(worker, key, evidence_id=added['evidence_id'], expected_version=3,
                                 control_epoch=1, command_id='self-verification')
    service.verify_evidence(owner, key, evidence_id=added['evidence_id'], expected_version=3, control_epoch=1, command_id='verified')
    two = service.record_round(owner, key, evidence_ids=[added['evidence_id']], action_taken='Read original bytes',
                                result='Independent source check passed', expected_version=4, control_epoch=1, command_id='round-2')
    assert two['progress'] is True
    three = service.record_round(owner, key, evidence_ids=[added['evidence_id']], action_taken='Repeat the source check',
                                  result='No changed bytes', expected_version=5, control_epoch=1, command_id='round-3')
    assert three['progress'] is False


def test_no_progress_triggers_replan_not_completion_or_user_escalation(runtime_case):
    _, runtime, owner, _, active, _ = runtime_case
    initialize(runtime, owner, active)
    key = open_dispute(runtime, owner, active)['dispute_id']
    service = DisputeService(runtime)
    for version in range(1, 4):
        result = service.record_round(owner, key, evidence_ids=[], action_taken='Reword request', result='No new facts',
                                       expected_version=version, control_epoch=1, command_id=f'round-{version}')
    assert result['state'] == 'replanning'
    assert not result['ask_user'] and not result['task_completed']
    with pytest.raises(TeamStateError, match='CURRENT_EVIDENCE_REQUIRED'):
        service.resolve(owner, key, outcome='resolved', evidence_ids=[], explanation='Stop after three rounds',
                         expected_version=4, control_epoch=1, command_id='fake-finished')


def test_resolution_requires_current_independent_evidence_and_does_not_complete_task(runtime_case):
    _, runtime, owner, worker, active, _ = runtime_case
    initialize(runtime, owner, active)
    key = open_dispute(runtime, owner, active)['dispute_id']
    service = DisputeService(runtime)
    added = service.add_evidence(worker, key, evidence=evidence(), expected_version=1, command_id='evidence')
    service.verify_evidence(owner, key, evidence_id=added['evidence_id'], expected_version=2, control_epoch=1, command_id='verify')
    done = service.resolve(owner, key, outcome='resolved', evidence_ids=[added['evidence_id']], explanation='The source confirms the unit',
                            expected_version=3, control_epoch=1, command_id='resolve')
    assert done['state'] == 'resolved' and done['task_completed'] is False
    assert runtime.task(owner, active['task_ids']['produce'])['state'] == 'queued'


def test_user_decision_requires_concrete_conflicting_goal_clauses(runtime_case):
    _, runtime, owner, worker, active, _ = runtime_case
    initialize(runtime, owner, active)
    key = open_dispute(runtime, owner, active)['dispute_id']
    service = DisputeService(runtime)
    added = service.add_evidence(worker, key, evidence=evidence(), expected_version=1, command_id='ev')
    service.verify_evidence(owner, key, evidence_id=added['evidence_id'], expected_version=2, control_epoch=1, command_id='ver')
    args = dict(outcome='decision_required', evidence_ids=[added['evidence_id']], explanation='Need an owner decision',
                expected_version=3, control_epoch=1, choices=['Provide a corrected source', 'Change the deliverable'])
    with pytest.raises(TeamStateError, match='CONCRETE_DECISION_REQUIRED'):
        service.resolve(owner, key, **args, conflicting_constraints=['Something is hard'], command_id='vague')
    result = service.resolve(owner, key, **args, conflicting_constraints=['Keep original sources', 'Do not invent facts'], command_id='concrete')
    assert result['user_action_required'] and not result['task_completed']


def test_goal_acceptance_is_separate_and_version_bound(runtime_case):
    _, runtime, owner, worker, active, schema = runtime_case
    initialize(runtime, owner, active)
    first, second = active['task_ids']['produce'], active['task_ids']['second']
    _, _, result = submit_candidate(runtime, worker, first, schema)
    review_candidate(runtime, owner, result)
    runtime.accept_input(owner, second, 'request', result['artifacts'][0]['version_id'], revision=1, accepted=True,
                         reason='Compatible with the second contract', control_epoch=1, command_id='receipt')
    _, _, result = submit_candidate(runtime, worker, second, schema, 'two')
    review_candidate(runtime, owner, result, 'two')
    run_id = active['team_run_id']
    version = runtime._runtime_run(run_id)['state_version']
    with pytest.raises(TeamStateError, match='GOAL_REVIEW_REQUIRED'):
        runtime.finalize(owner, run_id, expected_version=version, control_epoch=1, evidence='Task checks only', command_id='early')
    MissionService(runtime).review_goal(owner, run_id, scores=[dict(criterion_id='delivery', score=10000, evidence='Both actual outputs fulfill the root request')],
                                        accepted=True, expected_goal_version=1, expected_plan_revision=1, control_epoch=1, command_id='goal-review')
    assert runtime.finalize(owner, run_id, expected_version=version, control_epoch=1, evidence='Accepted root goal', command_id='finish')['state'] == 'completed'


def test_mission_dispute_and_evidence_survive_reopen(tmp_path):
    path = tmp_path / 'coord.sqlite'
    with CoordinationStore(path, enable_team_v2=True) as store:
        runtime, owner, worker, active, _ = runtime_fixture(store)
        initialize(runtime, owner, active)
        dispute = open_dispute(runtime, owner, active)
    with CoordinationStore(path, enable_team_v2=True) as store:
        runtime = store.teams_v2.runtime
        assert MissionService(runtime).view(owner, active['team_run_id'])['version'] == 1
        assert DisputeService(runtime).get(owner, dispute['dispute_id'])['owner_role'] == 'producer'


def test_cross_project_dispute_access_denied(runtime_case):
    _, runtime, owner, _, active, _ = runtime_case
    key = open_dispute(runtime, owner, active)['dispute_id']
    stranger = TeamActor('owner', 'other-project', owner.permissions)
    with pytest.raises(TeamStateError, match='TEAM_NOT_ACCESSIBLE'):
        DisputeService(runtime).get(stranger, key)
