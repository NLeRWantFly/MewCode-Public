"""Read-only path round-trip diagnostic using a fresh synthetic LPAC fixture.

This runner is deliberately separate from Git trials and production gates. It
creates one throwaway fixture only when invoked with ``--run`` and never copies
or launches Git. AppContainer ACL grants are limited to the exact synthetic
cwd, sibling synthetic parent, temporary directory, and compiled probe file.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import uuid
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
IS_WINDOWS = os.name == "nt"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mewcode.sandbox.appcontainer_policy import build_probe_request  # noqa: E402
from scripts import probe_appcontainer_cwd as cwd_diagnostic  # noqa: E402
from scripts import probe_windows_restricted_candidate as candidate  # noqa: E402


REPORT_SCHEMA = "mewcode-cwd-roundtrip-diagnostic-v1"
MARKER_SCHEMA = "mewcode-cwd-roundtrip-v1"
FIXTURE_PREFIX = "mewcode-lpac-probe-"
SENTINEL_NAME = ".mewcode-synthetic-fixture"
SENTINEL_CONTENT = b"mewcode-synthetic-only-v1\n"
CHILD_TIMEOUT_MS = 20_000
HOST_TIMEOUT_SECONDS = 15
HELPER_TIMEOUT_SECONDS = 35
MAX_PROCESS_OUTPUT_BYTES = 128 * 1024
CAPABILITIES = ["registryRead", "lpacInstrumentation", "lpacCom"]
FORMS = ("dos", "guid", "nt", "none")
VIEWS = ("normalized", "opened")
OPERATIONS = ("query", "reopen", "identity", "same_file")
STATUS_VALUES = {"ok", "buffer_too_small", "not_executed"}
SAME_FILE_VALUES = {"yes", "no", "not_executed"}
_WIN32_STATUS = re.compile(r"failed:win32=[0-9]+\Z")


class RoundtripInputError(ValueError):
    """The fixture inputs are not the exact synthetic paths expected here."""


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.fspath(path)))


def _strictly_beneath(path: Path, root: Path) -> bool:
    try:
        return _path_key(path) != _path_key(root) and os.path.commonpath(
            (_path_key(path), _path_key(root))
        ) == _path_key(root)
    except ValueError:
        return False


def _has_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return True
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & 0x0400
    )


def _check_ordinary_ancestry(path: Path, label: str) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise RoundtripInputError(f"{label} must be an absolute path without traversal")
    for component in (path, *path.parents):
        if _has_reparse(component):
            raise RoundtripInputError(f"{label} must not use missing or reparse-backed paths")


def _existing_directory(path: Path, label: str) -> Path:
    _check_ordinary_ancestry(path, label)
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise RoundtripInputError(f"{label} must exist") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise RoundtripInputError(f"{label} must be an existing directory")
    return resolved


def _existing_file(path: Path, label: str, *, suffix: str | None = None) -> Path:
    _check_ordinary_ancestry(path, label)
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise RoundtripInputError(f"{label} must exist") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RoundtripInputError(f"{label} must be a regular file without hard links")
    if suffix is not None and resolved.suffix.casefold() != suffix.casefold():
        raise RoundtripInputError(f"{label} must have a {suffix} suffix")
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_bytes_new(path: Path, raw: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(raw)


def _write_json_new(path: Path, value: dict[str, Any]) -> None:
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _write_bytes_new(path, raw)


def _new_fixture() -> dict[str, Path]:
    """Create a brand-new synthetic fixture beneath the project scratch dir."""
    scratch = _existing_directory(ROOT / ".tmp", "project scratch directory")
    if _path_key(scratch.parent) != _path_key(ROOT):
        raise RoundtripInputError("project scratch directory is not a direct project child")
    fixture = Path(tempfile.mkdtemp(prefix=FIXTURE_PREFIX, dir=scratch))
    if not fixture.name.casefold().startswith(FIXTURE_PREFIX):
        raise RoundtripInputError("new fixture name is not recognized")

    workspace = fixture / "workspace"
    cwd = workspace / "中文 synthetic-cwd"
    parent = workspace / "synthetic-parent"
    temp = cwd / "temp"
    profile = fixture / "profile"
    paths = {
        "fixture": fixture,
        "workspace": workspace,
        "cwd": cwd,
        "parent": parent,
        "temp": temp,
        "profile": profile,
        "profile_local": profile / "AppData" / "Local",
        "profile_roaming": profile / "AppData" / "Roaming",
        "helper_output": fixture / "compiled-helper",
        "probe_output": fixture / "compiled",
    }
    for name in ("workspace", "cwd", "parent", "temp", "profile_local", "profile_roaming"):
        paths[name].mkdir(parents=True, exist_ok=False)
    sentinel = fixture / SENTINEL_NAME
    _write_bytes_new(sentinel, SENTINEL_CONTENT)
    if sentinel.stat().st_size != len(SENTINEL_CONTENT):
        raise RoundtripInputError("new fixture sentinel did not persist exactly")
    paths["sentinel"] = sentinel
    paths["request_ac"] = fixture / "roundtrip-ac.request.json"
    paths["request_lpac"] = fixture / "roundtrip-lpac.request.json"
    paths["report"] = fixture / "roundtrip.report.json"
    paths["host_stdout"] = fixture / "host.stdout.bin"
    paths["host_stderr"] = fixture / "host.stderr.bin"
    for mode in ("ac", "lpac"):
        paths[f"helper_stdout_{mode}"] = fixture / f"helper-{mode}.stdout.bin"
        paths[f"helper_stderr_{mode}"] = fixture / f"helper-{mode}.stderr.bin"
        paths[f"helper_receipt_{mode}"] = fixture / f"helper-{mode}.receipt.json"
    return paths


def _validate_new_fixture(paths: dict[str, Path]) -> None:
    fixture = _existing_directory(paths["fixture"], "fixture")
    scratch = _existing_directory(ROOT / ".tmp", "project scratch directory")
    if _path_key(fixture.parent) != _path_key(scratch):
        raise RoundtripInputError("fixture must be a direct child of project .tmp")
    if not fixture.name.casefold().startswith(FIXTURE_PREFIX):
        raise RoundtripInputError("fixture name is not a recognized synthetic fixture")
    sentinel = _existing_file(fixture / SENTINEL_NAME, "fixture sentinel")
    if sentinel.read_bytes() != SENTINEL_CONTENT:
        raise RoundtripInputError("fixture sentinel content does not match")
    if _path_key(paths["cwd"]) != _path_key(fixture / "workspace" / "中文 synthetic-cwd"):
        raise RoundtripInputError("cwd is not the expected synthetic cwd")
    if _path_key(paths["parent"]) != _path_key(fixture / "workspace" / "synthetic-parent"):
        raise RoundtripInputError("parent is not the expected synthetic parent")
    if _path_key(paths["cwd"]) == _path_key(paths["parent"]):
        raise RoundtripInputError("cwd and parent must be different directories")
    for name in ("workspace", "cwd", "parent", "temp", "profile_local", "profile_roaming"):
        _existing_directory(paths[name], name)


def _compile_roundtrip_probe(compiler: Path, output_directory: Path) -> tuple[Path, dict[str, str | int]]:
    """Compile only the fixed CwdAccessProbe source into this fresh fixture."""
    source = _existing_file(cwd_diagnostic.SOURCE_PATH, "fixed cwd round-trip source", suffix=".cs")
    output_directory.mkdir(parents=True, exist_ok=False)
    target = output_directory / "MewCode.CwdAccessProbe.exe"
    source_hash_before = _sha256_file(source)
    command = [
        str(compiler), "/nologo", "/platform:x64", "/out:" + str(target), str(source),
    ]
    result = subprocess.run(
        command,
        cwd=str(ROOT),
        capture_output=True,
        timeout=30,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    source_hash_after = _sha256_file(source)
    if source_hash_before != source_hash_after:
        raise RuntimeError("round-trip probe source changed during compilation")
    if result.returncode != 0 or not target.is_file():
        raise RuntimeError("round-trip probe compilation failed")
    executable = _existing_file(target, "compiled round-trip probe", suffix=".exe")
    return executable, {
        "exit_code": result.returncode,
        "source_sha256_before": source_hash_before,
        "source_sha256_after": source_hash_after,
        "executable_sha256": _sha256_file(executable),
    }


def _status_value(value: str) -> bool:
    return value in STATUS_VALUES or bool(_WIN32_STATUS.fullmatch(value))


def _status_after(status: str) -> bool:
    return status == "not_executed" or _status_value(status)


def _roundtrip_marker_keys() -> tuple[str, ...]:
    keys = ["roundtrip_schema", "roundtrip_root_identity"]
    for form in FORMS:
        for view in VIEWS:
            for operation in OPERATIONS:
                keys.append(f"roundtrip_{form}_{view}_{operation}")
    for view in VIEWS:
        keys.append(f"roundtrip_none_{view}_paired_source")
    keys.extend((
        "roundtrip_parent_open", "roundtrip_parent_identity", "roundtrip_parent_same_file",
    ))
    return tuple(keys)


ROUNDTRIP_MARKER_KEYS = _roundtrip_marker_keys()


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError("non-finite JSON number")


def parse_roundtrip_markers(stdout: str | bytes | None) -> dict[str, Any]:
    """Strictly validate the complete fixed marker protocol and call chain."""
    invalid_utf8 = False
    oversized = False
    if isinstance(stdout, bytes):
        oversized = len(stdout) > MAX_PROCESS_OUTPUT_BYTES
        try:
            text = stdout.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            text = ""
            invalid_utf8 = True
    elif isinstance(stdout, str):
        try:
            raw = stdout.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raw = b""
            invalid_utf8 = True
        oversized = len(raw) > MAX_PROCESS_OUTPUT_BYTES
        text = stdout if not invalid_utf8 else ""
    else:
        text = ""
        invalid_utf8 = stdout is not None

    values: dict[str, list[str]] = {}
    malformed: list[str] = []
    unknown: list[str] = []
    if text:
        for line in text.splitlines():
            if not line or "=" not in line:
                malformed.append(line[:120] if line else "<empty>")
                continue
            key, value = line.split("=", 1)
            if key not in ROUNDTRIP_MARKER_KEYS:
                unknown.append(key[:120])
                continue
            values.setdefault(key, []).append(value)

    duplicates = [key for key, items in values.items() if len(items) != 1]
    missing = [key for key in ROUNDTRIP_MARKER_KEYS if key not in values]
    parsed = {key: items[0] for key, items in values.items() if len(items) == 1}
    invalid_values: list[str] = []
    if parsed.get("roundtrip_schema") != MARKER_SCHEMA:
        invalid_values.append("roundtrip_schema")
    for key, value in parsed.items():
        if key in {"roundtrip_schema"}:
            continue
        if key.endswith("_same_file") or key == "roundtrip_parent_same_file":
            if value not in SAME_FILE_VALUES:
                invalid_values.append(key)
        elif key == "roundtrip_none_normalized_paired_source":
            if value not in {"nt_normalized", "not_executed"}:
                invalid_values.append(key)
        elif key == "roundtrip_none_opened_paired_source":
            if value not in {"nt_opened", "not_executed"}:
                invalid_values.append(key)
        elif not _status_value(value):
            invalid_values.append(key)

    inconsistent: list[str] = []
    for view in VIEWS:
        paired = parsed.get(f"roundtrip_none_{view}_paired_source")
        nt_query = parsed.get(f"roundtrip_nt_{view}_query")
        if paired == f"nt_{view}" and nt_query != "ok":
            inconsistent.append(f"roundtrip_none_{view}_paired_source:nt_query_missing")
    root_identity = parsed.get("roundtrip_root_identity")
    if root_identity is not None and root_identity != "ok":
        for form in FORMS:
            for view in VIEWS:
                same = parsed.get(f"roundtrip_{form}_{view}_same_file")
                if same is not None and same != "not_executed":
                    inconsistent.append(f"roundtrip_{form}_{view}_same_file:root_identity_unavailable")
        parent_same = parsed.get("roundtrip_parent_same_file")
        if parent_same is not None and parent_same != "not_executed":
            inconsistent.append("roundtrip_parent_same_file:root_identity_unavailable")
    for form in FORMS:
        for view in VIEWS:
            prefix = f"roundtrip_{form}_{view}_"
            query = parsed.get(prefix + "query")
            reopen = parsed.get(prefix + "reopen")
            identity = parsed.get(prefix + "identity")
            same_file = parsed.get(prefix + "same_file")
            if query is None or reopen is None or identity is None or same_file is None:
                continue
            if query != "ok":
                if (reopen, identity, same_file) != ("not_executed",) * 3:
                    inconsistent.append(prefix.rstrip("_"))
                if form == "none" and parsed.get(f"roundtrip_none_{view}_paired_source") != "not_executed":
                    inconsistent.append(f"roundtrip_none_{view}_paired_source:source_mismatch")
                continue
            if reopen == "not_executed":
                if (identity, same_file) != ("not_executed", "not_executed"):
                    inconsistent.append(prefix.rstrip("_") + ":reopen_not_attempted_chain")
                if form == "none" and parsed.get(f"roundtrip_none_{view}_paired_source") != "not_executed":
                    inconsistent.append(f"roundtrip_none_{view}_paired_source:source_mismatch")
                continue
            if reopen != "ok":
                if (identity, same_file) != ("not_executed", "not_executed"):
                    inconsistent.append(prefix.rstrip("_") + ":reopen_failed_chain")
                continue
            if identity == "not_executed":
                inconsistent.append(prefix.rstrip("_") + ":reopen_without_identity")
            elif identity != "ok" and same_file != "not_executed":
                inconsistent.append(prefix.rstrip("_") + ":identity_failed_chain")
            elif identity == "ok" and same_file == "not_executed" and parsed.get("roundtrip_root_identity") == "ok":
                inconsistent.append(prefix.rstrip("_") + ":identity_without_comparison")

            if form == "none":
                paired_key = f"roundtrip_none_{view}_paired_source"
                expected = f"nt_{view}" if reopen != "not_executed" else "not_executed"
                if parsed.get(paired_key) != expected:
                    inconsistent.append(paired_key + ":source_mismatch")

    parent_open = parsed.get("roundtrip_parent_open")
    parent_identity = parsed.get("roundtrip_parent_identity")
    parent_same = parsed.get("roundtrip_parent_same_file")
    if parent_open is not None and parent_identity is not None and parent_same is not None:
        if parent_open != "ok":
            if (parent_identity, parent_same) != ("not_executed", "not_executed"):
                inconsistent.append("roundtrip_parent:open_failed_chain")
        elif parent_identity == "not_executed":
            inconsistent.append("roundtrip_parent:open_without_identity")
        elif parent_identity != "ok" and parent_same != "not_executed":
            inconsistent.append("roundtrip_parent:identity_failed_chain")
        elif parent_identity == "ok" and parent_same == "not_executed" and parsed.get("roundtrip_root_identity") == "ok":
            inconsistent.append("roundtrip_parent:identity_without_comparison")

    protocol_valid = not (
        invalid_utf8 or oversized or malformed or unknown or duplicates or missing
        or invalid_values or inconsistent
    )
    return {
        "schema": MARKER_SCHEMA,
        "protocol_valid": protocol_valid,
        "invalid_utf8": invalid_utf8,
        "oversized": oversized,
        "marker_count": len(parsed),
        "marker_values": parsed,
        "missing_markers": missing,
        "duplicate_markers": duplicates,
        "unknown_keys": unknown,
        "malformed_lines": malformed,
        "invalid_values": invalid_values,
        "inconsistent_chains": inconsistent,
    }


def _positive_control_passed(protocol: dict[str, Any], process: dict[str, Any]) -> bool:
    if not _process_is_complete(process) or process.get("exit_code") != 0:
        return False
    if not protocol.get("protocol_valid"):
        return False
    values = protocol["marker_values"]
    if values.get("roundtrip_root_identity") != "ok":
        return False
    for form in FORMS:
        for view in VIEWS:
            prefix = f"roundtrip_{form}_{view}_"
            if any(values.get(prefix + op) != "ok" for op in ("query", "reopen", "identity")):
                return False
            if values.get(prefix + "same_file") != "yes":
                return False
    return (
        values.get("roundtrip_none_normalized_paired_source") == "nt_normalized"
        and values.get("roundtrip_none_opened_paired_source") == "nt_opened"
        and values.get("roundtrip_parent_open") == "ok"
        and values.get("roundtrip_parent_identity") == "ok"
        and values.get("roundtrip_parent_same_file") == "no"
    )


def _restricted_roundtrip_passed(protocol: dict[str, Any]) -> bool:
    if not protocol.get("protocol_valid"):
        return False
    values = protocol["marker_values"]
    if values.get("roundtrip_root_identity") != "ok":
        return False
    for form in FORMS:
        for view in VIEWS:
            prefix = f"roundtrip_{form}_{view}_"
            if any(values.get(prefix + op) != "ok" for op in ("query", "reopen", "identity")):
                return False
            if values.get(prefix + "same_file") != "yes":
                return False
    return (
        values.get("roundtrip_none_normalized_paired_source") == "nt_normalized"
        and values.get("roundtrip_none_opened_paired_source") == "nt_opened"
        and values.get("roundtrip_parent_open") == "ok"
        and values.get("roundtrip_parent_identity") == "ok"
        and values.get("roundtrip_parent_same_file") == "no"
    )


def _nt_assertion_passed(protocol: dict[str, Any]) -> bool:
    if not protocol.get("protocol_valid"):
        return False
    values = protocol["marker_values"]
    if values.get("roundtrip_root_identity") != "ok":
        return False
    for view in VIEWS:
        prefix = f"roundtrip_nt_{view}_"
        if any(values.get(prefix + op) != "ok" for op in ("query", "reopen", "identity")):
            return False
        if values.get(prefix + "same_file") != "yes":
            return False
    return (
        values.get("roundtrip_parent_open") == "ok"
        and values.get("roundtrip_parent_identity") == "ok"
        and values.get("roundtrip_parent_same_file") == "no"
    )


def _process_is_complete(process: dict[str, Any]) -> bool:
    exit_code = process.get("exit_code")
    stdout = process.get("stdout")
    stderr = process.get("stderr")
    return (
        type(exit_code) is int
        and process.get("start_error_type") is None
        and process.get("timed_out") is False
        and process.get("cancelled") is False
        and isinstance(stdout, bytes)
        and isinstance(stderr, bytes)
        and len(stdout) <= MAX_PROCESS_OUTPUT_BYTES
        and len(stderr) <= MAX_PROCESS_OUTPUT_BYTES
    )


def _receipt_facts(invocation: dict[str, Any], protocol: dict[str, Any], *, lpac: bool) -> dict[str, Any]:
    receipt = invocation.get("receipt")
    if not isinstance(receipt, dict):
        receipt = None
    helper_code = invocation.get("exit_code")
    helper_code_valid = type(helper_code) is int
    if receipt is None:
        return {
            "helper_exit_code": helper_code if helper_code_valid else None,
            "helper_exit_code_valid": helper_code_valid,
            "helper_timed_out": invocation.get("timed_out") if type(invocation.get("timed_out")) is bool else None,
            "helper_cancelled": invocation.get("cancelled") if type(invocation.get("cancelled")) is bool else None,
            "receipt_valid": False,
            "receipt_error_type": invocation.get("receipt_error_type"),
            "child_exit_code": None,
            "identity_verified": False,
            "capabilities_verified": False,
            "cleanup_confirmed": False,
            "child_complete": False,
            "child_timed_out": None,
            "child_cancelled": None,
            "protocol": protocol,
        }

    child_code = receipt.get("exit_code")
    method = receipt.get("lpac_verification_method")
    capabilities = receipt.get("capabilities")
    identity_verified = (
        receipt.get("appcontainer_verified") is True
        and receipt.get("lpac_verified") is True
        and method in {"access_check", "token_query"}
    )
    if method == "access_check":
        expected_mask = 2 if lpac else 3
        identity_verified = identity_verified and type(receipt.get("lpac_access_mask")) is int and receipt.get("lpac_access_mask") == expected_mask
    expected_capabilities = CAPABILITIES if lpac else []
    capabilities_verified = type(capabilities) is list and capabilities == expected_capabilities
    cleanup_values = [receipt.get(key) for key in ("job_cleanup_ok", "profile_cleanup_ok", "acl_cleanup_ok")]
    cleanup_confirmed = all(value is True for value in cleanup_values)
    child_stdout = receipt.get("stdout")
    child_stderr = receipt.get("stderr")
    child_timed_out = receipt.get("timed_out") if type(receipt.get("timed_out")) is bool else None
    child_cancelled = receipt.get("cancelled") if type(receipt.get("cancelled")) is bool else None
    child_complete = (
        type(child_code) is int
        and child_code == 0
        and type(receipt.get("timed_out")) is bool and receipt["timed_out"] is False
        and type(receipt.get("cancelled")) is bool and receipt["cancelled"] is False
        and type(receipt.get("stdout_truncated")) is bool and receipt["stdout_truncated"] is False
        and type(receipt.get("stderr_truncated")) is bool and receipt["stderr_truncated"] is False
        and isinstance(child_stdout, str)
        and isinstance(child_stderr, str)
        and len(child_stdout.encode("utf-8", errors="replace")) <= MAX_PROCESS_OUTPUT_BYTES
        and len(child_stderr.encode("utf-8", errors="replace")) <= MAX_PROCESS_OUTPUT_BYTES
        and receipt.get("error") == ""
        and protocol.get("protocol_valid") is True
    )
    receipt_valid = (
        helper_code_valid
        and invocation.get("timed_out") is False
        and invocation.get("cancelled") is False
        and invocation.get("start_error_type") is None
        and invocation.get("output_oversized") is False
        and type(child_code) is int
        and helper_code == child_code
    )
    return {
        "helper_exit_code": helper_code if helper_code_valid else None,
        "helper_exit_code_valid": helper_code_valid,
        "helper_timed_out": invocation.get("timed_out") if type(invocation.get("timed_out")) is bool else None,
        "helper_cancelled": invocation.get("cancelled") if type(invocation.get("cancelled")) is bool else None,
        "receipt_valid": receipt_valid,
        "receipt_error_type": invocation.get("receipt_error_type"),
        "child_exit_code": child_code if type(child_code) is int else None,
        "identity_verified": bool(identity_verified),
        "capabilities_verified": capabilities_verified,
        "cleanup_confirmed": cleanup_confirmed,
        "job_cleanup_ok": receipt.get("job_cleanup_ok") if type(receipt.get("job_cleanup_ok")) is bool else None,
        "profile_cleanup_ok": receipt.get("profile_cleanup_ok") if type(receipt.get("profile_cleanup_ok")) is bool else None,
        "acl_cleanup_ok": receipt.get("acl_cleanup_ok") if type(receipt.get("acl_cleanup_ok")) is bool else None,
        "child_complete": child_complete,
        "child_timed_out": child_timed_out,
        "child_cancelled": child_cancelled,
        "protocol": protocol,
    }


async def _invoke_helper(helper: Path, request_path: Path, *, cwd: Path, environment: dict[str, str]) -> dict[str, Any]:
    result = await cwd_diagnostic._run_process(
        [str(helper), str(request_path)],
        cwd=cwd,
        environment=environment,
        timeout=HELPER_TIMEOUT_SECONDS,
    )
    stdout = result.get("stdout", b"")
    stderr = result.get("stderr", b"")
    invocation = {
        "exit_code": result.get("exit_code"),
        "start_error_type": result.get("start_error_type"),
        "timed_out": result.get("timed_out"),
        "cancelled": result.get("cancelled"),
        "stdout": stdout if isinstance(stdout, bytes) else b"",
        "stderr": stderr if isinstance(stderr, bytes) else b"",
        "output_oversized": (
            not isinstance(stdout, bytes) or not isinstance(stderr, bytes)
            or len(stdout) > MAX_PROCESS_OUTPUT_BYTES or len(stderr) > MAX_PROCESS_OUTPUT_BYTES
        ),
        "receipt": None,
        "receipt_error_type": None,
    }
    if not isinstance(stdout, bytes):
        invocation["receipt_error_type"] = "stdout_not_bytes"
        return invocation
    if len(stdout) > MAX_PROCESS_OUTPUT_BYTES:
        invocation["receipt_error_type"] = "stdout_too_large"
        return invocation
    try:
        text = stdout.decode("utf-8", errors="strict")
        receipt = json.loads(
            text, object_pairs_hook=_strict_json_object, parse_constant=_reject_nonfinite_json,
        )
        if isinstance(receipt, dict):
            invocation["receipt"] = receipt
        else:
            invocation["receipt_error_type"] = "receipt_not_object"
    except UnicodeDecodeError:
        invocation["receipt_error_type"] = "invalid_utf8"
    except json.JSONDecodeError:
        invocation["receipt_error_type"] = "invalid_json"
    except ValueError:
        invocation["receipt_error_type"] = "invalid_json_structure"
    return invocation


def _new_report(paths: dict[str, Path], run_id: str) -> dict[str, Any]:
    return {
        "schema": REPORT_SCHEMA,
        "run_id": run_id,
        "production_authorized": False,
        "infrastructure_complete": False,
        "roundtrip_candidate_passed": False,
        "roundtrip_assertion_passed": False,
        "git_compatibility_status": "NOT_TESTED",
        "git_repository_copied": False,
        "status": "incomplete",
        "fixture": {
            "recognized_synthetic_fixture": True,
            "cwd_relative": "workspace/中文 synthetic-cwd",
            "parent_relative": "workspace/synthetic-parent",
            "different_directories": True,
            "sentinel_verified": False,
        },
        "scope": {
            "read_grants": ["workspace/中文 synthetic-cwd", "workspace/synthetic-parent"],
            "write_grants": ["workspace/中文 synthetic-cwd/temp"],
            "execute_grants": ["compiled/MewCode.CwdAccessProbe.exe"],
            "profile_grants": [],
            "runtime_capabilities": CAPABILITIES,
            "system_paths_granted": False,
            "ancestor_paths_granted": False,
            "git_invoked": False,
            "host_acl_trial_invoked": False,
        },
        "artifacts": {
            "requests": {"appcontainer": paths["request_ac"].name, "lpac": paths["request_lpac"].name},
            "host_stdout": paths["host_stdout"].name,
            "host_stderr": paths["host_stderr"].name,
            "helper": {
                mode: {"stdout": None, "stderr": None, "receipt": None}
                for mode in ("appcontainer", "lpac")
            },
            "report": paths["report"].name,
        },
        "compiler": {},
        "helper_binary": {},
        "probe_binary_integrity": {
            "source_sha256_before": None,
            "source_sha256_after": None,
            "compiled_sha256": None,
            "host_before_sha256": None,
            "host_after_sha256": None,
            "helper_before_sha256": None,
            "helper_after_sha256": None,
            "final_sha256": None,
            "unchanged_through_run": None,
        },
        "request_integrity": {
            mode: {"sha256_before_run": None, "sha256_after_host": None,
                   "sha256_after_helper": None, "unchanged_through_run": None}
            for mode in ("appcontainer", "lpac")
        },
        "acl_cleanup_evidence": {
            "source": "helper_receipt_only",
            "independent_descriptor_snapshot": False,
            "exact_restore_verified": None,
        },
        "host_control": {
            "exit_code": None,
            "timed_out": None,
            "cancelled": None,
            "start_error_type": None,
            "stdout_bytes": None,
            "stderr_bytes": None,
            "protocol": None,
            "positive_control_passed": False,
        },
        "restricted_observations": {
            mode: {
                "helper_exit_code": None, "helper_timed_out": None, "helper_cancelled": None,
                "receipt_valid": False, "identity_verified": False, "capabilities_verified": False,
                "cleanup_confirmed": False, "job_cleanup_ok": None, "profile_cleanup_ok": None,
                "acl_cleanup_ok": None, "child_exit_code": None, "child_timed_out": None,
                "child_cancelled": None, "protocol": None, "roundtrip_passed": False,
                "nt_assertion_passed": False,
            }
            for mode in ("appcontainer", "lpac")
        },
        "failure_reasons": [],
    }


async def run_roundtrip_diagnostic() -> dict[str, Any]:
    """Run one fresh host control, then ordinary AppContainer and LPAC."""
    paths = _new_fixture()
    run_id = uuid.uuid4().hex
    report = _new_report(paths, run_id)
    request_hashes: dict[str, str] = {}
    probe_hashes: list[str | None] = []
    helper_hash_before: str | None = None
    helper_hash_after: str | None = None
    host_process: dict[str, Any] | None = None
    helper_facts: dict[str, dict[str, Any]] = {}
    helper_hash_at_build: str | None = None

    try:
        _validate_new_fixture(paths)
        report["fixture"]["sentinel_verified"] = True
        if not IS_WINDOWS:
            raise RuntimeError("round-trip execution requires Windows")

        helper, helper_build = candidate.compile_probe_helper(paths["helper_output"])
        helper = _existing_file(helper, "compiled AppContainer probe helper", suffix=".exe")
        helper_hash_before = _sha256_file(helper)
        helper_hash_at_build = helper_hash_before
        report["helper_binary"] = {
            "path_relative": helper.relative_to(paths["fixture"]).as_posix(),
            "sha256_from_compiler": helper_build.get("helper_sha256"),
            "sha256_before": helper_hash_before,
            "sha256_after": None,
            "unchanged": None,
        }
        if (not isinstance(helper_build.get("helper_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", helper_build["helper_sha256"])
                or helper_build["helper_sha256"] != helper_hash_before):
            raise RoundtripInputError("compiled helper hash does not match the build receipt")

        compiler = candidate.checked_executable(Path(str(helper_build["compiler_path"])), "fixed C# compiler")
        probe, compile_facts = _compile_roundtrip_probe(compiler, paths["probe_output"])
        report["compiler"] = {
            "path_relative_to_system_root": "Microsoft.NET/Framework64/v4.0.30319/csc.exe",
            "helper_compile_exit_code": helper_build.get("compiler_exit_code"),
            "probe_compile_exit_code": compile_facts["exit_code"],
        }
        report["probe_binary_integrity"].update({
            "source_sha256_before": compile_facts["source_sha256_before"],
            "source_sha256_after": compile_facts["source_sha256_after"],
            "compiled_sha256": compile_facts["executable_sha256"],
        })
        paths["executable"] = probe
        probe_hashes.append(_sha256_file(probe))

        requests: dict[str, dict[str, Any]] = {}
        for mode, lpac in (("appcontainer", False), ("lpac", True)):
            request = build_probe_request(
                fixture_root=paths["fixture"], executable=probe,
                arguments=["--roundtrip-v1", str(paths["parent"])], cwd=paths["cwd"],
                read_paths=[paths["cwd"], paths["parent"]], write_paths=[paths["temp"]],
                execute_paths=[probe], timeout_ms=CHILD_TIMEOUT_MS, lpac=lpac,
                runtime_registry_read=lpac, runtime_instrumentation=lpac, runtime_com=lpac,
            )
            if (request["arguments"] != ["--roundtrip-v1", str(paths["parent"])]
                    or request["read_paths"] != [str(paths["cwd"]), str(paths["parent"])]
                    or request["write_paths"] != [str(paths["temp"])]
                    or request["execute_paths"] != [str(probe)]
                    or request["lpac"] is not lpac
                    or request["runtime_registry_read"] is not lpac
                    or request["runtime_instrumentation"] is not lpac
                    or request["runtime_com"] is not lpac):
                raise RoundtripInputError("fixed AppContainer request contract changed")
            requests[mode] = request
            raw_request = (json.dumps(request, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            request_path = paths["request_ac" if mode == "appcontainer" else "request_lpac"]
            _write_bytes_new(request_path, raw_request)
            request_hashes[mode] = _sha256_file(request_path)
            report["request_integrity"][mode]["sha256_before_run"] = request_hashes[mode]
        request = requests["lpac"]
        if requests["appcontainer"]["environment"] != request["environment"]:
            raise RoundtripInputError("AppContainer and LPAC child environments differ")

        report["probe_binary_integrity"]["host_before_sha256"] = _sha256_file(probe)
        probe_hashes.append(report["probe_binary_integrity"]["host_before_sha256"])
        host_process = await cwd_diagnostic._run_process(
            [str(probe), *request["arguments"]],
            cwd=paths["cwd"],
            environment=request["environment"],
            timeout=HOST_TIMEOUT_SECONDS,
        )
        host_stdout = host_process.get("stdout", b"")
        host_stderr = host_process.get("stderr", b"")
        if not isinstance(host_stdout, bytes):
            host_stdout = b""
        if not isinstance(host_stderr, bytes):
            host_stderr = b""
        _write_bytes_new(paths["host_stdout"], host_stdout)
        _write_bytes_new(paths["host_stderr"], host_stderr)
        host_protocol = parse_roundtrip_markers(host_stdout)
        report["host_control"].update({
            "exit_code": host_process.get("exit_code") if type(host_process.get("exit_code")) is int else None,
            "timed_out": host_process.get("timed_out") if type(host_process.get("timed_out")) is bool else None,
            "cancelled": host_process.get("cancelled") if type(host_process.get("cancelled")) is bool else None,
            "start_error_type": host_process.get("start_error_type"),
            "stdout_bytes": len(host_stdout),
            "stderr_bytes": len(host_stderr),
            "protocol": host_protocol,
        })
        report["probe_binary_integrity"]["host_after_sha256"] = _sha256_file(probe)
        probe_hashes.append(report["probe_binary_integrity"]["host_after_sha256"])
        request_after_host = {
            mode: _sha256_file(paths["request_ac" if mode == "appcontainer" else "request_lpac"])
            for mode in ("appcontainer", "lpac")
        }
        for mode in request_hashes:
            report["request_integrity"][mode]["sha256_after_host"] = request_after_host[mode]

        host_unchanged = (
            request_after_host == request_hashes
            and report["probe_binary_integrity"]["host_after_sha256"] == report["probe_binary_integrity"]["compiled_sha256"]
        )
        positive = host_unchanged and _positive_control_passed(host_protocol, host_process)
        report["host_control"]["positive_control_passed"] = positive
        if not host_unchanged:
            report["failure_reasons"].append("host_control_integrity_or_security_snapshot_changed")
        elif not _process_is_complete(host_process):
            report["failure_reasons"].append("host_control_process_incomplete")
        elif not host_protocol.get("protocol_valid"):
            report["failure_reasons"].append("host_control_protocol_invalid")
        elif not positive:
            report["failure_reasons"].append("host_control_roundtrip_not_positive")

        if positive:
            for mode, lpac in (("appcontainer", False), ("lpac", True)):
                if (_sha256_file(probe) != report["probe_binary_integrity"]["compiled_sha256"]
                        or _sha256_file(helper) != helper_hash_at_build):
                    report["failure_reasons"].append(f"binary_changed_before_{mode}")
                    break
                request_path = paths["request_ac" if mode == "appcontainer" else "request_lpac"]
                if _sha256_file(request_path) != request_hashes[mode]:
                    report["failure_reasons"].append(f"request_changed_before_{mode}")
                    break
                report["probe_binary_integrity"]["helper_before_sha256"] = _sha256_file(probe)
                invocation = await _invoke_helper(helper, request_path, cwd=paths["fixture"],
                                                  environment=requests[mode]["environment"])
                out_path = paths[f"helper_stdout_{'ac' if mode == 'appcontainer' else 'lpac'}"]
                err_path = paths[f"helper_stderr_{'ac' if mode == 'appcontainer' else 'lpac'}"]
                receipt_path = paths[f"helper_receipt_{'ac' if mode == 'appcontainer' else 'lpac'}"]
                _write_bytes_new(out_path, invocation["stdout"])
                _write_bytes_new(err_path, invocation["stderr"])
                mode_artifacts = report["artifacts"]["helper"][mode]
                mode_artifacts.update({"stdout": out_path.name, "stderr": err_path.name})
                receipt = invocation.get("receipt")
                protocol = parse_roundtrip_markers(receipt.get("stdout")
                                                   if isinstance(receipt, dict) else None)
                if isinstance(receipt, dict):
                    _write_bytes_new(receipt_path, invocation["stdout"])
                    mode_artifacts["receipt"] = receipt_path.name
                facts = _receipt_facts(invocation, protocol, lpac=lpac)
                facts.pop("protocol", None)
                facts["protocol"] = protocol
                facts["roundtrip_passed"] = (
                    facts["receipt_valid"] and facts["identity_verified"]
                    and facts["capabilities_verified"] and facts["cleanup_confirmed"]
                    and facts["child_complete"] and _restricted_roundtrip_passed(protocol)
                )
                facts["nt_assertion_passed"] = (
                    facts["receipt_valid"] and facts["identity_verified"]
                    and facts["capabilities_verified"] and facts["cleanup_confirmed"]
                    and facts["child_complete"] and _nt_assertion_passed(protocol)
                )
                report["restricted_observations"][mode].update(facts)
                helper_facts[mode] = facts
                report["request_integrity"][mode]["sha256_after_helper"] = _sha256_file(request_path)
                probe_after = _sha256_file(probe) if probe.is_file() else None
                probe_hashes.append(probe_after)
                report["probe_binary_integrity"]["helper_after_sha256"] = probe_after
                helper_now = _sha256_file(helper) if helper.is_file() else None
                report["helper_binary"]["sha256_after"] = helper_now
                report["helper_binary"]["unchanged"] = helper_now == helper_hash_at_build
                # An uncertain cleanup receipt stops the next mode; never start
                # another profile while prior Job/ACL cleanup is unknown.
                if not (facts["receipt_valid"] and facts["cleanup_confirmed"]
                        and facts["child_complete"] and helper_now == helper_hash_at_build
                        and probe_after == report["probe_binary_integrity"]["compiled_sha256"]
                        and report["request_integrity"][mode]["sha256_after_helper"] == request_hashes[mode]):
                    report["failure_reasons"].append(f"{mode}_cleanup_or_integrity_unconfirmed")
                    break
        else:
            report["failure_reasons"].append("restricted_observation_skipped_after_host_control")

    except asyncio.CancelledError:
        report["failure_reasons"].append("cancelled_cleanup_unconfirmed")
        # The fixture and all evidence are intentionally retained. A cancelled
        # helper never proves Job, profile, or ACL cleanup.
    except (OSError, RuntimeError, ValueError, RoundtripInputError, subprocess.TimeoutExpired) as exc:
        report["failure_reasons"].append("execution_error_" + type(exc).__name__)
    finally:
        if helper_hash_at_build is not None:
            try:
                helper_hash_after = _sha256_file(helper)
            except (OSError, UnboundLocalError):
                helper_hash_after = None
            report["helper_binary"]["sha256_after"] = helper_hash_after
            report["helper_binary"]["unchanged"] = helper_hash_after == helper_hash_at_build
        if "executable" in paths:
            try:
                final_probe_hash = _sha256_file(paths["executable"])
            except OSError:
                final_probe_hash = None
            report["probe_binary_integrity"]["final_sha256"] = final_probe_hash
            probe_hashes.append(final_probe_hash)
        for mode in ("appcontainer", "lpac"):
            request_path = paths["request_ac" if mode == "appcontainer" else "request_lpac"]
            try:
                current_hash = _sha256_file(request_path)
            except OSError:
                current_hash = None
            if report["request_integrity"][mode]["sha256_after_helper"] is None:
                report["request_integrity"][mode]["sha256_after_helper"] = current_hash
            baseline = request_hashes.get(mode)
            evidence = report["request_integrity"][mode]
            evidence["unchanged_through_run"] = bool(
                baseline is not None and current_hash == baseline
                and evidence["sha256_after_host"] == baseline
                and evidence["sha256_after_helper"] == baseline
            )
        observed_probe_hashes = [value for value in probe_hashes if value is not None]
        if observed_probe_hashes:
            report["probe_binary_integrity"]["unchanged_through_run"] = (
                len(observed_probe_hashes) == len(probe_hashes)
                and all(value == report["probe_binary_integrity"]["compiled_sha256"] for value in observed_probe_hashes)
            )

        host_complete = (
            host_process is not None
            and report["host_control"].get("protocol") is not None
            and _process_is_complete(host_process)
            and report["host_control"]["protocol"].get("protocol_valid") is True
            and report["probe_binary_integrity"].get("unchanged_through_run") is True
            and all(report["request_integrity"][mode].get("unchanged_through_run") is True
                    for mode in ("appcontainer", "lpac"))
            and report["host_control"].get("positive_control_passed") is True
        )
        modes_complete = all(
            mode in helper_facts
            and helper_facts[mode].get("receipt_valid") is True
            and helper_facts[mode].get("identity_verified") is True
            and helper_facts[mode].get("capabilities_verified") is True
            and helper_facts[mode].get("cleanup_confirmed") is True
            and helper_facts[mode].get("child_complete") is True
            and helper_facts[mode].get("helper_timed_out") is False
            and helper_facts[mode].get("helper_cancelled") is False
            for mode in ("appcontainer", "lpac")
        )
        binaries_unchanged = (
            report["helper_binary"].get("unchanged") is True
            and report["probe_binary_integrity"].get("unchanged_through_run") is True
            and not report["failure_reasons"]
        )
        report["roundtrip_candidate_passed"] = bool(
            host_complete and modes_complete and binaries_unchanged
            and all(helper_facts[mode].get("roundtrip_passed") is True
                    for mode in ("appcontainer", "lpac"))
        )
        report["roundtrip_assertion_passed"] = bool(
            host_complete and modes_complete and binaries_unchanged
            and all(helper_facts[mode].get("nt_assertion_passed") is True
                    for mode in ("appcontainer", "lpac"))
        )
        report["infrastructure_complete"] = bool(
            host_complete and modes_complete and binaries_unchanged
            and all(report["request_integrity"][mode].get("unchanged_through_run") is True
                    for mode in ("appcontainer", "lpac"))
        )
        if report["roundtrip_candidate_passed"]:
            report["status"] = "roundtrip_candidate_passed"
        elif report["infrastructure_complete"]:
            report["status"] = "infrastructure_complete_candidate_failed"
        elif host_process is None:
            report["status"] = "infrastructure_incomplete"
        elif report["host_control"].get("positive_control_passed") is not True:
            report["status"] = "host_control_failed"
        else:
            report["status"] = "infrastructure_incomplete"
        _write_json_new(paths["report"], report)

    # Only paths intended as artifact links leave through the returned report;
    # the command-line summary never includes fixture IDs or raw stdout.
    report["report_path"] = str(paths["report"])
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="create a fresh fixture and run the diagnostic")
    parser.add_argument(
        "--assert-roundtrip", action="store_true",
        help="require both AppContainer modes to reopen NT paths as the same object, with verified cleanup",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.run:
        if args.assert_roundtrip:
            parser.error("--assert-roundtrip requires --run")
        parser.print_usage()
        return 0
    try:
        report = asyncio.run(run_roundtrip_diagnostic())
    except (OSError, RuntimeError, ValueError, RoundtripInputError) as exc:
        # Avoid echoing path, PID, or native error details to the terminal.
        print("report=unavailable status=setup_failed_" + type(exc).__name__)
        return 1
    report_path = report.get("report_path", "unavailable")
    status = report.get("status", "incomplete")
    print(f"report={report_path} status={status}")
    if args.assert_roundtrip and report.get("roundtrip_assertion_passed") is not True:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
