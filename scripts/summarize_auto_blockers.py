#!/usr/bin/env python3
"""Summarize permission audit JSONL rows without exposing request contents.

The CLI reads only explicitly supplied JSONL files and immediate ``*.jsonl``
children of explicitly supplied directories. It does not recurse. Output is
aggregate metadata; raw parameter previews, reasons, identifiers, and paths are
never copied to the report.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable


MAX_FILES = 128
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_KNOWN_MODES = {
    "default": "default",
    "acceptedits": "acceptEdits",
    "plan": "plan",
    "auto": "auto",
    "strictisolation": "strictIsolation",
    "bypasspermissions": "bypassPermissions",
    "custom": "custom",
    "dontask": "dontAsk",
}


def _label(row: dict[str, Any], field: str) -> str:
    """Return a short metadata token or a coverage bucket, never raw input."""
    if field not in row or row[field] is None:
        return "missing"
    value = row[field]
    if not isinstance(value, str):
        return "unknown"
    cleaned = value.strip()
    if not cleaned:
        return "missing"
    if not _SAFE_LABEL.fullmatch(cleaned):
        return "unknown"
    return cleaned.lower()


def _mode(row: dict[str, Any]) -> str:
    if "permission_mode" not in row or row["permission_mode"] is None:
        return "missing"
    value = row["permission_mode"]
    if not isinstance(value, str) or not value.strip():
        return "unknown" if value not in (None, "") else "missing"
    normalized = value.strip().lower()
    if normalized in _KNOWN_MODES:
        return _KNOWN_MODES[normalized]
    if _SAFE_LABEL.fullmatch(value.strip()):
        return "unknown"
    return "unknown"


def _operation_identity(row: dict[str, Any]) -> tuple[str, str] | None:
    """Build an in-memory dedup key; the identifiers never leave this process."""
    operation_id = row.get("operation_id")
    if not isinstance(operation_id, str) or not _SAFE_ID.fullmatch(operation_id.strip()):
        return None
    session_id = row.get("session_id")
    if isinstance(session_id, str) and _SAFE_ID.fullmatch(session_id.strip()):
        return session_id.strip(), operation_id.strip()
    # Tool call IDs are still useful when legacy rows omit session_id. They are
    # used only as an in-memory key and are never emitted.
    return "", operation_id.strip()


def _percent(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(100 * numerator / denominator, 2)


def _json_object(raw_line: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(raw_line.decode("utf-8-sig"))
    except (UnicodeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _resolve_inputs(inputs: Iterable[Path], max_files: int) -> dict[str, Any]:
    candidates: list[Path] = []
    seen: set[str] = set()
    missing_inputs = 0
    invalid_inputs = 0
    unreadable_inputs = 0
    symlink_inputs_ignored = 0
    empty_directories = 0

    for supplied in inputs:
        try:
            if supplied.is_symlink():
                symlink_inputs_ignored += 1
                continue
            if not supplied.exists():
                missing_inputs += 1
                continue
            if supplied.is_dir():
                try:
                    children = sorted(supplied.glob("*.jsonl"), key=lambda item: item.name.lower())
                except OSError:
                    unreadable_inputs += 1
                    continue
                if not children:
                    empty_directories += 1
                candidates.extend(child for child in children if not child.is_symlink() and child.is_file())
                symlink_inputs_ignored += sum(child.is_symlink() for child in children)
            elif supplied.is_file() and supplied.suffix.lower() == ".jsonl":
                candidates.append(supplied)
            else:
                invalid_inputs += 1
        except OSError:
            unreadable_inputs += 1

    unique: list[Path] = []
    duplicate_references = 0
    for path in candidates:
        try:
            identity = os.path.normcase(str(path.resolve(strict=False)))
        except OSError:
            identity = os.path.normcase(str(path.absolute()))
        if identity in seen:
            duplicate_references += 1
            continue
        seen.add(identity)
        unique.append(path)

    unique.sort(key=lambda item: os.path.normcase(str(item)))
    omitted_by_file_limit = max(0, len(unique) - max_files)
    selected = unique[:max_files]
    return {
        "files": selected,
        "omitted_files": unique[max_files:],
        "discovered_files": len(unique),
        "omitted_by_file_limit": omitted_by_file_limit,
        "missing_inputs": missing_inputs,
        "invalid_inputs": invalid_inputs,
        "unreadable_inputs": unreadable_inputs,
        "symlink_inputs_ignored": symlink_inputs_ignored,
        "empty_directories": empty_directories,
        "duplicate_references": duplicate_references,
    }


def _scan_file(
    path: Path,
    byte_budget: int,
    max_file_bytes: int,
    max_line_bytes: int,
    on_row: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    counters: Counter[str] = Counter()
    scanned_bytes = 0
    omitted_bytes = 0
    unreadable = 0
    try:
        file_size = path.stat().st_size
        read_limit = min(file_size, max_file_bytes, max(0, byte_budget))
        omitted_bytes = max(0, file_size - read_limit)
        with path.open("rb") as source:
            while scanned_bytes < read_limit:
                remaining = read_limit - scanned_bytes
                raw_line = source.readline(min(max_line_bytes + 1, remaining))
                if not raw_line:
                    break
                scanned_bytes += len(raw_line)
                counters["physical_lines_scanned"] += 1
                line_complete = raw_line.endswith(b"\n")

                if len(raw_line) > max_line_bytes:
                    counters["oversized_lines"] += 1
                    while not line_complete and scanned_bytes < read_limit:
                        raw_line = source.readline(min(max_line_bytes + 1, read_limit - scanned_bytes))
                        if not raw_line:
                            break
                        scanned_bytes += len(raw_line)
                        line_complete = raw_line.endswith(b"\n")
                    if not line_complete and scanned_bytes < file_size:
                        counters["incomplete_lines_omitted"] += 1
                        omitted_bytes = max(omitted_bytes, file_size - scanned_bytes)
                        break
                    continue

                if not line_complete and scanned_bytes < file_size:
                    # The byte cap ended inside a line. Keep it out of both the
                    # valid-row and malformed-row denominators.
                    counters["incomplete_lines_omitted"] += 1
                    omitted_bytes = max(omitted_bytes, file_size - scanned_bytes)
                    break

                stripped = raw_line.strip()
                if not stripped:
                    counters["blank_lines"] += 1
                    continue
                row = _json_object(stripped)
                if row is None:
                    counters["invalid_rows"] += 1
                    continue
                on_row(row)
                counters["decision_records"] += 1
    except OSError:
        unreadable = 1
        try:
            omitted_bytes = max(omitted_bytes, path.stat().st_size - scanned_bytes)
        except OSError:
            pass
    return {
        "counters": counters,
        "scanned_bytes": scanned_bytes,
        "omitted_bytes": omitted_bytes,
        "unreadable": unreadable,
    }


def summarize_inputs(
    inputs: Iterable[Path],
    *,
    max_files: int = MAX_FILES,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_line_bytes: int = MAX_LINE_BYTES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> dict[str, Any]:
    """Return aggregate-only audit metrics for explicit files/directories."""
    # Keep test-provided bounds subject to the same safe, positive limits.
    max_files = max(0, int(max_files))
    max_file_bytes = max(0, int(max_file_bytes))
    max_line_bytes = max(1, int(max_line_bytes))
    max_total_bytes = max(0, int(max_total_bytes))
    resolved = _resolve_inputs(inputs, max_files)

    dimensions = {
        "decision": Counter(),
        "source_layer": Counter(),
        "subcondition": Counter(),
        "rule_id": Counter(),
        "review_route_status": Counter(),
    }
    modes: Counter[str] = Counter()
    unknown_fields: Counter[str] = Counter()
    operation_ids: set[tuple[str, str]] = set()
    operation_rows = 0
    missing_operation_rows = 0
    records = 0
    total_bytes_scanned = 0
    total_bytes_omitted = 0
    file_stats: Counter[str] = Counter()
    truncation: Counter[str] = Counter()
    legacy_preview_suspects = 0

    def aggregate_row(row: dict[str, Any]) -> None:
        nonlocal records, operation_rows, missing_operation_rows, legacy_preview_suspects
        records += 1
        review_route = row.get("review_route")
        route = review_route if isinstance(review_route, dict) else {}
        route_row = {"review_route_status": route.get("status")} if "status" in route else {}
        values = {
            "decision": _label(row, "decision"),
            "source_layer": _label(row, "source_layer"),
            "subcondition": _label(row, "subcondition"),
            "rule_id": _label(row, "rule_id"),
            "review_route_status": _label(route_row, "review_route_status"),
        }
        for name, value in values.items():
            dimensions[name][value] += 1
            if value in {"missing", "unknown"}:
                unknown_fields[name] += 1

        mode = _mode(row)
        modes[mode] += 1
        if mode in {"missing", "unknown"}:
            unknown_fields["permission_mode"] += 1

        operation_key = _operation_identity(row)
        if operation_key is None:
            missing_operation_rows += 1
            unknown_fields["operation_id"] += 1
        else:
            operation_rows += 1
            operation_ids.add(operation_key)

        marker = row.get("params_summary_truncated")
        if "params_summary_truncated" not in row or marker is None:
            truncation["marker_missing"] += 1
            preview = row.get("params_summary")
            if isinstance(preview, str) and (len(preview) >= 200 or preview.endswith("...")):
                legacy_preview_suspects += 1
        elif isinstance(marker, bool):
            truncation["marker_true" if marker else "marker_false"] += 1
        else:
            truncation["marker_invalid"] += 1

    # File-count omissions are still visible in the byte coverage where their
    # metadata is readable. No omitted file content is opened.
    omitted_file_bytes = 0
    omitted_file_sizes_unknown = 0
    for omitted_path in resolved["omitted_files"]:
        try:
            omitted_file_bytes += max(0, omitted_path.stat().st_size)
        except OSError:
            omitted_file_sizes_unknown += 1
    total_bytes_omitted += omitted_file_bytes

    for path in resolved["files"]:
        if total_bytes_scanned >= max_total_bytes:
            file_stats["omitted_due_to_total_byte_limit"] += 1
            try:
                total_bytes_omitted += max(0, path.stat().st_size)
            except OSError:
                file_stats["omitted_size_unknown"] += 1
            continue
        result = _scan_file(
            path,
            max_total_bytes - total_bytes_scanned,
            max_file_bytes,
            max_line_bytes,
            aggregate_row,
        )
        file_stats["files_scanned"] += 1
        file_stats["unreadable_files"] += result["unreadable"]
        total_bytes_scanned += result["scanned_bytes"]
        total_bytes_omitted += result["omitted_bytes"]
        file_stats.update(result["counters"])

    parsed_rows = records + file_stats["invalid_rows"] + file_stats["oversized_lines"]
    valid_mode_rows = records - modes["missing"] - modes["unknown"]
    known_mode_names = [value for value in modes if value not in {"missing", "unknown"}]
    dimension_report: dict[str, dict[str, int]] = {
        name: dict(sorted(counts.items())) for name, counts in dimensions.items()
    }

    return {
        "schema_version": 1,
        "sample_scope": "explicit JSONL files and immediate *.jsonl children of explicit directories only",
        "production_attribution": "not inferred; synthetic fixtures are not classified as production",
        "counts": {
            "decision_records": records,
            "deduplicated_identifiable_operations": len(operation_ids),
            "decision_records_with_operation_id": operation_rows,
            "decision_records_without_valid_operation_id": missing_operation_rows,
            "duplicate_decision_records_for_identified_operations": max(0, operation_rows - len(operation_ids)),
            "total_operation_count": None if missing_operation_rows else len(operation_ids),
            "total_operation_count_status": "unknown_missing_operation_ids" if missing_operation_rows else "complete_for_scanned_records",
        },
        "aggregates": dimension_report,
        "permission_mode": {
            "by_value": dict(sorted(modes.items())),
            "known_auto_records": modes["auto"],
            "missing_mode_records": modes["missing"],
            "unknown_mode_records": modes["unknown"],
            "known_mode_records": valid_mode_rows,
            "coverage_percent": _percent(valid_mode_rows, records),
            "known_values": sorted(known_mode_names),
        },
        "parameter_truncation": {
            "explicitly_truncated_records": truncation["marker_true"],
            "explicitly_not_truncated_records": truncation["marker_false"],
            "missing_marker_records": truncation["marker_missing"],
            "invalid_marker_records": truncation["marker_invalid"],
            "marker_coverage_percent": _percent(truncation["marker_true"] + truncation["marker_false"], records),
            "possible_legacy_truncated_previews": legacy_preview_suspects,
            "legacy_preview_heuristic": "marker missing and private preview length >= 200 or ending in ellipsis; preview contents are never emitted",
        },
        "coverage": {
            "parseable_object_records": records,
            "invalid_rows": file_stats["invalid_rows"],
            "oversized_lines": file_stats["oversized_lines"],
            "incomplete_lines_omitted": file_stats["incomplete_lines_omitted"],
            "blank_lines": file_stats["blank_lines"],
            "parse_coverage_denominator_rows": parsed_rows,
            "parse_coverage_percent": _percent(records, parsed_rows),
            "unknown_or_missing_field_counts": dict(sorted(unknown_fields.items())),
            "field_coverage_percent": {
                "decision": _percent(records - unknown_fields["decision"], records),
                "source_layer": _percent(records - unknown_fields["source_layer"], records),
                "subcondition": _percent(records - unknown_fields["subcondition"], records),
                "rule_id": _percent(records - unknown_fields["rule_id"], records),
                "review_route_status": _percent(records - unknown_fields["review_route_status"], records),
                "permission_mode": _percent(valid_mode_rows, records),
                "operation_id": _percent(operation_rows, records),
                "params_summary_truncated_marker": _percent(truncation["marker_true"] + truncation["marker_false"], records),
            },
        },
        "turn_stop_events": {
            "count": None,
            "status": "unknown",
            "reason": "these audit rows do not record turn-stop events",
        },
        "input_coverage": {
            "discovered_jsonl_files": resolved["discovered_files"],
            "files_scanned": file_stats["files_scanned"],
            "files_omitted_by_file_limit": resolved["omitted_by_file_limit"],
            "omitted_file_sizes_unknown": omitted_file_sizes_unknown,
            "files_omitted_by_total_byte_limit": file_stats["omitted_due_to_total_byte_limit"],
            "unreadable_files": file_stats["unreadable_files"],
            "missing_inputs": resolved["missing_inputs"],
            "invalid_inputs": resolved["invalid_inputs"],
            "unreadable_inputs": resolved["unreadable_inputs"],
            "symlink_inputs_ignored": resolved["symlink_inputs_ignored"],
            "empty_directories": resolved["empty_directories"],
            "duplicate_references_ignored": resolved["duplicate_references"],
            "bytes_scanned": total_bytes_scanned,
            "bytes_omitted": total_bytes_omitted,
        },
        "read_limits": {
            "max_files": max_files,
            "max_file_bytes": max_file_bytes,
            "max_line_bytes": max_line_bytes,
            "max_total_bytes": max_total_bytes,
            "directory_depth": 1,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help="explicit audit JSONL file(s) or audit directory/directories")
    parser.add_argument("--output", required=True, type=Path, help="write aggregate JSON here")
    args = parser.parse_args(argv)
    try:
        report = summarize_inputs(args.inputs)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"cannot write aggregate output ({type(exc).__name__})", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
