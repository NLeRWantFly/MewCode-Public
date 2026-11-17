"""Agent-facing tool that submits a bounded runtime micrograph proposal."""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from mewcode.experts.node_expansion import (
    NodeExpansionHolder,
    NodeExpansionValidationError,
)
from mewcode.tools.base import Tool, ToolResult


class ProposeNodeExpansionParams(BaseModel):
    proposal: dict[str, Any] = Field(
        description="局部微图展开提案对象：parent_node_id、expected_flow_version、reason、"
                    "nodes、edges、join；nodes 每项含 node_id、label、task、input_refs、"
                    "outputs、acceptance、tools、write_paths；可用 requirement_ids 分配需求子集，"
                    "所有子任务合计必须覆盖父合同全部需求，所有约束仍继承。"
    )


class ProposeNodeExpansion(Tool):
    name = "ProposeNodeExpansion"
    description = (
        "Propose a bounded runtime micrograph expansion. For complex work with independently "
        "verifiable deliverables, propose the split before repeated failure or context exhaustion; "
        "the host enables child execution after review without a separate multi-agent toggle. "
        "Check budget.execution.decomposition first: each child plus the parent join needs one "
        "attempt, excluding the current attempt already accounted for. Stay within max_children "
        "and preserve retry headroom; the host reserves these attempts before publication. "
        "When an approach repeatedly blocks, "
        "use follow-up, adaptation or compensating nodes to take a different route while reusing "
        "valid prior artifacts. Prefer one small discriminating probe before committing the remaining "
        "work; a successful probe is not final acceptance. Only the currently active "
        "implement/executor node may call this; context overflow may also ask the bound model "
        "to propose a reviewed split before business execution. "
        "Rules: 2-6 worker nodes; edges only with condition \"always\"; every node must "
        "reach the reserved \"$join\" sink which is the single join; single level, children "
        "cannot expand again; proposals must not add models, budgets, or "
        "permissions. Ready independent read-only branches run concurrently with reserved sibling budgets; "
        "write/command/browser branches remain serial. Express dependencies explicitly, not executable pseudocode. "
        "Worker scopes must exclude Agent/TeamCreate/TeamDelete/ProposeNodeExpansion tools "
        "and may only shrink the inherited tool and write scope. A rejected proposal pauses the node and asks the "
        "user; it never falls back to a flat Agent. "
        "Shape: {\"parent_node_id\":\"<this node>\",\"expected_flow_version\":<int>,"
        "\"reason\":\"...\",\"nodes\":[{\"node_id\":\"a\",\"label\":\"...\",\"task\":\"...\","
        "\"input_refs\":[\"parent\"],\"outputs\":[\"draft\"],\"acceptance\":[\"...\"],"
        "\"tools\":[],\"write_paths\":[\"out/a.txt\"]}],"
        "\"edges\":[{\"from_node\":\"a\",\"to_node\":\"$join\",\"condition\":\"always\"}],"
        "\"join\":{\"label\":\"merge\",\"acceptance\":[\"...\"]}}"
    )
    category = "read"
    is_system_tool = True
    consumes_runtime_slot = False
    params_model = ProposeNodeExpansionParams

    def __init__(self, holder: NodeExpansionHolder):
        self.holder = holder

    async def execute(self, params: ProposeNodeExpansionParams) -> ToolResult:
        nodes = params.proposal.get('nodes')
        if self.holder.budget_capacity and isinstance(nodes, list) and 2 <= len(nodes) <= 6:
            capacity = self.holder.budget_capacity()
            if len(nodes) > capacity['max_children']:
                self.holder.value = None
                self.holder.rejection = None
                # Keep the current attempt alive so the model can propose a
                # smaller complete split, or finish directly when it fits.
                return ToolResult(output=json.dumps({
                    'status': 'budget_limited', 'budget': capacity,
                    'requested_children': len(nodes),
                    'instruction': '分工未激活，也未消耗子任务额度。合并分工以适配现有额度；'
                        '若无法保留完整要求，说明缺口并走预算恢复入口，不得新增额度或降低验收。',
                }, ensure_ascii=False), is_error=True)
        try:
            validated = self.holder.submit(params.proposal)
        except NodeExpansionValidationError as exc:
            payload = {
                "status": "rejected",
                "field": exc.field,
                "reason": exc.reason,
                "instruction": "提案未激活；宿主将暂停并向用户展示原因。",
            }
            return ToolResult(
                output=json.dumps(payload, ensure_ascii=False), is_error=True
            )
        payload = {
            "status": "accepted",
            "expansion_id": validated.expansion_id,
            "instruction": "停止父节点工作并等待宿主执行微图；不要继续调用工具或提交完成交接。",
        }
        return ToolResult(output=json.dumps(payload, ensure_ascii=False), is_error=False)
