"""Use saved handoffs and current contracts to explain a human decision."""
from __future__ import annotations

import copy
import json


def decision_sources(node, flow, snapshot):
    sources = []
    if snapshot.get("run_inputs"):
        sources.append({"kind": "run_inputs", "values": dict(snapshot["run_inputs"])})
    stage = ((snapshot.get("node_states") or {}).get(node.node_id) or {})
    input_notes = stage.get("input_notes") or {}
    if isinstance(input_notes, dict) and input_notes:
        sources.append({"kind": "stage_input_notes", "node_id": node.node_id,
                        "notes": copy.deepcopy(input_notes), "notes_role": "context_only"})
    gate = (snapshot.get("gate_dialogues") or {}).get(node.node_id) or {}
    gate_notes = gate.get("answer_notes") or {}
    if not gate_notes:
        gate_notes = ((snapshot.get("gate_confirmations") or {}).get(node.node_id) or {}).get("answer_notes") or {}
    if isinstance(gate_notes, dict) and gate_notes:
        sources.append({"kind": "gate_answer_notes", "node_id": node.node_id,
                        "notes": copy.deepcopy(gate_notes), "notes_role": "context_only"})
    for edge in flow.edges:
        if edge.to_node != node.node_id:
            continue
        state = (snapshot.get("node_states") or {}).get(edge.from_node) or {}
        result = state.get("result") or state
        handoff = result.get("handoff") or {}
        source = {"node_id": edge.from_node, "label": flow.node_map[edge.from_node].label,
            "status": state.get("status"),
            "summary": str(handoff.get("summary") or result.get("text") or "")[:1800],
            "decisions": handoff.get("decisions") or [], "open_items": handoff.get("open_items") or [],
            "artifacts": handoff.get("artifacts") or [], "verdict": result.get("verdict")}
        handoff_notes = handoff.get("answer_notes") or handoff.get("context_notes") or {}
        if isinstance(handoff_notes, dict) and handoff_notes:
            source.update(answer_notes=copy.deepcopy(handoff_notes), notes_role="context_only")
        sources.append(source)
    next_stages = []
    for edge in flow.edges:
        if edge.from_node != node.node_id:
            continue
        following = flow.node_map[edge.to_node]
        brief = (flow.meta.get("node_plan_briefs") or {}).get(following.node_id) or {}
        model = (snapshot.get("locked_models") or {}).get(following.node_id)
        if not model and following.model:
            model = following.model.as_dict()
        next_stages.append({"node_id": following.node_id, "label": following.label,
            "summary": brief.get("summary") or following.responsibility,
            "steps": brief.get("steps") or [], "outputs": brief.get("outputs") or [],
            "model": model, "condition": edge.condition,
            "input_when": edge.input_when,
            "members": [member.as_dict() for member in following.members]})
    if next_stages:
        sources.append({"kind": "next_stages", "stages": next_stages})
    return sources


def decision_background(node, flow, snapshot):
    """Facts for the card, never an inferred authorization or acceptance."""
    sources = decision_sources(node, flow, snapshot)
    brief = (flow.meta.get("node_plan_briefs") or {}).get(node.node_id) or {}
    sections = ["当前步骤：" + node.label]
    upstream = [source for source in sources if source.get("node_id")
                and source.get("kind") not in {"stage_input_notes", "gate_answer_notes"}]
    if upstream:
        lines = []
        for source in upstream:
            lines.append(source["label"] + "：" + (source["summary"] or "尚无交接摘要，保留原检查点。"))
            lines.extend("· " + str(item) for item in source["decisions"])
            if source.get("answer_notes"):
                lines.append("· 上游用户备注（仅作补充上下文，不改变已批准范围）："
                             + json.dumps(source["answer_notes"], ensure_ascii=False))
        sections.append("已取得的阶段结果\n" + "\n".join(lines))
    for source in sources:
        if source.get("kind") == "stage_input_notes" and source.get("notes"):
            sections.append("本节点参数备注（仅作补充上下文，不改变已绑定字段或授权范围）\n"
                            + json.dumps(source["notes"], ensure_ascii=False))
        elif source.get("kind") == "gate_answer_notes" and source.get("notes"):
            sections.append("本节点门禁备注（仅作补充上下文，不改变方案或授权范围）\n"
                            + json.dumps(source["notes"], ensure_ascii=False))
    if brief.get("summary") or node.responsibility:
        sections.append("本步为什么需要确认\n" + str(brief.get("summary") or node.responsibility))
    following = next((source["stages"] for source in sources if source.get("kind") == "next_stages"), [])
    if following:
        lines = []
        for stage in following:
            lines.append("· " + stage["label"] + "：" + str(stage["summary"] or "按已保存的节点职责处理。"))
            model = stage.get("model") or {}
            if isinstance(model, dict) and model.get("model_id"):
                route = " / ".join(str(model[key]) for key in ("provider_key", "model_id", "effort") if model.get(key))
                lines.append("  节点模型：" + route)
            if stage["outputs"]:
                lines.append("  产出：" + "；".join(str(item) for item in stage["outputs"]))
        sections.append("确认后准备进入的环节\n" + "\n".join(lines))
    values = snapshot.get("run_inputs") or {}
    current = []
    for item in flow.meta.get("run_input_contract") or []:
        name = item["name"]
        if item.get("collect_at", "start") not in {"start", node.node_id} and name != "model_bindings":
            continue
        value = values.get(name)
        text = "\n".join(str(part) for part in value) if isinstance(value, list) else str(value or "尚未提供")
        current.append(str(item.get("label") or name) + "：" + text)
    if current:
        sections.append("本次参数与待确认范围\n" + "\n".join(current))
    if brief.get("steps"):
        sections.append("本步安排\n" + "\n".join("· " + str(item) for item in brief["steps"]))
    sections.append("上游中间产物和未完成项会继续交给后续节点处理；本次确认不替代业务验收。")
    return "\n\n".join(sections)
