import json

import httpx
import pytest

from mewcode.workflow_core.connections import descriptors, listing, origin, register


def connect(tmp_path, **changes):
    return register(**dict(url='http://127.0.0.1:8794', run_id='a' * 32, conversation_id='conversation',
                           project=str(tmp_path / 'project'), title='四书编排', profile=tmp_path, **changes))


@pytest.mark.parametrize('url', ['https://127.0.0.1:8794', 'http://example.com:8794', 'http://localhost:8794',
    'http://127.0.0.1:8794/unrelated', 'http://user:pass@127.0.0.1:8794',
    'http://127.0.0.1:8794?target=secret', 'http://127.0.0.1:99999'])
def test_only_explicit_loopback_origins_are_connections(url):
    with pytest.raises(ValueError):
        origin(url)


@pytest.mark.asyncio
async def test_connection_reads_authoritative_status_without_mutations_or_run_store(tmp_path):
    item = connect(tmp_path)
    value = dict(task=dict(mode='workflow', run_id=item['run_id'], conversation_id=item['conversation_id'],
                          execution_status='running'), structure=dict(nodes=[{'status': '已完成'}, {'status': '进行中'}]))
    requests = []
    def respond(request):
        assert dict(request.url.params) == {key: item[key] for key in ('run_id', 'conversation_id', 'project')}
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json=value)
    transport = httpx.MockTransport(respond)
    first = (await listing(profile=tmp_path, transport=transport))['connections'][0]
    assert first['available'] and first['completed'] == 1 and first['nodes'] == 2
    assert first['url'] == '/workbench/connections/' + item['id']
    value['task']['execution_status'] = 'completed'
    value['structure']['nodes'][1]['status'] = '已完成'
    second = (await listing(profile=tmp_path, transport=transport))['connections'][0]
    assert second['completed'] == 2 and second['status_label'] == '已完成'
    assert requests == [('GET', '/api/workbench/view')] * 2
    assert not (tmp_path / 'expert-runs-v1').exists()
    assert descriptors(tmp_path / 'another-profile') == []


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['run', 'conversation', 'project', 'redirect', 'offline'])
async def test_reused_port_or_unavailable_host_does_not_offer_a_live_page(tmp_path, failure):
    item = connect(tmp_path)
    value = dict(task=dict(mode='workflow', run_id=item['run_id'], conversation_id=item['conversation_id'],
                          execution_status='running'), structure=dict(nodes=[]))
    if failure in {'run', 'conversation'}:
        value['task'][{'run': 'run_id', 'conversation': 'conversation_id'}[failure]] = 'different'
    def respond(request):
        if failure == 'offline':
            raise httpx.ConnectError('offline', request=request)
        if failure == 'redirect':
            return httpx.Response(302, headers={'location': 'https://example.com'})
        if failure == 'project':
            return httpx.Response(400, json={'error': '项目或对话已切换，请回到任务所属对话。'})
        return httpx.Response(200, json=value)
    result = (await listing(profile=tmp_path, transport=httpx.MockTransport(respond)))['connections'][0]
    assert not result['available'] and 'url' not in result


def test_invalid_descriptor_is_ignored_and_reregistration_is_atomic(tmp_path):
    item = connect(tmp_path)
    folder = tmp_path / 'workflow-connections-v1'
    (folder / ('b' * 32 + '.json')).write_text(json.dumps({'url': 'file:///secret'}), encoding='utf-8')
    assert [row['id'] for row in descriptors(tmp_path)] == [item['id']]
    updated = register(url='http://127.0.0.1:8795', run_id=item['id'], conversation_id='conversation',
                       project=item['project'], title='新的连接', profile=tmp_path)
    assert descriptors(tmp_path)[0] == updated
