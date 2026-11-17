import copy
import hashlib
import json

import pytest

from scripts.summarize_native_host_acl_trial import CAPABILITIES, NAMES, summarize


@pytest.fixture
def evidence():
    manifest = json.dumps({"schema": "mewcode-native-host-acl-trial-v1", "cases": [{"name": name} for name in NAMES]}).encode()
    cases = {}
    for name in NAMES:
        lpac = name.startswith("lpac_")
        output = ""
        if name.endswith("_nul"):
            output = "nul-read:open=ok\nnul-write:open=ok\nnul-readwrite:open=ok\n"
        elif name.endswith("_cwd"):
            output = "git_mingw_getcwd=ok\ndrive_root_attributes=ok\ndrive_root_metadata_open=ok\n"
        elif name.endswith("_git_version"):
            output = "git version synthetic\n"
        elif name.endswith("_powershell"):
            output = "MEWCODE_POWERSHELL_HOST_CONTROL_OK\n"
        cases[name] = dict(exit_code=0, stdout=output, error="", timed_out=False, cancelled=False,
                           stdout_truncated=False, stderr_truncated=False, job_cleanup_ok=True,
                           profile_cleanup_ok=True, acl_cleanup_ok=True, appcontainer_verified=True,
                           lpac_verified=True, lpac_verification_method="access_check",
                           lpac_access_mask=2 if lpac else 3, capabilities=CAPABILITIES if lpac else [])
    report = {"host-acl-trial-result.json": dict(manifest_sha256=hashlib.sha256(manifest).hexdigest(),
               restore_verified=True, watchdog_ready=True, watchdog_exited=True, watchdog_exit_code=0),
              "objects": [dict(label=label, original_dacl_sha256="a", current_dacl_sha256="a",
                               original_controls=37892, current_controls=37892,
                               original_sddl="known", current_sddl="known") for label in ("nul", "drive_root")],
              "trial_case_receipts": cases}
    return report, manifest


def test_success_never_authorizes_production(evidence):
    report, manifest = evidence
    result = summarize(report, manifest)
    assert result["compatibility_passed"] is True
    assert result["passed_case_count"] == 10
    assert result["production_authorized"] is False


def test_probe_exit_zero_does_not_mean_api_success(evidence):
    report, manifest = evidence
    report["trial_case_receipts"]["lpac_cwd"]["stdout"] = "git_mingw_getcwd=failed:win32=5\n"
    result = summarize(report, manifest)
    assert result["infrastructure_complete"] is True
    assert result["compatibility_passed"] is False
    assert result["passed_case_count"] == 9


@pytest.mark.parametrize("field,value", [("lpac_access_mask", 3), ("capabilities", []),
    ("acl_cleanup_ok", False), ("stdout_truncated", True), ("exit_code", False), ("error", "failure")])
def test_invalid_receipt_never_passes(evidence, field, value):
    report, manifest = evidence
    report["trial_case_receipts"]["lpac_nul"][field] = value
    assert summarize(report, manifest)["compatibility_passed"] is False


def test_restore_flag_is_not_enough_when_control_bits_differ(evidence):
    report, manifest = evidence
    report["objects"][1]["current_controls"] ^= 1024
    result = summarize(report, manifest)
    assert result["restore_verified"] is False
    assert result["compatibility_passed"] is False


def test_missing_receipt_and_changed_manifest_rejected(evidence):
    report, manifest = evidence
    missing = copy.deepcopy(report)
    del missing["trial_case_receipts"]["ac_nul"]
    with pytest.raises(ValueError, match="case receipts"):
        summarize(missing, manifest)
    with pytest.raises(ValueError, match="hash mismatch"):
        summarize(report, manifest + b" ")
