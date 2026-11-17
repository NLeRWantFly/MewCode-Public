"""Pure routing for deciding whether a blocker belongs to the system or user."""
from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from typing import Any, Literal, TypedDict


Owner = Literal["system", "user"]
HandlingState = Literal["deciding", "working", "needs_user", "blocked", "resolved"]


class RouteDecision(TypedDict):
    owner: Owner
    handling_state: HandlingState
    reason_kind: str
    reason: str
    evidence_refs: list[str]


_USER_ROOT_CAUSES = {
    "permission": "authorization",
    "authorization": "authorization",
    "authority": "authorization",
    "unmet_authority": "authorization",
    "missing_authority": "authorization",
    "scope": "scope",
    "scope_change": "scope",
    "acceptance_change": "scope",
    "business_scope": "scope",
    "cost": "cost",
    "fees": "cost",
    "fee": "cost",
    "price": "cost",
    "budget_choice": "cost",
    "goal": "goal",
    "goal_change": "goal",
    "objective_change": "goal",
    "model_no_authorized_backup": "model_fallback",
    "no_authorized_backup": "model_fallback",
    "no_authorized_model_fallback": "model_fallback",
    "user_input": "user_input",
    "user_information": "user_input",
    "user_preference": "user_input",
    "user_only_information": "user_input",
    "user_unique_information": "user_input",
    "missing_user_information": "user_input",
}
_USER_QUESTION_KINDS = {
    "permission": "authorization",
    "authorization": "authorization",
    "scope": "scope",
    "cost": "cost",
    "goal": "goal",
    "model_fallback": "model_fallback",
    "user_input": "user_input",
    "user_preference": "user_input",
}
_REASON_TEXT = {
    "authorization": "此操作需要用户本人通过现有授权流程确认。",
    "scope": "处理方式会改变约定范围或验收条件，需要用户决定。",
    "cost": "处理方式涉及费用或预算取舍，需要用户决定。",
    "goal": "处理方式会改变任务目标，需要用户决定。",
    "model_fallback": "没有已授权的备用模型，需要用户指定或授权后再继续。",
    "user_input": "缺少只有用户能够提供的事实或偏好。",
}


def _key(value: Any) -> str:
    text = str(value or "").strip().casefold()
    return "_".join(part for part in "".join(
        char if char.isalnum() else " " for char in text).split())


def _refs(*sources: Any) -> list[str]:
    refs: list[str] = []
    for source in sources:
        values = source if isinstance(source, (list, tuple, set)) else [source]
        for value in values:
            if isinstance(value, str) and value.strip() and value.strip() not in refs:
                refs.append(value.strip()[:160])
    return refs[:32]


def derive_handling(*, purpose: str, question_kind: str = "", questions: Sequence[Any] | None = None,
                    root_cause: str = "", explicit_user_gate: bool = False,
                    unmet_authority: Sequence[Any] | None = None,
                    missing_user_fields: Sequence[Any] | None = None,
                    context: Mapping[str, Any] | None = None) -> RouteDecision:
    """Classify ownership without invoking a model or requesting user input.

    User ownership requires an explicit human gate, host-reported missing
    authority/user data, or a recognized root cause with evidence. Ordinary
    technical choices remain with the system even when the selector is unsure.
    """

    facts = context if isinstance(context, Mapping) else {}
    root = _key(root_cause or facts.get("root_cause"))
    kind = _key(question_kind or facts.get("question_kind"))
    refs = _refs(facts.get("evidence_refs"), facts.get("evidence_ref"))
    missing = list(missing_user_fields or [])
    authority = list(unmet_authority or [])
    field_meta = facts.get("missing_fields")
    if isinstance(field_meta, Mapping):
        missing.extend(field_meta.get("user_only") or [])
    authority.extend(facts.get("unmet_authority") or [])
    refs = _refs(refs, [item.get("evidence_ref") or item.get("ref")
                        for item in authority if isinstance(item, Mapping)])

    if purpose == "authorization" or authority:
        reason_kind = "authorization"
    elif purpose == "user_input" or missing:
        reason_kind = "user_input"
    elif explicit_user_gate or facts.get("explicit_user_gate") is True:
        reason_kind = _USER_ROOT_CAUSES.get(root) or _USER_QUESTION_KINDS.get(kind) or "explicit_user_gate"
    else:
        reason_kind = _USER_ROOT_CAUSES.get(root) or _USER_QUESTION_KINDS.get(kind)
        # A root-cause label alone is not enough to hand off a scope, cost,
        # goal, or model-fallback decision; require linked evidence.
        if reason_kind and reason_kind != "authorization" and not refs:
            reason_kind = None

    if reason_kind:
        reason = _REASON_TEXT.get(reason_kind, "此问题包含必须由用户确认的门禁。")
        return {
            "owner": "user",
            "handling_state": "needs_user",
            "reason_kind": reason_kind,
            "reason": reason,
            "evidence_refs": refs,
        }

    return {
        "owner": "system",
        "handling_state": "deciding",
        "reason_kind": "technical_question",
        "reason": "这是内部技术问题，由主模型继续分析；Jev 只提供建议。",
        "evidence_refs": refs,
    }


def _candidate_rows(candidates: Sequence[Any] | None) -> list[dict[str, Any]]:
    rows = []
    seen: set[str] = set()
    for item in candidates or []:
        if hasattr(item, "model_dump"):
            item = item.model_dump()
        if not isinstance(item, Mapping):
            continue
        option_id = item.get("id") or item.get("option_id")
        if not isinstance(option_id, str) or not option_id.strip() or option_id in seen:
            continue
        if item.get("eligible", item.get("feasible", True)) is False:
            continue
        seen.add(option_id)
        rows.append(dict(item, id=option_id))
    return rows


def _main_model(decision: Mapping[str, Any], *, reason_kind: str, reason: str,
                selector_status: str = "") -> dict[str, Any]:
    result = dict(decision)
    result.update(
        status="main_model",
        owner="system",
        handling_state="deciding",
        reason_kind=reason_kind,
        reason=reason,
        next_action="主模型基于现有证据继续判断并调整方案；Jev 失败、低置信或拒绝候选都不构成询问用户的理由。",
    )
    if selector_status:
        result["selector_status"] = selector_status
    return result


async def resolve_handling(decision: Mapping[str, Any], *, candidates: Sequence[Any] | None,
                           selector=None, context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Resolve a system-owned choice; never call a selector for user-owned work.

    ``selector`` receives ``(eligible_candidates, context)`` and may return a
    selected id or a decision-result mapping. A selector is advisory only.
    """

    if decision.get("owner") == "user":
        result = dict(decision)
        result["status"] = "needs_user"
        return result
    if decision.get("owner") != "system":
        return _main_model(decision, reason_kind="invalid_route",
                           reason="路由信息不完整，由主模型继续判断。")

    eligible = _candidate_rows(candidates)
    if not eligible:
        result = dict(decision)
        result.update(status="blocked", owner="system", handling_state="blocked",
                      reason_kind="no_feasible_candidate",
                      reason="目前没有通过宿主约束的可行技术方案；主模型应继续查找证据或方案。",
                      next_action="继续检查现有证据和技术条件，不要把无候选方案转成用户选择题。")
        return result
    if selector is None:
        return _main_model(decision, reason_kind="selector_unavailable",
                           reason="辅助选择器不可用，由主模型继续判断。")

    try:
        selected = selector(eligible, dict(context or {}))
        if inspect.isawaitable(selected):
            selected = await selected
    except Exception as exc:  # noqa: BLE001 -- selector failures return to the main model.
        return _main_model(decision, reason_kind="selector_failure",
                           reason=f"辅助选择器未能完成判断（{type(exc).__name__}）；由主模型继续分析。",
                           selector_status="failure")

    selector_status = ""
    confidence = None
    if isinstance(selected, str):
        option_id = selected
    elif isinstance(selected, Mapping):
        selector_status = str(selected.get("status") or "")
        option_id = selected.get("selected_option_id") or selected.get("choice")
        confidence = selected.get("confidence")
    else:
        option_id = None
    if selector_status and selector_status not in {"selected", "resolved"}:
        return _main_model(decision, reason_kind="selection_uncertain",
                           reason="辅助选择器未能可靠选出方案；由主模型继续分析，不询问用户。",
                           selector_status=selector_status)
    if (isinstance(confidence, bool) or confidence is not None
            and (not isinstance(confidence, (int, float)) or confidence < .55)):
        return _main_model(decision, reason_kind="low_confidence",
                           reason="辅助选择器把握不足，由主模型继续分析，不询问用户。",
                           selector_status=selector_status or "low_confidence")
    if not isinstance(option_id, str) or option_id not in {row["id"] for row in eligible}:
        return _main_model(decision, reason_kind="invalid_selection",
                           reason="辅助选择器没有返回可行方案，由主模型继续分析。",
                           selector_status=selector_status or "invalid_selection")

    result = dict(decision)
    result.update(status="selected", owner="system", handling_state="working",
                  selected_option_id=option_id, reason_kind="advisory_selection",
                  reason="Jev 提供了候选建议；主模型仍需检查约束，执行还须经过宿主权限与结果核验。")
    return result