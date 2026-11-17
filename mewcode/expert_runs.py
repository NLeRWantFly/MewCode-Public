from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import queue
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from mewcode.runtime_paths import expert_runs_root
from mewcode.orchestration.write_lease import WriteLeaseError
from mewcode.experts.outcome import acceptance_recheck_nodes, acceptance_retry_required, effective_run_status, project_acceptance_status, run_activation_time, unresolved_audits
from mewcode.expert_checkpoints import ExpertCheckpointArchive, STOPPED, RESTORE_FIELDS
from mewcode.experts.flow import DEFAULT_EXPERT_FLOW, ExpertFlowSpec
from mewcode.conversation import (
    Message, ThinkingBlock, ToolResultBlock, ToolUseBlock,
)


SNAPSHOT_VERSION = 2
SUMMARY_PROJECTION_VERSION = 9
RUN_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
TERMINAL_STATUSES = {"completed", "needs_attention", "terminated"}
RESUMABLE_FLOW_PHASES = frozenset({
    "done",
    "finalizer_skipped",
    "batch_halted",
    "hitl_blocked",
    "recovery_revision_pending",
})


def _saved_plan_fingerprint(flow: Any) -> str:
    from mewcode.orchestration.revisions import execution_fingerprint
    return execution_fingerprint(flow)


def _unexecuted_output_cap_rejection(result: Any) -> bool:
    """Recognize the fixed request-schema failure, not an unknown-effect retry.

    Only explicit Continue uses this predicate. Partial execution, unknown
    usage, other provider failures and business/acceptance blocks stay closed.
    """
    if not isinstance(result, dict):
        return False
    handoff = result.get("handoff") or {}
    if not isinstance(handoff, dict):
        return False
    text = str(result.get("text") or "")
    return (
        result.get("status") in {"failed", "needs_attention"}
        and result.get("error_kind") == "provider_error"
        and "API error (400):" in text
        and "Unsupported parameter: max_output_tokens" in text
        and all(result.get(key) == 0 for key in (
            "input_tokens", "output_tokens", "billable_tokens", "iterations_used",
        ))
        and not result.get("activity_records")
        and not result.get("verification_records")
        and not result.get("loop_decision")
        and not result.get("verdict")
        and not any(handoff.values())
    )


def _write_locked_rubric(snapshot: dict[str, Any], node_id: str, criteria: list[Any]) -> None:
    flow = snapshot.get("locked_flow")
    if not isinstance(flow, dict):
        return
    meta = dict(flow.get("meta") or {})
    rubrics = dict(meta.get("audit_rubrics") or {})
    rubrics[str(node_id)] = copy.deepcopy(criteria)
    meta["audit_rubrics"] = rubrics
    flow["meta"] = meta
    snapshot["locked_flow"] = flow
    snapshot["flow_fingerprint"] = expert_flow_fingerprint(flow)
    snapshot["recovery_locked"] = True


def apply_recovery_decision(
    snapshot: dict[str, Any],
    *,
    action: str,
    info: str = "",
    grant_extra_budget: bool = False,
) -> None:
    from mewcode.experts.audit_protocol import bump_rubric, drop_incompatible_assessment
    from mewcode.experts.recovery_classify import CHANGE_METHOD

    proposal = snapshot.get("pending_recovery_revision")
    if action == "reject":
        snapshot["recovery_decision"] = "reject"
        snapshot.pop("pending_recovery_revision", None)
        for protocol in (snapshot.get("audit_protocols") or {}).values():
            if isinstance(protocol, dict):
                protocol["recovery_decision"] = "reject"
                protocol.pop("pending_recovery_revision", None)
        return
    if not isinstance(proposal, dict):
        raise ValueError("没有待批准的计划修订")
    if action == "supply_info":
        text = str(info or "").strip()
        if not text:
            raise ValueError("补充信息不能为空")
        snapshot["recovery_decision"] = "supply_info"
        snapshot.pop("pending_recovery_revision", None)
        snapshot.setdefault("supplemental_requirements", []).append({
            "attempt": int(snapshot.get("attempt") or 1) + 1,
            "text": text,
            "created_at": utc_now(),
        })
        for protocol in (snapshot.get("audit_protocols") or {}).values():
            if not isinstance(protocol, dict):
                continue
            protocol["recovery_decision"] = "supply_info"
            protocol["no_progress"] = 0
            protocol.pop("pending_recovery_revision", None)
        return
    if action != "approve":
        raise ValueError("未知的计划修订决定")
    change_kind = str(proposal.get("change_kind") or "")
    patch = proposal.get("rubric_patch")
    affected = [str(item) for item in (proposal.get("affected_nodes") or []) if item]
    states = snapshot.get("node_states") or snapshot.get("stages") or {}
    for node_id in affected:
        stage = states.get(node_id)
        if isinstance(stage, dict) and stage.get("status") == "completed":
            stage["status"] = "needs_attention"
            stage["passed"] = False
    for node_id, protocol in list((snapshot.get("audit_protocols") or {}).items()):
        if not isinstance(protocol, dict):
            continue
        if affected and node_id not in affected:
            continue
        if patch:
            protocol.setdefault("rubric_history", []).append(copy.deepcopy(protocol.get("rubric") or {}))
            new_rubric = bump_rubric(
                protocol.get("rubric") or {}, patch,
                change_kind=change_kind or CHANGE_METHOD,
                approved=True,
            )
            drop_incompatible_assessment(protocol)
            protocol["rubric"] = new_rubric
            _write_locked_rubric(snapshot, node_id, new_rubric["criteria"])
        protocol["recovery_decision"] = "approve"
        protocol["no_progress"] = 0
        protocol.pop("pending_recovery_revision", None)
        protocol.pop("pending_review", None)
        if grant_extra_budget:
            protocol.pop("repair_budget", None)
            protocol["round"] = 0
    snapshot["recovery_decision"] = "approve"
    snapshot["authorized_revision"] = {
        "affected_nodes": affected,
        "change_kind": change_kind,
        "execution_scope": proposal.get("execution_scope") or "",
        "files": list(proposal.get("files") or []),
    }
    snapshot.pop("pending_recovery_revision", None)
    snapshot["recovery_locked"] = True


def successors_after_node(flow: Any, node_id: str) -> list[str]:
    """Return node ids reachable from ``node_id`` (excluding the anchor)."""
    anchor = str(node_id or "").strip()
    if not anchor:
        return []
    if hasattr(flow, "as_dict"):
        payload = flow.as_dict()
    elif isinstance(flow, dict):
        payload = flow
    else:
        return []
    edges = payload.get("edges") or []
    adjacency: dict[str, list[str]] = {}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        src = str(edge.get("from_node") or edge.get("from") or "").strip()
        dst = str(edge.get("to_node") or edge.get("to") or "").strip()
        if src and dst:
            adjacency.setdefault(src, []).append(dst)
    seen: set[str] = set()
    stack = list(adjacency.get(anchor, []))
    ordered: list[str] = []
    while stack:
        current = stack.pop()
        if current == anchor or current in seen:
            continue
        seen.add(current)
        ordered.append(current)
        stack.extend(adjacency.get(current, []))
    return ordered


def is_resumable_snapshot(snapshot: dict[str, Any] | None) -> bool:
    """Whether an expert-run checkpoint can be continued without a full replay."""
    if not isinstance(snapshot, dict) or snapshot.get("legacy"):
        return False
    status = effective_run_status(snapshot)
    if status in {"paused", "blocked"}:
        return True
    if status != "needs_attention":
        return False
    phase = str((snapshot.get("flow_state") or {}).get("phase") or "")
    return phase in RESUMABLE_FLOW_PHASES
VOLATILE_EVENT_TYPES = {
    "expert_delta", "expert_thinking", "expert_child_event",
    "expert_node_delta", "expert_node_thinking",
    "expert_member_delta", "expert_member_thinking",
    "expert_pipeline_step_thinking",
}
MAX_PERSISTED_EVENTS = 2_500
EVENT_LOG_VERSION = 1
EVENT_SNAPSHOT_EVERY_BATCHES = 128
LARGE_FIELD_BLOB_VERSION = 1
REVIEW_QUEUE_BLOB_VERSION = 1
LARGE_FIELD_MIN_CHARS = 16_384
MAX_EVENT_TEXT_CHARS = 4_000
MAX_PUBLIC_TOOLS_PER_ROLE = 50
MAX_PUBLIC_TOOL_OUTPUT_CHARS = 1_500
PUBLIC_TOOL_EVENT_TYPES = {
    "expert_tool", "expert_tool_start", "expert_tool_result",
    "expert_tool_wait", "expert_tool_state",
}
PUBLIC_AGGREGATE_EVENT_TYPES = VOLATILE_EVENT_TYPES | {
    "expert_stage",
    "expert_tool", "expert_tool_start", "expert_tool_wait",
    "expert_tool_result", "expert_tool_state", "expert_role_wait",
    "expert_usage", "expert_child",
    "expert_node_delta", "expert_node_thinking", "expert_node_tool",
    "expert_member_delta", "expert_member_thinking", "expert_member_tool",
}
PUBLIC_SUMMARY_EVENT_TYPES = {
    "recovery_case",
    "expert_capability_executor_changed",
    "expert_stage", "expert_step", "expert_run_state", "expert_run_done",
    "expert_children_stopped",
    "expert_lessons_proposed", "expert_lessons_unavailable",
    "expert_flow_defined", "expert_node_started", "expert_node_completed",
    "expert_member_started", "expert_member_completed",
    "expert_join_started", "expert_join_completed", "expert_handoff",
    "expert_flow_state",
    "plan_revision",
    "expert_pipeline_started", "expert_pipeline_completed",
    "expert_pipeline_step_started", "expert_pipeline_step_completed",
    "expert_pipeline_step_output", "expert_pipeline_step_tool",
    "expert_pipeline_step_usage", "expert_pipeline_warning",
}


PRIVATE_STAGE_FIELDS = {
    'workflow_context', 'workflow_skill', 'review_protocol',
    "ledger_view",
    "messages", "partial_reasoning", "partial_text", "display_reasoning",
    "tools", "text", "topology", "knowledge_observations", "knowledge_cards",
    "tool_promotion_proposals", "scope_decision_notice_pending",
    "scope_decision_last_notified_id",
}
PRIVATE_TOP_LEVEL_FIELDS = {
    'workflow_core', 'workflow_legacy',
    'workbench_revision_drafts',
    "collaboration",
    "collaboration_import",
    'kernel_attempt_archives',
    "node_ledgers",
    "capability_executor_changes",
    "kernel_calls", "kernel_progress", "kernel_investigations",
    "kernel_review_verdicts",
    "kernel_arbitrations",
    "difficult_node_model_changes", "difficult_node_model_policy",
    "review_queues", "review_queue_refs", "review_queue_archives",
    "model_route_recoveries",
    "artifact_reconciliation",
    "process_files",
    "process_file_preview_refs",
    "interventions",
    "events", "stages", "node_states", "children", "members",
    "pipeline_runs",
    "locked_profile", "handoff", "task",
    "attachments", "tools", "test_records", "attempts",
    "supplemental_requirements", "preserved_worktrees",
    "cancelled_child_task_ids", "resumed_children",
    "loop_restarts",
    "audit_restarts",
    "acceptance_rechecks",
    "loop_budget_changes",
    "pause_dialogue_anchor",
    "pending_dialogue_handoff",
    "dialogue_handoff_reviews",
    "handoff_review_grant",
    "handoff_review_grants",
    "retired_paused_handoff_reviews",
}


AUDIT_RETRY_RETAIN_KEYS = (
    "transaction_dir", "pending_repair", "pending_review",
    "baseline_result", "repair_result",
    "repair_step", "repair_progress", "delivered_evidence", "changed_files",
    "delivery_files", "scratch_files", "last_decision", "accepted", "score",
    "image_probes", "progress_signature",
    "blocker", "recovery_proposal_count", "rubric_history", "command_attempts",
)

LEGACY_NODE_IDS = {
    "manager": "manager.intake",
    "planner": "planner",
    "executor": "executor",
    "auditor": "auditor",
}


def expert_flow_fingerprint(flow: ExpertFlowSpec | dict[str, Any]) -> str:
    """Stable identity for the exact executable graph locked by a run."""
    parsed = ExpertFlowSpec.from_value(flow)
    encoded = json.dumps(
        parsed.as_dict(), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()




def _validate_collaboration_write(snapshot: dict[str, Any]) -> None:
    """Refuse a durable collaboration record the kernel protocol cannot accept.

    The store is the last boundary before persistence, so an unknown format or
    capability must fail here rather than be silently dropped or migrated.
    ``state`` owns the allow-list; a run that never initialized collaboration
    stays writable.
    """
    if not isinstance(snapshot, dict) or snapshot.get("collaboration") is None:
        return
    from mewcode.kernel.collaboration import state
    state(snapshot)


def _normalize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Project old role-keyed snapshots into the v2 node-keyed shape."""
    if snapshot.get('workflow_core'):
        from mewcode.workflow_core.store import synchronize
        synchronize(snapshot)
        snapshot['stages'] = snapshot.setdefault('node_states', {})
        return snapshot
    states = snapshot.get("node_states") or {}
    raw_flow = snapshot.get("locked_flow") or DEFAULT_EXPERT_FLOW.as_dict()
    try:
        flow = ExpertFlowSpec.from_value(raw_flow)
    except ValueError:
        flow = DEFAULT_EXPERT_FLOW
    flow_nodes = flow.node_map
    for node_id, state in states.items():
        node = flow_nodes.get(node_id)
        if node is not None and isinstance(state, dict):
            state.setdefault("node_id", node_id)
            state.setdefault("role", node.role)
            state.setdefault("label", node.label)

    _close_stopped_stage_clocks(snapshot, states)
    from mewcode.experts.branch_state import reconcile_unresolved_branch_skips
    reconcile_unresolved_branch_skips({**snapshot, 'node_states': states, 'locked_flow': flow.as_dict()})
    snapshot.update({
        "version": SNAPSHOT_VERSION,
        "locked_flow": flow.as_dict(),
        "flow_id": str(snapshot.get("flow_id") or flow.flow_id),
        "flow_version": int(snapshot.get("flow_version") or flow.version),
        "finalizer_node_id": str(
            snapshot.get("finalizer_node_id") or flow.finalizer_node_id
        ),
        "flow_fingerprint": str(
            snapshot.get("flow_fingerprint")
            or expert_flow_fingerprint(flow)
        ),
        "node_states": states,
        "stages": states,
    })
    cursor = str(snapshot.get("stage_cursor") or "")
    snapshot["stage_cursor"] = cursor or flow.entry_node_ids()[0]
    snapshot.setdefault("node_attempts", {})
    snapshot.setdefault("members", {})
    snapshot.setdefault("pipeline_runs", {})
    snapshot.setdefault("node_expansions", {})
    snapshot.setdefault("handoffs", [])
    snapshot.setdefault("active_node_ids", [
        node_id for node_id, state in states.items()
        if isinstance(state, dict) and state.get("status") == "running"
    ])
    return snapshot


def _close_stopped_stage_clocks(snapshot: dict[str, Any], states: dict) -> None:
    """Repair missing stop metadata using recorded times, never the read time."""
    if snapshot.get("status") not in STOPPED:
        return

    def timestamp(value: Any) -> float:
        try:
            return float(value) if isinstance(value, (int, float)) else datetime.fromisoformat(str(value)).timestamp()
        except (ValueError, TypeError, OverflowError):
            return 0.0

    attempts = {int(item.get("attempt") or 0): item for item in snapshot.get("attempts") or []}
    for node_id, stage in states.items():
        if not isinstance(stage, dict):
            continue
        status = stage.get("status")
        if status not in {"running", "paused", "terminated", "review_pending"}:
            continue
        if status == "running" or (snapshot["status"] == "terminated" and status == "paused"):
            stage["status"] = "needs_attention" if snapshot["status"] == "completed" else snapshot["status"]
        start = timestamp(stage.get("started_at"))
        if not start or timestamp(stage.get("completed_at")):
            continue
        attempt = int(stage.get("attempt") or snapshot.get("attempt") or 0)
        ending = next((event.get("completed_at") for event in reversed(snapshot.get("events") or [])
                       if event.get("type") in {"expert_stage", "expert_node_completed"}
                       and event.get("node_id", event.get("role")) == node_id
                       and int(event.get("attempt") or attempt) == attempt
                       and int(event.get("node_attempt") or 0) == int(stage.get("node_attempt") or 0)
                       and timestamp(event.get("completed_at")) >= start), None)
        end = timestamp(ending or (attempts.get(attempt) or {}).get("ended_at"))
        if not end and attempt == int(snapshot.get("attempt") or 0):
            end = timestamp(snapshot.get("completed_at") or snapshot.get("updated_at"))
        if end >= start:
            stage["completed_at"] = end
            stage["duration_ms"] = round((end - start) * 1000)
    snapshot["active_node_ids"] = []


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def strategy_for_multi_agent(enabled: bool) -> str:
    return "fast" if enabled else "stable"


def strategy_source(strategy: str) -> str:
    return (
        "高成本快速 · 来自多 Agent 已开启"
        if strategy == "fast"
        else "稳定成本 · 来自多 Agent 未开启"
    )


def compact_expert_event(event: dict[str, Any]) -> dict[str, Any]:
    """Bound display-only event data before it reaches disk or the renderer."""
    payload = copy.deepcopy(event)
    if isinstance(payload.get("loop_state"), dict):
        payload["loop_state"] = compact_loop_state(payload["loop_state"])
    event_type = str(payload.get("type") or "")
    if event_type in {
        "expert_child_event", "expert_tool_result",
        "expert_pipeline_step_output", "expert_pipeline_step_tool",
        "expert_pipeline_warning",
    }:
        for key in ("output", "text"):
            value = payload.get(key)
            if isinstance(value, str) and len(value) > MAX_EVENT_TEXT_CHARS:
                payload[key] = value[:MAX_EVENT_TEXT_CHARS]
                payload[f"{key}_truncated"] = True
    return payload


def compact_loop_state(state: dict[str, Any]) -> dict[str, Any]:
    """The UI needs node status, not repeated copies of every loop transcript."""
    result = {key: copy.deepcopy(value) for key, value in state.items() if key not in {
        "round_results", "item_results", "carry_results", "recovery_history", "request_rejection_history",
        "contract_snapshot", "contract_rebindings"}}
    result["round_results"] = {node_id: {"status": value.get("status")}
                               for node_id, value in (state.get("round_results") or {}).items()}
    return result


def _compact_snapshot_events(snapshot: dict[str, Any]) -> None:
    events = [
        compact_expert_event(event)
        for event in snapshot.get("events") or []
        if isinstance(event, dict)
        and event.get("type") not in VOLATILE_EVENT_TYPES
    ]
    snapshot["events"] = events[-MAX_PERSISTED_EVENTS:]


def _tool_event_snapshot(previous, events, last_seq, updated_at):
    """Copy only paths changed by a tool receipt; never alter the old tree."""
    snapshot = dict(previous, events=[*(previous.get('events') or []), *events][-MAX_PERSISTED_EVENTS:],
                    last_seq=last_seq, updated_at=updated_at)
    copied = set()
    for event in events:
        if event.get('type') not in {'expert_tool', 'expert_tool_start', 'expert_tool_result'}:
            continue
        node_id, tool_id = str(event.get('node_id') or ''), str(event.get('id') or '')
        if not node_id or not tool_id:
            continue
        member = str(event.get('member_id') or '')
        bucket_name = 'members' if member else 'node_states'
        key = f'{node_id}::{member}' if member else node_id
        if bucket_name not in copied:
            snapshot[bucket_name] = dict(previous.get(bucket_name) or {})
            copied.add(bucket_name)
            if bucket_name == 'node_states' and (previous.get('stages') is previous.get('node_states')
                    or previous.get('stages') == previous.get('node_states')):
                snapshot['stages'] = snapshot[bucket_name]
        if (bucket_name, key) not in copied:
            stage = dict(snapshot[bucket_name].get(key) or {})
            stage['tools'] = dict(stage.get('tools') or {})
            snapshot[bucket_name][key] = stage
            copied.add((bucket_name, key))
        items = snapshot[bucket_name][key]['tools']
        if (bucket_name, key, tool_id) not in copied:
            if tool_id in items:
                items[tool_id] = copy.deepcopy(items[tool_id])
            copied.add((bucket_name, key, tool_id))
        _project_tool_lifecycle_event(snapshot, event)
    return snapshot


def _project_tool_lifecycle_event(snapshot: dict[str, Any], event: dict[str, Any]) -> None:
    """Keep crash-recoverable tool state in step with the durable event journal.

    A stream checkpoint can lag a side-effecting tool by several seconds.  The
    journaled start must therefore be enough to block an unsafe replay even
    when the process disappears before the next full snapshot is written.
    """
    kind = event.get("type")
    if kind not in {"expert_tool", "expert_tool_start", "expert_tool_result"}:
        return
    node_id = str(event.get("node_id") or "")
    tool_id = str(event.get("id") or "")
    if not node_id or not tool_id:
        return
    member_id = str(event.get("member_id") or "")
    bucket = snapshot.setdefault("members" if member_id else "node_states", {})
    key = f"{node_id}::{member_id}" if member_id else node_id
    stage = bucket.setdefault(key, {})
    tools = stage.setdefault("tools", {})
    item = tools.setdefault(tool_id, {"tool_id": tool_id})
    item.setdefault("epoch", stage.get("epoch", 0))
    item.setdefault("name", event.get("name") or "")
    if kind == "expert_tool":
        item.setdefault("args", copy.deepcopy(event.get("args") or {}))
        if item.get('name') == 'report_step':
            item.setdefault('input_revision', stage.get('input_revision'))
        item.setdefault("declared_at", event.get("declared_at"))
        item.setdefault("attempt", event.get("attempt"))
        item.setdefault("node_attempt", event.get("node_attempt"))
        item.setdefault("status", "queued")
    elif kind == "expert_tool_start":
        if item.get("status") not in {"completed", "failed"}:
            item.update(status="running", started_at=event.get("started_at"))
    else:
        item.update(status="failed" if event.get("is_error") else "completed",
                    completed_at=event.get("completed_at"),
                    output=event.get("output", ""),
                    is_error=bool(event.get("is_error")),
                    execution_seconds=event.get("elapsed"))
        if event.get("dispatch_seconds") is not None:
            item["dispatch_seconds"] = event["dispatch_seconds"]
        if isinstance(event.get("host_observation"), dict):
            item["host_observation"] = copy.deepcopy(event["host_observation"])
        if event.get("output_truncated"):
            item["result_output_truncated"] = True
        if item.get('name') == 'report_step':
            from mewcode.experts.work_reporting import apply_work_report
            apply_work_report(stage, item, event)


def _append_aggregate_display_events(
    snapshot: dict[str, Any],
    *,
    source: dict[str, Any] | None = None,
    include_details: bool = True,
    role_filter: str = "",
) -> None:
    """Rebuild a small history view from resumable aggregate checkpoint state."""
    aggregate = source or snapshot
    run_id = str(snapshot.get("run_id") or "")
    events = [
        event for event in snapshot.get("events") or []
        if event.get("type") not in PUBLIC_AGGREGATE_EVENT_TYPES
        and (
            not role_filter
            or not event.get("role")
            or event.get("role") == role_filter or event.get("node_id") == role_filter
        )
    ]
    snapshot["events"] = events
    flow_nodes = {
        str(item.get("node_id") or ""): item
        for item in (aggregate.get("locked_flow") or {}).get("nodes") or []
        if isinstance(item, dict)
    }
    for node_id, stage in sorted((aggregate.get("node_states") or aggregate.get("stages") or {}).items()):
        if not isinstance(stage, dict):
            continue
        node = flow_nodes.get(str(node_id)) or {}
        role = str(stage.get("role") or node.get("role") or node_id)
        if role_filter and role_filter not in {str(node_id), role}:
            continue
        identity = {"attempt": int(stage.get("attempt") or 0),
                    "node_attempt": int(stage.get("node_attempt") or (aggregate.get("node_attempts") or {}).get(node_id) or 0),
                    "member_id": str(stage.get("member_id") or "")}
        stage_status = str(stage.get("status") or "pending")
        if (
            snapshot.get("status") in TERMINAL_STATUSES
            and stage_status == "running"
        ):
            stage_status = "terminated"
        events.append({
            "type": "expert_stage", "run_id": run_id, "role": role,
            "node_id": node_id,
            **{
                key: copy.deepcopy(value)
                for key, value in stage.items()
                if key not in PRIVATE_STAGE_FIELDS
            },
            "status": stage_status, "snapshot_aggregate": True, **identity,
        })
        text = str(stage.get("text") or stage.get("partial_text") or "")
        reasoning = str(stage.get("display_reasoning") or "")
        if text:
            events.append({
                "type": "expert_delta", "run_id": run_id, "role": role,
                "node_id": node_id,
                "text": text, "snapshot_aggregate": True, **identity,
            })
        if reasoning:
            events.append({
                "type": "expert_thinking", "run_id": run_id, "role": role,
                "node_id": node_id,
                "text": reasoning, "snapshot_aggregate": True, **identity,
            })
        if include_details:
            tools = [
                tool for tool in (stage.get("tools") or {}).values()
                if isinstance(tool, dict)
            ][-MAX_PUBLIC_TOOLS_PER_ROLE:]
            for tool in tools:
                status = str(tool.get("status") or "")
                event_type = (
                    "expert_tool_wait" if status == "waiting_permission"
                    else "expert_tool_result" if status in {"completed", "failed"}
                    else "expert_tool_state"
                )
                events.append({
                    "type": event_type, "run_id": run_id, "role": role,
                    "node_id": node_id,
                    "id": tool.get("tool_id"), "name": tool.get("name"),
                    "args": copy.deepcopy(tool.get("args") or {}),
                    "status": status,
                    "output": str(tool.get("output") or "")[:MAX_PUBLIC_TOOL_OUTPUT_CHARS],
                    "is_error": bool(tool.get("is_error")),
                    "declared_at": tool.get("declared_at"),
                    "started_at": tool.get("started_at"),
                    "completed_at": tool.get("completed_at"),
                    "elapsed": tool.get("execution_seconds"),
                    "dispatch_seconds": tool.get("dispatch_seconds"),
                    "wait_started_at": tool.get("wait_started_at"),
                    "wait_completed_at": tool.get("wait_completed_at"),
                    "snapshot_aggregate": True, **identity,
                    "attempt": int(tool.get("attempt") or identity["attempt"]),
                    "node_attempt": int(tool.get("node_attempt") or identity["node_attempt"]),
                })
        usage = stage.get("usage")
        if isinstance(usage, dict) and usage:
            events.append({
                "type": "expert_usage", "run_id": run_id, "role": role,
                "node_id": node_id,
                **copy.deepcopy(usage), "snapshot_aggregate": True, **identity,
            })
    if not include_details or (role_filter and role_filter != "executor"):
        return
    for child in (aggregate.get("children") or {}).values():
        if not isinstance(child, dict):
            continue
        events.append({
            "type": "expert_child", "run_id": run_id,
            "role": str(child.get("role") or "executor"),
            "node_id": str(child.get("node_id") or "executor"),
            **{
                key: copy.deepcopy(value)
                for key, value in child.items()
                if key not in {"messages", "result"}
            },
            "snapshot_aggregate": True,
        })


def _append_event_tool_details(public: dict[str, Any], source: dict[str, Any], role_filter: str) -> None:
    """Recover bounded details when a failed audit replaced its aggregate stage.

    Summaries intentionally omit tools. Detailed reads can still recover the
    original events even when node_states[node].tools is empty after failure.
    This is a display projection only; never replay a tool or change a run.
    """
    represented = {str(event.get("id") or event.get("tool_id") or "")
                   for event in public["events"] if event.get("type") in PUBLIC_TOOL_EVENT_TYPES}
    records: dict[str, dict[str, Any]] = {}
    for event in source.get("events") or []:
        if not isinstance(event, dict) or event.get("type") not in PUBLIC_TOOL_EVENT_TYPES:
            continue
        if role_filter and event.get("role") != role_filter and event.get("node_id") != role_filter:
            continue
        tool_id = str(event.get("id") or event.get("tool_id") or "")
        if not tool_id or tool_id in represented:
            continue
        records[tool_id] = {**records.get(tool_id, {}), **event}
    counts: dict[str, int] = {}
    for event in public["events"]:
        if event.get("type") in PUBLIC_TOOL_EVENT_TYPES:
            node_id = str(event.get("node_id") or event.get("role") or "")
            counts[node_id] = counts.get(node_id, 0) + 1
    recovered = []
    for event in reversed(list(records.values())):
        node_id = str(event.get("node_id") or event.get("role") or "")
        if counts.get(node_id, 0) >= MAX_PUBLIC_TOOLS_PER_ROLE:
            continue
        counts[node_id] = counts.get(node_id, 0) + 1
        item = copy.deepcopy(event)
        item["output"] = str(item.get("output") or "")[:MAX_PUBLIC_TOOL_OUTPUT_CHARS]
        item["snapshot_aggregate"] = True
        if item["type"] in {"expert_tool", "expert_tool_start"} and public.get("status") != "running":
            item.update(type="expert_tool_state", status="outcome_unknown")
        recovered.append(item)
    public["events"].extend(reversed(recovered))


def is_explicit_new_run_message(text: str) -> bool:
    """Recognize a new-task prefix so it cannot be mistaken for a resume.

    This text alone does not authorize execution of any saved workflow.
    """
    return bool(re.match(
        r"^(?:请\s*)?(?:新建任务|开始新任务|另起任务|重新开始任务|从头开始任务)\s*[:：,，。！!\s]",
        str(text or "").strip() + " ",
    ))


def parse_resume_message(text: str) -> tuple[bool, str]:
    value = str(text or "").strip()
    if is_explicit_new_run_message(value):
        return False, ""
    # Questions/negations about controls are discussion, not permission to run.
    if re.search(r"[?？]|怎么|如何|为什么|能否|是否|按钮|不要(?:继续|恢复|重试)|不(?:继续|恢复|重试)", value):
        return False, ""
    if re.match(r"^(?:请\s*)?(?:从|接着从).{0,40}(?:失败|受阻|检查点|\d+(?:\.\d+)?).{0,20}(?:阶段|节点|位置)?(?:开始|继续|重试|恢复)", value):
        return True, value
    if re.search(r"(?:恢复|接着|重试)(?:之前|原来|上次|当前|失败|受阻|的|这条|这个|任务|记录|检查点|阶段|节点)", value):
        return True, value
    chinese = re.match(r"^(继续|接着|恢复)(?:\s*[:：,，.!！。-]?\s*)(.*)$", value, re.DOTALL)
    if chinese:
        return True, str(chinese.group(2) or "").strip()
    english = re.match(
        r"^(resume|continue)(?:\s+|\s*[:：,，.!！。-]\s*|$)(.*)$",
        value, re.IGNORECASE | re.DOTALL,
    )
    if english:
        return True, str(english.group(2) or "").strip()
    # Natural requests often put the continuation intent at the end, e.g.
    # “先修复这些问题，然后进入下一阶段，继续任务”.  Treat only a
    # delimiter-bounded suffix as control intent so ordinary prose containing
    # “继续” is not accidentally resumed.
    chinese_suffix = re.search(
        r"(?:^|[\s，,。；;！!])"
        r"(?:请)?(?:继续(?:任务|执行)?|接着(?:执行|任务)?|恢复(?:任务|执行)?)"
        r"[\s。！!]*$",
        value,
    )
    if chinese_suffix:
        supplemental = value[:chinese_suffix.start()].rstrip(" \t，,。；;！!")
        return True, supplemental
    return False, ""


def interval_union_seconds(intervals: list[dict[str, Any]], *, now: float | None = None) -> float:
    """Return the union of possibly-overlapping epoch-second intervals."""
    current = datetime.now(timezone.utc).timestamp() if now is None else now
    ranges: list[tuple[float, float]] = []
    for interval in intervals:
        start = interval.get("start", interval.get("started_at"))
        end = interval.get("end", interval.get("ended_at"))
        try:
            first = (
                datetime.fromisoformat(start).timestamp()
                if isinstance(start, str) else float(start)
            )
            last = (
                datetime.fromisoformat(end).timestamp()
                if isinstance(end, str) else float(end if end is not None else current)
            )
        except (TypeError, ValueError):
            continue
        if last >= first:
            ranges.append((first, last))
    ranges.sort()
    total = 0.0
    merged_start: float | None = None
    merged_end = 0.0
    for first, last in ranges:
        if merged_start is None:
            merged_start, merged_end = first, last
        elif first <= merged_end:
            merged_end = max(merged_end, last)
        else:
            total += merged_end - merged_start
            merged_start, merged_end = first, last
    if merged_start is not None:
        total += merged_end - merged_start
    return total


_DISPLAY_SENSITIVE_KEY = re.compile(
    r"(?:^|[_-])(api[_-]?key|key|token|password|passwd|secret|cookie|authorization)(?:$|[_-])",
    re.IGNORECASE,
)
_PUBLIC_TOKEN_NUMBERS = frozenset({
    "tokens", "token_budget", "input_tokens", "output_tokens", "billable_tokens",
    "total_tokens", "burst_tokens", "token_bucket_wait_seconds",
    "token_bucket_rate_per_minute", "forecast_remaining_tokens",
})
_PUBLIC_TOKEN_OBJECTS = frozenset({"token_window", "token_bucket", "token_window_receipts"})


def _safe_public_token_metric(key: str, item: Any) -> bool:
    name = key.casefold()
    return ((name in _PUBLIC_TOKEN_NUMBERS and type(item) in {int, float})
            or (name in _PUBLIC_TOKEN_OBJECTS and isinstance(item, (dict, list))))


def redact_for_display(value: Any) -> Any:
    """Recursively redact credentials while preserving private snapshots."""
    if isinstance(value, dict):
        return {
            str(key): (
                "[已脱敏]" if _DISPLAY_SENSITIVE_KEY.search(str(key)) and not _safe_public_token_metric(str(key), item)
                else redact_for_display(item)
            )
            for key, item in value.items()
            if str(key).casefold() not in {"signature", "thinking_signature"}
        }
    if isinstance(value, list):
        return [redact_for_display(item) for item in value]
    return value


def aggregate_snapshot_usage(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Aggregate each role/child exactly once from its latest cumulative counters."""
    sources: list[dict[str, Any]] = []
    for stage in (snapshot.get("node_states") or snapshot.get("stages") or {}).values():
        if isinstance(stage, dict) and isinstance(stage.get("usage"), dict):
            sources.append(stage["usage"])
    for member in (snapshot.get("members") or {}).values():
        if isinstance(member, dict) and isinstance(member.get("usage"), dict):
            sources.append(member["usage"])
    for child in (snapshot.get("children") or {}).values():
        if isinstance(child, dict) and isinstance(child.get("usage"), dict):
            sources.append(child["usage"])

    def total(*names: str) -> int:
        return sum(
            max(int(next((source.get(name) for name in names if source.get(name) is not None), 0) or 0), 0)
            for source in sources
        )

    input_tokens = total("input_tokens")
    output_tokens = total("output_tokens")
    cache_read = total("cumulative_cache_hit_tokens", "cache_read_tokens")
    cache_write = total("cumulative_cache_creation_tokens", "cache_creation_tokens")
    cache_miss = total("cumulative_cache_miss_tokens", "input_tokens")
    reported = sum(
        1 for source in sources
        if bool(source.get("cumulative_cache_usage_available", source.get("cache_usage_available", False)))
    )
    denominator = cache_read + cache_miss
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_write,
        "cache_miss_tokens": cache_miss,
        "cache_hit_rate": (cache_read / denominator) if denominator else 0.0,
        "cache_usage_available": bool(sources) and reported == len(sources),
        "cache_usage_partial": 0 < reported < len(sources),
        "source_count": len(sources),
    }


def _host_observation_snapshot(value: Any) -> dict[str, Any] | list[Any] | None:
    """Copy JSON-shaped host observations for durable message snapshots."""
    if not isinstance(value, (dict, list)):
        return None

    def is_json(item: Any) -> bool:
        if isinstance(item, dict):
            return all(isinstance(key, str) and is_json(nested)
                       for key, nested in item.items())
        if isinstance(item, list):
            return all(is_json(nested) for nested in item)
        return item is None or type(item) in (str, bool, int, float)

    try:
        if not is_json(value):
            return None
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, RecursionError):
        return None


def serialize_messages(messages: list[Message]) -> list[dict[str, Any]]:
    def serialize_tool_result(item: ToolResultBlock) -> dict[str, Any]:
        value = {
            "id": item.tool_use_id, "content": item.content,
            "is_error": item.is_error,
            "content_blocks": copy.deepcopy(item.content_blocks),
        }
        observation = _host_observation_snapshot(item.host_observation)
        if observation is not None:
            value["host_observation"] = observation
        return value

    def serialize_message(message: Message) -> dict[str, Any]:
        value = {
            "role": message.role,
            "content": message.content,
            "content_blocks": copy.deepcopy(message.content_blocks),
            "tool_uses": [
                {
                    "id": item.tool_use_id, "name": item.tool_name,
                    "arguments": copy.deepcopy(item.arguments),
                }
                for item in message.tool_uses
            ],
            "tool_results": [serialize_tool_result(item) for item in message.tool_results],
            "thinking": [
                {
                    "text": item.thinking, "signature": item.signature,
                    "kind": item.kind,
                }
                for item in message.thinking_blocks
            ],
        }
        observation = _host_observation_snapshot(message.host_observation)
        if observation is not None:
            value["host_observation"] = observation
        return value

    return [serialize_message(message) for message in messages if not message.transient]


def deserialize_messages(items: list[dict[str, Any]]) -> list[Message]:
    messages: list[Message] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        messages.append(Message(
            role=str(item.get("role") or "user"),
            content=str(item.get("content") or ""),
            content_blocks=list(item.get("content_blocks") or []),
            host_observation=_host_observation_snapshot(item.get("host_observation")),
            tool_uses=[
                ToolUseBlock(
                    tool_use_id=str(value.get("id") or ""),
                    tool_name=str(value.get("name") or ""),
                    arguments=dict(value.get("arguments") or {}),
                )
                for value in item.get("tool_uses") or [] if isinstance(value, dict)
            ],
            tool_results=[
                ToolResultBlock(
                    tool_use_id=str(value.get("id") or ""),
                    content=str(value.get("content") or ""),
                    is_error=bool(value.get("is_error")),
                    content_blocks=list(value.get("content_blocks") or []),
                    host_observation=_host_observation_snapshot(value.get("host_observation")),
                )
                for value in item.get("tool_results") or [] if isinstance(value, dict)
            ],
            thinking_blocks=[
                ThinkingBlock(
                    thinking=str(value.get("text") or ""),
                    signature=str(value.get("signature") or ""),
                    kind=str(value.get("kind") or "auto"),
                )
                for value in item.get("thinking") or [] if isinstance(value, dict)
            ],
        ))
    return messages


log = logging.getLogger(__name__)


class ExpertRunStore:
    """Versioned, atomically-written checkpoints for resumable expert runs."""

    def __init__(self, root: str | os.PathLike[str] | None = None, *, storage: str = 'legacy') -> None:
        self.root = Path(root) if root else expert_runs_root()
        if storage not in {'legacy', 'sqlite'}:
            raise ValueError('unsupported run storage')
        self.storage = storage
        self._commit_notices = {}
        self._read_model_cache = {}
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._live: dict[str, dict[str, Any]] = {}
        self._write_generation: dict[str, int] = {}
        self._written_generation: dict[str, int] = {}
        self._write_error_generation: dict[str, int] = {}
        self._event_only_batches: dict[str, int] = {}
        self._write_queue: queue.Queue[tuple[str, int] | None] = queue.Queue()
        self._writer = threading.Thread(
            target=self._writer_loop, name="expert-run-writer", daemon=True,
        )
        self._writer.start()
        self.checkpoint_archive = ExpertCheckpointArchive(self.root / "turn-checkpoints")
        self._public_summary_cache: dict[
            str, tuple[int, int, str, dict[str, Any]]
        ] = {}

    def _snapshot_paths(self) -> list[Path]:
        identities = {path.stem for path in self.root.glob("*.json")
                      if RUN_ID_PATTERN.fullmatch(path.stem)}
        identities.update(path.parent.name for path in (self.root / "kernel-events").glob("*/header.json")
                          if RUN_ID_PATTERN.fullmatch(path.parent.name))
        return [self._path(identity) for identity in sorted(identities)]

    def _source_stat(self, path: Path) -> os.stat_result:
        from mewcode.kernel.events import EventJournal
        journal = EventJournal(self.root, path.stem)
        if journal.sqlite:
            from types import SimpleNamespace
            revision, _ = journal._database().revision()
            return SimpleNamespace(st_mtime_ns=revision, st_size=revision)
        events = sorted((self.root / "kernel-events" / path.stem).glob("[0-9]*.json"))
        return events[-1].stat() if events else path.stat()

    def _commit(self, snapshot, *, kind='state_changed', previous=None, tool_events=None,
                stream_checkpoint=False, loop_checkpoint=False, call_checkpoint=False,
                history_events=None, cache_owned=None):
        from mewcode.kernel.events import EventJournal
        from mewcode.kernel.run_views import build, update_tools, update_stream, update_loop, update_calls
        journal = EventJournal(self.root, str(snapshot['run_id']), storage=self.storage)
        if journal.sqlite or (not journal.exists and self.storage == 'sqlite'):
            projected = dict(snapshot, kernel_revision=snapshot.get('kernel_revision', 0) + 1)
            cached = self._read_model_cache.get(snapshot['run_id'])
            old_models = cached[1] if cached and cached[0] == snapshot.get('kernel_revision', 0) else None
            models = (update_tools(projected, previous, old_models, tool_events)
                      if previous is not None and old_models is not None and tool_events is not None else None)
            if stream_checkpoint and previous is not None and old_models is not None:
                models = update_stream(projected, previous, old_models)
            if loop_checkpoint and previous is not None and old_models is not None:
                models = update_loop(projected, previous, old_models)
            if call_checkpoint and previous is not None and old_models is not None:
                models = update_calls(projected, previous, old_models)
            if models is None:
                if previous is not None:
                    models = build(projected, history_events=(
                        tool_events if tool_events is not None else history_events) or [])
                else:
                    models = build(projected)
            commit_snapshot = snapshot
            owns_cache = previous is not None if cache_owned is None else cache_owned
            if previous is not None and cache_owned is False:
                # The journal drops borrowed incremental state from its cache.
                # General callbacks own their live tree, so give the journal a
                # detached tree instead of forcing a full SQL reload next time.
                commit_snapshot = copy.deepcopy(snapshot)
                owns_cache = True
            result = journal.commit(commit_snapshot, kind=kind, models=models,
                previous=previous, previous_models=old_models,
                cache_owned=owns_cache)
            if commit_snapshot is not snapshot:
                snapshot['kernel_revision'] = result
            if result == projected['kernel_revision']:
                self._read_model_cache[snapshot['run_id']] = (result, models)
            while len(self._read_model_cache) > 8:
                self._read_model_cache.pop(next(iter(self._read_model_cache)))
            self._commit_notices[snapshot['run_id']] = dict(task_id=models['workbench']['task']['id'],
                version='run:' + str(result), revision=result)
            return result
        result = journal.commit(snapshot, kind=kind)
        # Legacy projection failures cannot undo a committed authoritative fact.
        try:
            journal._database().publish_models(snapshot, build(snapshot))
        except (OSError, ValueError):
            log.exception('Run read model needs rebuilding: %s', snapshot['run_id'])
        return result

    def read_public(self, run_id, *, role=''):
        """Query-only adapter. Never fall back to loading execution state."""
        from mewcode.kernel.sqlite_state import RunDatabase
        if not RUN_ID_PATTERN.fullmatch(run_id):
            return None
        try:
            result = RunDatabase(self.root, run_id).read('public')
        except (OSError, LookupError):
            return None
        if role:
            result['node_states'] = {key: value for key, value in result.get('node_states', {}).items()
                                     if key == role or value.get('role') == role}
            result['stages'] = copy.deepcopy(result['node_states'])
            result['events'] = [event for event in result.get('events', []) if not event.get('role')
                                or event.get('role') == role or event.get('node_id') == role]
        return result

    def read_owner(self, run_id):
        """Check a history request's scope without loading the whole display."""
        from mewcode.kernel.sqlite_state import RunDatabase
        if not RUN_ID_PATTERN.fullmatch(run_id):
            return None
        try:
            return RunDatabase(self.root, run_id).read('public', ('conversation_id',))
        except (OSError, LookupError):
            return None

    def graph_routing(self, run_id):
        from mewcode.kernel.sqlite_state import RunDatabase
        try:
            return RunDatabase(self.root, run_id).read('aux', ('graph_routing',))
        except (OSError, LookupError):
            return {}

    def read_workbench(self, run_id):
        from mewcode.kernel.sqlite_state import RunDatabase
        if not RUN_ID_PATTERN.fullmatch(run_id):
            return None
        database = RunDatabase(self.root, run_id)
        try:
            # One read transaction keeps the card and its decision bindings together.
            with database.connection() as db:
                db.execute('BEGIN')
                revision = db.execute('SELECT revision FROM run_views WHERE run_id=?', (run_id,)).fetchone()
                cached = self._read_model_cache.get(run_id)
                if cached and revision and cached[0] == revision[0]:
                    # Committed projections are replaced, never edited in place.
                    # Keep readers off the execution lock and detach their result.
                    return copy.deepcopy(dict(cached[1]['aux'], _workbench_view=cached[1]['workbench']))
                result = database._read(db, 'aux')
                result['_workbench_view'] = database._read(db, 'workbench')
                return result
        except (LookupError, OSError):
            return None

    def read_design(self, run_id):
        """Read the committed task card without taking the execution lock."""
        from mewcode.kernel.sqlite_state import RunDatabase
        from mewcode.kernel.run_views import DESIGN_VERSION
        if not RUN_ID_PATTERN.fullmatch(run_id):
            return None
        try:
            database = RunDatabase(self.root, run_id)
            with database.connection() as db:
                db.execute('BEGIN')
                value = database._read(db, 'design')
        except (LookupError, OSError):
            return None
        return value if value.get('version') == DESIGN_VERSION else None

    def query_summaries(self, conversation_id, *, limit=50):
        from mewcode.kernel.sqlite_state import RunDatabase
        try:
            return RunDatabase(self.root, '').summaries(conversation_id, limit=limit)
        except OSError:
            return []

    def query_run_ids(self, conversation_id, *, limit=50):
        from mewcode.kernel.sqlite_state import RunDatabase
        try:
            return [row['run_id'] for row in RunDatabase(self.root, '').summaries(
                conversation_id, limit=limit, ids_only=True)]
        except OSError:
            return []

    def history_page(self, run_id, *, after=-1, limit=50, **filters):
        from mewcode.kernel.sqlite_state import RunDatabase
        return RunDatabase(self.root, run_id).page(after=after, limit=limit, **filters)

    def _writer_loop(self) -> None:
        while True:
            item = self._write_queue.get()
            if item is None:
                return
            run_id, generation = item
            with self._lock:
                if generation != self._write_generation.get(run_id):
                    continue
                snapshot = copy.deepcopy(self._live.get(run_id))
            if snapshot is None:
                continue
            succeeded = False
            try:
                self._persist(snapshot)
                succeeded = True
            except Exception:
                log.exception("failed to persist expert run %s", run_id)
            with self._cv:
                if succeeded:
                    written = self._written_generation.get(run_id, 0)
                    if generation >= written:
                        self._written_generation[run_id] = generation
                else:
                    self._write_error_generation[run_id] = generation
                self._cv.notify_all()

    def _enqueue_write(self, run_id: str, *, durable: bool) -> None:
        from mewcode.kernel.events import EventJournal
        journal = EventJournal(self.root, run_id)
        if journal.sqlite or (not journal.exists and self.storage == 'sqlite'):
            return  # The state and read models commit in one FULL SQLite transaction.
        generation = self._write_generation.get(run_id, 0) + 1
        self._write_generation[run_id] = generation
        self._write_queue.put((run_id, generation))
        if durable:
            if not self._cv.wait_for(
                lambda: (self._written_generation.get(run_id, 0) >= generation
                         or self._write_error_generation.get(run_id, 0) >= generation),
                timeout=60,
            ):
                raise TimeoutError(f"timed out waiting for expert run snapshot {run_id}")
            if self._written_generation.get(run_id, 0) < generation:
                raise OSError(f"expert run snapshot {run_id} was not persisted")

    def _cached(self, run_id: str) -> dict[str, Any] | None:
        live = self._live.get(run_id)
        if live is not None:
            return live
        loaded = self._read_disk(run_id)
        if loaded is not None:
            self._live[run_id] = loaded
        return loaded

    def _read_disk(self, run_id: str) -> dict[str, Any] | None:
        try:
            from mewcode.kernel.events import EventJournal
            journal = EventJournal(self.root, run_id)
            if journal.exists:
                value = journal.load()
                if value and value.get('stages') == value.get('node_states'):
                    value['stages'] = value['node_states']
                return value
            path = self._path(run_id)
            if not path.exists():
                legacy_path = self.root.parent / "expert-runs" / f"{run_id}.json"
                if legacy_path.exists():
                    legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
                    if isinstance(legacy, dict):
                        return legacy
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return None
            value = self._hydrate_large_fields(run_id, value)
            value = self._hydrate_review_queues(run_id, value)
            return value
        except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
            return None

    def save_turn_checkpoint(self, run_id: str, *, turn_key: str = "", kind: str = "turn_end", node_id: str = "") -> dict[str, Any]:
        with self._lock:
            snapshot = self._cached(run_id)
            if snapshot is None:
                raise KeyError(run_id)
            meta = self.checkpoint_archive.save(snapshot, turn_key=turn_key or f"expert:{snapshot.get('attempt', 1)}", kind=kind, node_id=node_id)
            if snapshot.get("last_checkpoint") != meta:
                snapshot = copy.deepcopy(snapshot)
                snapshot["last_checkpoint"] = meta
                self._write(snapshot)
            return copy.deepcopy(meta)

    def _startup_fields(self, run_id, *fields, presence=()):
        """Read only the authoritative fields needed to decide startup work."""
        from mewcode.kernel.events import EventJournal
        journal = EventJournal(self.root, run_id)
        if not journal.sqlite:
            return None
        database = journal._database()
        with database.connection() as db:
            db.execute('BEGIN')
            result = {}
            for key in fields:
                try:
                    result[key] = database._read(db, 'state', key if isinstance(key, tuple) else (key,))
                except LookupError:
                    result[key] = None
            from mewcode.kernel.sqlite_state import pointer
            for key in presence:
                path = pointer((key,))
                row = db.execute('SELECT kind FROM cells WHERE run_id=? AND space=? AND path=?',
                                 (run_id, 'state', path)).fetchone()
                if row is None:
                    result[key] = False
                elif row[0] in {'dict', 'list', 'seq'}:
                    result[key] = db.execute('SELECT 1 FROM cells WHERE run_id=? AND space=? '
                        'AND path>=? AND path<? LIMIT 1', (run_id, 'state', path+'/', path+'/\uffff')).fetchone() is not None
                else:
                    result[key] = bool(database._read(db, 'state', (key,)))
            return result

    def backfill_latest_checkpoints(self) -> int:
        """Older runs have only their latest state; never invent past versions."""
        count = 0
        for path in self._snapshot_paths():
            header = self._startup_fields(path.stem, 'status', 'last_checkpoint')
            if header is not None and (header.get('status') not in STOPPED or header.get('last_checkpoint')):
                continue
            snapshot = self.load(path.stem)
            if snapshot and snapshot.get("status") in STOPPED and not snapshot.get("last_checkpoint"):
                self.save_turn_checkpoint(path.stem)
                count += 1
        return count

    def restore_turn_checkpoint(self, run_id: str, checkpoint_id: str, *,
                                conversation_id: str, expected_updated_at: str) -> dict[str, Any]:
        with self._lock:
            current = self._cached(run_id)
            if current is None or current.get("conversation_id") != conversation_id:
                raise ValueError("检查点不属于当前对话")
            if current.get("status") not in STOPPED:
                raise ValueError("请先暂停当前任务，再恢复检查点")
            if not expected_updated_at or current.get("updated_at") != expected_updated_at:
                raise ValueError("任务状态已经变化，请刷新检查点列表后重试")
            meta, saved = self.checkpoint_archive.load(run_id, checkpoint_id)
            if saved.get("conversation_id") != conversation_id or (saved.get("status") not in STOPPED and meta.get("kind") != "node_end"):
                raise ValueError("检查点不是该对话已结束的运行状态")
            backup = self.checkpoint_archive.save(current, kind="before_restore", turn_key=utc_now())
            restored = copy.deepcopy(current)
            # Only execution progress is rewound. Later messages, events, usage,
            # attempt numbering, and project files are never removed/rolled back.
            for key in RESTORE_FIELDS:
                if key in saved:
                    restored[key] = copy.deepcopy(saved[key])
                else:
                    restored.pop(key, None)
            for bucket in ("node_states", "stages", "members"):
                for item in (restored.get(bucket) or {}).values():
                    if isinstance(item, dict) and item.get("status") == "running":
                        item["status"] = "paused"
            status = saved["status"] if saved["status"] in {"completed", "blocked", "paused"} else "paused"
            reason = f"已恢复到第 {meta['attempt']} 轮结束的检查点；未启动执行，稿件文件和聊天记录未回滚。"
            restored.update(status=status, active_node_ids=[], status_reason=reason,
                            updated_at=utc_now(), last_checkpoint=meta,
                            completed_at=saved.get("completed_at") if status == "completed" else None,
                            flow_state={"phase": "checkpoint_restored", "status": status, "reason": reason})
            restored.pop("resume_after_node", None)
            restored.pop("retry_node_id", None)
            restored["last_seq"] = int(current.get("last_seq") or 0) + 1
            restored.setdefault("checkpoint_restorations", []).append({
                "at": restored["updated_at"], "checkpoint_id": checkpoint_id,
                "backup_checkpoint_id": backup["checkpoint_id"],
            })
            restored.setdefault("events", []).append({
                "type": "expert_checkpoint_restored", "run_id": run_id,
                "conversation_id": conversation_id, "seq": restored["last_seq"],
                "attempt": restored.get("attempt"), "status": status,
                "checkpoint_id": checkpoint_id, "server_time": restored["updated_at"],
            })
            self._write(restored)
            return copy.deepcopy(restored)

    def _path(self, run_id: str) -> Path:
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("invalid expert run id")
        return self.root / f"{run_id}.json"

    def _event_log_path(self, run_id: str) -> Path:
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("invalid expert run id")
        return self.root / f"{run_id}.events.jsonl"

    def _large_field_path(self, run_id: str, digest: str) -> Path:
        if not RUN_ID_PATTERN.fullmatch(run_id) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("invalid large-field identity")
        return self.root / "blobs" / run_id / f"{digest}.json"

    def _review_queue_path(self, run_id: str, digest: str) -> Path:
        if not RUN_ID_PATTERN.fullmatch(run_id) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("invalid review-queue identity")
        return self.root / "blobs" / run_id / f"{digest}.queue.json"

    def _store_review_queue(self, run_id: str, queue: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps(queue, ensure_ascii=True, sort_keys=True,
                             separators=(",", ":")).encode("ascii")
        digest = hashlib.sha256(payload).hexdigest()
        path = self._review_queue_path(run_id, digest)
        if path.is_file():
            if path.read_bytes() != payload:
                raise OSError("review-queue blob differs from its content hash")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary_name = tempfile.mkstemp(
                prefix=f".{digest}.", suffix=".tmp", dir=str(path.parent))
            try:
                with os.fdopen(handle, "wb") as temporary:
                    temporary.write(payload)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                os.replace(temporary_name, path)
            finally:
                Path(temporary_name).unlink(missing_ok=True)
        return {"$mewcode_review_queue": REVIEW_QUEUE_BLOB_VERSION,
                "sha256": digest, "revision": queue.get("revision")}

    def _read_review_queue_ref(self, run_id: str, reference: dict[str, Any]) -> dict[str, Any]:
        if (not isinstance(reference, dict)
                or set(reference) != {"$mewcode_review_queue", "sha256", "revision"}
                or reference.get("$mewcode_review_queue") != REVIEW_QUEUE_BLOB_VERSION):
            raise ValueError("invalid review-queue reference")
        digest = reference.get("sha256")
        payload = self._review_queue_path(run_id, digest).read_bytes()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError("review-queue blob digest mismatch")
        queue = json.loads(payload)
        if not isinstance(queue, dict) or queue.get("revision") != reference["revision"]:
            raise ValueError("review-queue blob revision mismatch")
        return queue

    def _hydrate_review_queues(self, run_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        references = snapshot.get("review_queue_refs")
        if references is None:
            return snapshot  # Legacy inline queues remain readable.
        if not isinstance(references, dict) or snapshot.get("review_queues"):
            raise ValueError("ambiguous review-queue storage")
        snapshot["review_queues"] = {
            str(node_id): self._read_review_queue_ref(run_id, reference)
            for node_id, reference in references.items()
        }
        return snapshot

    def _store_large_field(self, run_id: str, value: str) -> dict[str, Any]:
        # ASCII JSON preserves even legacy lone surrogates without lossy
        # encoding. The hash covers the exact serialized string.
        payload = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        digest = hashlib.sha256(payload).hexdigest()
        path = self._large_field_path(run_id, digest)
        if path.is_file():
            if path.read_bytes() != payload:
                raise OSError("large-field blob differs from its content hash")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary_name = tempfile.mkstemp(prefix=f".{digest}.", suffix=".tmp", dir=str(path.parent))
            try:
                with os.fdopen(handle, "wb") as temporary:
                    temporary.write(payload)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                os.replace(temporary_name, path)
            finally:
                Path(temporary_name).unlink(missing_ok=True)
        return {"$mewcode_large_field": LARGE_FIELD_BLOB_VERSION, "sha256": digest,
                "chars": len(value)}

    def _externalize_large_fields(self, run_id: str, value: Any) -> Any:
        if isinstance(value, str) and len(value) >= LARGE_FIELD_MIN_CHARS:
            return self._store_large_field(run_id, value)
        if isinstance(value, list):
            return [self._externalize_large_fields(run_id, item) for item in value]
        if isinstance(value, dict):
            return {key: self._externalize_large_fields(run_id, item)
                    for key, item in value.items()}
        return value

    def _hydrate_large_fields(self, run_id: str, value: Any) -> Any:
        if isinstance(value, list):
            return [self._hydrate_large_fields(run_id, item) for item in value]
        if not isinstance(value, dict):
            return value
        if value.get("$mewcode_large_field") == LARGE_FIELD_BLOB_VERSION:
            if set(value) != {"$mewcode_large_field", "sha256", "chars"}:
                raise ValueError("invalid large-field reference")
            digest = value.get("sha256")
            path = self._large_field_path(run_id, digest)
            payload = path.read_bytes()
            if hashlib.sha256(payload).hexdigest() != digest:
                raise ValueError("large-field digest mismatch")
            restored = json.loads(payload)
            if not isinstance(restored, str) or len(restored) != value.get("chars"):
                raise ValueError("invalid large-field payload")
            return restored
        return {key: (item if key == "process_file_preview_refs"
                      else self._hydrate_large_fields(run_id, item))
                for key, item in value.items()}


    def _append_event_log(self, run_id: str, events: list[dict[str, Any]], last_seq: int) -> None:
        entry = {"journal_version": EVENT_LOG_VERSION, "last_seq": last_seq, "events": events}
        payload = (json.dumps(entry, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")) + "\n").encode("utf-8", errors="backslashreplace")
        with self._event_log_path(run_id).open("ab") as handle:
            if handle.tell():
                with self._event_log_path(run_id).open("rb") as check:
                    check.seek(-1, os.SEEK_END)
                    if check.read(1) != b"\n":
                        raise OSError("event log has an incomplete tail; no new event was appended")
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

    def _summary_path(self, run_id: str) -> Path:
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("invalid expert run id")
        return self.root / f"{run_id}.public.json"

    @staticmethod
    def _child_task_ids(snapshot: dict[str, Any]) -> list[str]:
        return sorted({
            str(child.get("task_id") or "")
            for child in (snapshot.get("children") or {}).values()
            if isinstance(child, dict) and child.get("task_id")
        })

    def _write_summary_cache(
        self, snapshot: dict[str, Any], source_path: Path,
    ) -> dict[str, Any]:
        summary = self._public_value(snapshot, include_details=False)
        if 'kernel_revision' not in snapshot:
            summary.update(migration_required=True, read_only=True)
        summary["_child_task_ids"] = self._child_task_ids(snapshot)
        try:
            stat = self._source_stat(source_path)
            payload = {
                "source_mtime_ns": stat.st_mtime_ns,
                "source_size": stat.st_size,
                "event_log_size": self._event_log_path(str(snapshot["run_id"])).stat().st_size
                    if self._event_log_path(str(snapshot["run_id"])).is_file() else 0,
                "snapshot_version": SNAPSHOT_VERSION,
                "projection_version": SUMMARY_PROJECTION_VERSION,
                "conversation_id": str(snapshot.get("conversation_id") or ""),
                "summary": summary,
            }
            cache_path = self._summary_path(str(snapshot["run_id"]))
            handle, temporary_name = tempfile.mkstemp(
                prefix=f".{cache_path.stem}.", suffix=".tmp",
                dir=str(cache_path.parent),
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8", errors="backslashreplace") as temporary:
                    json.dump(payload, temporary, ensure_ascii=False)
                os.replace(temporary_name, cache_path)
            finally:
                Path(temporary_name).unlink(missing_ok=True)
        except OSError:
            pass
        return summary

    def _read_summary_cache(
        self, path: Path, stat: os.stat_result,
    ) -> tuple[str, dict[str, Any]] | None:
        try:
            payload = json.loads(
                self._summary_path(path.stem).read_text(encoding="utf-8")
            )
            if (
                isinstance(payload, dict)
                and int(payload.get("source_mtime_ns") or 0) == stat.st_mtime_ns
                and int(payload.get("source_size") or 0) == stat.st_size
                and int(payload.get("event_log_size") or 0) == (
                    self._event_log_path(path.stem).stat().st_size
                    if self._event_log_path(path.stem).is_file() else 0)
                and int(payload.get("snapshot_version") or 0) == SNAPSHOT_VERSION
                and int(payload.get("projection_version") or 0) == SUMMARY_PROJECTION_VERSION
                and isinstance(payload.get("summary"), dict)
            ):
                return (
                    str(payload.get("conversation_id") or ""),
                    payload["summary"],
                )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
        return None

    def _write(self, snapshot: dict[str, Any]) -> None:
        _validate_collaboration_write(snapshot)
        _normalize_snapshot(snapshot)
        from mewcode.kernel.escalation import ensure_owners
        ensure_owners(snapshot)
        _compact_snapshot_events(snapshot)
        run_id = str(snapshot["run_id"])
        previous = copy.deepcopy(self._live.get(run_id))
        self._live[run_id] = snapshot
        try:
            self._enqueue_write(run_id, durable=True)
            self._commit(snapshot)
        except (OSError, TimeoutError, ValueError, WriteLeaseError):
            if previous is None:
                self._live.pop(run_id, None)
            else:
                self._live[run_id] = previous
            raise

    def _persist(self, snapshot: dict[str, Any]) -> None:
        run_id = str(snapshot["run_id"])
        from mewcode.kernel.events import EventJournal
        if EventJournal(self.root, run_id).sqlite:
            return
        path = self._path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Drop the compatibility alias *before* walking/externalizing large
        # fields.  Removing it afterwards saves disk bytes but still doubles
        # every per-write traversal and blob-integrity check.
        duplicate_stages = snapshot.get("stages") == snapshot.get("node_states")
        disk_source = ({key: value for key, value in snapshot.items() if key != "stages"}
                       if duplicate_stages else snapshot)
        disk_source = dict(disk_source)
        queues = snapshot.get("review_queues") or {}
        if not isinstance(queues, dict):
            raise ValueError("review queues must be a mapping")
        disk_source["review_queue_refs"] = {
            str(node_id): self._store_review_queue(run_id, queue)
            for node_id, queue in queues.items()
        }
        disk_source.pop("review_queues", None)
        preview_refs = dict(snapshot.get("process_file_preview_refs") or {})
        process_files = []
        current_file_ids = set()
        for record in snapshot.get("process_files") or []:
            if not isinstance(record, dict):
                process_files.append(record)
                continue
            item = dict(record)
            file_id = item.get("id")
            if isinstance(file_id, str):
                current_file_ids.add(file_id)
                preview = item.pop("preview", None)
                if isinstance(preview, str):
                    preview_refs[file_id] = self._store_large_field(run_id, preview)
            process_files.append(item)
        if "process_files" in snapshot:
            disk_source["process_files"] = process_files
            disk_source["process_file_preview_refs"] = {
                key: value for key, value in preview_refs.items()
                if key in current_file_ids
            }
        disk_snapshot = self._externalize_large_fields(str(snapshot["run_id"]), disk_source)
        # ``stages`` is a legacy read alias for ``node_states``.  The in-memory
        # projection may keep both for old callers, but storing the same tree
        # twice makes every durable update rewrite megabytes of duplicate
        # state.  _normalize_snapshot restores the alias when loading.  Keep
        # both only if an unexpected divergence would otherwise lose data.
        if duplicate_stages:
            disk_snapshot.pop("stages", None)
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{path.stem}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            # Preserve imported lone surrogates as JSON escapes; old completed
            # work must remain recoverable even before transport normalization.
            with os.fdopen(handle, "w", encoding="utf-8", errors="backslashreplace") as temporary:
                json.dump(disk_snapshot, temporary, ensure_ascii=False, indent=2)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, path)
            stat = path.stat()
            run_id = str(snapshot["run_id"])
            with self._lock:
                newer_live = int((self._live.get(run_id) or {}).get("last_seq") or 0) > int(snapshot.get("last_seq") or 0)
            if not newer_live:
                summary = self._write_summary_cache(snapshot, path)
                self._public_summary_cache[run_id] = (
                    stat.st_mtime_ns, stat.st_size,
                    str(snapshot.get("conversation_id") or ""), summary,
                )
                with self._lock:
                    self._event_only_batches[run_id] = 0
            else:
                self._public_summary_cache.pop(run_id, None)
        finally:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass

    def create(
        self,
        *,
        run_id: str,
        conversation_id: str,
        task: str,
        attachments: list[dict[str, Any]],
        profile: dict[str, Any],
        strategy: str,
        flow: ExpertFlowSpec | dict[str, Any] | None = None,
        initial_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        locked_flow = ExpertFlowSpec.from_value(flow or DEFAULT_EXPERT_FLOW)
        snapshot: dict[str, Any] = {
            "version": SNAPSHOT_VERSION,
            "run_id": run_id,
            "conversation_id": conversation_id,
            "task": task,
            "supplemental_requirements": [],
            "attachments": attachments,
            "status": "running",
            "stage_cursor": locked_flow.entry_node_ids()[0],
            "attempt": 1,
            "strategy": strategy,
            "strategy_source": strategy_source(strategy),
            "locked_profile": profile,
            "locked_flow": locked_flow.as_dict(),
            "flow_id": locked_flow.flow_id,
            "flow_version": locked_flow.version,
            "flow_fingerprint": expert_flow_fingerprint(locked_flow),
            "saved_plan_fingerprint": _saved_plan_fingerprint(locked_flow),
            "finalizer_node_id": locked_flow.finalizer_node_id,
            "created_at": now,
            "updated_at": now,
            "started_at": now,
            "completed_at": None,
            "paused_intervals": [],
            "attempts": [],
            "stages": {},
            "node_states": {},
            "node_ledgers": {},
            "node_attempts": {},
            "members": {},
            "pipeline_runs": {},
            "node_expansions": {},
            "handoffs": [],
            "active_node_ids": [],
            "tools": {},
            "children": {},
            "usage": {},
            "events": [],
            "display_index": 0,
            "last_seq": 0,
            "legacy": False,
        }
        self._start_attempt_inplace(snapshot, strategy)
        from mewcode.kernel.collaboration import flow_mode, initialize
        snapshot["orchestration_mode"] = flow_mode(locked_flow.as_dict())
        if snapshot["orchestration_mode"] == "collaborative":
            from mewcode.orchestration.collab_compile import compile_plan
            collaboration = initialize(snapshot, compile_plan(locked_flow))
            if initial_state is None:
                # New runs share their root allowance. Existing/imported runs
                # keep their saved policy until an explicit user decision.
                collaboration['policy']['lineage_budget_mode'] = 'elastic'
                collaboration['required_capabilities'] = sorted(set(collaboration['required_capabilities']) | {'elastic_budget'})
                from mewcode.orchestration.finite_work_capacity import prepare as prepare_finite_capacity
                prepare_finite_capacity(collaboration, locked_flow)
        if initial_state is not None:
            if set(initial_state) - {'status', 'migration'} or initial_state.get('status') != 'paused':
                raise ValueError('initial import state must be a paused migration')
            snapshot.update(copy.deepcopy(initial_state))
        with self._lock:
            if snapshot["orchestration_mode"] == "collaborative" and self._cached(run_id) is not None:
                raise ValueError("协作运行身份已存在，请恢复原运行，不能覆盖启动记录")
            self._write(snapshot)
        return copy.deepcopy(snapshot)

    def locked_model_for(self, run_id: str, node_id: str, role: str) -> dict[str, Any] | None:
        """Read the live node binding without cloning an entire large run."""
        with self._lock:
            snapshot = self._cached(run_id) or {}
            models = snapshot.get("locked_models") or {}
            value = models.get(node_id) or models.get(role)
            return copy.deepcopy(value) if isinstance(value, dict) else None

    def load(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._cached(run_id)
            return copy.deepcopy(value) if value is not None else None

    def read_collaboration(self, run_id: str) -> dict[str, Any]:
        from mewcode.kernel.collaboration import state
        with self._lock:
            return copy.deepcopy(state(self._cached(run_id) or {}))

    def read_collaboration_fields(self, run_id: str, *fields: str) -> dict[str, Any]:
        """Read scheduler metadata without copying past delivery bodies."""
        from mewcode.kernel.collaboration import state
        with self._lock:
            value = state(self._cached(run_id) or {})
            return copy.deepcopy({key: value[key] for key in fields if key in value})

    def read_current_certificate(self, run_id: str, node_id: str):
        """Validate the live dependency chain and detach its physical checks."""
        from mewcode.kernel.collaboration import state, valid_certificate
        with self._lock:
            value = state(self._cached(run_id) or {})
            if not valid_certificate(value, node_id):
                return None
            certificate = value['certificates'][node_id]
            return copy.deepcopy({'certificate': certificate,
                'workspace': value['attempts'][certificate['attempt_id']].get('workspace')})

    def read_stage_fields(self, run_id: str, node_id: str, *fields: str):
        """Detach exact stage fields without replaying tools or transcripts."""
        with self._lock:
            stage = ((self._cached(run_id) or {}).get('node_states') or {}).get(node_id) or {}
            return copy.deepcopy({key: stage[key] for key in fields if key in stage})

    def read_segment_progress(self, run_id: str, node_id: str):
        """Project existing progress and blockers from one coherent revision."""
        from mewcode.kernel.work_continuation import segment_progress
        with self._lock:
            return copy.deepcopy(segment_progress(self._cached(run_id) or {}, node_id))

    def read_execution_fields(self, run_id: str, *fields: str) -> dict[str, Any]:
        """Detach scheduler metadata without walking transcripts or archives."""
        with self._lock:
            record = self._cached(run_id) or {}
            return copy.deepcopy({key: record[key] for key in fields if key in record})

    def read_execution_flow(self, run_id: str):
        """Read the validated active graph without cloning archived attempts."""
        from mewcode.kernel.collaboration_graph import effective_flow
        with self._lock:
            record = self._cached(run_id)
            if record is None:
                raise KeyError(run_id)
            return effective_flow(record)

    def read_workflow_blueprint(self, run_id: str, selection: str = 'applied'):
        """Detach the graph and public summary without copying tool checkpoints."""
        from mewcode.workflow_core.blueprint import extract
        with self._lock:
            record = self._cached(run_id)
            if record is None:
                raise LookupError('找不到这个工作流运行')
            return extract(record, selection)

    def prepare_authorization_manifest(self, run_id: str, *, workspace,
                                       task_authorization=None, task_id='', flow=None):
        """Recheck live authority and commit only a changed capability manifest.

        The compiler reads the locked record and builds a new manifest. Only
        that manifest enters the write candidate; execution history is shared.
        """
        from mewcode.kernel.preflight_binding import prepare_manifest
        from mewcode.kernel.events import EventJournal
        with self._lock:
            current = self._cached(run_id)
            if current is None:
                raise KeyError(run_id)
            if 'kernel_revision' not in current:
                raise ValueError('旧运行只读；请先显式迁移为新运行。')
            _validate_collaboration_write(current)
            journal = EventJournal(self.root, run_id)
            if journal.sqlite and journal._database().revision()[0] != current['kernel_revision']:
                raise ValueError('kernel writer is stale; refresh before mutating')
            manifest = prepare_manifest(dict(current), flow or self.read_execution_flow(run_id),
                workspace, task_authorization=task_authorization, task_id=task_id)
            if manifest == current.get('authorization_manifest'):
                return manifest
            if not journal.sqlite:
                self.mutate(run_id, lambda value: value.update(
                    authorization_manifest=copy.deepcopy(manifest)), durable=True, return_snapshot=False)
                return manifest
            previous = journal._sql_load(detached=False)
            if previous.get('kernel_revision') != current.get('kernel_revision'):
                raise ValueError('kernel writer is stale; refresh before mutating')
            candidate = dict(previous, authorization_manifest=copy.deepcopy(manifest), updated_at=utc_now())
            self._commit(candidate, kind='authorization_manifest', previous=previous)
            self._live[run_id] = dict(current, authorization_manifest=copy.deepcopy(manifest),
                updated_at=candidate['updated_at'], kernel_revision=candidate['kernel_revision'])
            self._after_event_log_append(run_id, candidate)
            return manifest

    def mutate_collaboration(self, run_id: str, callback):
        """Commit orchestration facts without rebuilding unrelated tool state.

        The callback receives only a detached collaboration tree. Tool receipts,
        node ledgers and execution transcripts remain owned by their existing
        writers; this path cannot change them or synthesize clearances.
        """
        from mewcode.kernel.collaboration import state
        with self._lock:
            previous = self._cached(run_id)
            if previous is None:
                raise KeyError(run_id)
            if 'kernel_revision' not in previous:
                raise ValueError('旧运行只读；请先显式迁移为新运行。')
            current = copy.deepcopy(state(previous))
            result = callback(current)
            snapshot = dict(previous, collaboration=current, updated_at=utc_now())
            _validate_collaboration_write(snapshot)
            if current == previous['collaboration']:
                return copy.deepcopy(result)
            self._live[run_id] = snapshot
            try:
                self._enqueue_write(run_id, durable=True)
                self._commit(snapshot, previous=previous)
            except (OSError, TimeoutError, ValueError, WriteLeaseError):
                self._live[run_id] = previous
                raise
            from mewcode.orchestration.workbench_events import changed
            changed('run', root=str(self.root), conversation_id=snapshot.get('conversation_id', ''),
                    run_id=run_id, **self._commit_notices.get(run_id, {}))
            return copy.deepcopy(result)

    def read_recovery_section(self, run_id, node_id, section, *, epoch=None, input_revision=None):
        """Detach one bound evidence section, never the full execution history."""
        from mewcode.experts.recovery_context import context_sections, validate_scope
        with self._lock:
            # A top-level view keeps ledger initialization private to this read.
            record = dict(self._cached(run_id) or {})
            validate_scope(record, run_id, node_id, epoch, input_revision)
            return copy.deepcopy(context_sections(record, node_id, section=section)[section])

    def stage_epoch(self, run_id, stage_key, *, member=False):
        """A tool declaration needs a generation, not the conversation archive."""
        with self._lock:
            snapshot = self._cached(run_id) or {}
            stage = (snapshot.get('members' if member else 'node_states') or {}).get(stage_key) or {}
            return int(stage.get('epoch') or 0)

    def read_turn_boundary(self, run_id, node_id):
        """Detach the mailbox and control fields needed between model turns."""
        with self._lock:
            snapshot = self._cached(run_id) or {}
            stage = (snapshot.get('node_states') or {}).get(node_id) or {}
            return copy.deepcopy({
                'interventions': snapshot.get('interventions') or [],
                'authorizations': snapshot.get('authorizations') or {},
                'assistance_request': stage.get('assistance_request') or {},
                'adaptive_request': stage.get('adaptive_request') or {},
            })

    def record_model_response(self, run_id, node_id, *, turn, node_attempt, history_size):
        """Commit a turn receipt without copying unrelated execution history."""
        from mewcode.experts.node_ledger import ledger_for_run, sync_run_facts
        from mewcode.kernel.escalation import ensure_owners
        from mewcode.kernel.events import EventJournal

        def record(value):
            ledger_for_run(value).record(node_id, 'receipt',
                {'model_response_completed': True, 'turn': turn},
                identity=['model_response', node_attempt, turn, history_size])

        with self._lock:
            current = self._cached(run_id)
            if current is None:
                raise KeyError(run_id)
            journal = EventJournal(self.root, run_id)
            if not journal.sqlite or current.get('status') != 'running':
                self.mutate(run_id, record, durable=True, return_snapshot=False)
                return
            previous = journal._sql_load(detached=False)
            if previous.get('kernel_revision') != current.get('kernel_revision'):
                raise ValueError('kernel writer is stale; refresh before mutating')
            _validate_collaboration_write(previous)
            # These helpers write only facts and derived stage fields. All
            # other values belong to the private committed tree and stay shared.
            candidate = dict(previous,
                node_states={name: dict(value) for name, value in previous['node_states'].items()},
                node_ledgers=copy.deepcopy(previous.get('node_ledgers') or {}))
            candidate['stages'] = candidate['node_states']
            record(candidate)
            sync_run_facts(candidate)
            ensure_owners(candidate)
            if (candidate['node_ledgers'] == previous.get('node_ledgers')
                    and candidate['node_states'] == previous['node_states']):
                return
            candidate['updated_at'] = utc_now()
            self._commit(candidate, kind='model_response', previous=previous,
                         stream_checkpoint=True)
            # Publication follows a successful transaction. Live callbacks
            # must never acquire references into the durable cache.
            live = dict(current, updated_at=candidate['updated_at'],
                kernel_revision=candidate['kernel_revision'],
                node_ledgers=copy.deepcopy(candidate['node_ledgers']),
                node_states=dict(current.get('node_states') or {}))
            for name, value in candidate['node_states'].items():
                old = previous['node_states'].get(name) or {}
                fields = {field: copy.deepcopy(item) for field, item in value.items()
                          if field not in old or item is not old[field]}
                if fields:
                    live['node_states'][name] = dict(live['node_states'].get(name) or {}, **fields)
            live['stages'] = live['node_states']
            self._live[run_id] = live
            self._after_event_log_append(run_id, candidate)

    def read_authorization_state(self, run_id, node_id):
        """Scope a capability check to live grants, bindings and its node facts.

        Binding reconciliation reads all grants and bound calls, but only
        projects the checked node. Execution messages and artifacts are not
        inputs to that permission decision.
        """
        with self._lock:
            snapshot = self._cached(run_id) or {}
            stage = (snapshot.get('node_states') or {}).get(node_id) or {}
            return copy.deepcopy({
                'authorization_manifest': snapshot.get('authorization_manifest'),
                'authorizations': snapshot.get('authorizations') or {},
                'node_ledgers': {node_id: snapshot['node_ledgers'][node_id]}
                    if node_id in (snapshot.get('node_ledgers') or {}) else {},
                'node_states': {node_id: {key: stage[key] for key in
                    ('epoch', 'node_phase', 'assistance_receipts', 'ledger_view') if key in stage}},
            })

    def read_kernel_call(self, run_id, call_id):
        with self._lock:
            snapshot = self._cached(run_id) or {}
            return copy.deepcopy((snapshot.get('kernel_calls') or {}).get(call_id))

    def record_tool_metadata(self, run_id, node_id, *, process_file=None, test_record=None):
        """Append tool evidence using the same writer, without cloning transcripts."""
        from mewcode.orchestration.process_files import add_process_file
        process_file, test_record = copy.deepcopy((process_file, test_record))
        def apply(value):
            if process_file is not None:
                add_process_file(value, process_file)
            if test_record is not None:
                value.setdefault('test_records', []).append(test_record)
        fields = (('process_files', 'process_file_receipts') if process_file is not None else ())
        if test_record is not None:
            fields += ('test_records',)
        if fields:
            self.mutate_tool_boundary(run_id, node_id, apply, fields=fields)

    def mutate_tool_boundary(self, run_id, node_id, callback, *, fields):
        """Commit a call/permission boundary without copying execution archives.

        The host callback owns only the named tool fields and this node's facts.
        It cannot edit conversations, contracts, other nodes, or grants. Facts,
        their projections and display documents still commit in one transaction.
        """
        if not fields or set(fields) - {'kernel_calls', 'authorization_manifest',
                                       'process_files', 'process_file_receipts', 'test_records'}:
            raise ValueError('unsupported tool boundary fields')
        from mewcode.kernel.events import EventJournal
        from mewcode.experts.node_ledger import sync_node_facts
        from mewcode.experts.blocking_policy import refresh_clearances
        from mewcode.kernel.escalation import ensure_owners
        projection_fields = ('epoch', 'node_phase', 'assistance_receipts', 'ledger_view')
        with self._lock:
            current = self._cached(run_id)
            if current is None:
                raise KeyError(run_id)
            journal = EventJournal(self.root, run_id)
            if not journal.sqlite:
                result = []
                self.mutate(run_id, lambda value: result.append(callback(value)),
                            durable=True, return_snapshot=False)
                return copy.deepcopy(result[0])
            previous = journal._sql_load(detached=False)
            if previous.get('kernel_revision') != current.get('kernel_revision'):
                raise ValueError('kernel writer is stale; refresh before mutating')
            _validate_collaboration_write(previous)
            stage = (previous.get('node_states') or {}).get(node_id) or {}
            scope = copy.deepcopy({
                **{key: previous[key] for key in (*fields, 'task', 'run_inputs', 'authorizations',
                                                'kernel_call_retry_authorizations')
                   if key in previous},
                'node_states': {node_id: {key: stage[key] for key in projection_fields if key in stage}},
                'node_ledgers': {node_id: previous['node_ledgers'][node_id]}
                    if node_id in (previous.get('node_ledgers') or {}) else {},
            })
            result = callback(scope)
            if (set(scope['node_states']) != {node_id} or set(scope['node_ledgers']) - {node_id}
                    or set(scope['node_states'][node_id]) - set(projection_fields)):
                raise ValueError('tool boundary changed an unrelated node field')
            # Detach the callback's owned results, including anything it retained.
            updates = copy.deepcopy({key: scope[key] for key in fields if key in scope})
            states = dict(previous.get('node_states') or {})
            states[node_id] = dict(stage, **copy.deepcopy(scope['node_states'][node_id]))
            ledgers = dict(previous.get('node_ledgers') or {})
            if node_id in scope['node_ledgers']:
                ledgers[node_id] = copy.deepcopy(scope['node_ledgers'][node_id])
            candidate = dict(previous, **updates, node_states=states, stages=states, node_ledgers=ledgers)
            # Only this node's structured runtime evidence could have changed.
            # Existing helpers keep clearance and recovery ownership semantics.
            sync_node_facts(candidate, node_id, include_legacy_blockers=True)
            refresh_clearances(candidate, node_id)
            ensure_owners({'node_states': {node_id: states[node_id]},
                           'node_ledgers': {node_id: ledgers[node_id]}})
            if (all(candidate.get(key) == previous.get(key) for key in fields)
                    and states[node_id] == stage
                    and ledgers[node_id] == (previous.get('node_ledgers') or {}).get(node_id)):
                return copy.deepcopy(result)
            candidate['updated_at'] = utc_now()
            self._commit(candidate, kind='tool_boundary', previous=previous,
                         call_checkpoint=set(fields) == {'kernel_calls'})
            # Publish only after commit; neither live state nor callback results
            # may acquire references into the private durable tree.
            live = dict(current, updated_at=candidate['updated_at'],
                        kernel_revision=candidate['kernel_revision'])
            live.update(copy.deepcopy(updates))
            live['node_ledgers'] = dict(current.get('node_ledgers') or {})
            live['node_ledgers'][node_id] = copy.deepcopy(ledgers[node_id])
            live['node_states'] = dict(current.get('node_states') or {})
            live_stage = dict(live['node_states'].get(node_id) or {})
            live_stage.update({key: copy.deepcopy(value) for key, value in states[node_id].items()
                               if key not in stage or value is not stage[key]})
            live['node_states'][node_id] = live_stage
            live['stages'] = live['node_states']
            self._live[run_id] = live
            self._after_event_log_append(run_id, candidate)
            return copy.deepcopy(result)

    def record_reviewed_tool_start(self, run_id, node_id, tool_id, tool_name, arguments):
        """Avoid a whole-run transaction when the review facts do not change."""
        from mewcode.experts.blocking_policy import record_reviewed_tool_start
        with self._lock:
            current = self._cached(run_id)
            if current is None:
                raise KeyError(run_id)
            # The policy only writes this node's ledger and derived view.
            # Keep large messages and unrelated nodes out of its private copy.
            record = dict(current)
            record['node_states'] = dict(current.get('node_states') or {})
            record['node_states'][node_id] = dict(record['node_states'].get(node_id) or {})
            record['node_ledgers'] = dict(current.get('node_ledgers') or {})
            if node_id in record['node_ledgers']:
                record['node_ledgers'][node_id] = copy.deepcopy(record['node_ledgers'][node_id])
            record_reviewed_tool_start(record, node_id, tool_id, tool_name, arguments)
            if (record['node_states'][node_id] == (current.get('node_states') or {}).get(node_id)
                    and record['node_ledgers'].get(node_id) == (current.get('node_ledgers') or {}).get(node_id)):
                return
            self.mutate(run_id, lambda value: record_reviewed_tool_start(
                value, node_id, tool_id, tool_name, arguments), durable=True, return_snapshot=False)

    def load_review_queue(self, run_id: str, node_id: str) -> dict[str, Any] | None:
        """Read one queue without hydrating the whole run from disk.

        Internal callers must still enforce the run's conversation ownership
        and contract hash.  A missing or corrupt shard is never an empty queue.
        """
        with self._lock:
            from mewcode.kernel.events import EventJournal
            if EventJournal(self.root, run_id).exists:
                value = self._cached(run_id)
                queue = ((value or {}).get('review_queues') or {}).get(node_id)
                return copy.deepcopy(queue) if queue is not None else None
            live = self._live.get(run_id)
            if live is not None:
                queue = (live.get("review_queues") or {}).get(node_id)
                return copy.deepcopy(queue) if queue is not None else None
            path = self._path(run_id)
            if not path.exists():
                return None
            raw = json.loads(path.read_text(encoding="utf-8"))
            references = raw.get("review_queue_refs")
            if references is not None:
                if not isinstance(references, dict) or raw.get("review_queues"):
                    raise ValueError("ambiguous review-queue storage")
                reference = references.get(node_id)
                return self._read_review_queue_ref(run_id, reference) if reference is not None else None
            queue = (raw.get("review_queues") or {}).get(node_id)
            return copy.deepcopy(queue) if queue is not None else None

    def process_file_preview(self, run_id: str, file_id: str) -> dict[str, Any] | None:
        """Read one preview only after the caller has checked conversation ownership."""
        with self._lock:
            snapshot = self._cached(run_id)
            if snapshot is None:
                return None
            record = next((item for item in snapshot.get("process_files") or []
                           if isinstance(item, dict) and item.get("id") == file_id), None)
            if record is None:
                return None
            result = copy.deepcopy(record)
            if "preview" not in result:
                reference = (snapshot.get("process_file_preview_refs") or {}).get(file_id)
                if reference is not None:
                    result["preview"] = self._hydrate_large_fields(run_id, reference)
            return result

    @staticmethod
    def _public_value(
        value: dict[str, Any], *, include_details: bool = True,
        role_filter: str = "",
    ) -> dict[str, Any]:
        """Project a bounded UI payload without copying resumable message chains."""
        public = {
            key: copy.deepcopy(item)
            for key, item in value.items()
            if key not in PRIVATE_TOP_LEVEL_FIELDS
        }
        if value.get("orchestration_mode") == "collaborative":
            from mewcode.orchestration.task_view import collaboration_projection as projection
            public["collaboration_progress"] = projection(value)
        from mewcode.experts.handling_state import handling_for_snapshot
        handling = handling_for_snapshot(value)
        if handling:
            public['handling'] = handling
        public["saved_plan_revision_pending"] = (
            (value.get("pending_dialogue_handoff") or {}).get("relation") == "plan_changed"
        )
        public["capability_executor"] = copy.deepcopy(
            (value.get("locked_profile") or {}).get("capability_executor")
        )
        public["review_progress"] = {
            node_id: {"total": len(queue.get("items") or []),
                      "recorded": len(queue.get("records") or {}),
                      "complete": sum(record.get("state") == "complete" for record in (queue.get("records") or {}).values())}
            for node_id, queue in (value.get("review_queues") or {}).items()
        }
        public["node_states"] = {
            str(node_id): {
                key: copy.deepcopy(item)
                for key, item in stage.items()
                if key not in PRIVATE_STAGE_FIELDS
            }
            for node_id, stage in (
                value.get("node_states") or value.get("stages") or {}
            ).items()
            if isinstance(stage, dict) and (
                not role_filter
                or node_id == role_filter
                or stage.get("role") == role_filter
            )
        }
        public["stages"] = copy.deepcopy(public["node_states"])
        if value.get("loop_states"):
            public["loop_states"] = {key: compact_loop_state(state) for key, state in value["loop_states"].items()}
        public["children"] = {}
        public["events"] = [
            compact_expert_event(event)
            for event in value.get("events") or []
            if isinstance(event, dict)
            and event.get("type") in PUBLIC_SUMMARY_EVENT_TYPES
            and (
                not role_filter
                or not event.get("role")
                or event.get("role") == role_filter or event.get("node_id") == role_filter
            )
        ][-MAX_PERSISTED_EVENTS:]
        _append_aggregate_display_events(
            public, source=value, include_details=include_details,
            role_filter=role_filter,
        )
        if include_details:
            _append_event_tool_details(public, value, role_filter)
        # Re-project the current pause from saved facts, including tool denials
        # that are private to the checkpoint. Old generic cards need no rerun.
        from mewcode.experts.auto_card import auto_card
        current_cards = {}
        for node_id, stage in (value.get("node_states") or {}).items():
            if not isinstance(stage, dict):
                continue
            decisions = (stage.get("scheduler_recovery") or {}).get("decisions") or []
            if not isinstance(decisions, list) or not decisions or stage.get("status") == "completed":
                continue
            decision = decisions[-1]
            if decision.get("action") not in {"human", "hold_until_clearance"}:
                continue
            display_stage = dict(stage)
            selected_model = (value.get("locked_models") or {}).get(node_id)
            user_model = (value.get("difficult_node_model_policy") or {}).get("model")
            if selected_model and selected_model == user_model:
                display_stage["configured_resume_model"] = selected_model
            presentation = auto_card(decision, display_stage)
            if decision.get("decision_id"):
                current_cards[(str(node_id), decision["decision_id"])] = presentation
            public_stage = public["node_states"].get(node_id)
            if public_stage:
                public_stage["scheduler_recovery"]["decisions"][-1]["presentation"] = presentation
        for event in public["events"]:
            decision = event.get("recovery_decision")
            key = (str(event.get("node_id") or ""), decision.get("decision_id")) if isinstance(decision, dict) else None
            if key in current_cards:
                decision["presentation"] = current_cards[key]
        public["stages"] = copy.deepcopy(public["node_states"])
        from mewcode.unicode_text import utf8_safe_json
        public["activated_at"] = run_activation_time(value)
        result = utf8_safe_json(redact_for_display(project_acceptance_status(public, source=value)))
        # Provider IDs are public catalog identifiers. The generic credential
        # redactor intentionally hides '*_key', so restore only this typed
        # model reference after redacting the remaining run data.
        from mewcode.model_registry import ModelRef
        raw_roles = value.get('role_models')
        if isinstance(raw_roles, dict):
            result['role_models'] = {**copy.deepcopy(raw_roles), 'roles': {
                role: ModelRef.from_value(ref).as_dict() for role, ref in raw_roles.get('roles', {}).items()}}
        raw_helper = (value.get("locked_profile") or {}).get("capability_executor")
        try:
            result["capability_executor"] = ModelRef.from_value(raw_helper).as_dict() if raw_helper else None
        except ValueError:
            result["capability_executor"] = None
        raw_difficulty = (value.get("difficult_node_model_policy") or {}).get("model")
        try:
            result["difficult_node_model"] = ModelRef.from_value(raw_difficulty).as_dict() if raw_difficulty else None
        except ValueError:
            result["difficult_node_model"] = None
        from mewcode.experts.resume_advice import resume_advice
        result["resume_advice"] = resume_advice(value)
        return result

    def public(self, run_id: str, *, role: str = "") -> dict[str, Any] | None:
        value = self.load(run_id)
        if value is not None and 'kernel_revision' not in value:
            value = {**value, 'migration_required': True, 'read_only': True}
        return (
            self._public_value(value, include_details=True, role_filter=role)
            if value is not None else None
        )

    def public_for_conversation(self, conversation_id: str) -> list[dict[str, Any]]:
        if (self.root / 'runs.sqlite3').is_file():
            return self.query_summaries(conversation_id)
        items: list[dict[str, Any]] = []
        with self._lock:
            for path in self._snapshot_paths():
                try:
                    stat = self._source_stat(path)
                    cached = self._public_summary_cache.get(path.stem)
                    if cached is not None and cached[:2] == (
                        stat.st_mtime_ns, stat.st_size
                    ):
                        owner, summary = cached[2], cached[3]
                    else:
                        disk_cache = self._read_summary_cache(path, stat)
                        if disk_cache is not None:
                            owner, summary = disk_cache
                        else:
                            value = self._read_disk(path.stem)
                            if not isinstance(value, dict):
                                continue
                            owner = str(value.get("conversation_id") or "")
                            summary = self._write_summary_cache(value, path)
                        self._public_summary_cache[path.stem] = (
                            stat.st_mtime_ns, stat.st_size, owner, summary,
                        )
                    if owner == conversation_id:
                        public_summary = copy.deepcopy(summary)
                        public_summary.pop("_child_task_ids", None)
                        items.append(project_acceptance_status(public_summary))
                except (OSError, json.JSONDecodeError, ValueError):
                    continue
        items.sort(
            key=lambda item: str(item.get("updated_at") or ""), reverse=True
        )
        return items

    def chat_for_conversation(self, conversation_id: str) -> list[dict[str, Any]]:
        from mewcode.kernel.chat_view import compact
        from mewcode.kernel.sqlite_state import RunDatabase
        if (self.root / 'runs.sqlite3').is_file():
            return RunDatabase(self.root, '').summaries(conversation_id, chat=True)
        return [compact(value) for value in self.public_for_conversation(conversation_id)]

    def refresh_public_summaries(self) -> int:
        """Warm durable summary sidecars so opening a chat never parses old blobs."""
        refreshed = 0
        with self._lock:
            for path in self._snapshot_paths():
                try:
                    from mewcode.kernel.events import EventJournal
                    from mewcode.kernel.run_views import build
                    journal = EventJournal(self.root, path.stem)
                    if journal.sqlite:
                        if self.read_design(path.stem) is None:
                            # Upgrade derived documents once during startup, never
                            # from a query. The authoritative revision is unchanged.
                            value = self._read_disk(path.stem)
                            if isinstance(value, dict):
                                models = build(value, history_events=[])
                                models.pop('history')  # Preserve the indexed historical document.
                                journal._database().publish_models(value, models)
                                self._read_model_cache.pop(path.stem, None)
                                refreshed += 1
                        continue
                    stat = self._source_stat(path)
                    if (self._read_summary_cache(path, stat) is not None and self.read_workbench(path.stem)
                            and self.read_design(path.stem) is not None):
                        continue
                    value = self._read_disk(path.stem)
                    if not isinstance(value, dict):
                        continue
                    summary = self._write_summary_cache(value, path)
                    journal._database().publish_models(value, build(value))
                    self._public_summary_cache[path.stem] = (
                        stat.st_mtime_ns, stat.st_size,
                        str(value.get("conversation_id") or ""), summary,
                    )
                    refreshed += 1
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    continue
        return refreshed

    def is_expert_child_task(
        self, task_id: str, conversation_id: str = "",
    ) -> bool:
        """Recognize legacy child tasks whose persisted record predates origin."""
        if not task_id:
            return False
        with self._lock:
            for snapshot in self._live.values():
                if conversation_id and str(snapshot.get("conversation_id") or "") != conversation_id:
                    continue
                if task_id in self._child_task_ids(snapshot):
                    return True
        for path in self._snapshot_paths():
            try:
                stat = self._source_stat(path)
                cached = self._public_summary_cache.get(path.stem)
                if cached is not None and cached[:2] == (
                    stat.st_mtime_ns, stat.st_size
                ):
                    owner, summary = cached[2], cached[3]
                else:
                    disk_cache = self._read_summary_cache(path, stat)
                    if disk_cache is not None:
                        owner, summary = disk_cache
                    else:
                        value = self._read_disk(path.stem)
                        if not isinstance(value, dict):
                            continue
                        owner = str(value.get("conversation_id") or "")
                        summary = self._write_summary_cache(value, path)
                    self._public_summary_cache[path.stem] = (
                        stat.st_mtime_ns, stat.st_size, owner, summary,
                    )
                if (
                    (not conversation_id or owner == conversation_id)
                    and task_id in (summary.get("_child_task_ids") or [])
                ):
                    return True
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
        return False

    def mutate(
        self, run_id: str, callback: Callable[[dict[str, Any]], None],
        *, durable: bool | None = None, return_snapshot: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            snapshot = self._cached(run_id)
            if snapshot is None:
                raise KeyError(run_id)
            if 'kernel_revision' not in snapshot:
                raise ValueError('旧运行只读；请先显式迁移为新运行。')
            previous = snapshot
            _validate_collaboration_write(previous)
            snapshot = copy.deepcopy(snapshot)
            callback(snapshot)
            _validate_collaboration_write(snapshot)
            from mewcode.kernel.collaboration import flow_mode
            if (previous.get("orchestration_mode", "classic") != snapshot.get("orchestration_mode", "classic")
                    or flow_mode(snapshot.get("locked_flow") or {}) != snapshot.get("orchestration_mode", "classic")):
                raise ValueError("运行开始后不能切换编排模式；请创建另一模式的新任务。")
            from mewcode.experts.node_ledger import sync_run_facts
            sync_run_facts(snapshot)
            from mewcode.kernel.escalation import ensure_owners
            ensure_owners(snapshot)
            snapshot["updated_at"] = utc_now()
            _normalize_snapshot(snapshot)
            _compact_snapshot_events(snapshot)
            if durable is None:
                durable = str(snapshot.get("status") or "") != "running"
            self._live[run_id] = snapshot
            try:
                self._enqueue_write(str(snapshot["run_id"]), durable=durable)
                from mewcode.kernel.events import EventJournal
                from mewcode.kernel.sqlite_state import sequenced
                journal = EventJournal(self.root, run_id)
                committed = journal._sql_load(detached=False) if journal.sqlite else None
                if committed is not None and all(not rows or sequenced(rows) for rows in (
                        committed.get('events') or [], snapshot.get('events') or [])):
                    old_events = {row['seq']: row for row in committed.get('events') or []}
                    changed_events = [row for row in snapshot.get('events') or []
                                      if old_events.get(row['seq']) != row]
                    # A general callback may retain its live objects. Keep the
                    # durable cache detached, while comparing against the
                    # committed tree and indexing only changed history events.
                    self._commit(snapshot, previous=committed,
                                 history_events=changed_events, cache_owned=False)
                else:
                    self._commit(snapshot)
            except (OSError, TimeoutError, ValueError, WriteLeaseError):
                self._live[run_id] = previous
                raise
            from mewcode.orchestration.workbench_events import changed
            changed('run', root=str(self.root), conversation_id=snapshot.get('conversation_id', ''),
                    run_id=run_id, **self._commit_notices.get(run_id, {}))
            return copy.deepcopy(snapshot) if return_snapshot else {}

    def record_workflow_attempt(self, run_id, identity, kind, fields, **metadata):
        """Commit node output without copying every historical model conversation."""
        from mewcode.workflow_core.store import publish_attempt, synchronize
        from mewcode.kernel.events import EventJournal
        with self._lock:
            current = self._cached(run_id)
            if current is None:
                raise KeyError(run_id)
            journal = EventJournal(self.root, run_id)
            if not journal.sqlite:
                result = []
                def apply(value):
                    result.append(publish_attempt(value['workflow_core'], identity, kind, fields, **metadata))
                    synchronize(value)
                self.mutate(run_id, apply, durable=True, return_snapshot=False)
                return result[0]
            previous = journal._sql_load(detached=False)
            if previous.get('kernel_revision') != current.get('kernel_revision'):
                raise ValueError('kernel writer is stale; refresh before mutating')
            _validate_collaboration_write(previous)
            original = previous['workflow_core']
            if identity not in original['attempts']:
                return None
            attempt = dict(original['attempts'][identity])
            state = dict(original, attempts=dict(original['attempts']),
                         node_sessions=dict(original.get('node_sessions') or {}))
            state['attempts'][identity] = attempt
            session_id = attempt.get('session_id')
            if session_id in state['node_sessions']:
                session = state['node_sessions'][session_id]
                state['node_sessions'][session_id] = dict(session, turns=list(session['turns']))
            candidate = dict(previous, workflow_core=state,
                node_states={key:dict(stage) for key,stage in previous['node_states'].items()}, updated_at=utc_now())
            candidate['stages'] = candidate['node_states']
            result = publish_attempt(state, identity, kind, fields, **metadata)
            synchronize(candidate)
            self._commit(candidate, kind='workflow_checkpoint', previous=previous)
            # The journal owns the frozen tree. Expose separate copies only for
            # the changed attempt/session; unrelated history keeps its identity.
            live_state = dict(current['workflow_core'], attempts=dict(current['workflow_core']['attempts']),
                              node_sessions=dict(current['workflow_core'].get('node_sessions') or {}))
            live_state['attempts'][identity] = copy.deepcopy(attempt)
            if attempt.get('session_id'):
                session_id = attempt['session_id']
                live_state['node_sessions'][session_id] = copy.deepcopy(state['node_sessions'][session_id])
            live = dict(current, workflow_core=live_state, kernel_revision=candidate['kernel_revision'],
                        updated_at=candidate['updated_at'], node_states=copy.deepcopy(candidate['node_states']))
            live['stages'] = live['node_states']
            self._live[run_id] = live
            self._after_event_log_append(run_id, candidate)
            return result

    def record_running_stage(self, run_id: str, node_id: str, saved_stage: dict[str, Any],
                             *, member_id: str = '', workflow_projection=None) -> None:
        """Save an active segment without cloning unrelated attempts or nodes.

        Starting and terminal transitions retain the ordinary mutation path.
        This method replaces only the supplied stage fields and derived facts.
        """
        if (saved_stage.get('status') != 'running' or saved_stage.get('node_id') != node_id
                or saved_stage.get('member_id', '') != member_id):
            raise ValueError('expected a running stage checkpoint')
        saved_stage = copy.deepcopy(saved_stage)
        bucket = 'members' if member_id else 'node_states'
        key = f'{node_id}::{member_id}' if member_id else node_id

        def apply(value):
            value.setdefault(bucket, {}).setdefault(key, {}).update(saved_stage)
            value[bucket][key]['attempt'] = value.get('attempt')
            if workflow_projection is not None:
                value[bucket][key].update(workflow_projection(value[bucket][key]))
            value.setdefault('usage', {})['total'] = aggregate_snapshot_usage(value)

        with self._lock:
            current = self._cached(run_id)
            if current is None:
                raise KeyError(run_id)
            from mewcode.kernel.events import EventJournal
            journal = EventJournal(self.root, run_id)
            if (not journal.sqlite or current.get('status') != 'running'
                    or key not in (current.get(bucket) or {})):
                self.mutate(run_id, apply, durable=True, return_snapshot=False)
                return
            previous = journal._sql_load(detached=False)
            if previous.get('kernel_revision') != current.get('kernel_revision'):
                raise ValueError('kernel writer is stale; refresh before mutating')
            _validate_collaboration_write(previous)
            candidate = dict(previous,
                node_states={name: dict(value) for name, value in previous['node_states'].items()},
                node_ledgers=copy.deepcopy(previous.get('node_ledgers') or {}),
                usage=dict(previous.get('usage') or {}))
            candidate['stages'] = candidate['node_states']
            if member_id:
                candidate['members'] = dict(previous.get('members') or {})
                candidate['members'][key] = dict(candidate['members'][key])
            apply(candidate)
            from mewcode.experts.node_ledger import sync_run_facts
            from mewcode.kernel.escalation import ensure_owners
            from mewcode.experts.branch_state import reconcile_unresolved_branch_skips
            sync_run_facts(candidate)
            ensure_owners(candidate)
            reconcile_unresolved_branch_skips(candidate)
            candidate['updated_at'] = utc_now()
            self._commit(candidate, kind='stage_checkpoint', previous=previous)
            live = dict(current, updated_at=candidate['updated_at'],
                        kernel_revision=candidate['kernel_revision'],
                        usage=copy.deepcopy(candidate['usage']),
                        node_ledgers=copy.deepcopy(candidate['node_ledgers']))
            for name in ('node_states', 'members'):
                if name not in candidate:
                    continue
                live[name] = dict(current.get(name) or {})
                for stage_id, stage in candidate[name].items():
                    old = (previous.get(name) or {}).get(stage_id, {})
                    fields = {field: copy.deepcopy(item) for field, item in stage.items()
                              if field not in old or item is not old[field]}
                    if fields:
                        live[name][stage_id] = dict(live[name].get(stage_id) or {}, **fields)
            live['stages'] = live['node_states']
            self._live[run_id] = live
            self._after_event_log_append(run_id, candidate)

    def record_loop_state(self, run_id: str, event: dict[str, Any]) -> None:
        """Commit a loop checkpoint without traversing unrelated execution history.

        The loop payload is still frozen and durably committed before returning.
        Ledger synchronization keeps tool progress and recovery facts current;
        changed recovery projections use the ordinary full read-model builder.
        """
        if (event.get('type') != 'expert_flow_state' or event.get('phase') != 'loop'
                or not isinstance(event.get('loop_state'), dict) or not event.get('loop_state_key')
                or any(key in event for key in ('audit_protocol', 'pending_recovery_revision', 'activated_rubric'))):
            raise ValueError('expected an ordinary loop checkpoint')

        def apply(value):
            value['flow_state'] = {key: item for key, item in event.items()
                                   if key not in {'type', 'run_id', 'seq', 'server_time'}}
            loop_state = event['loop_state']
            key = str(event['loop_state_key'])
            if (loop_state.get('token_window_receipts')
                    and (loop_state.get('loop_resource_policy') or {}).get('mode') == 'adaptive'):
                from mewcode.orchestration.token_window import append_window_change
                append_window_change(value, loop_state_key=key,
                                     loop_id=str(event.get('loop_id') or ''), loop_state=loop_state)
            value.setdefault('loop_states', {})[key] = loop_state

        with self._lock:
            current = self._cached(run_id)
            if current is None:
                raise KeyError(run_id)
            # The collaboration scheduler commits before publishing its event;
            # the web subscriber can receive the same checkpoint afterwards.
            # One state transition has one durable owner, not two transactions.
            projection = {key: item for key, item in event.items()
                          if key not in {'type', 'run_id', 'seq', 'server_time'}}
            if (current.get('flow_state') == projection
                    and (current.get('loop_states') or {}).get(str(event['loop_state_key'])) == event['loop_state']):
                return
            from mewcode.kernel.events import EventJournal
            journal = EventJournal(self.root, run_id)
            if not journal.sqlite or current.get('status') != 'running':
                self.mutate(run_id, apply, durable=True, return_snapshot=False)
                return
            previous = journal._sql_load(detached=False)
            if previous.get('kernel_revision') != current.get('kernel_revision'):
                raise ValueError('kernel writer is stale; refresh before mutating')
            _validate_collaboration_write(previous)
            candidate = dict(previous,
                node_states={name: dict(value) for name, value in previous['node_states'].items()},
                node_ledgers=copy.deepcopy(previous.get('node_ledgers') or {}),
                loop_states=dict(previous.get('loop_states') or {}))
            candidate['stages'] = candidate['node_states']
            if 'loop_budget_changes' in previous:
                candidate['loop_budget_changes'] = list(previous['loop_budget_changes'])
            # Freeze only the new payload. The committed tree is never shared
            # with the caller or the callback-owned live checkpoint.
            event = copy.deepcopy(event)
            apply(candidate)
            from mewcode.experts.node_ledger import sync_run_facts
            from mewcode.kernel.escalation import ensure_owners
            sync_run_facts(candidate)
            ensure_owners(candidate)
            from mewcode.experts.branch_state import reconcile_unresolved_branch_skips
            reconcile_unresolved_branch_skips(candidate)
            candidate['updated_at'] = utc_now()
            self._commit(candidate, kind='loop_checkpoint', previous=previous, loop_checkpoint=True)
            live = dict(current, updated_at=candidate['updated_at'],
                        kernel_revision=candidate['kernel_revision'],
                        flow_state=copy.deepcopy(candidate['flow_state']),
                        node_ledgers=copy.deepcopy(candidate['node_ledgers']),
                        loop_states=dict(current.get('loop_states') or {}),
                        node_states=dict(current.get('node_states') or {}))
            live['loop_states'][str(event['loop_state_key'])] = live['flow_state']['loop_state']
            if 'loop_budget_changes' in candidate:
                live['loop_budget_changes'] = copy.deepcopy(candidate['loop_budget_changes'])
            for name, value in candidate['node_states'].items():
                old = previous['node_states'][name]
                fields = {key: copy.deepcopy(item) for key, item in value.items()
                          if key not in old or item is not old[key]}
                if fields:
                    live['node_states'][name] = dict(live['node_states'].get(name) or {}, **fields)
            live['stages'] = live['node_states']
            self._live[run_id] = live
            self._after_event_log_append(run_id, candidate)

    def mutate_stopped(self, run_id: str, callback: Callable[[dict[str, Any]], None], *,
                       conversation_id: str, expected_updated_at: str) -> dict[str, Any]:
        """Apply host maintenance on a copy, with durable rollback on failure.

        This narrow entry point cannot start a run or replace its ownership.
        Ordinary scheduler events continue to use mutate().
        """
        with self._lock:
            current = self._cached(run_id)
            if (current is None or current.get("conversation_id") != conversation_id
                    or current.get("updated_at") != expected_updated_at):
                raise ValueError("原运行状态或所属对话已变化，请重新核对。")
            if current.get("status") == "running" or current.get("active_node_ids"):
                raise ValueError("运行仍在执行，请先暂停再检修。")
            if 'kernel_revision' not in current:
                raise ValueError('旧运行只读；请先显式迁移为新运行。')
            previous = copy.deepcopy(current)
            _validate_collaboration_write(current)
            candidate = copy.deepcopy(current)
            callback(candidate)
            _validate_collaboration_write(candidate)
            from mewcode.kernel.collaboration import flow_mode
            if (previous.get("orchestration_mode", "classic") != candidate.get("orchestration_mode", "classic")
                    or flow_mode(candidate.get("locked_flow") or {}) != candidate.get("orchestration_mode", "classic")):
                raise ValueError("运行开始后不能切换编排模式；请创建另一模式的新任务。")
            from mewcode.experts.node_ledger import sync_run_facts
            sync_run_facts(candidate)
            from mewcode.kernel.escalation import ensure_owners
            ensure_owners(candidate)
            if (candidate.get("run_id") != run_id or candidate.get("conversation_id") != conversation_id
                    or candidate.get("status") == "running" or candidate.get("active_node_ids")):
                raise ValueError("维护不能改变运行归属或启动节点。")
            candidate["updated_at"] = utc_now()
            _normalize_snapshot(candidate)
            _compact_snapshot_events(candidate)
            self._live[run_id] = candidate
            generation = self._write_generation.get(run_id, 0) + 1
            try:
                self._enqueue_write(run_id, durable=True)
                self._commit(candidate, kind='maintenance')
            except (OSError, TimeoutError, ValueError, WriteLeaseError):
                if self._write_generation.get(run_id) == generation:
                    self._live[run_id] = previous
                    try:
                        self._enqueue_write(run_id, durable=True)
                    except (OSError, TimeoutError) as rollback:
                        raise OSError("运行维护保存失败，内存已恢复；磁盘恢复尚未确认，请核对维护前检查点。") from rollback
                raise
            return copy.deepcopy(candidate)

    @staticmethod
    def _start_attempt_inplace(snapshot: dict[str, Any], strategy: str) -> None:
        now = utc_now()
        attempt = int(snapshot.get("attempt") or 1)
        snapshot["strategy"] = strategy
        snapshot["strategy_source"] = strategy_source(strategy)
        snapshot.setdefault("attempts", []).append({
            "attempt": attempt,
            "strategy": strategy,
            "strategy_source": strategy_source(strategy),
            "started_at": now,
            "ended_at": None,
            "status": "running",
        })

    def set_loop_token_policy(
        self, run_id: str, *, loop_id: str, expected_updated_at: str,
        command_id: str, reason: str, ceiling: int, floor: int = 16_384,
        rate_per_minute: int | None = None, burst_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Authorize a finite adaptive window without changing the loop contract.

        This is a stopped-run user grant. Runtime window changes stay below the
        approved ceiling and are separately receipted; no model can call this.
        """
        from mewcode.orchestration.loops import LOOP_BUDGET_LIMITS, LoopContract, LoopWatchdog
        from mewcode.orchestration.token_window import initial_token_window
        if not command_id.strip() or not reason.strip() or not expected_updated_at:
            raise ValueError("Token 窗口授权必须有命令编号、原因和当前检查点版本")
        if (isinstance(ceiling, bool) or not isinstance(ceiling, int)
                or not 1024 <= ceiling <= LOOP_BUDGET_LIMITS["token_budget"]["max"]):
            raise ValueError("自适应 Token 总额必须为受支持的有限整数")
        if isinstance(floor, bool) or not isinstance(floor, int) or floor < 1024:
            raise ValueError("自适应 Token 初始窗口必须至少为 1024")
        if (rate_per_minute is not None and
                (type(rate_per_minute) is not int or
                 not 1 <= rate_per_minute <= ceiling * 60)):
            raise ValueError("Token 每分钟补充速度须为正整数且不超过总额的每秒补满值")
        if (burst_tokens is not None and
                (type(burst_tokens) is not int or burst_tokens < 1024 or burst_tokens > ceiling)):
            raise ValueError("Token 突发容量必须在 1024 与获批总额之间")
        if rate_per_minute is None and burst_tokens is not None:
            raise ValueError("Token 突发容量需要同时明确补充速度")

        def apply(snapshot):
            state = (snapshot.get("loop_states") or {}).get(loop_id)
            if not isinstance(state, dict):
                raise ValueError("指定循环检查点不存在")
            previous = state.get("loop_resource_policy") or {}
            spent = state.get("tokens", 0)
            if isinstance(spent, bool) or not isinstance(spent, int) or spent < 0:
                raise ValueError("循环累计用量无有效回执，须先对账")
            effective_floor = min(floor, ceiling - spent)
            effective_rate = rate_per_minute
            effective_burst = (burst_tokens if burst_tokens is not None else effective_floor)
            if effective_rate is None:
                effective_burst = None
            if previous.get("change_id") == command_id:
                if (previous.get("reason") != reason or previous.get("mode") != "adaptive"
                        or previous.get("limit") != ceiling
                        or previous.get("requested_floor") != floor
                        or previous.get("requested_rate_per_minute") != rate_per_minute
                        or previous.get("requested_burst_tokens") != burst_tokens):
                    raise ValueError("相同 Token 授权命令不能更换内容")
                return
            if snapshot.get("updated_at") != expected_updated_at:
                raise ValueError("检查点已变化，请重新核对后授权")
            if snapshot.get("status") not in {"blocked", "paused", "needs_attention"} or snapshot.get("active_node_ids"):
                raise ValueError("只能修改已停止且没有执行节点的循环 Token 限额")
            definition = next((item for item in (snapshot.get("locked_flow") or {}).get("meta", {}).get("loops", [])
                               if item.get("loop_id") == loop_id), None)
            if definition is None:
                raise ValueError("只允许指定当前运行的顶层循环")
            contract = LoopContract.from_value(definition)
            watchdog = LoopWatchdog(contract, state)
            if watchdog.team_mode:
                raise ValueError("团队模式本就没有循环 Token 累计限额；请先明确切换严格模式")
            if state.get("active_at"):
                raise ValueError("循环仍有未结算执行，不能改变资源授权")
            window = initial_token_window(spent_tokens=spent, floor=effective_floor,
                                          ceiling=ceiling, rate_per_minute=effective_rate,
                                          burst_tokens=effective_burst)
            change = {"change_id": command_id, "authorized_by": "user",
                      "authorized_at": utc_now(), "reason": reason,
                      "contract_hash": contract.contract_hash, "mode": "adaptive",
                      "limit": ceiling, "floor": effective_floor,
                      "requested_floor": floor,
                      "rate_per_minute": effective_rate,
                      "burst_tokens": effective_burst,
                      "requested_rate_per_minute": rate_per_minute,
                      "requested_burst_tokens": burst_tokens,
                      "previous_limit": watchdog.token_limit}
            snapshot.setdefault("loop_budget_changes", []).append({**change, "loop_id": loop_id,
                "tokens_at_change": spent, "elapsed_at_change": state.get("elapsed")})
            state["loop_resource_policy"] = change
            state["token_window"] = window
            state["token_window_receipts"] = []
            state["budget_resume_pending"] = bool(
                state.get("status") == "blocked" and state.get("error_kind") == "budget_exhausted"
                and watchdog.token_limit is not None and spent >= watchdog.token_limit
                and state.get("reason") in {"达到循环 Token 预算", "达到循环 Token 或时间预算"}
            )

        return self.mutate(run_id, apply, durable=True)

    def disable_loop_token_limit(
        self, run_id: str, *, loop_id: str, expected_updated_at: str,
        command_id: str, reason: str,
    ) -> dict[str, Any]:
        """Trusted host operation for an explicit user grant; never starts work.

        Call while the owning backend is stopped, or through its own store.
        Other processes must not mutate this store's cached snapshots.
        """
        from mewcode.orchestration.loops import LoopContract, LoopWatchdog
        if not command_id.strip() or not reason.strip() or not expected_updated_at:
            raise ValueError("Token 授权必须有命令编号、原因和当前检查点版本")

        def apply(snapshot):
            state = (snapshot.get("loop_states") or {}).get(loop_id)
            if not isinstance(state, dict):
                raise ValueError("指定循环检查点不存在")
            previous = state.get("loop_resource_policy") or {}
            if previous.get("change_id") == command_id:
                if previous.get("reason") != reason:
                    raise ValueError("相同 Token 授权命令不能更换内容")
                return
            if snapshot.get("updated_at") != expected_updated_at:
                raise ValueError("检查点已变化，请重新核对后授权")
            if snapshot.get("status") not in {"blocked", "paused", "needs_attention"} or snapshot.get("active_node_ids"):
                raise ValueError("只能修改已停止且没有执行节点的循环 Token 限额")
            definition = next((item for item in (snapshot.get("locked_flow") or {}).get("meta", {}).get("loops", [])
                               if item.get("loop_id") == loop_id), None)
            if definition is None:
                raise ValueError("只允许指定当前运行的顶层循环，不隐式修改嵌套或其他循环")
            contract = LoopContract.from_value(definition)
            watchdog = LoopWatchdog(contract, state)
            if state.get("active_at"):
                raise ValueError("循环仍有未结算执行，不能改变资源授权")
            if watchdog.token_limit is None:
                return
            change = {"change_id": command_id, "authorized_by": "user", "authorized_at": utc_now(),
                      "reason": reason, "contract_hash": contract.contract_hash, "limit": None,
                      "previous_limit": watchdog.token_limit}
            snapshot.setdefault("loop_budget_changes", []).append({**change, "loop_id": loop_id,
                "tokens_at_change": state["tokens"], "elapsed_at_change": state["elapsed"]})
            state["loop_resource_policy"] = change
            # Only the token block may reopen on a later explicit Continue.
            # Do not reset the round, incomplete results, messages or budgets.
            state["budget_resume_pending"] = bool(
                state.get("status") == "blocked" and state.get("error_kind") == "budget_exhausted"
                and watchdog.token_limit is not None and state["tokens"] >= watchdog.token_limit
                and state.get("reason") in {"达到循环 Token 预算", "达到循环 Token 或时间预算"}
            )

        return self.mutate(run_id, apply, durable=True)

    def _align_unreviewed_loop_summary(self, run_id, contract, state):
        """Compatibility for edited exit prose; never transfer old acceptance.

        All other contract fields, including criteria, queue order/goals and limits,
        must be identical. A legacy definition must be attested by an immutable
        checkpoint, not inferred from similar wording or supplied by a model.
        """
        from mewcode.orchestration.loops import LoopContract
        old_hash = state.get('contract_hash')
        if not old_hash or old_hash == contract.contract_hash:
            return state
        if (state.get('status') == 'completed' or state.get('completed_items') or state.get('item_results')
                or state.get('reports') or state.get('scores') is not None
                or any(result.get('loop_decision') for result in (state.get('round_results') or {}).values())):
            raise ValueError('循环验收定义已变化；不能沿用旧验收，请先确认变更范围')
        previous = state.get('contract_snapshot')
        if previous is None:
            # New runs persist their exact definition. Only legacy runs need
            # this bounded archive lookup, without creating or modifying files.
            for point in self.checkpoint_archive.list(run_id)[:32]:
                _, archived = self.checkpoint_archive.load(run_id, point['checkpoint_id'])
                for candidate in (archived.get('locked_flow') or {}).get('meta', {}).get('loops', []):
                    if candidate.get('loop_id') == contract.loop_id and LoopContract.from_value(candidate).contract_hash == old_hash:
                        previous = candidate
                        break
                if previous is not None:
                    break
        if previous is None or LoopContract.from_value(previous).contract_hash != old_hash:
            raise ValueError('找不到匹配的旧循环定义；保留进度，请先核对计划变更')
        old_definition = LoopContract.from_value(previous).as_dict()
        current_definition = contract.as_dict()
        if ({key: value for key, value in old_definition.items() if key != 'exit_condition'}
                != {key: value for key, value in current_definition.items() if key != 'exit_condition'}):
            raise ValueError('循环执行或验收条件发生实质变化；请先确认变更范围，旧成果已保留')
        for name in ('loop_resource_policy', 'loop_control_policy'):
            policy = state.get(name)
            if policy is not None:
                if not isinstance(policy, dict) or policy.get('contract_hash') != old_hash:
                    raise ValueError('旧运行策略与检查点不匹配，未调整授权')
        aligned = copy.deepcopy(state)
        aligned['contract_hash'] = contract.contract_hash
        aligned['contract_snapshot'] = current_definition
        aligned.setdefault('contract_rebindings', []).append({
            'from_hash': old_hash, 'to_hash': contract.contract_hash, 'at': utc_now(),
            'reason': 'unreviewed_exit_summary_updated', 'previous_exit_condition': old_definition['exit_condition'],
            'current_exit_condition': current_definition['exit_condition']})
        for name in ('loop_resource_policy', 'loop_control_policy'):
            if aligned.get(name) is not None:
                aligned[name]['contract_hash'] = contract.contract_hash
        return aligned

    def set_capability_executor(
        self, run_id: str, *, conversation_id: str, model: dict[str, str] | None,
        expected_updated_at: str, command_id: str, reason: str,
    ) -> dict[str, Any]:
        """Explicitly rebind a paused run's assistance slot without resuming it.

        Model availability is validated by the owning backend before this
        operation. Use its store; other processes must not edit live caches.
        """
        from mewcode.model_registry import ModelRef
        selected = ModelRef.from_value(model).as_dict() if model is not None else None
        if not conversation_id or not expected_updated_at or not command_id.strip() or not reason.strip():
            raise ValueError("辅助执行器变更必须包含对话、检查点版本、命令编号和用户要求")
        with self._lock:
            snapshot = self._cached(run_id)
            if snapshot is None or snapshot.get("conversation_id") != conversation_id:
                raise ValueError("任务不存在或不属于该对话")
            for previous in snapshot.get("capability_executor_changes") or []:
                if previous.get("change_id") == command_id:
                    if previous.get("model") != selected or previous.get("reason") != reason:
                        raise ValueError("相同辅助执行器命令不能更换内容")
                    # A prior durable write may still be waiting or may have
                    # failed after publishing the candidate to the live cache.
                    # Never acknowledge an idempotent retry from cache alone.
                    self._enqueue_write(run_id, durable=True)
                    return copy.deepcopy(self._live[run_id])
            if snapshot.get("updated_at") != expected_updated_at:
                raise ValueError("检查点已变化，请重新核对辅助执行器绑定")
            if (snapshot.get("status") not in {"blocked", "paused", "needs_attention"}
                    or snapshot.get("active_node_ids")):
                raise ValueError("只能修改已暂停且没有执行节点的任务辅助执行器")
            profile = snapshot.get("locked_profile")
            if not isinstance(profile, dict):
                raise ValueError("当前任务缺少锁定模型配置")
            candidate = copy.deepcopy(snapshot)
            old_model = copy.deepcopy(profile.get("capability_executor"))
            backup = self.checkpoint_archive.save(snapshot, turn_key="before-helper:" + command_id,
                                                  kind="before_capability_executor_change")
            if selected is None:
                candidate["locked_profile"].pop("capability_executor", None)
            else:
                candidate["locked_profile"]["capability_executor"] = selected
            now = utc_now()
            change = {"change_id": command_id, "authorized_by": "user", "authorized_at": now,
                      "reason": reason, "previous_model": old_model, "model": selected,
                      "checkpoint_id": backup["checkpoint_id"]}
            candidate.setdefault("capability_executor_changes", []).append(change)
            candidate["updated_at"] = now
            candidate["last_seq"] = int(snapshot.get("last_seq") or 0) + 1
            candidate.setdefault("events", []).append({
                "type": "expert_capability_executor_changed", "run_id": run_id,
                "conversation_id": conversation_id, "seq": candidate["last_seq"],
                "server_time": now, "attempt": snapshot.get("attempt"), **change,
            })
            self._write(candidate)
            return copy.deepcopy(candidate)

    def set_verifier_model(self, run_id: str, *, conversation_id: str, model: dict[str, str],
                           catalog: dict, expected_updated_at: str, command_id: str) -> dict[str, Any]:
        """Apply an explicit default-verifier choice without rewriting old results."""
        from mewcode.kernel.models import RoleModels, rebind_role
        from mewcode.kernel.collaboration_recovery import unresolved_calls
        pool = RoleModels(catalog)
        selected = pool.require(model, 'verifier').as_dict()
        if not conversation_id or not expected_updated_at or not command_id.strip():
            raise ValueError('核查模型变更缺少对话、检查点版本或命令编号')
        with self._lock:
            snapshot = self._cached(run_id)
            if not snapshot or snapshot.get('conversation_id') != conversation_id:
                raise ValueError('任务不存在或不属于该对话')
            for change in snapshot.get('verifier_model_changes') or []:
                if change['change_id'] == command_id:
                    if change['model'] != selected:
                        raise ValueError('相同核查模型命令不能更换内容')
                    self._enqueue_write(run_id, durable=True)
                    return copy.deepcopy(self._live[run_id])
            if snapshot.get('updated_at') != expected_updated_at:
                raise ValueError('检查点已变化，请重新读取核查模型')
            if snapshot.get('status') not in {'paused', 'blocked', 'needs_attention'} or snapshot.get('active_node_ids'):
                raise ValueError('请先暂停任务，再修改核查模型')
            value = snapshot.get('collaboration')
            if not value or not (snapshot.get('role_models') or {}).get('roles', {}).get('verifier'):
                raise ValueError('当前任务没有协作核查角色')
            if (unresolved_calls(snapshot, list(value['contracts'])) or any(
                    call.get('status') in {'dispatched', 'unknown'}
                    for call in value.get('calls', {}).values())):
                raise ValueError('仍有未结算调用，请先核对执行结果')
            candidate = copy.deepcopy(snapshot)
            backup = self.checkpoint_archive.save(snapshot, turn_key='before-verifier:' + command_id,
                                                  kind='before_verifier_model_change')
            rebind_role(candidate, pool=pool, role='verifier', model=selected, source_ref='user:' + command_id)
            candidate['role_models']['explicit_roles'] = sorted(set(candidate['role_models'].get('explicit_roles') or []) | {'verifier'})
            candidate['locked_profile']['auditor'] = copy.deepcopy(selected)
            candidate['locked_profile'].setdefault('role_models', {})['verifier'] = copy.deepcopy(selected)
            now = utc_now()
            change = dict(change_id=command_id, authorized_by='user', authorized_at=now, model=selected,
                previous_model=copy.deepcopy(snapshot['role_models']['roles']['verifier']), checkpoint_id=backup['checkpoint_id'])
            candidate.setdefault('verifier_model_changes', []).append(change)
            candidate.update(updated_at=now, last_seq=int(snapshot.get('last_seq') or 0) + 1)
            candidate.setdefault('events', []).append(dict(type='expert_verifier_model_changed', run_id=run_id,
                conversation_id=conversation_id, seq=candidate['last_seq'], server_time=now, **change))
            self._write(candidate)
            return copy.deepcopy(candidate)

    def set_role_models(self, run_id: str, *, conversation_id: str, models: dict,
                        catalog: dict, expected_updated_at: str, command_id: str, reason: str):
        """Atomically apply exact host-user choices to a stopped run's roles."""
        from mewcode.kernel.models import ROLES, RoleModels, rebind_role
        from mewcode.kernel.collaboration_recovery import unresolved_calls
        if (not isinstance(models, dict) or not models or set(models) - set(ROLES)
                or not conversation_id or not isinstance(expected_updated_at, str) or not expected_updated_at
                or not isinstance(command_id, str) or not command_id.strip()
                or not isinstance(reason, str) or not reason.strip()):
            raise ValueError('角色模型变更需要明确角色、检查点版本和用户要求')
        pool = RoleModels(catalog)
        selected = {role: pool.require(model, role).as_dict() for role, model in models.items()}
        with self._lock:
            snapshot = self._cached(run_id)
            if not snapshot or snapshot.get('conversation_id') != conversation_id:
                raise ValueError('任务不存在或不属于该对话')
            for previous in snapshot.get('role_model_binding_changes') or []:
                if previous['change_id'] == command_id:
                    if previous['models'] != selected or previous['reason'] != reason:
                        raise ValueError('相同角色换绑命令不能更换内容')
                    self._enqueue_write(run_id, durable=True)
                    return copy.deepcopy(self._live[run_id])
            if snapshot.get('updated_at') != expected_updated_at:
                raise ValueError('检查点已变化，请重新核对角色模型')
            if snapshot.get('status') not in {'paused', 'blocked', 'needs_attention'} or snapshot.get('active_node_ids'):
                raise ValueError('请先暂停任务，再修改角色模型')
            value = snapshot.get('collaboration')
            roles = (snapshot.get('role_models') or {}).get('roles') or {}
            if not value or not set(selected) <= set(roles):
                raise ValueError('当前任务没有对应的协作角色')
            if (unresolved_calls(snapshot, list(value['contracts'])) or any(
                    call.get('status') in {'dispatched', 'unknown'} for call in value.get('calls', {}).values())):
                raise ValueError('仍有未结算调用，请先核对执行结果')
            candidate = copy.deepcopy(snapshot)
            backup = self.checkpoint_archive.save(snapshot, turn_key='before-roles:' + command_id,
                                                  kind='before_role_model_change')
            for role, model in selected.items():
                rebind_role(candidate, pool=pool, role=role, model=model, source_ref='user:' + command_id + ':' + role)
                candidate['locked_profile'].setdefault('role_models', {})[role] = copy.deepcopy(model)
                profile_key = {'planner': 'manager', 'executor': 'executor', 'verifier': 'auditor'}.get(role)
                if profile_key:
                    candidate['locked_profile'][profile_key] = copy.deepcopy(model)
            candidate['role_models']['explicit_roles'] = sorted(set(candidate['role_models'].get('explicit_roles') or []) | set(selected))
            now = utc_now()
            change = {'change_id': command_id, 'authorized_by': 'user', 'authorized_at': now, 'reason': reason,
                'models': selected, 'previous_models': {role: copy.deepcopy(roles[role]) for role in selected},
                'checkpoint_id': backup['checkpoint_id']}
            candidate.setdefault('role_model_binding_changes', []).append(change)
            candidate.update(updated_at=now, last_seq=int(snapshot.get('last_seq') or 0) + 1)
            candidate.setdefault('events', []).append({'type': 'expert_role_models_changed', 'run_id': run_id,
                'conversation_id': conversation_id, 'seq': candidate['last_seq'], 'server_time': now, **change})
            self._write(candidate)
            return copy.deepcopy(candidate)

    def set_difficult_node_model(
        self, run_id: str, *, conversation_id: str, model: dict[str, str],
        node_ids: list[str], apply_node_ids: list[str], expected_updated_at: str,
        command_id: str, reason: str, record_backup: bool = True,
    ) -> dict[str, Any]:
        """Record an explicit recovery route and optionally rebind paused nodes.

        The owning host validates availability first. This never resumes work,
        clears blockers, resets costs, or changes the frozen business contract.
        """
        from mewcode.model_registry import ModelRef
        selected = ModelRef.from_value(model).as_dict()
        scope, immediate = sorted(set(node_ids)), sorted(set(apply_node_ids))
        if (not conversation_id or not expected_updated_at or not command_id.strip() or not reason.strip()
                or not scope or not set(immediate) <= set(scope) or not record_backup and scope != immediate):
            raise ValueError("困难节点模型变更须包含当前版本、用户要求及明确节点范围")
        with self._lock:
            snapshot = self._cached(run_id)
            if snapshot is None or snapshot.get("conversation_id") != conversation_id:
                raise ValueError("任务不存在或不属于该对话")
            for previous in snapshot.get("difficult_node_model_changes") or []:
                if previous.get("change_id") == command_id:
                    if any(previous.get(key) != expected for key, expected in (
                            ("model", selected), ("node_ids", scope), ("apply_node_ids", immediate), ("reason", reason))) or previous.get("record_backup", True) != record_backup:
                        raise ValueError("相同困难节点模型命令不能更换内容")
                    self._enqueue_write(run_id, durable=True)
                    return copy.deepcopy(self._live[run_id])
            if snapshot.get("updated_at") != expected_updated_at:
                raise ValueError("检查点已变化，请重新核对困难节点模型")
            if (snapshot.get("status") not in {"paused", "blocked", "needs_attention"}
                    or snapshot.get("active_node_ids")):
                raise ValueError("只能调整已暂停且没有执行节点的任务模型")
            from mewcode.kernel.collaboration_graph import effective_flow
            from mewcode.kernel.collaboration import valid_certificate
            from mewcode.kernel.collaboration_recovery import unresolved_calls
            from mewcode.experts.model_route_recovery import current_node_model
            nodes = {row.node_id: row.as_dict() for row in effective_flow(snapshot).nodes if row.kind == "role"}
            if not set(scope) <= set(nodes):
                raise ValueError("只能配置当前锁定流程中的角色节点")
            for node_id in immediate:
                collaboration = snapshot.get("collaboration") or {}
                if ((snapshot.get("node_states") or {}).get(node_id, {}).get("status") == "completed"
                        or collaboration and valid_certificate(collaboration, node_id)):
                    raise ValueError("已完成节点不能通过困难模型配置重新绑定或重跑")
                if collaboration.get("contracts", {}).get(node_id, {}).get("join_children"):
                    raise ValueError("汇合节点由宿主处理，请修改实际执行的子节点模型")
            if unresolved_calls(snapshot, immediate):
                raise ValueError("节点仍有未结算调用，请先核对执行结果后修改模型")
            candidate = copy.deepcopy(snapshot)
            backup = self.checkpoint_archive.save(snapshot, turn_key="before-difficulty:" + command_id,
                                                  kind="before_difficult_node_model_change")
            now = utc_now()
            change = {"change_id": command_id, "authorized_by": "user", "authorized_at": now,
                      "reason": reason, "model": selected, "node_ids": scope, "apply_node_ids": immediate,
                      "record_backup": record_backup,
                      "flow_fingerprint": snapshot.get("flow_fingerprint"), "checkpoint_id": backup["checkpoint_id"],
                      "previous_models": {}, "previous_policy": copy.deepcopy(snapshot.get("difficult_node_model_policy"))}
            for node_id in immediate:
                node = nodes[node_id]
                old = current_node_model(snapshot, node)
                change["previous_models"][node_id] = copy.deepcopy(old)
                candidate.setdefault("locked_models", {})[node_id] = copy.deepcopy(selected)
                if candidate.get("collaboration"):
                    candidate.setdefault("collaboration_model_changes", []).append({"node_id": node_id,
                        "before": copy.deepcopy(old), "after": copy.deepcopy(selected),
                        "source": "user", "decision_id": command_id})
            if record_backup:
                candidate["difficult_node_model_policy"] = {key: copy.deepcopy(change[key]) for key in (
                    "change_id", "authorized_by", "authorized_at", "reason", "model", "node_ids", "flow_fingerprint")}
            candidate.setdefault("difficult_node_model_changes", []).append(change)
            candidate.update(updated_at=now, last_seq=int(snapshot.get("last_seq") or 0) + 1)
            candidate.setdefault("events", []).append({"type": "expert_difficult_node_model_changed",
                "run_id": run_id, "conversation_id": conversation_id, "seq": candidate["last_seq"],
                "server_time": now, "model": selected, "apply_node_ids": immediate, "change_id": command_id})
            self._write(candidate)
            return copy.deepcopy(candidate)

    def set_loop_control_mode(self, run_id: str, *, mode: str, expected_updated_at: str,
                              command_id: str, reason: str) -> dict[str, Any]:
        """User-authorized run policy, separate from targets and acceptance.

        Applies to this run's loops (including future nested iterations).
        Does not resume, call a provider, clear usage, or edit workspace files.
        """
        from mewcode.orchestration.loops import LoopContract, LoopWatchdog
        if mode not in {"team", "strict"} or not command_id.strip() or not reason.strip():
            raise ValueError("请指定运行模式、命令编号和用户要求")

        def apply(snapshot):
            previous = snapshot.get("loop_control_policy") or {}
            if previous.get("change_id") == command_id:
                if previous.get("mode") != mode or previous.get("reason") != reason:
                    raise ValueError("相同命令不能更换内容")
                return
            if snapshot.get("updated_at") != expected_updated_at:
                raise ValueError("检查点已变化，请重新核对")
            if snapshot.get("status") not in {"blocked", "paused", "needs_attention"} or snapshot.get("active_node_ids"):
                raise ValueError("只能调整已暂停且没有执行节点的任务")
            definitions = {item["loop_id"]: LoopContract.from_value(item)
                           for item in (snapshot.get("locked_flow") or {}).get("meta", {}).get("loops", [])}
            if not definitions:
                raise ValueError("当前任务没有协作循环")
            states = copy.deepcopy(snapshot.get("loop_states") or {})
            change = {"mode": mode, "authorized_by": "user", "change_id": command_id,
                      "authorized_at": utc_now(), "reason": reason}
            for key, state in states.items():
                contract = definitions.get(key.split("/")[-1])
                if contract is None or state.get("active_at"):
                    raise ValueError("循环定义不匹配或仍有未结算执行")
                state = self._align_unreviewed_loop_summary(run_id, contract, state)
                states[key] = state
                LoopWatchdog(contract, state)
                state["loop_control_policy"] = {**change, "contract_hash": contract.contract_hash}
                if mode == "strict":
                    state.pop("loop_resource_policy", None)
                    state["budget_resume_pending"] = False
            backup = self.checkpoint_archive.save(snapshot, turn_key="before-control:" + command_id,
                                                  kind="before_control_change")
            change["checkpoint_id"] = backup["checkpoint_id"]
            snapshot["loop_states"] = states
            snapshot["loop_control_policy"] = change
            snapshot.setdefault("loop_budget_changes", []).append({**change, "previous": previous})

        return self.mutate(run_id, apply, durable=True)

    def resume(
        self,
        run_id: str,
        *,
        strategy: str,
        supplemental: str = "",
        resume_after_node: str = "",
        retry_node_id: str = "",
        flow: ExpertFlowSpec | dict[str, Any] | None = None,
        recovery_action: str = "",
        recovery_info: str = "",
        grant_extra_budget: bool = False,
    ) -> dict[str, Any]:
        anchor = str(resume_after_node or "").strip()
        retry = str(retry_node_id or "").strip()
        action = str(recovery_action or "").strip()
        if anchor and retry:
            raise ValueError("重试失败节点不能同时指定重跑后续阶段")
        replacement_flow = ExpertFlowSpec.from_value(flow) if flow is not None else None
        replacement_fingerprint = (
            expert_flow_fingerprint(replacement_flow)
            if replacement_flow is not None else ""
        )

        def apply_resume(snapshot: dict[str, Any]) -> None:
            nonlocal replacement_flow, replacement_fingerprint
            from mewcode.kernel.collaboration import flow_mode
            from mewcode.kernel.collaboration_graph import effective_flow
            if snapshot.get("collaboration", {}).get("runtime_flow") and replacement_flow is not None:
                if replacement_fingerprint != expert_flow_fingerprint(snapshot["locked_flow"]):
                    raise ValueError("此任务已有已发布子图，请通过当前任务的要求修订入口修改")
            if replacement_flow is not None and flow_mode(replacement_flow.as_dict()) != snapshot.get("orchestration_mode", "classic"):
                raise ValueError("恢复不能切换编排模式")
            if (replacement_flow is not None and snapshot.get('assistance_flow_revision')
                    and _saved_plan_fingerprint(replacement_flow) == snapshot.get('saved_plan_fingerprint')):
                # The canvas did not change. Keep the accepted run-local patch
                # across an ordinary Continue instead of restoring its old node.
                replacement_flow = ExpertFlowSpec.from_value(snapshot['locked_flow'])
                replacement_fingerprint = expert_flow_fingerprint(replacement_flow)
            if replacement_flow is not None:
                from mewcode.orchestration.maintenance_revision import require_adopted_maintenance
                adoption = require_adopted_maintenance(snapshot, replacement_flow)
                if adoption and adoption.get("status") == "choice_required":
                    from mewcode.experts.maintenance_adoption import MaintenanceChoiceRequired
                    raise MaintenanceChoiceRequired(adoption)
            was_resumable = is_resumable_snapshot(snapshot)
            was_completed = str(snapshot.get("status") or "") == "completed"
            states_before = snapshot.get("node_states") or snapshot.get("stages") or {}
            # Nothing is paused to continue when all executed stages ended
            # but final acceptance failed. Require the existing explicit retry
            # control instead of silently repeating audits and human gates.
            if not (anchor or retry or action) and was_resumable:
                unresolved_before = unresolved_audits(snapshot.get("locked_flow") or {}, states_before)
                same_flow = replacement_flow is None or replacement_fingerprint == snapshot.get("flow_fingerprint")
                if same_flow and acceptance_retry_required(snapshot):
                    raise ValueError("执行阶段已结束，但验收未通过；请明确重试待处理审查节点：" + ", ".join(unresolved_before))
            pending_revision = snapshot.get("pending_recovery_revision")
            if pending_revision and not action and not retry:
                raise ValueError("有待批准的计划修订，请先在修订卡中决定")
            if action:
                apply_recovery_decision(
                    snapshot, action=action, info=recovery_info,
                    grant_extra_budget=grant_extra_budget,
                )
            if retry and pending_revision:
                snapshot.pop("pending_recovery_revision", None)
                for protocol in (snapshot.get("audit_protocols") or {}).values():
                    if isinstance(protocol, dict):
                        protocol.pop("pending_recovery_revision", None)
            if retry:
                states = snapshot.get("node_states") or snapshot.get("stages") or {}
                failed = states.get(retry) or {}
                failed_audits = unresolved_audits(snapshot.get("locked_flow") or {}, states)
                if not was_resumable or (failed.get("status") not in {
                    "continuing", "failed", "stalled", "needs_attention", "blocked", "budget_exhausted", "paused",
                } and retry not in failed_audits):
                    raise ValueError("只能重试当前受阻任务中的失败或待处理节点；已完成节点不会重跑")
                retry_flow = effective_flow(snapshot) if snapshot.get("collaboration") else (replacement_flow or ExpertFlowSpec.from_value(snapshot["locked_flow"]))
                if retry not in retry_flow.node_map:
                    raise ValueError("最新编排已移除该失败节点，未重置进度；请先核对编排")
                if any(retry in loop.get("node_ids", []) for loop in (retry_flow.meta or {}).get("loops", [])):
                    raise ValueError("循环体节点请通过循环检查点恢复，不能从中途重试")
            if (
                replacement_flow is not None
                and replacement_fingerprint != str(
                    snapshot.get("flow_fingerprint")
                    or expert_flow_fingerprint(
                        snapshot.get("locked_flow") or DEFAULT_EXPERT_FLOW
                    )
                )
            ):
                previous_flow = ExpertFlowSpec.from_value(
                    snapshot.get("locked_flow") or DEFAULT_EXPERT_FLOW
                )
                valid_ids = set(replacement_flow.node_map)
                states = snapshot.get("node_states") or snapshot.get("stages") or {}
                lost_completed = [str(node_id) for node_id, state in states.items()
                                  if isinstance(state, dict) and state.get("status") == "completed"
                                  and str(node_id) not in valid_ids]
                if lost_completed:
                    raise ValueError("最新编排缺少已完成节点，已阻止恢复以保护进度：" + ", ".join(lost_completed))
                kept_states = {
                    str(node_id): copy.deepcopy(state)
                    for node_id, state in states.items()
                    if str(node_id) in valid_ids and isinstance(state, dict)
                }
                snapshot["node_states"] = kept_states
                snapshot["stages"] = kept_states
                for key in ("node_attempts", "pipeline_runs"):
                    values = snapshot.get(key)
                    if isinstance(values, dict):
                        snapshot[key] = {
                            str(node_id): item
                            for node_id, item in values.items()
                            if str(node_id) in valid_ids
                        }
                members = snapshot.get("members")
                if isinstance(members, dict):
                    snapshot["members"] = {
                        member_id: member
                        for member_id, member in members.items()
                        if not isinstance(member, dict)
                        or str(
                            member.get("node_id")
                            or member.get("parent_node")
                            or ""
                        ) in valid_ids
                    }
                snapshot.update({
                    "locked_flow": replacement_flow.as_dict(),
                    "flow_id": replacement_flow.flow_id,
                    "flow_version": replacement_flow.version,
                    "flow_fingerprint": replacement_fingerprint,
                    "saved_plan_fingerprint": _saved_plan_fingerprint(replacement_flow),
                    "finalizer_node_id": replacement_flow.finalizer_node_id,
                    "active_node_ids": [],
                    "handoffs": [],
                    # Node bindings are part of the edited graph.  Old per-node
                    # locks must not override models selected on the canvas.
                    "locked_models": {},
                    "recovery_locked": False,
                })
                if str(snapshot.get("stage_cursor") or "") not in valid_ids:
                    entries = replacement_flow.entry_node_ids()
                    snapshot["stage_cursor"] = entries[0] if entries else ""
                snapshot.pop("flow_state", None)
                snapshot.setdefault("flow_revisions", []).append({
                    "at": utc_now(),
                    "from_flow_id": previous_flow.flow_id,
                    "from_version": previous_flow.version,
                    "from_fingerprint": expert_flow_fingerprint(previous_flow),
                    "to_flow_id": replacement_flow.flow_id,
                    "to_version": replacement_flow.version,
                    "to_fingerprint": replacement_fingerprint,
                })
            if anchor:
                node_states = snapshot.get("node_states") or snapshot.get("stages") or {}
                stage = node_states.get(anchor) if isinstance(node_states, dict) else None
                if not isinstance(stage, dict) or stage.get("status") != "completed":
                    raise ValueError(
                        f"resume_after_node requires completed anchor: {anchor}"
                    )
                if not (
                    was_resumable or was_completed
                    or str(snapshot.get("status") or "") == "terminated"
                ):
                    raise ValueError("expert run is not resumable for resume_after_node")
                succ = successors_after_node(effective_flow(snapshot), anchor)
                for loop in (snapshot.get("locked_flow") or {}).get("meta", {}).get("loops") or []:
                    if anchor in loop["node_ids"]:
                        raise ValueError("不能从循环体中途重跑；请选循环入口之前的已完成阶段，或直接恢复当前检查点")
                    if set(loop["node_ids"]) & set(succ):
                        for state_key in list(snapshot.get("loop_states") or {}):
                            if state_key == loop["loop_id"] or state_key.startswith(loop["loop_id"] + "/"):
                                old_loop = snapshot["loop_states"].pop(state_key)
                                snapshot.setdefault("loop_restarts", []).append({"at": utc_now(), "anchor": anchor,
                                    "state_key": state_key, "previous": old_loop})
                for node_id in succ:
                    node_states.pop(node_id, None)
                    old_queue = (snapshot.get("review_queues") or {}).pop(node_id, None)
                    if old_queue:
                        snapshot.setdefault("review_queue_archives", []).append({
                            "node_id": node_id, "anchor": anchor, "queue": old_queue})
                stages = snapshot.get("stages")
                if isinstance(stages, dict):
                    for node_id in succ:
                        stages.pop(node_id, None)
                pipeline_runs = snapshot.get("pipeline_runs")
                if isinstance(pipeline_runs, dict):
                    for node_id in succ:
                        pipeline_runs.pop(node_id, None)
                node_attempts = snapshot.get("node_attempts")
                if isinstance(node_attempts, dict):
                    for node_id in succ:
                        node_attempts.pop(node_id, None)
                members = snapshot.get("members")
                if isinstance(members, dict):
                    for member_id, member in list(members.items()):
                        if not isinstance(member, dict):
                            continue
                        owner = str(member.get("node_id") or member.get("parent_node") or "")
                        if owner in succ:
                            members.pop(member_id, None)
                snapshot["resume_after_node"] = anchor
            elif not was_resumable:
                raise ValueError("expert run is not resumable")
            else:
                snapshot.pop("resume_after_node", None)
            snapshot.pop("retry_node_id", None)
            if not anchor:
                affected = acceptance_recheck_nodes(
                    effective_flow(snapshot).as_dict(), snapshot.get("node_states") or snapshot.get("stages") or {},
                    retry=retry,
                )
                if affected:
                    snapshot.setdefault("acceptance_rechecks", []).append({
                        "at": utc_now(), "node_ids": affected,
                        "previous": {node_id: copy.deepcopy((snapshot.get("node_states") or {}).get(node_id))
                                     for node_id in affected},
                    })
                    for key in ("node_states", "stages", "pipeline_runs", "gate_confirmations"):
                        for node_id in affected:
                            (snapshot.get(key) or {}).pop(node_id, None)
                    # Restart conversation/acceptance, not the knowledge of
                    # prior writes and failures. Never replay old tool calls.
                    from mewcode.experts.attempt_memory import summarize_attempt
                    previous = snapshot["acceptance_rechecks"][-1]["previous"]
                    for node_id, old in previous.items():
                        if not isinstance(old, dict):
                            continue
                        history = list(old.get("attempt_history") or [summarize_attempt(old)])[-3:]
                        for key in ("node_states", "stages"):
                            snapshot.setdefault(key, {})[node_id] = {
                                "status": "pending", "attempt_history": copy.deepcopy(history),
                            }
                    snapshot["stage_cursor"] = retry or affected[0]
            if retry:
                snapshot["retry_node_id"] = retry
                if snapshot.get('collaboration'):
                    from mewcode.kernel import collaboration as collaboration_ledger
                    current = snapshot['collaboration']
                    if collaboration_ledger.valid_certificate(current, retry):
                        collaboration_ledger.invalidate(current, {retry}, '用户明确要求重新核验当前受阻节点')
                        current.setdefault('retry_generations', {})[retry] = current.get('retry_generations', {}).get(retry, 0) + 1
                    else:
                        prior = collaboration_ledger.attempts_for(current, retry)
                        if prior and prior[-1]['status'] == 'rejected' and prior[-1].get('repair_stalled'):
                            # Only an explicit targeted retry releases a saved
                            # no-progress repair; ordinary Continue retains it.
                            current.setdefault('retry_generations', {})[retry] = current.get('retry_generations', {}).get(retry, 0) + 1
                # A user-clicked retry grants a fresh bounded repair attempt,
                # not a reset of successful stages or unrelated audit loops.
                for node in effective_flow(snapshot).nodes:
                    if node.node_id != retry and node.failure_policy.target_node_id != retry:
                        continue
                    protocol = (snapshot.get("audit_protocols") or {}).get(node.node_id)
                    if isinstance(protocol, dict):
                        snapshot.setdefault("audit_restarts", []).append({
                            "at": utc_now(), "node_id": node.node_id, "retry_node_id": retry,
                            "previous": copy.deepcopy(protocol),
                        })
                        snapshot["audit_protocols"][node.node_id] = {
                            "rubric": copy.deepcopy(protocol.get("rubric") or {}),
                            "round": 0, "no_progress": 0, "seen_candidates": [],
                            **{key: copy.deepcopy(protocol[key]) for key in AUDIT_RETRY_RETAIN_KEYS
                               if key in protocol},
                        }
            # Apply only after the plan/acceptance/recovery validation above;
            # a rejected Continue must not unblock the cached loop in memory.
            if not (anchor or retry or action) and was_resumable:
                from mewcode.orchestration.loops import LoopContract, LoopWatchdog
                from mewcode.experts.loop_recovery import reopen_team_checkpoint, unfinished_roots, invalidate_results, invalidate_nested_inputs
                # Same atomic transaction as validated Continue. A failed plan
                # or acceptance check must not retire a scheduling wait.
                loop_flow = ExpertFlowSpec.from_value(snapshot["locked_flow"])
                loop_definitions = {item["loop_id"]: item for item in (snapshot.get("locked_flow") or {}).get("meta", {}).get("loops", [])}
                for state_key, loop_state in list((snapshot.get('loop_states') or {}).items()):
                    definition = loop_definitions.get(state_key.split('/')[-1])
                    if definition:
                        snapshot['loop_states'][state_key] = self._align_unreviewed_loop_summary(
                            run_id, LoopContract.from_value(definition), loop_state)
                # An explicit Continue resumes work, not a cached failure. A
                # team checkpoint may have stale downstream "completed" rows
                # produced from an unfinished upstream result by older versions.
                for state_key, loop_state in (snapshot.get("loop_states") or {}).items():
                    definition = loop_definitions.get(state_key.split("/")[-1])
                    if not definition:
                        continue
                    contract = LoopContract.from_value(definition)
                    from mewcode.kernel.work_continuation import restore_completed_loop_delivery
                    if restore_completed_loop_delivery(snapshot, loop_state, contract):
                        continue
                    # A cancelled recovery dialogue can leave the outer run
                    # paused while its loop still says running. Repair that
                    # checkpoint too, after the same resume authorization above.
                    stale_running = (loop_state.get("status") in {"running", "paused"}
                                     and bool(unfinished_roots(loop_flow, contract, loop_state)))
                    if loop_state.get("status") != "blocked" and not stale_running:
                        continue
                    watchdog = LoopWatchdog(contract, loop_state)
                    if not watchdog.team_mode:
                        continue
                    affected = reopen_team_checkpoint(loop_flow, contract, loop_state)
                    invalidate_nested_inputs(loop_flow, {key: LoopContract.from_value(value) for key, value in loop_definitions.items()},
                        snapshot['loop_states'], state_key, affected, loop_state['recovery_history'][-1]['roots'])
                    for collection in ("node_states", "stages"):
                        for node_id in affected:
                            old = (snapshot.get(collection) or {}).get(node_id)
                            if isinstance(old, dict):
                                old.update(status="pending", status_reason="旧输入已失效，等待重新交接")
                    snapshot["stage_cursor"] = loop_state["next_node_id"]
                for loop_state in (snapshot.get("loop_states") or {}).values():
                    if loop_state.get("status") == "blocked" and not loop_state.get("error_kind"):
                        interrupted = [(node_id, result) for node_id, result in loop_state.get("round_results", {}).items()
                                       if result.get("status") in {"failed", "needs_attention"}
                                       and "provider stalled: no response activity for " in str(result.get("text") or "")]
                        if len(interrupted) == 1:
                            loop_state.update(error_kind="provider_interrupted", failed_node_id=interrupted[0][0])
                    if loop_state.get("status") == "blocked" and loop_state.get("error_kind") == "provider_interrupted":
                        failed_id = loop_state.get("failed_node_id")
                        loop_state.get("round_results", {}).pop(failed_id, None)
                        loop_state.update(status="running", reason="", interrupted_node=failed_id, error_kind="", failed_node_id="")
                for state_key, loop_state in (snapshot.get("loop_states") or {}).items():
                    if not loop_state.get("budget_resume_pending") or state_key not in loop_definitions:
                        continue
                    contract = LoopContract.from_value(loop_definitions[state_key])
                    watchdog = LoopWatchdog(contract, loop_state)
                    if (watchdog.token_limit is None and loop_state.get("status") == "blocked"
                            and loop_state.get("error_kind") == "budget_exhausted"
                            and loop_state["elapsed"] < contract.timeout_seconds
                            and loop_state["stalled"] < contract.max_stalled_rounds
                            and (loop_state.get("round_open") or not watchdog.remaining())):
                        loop_state.update(status="running", reason="", error_kind="", failed_node_id="",
                                          budget_resume_pending=False)
                        # A released cap must resume the incomplete producer,
                        # never pass its old budget_exhausted output downstream.
                        roots = unfinished_roots(loop_flow, contract, loop_state)
                        if roots:
                            invalidate_results(loop_flow, contract, loop_state, roots, reason="resource_limit_released")
                            loop_state["interrupted_nodes"] = sorted(set(loop_state.get("interrupted_nodes") or []) | set(roots))
                for loop_state in (snapshot.get("loop_states") or {}).values():
                    if loop_state.get("status") == "blocked" and loop_state.get("error_kind") == "provider_error":
                        failed_id = loop_state.get("failed_node_id")
                        rejected = loop_state.get("round_results", {}).get(failed_id)
                        if _unexecuted_output_cap_rejection(rejected):
                            loop_state.setdefault("request_rejection_history", []).append({
                                "node_id": failed_id, "result": copy.deepcopy(rejected),
                                "resumed_at": utc_now(), "action": "explicit_continue",
                            })
                            loop_state["round_results"].pop(failed_id)
                            # No model turn ran: use a fresh request, not the
                            # interrupted-message replay path. Keep this round,
                            # completed siblings, elapsed time and token ledger.
                            loop_state.update(status="running", reason="", error_kind="", failed_node_id="")
                blocked = next((state for state in (snapshot.get("loop_states") or {}).values()
                                if state.get("status") == "blocked"), None)
                if blocked is not None:
                    raise ValueError("未启动新调用：" + str(blocked.get("reason") or "严格模式的暂停条件尚未解除")
                                     + "。请在运行方式中调整限额策略，或处理具体问题后再继续；已有进度未改动。")
            old_strategy = str(snapshot.get("strategy") or "stable")
            snapshot["attempt"] = int(snapshot.get("attempt") or 1) + 1
            if not anchor and snapshot.get('collaboration'):
                from mewcode.kernel.acceptance import retry_malformed_review
                for saved_attempt in snapshot['collaboration']['attempts'].values():
                    if not retry or saved_attempt['contract_id'] == retry:
                        retry_malformed_review(snapshot, saved_attempt)
            snapshot["status"] = "running"
            snapshot["status_reason"] = ""
            snapshot["completed_at"] = None
            snapshot["flow_state"] = {**(snapshot.get("flow_state") or {}),
                                      "phase": "resuming", "status": "running", "reason": "",
                                      "attempt": snapshot["attempt"]}
            snapshot.pop("plan_revision_pending", None)
            snapshot["resume_transition"] = f"{old_strategy}->{strategy}"
            intervals = snapshot.setdefault("paused_intervals", [])
            if intervals and intervals[-1].get("ended_at") is None:
                intervals[-1]["ended_at"] = utc_now()
            if supplemental.strip():
                snapshot.setdefault("supplemental_requirements", []).append({
                    "attempt": snapshot["attempt"],
                    "text": supplemental.strip(),
                    "created_at": utc_now(),
                })
            # A successful resume consumes the paused-dialogue window. A new
            # anchor is written only if this new attempt stops again.
            snapshot.pop("pause_dialogue_anchor", None)
            snapshot.pop("pending_dialogue_handoff", None)
            for child in (snapshot.get("children") or {}).values():
                if not isinstance(child, dict):
                    continue
                completed = child.get("status") == "completed"
                if old_strategy == "fast" and strategy == "stable" and not completed:
                    child["status"] = "stopped_for_stable_takeover"
                    child["resume_action"] = "executor_integrates_checkpoint"
                elif old_strategy == "fast" and strategy == "fast":
                    child["resume_action"] = "skip_completed" if completed else "resume_checkpoint"
                    if not completed:
                        child["attempt"] = snapshot["attempt"]
                        child["status"] = "resume_pending"
            self._start_attempt_inplace(snapshot, strategy)

        # mutate already supplies a detached candidate and publishes it only
        # after validation and commit. A second whole-run copy adds no isolation.
        return self.mutate(run_id, apply_resume, durable=True)

    def reject_recovery_revision(self, run_id: str, *, expected=None) -> dict[str, Any]:
        def apply(snapshot: dict[str, Any]) -> None:
            if expected is not None and snapshot.get('pending_recovery_revision') != expected:
                raise ValueError('该事项已经处理或已过期，请以当前显示的内容为准')
            apply_recovery_decision(snapshot, action="reject")

        return self.mutate(run_id, apply, durable=True)

    def transition(
        self, run_id: str, status: str, *, reason: str = ""
    ) -> dict[str, Any]:
        def apply(snapshot: dict[str, Any]) -> None:
            if status == "completed" and snapshot.get("orchestration_mode") == "collaborative":
                from mewcode.kernel.collaboration import state, final_ready
                current = state(snapshot)
                if not final_ready(current):
                    raise ValueError("最终验收已失效或仍有待处理修订，不能将协作任务标为完成。")
            previous = snapshot.get("status")
            snapshot["status"] = status
            snapshot["status_reason"] = reason
            if status in STOPPED:
                # Run-level state is authoritative.  A worker that disappears
                # cannot emit its usual final per-node event, so normalize any
                # leftover active node/member here instead of exposing a
                # paused run whose current stage still says ``running``.
                for bucket_name in ("node_states", "stages", "members"):
                    bucket = snapshot.get(bucket_name)
                    if not isinstance(bucket, dict):
                        continue
                    for item in bucket.values():
                        if isinstance(item, dict) and (item.get("status") == "running"
                                or (status == "terminated" and item.get("status") == "paused")):
                            item["status"] = "needs_attention" if status == "completed" else status
                snapshot["active_node_ids"] = []
            attempts = snapshot.get("attempts") or []
            if attempts and attempts[-1].get("ended_at") is None:
                attempts[-1]["ended_at"] = utc_now()
                attempts[-1]["status"] = status
            if status == "paused" and previous != "paused":
                snapshot.setdefault("paused_intervals", []).append({
                    "started_at": utc_now(), "ended_at": None,
                })
            if status in TERMINAL_STATUSES:
                snapshot["completed_at"] = utc_now()

        return self.mutate(run_id, apply)

    def append_event(self, run_id: str, event: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            snapshot = self._cached(run_id)
            if snapshot is None:
                raise KeyError(run_id)
            snapshot = copy.deepcopy(snapshot)
            last_seq = int(snapshot.get("last_seq") or 0)
            payload = compact_expert_event(event)
            seq = max(int(payload.get("seq") or 0), last_seq + 1)
            payload["seq"] = seq
            payload.setdefault("server_time", utc_now())
            journal_events = [] if payload.get("type") in VOLATILE_EVENT_TYPES else [payload]
            from mewcode.kernel.events import EventJournal
            if not EventJournal(self.root, run_id).sqlite:
                self._append_event_log(run_id, journal_events, seq)
            snapshot["last_seq"] = seq
            snapshot.setdefault("events", []).extend(journal_events)
            for item in journal_events:
                _project_tool_lifecycle_event(snapshot, item)
            snapshot["updated_at"] = utc_now()
            _compact_snapshot_events(snapshot)
            self._commit(snapshot, kind='host_event')
            self._live[run_id] = snapshot
            self._after_event_log_append(run_id, snapshot)
            return copy.deepcopy(snapshot)

    def _after_event_log_append(self, run_id: str, snapshot: dict[str, Any]) -> None:
        """Refresh the small UI sidecar; periodically checkpoint journal state."""
        from mewcode.kernel.events import EventJournal
        if EventJournal(self.root, run_id).sqlite:
            from mewcode.orchestration.workbench_events import changed
            changed('run', root=str(self.root), conversation_id=snapshot.get('conversation_id', ''),
                    run_id=run_id, **self._commit_notices.get(run_id, {}))
            return
        source = self._path(run_id)
        if source.is_file():
            summary = self._write_summary_cache(snapshot, source)
            stat = self._source_stat(source)
            self._public_summary_cache[run_id] = (stat.st_mtime_ns, stat.st_size,
                str(snapshot.get("conversation_id") or ""), summary)
        count = self._event_only_batches.get(run_id, 0) + 1
        self._event_only_batches[run_id] = count
        if count >= EVENT_SNAPSHOT_EVERY_BATCHES:
            self._enqueue_write(run_id, durable=False)
            self._event_only_batches[run_id] = 0

    def append_sequenced_events(
        self, run_id: str, events: list[dict[str, Any]], *, return_snapshot: bool = True
    ) -> dict[str, Any]:
        """Persist a sequenced batch while omitting reconstructable stream chunks."""
        if not events:
            with self._lock:
                value = self._cached(run_id)
                if value is None:
                    raise KeyError(run_id)
                return copy.deepcopy(value) if return_snapshot else {"last_seq": int(value.get("last_seq") or 0)}

        with self._lock:
            snapshot = self._cached(run_id)
            if snapshot is None:
                raise KeyError(run_id)
            live = snapshot
            last_seq = int(snapshot.get("last_seq") or 0)
            accepted: list[dict[str, Any]] = []
            for event in events:
                payload = compact_expert_event(event)
                seq = int(payload.get("seq") or 0)
                if seq <= last_seq:
                    continue
                payload.setdefault("server_time", utc_now())
                if payload.get("type") not in VOLATILE_EVENT_TYPES:
                    accepted.append(payload)
                last_seq = seq
            if last_seq <= int(snapshot.get("last_seq") or 0):
                return copy.deepcopy(snapshot) if return_snapshot else {"last_seq": last_seq}
            from mewcode.kernel.events import EventJournal
            journal = EventJournal(self.root, run_id)
            if journal.sqlite:
                # _live may contain references owned by a streaming callback.
                # Calculate durable changes from the private committed tree,
                # and update the live tree separately without joining aliases.
                previous = journal._sql_load(detached=False)
                if previous.get('kernel_revision') != live.get('kernel_revision'):
                    raise ValueError('kernel writer is stale; refresh before mutating')
                updated_at = utc_now()
                snapshot = _tool_event_snapshot(previous, accepted, last_seq, updated_at)
                self._commit(snapshot, kind='host_event_batch', previous=previous, tool_events=accepted)
                live = _tool_event_snapshot(live, copy.deepcopy(accepted), last_seq, updated_at)
                live['kernel_revision'] = snapshot['kernel_revision']
                self._live[run_id] = live
                self._after_event_log_append(run_id, snapshot)
                return copy.deepcopy(live) if return_snapshot else {"last_seq": last_seq}
            snapshot = copy.deepcopy(live)
            if not journal.sqlite:
                self._append_event_log(run_id, accepted, last_seq)
            snapshot.setdefault("events", []).extend(accepted)
            for item in accepted:
                _project_tool_lifecycle_event(snapshot, item)
            snapshot["last_seq"] = last_seq
            snapshot["updated_at"] = utc_now()
            _compact_snapshot_events(snapshot)
            self._commit(snapshot, kind='host_event_batch')
            self._live[run_id] = snapshot
            self._after_event_log_append(run_id, snapshot)
            return copy.deepcopy(snapshot) if return_snapshot else {"last_seq": last_seq}

    def flush_stream(
        self,
        run_id: str,
        node_id: str,
        *,
        text: str,
        reasoning: str,
        messages: list[dict[str, Any]],
        children: dict[str, dict[str, Any]] | None = None,
        member_id: str = "",
        safety_review_state: dict[str, Any] | None = None,
        stream_activity: dict[str, Any] | None = None,
        return_snapshot: bool = True,
        is_current: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Coalesced checkpoint writer for aggregate node and member state."""
        def apply(snapshot: dict[str, Any]) -> None:
            bucket = snapshot.setdefault("members" if member_id else "node_states", {})
            key = f"{node_id}::{member_id}" if member_id else node_id
            stage = bucket.setdefault(key, {})
            stage.update({
                "node_id": node_id,
                "member_id": member_id,
                "partial_text": text,
                "partial_reasoning": reasoning,
                "messages": messages,
            })
            if children is not None:
                snapshot.setdefault("children", {}).update(children)
            if safety_review_state is not None:
                stage["safety_review_state"] = copy.deepcopy(safety_review_state)
            if stream_activity is not None:
                stage["stream_activity"] = copy.deepcopy(stream_activity)
            snapshot.setdefault("usage", {})["total"] = aggregate_snapshot_usage(
                snapshot
            )

        # Tool events already persist their own lifecycle. Repeated stream
        # flushes must not clone and commit an unchanged execution history.
        with self._lock:
            if is_current is not None and not is_current():
                return {}
            current = self._cached(run_id)
            if current is None:
                raise KeyError(run_id)
            from mewcode.kernel.events import EventJournal
            journal = EventJournal(self.root, run_id)
            previous = journal._sql_load(detached=False) if journal.sqlite else current
            if previous.get('kernel_revision') != current.get('kernel_revision'):
                raise ValueError('kernel writer is stale; refresh before mutating')
            key = f"{node_id}::{member_id}" if member_id else node_id
            stage = (previous.get("members" if member_id else "node_states") or {}).get(key, {})
            expected = dict(node_id=node_id, member_id=member_id, partial_text=text,
                            partial_reasoning=reasoning, messages=messages)
            if safety_review_state is not None:
                expected["safety_review_state"] = safety_review_state
            if stream_activity is not None:
                expected["stream_activity"] = stream_activity
            actual = {name: stage.get(name) for name in expected}
            old_children = {name: (previous.get("children") or {}).get(name) for name in children or {}}
            from mewcode.kernel.sqlite_state import encoded
            unchanged = (all(name in stage for name in expected)
                and encoded(actual) == encoded(expected)
                and encoded(old_children) == encoded(children or {}))
            if unchanged:
                return copy.deepcopy(current) if return_snapshot else {}
            if journal.sqlite and current.get('status') == 'running' and stage:
                _validate_collaboration_write(previous)
                bucket_name = 'members' if member_id else 'node_states'
                if key not in (previous.get(bucket_name) or {}):
                    return self.mutate(run_id, apply, return_snapshot=return_snapshot)
                # Only derived stage fields and the ledger can be changed by
                # fact synchronization. Messages and archived attempts remain
                # shared with the private committed tree, never deep-copied.
                candidate = dict(previous,
                    node_states={name: dict(value) for name, value in previous['node_states'].items()},
                    node_ledgers=copy.deepcopy(previous.get('node_ledgers') or {}),
                    usage=dict(previous.get('usage') or {}),
                    children=dict(previous.get('children') or {}))
                candidate['stages'] = candidate['node_states']
                if member_id:
                    candidate['members'] = dict(previous.get('members') or {})
                    candidate['members'][key] = dict(candidate['members'][key])
                candidate[bucket_name][key].update(copy.deepcopy(expected))
                candidate['children'].update(copy.deepcopy(children or {}))
                candidate['usage']['total'] = aggregate_snapshot_usage(candidate)
                from mewcode.experts.node_ledger import sync_run_facts
                from mewcode.kernel.escalation import ensure_owners
                sync_run_facts(candidate)
                ensure_owners(candidate)
                candidate['updated_at'] = utc_now()
                if is_current is not None and not is_current():
                    return {}
                self._commit(candidate, kind='stream_checkpoint', previous=previous,
                             stream_checkpoint=True)
                # Keep callback-owned live objects separate from the durable
                # cache. Publish only the fields changed by this checkpoint.
                live = dict(current, updated_at=candidate['updated_at'],
                            kernel_revision=candidate['kernel_revision'],
                            usage=copy.deepcopy(candidate['usage']),
                            node_ledgers=copy.deepcopy(candidate['node_ledgers']))
                for bucket in ('node_states', 'members'):
                    live[bucket] = dict(current.get(bucket) or {})
                    for name, value in (candidate.get(bucket) or {}).items():
                        old = (previous.get(bucket) or {}).get(name) or {}
                        fields = {field: copy.deepcopy(item) for field, item in value.items()
                                  if field not in old or item is not old[field]}
                        if fields:
                            live[bucket][name] = dict(live[bucket].get(name) or {}, **fields)
                live['stages'] = live['node_states']
                live['children'] = dict(current.get('children') or {}, **copy.deepcopy(children or {}))
                self._live[run_id] = live
                self._after_event_log_append(run_id, candidate)
                return copy.deepcopy(live) if return_snapshot else {}
            return self.mutate(run_id, apply, return_snapshot=return_snapshot)

    def list_for_conversation(self, conversation_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        with self._lock:
            for path in self._snapshot_paths():
                try:
                    # Skip full parses for snapshots the owner cache already
                    # proves belong to another conversation (stat is enough).
                    stat = self._source_stat(path)
                    owner: str | None = None
                    cached = self._public_summary_cache.get(path.stem)
                    if cached is not None and cached[:2] == (
                        stat.st_mtime_ns, stat.st_size
                    ):
                        owner = cached[2]
                    else:
                        disk_cache = self._read_summary_cache(path, stat)
                        if disk_cache is not None:
                            owner = disk_cache[0]
                            self._public_summary_cache[path.stem] = (
                                stat.st_mtime_ns, stat.st_size,
                                disk_cache[0], disk_cache[1],
                            )
                    if owner is not None and owner != conversation_id:
                        continue
                    value = self._read_disk(path.stem)
                except (OSError, json.JSONDecodeError):
                    continue
                if value and value.get("conversation_id") == conversation_id:
                    items.append(value)
        items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return items

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Newest run snapshots across all conversations (used for cross-conversation
        canvas fallback when the active chat has no run yet). Never returns checkpoints."""
        items: list[dict[str, Any]] = []
        for path in self._snapshot_paths():
            try:
                value = self._read_disk(path.stem)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict) or not value.get("run_id"):
                continue
            items.append(value)
        items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return items[:max(1, min(int(limit), 500))]

    def latest_paused(self, conversation_id: str) -> dict[str, Any] | None:
        return next(
            (
                item for item in self.list_for_conversation(conversation_id)
                if item.get("status") == "paused" and not item.get("legacy")
            ),
            None,
        )

    def latest_resumable(self, conversation_id: str) -> dict[str, Any] | None:
        # "Continue" belongs to the most recently activated task. Filtering
        # first used to resurrect an older blocked task after its replacement
        # completed. An explicit history retry can still select any run by ID.
        latest = max(self.list_for_conversation(conversation_id), key=run_activation_time, default=None)
        return latest if latest is not None and is_resumable_snapshot(latest) else None

    def recover_stale_runs(self) -> int:
        recovered = 0
        for path in self._snapshot_paths():
            try:
                header = self._startup_fields(path.stem, 'status', 'interventions', 'active_node_ids', ('workflow_core', 'format'),
                                              presence=('node_ledgers', 'collaboration'))
                if header is not None and header.get('status') != 'running':
                    pending_takeover = any(item.get('kind') == 'takeover' and item.get('status') in {'queued', 'handling'}
                                           for item in header.get('interventions') or [])
                    needs_reconciliation = (header.get('status') in {'paused', 'blocked', 'needs_attention'}
                        and header.get(('workflow_core', 'format')) != 'mewcode.workflow.run.v2'
                        and not header.get('active_node_ids') and (header['node_ledgers'] or header['collaboration']))
                    if not pending_takeover and not needs_reconciliation:
                        continue
                value = self._read_disk(path.stem)
            except (OSError, json.JSONDecodeError):
                continue
            if value is None:
                continue
            if any(item.get("kind") == "takeover" and item.get("status") in {"queued", "handling"}
                   for item in value.get("interventions") or []):
                from mewcode.orchestration.interventions import update_receipts
                def recover_takeover(snapshot):
                    for item in snapshot.get("interventions") or []:
                        if item.get("kind") == "takeover" and item.get("status") in {"queued", "handling"}:
                            item["status"] = "interrupted" if item["status"] == "handling" else "not_handled"
                    snapshot["takeover_state"] = {"phase": "held", "text": "接管过程因后端重启停止，原进度保留；不会自动重放插入任务。"}
                    update_receipts(snapshot)
                self.mutate(str(value.get("run_id")), recover_takeover, durable=True)
            if value.get("status") != "running":
                from mewcode.kernel.collaboration_recovery import reconcile_attempt_order_gates
                from mewcode.kernel.reconcile import reconcile_pre_dispatch_effects
                if reconcile_pre_dispatch_effects(copy.deepcopy(value)):
                    value = self.mutate(str(value.get("run_id")), reconcile_pre_dispatch_effects, durable=True)
                # Rebind only a proven object-order bug; this leaves a fresh
                # user gate and never resumes work or records an approval.
                if reconcile_attempt_order_gates(copy.deepcopy(value)):
                    self.mutate(str(value.get("run_id")), reconcile_attempt_order_gates, durable=True)
                continue
            try:
                self.transition(
                    str(value.get("run_id")), "paused",
                    reason="后端重启或进程异常退出，已保存为可接力状态",
                )
                recovered += 1
            except (KeyError, ValueError):
                continue
        return recovered

    def delete_conversation(self, conversation_id: str) -> int:
        removed = 0
        for value in self.list_for_conversation(conversation_id):
            try:
                run_id = str(value.get('run_id'))
                with self._lock:
                    if run_id in self._live:
                        self._enqueue_write(run_id, durable=True)
                    from mewcode.kernel.events import EventJournal
                    EventJournal(self.root, run_id).delete()
                    self._live.pop(run_id, None)
                    self._write_generation[run_id] = self._write_generation.get(run_id, 0) + 1
                self._path(str(value.get("run_id"))).unlink(missing_ok=True)
                self._summary_path(str(value.get("run_id"))).unlink(missing_ok=True)
                self.checkpoint_archive.delete(str(value.get("run_id")))
                self._public_summary_cache.pop(str(value.get("run_id")), None)
                removed += 1
            except (OSError, ValueError):
                continue
        return removed
