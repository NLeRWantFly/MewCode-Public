from copy import deepcopy

import pytest

from scripts.probe_windows_appcontainer import assess_smoke, powershell_arguments


def successful_smoke():
    common = dict(exit_code=0, timed_out=False, error="", job_cleanup_ok=True,
                  profile_cleanup_ok=True, acl_cleanup_ok=True, appcontainer_verified=True,
                  stdout_truncated=False, stderr_truncated=False, capabilities=[])
    return {
        "cases": [
            {"name": "cmd_smoke", "helper_exit_code": 0,
             "receipt": dict(common, stdout="CMD_nonce\n")},
            {"name": "powershell_boundary", "helper_exit_code": 0,
             "receipt": dict(common, stdout="\n".join([
                 "POWERSHELL_nonce", "ENV_CLEAN", "INPUT_OK", "OUTPUT_OK", "OUTSIDE_READ_DENIED",
                 "SECRET_READ_DENIED", "OUTSIDE_WRITE_DENIED", "NETWORK_DENIED",
             ]))},
            {"name": "exe_boundary", "helper_exit_code": 0,
             "receipt": dict(common, stdout="\n".join([
                 "EXE_nonce", "ENV_CLEAN", "INPUT_OK", "OUTPUT_OK", "OUTSIDE_READ_DENIED",
                 "SECRET_READ_DENIED", "JUNCTION_READ_DENIED", "OUTSIDE_WRITE_DENIED", "NETWORK_DENIED",
             ]))},
        ],
        "observed": {"network_child_payload_received": False, "outside_write_exists": False,
                     "output_readable_by_host": True, "exe_output_readable_by_host": True},
        "fixture_acl_cleanup_ok": True,
        "host_control": {"junction_target_readable": True},
    }


def test_smoke_is_not_production_authorization():
    result = assess_smoke(successful_smoke(), "nonce")
    assert result["smoke_passed"]
    assert not result["production_authorized"]
    assert result["missing_checks"]


@pytest.mark.parametrize("exists,completed,expected", [
    (False, True, "denial_verified"),
    (False, False, "no_write_observed_probe_incomplete"),
    (True, True, "write_observed"),
    (True, False, "write_observed"),
    (None, True, "observation_unavailable"),
])
def test_outside_write_observation_distinguishes_escape_from_missing_proof(exists, completed, expected):
    report = successful_smoke()
    report["observed"]["outside_write_exists"] = exists
    if not completed:
        report["cases"][1]["receipt"]["timed_out"] = True
    result = assess_smoke(report, "nonce")
    assert result["outside_write_observation"] == expected
    assert result["smoke_passed"] is (exists is False and completed)
    assert result["production_authorized"] is False


def test_diagnostic_apartment_does_not_silently_change_default():
    assert "-Mta" not in powershell_arguments("Write-Output probe")
    assert "-Mta" in powershell_arguments("Write-Output probe", mta=True)


def test_empty_runspace_diagnostic_is_not_powershell_boundary_evidence():
    report = successful_smoke()
    report["cases"][1]["receipt"]["stdout"] = ""
    report["cases"].append({"name": "powershell_init_diagnostic", "helper_exit_code": 0,
                            "receipt": {"exit_code": 0, "stdout": "PS_INIT_DONE"}})
    assert not assess_smoke(report, "nonce")["smoke_passed"]


@pytest.mark.parametrize("field,value", [
    ("exit_code", 1), ("timed_out", True), ("error", "launch failed"),
    ("job_cleanup_ok", False), ("profile_cleanup_ok", False), ("acl_cleanup_ok", False),
    ("appcontainer_verified", False),
    ("stdout_truncated", True), ("stderr_truncated", True),
    ("stdout", "echo POWERSHELL_nonce; NETWORK_DENIED"),
])
def test_failed_helper_cannot_attest_smoke(field, value):
    report = successful_smoke()
    report["cases"][1]["receipt"][field] = value
    assert not assess_smoke(report, "nonce")["smoke_passed"]


@pytest.mark.parametrize("field,value", [
    ("network_child_payload_received", True), ("outside_write_exists", True),
    ("output_readable_by_host", False),
    ("exe_output_readable_by_host", False),
])
def test_host_observation_overrides_child_markers(field, value):
    report = successful_smoke()
    report["observed"][field] = value
    assert not assess_smoke(report, "nonce")["smoke_passed"]


def test_encoded_powershell_preserves_unicode_and_quotes():
    import base64
    script = "Get-Content -LiteralPath 'C:\\合成 文件\\input.txt'"
    arguments = powershell_arguments(script)
    assert arguments[-2] == "-EncodedCommand"
    assert base64.b64decode(arguments[-1]).decode("utf-16-le") == script


def test_powershell_success_does_not_substitute_exe_boundary():
    report = successful_smoke()
    report["cases"].pop()
    result = assess_smoke(report, "nonce")
    assert not result["smoke_passed"]
    assert not result["checks"]["loopback_host_observation"]


def test_network_fault_is_not_evidence_of_network_isolation():
    report = successful_smoke()
    receipt = report["cases"][2]["receipt"]
    receipt["stdout"] = receipt["stdout"].replace("NETWORK_DENIED", "NETWORK_UNKNOWN")
    assert not assess_smoke(report, "nonce")["smoke_passed"]


def test_silent_drop_requires_exact_payload_host_control_and_no_delivery():
    report = successful_smoke()
    case = report["cases"][2]
    case["helper_exit_code"] = case["receipt"]["exit_code"] = 2
    case["receipt"]["stdout"] = case["receipt"]["stdout"].replace("NETWORK_DENIED", "NETWORK_UNKNOWN_TIMEOUT")
    assert not assess_smoke(report, "nonce")["smoke_passed"]
    report["host_control"] = {"same_payload_network_reachable": True}
    verdict = assess_smoke(report, "nonce")
    assert verdict["smoke_passed"]
    assert verdict["network_observation"] == "bounded_no_delivery_with_same_payload_control"
    assert not verdict["production_authorized"]
    report["observed"]["network_child_payload_received"] = True
    assert not assess_smoke(report, "nonce")["smoke_passed"]


def test_control_does_not_turn_other_network_failure_or_exit_into_success():
    report = successful_smoke()
    report["host_control"] = {"same_payload_network_reachable": True}
    case = report["cases"][2]
    case["helper_exit_code"] = case["receipt"]["exit_code"] = 2
    original = case["receipt"]["stdout"]
    for result in ("NETWORK_UNKNOWN_ConnectionRefused", "NETWORK_UNKNOWN_TypeInitializationException"):
        case["receipt"]["stdout"] = original.replace("NETWORK_DENIED", result)
        assert not assess_smoke(report, "nonce")["smoke_passed"]
    case["receipt"]["stdout"] = original.replace("NETWORK_DENIED", "NETWORK_UNKNOWN_TIMEOUT")
    case["receipt"]["exit_code"] = 1
    assert not assess_smoke(report, "nonce")["smoke_passed"]


def test_junction_read_needs_both_positive_control_and_actual_denial():
    report = successful_smoke()
    assert "junction_read_escape" not in assess_smoke(report, "nonce")["missing_checks"]
    report["host_control"]["junction_target_readable"] = False
    assert "junction_read_escape" in assess_smoke(report, "nonce")["missing_checks"]
    receipt = report["cases"][2]["receipt"]
    receipt["stdout"] = receipt["stdout"].replace("JUNCTION_READ_DENIED", "JUNCTION_READ_UNKNOWN_FileNotFoundException")
    assert not assess_smoke(report, "nonce")["smoke_passed"]


def test_lpac_requires_independent_identity_evidence():
    report = successful_smoke()
    report["lpac"] = True
    assert not assess_smoke(report, "nonce")["smoke_passed"]


def test_capability_declaration_must_match_explicit_request():
    report = successful_smoke()
    report["runtime_registry_read"] = True
    assert not assess_smoke(report, "nonce")["smoke_passed"]
    report["runtime_registry_read"] = False
    report["cases"][0]["receipt"]["capabilities"] = ["registryRead"]
    assert not assess_smoke(report, "nonce")["smoke_passed"]


def test_lifecycle_needs_positive_pid_observation_and_cleanup():
    report = successful_smoke()
    case = deepcopy(report["cases"][0])
    case.update(name="root_exit_descendants", descendant_exit_observed=True)
    report["cases"].append(case)
    assert assess_smoke(report, "nonce")["lifecycle_checks"]["root_exit_descendants"]
    case["receipt"]["job_cleanup_ok"] = False
    assert not assess_smoke(report, "nonce")["lifecycle_checks"]["root_exit_descendants"]
    assert not assess_smoke(report, "nonce")["lifecycle_checks"]["timeout_descendants"]


def test_expected_timeout_does_not_mask_other_cleanup_errors():
    report = successful_smoke()
    case = deepcopy(report["cases"][0])
    case.update(name="timeout_descendants", descendant_exit_observed=True, helper_exit_code=1)
    case["receipt"].update(timed_out=True, exit_code=None, error="child process exceeded timeout_ms")
    report["cases"].append(case)
    assert assess_smoke(report, "nonce")["lifecycle_checks"]["timeout_descendants"]
    case["receipt"]["error"] += "; profile cleanup failed"
    assert not assess_smoke(report, "nonce")["lifecycle_checks"]["timeout_descendants"]


def test_missing_and_stderr_evidence_are_not_passes():
    assert not assess_smoke({}, "nonce")["smoke_passed"]
    report = deepcopy(successful_smoke())
    receipt = report["cases"][1]["receipt"]
    receipt["stderr"], receipt["stdout"] = receipt["stdout"], ""
    assert not assess_smoke(report, "nonce")["smoke_passed"]


def test_cancellation_requires_signal_descendant_exit_and_complete_cleanup():
    report = successful_smoke()
    case = deepcopy(report["cases"][0])
    case.update(name="cancellation_descendants", descendant_exit_observed=True,
                cancel_signal_sent=True, helper_exit_code=1)
    case["receipt"].update(cancelled=True, timed_out=False, exit_code=None)
    report["cases"].append(case)
    verdict = assess_smoke(report, "nonce")
    assert verdict["lifecycle_checks"]["cancellation_descendants"]
    assert "cancellation_descendants" not in verdict["missing_checks"]
    assert not verdict["production_authorized"]
    for field in ("job_cleanup_ok", "acl_cleanup_ok", "profile_cleanup_ok", "cancelled"):
        changed = deepcopy(report)
        changed["cases"][-1]["receipt"][field] = False
        assert not assess_smoke(changed, "nonce")["lifecycle_checks"]["cancellation_descendants"]
    case["receipt"]["timed_out"] = True
    assert not assess_smoke(report, "nonce")["lifecycle_checks"]["cancellation_descendants"]


def test_drive_alias_requires_cleanup_evidence():
    report = successful_smoke()
    report["alias_workspace_drive"] = True
    assert not assess_smoke(report, "nonce")["smoke_passed"]
    report["drive_alias_cleanup_ok"] = True
    assert assess_smoke(report, "nonce")["smoke_passed"]
