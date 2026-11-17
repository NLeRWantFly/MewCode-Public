"""Explicit one-time configuration, separate from ordinary node dispatch."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import shutil
import time
import uuid

from mewcode.runtime_paths import user_data_root
from mewcode.sandbox.srt_runtime import WORKER, configuration_status, runtime_paths


def configure_and_verify():
    current = configuration_status()
    if not current['provisioned'] or current['wfp'] == 'absent':
        node, package, _ = runtime_paths()
        run = subprocess.run([str(node), str(WORKER), str(package)],
            input=b'{"operation":"install","request_id":"install"}\n',
            capture_output=True, timeout=180, creationflags=subprocess.CREATE_NO_WINDOW)
        if run.returncode != 0 or json.loads(run.stdout).get('installed') is not True:
            raise RuntimeError('NETWORK_RUNTIME_UNAVAILABLE: Windows 联网运行器安装未完成。')
        current = configuration_status()
    ensure_helper_execute(current)
    configure_lpac_network()
    # Only this host action runs acceptance. No ordinary tool or node can
    # configure accounts, filters or write its own acceptance record.
    from mewcode.sandbox.srt_acceptance import verify_runtime
    return verify_runtime(user_data_root() / 'srt-acceptance-work', publish=True)


def configure_lpac_network():
    """Explicit host setup; ordinary nodes cannot build or elevate components."""
    from mewcode.sandbox import lpac_network, network_fence
    from mewcode.sandbox.lpac_runtime import checked_path
    from mewcode.sandbox.srt_runtime import _RunnerLock, cleanup_blocked
    if cleanup_blocked():
        from mewcode.sandbox.network_cleanup import recover_cleanup
        recover_cleanup()
    lock = _RunnerLock()
    try:
        deadline = time.monotonic() + 120
        while not lock.acquire():
            if time.monotonic() > deadline:
                raise RuntimeError('NETWORK_RUNTIME_UNAVAILABLE: 有命令正在使用隔离环境，请在结束后配置。')
            time.sleep(.1)
        if cleanup_blocked():
            raise RuntimeError('NETWORK_RUNTIME_UNAVAILABLE: 请先处理上次未确认的执行清理。')
        from mewcode.sandbox.network_git import ensure_https_helpers
        ensure_https_helpers()
        try:
            lpac_network.paths()
        except (OSError, ValueError, RuntimeError):
            lpac_network.install()
        profile = lpac_network.profile(provision=True)
        target = user_data_root() / 'native/windows-network-fence-v1'
        try:
            fence = network_fence.load_candidate(target)
        except network_fence.NetworkFenceError:
            from scripts.build_network_fence import build
            candidate = network_fence.ROOT / ('.tmp/network-fence-' + uuid.uuid4().hex)
            build(candidate)
            network_fence.load_candidate(candidate)
            target.parent.mkdir(parents=True, exist_ok=True)
            checked_path(target.parent)
            stage = target.parent / ('.network-fence-build-' + uuid.uuid4().hex)
            stage.mkdir()
            for name in (network_fence.BINARY_NAME, network_fence.MANIFEST_NAME):
                shutil.copy2(candidate / name, stage / name)
            network_fence.load_candidate(stage)
            backup = None
            if target.exists():
                checked_path(target)
                for item in target.iterdir():
                    if item.name not in (network_fence.BINARY_NAME, network_fence.MANIFEST_NAME):
                        raise RuntimeError('NETWORK_RUNTIME_UNAVAILABLE: 联网组件目录含未知文件。')
                    checked_path(item, file=True)
                backup = target.with_name(target.name + '-previous-' + uuid.uuid4().hex)
                target.rename(backup)
            try:
                stage.rename(target)
                fence = network_fence.load_candidate(target)
            except BaseException:
                if backup is not None:
                    if target.exists():
                        target.rename(target.with_name(target.name + '-failed-' + uuid.uuid4().hex))
                    backup.rename(target)
                raise
        install_path = user_data_root() / 'network-fence-install.json'
        installed = None
        if install_path.exists():
            try:
                installed = json.loads(checked_path(install_path, file=True).read_text(encoding='utf-8'))
            except (OSError, ValueError):
                pass
        call = network_fence.prepare_status_call(fence, profile.profile_sid)
        observed = subprocess.run(call.argv, capture_output=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW)
        status = network_fence.parse_status_result(observed.stdout, expected_sid=profile.profile_sid)
        remembered = (isinstance(installed, dict) and installed.get('sha256') == fence.sha256
            and installed.get('sid') == profile.profile_sid and installed.get('installed') is True)
        # An unelevated host may lack WFP enumeration access. Its previous
        # setup receipt can avoid another UAC; kernel probes still gate each call.
        if not (status.installed is True or (status.requires_administrator and remembered)):
            reports = user_data_root() / 'network-setup-reports'
            reports.mkdir(parents=True, exist_ok=True)
            checked_path(reports)
            report = reports / (uuid.uuid4().hex + '.json')
            call = network_fence.prepare_install_call(fence, profile.profile_sid, report)
            _elevate_fixed_install(call)
            status = network_fence.read_install_report(report, expected_sid=profile.profile_sid)
            if not status.ok or status.installed is not True:
                raise RuntimeError('NETWORK_RUNTIME_UNAVAILABLE: AppContainer 出网约束配置失败：' + status.detail)
        value = {'sid': profile.profile_sid, 'sha256': fence.sha256, 'installed': True}
        pending = install_path.with_suffix('.pending')
        pending.write_text(json.dumps(value, sort_keys=True), encoding='utf-8')
        os.replace(pending, install_path)
        return value
    finally:
        lock.close()


def _elevate_fixed_install(call):
    """One fixed, verified native action; no arbitrary command elevation."""
    import ctypes as C
    from ctypes import wintypes as W
    from mewcode.sandbox.metadata_broker import _ShellExecuteInfo
    from mewcode.sandbox.network_fence import load_candidate, verify_candidate
    from mewcode.sandbox.publication_io import PinnedPath
    fence = load_candidate(call.executable.parent)
    verify_candidate(fence)
    with PinnedPath(call.executable) as pin:
        if pin.digest(16 * 1024 * 1024) != call.sha256:
            raise RuntimeError('NETWORK_RUNTIME_UNAVAILABLE: 安装组件在执行前发生变化。')
        info = _ShellExecuteInfo()
        info.size, info.mask, info.verb, info.file = C.sizeof(info), 0x40 | 0x100, 'runas', str(call.executable)
        info.parameters, info.show = subprocess.list2cmdline(call.arguments), 0
        shell = C.WinDLL('shell32', use_last_error=True)
        shell.ShellExecuteExW.argtypes, shell.ShellExecuteExW.restype = [C.POINTER(_ShellExecuteInfo)], W.BOOL
        if not shell.ShellExecuteExW(C.byref(info)):
            raise RuntimeError('NETWORK_RUNTIME_UNAVAILABLE: Windows 未完成一次性联网约束配置。') from C.WinError(C.get_last_error())
        kernel = C.WinDLL('kernel32', use_last_error=True)
        kernel.WaitForSingleObject.argtypes, kernel.WaitForSingleObject.restype = [W.HANDLE, W.DWORD], W.DWORD
        kernel.CloseHandle.argtypes = [W.HANDLE]
        try:
            if kernel.WaitForSingleObject(info.process, 180000) != 0:
                raise RuntimeError('NETWORK_RUNTIME_UNAVAILABLE: 一次性网络安装尚未完成，请稍后检查状态。')
        finally:
            kernel.CloseHandle(info.process)


def ensure_helper_execute(status=None):
    """Grant RX to this fixed executable, never to a profile or whole tree."""
    import re
    status = status or configuration_status()
    sid = status.get('sandbox_sid', '')
    if not re.fullmatch(r'S-1-5-21-\d+-\d+-\d+-\d+', sid):
        raise RuntimeError('NETWORK_RUNTIME_UNAVAILABLE: 专用沙箱账户身份不完整。')
    _, _, helper = runtime_paths()
    result = subprocess.run([str(Path(os.environ['SystemRoot']) / 'System32/icacls.exe'),
        str(helper), '/grant', '*' + sid + ':RX'], capture_output=True, timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode != 0:
        raise RuntimeError('NETWORK_RUNTIME_UNAVAILABLE: 无法给专用沙箱账户设置运行器读取与执行权限。')
    return {'helper_execute_configured': True, 'file': str(helper)}


def runtime_read_roots():
    import sys
    from mewcode.sandbox.lpac_runtime import find_runtime_helper, checked_path
    from mewcode.sandbox.staged_execution import PROTOCOL
    python = checked_path(Path(sys.base_prefix).resolve(strict=True))
    if not (python / 'python.exe').is_file() or not (python / 'Lib/encodings/__init__.py').is_file():
        raise RuntimeError('NETWORK_RUNTIME_UNAVAILABLE: Python 运行依赖目录无效。')
    git = checked_path(find_runtime_helper(require_protocol=PROTOCOL).parent / 'git')
    return (python, git)


def ensure_runtime_read(status=None):
    """One-time read-only SDK grants, separate from per-command data grants."""
    import re
    status = status or configuration_status()
    sid = status.get('sandbox_group_sid', '')
    if not re.fullmatch(r'S-1-5-21-\d+-\d+-\d+-\d+', sid):
        raise RuntimeError('NETWORK_RUNTIME_UNAVAILABLE: 沙箱运行器组身份不完整。')
    roots = runtime_read_roots()
    for root in roots:
        result = subprocess.run([str(Path(os.environ['SystemRoot']) / 'System32/icacls.exe'),
            str(root), '/grant', '*' + sid + ':(OI)(CI)RX'], capture_output=True, timeout=180,
            creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode != 0:
            raise RuntimeError('NETWORK_RUNTIME_UNAVAILABLE: 只读运行依赖配置失败。')
    return {'runtime_read_configured': True, 'roots': list(map(str, roots))}
