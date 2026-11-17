"""The design entry saves and edits the same graph that Core executes."""
import copy

import httpx
import pytest

from frontend import server
from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.task_design import TaskDesign
from mewcode.orchestration.workflow_blocks import WorkflowBlocks
from mewcode.orchestration.workflow_draft import seed_document, project_graph
from mewcode.workflow_core.planning import spec_for_graph
from mewcode.workflow_core.schema import normalize
from tests.test_task_design_entry import task_api_env  # noqa: F401


MODEL = dict(provider_key='selected', model_id='gpt-6.1-sol', effort='max')


def graph():
    return normalize(dict(label='可复用任务', goal='按材料处理并输出', nodes=[
        dict(id='choose', kind='judge', config={'expression': {'path': 'inputs.enabled', 'equals': True}}),
        dict(id='route', kind='branch', config={'source': 'inputs.choice', 'cases': [{'equals': 'a', 'port': 'chosen'}]}),
        dict(id='batch', kind='loop', config={'mode': 'foreach', 'items': [1, 2], 'graph': {
            'nodes': [dict(id='work', kind='work', label='处理', task='输出处理结果', model=MODEL)], 'edges': []}}),
        dict(id='parallel', kind='work', task='独立输出', model=MODEL)],
        edges=[dict(id='pass', source='choose', target='route', port='pass'),
               dict(id='choice', source='route', target='batch', port='chosen')],
        layout={'choose': {'x': 11, 'y': 12}}))


def test_native_components_survive_plan_material_roundtrip():
    original = graph()
    spec = spec_for_graph(original).as_dict()
    document = seed_document(spec)
    rebuilt = project_graph(document, spec, conversation_id='c', revision=2, native=True)
    result = rebuilt.meta['workflow_core']
    assert result == original
    flow = to_expert_flow(rebuilt)
    assert flow.meta['workflow_core'] == original
    assert 'loops' not in flow.meta
    from mewcode.orchestration.validate import validate_spec
    from mewcode.orchestration.control_plan import build_control_plan
    assert validate_spec(rebuilt, require_bindings=True) == []
    assert not build_control_plan(rebuilt).retries
    unbound = copy.deepcopy(original)
    unbound['nodes'][2]['config']['graph']['nodes'][0].pop('model')
    with pytest.raises(ValueError, match='batch/work'):
        validate_spec(spec_for_graph(unbound), require_bindings=True)


@pytest.mark.asyncio
async def test_legacy_loop_bindings_survive_save_and_execute():
    from mewcode.workflow_core.planning import sync_materials, plan_spec
    from mewcode.workflow_core.runtime import Core, MemoryBackend
    from mewcode.workflow_core.state import fresh
    legacy = dict(label='旧草稿', goal='处理资料', nodes=[
        dict(node_id='make', kind='implement', label='生产', model=MODEL, position={'x': 10, 'y': 20},
             plan_brief={'outputs': ['成果']}),
        dict(node_id='check', kind='decision', label='判断', model=MODEL),
        dict(node_id='use', kind='implement', label='使用', model=MODEL, plan_brief={'known_inputs': ['成果']})],
        edges=[dict(from_node='make', to_node='check'), dict(from_node='check', to_node='use', condition='pass')],
        meta={'loops': [dict(loop_id='pages', node_ids=['make', 'check'], entry_node_id='make', check_node_id='check')],
              'workflow_draft': {'node_input_bindings': {'use': {'成果': 'make'}}}})
    state = {'materials': {'document': seed_document(legacy), 'base_spec': legacy, 'sequence': 1}}
    spec = plan_spec(state, legacy, 'c').as_dict()
    sync_materials(state, spec)
    restored = plan_spec(state, spec, 'c')
    assert restored.meta['workflow_core'] == spec['meta']['workflow_core']
    assert not restored.meta['workflow_core']['layout']
    loop = next(n for n in restored.meta['workflow_core']['nodes'] if n['kind'] == 'loop')
    assert not loop['config']['graph']['layout']
    assert restored.meta['workflow_draft']['node_input_bindings']['use'] == {'成果': 'loop_pages'}
    calls = []
    async def execute(ctx):
        calls.append(ctx.node_id)
        return dict(text='已交付', value=True)
    result = await Core(MemoryBackend(fresh(to_expert_flow(restored).meta['workflow_core'])), execute).run()
    assert result['status'] == 'completed'
    assert calls == ['loop_pages/make', 'loop_pages/check', 'use']


@pytest.mark.asyncio
async def test_empty_design_edits_reload_and_undo_in_native_canvas(task_api_env, monkeypatch):
    _, plans, _, owner, _ = task_api_env
    service = TaskDesign(owner['project'], owner['conversation_id'])
    service.start('plan')
    monkeypatch.setattr(server.MODEL_REGISTRY, 'resolve', lambda ref: ref)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test',
                                headers={'X-Mew-Workbench': '1'}) as client:
        initial = await client.get('/api/workbench/view', params=owner)
        assert initial.status_code == 200, initial.text
        view = initial.json()
        assert view['structure']['workflow'] and view['editor']['planning']
        async def change(operation):
            nonlocal view
            result = await client.post('/api/workbench/graph/edit', json=dict(**owner,
                task_id=view['task']['id'], basis=view['editor']['basis'], expected_revision=view['editor']['revision'],
                operation=operation))
            assert result.status_code == 200, result.text
            view = result.json()['view']
            return result.json()
        await change(dict(action='upsert', kind='node', id='work', fields=dict(kind='work', label='执行', task='输出结果', model=MODEL)))
        await change(dict(action='upsert', kind='node', id='branch', fields=dict(kind='branch', label='选择', config={'source': 'inputs.choice'})))
        await change(dict(action='upsert', kind='edge', id='edge', fields=dict(source='work', target='branch', port='always')))
        await change(dict(action='positions', positions={'work': {'x': 100, 'y': 80}}))
        assert {n['kind'] for n in view['structure']['nodes']} == {'work', 'branch'}
        assert view['editor']['positions']['work']['x'] == 100
        saved = server._conversation_plan_store(owner['conversation_id']).get(owner['project']).spec
        assert to_expert_flow(saved).meta['workflow_core'] == saved.meta['workflow_core']
        refreshed = (await client.get('/api/workbench/view', params=owner)).json()
        assert refreshed['editor']['basis'] == view['editor']['basis']
        stale = dict(**owner, task_id=view['task']['id'], basis=initial.json()['editor']['basis'], operation={'action': 'undo'})
        assert (await client.post('/api/workbench/graph/edit', json=stale)).status_code == 409
        await change({'action': 'undo'})
        assert 'work' not in view['editor']['positions']
        await change({'action': 'redo'})
        assert view['editor']['positions']['work']['x'] == 100
    assert not service.read().get('run_id')


@pytest.mark.asyncio
async def test_build_entry_compiles_without_model_generation_or_review(task_api_env, monkeypatch):
    _, _, path, owner, _ = task_api_env
    service = TaskDesign(owner['project'], owner['conversation_id'])
    service.start('plan')
    service.record_turn('按材料处理并输出')
    with service.store.db() as db:
        state = service.store._get(db)
        state['materials'] = dict(sequence=1, document=seed_document(spec_for_graph(graph()).as_dict()), sources=[])
        service.store._put(db, state)
    blocks = WorkflowBlocks(owner['project'], owner['conversation_id'], source_path=path, base_spec=lambda: None)
    basis = blocks.current_basis()
    current = service.read()
    queued = service.queue_build(task_id=current['task_id'], revision=current['revision'], compiler='workflow_core',
        **{key: basis[key] for key in ('sequence', 'source_hash', 'base_graph_hash', 'user_source_hash', 'requirement_fingerprint')})
    claimed = service.claim_build(queued)
    expected = dict(claimed['build_request'], task_id=claimed['task_id'], task_revision=claimed['revision'],
                    build_id=claimed['build_request']['id'], build_epoch=claimed['build_request']['epoch'])
    def forbidden(*args, **kwargs):
        raise AssertionError('Native planning must not regenerate or review authored nodes')
    monkeypatch.setattr(server, '_new_orchestration_client', forbidden)
    await server.task_design_build_graph(owner['project'], owner['conversation_id'], expected=expected)
    preview = await server.task_design_preview_graph(owner['project'], owner['conversation_id'], expected=expected)
    saved = await server.task_design_apply_graph(owner['project'], owner['conversation_id'], preview['id'], expected=expected)
    assert saved['spec']['meta']['workflow_core']['nodes'][2]['kind'] == 'loop'
    assert service.read()['status'] == 'ready'
    assert service.read()['build_request']['state'] == 'saved'
    assert not service.read().get('run_id')
    assert DraftStore.read_state(owner['project'], owner['conversation_id'])['workflow_build']['id'] == preview['id']
