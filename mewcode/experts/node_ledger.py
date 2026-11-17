"""Append-only node facts; legacy stage fields are derived compatibility views."""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import time
from typing import Any, Literal, Mapping

NodePhase = Literal["ready", "running", "escalated", "waiting_human", "done", "failed"]
FactKind = Literal["receipt", "artifact", "blocker", "clearance", "handoff", "assistance",
                   "question", "answer", "progress", "epoch_change", "phase_change", "maintenance", "case", "case_round"]
PHASES = frozenset({"ready", "running", "escalated", "waiting_human", "done", "failed"})
KINDS = frozenset({"receipt", "artifact", "blocker", "clearance", "handoff", "assistance",
                  "question", "answer", "progress", "epoch_change", "phase_change", "maintenance", "case", "case_round"})
EPOCH_REASONS = frozenset({"maintenance_applied", "assistance_patch", "user_direction", "explicit_rebase"})
STATUS_PHASE = {"pending": "ready", "ready": "ready", "running": "running", "completed": "done",
                "escalated": "escalated", "needs_attention": "escalated", "blocked": "escalated", "paused": "waiting_human",
                "waiting_human": "waiting_human", "failed": "failed", "stalled": "escalated",
                "budget_exhausted": "escalated", "cancelled": "escalated"}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


@dataclass(frozen=True)
class LedgerFact:
    fact_id: str
    node_id: str
    epoch: int
    kind: FactKind
    payload: Mapping[str, Any]
    source: str
    created_at: float
    superseded_by: str | None = None


@dataclass(frozen=True)
class NodeView:
    node_id: str
    phase: NodePhase
    epoch: int
    active_blockers: tuple[LedgerFact, ...]
    current_receipts: tuple[LedgerFact, ...]
    artifacts: tuple[LedgerFact, ...]
    pending_question: LedgerFact | None
    last_progress: LedgerFact | None
    history: tuple[LedgerFact, ...]


from mewcode.kernel.calls import successful_receipt


class NodeLedger:
    def __init__(self, storage: dict | None = None):
        self.storage = storage if storage is not None else {}

    def _facts(self, node_id):
        return [LedgerFact(**copy.deepcopy(row)) for row in self.storage.get(node_id, {}).get("facts", [])]

    def append(self, fact: LedgerFact) -> None:
        if fact.kind not in KINDS or fact.epoch < 0 or not fact.node_id or not fact.fact_id:
            raise ValueError("Invalid node ledger fact")
        if fact.kind == "phase_change" and fact.payload.get("phase") not in PHASES:
            raise ValueError("Invalid node phase")
        bucket = self.storage.setdefault(fact.node_id, {"version": 1, "facts": []})
        existing = next((row for row in bucket["facts"] if row["fact_id"] == fact.fact_id), None)
        row = asdict(fact)
        if existing is not None:
            if existing != row:
                raise ValueError("Conflicting node fact identity")
            return
        bucket["facts"].append(copy.deepcopy(row))

    def record(self, node_id, kind, payload, *, source="host", identity=None, epoch=None):
        if kind == 'blocker' and source != 'migration':
            from mewcode.kernel.blockers import canonical_blocker, policy
            payload = dict(payload)
            payload['kind'] = canonical_blocker(payload['kind']).value
            contract = policy(payload['kind'])
            payload.setdefault('owner', contract.owner)
            payload.setdefault('clearance', contract.clearance)
        epoch = self.view(node_id).epoch if epoch is None else epoch
        key = _digest([node_id, epoch, kind, source, identity if identity is not None else payload])
        existing = next((row for row in self.storage.get(node_id, {}).get("facts", []) if row["fact_id"] == key), None)
        if existing is not None:
            return LedgerFact(**copy.deepcopy(existing))
        fact = LedgerFact(key, node_id, epoch, kind, copy.deepcopy(payload), source, time.time())
        self.append(fact)
        return fact

    def supersede(self, fact_id: str, by_fact_id: str) -> None:
        facts = [f for node_id in self.storage for f in self._facts(node_id)]
        target = next((f for f in facts if f.fact_id == fact_id), None)
        replacement = next((f for f in facts if f.fact_id == by_fact_id), None)
        if target is None or replacement is None or target.node_id != replacement.node_id or fact_id == by_fact_id:
            raise ValueError("Supersession requires two existing facts of the same node")
        self.record(target.node_id, "maintenance", {"supersedes": fact_id, "by_fact_id": by_fact_id},
                    identity=["supersede", fact_id, by_fact_id])

    def bump_epoch(self, node_id: str, *, reason: str, source_ref: str) -> int:
        if reason not in EPOCH_REASONS or not source_ref:
            raise ValueError("Epoch change requires an explicit host transition reference")
        view = self.view(node_id)
        if view.phase == "done" and (view.artifacts or any(successful_receipt(f.payload) for f in view.current_receipts)
                or any(f.kind == "handoff" and f.payload.get("status") == "completed" for f in view.history)):
            raise ValueError("Completed node with evidence cannot be rebased")
        prior = next((f for f in view.history if f.kind == "epoch_change" and f.payload.get("source_ref") == source_ref), None)
        if prior is not None:
            return prior.epoch
        commands = {}
        for fact in view.history:
            if fact.kind == 'receipt' and fact.payload.get('request_id') and fact.payload.get('status'):
                commands[fact.payload['request_id']] = fact.payload
        if any(row.get('status') in {'outcome_unknown', 'in_flight', 'effect_partial'}
               and row.get('effect_class') != 'readonly' for row in commands.values()):
            raise ValueError('效果未知的写操作须先核验，换代际不能解除防重放边界')
        epoch = view.epoch + 1
        change = self.record(node_id, "epoch_change", {"reason": reason, "source_ref": source_ref,
            "previous_epoch": view.epoch}, epoch=epoch, identity=source_ref)
        for fact in view.history:
            if fact.epoch == view.epoch and fact.kind in {"blocker", "question"} and not fact.superseded_by:
                self.supersede(fact.fact_id, change.fact_id)
        self.set_phase(node_id, "ready", reason=reason)
        from mewcode.experts.recovery_case import supersede_cases
        supersede_cases({'node_ledgers': self.storage}, node_id)
        return epoch

    def set_phase(self, node_id: str, phase: NodePhase, *, reason: str) -> None:
        if phase not in PHASES:
            raise ValueError("Invalid node phase")
        if self.view(node_id).phase == phase and self._facts(node_id):
            return
        self.record(node_id, "phase_change", {"phase": phase, "reason": reason},
                    identity=[phase, len(self._facts(node_id))])

    def view(self, node_id: str) -> NodeView:
        raw = self._facts(node_id)
        superseded = {f.payload["supersedes"]: f.payload["by_fact_id"] for f in raw
                      if f.kind == "maintenance" and "supersedes" in f.payload and "by_fact_id" in f.payload}
        history = tuple(replace(f, superseded_by=superseded.get(f.fact_id, f.superseded_by)) for f in raw)
        epoch = max((f.epoch for f in history if f.kind == "epoch_change"), default=0)
        current = [f for f in history if f.epoch == epoch and not f.superseded_by]
        clearances = {f.payload.get("blocker_fact_id") for f in current if f.kind == "clearance"
                      and f.source in {"host", "maintenance", "migration"}}
        answers = {f.payload.get("question_fact_id") for f in current if f.kind == "answer" and f.source in {"user", "host"}}
        phases = [f.payload["phase"] for f in current if f.kind == "phase_change"]
        receipts = {}
        artifacts = {}
        for fact in history:
            if fact.superseded_by:
                continue
            if (fact.kind == "receipt" and 'status' in fact.payload
                    and (fact.epoch == epoch or successful_receipt(fact.payload))):
                key = fact.payload.get("request_id") or fact.fact_id
                old = receipts.get(key)
                if old is None or not successful_receipt(old.payload) or successful_receipt(fact.payload):
                    receipts[key] = fact
            if fact.kind == "artifact":
                artifacts[fact.payload.get("path") or fact.fact_id] = fact
        questions = [f for f in current if f.kind == "question" and f.fact_id not in answers]
        progress = [f for f in current if f.kind == "progress"]
        return NodeView(node_id, phases[-1] if phases else "ready", epoch,
            tuple(f for f in current if f.kind == "blocker" and f.fact_id not in clearances),
            tuple(receipts.values()), tuple(artifacts.values()), questions[-1] if questions else None,
            progress[-1] if progress else None, history)


def ledger_for_run(record):
    return NodeLedger(record.setdefault("node_ledgers", {}))


def project_node_view(record, node_id):
    """Deprecated stage keys are projections; the append-only facts own state."""
    view = ledger_for_run(record).view(node_id)
    stage = record.setdefault("node_states", {}).setdefault(node_id, {})
    handoffs = [fact for fact in view.history if fact.kind == "handoff" and fact.epoch == view.epoch and not fact.superseded_by]
    stage["epoch"] = view.epoch
    stage["node_phase"] = view.phase
    stage.setdefault("assistance_receipts", [])
    stage["ledger_view"] = {"node_id": node_id, "phase": view.phase, "epoch": view.epoch,
        "active_blockers": [asdict(f) for f in view.active_blockers],
        "current_receipts": [asdict(f) for f in view.current_receipts],
        "artifacts": [asdict(f) for f in view.artifacts],
        "pending_question": asdict(view.pending_question) if view.pending_question else None,
        "last_progress": asdict(view.last_progress) if view.last_progress else None,
        "last_handoff": asdict(handoffs[-1]) if handoffs else None,
        "history_count": len(view.history)}
    return view


def sync_node_facts(record, node_id, *, source="host", include_legacy_blockers=False):
    """Ingest structured host state after an event, never infer completed work."""
    ledger = ledger_for_run(record)
    stage = (record.get("node_states") or {}).get(node_id) or {}
    for row in stage.get("command_receipts") or []:
        ledger.record(node_id, "receipt", row, source=source)
    handoff = stage.get("handoff")
    if isinstance(handoff, dict) and handoff:
        ledger.record(node_id, "handoff", handoff, source=source)
    artifacts = list(stage.get("artifacts") or []) + list((handoff or {}).get("artifacts") or [])
    for value in artifacts:
        payload = value if isinstance(value, dict) else {"path": value}
        if payload.get("path"):
            ledger.record(node_id, "artifact", payload, source=source)
    pending = stage.get("pending_interaction")
    # The wait protocol owns live question facts and their fingerprints.
    # Only migration imports legacy questions without that protocol metadata.
    if source == 'migration' and isinstance(pending, dict) and pending:
        ledger.record(node_id, "question", pending, source=source)
    if include_legacy_blockers:
        from mewcode.experts.recovery_state import blockers
        raw_stage = {key: value for key, value in stage.items() if key != "ledger_view"}
        # An active tool is not an uncertain effect. Recovery reconciliation
        # marks interrupted calls outcome_unknown after their lifetime ends.
        raw_stage['tools'] = {key: value for key, value in (stage.get('tools') or {}).items()
                              if value.get('status') != 'running'}
        for row in blockers(raw_stage, error_kind=str(stage.get("error_kind") or "")):
            if row.get('ref') == 'current_stop':
                row = {**row, 'occurrence': [stage.get('node_attempt'), stage.get('input_revision')]}
            ledger.record(node_id, "blocker", row, source=source)
        for row in stage.get("blockers") or []:
            payload = row if isinstance(row, dict) else {"kind": "business_ambiguity", "reason": str(row)}
            ledger.record(node_id, "blocker", payload, source=source)
    phase = STATUS_PHASE.get(stage.get("status"))
    if phase:
        if source == "migration":
            ledger.record(node_id, "phase_change", {"phase": phase, "reason": "legacy_status"}, source=source)
        else:
            ledger.set_phase(node_id, phase, reason="host_status")
    from mewcode.experts.recovery_state import observed_progress_marker, advance_work_cursor
    marker = observed_progress_marker(record, node_id)
    previous = ledger.view(node_id).last_progress
    previous_marker = previous.payload.get("marker") if previous else None
    if previous_marker:
        _, marker = advance_work_cursor({"before": previous_marker, "after": marker})
    ledger.record(node_id, "progress", {"marker": marker}, source=source)
    return project_node_view(record, node_id)


def sync_run_facts(record):
    if "node_ledgers" not in record:
        return
    from mewcode.experts.blocking_policy import refresh_clearances
    for node_id in list(record.get("node_states") or {}):
        sync_node_facts(record, node_id, include_legacy_blockers=True)
        refresh_clearances(record, node_id)


def migrate_legacy_node_state(run_record):
    """Pure, idempotent conversion. No model, storage, or execution calls."""
    if "node_ledgers" in run_record:
        return run_record
    run_record["node_ledgers"] = {}
    if "node_states" not in run_record:
        run_record["node_states"] = copy.deepcopy(run_record.get("stages") or {})
    for node_id in run_record["node_states"]:
        sync_node_facts(run_record, node_id, source="migration", include_legacy_blockers=True)
    run_record["status"] = "paused"
    run_record["ledger_migration"] = {"version": 1, "model_calls": 0, "requires_explicit_resume": True}
    return run_record


def bump_run_epoch(record, node_id, *, reason, source_ref):
    ledger = ledger_for_run(record)
    epoch = ledger.bump_epoch(node_id, reason=reason, source_ref=source_ref)
    from mewcode.experts.recovery_case import cases, supersede_cases
    supersede_cases(record, node_id)
    latest = cases(record, node_id)
    if latest:
        record['node_states'][node_id]['recovery_case'] = latest[-1]
        record['node_states'][node_id].pop('pending_recovery_case', None)
    view = project_node_view(record, node_id)
    stage = record["node_states"][node_id]
    stage.update(status="pending", handoff={}, blockers=[], pending_interaction=None,
        command_receipts=[copy.deepcopy(dict(f.payload)) for f in view.current_receipts])
    for key in ("error_kind", "request_error", "pending_permission"):
        stage.pop(key, None)
    stage.pop("completed_at", None)
    return epoch
