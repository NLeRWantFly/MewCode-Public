from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from mewcode.permissions.network_policy import (
    NetworkDenied, NetworkPolicy, NetworkPolicyStore, policy_for, task_network_scope,
)
from mewcode.permissions.policy import build_permission_checker
from mewcode.tools.browser import WebFetch
from mewcode.tools.network_transport import PolicyTransport, checked_addresses


def test_default_enabled_and_settings_survive_restart(tmp_path):
    store = NetworkPolicyStore(tmp_path / 'network.json')
    assert store.read().enabled
    current = store.save({'enabled': False, 'denied_domains': ['EXAMPLE.COM.', '*.例子.中国']},
                         expected_revision=store.read().revision)
    restored = NetworkPolicyStore(store.path).read()
    assert restored == current
    assert 'example.com' in current.denied_domains
    assert '*.xn--fsqu00a.xn--fiqs8s' in current.denied_domains
    with pytest.raises(ValueError, match='已变化'):
        store.save({'enabled': True}, expected_revision=NetworkPolicy().revision)


@pytest.mark.parametrize('host', ['evil.example', 'a.evil.example', 'EVIL.EXAMPLE.'])
def test_blacklist_parent_and_subdomains(host):
    policy = NetworkPolicy.from_dict({'denied_domains': ['evil.example']})
    with pytest.raises(NetworkDenied, match='evil.example'):
        policy.check_host(host)
    assert policy.check_host('not-evil.example') == 'not-evil.example'


@pytest.mark.parametrize('entry', ['https://example.com', 'example.com/path', 'example.com:443',
    '*.127.0.0.1', 'bad\x00.example', 'bad..example', '-bad.example'])
def test_bad_blacklist_entries(entry):
    with pytest.raises(ValueError):
        NetworkPolicy.from_dict({'denied_domains': [entry]})


def test_ipv6_and_disabled():
    policy = NetworkPolicy.from_dict({'denied_domains': ['[::1]']})
    with pytest.raises(NetworkDenied, match='BLACKLISTED'):
        policy.check_url('http://[::1]:8080')
    with pytest.raises(NetworkDenied, match='DISABLED'):
        NetworkPolicy(False).check_url('https://example.com')


def test_children_observe_new_settings(tmp_path):
    parent = build_permission_checker(tmp_path)
    parent.network_policy_store = NetworkPolicyStore(tmp_path / 'network.json')
    child = build_permission_checker(tmp_path, parent_policy=parent)
    before = policy_for(child)
    parent.network_policy_store.save({'enabled': False}, expected_revision=before.revision)
    assert not policy_for(child).enabled
    assert child.check(WebFetch(), {'url': 'https://example.com'}).subcondition == 'network_disabled'


def test_readonly_web_allowed_without_domain_card(tmp_path):
    checker = build_permission_checker(tmp_path)
    checker.network_policy = NetworkPolicy()
    decision = checker.check(WebFetch(), {'url': 'https://example.com'})
    assert decision.effect == 'allow'
    assert decision.source == 'network_policy'


@pytest.mark.asyncio
async def test_transport_checks_every_redirect_and_does_not_upload_on_deny():
    requests = []
    async def handler(reader, writer):
        header = await reader.readuntil(b'\r\n\r\n')
        requests.append(header)
        writer.write(b'HTTP/1.1 302 Found\r\nLocation: http://evil.example/target\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
        await writer.drain(); writer.close(); await writer.wait_closed()
    server = await asyncio.start_server(handler, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    owner = SimpleNamespace(network_policy=NetworkPolicy(True, ('*.evil.example',)))
    try:
        with task_network_scope(owner):
            async with httpx.AsyncClient(transport=PolicyTransport(), follow_redirects=True) as client:
                with pytest.raises(NetworkDenied, match='BLACKLISTED'):
                    await client.get(f'http://127.0.0.1:{port}/start')
        assert len(requests) == 1
    finally:
        server.close(); await server.wait_closed()


@pytest.mark.asyncio
async def test_disabled_transport_does_not_connect(monkeypatch):
    called = []
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', lambda *a, **kw: called.append(a))
    with task_network_scope(SimpleNamespace(network_policy=NetworkPolicy(False))):
        async with httpx.AsyncClient(transport=PolicyTransport()) as client:
            with pytest.raises(NetworkDenied, match='DISABLED'):
                await client.post('http://example.com', json={'synthetic': 'sample'})
    assert not called


@pytest.mark.asyncio
async def test_dns_alias_cannot_reach_blacklisted_address(monkeypatch):
    async def resolve(host, port, **kwargs):
        return [(2, 1, 6, '', ('127.0.0.2', port))]
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
    with task_network_scope(SimpleNamespace(network_policy=NetworkPolicy(True, ('evil.example',)))):
        with pytest.raises(NetworkDenied, match='evil.example'):
            await checked_addresses('alias.example', 80)


def _set_proxy_environment(monkeypatch, proxy_url):
    for name in ('http_proxy', 'https_proxy', 'all_proxy', 'HTTP_PROXY', 'HTTPS_PROXY',
                 'ALL_PROXY', 'NO_PROXY', 'no_proxy'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('HTTP_PROXY', proxy_url)
    monkeypatch.setenv('HTTPS_PROXY', proxy_url)


@pytest.mark.asyncio
async def test_task_transport_keeps_http_and_https_environment_proxies(monkeypatch):
    requests = []

    async def handler(reader, writer):
        line = await reader.readline()
        while await reader.readline() != b'\r\n':
            pass
        requests.append(line.decode('ascii').strip())
        if line.startswith(b'CONNECT '):
            writer.write(b'HTTP/1.1 502 Proxy test complete\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
        else:
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok')
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    _set_proxy_environment(monkeypatch, f'http://127.0.0.1:{port}')
    loop = asyncio.get_running_loop()
    original_getaddrinfo = loop.getaddrinfo

    async def resolve(host, port, **kwargs):
        if host == 'public.example':
            return [(2, 1, 6, '', ('203.0.113.10', port))]
        return await original_getaddrinfo(host, port, **kwargs)

    monkeypatch.setattr(loop, 'getaddrinfo', resolve)
    try:
        with task_network_scope(SimpleNamespace(network_policy=NetworkPolicy())):
            async with httpx.AsyncClient(transport=PolicyTransport(), follow_redirects=True) as client:
                response = await client.get('http://public.example/resource')
                assert response.status_code == 200
                assert response.text == 'ok'
                with pytest.raises(httpx.ProxyError):
                    await client.get('https://public.example/secure')
        assert requests == [
            'GET http://203.0.113.10/resource HTTP/1.1',
            'CONNECT 203.0.113.10:443 HTTP/1.1',
        ]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('host', 'denied'),
    [
        ('evil.example', ('evil.example',)),
        ('child.evil.example', ('evil.example',)),
        ('127.0.0.2', ('127.0.0.2',)),
    ],
)
async def test_proxy_transport_blocks_blacklisted_domain_subdomain_and_ip(
    monkeypatch, host, denied,
):
    requests = []

    async def handler(reader, writer):
        requests.append(await reader.readuntil(b'\r\n\r\n'))
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    _set_proxy_environment(monkeypatch, f'http://127.0.0.1:{port}')
    loop = asyncio.get_running_loop()
    original_getaddrinfo = loop.getaddrinfo

    async def resolve(target, target_port, **kwargs):
        if target in {'evil.example', 'child.evil.example'}:
            return [(2, 1, 6, '', ('203.0.113.11', target_port))]
        return await original_getaddrinfo(target, target_port, **kwargs)

    monkeypatch.setattr(loop, 'getaddrinfo', resolve)
    try:
        with task_network_scope(SimpleNamespace(network_policy=NetworkPolicy(True, denied))):
            async with httpx.AsyncClient(transport=PolicyTransport()) as client:
                with pytest.raises(NetworkDenied, match='BLACKLISTED'):
                    await client.get(f'http://{host}/blocked')
        assert requests == []
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_proxy_transport_blocks_dns_alias_of_blacklisted_ip(monkeypatch):
    requests = []

    async def handler(reader, writer):
        requests.append(await reader.readuntil(b'\r\n\r\n'))
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, '127.0.0.1', 0)
    proxy_port = server.sockets[0].getsockname()[1]
    _set_proxy_environment(monkeypatch, f'http://127.0.0.1:{proxy_port}')
    loop = asyncio.get_running_loop()
    original_getaddrinfo = loop.getaddrinfo

    async def resolve(host, port, **kwargs):
        if host in {'alias.example', 'blocked.example'}:
            return [(2, 1, 6, '', ('127.0.0.2', port))]
        return await original_getaddrinfo(host, port, **kwargs)

    monkeypatch.setattr(loop, 'getaddrinfo', resolve)
    try:
        policy = NetworkPolicy(True, ('blocked.example',))
        with task_network_scope(SimpleNamespace(network_policy=policy)):
            async with httpx.AsyncClient(transport=PolicyTransport()) as client:
                with pytest.raises(NetworkDenied, match='blocked.example'):
                    await client.get('http://alias.example/path')
        assert requests == []
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_proxy_redirect_target_is_checked_before_second_request(monkeypatch):
    requests = []

    async def handler(reader, writer):
        requests.append(await reader.readuntil(b'\r\n\r\n'))
        writer.write(
            b'HTTP/1.1 302 Found\r\nLocation: http://child.evil.example/target\r\n'
            b'Content-Length: 0\r\nConnection: close\r\n\r\n'
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    _set_proxy_environment(monkeypatch, f'http://127.0.0.1:{port}')
    loop = asyncio.get_running_loop()
    original_getaddrinfo = loop.getaddrinfo

    async def resolve(host, target_port, **kwargs):
        if host == 'public.example':
            return [(2, 1, 6, '', ('203.0.113.12', target_port))]
        return await original_getaddrinfo(host, target_port, **kwargs)

    monkeypatch.setattr(loop, 'getaddrinfo', resolve)
    try:
        with task_network_scope(
            SimpleNamespace(network_policy=NetworkPolicy(True, ('*.evil.example',)))
        ):
            async with httpx.AsyncClient(transport=PolicyTransport(), follow_redirects=True) as client:
                with pytest.raises(NetworkDenied, match='evil.example'):
                    await client.get('http://public.example/start')
        assert len(requests) == 1
        assert b'GET http://203.0.113.12/start HTTP/1.1' in requests[0]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_policy_revision_retires_pool_after_active_response_finishes(monkeypatch):
    slow_headers_sent = asyncio.Event()
    release_slow_body = asyncio.Event()
    owner = SimpleNamespace(network_policy=NetworkPolicy())

    async def handler(reader, writer):
        request = await reader.readuntil(b'\r\n\r\n')
        if b' /slow ' in request.split(b'\r\n', 1)[0]:
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\n')
            await writer.drain()
            slow_headers_sent.set()
            await release_slow_body.wait()
            writer.write(b'hello')
        else:
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 4\r\nConnection: close\r\n\r\nfast')
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setenv('NO_PROXY', '*')
    monkeypatch.setenv('no_proxy', '*')
    try:
        with task_network_scope(owner):
            async with httpx.AsyncClient(transport=PolicyTransport()) as client:
                request = client.build_request('GET', f'http://127.0.0.1:{port}/slow')
                slow_response = await client.send(request, stream=True)
                await asyncio.wait_for(slow_headers_sent.wait(), timeout=2)
                owner.network_policy = NetworkPolicy(True, ('blocked.example',))
                fast_response = await client.get(f'http://127.0.0.1:{port}/fast')
                assert fast_response.text == 'fast'
                release_slow_body.set()
                assert (await asyncio.wait_for(slow_response.aread(), timeout=2)) == b'hello'
                await slow_response.aclose()
    finally:
        release_slow_body.set()
        server.close()
        await server.wait_closed()


def test_bad_config_fails_closed(tmp_path):
    path = tmp_path / 'network.json'
    path.write_text(json.dumps({'enabled': 'yes'}))
    with pytest.raises(NetworkDenied, match='POLICY_INVALID'):
        NetworkPolicyStore(path).read()
