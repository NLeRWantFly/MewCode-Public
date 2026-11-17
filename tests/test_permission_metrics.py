from __future__ import annotations

import json
import math
import builtins
import hashlib
from pathlib import Path
from typing import Any

import pytest

import mewcode.permissions.audit as audit_module
from mewcode.agent import PermissionResponse
from mewcode.permissions.audit import AuditLogger
from mewcode.permissions.audit import ARGS_SCHEMA, canonical_args_sha256


def _write_rows(logger: AuditLogger, rows: list[dict[str, Any]]) -> None:
    path = logger._get_current_file()
    with path.open("a", encoding="utf-8") as output:
        for row in rows:
            output.write(json.dumps(row, ensure_ascii=False, allow_nan=True) + "\n")


def test_summary_keeps_old_shape_and_counts_old_records_as_unknown(tmp_path: Path) -> None:
    logger = AuditLogger(str(tmp_path), session_id="old-session")
    _write_rows(logger, [
        {
            "session_id": "old-session", "tool_name": "Bash",
            "params_summary": "python -c 'print(secret_value)'",
            "reason": "private credential detail", "decision": "ask",
            "source_layer": "sandbox_path",
        },
        {
            "session_id": "old-session", "decision": "allow",
            "source_layer": "auto_safety_reviewer",
        },
        {
            "session_id": "old-session", "source_layer": "auto_safety_reviewer",
        },
    ])

    summary = logger.summary()

    assert summary["count"] == 3
    assert summary["by_source"]["auto_safety_reviewer"] == 2
    assert summary["by_decision"]["ask"] == 1
    assert summary["ask_count"] == 1
    assert summary["model_reviews"] == {
        "count": 2,
        "allow_count": 1,
        "deny_count": 0,
        "unknown_outcome_count": 1,
        "valid_outcome_count": 1,
        "cache_reuse_count": 0,
        "uncached_decision_count": 0,
        "allow_rate_percent": 50.0,
    }
    assert summary["unknown_field_counts"]["permission_mode"] == 3
    assert summary["unknown_field_counts"]["execution_backend"] == 3
    assert summary["unknown_field_counts"]["escalation_requested"] == 3
    assert summary["unknown_field_counts"]["cache_hit"] == 3
    assert summary["out_of_scope_request_count"] == 1
    assert summary["waiting_for_user_ms"]["total"] is None
    assert summary["human_wait_ms"] == {"p50": 0.0, "p95": 0.0, "total": 0}
    serialized = json.dumps(summary, ensure_ascii=False)
    assert "secret_value" not in serialized
    assert "private credential detail" not in serialized


def test_new_fields_are_optional_and_logged_when_provided(tmp_path: Path) -> None:
    logger = AuditLogger(str(tmp_path), session_id="s")
    logger.log_decision(
        tool_name="Bash",
        params_summary="powershell -Command Write-Output 'private-command'",
        decision="user_allow",
        source_layer="human_decision",
        reason="private reason",
        permission_mode="auto",
        execution_backend="native_windows",
        operation_id="op-42",
        args_sha256=hashlib.sha256(b'{"command":"private-command"}').hexdigest(),
        args_schema=ARGS_SCHEMA,
        params_summary_truncated=True,
        prior_decision="DENY",
        escalation_requested=False,
        user_choice=PermissionResponse.ALLOW_ALWAYS.value,
        wait_ms=125.25,
    )
    logger.log_decision(
        tool_name="ReadFile", params_summary="x", decision="allow",
        source_layer="safe_readonly",
    )
    rows = [json.loads(line) for line in logger._get_current_file().read_text(encoding="utf-8").splitlines()]

    assert rows[0]["permission_mode"] == "auto"
    assert rows[0]["execution_backend"] == "native_windows"
    assert rows[0]["operation_id"] == "op-42"
    assert rows[0]["args_sha256"] == hashlib.sha256(b'{"command":"private-command"}').hexdigest()
    assert rows[0]["args_schema"] == ARGS_SCHEMA
    assert rows[0]["params_summary_truncated"] is True
    assert rows[0]["prior_decision"] == "deny"
    assert rows[0]["escalation_requested"] is False
    assert not {
        "permission_mode", "execution_backend", "operation_id",
        "prior_decision", "escalation_requested", "args_sha256", "args_schema",
        "params_summary_truncated",
    }.intersection(rows[1])
    summary = logger.summary()
    assert summary["manual_deny_override_count"] == 1
    assert summary["waiting_for_user_ms"]["total"] == 125.25
    assert "private-command" not in json.dumps(summary)
    assert "private reason" not in json.dumps(summary)


def test_argument_fingerprint_validation_and_legacy_missing_fields_stay_unknown(tmp_path: Path) -> None:
    logger = AuditLogger(str(tmp_path), session_id="s")
    arguments = {"token": "credential-marker-not-persisted"}
    digest = canonical_args_sha256(arguments)
    assert digest is not None
    logger.log_decision(
        tool_name="ReadFile", params_summary="safe summary", decision="allow",
        source_layer="mode", args_sha256=digest, args_schema=ARGS_SCHEMA,
        params_summary_truncated=False,
    )
    logger.log_decision(
        tool_name="ReadFile", params_summary="safe summary", decision="allow",
        source_layer="mode", args_sha256="A" * 64, args_schema=ARGS_SCHEMA,
        params_summary_truncated="false",
    )
    logger.log_decision(
        tool_name="ReadFile", params_summary="safe summary", decision="allow",
        source_layer="mode", args_sha256="b" * 64, args_schema="unknown-encoding-v1",
        params_summary_truncated=False,
    )
    _write_rows(logger, [{
        "session_id": "s", "tool_name": "ReadFile", "decision": "allow",
        "source_layer": "mode",
    }])

    rows = [json.loads(line) for line in logger._get_current_file().read_text(encoding="utf-8").splitlines()]
    assert rows[0]["args_sha256"] == digest
    assert rows[0]["args_schema"] == ARGS_SCHEMA
    assert rows[0]["params_summary_truncated"] is False
    assert not {"args_sha256", "args_schema", "params_summary_truncated"}.intersection(rows[1])
    assert not {"args_sha256", "args_schema"}.intersection(rows[2])
    assert rows[2]["params_summary_truncated"] is False
    assert not {"args_sha256", "args_schema", "params_summary_truncated"}.intersection(rows[3])
    serialized = json.dumps(rows, ensure_ascii=False)
    assert "credential-marker-not-persisted" not in serialized
    assert '"arguments"' not in serialized


def test_logger_marks_its_own_summary_truncation(tmp_path: Path) -> None:
    logger = AuditLogger(str(tmp_path), session_id="s")
    logger.log_decision(
        tool_name="ReadFile", params_summary="x" * 201, decision="allow",
        source_layer="mode", args_sha256="a" * 64, args_schema=ARGS_SCHEMA,
        params_summary_truncated=False,
    )
    row = json.loads(logger._get_current_file().read_text(encoding="utf-8"))
    assert row["params_summary"] == "x" * 197 + "..."
    assert row["params_summary_truncated"] is True


def test_empty_or_conclusion_free_reviews_return_unknown_rate(tmp_path: Path) -> None:
    logger = AuditLogger(str(tmp_path), session_id="s")
    empty = logger.summary()
    assert empty["count"] == 0
    assert empty["model_reviews"]["allow_rate_percent"] is None
    assert empty["waiting_for_user_ms"]["p50"] is None
    assert empty["truncation"]["truncated"] is False
    assert empty["unavailable"] is False

    _write_rows(logger, [{
        "session_id": "s", "source_layer": "auto_safety_reviewer",
        "decision": None,
    }])
    summary = logger.summary()
    assert summary["model_reviews"]["count"] == 1
    assert summary["model_reviews"]["unknown_outcome_count"] == 1
    assert summary["model_reviews"]["allow_count"] == 0
    assert summary["model_reviews"]["allow_rate_percent"] is None


def test_omitted_measurements_are_not_zero_ms_observations(tmp_path: Path) -> None:
    logger = AuditLogger(str(tmp_path))
    logger.log_decision(tool_name="ReadFile", params_summary="", decision="allow", source_layer="mode")
    result = logger.summary()
    assert result["timing_quality"]["latency_unknown_count"] == 1


def test_duration_total_overflow_keeps_json_finite(tmp_path: Path) -> None:
    logger = AuditLogger(str(tmp_path))
    for _ in range(2):
        logger.log_decision(tool_name="ReadFile", params_summary="", decision="user_allow",
                            source_layer="human_decision", wait_ms=1e308, latency_ms=1e308)
    result = logger.summary()
    assert result["waiting_for_user_ms"]["total"] is None
    assert result["latency_ms"]["total"] is None
    json.dumps(result, allow_nan=False)


def test_legacy_wait_percentiles_are_order_independent(tmp_path: Path) -> None:
    logger = AuditLogger(str(tmp_path))
    for delay in (100, 1, 50):
        logger.log_decision(tool_name="ReadFile", params_summary="", decision="user_allow",
                            source_layer="human_decision", wait_ms=delay)
    result = logger.summary()
    assert result["human_wait_ms"]["p50"] == 50
    assert result["human_wait_ms"]["p50"] == result["waiting_for_user_ms"]["p50"]


def test_metrics_require_explicit_override_evidence_and_use_observed_boundaries(tmp_path: Path) -> None:
    logger = AuditLogger(str(tmp_path), session_id="s")
    rows = [
        {
            "session_id": "s", "source_layer": "auto_safety_reviewer",
            "decision": "allow", "cache_hit": False,
        },
        {
            "session_id": "s", "source_layer": "auto_safety_reviewer",
            "decision": "deny", "cache_hit": True,
        },
        {
            "session_id": "s", "source_layer": "auto_safety_reviewer",
        },
        {
            "session_id": "s", "source_layer": "human_decision",
            "decision": "user_allow", "user_choice": "allow",
            "prior_decision": "deny", "operation_id": "same-op", "wait_ms": 100,
        },
        {
            "session_id": "s", "source_layer": "human_decision",
            "decision": "user_allow", "user_choice": "allow",
            "prior_decision": "deny", "operation_id": "same-op", "wait_ms": 120,
        },
        {
            "session_id": "s", "source_layer": "human_decision",
            "decision": "user_allow", "user_choice": "allow", "wait_ms": 50,
        },
        {
            "session_id": "s", "source_layer": "hitl",
            "decision": "allow", "user_choice": "allow", "prior_decision": "deny",
        },
        {
            "session_id": "s", "source_layer": "invariant_guardrail",
            "decision": "deny",
        },
        {
            "session_id": "s", "source_layer": "auto_safety_reviewer",
            "decision": "deny",
        },
        {
            "session_id": "s", "source_layer": "sandbox_path",
            "decision": "ask",
        },
        {
            "session_id": "s", "source_layer": "mode",
            "decision": "deny",
            "escalation_requested": True,
        },
    ]
    _write_rows(logger, rows)

    summary = logger.summary()

    assert summary["ask_count"] == 1
    assert summary["model_reviews"]["count"] == 4
    assert summary["model_reviews"]["unknown_outcome_count"] == 1
    assert summary["model_reviews"]["allow_rate_percent"] == 25.0
    assert summary["model_reviews"]["cache_reuse_count"] == 1
    assert summary["model_reviews"]["uncached_decision_count"] == 1
    assert summary["unknown_field_counts"]["cache_hit"] == 9
    # Count unique associated operations, and require every piece of evidence.
    assert summary["manual_deny_override_count"] == 1
    assert summary["manual_deny_override_unknown_count"] == 2
    assert summary["boundary_denial_count"] == 2
    assert summary["out_of_scope_request_count"] == 2
    assert summary["waiting_for_user_ms"]["valid_count"] == 3
    assert summary["waiting_for_user_ms"]["total"] == 270


def test_malformed_delays_are_ignored_and_counted(tmp_path: Path) -> None:
    logger = AuditLogger(str(tmp_path), session_id="s")
    invalid_values = [float("nan"), float("inf"), -3, "not-a-number"]
    _write_rows(logger, [
        {
            "session_id": "s", "source_layer": "human_decision",
            "decision": "user_allow", "latency_ms": value, "wait_ms": value,
        }
        for value in invalid_values
    ])
    # The writer also handles malformed inputs without failing or persisting them.
    logger.log_decision(
        tool_name="Bash", params_summary="x", decision="deny",
        source_layer="rate_limit", latency_ms="bad", wait_ms=math.inf,
    )

    summary = logger.summary()

    assert summary["latency_ms"] == {"p50": 0.0, "p95": 0.0, "total": 0}
    assert summary["timing_quality"]["latency_invalid_count"] == 5
    assert summary["timing_quality"]["wait_invalid_count"] == 5
    assert summary["waiting_for_user_ms"]["p50"] is None
    assert summary["waiting_for_user_ms"]["total"] is None
    assert summary["waiting_for_user_ms"]["invalid_count"] == 4


def test_summary_keeps_session_filter_and_reports_sample_truncation(tmp_path: Path) -> None:
    session_a = AuditLogger(str(tmp_path), session_id="a")
    session_b = AuditLogger(str(tmp_path), session_id="b")
    for decision in ("allow", "deny", "ask"):
        session_a.log_decision(
            tool_name="ReadFile", params_summary="x", decision=decision,
            source_layer="mode",
        )
    session_b.log_decision(
        tool_name="WriteFile", params_summary="x", decision="deny",
        source_layer="mode",
    )

    summary_a = session_a.summary(limit=2)
    summary_b = session_b.summary()

    assert summary_a["count"] == 2
    assert summary_a["truncation"]["truncated"] is True
    assert summary_a["truncation"]["samples_omitted"] == 1
    assert summary_a["ask_count"] == 1
    assert summary_b["count"] == 1
    assert summary_b["by_decision"] == {"deny": 1}

    unbound = AuditLogger(str(tmp_path))
    assert unbound.summary(session_id="a")["count"] == 3
    assert unbound.summary(session_id="b")["count"] == 1
    assert unbound.summary(session_id="")["count"] == 4


def test_non_scalar_source_and_decision_labels_are_unknown_and_not_echoed(tmp_path: Path) -> None:
    logger = AuditLogger(str(tmp_path), session_id="s")
    _write_rows(logger, [{
        "session_id": "s",
        "source_layer": ["private-command-credential"],
        "decision": {"raw": "private-command-credential"},
        "params_summary": "private-command-credential",
    }, {
        "session_id": "s",
        "source_layer": "private-command-credential-" + ("x" * 100),
        "decision": "private-command-credential-" + ("y" * 100),
    }])

    summary = logger.summary()

    assert summary["by_source"] == {"unknown": 2}
    assert summary["by_decision"] == {"unknown": 2}
    assert summary["unknown_field_counts"]["source_layer"] == 2
    assert summary["unknown_field_counts"]["decision"] == 2
    assert "private-command-credential" not in json.dumps(summary)


def test_summary_read_error_is_explicit_but_missing_file_is_empty(tmp_path: Path, monkeypatch) -> None:
    logger = AuditLogger(str(tmp_path), session_id="s")
    absent = logger.summary()
    assert absent["count"] == 0
    assert absent["unavailable"] is False

    def fail_open(*_args, **_kwargs):
        raise PermissionError("private path must not appear")

    monkeypatch.setattr(audit_module, "open", fail_open, raising=False)
    unavailable = logger.summary()
    assert unavailable["unavailable"] is True
    assert unavailable["error_kind"] == "PermissionError"
    assert unavailable["count"] is None
    assert unavailable["ask_count"] is None
    assert unavailable["model_reviews"]["allow_rate_percent"] is None
    assert "private path" not in json.dumps(unavailable)


def test_summary_reads_only_the_file_size_seen_at_open(tmp_path: Path, monkeypatch) -> None:
    logger = AuditLogger(str(tmp_path), session_id="s")
    path = logger._get_current_file()
    first = {
        "session_id": "s", "decision": "allow", "source_layer": "mode",
    }
    second = {
        "session_id": "s", "decision": "deny", "source_layer": "mode",
    }
    path.write_text(json.dumps(first) + "\n", encoding="utf-8")
    real_open = builtins.open

    class AppendOnRead:
        def __init__(self, stream):
            self.stream = stream
            self.appended = False

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def seek(self, *args):
            return self.stream.seek(*args)

        def tell(self):
            return self.stream.tell()

        def readline(self, size=-1):
            if not self.appended:
                self.appended = True
                with real_open(path, "a", encoding="utf-8") as output:
                    output.write(json.dumps(second) + "\n")
            return self.stream.readline(size)

    def append_during_summary(file, mode="r", *args, **kwargs):
        stream = real_open(file, mode, *args, **kwargs)
        if Path(file) == path and mode == "rb":
            return AppendOnRead(stream)
        return stream

    monkeypatch.setattr(audit_module, "open", append_during_summary, raising=False)
    summary = logger.summary()

    assert summary["count"] == 1
    assert summary["by_decision"] == {"allow": 1}


def test_summary_skips_a_line_over_the_configured_limit(tmp_path: Path, monkeypatch) -> None:
    logger = AuditLogger(str(tmp_path), session_id="s")
    monkeypatch.setattr(audit_module, "MAX_SUMMARY_LINE_BYTES", 128)
    valid = json.dumps({
        "session_id": "s", "decision": "deny", "source_layer": "mode",
    }).encode("utf-8")
    logger._get_current_file().write_bytes(b"x" * 129 + b"\n" + valid + b"\n")

    summary = logger.summary()

    assert summary["count"] == 1
    assert summary["by_decision"] == {"deny": 1}
    assert summary["truncation"]["truncated"] is True
    assert summary["truncation"]["oversized_lines"] == 1


@pytest.mark.parametrize("bad_record", [
    b"[" * 2000 + b"0" + b"]" * 2000,
    b'{"number":' + b"9" * 5000 + b"}",
    b"[]", b"null", b"42", b'"not an audit record"', b"\xff",
], ids=["deep-json", "huge-integer", "array", "null", "integer", "string", "invalid-utf8"])
def test_corrupt_records_do_not_break_summary_or_query(tmp_path: Path, bad_record: bytes) -> None:
    logger = AuditLogger(str(tmp_path), session_id="s")
    valid = {"session_id": "s", "decision": "deny", "source_layer": "mode"}
    logger._get_current_file().write_bytes(bad_record + b"\n" + json.dumps(valid).encode() + b"\n")
    summary = logger.summary()
    assert summary["count"] == 1
    assert summary["truncation"]["invalid_lines"] == 1
    assert summary["truncation"]["truncated"] is True
    assert logger.query(decision="deny") == [valid]


def test_bounded_tail_preserves_complete_first_record(tmp_path: Path, monkeypatch) -> None:
    logger = AuditLogger(str(tmp_path), session_id="s")
    valid = {"session_id": "s", "decision": "deny", "source_layer": "mode"}
    line = json.dumps(valid).encode() + b"\n"
    logger._get_current_file().write_bytes(b"{}\n" + line)
    monkeypatch.setattr(audit_module, "MAX_SUMMARY_FILE_BYTES", len(line))
    summary = logger.summary()
    assert summary["count"] == 1
    assert summary["truncation"]["older_bytes_omitted"] == 3
    assert summary["truncation"]["truncated"] is True
