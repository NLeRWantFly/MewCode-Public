from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path

import pytest

from scripts.replay_permission_policy import (
    PINNED_BASELINE,
    build_fixture,
    report,
    sanitize_audit_line,
    verify_fixture,
)


FIXTURE = Path(__file__).parent / "fixtures" / "permission-replay"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def workspace_tmp(request: pytest.FixtureRequest) -> Path:
    base = ROOT / ".tmp"
    base.mkdir(exist_ok=True)
    manager = tempfile.TemporaryDirectory(prefix="permission-replay-test-", dir=base)
    request.addfinalizer(manager.cleanup)
    return Path(manager.name)


def _json_line(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False)


def test_audit_sanitizer_keeps_only_categories_and_redacts_paths() -> None:
    row = {
        "tool_name": "ReadFile",
        "decision": "ask",
        "source_layer": "sandbox_path",
        "session_id": "private-session",
        "params_summary": _json_line({
            "file_path": r"E:\work\reports\status.md", "offset": 0, "limit": 10,
        }),
        "reason": "private reason text",
    }
    sanitized = sanitize_audit_line(_json_line(row), ordinal=1, workspace_root=r"E:\work")
    assert sanitized == {
        "id": "audit-000001", "tool": "ReadFile", "category": "read",
        "command_head": "none", "path_class": "workspace_internal", "decision": "ask",
        "source_layer": "sandbox_path", "truncated": False, "quality": "complete",
    }
    assert "private-session" not in json.dumps(sanitized)
    assert "E:\\work" not in json.dumps(sanitized)
    assert "reason" not in sanitized


def test_truncated_parameter_preview_is_unknown_and_not_replayable() -> None:
    params = '{"command":"python tools/check.py", "path":"private-value"}'
    params = (params + (" " * 200))[:200]
    raw = _json_line({
        "tool_name": "Bash", "decision": "allow", "source_layer": "auto_safety_reviewer",
        "params_summary": params,
    })
    sanitized = sanitize_audit_line(raw, ordinal=8, workspace_root=r"E:\work")
    assert sanitized["truncated"] is True
    assert sanitized["quality"] == "unknown"
    assert sanitized["command_head"] == "unknown"
    assert sanitized["path_class"] == "unknown"
    assert "private-value" not in json.dumps(sanitized)


def test_build_fixture_pins_documented_baseline_and_records_source_drift(workspace_tmp: Path) -> None:
    source = workspace_tmp / "decisions.jsonl"
    source.write_text("\n".join([
        _json_line({"tool_name": "Bash", "decision": "allow", "source_layer": "auto_safety_reviewer",
                    "params_summary": _json_line({"command": "python tools/a.py"})}),
        _json_line({"tool_name": "ReadFile", "decision": "ask", "source_layer": "sandbox_path",
                    "params_summary": _json_line({"file_path": r"C:\outside\ordinary.txt"})}),
        _json_line({"tool_name": "Agent", "decision": "user_allow", "source_layer": "human_decision",
                    "params_summary": "{}"}),
    ]) + "\n", encoding="utf-8")
    out = workspace_tmp / "fixture"
    out.mkdir()
    (out / "adversarial.jsonl").write_bytes((FIXTURE / "adversarial.jsonl").read_bytes())
    manifest = build_fixture(source, out, workspace_root=r"E:\work")
    assert manifest["policy_baseline"] == PINNED_BASELINE
    assert manifest["provenance"]["source_line_count"] == 3
    assert manifest["provenance"]["documented_source_line_count"] == 489
    assert manifest["provenance"]["source_count_discrepancy"] is True
    assert manifest["source_observed_metrics"]["workspace_external_read_asks"] == 1
    assert manifest["source_observed_metrics"]["human_decisions"] == 1
    assert manifest["provenance"]["raw_parameters_persisted"] is False
    assert hashlib.sha256((out / "decisions.jsonl").read_bytes()).hexdigest() == manifest["decisions_sha256"]
    loaded, records, cases = verify_fixture(out)
    assert loaded == manifest
    assert len(records) == 3
    assert len(cases) >= 30


def test_checked_in_fixture_is_sanitized_and_has_required_adversarial_coverage() -> None:
    manifest, records, cases = verify_fixture(FIXTURE)
    assert manifest["policy_baseline"] == PINNED_BASELINE
    assert manifest["provenance"]["source_path_redacted"] is True
    assert manifest["provenance"]["raw_parameters_persisted"] is False
    assert len(records) == manifest["provenance"]["sanitized_record_count"]
    assert len(records) == manifest["provenance"]["source_line_count"]
    assert len(cases) >= 30
    required = {
        "shell_chain_smuggles_recursive_delete", "relative_path_traversal",
        "junction_write_escape", "environment_credential_then_network",
        "ssh_private_key_read", "provider_config_read", "remote_http_download",
        "powershell_http_request", "git_remote_push", "python_package_install",
    }
    assert required.issubset({case["scenario"] for case in cases})
    raw = b"".join((FIXTURE / name).read_bytes() for name in
                   ("manifest.json", "decisions.jsonl", "adversarial.jsonl"))
    assert re.search(rb"sk-|api_key|Bearer", raw) is None
    for row in records:
        assert set(row) == {
            "id", "tool", "category", "command_head", "path_class", "decision",
            "source_layer", "truncated", "quality",
        }
        if row["quality"] == "unknown":
            assert row["command_head"] == "unknown"
            assert row["path_class"] == "unknown"


def test_report_does_not_count_unknown_rows_as_success_and_has_zero_adversarial_allows() -> None:
    result = report(FIXTURE)
    assert result["policy_versions"]["baseline"] == PINNED_BASELINE["policy_id"]
    assert result["baseline_mismatch"]["kind"] == "source_snapshot_drift_not_policy_regression"
    assert result["baseline_mismatch"]["detected"] is True
    assert result["coverage"]["unknown"] > 0
    assert result["coverage"]["unknown_counted_as_success"] == 0
    assert result["adversarial_candidate"]["total"] >= 30
    assert result["adversarial_candidate"]["allow"] == 0
    assert result["assertions"]["adversarial_allow_count_zero"] is True
    assert "adv-002" not in result["adversarial_candidate"]["allowed_case_ids"]


def test_real_symlink_escape_is_checked_or_marked_unknown() -> None:
    result = report(FIXTURE)
    candidate = result["adversarial_candidate"]
    if candidate["junction_escape"] == "unknown":
        pytest.skip("Windows did not allow creating the temporary directory symlink")
    assert candidate["junction_escape"] == "verified_symlink"
    assert "adv-032" not in candidate["allowed_case_ids"]


def test_assert_fails_closed_for_drift_or_unknown_rows() -> None:
    with pytest.raises(AssertionError, match="permission replay gate failed"):
        report(FIXTURE, assert_integrity=True)


def test_fixture_digest_mismatch_fails_closed(workspace_tmp: Path) -> None:
    for name in ("manifest.json", "decisions.jsonl", "adversarial.jsonl"):
        (workspace_tmp / name).write_bytes((FIXTURE / name).read_bytes())
    with (workspace_tmp / "decisions.jsonl").open("ab") as output:
        output.write(b"{}\n")
    with pytest.raises(ValueError, match="digest mismatch"):
        verify_fixture(workspace_tmp)
