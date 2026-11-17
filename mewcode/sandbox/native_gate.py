"""Evidence-driven production gate for the installed native Windows boundary.

The two historical gates (``native_runtime_ready`` / ``native_fastpath_ready``)
were hard-coded ``False``. This module replaces that with a five-state,
fail-closed state machine driven by host-side evidence:

    not_installed  no runtime bundle under the user data root
    invalid        bundle drift, or a gate record bound to another bundle
                   identity or another machine, or an enablement record
                   without a valid attestation
    pending        bundle verified; acceptance attestation missing/incomplete
    eligible       attestation complete; explicit host enablement missing
    enabled        attestation + enablement valid; the gates open

A bundle must never authorize itself: the manifest ``production_enabled``
field is installer-checked to be ``false`` and is never consumed here. The
gate records (``attestation.json`` / ``enabled.json``) live inside the
installed bundle directory, are written only by the host-side installer
script after real acceptance evidence exists, and are bound to the exact
installed bundle identity and this machine's cryptographic MachineGuid.
Uninstall removes them; a fresh install starts at ``pending`` again; a
rollback restores the archived bundle together with its own records.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import os
import stat
from pathlib import Path

ATTESTATION_NAME = "attestation.json"
ENABLEMENT_NAME = "enabled.json"
ATTESTATION_SCHEMA = "mewcode-native-attestation-v1"
ENABLEMENT_SCHEMA = "mewcode-native-enablement-v1"
MAX_RECORD_BYTES = 16384

# Acceptance evidence required before AUTO may be enabled. The keys are
# defined here, not in the record, so a record can never shrink the
# requirement. Each entry includes a structured report path and its SHA-256;
# the gate checks its cases, machine/bundle binding, and current bytes.
REQUIRED_EVIDENCE: tuple[str, ...] = (
    "sensitive_read_denied",          # boundary matrix: sensitive reads refused
    "workspace_escape_write_denied",  # boundary matrix: out-of-scope writes refused
    "junction_escape_denied",         # boundary matrix: junction/symlink escape refused
    "network_denied",                 # boundary matrix: network incl. loopback refused
    "secret_env_scrubbed",            # boundary matrix: secret env vars stripped
    "process_tree_reaped",            # interruption reaps the child process tree
    "compat_powershell",              # PowerShell commands, CJK output, exit codes
    "compat_paths",                   # multi-volume / CJK / spaces / relative paths
    "compat_python",                  # project Python and test commands
    "compat_git",                     # git status/diff/add/commit in authorized repos
)


def required_evidence(root: Path) -> tuple[str, ...]:
    """A privileged metadata capability needs its own live acceptance proof."""
    from mewcode.sandbox.lpac_runtime import checked_path
    manifest = checked_path(root / "manifest.json", file=True)
    if manifest.stat().st_size > MAX_RECORD_BYTES:
        raise _RecordInvalid("Native manifest exceeds bound")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise _RecordInvalid("Native manifest must be an object")
    protocols = data.get("protocols", [])
    if (not isinstance(protocols, list) or any(type(item) is not str for item in protocols)
            or len(protocols) != len(set(protocols))):
        raise _RecordInvalid("Native manifest protocols are malformed")
    if "mewcode-metadata-broker-v1" in protocols:
        return (*REQUIRED_EVIDENCE, "metadata_preserved",
                "metadata_protocol_quick", "metadata_protocol_lifetime")
    return REQUIRED_EVIDENCE


def _metadata_broker_sha256(root: Path) -> str:
    from mewcode.sandbox.metadata_broker import installed_artifact
    artifact = installed_artifact(root / "MewCode.AppContainerRuntime.exe")
    if artifact is None:
        raise _RecordInvalid("Bundle declares metadata broker without a verified broker artifact")
    return artifact[1]


class NativeGateState(Enum):
    NOT_INSTALLED = "not_installed"
    INVALID = "invalid"
    PENDING = "pending"
    ELIGIBLE = "eligible"
    ENABLED = "enabled"


@dataclass(frozen=True)
class GateStatus:
    """A point-in-time gate verdict; ``reason`` must name the real blocker."""

    state: NativeGateState
    reason: str
    bundle_identity: str | None = None
    missing_evidence: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.state is NativeGateState.ENABLED


class _RecordInvalid(RuntimeError):
    """A gate record exists but cannot be trusted."""


def machine_guid() -> str:
    """This machine's cryptographic identity; "" when it cannot be read."""
    if os.name != "nt":
        return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
        return str(value)
    except OSError:
        return ""


def bundle_identity(helper_sha256: str, files: object) -> str:
    """Canonical identity of a verified bundle (helper hash + payload map)."""
    canonical = json.dumps({"helper": helper_sha256, "files": files or {}},
                           sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def installed_bundle_identity(root: Path) -> str:
    """Identity of an already-verified installed bundle directory."""
    from mewcode.sandbox.windows import NativeBoundaryUnavailable
    try:
        data = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        helper = data["sha256"]
        if not isinstance(helper, str):
            raise TypeError
        return bundle_identity(helper, data.get("files"))
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        raise NativeBoundaryUnavailable("Installed bundle manifest is unreadable") from exc


def _default_bundle_root() -> Path:
    from mewcode.runtime_paths import user_data_root
    return user_data_root() / "native" / "windows-lpac-v1"


def _read_record(root: Path, name: str, schema: str) -> dict:
    path = root / name
    try:
        if path.is_symlink() or not path.is_file():
            raise _RecordInvalid(f"Native gate record {name} is missing")
        if path.stat().st_size > MAX_RECORD_BYTES:
            raise _RecordInvalid(f"Native gate record {name} is oversized")
        data = json.loads(path.read_text(encoding="utf-8"))
    except _RecordInvalid:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise _RecordInvalid(f"Native gate record {name} is unreadable") from exc
    if not isinstance(data, dict) or data.get("schema") != schema:
        raise _RecordInvalid(f"Native gate record {name} has an invalid schema")
    return data


def _evidence_passes(entry: object, key: str, identity: str, machine: str, cache: dict,
                     broker_sha256: str | None = None) -> bool:
    from mewcode.sandbox.native_evidence import read_report, evidence_passes
    if (not isinstance(entry, dict) or entry.get("result") != "pass"
            or not isinstance(entry.get("report"), str)
            or not isinstance(entry.get("sha256"), str)):
        return False
    path = Path(entry["report"])
    if not path.is_absolute():
        return False
    try:
        if str(path) not in cache:
            cache[str(path)] = read_report(path)
        report, digest = cache[str(path)]
        return (digest == entry["sha256"]
                and evidence_passes(report, key, identity, machine, broker_sha256=broker_sha256))
    except (OSError, ValueError, RuntimeError, RecursionError):
        return False


def _bound(record: dict, identity: str, machine: str) -> bool:
    return (record.get("bundle_identity") == identity
            and bool(machine) and record.get("machine_guid") == machine)


def native_gate_status(bundle_root: Path | None = None) -> GateStatus:
    """Fail-closed gate verdict; any inconsistency is ``invalid``, never open."""
    from mewcode.sandbox.windows import NativeBoundaryUnavailable
    root = Path(bundle_root) if bundle_root is not None else _default_bundle_root()
    try:
        root_mode = root.stat().st_mode
    except FileNotFoundError:
        return GateStatus(
            NativeGateState.NOT_INSTALLED,
            "native runtime bundle is not installed; install it with scripts/manage_native_runtime.py")
    except OSError:
        return GateStatus(NativeGateState.INVALID, "native runtime bundle is inaccessible; check host access")
    if not stat.S_ISDIR(root_mode):
        return GateStatus(NativeGateState.INVALID, "native runtime bundle path is not a directory")
    from mewcode.sandbox.lpac_runtime import find_runtime_helper
    try:
        if bundle_root is not None:
            # Explicit root (installer flows): verify against that directory.
            from mewcode.sandbox.lpac_runtime import checked_path, validate_bundle_files
            checked_root = checked_path(root)
            manifest = checked_path(checked_root / "manifest.json", file=True)
            if manifest.stat().st_size > 16384:
                raise NativeBoundaryUnavailable("Native helper manifest is oversized")
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise NativeBoundaryUnavailable("Native helper manifest must be an object")
            helper = checked_path(checked_root / "MewCode.AppContainerRuntime.exe", file=True)
            if (data.get("schema") != "mewcode-lpac-bundle-v1" or
                    data.get("sha256") != hashlib.sha256(helper.read_bytes()).hexdigest()):
                raise NativeBoundaryUnavailable("Native helper bundle identity mismatch")
            validate_bundle_files(checked_root, data.get("files"))
            identity = bundle_identity(data["sha256"], data.get("files"))
        else:
            find_runtime_helper()
            identity = installed_bundle_identity(root)
    except (NativeBoundaryUnavailable, OSError, ValueError) as exc:
        return GateStatus(
            NativeGateState.INVALID,
            f"installed native runtime bundle failed verification: {exc}")
    machine = machine_guid()
    try:
        required = required_evidence(root)
        broker_sha256 = (_metadata_broker_sha256(root)
                         if "metadata_protocol_quick" in required else None)
    except (OSError, ValueError, RuntimeError) as exc:
        return GateStatus(NativeGateState.INVALID, str(exc), bundle_identity=identity)
    attestation_path = root / ATTESTATION_NAME
    enablement_path = root / ENABLEMENT_NAME
    if not attestation_path.is_file():
        if enablement_path.is_file():
            return GateStatus(
                NativeGateState.INVALID,
                "enablement record exists without an acceptance attestation; re-attest or reinstall",
                bundle_identity=identity)
        return GateStatus(
            NativeGateState.PENDING,
            "native runtime bundle is installed and verified, but no acceptance attestation is "
            "recorded; required evidence: " + ", ".join(required),
            bundle_identity=identity, missing_evidence=required)
    try:
        attestation = _read_record(root, ATTESTATION_NAME, ATTESTATION_SCHEMA)
    except _RecordInvalid as exc:
        return GateStatus(NativeGateState.INVALID, str(exc), bundle_identity=identity)
    if attestation.get("bundle_identity") != identity:
        return GateStatus(
            NativeGateState.INVALID,
            "acceptance attestation is bound to a different bundle identity; the bundle changed "
            "after attestation and must be re-attested",
            bundle_identity=identity)
    if not _bound(attestation, identity, machine):
        return GateStatus(
            NativeGateState.INVALID,
            "acceptance attestation is not bound to this machine",
            bundle_identity=identity)
    evidence = attestation.get("evidence")
    if not isinstance(evidence, dict):
        return GateStatus(NativeGateState.INVALID,
                          "acceptance attestation has an invalid evidence map",
                          bundle_identity=identity)
    unknown = sorted(set(evidence) - set(required))
    if unknown:
        return GateStatus(NativeGateState.INVALID,
                          "acceptance attestation carries unknown evidence keys: " + ", ".join(unknown),
                          bundle_identity=identity)
    report_cache: dict = {}
    missing = tuple(key for key in required
                    if not _evidence_passes(evidence.get(key), key, identity, machine,
                                            report_cache, broker_sha256=broker_sha256))
    if missing:
        return GateStatus(
            NativeGateState.PENDING,
            "acceptance attestation is incomplete; missing or non-pass evidence: " + ", ".join(missing),
            bundle_identity=identity, missing_evidence=missing)
    if not enablement_path.is_file():
        return GateStatus(
            NativeGateState.ELIGIBLE,
            "acceptance attestation is complete; AUTO stays closed until explicit host enablement "
            "(scripts/manage_native_runtime.py enable)",
            bundle_identity=identity)
    try:
        enablement = _read_record(root, ENABLEMENT_NAME, ENABLEMENT_SCHEMA)
    except _RecordInvalid as exc:
        return GateStatus(NativeGateState.INVALID, str(exc), bundle_identity=identity)
    if not _bound(enablement, identity, machine):
        return GateStatus(
            NativeGateState.INVALID,
            "enablement record is bound to a different bundle or machine",
            bundle_identity=identity)
    return GateStatus(
        NativeGateState.ENABLED,
        "native boundary attested and explicitly enabled on this machine",
        bundle_identity=identity)
