"""Workbench design reads and choices use existing draft/chooser boundaries."""
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest
from frontend import server
from mewcode.expert_runs import ExpertRunStore
from mewcode.orchestration import inbox, task_view
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.task_design import TaskDesign, design_view
from tests.test_task_design_entry import task_api_env
from tests.test_graph_discussion import prepare


def test_design_projection_preserves_sources_and_controls():
    entry = dict(id='limit', kind='constraint', body='保留原文', sources=[dict(source_id='user', quote='不得改写')])
    state = dict(task_design=dict(task_id='task', revision=2, status='designing', intent='execute'), materials={'document': {'entries': [entry]}})
    before = copy.deepcopy(state)
    design = design_view(state)
    assert design['specification']['entries'] == [entry]
    view = task_view.design_view(design, conversation_id='chat')
    assert view['task']['controls'] == ['pause', 'set_intent']
    assert state == before
    for status, intent, controls in [('ready', 'plan', []), ('paused', 'execute', ['resume', 'set_intent']), ('completed', 'execute', [])]:
        assert task_view.design_view({**design, 'status':status, 'intent':intent}, conversation_id='chat')['task']['controls'] == controls


def test_draft_canvas_uses_current_materials_and_candidate_nodes_without_execution():
    source = dict(source_id='user', quote='对比资料并附来源')
    current = dict(id='read', kind='node', state='active', title='读取资料', body='保留来源',
                   data={'outputs':['资料清单'], 'acceptance':['可追溯']}, sources=[source])
    final = dict(id='deliver', kind='node', state='active', title='交付报告', body='对比并交付', data={})
    edge = dict(id='link', kind='edge', state='active', title='', body='',
                data={'from_node':'read', 'to_node':'deliver', 'condition':'success'})
    state = dict(task_design=dict(task_id='task', revision=2, status='designing', intent='plan'),
                 materials={'document': {'entries':[current]}}, candidate={'status':'ready', 'document': {'entries':[
                     {**current, 'body':'旧要求'}, final, edge,
                     dict(id='old', kind='node', state='retired', data={})]}})
    before = copy.deepcopy(state)
    design = design_view(state)
    view = task_view.design_view(design, conversation_id='chat')
    assert [node['id'] for node in view['structure']['nodes']] == ['read','deliver']
    assert view['structure']['edges'] == [{'id':'link','from':'read','to':'deliver','condition':'success'}]
    step = view['plan'][0]['steps'][0]
    assert step['detail']['goal'] == '保留来源'
    assert step['detail']['sources'] == [source] and step['detail']['outputs'] == ['资料清单']
    assert step['status'] == '草稿' and step['attempts'] == 0 and step['detail_ref'] is None
    assert not view['task']['run_id'] and view['deliverables'] == [] and view['structure']['versions'] == []
    assert state == before
    edited = copy.deepcopy(design)
    edited['specification']['entries'][0]['body'] = '补充新的资料来源'
    assert task_view.design_view(edited, conversation_id='chat')['version'] != view['version']
    removed = copy.deepcopy(design)
    removed['specification']['entries'][0]['state'] = 'retired'
    removed_view = task_view.design_view(removed, conversation_id='chat')
    assert [node['id'] for node in removed_view['structure']['nodes']] == ['deliver']
    assert removed_view['structure']['unresolved_edges'] == 1


def test_design_choices_preserve_native_values_and_ignore_system_issues(tmp_path):
    store = DraftStore(tmp_path, 'chat')
    candidate = prepare(store)
    question = next(row for row in candidate['document']['entries'] if row['id'] == 'meaning')
    question['data']['options'] = [{'value':'keep', 'label':'保留'}, {'value':'revise','label':'修订'}]
    candidate['document']['entries'].append(dict(id='tech', kind='question', state='active', title='技术问题', data={'issue_type':'technical'}))
    task = dict(id='task',conversation_id='chat')
    rows = inbox.design_choices(task, candidate)
    assert len(rows) == 1 and rows[0]['mandatory'] and not rows[0]['prepare_choices']
    assert rows[0]['input_schema'][0]['options'][0]['value'] == 'keep'
    assert inbox.known_id(rows[0]['id'])
    assert inbox.design_choices(task, {**candidate, 'sequence': candidate['sequence'] + 1})[0]['id'] != rows[0]['id']
    question['data']['options'] = []
    pending = inbox.design_choices(task,candidate)[0]
    assert pending['prepare_choices'] and pending['questions'] == []


@pytest.mark.asyncio
async def test_canvas_api_follows_draft_edits_then_uses_the_saved_execution_graph(task_api_env, monkeypatch):
    _, _, _, owner, _ = task_api_env
    service = TaskDesign(owner['project'], owner['conversation_id'])
    current = service.start('plan')
    store = service.store
    def material(title):
        with store.db() as db:
            state = store._get(db)
            state['materials'] = {'document': {'entries': [dict(id='work', kind='node', state='active',
                title=title, body='根据对话处理资料', sources=[], data={})]}}
            store._put(db, state)
    material('对话草稿')
    model = AsyncMock(side_effect=AssertionError('reading a canvas cannot call a model'))
    monkeypatch.setattr(server.MODEL_REGISTRY, 'catalog', model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        first = await client.get('/api/workbench/view', params=owner)
        assert first.status_code == 200, first.text
        assert first.json()['structure']['nodes'][0]['label'] == '对话草稿'
        material('补充后草稿')
        second = (await client.get('/api/workbench/view', params=owner)).json()
        assert second['structure']['nodes'][0]['label'] == '补充后草稿'
        assert first.json()['version'] != second['version']
        saved = {'label':'已保存编排', 'meta':{'task_design':{'task_id':current['task_id']}},
                 'nodes':[{'node_id':'work','label':'实际保存节点','kind':'implement','goal':'实际运行目标'}], 'edges':[]}
        plan = SimpleNamespace(spec=SimpleNamespace(as_dict=lambda:copy.deepcopy(saved)))
        monkeypatch.setattr(server, '_conversation_plan_store', lambda cid:SimpleNamespace(get=lambda project:plan))
        service.transition(current, 'ready', build_request={'state':'saved'})
        before = store.get()
        final = (await client.get('/api/workbench/view', params=owner)).json()
        assert final['structure']['nodes'][0]['label'] == '实际保存节点'
        assert final['structure']['nodes'][0]['status'] == '待运行'
        assert final['structure']['phase'] == 'saved' and not final['task']['run_id']
        assert store.get() == before
    model.assert_not_awaited()


@pytest.mark.asyncio
async def test_design_business_answer_uses_original_chooser_and_rejects_second_tab(task_api_env, tmp_path, monkeypatch):
    _, _, _, owner, _ = task_api_env
    monkeypatch.setattr(server, 'EXPERT_RUNS', ExpertRunStore(tmp_path/'runs'))
    model = AsyncMock(side_effect=AssertionError('read and explicit choice must not call a model'))
    monkeypatch.setattr(server.MODEL_REGISTRY, 'catalog', model)
    design = TaskDesign(owner['project'], owner['conversation_id']).start('plan')
    store = DraftStore(owner['project'],owner['conversation_id']); candidate=prepare(store)
    question=next(row for row in candidate['document']['entries'] if row['id']=='meaning')
    question['data']['options']=[{'value':'keep','label':'保留'}, {'value':'revise','label':'修订'}]
    store.checkpoint(candidate,candidate['document'],candidate['processed_ids'],'ready',autobuild=candidate['autobuild'])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://test') as client:
        values={**owner,'task_id':design['task_id']}
        first=await client.get('/api/workbench/view',params=values)
        assert first.status_code==200,first.text
        card=next(row for row in first.json()['inbox'] if row['type']=='design_choice')
        path='/api/workbench/inbox/'+card['id']+'/answer'
        bad=await client.post(path,json={**values,'answers':{'meaning':'invented'}},headers={'X-Mew-Workbench':'1'})
        assert bad.status_code==409
        answer=await client.post(path,json={**values,'answers':{'meaning':'keep'},'notes':{'meaning':'明确保留'}},headers={'X-Mew-Workbench':'1'})
        assert answer.status_code==200,answer.text
        again=await client.post(path,json={**values,'answers':{'meaning':'revise'}},headers={'X-Mew-Workbench':'1'})
        assert again.status_code==409 and again.json()['error']==inbox.STALE
        changed=next(row for row in store.get()['candidate']['document']['entries'] if row['id']=='meaning')
        assert changed['kind']=='decision' and changed['data']['category']=='confirmed'
    model.assert_not_awaited()
