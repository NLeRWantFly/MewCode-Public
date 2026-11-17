"""Full snapshots delete old content; reopening is not an implicit union."""
import json

import httpx
import pytest

from frontend import server
from mewcode.experts.flow import ExpertFlowStore
from mewcode.orchestration import OrchestrationSpec, get_template, to_pipeline_graph
from mewcode.orchestration.plan_session import PlanSessionStore, build_plan_session
from mewcode.pipeline.store import PipelineStore


def test_explicit_empty_groups_are_not_restored_from_history():
    from mewcode.orchestration.extract import carry_over_user_edits
    old = get_template('linear')
    raw = old.as_dict()
    parent, child = raw['nodes'][0]['node_id'], raw['nodes'][1]['node_id']
    raw['meta'] = {'canvas_groups':{parent:{'children':[child]}},'pipeline_graph_version':7}
    previous = OrchestrationSpec.from_value(raw)
    cleared = OrchestrationSpec.from_value({**raw,'meta':{'canvas_groups':{}}})
    result = carry_over_user_edits(cleared,previous)
    assert result.meta['canvas_groups'] == {}
    assert result.meta['pipeline_graph_version'] == 7
    from mewcode.orchestration.convert import resolve_canvas_groups
    renamed = cleared.as_dict()
    renamed['nodes'][1]['label'] = '子阶段'
    assert resolve_canvas_groups(OrchestrationSpec.from_value(renamed)) == {}


@pytest.mark.asyncio
async def test_replace_save_reopen_and_reject_stale_window(tmp_path, monkeypatch):
    plans = PlanSessionStore(tmp_path / 'plans.json')
    monkeypatch.setattr(server, 'get_plan_session_store', lambda: plans)
    monkeypatch.setattr(server, 'EXPERT_FLOWS_STORE', ExpertFlowStore(tmp_path / 'flows.json'))
    monkeypatch.setattr(server.SESSION, 'work_dir', str(tmp_path))
    old = get_template('linear', goal='old')
    graph = PipelineStore(tmp_path).create_pipeline(to_pipeline_graph(old, graph_id='g_replace'))
    plans.put(tmp_path, build_plan_session(old, draft_plan=True))
    raw = old.as_dict()
    last = dict(raw['nodes'][-1], node_id='only_new', label='Only new')
    raw.update(nodes=[last], edges=[], finalizer_node_id='only_new', meta={})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        replaced = await client.post('/api/orchestration/open-canvas', json={
            'spec': raw, 'graph_id': graph.graph_id, 'expected_version': graph.version})
        assert replaced.status_code == 200, replaced.text
        data = replaced.json()
        assert [n['id'] for n in data['graph']['nodes'] if n['type']=='task'] == ['only_new']
        assert set(json.loads(data['graph']['variables']['__orch.payload__'])['nodes']) == {'only_new'}
        saved = await client.post('/api/orchestration/canvas-sync', json={'graph': data['graph']})
        assert saved.status_code == 200, saved.text
        stale = await client.post('/api/orchestration/open-canvas', json={
            'spec': old.as_dict(), 'graph_id': graph.graph_id, 'expected_version': graph.version})
        assert stale.status_code == 409
        for _ in range(2):
            opened = await client.post('/api/orchestration/open-canvas', json={'graph_id': graph.graph_id})
            assert opened.status_code == 200, opened.text
            assert [n['id'] for n in opened.json()['graph']['nodes'] if n['type']=='task'] == ['only_new']
            assert opened.json()['graph']['version'] == saved.json()['graph']['version']
        assert {n.node_id for n in plans.get(tmp_path).spec.nodes} == {'only_new'}


@pytest.mark.asyncio
async def test_newer_saved_plan_replaces_old_canvas_on_open(tmp_path, monkeypatch):
    plans = PlanSessionStore(tmp_path / 'plans.json')
    monkeypatch.setattr(server, 'get_plan_session_store', lambda: plans)
    monkeypatch.setattr(server.SESSION, 'work_dir', str(tmp_path))
    old = get_template('linear')
    graph = PipelineStore(tmp_path).create_pipeline(to_pipeline_graph(old, graph_id='g_newer'))
    raw = old.as_dict()
    raw.update(nodes=[dict(raw['nodes'][-1], node_id='replacement')], edges=[],
               finalizer_node_id='replacement', version=graph.version+10, meta={'pipeline_graph_id':graph.graph_id})
    plans.put(tmp_path, build_plan_session(OrchestrationSpec.from_value(raw), draft_plan=True))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        opened = await client.post('/api/orchestration/open-canvas', json={'graph_id':graph.graph_id})
        assert opened.status_code == 200, opened.text
        assert [n['id'] for n in opened.json()['graph']['nodes'] if n['type']=='task'] == ['replacement']


@pytest.mark.asyncio
async def test_content_refresh_keeps_card_size_and_manual_layout(tmp_path, monkeypatch):
    plans = PlanSessionStore(tmp_path / 'plans.json')
    monkeypatch.setattr(server, 'get_plan_session_store', lambda: plans)
    monkeypatch.setattr(server.SESSION, 'work_dir', str(tmp_path))
    spec = get_template('linear')
    graph = to_pipeline_graph(spec, graph_id='g_sizes')
    node = next(n for n in graph.nodes if n.type.value == 'task')
    node.position.x, node.position.y = 333, 444
    payload = json.loads(graph.variables['__orch.payload__'])
    payload['nodes'][node.id]['canvas_size'] = {'width':420,'height':260}
    graph.variables['__orch.payload__'] = json.dumps(payload)
    stored = PipelineStore(tmp_path).create_pipeline(graph)
    raw = spec.as_dict()
    raw['nodes'][0]['goal'] = 'Updated content only'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        opened = await client.post('/api/orchestration/open-canvas', json={
            'graph_id':stored.graph_id,'spec':raw,'expected_version':stored.version})
        assert opened.status_code == 200, opened.text
        result = opened.json()['graph']
        changed = next(n for n in result['nodes'] if n['id']==node.id)
        assert changed['position'] == {'x':333.0,'y':444.0}
        meta = json.loads(result['variables']['__orch.payload__'])
        assert meta['nodes'][node.id]['canvas_size'] == {'width':420,'height':260}
        assert meta['meta']['canvas_auto_layout_pending'] is False
