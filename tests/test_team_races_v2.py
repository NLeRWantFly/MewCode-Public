import copy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from mewcode.mcp.coord_store import CoordinationStore
from mewcode.teams.runtime_protocol import ArtifactContent
from mewcode.teams.store_v2 import TeamStateError
from test_team_runtime_v2 import runtime_case, runtime_fixture, runtime_actor, submit_candidate, review_candidate


def racing(left, right):
    barrier = Barrier(2)
    def call(fn):
        barrier.wait()
        try:
            return fn()
        except TeamStateError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(call, fn) for fn in (left, right)]
        return [job.result(timeout=20) for job in jobs]


def test_two_connections_cannot_claim_the_same_task(tmp_path):
    path = tmp_path / 'coord.sqlite'
    with CoordinationStore(path, enable_team_v2=True) as first:
        runtime, owner, worker, active, _ = runtime_fixture(first)
        task = runtime.task(owner, active['task_ids']['produce'])
        with CoordinationStore(path, enable_team_v2=True) as second:
            result = racing(lambda: runtime.claim(worker, task['task_id'], worker_session_id='a', expected_version=task['state_version'], command_id='claim-a'),
                            lambda: second.teams_v2.runtime.claim(worker, task['task_id'], worker_session_id='b', expected_version=task['state_version'], command_id='claim-b'))
        assert sum(isinstance(item, dict) for item in result) == 1
        assert runtime.snapshot(owner, active['team_run_id'])['budget']['reserved_tokens'] == 300


def test_receipt_racing_revocation_cannot_leave_a_ready_consumer(tmp_path):
    path = tmp_path / 'coord.sqlite'
    with CoordinationStore(path, enable_team_v2=True) as store:
        runtime, owner, worker, active, schema = runtime_fixture(store)
        _, _, packet = submit_candidate(runtime, worker, active['task_ids']['produce'], schema)
        review_candidate(runtime, owner, packet)
        version, consumer = packet['artifacts'][0]['version_id'], active['task_ids']['second']
        with CoordinationStore(path, enable_team_v2=True) as other:
            racing(lambda: runtime.accept_input(owner, consumer, 'request', version, revision=1, accepted=True, reason='Exact input', control_epoch=1, command_id='receipt'),
                   lambda: other.teams_v2.runtime.revoke_artifact(owner, version, reason='Confirmed defect', control_epoch=1, command_id='revoke'))
        task = runtime.task(owner, consumer)
        assert task['revision'] == 2 and task['inputs'][0]['version_id'] is None and task['state'] != 'queued'
        old = runtime.db.execute('SELECT state FROM team_v2_task_revisions WHERE task_id=? AND revision=1', (active['task_ids']['produce'],)).fetchone()
        assert old[0] == 'completed'


def test_question_and_budget_survive_an_actual_reopen(tmp_path):
    path = tmp_path / 'coord.sqlite'
    with CoordinationStore(path, enable_team_v2=True) as store:
        runtime, owner, worker, active, _ = runtime_fixture(store)
        task_id = active['task_ids']['produce']
        task = runtime.task(owner, task_id)
        claim = runtime.claim(worker, task_id, worker_session_id='old', expected_version=task['state_version'], command_id='claim')
        blocked = runtime.block_attempt(worker, claim['token'], reason_code='needs_input', detail='Which output format?', needs_answer=True, usage_tokens=22, command_id='question')
    with CoordinationStore(path, enable_team_v2=True) as store:
        runtime = store.teams_v2.runtime
        task = runtime.task(owner, task_id)
        assert task['question']['question_id'] == blocked['question']['question_id']
        assert task['spent_tokens'] == 22 and task['held_tokens'] == 0
        runtime.resume(owner, task_id, expected_version=task['state_version'], control_epoch=1, question_id=task['question']['question_id'], answer='JSON', command_id='answer')
        fresh = runtime.claim(worker, task_id, worker_session_id='new', expected_version=runtime.task(owner, task_id)['state_version'], command_id='new-claim')
        assert fresh['reserved_tokens'] == 278
        with pytest.raises(TeamStateError, match='STALE_FENCE'):
            runtime.heartbeat(worker, claim['token'], command_id='late')


def test_receipt_cannot_bypass_unknown_effect_recovery(runtime_case):
    _, runtime, owner, worker, active, schema = runtime_case
    task_id = active['task_ids']['produce']
    claim = runtime.claim(worker, task_id, worker_session_id='old', expected_version=runtime.task(owner, task_id)['state_version'], command_id='claim')
    seed = runtime.register_input(owner, active['team_run_id'], ArtifactContent.from_bytes(port_id='request', schema_ref=schema, media_type='text/plain', content=b'changed'), control_epoch=1, command_id='seed2')
    runtime.revise(owner, task_id, inputs={'request': seed['version_id']}, expected_version=runtime.task(owner, task_id)['state_version'], control_epoch=1, reason='Explicit changed input', approved=True, command_id='revise')
    runtime.accept_input(owner, task_id, 'request', seed['version_id'], revision=2, accepted=True, reason='Compatible', control_epoch=1, command_id='receipt')
    assert runtime.task(owner, task_id)['state'] == 'blocked'
    runtime.resolve_usage(owner, claim['token']['attempt_id'], usage_tokens=30, evidence='Provider record', control_epoch=1, command_id='usage')
    task = runtime.task(owner, task_id)
    with pytest.raises(TeamStateError, match='NEEDS_RECONCILIATION'):
        runtime.resume(owner, task_id, expected_version=task['state_version'], control_epoch=1, command_id='resume')
    with pytest.raises(TeamStateError, match='NEEDS_RECONCILIATION'):
        runtime.revise(owner, task_id, inputs={}, expected_version=task['state_version'], control_epoch=1, reason='Try to skip reconciliation', approved=True, command_id='bypass')


def test_revoking_old_output_keeps_new_valid_producer(runtime_case):
    _, runtime, owner, worker, active, schema = runtime_case
    task_id = active['task_ids']['produce']
    _, _, old = submit_candidate(runtime, worker, task_id, schema)
    review_candidate(runtime, owner, old)
    runtime.revise(owner, task_id, inputs={}, expected_version=runtime.task(owner, task_id)['state_version'], control_epoch=1, reason='Explicit new deliverable', approved=True, command_id='revise')
    _, _, current = submit_candidate(runtime, worker, task_id, schema, 'new')
    runtime.review(owner, current['handoff_id'], accepted=True, scores=[dict(criterion_id='valid', score=10000, evidence='Current version verified')], expected_revision=2, control_epoch=1, command_id='new-review')
    runtime.revoke_artifact(owner, old['artifacts'][0]['version_id'], reason='Old-only defect', control_epoch=1, command_id='revoke-old')
    task = runtime.task(owner, task_id)
    assert task['revision'] == 2 and task['state'] == 'completed'
    assert runtime.artifact(owner, current['artifacts'][0]['version_id'])['valid_for_consumption']


def test_revoked_ancestor_blocks_historical_derived_artifact(runtime_case):
    _, runtime, owner, worker, active, schema = runtime_case
    first, second = active['task_ids']['produce'], active['task_ids']['second']
    _, _, upstream = submit_candidate(runtime, worker, first, schema)
    review_candidate(runtime, owner, upstream)
    runtime.accept_input(owner, second, 'request', upstream['artifacts'][0]['version_id'], revision=1, accepted=True, reason='Compatible', control_epoch=1, command_id='receipt')
    _, _, derived = submit_candidate(runtime, worker, second, schema, 'derived')
    review_candidate(runtime, owner, derived, 'derived')
    replacement = runtime.register_input(owner, active['team_run_id'], ArtifactContent.from_bytes(port_id='request', schema_ref=schema, media_type='text/plain', content=b'independent'), control_epoch=1, command_id='replacement')
    runtime.revise(owner, second, inputs={'request': replacement['version_id']}, expected_version=runtime.task(owner, second)['state_version'], control_epoch=1, reason='Independent new input', approved=True, command_id='second-revise')
    runtime.revoke_artifact(owner, upstream['artifacts'][0]['version_id'], reason='Defective source', control_epoch=1, command_id='revoke')
    assert runtime.task(owner, second)['revision'] == 2  # current input is unrelated
    version = derived['artifacts'][0]['version_id']
    assert not runtime.artifact(owner, version)['valid_for_consumption']
    with pytest.raises(TeamStateError, match='ARTIFACT_UNAVAILABLE'):
        runtime._artifact_for_run(version, active['team_run_id'])


def test_contract_narrowing_keeps_cumulative_budget(runtime_case):
    _, runtime, owner, worker, active, schema = runtime_case
    task_id = active['task_ids']['produce']
    _, _, result = submit_candidate(runtime, worker, task_id, schema)
    review_candidate(runtime, owner, result)
    task = runtime.task(owner, task_id)
    contract = copy.deepcopy(task['contract'])
    contract['token_budget'] = 100
    runtime.revise(owner, task_id, inputs={}, contract=contract, expected_version=task['state_version'], control_epoch=1, reason='Narrow remaining authorization', approved=True, command_id='narrow')
    assert runtime.task(owner, task_id)['token_limit'] == 100
    claim = runtime.claim(worker, task_id, worker_session_id='new', expected_version=runtime.task(owner, task_id)['state_version'], command_id='second')
    assert claim['reserved_tokens'] == 70
