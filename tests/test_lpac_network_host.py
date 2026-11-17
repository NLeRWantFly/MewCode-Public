from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from mewcode.sandbox import lpac_network as network
from mewcode.sandbox import lpac_runtime


REQUEST_ID = '0123456789abcdef0123456789abcdef'
USER_SID = 'S-1-5-21-101-202-303-1001'
PROFILE_SID = 'S-1-15-2-10-20-30-40'


def _bundle(root: Path, *, outputs: dict[str, bytes] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    outputs = outputs or {name: ('fixture:' + name).encode() for name in network.FILES}
    for name, content in outputs.items():
        (root / name).write_bytes(content)
    manifest = {
        'schema': network.SCHEMA,
        'sources': network.source_identity(),
        'files': {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                  for name in network.FILES},
    }
    (root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return root


@pytest.fixture
def installed_bundle(tmp_path, monkeypatch):
    data_root = tmp_path / 'userdata'
    bundle = _bundle(data_root / 'native' / network.DIRECTORY)
    monkeypatch.setattr(network, 'user_data_root', lambda: data_root)
    return bundle


def test_network_transform_grants_only_verified_probe_leaf(installed_bundle):
    helper, probe = network.paths()
    request = {'schema': 'mewcode-lpac-request-v1', 'read_paths': ['workspace/input.txt'],
               'write_paths': ['workspace/output.txt']}
    envelope = {'request': request}
    network.transform(envelope)
    network.transform(envelope)
    assert request['read_paths'] == sorted(['workspace/input.txt', str(probe)])
    assert str(probe.parent) not in request['read_paths']
    assert str(helper) not in request['read_paths']
    assert request['write_paths'] == ['workspace/output.txt']
    assert request['runtime_network'] is True and request['schema'] == network.NET_SCHEMA


def test_network_transform_rejects_invalid_bundle_before_adding_grants(installed_bundle):
    (installed_bundle / network.FILES[1]).write_bytes(b'changed')
    envelope = {'request': {'read_paths': [], 'write_paths': []}}
    with pytest.raises(RuntimeError, match='NETWORK_RUNTIME_UNAVAILABLE'):
        network.transform(envelope)
    assert envelope['request'] == {'read_paths': [], 'write_paths': []}


def test_paths_verify_fixed_manifest_source_and_artifact_hashes(installed_bundle, monkeypatch):
    helper, probe = network.paths()
    assert helper.name == network.FILES[0]
    assert probe.name == network.FILES[1]

    manifest_path = installed_bundle / 'manifest.json'
    original = manifest_path.read_text(encoding='utf-8')
    manifest = json.loads(original)
    manifest['sources'][network.SOURCE_NAMES[0]] = '0' * 64
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(RuntimeError, match='NETWORK_RUNTIME_UNAVAILABLE'):
        network.paths()

    manifest_path.write_text(original, encoding='utf-8')
    current = network.source_identity()
    changed = dict(current)
    changed[network.SOURCE_NAMES[0]] = 'f' * 64
    monkeypatch.setattr(network, 'source_identity', lambda: changed)
    with pytest.raises(RuntimeError, match='NETWORK_RUNTIME_UNAVAILABLE'):
        network.paths()
    monkeypatch.setattr(network, 'source_identity', lambda: current)

    (installed_bundle / network.FILES[0]).write_bytes(b'drifted executable')
    with pytest.raises(RuntimeError, match='NETWORK_RUNTIME_UNAVAILABLE'):
        network.paths()


def test_paths_reject_unknown_files_and_reparse_checked_paths(installed_bundle, monkeypatch):
    extra = installed_bundle / 'unexpected.dll'
    extra.write_bytes(b'not allowed')
    with pytest.raises(RuntimeError, match='NETWORK_RUNTIME_UNAVAILABLE'):
        network.paths()
    extra.unlink()

    checked = network.checked_path
    target = installed_bundle / network.FILES[0]

    def reject_reparse(path, *, file=False):
        if Path(path) == target:
            raise RuntimeError('simulated reparse point')
        return checked(path, file=file)

    monkeypatch.setattr(network, 'checked_path', reject_reparse)
    with pytest.raises(RuntimeError, match='NETWORK_RUNTIME_UNAVAILABLE'):
        network.paths()


@pytest.mark.parametrize('value', [
    {'schema': network.PROFILE_SCHEMA, 'profile_name': 'wrong', 'profile_sid': PROFILE_SID, 'user_sid': USER_SID},
    {'schema': network.PROFILE_SCHEMA, 'profile_name': 'mewcode-network-v1-0123456789abcdef',
     'profile_sid': 'S-1-15-3-100', 'user_sid': USER_SID},
    {'schema': network.PROFILE_SCHEMA, 'profile_name': 'mewcode-network-v1-0123456789abcdef',
     'profile_sid': PROFILE_SID, 'user_sid': 'not-a-sid'},
])
def test_profile_rejects_invalid_native_identity(installed_bundle, monkeypatch, value):
    del installed_bundle
    monkeypatch.setattr(network.subprocess, 'run', lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=json.dumps(value).encode(), stderr=b''))
    with pytest.raises(RuntimeError, match='NETWORK_RUNTIME_UNAVAILABLE'):
        network.profile()


def test_profile_returns_typed_identity_and_uses_explicit_operation(installed_bundle, monkeypatch):
    del installed_bundle
    name = network._profile_name(USER_SID)
    payload = json.dumps({'schema': network.PROFILE_SCHEMA, 'profile_name': name,
                          'profile_sid': PROFILE_SID, 'user_sid': USER_SID}).encode()
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=payload, stderr=b'')

    monkeypatch.setattr(network.subprocess, 'run', run)
    identity = network.profile()
    assert isinstance(identity, network.NetworkProfileIdentity)
    assert identity.profile_name == name
    assert identity.profile_sid == PROFILE_SID
    assert identity.user_sid == USER_SID
    assert identity['profile_sid'] == PROFILE_SID
    assert calls[-1][-1] == '--network-profile-status-v1'

    provisioned = network.profile(provision=True)
    assert provisioned == identity
    assert calls[-1][-1] == '--provision-network-profile-v1'


def _receipt(*, capabilities=None, network_fields=None, cleanup=True, exit_code=0):
    return {
        'schema': 'mewcode-lpac-receipt-v1',
        'backend': lpac_runtime.BACKEND,
        'request_id': REQUEST_ID,
        'receipt': {
            'exit_code': exit_code,
            'stdout': 'completed\n',
            'stderr': '',
            'error': '',
            'cancelled': False,
            'timed_out': False,
            'job_cleanup_ok': cleanup,
            'acl_cleanup_ok': cleanup,
            'profile_cleanup_ok': cleanup,
            'appcontainer_verified': True,
            'lpac_verified': True,
            'lpac_verification_method': 'token_query',
            'capabilities': list(capabilities or [*lpac_runtime.CAPABILITIES, 'internetClient']),
            'network': network_fields or {
                'runtime_network': True,
                'persistent_profile': False,
                'profile_name': network._profile_name(USER_SID),
                'profile_sid': PROFILE_SID,
                'user_sid': USER_SID,
            },
        },
    }


def _decode(value, *, expected_sid=PROFILE_SID, helper_exit=0):
    return network.decode(json.dumps(value), REQUEST_ID, helper_exit, expected_sid=expected_sid)


def test_network_decode_accepts_only_the_exact_network_identity_and_capabilities():
    receipt = _decode(_receipt())
    assert receipt.status == 'exited'
    assert receipt.backend == 'mewcode-windows-srt'
    assert receipt.cleanup_confirmed is True

    mutations = [
        {'runtime_network': False},
        {'persistent_profile': True},
        {'profile_sid': 'S-1-15-2-10-20-30-41'},
        {'profile_name': 'mewcode-network-v1-0000000000000000'},
        {'user_sid': 'not-a-sid'},
    ]
    for changes in mutations:
        value = _receipt()
        value['receipt']['network'].update(changes)
        assert _decode(value).status == 'spawn_failed'

    for caps in ([*lpac_runtime.CAPABILITIES], [*lpac_runtime.CAPABILITIES, 'internetClient', 'internetClient'],
                 [*lpac_runtime.CAPABILITIES, 'internetClient', 'other']):
        assert _decode(_receipt(capabilities=caps)).status == 'spawn_failed'

    assert _decode(_receipt(), expected_sid='S-1-15-2-10-20').status == 'spawn_failed'


def test_network_decode_retains_legacy_cleanup_exit_and_offline_checks():
    dirty = _decode(_receipt(cleanup=False))
    assert dirty.status == 'spawn_failed'
    assert dirty.cleanup_confirmed is False

    bad_exit = _decode(_receipt(exit_code=4), helper_exit=0)
    assert bad_exit.status == 'spawn_failed'

    # The legacy offline validator still accepts only its original capability set.
    raw = json.dumps(_receipt()).encode()
    offline = lpac_runtime.decode_receipt(raw, REQUEST_ID, 0)
    assert offline.status == 'spawn_failed'


def _prepare_installer(tmp_path, monkeypatch):
    data_root = tmp_path / 'userdata'
    native_root = data_root / 'native'
    native_root.mkdir(parents=True)
    monkeypatch.setattr(network, 'user_data_root', lambda: data_root)
    system_root = tmp_path / 'windows'
    compiler = system_root / 'Microsoft.NET' / 'Framework64' / 'v4.0.30319' / 'csc.exe'
    compiler.parent.mkdir(parents=True)
    compiler.write_bytes(b'compiler stub')
    monkeypatch.setattr(network, '_windows_system_root', lambda: system_root)

    def compile_stub(args, **kwargs):
        output = Path(next(value[len('/out:'):] for value in args if value.startswith('/out:')))
        output.write_bytes(b'compiled:' + output.name.encode())
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

    monkeypatch.setattr(network.subprocess, 'run', compile_stub)
    return data_root, native_root


def test_install_builds_and_verifies_only_the_fixed_bundle(tmp_path, monkeypatch):
    _, native_root = _prepare_installer(tmp_path, monkeypatch)
    helper, probe = network.install()
    assert (helper, probe) == tuple((native_root / network.DIRECTORY / name).resolve() for name in network.FILES)
    assert network.paths() == (helper, probe)
    assert {item.name for item in (native_root / network.DIRECTORY).iterdir()} == set(network.FILES) | {'manifest.json'}


def test_system_compiler_validation_allows_fixed_framework_hardlink(tmp_path, monkeypatch):
    system_root = tmp_path / 'windows'
    compiler = system_root / 'Microsoft.NET' / 'Framework64' / 'v4.0.30319' / 'csc.exe'
    compiler.parent.mkdir(parents=True)
    compiler.write_bytes(b'fixed system compiler')
    hardlink = tmp_path / 'csc-hardlink.exe'
    try:
        os.link(compiler, hardlink)
    except OSError as exc:
        pytest.skip(f'filesystem does not support hardlinks: {exc}')
    assert compiler.stat().st_nlink > 1
    monkeypatch.setattr(network, '_windows_system_root', lambda: system_root)
    assert network._checked_framework_compiler(network._windows_system_root()) == compiler.resolve()

    invalid_root = tmp_path / 'invalid-windows'
    directory_instead_of_file = invalid_root / 'Microsoft.NET' / 'Framework64' / 'v4.0.30319' / 'csc.exe'
    directory_instead_of_file.mkdir(parents=True)
    with pytest.raises(ValueError, match='regular file'):
        network._checked_framework_compiler(invalid_root)


def test_install_rename_failure_restores_previous_bundle_bytes(tmp_path, monkeypatch):
    _, native_root = _prepare_installer(tmp_path, monkeypatch)
    target = _bundle(native_root / network.DIRECTORY,
                     outputs={name: ('old:' + name).encode() for name in network.FILES})
    original = {path.name: path.read_bytes() for path in target.iterdir()}
    real_rename = Path.rename

    def fail_staged_install(self, destination):
        if self.name.startswith('.network-build-') and Path(destination).name == network.DIRECTORY:
            raise OSError('injected second rename failure')
        return real_rename(self, destination)

    monkeypatch.setattr(Path, 'rename', fail_staged_install)
    with pytest.raises(RuntimeError, match='NETWORK_RUNTIME_UNAVAILABLE'):
        network.install()

    assert {path.name: path.read_bytes() for path in target.iterdir()} == original
    assert not list(native_root.glob(network.DIRECTORY + '-previous-*'))
