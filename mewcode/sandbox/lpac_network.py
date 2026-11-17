"""Fixed host-only network LPAC bundle; legacy offline bundle stays untouched."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import uuid

from mewcode.runtime_paths import user_data_root
from mewcode.sandbox.lpac_runtime import CAPABILITIES, MAX_RECEIPT, checked_path, decode_receipt
from mewcode.sandbox.receipt import NativeExecutionReceipt

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = 'windows-lpac-network-v1'
SCHEMA = 'mewcode-lpac-network-bundle-v1'
NET_SCHEMA = 'mewcode-lpac-network-request-v1'
PROFILE_SCHEMA = 'mewcode-lpac-network-profile-v1'
FILES = ('MewCode.AppContainerRuntime.exe', 'MewCode.NetworkProbe.exe')
SOURCE_NAMES = ('MewCode.AppContainerProbe.cs', 'MewCode.AppContainerRuntime.cs',
                'MewCode.StagedWorkspacePrototype.cs', 'MewCode.NetworkProbe.cs')
_MANIFEST_NAME = 'manifest.json'
_MANIFEST_MAX = 16 * 1024
_PROFILE_OUTPUT_MAX = 16 * 1024
_SHA256_RE = re.compile(r'[0-9a-f]{64}\Z')
_SID_NUMBER = r'(?:0|[1-9][0-9]{0,9})'
_USER_SID_RE = re.compile(rf'S-1-(?:0|[1-9][0-9]{{0,14}})-{_SID_NUMBER}(?:-{_SID_NUMBER}){{0,14}}\Z')
_APP_CONTAINER_SID_RE = re.compile(rf'S-1-15-2-{_SID_NUMBER}(?:-{_SID_NUMBER}){{0,14}}\Z')


@dataclass(frozen=True)
class NetworkProfileIdentity:
    """Validated identity returned by the pinned native profile command."""

    profile_name: str
    profile_sid: str
    user_sid: str

    def __getitem__(self, key: str) -> str:
        # Keep existing runtime call sites source-compatible while making the
        # result typed and immutable for new callers.
        if key not in {'profile_name', 'profile_sid', 'user_sid'}:
            raise KeyError(key)
        return getattr(self, key)


def _unavailable(message: str, exc: BaseException | None = None) -> RuntimeError:
    error = RuntimeError('NETWORK_RUNTIME_UNAVAILABLE: ' + message)
    if exc is not None:
        error.__cause__ = exc
    return error


def _json_no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _read_json(raw: bytes | str, *, limit: int) -> object:
    if isinstance(raw, bytes):
        if not raw or len(raw) > limit:
            raise ValueError('missing or oversized JSON')
        text = raw.decode('utf-8-sig')
    elif isinstance(raw, str):
        encoded = raw.encode('utf-8')
        if not encoded or len(encoded) > limit:
            raise ValueError('missing or oversized JSON')
        text = raw
    else:
        raise ValueError('invalid JSON input type')
    return json.loads(text, object_pairs_hook=_json_no_duplicates)


def _digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _valid_user_sid(value: object) -> bool:
    return isinstance(value, str) and _USER_SID_RE.fullmatch(value) is not None


def _valid_profile_sid(value: object) -> bool:
    return isinstance(value, str) and _APP_CONTAINER_SID_RE.fullmatch(value) is not None


def _profile_name(user_sid: str) -> str:
    if not _valid_user_sid(user_sid):
        raise ValueError('invalid user SID')
    suffix = hashlib.sha256(user_sid.encode('ascii')).hexdigest()[:16]
    return 'mewcode-network-v1-' + suffix


def sources() -> tuple[Path, ...]:
    return tuple(ROOT / 'native' / 'windows' / name for name in SOURCE_NAMES)


def source_identity() -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sources():
        if path.name in result:
            raise ValueError('duplicate network bundle source name')
        safe = checked_path(path, file=True)
        result[path.name] = hashlib.sha256(safe.read_bytes()).hexdigest()
    if set(result) != set(SOURCE_NAMES):
        raise ValueError('network bundle source set mismatch')
    return result


def _check_digest_map(value: object, names: tuple[str, ...]) -> bool:
    return (type(value) is dict and set(value) == set(names)
            and all(_digest(value[name]) for name in names))


def _verify_bundle(root: Path, expected_sources: dict[str, str] | None = None) -> tuple[Path, ...]:
    """Verify the fixed bundle shape and every source/artifact digest."""
    try:
        directory = checked_path(root)
        if not directory.is_dir():
            raise ValueError('bundle root is not a directory')
        entries = list(directory.iterdir())
        expected_names = set(FILES) | {_MANIFEST_NAME}
        if {entry.name for entry in entries} != expected_names:
            raise ValueError('bundle directory has missing or unknown files')
        safe_entries = {entry.name: checked_path(entry, file=True) for entry in entries}
        manifest_path = safe_entries[_MANIFEST_NAME]
        if manifest_path.stat().st_size > _MANIFEST_MAX:
            raise ValueError('bundle manifest exceeds bound')
        manifest = _read_json(manifest_path.read_bytes(), limit=_MANIFEST_MAX)
        if (type(manifest) is not dict or set(manifest) != {'schema', 'sources', 'files'}
                or manifest.get('schema') != SCHEMA):
            raise ValueError('bundle manifest shape or schema mismatch')
        current_sources = expected_sources if expected_sources is not None else source_identity()
        if (not _check_digest_map(current_sources, SOURCE_NAMES)
                or not _check_digest_map(manifest.get('sources'), SOURCE_NAMES)
                or manifest['sources'] != current_sources):
            raise ValueError('bundle source identity mismatch')
        if not _check_digest_map(manifest.get('files'), FILES):
            raise ValueError('bundle file hash map is malformed')
        result = []
        for name in FILES:
            path = safe_entries[name]
            if hashlib.sha256(path.read_bytes()).hexdigest() != manifest['files'][name]:
                raise ValueError('bundle artifact hash mismatch')
            result.append(path)
        return tuple(result)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        raise _unavailable('联网 LPAC 组件校验失败。', exc) from exc


def paths() -> tuple[Path, Path]:
    """Return only verified installed artifacts; never resolve from PATH."""
    try:
        root = user_data_root() / 'native' / DIRECTORY
        result = _verify_bundle(root)
        return result[0], result[1]
    except RuntimeError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise _unavailable('联网 LPAC 组件需要重新配置。', exc) from exc


def _lexists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _validate_replaceable_target(path: Path) -> None:
    """Reject reparse points and unexpected names before preserving an old bundle."""
    directory = checked_path(path)
    if not directory.is_dir():
        raise ValueError('installed network bundle is not a directory')
    allowed = set(FILES) | {_MANIFEST_NAME}
    entries = list(directory.iterdir())
    if any(entry.name not in allowed for entry in entries):
        raise ValueError('installed network bundle contains unknown files')
    for entry in entries:
        checked_path(entry, file=True)


def _cleanup_stage(path: Path | None) -> None:
    """Remove only known ordinary staging files; preserve anything unexpected."""
    if path is None or not _lexists(path):
        return
    try:
        directory = checked_path(path)
        if not directory.is_dir():
            return
        entries = list(directory.iterdir())
        if any(entry.name not in set(FILES) | {_MANIFEST_NAME} for entry in entries):
            return
        safe = [checked_path(entry, file=True) for entry in entries]
        for entry in safe:
            entry.unlink()
        directory.rmdir()
    except (OSError, RuntimeError, ValueError):
        # A nonconforming staging directory is left intact for inspection.
        return


def _rollback_install(target: Path, backup: Path | None, root: Path) -> None:
    if backup is None or not _lexists(backup):
        return
    if _lexists(target):
        failed = root / (DIRECTORY + '-failed-' + uuid.uuid4().hex)
        target.rename(failed)
    backup.rename(target)


def _windows_system_root() -> Path:
    """Read the machine Windows directory from the OS, not task/user input."""
    if os.name != 'nt':
        raise RuntimeError('Windows native compiler is available only on Windows')
    import ctypes
    from ctypes import wintypes
    buffer = ctypes.create_unicode_buffer(32768)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    get_directory = kernel.GetWindowsDirectoryW
    get_directory.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    get_directory.restype = wintypes.UINT
    length = get_directory(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise OSError(ctypes.get_last_error(), 'GetWindowsDirectoryW failed')
    return Path(buffer.value)


def _checked_framework_compiler(system_root: Path) -> Path:
    """Check the fixed framework compiler path while allowing system hardlinks.

    csc.exe is commonly a WinSxS hardlink. This narrow check still rejects
    reparse points in every ancestor and the leaf, and requires a regular file;
    installed bundles and all runtime scopes continue using checked_path's
    single-link rule.
    """
    root = Path(system_root)
    if not root.is_absolute() or '..' in root.parts:
        raise ValueError('Windows system directory is not a fixed absolute path')
    compiler = root / 'Microsoft.NET' / 'Framework64' / 'v4.0.30319' / 'csc.exe'
    for part in (*reversed(compiler.parents), compiler):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Windows framework compiler path contains a reparse point')
    if not stat.S_ISREG(compiler.stat().st_mode):
        raise ValueError('Windows framework compiler is not a regular file')
    return compiler.resolve(strict=True)


def install() -> tuple[Path, Path]:
    """Explicit setup only; compile a separate bundle and preserve old installs."""
    native_root: Path | None = None
    build: Path | None = None
    target: Path | None = None
    backup: Path | None = None
    old_moved = False
    try:
        native_root = user_data_root() / 'native'
        native_root.mkdir(parents=True, exist_ok=True)
        native_root = checked_path(native_root)
        if not native_root.is_dir():
            raise ValueError('native install root is not a directory')
        build = native_root / ('.network-build-' + uuid.uuid4().hex)
        build.mkdir()
        build = checked_path(build)
        expected_sources = source_identity()
        source_paths = tuple(checked_path(path, file=True) for path in sources())
        compiler = _checked_framework_compiler(_windows_system_root())
        groups = ((FILES[0], source_paths[:3], 'MewCode.Native.Windows.MewCodeAppContainerRuntime'),
                  (FILES[1], source_paths[3:], 'MewCode.Native.Windows.NetworkProbe'))
        for name, inputs, entry in groups:
            command = [str(compiler), '/nologo', '/platform:x64', '/r:System.Web.Extensions.dll',
                       '/main:' + entry, '/out:' + str(build / name), *map(str, inputs)]
            result = subprocess.run(command, capture_output=True, timeout=60,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                output = (result.stdout or b'') + b'\n' + (result.stderr or b'')
                raise RuntimeError('compiler failed: ' + output.decode(errors='replace')[:4000])
        if source_identity() != expected_sources:
            raise ValueError('network bundle sources changed while compiling')
        manifest = {'schema': SCHEMA, 'sources': expected_sources,
                    'files': {name: hashlib.sha256((build / name).read_bytes()).hexdigest()
                              for name in FILES}}
        (build / _MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=True, sort_keys=True, separators=(',', ':')),
            encoding='utf-8')
        _verify_bundle(build, expected_sources)

        target = native_root / DIRECTORY
        if _lexists(target):
            _validate_replaceable_target(target)
            backup = native_root / (DIRECTORY + '-previous-' + uuid.uuid4().hex)
            target.rename(backup)
            old_moved = True
        try:
            build.rename(target)
            build = None
        except BaseException:
            if old_moved:
                _rollback_install(target, backup, native_root)
                old_moved = False
            raise
        try:
            result = _verify_bundle(target, expected_sources)
        except BaseException:
            if old_moved:
                _rollback_install(target, backup, native_root)
                old_moved = False
            else:
                failed = native_root / (DIRECTORY + '-failed-' + uuid.uuid4().hex)
                target.rename(failed)
            raise
        return result[0], result[1]
    except BaseException as exc:
        if old_moved and target is not None and native_root is not None:
            try:
                _rollback_install(target, backup, native_root)
                old_moved = False
            except BaseException as rollback_error:
                _cleanup_stage(build)
                raise _unavailable('安装失败，旧版仍保存在 ' + str(backup) + '。', rollback_error) from exc
        _cleanup_stage(build)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(exc, RuntimeError) and str(exc).startswith('NETWORK_RUNTIME_UNAVAILABLE:'):
            raise
        raise _unavailable('联网 LPAC 组件编译或安装失败。', exc) from exc


def profile(*, provision: bool = False) -> NetworkProfileIdentity:
    """Read or provision the fixed host-user profile through the trusted helper."""
    if type(provision) is not bool:
        raise _unavailable('联网 profile 操作无效。')
    helper, _ = paths()
    operation = '--provision-network-profile-v1' if provision else '--network-profile-status-v1'
    try:
        result = subprocess.run([str(helper), operation], capture_output=True, timeout=30,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise ValueError('profile helper returned an error')
        raw = result.stdout
        if not isinstance(raw, (bytes, str)):
            raise ValueError('profile helper output has an invalid type')
        value = _read_json(raw, limit=_PROFILE_OUTPUT_MAX)
        expected_keys = {'schema', 'profile_name', 'profile_sid', 'user_sid'}
        if type(value) is not dict or set(value) != expected_keys:
            raise ValueError('profile identity shape mismatch')
        user_sid = value['user_sid']
        profile_sid = value['profile_sid']
        if (value['schema'] != PROFILE_SCHEMA or not _valid_user_sid(user_sid)
                or not _valid_profile_sid(profile_sid)
                or value['profile_name'] != _profile_name(user_sid)):
            raise ValueError('profile identity validation failed')
        return NetworkProfileIdentity(value['profile_name'], profile_sid, user_sid)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise _unavailable('联网 profile 身份未确认。', exc) from exc


def transform(envelope: dict) -> None:
    """Freeze network mode and grant only the verified host probe leaf.

    extra_read is a directory interface; passing the probe executable there
    rejects preparation. The host-owned probe needs one exact read/execute
    leaf, never access to its containing bundle or sibling helper.
    """
    _, probe = paths()
    request = envelope['request']
    reads = request.get('read_paths')
    if not isinstance(reads, list) or any(not isinstance(path, str) for path in reads):
        raise _unavailable('冻结请求缺少有效只读路径清单。')
    request['read_paths'] = sorted(set([*reads, str(probe)]))
    request['schema'] = NET_SCHEMA
    request['runtime_network'] = True


def decode(raw: bytes | str, request_id: str, helper_exit: int | None, *,
           expected_sid: str, source_sha256: str | None = None) -> NativeExecutionReceipt:
    """Validate network identity, then reuse legacy cleanup/exit/output checks."""
    def rejected(reason: str, cleanup: bool = False) -> NativeExecutionReceipt:
        return NativeExecutionReceipt('spawn_failed', backend='mewcode-windows-srt', cleanup_confirmed=cleanup,
            error_code='native_receipt_unverified', command_started=None, detail=reason)

    if not _valid_profile_sid(expected_sid):
        return rejected('Network LPAC expected identity is invalid')
    try:
        value = _read_json(raw, limit=MAX_RECEIPT)
        if type(value) is not dict:
            return rejected('Network LPAC receipt malformed')
        receipt = value.get('receipt')
        if type(receipt) is not dict:
            return rejected('Network LPAC receipt malformed')
        net = receipt.get('network')
        if type(net) is not dict:
            return rejected('Network LPAC identity/capability mismatch')
        user_sid = net.get('user_sid')
        if (value.get('request_id') != request_id
                or net.get('runtime_network') is not True
                or net.get('persistent_profile') is not False
                or net.get('profile_sid') != expected_sid
                or not _valid_profile_sid(net.get('profile_sid'))
                or not _valid_user_sid(user_sid)
                or net.get('profile_name') != _profile_name(user_sid)
                or type(receipt.get('capabilities')) is not list
                or receipt.get('capabilities') != [*CAPABILITIES, 'internetClient']):
            return rejected('Network LPAC identity/capability mismatch')
        # Preserve the old decoder as authority over all shared identity,
        # token, cancellation, cleanup, exit-code and output invariants.
        receipt['capabilities'] = list(CAPABILITIES)
        old = decode_receipt(json.dumps(value, ensure_ascii=True).encode('utf-8'), request_id, helper_exit)
        from dataclasses import replace
        diagnostic = receipt.get('error')
        detail = old.detail
        if old.status == 'spawn_failed' and isinstance(diagnostic, str) and diagnostic.strip():
            detail += ' Native diagnostic: ' + diagnostic.strip()[:1600]
        return replace(old, backend='mewcode-windows-srt', detail=detail)
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        return rejected('Network LPAC receipt malformed')
