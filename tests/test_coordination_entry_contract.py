"""Compatibility cases for serial and nested coordination boundaries.

These cases deliberately exercise the canvas graph rather than treating
``parent_id`` as an execution edge.  A coordinator may hand off to the first
declared child while the remaining children are connected by explicit
forward edges in the same serial chain.
"""

import pytest

from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.spec import OrchestrationError, OrchestrationSpec


def _node(node_id, *, kind="implement", goal=None, outputs=(), acceptance=()):
    value = {
        "node_id": node_id,
        "kind": kind,
        "label": node_id,
        "goal": goal or node_id,
    }
    if outputs or acceptance:
        value["plan_brief"] = {
            "summary": goal or node_id,
            "outputs": list(outputs),
            "acceptance": list(acceptance),
        }
    return value


def _spec(nodes, edges, *, finalizer, children, coordinators=("coordinator",)):
    return OrchestrationSpec.from_value({
        "spec_id": "coordination-entry-compat",
        "label": "协调边界兼容回归",
        "nodes": nodes,
        "edges": edges,
        "finalizer_node_id": finalizer,
        "meta": {"workflow_draft": {
            "coordination_nodes": list(coordinators),
            "parents": dict(children),
        }},
    })


def _contract(flow, node_id):
    return flow.meta["coordination_contracts"][node_id]


def test_serial_entry_accepts_only_first_direct_handoff_and_keeps_business_contract():
    spec = _spec(
        [
            _node("coordinator", goal="协调页面批次", outputs=("页面批次产物",),
                  acceptance=("逐页核验完成",)),
            _node("page_first", goal="读取页面"),
            _node("page_second", goal="处理页面"),
            _node("finish", kind="reduce", goal="汇总结果"),
        ],
        [
            {"from_node": "coordinator", "to_node": "page_first"},
            {"from_node": "page_first", "to_node": "page_second"},
            {"from_node": "page_second", "to_node": "finish"},
        ],
        finalizer="finish",
        children={"page_first": "coordinator", "page_second": "coordinator"},
    )

    flow = to_expert_flow(spec)
    contract = _contract(flow, "coordinator")
    assert contract["mode"] == "entry"
    assert set(contract["child_node_ids"]) == {"page_first", "page_second"}
    assert "页面批次产物" in contract["original_outputs"]
    assert "逐页核验完成" in contract["original_acceptance"]
    assert "[COORDINATE] ENTRY" in flow.node_map["coordinator"].responsibility


def test_nested_page_entry_uses_the_same_serial_forward_contract():
    spec = _spec(
        [
            _node("coordinator", goal="协调外层页面", outputs=("外层结果",), acceptance=("外层完整",)),
            _node("outer_first", goal="准备嵌套页面"),
            _node("nested_page", goal="协调嵌套页面", outputs=("嵌套结果",), acceptance=("嵌套完整",)),
            _node("nested_first", goal="读取嵌套页"),
            _node("nested_second", goal="处理嵌套页"),
            _node("finish", kind="reduce", goal="汇总结果"),
        ],
        [
            {"from_node": "coordinator", "to_node": "outer_first"},
            {"from_node": "outer_first", "to_node": "nested_page"},
            {"from_node": "nested_page", "to_node": "nested_first"},
            {"from_node": "nested_first", "to_node": "nested_second"},
            {"from_node": "nested_second", "to_node": "finish"},
        ],
        finalizer="finish",
        children={
            "outer_first": "coordinator",
            "nested_page": "coordinator",
            "nested_first": "nested_page",
            "nested_second": "nested_page",
        },
        coordinators=("coordinator", "nested_page"),
    )

    flow = to_expert_flow(spec)
    assert _contract(flow, "coordinator")["mode"] == "entry"
    assert _contract(flow, "nested_page")["mode"] == "entry"
    assert set(_contract(flow, "nested_page")["child_node_ids"]) == {
        "nested_first", "nested_second"
    }
    assert "嵌套结果" in _contract(flow, "nested_page")["original_outputs"]
    assert "嵌套完整" in _contract(flow, "nested_page")["original_acceptance"]


def test_serial_exit_accepts_last_child_handoff_and_keeps_child_review_results():
    spec = _spec(
        [
            _node("start", kind="plan", goal="开始"),
            _node("review_first", kind="audit", goal="初次核验",
                  outputs=("初次核验结果",), acceptance=("初次核验有证据",)),
            _node("review_last", kind="audit", goal="最终核验",
                  outputs=("最终核验结果",), acceptance=("最终核验有证据",)),
            _node("coordinator", kind="reduce", goal="汇总页面核验",
                  outputs=("汇总核验结果",), acceptance=("逐项保留核验结论",)),
        ],
        [
            {"from_node": "start", "to_node": "review_first"},
            {"from_node": "review_first", "to_node": "review_last"},
            {"from_node": "review_last", "to_node": "coordinator"},
        ],
        finalizer="coordinator",
        children={"review_first": "coordinator", "review_last": "coordinator"},
    )

    flow = to_expert_flow(spec)
    contract = _contract(flow, "coordinator")
    assert contract["mode"] == "exit"
    assert set(contract["child_node_ids"]) == {"review_first", "review_last"}
    assert "汇总核验结果" in contract["original_outputs"]
    assert "逐项保留核验结论" in contract["original_acceptance"]
    assert "初次核验结果" in flow.node_map["review_first"].responsibility
    assert "最终核验有证据" in flow.node_map["review_last"].responsibility
    assert "[COORDINATE] EXIT" in flow.node_map["coordinator"].responsibility


def test_marked_coordinator_without_declared_or_forward_children_is_rejected():
    spec = _spec(
        [_node("coordinator"), _node("finish", kind="reduce")],
        [],
        finalizer="coordinator",
        children={},
    )

    with pytest.raises(OrchestrationError, match="入口或出口子边"):
        to_expert_flow(spec)


def test_declared_children_on_both_sides_are_an_ambiguous_boundary():
    spec = _spec(
        [_node("coordinator"), _node("entry_child"), _node("exit_child")],
        [
            {"from_node": "coordinator", "to_node": "entry_child"},
            {"from_node": "exit_child", "to_node": "coordinator"},
        ],
        finalizer="coordinator",
        children={"entry_child": "coordinator", "exit_child": "coordinator"},
    )

    with pytest.raises(OrchestrationError):
        to_expert_flow(spec)
