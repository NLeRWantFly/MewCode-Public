"""Append-only receipts and bounded, resumable review of a frozen source.

An in-flight remote call is *not* replayed after a crash: its billing/outcome is
unknown until reconciled.  Only completed, source-bound read judgments are reused.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mewcode.orchestration.paused_handoff_classify import (
    CLASSIFIER_VERSION, SemanticJudge, classification_semantic_request,
    classify_fragment,
    hard_review_hold, host_retention,
)
from mewcode.permissions.review_outcome import ReviewContextTooLarge
from mewcode.orchestration.paused_handoff_index import PausedHandoffIndex


REVIEW_VERSION = 1


def _canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


@contextmanager
def _journal_lock(path: Path):
    """Crash-released, cross-process lock for one private review journal."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _journal_head(path: Path) -> str:
    """Read and self-check only the last complete event under its lock."""
    if not path.exists():
        return "0" * 64
    with path.open("rb") as handle:
        end = handle.seek(0, os.SEEK_END)
        if not end:
            return "0" * 64
        handle.seek(end - 1)
        if handle.read(1) != b"\n":
            raise ValueError("handoff review journal has an incomplete tail")
        cursor = end - 2
        start = 0
        while cursor >= 0:
            size = min(8192, cursor + 1)
            handle.seek(cursor + 1 - size)
            block = handle.read(size)
            newline = block.rfind(b"\n")
            if newline >= 0:
                start = cursor + 2 - size + newline
                break
            cursor -= size
        handle.seek(start)
        raw = handle.read(end - 1 - start)
    try:
        event = json.loads(raw)
        digest = event.pop("event_sha256")
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("handoff review journal tail is invalid") from exc
    if digest != hashlib.sha256(_canonical(event)).hexdigest():
        raise ValueError("handoff review journal tail is corrupt")
    return digest


class ReviewLedger:
    def __init__(self, index: PausedHandoffIndex, *, job_id: str = "",
                 reconciled_records: dict[str, str] | None = None) -> None:
        self.index = index
        if job_id and not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise ValueError("invalid handoff review job id")
        self.job_id = job_id
        self.reconciled_records = reconciled_records or {}
        self.path = index.manifest_path.with_suffix(
            f".{job_id}.receipts.jsonl" if job_id else ".receipts.jsonl"
        )
        self.events: list[dict[str, Any]] = []
        self.latest: dict[str, dict[str, Any]] = {}
        self.classifications: dict[str, dict[str, Any]] = {}
        self._fragments = {
            fragment["id"]: fragment
            for record in index.manifest["records"] for fragment in record["fragments"]
        }
        self._head = "0" * 64
        self._active_review_attempts: set[str] = set()
        self._active_omission_attempts: set[str] = set()
        with _journal_lock(self.path):
            self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("rb") as handle:
            for raw in handle:
                event = json.loads(raw)
                digest = event.pop("event_sha256", None)
                if event.get("previous_sha256") != self._head or digest != hashlib.sha256(_canonical(event)).hexdigest():
                    raise ValueError("handoff review receipt chain is corrupt")
                if event.get("source_id") != self.index.manifest["id"]:
                    raise ValueError("handoff review receipt belongs to another source")
                if event.get("job_id", "") != self.job_id:
                    raise ValueError("handoff review receipt belongs to another job")
                fragment = self._fragments.get(event.get("fragment_id"))
                if fragment is None or event.get("fragment_sha256") != fragment["sha256"]:
                    raise ValueError("handoff review receipt has an invalid source fragment")
                self._check_transition(fragment, event["phase"])
                self._check_escalation(fragment, event)
                if event["phase"] in {"classified", "reviewed"} and not isinstance(event.get("classification"), dict):
                    raise ValueError("handoff review classification receipt is invalid")
                if (event["phase"] == "omission_reviewed"
                        and event.get("classification") != self.classifications.get(fragment["id"])):
                    raise ValueError("handoff omission receipt changed its classification")
                event["event_sha256"] = digest
                self.events.append(event)
                self.latest[event["fragment_id"]] = event
                if event["phase"] in {"classified", "reviewed"}:
                    self.classifications[event["fragment_id"]] = event["classification"]
                self._head = digest

    def _check_transition(self, fragment: dict, phase: str) -> None:
        last = self.latest.get(fragment["id"])
        previous = last["phase"] if last else None
        allowed = {
            None: {"started", "classified"},
            "started": {"classified"},
            "classified": {"review_started", "reviewed", "omission_started", "omission_reviewed"},
            "review_started": {"reviewed"},
            "omission_started": {"omission_reviewed"},
            "omission_reviewed": set(),
            "reviewed": {"review_started", "reviewed"},
        }
        if phase not in allowed.get(previous, set()):
            raise ValueError("handoff review receipt transition is invalid")

    def _check_escalation(self, fragment: dict, event: dict) -> None:
        previous = self.latest.get(fragment["id"])
        if not previous or previous.get("phase") != "reviewed":
            return
        if (previous.get("decision") != "hold"
                or event.get("escalation_from") != previous.get("event_sha256")):
            raise ValueError("handoff review escalation is not bound to the prior hold")
        if event.get("phase") == "review_started":
            if previous.get("model") == "host":
                if not re.fullmatch(r"[a-f0-9]{64}",
                                    str(event.get("reconciliation_receipt") or "")):
                    raise ValueError("host hold needs a source-bound reconciliation reference")
            elif (previous.get("model") in {"", event.get("model")}
                  or (previous.get("judgment") or {}).get("choice") != "unchanged"):
                raise ValueError("only uncertain semantic holds can use a distinct review route")
            if any(item.get("phase") == "review_started"
                   and item.get("escalation_from")
                   and item.get("fragment_id") == fragment["id"]
                   for item in self.events):
                raise ValueError("handoff review escalation is limited to one attempt")
        elif event.get("phase") == "reviewed":
            original = self.latest.get(str(event.get("inherited_from") or "")) or {}
            if (not event.get("inherited_from")
                    or str(event.get("inherited_from")).split(":")[0]
                    != fragment["id"].split(":")[0]
                    or original.get("phase") != "reviewed"
                    or original.get("decision") != "retain"
                    or event.get("decision") != "retain"
                    or event.get("evidence_refs") != [fragment["id"]]):
                raise ValueError("inherited escalation lacks a retained original")

    def append(self, fragment: dict, phase: str, **fields: Any) -> dict:
        known = self._fragments.get(fragment.get("id"))
        if known is None or known["sha256"] != fragment.get("sha256"):
            raise ValueError("handoff review fragment is not indexed")
        self._check_transition(fragment, phase)
        self._check_escalation(fragment, {"phase": phase, **fields})
        if phase in {"classified", "reviewed"} and not isinstance(fields.get("classification"), dict):
            raise ValueError("handoff review classification receipt is invalid")
        if (phase == "omission_reviewed"
                and fields.get("classification") != self.completed_classification(fragment)):
            raise ValueError("handoff omission receipt changed its classification")
        event = {
            "schema_version": REVIEW_VERSION,
            "source_id": self.index.manifest["id"],
            "job_id": self.job_id,
            "fragment_id": fragment["id"], "fragment_sha256": fragment["sha256"],
            "policy_version": CLASSIFIER_VERSION,
            "phase": phase, "at": time.time(),
            "previous_sha256": self._head, **fields,
        }
        event["event_sha256"] = hashlib.sha256(_canonical(event)).hexdigest()
        with _journal_lock(self.path):
            if _journal_head(self.path) != self._head:
                raise ValueError("handoff review journal advanced concurrently; reload before any paid call")
            with self.path.open("ab") as handle:
                handle.write(_canonical(event) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
        self.events.append(event)
        self.latest[fragment["id"]] = event
        if phase in {"classified", "reviewed"}:
            self.classifications[fragment["id"]] = event["classification"]
        self._head = event["event_sha256"]
        return event

    def completed_classification(self, fragment: dict) -> dict | None:
        if self._fragments.get(fragment["id"], {}).get("sha256") != fragment["sha256"]:
            return None
        return self.classifications.get(fragment["id"])

    def mark_reviewed(self, fragment: dict, *, decision: str,
                      evidence_refs: list[str], model: str,
                      usage: dict[str, int] | None = None,
                      inherited_from: str = "") -> dict:
        classification = self.completed_classification(fragment)
        if classification is None:
            raise ValueError("fragment must be classified before review")
        if decision not in {"retain", "hold"}:
            raise ValueError("review cannot authorize orchestration resume")
        if not evidence_refs or any(ref not in {fragment["id"], fragment["record_id"]}
                                    for ref in evidence_refs):
            raise ValueError("review evidence must refer to the frozen source")
        current = self.latest.get(fragment["id"])
        if current and current["phase"] == "review_started":
            raise ValueError("remote review outcome is unknown and needs reconciliation")
        if current and current["phase"] == "reviewed":
            if current.get("decision") != decision or current.get("evidence_refs") != evidence_refs:
                raise ValueError("review receipt is already final")
            return current
        return self.append(fragment, "reviewed", classification=classification,
                           decision=decision, evidence_refs=evidence_refs,
                           model=model, usage=usage or {},
                           inherited_from=inherited_from)

    def start_review(self, fragment: dict, *, model: str,
                     reserved_tokens: int,
                     base_reserved_tokens: int | None = None,
                     reconciliation_receipt: str = "") -> str:
        if self.completed_classification(fragment) is None:
            raise ValueError("fragment must be classified before review")
        attempt_id = uuid.uuid4().hex
        previous = self.latest.get(fragment["id"]) or {}
        if (previous.get("phase") == "reviewed" and previous.get("model") == "host"
                and reconciliation_receipt
                != self.reconciled_records.get(fragment["record_id"])):
            raise ValueError("host hold needs a current effect reconciliation receipt")
        escalation_from = (previous.get("event_sha256")
                           if previous.get("phase") == "reviewed" else None)
        self.append(fragment, "review_started", model=model,
                    reserved_tokens=max(0, int(reserved_tokens)),
                    base_reserved_tokens=max(0, int(
                        reserved_tokens if base_reserved_tokens is None else base_reserved_tokens)),
                    attempt_id=attempt_id, escalation_from=escalation_from,
                    reconciliation_receipt=reconciliation_receipt)
        self._active_review_attempts.add(attempt_id)
        return attempt_id

    def finish_review(self, fragment: dict, *, attempt_id: str,
                      decision: str, evidence_refs: list[str], model: str,
                      judgment: dict[str, Any], usage: dict[str, int],
                      latency_ms: int | None = None) -> dict:
        latest = self.latest.get(fragment["id"])
        if (attempt_id not in self._active_review_attempts or not latest
                or latest.get("phase") != "review_started"
                or latest.get("attempt_id") != attempt_id):
            raise ValueError("remote review attempt is not active in this process")
        if decision not in {"retain", "hold"}:
            raise ValueError("review decision cannot authorize orchestration resume")
        if not evidence_refs or fragment["id"] not in evidence_refs:
            raise ValueError("review evidence must include this fragment")
        event = self.append(fragment, "reviewed",
                            classification=self.completed_classification(fragment),
                            decision=decision, evidence_refs=evidence_refs,
                            model=model, judgment=judgment, usage=usage,
                            latency_ms=latency_ms)
        self._active_review_attempts.discard(attempt_id)
        return event

    def inherit_escalated_review(self, fragment: dict, *, original: dict) -> dict:
        previous = self.latest.get(fragment["id"]) or {}
        if (previous.get("phase") != "reviewed" or previous.get("decision") != "hold"
                or original.get("phase") != "reviewed"
                or original.get("decision") != "retain"
                or original.get("fragment_id") == fragment["id"]
                or original.get("fragment_id", "").split(":")[0]
                != fragment["id"].split(":")[0]):
            raise ValueError("escalated inheritance requires the same frozen record")
        return self.append(
            fragment, "reviewed", classification=self.completed_classification(fragment),
            decision="retain", evidence_refs=[fragment["id"]],
            model=original.get("model") or "", usage={},
            inherited_from=original["fragment_id"],
            escalation_from=previous["event_sha256"],
        )

    def start_omission(self, fragment: dict, *, model: str,
                       reserved_tokens: int,
                       base_reserved_tokens: int | None = None) -> str:
        if (self.completed_classification(fragment) or {}).get("level") not in {"routine", "unrelated"}:
            raise ValueError("only low-attention source can receive an omission audit")
        attempt_id = uuid.uuid4().hex
        self.append(fragment, "omission_started", model=model,
                    reserved_tokens=max(0, int(reserved_tokens)),
                    base_reserved_tokens=max(0, int(
                        reserved_tokens if base_reserved_tokens is None else base_reserved_tokens)),
                    attempt_id=attempt_id)
        self._active_omission_attempts.add(attempt_id)
        return attempt_id

    def finish_omission(self, fragment: dict, *, attempt_id: str,
                        safe: bool, judgment: dict[str, Any],
                        model: str, usage: dict[str, int],
                        latency_ms: int | None = None) -> dict:
        latest = self.latest.get(fragment["id"])
        if (attempt_id not in self._active_omission_attempts or not latest
                or latest.get("phase") != "omission_started"
                or latest.get("attempt_id") != attempt_id):
            raise ValueError("remote omission audit outcome is unknown")
        event = self.append(fragment, "omission_reviewed", safe=bool(safe),
                            judgment=judgment, model=model, usage=usage,
                            classification=self.completed_classification(fragment),
                            latency_ms=latency_ms)
        self._active_omission_attempts.discard(attempt_id)
        return event

    def import_verified_prefix(self, prior_index: PausedHandoffIndex,
                               prior: "ReviewLedger") -> int:
        """Carry read-only receipts across an append-only source generation.

        Usage is *not* copied: the caller must include prior generations when
        calculating the one user-approved total token cap.
        """
        prior_index.verify(original=True)
        self.index.verify(original=True)
        old_source = prior_index.manifest["source"]
        new_source = self.index.manifest["source"]
        if (old_source["run_id"] != new_source["run_id"]
                or old_source["conversation_id"] != new_source["conversation_id"]
                or old_source["contract_fingerprint"] != new_source["contract_fingerprint"]
                or old_source["log_path"] != new_source["log_path"]
                or old_source["file_identity"] != new_source["file_identity"]
                or old_source["start"] != new_source["start"]
                or old_source["end"] >= new_source["end"]
                or prior_index.manifest.get("frozen_boundaries")
                != self.index.manifest.get("frozen_boundaries")
                or prior.job_id != self.job_id):
            raise ValueError("prior handoff generation is not an append-only prefix")
        current_records = {record["id"]: record for record in self.index.manifest["records"]}
        imported = 0
        for old_record in prior_index.manifest["records"]:
            new_record = current_records.get(old_record["id"])
            if (new_record is None or new_record["sha256"] != old_record["sha256"]
                    or new_record["start"] != old_record["start"]
                    or new_record["end"] != old_record["end"]):
                raise ValueError("handoff source prefix record changed")
            for fragment in old_record["fragments"]:
                latest = prior.latest.get(fragment["id"])
                if latest is None:
                    continue
                existing = self.latest.get(fragment["id"])
                classification = prior.completed_classification(fragment)
                if classification is None:
                    if latest["phase"] == "started" and existing is None:
                        self.append(fragment, "started", model=latest.get("model") or "",
                                    reserved_tokens=latest.get("reserved_tokens") or 0,
                                    reused_from_source_id=prior_index.manifest["id"])
                        imported += 1
                    continue
                if existing is None:
                    self.append(fragment, "classified", classification=classification,
                                model=latest.get("model") or "", usage={},
                                reused_from_source_id=prior_index.manifest["id"])
                    imported += 1
                    existing = self.latest.get(fragment["id"])
                if existing["phase"] != "classified":
                    continue
                if latest["phase"] == "review_started":
                    self.append(fragment, "review_started",
                                model=latest.get("model") or "",
                                reserved_tokens=latest.get("reserved_tokens") or 0,
                                attempt_id=latest.get("attempt_id") or "",
                                reused_from_source_id=prior_index.manifest["id"])
                elif latest["phase"] == "reviewed":
                    self.append(fragment, "reviewed", classification=classification,
                                decision=latest.get("decision"),
                                evidence_refs=latest.get("evidence_refs") or [],
                                model=latest.get("model") or "",
                                judgment=latest.get("judgment") or {},
                                inherited_from=latest.get("inherited_from") or "",
                                usage={}, reused_from_source_id=prior_index.manifest["id"])
                elif latest["phase"] in {"omission_started", "omission_reviewed"}:
                    self.append(fragment, latest["phase"],
                                model=latest.get("model") or "",
                                reserved_tokens=latest.get("reserved_tokens") or 0,
                                attempt_id=latest.get("attempt_id") or "",
                                safe=latest.get("safe", False),
                                judgment=latest.get("judgment") or {},
                                classification=classification, usage={},
                                reused_from_source_id=prior_index.manifest["id"])
        return imported

    def progress(self) -> dict[str, Any]:
        fragments = [fragment for record in self.index.manifest["records"]
                     for fragment in record["fragments"]]
        reviewed = sum(self.latest.get(fragment["id"], {}).get("phase") == "reviewed"
                       for fragment in fragments)
        retained = sum(
            self.latest.get(fragment["id"], {}).get("phase") == "reviewed"
            and self.latest[fragment["id"]].get("decision") == "retain"
            for fragment in fragments
        )
        held = reviewed - retained
        core_total = sum(
            (self.completed_classification(fragment) or {}).get("level")
            in {"core", "uncertain"}
            for fragment in fragments
        )
        supporting_total = sum(
            (self.completed_classification(fragment) or {}).get("level") == "supporting"
            for fragment in fragments
        )
        classified = sum(self.completed_classification(fragment) is not None
                         for fragment in fragments)
        unresolved = [fragment["id"] for fragment in fragments
                      if self.latest.get(fragment["id"], {}).get("phase")
                      in {"started", "review_started", "omission_started"}]
        unknown_reserved = sum(max(0, int(self.latest[fragment_id].get("reserved_tokens") or 0))
                               for fragment_id in unresolved)
        usage = {"input_tokens": 0, "output_tokens": 0}
        for event in self.events:
            if event["phase"] not in {"classified", "reviewed", "omission_reviewed"}:
                continue
            actual = event.get("usage") or {}
            for key in usage:
                usage[key] += max(0, int(actual.get(key) or 0))
        return {"total": len(fragments), "classified": classified,
                "core_total": core_total, "supporting_total": supporting_total,
                "reviewed": reviewed, "retained": retained, "held": held,
                "outcome_unknown": unresolved,
                "unknown_reserved_tokens": unknown_reserved,
                "omission_reviewed": sum(
                    self.latest.get(fragment["id"], {}).get("phase") == "omission_reviewed"
                    for fragment in fragments
                ),
                "usage": usage}


def preflight(index: PausedHandoffIndex, *, remaining_tokens: int | None,
              remaining_seconds: float | None,
              tokens_per_second: float | None = None,
              ledger: ReviewLedger | None = None,
              task_contract_bytes: int = 0,
              escalation_enabled: bool = False,
              reconciled_records: dict[str, str] | None = None) -> dict[str, Any]:
    """Conservative budget projection; missing limits never imply new grants."""
    reconciled_records = reconciled_records or {}
    fragments = [fragment for record in index.manifest["records"]
                 for fragment in record["fragments"]]
    needs_classification = [
        fragment for record in index.manifest["records"]
        if host_retention(record)[0] is None
        for fragment in record["fragments"]
        if ledger is None or ledger.completed_classification(fragment) is None
    ]
    semantic_bytes = sum(fragment["end"] - fragment["start"]
                         for fragment in needs_classification)
    needs_review = [
        record for record in index.manifest["records"]
        if (host_retention(record)[0] in {"core", "uncertain", None}
            or ledger is not None and any(
                (ledger.completed_classification(fragment) or {}).get("level")
                in {"core", "uncertain"} for fragment in record["fragments"]
            ))
        and (ledger is None or any(
            ledger.latest.get(fragment["id"], {}).get("phase") != "reviewed"
            or (record["id"] in reconciled_records
                and ledger.latest.get(fragment["id"], {}).get("model") == "host")
            for fragment in record["fragments"]
        ))
    ]
    # Hard provenance/effect holds remain outstanding even after their source
    # has been marked ``reviewed: hold``.  Recomputing only unfinished reviews
    # would incorrectly make the UI report zero reconciliation records.
    hard_holds = [(record, hard_review_hold(record)) for record in index.manifest["records"]]
    hard_holds = [(record, reason) for record, reason in hard_holds
                  if reason and record["id"] not in reconciled_records]
    hard_hold_tools: Counter[str] = Counter()
    for record, _reason in hard_holds:
        calls = ((record.get("calls") or []) if record.get("record_type") == "assistant"
                 else [record])
        for call in calls:
            tool = str(call.get("tool") or "unknown")
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", tool):
                tool = "unknown"
            hard_hold_tools[tool] += 1
    eligible_review = [record for record in needs_review
                       if not hard_review_hold(record) or record["id"] in reconciled_records]
    core_review_bytes = sum(record["end"] - record["start"]
                            for record in eligible_review)
    escalation_records = []
    if escalation_enabled and ledger is not None:
        escalated_ids = {
            event["fragment_id"] for event in ledger.events
            if event.get("phase") == "review_started" and event.get("escalation_from")
        }
        for record in index.manifest["records"]:
            if hard_review_hold(record):
                continue
            critical = [fragment for fragment in record["fragments"]
                        if (ledger.completed_classification(fragment) or {}).get("level")
                        in {"core", "uncertain"}]
            if not critical or critical[0]["id"] in escalated_ids:
                continue
            receipt = ledger.latest.get(critical[0]["id"]) or {}
            if (receipt.get("phase") == "reviewed"
                    and receipt.get("decision") == "hold"
                    and receipt.get("model") not in {"", "host"}
                    and (receipt.get("judgment") or {}).get("choice") == "unchanged"):
                escalation_records.append(record)
    possible_omission = [
        record for record in index.manifest["records"]
        if host_retention(record)[0] is None
        and (ledger is None or any(
            (ledger.completed_classification(fragment) or {}).get("level")
            in {"routine", "unrelated"}
            and ledger.latest.get(fragment["id"], {}).get("phase") != "omission_reviewed"
            for fragment in record["fragments"]
        ))
    ]
    # UTF-8 byte length is a conservative planning ceiling, not a tokenizer.
    classification_tokens_upper = semantic_bytes + (2048 + task_contract_bytes) * len(needs_classification)
    core_review_tokens_upper = core_review_bytes + (2048 + task_contract_bytes) * len(eligible_review)
    escalation_tokens_upper = sum(record["end"] - record["start"]
                                  for record in escalation_records) + (
                                      (2048 + task_contract_bytes) * len(escalation_records)
                                  )
    omission_tokens_upper = sum(record["end"] - record["start"]
                                for record in possible_omission) + (
                                    2048 + task_contract_bytes
                                ) * len(possible_omission)
    base_remaining = (classification_tokens_upper + core_review_tokens_upper
                      + escalation_tokens_upper
                      + omission_tokens_upper)
    # Compare actual completed calls to their durable per-call reservations.
    # An observed overrun raises the future estimate; cheaper calls only
    # reduce it by becoming completed work, never by weakening the bound.
    from mewcode.orchestration.paused_handoff_budget import (
        FINAL_DECISION_RESERVE_TOKENS, calibrated_review_reservation,
        observed_review_token_ratio,
    )
    ratio_numerator, ratio_denominator, ratio_samples = (
        observed_review_token_ratio(ledger.events) if ledger is not None else (1, 1, 0)
    )
    adjusted_remaining = calibrated_review_reservation(
        base_remaining, ratio_numerator=ratio_numerator,
        ratio_denominator=ratio_denominator,
    )
    final_reserve = calibrated_review_reservation(
        FINAL_DECISION_RESERVE_TOKENS, ratio_numerator=ratio_numerator,
        ratio_denominator=ratio_denominator,
    )
    estimate_tokens = adjusted_remaining + final_reserve
    observed_rates = []
    if ledger is not None and tokens_per_second is None:
        for event in ledger.events:
            usage = event.get("usage") or {}
            spent_tokens = max(0, int(usage.get("input_tokens") or 0)) + max(
                0, int(usage.get("output_tokens") or 0)
            )
            latency_ms = event.get("latency_ms")
            if (event.get("phase") in {"classified", "reviewed", "omission_reviewed"}
                    and spent_tokens > 0 and isinstance(latency_ms, (int, float))
                    and not isinstance(latency_ms, bool) and latency_ms > 0):
                observed_rates.append(spent_tokens * 1000 / latency_ms)
        if len(observed_rates) >= 3:
            tokens_per_second = min(observed_rates)
    seconds = (estimate_tokens / tokens_per_second
               if tokens_per_second and tokens_per_second > 0 else None)
    effective_remaining = (None if remaining_tokens is None else
                           max(0, remaining_tokens - sum(ledger.progress()["usage"].values())
                               - ledger.progress()["unknown_reserved_tokens"])
                           if ledger is not None else remaining_tokens)
    shortage = (
        "budget_unknown" if effective_remaining is None
        else "insufficient_budget" if estimate_tokens > effective_remaining
        else "insufficient_time" if seconds is not None and remaining_seconds is not None and seconds > remaining_seconds
        else ""
    )
    return {"source_id": index.manifest["id"], "fragments": len(fragments),
            "raw_bytes": index.manifest["source"]["end"] - index.manifest["source"]["start"],
            "estimated_classification_tokens_upper": classification_tokens_upper,
            "estimated_core_review_tokens_upper": core_review_tokens_upper,
            "estimated_escalation_tokens_upper": escalation_tokens_upper,
            "estimated_omission_audit_tokens_upper": omission_tokens_upper,
            "estimated_final_decision_tokens_upper": final_reserve,
            "base_remaining_tokens_upper": base_remaining,
            "observed_token_ratio": round(ratio_numerator / ratio_denominator, 3),
            "observed_token_ratio_samples": ratio_samples,
            "deterministic_hold_records": len(hard_holds),
            "deterministic_hold_reasons": dict(sorted(Counter(
                reason for _record, reason in hard_holds
            ).items())),
            "deterministic_hold_tools": dict(sorted(hard_hold_tools.items())),
            "first_hold_ref": hard_holds[0][0]["id"] if hard_holds else "",
            "estimated_semantic_tokens_upper": estimate_tokens,
            "estimated_seconds": seconds, "remaining_tokens": remaining_tokens,
            "time_estimate_basis": ("conservative_observed_minimum" if len(observed_rates) >= 3
                                    else "caller_supplied_rate" if tokens_per_second else
                                    "insufficient_completed_samples"),
            "time_estimate_samples": len(observed_rates),
            "estimated_cost": None, "cost_estimate_basis": "model_tariff_unavailable",
            "effective_remaining_tokens": effective_remaining,
            "remaining_seconds": remaining_seconds, "shortage": shortage,
            "algorithm": "utf8_bytes_receipt_recalibration_and_final_reserve_v3"}


async def classify_with_receipts(index: PausedHandoffIndex, ledger: ReviewLedger,
                                 judge: SemanticJudge | None, *, task_contract: str,
                                 remaining_tokens: int | None) -> dict[str, Any]:
    index.verify()
    from mewcode.orchestration.paused_handoff_budget import (
        calibrated_review_reservation, observed_review_token_ratio,
    )
    spent = ledger.progress()["usage"]
    used = spent["input_tokens"] + spent["output_tokens"]
    ratio_numerator, ratio_denominator, _ = observed_review_token_ratio(ledger.events)
    semantic_unavailable = 0
    for record in index.manifest["records"]:
        record_decision = None
        for fragment in record["fragments"]:
            existing = ledger.completed_classification(fragment)
            if existing is not None:
                record_decision = existing
                continue
            if host_retention(record)[0] is None and judge is None:
                # No-grant preparation may index and classify host-known
                # records, but must not freeze a speculative "uncertain"
                # semantic receipt that a later authorized job cannot refine.
                semantic_unavailable += 1
                continue
            last = ledger.latest.get(fragment["id"])
            if last and last["phase"] == "started":
                return {"status": "outcome_unknown", "fragment_id": fragment["id"],
                        "progress": ledger.progress()}
            semantic = host_retention(record)[0] is None and judge is not None and record_decision is None
            base_reserve = (record["end"] - record["start"]
                            + len(task_contract.encode("utf-8")) + 2048) if semantic else 0
            reserve = calibrated_review_reservation(
                base_reserve, ratio_numerator=ratio_numerator,
                ratio_denominator=ratio_denominator,
            )
            if semantic:
                request = classification_semantic_request(
                    index, record, fragment, task_contract=task_contract,
                )
                local_preflight = getattr(judge, "preflight_evaluation", None)
                if request is not None and callable(local_preflight):
                    try:
                        local_preflight(*request)
                    except ReviewContextTooLarge as exc:
                        return {"status": "context_window_insufficient",
                                "fragment_id": fragment["id"],
                                "context_sizes": exc.sizes,
                                "progress": ledger.progress()}
            if semantic and remaining_tokens is None:
                return {"status": "budget_unknown", "fragment_id": fragment["id"],
                        "progress": ledger.progress()}
            if remaining_tokens is not None and used + reserve > remaining_tokens:
                return {"status": "insufficient_budget", "fragment_id": fragment["id"],
                        "needed_tokens_upper": used + reserve,
                        "progress": ledger.progress()}
            started = time.monotonic()
            if semantic:
                ledger.append(fragment, "started", model=str(getattr(judge, "model", "")),
                              reserved_tokens=reserve,
                              base_reserved_tokens=base_reserve)
            if record_decision is not None:
                classification = {
                    **record_decision,
                    "fragment_id": fragment["id"], "record_id": record["id"],
                    "start": fragment["start"], "end": fragment["end"],
                    "source_sha256": fragment["sha256"],
                    "inherited_from": record["fragments"][0]["id"],
                    "usage": {},
                }
            else:
                classification = await classify_fragment(
                    index, record, fragment, judge, task_contract=task_contract,
                )
                classification["semantic_record_sha256"] = record["sha256"]
            record_decision = classification
            usage = classification.get("usage") or {}
            actual = max(0, int(usage.get("input_tokens") or 0)) + max(
                0, int(usage.get("output_tokens") or 0))
            if semantic and (classification["confidence_source"] == "model_error"
                             or classification["confidence_source"] == "typed_semantic"
                             and actual <= 0):
                # The remote request may have consumed budget, and may even
                # have completed despite a transport/schema failure.  Leave
                # its durable start receipt unresolved; never bill it as zero
                # and move on to another paid fragment.
                return {"status": "outcome_unknown", "fragment_id": fragment["id"],
                        "reason": "semantic_result_or_usage_unknown",
                        "progress": ledger.progress()}
            classification.pop("usage", None)
            used += actual
            ledger.append(fragment, "classified", classification=classification,
                          model=classification.get("model") or "",
                          usage=usage, latency_ms=round((time.monotonic() - started) * 1000),
                          next_cursor=fragment["end"])
            if semantic and actual * ratio_denominator > ratio_numerator * base_reserve:
                ratio_numerator, ratio_denominator = actual, base_reserve
            if remaining_tokens is not None and used > remaining_tokens:
                return {"status": "actual_budget_overrun", "fragment_id": fragment["id"],
                        "spent_tokens": used, "progress": ledger.progress()}
    return {"status": "semantic_route_unavailable" if semantic_unavailable else "classified",
            "unclassified_semantic_fragments": semantic_unavailable,
            "progress": ledger.progress()}
