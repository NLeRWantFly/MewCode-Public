import json

import httpx
import pytest
from fastapi import FastAPI

from frontend import workbench_connections as relay
from mewcode.workflow_core.connections import register


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    item = register(url='http://127.0.0.1:8794', run_id='a' * 32, conversation_id='conversation',
                    project=str(tmp_path), title='测试 </script> 工作流', profile=tmp_path)
    requests = []
    state = {'identity': item['run_id'], 'offline': False, 'redirect': False}
    def respond(request):
        requests.append(request)
        if state['offline']:
            raise httpx.ConnectError('offline', request=request)
        if state['redirect']:
            return httpx.Response(302, headers={'location': 'http://127.0.0.1:8795/'})
        if request.url.path == '/api/workbench/view':
            return httpx.Response(200, json={'task': {'mode': 'workflow', 'run_id': state['identity'],
                'conversation_id': item['conversation_id'], 'execution_status': 'running'}, 'structure': {'nodes': []}})
        if request.url.path == '/api/workbench/artifact':
            return httpx.Response(200, content=b'actual artifact bytes', headers={
                'Content-Type': 'application/octet-stream', 'Content-Disposition': 'attachment; filename="result.txt"'})
        return httpx.Response(200, json={'received': json.loads(request.content) if request.content else dict(request.url.params)})
    async_client = httpx.AsyncClient
    monkeypatch.setattr(relay, 'descriptor', lambda key: item if key == item['id'] else None)
    monkeypatch.setattr(relay.httpx, 'AsyncClient', lambda **kwargs: async_client(transport=httpx.MockTransport(respond), **kwargs))
    app = FastAPI()
    relay.install(app)
    return item, requests, state, async_client(transport=httpx.ASGITransport(app), base_url='http://client')


@pytest.mark.asyncio
async def test_reads_and_artifacts_use_bound_owner_without_forwarding_client_credentials(fixture):
    item, requests, _, client = fixture
    prefix = '/api/workbench/connections/' + item['id']
    async with client:
        detail = await client.get(prefix + '/api/workbench/node', params={'node_id': 'math'}, headers={
            'Cookie': 'desktop-session=secret', 'Authorization': 'Bearer secret'})
        assert detail.status_code == 200
        assert detail.json()['received'] == {**{key: item[key] for key in ('run_id', 'conversation_id', 'project')}, 'node_id': 'math'}
        artifact = await client.get(prefix + '/api/workbench/artifact', params={'node_id': 'math', 'path': 'result.txt'})
        assert artifact.content == b'actual artifact bytes'
        assert artifact.headers['content-disposition'] == 'attachment; filename="result.txt"'
        assert artifact.headers['cache-control'] == 'no-store'
    assert all('cookie' not in r.headers and 'authorization' not in r.headers for r in requests)
    assert [r.url.path for r in requests] == ['/api/workbench/node', '/api/workbench/view', '/api/workbench/artifact']


@pytest.mark.asyncio
async def test_edit_and_node_message_reach_same_owner_exactly_once(fixture):
    item, requests, _, client = fixture
    prefix = '/api/workbench/connections/' + item['id']
    async with client:
        for path, payload in [('graph/edit', {'operation': {'kind': 'move', 'node_id': 'math'}}),
                              ('node-message', {'node_id': 'math', 'message': '请解释结果', 'request_id': 'unique'})]:
            response = await client.post(prefix + '/api/workbench/' + path, json=payload,
                headers={'X-Mew-Workbench': '1', 'Origin': 'http://client'})
            assert response.status_code == 200
            assert response.json()['received'] == {**payload, **{key: item[key] for key in ('run_id', 'conversation_id', 'project')}}
    writes = [r for r in requests if r.method == 'POST']
    assert len(writes) == 2 and all(r.headers['origin'] == item['url'] for r in writes)


@pytest.mark.asyncio
async def test_connection_rejects_other_scope_endpoints_and_cross_site_writes(fixture):
    item, requests, _, client = fixture
    prefix = '/api/workbench/connections/' + item['id']
    async with client:
        for key in ('run_id', 'conversation_id', 'project'):
            response = await client.get(prefix + '/api/workbench/view', params={key: 'different'})
            assert response.status_code == 400
        for headers in ({}, {'X-Mew-Workbench': '1', 'Origin': 'http://other'},
                        {'X-Mew-Workbench': '1', 'Sec-Fetch-Site': 'cross-site'}):
            assert (await client.post(prefix + '/api/workbench/run-action', json={'operation': 'stop'}, headers=headers)).status_code == 403
        assert (await client.get(prefix + '/api/settings')).status_code == 404
        assert (await client.get('/api/workbench/connections/' + 'b' * 32 + '/api/workbench/view')).status_code == 404
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['identity', 'offline', 'redirect'])
async def test_unavailable_or_reused_owner_never_receives_a_mutation(fixture, failure):
    item, requests, state, client = fixture
    state[failure] = 'other' if failure == 'identity' else True
    async with client:
        response = await client.post('/api/workbench/connections/' + item['id'] + '/api/workbench/run-action',
            json={'operation': 'resume'}, headers={'X-Mew-Workbench': '1'})
    assert response.status_code == 409
    assert all(r.method == 'GET' for r in requests)


@pytest.mark.asyncio
async def test_page_bootstrap_is_bound_and_escapes_script_content(fixture):
    item, requests, _, client = fixture
    async with client:
        response = await client.get('/workbench/connections/' + item['id'])
    assert response.status_code == 200
    assert '<title>测试 &lt;/script&gt; 工作流</title>' in response.text
    assert '"run_id": "' + item['id'] + '"' in response.text
    assert 'connection-page.js' in response.text
    assert requests == []
