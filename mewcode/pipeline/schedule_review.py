"""定时任务逐轮审查：结构检查、Agent 提示词与回复解析。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from mewcode.pipeline.models import NodeStatus, PipelineRunRecord, RunStatus


@dataclass(frozen=True)
class ScheduleReviewDecision:
    passed: bool
    reason: str
    source: str


def structural_review(
    record: PipelineRunRecord,
) -> ScheduleReviewDecision | None:
    """先做不依赖模型的硬检查；结构正常时返回 None 交给 Agent 复核。"""
    if record.status != RunStatus.COMPLETED:
        reason = record.error_message or f"运行状态为 {record.status.value}"
        return ScheduleReviewDecision(False, reason, "structure")

    failed = [
        node_id
        for node_id, node in record.node_records.items()
        if node.status == NodeStatus.FAILED
    ]
    if failed:
        return ScheduleReviewDecision(
            False,
            "存在失败节点：" + "、".join(failed[:8]),
            "structure",
        )
    return None


def build_schedule_review_prompt(
    criteria: str,
    run_summary: dict,
) -> str:
    """构造独立审查提示词；允许 Agent 使用工具核实真实交付物。"""
    summary = json.dumps(run_summary, ensure_ascii=False, indent=2)
    return (
        "你是定时任务的独立审查员。请判断本轮运行是否真正达成目标，"
        "不能把‘已经开始’‘尝试完成’或仅描述过程当成完成。\n"
        "如有工具可用，应核实文件、通知或其他可验证的交付物。"
        "审查阶段只做只读核实，不得重复发送、修改、创建或删除任何内容。\n\n"
        "瞬时动作（例如通知、Toast、弹窗、声音）应审查它在本轮执行时是否"
        "成功调用，不要求窗口、声音或临时进程在审查时仍然存在。"
        "若运行记录包含成功状态、退出码、进程 ID 或系统调用结果等具体证据，"
        "不得仅因瞬时对象随后关闭而判定失败；只有缺少当次调用证据或存在明确"
        "失败证据时才应拒绝。\n\n"
        f"## 审查标准\n{criteria}\n\n"
        f"## 本轮运行记录\n{summary}\n\n"
        "只输出一个 JSON 对象，不要 Markdown："
        '{"passed":true或false,"reason":"一句具体理由"}'
    )


def reconcile_ephemeral_review(
    decision: ScheduleReviewDecision,
    criteria: str,
    run_summary: dict[str, Any],
) -> ScheduleReviewDecision:
    """纠正“瞬时对象审查时已消失”造成的假失败。

    只在目标明确是瞬时动作、运行记录带有具体成功凭据，并且 Agent 的拒绝
    理由确实是事后不可见时生效。真正的命令失败或单纯口头声称完成不会被放行。
    """
    if decision.passed:
        return decision

    target = criteria.casefold()
    ephemeral_hints = (
        "通知", "弹窗", "提醒", "toast", "notification", "notify", "声音", "播放",
    )
    if not any(hint in target for hint in ephemeral_hints):
        return decision

    reason = decision.reason.casefold()
    expiry_hints = (
        "已结束", "不再存在", "已经退出", "已退出", "已关闭", "不存在",
        "无法确认", "未持续", "当前未发现", "no longer", "not running",
    )
    if not any(hint in reason for hint in expiry_hints):
        return decision

    node_results = run_summary.get("node_results", {})
    evidence = json.dumps(node_results, ensure_ascii=False).casefold()
    success_hints = (
        "已显示", "发送成功", "调用成功", "执行正常", "成功创建", "成功触发",
        "exit code 0", "退出码 0", "返回码 0", "succeeded", "success",
    )
    concrete_hints = (
        "进程 id", "process id", "pid", "exit code", "退出码", "返回码",
        "状态码", "系统调用", "api",
    )
    if not (
        any(hint in evidence for hint in success_hints)
        and any(hint in evidence for hint in concrete_hints)
    ):
        return decision

    return ScheduleReviewDecision(
        True,
        "运行记录包含本轮瞬时动作的具体成功凭据；弹窗或临时进程随后退出不代表未触发",
        "agent-ephemeral-evidence",
    )


def parse_schedule_review_reply(reply: str) -> ScheduleReviewDecision | None:
    """解析 Agent 的 JSON 审查结论，并兼容简单 PASS/FAIL 回复。"""
    text = reply.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start : end + 1])
        except (json.JSONDecodeError, TypeError):
            data = None
        if isinstance(data, dict) and isinstance(data.get("passed"), bool):
            reason = str(data.get("reason") or "Agent 未提供理由").strip()
            return ScheduleReviewDecision(
                data["passed"],
                reason[:500],
                "agent",
            )

    upper = text.upper()
    if upper.startswith("PASS"):
        return ScheduleReviewDecision(True, text[4:].lstrip(" ：:")[:500], "agent")
    if upper.startswith("FAIL"):
        return ScheduleReviewDecision(False, text[4:].lstrip(" ：:")[:500], "agent")
    return None
