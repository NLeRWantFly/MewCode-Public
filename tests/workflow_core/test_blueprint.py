import copy
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from frontend import server
from mewcode.expert_runs import ExpertRunStore
from mewcode.workflow_core.blueprint import documents, extract
from mewcode.workflow_core.runtime import Core, MemoryBackend
from mewcode.workflow_core.schema import normalize, walk
from mewcode.workflow_core.state import fresh
from mewcode.workflow_core.store import ensure
from mewcode.workflow_core.web import envelope
from tests.test_task_design_entry import task_api_env
from tests.workflow_core.test_runtime import graph

MODEL = dict(provider_key='codex:custom', model_id='gpt-6.1-sol', effort='max')


def specimen():
    inner = graph(['leaf'])
    inner['layout'] = {'leaf': {'x': 23, 'y': -8}}
    body = graph([dict(id='choose', kind='branch', config={'source': 'inputs.item',
        'cases': [{'equals': 'a', 'port': 'alpha'}, {'equals': 'b', 'port': 'beta'}], 'default': 'none'}),
        'a', 'b', dict(id='twice', kind='loop', config={'mode': 'count', 'count': 2, 'graph': inner})],
        [('choose', 'a', 'alpha'), ('choose', 'b', 'beta'), ('a', 'twice', 'always'), ('b', 'twice', 'always')])
    result = graph([dict(id='prepare', task='完整任务：保留 ```json 示例和特殊字符。', tools=['ReadFile'], deny_tools=['DeleteFile'],
        outputs=[{'name': 'index.json', 'contract': {'source': 'page'}}], model=MODEL, extra={'future_field': ['keep', 4]}),
        dict(id='judge', kind='judge', config={'expression': {'value': 2, 'op': 'gte', 'right': 1}}),
        dict(id='loop', kind='loop', config={'mode': 'foreach', 'items': ['a', 'b'], 'graph': body}),
        'independent', 'join'], [('prepare', 'judge', 'always'), ('judge', 'loop', 'pass'),
                              ('loop', 'join', 'always'), ('independent', 'join', 'always')])
    result.update(label='带循环的构造参考', goal='保留独立分支与条件汇合', layout={'prepare': {'x': -40, 'y': 16}, 'loop': {'x': 400, 'y': 180}},
                  metadata={'custom_contract': {'units': 'mm'}})
    return normalize(result)


def record(state):
    return dict(run_id='a' * 32, conversation_id='chat', kernel_revision=12, updated_at='2026-11-09',
                workflow_core=state, locked_profile={'executor': MODEL},
                locked_flow={'meta': {'run_input_contract': {'name': 'materials', 'required': True},
                                      'goal_contract': {'outputs': ['catalogue']}}})


@pytest.mark.asyncio
async def test_export_roundtrip_preserves_control_flow_nested_layout_and_complete_node_fields():
    original = specimen()
    traces = []
    async def run(flow):
        trace = []
        async def execute(ctx):
            trace.append((ctx.node_id, ctx.iteration))
            return dict(text='已交付，仍有1项待复核。')
        state = await Core(MemoryBackend(fresh(flow)), execute).run()
        traces.append(sorted(trace))
        return state
    state = await run(original)
    before = copy.deepcopy(state)
    packet = extract(record(state))
    restored = json.loads(documents(packet)['documents']['json']['text'])
    assert restored['workflow'] == original
    assert restored['execution_context']['default_model'] == MODEL
    assert restored['execution_context']['goal_contract'] == {'outputs': ['catalogue']}
    assert restored['source']['selection'] == 'applied'
    assert normalize(restored['workflow']) == original
    rerun = await run(restored['workflow'])
    assert rerun['status'] == 'completed' and traces[0] == traces[1]
    assert state == before
    texts = documents(packet)
    assert texts['node_count'] == len(list(walk(original)))
    assert 'FOR_EACH ["a","b"]' in texts['documents']['pseudocode']['text']
    assert 'REPEAT 2' in texts['documents']['pseudocode']['text']
    assert 'SELECT_PORT_FROM {"path":"inputs.item"}' in texts['documents']['pseudocode']['text']
    assert 'future_field' in texts['documents']['brief']['text']
    assert '循环体' in texts['documents']['brief']['text']
    assert any(row['unresolved_notes'] for row in packet['summary']['nodes'])


def test_draft_is_explicit_and_summary_uses_published_delivery_without_tool_history():
    class NeverCopy:
        def __deepcopy__(self, _):
            raise AssertionError('raw checkpoints must not be read or copied')
    state = fresh(graph(['a']))
    state['draft'] = graph(['a', 'later'])
    state['draft']['revision'] = 2
    state['attempts'] = {'raw': {'messages': NeverCopy(), 'diagnostics': NeverCopy()}}
    state['nodes']['a'] = {'status': 'completed', 'published_delivery_id': 'v2', 'result': {'text': 'old answer'}}
    state['deliveries'] = {'v2': {'result': {'text': '新版本结果；仍有2项未解决。', 'value': False, 'artifacts': []}}}
    state['node_requests'] = {'ask': dict(node_id='a', kind='ask', status='completed', message='解释决定', reply={'text': '采用新版格式'})}
    applied, draft = extract(record(state)), extract(record(state), 'draft')
    assert len(applied['workflow']['nodes']) == 1 and len(draft['workflow']['nodes']) == 2
    assert draft['source']['graph_revision'] == 2 and draft['summary']['graph_revision'] == 1
    assert draft['summary']['nodes'][0]['decision'] is False
    assert '新版本结果' in draft['summary']['nodes'][0]['outcome']
    assert 'messages' not in json.dumps(draft)
    state['draft'] = None
    with pytest.raises(ValueError, match='草稿'):
        extract(record(state), 'draft')


def test_credentials_are_placeholders_without_redacting_model_routes_or_token_limits():
    state = fresh(specimen())
    state['inputs'] = {'api_key': 'secret credential', 'source_dir': 'C:/materials', 'runtime_recovery': {'private': 'not a reusable parameter'}}
    state['bindings'] = {'prepare': {'authorization': 'Bearer secret', 'source_material': 'keep.json',
                                   'recovered_context': {'files': ['old-checkpoint.json']}}}
    state['graph']['nodes'][2]['config']['limits'] = {'tokens': 12000, 'seconds': 500}
    state['graph']['nodes'][0]['inputs'] = [{'name': 'password', 'required': True, 'default': 'secret default'}]
    packet = extract(record(state))
    data = documents(packet)['documents']['json']['text']
    assert 'secret credential' not in data and 'secret default' not in data and 'Bearer secret' not in data
    assert 'runtime_recovery' not in data and packet['redacted_fields']
    assert 'old-checkpoint.json' not in data
    assert packet['execution_context']['input_bindings']['prepare']['source_material'] == 'keep.json'
    assert packet['workflow']['nodes'][0]['model']['provider_key'] == MODEL['provider_key']
    assert packet['workflow']['nodes'][2]['config']['limits']['tokens'] == 12000
    assert packet['workflow']['nodes'][0]['inputs'][0]['name'] == 'password'


@pytest.mark.asyncio
async def test_export_api_is_scoped_read_only_and_does_not_start_models(task_api_env, tmp_path, monkeypatch):
    _, _, _, scope, _ = task_api_env
    store = ExpertRunStore(tmp_path / 'runs', storage='sqlite')
    monkeypatch.setattr(server, 'EXPERT_RUNS', store)
    launch = AsyncMock()
    monkeypatch.setattr(server, 'launch_expert_resume', launch)
    rid, flow = 'd' * 32, specimen()
    store.create(run_id=rid, conversation_id=scope['conversation_id'], task='Reusable', attachments=[],
                 profile={'executor': MODEL}, strategy='stable', flow=envelope(flow))
    backend = ensure(store, rid, flow)
    before = backend.read()
    revision = store.read_workbench(rid)['kernel_revision']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test') as client:
        owner = dict(scope, run_id=rid)
        response = await client.get('/api/workbench/export', params=owner)
        assert response.status_code == 200, response.text
        assert response.headers['cache-control'] == 'no-store'
        payload = response.json()
        assert json.loads(payload['documents']['json']['text'])['workflow'] == flow
        assert json.loads(payload['documents']['json']['text'])['execution_context']['default_model'] == MODEL
        assert (await client.get('/api/workbench/export', params=dict(owner, conversation_id='foreign'))).status_code == 409
        assert (await client.get('/api/workbench/export', params=dict(owner, project=str(tmp_path / 'foreign')))).status_code == 409
        assert (await client.get('/api/workbench/export', params=dict(owner, selection='draft'))).status_code == 409
        assert (await client.post('/api/workbench/export', json=owner)).status_code == 405
    assert backend.read() == before
    assert store.read_workbench(rid)['kernel_revision'] == revision
    launch.assert_not_awaited()
