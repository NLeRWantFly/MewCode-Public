"""Bounded, in-memory graph completion and diagnostic-guided repair."""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
from typing import Any, Callable, Sequence

from .graph_completion import apply_completion, can_resolve_proposed_conflict, protected_violations
from .graph_completion_protocol import (
    COMPLETION_SYSTEM, CompletionParseError, CompletionRequestTooLarge,
    build_completion_request, normalize_proposal, parse_completion_response,
)
from .graph_readiness import readiness
from .graph_completion_scope import scope_rejections


def _atoms(diagnostics):
    atoms = set()
    for diagnostic in diagnostics:
        code = diagnostic.get("code", "")
        if code == "disconnected" and isinstance(diagnostic.get("components"), list):
            components = diagnostic["components"]
            # Joining existing components only removes disconnected pairs;
            # adding a new orphan still introduces new errors. Singleton-only
            # diagnostics used to hide other components until a valid fix.
            for index, part in enumerate(components):
                for other in components[index + 1:]:
                    atoms.update((code, *sorted((left, right))) for left in part for right in other)
            continue
        ids = diagnostic.get("entry_ids") or []
        if ids:
            atoms.update((code, str(entry_id)) for entry_id in ids)
        else:
            atoms.add((code, str(diagnostic.get("path", "$"))))
    return atoms


def _receipts(rows):
    return [{"entry_id": row.get("entry_id", ""), "code": row.get("code", ""),
             **({"message": row["message"]} if row.get("message") else {})}
            for row in rows]


def _round_record(index, phase, before_count):
    return {
        "round": index, "phase": phase, "diagnostics_before": before_count,
        "diagnostics_after": before_count,
        "structural": {"applied": [], "rejected": [], "adopted": False, "reason": "not_proposed"},
        "ledger": {"applied": [], "rejected": [], "adopted": False, "reason": "not_proposed"},
        "notes": "", "normalize_notes": [], "error": "",
    }


def _feedback(record):
    return {"round": record.get("round"), "phase": record.get("phase"),
            "error": record.get("error", ""),
            **({"failure": copy.deepcopy(record["failure"])} if record.get("failure") else {}),
            **{part: copy.deepcopy(record.get(part, {})) for part in ("structural", "ledger")}}


def _split_proposal(document, upserts):
    existing = {entry.get("id"): entry.get("kind") for entry in document.get("entries", [])}
    structural, ledger = [], []
    for entry in upserts:
        entry_id = entry.get("id")
        kind = entry.get("kind") or (existing.get(entry_id) if isinstance(entry_id, str) else None)
        (ledger if isinstance(kind, str) and kind in {"decision", "input", "question"}
         else structural).append(entry)
    return structural, ledger


def _trial(document, upserts, scope=None):
    try:
        rejected = scope_rejections(document, upserts, scope)
        if rejected:
            return copy.deepcopy(document), [], rejected
        return apply_completion(document, {"upserts": upserts})
    except Exception:
        # A malformed model item is a rejected proposal, never an orchestration
        # failure or a reason to expose its full text in the round log.
        return copy.deepcopy(document), [], [
            {"entry_id": str(item.get("id", "")), "code": "not_whitelisted",
             "message": "补全条目格式无效"} for item in upserts]


def locked_snapshot(document) -> dict:
    """Copy all entries locked by their evidence or reviewed state."""
    return {entry["id"]: copy.deepcopy(entry) for entry in document.get("entries", [])
            if entry.get("evidence") in {"user_source", "saved_graph", "manual"}
            or entry.get("reviewed") is True}


async def autofix(document, base, *, conversation_id, revision, ask_model, max_repair_rounds=3,
                  extra_diagnostics: Sequence[dict] = (), require_improvement: bool = True,
                  locked_baseline: dict | None = None,
                  on_round: Callable[[dict, dict, int], Any] | None = None,
                  resume: dict | None = None, previous_rounds: Sequence[dict] = ()) -> dict:
    """Run one completion and bounded repairs, adopting only safe improvements."""
    current = copy.deepcopy(document)
    original_locked = copy.deepcopy(locked_baseline) if locked_baseline is not None else locked_snapshot(current)
    from .draft_dedupe import apply_dedupe
    current, host_changes, _ = apply_dedupe(current)
    for change in host_changes:
        entry_id = change["entry_id"]
        if original_locked.get(entry_id) == change["before"]:
            original_locked[entry_id] = copy.deepcopy(change["after"])
    rounds = copy.deepcopy(resume["rounds"]) if resume is not None else []
    model_calls = resume["model_calls"] if resume is not None else 0
    early_reason = ""
    repair_limit = None if max_repair_rounds is None else max(0, max_repair_rounds)
    completed_completion = any(row.get("phase") == "completion" for row in rounds)
    completed_repairs = sum(row.get("phase") == "repair" for row in rounds)
    last = rounds[-1] if rounds else {}
    if last.get("structural", {}).get("reason") == "model_error":
        early_reason = "model_error"
    elif last.get("error") == "CompletionRequestTooLarge":
        early_reason = "request_too_large"

    async def emit_round(record):
        if on_round is not None:
            result = on_round(copy.deepcopy(record), copy.deepcopy(current), model_calls)
            if inspect.isawaitable(result):
                await result

    def phases():
        if not completed_completion:
            yield "completion"
        if repair_limit is None:
            while True:
                yield "completion" if rounds and rounds[-1].get("error") == "CompletionParseError" else "repair"
        else:
            yield from ["repair"] * max(0, repair_limit - completed_repairs)
    for phase in (() if early_reason else phases()):
        before = readiness(current, base, conversation_id=conversation_id, revision=revision)
        if phase == "repair" and before["ready"]:
            break
        if (phase == "repair" and before["diagnostics"]
                and all(d["code"] == "entry_conflict" for d in before["diagnostics"])
                and not any(can_resolve_proposed_conflict(e) for e in current.get("entries", []))):
            early_reason = "conflict_requires_review"
            break
        record = _round_record(len(rounds) + 1, phase, len(before["diagnostics"]))
        record["blocking_diagnostics_after"] = sum(d.get("severity") != "warning" for d in before["diagnostics"])
        record["diagnostics"] = copy.deepcopy([*before["diagnostics"], *extra_diagnostics])
        if extra_diagnostics:
            record["extra_diagnostics"] = len(extra_diagnostics)
        rounds.append(record)
        round_start = copy.deepcopy(current)
        try:
            request = build_completion_request(current, [*before["diagnostics"], *extra_diagnostics],
                previous_feedback=[_feedback(row) for row in [*previous_rounds, *rounds[:-1]][-2:]])
        except CompletionRequestTooLarge as exc:
            record["error"] = "CompletionRequestTooLarge"
            record["failure"] = {"detail": str(exc), "phase": "preparing"}
            early_reason = "request_too_large"
            await emit_round(record)
            break
        request_scope = json.loads(request).get("scope")
        if request_scope:
            record["request_scope"] = copy.deepcopy(request_scope)
        model_calls += 1
        try:
            raw = await ask_model(COMPLETION_SYSTEM, request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            from .extract import OrchestrationGenerationLimit, OrchestrationStreamTimeout
            record["structural"]["reason"] = "model_error"
            record["error"] = type(exc).__name__
            if isinstance(exc, OrchestrationStreamTimeout):
                record["failure"] = {"detail": str(exc)[:1000], "phase": exc.phase,
                    "timeout_seconds": exc.timeout_seconds, "thinking_chars": exc.thinking_chars,
                    "output_chars": exc.output_chars}
            elif isinstance(exc, OrchestrationGenerationLimit):
                record["failure"] = {"detail": str(exc)[:1000], "phase": exc.phase}
                if exc.timeout_seconds is not None:
                    record["failure"]["timeout_seconds"] = exc.timeout_seconds
            early_reason = "model_error"
            await emit_round(record)
            break
        try:
            proposal = parse_completion_response(raw)
        except CompletionParseError:
            record["structural"]["reason"] = "parse_failed"
            record["error"] = "CompletionParseError"
            if before["ready"] and repair_limit is not None:
                early_reason = "parse_failed"
            await emit_round(record)
            if early_reason:
                break
            continue
        normalized, normalize_notes = normalize_proposal(current, proposal)
        record["proposal"] = copy.deepcopy(normalized)
        record["notes"] = normalized["notes"]
        record["normalize_notes"] = normalize_notes
        structural, ledger = _split_proposal(current, normalized["upserts"])

        baseline = before
        if structural:
            trial_document, applied, rejected = _trial(current, structural, request_scope)
            record["structural"]["applied"] = _receipts(applied)
            record["structural"]["rejected"] = _receipts(rejected)
            if not applied:
                record["structural"]["reason"] = "nothing_applied"
            else:
                trial_readiness = readiness(trial_document, base,
                                            conversation_id=conversation_id, revision=revision)
                record["structural"]["diagnostics"] = copy.deepcopy(trial_readiness["diagnostics"])
                old_atoms, new_atoms = _atoms(baseline["diagnostics"]), _atoms(trial_readiness["diagnostics"])
                if not new_atoms.issubset(old_atoms):
                    record["structural"]["reason"] = "new_errors"
                elif require_improvement and len(new_atoms) >= len(old_atoms):
                    record["structural"]["reason"] = "no_improvement"
                else:
                    current, baseline = trial_document, trial_readiness
                    record["structural"].update(
                        adopted=True, reason="improved" if len(new_atoms) < len(old_atoms)
                        else "no_regression")

        # Design explanations in a mixed proposal describe its structure. They
        # must not become active facts when that structure was refused, even if
        # a subset happened to improve the graph. Inputs/questions remain useful.
        if structural and (not record["structural"]["adopted"] or record["structural"]["rejected"]):
            kinds = {e["id"]: e["kind"] for e in current.get("entries", [])}
            decisions = [e for e in ledger if (e.get("kind") or kinds.get(e.get("id"))) == "decision"]
            ledger = [e for e in ledger if (e.get("kind") or kinds.get(e.get("id"))) != "decision"]
            record["ledger"]["rejected"] = [{"entry_id": e.get("id", ""),
                "code": "structure_not_adopted", "message": "同批结构未完整采用，相关设计说明仅保留在提案记录中"}
                for e in decisions]
            if decisions:
                record["ledger"]["reason"] = "structure_not_adopted"
        if ledger:
            trial_document, applied, rejected = _trial(current, ledger, request_scope)
            record["ledger"]["applied"] = _receipts(applied)
            record["ledger"]["rejected"].extend(_receipts(rejected))
            if not applied:
                record["ledger"]["reason"] = "nothing_applied"
            else:
                trial_readiness = readiness(trial_document, base,
                                            conversation_id=conversation_id, revision=revision)
                record["ledger"]["diagnostics"] = copy.deepcopy(trial_readiness["diagnostics"])
                if _atoms(trial_readiness["diagnostics"]).issubset(_atoms(baseline["diagnostics"])):
                    current = trial_document
                    record["ledger"].update(adopted=True, reason="adopted")
                else:
                    record["ledger"]["reason"] = "new_errors"

        current_entries = {entry["id"]: entry for entry in current.get("entries", [])}
        if any(protected_violations(original, current_entries.get(entry_id, {}))
               for entry_id, original in original_locked.items()):
            current = round_start
            record["structural"].update(adopted=False, reason="guard_failed")
            if record["ledger"]["adopted"]:
                record["ledger"].update(adopted=False, reason="new_errors")
        after = readiness(current, base, conversation_id=conversation_id, revision=revision)
        record["diagnostics_after"] = len(after["diagnostics"])
        record["blocking_diagnostics_after"] = sum(d.get("severity") != "warning" for d in after["diagnostics"])
        await emit_round(record)

    final = readiness(current, base, conversation_id=conversation_id, revision=revision)
    status = "ready" if final["ready"] else "needs_review"
    reason = early_reason or ("" if final["ready"] else "rounds_exhausted")
    blocking = [entry["id"] for entry in current.get("entries", [])
                if entry.get("state") == "active" and entry.get("kind") == "question"
                and (entry.get("data") or {}).get("blocking") is True]
    return {"document": copy.deepcopy(current), "status": status, "reason": reason,
            "model_calls": model_calls, "rounds": rounds, "host_dedupe": host_changes,
            "remaining": copy.deepcopy(final["diagnostics"]), "blocking_question_ids": blocking}
