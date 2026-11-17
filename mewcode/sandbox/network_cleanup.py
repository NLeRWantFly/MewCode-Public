"""Explicit host recovery of temporary network ACEs, never command replay."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

from mewcode.runtime_paths import user_data_root
from mewcode.sandbox import lpac_network, lpac_runtime
from mewcode.sandbox.cleanup_evidence import read_cleanup_evidence
from mewcode.sandbox.transaction_registry import TransactionRegistry

SOURCE_NAMES = ('MewCode.AppContainerProbe.cs', 'MewCode.AppContainerRuntime.cs',
                'MewCode.StagedWorkspacePrototype.cs', 'MewCode.NetworkCleanup.cs')
BINARY = 'MewCode.NetworkCleanup.exe'


def _helper():
    sources = [lpac_runtime.checked_path(lpac_network.ROOT / 'native/windows' / name, file=True)
               for name in SOURCE_NAMES]
    hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    identity = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    directory = user_data_root() / 'native/network-cleanup' / identity
    if not directory.exists():
        directory.mkdir(parents=True)
        lpac_runtime.checked_path(directory)
        compiler = lpac_network._checked_framework_compiler(lpac_network._windows_system_root())
        result = subprocess.run([str(compiler), '/nologo', '/platform:x64', '/r:System.Web.Extensions.dll',
            '/main:MewCode.Native.Windows.MewCodeNetworkCleanup', '/out:' + str(directory / BINARY),
            *(str(path) for path in sources)], capture_output=True, timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError('NETWORK_CLEANUP_UNAVAILABLE: 固定清理组件编译失败。')
        if hashes != {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}:
            raise RuntimeError('NETWORK_CLEANUP_UNAVAILABLE: 清理组件源码发生变化。')
        (directory / 'manifest.json').write_text(json.dumps({'sources': hashes,
            'sha256': hashlib.sha256((directory / BINARY).read_bytes()).hexdigest()}), encoding='utf-8')
    lpac_runtime.checked_path(directory)
    executable = lpac_runtime.checked_path(directory / BINARY, file=True)
    manifest = lpac_network._read_json(lpac_runtime.checked_path(directory / 'manifest.json', file=True).read_bytes(),
                                      limit=16384)
    if manifest.get('sources') != hashes or manifest.get('sha256') != hashlib.sha256(executable.read_bytes()).hexdigest():
        raise RuntimeError('NETWORK_CLEANUP_UNAVAILABLE: 清理组件完整性不匹配。')
    return executable


def recover_cleanup(registry: TransactionRegistry | None = None) -> dict:
    """Called only by the explicit host setup action, under the user runner lock.

    It removes only old temporary grants after bound native evidence confirms
    the old job and profile are gone. It never publishes or reruns the candidate.
    """
    from mewcode.sandbox.srt_runtime import _RunnerLock, _network_shared_root, _clear_cleanup_marker
    registry = registry or TransactionRegistry()
    lock = _RunnerLock()
    try:
        if not lock.acquire():
            raise RuntimeError('NETWORK_CLEANUP_UNAVAILABLE: 运行器仍在使用中。')
        marker_path = _network_shared_root() / 'cleanup-unconfirmed.json'
        if not marker_path.exists():
            return {'cleanup_confirmed': True, 'recovered': False}
        marker = lpac_network._read_json(lpac_runtime.checked_path(marker_path, file=True).read_bytes(), limit=16384)
        records = [r for r in registry.pending() if r.get('request_id') == marker.get('request_id')]
        if len(records) != 1 or (records[0].get('native_receipt') or {}).get('backend') != 'mewcode-windows-srt':
            raise RuntimeError('NETWORK_CLEANUP_UNAVAILABLE: 找不到该请求唯一的宿主事务，请从原执行记录恢复。')
        record = records[0]
        with registry.claim(record['id']):
            transaction = lpac_runtime.checked_path(Path(record['transaction']))
            frozen = lpac_runtime.checked_path(transaction / 'cleanup/request.bin', file=True).read_bytes()
            if len(frozen) > lpac_runtime.MAX_REQUEST or hashlib.sha256(frozen).hexdigest() != record['request_sha256']:
                raise RuntimeError('NETWORK_CLEANUP_UNAVAILABLE: 原始请求与事务哈希不匹配。')
            envelope = lpac_network._read_json(frozen, limit=lpac_runtime.MAX_REQUEST)
            if (envelope.get('request_id') != record['request_id']
                    or Path(envelope['request']['workspace']).resolve() != transaction / 'workspace'
                    or Path(envelope['original_workspace']).resolve() != Path(record['workspace']).resolve()):
                raise RuntimeError('NETWORK_CLEANUP_UNAVAILABLE: 冻结请求的事务和工作区身份不匹配。')
            proof_path = transaction / 'cleanup/recovery-receipt.json'
            if not proof_path.exists():
                result = subprocess.run([str(_helper())], input=frozen, capture_output=True, timeout=120,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
                if result.returncode != 0:
                    detail = result.stdout.decode('utf-8', errors='replace')[:1600]
                    raise RuntimeError('NETWORK_CLEANUP_UNAVAILABLE: 原生清理没有通过核验。' + detail)
            proof = read_cleanup_evidence(proof_path, request_id=record['request_id'],
                                          request_sha256=record['request_sha256'])
            # Preserve execution failure. A cleanup proof cannot assert success
            # or approve an interrupted publication.
            state = 'recovery_required' if (transaction / 'publication/plan.json').exists() else 'rejected'
            registry.update(record['id'], state, native_receipt={**record['native_receipt'], 'cleanup_confirmed': True},
                            cleanup_evidence=proof, reason='network_temporary_acl_cleanup_recovered')
            _clear_cleanup_marker(record['request_id'])
            return {'cleanup_confirmed': True, 'recovered': True, 'transaction_id': record['id'],
                    'state': state, 'command_replayed': False}
    finally:
        lock.close()
