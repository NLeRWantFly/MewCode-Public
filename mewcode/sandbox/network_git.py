"""Install and validate the independent, pinned network Git runtime bundle.

The base LPAC bundle remains immutable.  This package copies its verified Git
executable and fixed runtime DLLs, then adds only the pinned HTTP(S) helper
closure.  Setup is host-only; it does not run Git or access the network.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from mewcode.runtime_paths import user_data_root
from mewcode.sandbox import lpac_runtime

ROOT = Path(__file__).resolve().parents[2]
SDK_BUILD = ".tmp/git-auto-compat-20260926/sdk/mingw64"
SDK_HELPER_SOURCE = ".tmp/git-auto-compat-20260926/patched/git-remote-http.exe"
SDK_HELPER_SHA256 = "670dcd991d43d489ee1ef110139f74c4a4de44a3ca7ae91b3af44b0bbcb9619c"
HELPER_NAMES = ("git-remote-http.exe", "git-remote-https.exe")
DLL_NAMES = ("libiconv-2.dll", "libintl-8.dll", "libpcre2-8-0.dll", "zlib1.dll")
BASE_DLL_SHA256 = {
    "libiconv-2.dll": "7a282a854e01be726c6cccfe46f548c716aa45b3014818468253aaa4efbcd067",
    "libintl-8.dll": "0537c3dd2378218508ebe3cc416d72a99ee2d24ae1c5525e23458f32544ef861",
    "libpcre2-8-0.dll": "ed6f43f96b0891a5b470e724f727bb4c12255354e609b40bd81fd5be6c0b56ac",
    "zlib1.dll": "93e9243a44c29200eeacaf9658efe2558581770e4b11ca4b500e18e424a6e3b5",
}
CURL_DLL_SHA256 = {
    "libbrotlicommon.dll": "ca7bc06ead80259971badf2cd0bfc688db9154dad65f139f49a38b4be9976f97",
    "libbrotlidec.dll": "a142dc6cfa77cb907028aa9426f486f3d8de37855e8db2ae2894147908121881",
    "libcurl-4.dll": "40be8207e0d478ac6f8cf8cebc31376ef1d566c0be1a96ed4272b11a9a5f3ef2",
    "libidn2-0.dll": "9880b52651a0247640a2531d777380f7c35fac7efa7e3979b265ddcb78db8642",
    "libpsl-5.dll": "b31fc70eda598a39b65fe2736d815518db0652d641346f75eac40c2a0499b058",
    "libssh2-1.dll": "ee7fe3e749859334c99e9261689481dd879811122598d22585421f95d29fc7ae",
    "libunistring-5.dll": "b91e2850d3668d4931056a8f4d35783b8ec3eea3735485108a936a072cd16071",
    "libzstd.dll": "b95c223a9548a9ecf51377c962e0bc8f0c51eb0c6f67a296dbc885996f0dd40d",
    "libcurl-openssl-4.dll": "b2ce87aab267f5009803585944e08c17d94a27683ed875294508b8bbb45244dc",
    "libcrypto-3-x64.dll": "0330b5f558996f297d687e1a2b2fcc2cacf883b16baef74aaef35285d7c1231c",
    "libssl-3-x64.dll": "feb5b300e0b3a021fed481178f3d66896426576d4d13b85a304d2a3809b25bfd",
    "libnghttp2-14.dll": "25e6516899fd5021df8c67b92e9f7246249fb03bb7bdf56a08260a8dded06b00",
}
CA_BUNDLE_NAME = "ca-bundle.crt"
CA_BUNDLE_SHA256 = "f345ac0d7dbd1a584dd80ff96eb2919e8dac68e962db2e7111401b434e888493"
PACKAGE_NAME = "windows-network-git-v1"
MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA = "mewcode-network-git-v1"
MANIFEST_LIMIT = 16 * 1024
PAYLOAD_LIMIT = 128 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MANIFEST_KEYS = frozenset({"schema", "base_bundle_identity", "files"})


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate manifest key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _refuse(message: str) -> None:
    raise lpac_runtime.NativeBoundaryUnavailable(f"Network Git setup refused: {message}")


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _digest_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_bounded(path: Path, limit: int, label: str) -> bytes:
    safe = lpac_runtime.checked_path(path, file=True)
    size = safe.stat().st_size
    if size <= 0 or size > limit:
        _refuse(f"{label} is empty or oversized")
    with safe.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) != size or len(raw) > limit:
        _refuse(f"{label} changed while reading")
    return raw


def _read_manifest(path: Path, *, label: str) -> tuple[dict[str, object], bytes]:
    raw = _read_bounded(path, MANIFEST_LIMIT, label)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise lpac_runtime.NativeBoundaryUnavailable(
            f"Network Git setup refused: {label} is invalid") from error
    if type(value) is not dict:
        _refuse(f"{label} is not an object")
    return value, raw


def _expected_target_hashes() -> dict[str, str]:
    """The fixed 15 supplemental payload leaves; exported for core setup checks."""
    return {
        **{f"git/{name}": SDK_HELPER_SHA256 for name in HELPER_NAMES},
        **{f"git/{name}": digest for name, digest in CURL_DLL_SHA256.items()},
        f"git/{CA_BUNDLE_NAME}": CA_BUNDLE_SHA256,
    }


def runtime_git_directory() -> Path:
    """Return the fixed network-bundle Git directory without accessing it."""
    return Path(user_data_root()) / "native" / PACKAGE_NAME / "git"


def _network_root() -> Path:
    return runtime_git_directory().parent


def _current_core() -> tuple[Path, str, dict[str, str], dict[str, bytes]]:
    """Validate the installed core and return its identity and fixed base leaves."""
    from mewcode.sandbox.native_gate import installed_bundle_identity

    try:
        helper = lpac_runtime.checked_path(lpac_runtime.find_runtime_helper(), file=True)
        core = lpac_runtime.checked_path(helper.parent)
        manifest, _ = _read_manifest(core / MANIFEST_NAME, label="core bundle manifest")
    except lpac_runtime.NativeBoundaryUnavailable:
        raise
    except (OSError, ValueError, TypeError) as error:
        raise lpac_runtime.NativeBoundaryUnavailable(
            "Network Git setup refused: installed core bundle is unavailable") from error

    core_files = manifest.get("files")
    if type(core_files) is not dict:
        _refuse("installed core files map is invalid")
    supplemental = _expected_target_hashes()
    present = sorted(set(core_files).intersection(supplemental))
    if present:
        _refuse("installed core already contains network payload leaves: " + ", ".join(present))
    for relative in supplemental:
        if os.path.lexists(core / relative):
            _refuse(f"installed core has a residual network payload leaf: {relative}")

    base_files: dict[str, str] = {}
    base_payloads: dict[str, bytes] = {}
    base_names = ("git.exe", *DLL_NAMES)
    for name in base_names:
        relative = f"git/{name}"
        expected = core_files.get(relative)
        if not isinstance(expected, str) or _SHA256.fullmatch(expected) is None:
            _refuse(f"installed core manifest does not declare {relative}")
        if name in BASE_DLL_SHA256 and expected != BASE_DLL_SHA256[name]:
            _refuse(f"installed core manifest does not pin base dependency {name}")
        source = lpac_runtime.checked_path(core / relative, file=True)
        raw = _read_bounded(source, PAYLOAD_LIMIT, f"installed core payload {relative}")
        if _digest_bytes(raw) != expected:
            _refuse(f"installed core payload hash mismatch for {relative}")
        base_files[relative] = expected
        base_payloads[relative] = raw

    try:
        identity = installed_bundle_identity(core)
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise lpac_runtime.NativeBoundaryUnavailable(
            "Network Git setup refused: installed core identity is unavailable") from error
    if not isinstance(identity, str) or _SHA256.fullmatch(identity) is None:
        _refuse("installed core identity is invalid")
    return core, identity, base_files, base_payloads


def _expected_package_files(base_files: dict[str, str]) -> dict[str, str]:
    return {**base_files, **_expected_target_hashes()}


def _validate_package(root: Path, *, core_identity: str | None = None,
                      base_files: dict[str, str] | None = None) -> tuple[Path, ...]:
    try:
        root = lpac_runtime.checked_path(root)
    except lpac_runtime.NativeBoundaryUnavailable:
        raise
    except OSError as error:
        raise lpac_runtime.NativeBoundaryUnavailable(
            "Network Git setup refused: network bundle is missing or inaccessible") from error
    if not root.is_dir():
        _refuse("network bundle root is not a directory")
    try:
        manifest_path = lpac_runtime.checked_path(root / MANIFEST_NAME, file=True)
        manifest, _ = _read_manifest(manifest_path, label="network bundle manifest")
    except lpac_runtime.NativeBoundaryUnavailable:
        raise
    except OSError as error:
        raise lpac_runtime.NativeBoundaryUnavailable(
            "Network Git setup refused: network bundle manifest is missing or inaccessible") from error
    if set(manifest) != _MANIFEST_KEYS or manifest.get("schema") != MANIFEST_SCHEMA:
        _refuse("network bundle manifest fields or schema are invalid")

    if core_identity is None or base_files is None:
        _, current_identity, current_base_files, _ = _current_core()
        core_identity = current_identity
        base_files = current_base_files
    if manifest.get("base_bundle_identity") != core_identity:
        _refuse("network bundle is bound to a different core bundle identity")

    files = manifest.get("files")
    expected_files = _expected_package_files(base_files)
    if type(files) is not dict or files != expected_files or len(files) != 20:
        _refuse("network bundle files map is not the exact fixed 20-leaf set")

    git_directory = lpac_runtime.checked_path(root / "git")
    if not git_directory.is_dir():
        _refuse("network bundle git path is not a directory")

    root_entries = list(root.iterdir())
    for entry in root_entries:
        lpac_runtime.checked_path(entry)
    if {entry.name for entry in root_entries} != {MANIFEST_NAME, "git"}:
        _refuse("network bundle contains an unknown root file or directory")

    expected_names = {relative.removeprefix("git/") for relative in expected_files}
    git_entries = list(git_directory.iterdir())
    for entry in git_entries:
        lpac_runtime.checked_path(entry, file=True)
    if {entry.name for entry in git_entries} != expected_names:
        _refuse("network bundle contains an unknown or missing Git payload leaf")

    returned = [manifest_path]
    for relative, expected in sorted(expected_files.items()):
        path = lpac_runtime.checked_path(root / relative, file=True)
        size = path.stat().st_size
        if size <= 0 or size > PAYLOAD_LIMIT:
            _refuse(f"network bundle payload is empty or oversized at {relative}")
        if _digest(path) != expected:
            _refuse(f"network bundle payload hash mismatch for {relative}")
        returned.append(path)
    return tuple(returned)


def paths() -> tuple[Path, ...]:
    """Return the validated package manifest and all declared file paths."""
    _, identity, base_files, _ = _current_core()
    return _validate_package(_network_root(), core_identity=identity, base_files=base_files)


def _verified_payload(path: Path, expected: str, label: str) -> bytes:
    try:
        raw = _read_bounded(path, PAYLOAD_LIMIT, label)
    except lpac_runtime.NativeBoundaryUnavailable:
        raise
    except (OSError, ValueError) as error:
        raise lpac_runtime.NativeBoundaryUnavailable(
            f"Network Git setup refused: fixed SDK source is missing or unsafe ({label})") from error
    if _digest_bytes(raw) != expected:
        _refuse(f"pinned SDK payload hash mismatch for {label}")
    return raw


def _sdk_payloads() -> dict[str, bytes]:
    helper = _verified_payload(ROOT / SDK_HELPER_SOURCE, SDK_HELPER_SHA256,
                                "patched Git HTTP helper")
    payloads = {f"git/{name}": helper for name in HELPER_NAMES}
    for name, digest in CURL_DLL_SHA256.items():
        payloads[f"git/{name}"] = _verified_payload(
            ROOT / SDK_BUILD / "bin" / name, digest, name)
    payloads[f"git/{CA_BUNDLE_NAME}"] = _verified_payload(
        ROOT / SDK_BUILD / "etc" / "ssl" / "certs" / CA_BUNDLE_NAME,
        CA_BUNDLE_SHA256, "CA bundle")
    return payloads


def _ensure_native_parent() -> Path:
    native = Path(user_data_root()) / "native"
    if not native.is_absolute():
        _refuse("user data root is not absolute")
    missing: list[Path] = []
    cursor = native
    while not os.path.lexists(cursor):
        missing.append(cursor)
        cursor = cursor.parent
    ancestor = lpac_runtime.checked_path(cursor)
    if not ancestor.is_dir():
        _refuse("native data parent is not a directory")
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            pass
        checked = lpac_runtime.checked_path(directory)
        if not checked.is_dir():
            _refuse("native data parent is not a directory")
    return lpac_runtime.checked_path(native)


def _new_stage(native: Path) -> Path:
    for _ in range(8):
        stage = native / f".{PACKAGE_NAME}.staging-{uuid.uuid4().hex}"
        try:
            stage.mkdir()
        except FileExistsError:
            continue
        lpac_runtime.checked_path(stage)
        return stage
    _refuse("could not allocate a unique host staging directory")


def _write_synced(path: Path, raw: bytes) -> None:
    try:
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as error:
        raise lpac_runtime.NativeBoundaryUnavailable(
            f"Network Git setup refused: staging leaf already exists ({path.name})") from error


def _manifest_bytes(identity: str, files: dict[str, str]) -> bytes:
    value = {"schema": MANIFEST_SCHEMA, "base_bundle_identity": identity, "files": files}
    raw = (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if len(raw) > MANIFEST_LIMIT:
        _refuse("network bundle manifest exceeds its fixed size bound")
    return raw


def _result(root: Path, *, changed: bool) -> dict[str, object]:
    return {
        "configured": True,
        "changed": changed,
        "directory": str(root / "git"),
        "helpers": [str(root / "git" / name) for name in HELPER_NAMES],
        "sha256": SDK_HELPER_SHA256,
    }


def ensure_https_helpers() -> dict[str, object]:
    """Build the separate pinned network Git bundle during explicit host setup.

    The caller holds the shared runner lock.  This operation only reads the
    verified core and fixed SDK inputs, and publishes one sibling directory
    rename; it never edits the LPAC core, runs Git, or accesses the network.
    """
    _, identity, base_files, base_payloads = _current_core()
    native = _ensure_native_parent()
    target = _network_root()
    if os.path.lexists(target):
        _validate_package(target, core_identity=identity, base_files=base_files)
        return _result(target, changed=False)

    # Validate every development source before allocating or publishing a
    # staging directory. A deployed complete package does not need any SDK.
    payloads = {**base_payloads, **_sdk_payloads()}
    files = _expected_package_files(base_files)
    if set(payloads) != set(files) or any(_digest_bytes(payloads[key]) != digest
                                           for key, digest in files.items()):
        _refuse("assembled payload set does not match its fixed hashes")

    stage = _new_stage(native)
    git = stage / "git"
    git.mkdir()
    for relative, raw in sorted(payloads.items()):
        _write_synced(stage / relative, raw)
    _write_synced(stage / MANIFEST_NAME, _manifest_bytes(identity, files))

    # Re-read the core before publication. If it changed while the candidate
    # was being built, leave this unpublished stage for diagnosis.
    _, current_identity, current_base_files, _ = _current_core()
    if current_identity != identity or current_base_files != base_files:
        _refuse("core bundle identity changed while staging the network package")
    _validate_package(stage, core_identity=identity, base_files=base_files)

    try:
        os.rename(stage, target)
    except OSError as error:
        raise lpac_runtime.NativeBoundaryUnavailable(
            "Network Git setup refused: could not publish the staged bundle; staging was retained") from error

    # A post-commit problem leaves the independent package in place and closed;
    # it never authorizes itself or changes core gate records.
    _validate_package(target, core_identity=identity, base_files=base_files)
    return _result(target, changed=True)
