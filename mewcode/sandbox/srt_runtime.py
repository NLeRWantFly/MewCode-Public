"""Pinned SRT adapter. No installer or host-shell fallback during a task."""
from __future__ import annotations

import asyncio
import base64
import copy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from mewcode.permissions.network_policy import policy_for
from mewcode.runtime_paths import user_data_root
from mewcode.sandbox.receipt import NativeExecutionReceipt
from mewcode.sandbox.network_acceptance_status import describe_acceptance

BACKEND = 'mewcode-windows-srt'
WORKER = Path(__file__).with_name('srt_lpac_worker.mjs')
REQUIRED_CASES = ('http_allowed', 'ocr_upload_allowed', 'git_https_allowed', 'blacklist_domain',
    'blacklist_redirect', 'blacklist_ip', 'direct_socket_denied', 'network_disabled',
    'workspace_escape_denied', 'sensitive_read_denied', 'cancel_cleanup', 'publication')


def runtime_paths():
    node = Path(shutil.which('node.exe') or '')
    package = Path(os.environ.get('APPDATA', '')) / 'npm/node_modules/@anthropic-ai/sandbox-runtime'
    if not node.is_absolute() or not package.is_absolute():
        raise RuntimeError('NETWORK_RUNTIME_UNAVAILABLE: 未找到 Node 或 SRT 运行器。')
    data = json.loads((package / 'package.json').read_text(encoding='utf-8'))
    if data.get('version') != '0.0.77':
        raise RuntimeError('NETWORK_RUNTIME_UNAVAILABLE: 需要固定版本 SRT 0.0.77。')
    arch = 'arm64' if os.environ.get('PROCESSOR_ARCHITECTURE', '').upper() == 'ARM64' else 'x64'
    helper = package / f'vendor/srt-win/{arch}/srt-win.exe'
    if not node.is_file() or not helper.is_file() or not WORKER.is_file():
        raise RuntimeError('NETWORK_RUNTIME_UNAVAILABLE: 联网运行器文件不完整。')
    return node, package, helper


def runtime_identity() -> str:
    node, package, helper = runtime_paths()
    digest = hashlib.sha256()
    host_files = [Path(__file__), Path(__file__).with_name('srt_acceptance.py'),
        Path(__file__).with_name('srt_parent_proxy.mjs'),
        Path(__file__).with_name('srt_setup.py'), Path(__file__).with_name('windows_job.py'),
        Path(__file__).with_name('staged_execution.py'), Path(__file__).with_name('staged_workspace.py'),
        Path(__file__).with_name('lpac_network.py'), Path(__file__).with_name('network_fence.py'),
        Path(__file__).with_name('network_acceptance_status.py'), Path(__file__).with_name('lpac_runtime.py'),
        Path(__file__).with_name('cleanup_evidence.py'),
        Path(__file__).with_name('network_cleanup.py'),
        Path(__file__).with_name('network_git.py'),
        Path(__file__).resolve().parents[2] / 'native/windows/MewCode.NetworkCleanup.cs',
        Path(__file__).with_name('staged_publication.py'), Path(__file__).with_name('publication_io.py'),
        Path(__file__).with_name('metadata_broker.py'),
        Path(__file__).parents[1] / 'permissions/network_policy.py']
    from mewcode.sandbox.lpac_runtime import find_runtime_helper
    offline_helper = find_runtime_helper(require_protocol='mewcode-lpac-staged-request-v1')
    offline_manifest = offline_helper.with_name('manifest.json')
    host_files.extend((offline_helper, offline_manifest))
    for relative in sorted(json.loads(offline_manifest.read_text(encoding='utf-8')).get('files', {})):
        host_files.append(offline_helper.parent / relative)
    from mewcode.sandbox.network_git import paths as network_git_paths
    host_files.extend(network_git_paths())
    from mewcode.sandbox.lpac_network import paths as network_paths
    host_files.extend(network_paths())
    from mewcode.sandbox.network_fence import load_candidate
    fence = load_candidate(user_data_root() / 'native/windows-network-fence-v1')
    host_files.extend((fence.executable, fence.directory / 'manifest.json',
                       user_data_root() / 'network-fence-install.json'))
    for path in (node, package / 'package.json', helper, WORKER, *host_files,
                 *sorted((package / 'dist').rglob('*.js'))):
        digest.update(str(path.resolve()).encode())
        with path.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def cleanup_blocked() -> bool:
    from mewcode.sandbox.transaction_registry import TransactionRegistry
    if (_network_shared_root() / 'cleanup-unconfirmed.json').exists():
        return True
    return any((item.get('native_receipt') or {}).get('backend') == BACKEND
        and (item.get('native_receipt') or {}).get('cleanup_confirmed') is not True
        for item in TransactionRegistry(user_data_root() / 'native').pending())


def runtime_status() -> dict:
    try:
        from mewcode.sandbox.native_gate import native_gate_status
        gate = native_gate_status()
        if not gate.ready:
            return {'ready': False, 'state': 'native_gate_' + gate.state.value, 'version': '0.0.77',
                'reason': 'NETWORK_RUNTIME_UNAVAILABLE: 基础执行器尚未就绪：' + gate.reason,
                'native_gate_state': gate.state.value}
        if cleanup_blocked():
            return {'ready': False, 'state': 'cleanup_unconfirmed', 'version': '0.0.77',
                'reason': 'NETWORK_RUNTIME_UNAVAILABLE: 上次联网执行的清理未确认，请先在执行恢复中处理该事务。'}
        identity = runtime_identity()
        try:
            proof = json.loads((user_data_root() / 'srt-network-acceptance.json').read_text(encoding='utf-8'))
        except FileNotFoundError:
            proof = {}
        try:
            last = json.loads((user_data_root() / 'srt-network-last-check.json').read_text(encoding='utf-8'))
        except FileNotFoundError:
            last = {}
        last_status = describe_acceptance(last, identity=identity, required_cases=REQUIRED_CASES)
        proof_status = describe_acceptance(proof, identity=identity, required_cases=REQUIRED_CASES)
        ready = (proof_status['ready'] and last_status['ready']
                 and bool(proof.get('check_id')) and proof.get('check_id') == last.get('check_id')
                 )
        if not ready and last_status['schema_valid']:
            return {**last_status, 'ready': False, 'version': '0.0.77',
                'state': 'acceptance_failed' if last_status['failed_checks'] else 'pending',
                'reason': last_status['reason'] or
                    'NETWORK_RUNTIME_UNAVAILABLE: 本机验收尚未发布或准入记录不匹配，请重新验收。'}
        return {'ready': ready, 'state': 'ready' if ready else 'pending', 'version': '0.0.77',
            'reason': '' if ready else 'NETWORK_RUNTIME_UNAVAILABLE: 联网运行器尚未完成本机验收。'}
    except FileNotFoundError:
        return {'ready': False, 'state': 'not_configured', 'version': '0.0.77',
            'reason': 'NETWORK_RUNTIME_UNAVAILABLE: 请在沙箱设置中完成一次性联网运行器配置。'}
    except (OSError, ValueError, RuntimeError) as exc:
        return {'ready': False, 'state': 'unavailable', 'version': '0.0.77', 'reason': str(exc)}


def configuration_status() -> dict:
    """Read live provisioning; this never installs, grants ACLs or uploads data."""
    node, package, _ = runtime_paths()
    request = {'operation': 'status', 'request_id': 'status'}
    result = subprocess.run([str(node), str(WORKER), str(package)],
        input=json.dumps(request).encode() + b'\n', capture_output=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode != 0:
        raise RuntimeError('NETWORK_RUNTIME_UNAVAILABLE: 无法读取 SRT 配置状态。')
    status = json.loads(result.stdout)
    if 'provisioned' not in status:
        raise RuntimeError(status.get('detail') or 'NETWORK_RUNTIME_UNAVAILABLE: 无法读取 SRT 状态。')
    return status


class _RunnerLock:
    """Serialize SRT ACL sessions across MewCode host processes."""
    def __init__(self, root=None):
        import msvcrt
        self.api = msvcrt
        path = (Path(root) if root is not None else _network_shared_root()) / 'runner.lock'
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open('a+b')
        if self.file.tell() == 0:
            self.file.write(b'0'); self.file.flush()

    def acquire(self):
        self.file.seek(0)
        try:
            self.api.locking(self.file.fileno(), self.api.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def close(self):
        self.file.close()


def _network_shared_root():
    # Identity follows the Windows user, just like the AppContainer SID.
    # APPDATA and MEWCODE_USER_DATA_DIR overrides cannot split the fence.
    import ctypes
    from ctypes import wintypes
    output = ctypes.create_unicode_buffer(32768)
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    shell.SHGetFolderPathW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.HANDLE,
                                     wintypes.DWORD, wintypes.LPWSTR]
    shell.SHGetFolderPathW.restype = ctypes.c_long
    if shell.SHGetFolderPathW(None, 28, None, 0, output) != 0:
        raise RuntimeError('NETWORK_RUNTIME_UNAVAILABLE: 无法取得用户级隔离锁目录。')
    return Path(output.value) / 'MewCode/network-runtime'


async def invoke_srt(envelope, *, checker, cancel=None, on_receipt=None, acceptance=False, test_hosts=()):
    try:
        node, package, _ = runtime_paths()
        policy = policy_for(checker)
        if not acceptance:
            status = runtime_status()
            if not status['ready']:
                raise RuntimeError(status['reason'])
        from mewcode.sandbox import lpac_network
        from mewcode.sandbox.lpac_runtime import encode_request
        helper, probe = lpac_network.paths()
        identity = lpac_network.profile()
        if envelope['request'].get('schema') != lpac_network.NET_SCHEMA or envelope['request'].get('runtime_network') is not True:
            raise RuntimeError('NETWORK_RUNTIME_UNAVAILABLE: 缺少冻结的联网 LPAC 请求。')
        probe_request = copy.deepcopy(envelope)
        probe_id = hashlib.sha256((envelope['request_id'] + ':network-preflight').encode()).hexdigest()[:32]
        probe_request['request_id'] = probe_request['request']['request_id'] = probe_id
        probe_request.pop('cleanup_receipt_path', None)
        probe_root = str(probe.parent).replace("'", "''")
        probe_workspace = str(envelope['request']['workspace']).replace("'", "''")
        probe_request['request']['arguments'] = ['-NoLogo', '-NoProfile', '-NonInteractive', '-Command',
            "$ErrorActionPreference='Stop'; "
            f"New-PSDrive -Name MewWork -PSProvider FileSystem -Root '{probe_workspace}' | Out-Null; "
            "Set-Location -LiteralPath 'MewWork:\\'; "
            f"New-PSDrive -Name MewProbe -PSProvider FileSystem -Root '{probe_root}' | Out-Null; "
            f"& 'MewProbe:\\{probe.name}' '{identity['profile_sid']}' 9999; exit $LASTEXITCODE"]
        request = {'operation': 'execute', 'request_id': envelope['request_id'],
            'envelope_base64': base64.b64encode(encode_request(envelope)).decode('ascii'),
            'probe_envelope_base64': base64.b64encode(encode_request(probe_request)).decode('ascii'),
            'native_helper': {'path': str(helper), 'sha256': hashlib.sha256(helper.read_bytes()).hexdigest(),
                              'profile_sid': identity['profile_sid']},
            'probe_helper': {'path': str(probe), 'sha256': hashlib.sha256(probe.read_bytes()).hexdigest()},
            'network': policy.as_dict(), 'test_hosts': list(test_hosts)}
        lock = _RunnerLock()
    except (OSError, ValueError, RuntimeError) as exc:
        # No command worker or ACL mutation has started in this preparation phase.
        result = NativeExecutionReceipt('denied', backend=BACKEND, command_started=False,
            cleanup_confirmed=True, detail=str(exc), error_code='network_runtime_unavailable')
        if on_receipt is not None:
            on_receipt(result)
        return result
    proc = None
    job = None
    result = None
    operation = None
    assigned = False
    interrupted = None
    marker_owned = False
    try:
        while not lock.acquire():
            if policy_for(checker).revision != policy.revision:
                raise RuntimeError('NETWORK_POLICY_CHANGED: 排队期间网络设置已变化，请重新评估任务。')
            await asyncio.sleep(.1)
        # Recheck after the previous holder has persisted its cleanup result.
        # Acceptance bypasses readiness, never an unsettled SID lifecycle.
        if cleanup_blocked():
            raise RuntimeError('NETWORK_RUNTIME_UNAVAILABLE: 上次联网执行清理尚未确认。')
        _poison_cleanup(envelope['request_id'])
        marker_owned = True
        from mewcode.sandbox.windows_job import ProcessJob
        job = ProcessJob()
        proc = await asyncio.create_subprocess_exec(str(node), str(WORKER), str(package),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW)
        # Worker waits for its private request before starting any descendants.
        job.assign(proc.pid)
        assigned = True
        data = json.dumps(request, ensure_ascii=True).encode() + b'\n'
        operation = asyncio.create_task(proc.communicate(data))
        deadline = time.monotonic() + envelope['timeout_ms'] / 1000 + 45
        while not operation.done():
            if policy_for(checker).revision != policy.revision:
                interrupted = RuntimeError('NETWORK_POLICY_CHANGED: 网络设置已变化，原调用已停止。')
                break
            if time.monotonic() > deadline:
                raise TimeoutError('NETWORK_TIMEOUT: 联网命令超时。')
            await asyncio.sleep(.1)
        if interrupted is not None:
            if cancel is not None:
                cancel.signal()
            await asyncio.wait_for(asyncio.shield(operation), 25)
        out, _ = await operation
        value = json.loads(out)
        if value.get('schema') != 'mewcode-srt-lpac-receipt-v1' or value.get('request_id') != envelope['request_id']:
            raise ValueError('SRT host receipt identity mismatch')
        cleanup = value.get('cleanup_confirmed') is True and job.active() == 0
        if value.get('native_receipt') is not None:
            result = lpac_network.decode(json.dumps(value['native_receipt']), envelope['request_id'],
                value['native_exit_code'], expected_sid=identity['profile_sid'])
            result = replace(result, cleanup_confirmed=cleanup and result.cleanup_confirmed is True,
                stderr=result.stderr + ('\n' + '\n'.join(value.get('network_denials') or [])
                                      if value.get('network_denials') else ''),
                command_started=value.get('command_started'), network_policy={**policy.as_dict(),
                    'file_boundary': 'lpac', 'package_sid': identity['profile_sid'],
                    'wfp_proxy_ports': [60080, 60081], 'preflight': value.get('preflight')})
        else:
            result = NativeExecutionReceipt('denied', backend=BACKEND, command_started=False,
                cleanup_confirmed=cleanup, detail=value.get('detail', ''),
                error_code=value.get('error_code', 'network_fence_unavailable'),
                network_policy={**policy.as_dict(), 'preflight': value.get('preflight')})
        if interrupted is not None:
            result = replace(result, status='cancelled', detail=str(interrupted))
        return result
    except (asyncio.CancelledError, TimeoutError, OSError, ValueError, RuntimeError) as exc:
        if assigned and cancel is not None and operation is not None and not operation.done():
            cancel.signal()
            try:
                out, _ = await asyncio.wait_for(asyncio.shield(operation), 25)
                value = json.loads(out)
                native = value.get('native_receipt')
                verified = lpac_network.decode(json.dumps(native), envelope['request_id'], value.get('native_exit_code'),
                    expected_sid=identity['profile_sid']) if native else None
                if (value.get('schema') == 'mewcode-srt-lpac-receipt-v1'
                        and value.get('request_id') == envelope['request_id'] and value.get('cleanup_confirmed') is True
                        and job.active() == 0 and (verified is None or verified.cleanup_confirmed is True)):
                    result = NativeExecutionReceipt('cancelled' if isinstance(exc, asyncio.CancelledError) else 'timed_out',
                        backend=BACKEND, cleanup_confirmed=True, command_started=value.get('command_started'),
                        detail=str(exc), network_policy=policy.as_dict())
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    return result
            except (TimeoutError, OSError, ValueError):
                pass
        terminated = assigned and job is not None and await asyncio.to_thread(job.terminate)
        if proc is not None:
            if not assigned and proc.returncode is None:
                proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except TimeoutError:
                terminated = False
        if operation is not None:
            try:
                diagnostic = await asyncio.wait_for(asyncio.shield(operation), 5)
            except (TimeoutError, asyncio.CancelledError, OSError):
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
                diagnostic = None
                terminated = False
        else:
            diagnostic = None
        cleanup = proc is None
        if terminated:
            cleanup = False  # forced termination cannot certify LPAC ACL/profile cleanup
        elif not assigned and proc is not None and proc.returncode is not None:
            cleanup = True  # worker received no input; no descendant or ACL operation started
        cleanup_evidence = None
        if assigned and job is not None and job.active() == 0 and envelope.get('cleanup_receipt_path'):
            # The proxy worker can lose its transport after the native helper
            # has durably completed cleanup. Match the exact frozen bytes;
            # process exit or an arbitrary child success message is insufficient.
            from mewcode.sandbox.cleanup_evidence import read_cleanup_evidence
            try:
                cleanup_evidence = read_cleanup_evidence(Path(envelope['cleanup_receipt_path']),
                    request_id=envelope['request_id'],
                    request_sha256=hashlib.sha256(encode_request(envelope)).hexdigest())
            except (OSError, ValueError):
                pass
            else:
                cleanup = True
        result = NativeExecutionReceipt('cancelled' if isinstance(exc, asyncio.CancelledError) else
            'timed_out' if isinstance(exc, TimeoutError) else 'denied', backend=BACKEND,
            cleanup_confirmed=cleanup, command_started=None if proc else False,
            detail=str(exc) or 'NETWORK_CANCELLED: 联网命令已取消。', error_code='srt_interrupted',
            network_policy={**policy.as_dict(), 'process_tree_stopped': terminated,
                'cleanup_evidence': cleanup_evidence})
        if isinstance(diagnostic, tuple):
            phases = [line.decode(errors='replace') for line in diagnostic[1].splitlines()
                      if line.startswith(b'MEW_SRT_STAGE: ')]
            if phases:
                result = replace(result, detail=result.detail + ' (' + phases[-1] + ')')
        if isinstance(exc, asyncio.CancelledError):
            raise
        return result
    finally:
        try:
            if job is not None:
                job.close()
            if result is not None:
                if on_receipt is not None:
                    try:
                        on_receipt(result)
                    except BaseException:
                        _poison_cleanup(envelope['request_id'])
                        raise
                elif marker_owned and result.cleanup_confirmed is not True:
                    _poison_cleanup(envelope['request_id'])
                if marker_owned and result.cleanup_confirmed is True:
                    _clear_cleanup_marker(envelope['request_id'])
        finally:
            lock.close()


def _poison_cleanup(request_id):
    path = _network_shared_root() / 'cleanup-unconfirmed.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.pending')
    with temporary.open('w', encoding='utf-8') as stream:
        stream.write(json.dumps({'request_id': request_id, 'cleanup_confirmed': False}))
        stream.flush()
        os.fsync(stream.fileno())
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    kernel.MoveFileExW.restype = wintypes.BOOL
    if not kernel.MoveFileExW(str(temporary), str(path), 1 | 8):  # REPLACE_EXISTING | WRITE_THROUGH
        raise ctypes.WinError(ctypes.get_last_error())


def _clear_cleanup_marker(request_id):
    path = _network_shared_root() / 'cleanup-unconfirmed.json'
    if json.loads(path.read_text(encoding='utf-8')).get('request_id') != request_id:
        raise RuntimeError('NETWORK_RUNTIME_UNAVAILABLE: 清理标记身份发生变化。')
    path.unlink()


async def execute_srt(command, *, checker, work_dir, timeout, on_receipt=None, publication_mode=None):
    from mewcode.sandbox import lpac_network
    from mewcode.sandbox.lpac_runtime import NativeScope, find_runtime_helper
    from mewcode.sandbox.staged_execution import PROTOCOL, execute_staged
    from mewcode.sandbox.windows import NativeBoundaryUnavailable
    try:
        scope = NativeScope.from_checker(checker, publication_mode=publication_mode)
        if str(scope.workspace) != str(Path(work_dir).resolve()):
            raise NativeBoundaryUnavailable('Network command workspace differs from host task scope')
    except (NativeBoundaryUnavailable, OSError, ValueError, AttributeError) as exc:
        return NativeExecutionReceipt('denied', backend=BACKEND, command_started=False,
            cleanup_confirmed=True, detail=str(exc), error_code='native_scope_invalid')
    status = runtime_status()
    if not status['ready']:
        return NativeExecutionReceipt('denied', backend=BACKEND, command_started=False,
            cleanup_confirmed=True, detail=status['reason'], error_code='network_runtime_unavailable',
            network_policy=policy_for(checker).as_dict())
    helper = find_runtime_helper(require_protocol=PROTOCOL)
    from mewcode.sandbox.network_git import runtime_git_directory
    return await execute_staged(command, scope=scope, helper=helper, timeout=timeout,
        on_receipt=on_receipt,
        runtime_git_dir=runtime_git_directory(),
        request_transform=lpac_network.transform,
        runner=lambda envelope, cancel, observed:
            invoke_srt(envelope, checker=checker, cancel=cancel, on_receipt=observed))
