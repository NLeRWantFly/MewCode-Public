"""Check stage execution claims, independently of prose and final acceptance."""
from __future__ import annotations

import json
from pathlib import Path


def rejected_verdict_correction(verdict, tools, state, *, input_revision, node_attempt, epoch=0):
    """Give an auditor bounded correction turns without converting errors to questions."""
    if verdict is not None:
        return ''
    binding = [input_revision, node_attempt, epoch]
    if state.get('binding') != binding:
        state.clear()
        state.update(binding=binding, attempts=0)
    if state['attempts'] >= 2:
        return ''
    rows = [row for row in tools.values() if row.get('node_attempt') == node_attempt and row.get('epoch', 0) == epoch]
    if any(row.get('status') in {'queued', 'running', 'waiting_permission', 'outcome_unknown'} for row in rows):
        return ''
    state['attempts'] += 1
    return ('本轮尚无被接收的 SubmitVerdict，结束回复不能代替审计提交。'
            '依据宿主最新 state 的提交要求和纠错动作，直接复用当前草稿及有效证据提交。'
            '仅缺少草稿字段或当前要求时，定向读取 ReadWorkflowContext 的 submission 或 requirements；无需重读旧历史。'
            '仅本次必需集合中的引用才可提交；空集合用空对象。'
            '真实质量问题以 NEEDS_ATTENTION 和具体问题如实提交，不得伪造 PASS；格式纠正不需要用户选择。'
            '当前权限、独立证据边界和剩余预算不变。')


def rejected_handoff_correction(handoff, tools, state, *, input_revision, node_attempt, epoch=0,
                                require_handoff=True, require_loop_decision=False, loop_decision=None):
    """Let a model submit or repair its delivery before ending the role call.

    Bound corrections across resumed slices. This neither accepts the rejected
    content nor bypasses permission, output, iteration or time limits.
    """
    missing_handoff = require_handoff and handoff is None
    missing_decision = require_loop_decision and loop_decision is None
    # A partial/blocked delivery is an intentional yield, not an invitation to
    # invent a final review decision merely to satisfy the submission protocol.
    if handoff is not None and (handoff.get('status') != 'completed'
                               or handoff.get('blockers') or handoff.get('question')):
        return ''
    if not missing_handoff and not missing_decision:
        return ''
    rows = [row for row in tools.values() if row.get('node_attempt') == node_attempt and row.get('epoch', 0) == epoch]
    if any(row.get('status') in {'queued', 'running', 'waiting_permission', 'outcome_unknown'} for row in rows):
        return ''
    submissions = sorted((row for row in rows if row.get('name') == 'SubmitHandoff'),
                         key=lambda row: row.get('declared_at') or row.get('started_at') or 0)
    binding = [input_revision, node_attempt, epoch]
    if state.get('binding') != binding:
        state.clear()
        state.update(binding=binding, attempts=0)
    if state['attempts'] >= 2:
        return ''
    state['attempts'] += 1
    reason = ('SubmitHandoff 被拒绝，尚未登记交付。请按最近一次工具反馈修正后重交。'
              if submissions and submissions[-1].get('is_error') else
              '本次调用尚无有效 SubmitHandoff。历史交接仅供复用，不能代替本次产物状态；'
              '请核对已有产物并提交实际状态和证据。已有完整正文只缺落盘时，使用当前允许的文件工具完成并回读。')
    if not missing_handoff:
        reason = '本次交接已登记，不要重复提交或重跑已完成的操作。' if require_handoff else ''
    if missing_decision:
        reason += ('本轮仍缺有效 SubmitLoopDecision，请按当前冻结合同逐项提交实际证据与判定；'
                   '交接完成只表示本职责已执行，不等于全部检查 PASS。缺口必须如实保留，不能为结束调用改成 EXIT。')
    elif require_loop_decision:
        reason += '本轮 SubmitLoopDecision 已登记且不可覆盖；现在只补交接。'
    return (reason + '不要用结束回复代替纠错，不重跑已完成的操作。'
            + ('尚有已授权工作则继续执行，或提交 needs_attention + internal_work + 具体 open_items；'
               '真实阻塞按其类型如实交接。' if require_handoff else '') +
            '不得将未完成状态改成完成，当前权限、合同和剩余预算不变。')


def previous_submitted_handoff(stage, *, input_revision=None):
    """Recover evidence for a previous-delivery source, never current acceptance."""
    revision = stage.get('handoff_input_revision', stage.get('input_revision'))
    if input_revision is not None and (not revision or revision != input_revision):
        return None
    if not revision or not stage.get('node_attempt'):
        return None
    rows = [row for row in (stage.get('tools') or {}).values()
            if row.get('name') == 'SubmitHandoff' and row.get('node_attempt') == stage['node_attempt']
            and row.get('epoch', 0) == stage.get('epoch', 0)
            and row.get('input_revision', revision) == revision]
    if not rows:
        return None
    row = max(rows, key=lambda row: row.get('declared_at') or row.get('started_at') or 0)
    if row.get('status') != 'completed' or row.get('is_error') is not False:
        return None
    from mewcode.tools.submit_handoff import SubmitHandoffParams
    try:
        return SubmitHandoffParams.model_validate(row.get('args') or {}).model_dump()
    except ValueError:
        return None


def handoff_blocker(handoff, *, work_dir=None):
    """A declared blocker or missing local artifact cannot count as completion.

    This does not certify semantic correctness. Independent audit still owns
    acceptance. Never infer completion by parsing the assistant's prose.
    """
    if not isinstance(handoff, dict):
        return ""
    status = handoff.get("status")
    if status in {"blocked", "needs_attention"} or handoff.get("blockers"):
        detail = "; ".join(str(x) for x in handoff.get("blockers") or [status])
        if handoff.get("question"):
            detail += "；需要你回答：" + str(handoff["question"])
        return "本节点交接声明未完成：" + detail
    if not work_dir or status != "completed":
        return ""
    root = Path(work_dir).resolve()
    for artifact in handoff.get("artifacts") or []:
        if not isinstance(artifact, dict):
            return "交付文件声明格式无效"
        raw = str(artifact.get("path") or "")
        if not raw:
            return "交付文件路径为空"
        # Remote outputs have separate service evidence. Do not open URLs.
        if "://" in raw:
            continue
        path = Path(raw)
        path = (path if path.is_absolute() else root / path).resolve()
        if not path.is_relative_to(root):
            return "交付文件不在本节点工作目录内，无法核验：" + raw
        if not path.exists():
            return "声明的交付文件不存在：" + raw
        if path.is_dir() and artifact.get("kind", "file") not in {"directory", "folder"}:
            return "声明为文件的产物实际是目录，请修正交接类型：" + raw
        if path.is_file() and path.stat().st_size == 0:
            return "声明的交付文件为空：" + raw
        # A machine-readable blocked handoff is not a delivered manuscript,
        # even when a model claims that writing the JSON completed its task.
        is_status_record = artifact.get("kind") in {"handoff", "delivery_record", "status"} or path.name.casefold() in {
            "handoff.json", "本轮交接.json", "delivery_status.json", "delivery_manifest.json"}
        if is_status_record and path.is_file() and path.suffix.lower() == ".json" and path.stat().st_size <= 1024 * 1024:
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and str(data.get("status") or "").upper().startswith(("BLOCKED", "NEEDS_ATTENTION")):
                return "交付记录仍声明受阻，不能记为完成：" + raw
    return ""
