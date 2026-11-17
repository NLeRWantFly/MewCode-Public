from __future__ import annotations

import asyncio
import ipaddress
import ssl
from contextlib import suppress
from types import SimpleNamespace

import httpx
import pytest

from mewcode.permissions.network_policy import NetworkDenied, NetworkPolicy, task_network_scope
from mewcode.tools.network_transport import (
    PolicyTransport, _ip_authority, _parse_connect_authority, _pinned_address,
)


def _write_localhost_certificate(cert_path, key_path):
    # Generate disposable test credentials; no private key is distributed.
    import os
    from pathlib import Path
    import shutil
    import subprocess
    executable = shutil.which('openssl')
    if not executable:
        for suffix in ('Git/usr/bin/openssl.exe', 'Git/mingw64/bin/openssl.exe'):
            candidate = Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / suffix
            if candidate.is_file():
                executable = str(candidate)
                break
    if not executable:
        pytest.skip('OpenSSL is required to generate a disposable local TLS certificate')
    subprocess.run([executable, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-sha256',
        '-days', '2', '-keyout', str(key_path), '-out', str(cert_path), '-subj', '/CN=localhost',
        '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1'],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _set_proxy_environment(monkeypatch, proxy_url: str) -> None:
    for name in ('http_proxy', 'https_proxy', 'all_proxy', 'HTTP_PROXY', 'HTTPS_PROXY',
                 'ALL_PROXY', 'NO_PROXY', 'no_proxy'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('HTTP_PROXY', proxy_url)
    monkeypatch.setenv('HTTPS_PROXY', proxy_url)


@pytest.mark.asyncio
async def test_rebinding_http_origin_is_pinned_in_proxy_request_line(monkeypatch):
    proxy_requests = []

    async def proxy_handler(reader, writer):
        header = await reader.readuntil(b'\r\n\r\n')
        proxy_requests.append(header)
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok')
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(proxy_handler, '127.0.0.1', 0)
    proxy_port = server.sockets[0].getsockname()[1]
    _set_proxy_environment(monkeypatch, f'http://127.0.0.1:{proxy_port}')
    loop = asyncio.get_running_loop()
    original_getaddrinfo = loop.getaddrinfo
    origin_resolutions = []

    async def resolve(host, port, **kwargs):
        if host == 'rebind.example':
            origin_resolutions.append(port)
            address = '198.51.100.20' if len(origin_resolutions) == 1 else '203.0.113.31'
            return [(2, 1, 6, '', (address, port))]
        return await original_getaddrinfo(host, port, **kwargs)

    monkeypatch.setattr(loop, 'getaddrinfo', resolve)
    try:
        with task_network_scope(SimpleNamespace(network_policy=NetworkPolicy())):
            async with httpx.AsyncClient(transport=PolicyTransport()) as client:
                response = await client.get('http://rebind.example/resource?x=1')
                assert response.status_code == 200
                assert response.text == 'ok'
        assert origin_resolutions == [80, 80]
        assert proxy_requests[0].split(b'\r\n', 1)[0] == (
            b'GET http://203.0.113.31/resource?x=1 HTTP/1.1'
        )
        assert b'Host: rebind.example\r\n' in proxy_requests[0]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_http_proxy_absolute_target_brackets_dns_resolved_ipv6(monkeypatch):
    proxy_requests = []

    async def proxy_handler(reader, writer):
        proxy_requests.append(await reader.readuntil(b'\r\n\r\n'))
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok')
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(proxy_handler, '127.0.0.1', 0)
    proxy_port = server.sockets[0].getsockname()[1]
    _set_proxy_environment(monkeypatch, f'http://127.0.0.1:{proxy_port}')
    loop = asyncio.get_running_loop()
    original_getaddrinfo = loop.getaddrinfo

    async def resolve(host, port, **kwargs):
        if host == 'origin-v6.example':
            return [(10, 1, 6, '', ('2001:db8::23', port, 0, 0))]
        return await original_getaddrinfo(host, port, **kwargs)

    monkeypatch.setattr(loop, 'getaddrinfo', resolve)
    try:
        with task_network_scope(SimpleNamespace(network_policy=NetworkPolicy())):
            async with httpx.AsyncClient(transport=PolicyTransport()) as client:
                response = await client.get('http://origin-v6.example/resource?q=1')
                assert response.status_code == 200
                assert response.text == 'ok'
        assert proxy_requests[0].split(b'\r\n', 1)[0] == (
            b'GET http://[2001:db8::23]/resource?q=1 HTTP/1.1'
        )
        assert b'Host: origin-v6.example\r\n' in proxy_requests[0]
    finally:
        server.close()
        await server.wait_closed()


async def _relay_proxy_tunnel(client_reader, client_writer, upstream_reader, upstream_writer):
    async def pump(reader, writer):
        try:
            while chunk := await reader.read(65536):
                writer.write(chunk)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            with suppress(AttributeError, OSError, RuntimeError):
                writer.write_eof()

    pumps = [asyncio.create_task(pump(client_reader, upstream_writer)),
             asyncio.create_task(pump(upstream_reader, client_writer))]
    try:
        await asyncio.wait_for(asyncio.gather(*pumps), timeout=10)
    finally:
        for pump_task in pumps:
            if not pump_task.done():
                pump_task.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)
        upstream_writer.close()
        with suppress(OSError):
            await upstream_writer.wait_closed()
        client_writer.close()
        with suppress(OSError):
            await client_writer.wait_closed()


@pytest.mark.asyncio
async def test_https_proxy_pins_connect_but_preserves_origin_host_and_sni(monkeypatch, tmp_path):
    cert_path = tmp_path / 'localhost-cert.pem'
    key_path = tmp_path / 'localhost-key.pem'
    _write_localhost_certificate(cert_path, key_path)

    sni_names = []
    inner_requests = []

    def capture_sni(_ssl_object, server_name, _context):
        sni_names.append(server_name)

    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    server_context.set_servername_callback(capture_sni)

    async def tls_handler(reader, writer):
        header = await reader.readuntil(b'\r\n\r\n')
        inner_requests.append(header)
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello')
        await writer.drain()
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()

    tls_server = await asyncio.start_server(
        tls_handler, '127.0.0.1', 0, ssl=server_context
    )
    tls_port = tls_server.sockets[0].getsockname()[1]

    proxy_requests = []
    proxy_errors = []

    async def proxy_handler(reader, writer):
        upstream_writer = None
        try:
            header = await reader.readuntil(b'\r\n\r\n')
            proxy_requests.append(header)
            line = header.split(b'\r\n', 1)[0]
            method, authority, _version = line.split(b' ', 2)
            if method != b'CONNECT':
                raise AssertionError(f'unexpected proxy request: {line!r}')
            host_bytes, port_text = authority.rsplit(b':', 1)
            host = host_bytes.decode('ascii')
            ipaddress.ip_address(host)
            upstream_reader, upstream_writer = await asyncio.open_connection(host, int(port_text))
            writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            await writer.drain()
            await _relay_proxy_tunnel(reader, writer, upstream_reader, upstream_writer)
            upstream_writer = None
        except Exception as exc:
            proxy_errors.append(exc)
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
                with suppress(OSError):
                    await upstream_writer.wait_closed()
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    proxy_server = await asyncio.start_server(proxy_handler, '127.0.0.1', 0)
    proxy_port = proxy_server.sockets[0].getsockname()[1]
    _set_proxy_environment(monkeypatch, f'http://127.0.0.1:{proxy_port}')
    loop = asyncio.get_running_loop()
    original_getaddrinfo = loop.getaddrinfo

    async def resolve(host, port, **kwargs):
        if host == 'localhost':
            return [(2, 1, 6, '', ('127.0.0.1', port))]
        return await original_getaddrinfo(host, port, **kwargs)

    monkeypatch.setattr(loop, 'getaddrinfo', resolve)
    client_context = ssl.create_default_context(cafile=str(cert_path))
    try:
        with task_network_scope(SimpleNamespace(network_policy=NetworkPolicy())):
            async with httpx.AsyncClient(
                transport=PolicyTransport(verify=client_context), trust_env=False
            ) as client:
                response = await client.get(f'https://localhost:{tls_port}/secure')
                assert response.status_code == 200
                assert response.text == 'hello'
        assert proxy_errors == []
        assert len(proxy_requests) == 1
        assert proxy_requests[0].split(b'\r\n', 1)[0] == (
            f'CONNECT 127.0.0.1:{tls_port} HTTP/1.1'.encode('ascii')
        )
        assert f'Host: 127.0.0.1:{tls_port}\r\n'.encode('ascii') in proxy_requests[0]
        assert sni_names == ['localhost']
        assert len(inner_requests) == 1
        assert inner_requests[0].split(b'\r\n', 1)[0] == b'GET /secure HTTP/1.1'
        assert f'Host: localhost:{tls_port}\r\n'.encode('ascii') in inner_requests[0]
    finally:
        proxy_server.close()
        tls_server.close()
        await proxy_server.wait_closed()
        await tls_server.wait_closed()


@pytest.mark.asyncio
async def test_ipv6_literal_https_proxy_brackets_connect_and_verifies_ip_san(monkeypatch, tmp_path):
    cert_path = tmp_path / 'ipv6-cert.pem'
    key_path = tmp_path / 'ipv6-key.pem'
    _write_localhost_certificate(cert_path, key_path)

    sni_names = []
    inner_requests = []

    def capture_sni(_ssl_object, server_name, _context):
        sni_names.append(server_name)

    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    server_context.set_servername_callback(capture_sni)

    async def tls_handler(reader, writer):
        inner_requests.append(await reader.readuntil(b'\r\n\r\n'))
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello')
        await writer.drain()
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()

    try:
        tls_server = await asyncio.start_server(tls_handler, '::1', 0, ssl=server_context)
    except OSError as exc:
        pytest.skip(f'IPv6 loopback is unavailable: {exc}')
    tls_port = tls_server.sockets[0].getsockname()[1]

    proxy_requests = []
    proxy_errors = []

    async def proxy_handler(reader, writer):
        upstream_writer = None
        try:
            header = await reader.readuntil(b'\r\n\r\n')
            proxy_requests.append(header)
            line = header.split(b'\r\n', 1)[0]
            method, authority, _version = line.split(b' ', 2)
            if method != b'CONNECT':
                raise AssertionError(f'unexpected proxy request: {line!r}')
            host, port = _parse_connect_authority(authority)
            if not isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address):
                raise AssertionError(f'CONNECT target was not IPv6: {authority!r}')
            upstream_reader, upstream_writer = await asyncio.open_connection(host, port)
            writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            await writer.drain()
            await _relay_proxy_tunnel(reader, writer, upstream_reader, upstream_writer)
            upstream_writer = None
        except Exception as exc:
            proxy_errors.append(exc)
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
                with suppress(OSError):
                    await upstream_writer.wait_closed()
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    proxy_server = await asyncio.start_server(proxy_handler, '127.0.0.1', 0)
    proxy_port = proxy_server.sockets[0].getsockname()[1]
    _set_proxy_environment(monkeypatch, f'http://127.0.0.1:{proxy_port}')
    client_context = ssl.create_default_context(cafile=str(cert_path))
    try:
        with task_network_scope(SimpleNamespace(network_policy=NetworkPolicy())):
            async with httpx.AsyncClient(
                transport=PolicyTransport(verify=client_context), trust_env=False
            ) as client:
                response = await client.get(f'https://[::1]:{tls_port}/secure-ip')
                assert response.status_code == 200
                assert response.text == 'hello'
        assert proxy_errors == []
        assert len(proxy_requests) == 1
        assert proxy_requests[0].split(b'\r\n', 1)[0] == (
            f'CONNECT [::1]:{tls_port} HTTP/1.1'.encode('ascii')
        )
        assert f'Host: [::1]:{tls_port}\r\n'.encode('ascii') in proxy_requests[0]
        assert sni_names == [None]
        assert len(inner_requests) == 1
        assert inner_requests[0].split(b'\r\n', 1)[0] == b'GET /secure-ip HTTP/1.1'
        assert f'Host: [::1]:{tls_port}\r\n'.encode('ascii') in inner_requests[0]
    finally:
        proxy_server.close()
        tls_server.close()
        await proxy_server.wait_closed()
        await tls_server.wait_closed()


@pytest.mark.asyncio
async def test_ipv4_mapped_ipv6_is_normalized_and_blacklisted_ipv4_stays_blocked(monkeypatch):
    assert _parse_connect_authority(b'[::ffff:127.0.0.1]:443') == ('127.0.0.1', 443)
    assert _ip_authority('::ffff:127.0.0.1', 443) == b'127.0.0.1:443'
    with pytest.raises(NetworkDenied):
        _parse_connect_authority(b'::1:443')
    with pytest.raises(NetworkDenied):
        _parse_connect_authority(b'[not-an-ip]:443')
    with pytest.raises(NetworkDenied, match='scope-id'):
        _parse_connect_authority(b'[fe80::1%eth0]:443')

    loop = asyncio.get_running_loop()

    async def mapped_dns(host, port, **kwargs):
        if host in {'mapped.example', 'blocked.example'}:
            return [(10, 1, 6, '', ('::ffff:127.0.0.1', port, 0, 0))]
        return [(2, 1, 6, '', ('127.0.0.1', port))]

    monkeypatch.setattr(loop, 'getaddrinfo', mapped_dns)
    assert await _pinned_address('mapped.example', 443) == '127.0.0.1'

    policy = NetworkPolicy(True, ('127.0.0.1',))
    with task_network_scope(SimpleNamespace(network_policy=policy)):
        with pytest.raises(NetworkDenied, match='127.0.0.1'):
            await _pinned_address('blocked.example', 443)
