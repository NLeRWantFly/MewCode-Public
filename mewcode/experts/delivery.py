"""Content delivery is separate from file mutation and human decisions."""
from __future__ import annotations

import copy


def tool_scenario_prompt(role, tool_names, *, assistance_session=False):
    """A short next-action guide derived only from this runtime's enabled tools."""
    names = set(tool_names)
    scenario = ('协助回执' if assistance_session else '循环审查' if 'SubmitLoopDecision' in names
                else '独立审查' if 'SubmitVerdict' in names and 'SubmitHandoff' not in names else '节点执行与交付')
    lines = [f'\n【当前工具调用摘要：{scenario} / {role}】']
    if 'ReadStageResult' in names:
        lines.append('读取输入：ReadStageResult(node_id="") 查当前来源索引，再按准确 ID 分页；用返回的 NEXT_OFFSET 续读。其他节点报告中提到的来源不自动属于本节点；协助后读取当前回执。上游摘要不代替完整交付。')
    if 'FindSkills' in names:
        lines.append('查找操作指引：FindSkills(query="任务关键词")；无匹配可用空 query 查看本地目录。')
    if 'LoadSkill' in names:
        lines.append('加载匹配的指引：LoadSkill(name="目录中的准确名称")；成功后遵循已加载 SOP。本编排节点只加载指引，工具与权限不增加。')
    if 'ToolSearch' in names:
        lines.append('查找延迟工具：ToolSearch(query="select:准确工具名") 返回完整参数格式；未找到不代表已暴露工具不存在。')
    if 'InvokeDeferredTool' in names:
        lines.append('调用已发现的延迟工具：InvokeDeferredTool(name="准确名称", arguments={按发现结果填写})；原有权限与校验仍适用。')
    if 'RequestAssistance' in names:
        lines.append('内部障碍：RequestAssistance(kind="diagnosis", need="已做工作、具体障碍和需要协助事项")。只有具体调查提权才用 investigation 与 scope_requests。返回 pending 后让出节点，等宿主回执，不再提交 completed 或重复请求。')
    if 'SubmitAssistanceReceipt' in names:
        lines.append('回交协助：SubmitAssistanceReceipt(next_step="resume", notes="实际发现与下一步")；需修订选 rebase，需用户决定选 ask_human，无法恢复选 fail。无宿主事实不填写 clearances。')
    if 'SubmitHandoff' in names:
        lines.append('保存本段交付：SubmitHandoff(summary="实际结果", inline_outputs=[{name:"交付名称",content:"完整正文"}])；默认 completed 仅用于本职责实际完成。仍有已授权待办则 needs_attention + internal_work + open_items。协助节点先交回执再交接。')
    if 'ReviewWorkQueue' in names:
        lines.append('批量审阅：ReviewWorkQueue 冻结完整清单，next 领小批，读取实际证据后逐项 record；完整台账通过后再作最终判定。')
    if 'SubmitLoopDecision' in names:
        lines.append('本轮判定：SubmitLoopDecision 使用当前 contract_hash，逐项引用证据；团队内缺口选 CONTINUE，全部 PASS 才 EXIT。')
        if 'SubmitHandoff' in names:
            lines.append('本节点同时需要 SubmitHandoff 和 SubmitLoopDecision，两者都成功才结束本次审查；先后顺序不限，成功的提交不重复。交接 completed 只表示本次审查和报告交付完成，报告发现缺陷时循环判定仍须如实 CONTINUE；不得把完成审查等同于检查全部 PASS。')
    elif 'SubmitVerdict' in names:
        lines.append('独立判定：SubmitVerdict 按冻结检查表给出 PASS 或带稳定 issue_id 的 NEEDS_ATTENTION；自由文本不算验收。')
    lines.append('任何工具返回错误时，以错误说明修正当前调用；收到成功回执后再推进，不能把请求当作已经执行。')
    return '\n'.join(lines) + '\n'


def delivery_prompt(role, tool_names, *, assistance_session=False):
    writes = sorted(set(tool_names) & {"WriteFile", "EditFile", "Bash"})
    decision = ("SubmitLoopDecision" if "SubmitLoopDecision" in tool_names else
                "SubmitVerdict" if "SubmitVerdict" in tool_names else "")
    common = (tool_scenario_prompt(role, tool_names, assistance_session=assistance_session)
        + "\n【交付形式与实际能力】\n"
        f"当前角色：{role}；本地写入与命令工具：{', '.join(writes) or '无'}。角色是职责分工，权限以全局检查为准。"
        "先读本节点当前 epoch 的回执和上次进度，复用完成结果；未知写入效果先独立核验。\n")
    if "SubmitHandoff" not in tool_names:
        if not decision:
            return common
        return common + (
            "用 " + decision + " 提交实际判定；证据完整且符合冻结验收时通过，否则列出缺陷或 UNKNOWN。"
            "只读审查的缺陷交给执行节点返修。"
            + ("批量审阅使用 ReviewWorkQueue 冻结任务清单及 SHA256，每次 next 领取小批。"
               "图片与 PDF 用 ReadAuditArtifact，criterion_id 使用 item_id；文本用 evidence(source_path, source_selector)，"
               "record(evidence_ids) 引用取得的证据。完成一项立即 record；需返修时 CONTINUE 并给 repair_node_ids。"
               "needs_repair 与 unknown 保留为未完成，最终判定依据完整台账。" if "ReviewWorkQueue" in tool_names else ""))
    return common + (
        "分析表、方案和建议用 SubmitHandoff.inline_outputs 提交完整名称与正文，宿主持久化，下游通过 ReadStageResult 读取。"
        "未要求独立文件时，内容完成可提交 completed 且 artifacts 为空；明确要求文件或执行操作时，提供对应实际产物和回执。"
        "能力缺失使用 capability_missing；普通待办放 open_items。分段工作用 needs_attention + internal_work + open_items；"
        "真实业务问题用 user_input 和具体 question，外部依赖据实列 blockers。"
        "已准备完整正文且只剩落盘时，可用 file_requests 指定 output_name 与相对 path。"
        "只读探测可用 command_requests 给出 command、purpose、input_scope、forbidden_changes、deliverables，"
        "verification_command 与 command 相同；探测结果先回传本节点，再根据结果提出文件请求。"
        "command_receipts 记录未派发、未启动、在途、完成和结果未知；门禁拒绝不是测试失败。"
        "交接形式问题用 delivery_format，内容和操作的完成以真实证据为准。"
        "本节点完成意味着本职责已执行；整体交付仍由独立审查与最终汇总确认。")


def handoff_error_kind(handoff):
    value = handoff or {}
    if value.get("status") not in {"blocked", "needs_attention"} and not value.get("blockers"):
        return ""
    if value.get("question"):
        return "awaiting_input"
    return {"delivery_format": "handoff_format", "capability_missing": "capability_missing",
            "user_input": "awaiting_input", "external_dependency": "external_dependency",
            "internal_work": "internal_work"}.get(value.get("blocker_kind"), "")


def prior_handoff_source(node_id, stage, *, input_revision=None, archives=None):
    """Expose only this node's prior delivery, not its conversation or siblings."""
    visited = set()
    while stage:
        source = _stage_handoff_source(node_id, stage, input_revision=input_revision)
        if source:
            return source
        # Starting a new attempt archives and clears its predecessor's tools.
        # Follow only the host's direct archive link, retaining revision/epoch
        # identity. A newer submitted result (even a rejected one) supersedes
        # older content; never search arbitrary archives for a favorable result.
        archive_id = stage.get('last_archive_id')
        if (not archives or not archive_id or archive_id in visited
                or stage.get('status') == 'completed'
                or (stage.get('handoff') or {}).get('status') == 'completed'
                or any(row.get('name') == 'SubmitHandoff' for row in (stage.get('tools') or {}).values())):
            return None
        archived = archives.get(archive_id) or {}
        previous = archived.get('data') or {}
        if (archived.get('node_id') != node_id or not previous
                or previous.get('epoch', 0) != stage.get('epoch', 0)
                or not isinstance(previous.get('node_attempt'), int)
                or previous['node_attempt'] > int(stage.get('node_attempt') or 0)
                or previous.get('handoff_input_revision', previous.get('input_revision'))
                   != stage.get('handoff_input_revision', stage.get('input_revision'))):
            return None
        visited.add(archive_id)
        stage = previous
    return None


def _stage_handoff_source(node_id, stage, *, input_revision):
    handoff = stage.get("handoff") or {}
    if stage.get("status") == "completed" or handoff.get("status") == "completed":
        return None  # A later queue item must not inherit a finished item's analysis.
    source_revision = stage.get("handoff_input_revision", stage.get("input_revision"))
    if input_revision and source_revision and source_revision != input_revision:
        return None  # Changed upstream inputs require fresh analysis, not a format correction.
    if handoff.get('blocker_kind') == 'delivery_format':
        # A later slice can miss its final submission after an earlier slice
        # successfully recorded a pending delivery. Preserve that actual body
        # as a previous source; it is not a new submission or an accepted result.
        from mewcode.experts.completion import previous_submitted_handoff
        handoff = previous_submitted_handoff(stage, input_revision=input_revision) or handoff
    from mewcode.experts.command_receipts import stage_command_receipts
    receipts = stage_command_receipts(node_id, stage)
    if not any(handoff.get(key) for key in ("inline_outputs", "decisions", "artifacts", "command_requests", "file_requests")) and not receipts:
        return None
    return copy.deepcopy({"node_id": node_id, "status": stage.get("status"),
        "record_kind": "previous_attempt_not_current_acceptance",
        "input_revision": source_revision, "handoff": handoff,
        "command_receipts": receipts})


def recovery_action(kind):
    if kind == "awaiting_input":
        return "请回答具体问题后发送“继续”；已有成果保留，不重跑整图。"
    if kind == "capability_missing":
        return "请按上述缺口调整节点交付要求或配置已授权的能力后继续；原样重试不会补齐工具。已有成果保留。"
    if kind == "handoff_format":
        return "一次交接形式补交仍未完成，已保留内容；请核对交付要求后继续，不需要重新批准已授权的分析。"
    return "已保留成果与具体停止原因；处理对应问题后可继续，从未完成节点接续，不重跑整图。"
