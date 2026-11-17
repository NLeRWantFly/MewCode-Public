"""Host-owned command request identities and execution observations.

These records describe dispatch and results; they never authorize execution.
Missing observations stay unknown, and a denied launch has no process exit code.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re


def request_id(node_id, input_revision, request):
    request = _request_dict(request)
    execution = {key: request.get(key) for key in
                 ("command", "input_scope", "forbidden_changes", "verification_command")}
    for key in ("write_paths", "declared_write_paths"):
        if key in request:
            execution[key] = request.get(key)
    for key in ("command", "verification_command"):
        execution[key] = str(execution[key] or "").strip()
    from mewcode.kernel.calls import invocation_id
    return invocation_id(node_id, input_revision, execution)


def _request_dict(request):
    if isinstance(request, dict):
        return request
    dump = getattr(request, "model_dump", None)
    if callable(dump):
        value = dump()
        return value if isinstance(value, dict) else {}
    return {}


def command_effect_fingerprint(node_id, request, *, policy=None):
    """Hash the exact effect-bearing request fields, independent of input epoch."""
    request = _request_dict(request)
    write_paths = request.get("declared_write_paths", request.get("write_paths"))
    if write_paths is None and isinstance(policy, dict):
        write_paths = policy.get("declared_write_paths", policy.get("write_paths"))
    payload = {
        "node_id": str(node_id or ""),
        "command": str(request.get("command") or "").strip(),
        "verification_command": str(request.get("verification_command") or "").strip(),
        "input_scope": request.get("input_scope") or [],
        "write_paths": write_paths or [],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def request_receipts(node_id, input_revision, requests, *, status="not_dispatched", reason="",
                     epoch=0, policy=None):
    """Register host-classified command effects before any helper dispatch."""
    from mewcode.sandbox.execution_conditions import classify_command_effect

    current_epoch = epoch if type(epoch) is int and epoch >= 0 else 0
    rows = []
    for request in requests:
        request_value = request if isinstance(request, dict) else {}
        rows.append({
            "request_id": request_id(node_id, input_revision, request_value),
            "node_id": node_id,
            "input_revision": input_revision,
            "epoch": current_epoch,
            "effect_class": classify_command_effect(request, policy=policy),
            "effect_fingerprint": command_effect_fingerprint(node_id, request_value, policy=policy),
            "request": copy.deepcopy(request_value if isinstance(request_value, dict)
                                      else _request_dict(request)),
            "status": status,
            "command_started": False if status in {"not_dispatched", "not_started"} else None,
            "stdout": None,
            "stderr": None,
            "exit_code": None,
            "reason": reason,
            "evidence_source": "host_dispatch",
        })
    return rows


_DEFAULT_STARTED = object()


def execution_observation(status, *, stdout=None, stderr=None, exit_code=None,
                          reason="", output_truncated=False, command_started=_DEFAULT_STARTED):
    """Construct metadata at the execution boundary, before text truncation."""
    limit = 64000
    started = (False if status in {"denied", "spawn_failed"} else
               True if status in {"exited", "timed_out"} else None) if command_started is _DEFAULT_STARTED else command_started
    return {"status": status,
            "command_started": started if type(started) is bool else None,
            "stdout": stdout[:limit] if isinstance(stdout, str) else None,
            "stderr": stderr[:limit] if isinstance(stderr, str) else None,
            "stdout_chars": len(stdout) if isinstance(stdout, str) else None,
            "stderr_chars": len(stderr) if isinstance(stderr, str) else None,
            "exit_code": exit_code, "reason": reason,
            "output_truncated": output_truncated or any(
                isinstance(value, str) and len(value) > limit for value in (stdout, stderr))}


def compact_execution_observation(observation):
    """Keep execution attribution in persisted events without copying stdout."""
    if not isinstance(observation, dict):
        return None
    result = {}
    command = observation.get('command_execution')
    if isinstance(command, dict):
        result['command_execution'] = {key: command.get(key)
            for key in ('status', 'command_started', 'exit_code')}
    if observation.get('reused') is True:
        result['reused'] = True
        for kind in ('kernel_receipt_reuse', 'command_receipt_reuse'):
            source = observation.get(kind)
            if isinstance(source, dict):
                result[kind] = copy.deepcopy(source)
    return result or None


def _dispatch_did_not_invoke(row, observation):
    if not isinstance(observation, dict):
        return False
    command = str((row.get("request") or {}).get("command") or "").strip()
    return (observation.get("version") == 1 and observation.get("guarded") is True
            and observation.get("closed") is True and bool(row.get("dispatch_id"))
            and observation.get("dispatch_id") == row["dispatch_id"]
            and command in observation.get("allowed_commands", [])
            and command not in observation.get("attempted_commands", []))


def observe_requests(receipts, verification_records, *, dispatch_observation=None):
    observed = copy.deepcopy(receipts)
    for row in observed:
        matches = [item for item in verification_records or [] if isinstance(item, dict)
                   and item.get("name") == "Bash"
                   and str((item.get("args") or {}).get("command") or
                           item.get("command") or "").strip() == row["request"]["command"].strip()]
        if not matches:
            if (_dispatch_did_not_invoke(row, dispatch_observation)
                    or (not row.get("dispatch_id") and row.get("command_started") is False
                        and row.get("status") in {"not_dispatched", "not_started"})):
                row.update(status="not_started", command_started=False, exit_code=None,
                           evidence_source="host_execution", dispatch_closed=True,
                           reason="宿主确认辅助执行已结束，精确命令入口未被调用；命令未启动")
            else:
                row.update(status="outcome_unknown", reason="已派发，但没有该命令的宿主执行观察")
            continue
        exits = [item for item in matches if
                 ((item.get("host_observation") or {}).get("command_execution") or {}).get("status") == "exited"]
        # A later queued/denied retry must not hide an earlier possible effect.
        uncertain = [item for item in matches if item.get("status") != "interrupted_before_execution"
                     and (item.get("host_observation") or {}).get("operation_blocked") is not True
                     and ((item.get("host_observation") or {}).get("command_execution") or {}).get("command_started") is not False]
        item = (exits or uncertain or matches)[-1]
        host = (item.get("host_observation") or {}).get("command_execution")
        output = str(item.get("output") or "")
        row.update(tool_id=item.get("tool_id"), output=output,
                   output_truncated=bool(item.get("output_truncated")))
        if isinstance(host, dict):
            row.update(copy.deepcopy(host), evidence_source="host_execution")
            if host.get("status") == "exited" or (
                    host.get("command_started") is True and type(host.get("exit_code")) is int):
                row["status"] = "completed"
            else:
                row["status"] = "not_started" if host.get("command_started") is False else "outcome_unknown"
        elif (item.get("status") == "interrupted_before_execution"
              or (item.get("host_observation") or {}).get("operation_blocked") is True):
            row.update(status="not_started", command_started=False, exit_code=None,
                       evidence_source="host_execution", reason="宿主确认工具在执行前中断")
        else:
            # Legacy tool records have no structured host fields. Only the final
            # wrapper line proves an exit; arbitrary prose or handoff evidence cannot.
            match = re.search(r"(?:^|\n)EXIT CODE: (-?\d+)\s*\Z", output)
            if match and item.get("status") in {"completed", "failed"}:
                row.update(status="completed", command_started=True, exit_code=int(match[1]),
                           evidence_source="legacy_tool_result", output_truncated=True,
                           reason="旧回执仅保留工具文本；stdout/stderr 完整性未知")
            else:
                row.update(status="outcome_unknown", command_started=None,
                           evidence_source="legacy_tool_result", reason="缺少宿主启动或退出观察")
        row["successful"] = (row["status"] == "completed" and row["exit_code"] == 0
                             and not item.get("is_error"))
    return observed


def stage_command_receipts(node_id, stage):
    """Project this stage's accepted requests, including older command-only handoffs."""
    saved = [row for row in stage.get("command_receipts") or []
             if row.get("node_id") == node_id
             and row.get("input_revision") == (stage.get("input_revision") or "")]
    requests = []
    for item in (stage.get("tools") or {}).values():
        if (item.get("name") == "SubmitHandoff" and item.get("status") == "completed"
                and not item.get("is_error")):
            requests.extend((item.get("args") or {}).get("command_requests") or [])
    requests.extend((stage.get("handoff") or {}).get("command_requests") or [])
    rows = request_receipts(node_id, stage.get("input_revision") or "", requests,
                            status="outcome_unknown", reason="历史请求缺少宿主派发回执；不能据此推断已运行或测试失败",
                            epoch=stage.get("epoch", 0))
    records = list(stage.get("verification_records") or [])
    records.extend({**item, "tool_id": key} for key, item in (stage.get("tools") or {}).items()
                   if isinstance(item, dict) and item.get("name") == "Bash")
    rows = observe_requests(rows, records) if records else rows
    combined = {row["request_id"]: row for row in rows}
    combined.update({row["request_id"]: copy.deepcopy(row) for row in saved})
    return list(combined.values())[-32:]


def persist_command_receipts(value, event):
    """Merge by bound request ID so a later empty handoff cannot erase receipts."""
    node_id = event.get("node_id")
    if not node_id or "command_receipts" not in event:
        return
    stage = value.setdefault("node_states", {}).setdefault(node_id, {})
    combined = {row["request_id"]: row for row in stage.get("command_receipts") or []}
    for row in event["command_receipts"]:
        old = combined.get(row["request_id"]) or {}
        if ((old.get("status") == "completed" and row["status"] != "completed")
                or (old.get("status") in {"in_flight", "outcome_unknown"}
                    and row["status"] in {"not_dispatched", "not_started"}
                    and not _same_unstarted_execution(old, row))):
            continue  # A later refusal cannot erase an earlier execution/unknown effect.
        retained = copy.deepcopy(row)
        if old.get("recovery_attempt") and not retained.get("recovery_attempt"):
            retained["recovery_attempt"] = copy.deepcopy(old["recovery_attempt"])
        combined[row["request_id"]] = retained
    stage["command_receipts"] = list(combined.values())[-32:]


def _same_unstarted_execution(old, row):
    return (row.get("status") == "not_started" and row.get("command_started") is False
            and row.get("evidence_source") == "host_execution"
            and ((old.get("dispatch_id") and old["dispatch_id"] == row.get("dispatch_id"))
                 or (old.get("tool_id") and old["tool_id"] == row.get("tool_id"))))


def reconcile_helper_receipts(rows, helper):
    """Use only a bound host checkpoint, never an executor's completion prose."""
    helper = helper or {}
    records = [{**item, "tool_id": key} for key, item in (helper.get("tools") or {}).items()
               if isinstance(item, dict) and item.get("name") == "Bash"]
    result = []
    for row in rows:
        bound = (row.get("dispatch_id") and row["dispatch_id"] == helper.get("command_dispatch_id"))
        matching = records if bound else [item for item in records
            if row.get("tool_id") and item.get("tool_id") == row["tool_id"]]
        observation = helper.get("command_dispatch_observation")
        if bound and not observation and _legacy_closed_without_command(helper):
            observation = {"version": 1, "guarded": True, "closed": True,
                "dispatch_id": row["dispatch_id"], "allowed_commands": [row["request"]["command"].strip()],
                "attempted_commands": []}
        if (row.get("status") in {"in_flight", "outcome_unknown"}
                and (matching or _dispatch_did_not_invoke(row, observation))
                and row.get("input_revision") == helper.get("input_revision")):
            candidate = observe_requests([row], matching, dispatch_observation=observation)[0]
            if candidate["status"] == "completed" or _same_unstarted_execution(row, candidate):
                row = candidate
        result.append(copy.deepcopy(row))
    return result


def _legacy_closed_without_command(helper):
    """Narrow migration of a normally finished, command-only helper ledger.

    The old web runner stamped completed_at only after closing its runtime.
    Every possible command tool was Bash and every declared call is checkpointed.
    Missing, interrupted, errored, mixed-attempt or wider-capability ledgers do
    not prove absence of execution. Never consult the model's handoff narrative.
    """
    if (helper.get("capability_snapshot_version") != 1
            or helper.get("status") not in {"completed", "needs_attention"}
            or helper.get("request_error") != ""
            or not helper.get("started_at") or not helper.get("completed_at")
            or helper["completed_at"] < helper["started_at"]
            or not isinstance(helper.get("tools"), dict)):
        return False
    allowed = {"Bash", "ReadFile", "Glob", "Grep", "ReadStageResult", "SubmitHandoff",
               "AskUserQuestion", "human_oncall", "report_step"}
    available = set(helper.get("available_tools") or [])
    rows = list(helper["tools"].values())
    return ("Bash" in available and available <= allowed and bool(rows)
            and any(t.get("name") == "SubmitHandoff" and t.get("status") == "completed"
                    and not t.get("is_error") for t in rows)
            and all(t.get("name") in allowed - {"Bash"}
                    and t.get("status") in {"completed", "failed"}
                    and helper.get("attempt") is not None and t.get("attempt") == helper["attempt"]
                    for t in rows))


def reconcile_run_command_receipts(value):
    """Reconcile a stopped run using its own append-only dispatch events.

    Older checkpoints lack dispatch IDs and later empty handoffs may have lost
    the top-level receipts. Bind those only to a unique dispatch interval in
    the same attempt/revision and its recorded helper tool call.
    """
    states = value.get("node_states") or {}
    events = value.get("events") or []
    corrected, unresolved = 0, []
    for node_id, stage in states.items():
        node_events = [event for event in events if event.get("node_id") == node_id
                       and event.get("command_receipts")]
        ledger = {"node_states": {node_id: {"command_receipts": []}}}
        for event in node_events:
            persist_command_receipts(ledger, {"node_id": node_id, "command_receipts": [
                row for row in event["command_receipts"]
                if row.get("input_revision") == stage.get("input_revision")]})
        merged = {row["request_id"]: row for row in ledger["node_states"][node_id]["command_receipts"]}
        helper = states.get(node_id + "__command_probe") or {}
        for row in merged.values():
            if row.get("dispatch_id") or row.get("tool_id") or row.get("status") not in {"in_flight", "outcome_unknown"}:
                continue
            starts = [event for event in node_events if event.get("phase") == "capability_escalation"
                      and event.get("status") == "running" and any(
                          item.get("request_id") == row["request_id"] and item.get("status") == "in_flight"
                          for item in event["command_receipts"])]
            if len(starts) != 1:
                continue
            start = starts[0]
            ends = [event for event in node_events if event.get("phase") == "capability_escalation"
                    and event.get("status") != "running" and event.get("attempt") == start.get("attempt")
                    and event.get("seq", 0) > start.get("seq", 0)
                    and any(item.get("request_id") == row["request_id"] for item in event["command_receipts"])]
            if (not ends or not helper.get("started_at") or not helper.get("completed_at")
                    or start.get("attempt") is None or helper.get("attempt") != start["attempt"]
                    or helper.get("input_revision") != row.get("input_revision")
                    or not (start.get("server_time", float("inf")) <= helper["started_at"]
                            <= helper["completed_at"] <= ends[0].get("server_time", 0))):
                continue
            matches = [(key, item) for key, item in (helper.get("tools") or {}).items()
                       if item.get("name") == "Bash" and item.get("attempt") == start["attempt"]
                       and str((item.get("args") or {}).get("command") or "").strip()
                       == str(row["request"]["command"]).strip()]
            if len(matches) == 1:
                row["tool_id"] = matches[0][0]
                row["legacy_dispatch_seq"] = start["seq"]
        # Bind legacy event identities before merging already-reconciled stage
        # observations, so a verified same-tool correction remains idempotent.
        persist_command_receipts(ledger, {"node_id": node_id,
            "command_receipts": stage.get("command_receipts") or []})
        rows = ledger["node_states"][node_id]["command_receipts"]
        reconciled = reconcile_helper_receipts(rows, helper)
        corrected += sum(a.get("status") != b.get("status") for a, b in zip(rows, reconciled))
        # Merge through the normal receipt guard after binding the old record.
        stage["command_receipts"] = rows[-32:]
        persist_command_receipts(value, {"node_id": node_id, "command_receipts": reconciled})
        unresolved.extend({"node_id": node_id, "request_id": row["request_id"], "status": row["status"]}
                          for row in stage["command_receipts"]
                          if row.get("input_revision") == stage.get("input_revision")
                          and row.get("status") in {"in_flight", "outcome_unknown"})
    return {"corrected": corrected, "unresolved": unresolved}


def resolve_unknown_receipt(record, node_id, receipt_id, verification=None, *, work_dir=None):
    """Resolve a command with an unknown effect, without replaying blindly.

    The returned action is advisory to the normal scheduler.  Any queued retry is
    represented by a host receipt and is still dispatched through normal review.
    Model claims are never evidence: effect verification references must identify
    current-epoch host observations bound to the exact request fingerprint.
    """
    from mewcode.experts.node_ledger import ledger_for_run, project_node_view
    from mewcode.sandbox.execution_conditions import classify_command_effect

    ledger = ledger_for_run(record)
    view = ledger.view(node_id)
    matches = [f for f in view.history if f.kind == "receipt" and not f.superseded_by
               and isinstance(f.payload.get("request"), dict)
               and (f.payload.get("request_id") == receipt_id or f.fact_id == receipt_id)]
    if not matches:
        raise ValueError("No host command receipt matches this request")
    receipt_fact = matches[-1]
    row = dict(receipt_fact.payload)
    request = _request_dict(row.get("request"))
    request_ref = row.get("request_id")
    effect_class = row.get("effect_class") or classify_command_effect(request)
    receipt_epoch = row.get("epoch", receipt_fact.epoch)
    retry_count = row.get("retry_count") if type(row.get("retry_count")) is int else 0
    if not request_ref or type(receipt_epoch) is not int:
        raise ValueError("Command receipt has no valid epoch binding")
    if row.get("status") not in {"outcome_unknown", "in_flight", "effect_unknown", "effect_partial"}:
        return {"action": "already_resolved", "request_id": request_ref,
                "status": row.get("status")}

    def persist_receipt(payload):
        stage = record.setdefault("node_states", {}).setdefault(node_id, {})
        combined = {item.get("request_id"): copy.deepcopy(item)
                    for item in stage.get("command_receipts") or [] if item.get("request_id")}
        combined[request_ref] = copy.deepcopy(payload)
        stage["command_receipts"] = list(combined.values())[-32:]
        ledger.record(node_id, "receipt", payload, source="host", identity=[
            "command_resolution", request_ref, payload.get("status"), payload.get("retry_count"),
            payload.get("effect_verified")])

    def blocker(kind, reason):
        existing = next((f for f in view.active_blockers if f.payload.get("kind") == kind
                         and (f.payload.get("request_id") == request_ref
                              or f.payload.get("ref") == request_ref)), None)
        if existing is None:
            ledger.record(node_id, "blocker", {"kind": kind, "request_id": request_ref,
                "ref": request_ref, "reason": reason}, source="host",
                identity=["command_effect", request_ref, kind, view.epoch])

    def retry_exhausted(kind, reason):
        existing = next((f for f in view.active_blockers if f.payload.get("kind") == kind
                         and f.payload.get("unbound_request_id") == request_ref), None)
        if existing is None:
            ledger.record(node_id, "blocker", {"kind": kind,
                "unbound_request_id": request_ref, "reason": reason}, source="host",
                identity=["retry_exhausted", request_ref, kind, view.epoch])
        project_node_view(record, node_id)
        return {"action": "retry_exhausted", "request_id": request_ref,
                "effect_class": effect_class}

    def record_effect_proof(effect, *, refs=None, proof=None, identity):
        payload = {"request_id": request_ref, "effect": effect, "host_verified": True,
            "host_verified_effect": effect,
            "effect_fingerprint": row.get("effect_fingerprint") or
                                  command_effect_fingerprint(node_id, request)}
        if refs is not None:
            payload["effect_verification_refs"] = list(refs)
        if proof is not None:
            payload["effect_verification_proof"] = copy.deepcopy(proof)
            payload["target"] = proof.get("target")
            payload["result_sha256"] = proof.get("result_sha256")
        related = [fact for fact in view.active_blockers
            if fact.payload.get("kind") in {"unknown_effect", "effect_unknown"}
            and (fact.payload.get("request_id") == request_ref
                 or fact.payload.get("ref") == request_ref
                 or (row.get("tool_id") and fact.payload.get("ref") == row.get("tool_id")))]
        # A legacy current_stop may coexist with the request-bound blocker
        # introduced by partial verification. Bind both when this is the sole
        # unresolved command; never let one command's proof hide another write.
        unresolved = [fact for fact in view.current_receipts
            if fact.kind == "receipt" and isinstance(fact.payload.get("request"), dict)
            and fact.payload.get("status") in {"outcome_unknown", "in_flight", "effect_unknown", "effect_partial"}
            and fact.payload.get("input_revision") == row.get("input_revision")]
        if len(unresolved) == 1 and unresolved[0].payload.get("request_id") == request_ref:
            related.extend(fact for fact in view.active_blockers
                if fact.payload.get("kind") in {"unknown_effect", "effect_unknown"}
                and fact.payload.get("ref") == "current_stop"
                and (fact.payload.get("occurrence") or [None, row.get("input_revision")])[-1]
                    == row.get("input_revision") and fact not in related)
        if related:
            for blocker_fact in related:
                ledger.record(node_id, "receipt", {**payload,
                    "blocker_fact_id": blocker_fact.fact_id}, source="host",
                    identity=[identity, blocker_fact.fact_id])
        else:
            ledger.record(node_id, "receipt", payload, source="host", identity=identity)

    def reserve_retry(reason, *, readonly_retry=False):
        retries = [f.payload for f in view.history if f.epoch == view.epoch
                   and f.kind == "receipt" and f.source == "host"
                   and f.payload.get("request_id") == request_ref
                   and f.payload.get("retry_reserved") is True]
        if retries or retry_count >= 1 or row.get("retry_reserved"):
            kind = "readonly_effect_unknown" if effect_class == "readonly" else "effect_unknown"
            return retry_exhausted(kind, "The one current-epoch retry was consumed; inspect the new outcome.")
        payload = {**row, "request": copy.deepcopy(request), "status": "not_started",
            "command_started": False,
            "evidence_source": "host_readonly_retry" if readonly_retry else "host_effect_verification",
            "effect_class": effect_class, "retry_count": 1, "retry_reserved": True,
            "reason": reason, "epoch": view.epoch}
        if readonly_retry:
            payload["readonly_retry_receipt"] = True
        persist_receipt(payload)
        retry_fact = {"request_id": request_ref, "effect_class": effect_class,
            "retry_reserved": True, "retry_count": 1, "effect_retry_receipt": True}
        if readonly_retry:
            retry_fact.update(readonly_retry_receipt=True, host_verified=True, effect="not_applied")
            ledger.record(node_id, "receipt", retry_fact, source="host",
                          identity=["readonly_retry", request_ref, view.epoch])
            blocker("readonly_effect_unknown", "Read-only command may be retried once in the current epoch.")
        else:
            ledger.record(node_id, "receipt", retry_fact, source="host",
                          identity=["verified_no_effect_retry", request_ref, view.epoch])
        project_node_view(record, node_id)
        return {"action": "retry_once", "request_id": request_ref, "effect_class": effect_class}

    if verification is None:
        if effect_class == "readonly":
            if receipt_epoch != view.epoch:
                # A stale read-only request cannot be requeued under the new
                # input revision. Its host-classified effect is still harmless.
                record_effect_proof("not_applied", identity=["readonly_effect_stale", request_ref, view.epoch])
                project_node_view(record, node_id)
                return {"action": "completed", "request_id": request_ref, "replay": False,
                        "stale_request": True}
            # Host classification proves no persistent effect, even when the
            # command itself may have run. Retain the original receipt and use
            # the standard dispatcher for one idempotent observation retry.
            record_effect_proof("not_applied", identity=["readonly_effect", request_ref, view.epoch])
            return reserve_retry("The command is host-classified read-only; reserve one same-epoch retry.",
                                 readonly_retry=True)
        retry_was_used = any(f.epoch == view.epoch and f.kind == "receipt"
            and f.source == "host" and f.payload.get("request_id") == request_ref
            and f.payload.get("retry_reserved") is True for f in view.history)
        if retry_was_used:
            retry_exhausted("effect_unknown", "The retry outcome is unknown; obtain new independent evidence.")
        else:
            blocker("effect_unknown", "A command may have changed state; independently verify its current effect.")
        project_node_view(record, node_id)
        return {"action": "verify_effect", "request_id": request_ref,
                "effect_class": effect_class}

    if not isinstance(verification, dict) or verification.get("effect") not in {
            "applied", "not_applied", "partial"}:
        raise ValueError("Effect verification must report applied, not_applied, or partial")
    refs = verification.get("evidence_refs")
    if (not isinstance(refs, list) or not refs
            or any(not isinstance(ref, str) or not ref for ref in refs)
            or len(refs) != len(set(refs))):
        raise ValueError("Effect verification needs unique host evidence references")
    fingerprint = row.get("effect_fingerprint") or command_effect_fingerprint(node_id, request)
    proof = verification.get("proof")
    if work_dir is None or not isinstance(proof, dict):
        raise ValueError("Effect verification requires a host-derived proof and workspace binding")
    evidence = []
    for fact_id in refs:
        fact = next((f for f in view.history if f.fact_id == fact_id), None)
        if (fact is None or fact.epoch != view.epoch or fact.superseded_by
                or fact.source != "host" or fact.kind != "receipt"):
            raise ValueError("Effect evidence must reference current-epoch host receipts")
        payload = fact.payload
        if (payload.get("effect_verification_observation") is not True
                or payload.get("request_id") != request_ref
                or payload.get("effect_fingerprint") != fingerprint
                or not payload.get("assistance_request_id")
                or not payload.get("tool_id") or not payload.get("result_hash")
                or not isinstance(payload.get("host_observation"), dict)):
            raise ValueError("Effect evidence is not bound to this request and assistance")
        tool_name = payload.get("tool_name")
        if tool_name != "ReadFile":
            raise ValueError("Single-file effect verification requires a host ReadFile observation")
        evidence.append(fact)

    effect = verification["effect"]
    from mewcode.experts.effect_verification import verify_effect_observations
    observations = [{"name": fact.payload.get("tool_name"),
        "tool_id": fact.payload.get("tool_id"), "args": fact.payload.get("args") or {},
        "is_error": fact.payload.get("is_error"),
        "host_observation": fact.payload.get("host_observation")}
        for fact in evidence]
    expected_verification = verify_effect_observations(row, observations, work_dir)
    if (expected_verification is None or expected_verification.get("effect") != effect
            or expected_verification.get("proof") != proof
            or proof.get("host_verified_effect") != effect
            or proof.get("effect_fingerprint") != fingerprint):
        raise ValueError("Host proof does not establish the claimed effect for this command")
    receipt_payload = {**row, "effect_verified": effect == "applied",
        "verified_effect": effect, "effect_verification_refs": list(refs),
        "effect_verification_epoch": view.epoch}
    # The host observation is preserved independently of the submitted effect claim.
    record_effect_proof(effect, refs=refs, proof=proof,
        identity=["effect_verified", request_ref, view.epoch, effect, refs])
    if effect == "applied":
        receipt_payload.update(status="completed", command_started=True,
                               successful=True, evidence_source="host_effect_verification")
        persist_receipt(receipt_payload)
        project_node_view(record, node_id)
        return {"action": "completed", "request_id": request_ref, "replay": False}
    if effect == "not_applied":
        # Verification is an authoritative no-effect result; reserve one ordinary reviewed retry.
        row["retry_count"] = max(retry_count, 0)
        row.update(copy.deepcopy(receipt_payload))
        if receipt_epoch != view.epoch:
            receipt_payload.update(status="verified_not_applied", command_started=True,
                                   successful=False, evidence_source="host_effect_verification")
            persist_receipt(receipt_payload)
            project_node_view(record, node_id)
            return {"action": "completed", "request_id": request_ref, "replay": False,
                    "verification_refs": list(refs), "stale_request": True}
        result = reserve_retry("Current-state host evidence confirmed the command effect was not applied.",
                               readonly_retry=effect_class == "readonly")
        result["verification_refs"] = list(refs)
        return result
    receipt_payload.update(status="effect_partial", command_started=True,
                           successful=False, evidence_source="host_effect_verification")
    persist_receipt(receipt_payload)
    blocker("effect_unknown", "Current-state evidence shows a partial effect; continue from observed state.")
    project_node_view(record, node_id)
    return {"action": "continue_from_partial", "request_id": request_ref,
            "replay": False, "verification_refs": list(refs)}
