import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from mewcode.orchestration import get_template, validate_spec
from mewcode.orchestration.extract import refill_plan_briefs_from_dialogue, inventory_orchestration_from_dialogue
from mewcode.orchestration.plan_integrity import require_business_goal, require_resolved_plan_inputs
from mewcode.orchestration.spec import OrchestrationError, PlanBrief
from mewcode.orchestration.workflow_draft import apply_patch, seed_document, project_graph




def test_offline_drawing_repair_preserves_other_fields():
    from scripts.repair_painter_flow_candidates import repair_drawing, PASS_EDGES
    ids = ["topic_plan", "scope_gate", "tool_preflight", "figure_review_gate", "material_extract", "migration_validation", "experience_enable"]
    original = {"version": 6, "meta": {"loops": [{"loop_id": "keep", "control_mode": "team"}]},
        "nodes": [{"node_id": id, "goal": "具体目标", "model": {"provider_key": "keep"},
                   "plan_brief": {"known_inputs": [f"既有阶段 {id}.known_inputs 中的值"],
                                  "steps": ["操作"], "outputs": ["文件"], "acceptance": ["验收"]}} for id in ids],
        "edges": [{"from_node": a, "to_node": b, "condition": "always"} for a,b in sorted(PASS_EDGES)]}
    snapshot = json.dumps(original)
    repaired, changes = repair_drawing(original)
    assert json.dumps(original) == snapshot
    assert len(changes) == 11
    assert repaired["meta"] == original["meta"]
    assert all(e["condition"] == "pass" for e in repaired["edges"])
    for old, new in zip(original["nodes"], repaired["nodes"]):
        assert old["model"] == new["model"]
        for key in ("steps", "outputs", "acceptance"):
            assert new["plan_brief"][key] == old["plan_brief"][key]


def with_input(value):
    spec = get_template("linear")
    node = spec.nodes[0]
    return replace(spec, nodes=(replace(node, plan_brief=PlanBrief(known_inputs=(value,))), *spec.nodes[1:]))


@pytest.mark.parametrize("value", [
    "既有阶段 topic_plan.known_inputs 中的 Topic、模态、图种与验证范围",
    "最新修复方案第 1 节：官方目录、明确时间窗与发表身份要求",
])
def test_unresolved_inputs_editable_but_not_runnable(value):
    spec = with_input(value)
    assert any("未解析" in x for x in validate_spec(spec))
    with pytest.raises(OrchestrationError, match="未解析"):
        validate_spec(spec, require_bindings=True)
    with pytest.raises(OrchestrationError, match="未解析"):
        project_graph(seed_document(spec.as_dict()), spec.as_dict(), conversation_id="test", revision=1)


def test_self_input_is_not_an_upstream_result():
    spec = with_input("placeholder")
    node_id = spec.nodes[0].node_id
    with pytest.raises(OrchestrationError, match="未解析"):
        require_resolved_plan_inputs(with_input(f"读取 {node_id}.known_inputs"))
    require_resolved_plan_inputs(with_input("Topic：RGB-T 多模态目标检测；流程图和模块图"))
    require_resolved_plan_inputs(with_input("读取 paper_search 的上游产物 papers/catalog.json"))
    require_resolved_plan_inputs(with_input("阅读 repair.md 第 1 节"))


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"edges": []}, {"nodes": []}, {"requires_graph_review": True}])
async def test_refill_rejects_partial_structural_repair(monkeypatch, extra):
    spec = with_input("具体要求")
    async def model(*args, **kwargs):
        return json.dumps({"stages": [{"stage_id": spec.nodes[0].node_id}], **extra})
    monkeypatch.setattr("mewcode.orchestration.extract.collect_model_text", model)
    with pytest.raises(OrchestrationError, match="流程结构"):
        await refill_plan_briefs_from_dialogue(object(), [SimpleNamespace(role="user", content="修复通过条件")], spec=spec)


@pytest.mark.asyncio
async def test_refill_receives_and_preserves_complete_brief(monkeypatch):
    spec = with_input("Topic：RGB-T")
    node = replace(spec.nodes[0], plan_brief=PlanBrief(summary="旧摘要", known_inputs=("Topic：RGB-T",),
        steps=("检索目录",), outputs=("papers.json",), acceptance=("可追溯来源",)))
    spec = replace(spec, nodes=(node, *spec.nodes[1:]))
    async def model(*args, user_text, **kwargs):
        assert all(x in user_text for x in ("检索目录", "papers.json", "可追溯来源"))
        return json.dumps({"stages": [{"stage_id": node.node_id, "summary": "新摘要"}]})
    monkeypatch.setattr("mewcode.orchestration.extract.collect_model_text", model)
    result = await refill_plan_briefs_from_dialogue(object(), [SimpleNamespace(role="user", content="更新摘要")], spec=spec)
    brief = result["spec"]["nodes"][0]["plan_brief"]
    assert brief == {**node.plan_brief.as_dict(), "summary": "新摘要"}


def test_restoration_instruction_cannot_replace_business_definition():
    source = {"id": "s1", "role": "user", "text": "给我复原从最初约定到现在实际执行的完整流程"}
    spec = with_input("逐段双稿，英文改写、中文回译、审计、本机编译")
    doc = seed_document(spec.as_dict())
    goal = {**doc["entries"][0], "body": "复原流程，说明历史偏差", "sources": [{"source_id": "s1", "quote": source["text"]}]}
    after = apply_patch(doc, {"upserts": [goal], "ignored": []}, [source])
    assert doc["entries"][0]["body"] == spec.goal
    assert after["entries"][0]["state"] == "conflict"
    with pytest.raises(ValueError, match="冲突"):
        project_graph(after, spec.as_dict(), conversation_id="paper", revision=1)


def test_graph_diff_exposes_goal_and_edge_condition_changes():
    from frontend.orch_drafts import graph_changes
    before = {"goal": "业务", "edges": [{"from_node": "a", "to_node": "b", "condition": "always"}]}
    after = {"goal": "变更目标", "edges": [{"from_node": "a", "to_node": "b", "condition": "pass"}]}
    changes = graph_changes(before, after)
    assert {x["kind"] for x in changes} == {"goal", "edges"}


@pytest.mark.asyncio
async def test_legacy_inventory_rejects_meta_workflow(monkeypatch):
    messages = [SimpleNamespace(role="user", content=x) for x in ("论文逐段分析、改写、回译与审计", "给我复原完整流程")]
    async def model(*args, **kwargs):
        return json.dumps({"goal": "复原完整流程，说明流程演变与停点", "stages": [{"stage_id": "inspect_sources", "label": "核对出处"}]})
    monkeypatch.setattr("mewcode.orchestration.extract.collect_model_text", model)
    with pytest.raises(OrchestrationError, match="编辑请求"):
        await inventory_orchestration_from_dialogue(object(), messages)
    require_business_goal("论文逐段分析、改写、回译与审计", messages)
    require_business_goal("恢复备份数据流程", [SimpleNamespace(role="user", content="新建一个恢复备份数据的流程")])
