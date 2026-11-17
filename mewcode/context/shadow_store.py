"""Metadata-only shadow reports. Never store transcript content or apply plans."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


class ShadowStore:
    def __init__(self, path: Path):
        self.path = path

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("""CREATE TABLE IF NOT EXISTS tool_history_shadow (
            scope TEXT, sample TEXT, created REAL, report TEXT,
            PRIMARY KEY(scope, sample))""")
        db.execute("""CREATE TABLE IF NOT EXISTS tool_history_revisits (
            scope TEXT, sample TEXT, ref TEXT, PRIMARY KEY(scope, sample, ref))""")
        return db

    def record(self, scope: str, sample: str, report: dict):
        if not scope:
            return
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO tool_history_shadow VALUES (?,?,?,?)",
                       (scope, sample, time.time(), json.dumps(report, ensure_ascii=False, allow_nan=False)))

    def reports(self, scope: str):
        if not scope or not self.path.exists():
            return []
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT report FROM tool_history_shadow WHERE scope=? ORDER BY created DESC LIMIT 20", (scope,))]

    def observe_revisits(self, scope: str, references: list[dict]):
        if not references or not self.path.exists():
            return
        for report in self.reports(scope):
            old = set(report["observed_refs"])
            resources = {row["resource"] for row in report["decisions"] if row["action"] != "keep"}
            new = [row["ref"] for row in references if row["ref"] not in old and row["resource"] in resources]
            if new:
                with self.connect() as db:
                    db.executemany("INSERT OR IGNORE INTO tool_history_revisits VALUES (?,?,?)",
                                   [(scope, report["sample"], ref) for ref in new])

    def summary(self, scope: str):
        reports = self.reports(scope)
        revisits = 0
        if scope and self.path.exists():
            with self.connect() as db:
                revisits = db.execute("SELECT COUNT(*) FROM tool_history_revisits WHERE scope=?", (scope,)).fetchone()[0]
        return {"mode": "shadow", "applied": False, "samples": len(reports), "actual_saved_tokens": 0,
                "latest": reports[0] if reports else None,
                "reported_judgment_input_tokens": sum(r["judgment"].get("reported_input_tokens", 0) for r in reports),
                "reported_judgment_output_tokens": sum(r["judgment"].get("reported_output_tokens", 0) for r in reports),
                "revisit_signals": revisits, "revisit_note": "Re-reading is a risk proxy, not proof of a mistaken removal.",
                "promotion": {"allowed": False, "reason": "shadow_only_build", "independent_review_required": True,
                              "misdeletion_rate": None, "real_savings_validated": False},
                "note": "Overlapping snapshot estimates must not be summed as realized savings."}


def evaluate_labeled_report(report: dict, labels: dict[str, str]):
    """Offline independent labels only; this metric NEVER enables pruning.

    Labels are retain / dispensable / unknown, keyed by the report's anonymous
    tool ref. Do not obtain ground truth from the same judgment being evaluated.
    """
    proposed = [row for row in report["decisions"] if row["action"] != "keep"]
    reviewed = [row for row in proposed if labels.get(row["ref"]) in {"retain", "dispensable"}]
    errors = sum(labels[row["ref"]] == "retain" for row in reviewed)
    return {"proposed_removals": len(proposed), "reviewed_removals": len(reviewed),
            "unreviewed_removals": len(proposed) - len(reviewed), "mistaken_removals": errors,
            "misdeletion_rate": errors / len(reviewed) if reviewed else None,
            "promotion_allowed": False, "real_savings_validated": False}


def main(argv=None):
    """Offline scoring of an exported report and independent, action-specific labels."""
    import argparse
    parser = argparse.ArgumentParser(description="Score a shadow proposal; never apply it or enable pruning.")
    parser.add_argument("report", type=Path)
    parser.add_argument("labels", type=Path)
    args = parser.parse_args(argv)
    for path in (args.report, args.labels):
        if path.stat().st_size > 2_000_000:
            parser.error("Input exceeds the offline evaluation size limit")
    report = json.loads(args.report.read_text(encoding="utf-8"))
    labels = json.loads(args.labels.read_text(encoding="utf-8"))
    if isinstance(report, dict) and "latest" in report:
        report = report["latest"]
    if not isinstance(report, dict) or report.get("mode") != "shadow" or report.get("applied") is not False:
        parser.error("Expected a non-applied shadow report")
    if not isinstance(labels, dict) or any(value not in ("retain", "dispensable", "unknown") for value in labels.values()):
        parser.error("Labels must be an independent ref -> retain/dispensable/unknown mapping")
    result = evaluate_labeled_report(report, labels)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return result


if __name__ == "__main__":
    main()
