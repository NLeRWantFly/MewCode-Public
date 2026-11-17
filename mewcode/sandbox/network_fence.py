"""Typed host-side interface for the optional per-AppContainer WFP fence.

This module only builds verified invocation descriptions and parses their
results. It never runs the native executable or requests elevation.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CANDIDATE = ROOT / ".tmp/native-network-fence"
MANIFEST_NAME = "manifest.json"
BINARY_NAME = "MewCode.NetworkFence.exe"
SCHEMA = "mewcode-network-fence-v1"
CANDIDATE_SCHEMA = "mewcode-network-fence-candidate-v1"
SOURCE_NAME = "MewCode.NetworkFence.cs"
SID_RE = re.compile(r"S-1-15-2-(?:0|[1-9][0-9]*)-(?:0|[1-9][0-9]*)-(?:0|[1-9][0-9]*)-(?:0|[1-9][0-9]*)-(?:0|[1-9][0-9]*)-(?:0|[1-9][0-9]*)-(?:0|[1-9][0-9]*)\Z")
MAX_SUBAUTHORITY = 0xFFFFFFFF
FILTER_NAMES = (
    "block_v4", "block_v6", "allow_v4_60080", "allow_v4_60081",
    "allow_v6_60080", "allow_v6_60081",
)


class NetworkFenceError(RuntimeError):
    """A typed validation or host-result failure."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class NetworkFenceCandidate:
    directory: Path
    executable: Path
    sha256: str
    source_sha256: str


@dataclass(frozen=True)
class NetworkFenceInvocation:
    """An explicit argv array for a host caller; it is never executed here."""

    executable: Path
    arguments: tuple[str, ...]
    operation: str
    sid: str
    sha256: str

    @property
    def argv(self) -> tuple[str, ...]:
        return (str(self.executable), *self.arguments)


@dataclass(frozen=True)
class NetworkFenceResult:
    operation: str
    ok: bool
    error_code: str
    detail: str
    sid: str | None
    installed: bool | None
    rules_complete: bool | None
    loopback_exempt: bool | None
    provider_present: bool | None
    sublayer_present: bool | None
    filters: Mapping[str, bool]
    filter_keys: Mapping[str, str]
    requires_administrator: bool
    raw: Mapping[str, Any]


def validate_appcontainer_sid(value: str) -> str:
    """Accept only a canonical eight-subauthority S-1-15-2 profile SID."""
    if not isinstance(value, str) or not SID_RE.fullmatch(value):
        raise NetworkFenceError(
            "invalid_appcontainer_sid",
            "Only a canonical full S-1-15-2 AppContainer profile SID is accepted.",
        )
    parts = value.split("-")
    try:
        subauthorities = [int(part) for part in parts[1:]]
    except ValueError as error:
        raise NetworkFenceError("invalid_appcontainer_sid", "The AppContainer SID is malformed.") from error
    if any(number > MAX_SUBAUTHORITY for number in subauthorities):
        raise NetworkFenceError("invalid_appcontainer_sid", "An AppContainer SID subauthority is out of range.")
    if len(parts) != 11:
        raise NetworkFenceError("invalid_appcontainer_sid", "The AppContainer SID has an unexpected profile length.")
    return value


def _read_manifest(directory: Path) -> tuple[NetworkFenceCandidate, dict[str, Any]]:
    try:
        resolved_directory = Path(directory).resolve(strict=True)
        manifest_path = resolved_directory / MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NetworkFenceError("candidate_unavailable", "The native network-fence candidate manifest is unavailable or invalid.") from error
    if not isinstance(manifest, dict) or manifest.get("schema") != CANDIDATE_SCHEMA:
        raise NetworkFenceError("candidate_manifest_invalid", "The candidate manifest has an unsupported schema.")
    if manifest.get("binary") != BINARY_NAME:
        raise NetworkFenceError("candidate_manifest_invalid", "The candidate manifest names an unexpected executable.")
    source_hashes = manifest.get("source_sha256")
    expected_source = ROOT / "native/windows" / SOURCE_NAME
    if (not isinstance(source_hashes, dict)
            or set(source_hashes) != {SOURCE_NAME}
            or not isinstance(source_hashes.get(SOURCE_NAME), str)
            or not re.fullmatch(r"[0-9a-f]{64}", source_hashes[SOURCE_NAME])):
        raise NetworkFenceError("candidate_manifest_invalid", "The candidate manifest has invalid source digests.")
    try:
        actual_source_hash = hashlib.sha256(expected_source.read_bytes()).hexdigest()
    except OSError as error:
        raise NetworkFenceError("candidate_source_unavailable", "The native network-fence source cannot be read.") from error
    if actual_source_hash != source_hashes[SOURCE_NAME]:
        raise NetworkFenceError("candidate_source_mismatch", "The candidate was built from a different native source revision.")
    expected_hash = manifest.get("sha256")
    if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise NetworkFenceError("candidate_manifest_invalid", "The candidate manifest has no valid executable SHA-256.")
    executable = resolved_directory / BINARY_NAME
    candidate = NetworkFenceCandidate(resolved_directory, executable, expected_hash,
                                      source_hashes[SOURCE_NAME])
    verify_candidate(candidate)
    return candidate, manifest


def load_candidate(directory: Path = DEFAULT_CANDIDATE) -> NetworkFenceCandidate:
    """Load the fixed candidate and verify its executable against its manifest."""
    candidate, _ = _read_manifest(Path(directory))
    return candidate


def verify_candidate(candidate: NetworkFenceCandidate) -> str:
    try:
        directory = Path(candidate.directory).resolve(strict=True)
        executable = Path(candidate.executable).resolve(strict=True)
        expected_path = directory / BINARY_NAME
        if executable != expected_path or not executable.is_file():
            raise NetworkFenceError("candidate_path_invalid", "The candidate executable is outside its fixed bundle path.")
        actual_hash = hashlib.sha256(executable.read_bytes()).hexdigest()
        source_path = ROOT / "native/windows" / SOURCE_NAME
        actual_source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    except NetworkFenceError:
        raise
    except OSError as error:
        raise NetworkFenceError("candidate_unavailable", "The candidate executable cannot be read.") from error
    if actual_hash != candidate.sha256:
        raise NetworkFenceError("candidate_hash_mismatch", "The candidate executable no longer matches its recorded SHA-256.")
    if actual_source_hash != candidate.source_sha256:
        raise NetworkFenceError("candidate_source_mismatch", "The candidate no longer matches the current native source revision.")
    return actual_hash


def _invocation(candidate: NetworkFenceCandidate, operation: str,
                sid: str, arguments: tuple[str, ...]) -> NetworkFenceInvocation:
    canonical_sid = validate_appcontainer_sid(sid)
    digest = verify_candidate(candidate)
    return NetworkFenceInvocation(
        executable=Path(candidate.executable).resolve(strict=True),
        arguments=arguments,
        operation=operation,
        sid=canonical_sid,
        sha256=digest,
    )


def prepare_install_call(candidate: NetworkFenceCandidate, sid: str,
                         report_path: Path) -> NetworkFenceInvocation:
    """Prepare an install argv for an explicit host setup flow; do not run it."""
    path = Path(report_path)
    if not path.is_absolute():
        raise NetworkFenceError("invalid_report_path", "The install report path must be absolute.")
    path = path.resolve(strict=False)
    if not path.parent.is_dir():
        raise NetworkFenceError("report_parent_missing", "The install report parent directory must already exist.")
    if path.exists():
        raise NetworkFenceError("report_path_exists", "The install report path already exists and will not be overwritten.")
    return _invocation(candidate, "install", sid,
                       ("--install", "--sid", validate_appcontainer_sid(sid), "--report", str(path)))


def prepare_status_call(candidate: NetworkFenceCandidate, sid: str) -> NetworkFenceInvocation:
    """Prepare the read-only status argv for a host caller; do not run it."""
    canonical_sid = validate_appcontainer_sid(sid)
    return _invocation(candidate, "status", canonical_sid,
                       ("--status", "--sid", canonical_sid))


def prepare_uninstall_call(candidate: NetworkFenceCandidate, sid: str) -> NetworkFenceInvocation:
    """Prepare an uninstall argv for an explicit host setup flow; do not run it."""
    canonical_sid = validate_appcontainer_sid(sid)
    return _invocation(candidate, "uninstall", canonical_sid,
                       ("--uninstall", "--sid", canonical_sid))


def parse_network_fence_result(payload: str | bytes, *,
                               expected_operation: str | None = None,
                               expected_sid: str | None = None) -> NetworkFenceResult:
    """Parse one native JSON response into a typed result."""
    try:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
        raise NetworkFenceError("invalid_native_result", "The native network-fence result is not valid UTF-8 JSON.") from error
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise NetworkFenceError("invalid_native_result", "The native result has an unsupported schema.")
    operation = raw.get("operation", "")
    if not isinstance(operation, str) or (expected_operation and operation != expected_operation):
        raise NetworkFenceError("invalid_native_result", "The native result does not match the requested operation.")
    sid = raw.get("sid")
    if sid is not None:
        sid = validate_appcontainer_sid(sid)
    if expected_sid is not None and sid != validate_appcontainer_sid(expected_sid):
        raise NetworkFenceError("invalid_native_result", "The native result does not match the requested AppContainer SID.")
    ok = raw.get("ok")
    if not isinstance(ok, bool):
        raise NetworkFenceError("invalid_native_result", "The native result is missing its success flag.")
    error_code = raw.get("error_code", "")
    detail = raw.get("detail", "")
    if not isinstance(error_code, str) or not isinstance(detail, str):
        raise NetworkFenceError("invalid_native_result", "The native result has invalid error fields.")
    filters = raw.get("filters", {})
    filter_keys = raw.get("filter_keys", {})
    if not isinstance(filters, dict) or not isinstance(filter_keys, dict):
        raise NetworkFenceError("invalid_native_result", "The native result has invalid filter maps.")
    if any(not isinstance(name, str) or not isinstance(value, bool) for name, value in filters.items()):
        raise NetworkFenceError("invalid_native_result", "The native result has an invalid filter state.")
    if any(not isinstance(name, str) or not isinstance(value, str) for name, value in filter_keys.items()):
        raise NetworkFenceError("invalid_native_result", "The native result has an invalid filter key.")
    if not set(filters).issubset(FILTER_NAMES) or not set(filter_keys).issubset(FILTER_NAMES):
        raise NetworkFenceError("invalid_native_result", "The native result contains an unknown filter name.")
    bool_fields: dict[str, bool | None] = {}
    for name in ("installed", "rules_complete", "loopback_exempt", "provider_present", "sublayer_present"):
        value = raw.get(name)
        if value is not None and not isinstance(value, bool):
            raise NetworkFenceError("invalid_native_result", "The native result has a non-boolean status field.")
        bool_fields[name] = value
    requires_admin = raw.get("requires_administrator", False)
    if not isinstance(requires_admin, bool):
        raise NetworkFenceError("invalid_native_result", "The native result has an invalid administrator-required flag.")
    return NetworkFenceResult(
        operation=operation,
        ok=ok,
        error_code=error_code,
        detail=detail,
        sid=sid,
        installed=bool_fields["installed"],
        rules_complete=bool_fields["rules_complete"],
        loopback_exempt=bool_fields["loopback_exempt"],
        provider_present=bool_fields["provider_present"],
        sublayer_present=bool_fields["sublayer_present"],
        filters=dict(filters),
        filter_keys=dict(filter_keys),
        requires_administrator=requires_admin,
        raw=dict(raw),
    )


def read_install_report(report_path: Path, *, expected_sid: str) -> NetworkFenceResult:
    """Read a bounded install report written by the explicit native host call."""
    try:
        path = Path(report_path)
        if not path.is_absolute():
            raise NetworkFenceError("invalid_report_path", "The install report path must be absolute.")
        if path.stat().st_size > 65536:
            raise NetworkFenceError("invalid_report", "The install report exceeds its fixed size bound.")
        payload = path.read_bytes()
    except NetworkFenceError:
        raise
    except OSError as error:
        raise NetworkFenceError("report_unavailable", "The native install report cannot be read.") from error
    return parse_network_fence_result(payload, expected_operation="install", expected_sid=expected_sid)


def parse_status_result(payload: str | bytes, *, expected_sid: str) -> NetworkFenceResult:
    return parse_network_fence_result(payload, expected_operation="status", expected_sid=expected_sid)
