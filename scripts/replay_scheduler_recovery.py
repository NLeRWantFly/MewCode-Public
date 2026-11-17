"""Analyze a redacted recovery replay without running a model or tools."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from mewcode.experts.recovery_replay import (
    RECOVERY_METRIC_NAMES, analyze_replay, load_replay,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--mode", choices=("all", "measure", "classify", "simulate"), default="all")
    parser.add_argument("--pages", type=int,
                        help="explicit number of pages for the billable-token/page metric")
    parser.add_argument("--assert", dest="assert_metric_coverage", action="store_true",
                        help="fail unless all three recovery metrics have complete coverage; does not compare before/after")
    parser.add_argument("--assert-integrity", action="store_true",
                        help="fail if manifest, source summary, and redacted records are inconsistent")
    args = parser.parse_args(argv)
    manifest, records = load_replay(args.manifest)
    source_rows = manifest.get("source_rows")
    source_summary = manifest.get("source_coverage")
    if args.assert_integrity or args.assert_metric_coverage:
        integrity_error = None
        if len(records) != manifest.get("records"):
            integrity_error = "replay record count mismatch"
        elif manifest.get("metric_schema_version") != 2:
            integrity_error = "replay lacks source-coverage metric schema v2"
        elif isinstance(source_rows, bool) or not isinstance(source_rows, int) or source_rows <= 0:
            integrity_error = "replay source row denominator is missing or invalid"
        elif not isinstance(source_summary, dict) or source_summary.get("source_rows") != source_rows:
            integrity_error = "replay source coverage summary is missing or inconsistent"
        if integrity_error:
            print(json.dumps({"status": "FAIL", "assertion": "integrity",
                              "errors": [integrity_error]}, ensure_ascii=False, sort_keys=True))
            return 1
    report = analyze_replay(records, mode=args.mode, pages=args.pages,
                            source_rows=source_rows if isinstance(source_rows, int) and not isinstance(source_rows, bool) else None,
                            source_summary=source_summary if isinstance(source_summary, dict) else None)
    incomplete = [name for name in RECOVERY_METRIC_NAMES
                  if report["recovery_metrics"][name].get("coverage") != "complete"]
    if args.assert_metric_coverage:
        report["metric_coverage_assertion"] = {
            "status": "FAIL" if incomplete else "PASS",
            "requires": list(RECOVERY_METRIC_NAMES),
            "incomplete_metrics": incomplete,
            "note": "coverage assertion only; before/after strict-decrease acceptance is handled by gate_recovery_replay.py",
        }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    if args.assert_metric_coverage and incomplete:
        return 1
    if args.assert_metric_coverage:
        return 0
    if args.assert_integrity:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
