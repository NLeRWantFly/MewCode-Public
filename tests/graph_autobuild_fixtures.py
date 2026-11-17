"""Synthetic five-stage histories and model replies for autobuild acceptance."""
from __future__ import annotations

import json

from tests.test_workflow_drafts import cited, entry


RUNTIME_SOURCE = "策划、检索、写作、独立审查、终验五个阶段。检索与写作先后未说明。写作验收：不得丢失任一原始要求。主题、图数、读者开跑时提供。"
DEFAULT_SOURCE = "策划、检索、写作、独立审查、终验五个阶段。检索与写作先后未说明。写作验收：不得丢失任一原始要求。篇数和范围采用合理默认，交付时列出。"


def extraction_patch(inputs):
    source = inputs["sources"][0]
    nodes = [("plan", "plan", "策划"), ("research", "implement", "检索"),
             ("write", "implement", "写作"), ("audit", "audit", "独立审查"),
             ("finish", "reduce", "终验")]
    items = [entry("goal", "goal", "完成五阶段写作流程")]
    for node_id, kind, label in nodes:
        items.append(entry(node_id, body=label, data={
            "node_kind": kind, "outputs": [label + "产物"],
            "acceptance": ["不得丢失任一原始要求"] if node_id == "write" else [label + "通过"],
        }))
    for edge_id, start, end in [("to_research", "plan", "research"),
                                ("to_audit", "write", "audit"),
                                ("to_finish", "audit", "finish")]:
        items.append(entry(edge_id, "edge", "执行依赖", data={"from_node": start, "to_node": end, "condition": "always"}))
    return {"upserts": [cited(item, source) for item in items],
            "ignored": [{"source_id": s["id"], "reason": "重复要求"} for s in inputs["sources"][1:]]}


def completion_patch(inputs, *, kind="runtime", nonconverging=False):
    if nonconverging:
        return {"upserts": [], "notes": "没有可采用的结构改进"}
    existing = {item["id"] for item in inputs["entries"]}
    items = [
        {"id": "research_before_write", "kind": "edge", "title": "检索后写作", "body": "写作使用检索结果",
         "data": {"from_node": "research", "to_node": "write", "condition": "always"}, "rationale": "先收集资料再写作"},
        {"id": "order_default", "kind": "decision", "title": "顺序假设", "body": "检索后再写作",
         "data": {"category": "defaulted", "value": "检索 → 写作"}, "rationale": "原文没有指定先后，采用资料先于写作的顺序"},
        # Deliberately forbidden: the host must keep the user's exact criterion.
        {"id": "write", "kind": "node", "data": {"acceptance": ["内容基本完整即可"]},
         "rationale": "故意尝试改动用户锁定验收"},
    ]
    if kind == "runtime":
        for index, name in enumerate(("主题", "图数", "读者")):
            items.append({"id": "runtime_" + str(index), "kind": "input", "title": name, "body": "开跑时提供" + name,
                          "data": {"name": name, "description": "本次工作的" + name, "required": True,
                                   "default": "3" if name == "图数" else ""}, "rationale": "该值由用户在开跑前集中提供"})
    else:
        for index, (name, value) in enumerate((("篇数", "3"), ("范围", "近五年"))):
            items.append({"id": "default_" + str(index), "kind": "decision", "title": name + "默认值",
                          "body": "默认" + name + "：" + value, "data": {"category": "defaulted", "value": value},
                          "rationale": "用户允许采用合理默认，交付时明确列出"})
    return {"upserts": [item for item in items if item["id"] not in existing or item["id"] == "write"], "notes": "已补齐依赖与未知项账本"}


class BuildModel:
    def __init__(self, kind="runtime", *, nonconverging=False, on_call=None):
        self.kind, self.nonconverging, self.on_call = kind, nonconverging, on_call
        self.calls = []

    async def __call__(self, client, **kwargs):
        inputs = json.loads(kwargs["user_text"])
        phase = "control" if "active_seconds" in inputs and "soft_seconds" in inputs else "extract" if "sources" in inputs else "review" if "requirements" in inputs else "complete"
        self.calls.append({"phase": phase, "client": client, "inputs": inputs})
        if self.on_call is not None:
            await self.on_call(phase, kwargs)
        if phase == "control":
            value = {"control": {"action": "stop", "reason": "尚未找到可靠的新修法，先保留当前草图。", "next_step": ""}}
        elif phase == "extract":
            value = extraction_patch(inputs)
        elif phase == "review":
            value = {"gaps": [], "notes": "全部用户要求已有节点、假设或运行参数对应"}
        else:
            value = completion_patch(inputs, kind=self.kind, nonconverging=self.nonconverging)
        return json.dumps(value, ensure_ascii=False)
