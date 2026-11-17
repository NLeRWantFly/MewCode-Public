"""Host-owned upward reports for capacity and reasoning recovery.

These reports describe the next decision; they never change a grant, route,
permission, acceptance criterion, or the saved execution checkpoint.
"""
from __future__ import annotations

from typing import Any


POLICY_VERSION = "recovery-escalation-v1"
REASONING_ERRORS = frozenset({"semantic_uncertain", "reasoning_insufficient",
                              "review_low_confidence"})
CONTEXT_ERRORS = frozenset({"context_limit", "model_context_limit",
                            "safety_review_context_limit"})


def recovery_upward_report(*, error_kind: str, diagnosis: dict[str, Any],
                           health: dict[str, Any], remaining_tokens: int | None,
                           remaining_seconds: float | None,
                           active_blockers: list[dict[str, Any]] | None = None,
                           token_window_receipt: dict[str, Any] | None = None) -> dict[str, Any]:
    """Separate a hard stop from an execution-window or reasoning request."""
    if remaining_tokens is not None and (isinstance(remaining_tokens, bool)
                                         or not isinstance(remaining_tokens, int)):
        raise ValueError("remaining_tokens must be an integer or unknown")
    kind = str(diagnosis.get("primary_kind") or "insufficient_evidence")
    signals = list(diagnosis.get("evidence_refs") or [])
    blockers = [str(row.get("kind") or "") for row in active_blockers or []]
    window = token_window_receipt or {}
    base = {"policy_version": POLICY_VERSION, "error_kind": error_kind,
            "diagnosis": kind, "evidence_refs": signals,
            "current_remaining_tokens": remaining_tokens,
            "current_remaining_seconds": remaining_seconds,
            "ceiling_warning": window.get("upward_warning"),
            "automatic_total_increase": False, "automatic_route_change": False,
            "changes_acceptance": False}
    hard_blockers = [name for name in blockers if name in {
        "permission", "unknown_effect", "human_question", "safety_review",
        "legacy_safety_review", "scope_change_required", "acceptance_conflict",
    }]
    if hard_blockers or kind == "business_ambiguity":
        return {**base, "kind": "hard_boundary", "requires_pause": True,
                "action": "resolve_evidence_or_authority",
                "blocker_kinds": hard_blockers,
                "message": "先对账、补证或取得所需授权；额度和模型升级不能解除硬门禁。"}
    if window.get("action") == "reconcile_overrun":
        return {**base, "kind": "accounting_overrun", "requires_pause": True,
                "action": "reconcile_actual_usage",
                "message": "实测用量超过获批总额，先对账，不重试或自行扩容。"}
    if (window.get("action") == "request_total_cap" or
            (remaining_tokens is not None and remaining_tokens < 1024)):
        forecast = window.get("forecast_remaining_tokens")
        spent = window.get("spent_tokens")
        suggested = (spent + forecast if isinstance(spent, int)
                     and isinstance(forecast, int) and forecast > 0 else None)
        return {**base, "kind": "total_token_ceiling", "requires_pause": True,
                "action": "request_explicit_finite_total_cap",
                "suggested_total_tokens": suggested,
                "forecast_basis": "observed_productive_token_cost" if suggested else "unknown",
                "message": "已到获批总额；可上报剩余工作预测，但只有用户明确授权才能提高硬上限。"}
    if window.get("action") == "replan":
        return {**base, "kind": "ineffective_execution", "requires_pause": False,
                "action": "replan_method_without_more_tokens",
                "message": "连续消耗而无宿主持久化进展；先换执行方法，不以空转换取更大额度。"}
    if error_kind in CONTEXT_ERRORS or kind == "context_window_limit":
        return {**base, "kind": "context_window", "requires_pause": False,
                "action": "lossless_split_or_request_larger_declared_window",
                "message": "单次输入窗口不足；优先无损分段，必要时上报申请已声明的大窗口路由，不重发相同超限输入。"}
    if error_kind == "output_window_limit" or kind == "output_window_limit":
        return {**base, "kind": "output_window", "requires_pause": False,
                "action": "split_output_or_request_larger_declared_output",
                "message": "单次回复多次截断；分块提交或上报已声明的更大输出能力，不把截断内容记为完成。"}
    if error_kind in REASONING_ERRORS or kind == "reasoning_limit":
        return {**base, "kind": "reasoning_capacity", "requires_pause": False,
                "action": "request_deeper_reasoning_or_independent_review",
                "message": "判断不确定，不应缩小证据或降低标准；保留原文并上报更强推理/独立复核需求。"}
    if diagnosis.get("hard_boundary"):
        return {**base, "kind": "hard_boundary", "requires_pause": True,
                "action": "resolve_evidence_or_authority", "blocker_kinds": blockers,
                "message": "先对账、补证或取得所需授权；额度和模型升级不能解除硬门禁。"}
    if window.get("action") == "replan" or kind == "idle_stall":
        return {**base, "kind": "ineffective_execution", "requires_pause": False,
                "action": "replan_method_without_more_tokens",
                "message": "连续消耗而无宿主持久化进展；先换执行方法，不以空转换取更大额度。"}
    if kind == "capacity_shortfall" and error_kind in {"segment_timeout", "budget_exhausted", "internal_work"}:
        return {**base, "kind": "execution_window", "requires_pause": False,
                "action": "increase_next_slice_within_parent_limit",
                "message": "本段有可核验进展；下一段可在现有总额内提高软窗口并保存调整回执。"}
    if kind == "insufficient_evidence" and error_kind == "segment_timeout":
        timing = health.get("timing") or {}
        return {**base, "kind": "needs_capacity_diagnosis", "requires_pause": False,
                "action": "inspect_model_and_tool_spans_before_downgrade",
                "timing_coverage": timing.get("coverage"),
                "message": "段超时不等于无能力或 token 用尽；先核对模型、工具与审查耗时，再决定扩大窗口、重规划或上报。"}
    return {**base, "kind": "none", "requires_pause": False,
            "action": "follow_existing_recovery_ladder", "message": "沿原恢复阶梯处理。"}
