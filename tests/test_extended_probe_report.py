from copy import deepcopy

import pytest

from scripts.probe_windows_appcontainer_extended import (
    CAPABILITIES, FILE_MARKERS, REQUIRED_CHECKS, extended_smoke_passed, valid_execution,
)


def successful_case():
    return {"helper_exit_code": 0, "receipt": {
        "exit_code": 0, "stdout": "\n".join(FILE_MARKERS), "timed_out": False, "cancelled": False,
        "capabilities": list(CAPABILITIES), "error": "", "lpac_verified": True, "lpac_access_mask": 2,
        "job_cleanup_ok": True, "acl_cleanup_ok": True, "profile_cleanup_ok": True,
        "appcontainer_verified": True, "stdout_truncated": False, "stderr_truncated": False,
    }}


def test_all_file_proofs_are_required_even_when_no_escaped_file_exists():
    case = successful_case()
    assert valid_execution(case, FILE_MARKERS)
    for marker in FILE_MARKERS:
        broken = deepcopy(case)
        broken["receipt"]["stdout"] = broken["receipt"]["stdout"].replace(marker, "UNKNOWN")
        assert not valid_execution(broken, FILE_MARKERS)


@pytest.mark.parametrize("field,value", [
    ("exit_code", 2), ("timed_out", True), ("cancelled", True), ("error", "failure"),
    ("capabilities", CAPABILITIES + ["internetClient"]), ("lpac_verified", False),
    ("lpac_access_mask", 3), ("acl_cleanup_ok", False), ("stdout_truncated", True),
    ("profile_cleanup_ok", False), ("appcontainer_verified", False),
])
def test_failed_or_broader_helper_cannot_pass(field, value):
    case = successful_case()
    case["receipt"][field] = value
    assert not valid_execution(case, FILE_MARKERS)


def test_missing_checks_never_pass_via_empty_all():
    assert not extended_smoke_passed({})
    report = {"checks": {key: True for key in REQUIRED_CHECKS}}
    assert extended_smoke_passed(report)
    for missing in REQUIRED_CHECKS:
        broken = deepcopy(report)
        del broken["checks"][missing]
        assert not extended_smoke_passed(broken)
    report["error"] = "listener failed"
    assert not extended_smoke_passed(report)
