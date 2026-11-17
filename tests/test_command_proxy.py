"""Real local proxy traffic, host receipts, revocation and unchanged backend gates."""
import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from mewcode.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine
from mewcode.permissions.network_policy import NetworkDenied, NetworkPolicy, task_network_scope
from mewcode.tools.bash import Bash, Params
from mewcode.tools.command_proxy import CommandProxy, proxy_origins


@pytest.fixture
def checker(tmp_path):
    value = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(tmp_path)), RuleEngine(), PermissionMode.AUTO)
    value.task_id = 'proxy-fixture'
    value.network_policy = NetworkPolicy()
    return value


async def connect(proxy, target, *, authenticated=True):
    url = urlsplit(proxy.url)
    reader, writer = await asyncio.open_connection(url.hostname, url.port)
    auth = b'Proxy-Authorization: ' + proxy.auth + b'\r\n' if authenticated else b''
    writer.write(b'CONNECT ' + target.encode() + b' HTTP/1.1\r\n' + auth + b'\r\n')
    await writer.drain()
    status = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 2)
    return reader, writer, status


@pytest.mark.asyncio
async def test_host_records_actual_tunnel_and_byte_counts(checker):
    received = []
    async def endpoint(reader, writer):
        received.append(await reader.readexactly(4))
        writer.write(b'pong')
        await writer.drain()
        writer.close()
    server = await asyncio.start_server(endpoint, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with CommandProxy(checker, [f'https://127.0.0.1:{port}']) as proxy:
            reader, writer, status = await connect(proxy, f'127.0.0.1:{port}')
            assert status.startswith(b'HTTP/1.1 200')
            writer.write(b'ping')
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(4), 2) == b'pong'
            assert await reader.read() == b''
            writer.close()
        evidence = proxy.evidence()
        assert received == [b'ping']
        assert evidence['connections'][0]['sent_bytes'] == evidence['connections'][0]['received_bytes'] == 4
        assert evidence['connections'][0]['target'] == f'https://127.0.0.1:{port}'
        assert evidence['direct_socket_enforcement'] is False
        assert proxy.password not in json.dumps(evidence)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_unauthorized_or_other_origin_never_dials(checker, monkeypatch):
    async def forbidden(*_, **__):
        pytest.fail('denied target must not reach DNS or upstream connection')
    monkeypatch.setattr('mewcode.tools.network_transport.checked_addresses', forbidden)
    async with CommandProxy(checker, ['https://allowed.test']) as proxy:
        for target, auth, expected in [('allowed.test:443', False, b'407'), ('other.test:443', True, b'403')]:
            _, writer, status = await connect(proxy, target, authenticated=auth)
            assert expected in status.split(b'\r\n')[0]
            writer.close()
    assert proxy.evidence()['unauthenticated_connections'] == 1
    assert len(proxy.rows) == 1 and proxy.rows[0]['status'] == 'denied'


@pytest.mark.asyncio
async def test_network_revocation_closes_an_existing_tunnel(checker):
    async def endpoint(reader, writer):
        await reader.read()
        writer.close()
    server = await asyncio.start_server(endpoint, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with CommandProxy(checker, [f'https://127.0.0.1:{port}']) as proxy:
            reader, writer, status = await connect(proxy, f'127.0.0.1:{port}')
            assert b'200' in status
            checker.network_policy = NetworkPolicy(enabled=False)
            assert await asyncio.wait_for(reader.read(), 2) == b''
            assert proxy.revoked
            writer.close()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize('targets', [[], ['http://example.test'], ['https://u:p@example.test'],
    ['https://example.test/path'], ['https://example.test?x=1'], ['https://example.test:0'],
    ['https://*.example.test'], ['https://example.test\r\nOther: yes']])
def test_proxy_origins_reject_unbounded_or_ambiguous_targets(targets):
    with pytest.raises(ValueError):
        proxy_origins(targets)


def test_disabled_network_and_narrow_task_scope_still_apply(checker):
    checker.network_policy = NetworkPolicy(enabled=False)
    with pytest.raises(NetworkDenied):
        CommandProxy(checker, ['https://example.test'])
    checker.network_policy = NetworkPolicy()
    checker.task_authorization = SimpleNamespace(task_id=checker.task_id, network_hosts=['other.test'], expires_at=2**40)
    with pytest.raises(NetworkDenied, match='outside'):
        CommandProxy(checker, ['https://example.test'])


@pytest.mark.asyncio
async def test_bash_actual_process_uses_proxy_without_snapshot_or_secrets_in_receipt(checker, tmp_path):
    received = []
    async def endpoint(reader, writer):
        received.append(await reader.readexactly(4))
        writer.write(b'pong')
        await writer.drain()
        writer.close()
    server = await asyncio.start_server(endpoint, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    # An unrelated sparse file exceeds the native snapshot budget. Host proxy
    # execution must never copy/read it to run this already-authorized script.
    with (tmp_path / 'large-unrelated.bin').open('wb') as stream:
        stream.truncate(513 * 1024 * 1024)
    script = tmp_path / 'probe.py'
    script.write_text('''import base64, os, socket
from urllib.parse import urlsplit
p = urlsplit(os.environ['HTTPS_PROXY'])
auth = base64.b64encode((p.username + ':' + p.password).encode()).decode()
s = socket.create_connection((p.hostname, p.port), timeout=5)
s.sendall(('CONNECT 127.0.0.1:PORT HTTP/1.1\\r\\nProxy-Authorization: Basic ' + auth + '\\r\\n\\r\\n').encode())
f = s.makefile('rb')
assert f.readline().startswith(b'HTTP/1.1 200')
while f.readline() != b'\\r\\n': pass
s.sendall(b'ping')
assert f.read(4) == b'pong'
print('verified', os.environ['MEWCODE_COMMAND_PROXY_ID'])
# Deliberate accidental credential output must be redacted by the host.
print(p.password)
print(auth)
s.close()
blocked = socket.create_connection((p.hostname, p.port), timeout=5)
blocked.sendall(('CONNECT outside.test:443 HTTP/1.1\\r\\nProxy-Authorization: Basic ' + auth + '\\r\\n\\r\\n').encode())
assert b'403' in blocked.recv(4096)
blocked.close()
'''.replace('PORT', str(port)), encoding='utf-8')
    try:
        with task_network_scope(checker):
            result = await Bash().run(Params(command='python probe.py', timeout=15,
                network='proxy', proxy_targets=[f'https://127.0.0.1:{port}']), work_dir=str(tmp_path))
        assert not result.is_error, result.output
        assert received == [b'ping']
        observation = result.host_observation['command_execution']
        assert observation['command_started'] is True and observation['exit_code'] == 0
        assert observation['transport']['connections'][0]['sent_bytes'] == 4
        assert observation['transport']['denied_connections'] == 1
        assert observation['transport']['connections'][1]['sent_bytes'] == 0
        assert '[redacted]' in result.output
        assert result.output.count('[redacted]') == 2
        assert not (tmp_path / '.mewcode').exists()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_required_native_backend_never_falls_back_to_host_proxy(checker, tmp_path, monkeypatch):
    from mewcode.tools.command_sandbox import native_auto_command
    async def forbidden(*_, **__):
        pytest.fail('must not start a host process')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)
    with native_auto_command(checker), task_network_scope(checker):
        result = await Bash().run(Params(command='python probe.py', network='proxy',
            proxy_targets=['https://example.test']), work_dir=str(tmp_path))
    assert result.is_error
    assert result.host_observation['command_execution']['command_started'] is False


def test_node_command_grant_cannot_extend_proxy_destinations(tmp_path):
    from tests.test_kernel_capabilities import flow
    from mewcode.kernel.capabilities import call_gap, compile_manifest
    from mewcode.kernel.authorization_binding import call_key
    manifest = compile_manifest(flow(), workspace=str(tmp_path), approved=True)
    args = {'command': 'python check.py', 'network': 'proxy', 'proxy_targets': ['https://example.test']}
    assert call_gap(manifest, 'N03', 'Bash', args) is None
    bad = {**args, 'proxy_targets': ['https://outside.test']}
    manifest['call_bindings'] = {call_key('N03', 'Bash', bad, str(tmp_path)): {}}
    assert call_gap(manifest, 'N03', 'Bash', bad)
    manifest['nodes']['N03']['external_targets'] = ['https://example.test/parse']
    assert call_gap(manifest, 'N03', 'Bash', args) is None
    assert call_gap(manifest, 'N03', 'Bash', {**args, 'proxy_targets': ['https://example.test:8443']})


@pytest.mark.asyncio
async def test_real_https_post_from_child_preserves_tls_and_host_receipt(checker, tmp_path):
    import datetime
    import ipaddress
    import ssl
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Local proxy test')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1))
        .not_valid_after(now+datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), False)
        .sign(key, hashes.SHA256()))
    (tmp_path/'cert.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path/'key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tmp_path/'cert.pem', tmp_path/'key.pem')
    received = []
    async def endpoint(reader, writer):
        header = await reader.readuntil(b'\r\n\r\n')
        size = int(next(line.split(b':', 1)[1] for line in header.split(b'\r\n')
                        if line.lower().startswith(b'content-length:')))
        received.append((header, await reader.readexactly(size)))
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok')
        await writer.drain()
        writer.close()
    server = await asyncio.start_server(endpoint, '127.0.0.1', 0, ssl=context)
    port = server.sockets[0].getsockname()[1]
    (tmp_path/'tls.py').write_text('''import http.client, ssl, os, base64
from urllib.parse import urlsplit
p=urlsplit(os.environ['HTTPS_PROXY'])
headers={'Proxy-Authorization':'Basic '+base64.b64encode((p.username+':'+p.password).encode()).decode()}
def call(context):
 c=http.client.HTTPSConnection(p.hostname,p.port,context=context,timeout=5)
 c.set_tunnel('127.0.0.1',PORT,headers=headers)
 try:
  c.request('POST','/ocr',body=b'synthetic-page',headers={'Authorization':'Bearer test-only'})
  r=c.getresponse()
  assert r.status==200 and r.read()==b'ok'
 finally:c.close()
try:call(ssl.create_default_context())
except ssl.SSLCertVerificationError:pass
else:raise AssertionError('Untrusted TLS certificate was accepted')
call(ssl.create_default_context(cafile='cert.pem'))
print('TLS verified, one POST, no direct fallback')
'''.replace('PORT', str(port)), encoding='utf-8')
    try:
        with task_network_scope(checker):
            result = await Bash().run(Params(command='python tls.py', timeout=20,
                network='proxy', proxy_targets=[f'https://127.0.0.1:{port}']), work_dir=str(tmp_path))
        assert not result.is_error, result.output
        assert len(received) == 1 and received[0][1] == b'synthetic-page'
        assert b'proxy-authorization' not in received[0][0].lower()
        transport = result.host_observation['command_execution']['transport']
        assert len(transport['connections']) == 2
        assert all(row['sent_bytes'] > 0 and row['received_bytes'] > 0 for row in transport['connections'])
        assert 'HOST PROXY RECEIPT: ' in result.output
        assert 'test-only' not in result.output
    finally:
        server.close()
        await server.wait_closed()
