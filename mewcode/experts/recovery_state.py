"""Host-owned recovery facts and interaction lifecycle. No inferred approvals."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import time

# Only bounded local observations may be retried after cancellation. Unknown
# plugins, browser operations and commands never inherit safety from a label.
OBSERVATIONS = frozenset({"ReadFile", "Glob", "Grep", "ReadStageResult",
                          "ReadReviewQueue", "ReadAuditArtifact", "ReadRunArchive",
                          "ReadAuthorizations", "ReadWorkflowContext"})
# Reporting, user interaction and local decision helpers are neither durable
# artifact writes nor queue submissions, even when their tool call succeeds.
NON_PROGRESS_TOOLS = frozenset({"report_step", "ReportStep", "AskUserQuestion",
                                "HumanOnCall", "DecideNextStep", "InspectToolHistory",
                                "RequestReplan", "SubmitHandoff", "LoadSkill",
                                "ProbeCapabilities", "BackgroundTasks"})
from mewcode.experts.blocking_policy import BLOCKER_CLASS

HARD_KINDS = frozenset(kind for kind, category in BLOCKER_CLASS.items() if category != "auto")


# The host sets this only for calls the checkpoint proved never left the host
# (queued or waiting for a decision). Treating them as unknown effects deadlocks
# recovery forever: nothing can ever turn such a row into a result.
NEVER_EXECUTED = "interrupted_before_execution"


def tool_rows(stage):
    tools = stage.get("tools") or {}
    rows = list(tools.values()) if isinstance(tools, dict) else tools
    return [row for row in rows if "epoch" not in stage or row.get("epoch", 0) == stage["epoch"]]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def _node_queues(snapshot, node_id):
    """Queues this node reviews, plus the ones it submits repair batches into.

    A repair executor's own name is the *owner* of batches, while the queue
    itself belongs to the auditor that froze it. Looking only for a queue named
    after the node reports an empty marker forever and denies the executor the
    continuations its real submissions earned.
    """
    queues = snapshot.get("review_queues") or {}
    rows = [(key, queue) for key, queue in queues.items() if key == node_id]
    rows.extend((key, queue) for key, queue in queues.items() if key != node_id
                and any(batch.get("owner") == node_id
                        for batch in (queue.get("repair_batches") or {}).values()))
    return rows


def _effecting_receipts(stage):
    """File versions, not arbitrary successful tool calls or new call IDs."""
    receipts = set()
    for key, tool in (stage.get("tools") or {}).items():
        if (tool.get("status") != "completed" or tool.get("is_error")
                or tool.get("name") not in {"WriteFile", "EditFile"}):
            continue
        args = tool.get("args") or {}
        path = args.get("file_path") or args.get("path")
        if path:
            receipts.add(digest([os.path.normcase(os.path.normpath(str(path))),
                tool.get("effect_sha256") or args]))
        else:
            # Old checkpoints can prove a write succeeded but lack its bytes.
            receipts.add(f"{tool.get('tool_id') or key}:{tool.get('name')}")
    return receipts


def _content_progress(stage):
    """Host-retained source bytes and delivered content, never summary claims."""
    sources, outputs = set(), set()
    for tool in tool_rows(stage):
        if tool.get("status") != "completed" or tool.get("is_error"):
            continue
        args = tool.get("args") or {}
        fingerprint = tool.get("result_sha256")
        if tool.get('name') == 'ReadStageResult':
            cursor = (tool.get('host_observation') or {}).get('read_cursor') or {}
            node = cursor.get('node_id')
            revision = cursor.get('source_revision')
            start, end, total = (cursor.get(key) for key in ('offset', 'next_offset', 'total_chars'))
            if (isinstance(node, str) and node and not node.startswith('@')
                    and node == args.get('node_id') and isinstance(revision, str) and len(revision) == 64
                    and all(char in '0123456789abcdef' for char in revision)
                    and all(type(value) is int for value in (start, end, total)) and 0 <= start < end <= total):
                sources.add(digest(['stage', node, revision, start, end]))
        if (tool.get("name") in {"ReadFile", "ReadAuditArtifact"} and isinstance(fingerprint, str)
                and len(fingerprint) == 64 and all(char in "0123456789abcdef" for char in fingerprint)):
            path = args.get("file_path") or args.get("path") or args.get("artifact_ref")
            if path:
                cursor = {key: args[key] for key in ("offset", "limit", "page", "pages", "start_page", "end_page") if key in args}
                sources.add(digest([os.path.normcase(os.path.normpath(str(path))), fingerprint, cursor]))
        elif tool.get("name") == "ReadAuditArtifact":
            # Legacy host tool rows retain the actual page/segment output but
            # predate result_sha256. Hash those observed bytes, not model prose.
            output = tool.get("output")
            if isinstance(output, str) and output:
                sources.add(digest([args, output]))
        if tool.get("name") == "SubmitHandoff":
            for item in args.get("inline_outputs") or []:
                if isinstance(item, dict) and item.get("name") and item.get("content"):
                    outputs.add(digest(item["content"]))
    for item in (stage.get("handoff") or {}).get("inline_outputs") or []:
        if isinstance(item, dict) and item.get("name") and item.get("content"):
            outputs.add(digest(item["content"]))
    return sorted(sources), sorted(outputs)


def progress_marker(snapshot, node_id):
    """Only durable domain progress, never more chat/read calls or billing."""
    ledger = snapshot.get("node_ledgers")
    if isinstance(ledger, dict) and node_id in ledger:
        from mewcode.experts.node_ledger import NodeLedger
        progress = NodeLedger(ledger).view(node_id).last_progress
        if progress is not None and isinstance(progress.payload.get("marker"), dict):
            return dict(progress.payload["marker"])
    return observed_progress_marker(snapshot, node_id)


def observed_progress_marker(snapshot, node_id):
    """Build the host observation that is ingested into the ledger."""
    rows = _node_queues(snapshot, node_id)
    records = [record for _, queue in rows for record in (queue.get("records") or {}).values()]
    stage = ((snapshot.get("node_states") or {}).get(node_id)
             or (snapshot.get("members") or {}).get(node_id) or {})
    sources, outputs = _content_progress(stage)
    return {"schema_version": 2, "observed_sources": sources, "inline_outputs": outputs,
            "review_complete": sum(record.get("state") in {'complete', 'pass'} for record in records),
            "review_recorded": len(records),
            "write_receipts": sorted(_effecting_receipts(stage)),
            "submitted_items": sorted({item_id for _, queue in rows
                for b in queue.get("repair_batches", {}).values() if b.get("owner") == node_id and b.get("status") == "submitted"
                for item_id in b.get("item_ids", [])}),
            "submitted_revisions": sorted({f"{item_id}:{candidate['candidate_revision']}"
                for _, queue in rows
                for b in queue.get("repair_batches", {}).values()
                if b.get("owner") == node_id and b.get("status") == "submitted"
                for item_id, candidate in b.get("candidates", {}).items()
                if candidate.get("candidate_revision")})}


def advanced(before, after):
    return isinstance(before, dict) and (
        after["review_complete"] > before.get("review_complete", 0)
        or after["review_recorded"] > before.get("review_recorded", 0)
        or bool(set(after.get("write_receipts", [])) - set(before.get("write_receipts", [])))
        or bool(set(after.get("observed_sources", [])) - set(before.get("observed_sources", [])))
        or bool(set(after.get("inline_outputs", [])) - set(before.get("inline_outputs", [])))
        or bool(set(after["submitted_items"]) - set(before.get("submitted_items", [])))
        or ("submitted_revisions" in before and bool(set(after.get("submitted_revisions", []))
            - set(before["submitted_revisions"]))))


def work_progress(snapshot, node_id):
    stage = (snapshot.get("node_states") or {}).get(node_id) or {}
    from mewcode.experts.loop_progress import artifact_versions
    versions = artifact_versions(snapshot, node_id)
    return {"before": stage.get("slice_progress_baseline"), "after": progress_marker(snapshot, node_id),
            "blockers": blockers(stage),
            **({'artifact_versions': versions} if versions is not None else {})}


def advance_work_cursor(progress, previous=None):
    """A regressed queue must not buy retries by oscillating old evidence."""
    before, after = previous or progress.get("before"), progress.get("after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False, previous
    cursor = {key: max(before.get(key, 0), after.get(key, 0))
              for key in ("review_complete", "review_recorded")}
    cursor["schema_version"] = 2
    cursor["submitted_items"] = sorted(set(before.get("submitted_items", []))
                                       | set(after.get("submitted_items", [])))
    cursor["write_receipts"] = sorted(set(before.get("write_receipts", []))
                                      | set(after.get("write_receipts", [])))
    for key in ("observed_sources", "inline_outputs"):
        if key in before or key in after:
            cursor[key] = sorted(set(before.get(key, [])) | set(after.get(key, [])))
    if "submitted_revisions" in before or "submitted_revisions" in after:
        cursor["submitted_revisions"] = sorted(set(before.get("submitted_revisions", []))
                                               | set(after.get("submitted_revisions", [])))
    return advanced(before, cursor), cursor


def binding(stage):
    # Successful observations/receipts, not narrative text or attempt counters,
    # prove new evidence. Reopening the same checkpoint is not progress.
    view = stage.get("ledger_view")
    if isinstance(view, dict):
        receipts = [fact.get("payload") for fact in view.get("current_receipts", [])]
        progress = view.get("last_progress") or {}
        return {"epoch": view["epoch"], "input_revision": stage.get("input_revision", ""),
                "receipts_digest": digest([receipts, progress.get("payload"), sorted(
                    (str(t.get("tool_id") or ""), t.get("name", ""), digest(t.get("output", "")))
                    for t in tool_rows(stage) if t.get("status") == "completed" and not t.get("is_error"))])}
    receipts = sorted((str(t.get("tool_id") or ""), t.get("name", ""), digest(t.get("output", "")))
                      for t in tool_rows(stage) if t.get("status") == "completed" and not t.get("is_error"))
    return {"attempt": stage.get("attempt"), "node_attempt": stage.get("node_attempt"),
            "input_revision": stage.get("input_revision", ""), "receipts_digest": digest(receipts)}


def blockers(stage, *, error_kind=""):
    from mewcode.experts.blocking_policy import clearance_for_blocker

    rows = []
    def add(kind, ref):
        rows.append({"kind": kind, "ref": ref,
                     "clearance": clearance_for_blocker(kind),
                     "clearance_satisfied": bool(stage.get("clearance_receipts", {}).get(ref))})

    view = stage.get("ledger_view")
    if isinstance(view, dict):
        rows.extend({**fact["payload"], "fact_id": fact["fact_id"],
                     "clearance": clearance_for_blocker(fact["payload"]["kind"]),
                     "clearance_satisfied": bool(fact["payload"].get("clearance_receipt"))}
                    for fact in view.get("active_blockers", []))
        if error_kind in BLOCKER_CLASS and not any(row.get("kind") == error_kind for row in rows):
            add(error_kind, "current_stop")
        return rows

    for tool in tool_rows(stage):
        status, name = tool.get("status"), tool.get("name", "")
        if status == "waiting_permission":
            add("permission", tool.get("tool_id", name))
        elif status in {"running", "outcome_unknown"} and name not in OBSERVATIONS:
            observation = tool.get("host_observation") or {}
            if (observation.get("operation_blocked") is True
                    or (observation.get("command_execution") or {}).get("command_started") is False):
                continue  # Pre-dispatch review cannot create an unknown effect.
            from mewcode.experts.recovery_context import READONLY_TOOLS
            if status == 'outcome_unknown' and name in READONLY_TOOLS:
                continue  # Read failure is retryable; vision cost stays in its quota journal.
            add("unknown_effect", tool.get("tool_id", name))
    if stage.get("pending_permission"):
        add("permission", str(stage["pending_permission"].get("tool_id", "pending_permission")))
    if (stage.get("safety_review_state") or {}).get("pending_call_ref"):
        add("safety_review", stage["safety_review_state"]["pending_call_ref"])
    if "safety_review_state" not in stage:
        # Old checkpoints lack explicit review lifecycle state. A later read
        # cannot resolve an inconclusive write/command review.
        unresolved = {}
        for tool in tool_rows(stage):
            key = digest([tool.get("name"), tool.get("args")])
            if tool.get("status") == "completed" and not tool.get("is_error"):
                unresolved.pop(key, None)
            elif str(tool.get("output", "")).startswith(("AUTO_REVIEW_INCONCLUSIVE", "AUTO_REVIEW_UNAVAILABLE", "REVIEW_EVIDENCE_UNAVAILABLE")):
                unresolved[key] = tool.get("tool_id", tool.get("name", ""))
        for ref in unresolved.values():
            add("legacy_safety_review", ref)
    if error_kind in BLOCKER_CLASS:
        add(error_kind, "current_stop")
    pending = stage.get("pending_interaction") or {}
    if pending and pending.get("tool") != "SchedulerRecovery":
        add("human_question", pending.get("request_id", ""))
    return rows


def reconcile(stage, *, error_kind=""):
    """Called only after the prior execution was cancelled/finished.

    Retiring a derived scheduling prompt is not answering it. Permissions,
    business questions, budget counters and low-confidence model decisions stay.
    """
    for tool in tool_rows(stage):
        if tool.get("status") in {"running", "outcome_unknown"} and tool.get("name") == "RequestReplan":
            # RequestReplan is a durable host-state write, not an observation.
            # Its idempotency key is derived from the exact submitted request.
            # Reconcile only when the saved request proves what happened; a
            # different request is an actual conflict and remains blocked.
            args = tool.get("args") if isinstance(tool.get("args"), dict) else {}
            proposed = {"reason": args.get("reason"), "evidence_refs": args.get("evidence_refs"),
                        "input_revision": stage.get("input_revision", ""),
                        "node_attempt": stage.get("node_attempt")}
            saved = stage.get("adaptive_request")
            saved = saved if isinstance(saved, dict) else {}
            if saved.get("request_id") == digest(proposed) and all(
                    saved.get(key) == value for key, value in proposed.items()):
                tool.update(status="completed", is_error=False,
                            recovery_reason="matching_host_replan_receipt")
            elif not saved and args.get("reason") and isinstance(args.get("evidence_refs"), list):
                tool.update(status="not_executed",
                            recovery_reason="host_replan_request_absent")
        elif tool.get("status") in {"running", "outcome_unknown"} and tool.get("name") in OBSERVATIONS:
            tool.update(status="interrupted_read_only", recovery_reason="cancelled_observation_no_result")
        elif tool.get("status") == NEVER_EXECUTED:
            # The host already proved this call never left it (queued or awaiting
            # a decision). Leaving the row as-is makes every future resume report
            # an unknown effect that no result can ever resolve; normalize it so
            # the next run may simply issue the call again under normal review.
            tool.update(status="not_executed", recovery_reason="interrupted_before_execution_no_effect")
    pending = stage.get("pending_interaction") or {}
    if pending.get("tool") != "SchedulerRecovery" or blockers(stage, error_kind=error_kind):
        return False
    state = stage.get("scheduler_recovery") or {}
    decisions = state.get("decisions") or []
    # A scheduler question belongs to one decision, or (for old checkpoints)
    # one loop round. A paid human decision from an earlier round must not
    # strand a later host-generated question forever. Unknown legacy scopes
    # remain conservative.
    scoped = decisions
    decision_id = pending.get("scheduler_decision_id")
    if decision_id:
        matched_at = next((i for i, d in enumerate(decisions)
                           if d.get("decision_id") == decision_id), None)
        if matched_at is not None:
            # A newer paid decision is also a real gate, even if the old
            # prompt was not replaced correctly.
            scoped = decisions[matched_at:]
    elif pending.get("binding") and decisions:
        facts = decisions[-1].get("host_facts") or {}
        if facts.get("loop_id") and facts.get("round") is not None:
            scoped = [d for d in decisions if (d.get("host_facts") or {}).get("loop_id") == facts["loop_id"]
                      and (d.get("host_facts") or {}).get("round") == facts["round"]]
            if not scoped:
                scoped = decisions
    # An actual uncertain/interrupted *current* model decision still needs
    # its own resolution; progress must not reset its paid decision budget.
    if state.get("phase") == "deciding" or any(
            d.get("action") == "human" and (d.get("review_choice") or d.get("decision_tokens")) for d in scoped):
        return False
    old = pending.get("binding")
    now = binding(stage)
    if old:
        changed = old.get("receipts_digest") != now["receipts_digest"]
    else:
        # Conservative legacy migration: host-generated (zero model tokens)
        # prompts plus actual successful work, and no present hard boundary.
        changed = bool(decisions) and all(not d.get("decision_tokens") for d in decisions if d.get("action") == "human")
        changed = changed and any(t.get("status") == "completed" and not t.get("is_error") for t in tool_rows(stage))
    if not changed:
        return False
    if answered_recovery_prompt(stage) is not None:
        # Retiring a derived prompt is revalidation; retiring one the user
        # answered is overriding a human decision. Keep it until the evidence
        # actually moves, so the same question is not asked again unchanged.
        return False
    stage.setdefault("interaction_history", []).append({"pending": copy.deepcopy(pending),
        "status": "superseded", "answered": False, "reason": "host_facts_revalidated",
        "binding": now, "at": time.time()})
    stage.pop("pending_interaction", None)
    for decision in scoped:
        if (decision.get("action") == "human" and not decision.get("decision_tokens")
                and decision.get("execution_status") == "等待人工"):
            decision.update(execution_status="旧调度提示已失效，宿主重新核验；未代答用户",
                            superseded_request_id=pending.get("request_id"))
    state.update(phase="revalidate", revalidation_reason="host_facts_revalidated")
    stage["scheduler_recovery"] = state
    return True


def answered_recovery_prompt(stage):
    """The scheduler prompt the user already answered, if it is still unresolved.

    Host revalidation retires *derived* prompts, but a prompt the user actually
    answered is a decision, not a derivation. Dropping it silently asks the same
    question again with no new evidence, which is what turned a single blocked
    stage into an endless pause/continue loop.
    """
    pending = stage.get("pending_interaction") or {}
    request_id = pending.get("request_id")
    if pending.get("tool") != "SchedulerRecovery" or not request_id:
        return None
    for row in reversed(stage.get("interaction_history") or []):
        if (row.get("pending") or {}).get("request_id") == request_id:
            return row if row.get("answered") else None
    return None


def reconcile_saved(store, run_id, stage_key, *, bucket="node_states", error_kind="", retire_gates=False):
    result = {}
    def apply(snapshot):
        stage = snapshot.setdefault(bucket, {}).setdefault(stage_key, {})
        result["superseded"] = reconcile(stage, error_kind=error_kind)
        if result["superseded"] and "node_ledgers" in snapshot:
            from mewcode.experts.node_ledger import ledger_for_run
            ledger = ledger_for_run(snapshot)
            for question in ledger.view(stage_key).history:
                if question.kind == "question" and question.payload.get("tool") == "SchedulerRecovery" and not question.superseded_by:
                    fact = ledger.record(stage_key, "maintenance", {"reason": "host_facts_revalidated",
                        "question_fact_id": question.fact_id})
                    ledger.supersede(question.fact_id, fact.fact_id)
        result["stage"] = copy.deepcopy(stage)
    store.mutate(run_id, apply, durable=True)
    return result


def save_interaction(snapshot, bucket, stage_key, pending):
    stage = snapshot.setdefault(bucket, {}).setdefault(stage_key, {})
    existing = stage.get("pending_interaction")
    if existing:
        if existing.get("request_id") != pending["request_id"]:
            raise ValueError("Cannot overwrite an unresolved interaction")
        if any(existing.get(k) != pending.get(k) for k in ("tool", "questions", "kind")):
            raise ValueError("Cannot change an existing interaction contract")
        return copy.deepcopy(existing)
    saved = {**copy.deepcopy(pending), "schema_version": 1, "status": "pending", "created_at": time.time(),
             "origin": "scheduler" if pending.get("tool") == "SchedulerRecovery" else "human",
             "binding": binding(stage)}
    if pending.get("tool") == "SchedulerRecovery":
        decisions = (stage.get("scheduler_recovery") or {}).get("decisions") or []
        if decisions and decisions[-1].get("decision_id"):
            saved["scheduler_decision_id"] = decisions[-1]["decision_id"]
    stage["pending_interaction"] = saved
    return copy.deepcopy(saved)


def finish_interaction(snapshot, bucket, stage_key, request_id):
    stage = snapshot.setdefault(bucket, {}).setdefault(stage_key, {})
    pending = stage.get("pending_interaction")
    if not pending or pending.get("request_id") != request_id:
        raise ValueError("Stale interaction response; current question was not consumed")
    stage.setdefault("interaction_history", []).append({"pending": copy.deepcopy(pending),
        "status": "answered", "answered": True, "at": time.time()})
    stage.pop("pending_interaction")
