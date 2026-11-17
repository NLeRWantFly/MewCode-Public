"""Readiness checks for explicit audit failure routes."""
from __future__ import annotations

from mewcode.orchestration.graph_readiness import readiness


def node(node_id, kind="implement"):
    return {"id": node_id, "kind": "node", "title": node_id, "body": "完成既定工作",
            "state": "active", "data": {"node_kind": kind, "outputs": ["产物"],
                                          "acceptance": ["按既定标准验收"]},
            "sources": [], "evidence": "manual"}


def edge(edge_id, source, target, condition="always"):
    return {"id": edge_id, "kind": "edge", "title": edge_id, "body": "执行依赖",
            "state": "active", "data": {"from_node": source, "to_node": target,
                                          "condition": condition},
            "sources": [], "evidence": "manual"}


def document(*entries):
    return {"schema_version": 1, "entries": list(entries), "ignored": []}


def base(audit_target="N03"):
    return {"spec_id": "test", "finalizer_node_id": "finish", "nodes": [
        {"node_id": "N01", "kind": "plan"},
        {"node_id": "N03", "kind": "implement"},
        {"node_id": "N04A", "kind": "audit", "audit_target": audit_target},
        {"node_id": "error_summary", "kind": "reduce"},
        {"node_id": "finish", "kind": "reduce"},
    ], "edges": [], "meta": {}}


def check(doc, graph=None):
    return readiness(doc, graph or base(), conversation_id="test", revision=1)


def find_route_gap(result):
    return next(d for d in result["diagnostics"] if d["code"] == "audit_rework_route_gap")


def simple_audit_graph(*extra_edges):
    return document(node("N01", "plan"), node("N03"), node("N04A", "audit"),
                    node("error_summary", "reduce"), node("finish", "reduce"),
                    edge("plan_work", "N01", "N03"), edge("work_audit", "N03", "N04A"),
                    edge("audit_pass", "N04A", "finish", "pass"), *extra_edges)


def test_single_unconditional_fail_edge_conflicting_with_explicit_target_gets_safe_suggestion():
    result = check(simple_audit_graph(
        edge("audit_fail", "N04A", "error_summary", "fail"),
        edge("error_finish", "error_summary", "finish")))

    diagnostic = find_route_gap(result)
    assert diagnostic["entry_ids"] == ["N04A", "audit_fail"]
    assert "N03" in diagnostic["message"]
    assert diagnostic["suggested_fix"] == (
        "将唯一无条件 fail 边 audit_fail 的 to_node 对齐到显式 audit_target N03，"
        "保留 condition=fail；不要改变其它节点或验收。")


def test_multiple_fail_edges_report_gap_without_choosing_a_branch_to_rewrite():
    graph = simple_audit_graph(node("error_summary_2", "reduce"),
        edge("audit_fail_1", "N04A", "error_summary", "fail"),
        edge("audit_fail_2", "N04A", "error_summary_2", "fail"),
        edge("error_finish", "error_summary", "finish"),
        edge("error2_finish", "error_summary_2", "finish"))

    diagnostic = find_route_gap(check(graph))
    assert "audit_fail_1" in diagnostic["entry_ids"]
    assert "audit_fail_2" in diagnostic["entry_ids"]
    assert "suggested_fix" not in diagnostic


def test_explicit_target_without_fail_edges_is_already_an_executable_route():
    result = check(simple_audit_graph())
    assert not any(d["code"] == "audit_rework_route_gap" for d in result["diagnostics"])


def test_explicit_non_implement_target_is_reported_without_guessing_a_replacement():
    result = check(simple_audit_graph(), base(audit_target="error_summary"))
    diagnostic = find_route_gap(result)
    assert "error_summary" in diagnostic["message"]
    assert "suggested_fix" not in diagnostic


def test_missing_repair_roots_in_declared_loop_use_existing_loop_diagnostics():
    loop = {"id": "cycle", "kind": "loop", "title": "返修循环", "body": "不合格时返修",
            "state": "active", "sources": [], "evidence": "manual",
            "data": {"loop_id": "cycle", "entry_node_id": "N03", "check_node_id": "N04A",
                     "node_ids": ["N03", "N04A"], "reviewer_node_ids": ["N04A"],
                     "exit_condition": "所有既定条件通过", "control_mode": "team",
                     "criteria": [{"criterion_id": "C1", "acceptance_test": "按既定标准验收"}],
                     "requires_repair": True, "repair_node_ids": [],
                     "repair_dispatch": "after_decision"}}
    graph = document(node("N01", "plan"), node("N03"), node("N04A", "audit"),
                     node("finish", "reduce"), edge("plan_work", "N01", "N03"),
                     edge("work_check", "N03", "N04A"), edge("check_finish", "N04A", "finish", "pass"),
                     edge("check_rework", "N04A", "N03", "loop"), loop)

    result = check(graph, {"finalizer_node_id": "finish"})
    assert any(d["code"] == "invalid_loop" and "repair_node_ids" in d["message"]
               for d in result["diagnostics"])
    assert not any(d["code"] == "audit_rework_route_gap" for d in result["diagnostics"])
