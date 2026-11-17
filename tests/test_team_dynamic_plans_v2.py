import copy

import pytest

from mewcode.teams.dynamic_plans import PlanService
from mewcode.teams.responsibility import MissionService
from mewcode.teams.service import TeamService
from mewcode.teams.spec_compiler import CompilationContext, SpecCompilationError
from mewcode.teams.store_v2 import TeamStateError
from test_team_runtime_v2 import runtime_case, submit_candidate, review_candidate
from test_team_responsibility_v2 import initialize, open_dispute


def planner(runtime, owner):
    compiled = runtime._compiled(runtime.db.execute('SELECT team_run_id FROM team_v2_runs').fetchone()[0])
    context = CompilationContext(compiled.effective_policy, compiled.effective_policy,
                                 frozenset({'software-artifact/v1'}), frozenset({'software-check/v1'}))
    return PlanService(TeamService(runtime.repo._store, actor=owner, compilation_context=context))


def assignments_for(raw):
    return [dict(task_key=t['task_key'], owner_role=t['role_id'], goal_criteria=['delivery']) for t in raw['tasks']]


def proposal(service, owner, run_id, raw, key='proposal'):
    return service.propose(owner, run_id, definition=raw, assignments=assignments_for(raw), reason='A bounded revised route',
                           expected_goal_version=1, expected_plan_revision=1, control_epoch=1, command_id=key)


def replacement(runtime, run_id):
    raw = runtime._compiled(run_id).spec.model_dump(mode='json')
    raw['version'] = 2
    second = copy.deepcopy(raw['tasks'][1])
    second['task_key'] = 'replacement'
    second['objective'] = 'An alternative final stage'
    raw['tasks'][1] = second
    for edge in raw['connections']:
        for endpoint in ('source', 'target'):
            if edge[endpoint].get('task_key') == 'second':
                edge[endpoint]['task_key'] = 'replacement'
    return raw


def test_replaces_branch_keeps_history_and_reuses_valid_upstream(runtime_case):
    _, runtime, owner, worker, active, schema = runtime_case
    initialize(runtime, owner, active)
    run_id = active['team_run_id']
    _, _, output = submit_candidate(runtime, worker, active['task_ids']['produce'], schema)
    review_candidate(runtime, owner, output)
    before = runtime.task(owner, active['task_ids']['produce'])
    service = planner(runtime, owner)
    proposed = proposal(service, owner, run_id, replacement(runtime, run_id))
    assert proposed['impact']['reused'] == ['produce']
    result = service.apply(owner, proposed['proposal_id'], approved=True, control_epoch=1, command_id='apply')
    snapshot = runtime.snapshot(owner, run_id)
    assert result['task_set_revision'] == 2
    assert {t['task_key'] for t in snapshot['tasks']} == {'produce', 'replacement'}
    assert [t['task_key'] for t in snapshot['historical_tasks']] == ['second']
    assert runtime.task(owner, before['task_id']) == before
    added = runtime.task(owner, result['added_task_ids']['replacement'])
    assert added['inputs'][0]['version_id'] == output['artifacts'][0]['version_id']
    assert added['inputs'][0]['receipt'] == 'pending'
    assert runtime.db.execute('SELECT COUNT(*) FROM team_v2_plan_revisions WHERE team_run_id=?', (run_id,)).fetchone()[0] == 2


def test_removed_required_task_no_longer_blocks_root_acceptance(runtime_case):
    _, runtime, owner, worker, active, schema = runtime_case
    initialize(runtime, owner, active)
    run_id = active['team_run_id']
    _, _, output = submit_candidate(runtime, worker, active['task_ids']['produce'], schema)
    review_candidate(runtime, owner, output)
    raw = runtime._compiled(run_id).spec.model_dump(mode='json')
    raw['version'] = 2
    raw['tasks'] = raw['tasks'][:1]
    raw['connections'] = [dict(source=dict(port_id='request'), target=dict(task_key='produce', port_id='request')),
                          dict(source=dict(task_key='produce', port_id='result'), target=dict(port_id='delivery'))]
    service = planner(runtime, owner)
    proposed = proposal(service, owner, run_id, raw)
    service.apply(owner, proposed['proposal_id'], approved=True, control_epoch=1, command_id='apply')
    assert runtime._runtime_run(run_id)['state'] == 'verifying'
    MissionService(runtime).review_goal(owner, run_id, scores=[dict(criterion_id='delivery', score=10000, evidence='The revised required output fulfills the root goal')],
                                        accepted=True, expected_goal_version=1, expected_plan_revision=2, control_epoch=1, command_id='root-review')
    current = runtime._runtime_run(run_id)
    assert runtime.finalize(owner, run_id, expected_version=current['state_version'], control_epoch=1,
                            evidence='Current root output verified', command_id='finalize')['state'] == 'completed'


def test_unknown_usage_in_removed_branch_still_blocks_completion(runtime_case):
    _, runtime, owner, worker, active, _ = runtime_case
    initialize(runtime, owner, active)
    run_id, first = active['team_run_id'], active['task_ids']['produce']
    task = runtime.task(owner, first)
    claim = runtime.claim(worker, first, worker_session_id='in-flight', expected_version=task['state_version'], command_id='claim')
    raw = runtime._compiled(run_id).spec.model_dump(mode='json')
    raw['version'] = 2
    raw['tasks'] = [dict(copy.deepcopy(raw['tasks'][0]), task_key='replacement')]
    raw['connections'] = [dict(source=dict(port_id='request'), target=dict(task_key='replacement', port_id='request')),
                          dict(source=dict(task_key='replacement', port_id='result'), target=dict(port_id='delivery'))]
    service = planner(runtime, owner)
    proposed = proposal(service, owner, run_id, raw)
    service.apply(owner, proposed['proposal_id'], approved=True, control_epoch=1, command_id='apply')
    assert runtime.task(owner, first)['held_tokens'] == claim['reserved_tokens']
    with pytest.raises(TeamStateError, match='STALE_FENCE'):
        runtime.heartbeat(worker, claim['token'], command_id='late')


def test_static_cycle_is_rejected_before_a_proposal_is_saved(runtime_case):
    _, runtime, owner, _, active, _ = runtime_case
    initialize(runtime, owner, active)
    service, run_id = planner(runtime, owner), active['team_run_id']
    raw = runtime._compiled(run_id).spec.model_dump(mode='json')
    raw['version'] = 2
    raw['connections'][0]['source'] = dict(task_key='second', port_id='result')
    with pytest.raises(SpecCompilationError, match='DEPENDENCY_CYCLE'):
        proposal(service, owner, run_id, raw)
    assert service.list(owner, run_id) == []


def test_plan_cannot_remove_an_existing_forbidden_change(runtime_case):
    _, runtime, owner, _, active, _ = runtime_case
    initialize(runtime, owner, active)
    service, run_id = planner(runtime, owner), active['team_run_id']
    raw = runtime._compiled(run_id).spec.model_dump(mode='json')
    raw['version'] = 2
    raw['tasks'][0]['forbidden_changes'] = ['A different unrelated restriction']
    with pytest.raises(TeamStateError, match='FORBIDDEN_SCOPE_WEAKENED'):
        proposal(service, owner, run_id, raw)


def test_affected_work_advancing_invalidates_the_impact_preview(runtime_case):
    _, runtime, owner, worker, active, _ = runtime_case
    initialize(runtime, owner, active)
    service, run_id = planner(runtime, owner), active['team_run_id']
    raw = runtime._compiled(run_id).spec.model_dump(mode='json')
    raw['version'] = 2
    raw['tasks'][0]['objective'] = 'A corrected implementation approach'
    proposed = proposal(service, owner, run_id, raw)
    first = runtime.task(owner, active['task_ids']['produce'])
    runtime.claim(worker, first['task_id'], worker_session_id='advanced', expected_version=first['state_version'], command_id='claim')
    with pytest.raises(TeamStateError, match='PLAN_PROPOSAL_STALE'):
        service.apply(owner, proposed['proposal_id'], approved=True, control_epoch=1, command_id='apply')
    assert runtime._compiled(run_id).spec.version == 1


def test_internal_plan_needs_policy_and_cannot_weaken_acceptance(runtime_case):
    _, runtime, owner, _, active, _ = runtime_case
    initialize(runtime, owner, active)
    service, run_id = planner(runtime, owner), active['team_run_id']
    raw = replacement(runtime, run_id)
    proposed = proposal(service, owner, run_id, raw)
    with pytest.raises(TeamStateError, match='APPROVAL_REQUIRED'):
        service.apply(owner, proposed['proposal_id'], approved=False, internal=True, control_epoch=1, command_id='internal-no-grant')
    MissionService(runtime).configure(owner, run_id, policy=dict(internal_replan=True), expected_version=1, control_epoch=1, command_id='allow-internal')
    assert service.apply(owner, proposed['proposal_id'], approved=False, internal=True, control_epoch=1, command_id='internal-granted')['state'] == 'applied'


def test_unaffected_progress_does_not_stale_an_independent_replacement(runtime_case):
    _, runtime, owner, worker, active, schema = runtime_case
    initialize(runtime, owner, active)
    service, run_id = planner(runtime, owner), active['team_run_id']
    proposed = proposal(service, owner, run_id, replacement(runtime, run_id))
    _, _, output = submit_candidate(runtime, worker, active['task_ids']['produce'], schema)
    review_candidate(runtime, owner, output)
    # Offering the new input does not change the removed consumer's state version.
    assert service.apply(owner, proposed['proposal_id'], approved=True, control_epoch=1, command_id='apply')['state'] == 'applied'
