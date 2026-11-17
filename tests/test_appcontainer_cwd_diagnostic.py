from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace

import pytest

from mewcode.sandbox import appcontainer_policy
from scripts import probe_appcontainer_cwd as diagnostic


REPO_ROOT = Path(__file__).resolve().parents[1]


def marker_output(*, omit: str | None = None, duplicate: str | None = None,
                  invalid: str | None = None, git_result: str = "ok") -> str:
    items = {
        "getcwd": "ok",
        "dot:attributes": "ok",
        "parent:attributes": "ok",
        "dot_read_attributes:open": "ok",
        "dot_list_directory:open": "ok",
        "parent_list_directory:open": "ok",
        "dot_star:find": "ok",
        "parent_star:find": "ok",
        "git_mingw_getcwd": git_result,
        "git_mingw_getcwd_stage": (
            "current_directory=ok;create_file=ok;compat_attributes=not_executed;"
            f"final_path_normalized_dos=ok;long_path_fallback=not_executed;result={git_result};"
            "scope=win32_path_only"
        ),
        "git_finalpath_open": "ok",
        "git_finalpath_dos_normalized": "ok",
        "git_finalpath_dos_opened": "not_executed",
        "git_finalpath_nt_normalized": "ok",
        "git_finalpath_nt_opened": "not_executed",
        "git_long_path_name": "not_executed",
        "volume_root_get": "ok",
        "volume_root_attributes": "ok",
        "volume_root_metadata_open": "ok",
        "drive_root_attributes": "ok",
        "drive_root_metadata_open": "ok",
    }
    if omit is not None:
        del items[omit]
    if invalid is not None:
        items[invalid] = "success"
    lines = [f"{key}={value}" for key, value in items.items()]
    if duplicate is not None:
        lines.append(f"{duplicate}=ok")
    return "\n".join(lines)


@pytest.fixture
def synthetic_trial(monkeypatch):
    scratch = REPO_ROOT / ".tmp"
    scratch.mkdir(exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="mewcode-lpac-probe-cwd-test-", dir=scratch)
    fixture = Path(temporary.name)
    (fixture / diagnostic.SENTINEL_NAME).write_bytes(diagnostic.SENTINEL_CONTENT)
    workspace = fixture / "workspace"
    cwd = workspace / "git-synthetic-repository"
    (cwd / "temp").mkdir(parents=True)
    (cwd / "synthetic-input.txt").write_text("SYNTHETIC INPUT\n", encoding="utf-8")
    profile = fixture / "profile"
    for directory in (profile / "AppData" / "Local", profile / "AppData" / "Roaming"):
        directory.mkdir(parents=True)
    compiled = fixture / "compiled"
    compiled.mkdir()
    helper = compiled / diagnostic.HELPER_NAME
    helper.write_bytes(b"synthetic helper placeholder")

    system_root = fixture / "synthetic-system"
    command_processor = system_root / "System32" / "cmd.exe"
    compiler = system_root / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    command_processor.parent.mkdir(parents=True)
    compiler.parent.mkdir(parents=True)
    command_processor.write_bytes(b"fixed system command placeholder")
    compiler.write_bytes(b"fixed compiler placeholder")
    monkeypatch.setenv("SystemRoot", str(system_root))

    read_paths = [
        cwd, cwd / "temp", cwd / "synthetic-input.txt",
        profile, profile / "AppData", profile / "AppData" / "Local",
        profile / "AppData" / "Roaming",
    ]
    write_paths = [cwd, cwd / "temp"]
    baseline = {
        "fixture_root": str(fixture),
        "executable": "C:/untrusted/from-baseline.exe",
        "arguments": ["do-not-run-this"],
        "cwd": str(cwd),
        "read_paths": [str(path) for path in read_paths],
        "write_paths": [str(path) for path in write_paths],
        "execute_paths": [],
        "environment": {"COMSPEC": "C:/untrusted/from-baseline.cmd", "PATH": "attacker-controlled"},
    }
    (fixture / diagnostic.BASELINE_NAME).write_text(json.dumps(baseline), encoding="utf-8")
    helper_digest = hashlib.sha256(helper.read_bytes()).hexdigest()
    (fixture / "nul-acl-trial-manifest.json").write_text(json.dumps({
        "fixture_root": str(fixture),
        "helper": str(helper),
        "helper_sha256": helper_digest,
        "cases": [],
    }), encoding="utf-8")
    yield fixture, cwd, helper, compiler
    temporary.cleanup()


def test_marker_parser_rejects_missing_duplicate_invalid_and_inconsistent_markers() -> None:
    complete = diagnostic.parse_probe_markers(marker_output())
    assert complete["protocol_valid"] is True
    assert complete["marker_counts"]["drive_root_attributes"] == 1

    missing = diagnostic.parse_probe_markers(marker_output(omit="drive_root_attributes"))
    assert missing["protocol_valid"] is False
    assert missing["missing_markers"] == ["drive_root_attributes"]

    duplicate = diagnostic.parse_probe_markers(marker_output(duplicate="dot:attributes"))
    assert duplicate["protocol_valid"] is False
    assert duplicate["duplicate_markers"] == ["dot:attributes"]

    invalid = diagnostic.parse_probe_markers(marker_output(invalid="volume_root_attributes"))
    assert invalid["protocol_valid"] is False
    assert "volume_root_attributes" in invalid["invalid_values"]

    mismatch = diagnostic.parse_probe_markers(marker_output(git_result="failed:errno=ENOENT"))
    assert mismatch["protocol_valid"] is True
    inconsistent = marker_output().replace("result=ok", "result=failed:errno=ENOENT")
    inconsistent_report = diagnostic.parse_probe_markers(inconsistent)
    assert inconsistent_report["protocol_valid"] is False
    assert "git_mingw_getcwd_stage_result_mismatch" in inconsistent_report["invalid_values"]


def test_receipt_timeout_missing_identity_or_cleanup_stays_incomplete() -> None:
    timeout = diagnostic._receipt_observation({
        "exit_code": None, "timed_out": True, "cancelled": False,
        "error_present": True, "receipt": None,
    }, (), "synthetic-user")
    assert timeout["helper"]["timed_out"] is True
    assert timeout["child"]["exit_code"] is None
    assert timeout["child"]["cleanup"] == {
        "job_cleanup_ok": None, "profile_cleanup_ok": None, "acl_cleanup_ok": None,
    }
    assert timeout["evidence_completeness"] == "incomplete_observations"
    assert timeout["compatibility_outcome"] is None

    unknown_identity = diagnostic._receipt_observation({
        "exit_code": 0,
        "timed_out": False,
        "cancelled": False,
        "error_present": False,
        "receipt": {
            "exit_code": 0,
            "stdout": marker_output(),
            "stderr": "",
            "timed_out": False,
            "cancelled": False,
            "appcontainer_verified": None,
            "lpac_verified": False,
            "job_cleanup_ok": True,
            "profile_cleanup_ok": True,
            "acl_cleanup_ok": True,
        },
    }, (), "synthetic-user")
    assert unknown_identity["evidence_completeness"] == "incomplete_observations"
    assert unknown_identity["child"]["identity"]["appcontainer_verified"] is None

    unknown_cleanup = diagnostic._receipt_observation({
        "exit_code": 0,
        "timed_out": False,
        "cancelled": False,
        "error_present": False,
        "receipt": {
            "exit_code": 0,
            "stdout": marker_output(),
            "stderr": "",
            "timed_out": False,
            "cancelled": False,
            "appcontainer_verified": True,
            "lpac_verified": False,
            "job_cleanup_ok": True,
            "profile_cleanup_ok": None,
            "acl_cleanup_ok": True,
        },
    }, (), "synthetic-user")
    assert unknown_cleanup["evidence_completeness"] == "incomplete_observations"
    assert unknown_cleanup["child"]["cleanup"]["profile_cleanup_ok"] is None
    assert unknown_cleanup["compatibility_outcome"] is None


def test_fixture_and_report_paths_are_confined_and_existing_evidence_is_not_reused(
    synthetic_trial, tmp_path: Path,
) -> None:
    fixture, cwd, _helper, _compiler = synthetic_trial
    assert diagnostic._validate_fixture(fixture) == fixture.resolve()

    fake_temporary = tempfile.TemporaryDirectory(
        prefix=diagnostic.FIXTURE_PREFIX + "spoofed-", dir=fixture.parent,
    )
    try:
        fake_fixture = Path(fake_temporary.name)
        (fake_fixture / diagnostic.SENTINEL_NAME).write_bytes(b"wrong sentinel\n")
        with pytest.raises(diagnostic.DiagnosticInputError):
            diagnostic._validate_fixture(fake_fixture)
    finally:
        fake_temporary.cleanup()

    outside = tmp_path / (diagnostic.FIXTURE_PREFIX + "outside")
    outside.mkdir()
    (outside / diagnostic.SENTINEL_NAME).write_bytes(diagnostic.SENTINEL_CONTENT)
    with pytest.raises(diagnostic.DiagnosticInputError, match="project .tmp"):
        diagnostic._validate_fixture(outside)

    report = fixture / "already-recorded.report.json"
    report.write_text('{"old":"evidence"}', encoding="utf-8")
    with pytest.raises(diagnostic.DiagnosticInputError, match="never overwritten"):
        diagnostic._new_output_paths(fixture, cwd, report)

    with pytest.raises(diagnostic.DiagnosticInputError, match="inside the selected fixture"):
        diagnostic._new_output_paths(fixture, cwd, tmp_path / "outside.json")

    for unsafe_name in ("NUL.json", "COM1.report.json", "invalid:stream.json"):
        with pytest.raises(diagnostic.DiagnosticInputError):
            diagnostic._new_output_paths(fixture, cwd, Path(unsafe_name))


def test_native_report_runs_same_fixed_probe_and_saves_new_schema_report(
    synthetic_trial, monkeypatch,
) -> None:
    fixture, cwd, helper, compiler = synthetic_trial
    old_report = fixture / "cwd-diagnostic-native-old.report.json"
    old_report.write_text('{"old":"report"}', encoding="utf-8")
    old_executable = cwd / "MewCode.CwdAccessProbe.exe"
    old_executable.write_bytes(b"old compiled probe")
    old_request = fixture / "cwd-diagnostic-native.request.json"
    old_request.write_text('{"old":"request"}', encoding="utf-8")
    old_contents = {path: path.read_bytes() for path in (old_report, old_executable, old_request)}

    def fake_compile(command, **kwargs):
        assert command[0] == str(compiler)
        output = Path(next(item[len("/out:"):] for item in command if item.startswith("/out:")))
        assert output.name.startswith("MewCode.CwdAccessProbe-")
        assert command[-1] == str(diagnostic.SOURCE_PATH)
        output.write_bytes(b"new synthetic compiled probe")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(diagnostic.subprocess, "run", fake_compile)
    marker_bytes = marker_output().encode()
    invocations: list[tuple[list[str], Path, dict[str, str]]] = []

    async def fake_process(command, *, cwd: Path, environment: dict[str, str], timeout: float):
        invocations.append((command, cwd, environment))
        if Path(command[0]) == helper:
            request_path = Path(command[1])
            request = json.loads(request_path.read_text(encoding="utf-8"))
            assert request["executable"] == invocations[0][0][0]
            assert request["arguments"] == [str(cwd.parent)]
            assert request["cwd"] == str(cwd)
            assert request["environment"] == environment
            receipt = {
                "exit_code": 0,
                "appcontainer_verified": True,
                "lpac_verified": False,
                "timed_out": False,
                "cancelled": False,
                "job_cleanup_ok": True,
                "profile_cleanup_ok": True,
                "acl_cleanup_ok": True,
                "stdout": marker_output(),
                "stderr": f"synthetic stderr at {cwd}",
                "error": "",
            }
            return {
                "pid": 234,
                "exit_code": 0,
                "start_error_type": None,
                "timed_out": False,
                "cancelled": False,
                "stdout": json.dumps(receipt).encode(),
                "stderr": b"",
            }
        return {
            "pid": 123,
            "exit_code": 0,
            "start_error_type": None,
            "timed_out": False,
            "cancelled": False,
            "stdout": marker_bytes,
            "stderr": f"synthetic host stderr at {cwd}".encode(),
        }

    monkeypatch.setattr(diagnostic, "_run_process", fake_process)
    report = asyncio.run(diagnostic.run_native_diagnostic(fixture, helper, None, False))

    report_path = Path(report["outputs"]["report"])
    request_path = Path(report["outputs"]["request"])
    executable = Path(report["outputs"]["executable"])
    assert report_path.is_file() and request_path.is_file() and executable.is_file()
    assert report_path != old_report and request_path != old_request and executable != old_executable
    assert {path: path.read_bytes() for path in old_contents} == old_contents
    archived = json.loads(report_path.read_text(encoding="utf-8"))
    assert archived["schema"] == diagnostic.REPORT_SCHEMA
    assert archived["production_authorized"] is False
    assert archived["grant_parent"] is False
    assert archived["baseline"]["baseline_command_used"] is False
    assert archived["baseline"]["baseline_environment_used"] is False
    assert archived["compiler"]["source_sha256"]
    assert archived["compiler"]["executable_sha256"]
    assert archived["helper_binary"]["sha256_before"]
    assert archived["request_file"]["sha256_before_run"] == archived["request_file"]["sha256_after_run"]
    assert archived["request_file"]["unchanged"] is True
    assert archived["probe_binary_integrity"]["unchanged_through_run"] is True
    assert archived["helper_binary"]["sha256_before_invoke"] == archived["helper_binary"]["sha256_before"]

    host, helper_call = invocations
    assert host[0] == [str(executable), str(cwd.parent)]
    assert helper_call[0][0] == str(helper)
    assert host[1] == helper_call[1] == cwd
    assert host[2] == helper_call[2] == archived["shared_child_spec"]["environment"]
    assert host[0][0] != "C:/untrusted/from-baseline.exe"
    assert archived["host_control"]["exit_code"] == 0
    assert archived["restricted_observation"]["helper"]["exit_code"] == 0
    assert archived["restricted_observation"]["child"]["exit_code"] == 0
    assert archived["host_control"]["markers"]["protocol_valid"] is True
    assert archived["restricted_observation"]["child"]["markers"]["protocol_valid"] is True
    assert archived["restricted_observation"]["compatibility_outcome"] is None
    assert "passed" not in json.dumps(archived).casefold()
    assert "<synthetic-path>" in archived["restricted_observation"]["child"]["stderr"]["text"]


def test_host_timeout_or_cancellation_stops_before_helper_and_report_is_written(
    synthetic_trial, monkeypatch,
) -> None:
    fixture, cwd, helper, compiler = synthetic_trial

    def fake_compile(command, **kwargs):
        output = Path(next(item[len("/out:"):] for item in command if item.startswith("/out:")))
        output.write_bytes(b"compiled")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(diagnostic.subprocess, "run", fake_compile)
    calls = []

    async def cancelled_host(command, *, cwd, environment, timeout):
        calls.append(command)
        return {
            "pid": 10, "exit_code": -1, "start_error_type": None,
            "timed_out": False, "cancelled": True, "stdout": b"", "stderr": b"",
        }

    monkeypatch.setattr(diagnostic, "_run_process", cancelled_host)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(diagnostic.run_native_diagnostic(fixture, helper, None, False))
    assert len(calls) == 1
    reports = sorted(fixture.glob("cwd-diagnostic-native-*.report.json"))
    report = next(json.loads(path.read_text(encoding="utf-8")) for path in reports
                  if json.loads(path.read_text(encoding="utf-8")).get("schema") == diagnostic.REPORT_SCHEMA)
    assert report["host_control"]["cancelled"] is True
    assert report["restricted_observation_skipped_reason"] == "host_control_cancelled"
    assert report["restricted_observation"]["helper"]["exit_code"] is None
    assert report["production_authorized"] is False


def test_legacy_cwd_flags_still_build_the_same_four_requests_without_live_helper(
    synthetic_trial, monkeypatch,
) -> None:
    fixture, cwd, helper, _compiler = synthetic_trial
    baseline_path = fixture / diagnostic.BASELINE_NAME
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline["environment"]["COMSPEC"] = str(fixture / "synthetic-system" / "System32" / "cmd.exe")
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    calls = []

    async def fake_invoker(helper_path, request, request_path):
        calls.append((helper_path, request, request_path))
        return {"helper_exit_code": 0, "receipt": {"exit_code": 0}}

    monkeypatch.setattr(diagnostic, "_invoke_probe_request", fake_invoker)
    monkeypatch.setattr("sys.argv", [
        "probe_appcontainer_cwd.py", "--fixture", str(fixture), "--helper", str(helper),
    ])
    with contextlib.redirect_stdout(io.StringIO()):
        asyncio.run(diagnostic.main())

    assert len(calls) == 4
    assert [item[1]["arguments"][-1] for item in calls] == [
        "cd", "type synthetic-input.txt", "dir /b .", "dir /b ..",
    ]
    assert all(item[1]["cwd"] == str(cwd) for item in calls)
    assert all(item[0] == helper.resolve() for item in calls)
