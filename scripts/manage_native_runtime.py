"""Assemble, install, verify, roll back and uninstall the native runtime bundle.

The installed bundle is the production runtime package for the Windows LPAC
backend: the host helper (``MewCode.AppContainerRuntime.exe``) plus the
AppContainer-compatible Git build and its four runtime DLLs under ``git/``.

Layout of an installed bundle (target ``<data-root>/native/windows-lpac-v1``)::

    MewCode.AppContainerRuntime.exe
    manifest.json
    git/git.exe
    git/libiconv-2.dll, libintl-8.dll, libpcre2-8-0.dll, zlib1.dll

Guarantees, independent of command line:

* No system Git is replaced; no global PATH, registry or system ACL changes.
* ``production_enabled`` stays ``false``; enabling AUTO is a separate,
  evidence-driven acceptance gate, never a side effect of installing.
* Every install path is hash-verified before and after the atomic swap.
* Rollback restores the previous version snapshot; uninstall removes the
  whole target directory so every consumer observes "not installed".
* Data-root resolution: ``--data-root`` > ``MEWCODE_USER_DATA_DIR`` >
  ``%APPDATA%/com.mewcode.desktop`` (the desktop app sets the env var to the
  same directory, see ``src-tauri/src/main.rs``).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mewcode.sandbox.lpac_runtime import validate_bundle_files  # noqa: E402
from mewcode.sandbox import native_gate  # noqa: E402
from scripts import probe_git_compat as probe  # noqa: E402
from scripts import probe_windows_path_roundtrip as rt  # noqa: E402

GATE_RECORD_NAMES = (native_gate.ATTESTATION_NAME, native_gate.ENABLEMENT_NAME, "recovery-policy.json")

HELPER_NAME = "MewCode.AppContainerRuntime.exe"
MANIFEST_NAME = "manifest.json"
BUNDLE_SCHEMA = "mewcode-lpac-bundle-v1"
TARGET_DIR_NAME = "windows-lpac-v1"
STAGING_DIR_NAME = ".windows-lpac-v1.staging"
VERSIONS_DIR_NAME = "windows-lpac-v1-versions"
MAX_MANIFEST_BYTES = 16384
GIT_VERSION = "2.55.0.windows.3"
DESKTOP_APP_ID = "com.mewcode.desktop"


class InstallerError(RuntimeError):
    """A refusal or verification failure; nothing was changed on disk."""


def _utc_stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha256(path: Path) -> str:
    return rt._sha256_file(path)


def _strict_read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"),
                      object_pairs_hook=rt._strict_json_object,
                      parse_constant=rt._reject_nonfinite_json)
    if not isinstance(data, dict):
        raise InstallerError(f"{path.name} must be a JSON object")
    return data


def resolve_data_root(explicit: Path | None) -> Path:
    """Resolve the data root exactly like the desktop backend would."""
    if explicit is not None:
        root = Path(explicit)
    else:
        configured = os.environ.get("MEWCODE_USER_DATA_DIR", "").strip()
        if configured:
            root = Path(configured)
        else:
            roaming = os.environ.get("APPDATA", "").strip()
            if not roaming:
                raise InstallerError("APPDATA is unavailable; pass --data-root explicitly")
            root = Path(roaming) / DESKTOP_APP_ID
    if not root.is_absolute():
        raise InstallerError("The data root must be an absolute path")
    if root == Path(root.anchor):
        raise InstallerError("A drive root is not a valid data root")
    return root


def _native_layout(data_root: Path) -> dict[str, Path]:
    native = data_root / "native"
    return {
        "native": native,
        "target": native / TARGET_DIR_NAME,
        "staging": native / STAGING_DIR_NAME,
        "versions": native / VERSIONS_DIR_NAME,
    }


def _checked_relative_entry(relative: str) -> tuple[str, ...]:
    """Validate a manifest ``files`` key without touching the filesystem."""
    if not isinstance(relative, str) or not relative:
        raise InstallerError("Native bundle entry path is malformed")
    parts = PurePosixPath(relative).parts
    if (PurePosixPath(relative).is_absolute() or ".." in parts
            or any(":" in part for part in parts) or len(parts) < 2
            or parts[0] not in {"git", "broker"}
            or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", part) for part in parts)):
        raise InstallerError(f"Native bundle entry path is not allowed: {relative!r}")
    return parts


def read_bundle_manifest(bundle: Path) -> dict:
    bundle = rt._existing_directory(bundle, "Native runtime bundle")
    manifest_path = rt._existing_file(bundle / MANIFEST_NAME, "Bundle manifest", suffix=".json")
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise InstallerError("Bundle manifest is oversized")
    data = _strict_read_json(manifest_path)
    if data.get("schema") != BUNDLE_SCHEMA:
        raise InstallerError("Bundle manifest schema mismatch")
    if data.get("production_enabled") is not False:
        raise InstallerError("Refusing a bundle that claims production authorization")
    version = data.get("bundle_version")
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}", version):
        raise InstallerError("Bundle manifest has an invalid bundle_version")
    files = data.get("files")
    if not isinstance(files, dict) or not files:
        raise InstallerError("Bundle manifest is missing the files map")
    for relative in files:
        _checked_relative_entry(relative)
    return data


def verify_bundle(bundle: Path) -> dict:
    """Full verification of an assembled bundle directory."""
    data = read_bundle_manifest(bundle)
    bundle = bundle.resolve(strict=True)
    problems: list[str] = []
    helper = bundle / HELPER_NAME
    try:
        helper_checked = rt._existing_file(helper, "Native helper")
    except Exception as exc:  # noqa: BLE001 - refusal detail only
        return {"ok": False, "problems": [f"helper: {exc}"], "manifest": data}
    if data.get("sha256") != _sha256(helper_checked):
        problems.append("helper sha256 mismatch")
    try:
        validate_bundle_files(bundle, data["files"])
    except Exception as exc:  # noqa: BLE001 - refusal detail only
        problems.append(f"files map: {exc}")
    allowed = {HELPER_NAME, MANIFEST_NAME, *GATE_RECORD_NAMES,
               *(f"{PurePosixPath(k).as_posix()}" for k in data["files"])}
    for path in sorted(bundle.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(bundle).as_posix()
        if relative not in allowed:
            problems.append(f"unexpected extra file: {relative}")
    return {"ok": not problems, "problems": problems, "manifest": data}


def _stage_bundle(bundle: Path, staging: Path) -> dict:
    """Copy a verified bundle into staging and stamp the install time."""
    data = read_bundle_manifest(bundle)
    bundle = bundle.resolve(strict=True)
    staging.mkdir(parents=True, exist_ok=False)
    try:
        shutil.copyfile(bundle / HELPER_NAME, staging / HELPER_NAME)
        for relative in data["files"]:
            parts = _checked_relative_entry(relative)
            target = staging.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(bundle.joinpath(*parts), target)
        installed = dict(data)
        installed["installed_utc"] = _utc_stamp()
        manifest_bytes = (json.dumps(installed, sort_keys=True, indent=2) + "\n").encode("utf-8")
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise InstallerError("Stamped manifest exceeds the size bound")
        (staging / MANIFEST_NAME).write_bytes(manifest_bytes)
        validate_bundle_files(staging, installed["files"])
        helper = staging / HELPER_NAME
        if installed["sha256"] != _sha256(helper):
            raise InstallerError("Staged helper hash mismatch")
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return installed


def _fresh_archive_path(versions: Path, version: str) -> Path:
    versions.mkdir(parents=True, exist_ok=True)
    base = re.sub(r"[^A-Za-z0-9._-]", "-", str(version)) or "unknown"
    candidate = versions / base
    counter = 2
    while candidate.exists():
        candidate = versions / f"{base}-{counter}"
        counter += 1
    return candidate


def _read_installed_manifest(target: Path) -> dict | None:
    manifest = target / MANIFEST_NAME
    if not manifest.is_file():
        return None
    try:
        return _strict_read_json(manifest)
    except (OSError, ValueError):
        return None


def _swap_into_place(staging: Path, target: Path, versions: Path) -> Path | None:
    """Atomic-ish swap; a missing target during the gap means fail-closed."""
    archived: Path | None = None
    if target.exists():
        previous = _read_installed_manifest(target)
        version = (previous or {}).get("bundle_version") or "unknown"
        archived = _fresh_archive_path(versions, str(version))
        os.rename(target, archived)
    try:
        os.rename(staging, target)
    except OSError:
        if archived is not None:
            try:
                os.rename(archived, target)
            except OSError:
                # Leave the archive in place; target stays absent, which every
                # consumer observes as "not installed" (fail-closed).
                pass
        raise
    return archived


def install_bundle(bundle: Path, data_root: Path, *, keep_staging_on_failure: bool = False) -> dict:
    report = verify_bundle(bundle)
    if not report["ok"]:
        raise InstallerError("Bundle verification failed: " + "; ".join(report["problems"]))
    layout = _native_layout(data_root)
    native = layout["native"]
    native.mkdir(parents=True, exist_ok=True)
    staging = layout["staging"]
    if staging.exists():
        # A leftover staging directory is always an incomplete copy of ours.
        shutil.rmtree(staging)
    installed = _stage_bundle(bundle.resolve(strict=True), staging)
    try:
        archived = _swap_into_place(staging, layout["target"], layout["versions"])
    except OSError as exc:
        if not keep_staging_on_failure:
            shutil.rmtree(staging, ignore_errors=True)
        raise InstallerError(
            "Atomic swap failed (is the desktop backend holding the helper exe? "
            f"Stop the backend and retry): {exc}") from exc
    result = {
        "installed_version": installed["bundle_version"],
        "installed_utc": installed["installed_utc"],
        "data_root": str(data_root),
        "target": str(layout["target"]),
        "archived_previous": str(archived) if archived else None,
        "production_enabled": installed["production_enabled"],
    }
    return result


def installed_state(data_root: Path) -> dict:
    layout = _native_layout(data_root)
    target = layout["target"]
    state: dict = {"data_root": str(data_root), "target": str(target)}
    try:
        state["installed"] = stat.S_ISDIR(target.stat().st_mode)
    except FileNotFoundError:
        state["installed"] = False
    except OSError:
        # Path.is_dir() suppresses PermissionError on recent Python versions;
        # inaccessible is unknown, not evidence that installation is missing.
        return {**state, "installed": None, "verify_ok": False,
                "verify_problems": ["Native runtime bundle is inaccessible; check host access"],
                "versions": [], "staging_strays": []}
    if state["installed"]:
        manifest = _read_installed_manifest(target)
        state["manifest"] = {
            key: manifest.get(key) for key in (
                "bundle_version", "installed_utc", "created_utc", "git_version",
                "production_enabled", "source_commit", "build_manifest_sha256")
        } if manifest else {"unreadable": True}
        report = verify_bundle(target)
        state["verify_ok"] = report["ok"]
        state["verify_problems"] = report["problems"]
    versions = layout["versions"]
    state["versions"] = []
    if versions.is_dir():
        for path in sorted(versions.iterdir()):
            if not path.is_dir():
                continue
            manifest = _read_installed_manifest(path)
            state["versions"].append({
                "directory": path.name,
                "bundle_version": (manifest or {}).get("bundle_version"),
                "installed_utc": (manifest or {}).get("installed_utc"),
                "verify_ok": verify_bundle(path)["ok"] if manifest else False,
            })
    state["staging_strays"] = (
        [str(p) for p in sorted(layout["native"].glob(f"{STAGING_DIR_NAME}*"))]
        if layout["native"].is_dir() else [])
    return state


def rollback(data_root: Path) -> dict:
    layout = _native_layout(data_root)
    versions = layout["versions"]
    if not versions.is_dir():
        raise InstallerError("No version archive exists to roll back to")
    candidates = []
    for path in versions.iterdir():
        if path.is_dir() and _read_installed_manifest(path) is not None:
            manifest = _read_installed_manifest(path)
            stamp = manifest.get("installed_utc") or ""
            candidates.append((stamp, path))
    if not candidates:
        raise InstallerError("The version archive holds no verifiable bundle")
    candidates.sort()
    _, archive = candidates[-1]
    # Install from a fresh copy so the archive itself stays intact as fallback.
    report = verify_bundle(archive)
    if not report["ok"]:
        raise InstallerError("Archive verification failed: " + "; ".join(report["problems"]))
    result = install_bundle(archive, data_root)
    result["rolled_back_from"] = str(archive)
    return result


def uninstall(data_root: Path, *, purge_versions: bool = False) -> dict:
    layout = _native_layout(data_root)
    target = layout["target"]
    if not target.is_dir():
        raise InstallerError("No installed native runtime bundle is present")
    native = layout["native"]
    retired = native / f".{TARGET_DIR_NAME}.uninstalled-{int(time.time())}"
    os.rename(target, retired)
    shutil.rmtree(retired)
    purged = False
    if purge_versions and layout["versions"].is_dir():
        shutil.rmtree(layout["versions"])
        purged = True
    return {"uninstalled": True, "data_root": str(data_root), "versions_purged": purged}


def _verified_target(data_root: Path) -> tuple[Path, str]:
    """The installed bundle directory plus its identity, or a refusal."""
    target = _native_layout(data_root)["target"]
    if not target.is_dir():
        raise InstallerError("No installed native runtime bundle is present")
    report = verify_bundle(target)
    if not report["ok"]:
        raise InstallerError("Installed bundle failed verification: " + "; ".join(report["problems"]))
    identity = native_gate.installed_bundle_identity(target.resolve(strict=True))
    return target, identity


def _write_gate_record(target: Path, name: str, record: dict) -> None:
    payload = (json.dumps(record, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if len(payload) > native_gate.MAX_RECORD_BYTES:
        raise InstallerError(f"Gate record {name} exceeds the size bound")
    (target / name).write_bytes(payload)


def record_attestation(data_root: Path, entries: list[tuple[str, str]]) -> dict:
    """Record passing acceptance evidence, bound to this bundle and machine.

    Each entry is ``key=report``; the key must be one of the gate's required
    evidence keys and the report must be a structured live receipt inside
    this repo, bound to this machine and bundle with complete case coverage.
    Its bytes are hashed and revalidated by the runtime gate on every check.
    """
    target, identity = _verified_target(data_root)
    required = native_gate.required_evidence(target)
    broker_sha256 = (native_gate._metadata_broker_sha256(target)
                     if "metadata_protocol_quick" in required else None)
    machine = native_gate.machine_guid()
    if not machine:
        raise InstallerError("This machine's cryptographic identity is unavailable")
    record: dict = {}
    path = target / native_gate.ATTESTATION_NAME
    if path.is_file():
        try:
            existing = _strict_read_json(path)
        except (OSError, ValueError) as exc:
            raise InstallerError(f"Existing attestation is unreadable; re-record from scratch: {exc}") from exc
        if (existing.get("schema") == native_gate.ATTESTATION_SCHEMA
                and existing.get("bundle_identity") == identity
                and existing.get("machine_guid") == machine
                and isinstance(existing.get("evidence"), dict)):
            record = dict(existing["evidence"])
        else:
            raise InstallerError(
                "Existing attestation is bound to a different bundle or machine; "
                "remove it explicitly before recording new evidence")
    updated: list[str] = []
    for key, report_ref in entries:
        if key not in required:
            raise InstallerError(
                f"Unknown evidence key {key!r}; allowed: {', '.join(required)}")
        if not isinstance(report_ref, str) or not report_ref.strip():
            raise InstallerError("Evidence report reference must be a non-empty path")
        normalized = PurePosixPath(report_ref.strip().replace("\\", "/"))
        if normalized.is_absolute() or ".." in normalized.parts:
            raise InstallerError("Evidence report must be a repo-relative path")
        report_path = ROOT / Path(*normalized.parts)
        if not report_path.is_file():
            raise InstallerError(f"Evidence report does not exist in this repo: {normalized.as_posix()}")
        from mewcode.sandbox.native_evidence import read_report, evidence_passes
        try:
            report_path = report_path.resolve(strict=True)
            if not report_path.is_relative_to(ROOT.resolve()):
                raise ValueError("Evidence report escapes the repository")
            payload, digest = read_report(ROOT / Path(*normalized.parts))
            if not evidence_passes(payload, key, identity, machine, broker_sha256=broker_sha256):
                raise ValueError("Evidence coverage, passing receipts, or bundle/machine binding is incomplete")
        except (OSError, ValueError, RuntimeError) as exc:
            raise InstallerError(f"Invalid acceptance evidence for {key}: {exc}") from exc
        record[key] = {"result": "pass", "report": str(report_path), "sha256": digest}
        updated.append(key)
    _write_gate_record(target, native_gate.ATTESTATION_NAME, {
        "schema": native_gate.ATTESTATION_SCHEMA,
        "bundle_identity": identity,
        "machine_guid": machine,
        "recorded_utc": _utc_stamp(),
        "evidence": record,
    })
    status = native_gate.native_gate_status(target.resolve(strict=True))
    return {"recorded": updated, "evidence_total": len(record),
            "evidence_required": len(required),
            "gate_state": status.state.value, "gate_reason": status.reason}


def enable_gate(data_root: Path, *, note: str) -> dict:
    """Explicit host enablement; only an attested (eligible) bundle qualifies."""
    target, identity = _verified_target(data_root)
    status = native_gate.native_gate_status(target.resolve(strict=True))
    if status.state is not native_gate.NativeGateState.ELIGIBLE:
        raise InstallerError(
            f"Refusing to enable while the gate is {status.state.value}: {status.reason}")
    machine = native_gate.machine_guid()
    _write_gate_record(target, native_gate.ENABLEMENT_NAME, {
        "schema": native_gate.ENABLEMENT_SCHEMA,
        "bundle_identity": identity,
        "machine_guid": machine,
        "enabled_utc": _utc_stamp(),
        "operator": note.strip() or "user",
    })
    final = native_gate.native_gate_status(target.resolve(strict=True))
    return {"enabled": final.ready, "gate_state": final.state.value, "gate_reason": final.reason}


def disable_gate(data_root: Path) -> dict:
    """Close the gate again; the attestation itself is left untouched."""
    target, _ = _verified_target(data_root)
    from mewcode.sandbox.native_recovery import host_gate_lock, revoke_recovery
    with host_gate_lock(target):
        revoke_recovery(target)
        path = target / native_gate.ENABLEMENT_NAME
        removed = path.is_file()
        if removed:
            path.unlink()
        final = native_gate.native_gate_status(target.resolve(strict=True))
    return {"enablement_removed": removed, "gate_state": final.state.value,
            "gate_reason": final.reason}


def assemble(build: Path, output: Path, version: str, *, helper_build: Path | None = None,
             metadata_build: Path | None = None) -> dict:
    """Build a self-contained bundle from the authorized project-local build."""
    build = probe.checked_build(build)
    manifest = probe.read_json(build / "build-manifest.json")
    probe.verify_manifest(build, manifest)
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            raise InstallerError("The output directory must be empty or absent")
    else:
        output.mkdir(parents=True)
    output = output.resolve(strict=True)
    if rt._strictly_beneath(output, build) or rt._strictly_beneath(build, output):
        raise InstallerError("The bundle output must live outside the build tree")
    helper_bundle = helper_build if helper_build is not None else build / "runtime-bundle"
    helper = rt._existing_file(helper_bundle / HELPER_NAME, "Native helper")
    helper_manifest = probe.read_json(helper_bundle / MANIFEST_NAME)
    if (helper_manifest.get("schema") != BUNDLE_SCHEMA
            or helper_manifest.get("sha256") != _sha256(helper)):
        raise InstallerError("Native helper bundle identity mismatch")
    sources = helper_manifest.get("source_sha256")
    if not isinstance(sources, dict) or not sources:
        raise InstallerError("Native helper manifest is missing source hashes")
    for name, digest in sources.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", str(name)):
            raise InstallerError("Native helper source name is not allowed")
        source = rt._existing_file(ROOT / "native/windows" / str(name), "Helper source")
        if _sha256(source) != digest:
            raise InstallerError(f"Helper source drifted from the repo: {name}")
    files: dict[str, str] = {}
    git_dir = output / "git"
    git_dir.mkdir()
    git_exe = build / "patched/git.exe"
    rt._existing_file(git_exe, "Patched Git executable")
    shutil.copyfile(git_exe, git_dir / "git.exe")
    files["git/git.exe"] = _sha256(git_exe)
    for relative in manifest["dlls"]:
        dll = rt._existing_file(build / relative, "Git runtime DLL")
        shutil.copyfile(dll, git_dir / Path(relative).name)
        files[f"git/{Path(relative).name}"] = _sha256(dll)
    shutil.copyfile(helper, output / HELPER_NAME)
    protocols = list(helper_manifest.get("protocols", ["mewcode-lpac-request-v1"]))
    sources = dict(sources)
    if metadata_build is not None:
        broker = rt._existing_file(metadata_build / "MewCode.MetadataBroker.exe", "Metadata broker")
        broker_manifest = probe.read_json(metadata_build / MANIFEST_NAME)
        broker_source = rt._existing_file(ROOT / "native/windows/MewCode.MetadataBroker.cs", "Metadata broker source")
        if (broker_manifest.get("sha256") != _sha256(broker)
                or broker_manifest.get("source_sha256") != _sha256(broker_source)):
            raise InstallerError("Metadata broker artifact or source drift")
        (output / "broker").mkdir()
        shutil.copyfile(broker, output / "broker/MewCode.MetadataBroker.exe")
        files["broker/MewCode.MetadataBroker.exe"] = _sha256(broker)
        sources[broker_source.name] = _sha256(broker_source)
        protocols.append("mewcode-metadata-broker-v1")
    bundle_manifest = {
        "schema": BUNDLE_SCHEMA,
        "sha256": _sha256(helper),
        "production_enabled": False,
        "bundle_version": version,
        "created_utc": _utc_stamp(),
        "git_version": GIT_VERSION,
        "source_commit": manifest["source_commit"],
        "sdk_commit": manifest["sdk_commit"],
        "build_manifest_sha256": _sha256(build / "build-manifest.json"),
        "source_sha256": dict(sources),
        "protocols": protocols,
        "files": dict(files),
    }
    manifest_bytes = (json.dumps(bundle_manifest, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise InstallerError("Assembled manifest exceeds the size bound")
    (output / MANIFEST_NAME).write_bytes(manifest_bytes)
    report = verify_bundle(output)
    if not report["ok"]:
        shutil.rmtree(output, ignore_errors=True)
        raise InstallerError("Assembled bundle failed self-verification: " + "; ".join(report["problems"]))
    return {"bundle": str(output), "bundle_version": version, "files": dict(files)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    assemble_parser = sub.add_parser("assemble", help="assemble a bundle from the authorized build")
    assemble_parser.add_argument("--build", required=True, type=Path)
    assemble_parser.add_argument("--output", required=True, type=Path)
    assemble_parser.add_argument("--helper-build", type=Path, help="fresh helper build; Git provenance remains fixed")
    assemble_parser.add_argument("--metadata-build", type=Path, help="bounded metadata broker build")
    assemble_parser.add_argument("--version", default=None,
                                 help="bundle version (default: date-based, e.g. 20260926.1)")

    install_parser = sub.add_parser("install", help="install a bundle into the data root")
    install_parser.add_argument("--bundle", required=True, type=Path)
    install_parser.add_argument("--data-root", type=Path, default=None)

    for name, help_text in (("status", "report the installed bundle state"),
                            ("verify", "re-verify the installed bundle"),
                            ("rollback", "restore the previous archived version")):
        sub_parser = sub.add_parser(name, help=help_text)
        sub_parser.add_argument("--data-root", type=Path, default=None)

    uninstall_parser = sub.add_parser("uninstall", help="remove the installed bundle entirely")
    uninstall_parser.add_argument("--data-root", type=Path, default=None)
    uninstall_parser.add_argument("--purge-versions", action="store_true",
                                  help="also delete the archived version history")

    attestation_parser = sub.add_parser(
        "record-attestation", help="record passing acceptance evidence for the installed bundle")
    attestation_parser.add_argument("--data-root", type=Path, default=None)
    attestation_parser.add_argument("--set", dest="entries", action="append", required=True,
                                    metavar="KEY=REPORT",
                                    help="evidence key and repo-relative report path; repeatable")

    enable_parser = sub.add_parser("enable", help="explicitly enable AUTO after full attestation")
    enable_parser.add_argument("--data-root", type=Path, default=None)
    enable_parser.add_argument("--note", default="", help="who/why this enablement happened")

    disable_parser = sub.add_parser("disable", help="close the AUTO gate again (keeps attestation)")
    disable_parser.add_argument("--data-root", type=Path, default=None)

    recovery_parser = sub.add_parser("authorize-recovery", help="allow bounded recovery of this enabled bundle")
    recovery_parser.add_argument("--data-root", type=Path, default=None)
    recovery_parser.add_argument("--note", default="user")

    args = parser.parse_args(argv)
    try:
        if args.command == "assemble":
            version = args.version
            if version is None:
                version = time.strftime("%Y%m%d", time.gmtime()) + ".1"
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}", version):
                raise InstallerError("Invalid bundle version")
            result = assemble(args.build, args.output, version, helper_build=args.helper_build,
                              metadata_build=args.metadata_build)
        elif args.command == "install":
            result = install_bundle(args.bundle, resolve_data_root(args.data_root))
        elif args.command in {"status", "verify"}:
            state = installed_state(resolve_data_root(args.data_root))
            if args.command == "status":
                target = Path(state["target"])
                gate = native_gate.native_gate_status(target)
                state["gate"] = {
                    "state": gate.state.value,
                    "reason": gate.reason,
                    "missing_evidence": list(gate.missing_evidence),
                }
                result = state
            else:
                result = {
                    "installed": state["installed"],
                    "verify_ok": state.get("verify_ok", False),
                    "verify_problems": state.get("verify_problems", ["not installed"]),
                }
        elif args.command == "rollback":
            result = rollback(resolve_data_root(args.data_root))
        elif args.command == "record-attestation":
            entries = []
            for item in args.entries:
                key, sep, report_ref = item.partition("=")
                if not sep:
                    raise InstallerError(f"--set expects KEY=REPORT, got {item!r}")
                entries.append((key.strip(), report_ref.strip()))
            result = record_attestation(resolve_data_root(args.data_root), entries)
        elif args.command == "enable":
            result = enable_gate(resolve_data_root(args.data_root), note=args.note)
        elif args.command == "disable":
            result = disable_gate(resolve_data_root(args.data_root))
        elif args.command == "authorize-recovery":
            from mewcode.sandbox.native_recovery import authorize_recovery
            target, _ = _verified_target(resolve_data_root(args.data_root))
            result = authorize_recovery(target, note=args.note)
        else:
            result = uninstall(resolve_data_root(args.data_root), purge_versions=args.purge_versions)
    except (InstallerError, ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
