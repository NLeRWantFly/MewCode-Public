"""Readiness detects topology and completeness gaps beyond validate_spec."""
import copy

from mewcode.orchestration.graph_readiness import readiness
from mewcode.orchestration.validate import validate_spec
from mewcode.orchestration.workflow_draft import project_graph, validate_entry


def node(node_id, kind="implement", *, inputs=None, outputs=None, acceptance=None):
    return {"id": node_id, "kind": "node", "title": node_id, "body": "执行",
            "state": "active", "data": {"node_kind": kind, "inputs": inputs or [],
                                      "outputs": outputs if outputs is not None else ["产物"],
                                      "acceptance": acceptance if acceptance is not None else ["验收"]},
            "sources": [], "evidence": "manual"}


def edge(edge_id, start, end, condition="always"):
    return {"id": edge_id, "kind": "edge", "title": edge_id, "body": "依赖",
            "state": "active", "data": {"from_node": start, "to_node": end, "condition": condition},
            "sources": [], "evidence": "manual"}


def doc(*entries):
    return {"schema_version": 1, "entries": list(entries), "ignored": []}


def codes(result):
    return {diagnostic["code"] for diagnostic in result["diagnostics"]}


def check(document, base=None):
    return readiness(document, base, conversation_id="test", revision=1)


def test_thirteen_unlinked_nodes_pass_existing_validator_but_are_disconnected():
    graph = doc(*(node(f"n{i}", "plan" if i == 0 else "implement") for i in range(13)))
    spec = project_graph(graph, None, conversation_id="test", revision=1)
    assert isinstance(validate_spec(spec, require_bindings=False), list)
    result = check(graph)
    assert not result["ready"] and "disconnected" in codes(result)
    disconnected = next(d for d in result["diagnostics"] if d["code"] == "disconnected")
    assert "n0" in disconnected["message"] and "n12" in disconnected["message"]


def test_weakly_connected_branch_can_still_fail_to_reach_finalizer():
    graph = doc(node("start", "plan"), node("finish", "reduce"), node("dead"),
                edge("e1", "start", "finish"), edge("e2", "finish", "dead"))
    result = check(graph, {"finalizer_node_id": "finish"})
    assert "disconnected" not in codes(result)
    assert any(d["code"] == "unreachable_to_finalizer" and d["entry_ids"] == ["dead"]
               for d in result["diagnostics"])


def test_missing_outputs_and_acceptance_skip_gate():
    graph = doc(node("start", "plan", outputs=[], acceptance=[]), node("gate", "gate", outputs=[], acceptance=[]),
                node("finish", "reduce"), edge("e1", "start", "gate"), edge("e2", "gate", "finish"))
    result = check(graph)
    assert "missing_outputs" in codes(result) and "missing_acceptance" in codes(result)
    assert all("gate" not in d["entry_ids"] for d in result["diagnostics"]
               if d["code"] in {"missing_outputs", "missing_acceptance"})


def test_finalizer_inside_loop_is_diagnostic_even_when_projection_fails():
    loop = {"id": "cycle", "kind": "loop", "title": "返修", "body": "不合格重做",
            "state": "active", "sources": [], "evidence": "manual",
            "data": {"loop_id": "cycle", "entry_node_id": "work", "check_node_id": "finish",
                     "node_ids": ["work", "finish"], "reviewer_node_ids": ["finish"],
                     "control_mode": "team", "exit_condition": "达标",
                     "criteria": [{"criterion_id": "C", "acceptance_test": "通过"}]}}
    graph = doc(node("work", "plan"), node("finish", "reduce"), edge("e1", "work", "finish"), loop)
    result = check(graph, {"finalizer_node_id": "finish"})
    assert not result["ready"] and "finalizer_in_loop" in codes(result)


def test_unresolved_reference_and_missing_projection_are_visible():
    graph = doc(node("work", "plan", inputs=["既有阶段 work.known_inputs"]), node("finish", "reduce"),
                edge("e1", "work", "finish"))
    assert "unresolved_reference" in codes(check(graph))
    healthy = doc(node("work", "plan"), node("finish", "reduce"), edge("e1", "work", "finish"),
                  edge("e2", "work", "finish"))
    duplicate = next(d for d in check(healthy)["diagnostics"] if d["code"] == "duplicate_business_key")
    assert duplicate["entry_ids"] == ["e1", "e2"]


def test_conflict_and_projection_failure_do_not_hide_other_diagnostics():
    graph = doc(node("work", "plan", outputs=[]), node("finish", "reduce"))
    graph["entries"][0]["state"] = "conflict"
    graph["entries"][0]["data"]["parent_id"] = "ghost"
    before = copy.deepcopy(graph)
    result = check(graph)
    assert graph == before
    assert {"entry_conflict", "invalid_reference", "disconnected", "missing_outputs"} <= codes(result)
    assert result["skipped"] == ["unreachable_to_finalizer", "finalizer_in_loop", "supervision", "audit_rework_route", "entry_not_projected"]


def test_healthy_graph_is_ready():
    graph = doc(node("plan", "plan"), node("audit", "audit"), node("finish", "reduce"),
                edge("e1", "plan", "audit"), edge("e2", "audit", "finish"))
    assert check(graph) == {"ready": True, "diagnostics": []}


def test_single_node_counts_as_connected():
    assert check(doc(node("only", "plan"))) == {"ready": True, "diagnostics": []}


def test_terminal_gate_stays_finalizer_when_an_audit_is_appended_later():
    graph = doc(node("start", "plan"), node("deliver", "gate"), node("last_audit", "audit"),
                edge("e1", "start", "last_audit"), edge("e2", "last_audit", "deliver", "pass"))
    projected = project_graph(graph, None, conversation_id="test", revision=1)
    assert projected.finalizer_node_id == "deliver" and check(graph)["ready"]
    projected = project_graph(graph, {"finalizer_node_id": "last_audit"}, conversation_id="test", revision=1)
    assert projected.finalizer_node_id == "last_audit"
    assert "unreachable_to_finalizer" in codes(check(graph, projected.as_dict()))


def test_invalid_cycle_does_not_hide_existing_paths_to_terminal_gate():
    graph = doc(node("work", "plan"), node("audit", "audit"), node("deliver", "gate"), node("orphan"),
                edge("e1", "work", "audit"), edge("e2", "audit", "work", "fail"),
                edge("e3", "audit", "deliver", "pass"))
    result = check(graph)
    assert {"forward_cycle", "projection_invalid", "disconnected", "unreachable_to_finalizer"} <= codes(result)
    assert result["skipped"] == ["supervision", "audit_rework_route", "entry_not_projected"]
    assert any(d["code"] == "unreachable_to_finalizer" and d["entry_ids"] == ["orphan"]
               for d in result["diagnostics"])


def test_hierarchy_remains_a_real_missing_dependency_and_components_are_complete():
    graph = doc(node("parent", "plan"), node("child"), node("finish", "reduce"), node("orphan"),
                edge("e1", "parent", "finish"))
    graph["entries"][1]["data"]["parent_id"] = "parent"
    before = check(graph)
    disconnected = next(d for d in before["diagnostics"] if d["code"] == "disconnected")
    assert disconnected["components"] == [["child"], ["finish", "parent"], ["orphan"]]
    assert disconnected["entry_ids"] == ["child", "finish", "orphan", "parent"]
    graph["entries"].append(edge("e2", "orphan", "finish"))
    after = check(graph)
    assert any(d["code"] == "unreachable_to_finalizer" and d["entry_ids"] == ["child"]
               for d in after["diagnostics"])


def test_malformed_references_do_not_hide_topology_diagnostics():
    bad_edge = edge("bad", "work", "finish")
    bad_edge["data"]["from_node"] = ["work"]
    assert validate_entry(bad_edge)["data"]["from_node"] == ["work"]
    graph = doc(node("work", "plan"), node("finish", "reduce"), bad_edge)
    result = check(graph)
    assert {"projection_invalid", "disconnected"} <= codes(result)
    bad_loop = {"id": "bad_loop", "kind": "loop", "title": "坏循环", "body": "",
                "state": "active", "sources": [], "evidence": "manual",
                "data": {"entry_node_id": ["work"], "check_node_id": "finish",
                         "node_ids": [["work"]], "reviewer_node_ids": []}}
    graph["entries"].append(bad_loop)
    assert "disconnected" in codes(check(graph))
