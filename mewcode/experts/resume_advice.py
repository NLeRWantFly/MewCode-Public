"""Read-only advice for choosing the next explicit expert-run control."""
from __future__ import annotations

import copy
from typing import Any

from mewcode.experts.recovery_case import cases


RETRYABLE_NODE_STATUSES = frozenset({
    "continuing", "failed", "stalled", "needs_attention", "blocked",
    "budget_exhausted", "paused",
})


class RecoveryResumeBlocked(ValueError):
    """A plain Continue was blocked by a saved recovery decision."""

    error_code = "recovery_resume_blocked"

    def __init__(self, advice: dict[str, Any]):
        self.resume_advice = copy.deepcopy(advice)
        reason = str(advice.get("reason") or "当前检查点仍需明确处理。")
        # Preserve the legacy exception prefix for callers that only know how
        # to render the former pending-case error, while the typed payload
        # carries the specific action/node for new clients.
        prefix = "系统受阻；" if advice.get("action") == "system_blocked" else "恢复案件需要具体决定；"
        super().__init__(prefix + reason)


def _node_label(record: dict[str, Any], node_id: str) -> str:
    stage = (record.get("node_states") or {}).get(node_id) or {}
    for node in (record.get("locked_flow") or {}).get("nodes", []):
        if isinstance(node, dict) and str(node.get("node_id") or "") == node_id:
            return str(node.get("label") or stage.get("label") or node_id)
    return str(stage.get("label") or node_id)


def _compact_reason(value: Any, *, limit: int = 320) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _case_summary(record: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    node_id = str(case.get("node_id") or "")
    report = case.get("report") if isinstance(case.get("report"), dict) else {}
    pending = case.get("pending") if isinstance(case.get("pending"), dict) else {}
    return {
        "case_id": str(case.get("case_id") or ""),
        "node_id": node_id,
        "node_label": _node_label(record, node_id),
        "status": str(case.get("status") or ""),
        "closed_by_user": bool(case.get("closed_by_user")),
        "root_cause": str(report.get("root_cause") or ""),
        "reason": _compact_reason(report.get("reason") or pending.get("question")),
    }


def _case_binding_reasons(record: dict[str, Any], case: dict[str, Any]) -> list[str]:
    """Return reasons a saved case no longer belongs to the current node."""
    from mewcode.experts.node_ledger import ledger_for_run
    from mewcode.experts.recovery_case import active_case

    node_id = str(case.get("node_id") or "")
    stage = (record.get("node_states") or {}).get(node_id)
    reasons: list[str] = []
    if not isinstance(stage, dict):
        return ["案件原节点已不在当前运行状态中"]
    view = ledger_for_run(record).view(node_id)
    if case.get("epoch") != view.epoch:
        reasons.append("案件代际已变化")
    if str(case.get("input_revision") or "") != str(stage.get("input_revision") or ""):
        reasons.append("案件输入版本已变化")
    current = active_case(record, node_id)
    if not current or current.get("case_id") != case.get("case_id"):
        reasons.append("案件已不是当前节点的活动案件")
    return reasons


def resume_advice(record: dict[str, Any] | None) -> dict[str, Any]:
    """Return a UI suggestion without changing the run or reconciling facts.

    ``action`` is guidance only.  The actual resume/retry path still performs
    its normal CAS and authorization checks.
    """
    # Enumeration only defaults the top-level ledger mapping and reads detached
    # NodeView facts. Do not copy every transcript just to find no pending case.
    # A real pending case still gets the private verifier scratch copy below.
    record = dict(record) if isinstance(record, dict) else {}
    pending = [case for case in cases(record)
               if isinstance(case, dict)
               and case.get("status") in {"reported", "awaiting_user"}]
    base = {
        "run_id": str(record.get("run_id") or ""),
        "conversation_id": str(record.get("conversation_id") or ""),
    }
    if not pending:
        return {**base, "action": "continue", "reason": "没有未决恢复案件，可以继续任务。",
                "node_id": "", "node_label": "", "case_id": ""}

    record = copy.deepcopy(record)
    summaries = [_case_summary(record, case) for case in pending]
    if len(pending) > 1:
        if all(not case.get('pending') and not case.get('closed_by_user') for case in pending):
            return {**base, 'action': 'system_blocked', 'reason': '内部处理尚未恢复，检查点已保留；具体故障见系统事项。',
                    'node_id': '', 'node_label': '', 'case_id': '', 'cases': summaries}
        return {**base, "action": "review_cases", "reason": "当前有多个恢复案件待处理，请逐项查看后选择。",
                "node_id": "", "node_label": "", "case_id": "", "cases": summaries}

    case = pending[0]
    summary = summaries[0]
    node_id = summary["node_id"]
    binding_reasons = _case_binding_reasons(record, case)
    if binding_reasons:
        return {**base, "action": "review_cases", "reason":
                "案件来源代际或输入版本已变化，请核对当前节点状态后再决定。",
                "node_id": node_id, "node_label": summary["node_label"],
                "case_id": summary["case_id"], "cases": summaries,
                "verification_reasons": binding_reasons}
    stage = (record.get("node_states") or {}).get(node_id) or {}
    if case.get("closed_by_user"):
        pending_case = case.get("pending")
        has_pending = pending_case and (
            not isinstance(pending_case, dict) or bool(pending_case)
        )
        if has_pending:
            return {**base, "action": "review_cases", "reason":
                    "该恢复案件已标记停止，但仍保留未决事项；请核对案件后再决定，不会自动恢复。",
                    "node_id": node_id, "node_label": summary["node_label"],
                    "case_id": summary["case_id"], "cases": summaries}
        if stage.get("status") in RETRYABLE_NODE_STATUSES:
            return {**base, "action": "retry_node", "reason":
                    "该恢复案件已停止；当前节点尚未完成，请明确选择重试该节点。不会自动清除停止状态。",
                    "node_id": node_id, "node_label": summary["node_label"],
                    "case_id": summary["case_id"], "cases": summaries}
        return {**base, "action": "review_cases", "reason":
                "该恢复案件已停止，但对应节点当前状态不支持重试；请核对案件与节点状态后再决定。不会自动恢复。",
                "node_id": node_id, "node_label": summary["node_label"],
                "case_id": summary["case_id"], "cases": summaries}

    # This import stays local so the advice module remains a read-only leaf and
    # expert_runs can use it without an import cycle during module loading.
    if ((record.get('locked_flow') or {}).get('meta', {}).get('node_kinds', {}).get(node_id) == 'gate'):
        return {**base, 'action': 'continue_checkpoint', 'source': 'normal_gate_handoff',
                'reason': '此处应进入原门禁的具体方案确认；继续仅准备方案，不形成批准或完成节点。',
                'node_id': node_id, 'node_label': summary['node_label'], 'case_id': summary['case_id']}
    from mewcode.experts.recovery_context import verify_input_discovery
    checked = verify_input_discovery(record, node_id, case=case,
        case_id=str(case.get("case_id") or ""))
    if checked.get("callback_eligible"):
        return {**base, "action": "continue_checkpoint", "reason":
                "已核验当前只读恢复回执，可以从原规划节点继续核对。",
                "node_id": node_id, "node_label": summary["node_label"],
                "case_id": summary["case_id"], "evidence_digest": checked.get("evidence_digest")}

    if not case.get('pending'):
        return {**base, 'action': 'system_blocked', 'reason':
                _compact_reason(summary['reason'] or '内部处理尚未恢复；检查点已保留，没有需要用户回答的问题。'),
                'node_id': node_id, 'node_label': summary['node_label'],
                'case_id': summary['case_id'], 'cases': summaries}

    return {**base, "action": "answer_case", "reason":
            _compact_reason(summary["reason"] or "当前恢复案件仍需明确处理。"),
            "node_id": node_id, "node_label": summary["node_label"],
            "case_id": summary["case_id"], "cases": summaries,
            "verification_reasons": list(checked.get("reasons") or [])[:8]}
