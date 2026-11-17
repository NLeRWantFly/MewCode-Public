import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from frontend.orch_startup import activate_saved_graph, resume_pending_gate, value_sha256, retry_graph_generation


@pytest.mark.asyncio
@pytest.mark.parametrize('guard', ['', 'project', 'conversation', 'graph', 'existing', 'models'])
async def test_explicit_cli_start_uses_normal_confirm_once_and_never_overrides_authority(tmp_path, guard):
    project = tmp_path / 'project'
    folder = project / '.mewcode/pipelines'
    folder.mkdir(parents=True)
    graph = folder / 'g_saved.json'
    graph.write_text('saved graph', encoding='utf-8')
    data = {'operation_id':'a'*32, 'project':str(project), 'conversation_id':'conv',
        'graph_id':'g_saved', 'graph_sha256':hashlib.sha256(graph.read_bytes()).hexdigest()}
    if guard == 'project': data['project'] = str(tmp_path)
    elif guard == 'conversation': data['conversation_id'] = 'other'
    elif guard == 'graph': data['graph_sha256'] = '0'*64
    elif guard == 'models': data['bindings'] = {'executor': 'unapproved model'}
    path = tmp_path / 'request.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    spec = SimpleNamespace(meta={'pipeline_graph_id':'g_saved','conversation_id':'conv'}, as_dict=lambda:{'saved':True})
    confirm = AsyncMock(return_value={'started':True,'execution_mode':'new'})
    async def invoke():
        return await activate_saved_graph(path, project=project, conversation_id='conv', saved_spec=spec,
            list_runs=lambda _: [{'run_id':'old'}] if guard == 'existing' else [],
            confirm=confirm, permission_mode='auto')
    result = await invoke()
    if guard:
        assert not result['started'] and result['error'] and not confirm.await_count
    else:
        assert result['started']
        confirm.assert_awaited_once_with({'conversation_id':'conv','spec':{'saved':True},
            'activate':True,'permission_mode':'auto'})
        again = await invoke()
        assert not again['started'] and confirm.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('guard', ['', 'waiting', 'project', 'conversation', 'graph', 'running', 'completed', 'not_gate', 'request', 'inputs', 'models', 'extra'])
async def test_explicit_resume_only_continues_exact_stopped_gate_without_approving_it(tmp_path, guard):
    project = tmp_path / 'project'
    folder = project / '.mewcode/pipelines'
    folder.mkdir(parents=True)
    graph = folder / 'g_saved.json'
    graph.write_text('saved graph', encoding='utf-8')
    record = {'run_id': 'b' * 32, 'conversation_id': 'conv', 'status': 'blocked', 'active_node_ids': [],
        'locked_flow': {'meta': {'pipeline_graph_id': 'g_saved', 'node_kinds': {'approval': 'gate'}},
                        'nodes': [{'node_id': 'approval', 'role': 'manager'}]},
        'run_inputs': {'cost': '现有额度，用完暂停'},
        'node_states': {'approval': {'status': 'blocked', 'pending_interaction': {'request_id': 'pending'}}}}
    data = {'operation_id': 'a' * 32, 'project': str(project), 'conversation_id': 'conv',
        'graph_id': 'g_saved', 'graph_sha256': hashlib.sha256(graph.read_bytes()).hexdigest(),
        'run_id': record['run_id'], 'node_id': 'approval', 'pending_request_id': 'pending',
        'inputs_sha256': value_sha256(record['run_inputs']), 'locked_flow_sha256': value_sha256(record['locked_flow'])}
    if guard == 'project': data['project'] = str(tmp_path)
    elif guard == 'waiting':
        record.update(status='running', active_node_ids=['approval'],
            gate_dialogues={'approval': {'scope_hash': 'scope', 'proposal': 'same plan'}})
        record['node_states']['approval']['pending_interaction'].update(kind='human_oncall', tool='human_oncall',
            scope_hash='scope:' + value_sha256('same plan'))
    elif guard == 'conversation': record['conversation_id'] = 'other'
    elif guard == 'graph': graph.write_text('changed graph')
    elif guard == 'running': record.update(status='running', active_node_ids=['approval'])
    elif guard == 'completed': record['node_states']['approval']['status'] = 'completed'
    elif guard == 'not_gate': record['locked_flow']['meta']['node_kinds']['approval'] = 'executor'
    elif guard == 'request': record['node_states']['approval']['pending_interaction']['request_id'] = 'other'
    elif guard == 'inputs': record['run_inputs']['cost'] = 'changed'
    elif guard == 'models': record['locked_flow']['nodes'][0]['model'] = {'model_id': 'other'}
    elif guard == 'extra': data['grant_extra_budget'] = True
    path = tmp_path / 'gate.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    resume = AsyncMock(return_value={'run_id': record['run_id'], 'status': 'running'})
    async def invoke():
        return await resume_pending_gate(path, project=project, conversation_id='conv',
            load_run=lambda _: record, resume=resume)
    result = await invoke()
    if guard and guard != 'waiting':
        assert not result['started'] and not resume.await_count
    else:
        assert result['started'] and resume.await_count == 1
        resume.assert_awaited_once_with(record)
        again = await invoke()
        assert not again['started'] and resume.await_count == 1
    assert not record.get('gate_confirmations') and not record.get('authorizations')


@pytest.mark.asyncio
@pytest.mark.parametrize('guard', ['', 'route', 'sources', 'candidate', 'status', 'scope', 'extra'])
async def test_graph_retry_binds_failed_candidate_route_and_sources_without_execution(tmp_path, guard):
    state = {'candidate': {'id': 'b'*32, 'sequence': 3, 'status': 'failed'}, 'source_ids': ['first', 'second']}
    route = {'provider_key': 'chosen', 'model': 'selected', 'requested_effort': 'max'}
    data = {'operation_id': 'a'*32, 'project': str(tmp_path), 'conversation_id': 'graph', 'draft_id': 'main',
        'candidate_id': 'b'*32, 'sequence': 3, 'sources_sha256': value_sha256(state['source_ids']),
        'provider_key': 'chosen', 'model': 'selected', 'effort': 'max'}
    if guard == 'route': route['requested_effort'] = 'high'
    elif guard == 'sources': state['source_ids'].append('new')
    elif guard == 'candidate': state['candidate']['id'] = 'c'*32
    elif guard == 'status': state['candidate']['status'] = 'working'
    elif guard == 'scope': data['conversation_id'] = 'other'
    elif guard == 'extra': data['activate'] = True
    path = tmp_path / 'retry.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    update = AsyncMock(return_value={'http_status': 202})
    async def invoke():
        return await retry_graph_generation(path, project=tmp_path, conversation_id='graph',
            read_state=lambda *_: state, selected_route=lambda: route, update=update)
    result = await invoke()
    if guard:
        assert not result['started'] and not update.await_count
    else:
        assert result['started']
        update.assert_awaited_once_with({'project': str(tmp_path), 'conversation_id': 'graph', 'draft_id': 'main'})
        again = await invoke()
        assert not again['started'] and update.await_count == 1
