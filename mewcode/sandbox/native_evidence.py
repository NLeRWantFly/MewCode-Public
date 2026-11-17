"""Validate host-produced native acceptance receipts, never prose assertions."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

SCHEMA = "mewcode-lpac-production-probe-v2"
METADATA_SCHEMA = "mewcode-metadata-broker-probe-v1"
METADATA_PROTOCOL_SCHEMA = "mewcode-metadata-protocol-probe-v1"
METADATA_CASES = frozenset({"standard_parent_privileged_snapshot", "original_registration_denied",
    "wrong_object_identity_denied", "nonempty_sacl_without_dacl_change",
    "registered_identity_survives_rename", "new_file_inherits_audit_policy",
    "modified_file_preserves_inherited_audit",
    "ordinary_parent_audit_policy_preserved",
    "directory_audit_policy_restored", "replayed_request_denied"})
METADATA_PROTOCOL_QUICK_CASES = frozenset({
    "wrong_nonce_denied", "duplicate_fields_denied", "oversized_frame_denied",
    "incomplete_frame_timeout", "wrong_server_pid_denied",
    "parent_exit_enforced_with_pipe_retained",
})
METADATA_PROTOCOL_LIFETIME_CASES = frozenset({"session_deadline_enforced"})
MAX_REPORT_BYTES = 2 * 1024 * 1024
REQUIRED_CASES = {
    "sensitive_read_denied": ("sensitive_read_denied",),
    "workspace_escape_write_denied": ("escape_write_denied",),
    "junction_escape_denied": ("junction_escape_denied",),
    "network_denied": ("network_loopback_denied",),
    "secret_env_scrubbed": ("secret_env_scrubbed",),
    "process_tree_reaped": ("process_tree_reaped", "process_tree_cancelled"),
    "compat_powershell": ("powershell_cjk", "workspace_file_roundtrip"),
    "compat_paths": ("powershell_cjk", "git_status_cjk", "python_cjk"),
    "compat_python": ("python_cjk", "python_unittest"),
    "compat_git": ("git_version", "git_status_cjk", "git_add_commit", "git_diff_cjk",
                   "git_second_commit", "git_config_denied", "git_hook_write_denied"),
}


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate evidence field")
        result[key] = value
    return result


def read_report(path: Path) -> tuple[dict, str]:
    from mewcode.sandbox.lpac_runtime import checked_path
    # Includes ancestor reparse checks; bounded reads also cover growth after stat.
    path = checked_path(path, file=True)
    with path.open("rb") as stream:
        raw = stream.read(MAX_REPORT_BYTES + 1)
    if not raw or len(raw) > MAX_REPORT_BYTES:
        raise ValueError("Native evidence report is empty or oversized")
    payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(payload, dict) or payload.get("schema") not in {
        SCHEMA, METADATA_SCHEMA, METADATA_PROTOCOL_SCHEMA,
    }:
        raise ValueError("Native evidence must be a structured live probe report")
    return payload, hashlib.sha256(raw).hexdigest()


def _metadata_protocol_passes(report: dict, key: str, identity: str, machine: str,
                              broker_sha256: str | None) -> bool:
    if key == "metadata_protocol_quick":
        mode = "quick"
        expected_cases = METADATA_PROTOCOL_QUICK_CASES
    elif key == "metadata_protocol_lifetime":
        mode = "lifetime"
        expected_cases = METADATA_PROTOCOL_LIFETIME_CASES
    else:
        return False

    required_fields = {"schema", "mode", "bundle_identity", "machine_guid",
                       "helper_sha256", "complete", "cases"}
    if (not isinstance(report, dict) or set(report) != required_fields
            or report.get("schema") != METADATA_PROTOCOL_SCHEMA
            or report.get("mode") != mode
            or report.get("complete") is not True
            or report.get("bundle_identity") != identity
            or not machine or report.get("machine_guid") != machine
            or not isinstance(broker_sha256, str)
            or report.get("helper_sha256") != broker_sha256):
        return False

    cases = report.get("cases")
    if not isinstance(cases, list) or len(cases) != len(expected_cases):
        return False
    indexed = {}
    for case in cases:
        if not isinstance(case, dict):
            return False
        name = case.get("case")
        if not isinstance(name, str) or name not in expected_cases or name in indexed:
            return False
        allowed_fields = {"case", "ok"}
        if key == "metadata_protocol_lifetime":
            allowed_fields |= {"elapsed_seconds", "exit_code"}
        elif name == "oversized_frame_denied":
            allowed_fields.add("exit_code")
            if "exit_code" in case and (type(case["exit_code"]) is not int or case["exit_code"] not in (0, 1)):
                return False
        if set(case) - allowed_fields or case.get("ok") is not True:
            return False
        indexed[name] = case
    if set(indexed) != expected_cases:
        return False

    if key == "metadata_protocol_lifetime":
        fact = indexed["session_deadline_enforced"]
        elapsed = fact.get("elapsed_seconds")
        if (isinstance(elapsed, bool) or not isinstance(elapsed, (int, float))
                or not math.isfinite(elapsed) or not 1190 <= elapsed <= 1230):
            return False
        if "exit_code" in fact and (type(fact["exit_code"]) is not int or fact["exit_code"] != 124):
            return False
    return True


def evidence_passes(report: dict, key: str, identity: str, machine: str,
                    *, broker_sha256: str | None = None) -> bool:
    if key in {"metadata_protocol_quick", "metadata_protocol_lifetime"}:
        return _metadata_protocol_passes(report, key, identity, machine, broker_sha256)
    if key == "metadata_preserved":
        if (report.get("schema") != METADATA_SCHEMA or report.get("complete") is not True
                or report.get("ok") is not True or report.get("bundle_identity") != identity
                or not machine or report.get("machine_guid") != machine):
            return False
        cases = report.get("cases")
        if not isinstance(cases, list) or any(not isinstance(c, dict) for c in cases):
            return False
        names = [c.get("case") for c in cases]
        return (all(isinstance(n, str) for n in names) and len(set(names)) == len(names)
                and METADATA_CASES <= set(names) and all(c.get("ok") is True for c in cases))
    if (report.get("schema") != SCHEMA or report.get("complete") is not True
            or report.get("bundle_identity") != identity
            or not machine or report.get("machine_guid") != machine):
        return False
    cases = report.get("cases")
    required = REQUIRED_CASES.get(key)
    if not isinstance(cases, list) or not required:
        return False
    indexed = {}
    for case in cases:
        if not isinstance(case, dict):
            return False
        fixture, name = case.get("fixture"), case.get("case")
        if not isinstance(fixture, str) or not isinstance(name, str):
            return False
        if (fixture, name) in indexed:
            return False
        indexed[fixture, name] = case
    fixtures = ("c-volume", "e-volume") if key == "compat_paths" else ("c-volume",)
    for fixture in fixtures:
        for name in required:
            case = indexed.get((fixture, name), {})
            expected_status = "cancelled" if name == "process_tree_cancelled" else "exited"
            if (case.get("ok") is not True or case.get("failures") != []
                    or case.get("status") != expected_status
                    or case.get("cleanup_confirmed") is not True
                    or case.get("backend") != "mewcode-windows-lpac"
                    or case.get("output_truncated") is not False):
                return False
            if expected_status == "exited" and type(case.get("exit_code")) is not int:
                return False
            if expected_status == "exited":
                denied_write = name in {"git_config_denied", "git_hook_write_denied"}
                if (case["exit_code"] != 0) != denied_write:
                    return False
            if name.startswith("process_tree_") and (case.get("child_reaped") is not True
                                                      or case.get("child_started") is not True):
                return False
    return True
