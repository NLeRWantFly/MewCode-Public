"""Host facts projected into a compact, action-specific Auto presentation."""
from __future__ import annotations

import json


_HANDOFF_BLOCKER_PREFIX = "本节点交接声明未完成："
_EMPTY_BLOCKER_VALUES = frozenset({
    "blocked", "needs_attention", "needs attention", "pending", "paused",
    "failed", "unknown", "none", "n/a", "not_available",
})


def _substantive_blocker(value):
    if not isinstance(value, str):
        return ""
    value = value.strip()
    while value.startswith(_HANDOFF_BLOCKER_PREFIX):
        value = value[len(_HANDOFF_BLOCKER_PREFIX):].strip()
    normalized = value.casefold().replace("-", "_")
    if not value or normalized in _EMPTY_BLOCKER_VALUES:
        return ""
    return value


def capability_problem(stage):
    """Display saved facts, without inferring a clearance or a new authority."""
    from mewcode.permissions.reviewer import redact_sensitive

    handoff = stage.get("handoff") or {}
    messages = [message for value in handoff.get("blockers") or []
                if (message := _substantive_blocker(value))]
    receipt = handoff.get("escalation_receipt") or {}
    if isinstance(receipt, dict):
        detail = receipt.get("error") or receipt.get("text")
        if detail:
            messages.append(str(detail))
    # A later successful execution of the same call retires its old failure.
    # Failed parameter validation is not evidence that the tool is missing.
    failures = {}
    tools = stage.get("tools") or {}
    rows = tools.values() if isinstance(tools, dict) else tools if isinstance(tools, list) else ()
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        if name not in {"Bash", "RunVerification", "TerminalStart", "ReadAuditArtifact", "ProbeCapabilities"}:
            continue
        key = (name, json.dumps(row.get("args") or {}, sort_keys=True, ensure_ascii=True, default=str))
        if row.get("status") == "completed" and not row.get("is_error"):
            failures.pop(key, None)
        elif row.get("status") == "failed" or row.get("is_error"):
            output = str(row.get("output") or "").strip()
            if output and not output.startswith("Parameter validation error:"):
                failures[key] = (name, output)
    seen_outputs = set()
    for name, output in reversed(list(failures.values())):
        if output in seen_outputs:
            continue
        seen_outputs.add(output)
        messages.append(f"{name}：{output[:350]}")
        if len(seen_outputs) >= 3:
            break
    messages = list(dict.fromkeys(redact_sensitive(value)[:350] for value in messages))[:6]
    requests = [row for row in handoff.get("command_requests") or [] if isinstance(row, dict)]
    deliveries = [redact_sensitive(str(row.get("purpose") or row.get("command") or ""))[:300]
                  for row in requests[:2]]
    deliveries += [redact_sensitive(str(row.get("output_name") or row.get("path") or ""))[:200]
                   for row in handoff.get("file_requests") or [] if isinstance(row, dict)][:2]
    label = str(stage.get("label") or stage.get("node_id") or "当前节点")
    reason = f"节点「{label}」受阻：" + (
        "；".join(messages) if messages else "交接未说明具体缺少哪项能力，需要补充缺口说明。")
    if deliveries:
        reason += "\n待完成：" + "；".join(value for value in deliveries if value)
    if any("隔离执行器" in message or "native execution boundary" in message for message in messages):
        next_step = "先恢复已授权的隔离命令执行通道，再执行节点提交的探测并回填回执；切换模型仍保留原工具权限。"
    elif requests:
        next_step = "将节点提交的具体命令交给已授权且可用的执行通道，核验回执后接续未完成交付。"
    elif handoff.get("file_requests"):
        next_step = "配置已授权且可用的辅助执行器，按文件交付请求核验产物后接续。"
    elif messages:
        next_step = "按上述具体缺口补齐能力或通过正常流程调整节点分工，再从检查点接续。"
    else:
        next_step = "补充具体缺少的工具、受影响的交付和失败回执，再确定接续方法。"
    return {"node_id": str(stage.get("node_id") or ""), "node_label": label,
            "messages": messages, "deliverables": deliveries,
            "reason": reason[:1800], "next_step": next_step}


def auto_card(decision, stage, *, marker=None, health=None):
    case = stage.get('recovery_case') or {}
    if case.get('report') and case.get('status') in {'reported', 'awaiting_user'}:
        return {'schema_version': 1, **case['report'], 'phase': 'paused',
            'next_step': '请在恢复案件卡中选择具体处理方式；继续任务不会追加轮数。'}
    action = decision.get("action") or "human"
    normal = action == "continue_checkpoint" and decision.get("source") == "host_progress_checkpoint"
    stopped = action in {"human", "hold_until_clearance"}
    kind = "continuation" if normal else "blocked" if stopped else "recovery"
    titles = {"continue_checkpoint": "继续处理下一批" if normal else "接续当前检查点",
        "diagnose_path": "调整执行方法", "reduce_execution_profile": "缩小下一批工作",
        "switch_declared_route": "启用已配置的备用路线", "human": "等待具体处理",
        "hold_until_clearance": "已暂停，进度已保存"}
    handoff = stage.get("handoff") or {}
    health = health or stage.get("segment_health") or {}
    delta = health.get("work_delta") or {}
    progress = []
    labels = {"review_complete": "复核完成", "review_recorded": "新增审阅记录",
        "write_receipts": "文件版本", "submitted_items": "已提交项",
        "observed_sources": "新来源片段", "inline_outputs": "内容交付"}
    for key, label in labels.items():
        value = delta.get(key)
        if isinstance(value, int) and value > 0:
            progress.append({"label": label, "value": value, "scope": "本段新增"})
    reason = (decision.get("error") or (decision.get("diagnostic_card") or {}).get("boundary_reason")
        or ("本段已保存新进展，仍有本节点待办。" if normal else decision.get("host_check"))
        or "正在核验接续条件")
    next_step = str((decision.get("chosen") or {}).get("next_step") or "")
    if normal and handoff.get("open_items"):
        next_step = str(handoff["open_items"][0])
    if stopped:
        next_step = str(handoff.get("question") or reason)
        kinds = {row.get("kind") for row in decision.get("clearance_conditions") or [] if isinstance(row, dict)}
        if "unknown_effect" in kinds:
            reason = "有工具调用的结果尚未确认，检查点已保存。"
            next_step = "先核对这些调用的实际结果，再接续未完成工作。"
        elif "permission" in kinds:
            reason = "当前步骤还在等待操作授权。"
            next_step = "处理节点的授权请求后，接续未完成工作。"
        elif kinds & {"human_question", "user_input"}:
            reason = "节点需要你的回答才能继续。"
            next_step = str(handoff.get("question") or "在节点的问题卡中回答具体问题。")
        elif ("capability_missing" in kinds or decision.get("trigger") == "capability_missing"
              or handoff.get("blocker_kind") == "capability_missing"):
            problem = capability_problem(stage)
            reason, next_step = problem["reason"], problem["next_step"]
    if action == "switch_declared_route":
        target = (decision.get("model_switch") or {}).get("to") or decision.get("model_switch_target") or {}
        if target.get("model_id"):
            effort = {"xhigh": "极高", "high": "高", "medium": "中", "max": "最高"}.get(
                target.get("effort"), target.get("effort") or "默认")
            reason = f"节点「{stage.get('label') or stage.get('node_id') or '当前节点'}」将使用 {target['model_id']}（{effort}）接续未完成工作。"
            next_step = "复用当前检查点和已保存成果，按原工具权限、预算与验收要求继续。"
    note = "本段新增记录已保存；业务验收由审查节点判定。" if normal else "检查点已保留。"
    repairs = [row for row in stage.get("primary_repairs") or []
               if row.get("input_revision") == stage.get("input_revision")]
    if repairs:
        repair = repairs[-1]
        if decision.get("source") == "primary_model_fix":
            titles[action] = "主模型 Fix 后回交验证"
            reason = "主模型已接手诊断与修复，原节点将依据新证据验证并继续未完成工作。"
        elif stopped:
            note += " 主模型已接手诊断与 Fix。"
            if repair.get("summary"):
                reason += "\n主模型结论：" + str(repair["summary"])[:700]
            next_step = "查看主模型 Fix 的实际证据与未解决条件；已有调查和修复成果保留。"
    resume_model = stage.get("configured_resume_model") or {}
    if stopped and resume_model.get("model_id"):
        effort = "极高" if resume_model.get("effort") == "xhigh" else resume_model.get("effort") or "默认"
        note += f" 接续模型已配置为 {resume_model['model_id']}（{effort}）；上述阻塞仍需处理。"
    return {"schema_version": 1, "kind": kind, "title": titles.get(action, "Auto 调度"),
        "reason": str(reason)[:1800], "next_step": next_step[:700], "progress": progress,
        "open_item_count": len(handoff.get("open_items") or []),
        "segment_id": health.get("segment_id", ""),
        "input_revision": stage.get("input_revision", ""),
        "eta": health.get("eta") or {},
        "phase": "paused" if stopped else "scheduled",
        "note": note}
