"""Bounded checkpoint projection shared by normal continuation and Auto cards."""
from __future__ import annotations

from dataclasses import dataclass
import json


@dataclass
class NodeContinuation:
    result: object
    policy: object
    prompt: str
    cursor: dict | None
    productive: bool


def checkpoint_prompt(stage, *, node_id, run=None, requirements=None):
    from mewcode.experts.workflow_context import (
        _bound_requirements, current_handoff, project_working_context,
    )
    view = stage.get("ledger_view")
    handoff = current_handoff(stage)
    working = project_working_context(stage, binding={
        'node_id': node_id, 'input_revision': stage.get('input_revision'),
        'node_attempt': stage.get('node_attempt'), 'epoch': stage.get('epoch', 0)},
        requirements=requirements if requirements is not None else _bound_requirements(stage))
    pending = stage.get('pending_permission') or stage.get('pending_interaction') or {}
    receipts = [f["payload"] for f in view.get("current_receipts", [])] if isinstance(view, dict) else stage.get("command_receipts") or []
    outputs = handoff.get("inline_outputs") or []
    brief = {"node_id": node_id, "epoch": (view or {}).get("epoch", 0), "input_revision": stage.get("input_revision", ""),
        "host_command_receipts": [{key: row.get(key) for key in
            ("request_id", "dispatch_id", "status", "command_started", "exit_code", "evidence_source", "reason")}
            for row in receipts],
        "summary": str(handoff.get("summary") or "")[:1200],
        "next_items": working['next_items'] if working['waiting_for'] == 'agent' else [],
        "next_action": working['next_action'], "waiting_for": working['waiting_for'],
        "open_item_count": len(handoff.get("open_items") or []) if working['waiting_for'] == 'agent' else 0,
        "blockers": [str(item)[:400] for item in (
            view.get('active_blockers', []) if isinstance(view, dict) else handoff.get('blockers') or [])[:8]],
        "question": str(pending.get("question") or "")[:600],
        "outputs": [{"name": str(item.get("name") or "")[:200],
                     "characters": len(str(item.get("content") or ""))}
                    for item in outputs[:32] if isinstance(item, dict)]}
    submission = working.get('submission')
    if working.get('review_protocol'):
        brief['review_protocol'] = working['review_protocol']
    if submission:
        brief['submission_feedback'] = {key: submission.get(key) for key in (
            'tool', 'tool_id', 'status', 'category', 'code', 'draft_ref')}
    from mewcode.experts.authorization_ledger import authorization_block
    return (authorization_block(run or {}, node_id) + "\n当前节点的最新工作检查点（摘要不是授权或验收）：\n"
        + json.dumps(brief, ensure_ascii=False)
        + "\n以最新 state 的 next_action 和当前待办接续，旧对话与旧概要不覆盖当前状态。已有且版本有效的证据直接复用。"
        "格式错误按工具反馈修正后重交；软截止仅交棒，不是业务阻塞。必要时缩小批次或调整执行顺序。"
        "仅当前必需内容缺失或版本变化时按具体引用定向读取，不重读历史恢复进度。"
        "命令状态以同输入版本的最新宿主回执为准，旧交接文字不能覆盖宿主对账结果；请求身份与每次dispatch身份须区分。")


def turn_guidance(stage, state):
    """Nudge a settled turn to correct or replan; never approve or stop work.

    Only identical host-observed reads count as repetition. A changed result,
    a new page/range, or a productive operation remains ordinary progress.
    State travels with the existing iteration checkpoint across soft slices.
    """
    from mewcode.experts.recovery_diagnostics import is_read_receipt, _resource_key
    from mewcode.experts.recovery_state import digest, tool_rows

    binding = [stage.get('epoch', 0), stage.get('node_attempt'), stage.get('input_revision')]
    if state.get('binding') != binding:
        state.clear()
        state['binding'] = binding
    rows = sorted((row for row in tool_rows(stage)
                   if row.get('node_attempt') == stage.get('node_attempt')),
                  key=lambda row: row.get('declared_at') or row.get('started_at') or 0)
    if not rows or any(row.get('status') in {'queued', 'running', 'waiting_permission', 'outcome_unknown'} for row in rows):
        return None
    last = rows[-1]
    from mewcode.experts.workflow_context import _bound_requirements, latest_submission
    if (stage.get('pending_permission') or stage.get('pending_interaction')
            or _bound_requirements(stage) is not None
            and ((stage.get('workflow_context') or {}).get('snapshot') or {}).get('waiting_for') == 'user'):
        return None
    submission = latest_submission(stage)
    if submission and submission.get('status') == 'accepted':
        return None
    from mewcode.experts.review_protocol import BATCH_TOOLS, protocol_correction
    correction = protocol_correction(stage, rows)
    if correction:
        failed = next(row for row in reversed(rows) if row.get('name') in BATCH_TOOLS and row.get('is_error'))
        marker = digest([stage['review_protocol'], failed.get('tool_id')])
        if state.get('review_protocol_correction') != marker:
            state['review_protocol_correction'] = marker
            return {'kind': 'correct_review_protocol', 'label': '按当前复核协议接续', 'prompt': correction}
        return None
    if (submission and submission.get('tool') == 'SubmitVerdict' and submission.get('status') == 'rejected'
            and submission.get('category') in {'contract_error', 'reference_mismatch'}):
        fingerprint = digest(submission)
        if state.get('verdict_feedback') != fingerprint:
            state['verdict_feedback'] = fingerprint
            return {'kind': 'correct_submission', 'label': '自动修正审计提交',
                'prompt': '当前 state 尚无被接收的审计提交；直接复用已有草稿与有效证据，按当前宿主反馈纠正后重交：'
                          + str(submission.get('next_action') or '')[:1600]
                          + '\n仅当前上下文缺少草稿字段时，用 ReadWorkflowContext(section="submission") 定向读取缺少的部分。'
                            '已读取且版本有效的内容直接复用，不重读历史恢复进度；'
                            '字段纠错不是用户未决事项，真实权限和质量要求仍有效。'}
    if last.get('name') == 'SubmitHandoff' and last.get('is_error'):
        fingerprint = digest([last.get('tool_id'), last.get('args'), last.get('output')])
        if state.get('handoff_feedback') != fingerprint:
            state['handoff_feedback'] = fingerprint
            return {'kind': 'correct_handoff', 'label': '自动修正交接',
                'prompt': '最近一次 SubmitHandoff 未被接受。复用已保存的产物与工具结果，按该工具返回的字段错误修正后重交。'
                          '仅有已授权待办时使用 needs_attention + internal_work + open_items，blockers 留空。'
                          '真实授权、未知执行结果或业务选择仍按真实类型处理；格式错误不需要用户选择，不得把未完成改成完成。'}
        return None
    reads, seen, repeats = [], set(), 0
    for row in rows:
        if row.get('name') in {'report_step', 'ReportStep'}:
            continue
        if row.get('status') != 'completed' or row.get('is_error') or not is_read_receipt(row):
            reads.clear()
            seen.clear()
            repeats = 0
            continue
        key = _resource_key(row, str(stage.get('input_revision') or ''), str(stage.get('node_id') or ''))
        repeated = bool(key and key in seen)
        repeats += repeated
        if key:
            seen.add(key)
        reads.append(repeated)
    if len(reads) < 8 or sum(reads[-8:]) < 4:
        return None
    marker = [len(rows) - len(reads), repeats // 4]
    if state.get('read_loop') == marker:
        return None
    state['read_loop'] = marker
    return {'kind': 'adjust_execution', 'label': '调整下一步执行',
        'prompt': '宿主观察到最近 8 次读取中至少 4 次重复读取相同范围且结果未变化。'
                  '这些调用没有新增交付。依据最新 state 直接复用有效结果，定位当前缺少的前提和下一项可验证产出；'
                  '前提齐全就执行最小工作批次，缺哪一项就只补哪一项。当前路径不可行时，'
                  '在原授权内换方法、拆小批次或先做独立工作；需要改变节点合同则提交现有规划/协助入口。'
                  '不要再次从目录、授权和全量清单开始检查，也不要把内部计划调整转成用户选择。'
                  '本提示不批准被拒绝操作、不允许重放未知结果、不改变模型、权限、验收或累计预算。'}


def decision_prompt(decision):
    brief = {key: decision[key] for key in ("action", "source", "host_check") if key in decision}
    brief["next_step"] = str((decision.get("chosen") or {}).get("next_step") or "")[:1000]
    return "\n宿主接续建议（不改变权限或验收）：" + json.dumps(brief, ensure_ascii=False)


def cumulative_segment_usage(before, current):
    """Add a segment's cumulative counters once; current context stays local."""
    if not current:
        return dict(before)
    value = dict(current)
    for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
                "cumulative_cache_hit_tokens", "cumulative_cache_miss_tokens",
                "cumulative_cache_creation_tokens", "total_cost_usd", "billable_tokens", "recovery_tokens"):
        value[key] = (before.get(key) or 0) + (current.get(key) or 0)
    for key in ("cache_usage_available", "cumulative_cache_usage_available"):
        value[key] = bool(current.get(key)) and (not before or bool(before.get(key)))
    denominator = value.get("cumulative_cache_hit_tokens", 0) + value.get("cumulative_cache_miss_tokens", 0)
    value["cumulative_cache_hit_rate"] = value.get("cumulative_cache_hit_tokens", 0) / denominator if denominator else 0.0
    return value
