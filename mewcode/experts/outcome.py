"""Acceptance is independent of reaching the end of a routed execution path."""
from __future__ import annotations

from typing import Any
from datetime import datetime


def downstream_reviews(flow: dict[str, Any], node_id: str) -> set[str]:
    """Reviews of work following a findings stage, never decision/gate nodes.

    Stop at the first real review on each path: a later, unrelated PASS must
    not hide an earlier failed final review. Merely routing to a manager is
    not remediation and cannot discharge an audit.
    """
    nodes = {str(n.get("node_id")): n for n in flow.get("nodes") or []}
    kinds = (flow.get("meta") or {}).get("node_kinds") or {}
    queue, seen, reviews = [(node_id, False)], set(), set()
    for current, worked in queue:
        if (current, worked) in seen:
            continue
        seen.add((current, worked))
        for edge in flow.get("edges") or []:
            if (edge.get("from_node") or edge.get("from")) != current:
                continue
            target = str(edge.get("to_node") or edge.get("to") or "")
            node = nodes.get(target, {})
            if node.get("role") == "auditor" and kinds.get(target) != "decision":
                if worked:
                    reviews.add(target)
                continue
            queue.append((target, worked or node.get("role") == "executor"))
    return reviews


def discharged_findings(flow: dict[str, Any], states: dict[str, Any]) -> set[str]:
    """A completed pre-work review can be superseded by completed final review.

    Keep all original findings/verdicts intact. This only classifies overall
    acceptance; it must not make the initial review's PASS edge fire.
    """
    discharged = set()
    for node in flow.get("nodes") or []:
        node_id = str(node.get("node_id") or "")
        stage = states.get(node_id) or {}
        if (node.get("role") != "auditor" or stage.get("status") != "completed"
                or (stage.get("verdict") or {}).get("verdict") != "NEEDS_ATTENTION"):
            continue
        reviews = downstream_reviews(flow, node_id)
        digest = (stage.get("audit_snapshot") or {}).get("snapshot_hash")
        active = [states.get(key) or {} for key in reviews
                  if (states.get(key) or {}).get("status") != "skipped"]
        if digest and active and all(s.get("status") == "completed" and s.get("passed") is not False
                          and (s.get("verdict") or {}).get("verdict") == "PASS"
                          and ((s.get("verdict") or {}).get("reviewed_snapshots") or {}).get(node_id) == digest for s in active):
            discharged.add(node_id)
    return discharged


def run_activation_time(snapshot: dict[str, Any]) -> float:
    values = [snapshot.get("created_at") or snapshot.get("updated_at")]
    values.extend(item.get("started_at") for item in snapshot.get("attempts") or [] if isinstance(item, dict))
    times = []
    for value in values:
        try:
            times.append(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
        except (ValueError, TypeError, OverflowError):
            continue
    return max(times, default=0.0)


def unresolved_audits(flow: dict[str, Any], states: dict[str, Any]) -> list[str]:
    """Use structured results only; a decision's fail branch is not a failed audit."""
    kinds = (flow.get("meta") or {}).get("node_kinds") or {}
    discharged = discharged_findings(flow, states)
    unresolved = []
    for node in flow.get("nodes") or []:
        node_id = str(node.get("node_id") or "")
        if node.get("role") != "auditor" or kinds.get(node_id) == "decision" or node_id in discharged:
            continue
        stage = states.get(node_id) or {}
        if stage.get("status") in {None, "pending", "queued", "skipped"}:
            continue
        verdict = stage.get("verdict") or {}
        if (stage.get("passed") is False
                or (verdict and verdict.get("verdict") != "PASS")
                or stage.get("status") != "completed"):
            unresolved.append(node_id)
    return unresolved


def effective_run_status(snapshot: dict[str, Any]) -> str:
    status = str(snapshot.get("status") or "")
    if status == "completed" and collaborative_draft_ready(snapshot):
        return status
    if status == "completed" and unresolved_audits(
        snapshot.get("locked_flow") or {}, snapshot.get("node_states") or snapshot.get("stages") or {},
    ):
        return "needs_attention"
    return status


def collaborative_draft_ready(snapshot: dict[str, Any]) -> bool:
    from mewcode.kernel.collaboration import final_ready
    value = snapshot.get("collaboration")
    return bool(snapshot.get("orchestration_mode") == "collaborative" and value and final_ready(value))


def acceptance_retry_required(snapshot: dict[str, Any]) -> bool:
    """No unfinished invocation remains to resume, but acceptance still failed."""
    if snapshot.get("status") not in {"completed", "needs_attention", "paused", "blocked"}:
        return False
    if collaborative_draft_ready(snapshot):
        return False
    flow = snapshot.get("locked_flow") or {}
    states = snapshot.get("node_states") or snapshot.get("stages") or {}
    finalizer = str(snapshot.get("finalizer_node_id") or flow.get("finalizer_node_id") or "")
    unresolved = unresolved_audits(flow, states)
    if not unresolved or not all(s.get("status") in {"completed", "skipped", "pending", "queued"} for s in states.values()):
        return False
    if (states.get(finalizer) or {}).get("status") == "completed":
        return True
    from mewcode.experts.flow import normalize_edge_condition
    nodes = {str(node.get("node_id")): node for node in flow.get("nodes") or []}
    return any((states.get(node_id) or {}).get("status") == "completed"
        and (nodes.get(node_id, {}).get("failure_policy") or {}).get("action") not in {"route", "fallback"}
        and not any((edge.get("from_node") or edge.get("from")) == node_id
                    and normalize_edge_condition(edge.get("condition", "")) == "fail" for edge in flow.get("edges") or [])
        for node_id in unresolved)


def acceptance_recheck_nodes(flow: dict[str, Any], states: dict[str, Any], *, retry: str = "") -> list[str]:
    """Only an explicit audit retry invalidates acceptance and its consumers.

    Ordinary continue resumes the checkpoint, including unfinished repair,
    pending human questions and attempt memory. Findings are not a request
    to restart upstream work.
    """
    if not retry:
        return []
    failed = unresolved_audits(flow, states)
    if retry:
        failed = [node_id for node_id in failed if node_id == retry]
    # Loop checkpoints own their recovery and budgets; never reset them here.
    loop_ids = {node_id for loop in (flow.get("meta") or {}).get("loops") or []
                for node_id in loop.get("node_ids") or []}
    owned_recovery = {node.get("node_id") for node in flow.get("nodes") or []
                      if (node.get("failure_policy") or {}).get("action") in {"route", "fallback"}}
    owned_targets = {(node.get("failure_policy") or {}).get("target_node_id") for node in flow.get("nodes") or []
                     if node.get("node_id") in owned_recovery}
    candidates = [node_id for node_id in failed if node_id not in loop_ids | owned_recovery
                  and (states.get(node_id) or {}).get("status") == "completed"]
    # Retrying failed work invalidates old consumers too. Otherwise a repaired
    # executor could reuse a PASS that belongs to its previous artifact.
    retry_stage = states.get(retry) or {}
    retrying_work = (retry not in loop_ids | owned_recovery | owned_targets
                     and retry_stage.get("status") in {"continuing", "failed", "stalled", "needs_attention", "blocked", "budget_exhausted", "paused"})
    if retrying_work and retry not in candidates:
        candidates.append(retry)
    affected: set[str] = set()
    for candidate in candidates:
        pending, descendants = [candidate], {candidate}
        for node_id in pending:
            for edge in flow.get("edges") or []:
                target = str(edge.get("to_node") or edge.get("to") or "")
                if (edge.get("from_node") or edge.get("from")) == node_id and target not in descendants:
                    descendants.add(target)
                    pending.append(target)
        # A downstream loop/recovery checkpoint must be restarted through its
        # own explicit controls, not partially invalidated by ordinary resume.
        if not descendants & (loop_ids | owned_recovery):
            if retrying_work:
                descendants.discard(retry)  # Resume this node's own messages.
            affected.update(descendants)
    return [str(node["node_id"]) for node in flow.get("nodes") or [] if node.get("node_id") in affected]


def project_acceptance_status(public: dict[str, Any], *, source: dict[str, Any] | None = None) -> dict[str, Any]:
    """Correct a display copy, including old cached summaries; never rewrite history."""
    source = source if source is not None else public
    status = effective_run_status(source)
    if status != source.get("status"):
        public["recorded_status"] = source["status"]
        public["status"] = status
        public["status_reason"] = "节点执行已结束，但交付验收未通过；已保留执行进度。"
        public["flow_state"] = {**(public.get("flow_state") or {}), "status": status}
    public["unresolved_audit_node_ids"] = unresolved_audits(
        source.get("locked_flow") or {}, source.get("node_states") or source.get("stages") or {},
    )
    discharged = discharged_findings(source.get("locked_flow") or {}, source.get("node_states") or source.get("stages") or {})
    public["discharged_audit_node_ids"] = sorted(discharged)
    public["resume_requires_retry"] = acceptance_retry_required(source)
    # Projection-only annotations; preserve the actual original verdict.
    for bucket in ("node_states", "stages"):
        if isinstance(public.get(bucket), dict):
            public[bucket] = {key: {**stage, "findings_discharged": key in discharged}
                              for key, stage in public[bucket].items()}
    if isinstance(public.get("events"), list):
        public["events"] = [{**event, "findings_discharged": event.get("node_id") in discharged}
                            if event.get("node_id") else event for event in public["events"]]
    return public
