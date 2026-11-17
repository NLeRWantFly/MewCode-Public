"""Pre-ask routing for human questions and advisory Jev selections."""
from __future__ import annotations

import json

from mewcode.decisions.spec import Candidate, DecisionInput
from mewcode.experts.interaction_routing import derive_handling, resolve_handling
from mewcode.tools.base import ToolResult
from mewcode.tools.ask_user import QuestionOption


def _main_model(route, *, reason_kind, reason, next_action=None):
    result = dict(route)
    result.update(status="main_model", owner="system", handling_state="deciding",
        reason_kind=reason_kind, reason=reason,
        next_action=next_action or "主模型基于现有证据继续判断；辅助选择器的不确定结果不构成询问用户的理由。")
    return ToolResult(json.dumps(result, ensure_ascii=False))


def _result(data):
    return ToolResult(json.dumps(data, ensure_ascii=False))


async def before_ask(agent, params):
    """Return advisory/model guidance, or ``None`` only for a user-owned question."""

    if params.purpose == "user_input":
        return None
    if params.purpose == "authorization":
        return _result({"status": "needs_authorization", "owner": "user",
            "handling_state": "needs_user", "reason_kind": "authorization",
            "reason": "授权必须绑定到宿主记录的具体操作。",
            "evidence_refs": [],
            "next_action": "使用现有授权流程并提供准确的 review_call_ref；普通提问不能授予权限。"})
    if params.purpose == "auto":
        conversation = getattr(agent, "_current_conversation", None)
        turn = sum(1 for message in getattr(conversation, "history", [])
                   if message.role == "user" and message.content
                   and not message.transient and not message.tool_results)
        key = (getattr(agent, "session_id", ""), turn)
        if getattr(agent, "_clarification_nudge", None) == key:
            return _main_model({"owner": "system", "handling_state": "deciding",
                "evidence_refs": []}, reason_kind="classification_repeated",
                reason="提问类型仍不明确；主模型应先判断这是内部技术问题还是用户独有信息。")
        agent._clarification_nudge = key
        return _result({"status": "classify_before_asking", "owner": "system",
            "handling_state": "deciding", "reason_kind": "unclassified_question",
            "reason": "先判断问题归属：技术选择由主模型分析并可用 Jev 辅助；只有用户独有事实、授权或有证据的范围/费用/目标/备用模型选择才询问用户。",
            "next_action": "若是技术问题，使用 DecideNextStep 或 AskUserQuestion purpose=decision；若只有用户知道该事实，使用 purpose=user_input；真实授权走现有授权流程。"})

    context = {
        "evidence_refs": params.evidence_refs,
        "missing_fields": {"user_only": params.missing_user_fields},
        "unmet_authority": params.unmet_authority,
        "root_cause": params.root_cause,
        "question_kind": params.question_kind,
        "explicit_user_gate": params.explicit_user_gate,
        "decision_state": params.decision_state,
    }
    route = derive_handling(
        purpose=params.purpose,
        question_kind=params.question_kind,
        questions=[question.model_dump() for question in params.questions],
        root_cause=params.root_cause,
        explicit_user_gate=params.explicit_user_gate,
        unmet_authority=params.unmet_authority,
        missing_user_fields=params.missing_user_fields,
        context=context,
    )
    if route["owner"] == "user":
        if route["reason_kind"] == "authorization":
            # A generic question must never grant missing authority. The caller
            # must enter the host's exact-action review path.
            return _result({**route, "status": "needs_authorization",
                "next_action": "通过现有宿主授权流程提交具体的 review_call_ref；普通提问不会授予执行权限。"})
        return None

    if params.purpose != "decision":
        return _main_model(route, reason_kind="unsupported_question_route",
                           reason="主模型应先把内部技术问题整理为一个稳定编号的方案选择。")
    issue = params.decision_issue
    if not issue:
        return _main_model(route, reason_kind="missing_decision_issue",
            reason="技术选择缺少稳定的 decision_issue；主模型应先整理问题编号和候选方案。")

    fallback_key = (getattr(agent, "session_id", ""), issue)
    if fallback_key in getattr(agent, "_decision_main_fallback", set()):
        return _main_model(route, reason_kind="bounded_attempts_exhausted",
            reason="该技术问题已用完辅助选择额度；由主模型继续分析，不询问用户。")

    runtime = getattr(agent, "decision_runtime", None)
    if runtime is None:
        return _main_model(route, reason_kind="selector_unavailable",
                           reason="没有可用的辅助选择器，由主模型继续判断。")
    previous = runtime.store.latest(runtime.scope, issue)
    if previous and previous.get("status") == "escalate":
        if not hasattr(agent, "_decision_main_fallback"):
            agent._decision_main_fallback = set()
        agent._decision_main_fallback.add(fallback_key)
        return _main_model(route, reason_kind="bounded_attempts_exhausted",
            reason="Jev 的有限尝试已结束；由主模型继续分析，不把技术问题升级给用户。")

    if len(params.questions) != 1 or not 1 <= len(params.questions[0].options) <= 12:
        return _main_model(route, reason_kind="invalid_candidate_set",
            reason="技术选择需要整理成一个问题和 1 至 12 个候选方案；主模型先整理候选，不询问用户。")

    question = params.questions[0]
    candidate_rows = []
    for index, option in enumerate(question.options):
        description = (option.label + (": " + option.description if option.description else "")
                       if isinstance(option, QuestionOption) else option)
        candidate_rows.append({"id": f"option_{index}", "description": description})

    registry = getattr(agent, "registry", None)
    decision_tool = registry.get("DecideNextStep") if registry is not None else None
    checker = getattr(agent, "permission_checker", None)
    try:
        permitted = (registry is None or registry.is_enabled("DecideNextStep"))
        if permitted and checker is not None:
            permitted = checker.check(decision_tool, {
                "spec": "option-selector", "issue": issue, "goal": question.message,
                "state": params.decision_state, "candidates": candidate_rows,
                "evidence_refs": params.evidence_refs,
            }).effect == "allow"
    except Exception:
        permitted = False
    if not permitted:
        if not hasattr(agent, "_decision_main_fallback"):
            agent._decision_main_fallback = set()
        agent._decision_main_fallback.add(fallback_key)
        return _main_model(route, reason_kind="decision_not_permitted",
            reason="当前范围不允许调用辅助选择器；主模型继续判断，不转问用户。")

    async def selector(rows, _context):
        request = DecisionInput(issue=issue, goal=question.message,
            state=params.decision_state,
            candidates=[Candidate(id=row["id"], description=row["description"],
                                  eligible=row.get("eligible", True)) for row in rows],
            evidence_refs=params.evidence_refs)
        result = await runtime.decide(request)
        if result.get("status") == "selected":
            return {**result, "selected_option_id": result.get("choice")}
        return result

    result = await resolve_handling(route, candidates=candidate_rows,
                                    selector=selector, context=params.decision_state)
    selector_status = result.get("selector_status")
    if result["status"] == "main_model" and selector_status in {
            "main_model", "escalate", "failure", "invalid_selection"}:
        if not hasattr(agent, "_decision_main_fallback"):
            agent._decision_main_fallback = set()
        agent._decision_main_fallback.add(fallback_key)
    if result["status"] == "selected":
        selected_id = result["selected_option_id"]
        index = int(selected_id.removeprefix("option_"))
        option = question.options[index]
        result["suggested_option"] = option.model_dump() if isinstance(option, QuestionOption) else option
        result["source"] = "jev_advisory_not_user_answer"
    return _result(result)