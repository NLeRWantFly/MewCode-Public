"""权限审计日志。

所有工具执行决策（allow/deny/ask）写入结构化审计日志，
存储在 .mewcode/audit/decisions.jsonl。
"""

from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import re
import time
from collections import Counter, deque
from datetime import datetime, timezone
from numbers import Real
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

AUDIT_DIR = ".mewcode/audit"
AUDIT_FILE = "decisions.jsonl"
MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB
MAX_ARCHIVE_FILES = 10
MAX_SUMMARY_FILE_BYTES = 64 * 1024 * 1024
MAX_SUMMARY_LINE_BYTES = 1024 * 1024
MAX_SUMMARY_SAMPLES = 100_000
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_ARGS_SHA256 = re.compile(r"^[0-9a-f]{64}$")
ARGS_SCHEMA = "json-sort-keys-compact-utf8-v1"


def canonical_args_sha256(arguments: Any) -> str | None:
    """Hash complete JSON arguments using the versioned canonical encoding.

    The serialized arguments are kept in memory only; callers persist only this
    digest. Non-JSON values are unknown rather than stringified ambiguously.
    """
    try:
        canonical = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        return None
    return hashlib.sha256(canonical).hexdigest()

# These are explicit enforcement layers observed in PermissionChecker and its
# callers. Reviewer outcomes and ordinary user prompts are intentionally not
# classified as boundary denials.
_BOUNDARY_DENIAL_LAYERS = frozenset({
    "permission_authority_guardrail",
    "invariant_guardrail",
    "sensitive_read_mask",
    "auto_hard_deny",
    "terminal_boundary",
    "native_boundary_unverified",
    "isolation_guardrail",
    "mode",
    "plan_mode",
    "host_tool_scope",
    "path_escape_guardrail",
    "sandbox",
    "grant_store_guardrail",
    "authorization_race",
    "child_write_scope",
    "node_write_scope",
})


def _valid_duration_ms(value: Any) -> float | None:
    """Return a finite, non-negative duration or None for malformed data."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, Real):
            duration = float(value)
        elif isinstance(value, str):
            duration = float(value.strip())
        else:
            return None
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(duration) or duration < 0:
        return None
    return round(duration, 2)


def _audit_duration_ms(value: Any) -> float | str | None:
    """Store a safe marker for malformed supplied durations, never their value."""
    if value is None:
        return None
    parsed = _valid_duration_ms(value)
    return parsed if parsed is not None else "invalid"


def _optional_text(value: Any, *, limit: int = 128) -> str | None:
    if hasattr(value, "value"):
        value = value.value
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned[:limit] or None


def _safe_label(value: Any) -> str:
    """Keep only short scalar labels; never stringify arbitrary audit values."""
    if not isinstance(value, str):
        return "unknown"
    cleaned = value.strip()
    if not _SAFE_LABEL.fullmatch(cleaned):
        return "unknown"
    return cleaned.lower()


def _decode_audit_record(raw_line: bytes) -> dict[str, Any] | None:
    """Corrupt or non-object rows are missing evidence, never API failures."""
    try:
        item = json.loads(raw_line.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        # ValueError also covers the decoder's oversized-integer limit.
        return None
    return item if isinstance(item, dict) else None


def _percentile(values: list[float], proportion: float) -> float:
    if not values:
        return 0.0
    return round(values[min(len(values) - 1, int((len(values) - 1) * proportion))], 2)


def _total_ms(values: list[float]) -> float | None:
    try:
        total = math.fsum(values)
    except OverflowError:
        return None
    return round(total, 2) if math.isfinite(total) else None


def _hitl_allow(row: dict[str, Any]) -> bool:
    layer = _safe_label(row.get("source_layer"))
    decision = _safe_label(row.get("decision"))
    if layer not in {"human_decision", "hitl"}:
        return False
    if decision not in {"user_allow", "hitl_allow"} and not (layer == "hitl" and decision == "allow"):
        return False
    raw_choice = row.get("user_choice")
    if raw_choice is None or raw_choice == "":
        return True
    choice = _safe_label(raw_choice)
    return choice in {
        "allow", "user_allow", "allow_always", "allow_directory",
    }


def _is_known_decision(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() in {
        "allow", "deny", "ask", "review", "user_allow", "user_deny",
    }


def _unavailable_summary(error_kind: str, sample_limit: int) -> dict[str, Any]:
    """Return an explicit unavailable result without implying zero activity."""
    none_timing = {"p50": None, "p95": None, "total": None}
    return {
        "count": None,
        "by_source": None,
        "by_decision": None,
        "cache_hits": None,
        "latency_ms": dict(none_timing),
        "human_wait_ms": dict(none_timing),
        "ask_count": None,
        "model_reviews": {
            "count": None,
            "allow_count": None,
            "deny_count": None,
            "unknown_outcome_count": None,
            "valid_outcome_count": None,
            "cache_reuse_count": None,
            "uncached_decision_count": None,
            "allow_rate_percent": None,
        },
        "manual_deny_override_count": None,
        "manual_deny_override_unknown_count": None,
        "boundary_denial_count": None,
        "out_of_scope_request_count": None,
        "waiting_for_user_ms": {
            "p50": None, "p95": None, "total": None,
            "valid_count": None, "invalid_count": None, "unknown_count": None,
        },
        "timing_quality": {
            "latency_invalid_count": None,
            "latency_unknown_count": None,
            "wait_invalid_count": None,
            "wait_unknown_count": None,
        },
        "unknown_field_counts": {
            "permission_mode": None,
            "execution_backend": None,
            "operation_id": None,
            "prior_decision": None,
            "escalation_requested": None,
            "cache_hit": None,
            "source_layer": None,
            "decision": None,
        },
        "truncation": {
            "truncated": True,
            "sample_limit": sample_limit,
            "samples_omitted": None,
            "older_bytes_omitted": None,
            "snapshot_bytes_unread": None,
            "file_omission_count_unknown": True,
            "invalid_lines": None,
            "oversized_lines": None,
            "max_line_bytes": MAX_SUMMARY_LINE_BYTES,
            "max_file_bytes_scanned": MAX_SUMMARY_FILE_BYTES,
        },
        "unavailable": True,
        "error_kind": error_kind,
    }


class AuditLogger:
    """结构化审计日志记录器。"""

    def __init__(self, work_dir: str, session_id: str = "") -> None:
        self._audit_dir = Path(work_dir) / AUDIT_DIR
        self._audit_dir.mkdir(parents=True, exist_ok=True)
        self._session_id = session_id

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def log_decision(
        self,
        *,
        tool_name: str,
        params_summary: str,
        decision: str,
        source_layer: str,
        rule_id: str = "",
        latency_ms: float | None = None,
        risk: str = "",
        reason: str = "",
        reviewer: str = "",
        cache_hit: bool = False,
        review_route: dict | None = None,
        session_id: str | None = None,
        wait_ms: float | None = None,
        authorization_scope: str = "",
        user_choice: str = "",
        subcondition: str = "",
        clearance: str = "",
        permission_mode: str | None = None,
        execution_backend: str | None = None,
        operation_id: str | None = None,
        prior_decision: str | None = None,
        escalation_requested: bool | None = None,
        args_sha256: str | None = None,
        args_schema: str | None = None,
        params_summary_truncated: bool | None = None,
    ) -> None:
        """记录一次权限决策。

        Args:
            tool_name: 工具名称。
            params_summary: 参数摘要（截断到 200 字符）。
            decision: allow / deny / ask。
            source_layer: 决策来源层：safe_readonly / dangerous / sandbox /
                         rule_engine / mode / hitl / plan_mode / harness / rate_limit。
            rule_id: 若来自规则引擎，记录规则标识。
            latency_ms: 决策耗时（毫秒）。
        """
        params_summary = re.sub(
            r"(?i)(api[_-]?key|token|password|secret|authorization)(['\"\s:=]+)[^,}\s]+",
            r"\1\2[REDACTED]",
            params_summary,
        )
        # 截断参数摘要
        summary_truncated_by_logger = len(params_summary) > 200
        if summary_truncated_by_logger:
            params_summary = params_summary[:197] + "..."

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool_name": tool_name,
            "params_summary": params_summary,
            "decision": decision,
            "source_layer": source_layer,
            "layer": source_layer,
            "subcondition": subcondition or source_layer,
            "clearance": clearance or (
                "use_safe_alternative_or_request_specific_authorization"
                if decision in {"deny", "ask"} else ""
            ),
            "rule_id": rule_id,
            "latency_ms": _audit_duration_ms(latency_ms),
            "session_id": self._session_id if session_id is None else session_id,
            "risk": risk,
            "reason": reason[:300],
            "reviewer": reviewer,
            "cache_hit": bool(cache_hit),
            "review_route": review_route or {},
            "wait_ms": _audit_duration_ms(wait_ms),
            "authorization_scope": authorization_scope,
            "user_choice": user_choice,
        }
        # Keep these additions optional so existing integrations and JSONL
        # readers continue to see the original record shape unless they opt in.
        for field_name, value in (
            ("permission_mode", permission_mode),
            ("execution_backend", execution_backend),
            ("operation_id", operation_id),
        ):
            cleaned = _optional_text(value)
            if cleaned is not None:
                entry[field_name] = cleaned
        prior = _optional_text(prior_decision, limit=32)
        if prior is not None:
            entry["prior_decision"] = prior.lower()
        if isinstance(escalation_requested, bool):
            entry["escalation_requested"] = escalation_requested
        if (
            isinstance(args_sha256, str)
            and _ARGS_SHA256.fullmatch(args_sha256)
            and args_schema == ARGS_SCHEMA
        ):
            entry["args_sha256"] = args_sha256
            entry["args_schema"] = ARGS_SCHEMA
        if isinstance(params_summary_truncated, bool) or summary_truncated_by_logger:
            entry["params_summary_truncated"] = (
                bool(params_summary_truncated) or summary_truncated_by_logger
            )

        file_path = self._get_current_file()
        try:
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            log.error("[audit] failed to write: %s", e)

        # 检查是否需要 rotate
        self._maybe_rotate(file_path)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def query(
        self,
        *,
        tool_name: str | None = None,
        decision: str | None = None,
        source_layer: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """查询审计日志。

        Args:
            tool_name: 按工具名过滤。
            decision: 按决策类型过滤。
            source_layer: 按决策来源过滤。
            limit: 返回记录数上限。
            offset: 跳过前 N 条。

        Returns:
            匹配的审计记录列表。
        """
        results: list[dict[str, Any]] = []
        file_path = self._get_current_file()

        if not file_path.exists():
            return results

        try:
            with open(file_path, "rb") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    entry = _decode_audit_record(line)
                    if entry is None:
                        continue

                    if tool_name and entry.get("tool_name") != tool_name:
                        continue
                    if decision and entry.get("decision") != decision:
                        continue
                    if source_layer and entry.get("source_layer") != source_layer:
                        continue

                    results.append(entry)
        except OSError as e:
            log.error("[audit] failed to read: %s", e)

        return results[offset : offset + limit]

    def summary(
        self,
        *,
        limit: int = 10000,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Summarize recent records without returning raw arguments or reasons."""
        effective_session_id = self._session_id if session_id is None else session_id
        try:
            requested_limit = int(limit)
        except (TypeError, ValueError, OverflowError):
            requested_limit = 10000
        sample_limit = max(1, min(requested_limit, MAX_SUMMARY_SAMPLES))
        recent: deque[dict[str, Any]] = deque(maxlen=sample_limit)
        matched_samples = 0
        invalid_lines = 0
        oversized_lines = 0
        truncated_by_file_size = False
        snapshot_short_read = False
        snapshot_bytes_unread = 0
        omitted_bytes = 0
        read_error_kind: str | None = None
        file_path = self._get_current_file()
        try:
            with open(file_path, "rb") as source:
                source.seek(0, os.SEEK_END)
                file_size = source.tell()
                start_offset = max(0, file_size - MAX_SUMMARY_FILE_BYTES)
                omitted_bytes = start_offset
                truncated_by_file_size = start_offset > 0
                partial_first_line = False
                if start_offset:
                    source.seek(start_offset - 1)
                    partial_first_line = source.read(1) != b"\n"
                source.seek(start_offset)
                bytes_remaining = file_size - start_offset
                if partial_first_line:
                    # Drop a possibly partial first line in the bounded tail.
                    raw_line = source.readline(min(MAX_SUMMARY_LINE_BYTES + 1, bytes_remaining))
                    bytes_remaining -= len(raw_line)
                    while raw_line and not raw_line.endswith(b"\n"):
                        raw_line = source.readline(min(MAX_SUMMARY_LINE_BYTES + 1, bytes_remaining))
                        bytes_remaining -= len(raw_line)

                while bytes_remaining > 0:
                    raw_line = source.readline(min(MAX_SUMMARY_LINE_BYTES + 1, bytes_remaining))
                    if not raw_line:
                        snapshot_short_read = True
                        snapshot_bytes_unread = bytes_remaining
                        break
                    bytes_remaining -= len(raw_line)
                    if len(raw_line) > MAX_SUMMARY_LINE_BYTES and not raw_line.endswith(b"\n"):
                        # Do not retain or parse a partial oversized record.
                        oversized_lines += 1
                        while raw_line and not raw_line.endswith(b"\n") and bytes_remaining > 0:
                            raw_line = source.readline(min(MAX_SUMMARY_LINE_BYTES + 1, bytes_remaining))
                            bytes_remaining -= len(raw_line)
                        continue
                    if len(raw_line) > MAX_SUMMARY_LINE_BYTES:
                        oversized_lines += 1
                        continue
                    item = _decode_audit_record(raw_line)
                    if item is None:
                        invalid_lines += 1
                        continue
                    if not effective_session_id or item.get("session_id") == effective_session_id:
                        matched_samples += 1
                        recent.append(item)
        except FileNotFoundError:
            # A new workspace may not have an audit file yet; an empty summary
            # is a valid state, distinct from an unreadable existing file.
            pass
        except OSError as exc:
            read_error_kind = type(exc).__name__
        if read_error_kind is not None:
            return _unavailable_summary(read_error_kind, sample_limit)
        entries = list(recent)
        sample_omitted = max(0, matched_samples - len(entries))
        truncated = bool(
            truncated_by_file_size or snapshot_short_read or sample_omitted
            or oversized_lines or invalid_lines
        )

        latency: list[float] = []
        wait: list[float] = []
        legacy_wait: list[float] = []
        latency_invalid = latency_unknown = 0
        wait_invalid = wait_unknown = 0
        user_wait_invalid = 0
        for row in entries:
            latency_value = row.get("latency_ms")
            if "latency_ms" not in row or latency_value is None:
                latency_unknown += 1
            else:
                parsed_latency = _valid_duration_ms(latency_value)
                if parsed_latency is None:
                    latency_invalid += 1
                else:
                    latency.append(parsed_latency)

            wait_value = row.get("wait_ms")
            user_wait_row = _safe_label(row.get("source_layer")) in {"human_decision", "hitl"}
            if "wait_ms" not in row or wait_value is None:
                if user_wait_row:
                    wait_unknown += 1
            else:
                parsed_wait = _valid_duration_ms(wait_value)
                if parsed_wait is None:
                    wait_invalid += 1
                    if user_wait_row:
                        user_wait_invalid += 1
                else:
                    if parsed_wait:
                        legacy_wait.append(parsed_wait)
                    if user_wait_row:
                        wait.append(parsed_wait)
        latency.sort()
        wait.sort()
        legacy_wait.sort()

        safe_sources = [_safe_label(row.get("source_layer")) for row in entries]
        safe_decisions = [_safe_label(row.get("decision")) for row in entries]
        source_counts = Counter(safe_sources)
        decision_counts = Counter(safe_decisions)
        review_rows = [
            (row, decision) for row, source, decision in zip(entries, safe_sources, safe_decisions)
            if source == "auto_safety_reviewer"
        ]
        review_allow = sum(decision == "allow" for _, decision in review_rows)
        review_deny = sum(decision == "deny" for _, decision in review_rows)
        review_unknown = len(review_rows) - review_allow - review_deny
        review_valid = review_allow + review_deny
        review_cache_reuse = sum(row.get("cache_hit") is True for row, _ in review_rows)
        review_uncached = sum(row.get("cache_hit") is False for row, _ in review_rows)

        override_operation_ids: set[str] = set()
        override_unknown = 0
        for row in entries:
            if not _hitl_allow(row):
                continue
            prior = row.get("prior_decision")
            operation_id = row.get("operation_id")
            if not _is_known_decision(prior):
                override_unknown += 1
            elif prior.strip().lower() != "deny":
                continue
            elif not isinstance(operation_id, str) or not operation_id.strip():
                override_unknown += 1
            else:
                override_operation_ids.add(operation_id.strip())

        boundary_denials = sum(
            decision == "deny" and source in _BOUNDARY_DENIAL_LAYERS
            for source, decision in zip(safe_sources, safe_decisions)
        )
        out_of_scope_requests = sum(
            (decision == "ask" and source == "sandbox_path")
            or row.get("escalation_requested") is True
            for row, source, decision in zip(entries, safe_sources, safe_decisions)
        )
        unknown_fields: dict[str, int] = {}
        for field_name in (
            "permission_mode", "execution_backend", "operation_id",
            "prior_decision", "escalation_requested", "source_layer", "decision",
            "cache_hit",
        ):
            if field_name in {"escalation_requested", "cache_hit"}:
                unknown_fields[field_name] = sum(
                    not isinstance(row.get(field_name), bool) for row in entries
                )
            elif field_name == "prior_decision":
                unknown_fields[field_name] = sum(
                    not _is_known_decision(row.get(field_name)) for row in entries
                )
            elif field_name in {"source_layer", "decision"}:
                labels = safe_sources if field_name == "source_layer" else safe_decisions
                unknown_fields[field_name] = sum(value == "unknown" for value in labels)
            else:
                unknown_fields[field_name] = sum(
                    not isinstance(row.get(field_name), str) or not row.get(field_name).strip()
                    for row in entries
                )
        waiting_for_user = {
            "p50": _percentile(wait, 0.5) if wait else None,
            "p95": _percentile(wait, 0.95) if wait else None,
            "total": _total_ms(wait) if wait else None,
            "valid_count": len(wait),
            "invalid_count": user_wait_invalid,
            "unknown_count": wait_unknown,
        }
        return {
            "count": len(entries),
            "by_source": dict(source_counts),
            "by_decision": dict(decision_counts),
            "cache_hits": sum(row.get("cache_hit") is True for row in entries),
            "latency_ms": {"p50": _percentile(latency, 0.5), "p95": _percentile(latency, 0.95),
                           "total": _total_ms(latency)},
            "human_wait_ms": {"p50": _percentile(legacy_wait, 0.5),
                              "p95": _percentile(legacy_wait, 0.95),
                              "total": _total_ms(legacy_wait)},
            "ask_count": sum(decision == "ask" for decision in safe_decisions),
            "model_reviews": {
                "count": len(review_rows),
                "allow_count": review_allow,
                "deny_count": review_deny,
                "unknown_outcome_count": review_unknown,
                "valid_outcome_count": review_valid,
                "cache_reuse_count": review_cache_reuse,
                "uncached_decision_count": review_uncached,
                # Unknown outcomes are not passes; rate is unavailable only
                # when there is no explicit allow/deny outcome to measure.
                "allow_rate_percent": round(review_allow / len(review_rows) * 100, 2)
                if review_valid else None,
            },
            "manual_deny_override_count": len(override_operation_ids),
            "manual_deny_override_unknown_count": override_unknown,
            "boundary_denial_count": boundary_denials,
            "out_of_scope_request_count": out_of_scope_requests,
            "waiting_for_user_ms": waiting_for_user,
            "timing_quality": {
                "latency_invalid_count": latency_invalid,
                "latency_unknown_count": latency_unknown,
                "wait_invalid_count": wait_invalid,
                "wait_unknown_count": wait_unknown,
            },
            "unknown_field_counts": unknown_fields,
            "truncation": {
                "truncated": truncated,
                "sample_limit": sample_limit,
                "samples_omitted": sample_omitted,
                "older_bytes_omitted": omitted_bytes,
                "snapshot_bytes_unread": snapshot_bytes_unread,
                "file_omission_count_unknown": truncated_by_file_size or snapshot_short_read,
                "invalid_lines": invalid_lines,
                "oversized_lines": oversized_lines,
                "max_line_bytes": MAX_SUMMARY_LINE_BYTES,
                "max_file_bytes_scanned": MAX_SUMMARY_FILE_BYTES,
            },
            "unavailable": False,
            "error_kind": None,
        }

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------

    def _get_current_file(self) -> Path:
        """获取当前审计日志文件路径。"""
        return self._audit_dir / AUDIT_FILE

    def _maybe_rotate(self, file_path: Path) -> None:
        """检查并执行日志 rotate。"""
        try:
            size = file_path.stat().st_size
        except OSError:
            return

        if size < MAX_FILE_BYTES:
            return

        # 归档当前文件
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        archive_path = self._audit_dir / f"decisions.{date_str}.jsonl"

        # 如果当天已有归档，追加
        try:
            with open(file_path, encoding="utf-8") as src:
                content = src.read()
            with open(archive_path, "a", encoding="utf-8") as dst:
                dst.write(content)
            # 清空当前文件
            file_path.write_text("", encoding="utf-8")
            log.info("[audit] rotated to %s", archive_path)
        except OSError as e:
            log.error("[audit] rotate failed: %s", e)
            return

        # 清理旧归档（保留最近 MAX_ARCHIVE_FILES 个）
        self._cleanup_archives()

    def _cleanup_archives(self) -> None:
        """删除过多的旧归档文件。"""
        archives = sorted(
            self._audit_dir.glob("decisions.*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in archives[MAX_ARCHIVE_FILES:]:
            try:
                old.unlink()
                log.info("[audit] removed old archive: %s", old)
            except OSError:
                pass


class AuditLogTool:
    """让 Agent 查询自身审计日志的工具。"""

    def __init__(self, audit_logger: AuditLogger) -> None:
        self._logger = audit_logger
