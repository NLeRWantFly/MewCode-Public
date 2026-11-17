"""Offline regression tests for the native SRT process and permission boundary."""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mewcode.permissions.modes import PermissionMode
from mewcode.permissions.network_policy import NetworkPolicy
from mewcode.permissions.policy import build_permission_checker
from mewcode.sandbox import lpac_network
from mewcode.sandbox import srt_runtime
from mewcode.sandbox.receipt import NativeExecutionReceipt
from mewcode.tools.command_sandbox import execute_native_command, native_auto_command
from mewcode.tools.terminal import TerminalInput, TerminalStart


def _envelope(tmp_path: Path) -> dict:
    return {
        'request_id': '0123456789abcdef0123456789abcdef',
        'timeout_ms': 1000,
        'request': {
            'schema': lpac_network.NET_SCHEMA,
            'request_id': '0123456789abcdef0123456789abcdef',
            'runtime_network': True,
            'workspace': str(tmp_path),
            'read_paths': [str(tmp_path)],
            'write_paths': [str(tmp_path)],
        },
    }


class _Lock:
    def __init__(self, *_args, **_kwargs):
        self.closed = False

    def acquire(self):
        return True

    def close(self):
        self.closed = True


class _FakeProcess:
    pid = 4567

    def __init__(self):
        self.returncode = None
        self.kill_count = 0
        self.wait_count = 0
        self.input_payloads = []
        self.output = b''
        self.finished = asyncio.Event()
        self.loop = asyncio.get_running_loop()

    async def communicate(self, data=None):
        if data is not None:
            self.input_payloads.append(data)
        await self.finished.wait()
        return self.output, b''

    def kill(self):
        self.kill_count += 1
        self.returncode = -9
        self.finished.set()

    async def wait(self):
        self.wait_count += 1
        await self.finished.wait()
        if self.returncode is None:
            self.returncode = -9
        return self.returncode


class _Job:
    def __init__(self, process, *, assign_error=None):
        self.process = process
        self.assign_error = assign_error
        self.assigned = False
        self.terminate_count = 0
        self.closed = False

    def assign(self, _pid):
        if self.assign_error is not None:
            raise self.assign_error
        self.assigned = True

    def terminate(self):
        self.terminate_count += 1
        self.process.loop.call_soon_threadsafe(self.process.finished.set)
        self.process.returncode = -9
        return True

    def active(self):
        return 0

    def close(self):
        self.closed = True


class _Cancel:
    def __init__(self, process, request_id):
        self.process = process
        self.request_id = request_id
        self.signalled = False

    def signal(self):
        self.signalled = True
        self.process.output = json.dumps({
            'schema': 'mewcode-srt-lpac-receipt-v1',
            'request_id': self.request_id,
            'cleanup_confirmed': True,
            'native_receipt': None,
            'native_exit_code': None,
            'command_started': False,
            'error_code': 'network_policy_changed',
            'detail': 'policy changed after dispatch',
        }).encode()
        self.process.loop.call_soon_threadsafe(self.process.finished.set)


def _patch_srt_host(monkeypatch, tmp_path, process, job):
    monkeypatch.setattr(srt_runtime, 'runtime_paths', lambda: (tmp_path / 'node.exe', tmp_path, tmp_path / 'srt.exe'))
    locks = []

    def make_lock():
        lock = _Lock()
        locks.append(lock)
        return lock

    monkeypatch.setattr(srt_runtime, '_RunnerLock', make_lock)
    monkeypatch.setattr(srt_runtime, '_network_shared_root', lambda: tmp_path / 'shared-network-root')
    monkeypatch.setattr(srt_runtime, 'cleanup_blocked', lambda: False)
    helper = tmp_path / 'MewCode.AppContainerRuntime.exe'
    probe = tmp_path / 'MewCode.NetworkProbe.exe'
    helper.write_bytes(b'network-helper-stub')
    probe.write_bytes(b'network-probe-stub')
    identity = lpac_network.NetworkProfileIdentity(
        profile_name='mewcode-network-v1-0123456789abcdef',
        profile_sid='S-1-15-2-10-20-30-40',
        user_sid='S-1-5-21-101-202-303-1001',
    )
    monkeypatch.setattr(lpac_network, 'paths', lambda: (helper, probe))
    monkeypatch.setattr(lpac_network, 'profile', lambda **_kwargs: identity)
    import mewcode.sandbox.windows_job as windows_job
    monkeypatch.setattr(windows_job, 'ProcessJob', lambda: job)

    async def create_process(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create_process)
    return locks


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["paths", "readiness", "profile", "lock"])
async def test_prelaunch_failure_reports_no_process_and_durable_cleanup(monkeypatch, tmp_path, phase):
    from unittest.mock import AsyncMock
    process = _FakeProcess()
    _patch_srt_host(monkeypatch, tmp_path, process, _Job(process))
    monkeypatch.setattr(srt_runtime, 'runtime_status', lambda: {'ready': True})
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic prelaunch failure')
    if phase == 'paths':
        monkeypatch.setattr(srt_runtime, 'runtime_paths', fail)
    elif phase == 'readiness':
        monkeypatch.setattr(srt_runtime, 'runtime_status', lambda: {'ready': False, 'reason': 'not ready'})
    elif phase == 'profile':
        monkeypatch.setattr(lpac_network, 'profile', fail)
    else:
        monkeypatch.setattr(srt_runtime, '_RunnerLock', fail)
    launch = AsyncMock(side_effect=AssertionError('prelaunch must not start a process'))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
    receipts = []
    result = await srt_runtime.invoke_srt(_envelope(tmp_path),
        checker=SimpleNamespace(network_policy=NetworkPolicy()), on_receipt=receipts.append)
    assert result.is_error and result.command_started is False and result.cleanup_confirmed is True
    assert receipts == [result]
    launch.assert_not_called()


@pytest.mark.asyncio
async def test_job_assignment_failure_kills_and_reaps_before_any_request_is_sent(monkeypatch, tmp_path):
    process = _FakeProcess()
    job = _Job(process, assign_error=OSError('simulated Job Object assignment failure'))
    _patch_srt_host(monkeypatch, tmp_path, process, job)
    checker = SimpleNamespace(network_policy=NetworkPolicy())
    wait_timeouts = []
    real_wait_for = asyncio.wait_for

    async def bounded_wait(awaitable, timeout):
        wait_timeouts.append(timeout)
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, 'wait_for', bounded_wait)
    receipt = await srt_runtime.invoke_srt(_envelope(tmp_path), checker=checker, acceptance=True)

    assert receipt.status == 'denied'
    assert receipt.cleanup_confirmed is True
    assert process.kill_count == 1
    assert process.wait_count == 1
    assert process.input_payloads == []  # communicate() was never started after assign failed.
    assert 5 in wait_timeouts  # the process reap has an explicit upper bound.
    assert job.closed is True


@pytest.mark.asyncio
async def test_policy_revision_change_cancels_inflight_call_without_replay(monkeypatch, tmp_path):
    process = _FakeProcess()
    job = _Job(process)
    _patch_srt_host(monkeypatch, tmp_path, process, job)
    cancel = _Cancel(process, '0123456789abcdef0123456789abcdef')
    checker = SimpleNamespace(network_policy=NetworkPolicy())
    creations = []
    real_create_process = asyncio.create_subprocess_exec

    async def create_once(*args, **kwargs):
        creations.append((args, kwargs))
        return await real_create_process(*args, **kwargs)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create_once)
    operation = asyncio.create_task(
        srt_runtime.invoke_srt(_envelope(tmp_path), checker=checker, cancel=cancel, acceptance=True)
    )
    for _ in range(100):
        if process.input_payloads:
            break
        await asyncio.sleep(0.01)
    assert len(process.input_payloads) == 1
    host_request = json.loads(process.input_payloads[0])
    assert host_request['operation'] == 'execute'
    assert host_request['native_helper']['profile_sid'] == 'S-1-15-2-10-20-30-40'
    staged = json.loads(base64.b64decode(host_request['envelope_base64']))
    assert staged['request']['schema'] == lpac_network.NET_SCHEMA
    assert staged['request']['runtime_network'] is True
    preflight = json.loads(base64.b64decode(host_request['probe_envelope_base64']))
    probe_script = preflight['request']['arguments'][-1]
    assert "New-PSDrive -Name MewWork" in probe_script
    assert "Set-Location -LiteralPath 'MewWork:\\'" in probe_script
    assert "New-PSDrive -Name MewProbe" in probe_script
    assert "& 'MewProbe:\\MewCode.NetworkProbe.exe'" in probe_script
    assert "Set-Location -LiteralPath 'MewProbe:" not in probe_script
    assert preflight['request']['read_paths'] == staged['request']['read_paths']
    assert preflight['request']['write_paths'] == staged['request']['write_paths']
    checker.network_policy = NetworkPolicy(False)

    receipt = await asyncio.wait_for(operation, timeout=2)

    assert receipt.status == 'cancelled'
    assert receipt.cleanup_confirmed is True
    assert cancel.signalled is True
    assert job.terminate_count == 0
    assert len(creations) == 1
    assert len(process.input_payloads) == 1


@pytest.mark.asyncio
async def test_cleanup_receipt_is_recorded_before_runner_lock_releases(monkeypatch, tmp_path):
    process = _FakeProcess()
    process.output = json.dumps({
        'schema': 'mewcode-srt-lpac-receipt-v1',
        'request_id': '0123456789abcdef0123456789abcdef',
        'cleanup_confirmed': True,
        'native_receipt': None,
        'native_exit_code': None,
        'command_started': False,
        'error_code': 'network_fence_unavailable',
        'detail': 'synthetic preflight failure',
        'preflight': {'ready': False, 'probe_stderr': 'synthetic path failure'},
    }).encode()
    job = _Job(process)
    locks = _patch_srt_host(monkeypatch, tmp_path, process, job)
    checker = SimpleNamespace(network_policy=NetworkPolicy())
    recorded = []

    async def create_process(*_args, **_kwargs):
        process.finished.set()
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create_process)
    marker = tmp_path / 'shared-network-root' / 'cleanup-unconfirmed.json'
    receipt = await srt_runtime.invoke_srt(_envelope(tmp_path), checker=checker, acceptance=True,
        on_receipt=lambda item: recorded.append((item.cleanup_confirmed, locks[0].closed, marker.exists())))

    assert receipt.cleanup_confirmed is True
    assert receipt.network_policy['preflight']['probe_stderr'] == 'synthetic path failure'
    assert recorded == [(True, False, True)]
    assert locks[0].closed is True
    assert marker.exists() is False


@pytest.mark.asyncio
async def test_cleanup_unconfirmed_poisons_only_isolated_shared_root(monkeypatch, tmp_path):
    process = _FakeProcess()
    process.output = json.dumps({
        'schema': 'mewcode-srt-lpac-receipt-v1',
        'request_id': '0123456789abcdef0123456789abcdef',
        'cleanup_confirmed': False,
        'native_receipt': None,
        'native_exit_code': None,
        'command_started': False,
        'error_code': 'network_fence_unavailable',
        'detail': 'synthetic cleanup failure',
    }).encode()
    job = _Job(process)
    _patch_srt_host(monkeypatch, tmp_path, process, job)
    checker = SimpleNamespace(network_policy=NetworkPolicy())

    async def create_process(*_args, **_kwargs):
        process.finished.set()
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create_process)
    receipt = await srt_runtime.invoke_srt(_envelope(tmp_path), checker=checker, acceptance=True)

    marker = tmp_path / 'shared-network-root' / 'cleanup-unconfirmed.json'
    assert receipt.cleanup_confirmed is False
    assert marker.exists()
    assert json.loads(marker.read_text(encoding='utf-8')) == {
        'request_id': '0123456789abcdef0123456789abcdef', 'cleanup_confirmed': False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize('matches_request', [True, False])
async def test_lost_proxy_transport_uses_only_exact_durable_native_cleanup(monkeypatch, tmp_path, matches_request):
    import hashlib
    from mewcode.sandbox.cleanup_evidence import BACKEND, SCHEMA
    from mewcode.sandbox.lpac_runtime import encode_request

    process = _FakeProcess()
    process.output = b'{broken outer transport'
    process.finished.set()
    job = _Job(process)
    _patch_srt_host(monkeypatch, tmp_path, process, job)
    envelope = _envelope(tmp_path)
    proof = tmp_path / 'cleanup' / 'receipt.json'
    proof.parent.mkdir()
    envelope['cleanup_receipt_path'] = str(proof)
    digest = hashlib.sha256(encode_request(envelope)).hexdigest()
    proof.write_text(json.dumps({'schema': SCHEMA, 'backend': BACKEND,
        'request_id': envelope['request_id'], 'request_sha256': digest if matches_request else '0' * 64,
        'completed': True, 'job_cleanup_ok': True, 'profile_cleanup_ok': True, 'acl_cleanup_ok': True}),
        encoding='utf-8')

    receipt = await srt_runtime.invoke_srt(envelope,
        checker=SimpleNamespace(network_policy=NetworkPolicy()), acceptance=True)

    assert receipt.status == 'denied'  # Cleanup never invents successful execution.
    assert receipt.command_started is None
    assert receipt.cleanup_confirmed is matches_request
    assert bool(receipt.network_policy['cleanup_evidence']) is matches_request
    assert (tmp_path / 'shared-network-root' / 'cleanup-unconfirmed.json').exists() is not matches_request


@pytest.mark.asyncio
async def test_cleanup_state_is_rechecked_after_lock_before_helper_starts(monkeypatch, tmp_path):
    process = _FakeProcess()
    job = _Job(process)
    locks = _patch_srt_host(monkeypatch, tmp_path, process, job)
    monkeypatch.setattr(srt_runtime, 'cleanup_blocked', lambda: True)
    checker = SimpleNamespace(network_policy=NetworkPolicy())
    launches = []
    marker = tmp_path / 'shared-network-root' / 'cleanup-unconfirmed.json'
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({'request_id': 'previous', 'cleanup_confirmed': False}), encoding='utf-8')

    async def create_process(*_args, **_kwargs):
        launches.append(True)
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create_process)
    receipt = await srt_runtime.invoke_srt(_envelope(tmp_path), checker=checker, acceptance=True)

    assert receipt.status == 'denied'
    assert launches == []
    assert process.input_payloads == []
    assert job.assigned is False
    assert locks[0].closed is True
    assert json.loads(marker.read_text(encoding='utf-8')) == {
        'request_id': 'previous', 'cleanup_confirmed': False,
    }


@pytest.mark.parametrize('mode', [PermissionMode.DEFAULT, PermissionMode.BYPASS])
def test_windows_terminal_start_and_input_stop_at_sandbox_boundary(monkeypatch, tmp_path, mode):
    import mewcode.permissions.checker as checker_module

    # Exercise the explicit Windows-only boundary on any test host without
    # changing the process-wide os module or starting a terminal session.
    monkeypatch.setattr(checker_module, 'os', SimpleNamespace(name='nt'))
    checker = build_permission_checker(tmp_path, mode=mode)
    checker.network_policy = NetworkPolicy()
    start = TerminalStart(sessions=None)
    terminal_input = TerminalInput(sessions=None)

    started = checker.check(start, {'command': 'echo synthetic'})
    sent = checker.check(terminal_input, {
        'session_id': 'synthetic-owned-session', 'text': 'echo synthetic\n',
    })
    stopped = checker.check(terminal_input, {
        'session_id': 'synthetic-owned-session', 'stop': True,
    })

    assert (started.effect, started.source, started.subcondition) == (
        'deny', 'terminal_boundary', 'interactive_terminal_unbounded'
    )
    assert (sent.effect, sent.source, sent.subcondition) == (
        'deny', 'terminal_boundary', 'interactive_terminal_unbounded'
    )
    assert (stopped.effect, stopped.source) == ('allow', 'terminal_stop')


@pytest.mark.asyncio
async def test_strict_windows_network_command_selects_native_srt_path(monkeypatch, tmp_path):
    import mewcode.permissions.checker as checker_module
    import mewcode.sandbox.srt_runtime as runtime_module

    monkeypatch.setattr(checker_module, 'os', SimpleNamespace(name='nt'))
    checker = build_permission_checker(tmp_path, mode=PermissionMode.STRICT_ISOLATION)
    checker.network_policy = NetworkPolicy(True)
    assert checker.uses_native_commands() is True
    calls = []

    async def fake_execute_srt(command, *, checker, work_dir, timeout,
                               on_receipt=None, publication_mode=None):
        calls.append((command, checker, work_dir, timeout, on_receipt, publication_mode))
        return NativeExecutionReceipt(
            'exited', exit_code=0, backend='mewcode-windows-srt',
            cleanup_confirmed=True, network_policy=checker.network_policy.as_dict(),
        )

    monkeypatch.setattr(runtime_module, 'execute_srt', fake_execute_srt)
    with native_auto_command(checker):
        receipt = await execute_native_command(
            'echo synthetic', work_dir=str(tmp_path), timeout=1,
        )

    assert receipt.backend == 'mewcode-windows-srt'
    assert len(calls) == 1
    assert calls[0][:4] == ('echo synthetic', checker, str(tmp_path), 1)
