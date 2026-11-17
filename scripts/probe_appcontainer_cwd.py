"""Read-only CMD cwd diagnostics in an existing synthetic AppContainer fixture."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mewcode.sandbox.appcontainer_policy import build_probe_request  # noqa: E402
from scripts.probe_windows_restricted_candidate import (  # noqa: E402
    _invoke_probe_request,
    powershell_arguments,
)


REPORT_SCHEMA = "mewcode-appcontainer-cwd-diagnostic-v1"
MARKER_SCHEMA = "mewcode-cwd-probe-markers-v1"
FIXTURE_PREFIX = "mewcode-lpac-probe-"
SENTINEL_NAME = ".mewcode-synthetic-fixture"
SENTINEL_CONTENT = b"mewcode-synthetic-only-v1\n"
BASELINE_NAME = "nul-acl-git_status.request.json"
HELPER_NAME = "MewCode.AppContainerProbe.exe"
SOURCE_PATH = ROOT / "native" / "windows" / "MewCode.CwdAccessProbe.cs"
MAX_BASELINE_BYTES = 1024 * 1024
MAX_STDERR_CHARS = 2048
MAX_STDOUT_CHARS = 8192
CHILD_TIMEOUT_MS = 10_000
HOST_TIMEOUT_SECONDS = 15
HELPER_TIMEOUT_SECONDS = 35

# Fixed stdout protocol emitted by MewCode.CwdAccessProbe.cs.
REQUIRED_MARKERS = (
    "getcwd",
    "dot:attributes",
    "parent:attributes",
    "dot_read_attributes:open",
    "dot_list_directory:open",
    "parent_list_directory:open",
    "dot_star:find",
    "parent_star:find",
    "git_mingw_getcwd",
    "git_mingw_getcwd_stage",
    "git_finalpath_open",
    "git_finalpath_dos_normalized",
    "git_finalpath_dos_opened",
    "git_finalpath_nt_normalized",
    "git_finalpath_nt_opened",
    "git_long_path_name",
    "volume_root_get",
    "volume_root_attributes",
    "volume_root_metadata_open",
    "drive_root_attributes",
    "drive_root_metadata_open",
)
STAGE_MARKERS = (
    "current_directory",
    "create_file",
    "compat_attributes",
    "final_path_normalized_dos",
    "long_path_fallback",
    "result",
    "scope",
)
_STATUS_RE = re.compile(r"failed:win32=\d+\Z")


class DiagnosticInputError(ValueError):
    """The selected input is not a validated, existing synthetic fixture."""


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.fspath(path)))


def _strictly_beneath(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((_path_key(path), _path_key(root))) == _path_key(root) and (
            _path_key(path) != _path_key(root)
        )
    except ValueError:
        return False


def _has_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return True
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x0400)


def _check_ordinary_ancestry(path: Path, label: str) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise DiagnosticInputError(f"{label} must be an absolute path without traversal")
    for candidate in (path, *path.parents):
        if _has_reparse(candidate):
            raise DiagnosticInputError(f"{label} must not use missing or reparse-backed paths")


def _existing_file(path: Path, label: str, *, suffix: str | None = None,
                   reject_hardlinks: bool = True) -> Path:
    _check_ordinary_ancestry(path, label)
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise DiagnosticInputError(f"{label} must exist") from exc
    if not stat.S_ISREG(info.st_mode) or reject_hardlinks and info.st_nlink != 1:
        raise DiagnosticInputError(f"{label} must be a regular file without hard links")
    if suffix is not None and resolved.suffix.casefold() != suffix.casefold():
        raise DiagnosticInputError(f"{label} must have a {suffix} suffix")
    return resolved


def _existing_directory(path: Path, label: str) -> Path:
    _check_ordinary_ancestry(path, label)
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise DiagnosticInputError(f"{label} must exist") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise DiagnosticInputError(f"{label} must be an existing directory")
    return resolved


def _validate_fixture(raw_fixture: Path) -> Path:
    if not raw_fixture.is_absolute() or ".." in raw_fixture.parts:
        raise DiagnosticInputError("fixture must be an absolute path without traversal")
    fixture = _existing_directory(raw_fixture, "fixture")
    scratch = _existing_directory(ROOT / ".tmp", "project scratch directory")
    if _path_key(fixture.parent) != _path_key(scratch):
        raise DiagnosticInputError("fixture must be a direct child of the project .tmp directory")
    if not fixture.name.casefold().startswith(FIXTURE_PREFIX):
        raise DiagnosticInputError("fixture name is not a recognized synthetic fixture")
    if _path_key(fixture) == _path_key(ROOT):
        raise DiagnosticInputError("repository root cannot be used as a fixture")

    sentinel = _existing_file(fixture / SENTINEL_NAME, "fixture sentinel")
    if sentinel.stat().st_size != len(SENTINEL_CONTENT) or sentinel.read_bytes() != SENTINEL_CONTENT:
        raise DiagnosticInputError("fixture sentinel content does not match")
    return fixture


def _read_baseline(fixture: Path) -> tuple[dict, Path, list[Path], list[Path], list[Path]]:
    path = _existing_file(fixture / BASELINE_NAME, "baseline request", suffix=".json")
    if path.stat().st_size > MAX_BASELINE_BYTES:
        raise DiagnosticInputError("baseline request is too large")
    try:
        baseline = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DiagnosticInputError("baseline request is not readable JSON") from exc
    if not isinstance(baseline, dict):
        raise DiagnosticInputError("baseline request must be a JSON object")

    if baseline.get("fixture_root") is not None and _path_key(Path(baseline["fixture_root"])) != _path_key(fixture):
        raise DiagnosticInputError("baseline fixture_root does not match the selected fixture")
    cwd = _existing_directory(Path(str(baseline.get("cwd", ""))), "synthetic cwd")
    expected_cwd = _existing_directory(fixture / "workspace" / "git-synthetic-repository", "expected synthetic cwd")
    if _path_key(cwd) != _path_key(expected_cwd) or not _strictly_beneath(cwd, fixture):
        raise DiagnosticInputError("baseline cwd is not the recognized synthetic repository")

    def path_list(name: str) -> list[Path]:
        values = baseline.get(name)
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise DiagnosticInputError(f"baseline {name} must be a list of paths")
        return [Path(value) for value in values]

    read_paths = path_list("read_paths")
    write_paths = path_list("write_paths")
    execute_paths = path_list("execute_paths")
    return baseline, cwd, read_paths, write_paths, execute_paths


def _validate_helper(raw_helper: Path, fixture: Path) -> tuple[Path, str]:
    helper = _existing_file(raw_helper, "helper executable", suffix=".exe")
    expected = fixture / "compiled" / HELPER_NAME
    if _path_key(helper) != _path_key(expected):
        raise DiagnosticInputError("helper must be the compiled AppContainer probe in this fixture")
    manifest_path = _existing_file(fixture / "nul-acl-trial-manifest.json", "synthetic trial manifest")
    if manifest_path.stat().st_size > MAX_BASELINE_BYTES:
        raise DiagnosticInputError("synthetic trial manifest is too large")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DiagnosticInputError("synthetic trial manifest is not readable JSON") from exc
    digest = _sha256(helper)
    if (not isinstance(manifest, dict)
            or _path_key(Path(str(manifest.get("fixture_root", "")))) != _path_key(fixture)
            or _path_key(Path(str(manifest.get("helper", "")))) != _path_key(helper)
            or manifest.get("helper_sha256") != digest):
        raise DiagnosticInputError("helper identity does not match the synthetic trial manifest")
    return helper, digest


def _validate_report_target(raw: Path | None, fixture: Path, run_id: str) -> Path:
    if raw is None:
        candidate = fixture / f"cwd-diagnostic-native-{run_id}.report.json"
    else:
        candidate = raw if raw.is_absolute() else fixture / raw
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise DiagnosticInputError("report path must be absolute or fixture-relative without traversal")
    for component in candidate.parts[1:] if candidate.anchor else candidate.parts:
        if ":" in component:
            raise DiagnosticInputError("report path must not contain a Windows alternate data stream")
        if component.endswith((".", " ")):
            raise DiagnosticInputError("report path must not use a trailing dot or space")
        device_stem = component.split(".", 1)[0].strip(" .").casefold()
        if (device_stem in {"con", "prn", "aux", "nul"}
                or re.fullmatch(r"(?:com|lpt)[1-9]", device_stem)):
            raise DiagnosticInputError("report path must not use a Windows reserved device name")
    if candidate.suffix.casefold() != ".json":
        raise DiagnosticInputError("report path must use the .json suffix")
    parent = _existing_directory(candidate.parent, "report directory")
    if not _strictly_beneath(parent, fixture) and _path_key(parent) != _path_key(fixture):
        raise DiagnosticInputError("report path must remain inside the selected fixture")
    if _path_key(candidate) == _path_key(fixture) or _path_key(candidate) == _path_key(ROOT):
        raise DiagnosticInputError("report path cannot name a directory root")
    try:
        candidate.lstat()
    except FileNotFoundError:
        return candidate
    except OSError as exc:
        raise DiagnosticInputError("report target cannot be inspected") from exc
    raise DiagnosticInputError("report target already exists; evidence files are never overwritten")


def _new_output_paths(fixture: Path, cwd: Path, report_arg: Path | None) -> tuple[str, Path, Path, Path]:
    for _ in range(8):
        run_id = uuid.uuid4().hex
        report_path = _validate_report_target(report_arg, fixture, run_id)
        request_path = fixture / f"cwd-diagnostic-native-{run_id}.request.json"
        executable = cwd / f"MewCode.CwdAccessProbe-{run_id}.exe"
        if _path_key(request_path) == _path_key(report_path) or _path_key(executable) == _path_key(report_path):
            if report_arg is not None:
                raise DiagnosticInputError("report path collides with a generated evidence path")
            continue
        for candidate, label in ((request_path, "request path"), (executable, "probe executable path")):
            _check_ordinary_ancestry(candidate.parent, label)
            if not (_strictly_beneath(candidate.parent, fixture)
                    or _path_key(candidate.parent) == _path_key(fixture)):
                raise DiagnosticInputError(f"{label} must remain inside the fixture")
            try:
                candidate.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise DiagnosticInputError(f"{label} cannot be inspected") from exc
            break
        else:
            return run_id, report_path, request_path, executable
        if report_arg is not None:
            raise DiagnosticInputError("a generated evidence path already exists")
    raise DiagnosticInputError("could not allocate unused diagnostic evidence paths")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _host_paths(fixture: Path, cwd: Path, read_paths: list[Path], write_paths: list[Path],
                execute_paths: list[Path], grant_parent: bool) -> tuple[dict, Path, Path, Path]:
    system_raw = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if not system_raw:
        raise DiagnosticInputError("Windows system root is unavailable")
    system_root = _existing_directory(Path(system_raw), "Windows system root")
    compiler = _existing_file(
        system_root / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe",
        "fixed C# compiler",
        suffix=".exe",
        reject_hardlinks=False,
    )
    cmd = _existing_file(
        system_root / "System32" / "cmd.exe",
        "fixed system command processor",
        suffix=".exe",
        reject_hardlinks=False,
    )

    fixed_read_paths = list(read_paths)
    if grant_parent:
        fixed_read_paths.append(cwd.parent)
    # This preliminary build validates all fixture paths and grants before any
    # compiler or probe output is written. Baseline executable, arguments, and
    # environment are deliberately ignored.
    validation_request = build_probe_request(
        fixture_root=fixture,
        executable=cmd,
        arguments=["/d", "/c", "ver"],
        cwd=cwd,
        read_paths=fixed_read_paths,
        write_paths=write_paths,
        execute_paths=execute_paths,
        timeout_ms=CHILD_TIMEOUT_MS,
        lpac=False,
    )
    source = _existing_file(SOURCE_PATH, "fixed cwd probe source", suffix=".cs")
    return validation_request, compiler, cmd, source


def _status_value_valid(key: str, value: str) -> bool:
    if key == "git_mingw_getcwd" and value == "failed:errno=ENOENT":
        return True
    return value in {"ok", "buffer_too_small", "not_executed"} or bool(_STATUS_RE.fullmatch(value))


def _parse_stage(value: str) -> dict:
    fields: dict[str, str] = {}
    malformed = []
    for field in value.split(";"):
        if "=" not in field:
            malformed.append(field[:80])
            continue
        key, item_value = field.split("=", 1)
        if key in fields:
            malformed.append(f"duplicate:{key}")
            continue
        fields[key] = item_value
    missing = [key for key in STAGE_MARKERS if key not in fields]
    unknown = [key for key in fields if key not in STAGE_MARKERS]
    invalid_values = [
        key for key, item in fields.items()
        if (key == "scope" and item != "win32_path_only")
        or (key == "result" and not _status_value_valid("git_mingw_getcwd", item))
        or (key not in {"scope", "result"} and not _status_value_valid(key, item))
    ]
    return {
        "fields": fields,
        "missing": missing,
        "unknown": unknown,
        "malformed": malformed,
        "invalid_values": invalid_values,
        "protocol_valid": not (missing or unknown or malformed or invalid_values),
    }


def parse_probe_markers(stdout: str | bytes | None) -> dict:
    """Parse the fixed C# key=value protocol without assigning an outcome."""
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    if not isinstance(stdout, str):
        stdout = ""
    values: dict[str, list[str]] = {}
    malformed_lines: list[str] = []
    unknown_keys: list[str] = []
    for line in stdout.splitlines():
        if not line:
            malformed_lines.append("<empty>")
            continue
        if "=" not in line:
            malformed_lines.append(line[:120])
            continue
        key, value = line.split("=", 1)
        if key not in REQUIRED_MARKERS:
            unknown_keys.append(key[:120])
            continue
        values.setdefault(key, []).append(value)

    counts = {key: len(values.get(key, [])) for key in REQUIRED_MARKERS}
    missing = [key for key, count in counts.items() if count == 0]
    duplicates = [key for key, count in counts.items() if count > 1]
    invalid_values = [key for key, items in values.items()
                      if key not in {"git_mingw_getcwd_stage"}
                      and any(not _status_value_valid(key, item) for item in items)]
    stage = _parse_stage(values.get("git_mingw_getcwd_stage", [""])[0]) if counts["git_mingw_getcwd_stage"] == 1 else None
    if stage is not None and not stage["protocol_valid"]:
        invalid_values.append("git_mingw_getcwd_stage")
    protocol_valid = not (missing or duplicates or malformed_lines or unknown_keys or invalid_values)
    if (counts.get("git_mingw_getcwd") == 1 and stage is not None
            and stage["fields"].get("result") != values["git_mingw_getcwd"][0]):
        invalid_values.append("git_mingw_getcwd_stage_result_mismatch")
        protocol_valid = False
    return {
        "schema": MARKER_SCHEMA,
        "protocol_valid": protocol_valid,
        "marker_counts": counts,
        "marker_values": values,
        "missing_markers": missing,
        "duplicate_markers": duplicates,
        "invalid_values": invalid_values,
        "unknown_keys": unknown_keys,
        "malformed_lines": malformed_lines,
        "git_mingw_getcwd_stage": stage,
    }


def _decode_output(raw: bytes, redacted_paths: tuple[Path, ...], limit: int) -> dict:
    text = raw.decode("utf-8", errors="replace")
    for path in sorted((str(item) for item in redacted_paths), key=len, reverse=True):
        text = text.replace(path, "<synthetic-path>").replace(path.replace("\\", "/"), "<synthetic-path>")
    truncated = len(text) > limit
    return {
        "text": text[:limit],
        "byte_count": len(raw),
        "truncated": truncated,
    }


def _identity_evidence(account: str, source: str) -> dict:
    return {
        "account_name": account,
        "launch_context": source,
        "token_independently_verified": False,
    }


def _bounded_process_result(result: dict, redacted_paths: tuple[Path, ...]) -> dict:
    return {
        "pid": result.get("pid"),
        "exit_code": result.get("exit_code"),
        "start_error_type": result.get("start_error_type"),
        "timed_out": result.get("timed_out"),
        "cancelled": result.get("cancelled"),
        "stdout": _decode_output(result.get("stdout", b""), redacted_paths, MAX_STDOUT_CHARS),
        "stderr": _decode_output(result.get("stderr", b""), redacted_paths, MAX_STDERR_CHARS),
    }


async def _run_process(command: list[str], *, cwd: Path, environment: dict[str, str], timeout: float) -> dict:
    """Run one fixed diagnostic process and represent timeout/cancel separately."""
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        return {
            "pid": None, "exit_code": None, "start_error_type": type(exc).__name__,
            "timed_out": False, "cancelled": False, "stdout": b"", "stderr": b"",
        }

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except (asyncio.TimeoutError, OSError):
            pass
        return {
            "pid": process.pid, "exit_code": process.returncode, "start_error_type": None,
            "timed_out": True, "cancelled": False, "stdout": b"", "stderr": b"",
        }
    except asyncio.CancelledError:
        process.kill()
        try:
            await asyncio.shield(process.wait())
        except OSError:
            pass
        return {
            "pid": process.pid, "exit_code": process.returncode, "start_error_type": None,
            "timed_out": False, "cancelled": True, "stdout": b"", "stderr": b"",
        }
    return {
        "pid": process.pid, "exit_code": process.returncode, "start_error_type": None,
        "timed_out": False, "cancelled": False, "stdout": stdout, "stderr": stderr,
    }


def _write_json_new(path: Path, value: dict) -> None:
    encoded = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(encoded)


def _receipt_observation(invocation: dict, redacted_paths: tuple[Path, ...], account: str) -> dict:
    receipt = invocation.get("receipt") if isinstance(invocation.get("receipt"), dict) else None
    if receipt is None:
        marker_facts = parse_probe_markers(None)
        cleanup = {key: None for key in ("job_cleanup_ok", "profile_cleanup_ok", "acl_cleanup_ok")}
        identity = {"appcontainer_verified": None, "lpac_verified": None}
        child_exit_code = None
        child_timed_out = None
        child_cancelled = None
        error_present = None
        stdout = b""
        stderr = b""
    else:
        stdout_value = receipt.get("stdout")
        stderr_value = receipt.get("stderr")
        stdout = stdout_value.encode("utf-8", errors="replace") if isinstance(stdout_value, str) else b""
        stderr = stderr_value.encode("utf-8", errors="replace") if isinstance(stderr_value, str) else b""
        marker_facts = parse_probe_markers(stdout_value)
        cleanup = {key: receipt.get(key) if isinstance(receipt.get(key), bool) else None
                   for key in ("job_cleanup_ok", "profile_cleanup_ok", "acl_cleanup_ok")}
        identity = {
            "appcontainer_verified": receipt.get("appcontainer_verified")
            if isinstance(receipt.get("appcontainer_verified"), bool) else None,
            "lpac_verified": receipt.get("lpac_verified")
            if isinstance(receipt.get("lpac_verified"), bool) else None,
        }
        child_exit_code = receipt.get("exit_code") if isinstance(receipt.get("exit_code"), int) and not isinstance(receipt.get("exit_code"), bool) else None
        child_timed_out = receipt.get("timed_out") if isinstance(receipt.get("timed_out"), bool) else None
        child_cancelled = receipt.get("cancelled") if isinstance(receipt.get("cancelled"), bool) else None
        error_value = receipt.get("error")
        error_present = bool(error_value) if isinstance(error_value, str) else None

    helper_exit = (invocation.get("exit_code")
                   if isinstance(invocation.get("exit_code"), int)
                   and not isinstance(invocation.get("exit_code"), bool) else None)
    helper_timed_out = invocation.get("timed_out") if isinstance(invocation.get("timed_out"), bool) else None
    helper_cancelled = invocation.get("cancelled") if isinstance(invocation.get("cancelled"), bool) else None
    completeness = (
        marker_facts["protocol_valid"]
        and all(isinstance(value, bool) for value in identity.values())
        and all(isinstance(value, bool) for value in cleanup.values())
        and child_exit_code is not None
        and helper_exit is not None
        and isinstance(helper_timed_out, bool)
        and isinstance(helper_cancelled, bool)
        and isinstance(invocation.get("error_present"), bool)
        and isinstance(child_timed_out, bool)
        and isinstance(child_cancelled, bool)
        and isinstance(error_present, bool)
    )
    return {
        "helper": {
            "exit_code": helper_exit,
            "start_error_type": invocation.get("start_error_type"),
            "timed_out": helper_timed_out,
            "cancelled": helper_cancelled,
            "error_present": invocation.get("error_present")
            if isinstance(invocation.get("error_present"), bool) else None,
            "stderr_byte_count": invocation.get("stderr_byte_count"),
            "identity": _identity_evidence(account, "direct child process; current process token inherited, not independently inspected"),
        },
        "child": {
            "exit_code": child_exit_code,
            "timed_out": child_timed_out,
            "cancelled": child_cancelled,
            "identity": identity,
            "cleanup": cleanup,
            "error_present": error_present,
            "stdout": _decode_output(stdout, redacted_paths, MAX_STDOUT_CHARS),
            "stderr": _decode_output(stderr, redacted_paths, MAX_STDERR_CHARS),
            "markers": marker_facts,
        },
        "evidence_completeness": "complete_observations" if completeness else "incomplete_observations",
        "compatibility_outcome": None,
    }


async def _invoke_helper(helper: Path, request_path: Path, *, cwd: Path,
                         environment: dict[str, str]) -> dict:
    result = await _run_process(
        [str(helper), str(request_path)],
        cwd=cwd,
        environment=environment,
        timeout=HELPER_TIMEOUT_SECONDS,
    )
    stderr = result.get("stderr", b"")
    facts = {
        "pid": result.get("pid"),
        "exit_code": result.get("exit_code"),
        "start_error_type": result.get("start_error_type"),
        "timed_out": result.get("timed_out"),
        "cancelled": result.get("cancelled"),
        "stderr_byte_count": len(stderr),
        "receipt": None,
    }
    stdout = result.get("stdout", b"")
    if len(stdout) > MAX_STDOUT_CHARS * 2:
        facts["receipt_parse_error"] = "stdout_too_large"
        return facts
    try:
        receipt = json.loads(stdout.decode("utf-8-sig"))
        if isinstance(receipt, dict):
            facts["receipt"] = receipt
        else:
            facts["receipt_parse_error"] = "receipt_not_object"
    except (UnicodeError, json.JSONDecodeError):
        facts["receipt_parse_error"] = "receipt_unavailable_or_invalid"
    if facts.get("start_error_type") or facts.get("timed_out") or facts.get("cancelled"):
        facts["error_present"] = True
    elif facts.get("receipt_parse_error"):
        facts["error_present"] = True
    elif isinstance(facts.get("exit_code"), int):
        facts["error_present"] = facts["exit_code"] != 0
    else:
        facts["error_present"] = None
    return facts


async def run_native_diagnostic(fixture_arg: Path, helper_arg: Path, report_arg: Path | None,
                                grant_parent: bool) -> dict:
    """Compile and run one same-spec host/AppContainer diagnostic pair."""
    fixture = _validate_fixture(fixture_arg)
    baseline, cwd, read_paths, write_paths, execute_paths = _read_baseline(fixture)
    helper, helper_sha256_before = _validate_helper(helper_arg, fixture)
    validation_request, compiler, cmd, source = _host_paths(
        fixture, cwd, read_paths, write_paths, execute_paths, grant_parent,
    )
    run_id, report_path, request_path, executable = _new_output_paths(fixture, cwd, report_arg)
    account = getpass.getuser()
    redacted_paths = (fixture, cwd, cwd.parent)

    report = {
        "schema": REPORT_SCHEMA,
        "run_id": run_id,
        "production_authorized": False,
        "decision": "observation_only",
        "grant_parent": grant_parent,
        "fixture": {"root": str(fixture), "cwd": str(cwd), "recognized_synthetic_fixture": True},
        "baseline": {
            "request_path": str(fixture / BASELINE_NAME),
            "baseline_command_used": False,
            "baseline_environment_used": False,
            "only_path_grant_lists_used": True,
        },
        "helper_binary": {
            "path": str(helper),
            "sha256_before": helper_sha256_before,
            "sha256_before_invoke": None,
            "sha256_after": None,
        },
        "outputs": {"report": str(report_path), "request": str(request_path), "executable": str(executable)},
        "compiler": {"path": str(compiler), "exit_code": None, "timed_out": False,
                     "stderr": None, "source_path": str(source), "source_sha256": _sha256(source),
                     "executable_sha256": None},
        "probe_binary_integrity": {
            "compiled_sha256": None,
            "before_host_sha256": None,
            "after_host_sha256": None,
            "before_helper_sha256": None,
            "after_helper_sha256": None,
            "unchanged_through_run": None,
        },
        "shared_child_spec": None,
        "host_control": {
            "role": "positive_control",
            "identity": _identity_evidence(account, "direct subprocess; inherits current process token, no elevation requested"),
            "exit_code": None,
            "timed_out": None,
            "cancelled": None,
            "start_error_type": None,
            "markers": parse_probe_markers(None),
            "stdout": {"text": "", "byte_count": 0, "truncated": False},
            "stderr": {"text": "", "byte_count": 0, "truncated": False},
            "compatibility_outcome": None,
        },
        "restricted_observation": {
            "helper": {"exit_code": None, "timed_out": None, "cancelled": None,
                       "start_error_type": None, "stderr_byte_count": None,
                       "identity": _identity_evidence(account, "direct subprocess; current process token inherited, not independently inspected")},
            "child": {"exit_code": None, "timed_out": None, "cancelled": None,
                      "identity": {"appcontainer_verified": None, "lpac_verified": None},
                      "cleanup": {"job_cleanup_ok": None, "profile_cleanup_ok": None, "acl_cleanup_ok": None},
                      "error_present": None,
                      "stdout": {"text": "", "byte_count": 0, "truncated": False},
                      "stderr": {"text": "", "byte_count": 0, "truncated": False},
                      "markers": parse_probe_markers(None)},
            "evidence_completeness": "incomplete_observations",
            "compatibility_outcome": None,
        },
        "request_file": {"sha256_before_run": None, "sha256_after_run": None, "unchanged": None},
    }

    compiled = None
    request_hash: str | None = None
    propagate_cancel = False
    probe_hashes: list[str | None] = []
    try:
        compile_command = [
            str(compiler), "/nologo", "/platform:x64", "/out:" + str(executable), str(source),
        ]
        try:
            compiled = subprocess.run(
                compile_command,
                cwd=str(ROOT),
                env=validation_request["environment"],
                capture_output=True,
                timeout=30,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            report["compiler"]["exit_code"] = compiled.returncode
            report["compiler"]["stderr"] = _decode_output(compiled.stderr, redacted_paths, MAX_STDERR_CHARS)
        except subprocess.TimeoutExpired as exc:
            report["compiler"]["timed_out"] = True
            raw_stderr = exc.stderr if isinstance(exc.stderr, bytes) else b""
            report["compiler"]["stderr"] = _decode_output(raw_stderr, redacted_paths, MAX_STDERR_CHARS)
        except OSError as exc:
            report["compiler"]["start_error_type"] = type(exc).__name__

        if compiled is not None and compiled.returncode == 0 and executable.is_file():
            executable = _existing_file(executable, "newly compiled cwd probe", suffix=".exe")
            request = build_probe_request(
                fixture_root=fixture,
                executable=executable,
                arguments=[str(cwd.parent)],
                cwd=cwd,
                read_paths=validation_request["read_paths"],
                write_paths=validation_request["write_paths"],
                execute_paths=validation_request["execute_paths"],
                timeout_ms=CHILD_TIMEOUT_MS,
                lpac=False,
            )
            report["compiler"]["executable_sha256"] = _sha256(executable)
            report["probe_binary_integrity"]["compiled_sha256"] = report["compiler"]["executable_sha256"]
            environment = request["environment"]
            report["shared_child_spec"] = {
                "executable": str(executable),
                "executable_sha256": report["compiler"]["executable_sha256"],
                "arguments": request["arguments"],
                "cwd": request["cwd"],
                "environment": environment,
                "environment_sha256": hashlib.sha256(
                    json.dumps(environment, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest(),
                "lpac_requested": request["lpac"],
                "timeout_ms": request["timeout_ms"],
            }
            try:
                _write_json_new(request_path, request)
                request_hash = _sha256(request_path)
                report["request_file"]["sha256_before_run"] = request_hash
                binary_hash = _sha256(executable)
                report["probe_binary_integrity"]["before_host_sha256"] = binary_hash
                probe_hashes.append(binary_hash)
                if binary_hash != report["probe_binary_integrity"]["compiled_sha256"]:
                    report["restricted_observation_skipped_reason"] = "probe_binary_changed_before_host"
                else:
                    host_result = await _run_process(
                        [str(executable), *request["arguments"]],
                        cwd=cwd,
                        environment=environment,
                        timeout=HOST_TIMEOUT_SECONDS,
                    )
                    host_facts = _bounded_process_result(host_result, redacted_paths)
                    report["host_control"].update({
                        "exit_code": host_facts["exit_code"],
                        "timed_out": host_facts["timed_out"],
                        "cancelled": host_facts["cancelled"],
                        "start_error_type": host_facts["start_error_type"],
                        "pid": host_facts["pid"],
                        "stdout": host_facts["stdout"],
                        "stderr": host_facts["stderr"],
                        "markers": parse_probe_markers(host_result.get("stdout", b"")),
                    })
                    after_host_hash = _sha256(executable) if executable.is_file() else None
                    report["probe_binary_integrity"]["after_host_sha256"] = after_host_hash
                    probe_hashes.append(after_host_hash)
                    request_after_host = _sha256(request_path) if request_path.is_file() else None
                    if after_host_hash != report["probe_binary_integrity"]["compiled_sha256"]:
                        report["restricted_observation_skipped_reason"] = "probe_binary_changed_during_host_control"
                    elif request_after_host != request_hash:
                        report["restricted_observation_skipped_reason"] = "request_changed_during_host_control"
                        report["request_file"]["sha256_after_host"] = request_after_host
                    elif host_result.get("cancelled") is True:
                        report["restricted_observation_skipped_reason"] = "host_control_cancelled"
                        propagate_cancel = True
                    elif host_result.get("timed_out") is True:
                        report["restricted_observation_skipped_reason"] = "host_control_timed_out"
                    elif host_result.get("start_error_type"):
                        report["restricted_observation_skipped_reason"] = "host_control_start_failed"
                    else:
                        helper_digest_before = _sha256(helper) if helper.is_file() else None
                        report["helper_binary"]["sha256_before_invoke"] = helper_digest_before
                        if helper_digest_before != report["helper_binary"]["sha256_before"]:
                            report["restricted_observation_skipped_reason"] = "helper_binary_changed_before_invoke"
                        else:
                            before_helper_hash = _sha256(executable)
                            report["probe_binary_integrity"]["before_helper_sha256"] = before_helper_hash
                            probe_hashes.append(before_helper_hash)
                            if before_helper_hash != report["probe_binary_integrity"]["compiled_sha256"]:
                                report["restricted_observation_skipped_reason"] = "probe_binary_changed_before_helper"
                            else:
                                invocation = await _invoke_helper(helper, request_path, cwd=cwd, environment=environment)
                                restricted = _receipt_observation(invocation, redacted_paths, account)
                                report["restricted_observation"] = restricted
                                propagate_cancel = invocation.get("cancelled") is True
                                if propagate_cancel:
                                    report["restricted_observation_cancelled"] = True
                                after_helper_hash = _sha256(executable) if executable.is_file() else None
                                report["probe_binary_integrity"]["after_helper_sha256"] = after_helper_hash
                                probe_hashes.append(after_helper_hash)
                                if after_helper_hash != report["probe_binary_integrity"]["compiled_sha256"]:
                                    report["restricted_observation"]["binary_changed_during_helper"] = True
            except (OSError, ValueError, DiagnosticInputError) as exc:
                report["execution_error_type"] = type(exc).__name__
            finally:
                if request_path.is_file():
                    report["request_file"]["sha256_after_run"] = _sha256(request_path)
                    if request_hash is not None:
                        report["request_file"]["unchanged"] = (
                            report["request_file"]["sha256_after_run"] == request_hash
                        )
                observed_hashes = [item for item in probe_hashes if item is not None]
                if observed_hashes:
                    report["probe_binary_integrity"]["unchanged_through_run"] = (
                        len(observed_hashes) == len(probe_hashes)
                        and all(item == report["probe_binary_integrity"]["compiled_sha256"]
                                for item in observed_hashes)
                    )
        report["helper_binary"]["sha256_after"] = _sha256(helper) if helper.is_file() else None
        report["helper_binary"]["unchanged"] = (
            report["helper_binary"]["sha256_before"] == report["helper_binary"]["sha256_after"]
        )
    finally:
        _write_json_new(report_path, report)
    if propagate_cancel:
        raise asyncio.CancelledError
    return report


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--helper", required=True, type=Path,
                        help="existing fixture/compiled/MewCode.AppContainerProbe.exe matching its trial manifest")
    parser.add_argument("--prepare-sleep", action="store_true")
    parser.add_argument("--native-probe", action="store_true")
    parser.add_argument("--grant-parent", action="store_true",
                        help="also grant read access to the synthetic workspace parent")
    parser.add_argument("--report", type=Path,
                        help="native-probe report path, absolute or relative to the fixture; must be new")
    args = parser.parse_args()
    if args.native_probe:
        report = await run_native_diagnostic(args.fixture, args.helper, args.report, args.grant_parent)
        print(json.dumps({
            "schema": report["schema"],
            "run_id": report["run_id"],
            "report": report["outputs"]["report"],
            "host_exit_code": report["host_control"]["exit_code"],
            "helper_exit_code": report["restricted_observation"]["helper"]["exit_code"],
            "child_exit_code": report["restricted_observation"]["child"]["exit_code"],
            "production_authorized": False,
        }, ensure_ascii=False, indent=2))
        return

    if args.report is not None:
        parser.error("--report is available only with --native-probe")
    fixture = args.fixture.resolve(strict=True)
    baseline = json.loads((fixture / BASELINE_NAME).read_text(encoding="utf-8"))
    cwd = Path(baseline["cwd"])
    cmd = Path(baseline["environment"]["COMSPEC"])
    read_paths = [Path(path) for path in baseline["read_paths"]]
    if args.grant_parent:
        read_paths.append(cwd.parent)
    if args.prepare_sleep:
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        request = build_probe_request(
            fixture_root=fixture,
            executable=powershell,
            arguments=powershell_arguments("Start-Sleep -Seconds 30; Write-Output SLEEP_DONE"),
            cwd=cwd,
            read_paths=read_paths,
            write_paths=[Path(path) for path in baseline["write_paths"]],
            execute_paths=[Path(path) for path in baseline["execute_paths"]],
            timeout_ms=50_000,
            lpac=False,
        )
        path = fixture / "cwd-diagnostic-sleep.request.json"
        path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
        print(path)
        return
    observations = {}
    for name, command in (
        ("cd", "cd"),
        ("type_file", "type synthetic-input.txt"),
        ("dir_current", "dir /b ."),
        ("dir_parent", "dir /b .."),
    ):
        request = build_probe_request(
            fixture_root=fixture,
            executable=cmd,
            arguments=["/d", "/c", command],
            cwd=cwd,
            read_paths=read_paths,
            write_paths=[Path(path) for path in baseline["write_paths"]],
            execute_paths=[Path(path) for path in baseline["execute_paths"]],
            timeout_ms=10_000,
            lpac=False,
        )
        suffix = "-with-parent" if args.grant_parent else ""
        path = fixture / ("cwd-diagnostic-" + name + suffix + ".request.json")
        result = await _invoke_probe_request(args.helper.resolve(strict=True), request, path)
        receipt = result.get("receipt") or {}
        observations[name] = {
            "helper_exit_code": result.get("helper_exit_code"),
            "child_exit_code": receipt.get("exit_code"),
            "appcontainer_verified": receipt.get("appcontainer_verified"),
            "stdout": receipt.get("stdout"),
            "stderr": receipt.get("stderr"),
            "error": receipt.get("error"),
            "job_cleanup_ok": receipt.get("job_cleanup_ok"),
            "profile_cleanup_ok": receipt.get("profile_cleanup_ok"),
            "acl_cleanup_ok": receipt.get("acl_cleanup_ok"),
        }
    print(json.dumps(observations, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
