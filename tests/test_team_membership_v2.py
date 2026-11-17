import pytest

from mewcode.teams.store_v2 import TeamStateError
from test_team_runtime_v2 import runtime_case


def member_state(runtime, owner, active, state, prefix):
    view = runtime.snapshot(owner, active['team_run_id'])
    member = next(m for m in view['members'] if m['role_id'] == 'producer')
    return runtime.set_member_state(owner, member['membership_id'], state=state, reason='Explicit host availability update', expected_version=view['team']['state_version'], control_epoch=1, command_id=prefix)


def test_idle_is_not_business_completion_and_leaving_fences_attempt(runtime_case):
    _, runtime, owner, worker, active, _ = runtime_case
    task_id = active['task_ids']['produce']
    member_state(runtime, owner, active, 'idle', 'idle')
    assert runtime.task(owner, task_id)['state'] == 'queued'
    claim = runtime.claim(worker, task_id, worker_session_id='old', expected_version=runtime.task(owner, task_id)['state_version'], command_id='claim')
    result = member_state(runtime, owner, active, 'left', 'left')
    assert not result['business_acceptance']
    assert runtime.task(owner, task_id)['state'] == 'blocked'
    assert runtime.task(owner, task_id)['held_tokens'] == 300
    with pytest.raises(TeamStateError):
        runtime.heartbeat(worker, claim['token'], command_id='late')
    with pytest.raises(TeamStateError):
        runtime.repo.get_run(worker, active['team_id'], active['team_run_id'])


def test_replacement_session_keeps_membership_task_budget_and_question(runtime_case):
    _, runtime, owner, worker, active, _ = runtime_case
    task_id = active['task_ids']['produce']
    claim = runtime.claim(worker, task_id, worker_session_id='old', expected_version=runtime.task(owner, task_id)['state_version'], command_id='claim')
    blocked = runtime.block_attempt(worker, claim['token'], reason_code='needs_input', detail='Which format?', needs_answer=True, usage_tokens=20, command_id='question')
    member_state(runtime, owner, active, 'left', 'left')
    member_state(runtime, owner, active, 'active', 'reactivate')
    task = runtime.task(owner, task_id)
    assert task['question']['question_id'] == blocked['question']['question_id']
    runtime.resume(owner, task_id, expected_version=task['state_version'], control_epoch=1, question_id=task['question']['question_id'], answer='JSON', command_id='resume')
    replacement = runtime.claim(worker, task_id, worker_session_id='new', expected_version=runtime.task(owner, task_id)['state_version'], command_id='replacement')
    assert replacement['reserved_tokens'] == 280 and replacement['token']['task_id'] == task_id
    with pytest.raises(TeamStateError, match='STALE_FENCE'):
        runtime.heartbeat(worker, claim['token'], command_id='old-hb')


def test_heartbeats_cannot_extend_a_turn_forever(runtime_case):
    _, runtime, owner, worker, active, _ = runtime_case
    task_id = active['task_ids']['produce']
    start = runtime.clock()
    claim = runtime.claim(worker, task_id, worker_session_id='bounded', expected_version=runtime.task(owner, task_id)['state_version'], command_id='claim')
    for second in (20, 40, 60, 80, 100, 120):
        runtime.clock = lambda second=second: start + second
        runtime.heartbeat(worker, claim['token'], command_id='hb-' + str(second))
    runtime.clock = lambda: start + 131
    with pytest.raises(TeamStateError, match='PROGRESS_TIMEOUT'):
        runtime.heartbeat(worker, claim['token'], command_id='forever')
    runtime.reap_expired(worker, active['team_run_id'], command_id='reap')
    assert runtime.task(owner, task_id)['state'] == 'expired'
