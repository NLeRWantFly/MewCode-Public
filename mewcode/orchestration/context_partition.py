"""Bounded, lossless planning packets for oversized collaboration contracts.

Packets are read-only views, not adopted contracts. Every referenced requirement
appears in full in at least one packet; global constraints, methods and recovery
facts appear in every packet. Only reviewed graph publication can narrow a
worker's responsibility. Model notes never replace the source specification.
"""
from __future__ import annotations

import copy
import json

from mewcode.kernel import collaboration as ledger, lineage_budget
from mewcode.orchestration.task_context import build_task_context, ContextBudgetExceeded
from mewcode.orchestration.context_budget import as_budget, model_context_budget


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False))


def context_packet(value, contract_id, requirements, *, budget, common=None, sources=None):
    budget = as_budget(budget)
    parent = value["contracts"][contract_id]
    scoped = {**parent, "requirement_ids": list(requirements)}
    view = {**value, "contracts": {**value["contracts"], contract_id: scoped}}
    envelope = {
        **(common or {}),
        "scope": {
            "kind": "planning_view_not_adopted_contract",
            "parent_contract_id": contract_id,
            "parent_contract_sha256": ledger.digest(parent),
            "parent_basis": ledger.dependencies(value, contract_id),
            "all_requirement_ids": list(parent["requirement_ids"]),
            "included_requirement_ids": list(requirements),
            "entry_sha256": {key: ledger.digest(value["entries"][key]) for key in requirements},
            "rule": "此页只负责列出的需求；全部约束、方法、当前恢复事实仍有效。其他页没有取消任何需求。",
        },
        "context": None,
    }
    # Count the wrapper without the projection slot. JSON nesting/escaping is
    # measured again on the completed packet below.
    wrapper = json.dumps(envelope, ensure_ascii=False)
    remaining = budget.subtract(wrapper)
    if remaining.limit <= 0:
        raise ContextBudgetExceeded("分工包的固定说明已超过上下文预算")
    projection = build_task_context(view, contract_id, role="partition_planner",
                                    sources=sources, context_budget=remaining)
    projection["identity"]["basis_hash"] = envelope["scope"]["parent_basis"]
    envelope["context"] = projection
    if not budget.fits(json.dumps(envelope, ensure_ascii=False)):
        raise ContextBudgetExceeded("分工包已超过上下文预算")
    return envelope


def context_packets(value, contract_id, *, budget, common=None, sources=None):
    """Greedily pack whole entries, failing explicitly on an indivisible entry."""
    keys = value["contracts"][contract_id]["requirement_ids"]
    groups, current = [], []
    for key in keys:
        try:
            context_packet(value, contract_id, [*current, key], budget=budget,
                           common=common, sources=sources)
        except ContextBudgetExceeded:
            if current:
                groups.append(current)
            try:
                context_packet(value, contract_id, [key], budget=budget,
                               common=common, sources=sources)
            except ContextBudgetExceeded as exc:
                raise ContextBudgetExceeded(
                    f"需求 {key} 连同公共目标、约束和恢复事实仍超过预算；需先细化这条需求，未丢弃原文。") from exc
            current = [key]
        else:
            current.append(key)
    if current or not groups:
        groups.append(current)
    return [context_packet(value, contract_id, group, budget=budget, common=common,
                           sources=sources) for group in groups]


PAGE_SYSTEM = (
    "你是当前任务所绑定的执行模型，现在只做分工规划，不执行业务操作。输入是完整规格的一页；"
    "context_encoding 若存在须按 context_notes 无损理解。保留本页全部要求、方法、假设、历史未知项和"
    "恢复指令；不要把部分覆盖声称为全部通过。分析可独立执行的工作、共用文件依赖、交付物和核验方法，"
    "指出需要串行的整合步骤。只返回 JSON {notes:文本}，notes 最多4000字符。此笔记只是规划依据，"
    "子任务与独立复核仍会收到对应完整原文。不得改变模型、累计预算、权限或最终验收。"
)

PLAN_SYSTEM = (
    "根据全部分页规划笔记和完整公共约束，把当前合同分成2至6个实际子任务。只返回微图 JSON："
    "{parent_node_id,expected_flow_version,reason,nodes:[{node_id,label,task,requirement_ids,"
    "input_refs:['parent'],outputs:[交付文本],acceptance:[检查文本],tools:[工具名],write_paths:[路径]}],"
    "edges:[{from_node,to_node,condition:'always'}],join:{label,acceptance:[完整验收检查文本]}}。"
    "acceptance 必须是非空文本列表，即使只有一条检查也要使用数组。"
    "requirement_ids 必须是非空父需求子集，合计覆盖全部需求；跨任务共同要求须分配给每个相关孩子。"
    "每个任务明确输入范围、禁止修改项、交付物与验证方式。所有全局约束和方法自动继承。"
    "子任务写入路径必须互不重叠且属于父写入范围，工具只能来自 allowed_tools。需要汇编文件时，"
    "安排一个依赖其他孩子的整合子任务并为其单独分配输出路径；$join 只汇集交付和做完整验收，"
    "不会替你执行文件合并。每个节点都必须有路径到 $join。不得增加模型、预算或权限。"
    "笔记是规划参考，原始需求没有被替换，发布前还会逐页独立复核。"
)


def planning_proposal(raw):
    """Keep a scalar criterion verbatim as one item; validate everything else."""
    proposal = copy.deepcopy(raw)
    if not isinstance(proposal, dict):
        return proposal
    nodes = proposal.get("nodes")
    rows = list(nodes) if isinstance(nodes, list) else []
    rows.append(proposal.get("join"))
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("acceptance"), str) and row["acceptance"].strip():
            row["acceptance"] = [row["acceptance"]]
    return proposal


async def plan_overflow(runtime, node, model, policy, attempt, *, remaining):
    """Use the bound model to propose a split, then use normal publication."""
    from mewcode.expert_group import ExpertRoleResult
    from mewcode.experts.node_expansion import FORBIDDEN_EXPANSION_TOOLS
    from mewcode.orchestration.collab_fragment import build_expansion_fragment

    holder = policy.expansion_holder
    if holder is None:
        raise ContextBudgetExceeded("当前合同上下文超限且不允许分解；已保留原要求与检查点")
    value = runtime.read()
    parent = value["contracts"][node.node_id]
    budget = model_context_budget(runtime.pool, model, value['policy'], system=PAGE_SYSTEM)
    # The role default is often '*', not an enumerated registry. Before the
    # business runner exists, use only the normal work-directory core tools;
    # each real invocation still passes the live registry and permission gate.
    allowed = {"ReadFile", "Glob", "Grep", "WriteFile", "EditFile", "Bash", "ReadStageResult"}
    allowed -= FORBIDDEN_EXPANSION_TOOLS
    if parent.get("tools") is not None and "*" not in parent["tools"]:
        allowed &= set(parent["tools"])
    allowed -= set(parent.get("deny_tools", []))
    holder.set_allowed_tools(allowed)
    common = {"purpose": "context_overflow_partition", "allowed_tools": sorted(allowed)}
    packets = context_packets(value, node.node_id, budget=budget,
                              common=common, sources=policy.stage_inputs)
    # Reserve only on a copy: preflight the minimum two workers plus their join.
    # The actual dispatches remain charged by the normal scheduler.
    preview = copy.deepcopy(value)
    lineage_budget.reserve(preview, "context-plan-preview:" + attempt["id"],
                           lineage_id=parent.get("lineage_id") or node.node_id,
                           kind="attempts", amount=3)
    from mewcode.kernel.collaboration_work_budget import expansion_capacity
    capacity = expansion_capacity(value, node.node_id)
    max_children = capacity['max_children']
    runtime.mutate(lambda current: ledger.mark_started(current, attempt["id"]))
    notes = []
    for index, packet in enumerate(packets):
        result = await runtime.coordinator.call(kind="coordinator",
            identity=f"context-page:{attempt['id']}:{index}:{ledger.digest(packet)}",
            payload=packet, model=runtime.pool.require(model, "executor"),
            system=PAGE_SYSTEM, related_contracts=[node.node_id])
        note = result.get("notes")
        if not isinstance(note, str) or not note.strip() or len(note) > 4000:
            raise ValueError("分页规划未返回有效的有界笔记，原需求与尝试已保留")
        notes.append({"requirement_ids": packet["scope"]["included_requirement_ids"],
                      "source_sha256": ledger.digest(packet), "notes": note})
    plan_budget = model_context_budget(runtime.pool, model, value['policy'], system=PLAN_SYSTEM)
    planning = context_packet(value, node.node_id, [], budget=plan_budget, sources=policy.stage_inputs,
        common={**common, "purpose": "context_overflow_plan", "page_notes": notes,
                "parent_node_id": node.node_id, "expected_flow_version": runtime.flow.version,
                "execution_budget": capacity,
                "max_children_with_remaining_attempt_budget": max_children})
    raw = await runtime.coordinator.call(kind="coordinator",
        identity="context-plan:" + attempt["id"] + ":" + ledger.digest(planning),
        payload=planning, model=runtime.pool.require(model, "executor"),
        system=PLAN_SYSTEM, related_contracts=[node.node_id])
    proposal = planning_proposal(raw)
    validated = holder.submit(proposal)
    if len(validated.proposal.nodes) > max_children:
        raise ValueError("所提子任务数量超过当前累计尝试预算，未发布也未增加预算")
    fragment = build_expansion_fragment(runtime.read(), runtime.flow, node.node_id, proposal,
        work_dir=runtime.acceptance.work_dir, sandbox=holder.context.sandbox,
        allowed_tools=allowed, node_attempt=attempt["number"])
    preview = copy.deepcopy(runtime.read())
    lineage_budget.reserve(preview, "context-workers-preview:" + attempt["id"],
                           lineage_id=parent.get("lineage_id") or node.node_id,
                           kind="attempts", amount=len(fragment["child_ids"]) + 1)
    preview["contracts"].update(copy.deepcopy(fragment["contracts"]))
    for child_id in fragment["child_ids"]:
        # Future child certificates introduce a small input envelope. Leave
        # room now; a later dispatch still measures its actual full prompt.
        child_budget = as_budget(remaining).subtract(reserve=3000)
        if child_budget.limit <= 0:
            raise ContextBudgetExceeded(child_budget.exceeded())
        build_task_context(preview, child_id, sources=policy.stage_inputs,
                           context_budget=child_budget, compact=True)
    runtime.mutate(lambda current: ledger.event(current, "context_partition_planned",
        contract_id=node.node_id, attempt_id=attempt["id"], pages=len(packets),
        requirement_ids=list(parent["requirement_ids"]), proposal_sha256=ledger.digest(proposal),
        raw_proposal_sha256=ledger.digest(raw)))
    result = ExpertRoleResult(text="上下文已分批规划，等待独立复核后执行子任务。",
                              status="node_expansion", node_expansion=validated.as_dict())
    return await runtime.publish_expansion(node, model, policy, attempt, result)


async def review_expansion(runtime, *, node_id, item, reviewer, identity, system):
    """Review full original requirements in bounded pages, not duplicated DAGs."""
    value = runtime.read()
    common = {"purpose": "context_partition_review", "proposal": item["fragment"]["proposal"],
              "candidate_sha256": ledger.digest(item["fragment"]),
              "authority": "逐页检查当前需求在子任务中完整覆盖、共同约束适用与整合依赖；父合同全部检查保留。"}
    budget = model_context_budget(runtime.pool, reviewer, value['policy'], system=system)
    packets = context_packets(value, node_id, budget=budget, common=common)
    evidence = []
    for index, packet in enumerate(packets):
        result = await runtime.coordinator.call(kind="semantic_review",
            identity=f"graph-page:{identity}:{index}:{ledger.digest(packet)}", payload=packet,
            model=reviewer, system=system, related_contracts=item["affected"])
        if result.get("passed") is not True or not result.get("evidence"):
            return result
        evidence.append({"page": index, "requirement_ids": packet["scope"]["included_requirement_ids"],
                         "payload_sha256": ledger.digest(packet), "review": result})
    return {"passed": True, "evidence": evidence, "coverage": list(value["contracts"][node_id]["requirement_ids"]),
            "candidate_sha256": common["candidate_sha256"]}
