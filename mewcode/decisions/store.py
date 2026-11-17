"""Atomic reservations prevent repeated paid judgments after retries/restarts."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


class DecisionStore:
    def __init__(self, path: Path):
        self.path = path

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("""CREATE TABLE IF NOT EXISTS decision_receipts (
            scope TEXT, issue TEXT, fingerprint TEXT, reserved_bytes INTEGER,
            created REAL, result TEXT, PRIMARY KEY(scope, issue, fingerprint))""")
        db.execute("""CREATE TABLE IF NOT EXISTS decision_repeats (
            scope TEXT, issue TEXT, fingerprint TEXT, count INTEGER,
            PRIMARY KEY(scope, issue, fingerprint))""")
        return db

    def reserve(self, scope, issue, fingerprint, size, max_rounds, *,
                max_reservations=64, max_reserved_bytes=240000, count_repeats=True):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT result FROM decision_receipts WHERE scope=? AND issue=? AND fingerprint=?",
                                  (scope, issue, fingerprint)).fetchone()
            if previous:
                cached = {**json.loads(previous[0]), "cached": True} if previous[0] else {
                    "status": "rethink", "reason": "inflight_or_unknown_receipt", "cached": True,
                    "next_action": "Do not resend this judgment; reason from evidence or change the plan."}
                if count_repeats and previous[0] and cached.get("status") == "rethink":
                    db.execute("INSERT INTO decision_repeats VALUES (?,?,?,1) ON CONFLICT(scope,issue,fingerprint) DO UPDATE SET count=count+1",
                               (scope, issue, fingerprint))
                    repeats = db.execute("SELECT count FROM decision_repeats WHERE scope=? AND issue=? AND fingerprint=?",
                                         (scope, issue, fingerprint)).fetchone()[0]
                    if repeats >= 3:
                        cached.update(status="escalate", reason="unchanged_replan_loop",
                                      next_action="Repeated unchanged proposals stopped. Report the concrete blocker; do not claim a second paid evaluation occurred.")
                        db.execute("UPDATE decision_receipts SET result=? WHERE scope=? AND issue=? AND fingerprint=?",
                                   (json.dumps(cached), scope, issue, fingerprint))
                return cached
            count, total = db.execute("SELECT COUNT(*), COALESCE(SUM(reserved_bytes),0) FROM decision_receipts WHERE scope=?",
                                      (scope,)).fetchone()
            rounds = db.execute("SELECT COUNT(*) FROM decision_receipts WHERE scope=? AND issue=?", (scope, issue)).fetchone()[0]
            if rounds >= max_rounds or count >= max_reservations or total + size > max_reserved_bytes:
                return {"status": "escalate", "reason": "decision_budget_exhausted", "cached": False,
                        "next_action": "Use main-model reasoning; ask the user only for the remaining concrete blocker."}
            db.execute("INSERT INTO decision_receipts VALUES (?,?,?,?,?,NULL)",
                       (scope, issue, fingerprint, size, time.time()))
            return {"reserved": True, "round": rounds + 1}

    def finish(self, scope, issue, fingerprint, result):
        with self.connect() as db:
            db.execute("UPDATE decision_receipts SET result=? WHERE scope=? AND issue=? AND fingerprint=?",
                       (json.dumps(result, ensure_ascii=False), scope, issue, fingerprint))

    def latest(self, scope, issue):
        with self.connect() as db:
            row = db.execute("SELECT result FROM decision_receipts WHERE scope=? AND issue=? ORDER BY created DESC LIMIT 1",
                             (scope, issue)).fetchone()
        return json.loads(row[0]) if row and row[0] else None
