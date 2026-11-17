"""Real local acceptance, using synthetic files and local HTTP/TLS servers."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import threading
import time
import uuid
import venv

from mewcode.permissions.network_policy import NetworkPolicy
from mewcode.permissions.policy import build_permission_checker
from mewcode.runtime_paths import user_data_root
from mewcode.sandbox import lpac_runtime as lpac
from mewcode.sandbox.lpac_runtime import NativeScope, find_runtime_helper
from mewcode.sandbox.srt_runtime import REQUIRED_CASES, invoke_srt, runtime_identity
from mewcode.sandbox.staged_execution import PROTOCOL, execute_staged
from mewcode.sandbox.transaction_registry import TransactionRegistry

DIRECT_SOCKET_PROBES = ('ipv4_tcp', 'ipv6_tcp', 'ipv4_udp', 'ipv6_udp')


def _network_disabled_pass(receipt, policy):
    return (receipt.is_error and 'WinError 10013' in receipt.stderr
        and receipt.cleanup_confirmed is True and receipt.network_policy == policy.as_dict())


def _same_sid_clean_rounds(expected_sid, observed_sids, receipts):
    return (len(observed_sids) == len(receipts) and bool(receipts)
        and all(sid == expected_sid for sid in observed_sids)
        and all(receipt.cleanup_confirmed is True for receipt in receipts))


def _direct_socket_denials_pass(receipts, *, ipv6_available, udp_observations=()):
    # UDP sendto may report success when WFP drops the packet. The receiver,
    # not the sender's return value, proves whether a datagram crossed the fence.
    tcp, udp = receipts[:2], receipts[2:]
    return (ipv6_available and len(receipts) == len(DIRECT_SOCKET_PROBES)
        and all(receipt.is_error and 'WinError 10013' in receipt.stderr
            and receipt.execution_started is True and receipt.cleanup_confirmed is True for receipt in tcp)
        and len(udp_observations) == 2
        and all(not receipt.is_error and receipt.execution_started is True
            and receipt.cleanup_confirmed is True and receipt.confirms_stdout_line(observation['marker'])
            and observation['before_control'] is True and observation['after_control'] is True
            and observation['receiver_error'] is None and observation['received_probe_count'] == 0
            for receipt, observation in zip(udp, udp_observations)))


def _file_boundary_denied_pass(receipt, *, forbidden_output=''):
    denied = ('PermissionError', 'UnauthorizedAccessException', 'PermissionDenied',
              '拒绝访问', '访问被拒绝', 'Access is denied', 'Permission denied')
    return (receipt.status == 'exited' and receipt.is_error
        and receipt.execution_started is True and receipt.cleanup_confirmed is True
        and any(marker.casefold() in receipt.stderr.casefold() for marker in denied)
        and (not forbidden_output or forbidden_output not in receipt.stdout))


class _UDPReceiver:
    """Synthetic host receiver with positive controls before and after a probe."""
    def __init__(self, family):
        self.socket = socket.socket(family, socket.SOCK_DGRAM)
        self.socket.bind(('127.0.0.1' if family == socket.AF_INET else '::1', 0))
        self.socket.settimeout(.1)
        self.address = self.socket.getsockname()
        self.received = []
        self.error = None
        self.stopped = threading.Event()
        self.changed = threading.Condition()
        self.thread = threading.Thread(target=self._receive, daemon=True)
        self.thread.start()

    def _receive(self):
        while not self.stopped.is_set():
            try:
                data, _ = self.socket.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError as exc:
                if not self.stopped.is_set():
                    self.error = type(exc).__name__
                return
            with self.changed:
                self.received.append(data)
                self.changed.notify_all()

    def control(self):
        nonce = ('CONTROL-' + uuid.uuid4().hex).encode('ascii')
        with socket.socket(self.socket.family, socket.SOCK_DGRAM) as sender:
            sender.sendto(nonce, self.address)
        deadline = time.monotonic() + 2
        with self.changed:
            while nonce not in self.received and self.error is None and time.monotonic() < deadline:
                self.changed.wait(max(0, deadline - time.monotonic()))
            return nonce in self.received and self.error is None

    def close(self):
        self.stopped.set()
        self.thread.join(2)
        self.socket.close()


async def _execute_acceptance_staged(script, *, checker, helper, scope, registry,
                                     timeout, run, hosts, identity, observed_receipts):
    """Route synthetic acceptance commands through the matching LPAC boundary."""
    policy = checker.network_policy

    async def runner(envelope, cancel, observed):
        if runtime_identity() != identity:
            raise RuntimeError('NETWORK_RUNTIME_CHANGED: 运行器在验收期间变化，必须重新验收。')
        if policy.enabled:
            frozen = {key: envelope['request'].get(key) for key in ('read_paths', 'write_paths')}
            frozen.update(backend='lpac', readonly_paths=envelope.get('readonly_paths'))
            (run / 'file-boundary.json').write_text(json.dumps(frozen, ensure_ascii=False, indent=2), encoding='utf-8')
        (run / 'synthetic-envelope.json').write_text(json.dumps(envelope, ensure_ascii=False), encoding='utf-8')

        def receipt(value):
            # The offline helper does not know about the host network switch;
            # attach that host-owned observation without adding Internet access.
            value = replace(value, network_policy=policy.as_dict())
            observed_receipts.append(value)
            observed(value)

        if policy.enabled:
            return await invoke_srt(envelope, checker=checker, cancel=cancel,
                acceptance=True, test_hosts=hosts, on_receipt=receipt)
        value = await lpac.invoke_helper(helper, envelope, cancel, on_receipt=receipt)
        return replace(value, network_policy=policy.as_dict())

    extra_read = ()
    request_transform = None
    runtime_git_dir = None
    if policy.enabled:
        from mewcode.sandbox import lpac_network
        from mewcode.sandbox.network_git import runtime_git_directory
        request_transform = lpac_network.transform
        runtime_git_dir = runtime_git_directory()
    return await execute_staged(script, scope=scope, helper=helper,
        timeout=timeout, registry=registry, runner=runner, extra_read=extra_read,
        request_transform=request_transform, runtime_git_dir=runtime_git_dir)


def _certificate(directory):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'synthetic-local-server')])
    cert = x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
    cert = cert.serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=5))
    cert = cert.not_valid_after(datetime.now(timezone.utc) + timedelta(hours=1))
    cert = cert.add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), False)
    pem = directory / 'localhost.pem'; private = directory / 'localhost.key'
    pem.write_bytes(cert.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM))
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
    return pem, private


def _save_check(path, record):
    """Atomically replace a host record, never exposing a partly written JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lpac.checked_path(path.parent)
    if path.exists():
        lpac.checked_path(path, file=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.pending')
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(record, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def verify_runtime(root: Path, *, publish=False) -> dict:
    from mewcode.sandbox.srt_runtime import _RunnerLock, _network_shared_root
    lock = _RunnerLock(_network_shared_root() / 'acceptance')
    try:
        if not lock.acquire():
            raise RuntimeError('NETWORK_RUNTIME_UNAVAILABLE: 本机验收正在进行，请等待当前检查完成。')
        record = {'schema': 'mewcode-srt-acceptance-v1', 'identity': runtime_identity(),
                  'check_id': uuid.uuid4().hex, 'cases': dict.fromkeys(REQUIRED_CASES, None), 'failure': None}
        last_path = user_data_root() / 'srt-network-last-check.json'
        if publish:
            # Immediately retire an older success. A crash/exception during
            # this check must leave an incomplete check, not an active old pass.
            _save_check(last_path, record)
        try:
            return _verify_runtime(root, publish=publish, record=record)
        except (OSError, ValueError, RuntimeError) as exc:
            record['failure'] = {'check': None, 'error_code': type(exc).__name__,
                                 'reason': str(exc)[:600]}
            if publish:
                _save_check(last_path, record)
            return {'ready': False, 'cases': record['cases'], 'report': '', 'failure': record['failure']}
    finally:
        lock.close()


def _verify_runtime(root: Path, *, publish, record) -> dict:
    identity = record['identity']
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    run = root / uuid.uuid4().hex[:10]
    workspace, outside, profile, private = (run / name for name in ('w', 'o', 'p', 't'))
    for path in (workspace, outside, profile, private):
        path.mkdir(parents=True)
    (outside / 'protected.txt').write_text('SYNTHETIC_OUTSIDE', encoding='utf-8')
    (profile / 'protected.txt').write_text('SYNTHETIC_PROFILE', encoding='utf-8')
    venv.EnvBuilder(with_pip=False).create(workspace / '.venv')
    requests = []
    started = threading.Event()
    cancel_pids = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            requests.append(('GET', self.path))
            if self.path == '/redirect':
                self.send_response(302); self.send_header('Location', 'http://blocked.example/target')
                self.send_header('Content-Length', '0'); self.end_headers(); return
            git = 'info/refs' in self.path
            body = b'001e# service=git-upload-pack\n00000000' if git else b'SYNTHETIC_HTTP_OK'
            self.send_response(200)
            self.send_header('Content-Type', 'application/x-git-upload-pack-advertisement' if git else 'text/plain')
            self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            requests.append(('POST', self.path, body == b'SYNTHETIC_OCR_PAGE'))
            if self.path == '/cancel-start':
                cancel_pids[:] = [int(value) for value in body.decode('ascii').split()]
                started.set()
            data = b'{"text":"SYNTHETIC_OCR_OK"}'
            self.send_response(200); self.send_header('Content-Length', str(len(data)))
            self.end_headers(); self.wfile.write(data)
    class IPv6ThreadingHTTPServer(ThreadingHTTPServer):
        address_family = socket.AF_INET6

    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    https = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    try:
        http6 = IPv6ThreadingHTTPServer(('::1', 0), Handler)
    except OSError:
        http6 = None
    cert, key = _certificate(private)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(cert, key)
    https.socket = context.wrap_socket(https.socket, server_side=True)
    servers = [http, https] + ([http6] if http6 is not None else [])
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    checker = build_permission_checker(workspace)
    checker.task_id = 'srt-acceptance-' + uuid.uuid4().hex
    checker.network_policy = NetworkPolicy()
    helper = find_runtime_helper(require_protocol=PROTOCOL)
    registry = TransactionRegistry(private / 'transactions')
    hosts = [f'127.0.0.1:{http.server_port}', f'127.0.0.1:{https.server_port}']
    from mewcode.sandbox import lpac_network
    network_profile_sid = lpac_network.profile()['profile_sid']
    cases = record['cases']
    details = {}
    failures = {}

    def record_case(name, passed, receipt=None, reason=''):
        cases[name] = bool(passed)
        if not passed:
            failures[name] = {'check': name,
                'error_code': receipt.error_code if receipt is not None else 'acceptance_check_failed',
                'reason': (receipt.detail or receipt.stderr or reason) if receipt is not None else reason}

    # Deliberately permissive ACL: additive allowWrite is not confinement.
    # Any escape must fail the real activation gate, never be explained away.
    allowed = subprocess.run(['icacls.exe', str(outside), '/grant', '*S-1-5-11:(OI)(CI)M'],
        capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    if allowed.returncode != 0:
        raise RuntimeError('Cannot configure the synthetic permissive-ACL challenge')

    async def command(script, *, policy=NetworkPolicy(), discard=True, timeout=15, cancelled=False):
        checker.network_policy = policy
        checker.native_publication_mode = 'discard' if discard else 'publish'
        current_scope = NativeScope.from_checker(checker)
        observed_receipts = []
        task = asyncio.create_task(_execute_acceptance_staged(script, checker=checker,
            helper=helper, scope=current_scope, registry=registry, timeout=timeout, run=run,
            hosts=hosts, identity=identity, observed_receipts=observed_receipts))
        if cancelled:
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            for _ in range(600):
                if started.is_set() or task.done():
                    break
                await asyncio.sleep(.1)
            handles = [kernel.OpenProcess(0x100000, False, pid) for pid in cancel_pids]
            genuine_start = started.is_set() and len(handles) == 2 and all(handles)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            stopped = genuine_start and all(kernel.WaitForSingleObject(handle, 5000) == 0 for handle in handles)
            for handle in handles:
                if handle:
                    kernel.CloseHandle(handle)
            complete = (genuine_start and stopped and len(observed_receipts) == 1
                        and observed_receipts[0].cleanup_confirmed is True)
            details['cancel'] = {'started': genuine_start, 'descendants_exited': stopped,
                'receipt_count': len(observed_receipts),
                'cleanup': observed_receipts[0].cleanup_confirmed if observed_receipts else None,
                'receipt_detail': observed_receipts[0].detail if observed_receipts else None}
            return complete
        receipt = await task
        details[str(len(details) + 1)] = {'status': receipt.status, 'exit_code': receipt.exit_code,
            'cleanup': receipt.cleanup_confirmed, 'error_code': receipt.error_code, 'detail': receipt.detail,
            'stdout': receipt.stdout, 'stderr': receipt.stderr, 'network': receipt.network_policy}
        return receipt

    async def run_cases():
        url = f'http://127.0.0.1:{http.server_port}'
        py = "& '.\\.venv\\Scripts\\python.exe' -c "
        get = py + f'"import urllib.request; print(urllib.request.urlopen(\'{url}/read\').read().decode())"'
        first_round = await command(get)
        first_round_sid = lpac_network.profile()['profile_sid']
        if first_round.is_error or first_round.cleanup_confirmed is not True:
            record_case('http_allowed', False, first_round, '首轮 HTTP 调用未通过')
            return
        second_round = await command(get)
        second_round_sid = lpac_network.profile()['profile_sid']
        record_case('http_allowed', (not first_round.is_error and not second_round.is_error
            and 'SYNTHETIC_HTTP_OK' in first_round.stdout and 'SYNTHETIC_HTTP_OK' in second_round.stdout
            and first_round.cleanup_confirmed is True and second_round.cleanup_confirmed is True
            and first_round_sid == network_profile_sid == second_round_sid),
            first_round if first_round.is_error else second_round, 'HTTP 响应或同一 SID 清理核验未通过')
        details['same_sid_second_round'] = _same_sid_clean_rounds(network_profile_sid,
            (first_round_sid, second_round_sid), (first_round, second_round))
        if not cases['http_allowed']:
            return
        post = py + f'"import urllib.request; print(urllib.request.urlopen(\'{url}/ocr\', data=b\'SYNTHETIC_OCR_PAGE\').read().decode())"'
        r = await command(post)
        record_case('ocr_upload_allowed', not r.is_error and 'SYNTHETIC_OCR_OK' in r.stdout and ('POST', '/ocr', True) in requests,
            r, '模拟 OCR 上传响应未通过')
        r = await command(f'git -c http.sslVerify=false ls-remote https://127.0.0.1:{https.server_port}/repo; exit $LASTEXITCODE')
        record_case('git_https_allowed', not r.is_error and any('info/refs' in row[1] for row in requests),
            r, '本机 Git HTTPS 服务没有收到查询')
        for name, target, policy in (
            ('blacklist_domain', 'http://blocked.example/page', NetworkPolicy(True, ('blocked.example',))),
            ('blacklist_redirect', url + '/redirect', NetworkPolicy(True, ('blocked.example',))),
            ('blacklist_ip', url + '/blocked-ip', NetworkPolicy(True, ('127.0.0.1',))),
        ):
            script = py + f'"import urllib.request; print(urllib.request.urlopen(\'{target}\').read())"'
            r = await command(script, policy=policy)
            marker = 'NETWORK_DISABLED' if name == 'network_disabled' else 'NETWORK_BLACKLISTED'
            record_case(name, r.is_error and marker in (r.stdout + r.stderr), r, '没有取得黑名单拦截证据')
        network_disabled_script = py + f'"import socket; socket.socket().connect((\'127.0.0.1\',{http.server_port}))"'
        offline_receipt = await command(network_disabled_script, policy=NetworkPolicy(False))
        offline_sid = lpac_network.profile()['profile_sid']
        record_case('network_disabled', (_network_disabled_pass(offline_receipt, NetworkPolicy(False))
            and offline_sid == network_profile_sid), offline_receipt, '关闭联网后的 socket 或 SID 清理核验未通过')
        details['offline_policy_preserved'] = _network_disabled_pass(offline_receipt, NetworkPolicy(False))
        details['same_sid_after_offline_round'] = _same_sid_clean_rounds(network_profile_sid,
            (offline_sid,), (offline_receipt,))

        probe_file = workspace / 'socket-descendant-probe.py'
        probe_file.write_text(
            "import socket, subprocess, sys\n"
            "target = sys.argv[1]\n"
            "if target == 'ipv4_tcp': code = \"import socket; socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(('127.0.0.1', " + str(http.server_port) + "))\"\n"
            "elif target == 'ipv6_tcp': code = \"import socket; socket.socket(socket.AF_INET6, socket.SOCK_STREAM).connect(('::1', " + str(http6.server_port if http6 is not None else 1) + "))\"\n"
            "else:\n"
            " family = 'AF_INET' if target == 'ipv4_udp' else 'AF_INET6'\n"
            " host = '127.0.0.1' if target == 'ipv4_udp' else '::1'\n"
            " port, nonce = int(sys.argv[2]), sys.argv[3]\n"
            " code = 'import socket, time\\ns = socket.socket(socket.' + family + ', socket.SOCK_DGRAM)\\n'\n"
            " code += 'for _ in range(3):\\n try: s.sendto(' + repr(nonce.encode('ascii')) + ', ' + repr((host, port)) + ')\\n except OSError as e:\\n  if getattr(e, \\\"winerror\\\", None) != 10013: raise\\n time.sleep(.05)\\n'\n"
            " code += 'print(' + repr('UDP_PROBE_ATTEMPTED:' + nonce + ':3') + ')'\n"
            "subprocess.run([sys.executable, '-c', code], check=True)\n",
            encoding='utf-8')
        direct_receipts = []
        for probe in DIRECT_SOCKET_PROBES[:2]:
            result = await command(f"& '.\\.venv\\Scripts\\python.exe' '.\\socket-descendant-probe.py' {probe}")
            direct_receipts.append(result)
        udp_observations = []
        for probe, family in zip(DIRECT_SOCKET_PROBES[2:], (socket.AF_INET, socket.AF_INET6)):
            receiver = None
            try:
                receiver = _UDPReceiver(family)
                nonce = 'PROBE-' + uuid.uuid4().hex
                before = await asyncio.to_thread(receiver.control)
                result = await command(f"& '.\\.venv\\Scripts\\python.exe' '.\\socket-descendant-probe.py' {probe} {receiver.address[1]} {nonce}")
                direct_receipts.append(result)
                await asyncio.sleep(.5)
                after = await asyncio.to_thread(receiver.control)
                udp_observations.append({'marker': 'UDP_PROBE_ATTEMPTED:' + nonce + ':3',
                    'before_control': before, 'after_control': after, 'receiver_error': receiver.error,
                    'received_probe_count': receiver.received.count(nonce.encode('ascii'))})
            except OSError as exc:
                details[probe] = {'receiver_error': type(exc).__name__}
            finally:
                if receiver is not None:
                    receiver.close()
        details['udp_delivery'] = udp_observations
        record_case('direct_socket_denied', _direct_socket_denials_pass(direct_receipts,
            ipv6_available=http6 is not None, udp_observations=udp_observations),
            reason='TCP 拒绝或双栈 UDP 接收端/前后正向控制/零送达核验未通过')
        literal = str(outside / 'escaped.txt').replace("'", "''")
        r = await command(f"[IO.File]::WriteAllText('{literal}', 'synthetic')")
        record_case('workspace_escape_denied', _file_boundary_denied_pass(r) and not (outside / 'escaped.txt').exists(), r,
            '工作区边界未取得明确拦截证据')
        literal = str(profile / 'protected.txt').replace("'", "''")
        r = await command(f"Get-Content -LiteralPath '{literal}'")
        record_case('sensitive_read_denied', _file_boundary_denied_pass(r, forbidden_output='SYNTHETIC_PROFILE'), r,
            '敏感文件边界未取得明确拦截证据')
        cancel_file = workspace / 'cancel-descendant-probe.py'
        cancel_file.write_text(
            "import os, subprocess, sys, time, urllib.request\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            f"urllib.request.urlopen('{url}/cancel-start', data=f'{{os.getpid()}} {{child.pid}}'.encode('ascii')).read()\n"
            "time.sleep(120)\n", encoding='utf-8')
        cancellation = "& '.\\.venv\\Scripts\\python.exe' '.\\cancel-descendant-probe.py'"
        record_case('cancel_cleanup', await command(cancellation, timeout=120, cancelled=True),
            reason='没有取得已启动子代退出及清理完成的联合证据')
        r = await command("[IO.File]::WriteAllText((Join-Path (Get-Location).ProviderPath 'published.txt'), 'SYNTHETIC_PUBLICATION')", discard=False)
        record_case('publication', not r.is_error and (workspace / 'published.txt').read_text() == 'SYNTHETIC_PUBLICATION',
            r, '成果发布或文件内容核对未通过')

    try:
        asyncio.run(run_cases())
    finally:
        for server in servers:
            server.shutdown(); server.server_close()
        subprocess.run(['icacls.exe', str(outside), '/remove:g', '*S-1-5-11'],
            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    unchanged = runtime_identity() == identity
    report = {'schema': 'mewcode-srt-acceptance-v1', 'identity': identity, 'check_id': record['check_id'], 'cases': cases,
              'details': details, 'synthetic_only': True, 'runtime_unchanged': unchanged,
              'failure': next((failures[name] for name in REQUIRED_CASES if name in failures), None),
              'ready': all(value is True for value in cases.values()) and unchanged}
    (run / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    saved = {k: report[k] for k in ('schema', 'identity', 'check_id', 'cases', 'failure')}
    if publish:
        _save_check(user_data_root() / 'srt-network-last-check.json', saved)
    if publish and report['ready']:
        _save_check(user_data_root() / 'srt-network-acceptance.json', saved)
    return {'ready': report['ready'], 'cases': cases, 'report': str(run / 'report.json')}
