from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from mewcode.sandbox.appcontainer_policy import build_probe_request
from scripts import probe_windows_path_roundtrip as roundtrip


def protocol_text(**overrides: str) -> str:
    values = {
        "roundtrip_schema": roundtrip.MARKER_SCHEMA,
        "roundtrip_root_identity": "ok",
    }
    for form in roundtrip.FORMS:
        for view in roundtrip.VIEWS:
            prefix = f"roundtrip_{form}_{view}_"
            values[prefix + "query"] = "ok"
            values[prefix + "reopen"] = "ok"
            values[prefix + "identity"] = "ok"
            values[prefix + "same_file"] = "yes"
    values["roundtrip_none_normalized_paired_source"] = "nt_normalized"
    values["roundtrip_none_opened_paired_source"] = "nt_opened"
    values["roundtrip_parent_open"] = "ok"
    values["roundtrip_parent_identity"] = "ok"
    values["roundtrip_parent_same_file"] = "no"
    values.update(overrides)
    return "\n".join(f"{key}={value}" for key, value in values.items()) + "\n"


def make_fixture(monkeypatch: pytest.MonkeyPatch, root: Path) -> dict[str, Path]:
    (root / ".tmp").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(roundtrip, "ROOT", root)
    paths = roundtrip._new_fixture()
    roundtrip._validate_new_fixture(paths)
    return paths


def receipt(protocol: str, *, lpac: bool, **changes) -> dict:
    value = {
        "exit_code": 0,
        "appcontainer_verified": True,
        "lpac_verified": True,
        "lpac_verification_method": "access_check",
        "lpac_access_mask": 2 if lpac else 3,
        "capabilities": roundtrip.CAPABILITIES[:] if lpac else [],
        "timed_out": False,
        "cancelled": False,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "stdout": protocol,
        "stderr": "",
        "error": "",
        "job_cleanup_ok": True,
        "profile_cleanup_ok": True,
        "acl_cleanup_ok": True,
    }
    value.update(changes)
    return value


def process_result(stdout: bytes, *, exit_code=0, timed_out=False, cancelled=False, **changes) -> dict:
    return {
        "pid": 123,
        "exit_code": exit_code,
        "start_error_type": None,
        "timed_out": timed_out,
        "cancelled": cancelled,
        "stdout": stdout,
        "stderr": b"",
        **changes,
    }


def test_parser_accepts_full_protocol_and_keeps_no_distinct_from_unknown():
    passed = roundtrip.parse_roundtrip_markers(protocol_text().encode("utf-8"))
    assert passed["protocol_valid"] is True
    assert passed["marker_count"] == len(roundtrip.ROUNDTRIP_MARKER_KEYS)
    assert passed["marker_values"]["roundtrip_nt_normalized_same_file"] == "yes"

    no_result = roundtrip.parse_roundtrip_markers(
        protocol_text(roundtrip_nt_normalized_same_file="no")
    )
    unknown_result = roundtrip.parse_roundtrip_markers(
        protocol_text(
            roundtrip_nt_normalized_query="failed:win32=5",
            roundtrip_nt_normalized_reopen="not_executed",
            roundtrip_nt_normalized_identity="not_executed",
            roundtrip_nt_normalized_same_file="not_executed",
            roundtrip_none_normalized_paired_source="not_executed",
            roundtrip_none_normalized_reopen="not_executed",
            roundtrip_none_normalized_identity="not_executed",
            roundtrip_none_normalized_same_file="not_executed",
        )
    )
    assert no_result["protocol_valid"] is True
    assert unknown_result["protocol_valid"] is True
    assert no_result["marker_values"]["roundtrip_nt_normalized_same_file"] == "no"
    assert unknown_result["marker_values"]["roundtrip_nt_normalized_same_file"] == "not_executed"


@pytest.mark.parametrize("mutation,field", [
    (lambda text: text.replace("roundtrip_root_identity=ok\n", "", 1), "missing_markers"),
    (lambda text: text + "roundtrip_root_identity=ok\n", "duplicate_markers"),
    (lambda text: text + "surprise=value\n", "unknown_keys"),
    (lambda text: text.replace("roundtrip_nt_opened_query=ok", "roundtrip_nt_opened_query=not-a-status"), "invalid_values"),
    (lambda text: text.replace(
        "roundtrip_dos_normalized_query=ok\nroundtrip_dos_normalized_reopen=ok",
        "roundtrip_dos_normalized_query=failed:win32=5\nroundtrip_dos_normalized_reopen=ok",
    ), "inconsistent_chains"),
])
def test_parser_rejects_incomplete_or_inconsistent_protocol(mutation, field):
    parsed = roundtrip.parse_roundtrip_markers(mutation(protocol_text()))
    assert parsed["protocol_valid"] is False
    assert parsed[field]


def test_parser_rejects_invalid_utf8_and_oversized_protocol():
    invalid = roundtrip.parse_roundtrip_markers(b"\xff")
    oversized = roundtrip.parse_roundtrip_markers(b"x" * (roundtrip.MAX_PROCESS_OUTPUT_BYTES + 1))
    assert invalid["invalid_utf8"] is True
    assert invalid["protocol_valid"] is False
    assert oversized["oversized"] is True
    assert oversized["protocol_valid"] is False


def test_parser_rejects_query_failure_followed_by_reopen_and_unknown_marker_enum():
    inconsistent = roundtrip.parse_roundtrip_markers(protocol_text(
        roundtrip_nt_opened_query="failed:win32=5",
        roundtrip_nt_opened_reopen="ok",
    ))
    invalid_same = roundtrip.parse_roundtrip_markers(protocol_text(
        roundtrip_nt_opened_same_file="unknown",
    ))
    assert inconsistent["protocol_valid"] is False
    assert invalid_same["protocol_valid"] is False


def test_parser_rejects_same_file_without_root_identity_and_unpaired_none_source():
    no_root = roundtrip.parse_roundtrip_markers(protocol_text(
        roundtrip_root_identity="failed:win32=5",
    ))
    unpaired = roundtrip.parse_roundtrip_markers(protocol_text(
        roundtrip_nt_normalized_query="failed:win32=5",
    ))
    assert no_root["protocol_valid"] is False
    assert unpaired["protocol_valid"] is False


def test_new_fixture_builds_exact_appcontainer_and_lpac_policy_requests(monkeypatch, tmp_path):
    if not roundtrip.IS_WINDOWS:
        pytest.skip("the fixed appcontainer policy requires Windows paths")
    paths = make_fixture(monkeypatch, tmp_path)
    probe_dir = paths["probe_output"]
    probe_dir.mkdir()
    probe = probe_dir / "MewCode.CwdAccessProbe.exe"
    probe.write_bytes(b"synthetic executable placeholder")

    common = dict(
        fixture_root=paths["fixture"], executable=probe,
        arguments=["--roundtrip-v1", str(paths["parent"])], cwd=paths["cwd"],
        read_paths=[paths["cwd"], paths["parent"]], write_paths=[paths["temp"]],
        execute_paths=[probe], timeout_ms=roundtrip.CHILD_TIMEOUT_MS,
    )
    ac = build_probe_request(**common, lpac=False)
    lpac = build_probe_request(
        **common, lpac=True, runtime_registry_read=True,
        runtime_instrumentation=True, runtime_com=True,
    )

    assert paths["temp"].parent == paths["cwd"]
    assert ac["arguments"] == lpac["arguments"] == ["--roundtrip-v1", str(paths["parent"])]
    assert ac["read_paths"] == lpac["read_paths"] == [str(paths["cwd"]), str(paths["parent"])]
    assert ac["write_paths"] == lpac["write_paths"] == [str(paths["temp"])]
    assert ac["execute_paths"] == lpac["execute_paths"] == [str(probe)]
    assert ac["lpac"] is False and ac["runtime_registry_read"] is False
    assert lpac["lpac"] is True
    assert lpac["runtime_registry_read"] is lpac["runtime_instrumentation"] is lpac["runtime_com"] is True
    assert ac["environment"] == lpac["environment"]


def test_receipt_requires_mode_specific_identity_capabilities_and_exact_integer_exit():
    protocol = roundtrip.parse_roundtrip_markers(protocol_text())
    ac = roundtrip._receipt_facts({"exit_code": 0, "timed_out": False, "cancelled": False,
                                   "start_error_type": None, "output_oversized": False,
                                   "receipt": receipt(protocol_text(), lpac=False)}, protocol, lpac=False)
    lpac = roundtrip._receipt_facts({"exit_code": 0, "timed_out": False, "cancelled": False,
                                     "start_error_type": None, "output_oversized": False,
                                     "receipt": receipt(protocol_text(), lpac=True)}, protocol, lpac=True)
    bad_capabilities = roundtrip._receipt_facts({
        "exit_code": 0, "timed_out": False, "cancelled": False, "start_error_type": None,
        "output_oversized": False, "receipt": receipt(protocol_text(), lpac=False,
                                                          capabilities=roundtrip.CAPABILITIES),
    }, protocol, lpac=False)
    bool_exit = roundtrip._receipt_facts({
        "exit_code": 0, "timed_out": False, "cancelled": False, "start_error_type": None,
        "output_oversized": False, "receipt": receipt(protocol_text(), lpac=True, exit_code=True),
    }, protocol, lpac=True)
    truncated = roundtrip._receipt_facts({
        "exit_code": 0, "timed_out": False, "cancelled": False, "start_error_type": None,
        "output_oversized": False, "receipt": receipt(protocol_text(), lpac=True, stdout_truncated=True),
    }, protocol, lpac=True)

    assert ac["capabilities_verified"] and ac["identity_verified"] and ac["cleanup_confirmed"]
    assert lpac["capabilities_verified"] and lpac["identity_verified"] and lpac["cleanup_confirmed"]
    assert bad_capabilities["capabilities_verified"] is False
    assert bool_exit["receipt_valid"] is False and bool_exit["child_complete"] is False
    assert truncated["child_complete"] is False


def test_helper_receipt_json_rejects_duplicate_fields_and_nonfinite_values(monkeypatch, tmp_path):
    async def fake_run(*_args, **_kwargs):
        return process_result(b'{"exit_code":0,"exit_code":0}')

    monkeypatch.setattr(roundtrip.cwd_diagnostic, "_run_process", fake_run)
    helper = tmp_path / "fake.exe"
    request = tmp_path / "request.json"
    duplicate = asyncio.run(roundtrip._invoke_helper(helper, request, cwd=tmp_path, environment={}))
    assert duplicate["receipt"] is None
    assert duplicate["receipt_error_type"] == "invalid_json_structure"

    async def fake_nan(*_args, **_kwargs):
        return process_result(b'{"exit_code":NaN}')

    monkeypatch.setattr(roundtrip.cwd_diagnostic, "_run_process", fake_nan)
    nonfinite = asyncio.run(roundtrip._invoke_helper(helper, request, cwd=tmp_path, environment={}))
    assert nonfinite["receipt"] is None
    assert nonfinite["receipt_error_type"] == "invalid_json_structure"


def _patch_mock_execution(monkeypatch, paths, *, host_result=None, helper_changes=None):
    helper_file = paths["helper_output"] / "MewCode.AppContainerProbe.exe"
    helper_file.parent.mkdir()
    helper_file.write_bytes(b"stable helper")
    helper_hash = roundtrip._sha256_file(helper_file)
    compiler = paths["fixture"] / "fake-csc.exe"
    compiler.write_bytes(b"compiler placeholder")

    def compile_helper(output):
        assert output == paths["helper_output"]
        return helper_file, {
            "compiler_path": str(compiler), "compiler_exit_code": 0,
            "helper_sha256": helper_hash,
        }

    def compile_probe(_compiler, output):
        output.mkdir()
        target = output / "MewCode.CwdAccessProbe.exe"
        target.write_bytes(b"stable probe")
        return target, {
            "exit_code": 0,
            "source_sha256_before": "source-digest",
            "source_sha256_after": "source-digest",
            "executable_sha256": roundtrip._sha256_file(target),
        }

    def request_builder(**kwargs):
        lpac = kwargs["lpac"]
        return {
            "fixture_root": str(kwargs["fixture_root"]),
            "executable": str(kwargs["executable"]),
            "arguments": kwargs["arguments"],
            "cwd": str(kwargs["cwd"]),
            "read_paths": [str(item) for item in kwargs["read_paths"]],
            "write_paths": [str(item) for item in kwargs["write_paths"]],
            "execute_paths": [str(item) for item in kwargs["execute_paths"]],
            "timeout_ms": kwargs["timeout_ms"], "lpac": lpac,
            "runtime_registry_read": kwargs["runtime_registry_read"],
            "runtime_instrumentation": kwargs["runtime_instrumentation"],
            "runtime_com": kwargs["runtime_com"],
            "environment": {"PATH": "synthetic", "TEMP": str(kwargs["cwd"] / "temp")},
        }

    monkeypatch.setattr(roundtrip, "_new_fixture", lambda: paths)
    monkeypatch.setattr(roundtrip.candidate, "compile_probe_helper", compile_helper)
    monkeypatch.setattr(roundtrip.candidate, "checked_executable", lambda p, label: Path(p))
    monkeypatch.setattr(roundtrip, "_compile_roundtrip_probe", compile_probe)
    monkeypatch.setattr(roundtrip, "build_probe_request", request_builder)
    monkeypatch.setattr(roundtrip, "IS_WINDOWS", True)

    calls = []
    def make_async_process():
        async def fake_run(command, *, cwd, environment, timeout):
            calls.append((command, cwd, environment, timeout))
            if Path(command[0]) == paths["probe_output"] / "MewCode.CwdAccessProbe.exe":
                return host_result or process_result(protocol_text().encode())
            mode = "appcontainer" if "-ac.request.json" in command[1] else "lpac"
            change = (helper_changes or {}).get(mode, {})
            receipt_changes = {key: value for key, value in change.items()
                               if key not in {"helper_process", "mutate_helper", "drop_receipt_fields"}}
            child_receipt = receipt(protocol_text(), lpac=(mode == "lpac"), **receipt_changes)
            for key in change.get("drop_receipt_fields", []):
                child_receipt.pop(key, None)
            out = json.dumps(child_receipt).encode("utf-8")
            result = process_result(out, **change.get("helper_process", {}))
            if change.get("mutate_helper"):
                helper_file.write_bytes(b"changed helper")
            return result
        return fake_run

    monkeypatch.setattr(roundtrip.cwd_diagnostic, "_run_process", make_async_process())
    return calls, helper_file, compiler


def test_runner_host_positive_control_then_ac_and_lpac_same_child_spec(monkeypatch, tmp_path):
    paths = make_fixture(monkeypatch, tmp_path)
    calls, _helper, _compiler = _patch_mock_execution(monkeypatch, paths)
    report = asyncio.run(roundtrip.run_roundtrip_diagnostic())

    assert len(calls) == 3
    host, ac, lpac = calls
    ac_request = json.loads(Path(ac[0][1]).read_text(encoding="utf-8"))
    lpac_request = json.loads(Path(lpac[0][1]).read_text(encoding="utf-8"))
    assert host[0][0] == ac_request["executable"] == lpac_request["executable"]
    assert host[1] == paths["cwd"]
    assert host[0][1:] == ["--roundtrip-v1", str(paths["parent"])]
    assert host[2] == ac[2] == lpac[2]
    assert report["infrastructure_complete"] is True
    assert report["roundtrip_candidate_passed"] is True
    assert report["roundtrip_assertion_passed"] is True
    assert report["restricted_observations"]["appcontainer"]["capabilities_verified"] is True
    assert report["restricted_observations"]["lpac"]["capabilities_verified"] is True
    assert report["production_authorized"] is False
    assert report["git_compatibility_status"] == "NOT_TESTED"
    assert report["acl_cleanup_evidence"]["source"] == "helper_receipt_only"


@pytest.mark.parametrize("host_result", [
    process_result(protocol_text(roundtrip_root_identity="failed:win32=5").encode()),
    process_result(protocol_text().encode(), timed_out=True),
])
def test_host_failure_or_timeout_skips_both_restricted_modes(monkeypatch, tmp_path, host_result):
    paths = make_fixture(monkeypatch, tmp_path)
    calls, _helper, _compiler = _patch_mock_execution(monkeypatch, paths, host_result=host_result)
    report = asyncio.run(roundtrip.run_roundtrip_diagnostic())
    assert len(calls) == 1
    assert report["host_control"]["positive_control_passed"] is False
    assert report["infrastructure_complete"] is False
    assert report["roundtrip_candidate_passed"] is False


@pytest.mark.parametrize("mode,changes", [
    ("appcontainer", {"mutate_helper": True}),
    ("appcontainer", {"helper_process": {"timed_out": True, "exit_code": None}}),
    ("appcontainer", {"helper_process": {"cancelled": True, "exit_code": None}}),
    ("appcontainer", {"stdout_truncated": True}),
    ("appcontainer", {"exit_code": True}),
    ("appcontainer", {"job_cleanup_ok": False}),
    ("appcontainer", {"profile_cleanup_ok": False}),
    ("appcontainer", {"acl_cleanup_ok": False}),
    ("appcontainer", {"drop_receipt_fields": ["job_cleanup_ok"]}),
    ("appcontainer", {"drop_receipt_fields": ["profile_cleanup_ok"]}),
    ("appcontainer", {"drop_receipt_fields": ["acl_cleanup_ok"]}),
])
def test_uncertain_cleanup_truncation_bool_exit_or_hash_drift_stops_next_mode(
    monkeypatch, tmp_path, mode, changes,
):
    paths = make_fixture(monkeypatch, tmp_path)
    calls, _helper, _compiler = _patch_mock_execution(monkeypatch, paths, helper_changes={mode: changes})
    report = asyncio.run(roundtrip.run_roundtrip_diagnostic())

    helper_calls = [call for call in calls if "request.json" in call[0][1]]
    assert len(helper_calls) == 1
    assert report["infrastructure_complete"] is False
    assert report["roundtrip_candidate_passed"] is False
    if changes.get("mutate_helper"):
        assert report["helper_binary"]["unchanged"] is False


def test_runner_does_not_execute_without_explicit_run(monkeypatch, capsys):
    def should_not_run():
        raise AssertionError("--run is required")
    monkeypatch.setattr(roundtrip, "run_roundtrip_diagnostic", should_not_run)
    assert roundtrip.main([]) == 0
    assert "usage:" in capsys.readouterr().out


def test_assertion_without_run_is_not_a_successful_validation(monkeypatch):
    def should_not_run():
        raise AssertionError("--run is required")
    monkeypatch.setattr(roundtrip, "run_roundtrip_diagnostic", should_not_run)
    with pytest.raises(SystemExit) as error:
        roundtrip.main(["--assert-roundtrip"])
    assert error.value.code == 2


def test_nt_assertion_requires_the_different_object_control():
    wrong_parent = roundtrip.parse_roundtrip_markers(
        protocol_text(roundtrip_parent_same_file="yes")
    )
    assert wrong_parent["protocol_valid"] is True
    assert roundtrip._nt_assertion_passed(wrong_parent) is False


def test_build_receipt_hash_mismatch_prevents_all_processes(monkeypatch, tmp_path):
    paths = make_fixture(monkeypatch, tmp_path)
    calls, _helper, _compiler = _patch_mock_execution(monkeypatch, paths)
    compile_helper = roundtrip.candidate.compile_probe_helper

    def mismatched_build(output):
        helper, facts = compile_helper(output)
        facts["helper_sha256"] = "0" * 64
        return helper, facts

    monkeypatch.setattr(roundtrip.candidate, "compile_probe_helper", mismatched_build)
    report = asyncio.run(roundtrip.run_roundtrip_diagnostic())
    assert calls == []
    assert report["infrastructure_complete"] is False
    assert report["roundtrip_assertion_passed"] is False
    assert report["failure_reasons"]


def test_final_probe_hash_drift_invalidates_a_completed_pair(monkeypatch, tmp_path):
    paths = make_fixture(monkeypatch, tmp_path)
    calls, _helper, _compiler = _patch_mock_execution(monkeypatch, paths)
    original_hash = roundtrip._sha256_file
    probe = paths["probe_output"] / "MewCode.CwdAccessProbe.exe"
    post_lpac_reads = 0

    def drift_on_final_read(path):
        nonlocal post_lpac_reads
        if path == probe and len(calls) == 3:
            post_lpac_reads += 1
            if post_lpac_reads == 2:
                probe.write_bytes(b"synthetic change after the last child receipt")
        return original_hash(path)

    monkeypatch.setattr(roundtrip, "_sha256_file", drift_on_final_read)
    report = asyncio.run(roundtrip.run_roundtrip_diagnostic())
    assert len(calls) == 3
    assert post_lpac_reads == 2
    assert report["probe_binary_integrity"]["unchanged_through_run"] is False
    assert report["infrastructure_complete"] is False
    assert report["roundtrip_candidate_passed"] is False
    assert report["roundtrip_assertion_passed"] is False
