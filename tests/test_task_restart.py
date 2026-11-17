"""A fresh host task uses existing save/launch fences and preserves its source."""
import copy

import httpx
import pytest

from frontend import server
from frontend.task_restart import preserve_requirements
from mewcode.orchestration.draft_sources import digest
from mewcode.orchestration.spec import OrchestrationSpec
from mewcode.orchestration.task_design import TaskDesign
from tests.test_orchestration_start_run import _bound_spec
from tests.test_task_design_entry import task_api_env

RUN = '7' * 32


def paused_design(project, cid='paper', mode='collaborative'):
    design = TaskDesign(project, cid)
    design.start('execute', orchestration_mode=mode)
    current = design.record_turn('Process every page and retain quality evidence')
    paused = design.transition(current, 'paused', run_id=RUN)
    return design, paused


def restart_args(current):
    return {'task_id': current['task_id'], 'revision': current['revision'], 'run_id': RUN,
            'request_id': 'explicit-rebuild', 'spec_hash': 'f' * 64, 'document': {'entries': []}}


def test_restart_archives_original_design_then_normal_save_and_claim_bind_a_distinct_run(tmp_path):
    design, old = paused_design(tmp_path)
    before = design.store.get()
    args = restart_args(old)
    prepared = design.restart(**args)
    assert prepared['task_id'] != old['task_id'] and not prepared.get('run_id')
    assert prepared['intent_authority'] == old['intent_authority']
    assert prepared['status'] == 'building'
    archived = design.store.get()['task_restart_archives'][args['request_id']]
    assert archived['task_design'] == before['task_design']
    assert archived['materials'] == before.get('materials')
    assert design.restart(**args)['task_id'] == prepared['task_id']
    assert len(design.store.get()['task_restart_archives']) == 1
    with pytest.raises(ValueError, match='保存结果'):
        design.claim_execution(prepared)
    build = prepared['build_request']
    expected = {'task_id': prepared['task_id'], 'task_revision': prepared['revision'],
                'build_id': build['id'], 'build_epoch': build['epoch']}
    with design.commit_build(expected) as result:
        result['spec'] = {'meta': {'pipeline_graph_version': 2}, 'nodes': ['actual saved graph']}
    saved = design.saved_build(expected)
    claimed = design.claim_execution(saved)
    with design.dispatch_guard(design.execution_token(claimed)):
        pass
    new_run = design.execution_run_id(claimed)
    bound = design.bind_run(task_id=claimed['task_id'], claim_id=claimed['build_request']['execution_claim_id'], run_id=new_run)
    assert new_run != RUN and bound['run_id'] == new_run
    assert design.store.get()['task_restart_archives'][args['request_id']]['task_design'] == before['task_design']


@pytest.mark.parametrize('fault', ['revision', 'run_id', 'request_reuse', 'running'])
def test_restart_is_cas_guarded_and_cannot_rewrite_an_existing_request(tmp_path, fault):
    design, old = paused_design(tmp_path)
    args = restart_args(old)
    if fault == 'request_reuse':
        design.restart(**args)
        args['spec_hash'] = 'b' * 64
    elif fault == 'revision':
        args['revision'] -= 1
    elif fault == 'run_id':
        args['run_id'] = '8' * 32
    else:
        old = design.transition(old, 'running')
        args['revision'] = old['revision']
    before = copy.deepcopy(design.store.get())
    with pytest.raises(ValueError):
        design.restart(**args)
    assert design.store.get() == before


def test_scheduling_restart_preserves_all_business_requirements():
    old = _bound_spec('All original pages with the original quality standard')
    raw = old.as_dict()
    raw['spec_id'] = 'rebuilt'
    raw['nodes'][0]['goal'] = 'Execute the same scope using a finite queue'
    preserve_requirements(old, OrchestrationSpec.from_value(raw))
    for key, replacement in [('goal', 'Fewer pages'), ('goal_contract', {'acceptance': ['No quality check']})]:
        changed = {**raw, key: replacement}
        with pytest.raises(ValueError, match='原业务总目标'):
            preserve_requirements(old, OrchestrationSpec.from_value(changed))


@pytest.mark.parametrize('bound', [False, True])
def test_cold_recovery_releases_only_a_proven_unbound_startup_claim(tmp_path, bound):
    design, old = paused_design(tmp_path)
    prepared = design.restart(**restart_args(old))
    build = prepared['build_request']
    expected = {'task_id': prepared['task_id'], 'task_revision': prepared['revision'],
                'build_id': build['id'], 'build_epoch': build['epoch']}
    with design.commit_build(expected) as result:
        result['spec'] = {'meta': {'pipeline_graph_version': 2}, 'nodes': ['saved']}
    claimed = design.claim_execution(design.saved_build(expected))
    token = design.execution_token(claimed)
    request = {**claimed['build_request'], 'execution_state': 'started'}
    stopped = design.transition(claimed, 'paused', build_request=request,
        **({'run_id': design.execution_run_id(claimed)} if bound else {}))
    assert design.record_execution_not_started(token, 'worker rejected before creating a run') is False
    assert design.record_execution_not_started(token, 'cold host verified no run exists', restart_recovery=True) is not bound
    current = design.read()
    assert current['status'] == 'paused' and current['goal'] == stopped['goal']
    if bound:
        assert current['build_request']['execution_state'] == 'started'
    else:
        assert 'execution_state' not in current['build_request']
        ready = design.transition(current, 'ready')
        next_claim = design.claim_execution(ready)
        assert next_claim['build_request']['execution_claim_id'] == token['claim_id']


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['', 'unknown', 'active', 'foreign', 'unconfirmed', 'stale', 'goal'])
async def test_restart_endpoint_saves_normally_and_only_retires_source_after_success(task_api_env, monkeypatch, fault):
    _, _, _, owner, _ = task_api_env
    design, current = paused_design(owner['project'], owner['conversation_id'], mode='classic')
    spec = _bound_spec('Process every page and retain quality evidence')
    spec.meta.update(conversation_id=owner['conversation_id'], task_design={'task_id': current['task_id']})
    store = server._conversation_plan_store(owner['conversation_id'])
    store.put(owner['project'], server.build_plan_session(spec, goal=spec.goal, confirmed=True))
    source = {'run_id': RUN, 'conversation_id': owner['conversation_id'], 'status': 'paused',
              'active_node_ids': [], 'kernel_calls': {}, 'run_inputs': {}, 'updated_at': 'source-revision'}
    from types import SimpleNamespace
    if fault == 'unknown':
        source['kernel_calls'] = {'call': {'state': 'unknown'}}
    elif fault == 'active':
        source['active_node_ids'] = ['work']
    elif fault == 'foreign':
        source['conversation_id'] = 'other'
    original = copy.deepcopy(source)
    retirements = []
    def transition(run_id, status, *, reason):
        assert run_id == RUN and status == 'terminated'
        retirements.append((run_id, status))
        source.update(status=status)
    monkeypatch.setattr(server, 'EXPERT_RUNS', SimpleNamespace(
        read_execution_fields=lambda *_: copy.deepcopy(source), transition=transition))
    invoked = []
    async def forbidden_start(*args, **kwargs):
        invoked.append(args)
        raise AssertionError('saving a rebuilt graph must not start execution')
    monkeypatch.setattr(server, 'start_runtime_turn', forbidden_start)
    raw = spec.as_dict()
    raw['spec_id'] = 'rebuilt-queue'
    if fault == 'goal':
        raw['goal'] = 'Smaller business scope'
    payload = {**owner, 'confirm_restart': fault != 'unconfirmed', 'spec': raw,
               'expected_task_id': current['task_id'], 'expected_revision': current['revision'] - int(fault == 'stale'),
               'expected_run_id': RUN, 'expected_run_updated_at': source['updated_at'], 'request_id': 'restart-once'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        response = await client.post('/api/orchestration/task-design/restart', json=payload)
        if fault:
            assert response.status_code == 409, response.text
            assert design.read()['task_id'] == current['task_id']
        else:
            assert response.status_code == 200, response.text
            saved = response.json()
            assert saved['task_design']['task_id'] != current['task_id']
            assert saved['task_design']['status'] == 'paused' and saved['started'] is False
            assert saved['spec']['spec_id'] == 'rebuilt-queue'
            assert server._conversation_plan_store(owner['conversation_id']).get(owner['project']).spec.spec_id == 'rebuilt-queue'
            again = await client.post('/api/orchestration/task-design/restart', json=payload)
            assert again.status_code == 200, again.text
            assert again.json()['already_saved'] is True
    assert not invoked
    assert source == (original if fault else {**original, 'status': 'terminated'})
    assert retirements == ([] if fault else [(RUN, 'terminated')])
    if not fault:
        state = design.store.get()
        assert state['workbench_startup']['task_id'] == saved['task_design']['task_id']
        assert state['workbench_startup']['spec_hash'] == digest(saved['spec'])
