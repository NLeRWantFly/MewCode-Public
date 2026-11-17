"""Manual edits share the real draft ledger and cannot alter a running graph."""
import copy
from unittest.mock import AsyncMock

import httpx
import pytest

from frontend import server
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.task_design import TaskDesign
from mewcode.permissions import PermissionMode
from tests.test_task_design_entry import task_api_env


def test_manual_model_binding_survives_projection_but_proposals_cannot_rebind(tmp_path):
    from mewcode.orchestration.manual_graph import snapshot, edit
    from mewcode.orchestration.workflow_draft import seed_document, project_graph
    service = TaskDesign(tmp_path, 'chat')
    design = service.start('plan')
    previous = dict(provider_key='old', model_id='old-model', effort='high')
    chosen = dict(provider_key='chosen', model_id='exact-model', effort='max')
    base = dict(label='原编排', nodes=[dict(node_id='work', kind='implement', label='工作', goal='交付', model=previous)], edges=[])
    document = seed_document(base)
    with service.store.db() as db:
        state = service.store._get(db)
        state['materials'] = dict(sequence=1, document=document, base_spec=base, sources=[])
        service.store._put(db, state)
    edit(tmp_path, 'chat', dict(task_id=design['task_id'], basis=snapshot(state, base)['basis'],
        operation=dict(action='upsert', kind='node', id='work', fields=dict(data=dict(model=chosen)))), base=base)
    saved = service.store.get()['materials']['document']
    projected = project_graph(saved, base, conversation_id='chat', revision=2)
    assert projected.as_dict()['nodes'][0]['model'] == chosen
    later_edit = copy.deepcopy(saved)
    edited = next(e for e in later_edit['entries'] if e['id'] == 'work')
    edited.update(evidence='user_source', title='补充目标后的节点')
    edited.pop('reviewed', None)
    assert project_graph(later_edit, base, conversation_id='chat', revision=3).as_dict()['nodes'][0]['model'] == chosen
    edited['data']['model'] = {**chosen, 'effort':'low'}
    with pytest.raises(ValueError, match='模型绑定'):
        project_graph(later_edit, base, conversation_id='chat', revision=3)
    proposed = copy.deepcopy(document)
    entry = next(e for e in proposed['entries'] if e['id'] == 'work')
    entry['data']['model'] = chosen
    with pytest.raises(ValueError, match='模型绑定'):
        project_graph(proposed, base, conversation_id='chat', revision=2)


@pytest.mark.asyncio
async def test_manual_node_model_api_validates_exact_target_without_model_calls(task_api_env, monkeypatch):
    from types import SimpleNamespace
    from mewcode.model_registry import ModelRef
    _, _, _, scope, _ = task_api_env
    chosen = ModelRef('chosen', 'exact-model', 'high')
    TaskDesign(scope['project'], scope['conversation_id']).start('plan')
    def resolve(ref):
        if ref != chosen:
            raise ValueError('所选模型不可用')
        return SimpleNamespace(ref=ref, effort=ref.effort, capabilities=('chat', 'tools'))
    monkeypatch.setattr(server.MODEL_REGISTRY, 'resolve', resolve)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test', headers={'X-Mew-Workbench':'1'}) as client:
        current = (await client.get('/api/workbench/view', params=scope)).json()
        body = dict(**scope, task_id=current['task']['id'], basis=current['editor']['basis'],
            operation=dict(action='upsert', kind='node', fields=dict(title='工作', data=dict(node_kind='implement', model=chosen.as_dict()))))
        result = await client.post('/api/workbench/graph/edit', json=body)
        assert result.status_code == 200, result.text
        current = result.json()['view']
        entry = next(row for row in current['requirements'] if row['kind'] == 'node')
        assert entry['data']['model'] == chosen.as_dict()
        before = DraftStore(scope['project'], scope['conversation_id']).get()
        body.update(task_id=current['task']['id'], basis=current['editor']['basis'])
        body['operation'].update(id=entry['id'], fields=dict(data=dict(model={**chosen.as_dict(), 'effort':'unsupported'})))
        rejected = await client.post('/api/workbench/graph/edit', json=body)
        assert rejected.status_code == 409
        assert DraftStore(scope['project'], scope['conversation_id']).get() == before


def test_manual_saved_graph_preserves_metadata_and_can_repair_incomplete_draft(tmp_path):
    from mewcode.orchestration.manual_graph import snapshot, edit
    from mewcode.orchestration.workflow_blocks import _baseline
    from mewcode.orchestration.workflow_draft import seed_document
    service = TaskDesign(tmp_path, 'chat')
    design = service.start('plan')
    base = dict(label='已有图',nodes=[dict(node_id='work',kind='implement',label='已有节点',goal='保留原有目标')],edges=[])
    old_base = copy.deepcopy(base);old_base['nodes'][0]['label']='过期名称'
    document = seed_document(base)
    node = next(e for e in document['entries'] if e['kind']=='node')
    node['data']['work_key']='stable-work';node['custom_annotation']={'keep':True}
    # An unrelated unresolved edge should not prevent saving a partial repair.
    document['entries'].append(dict(id='broken',kind='edge',title='',body='',state='active',sources=[],
        data=dict(from_node='missing',to_node='work',condition='always')))
    with service.store.db() as db:
        state = service.store._get(db)
        state['materials'] = dict(sequence=1,document=document,base_spec=base,sources=[],pending=[{'code':'old_issue'}])
        state['candidate'] = dict(id='old',sequence=2,status='ready',base_snapshot=old_base,document=copy.deepcopy(document),processed_ids=[])
        service.store._put(db,state)
    current = service.store.get()
    response = edit(tmp_path,'chat',dict(task_id=design['task_id'],basis=snapshot(current,base)['basis'],
        operation=dict(action='upsert',kind='node',id='work',fields=dict(title='用户新名称'))),base=base)
    assert response['ok']
    saved = service.store.get()
    edited = next(e for e in _baseline(saved,base)['entries'] if e['id']=='work')
    assert edited['title']=='用户新名称' and edited['body']=='保留原有目标'
    assert edited['custom_annotation']=={'keep':True} and edited['data']['work_key']=='stable-work'
    assert saved['materials']['pending']==[{'code':'old_issue'}]
    assert next(e for e in service.read()['specification']['entries'] if e['id']=='work')['title']=='用户新名称'
    bad = dict(task_id=design['task_id'],basis=snapshot(saved,base)['basis'],operation=dict(action='upsert',kind='edge',
        fields=dict(data=dict(from_node='work',to_node='not-a-node',condition='pass'))))
    with pytest.raises(ValueError,match='有效节点'):
        edit(tmp_path,'chat',bad,base=base)
    assert service.store.get()==saved


@pytest.mark.asyncio
async def test_manual_graph_roundtrip_branch_delete_and_cas(task_api_env, monkeypatch):
    _, _, _, scope, _ = task_api_env
    model = AsyncMock(side_effect=AssertionError('Manual edits must not call a model'))
    monkeypatch.setattr(server.MODEL_REGISTRY, 'catalog', model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test',
                               headers={'X-Mew-Workbench':'1'}) as client:
        async def view():
            response = await client.get('/api/workbench/view', params=scope)
            assert response.status_code == 200, response.text
            return response.json()
        async def change(operation, expected=None):
            current = await view() if expected is None else expected
            body = dict(**scope, task_id=current['task']['id'] if current else '',
                        basis=current['editor']['basis'] if current else '', operation=operation)
            return await client.post('/api/workbench/graph/edit', json=body)
        def node(name, kind='implement'):
            return dict(action='upsert', kind='node', fields=dict(title=name, body='人工填写的目标',
                data=dict(node_kind=kind, inputs=['原始资料'], outputs=['结果'], acceptance=['结果可核验'])))
        first = await change(node('核验结果', 'decision'))
        assert first.status_code == 200, first.text
        a = first.json()['changed_ids'][0]
        b = (await change(node('整理报告'))).json()['changed_ids'][0]
        c = (await change(node('补充资料'))).json()['changed_ids'][0]
        for target, condition in [(b,'pass'), (c,'fail')]:
            linked = await change(dict(action='upsert', kind='edge', fields=dict(data={
                'from_node':a, 'to_node':target, 'condition':condition})))
            assert linked.status_code == 200, linked.text
        connected = await view()
        assert {row['condition'] for row in connected['structure']['edges']} == {'pass','fail'}
        assert connected['structure']['nodes'][0]['kind'] == 'judge'
        assert connected['structure']['workflow'] is True
        assert connected['task']['intent'] == 'plan' and not connected['task']['run_id']
        assert connected['task']['status'] == '已暂停'
        positioned = await change(dict(action='positions', positions={a:dict(x=320,y=440)}))
        assert positioned.status_code == 200, positioned.text
        assert (await view())['editor']['positions'][a] == dict(x=320,y=440)
        # One tab cannot silently replace another tab's node changes.
        newer = await change(dict(action='upsert', kind='node', id=b, fields=dict(title='更新报告')))
        assert newer.status_code == 200
        rejected = await change(dict(action='delete', id=a), connected)
        assert rejected.status_code == 409
        assert len((await view())['structure']['edges']) == 2
        deleted = await change(dict(action='delete', id=a))
        assert deleted.status_code == 200, deleted.text
        final = await view()
        assert len(final['structure']['nodes']) == 2 and not final['structure']['edges']
        state = DraftStore(scope['project'], scope['conversation_id']).get()
        retired = [e for e in state['materials']['document']['entries'] if e['state']=='retired']
        assert len(retired) == 3 and all(e['evidence']=='manual' for e in retired)
        assert state['materials']['versions'][-1]['changes']
        assert len(state['materials']['sources']) == 7
    model.assert_not_called()


@pytest.mark.asyncio
async def test_manual_edit_authority_scope_and_unknown_fields(task_api_env, monkeypatch):
    _, _, _, scope, _ = task_api_env
    service = TaskDesign(scope['project'], scope['conversation_id'])
    service.start('execute')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        current = (await client.get('/api/workbench/view', params=scope)).json()
        body = dict(**scope, task_id=current['task']['id'], basis=current['editor']['basis'],
            operation=dict(action='upsert',kind='node',fields=dict(title='新增',data={'node_kind':'implement'})))
        before = service.store.get()
        assert (await client.post('/api/workbench/graph/edit',json=body)).status_code == 403
        headers={'X-Mew-Workbench':'1'}
        assert (await client.post('/api/workbench/graph/edit',json=body,headers={**headers,'Origin':'https://other.example'})).status_code == 403
        assert (await client.post('/api/workbench/graph/edit',json={**body,'conversation_id':'different'},headers=headers)).status_code == 409
        bad = copy.deepcopy(body);bad['operation']['fields']['data']['execute']=True
        assert (await client.post('/api/workbench/graph/edit',json=bad,headers=headers)).status_code == 409
        monkeypatch.setattr(server.SESSION, 'permission_mode', PermissionMode.PLAN)
        assert (await client.post('/api/workbench/graph/edit',json=body,headers=headers)).status_code == 409
        assert service.store.get() == before
        monkeypatch.setattr(server.SESSION, 'permission_mode', PermissionMode.DEFAULT)
        result = await client.post('/api/workbench/graph/edit',json=body,headers=headers)
        assert result.status_code == 200, result.text
        state = service.store.get()
        assert state['task_design']['intent'] == 'execute'  # Editing never grants or changes execution intent.
        assert not result.json()['execution_started']
        with service.store.db() as db:
            state = service.store._get(db)
            state['task_design'].update(run_id='bound-run',status='paused')
            service.store._put(db,state)
        from mewcode.orchestration.manual_graph import snapshot, edit
        body['basis'] = snapshot(state)['basis']
        before = service.store.get()
        with pytest.raises(ValueError, match='运行状态'):
            edit(scope['project'],scope['conversation_id'],body)
        assert service.store.get() == before
