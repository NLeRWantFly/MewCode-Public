"""Offline routing tests for the synthetic SRT/LPAC acceptance harness."""
from __future__ import annotations

from types import SimpleNamespace
import json

import pytest

from mewcode.permissions.network_policy import NetworkPolicy
from mewcode.sandbox import lpac_network, network_git, srt_acceptance, srt_runtime
from mewcode.sandbox.receipt import NativeExecutionReceipt


def _receipt(*, stderr='', policy=None, status='denied', cleanup=True):
    return NativeExecutionReceipt(status, stderr=stderr, backend='mewcode-windows-lpac',
        cleanup_confirmed=cleanup, exit_code=1 if status == 'denied' else 0,
        network_policy=policy)


@pytest.mark.asyncio
async def test_enabled_policy_transforms_staged_request_and_passes_cancel(monkeypatch, tmp_path):
    helper, probe = tmp_path / 'helper.exe', tmp_path / 'probe.exe'
    monkeypatch.setattr(lpac_network, 'paths', lambda: (helper, probe))
    monkeypatch.setattr(srt_acceptance, 'runtime_identity', lambda: 'stable-runtime')
    git_dir = tmp_path / 'separate-network-git'
    monkeypatch.setattr(network_git, 'runtime_git_directory', lambda: git_dir, raising=False)
    seen = {}
    observed_receipts = []
    staged_observed = []

    async def invoke_srt(envelope, *, checker, cancel, acceptance, test_hosts, on_receipt):
        seen.update(envelope=envelope, checker=checker, cancel=cancel,
                    acceptance=acceptance, test_hosts=test_hosts)
        receipt = _receipt(status='exited', policy=checker.network_policy.as_dict())
        on_receipt(receipt)
        return receipt

    async def execute_staged(script, **kwargs):
        seen['execution'] = kwargs
        envelope = {'request': {'schema': 'mewcode-lpac-staged-request-v1', 'read_paths': [], 'write_paths': []}}
        kwargs['request_transform'](envelope)
        cancel = object()
        seen['observed_callback'] = kwargs['runner']
        seen['result'] = await kwargs['runner'](envelope, cancel, staged_observed.append)
        seen['cancel_expected'] = cancel
        return seen['result']

    monkeypatch.setattr(srt_acceptance, 'invoke_srt', invoke_srt)
    monkeypatch.setattr(srt_acceptance, 'execute_staged', execute_staged)
    checker = SimpleNamespace(network_policy=NetworkPolicy())
    result = await srt_acceptance._execute_acceptance_staged('synthetic', checker=checker,
        helper=tmp_path / 'offline-helper.exe', scope=object(), registry=object(), timeout=5,
        run=tmp_path, hosts=['127.0.0.1:60000'], identity='stable-runtime',
        observed_receipts=observed_receipts)

    execution = seen['execution']
    assert execution['extra_read'] == ()
    assert execution['request_transform'] is lpac_network.transform
    assert execution['runtime_git_dir'] == git_dir
    assert seen['envelope']['request']['schema'] == lpac_network.NET_SCHEMA
    assert seen['envelope']['request']['runtime_network'] is True
    assert seen['envelope']['request']['read_paths'] == [str(probe)]
    assert seen['envelope']['request']['write_paths'] == []
    assert seen['cancel'] is seen['cancel_expected']
    assert seen['acceptance'] is True
    assert seen['test_hosts'] == ['127.0.0.1:60000']
    assert result.network_policy == checker.network_policy.as_dict()
    assert observed_receipts == [result]
    assert staged_observed == [result]


@pytest.mark.asyncio
async def test_disabled_policy_uses_legacy_offline_helper_and_keeps_host_policy(monkeypatch, tmp_path):
    monkeypatch.setattr(srt_acceptance, 'runtime_identity', lambda: 'stable-runtime')
    observed_receipts = []
    staged_observed = []
    seen = {}
    blocked = _receipt(stderr='PermissionError: [WinError 10013] access to socket denied')

    async def invoke_helper(helper, envelope, cancel, *, on_receipt):
        seen.update(helper=helper, envelope=envelope, cancel=cancel)
        on_receipt(blocked)
        return blocked

    async def execute_staged(script, **kwargs):
        seen['execution'] = kwargs
        envelope = {'request': {'schema': 'mewcode-lpac-staged-request-v1'}}
        assert kwargs['request_transform'] is None
        assert kwargs['extra_read'] == ()
        assert kwargs['runtime_git_dir'] is None
        cancel = object()
        result = await kwargs['runner'](envelope, cancel, staged_observed.append)
        seen['cancel_expected'] = cancel
        return result

    monkeypatch.setattr(srt_acceptance.lpac, 'invoke_helper', invoke_helper)
    monkeypatch.setattr(srt_acceptance, 'execute_staged', execute_staged)
    policy = NetworkPolicy(False)
    checker = SimpleNamespace(network_policy=policy)
    helper = tmp_path / 'legacy-offline-helper.exe'
    result = await srt_acceptance._execute_acceptance_staged('raw socket probe', checker=checker,
        helper=helper, scope=object(), registry=object(), timeout=5, run=tmp_path,
        hosts=['127.0.0.1:60000'], identity='stable-runtime', observed_receipts=observed_receipts)

    assert seen['helper'] == helper
    assert seen['cancel'] is seen['cancel_expected']
    assert seen['envelope']['request'].get('runtime_network') is None
    assert seen['envelope']['request'].get('schema') == 'mewcode-lpac-staged-request-v1'
    assert 'filesystem.json' not in {path.name for path in tmp_path.iterdir()}
    assert result.network_policy == policy.as_dict()
    assert observed_receipts == [result]
    assert staged_observed == [result]
    assert srt_acceptance._network_disabled_pass(result, policy)


def test_network_disabled_acceptance_requires_real_socket_denial_and_clean_receipt():
    policy = NetworkPolicy(False)
    valid = _receipt(stderr='PermissionError: [WinError 10013]', policy=policy.as_dict())
    assert srt_acceptance._network_disabled_pass(valid, policy)
    assert not srt_acceptance._network_disabled_pass(
        _receipt(stderr='NETWORK_DISABLED', policy=policy.as_dict()), policy)
    assert not srt_acceptance._network_disabled_pass(
        _receipt(stderr='[WinError 10013]', policy=NetworkPolicy().as_dict()), policy)
    assert not srt_acceptance._network_disabled_pass(
        _receipt(stderr='[WinError 10013]', policy=policy.as_dict(), cleanup=False), policy)


def test_same_sid_roundtrip_requires_cleanup_for_each_run():
    receipts = [_receipt(status='exited', cleanup=True) for _ in range(3)]
    assert srt_acceptance._same_sid_clean_rounds('S-1-15-2-fixed',
        ('S-1-15-2-fixed',) * 3, receipts)
    assert not srt_acceptance._same_sid_clean_rounds('S-1-15-2-fixed',
        ('S-1-15-2-fixed', 'S-1-15-2-other', 'S-1-15-2-fixed'), receipts)
    assert not srt_acceptance._same_sid_clean_rounds('S-1-15-2-fixed',
        ('S-1-15-2-fixed',) * 3, [*receipts[:2], _receipt(status='exited', cleanup=False)])


def test_descendant_probes_cover_ipv4_ipv6_tcp_and_udp_denials():
    assert srt_acceptance.DIRECT_SOCKET_PROBES == (
        'ipv4_tcp', 'ipv6_tcp', 'ipv4_udp', 'ipv6_udp')
    denied = [NativeExecutionReceipt('exited', exit_code=1, stderr='PermissionError: [WinError 10013]',
        cleanup_confirmed=True) for _ in range(2)]
    udp = [NativeExecutionReceipt('exited', exit_code=0, stdout=f'UDP_PROBE_ATTEMPTED:nonce-{i}:3\n',
        cleanup_confirmed=True) for i in range(2)]
    observed = [{'marker': f'UDP_PROBE_ATTEMPTED:nonce-{i}:3', 'before_control': True,
        'after_control': True, 'received_probe_count': 0, 'receiver_error': None} for i in range(2)]
    receipts = denied + udp
    assert srt_acceptance._direct_socket_denials_pass(receipts, ipv6_available=True, udp_observations=observed)
    assert not srt_acceptance._direct_socket_denials_pass(receipts[:3], ipv6_available=True, udp_observations=observed)
    assert not srt_acceptance._direct_socket_denials_pass(receipts, ipv6_available=False, udp_observations=observed)
    assert not srt_acceptance._direct_socket_denials_pass(
        [NativeExecutionReceipt('exited', exit_code=1, stderr='Connection refused', cleanup_confirmed=True),
         *receipts[1:]], ipv6_available=True, udp_observations=observed)
    # Returning success alone is never evidence that UDP was confined.
    assert not srt_acceptance._direct_socket_denials_pass(receipts, ipv6_available=True)
    for changes in ({'received_probe_count': 1}, {'before_control': False}, {'after_control': False},
                    {'receiver_error': 'OSError'}, {'marker': 'missing execution marker'}):
        failed = [dict(observed[0], **changes), observed[1]]
        assert not srt_acceptance._direct_socket_denials_pass(receipts, ipv6_available=True, udp_observations=failed)


def test_udp_receiver_controls_prove_delivery_and_capture_probe_packet():
    import socket
    receiver = srt_acceptance._UDPReceiver(socket.AF_INET)
    try:
        assert receiver.control()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(b'SYNTHETIC_PROBE_PACKET', receiver.address)
        assert receiver.control()
        assert receiver.received.count(b'SYNTHETIC_PROBE_PACKET') == 1
        assert receiver.error is None
    finally:
        receiver.close()
    assert not receiver.thread.is_alive()


def test_file_boundary_requires_a_completed_real_denial():
    valid = NativeExecutionReceipt('exited', exit_code=1, cleanup_confirmed=True,
        stderr='UnauthorizedAccessException: access is denied')
    assert srt_acceptance._file_boundary_denied_pass(valid)
    assert not srt_acceptance._file_boundary_denied_pass(_receipt(stderr='Permission denied'))
    assert not srt_acceptance._file_boundary_denied_pass(NativeExecutionReceipt(
        'exited', exit_code=1, cleanup_confirmed=False, stderr='Permission denied'))
    assert not srt_acceptance._file_boundary_denied_pass(NativeExecutionReceipt(
        'exited', exit_code=1, cleanup_confirmed=True, stderr='SyntaxError'))
    assert not srt_acceptance._file_boundary_denied_pass(NativeExecutionReceipt(
        'exited', exit_code=1, cleanup_confirmed=True, stderr='Permission denied', stdout='SECRET'),
        forbidden_output='SECRET')


def test_revalidation_retires_old_success_before_work_and_keeps_exception_record(monkeypatch, tmp_path):
    class Lock:
        def acquire(self): return True
        def close(self): pass
    monkeypatch.setattr(srt_runtime, '_RunnerLock', lambda *_: Lock())
    monkeypatch.setattr(srt_runtime, '_network_shared_root', lambda: tmp_path / 'shared')
    monkeypatch.setattr(srt_acceptance, 'user_data_root', lambda: tmp_path)
    monkeypatch.setattr(srt_acceptance, 'runtime_identity', lambda: 'stable-runtime')
    path = tmp_path / 'srt-network-last-check.json'
    path.write_text(json.dumps({'check_id': 'old-success', 'cases': dict.fromkeys(srt_runtime.REQUIRED_CASES, True)}))

    def interrupted(root, *, publish, record):
        started = json.loads(path.read_text(encoding='utf-8'))
        assert started['check_id'] != 'old-success'
        assert all(value is None for value in started['cases'].values())
        record['cases']['http_allowed'] = True
        raise OSError('synthetic environment unavailable')
    monkeypatch.setattr(srt_acceptance, '_verify_runtime', interrupted)

    result = srt_acceptance.verify_runtime(tmp_path / 'synthetic', publish=True)

    saved = json.loads(path.read_text(encoding='utf-8'))
    assert not result['ready']
    assert saved['cases']['http_allowed'] is True
    assert all(saved['cases'][key] is None for key in srt_runtime.REQUIRED_CASES[1:])
    assert saved['failure']['error_code'] == 'OSError'
    assert 'synthetic environment unavailable' in saved['failure']['reason']
