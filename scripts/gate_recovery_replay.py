"""Compare two redacted replays without running models, tools, or business checks."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from mewcode.experts.recovery_replay import (
    METRIC_SCHEMA_VERSION, analyze_replay, compare_recovery_metrics, load_replay,
)


def _metrics(path: Path, *, pages: int) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest, records = load_replay(path)
    if manifest.get("metric_schema_version") != METRIC_SCHEMA_VERSION:
        raise ValueError(f"{path}: replay has no supported recovery-metric coverage metadata")
    record_count = manifest.get("records")
    if isinstance(record_count, bool) or not isinstance(record_count, int) or record_count != len(records):
        raise ValueError(f"{path}: manifest record count is missing or inconsistent")
    source_rows = manifest.get("source_rows")
    if isinstance(source_rows, bool) or not isinstance(source_rows, int) or source_rows <= 0:
        raise ValueError(f"{path}: exact activity source row denominator is missing or zero")
    source_summary = manifest.get("source_coverage")
    if not isinstance(source_summary, dict) or source_summary.get("source_rows") != source_rows:
        raise ValueError(f"{path}: source coverage summary is missing or inconsistent")
    report = analyze_replay(records, mode="measure", pages=pages, source_rows=source_rows,
                            source_summary=source_summary)
    return manifest, report["recovery_metrics"]


def gate_replays(baseline_path: Path, candidate_path: Path, *,
                 baseline_pages: int, candidate_pages: int) -> dict[str, Any]:
    """Return a stable decision; no replay operation executes a model or tool."""
    if baseline_pages <= 0 or candidate_pages <= 0:
        return {"status": "FAIL", "errors": ["baseline and candidate page denominators must be positive"]}
    baseline_manifest, baseline = _metrics(baseline_path, pages=baseline_pages)
    candidate_manifest, candidate = _metrics(candidate_path, pages=candidate_pages)
    result = compare_recovery_metrics(baseline, candidate)
    result["inputs"] = {
        "baseline": {"run_key": baseline_manifest.get("run_key"),
                     "source_rows": baseline_manifest["source_rows"], "pages": baseline_pages},
        "candidate": {"run_key": candidate_manifest.get("run_key"),
                      "source_rows": candidate_manifest["source_rows"], "pages": candidate_pages},
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path, help="baseline replay manifest.json")
    parser.add_argument("candidate", type=Path, help="candidate replay manifest.json")
    parser.add_argument("--baseline-pages", type=int, required=True,
                        help="explicit number of pages represented by the baseline")
    parser.add_argument("--candidate-pages", type=int, required=True,
                        help="explicit number of pages represented by the candidate")
    args = parser.parse_args(argv)
    try:
        result = gate_replays(args.baseline, args.candidate,
                              baseline_pages=args.baseline_pages,
                              candidate_pages=args.candidate_pages)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result = {"status": "FAIL", "errors": [f"replay gate failed closed: {exc}"]}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
