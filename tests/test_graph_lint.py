"""Deterministic coverage for all graph lint rules."""
from __future__ import annotations

import json
import copy
from dataclasses import replace
from pathlib import Path

import pytest

from mewcode.experts.flow import ExpertFlowSpec
from mewcode.orchestration.convert import from_expert_flow
from mewcode.orchestration.graph_lint import lint_graph
from mewcode.orchestration.spec import OrchestrationSpec, PlanBrief


FIXTURE = Path(__file__).parent / "fixtures" / "paradigm" / "audit_tool_recovery_pause.json"


def _spec(nodes=(), edges=(), *, meta=None, run_inputs=()):
    nodes = list(nodes) or [_node()]
    return OrchestrationSpec.from_value({
        "spec_id": "lint-test", "label": "lint test", "goal": "test",
        "nodes": list(nodes), "edges": list(edges), "meta": dict(meta or {}),
        "goal_contract": {"objective": "test", "run_inputs": list(run_inputs)},
    })


def _node(node_id="work", kind="implement", *, steps=(), acceptance=(), known_inputs=(),
          outputs=(), tools=None, **extra):
    value = {
        "node_id": node_id, "kind": kind, "label": node_id, "goal": "complete work",
        "plan_brief": {"steps": list(steps), "acceptance": list(acceptance),
                       "known_inputs": list(known_inputs), "outputs": list(outputs)},
        **extra,
    }
    if tools is not None:
        value["tools"] = list(tools)
    return value


def _codes(spec, run_inputs=None):
    return {finding.code for finding in lint_graph(spec, run_inputs)}


@pytest.mark.parametrize("steps,acceptance,expected", [
    (("逐页读取全部文件",), (), True),
    (("读取元数据并选取 3 个样本",), (), False),
])
def test_plan_node_full_scan(steps, acceptance, expected):
    findings = lint_graph(_spec([_node(kind="plan", steps=steps, acceptance=acceptance)]))
    assert ("plan_node_full_scan" in {item.code for item in findings}) is expected
    if expected:
        assert findings[0].message == "计划节点 work 被要求逐项全量处理；把全量工作交给执行节点或批量循环，计划只做元数据与代表样本。"


@pytest.mark.parametrize("acceptance,expected", [
    (("完整处理所有材料",), True),
    (("全部 12 份材料按清单核对",), False),
])
def test_acceptance_unbounded(acceptance, expected):
    codes = _codes(_spec([_node(acceptance=acceptance)]))
    assert ("acceptance_unbounded" in codes) is expected


@pytest.mark.parametrize("known,contract,upstream,expected", [
    (("完整路径待提供",), [{"name": "pdf_paths", "label": "完整路径", "type": "list"}], (), True),
    (("survey.json 待提供",), [], [{"from_node": "source", "to_node": "work"}], True),
    (("一个还未确定的值待提供",), [], (), False),
])
def test_known_input_placeholder(known, contract, upstream, expected):
    nodes = [_node("work", known_inputs=known)]
    if upstream:
        nodes.insert(0, _node("source", outputs=("survey.json",)))
    codes = _codes(_spec(nodes, upstream, run_inputs=contract))
    assert ("known_input_placeholder" in codes) is expected


@pytest.mark.parametrize("declaration,expected", [
    ({"name": "paths", "type": "text", "default": "a\nb"}, True),
    ({"name": "paths", "example": "a\nb"}, True),
    ({"name": "paths", "type": "list", "default": "a\nb"}, False),
    ({"name": "paths", "type": "multiline", "default": "a\nb"}, False),
    ({"name": "paths", "default": "a\nb"}, False),
])
def test_multiline_text_input(declaration, expected):
    codes = _codes(_spec([], run_inputs=[declaration]))
    assert ("multiline_text_input" in codes) is expected


@pytest.mark.parametrize("upstream_steps,current_steps,expected", [
    ((), ("用 Python 运行解析脚本",), True),
    ((("PreparePythonEnvironment 准备依赖",),), ("用 Python 运行解析脚本",), False),
    ((), ("使用项目根目录 .venv 运行 Python",), False),
])
def test_environment_not_prepared(upstream_steps, current_steps, expected):
    nodes = []
    edges = []
    if upstream_steps:
        nodes.append(_node("prep", steps=upstream_steps[0], tools=("PreparePythonEnvironment",)))
        edges.append({"from_node": "prep", "to_node": "work"})
    nodes.append(_node("work", steps=current_steps))
    codes = _codes(_spec(nodes, edges))
    assert ("environment_not_prepared" in codes) is expected


@pytest.mark.parametrize("prep_tools,prep_steps,expected", [
    (("ReadFile",), ("在 2 个样本上执行 pilot",), True),
    (("ReadFile", "WriteFile"), ("对 2 个代表样本进行试点",), False),
    (("*",), ("对 2 个代表样本进行试点",), False),
    (("*",), ("只读取环境元数据",), True),
])
def test_loop_without_pilot(prep_tools, prep_steps, expected):
    nodes = [
        _node("prep", steps=prep_steps, tools=prep_tools),
        _node("work", tools=("ReadFile", "WriteFile")),
    ]
    edges = [{"from_node": "prep", "to_node": "work"}]
    loop = {"loop_id": "batch", "entry_node_id": "work", "node_ids": ["work"],
            "items": [{"item_id": "one", "goal": "process one item"}]}
    codes = _codes(_spec(nodes, edges, meta={"loops": [loop]}))
    assert ("loop_without_pilot" in codes) is expected


def test_work_item_loop_members_are_excluded_from_plan_full_scan_warning():
    node = _node("work", kind="plan", steps=("逐项处理每份输入",))
    loop = {"loop_id": "batch", "entry_node_id": "work", "node_ids": ["work"],
            "items": [{"item_id": "one", "goal": "process one item"}]}
    assert "plan_node_full_scan" not in _codes(_spec([node], meta={"loops": [loop]}))


@pytest.mark.parametrize("steps,expected", [
    (("检查环境版本，然后写入配置",), True),
    (("先检查环境版本，分步执行后再写入配置",), False),
])
def test_probe_mixed_with_write(steps, expected):
    codes = _codes(_spec([_node(steps=steps)]))
    assert ("probe_mixed_with_write" in codes) is expected


@pytest.mark.parametrize("steps,tools,expected", [
    (("运行命令并写入文件",), ("ReadFile",), True),
    (("运行命令并写入文件",), ("Bash", "WriteFile"), False),
])
def test_tools_too_narrow(steps, tools, expected):
    codes = _codes(_spec([_node(steps=steps, tools=tools)]))
    assert ("tools_too_narrow" in codes) is expected


@pytest.mark.parametrize("on_blocked,fallback_ids,nodes,expected", [
    ("fallback", (), ("work", "next"), True),
    ("fallback", ("missing",), ("work",), True),
    ("fallback", ("next",), ("work", "next"), False),
    ("retry", (), ("work",), False),
])
def test_fallback_ineffective(on_blocked, fallback_ids, nodes, expected):
    rows = [_node(node_id, on_blocked=on_blocked, fallback_node_ids=list(fallback_ids))
            if node_id == "work" else _node(node_id) for node_id in nodes]
    codes = _codes(_spec(rows))
    assert ("fallback_ineffective" in codes) is expected


@pytest.mark.parametrize("kind,steps,expected", [
    ("implement", ("请用户确认是否发布",), True),
    ("implement", ("不要询问用户；按已确认标准执行",), False),
    ("gate", ("请用户确认是否发布",), False),
])
def test_midrun_user_question(kind, steps, expected):
    codes = _codes(_spec([_node(kind=kind, steps=steps)]))
    assert ("midrun_user_question" in codes) is expected


@pytest.mark.parametrize("acceptance,steps,outputs,tools,expected", [
    (("逐批检查多批次产物",), (), (), ("ReadFile", "ReadAuditArtifact"), True),
    (("逐批检查多批次产物",), ("每批提交 SubmitHandoff inline_outputs",), (), ("ReadFile",), False),
    (("逐批检查多批次产物",), (), (), ("ReadFile", "WriteFile"), False),
])
def test_read_only_long_task(acceptance, steps, outputs, tools, expected):
    codes = _codes(_spec([_node(acceptance=acceptance, steps=steps, outputs=outputs, tools=tools)]))
    assert ("read_only_long_task" in codes) is expected


def test_locked_flow_fixture_exposes_the_three_documented_t2_findings():
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw_flow = copy.deepcopy(raw["locked_flow"])
    valid_ids = {node["node_id"] for node in raw_flow["nodes"]}
    for node in raw_flow["nodes"]:
        if not isinstance(node.get("model"), dict):
            node["model"] = None
        policy = node.get("failure_policy") or {}
        if policy.get("action") in {"route", "fallback"} and policy.get("target_node_id") not in valid_ids:
            policy["action"] = "stop"
            policy["target_node_id"] = ""
    # This shape-only replay fixture deliberately redacts loop criteria and item
    # source text; those are irrelevant to the three documented baseline signals.
    (raw_flow.setdefault("meta", {}))["loops"] = []
    raw_flow["edges"] = []  # Edge endpoints are redacted, so they cannot be replayed.
    flow = ExpertFlowSpec.from_value(raw_flow)
    spec = from_expert_flow(flow)
    n01 = spec.node_map()["N01"]
    n01 = replace(n01, plan_brief=PlanBrief(
        known_inputs=("完整路径待提供",),
        steps=("逐项检查每一页 PDF",),
        acceptance=(),
    ))
    spec = replace(spec, nodes=tuple(n01 if node.node_id == "N01" else node for node in spec.nodes))
    contract = [{"name": "pdf_paths", "label": "完整路径", "type": "text",
                 "example": "<fixture>/input_01.pdf\n<fixture>/input_02.pdf"}]
    codes = _codes(spec, contract)
    assert {"plan_node_full_scan", "known_input_placeholder", "multiline_text_input"} <= codes
