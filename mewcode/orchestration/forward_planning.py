"""Shared forward-planning guidance and a small projection of actual progress."""
from __future__ import annotations

from mewcode.kernel import collaboration as ledger


FORWARD_PLANNING_RULES = (
    "【按复杂度选择执行方式】开始执行前，依据用户目标、输入规模、交付物之间的独立性、"
    "依赖关系和验证边界判断是否值得拆分；有两个以上可独立交接并验证的工作单元时，"
    "优先在反复重试或上下文耗尽前用可用的 ProposeNodeExpansion 请求子任务运行。"
    "宿主复核通过后自动运行子任务，无需用户另开多 Agent 开关。简单工作直接完成。"
    "每项分工声明输入范围、禁止修改项、交付物与验证方式；独立只读任务可并行，"
    "共享写入、命令或浏览器状态操作按依赖串行，主节点保留整合责任，质量问题在交付时集中复核。"
    "以可供后续使用的初稿为阶段交付；一般质量问题写入待修订项，内部格式问题自行纠正。"
    "执行交付、可供下游审阅和最终验收是不同状态；按宿主 handoff_policy 引用具体检查及版本。"
    "已有可审阅产物但质量或证据待复核时提交 ready_for_review 与 unmet_checks，保留未知和未通过结论；"
    "不得仅因可延后检查未通过就声明执行受阻，也不得把可交接解释为达到质量阈值或放行全量。"
    "交接按对后续执行的实际影响分主次：指定模型不可用、下游必需材料无法提供时，停止受影响工作并修复；"
    "可后补的说明、排版、优化与非必需材料不阻止后续执行，保留待修订项，不新增重复任务或问题卡。"
    "普通文件修正直接执行并做相关小验证，不为同一次修正拆出证据审计、对账报告和交接证明等重复节点。"
    "先查看 budget.execution.decomposition：每个孩子需一次执行额度，父汇总另需一次；"
    "当前尝试已计入已用或预留，重试余量另算。拆分不能重置谱系或自动追加总额度。"
    "allocation_mode=elastic 时局部次数只是统计，子任务、修订与重试共享总任务额度，"
    "不因局部累计尝试或失败次数停下；根据真实新证据自主继续、换路线或拆分，"
    "仍遵守总任务额度与已配置的 Token、时间约束。没有新的可执行路线时如实说明，"
    "不能把弹性额度用于重复无进展动作。"
    "超过 max_children 时合并可合并的分工；连两个孩子加父汇总也容不下时，"
    "若完整合同仍可直接执行则继续当前尝试，否则说明最小缺口并等待原预算恢复入口。"
    "【遇阻后的后续规划】计划是达到用户目标的路线，不必把最初的每个步骤都修到可用。"
    "方法已不可行、节点频繁阻塞或同一证据反复失败时，重新判断节点内容和后续依赖，"
    "比较换方法、缩小当前验证、增补适配或补偿节点，以及重排尚未开始的后续工作；不要只重复原动作。"
    "优先复用已有产物和仍有效的验收。需要推翻旧做法时，可以保留旧记录，在后续新增节点完成替代和衔接；"
    "仅对真正受影响的输入做增量验证，不因换路线自动重做全部前置。"
    "选择一个能区分路线的小验证，写明待验证假设、最小样例、成功依据和失败后的替代方向；"
    "已有有效证据足够时直接复用，验证成立即推进，把尚不影响当前决策的细节留给后续节点。"
    "小验证只决定路线，不冒充最终交付或整体验收。"
    "节点内可自主调整未被用户固定的方法；新增节点用可用的 ProposeNodeExpansion，"
    "后续结构改动走宿主的版本化改图或滚动规划入口，明确复用项、影响范围与下一交接。"
    "提议不等于图已修改。保持用户最终目标、明确约束、模型和累计预算；新节点不重置旧额度。"
    "旧失败和未知效果照实保留，只让实际依赖这些结果的工作等待核验。"
)


def reassessment_context(value, contract_id):
    """Highlight repeated failure without importing old prose or changing state."""
    attempts = ledger.attempts_for(value, contract_id)
    failed = [row for row in attempts if row.get('status') == 'rejected'
              and not row.get('superseded')]
    if len(failed) < 2:
        return None
    contract = value['contracts'][contract_id]
    inputs = contract.get('bound_dependencies', contract.get('depends_on', []))
    accepted = []
    for key in inputs:
        if key in value['contracts'] and ledger.valid_certificate(value, key):
            cert = value['certificates'][key]
            accepted.append({'contract_id': key, 'contract_version': cert['contract_version'],
                             'report_hash': cert['report_hash']})
    downstream = ledger.dependent_contracts(value, [contract_id]) - {contract_id}
    unopened = [key for key in value['contracts']
                if key in downstream and not ledger.attempts_for(value, key)]
    return {
        'reason': 'repeated_blocking',
        'failed_attempt_count': len(failed),
        'recent_failed_attempt_ids': [row['id'] for row in failed[-4:]],
        'historical_failures_are_not_current_blockers': True,
        'reusable_accepted_inputs': accepted,
        'unstarted_successor_ids': unopened[:12],
        'unstarted_successor_count': len(unopened),
        'next_decision': '选择不同路线及一个足够的小验证；可增补后续节点，不自动重做已有前置。',
        'publication': '通过实际可用的宿主改图入口采用；本投影不更改合同或预算。',
    }
