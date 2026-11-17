import copy

import pytest

from mewcode.teams.spec_compiler import compile_team_spec, CompilationContext
from mewcode.teams.store_v2 import TeamStateError
from test_team_runtime_v2 import runtime_case, submit_candidate, review_candidate


def amended(runtime, run_id):
    original = runtime._compiled(run_id)
    raw = original.spec.model_dump(mode='json')
    raw['version'] += 1
    task = copy.deepcopy(raw['tasks'][0])
    task.update(task_key='extra', objective='Additional explicitly approved evidence', token_budget=100)
    raw['tasks'].append(task)
    raw['outputs'].append(dict(port_id='extra', schema_ref='software-artifact/v1', required=True))
    raw['connections'].extend([dict(source=dict(task_key='produce', port_id='result'), target=dict(task_key='extra', port_id='request')),
                               dict(source=dict(task_key='extra', port_id='result'), target=dict(port_id='extra'))])
    context = CompilationContext(original.effective_policy, original.effective_policy, frozenset({'software-artifact/v1'}), frozenset({'software-check/v1'}))
    return raw, context


def test_explicit_append_preserves_tasks_and_budget(runtime_case):
    _, runtime, owner, worker, active, schema = runtime_case
    _, _, result = submit_candidate(runtime, worker, active['task_ids']['produce'], schema)
    review_candidate(runtime, owner, result)
    before = runtime.task(owner, active['task_ids']['produce'])
    raw, context = amended(runtime, active['team_run_id'])
    compiled = compile_team_spec(raw, context=context)
    view = runtime.snapshot(owner, active['team_run_id'])
    changed = runtime.amend_plan(owner, active['team_run_id'], compiled=compiled, expected_version=view['run']['state_version'], control_epoch=1, approved=True, reason='Explicit additional deliverable', command_id='amend')
    assert changed['task_set_revision'] == 2
    assert runtime.task(owner, before['task_id']) == before
    extra = runtime.task(owner, changed['added_task_ids']['extra'])
    assert extra['state'] == 'waiting_inputs' and extra['inputs'][0]['receipt'] == 'pending'
    assert extra['inputs'][0]['version_id'] == result['artifacts'][0]['version_id']
    current = runtime.snapshot(owner, active['team_run_id'])
    assert current['budget']['limit_tokens'] == 1000 and current['budget']['used_tokens'] == 30
    assert current['run']['task_set_revision'] == 2
    assert runtime.repo.get_run(owner, active['team_id'], active['team_run_id'])['compiled']['spec']['version'] == 2
    assert len(runtime.db.execute('SELECT * FROM team_v2_plan_revisions').fetchall()) == 2
    assert any(s.version == 2 for s in runtime.repo.spec_catalog(owner))


def test_append_cannot_rewrite_old_contract_or_expand_grant(runtime_case):
    _, runtime, owner, _, active, _ = runtime_case
    raw, context = amended(runtime, active['team_run_id'])
    raw['tasks'][0]['objective'] = 'Silently replace the current task'
    with pytest.raises(TeamStateError, match='TASK_REVISION_REQUIRED'):
        runtime.amend_plan(owner, active['team_run_id'], compiled=compile_team_spec(raw, context=context), expected_version=runtime.snapshot(owner, active['team_run_id'])['run']['state_version'],
                           control_epoch=1, approved=True, reason='Not sufficient to replace contracts', command_id='bad')
    assert len(runtime.snapshot(owner, active['team_run_id'])['tasks']) == 2


def test_append_requires_new_version_and_explicit_approval(runtime_case):
    _, runtime, owner, _, active, _ = runtime_case
    raw, context = amended(runtime, active['team_run_id'])
    compiled = compile_team_spec(raw, context=context)
    with pytest.raises(TeamStateError, match='APPROVAL_REQUIRED'):
        runtime.amend_plan(owner, active['team_run_id'], compiled=compiled, expected_version=1, control_epoch=1, approved=False, reason='No approval', command_id='bad')
