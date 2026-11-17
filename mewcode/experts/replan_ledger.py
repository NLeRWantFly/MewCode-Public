"""Transactional replan receipts, independent of rewindable run checkpoints.

SQLite serializes claims across store instances/processes. An interrupted claim
is deliberately NOT a time-expiring lease: absence of a receipt cannot prove an
external request was not executed. Text-only advisory failure retains billing
and deduplication without owning an execution lease. Snapshot records are only
UI projections.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class LedgerConflict(ValueError):
    pass


ACTIVE = {"consulting", "outcome_unknown", "proposed", "probing", "effect_unknown"}
TRANSITIONS = {
    "consulting": {"proposed", "needs_input", "rejected", "stale", "outcome_unknown", "advisory_unknown", "advisory", "unavailable"},
    "proposed": {"probing", "stale", "needs_input"},
    "probing": {"probing", "applied", "candidate_ready", "rejected", "stale", "effect_unknown"},
}


class ReplanLedger:
    def __init__(self, root):
        self.path = Path(root) / "adaptive-replans.sqlite3"
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS episodes ("
                       "run TEXT NOT NULL, id TEXT NOT NULL, node TEXT NOT NULL, "
                       "revision INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(run,id))")

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def records(self, run_id):
        with self.transaction() as db:
            return {key: json.loads(body) for key, body in db.execute(
                "SELECT id,body FROM episodes WHERE run=?", (run_id,))}

    def reconcile_advisory_unknown(self, run_id, *, allow_legacy=False):
        """Release advisory ownership, never a trial or its billing reservation.

        Legacy migration is explicit: only this coordinator's old capsule
        shape, with a recorded failed request and no proposal/dispatch, qualifies.
        Unknown plugin requests and unknown filesystem effects stay fenced.
        """
        changed = []
        with self.transaction() as db:
            for key, rev, body in db.execute("SELECT id,revision,body FROM episodes WHERE run=?", (run_id,)).fetchall():
                value = json.loads(body)
                context = value.get("context") or {}
                known = value.get("request_kind") == "advisory_text_only"
                legacy = (allow_legacy and value.get("error_type")
                          and {"contract_hash", "flow_hash", "node_attempt", "executor_request", "candidate_root", "allowed_tools"} <= context.keys())
                if (value.get("status") != "outcome_unknown" or not (known or legacy)
                        or value.get("proposal") or value.get("probe") or value.get("dispatch_started")
                        or value.get("candidate_receipts")):
                    continue
                value.update(status="advisory_unknown", request_kind="advisory_text_only", revision=rev + 1,
                             reconciliation="no_execution_dispatched; billing_reservation_retained")
                db.execute("UPDATE episodes SET revision=?,body=? WHERE run=? AND id=?",
                           (rev + 1, json.dumps(value, ensure_ascii=False), run_id, key))
                changed.append(key)
        return changed

    def import_legacy(self, run_id, records):
        """Preserve old limits and unknown requests; never overwrite newer receipts."""
        with self.transaction() as db:
            for key, old in records.items():
                value = {**old, "revision": 1}
                # Old observed means only execution ended, not verification.
                if value.get("status") == "observed":
                    value["status"] = "rejected"
                    value["verification_status"] = "legacy_unverified"
                db.execute("INSERT OR IGNORE INTO episodes VALUES (?,?,?,?,?)",
                    (run_id, key, value["node_id"], 1, json.dumps(value, ensure_ascii=False)))

    def claim(self, run_id, record):
        with self.transaction() as db:
            rows = [json.loads(r[0]) for r in db.execute(
                "SELECT body FROM episodes WHERE run=?", (run_id,))]
            if any(r["episode_id"] == record["episode_id"] for r in rows):
                raise LedgerConflict("This evidence episode was already consulted; inspect its saved receipt")
            owned = [r for r in rows if r["node_id"] == record["node_id"]]
            if any(r["status"] in ACTIVE for r in owned):
                raise LedgerConflict("An unresolved proposal or request already owns this node")
            if len(rows) >= 6 or len(owned) >= 2:
                raise LedgerConflict("Persistent replan limit reached")
            record = {**record, "revision": 1}
            db.execute("INSERT INTO episodes VALUES (?,?,?,?,?)", (run_id,
                record["episode_id"], record["node_id"], 1, json.dumps(record, ensure_ascii=False)))
            return record

    def transition(self, run_id, key, *, expected_revision, status, **receipt):
        with self.transaction() as db:
            row = db.execute("SELECT revision,body FROM episodes WHERE run=? AND id=?",
                             (run_id, key)).fetchone()
            if row is None or row[0] != expected_revision:
                raise LedgerConflict("Stale replan fencing revision")
            value = json.loads(row[1])
            if status not in TRANSITIONS.get(value["status"], set()):
                raise LedgerConflict("Invalid replan state transition")
            if {"episode_id", "node_id", "context", "revision", "reserved_tokens"} & receipt.keys():
                raise LedgerConflict("Cannot replace frozen replan identity or reservation")
            if status == "probing" and value["status"] == "probing" and (
                    value.get("dispatch_started") or receipt.get("dispatch_started") is not True):
                raise LedgerConflict("Trial dispatch already consumed; reconcile its receipt before resuming")
            value.update(receipt, status=status, revision=expected_revision + 1)
            db.execute("UPDATE episodes SET revision=?,body=? WHERE run=? AND id=?",
                (value["revision"], json.dumps(value, ensure_ascii=False), run_id, key))
            return value
