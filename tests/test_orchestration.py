import asyncio
import json

from mewcode.conversation import Message
from mewcode.orchestration.control_plan import ControlPlan, build_control_plan
from mewcode.orchestration.convert import (
    apply_profile_bindings,
    from_expert_flow,
    merge_node_models,
    to_expert_flow,
)
from mewcode.orchestration.pipeline_bridge import (
    from_pipeline_graph,
    graph_has_canvas_nesting,
    is_orchestration_graph,
    sync_orchestration_graph_from_spec,
    to_pipeline_graph,
)
from mewcode.orchestration.extract import (
    build_clarify_prompt,
    carry_over_user_edits,
    collect_model_text,
    collect_open_questions,
    dialogue_has_user_turn,
    extract_orchestration_from_dialogue,
    format_dialogue_transcript,
    inventory_orchestration_from_dialogue,
    OrchestrationStreamTimeout,
    normalize_inventory,
    refill_plan_briefs_from_dialogue,
    repair_extracted_graph_cycles,
)
from mewcode.orchestration.plan_session import PlanSessionStore, infer_plan_status
from mewcode.orchestration.propose import pick_template_id, propose_orchestration, try_parse_planner_json
from mewcode.orchestration.spec import OrchestrationError, OrchestrationSpec, PlanBrief
from mewcode.orchestration.templates import get_template, list_templates
from mewcode.orchestration.validate import MISSING_BINDINGS_PREFIX, unbound_node_ids, validate_spec
from mewcode.tools.base import (
    StreamEnd,
    StreamStatus,
    TextDelta,
    ThinkingDelta,
    ToolCallComplete,
    ToolCallDelta,
    ToolCallStart,
)
import pytest


def test_list_templates_has_core_set():
    ids = {item["template_id"] for item in list_templates()}
    assert "linear" in ids
    assert "fe_be_parallel" in ids
    assert "classic_four_role" in ids
    assert "research_write" in ids


def test_pick_template_fe_be():
    assert pick_template_id("前端改 UI，后端补 API") == "fe_be_parallel"
    assert pick_template_id("修一个小 bug") == "linear"


def test_validate_rejects_cycle():
    raw = get_template("linear").as_dict()
    raw["edges"].append({"from": "summarize", "to": "plan"})
    with pytest.raises(OrchestrationError, match="cycle"):
        validate_spec(OrchestrationSpec.from_value(raw))


def test_validate_audit_target():
    raw = get_template("linear").as_dict()
    for node in raw["nodes"]:
        if node["node_id"] == "audit":
            node["audit_target"] = "missing"
    with pytest.raises(OrchestrationError, match="target missing"):
        validate_spec(OrchestrationSpec.from_value(raw))


def test_round_trip_to_expert_flow():
    spec = get_template("fe_be_parallel", goal="做前后端")
    flow = to_expert_flow(spec)
    assert flow.finalizer_node_id == "summarize"
    roles = {node.role for node in flow.nodes}
    assert "planner" in roles
    assert "executor" in roles
    assert "auditor" in roles
    back = from_expert_flow(flow)
    assert any(node.kind == "implement" and node.prefers == "frontend" for node in back.nodes)


def test_to_expert_flow_keeps_canvas_groups_for_board_nesting():
    raw = _bind_all_nodes(get_template("linear", goal="嵌套子阶段").as_dict())
    parent_id = raw["nodes"][1]["node_id"]
    child_a = {
        "node_id": "child_a",
        "kind": "implement",
        "label": "子阶段 A",
        "goal": "子步骤 A",
        "model": {"provider_key": "p", "model_id": "m"},
    }
    child_b = {
        "node_id": "child_b",
        "kind": "implement",
        "label": "子阶段 B",
        "goal": "子步骤 B",
        "model": {"provider_key": "p", "model_id": "m"},
    }
    # Append children at the end so a flat list would put them last.
    raw["nodes"] = list(raw["nodes"]) + [child_a, child_b]
    raw["edges"] = list(raw["edges"]) + [
        {"from_node": parent_id, "to_node": "child_a", "condition": "always"},
        {"from_node": "child_a", "to_node": "child_b", "condition": "always"},
    ]
    raw["meta"] = {
        "canvas_groups": {
            parent_id: {
                "expanded": True,
                "children": ["child_a", "child_b"],
                "label": raw["nodes"][1]["label"],
            }
        }
    }
    spec = OrchestrationSpec.from_value(raw)
    flow = to_expert_flow(spec)
    groups = (flow.meta or {}).get("canvas_groups") or {}
    assert parent_id in groups
    assert groups[parent_id]["children"] == ["child_a", "child_b"]
    assert flow.as_dict()["meta"]["canvas_groups"][parent_id]["children"] == [
        "child_a",
        "child_b",
    ]
    # Children are constraint pipelines: not independently scheduled.
    scheduled = {node.node_id for node in flow.nodes}
    assert "child_a" not in scheduled
    assert "child_b" not in scheduled
    assert parent_id in scheduled
    parent = flow.node_map[parent_id]
    assert "约束管线" in parent.responsibility
    assert "子步骤 A" in parent.responsibility
    assert "子步骤 B" in parent.responsibility
    assert flow.meta["schedule_skipped_node_ids"] == ["child_a", "child_b"]
    assert [item["node_id"] for item in flow.meta["constraint_pipelines"][parent_id]] == [
        "child_a",
        "child_b",
    ]


def test_to_expert_flow_keeps_pass_fail_nested_children_on_schedule():
    raw = _bind_all_nodes(get_template("linear", goal="审计分流子阶段").as_dict())
    raw["nodes"] = list(raw["nodes"]) + [
        {
            "node_id": "revise_exp",
            "kind": "implement",
            "label": "修订实验",
            "goal": "按审计意见修改",
            "model": {"provider_key": "p", "model_id": "m-revise"},
        }
    ]
    raw["edges"] = [
        edge
        for edge in raw["edges"]
        if not (
            (edge.get("from_node") or edge.get("from")) == "audit"
        )
    ] + [
        {"from_node": "audit", "to_node": "summarize", "condition": "pass"},
        {"from_node": "audit", "to_node": "revise_exp", "condition": "fail"},
        {"from_node": "revise_exp", "to_node": "summarize", "condition": "always"},
    ]
    raw["meta"] = {
        "canvas_groups": {
            "implement": {
                "expanded": True,
                "children": ["audit", "revise_exp"],
                "label": "实现",
            }
        },
        "pipeline_graph_id": "g_keep_canvas",
    }
    spec = OrchestrationSpec.from_value(raw)
    flow = to_expert_flow(spec)
    scheduled = {node.node_id for node in flow.nodes}
    assert "audit" in scheduled
    assert "revise_exp" in scheduled
    pairs = {(edge.from_node, edge.to_node, edge.condition) for edge in flow.edges}
    assert ("audit", "summarize", "pass") in pairs
    assert ("audit", "revise_exp", "fail") in pairs
    assert flow.meta.get("pipeline_graph_id") == "g_keep_canvas"
    back = from_expert_flow(flow)
    assert back.meta.get("pipeline_graph_id") == "g_keep_canvas"


def test_to_expert_flow_remaps_edges_through_constraint_parent():
    raw = _bind_all_nodes(get_template("linear", goal="边折叠").as_dict())
    parent_id = raw["nodes"][1]["node_id"]
    next_id = raw["nodes"][2]["node_id"]
    raw["nodes"] = list(raw["nodes"]) + [
        {
            "node_id": "child_a",
            "kind": "implement",
            "label": "子 A",
            "goal": "做 A",
            "model": {"provider_key": "p", "model_id": "m"},
        }
    ]
    # Parent → child → next  should become Parent → next after fold.
    raw["edges"] = [
        edge
        for edge in raw["edges"]
        if not (
            edge.get("from_node") == parent_id and edge.get("to_node") == next_id
        )
    ] + [
        {"from_node": parent_id, "to_node": "child_a", "condition": "always"},
        {"from_node": "child_a", "to_node": next_id, "condition": "always"},
    ]
    raw["meta"] = {
        "canvas_groups": {
            parent_id: {
                "expanded": True,
                "children": ["child_a"],
                "label": "父",
            }
        }
    }
    flow = to_expert_flow(OrchestrationSpec.from_value(raw))
    pairs = {(edge.from_node, edge.to_node) for edge in flow.edges}
    assert (parent_id, "child_a") not in pairs
    assert ("child_a", next_id) not in pairs
    assert (parent_id, next_id) in pairs


def test_to_expert_flow_infers_unnamed_child_chain_without_canvas_groups():
    raw = _bind_all_nodes(get_template("linear", goal="推断子阶段").as_dict())
    parent_id = raw["nodes"][1]["node_id"]
    next_id = raw["nodes"][2]["node_id"]
    raw["nodes"] = list(raw["nodes"]) + [
        {
            "node_id": "stage_aa11",
            "kind": "implement",
            "label": "子阶段",
            "goal": "核对图表",
            "model": {"provider_key": "p", "model_id": "m"},
        },
        {
            "node_id": "stage_bb22",
            "kind": "implement",
            "label": "子阶段",
            "goal": "核对表格",
            "model": {"provider_key": "p", "model_id": "m"},
        },
    ]
    raw["edges"] = [
        edge
        for edge in raw["edges"]
        if not (
            edge.get("from_node") == parent_id and edge.get("to_node") == next_id
        )
    ] + [
        {"from_node": parent_id, "to_node": "stage_aa11", "condition": "always"},
        {"from_node": "stage_aa11", "to_node": "stage_bb22", "condition": "always"},
        {"from_node": "stage_bb22", "to_node": next_id, "condition": "always"},
    ]
    spec = OrchestrationSpec.from_value(raw)
    flow = to_expert_flow(spec)
    scheduled = [node.node_id for node in flow.nodes]
    assert "stage_aa11" not in scheduled
    assert "stage_bb22" not in scheduled
    assert parent_id in scheduled
    assert next_id in scheduled
    assert scheduled.index(parent_id) < scheduled.index(next_id)
    groups = (flow.meta or {}).get("canvas_groups") or {}
    assert groups[parent_id]["children"] == ["stage_aa11", "stage_bb22"]
    pairs = {(edge.from_node, edge.to_node) for edge in flow.edges}
    assert (parent_id, next_id) in pairs
    assert (parent_id, "stage_aa11") not in pairs
    parent = flow.node_map[parent_id]
    assert "核对图表" in parent.responsibility
    assert "核对表格" in parent.responsibility


def test_to_pipeline_graph_nests_inferred_child_chain():
    raw = _bind_all_nodes(get_template("linear", goal="画布嵌套").as_dict())
    parent_id = raw["nodes"][1]["node_id"]
    next_id = raw["nodes"][2]["node_id"]
    raw["nodes"] = list(raw["nodes"]) + [
        {
            "node_id": "stage_aa11",
            "kind": "implement",
            "label": "子阶段",
            "goal": "核对图表",
            "model": {"provider_key": "p", "model_id": "m"},
            "position": {"x": 2000.0, "y": 160.0},
        },
        {
            "node_id": "stage_bb22",
            "kind": "implement",
            "label": "子阶段",
            "goal": "核对表格",
            "model": {"provider_key": "p", "model_id": "m"},
            "position": {"x": 2280.0, "y": 160.0},
        },
    ]
    raw["edges"] = [
        edge
        for edge in raw["edges"]
        if not (
            edge.get("from_node") == parent_id and edge.get("to_node") == next_id
        )
    ] + [
        {"from_node": parent_id, "to_node": "stage_aa11", "condition": "always"},
        {"from_node": "stage_aa11", "to_node": "stage_bb22", "condition": "always"},
        {"from_node": "stage_bb22", "to_node": next_id, "condition": "always"},
    ]
    spec = OrchestrationSpec.from_value(raw)
    graph = to_pipeline_graph(spec, graph_id="g_infer_nest")
    assert graph_has_canvas_nesting(graph)
    payload = json.loads(graph.variables["__orch.payload__"])
    assert payload["nodes"][parent_id].get("refine_group") is True
    assert payload["nodes"]["stage_aa11"].get("parent_group") == parent_id
    assert payload["nodes"]["stage_bb22"].get("parent_group") == parent_id
    parent = next(node for node in graph.nodes if node.id == parent_id)
    child = next(node for node in graph.nodes if node.id == "stage_aa11")
    assert child.position.x == parent.position.x + 24.0
    assert child.position.x < 400
    back = from_pipeline_graph(graph)
    assert back.meta["canvas_groups"][parent_id]["children"] == [
        "stage_aa11",
        "stage_bb22",
    ]


def test_sync_infers_child_chain_on_flat_canvas():
    raw = _bind_all_nodes(get_template("linear", goal="扁平画布推断").as_dict())
    parent_id = raw["nodes"][1]["node_id"]
    next_id = raw["nodes"][2]["node_id"]
    raw["nodes"] = list(raw["nodes"]) + [
        {
            "node_id": "stage_aa11",
            "kind": "implement",
            "label": "子阶段",
            "goal": "核对图表",
            "model": {"provider_key": "p", "model_id": "m"},
            "position": {"x": 2000.0, "y": 160.0},
        }
    ]
    raw["edges"] = [
        edge
        for edge in raw["edges"]
        if not (
            edge.get("from_node") == parent_id and edge.get("to_node") == next_id
        )
    ] + [
        {"from_node": parent_id, "to_node": "stage_aa11", "condition": "always"},
        {"from_node": "stage_aa11", "to_node": next_id, "condition": "always"},
    ]
    spec = OrchestrationSpec.from_value(raw)
    nested = to_pipeline_graph(spec, graph_id="g_flat_infer")
    payload = json.loads(nested.variables["__orch.payload__"])
    payload["meta"]["canvas_groups"] = {}
    for entry in payload["nodes"].values():
        if not isinstance(entry, dict):
            continue
        entry.pop("refine_group", None)
        entry.pop("parent_group", None)
        entry.pop("checkpoint_layout_version", None)
    flat = nested.model_copy(
        update={
            "variables": {
                **nested.variables,
                "__orch.payload__": json.dumps(payload, ensure_ascii=False),
            }
        }
    )
    assert not graph_has_canvas_nesting(flat)
    synced = sync_orchestration_graph_from_spec(flat, spec)
    assert graph_has_canvas_nesting(synced)
    restored = json.loads(synced.variables["__orch.payload__"])
    assert restored["nodes"][parent_id].get("refine_group") is True
    assert restored["nodes"]["stage_aa11"].get("parent_group") == parent_id
    child = next(node for node in synced.nodes if node.id == "stage_aa11")
    parent = next(node for node in synced.nodes if node.id == parent_id)
    assert child.position.x == parent.position.x + 24.0


def test_round_trip_orchestration_pipeline_bridge():
    raw = _bind_all_nodes(get_template("linear", goal="桥接画布").as_dict())
    spec = OrchestrationSpec.from_value(raw)
    graph = to_pipeline_graph(spec, graph_id="g_orch_test")
    assert is_orchestration_graph(graph)
    assert any(node.type.value == "start" for node in graph.nodes)
    assert any(node.type.value == "end" for node in graph.nodes)
    back = from_pipeline_graph(graph)
    assert back.goal == "桥接画布"
    assert {node.node_id for node in back.nodes} == {node.node_id for node in spec.nodes}
    by_id = back.node_map()
    for node in spec.nodes:
        if node.kind == "gate":
            continue
        assert by_id[node.node_id].model is not None
        assert by_id[node.node_id].model.model_id == node.model.model_id
        assert by_id[node.node_id].kind == node.kind


def test_recovery_fields_survive_pipeline_bridge():
    """fallback / on_blocked / loop_policy / escalation ride the canvas losslessly."""
    spec = OrchestrationSpec.from_value({
        "spec_id": "s",
        "label": "S",
        "nodes": [
            {
                "node_id": "a", "kind": "implement", "label": "A",
                "on_blocked": "escalate_model",
                "fallback_node_ids": ["b"],
                "max_auto_recovery": 2,
                "escalation_model": {"provider_key": "p", "model_id": "m"},
                "loop_policy": {"stall_threshold": 5, "on_stall": "fallback", "fallback_node_id": "b"},
            },
            {"node_id": "b", "kind": "reduce", "label": "B"},
        ],
        "goal_contract": {"objective": "交付 X", "constraints": ["不改公式"],
                          "assumptions": ["默认八篇"],
                          "run_inputs": [{"name": "主题", "required": True, "default": ""}]},
        "actions": [{"action_id": "a1", "revision": 1, "kind": "insert_node", "actor": "user"}],
    })
    from mewcode.orchestration.pipeline_bridge import merge_model_provider_keys
    back = merge_model_provider_keys(from_pipeline_graph(to_pipeline_graph(spec)))
    node = back.node_map()["a"]
    assert node.on_blocked == "escalate_model"
    assert node.fallback_node_ids == ("b",)
    assert node.max_auto_recovery == 2
    assert node.escalation_model is not None and node.escalation_model.model_id == "m"
    assert node.loop_policy is not None
    assert node.loop_policy.stall_threshold == 5
    assert node.loop_policy.on_stall == "fallback"
    assert node.loop_policy.fallback_node_id == "b"
    assert back.goal_contract is not None and back.goal_contract.constraints == ("不改公式",)
    assert back.goal_contract == spec.goal_contract
    assert [a.action_id for a in back.actions] == ["a1"]


def test_recovery_fields_default_clean_round_trip():
    """Unset recovery fields stay empty and never materialize phantom defaults."""
    spec = OrchestrationSpec.from_value({
        "spec_id": "s", "label": "S",
        "nodes": [
            {"node_id": "a", "kind": "implement", "label": "A"},
            {"node_id": "b", "kind": "reduce", "label": "B"},
        ],
    })
    back = from_pipeline_graph(to_pipeline_graph(spec))
    node = back.node_map()["a"]
    assert node.on_blocked == ""
    assert node.fallback_node_ids == ()
    assert node.escalation_model is None
    assert node.max_auto_recovery == 1
    assert node.loop_policy is None


def test_validate_spec_rejects_bad_fallback_and_warns():
    spec = OrchestrationSpec.from_value({
        "spec_id": "s", "label": "S",
        "nodes": [
            {"node_id": "a", "kind": "implement", "label": "A", "fallback_node_ids": ["ghost"]},
            {"node_id": "b", "kind": "reduce", "label": "B"},
        ],
    })
    try:
        validate_spec(spec)
        raise AssertionError("expected OrchestrationError for missing fallback target")
    except OrchestrationError as exc:
        assert "ghost" in str(exc)

    spec2 = OrchestrationSpec.from_value({
        "spec_id": "s", "label": "S",
        "nodes": [
            {"node_id": "a", "kind": "implement", "label": "A", "on_blocked": "fallback",
             "model": {"provider_key": "p", "model_id": "m"}},
            {"node_id": "b", "kind": "reduce", "label": "B",
             "model": {"provider_key": "p", "model_id": "m"}},
        ],
    })
    warnings = validate_spec(spec2)
    assert any("on_blocked=fallback" in w for w in warnings)


def test_rehydrate_canvas_nesting_from_meta_groups():
    """Flags stripped from node meta can be rebuilt from meta.canvas_groups."""
    from mewcode.orchestration.pipeline_bridge import (
        graph_has_canvas_nesting,
        rehydrate_canvas_nesting,
    )

    raw = _bind_all_nodes(get_template("linear", goal="恢复嵌套标志").as_dict())
    parent_id = raw["nodes"][1]["node_id"]
    raw["nodes"] = list(raw["nodes"]) + [
        {
            "node_id": "child_a",
            "kind": "implement",
            "label": "子阶段 A",
            "goal": "验收 A",
            "model": {"provider_key": "p", "model_id": "m"},
        }
    ]
    raw["meta"] = {
        "canvas_groups": {
            parent_id: {
                "expanded": True,
                "children": ["child_a"],
                "label": "父",
            }
        }
    }
    nested = OrchestrationSpec.from_value(raw)
    graph = to_pipeline_graph(nested, graph_id="g_rehydrate")
    payload = json.loads(graph.variables["__orch.payload__"])
    # Simulate a flattened payload that kept canvas_groups but lost flags.
    payload["nodes"][parent_id].pop("refine_group", None)
    payload["nodes"]["child_a"].pop("parent_group", None)
    graph = graph.model_copy(
        update={
            "variables": {
                "__orchestration__": "1",
                "__orch.payload__": json.dumps(payload, ensure_ascii=False),
            }
        }
    )
    assert not any(
        isinstance(item, dict) and item.get("refine_group")
        for item in json.loads(graph.variables["__orch.payload__"])["nodes"].values()
    )
    restored = rehydrate_canvas_nesting(graph)
    assert graph_has_canvas_nesting(restored)
    restored_payload = json.loads(restored.variables["__orch.payload__"])
    assert restored_payload["nodes"][parent_id].get("refine_group") is True
    assert restored_payload["nodes"]["child_a"].get("parent_group") == parent_id


def test_sync_orchestration_graph_keeps_nesting_when_plan_meta_lost():
    """Reopen must not flatten refine groups if the plan session dropped canvas_groups."""
    from mewcode.orchestration.pipeline_bridge import (
        graph_has_canvas_nesting,
        sync_orchestration_graph_from_spec,
    )

    raw = _bind_all_nodes(get_template("linear", goal="保留嵌套").as_dict())
    parent_id = raw["nodes"][1]["node_id"]
    raw["nodes"] = list(raw["nodes"]) + [
        {
            "node_id": "child_a",
            "kind": "implement",
            "label": "子阶段 A",
            "goal": "验收 A",
            "model": {"provider_key": "p", "model_id": "m"},
            "position": {"x": 140.0, "y": 320.0},
        },
        {
            "node_id": "child_b",
            "kind": "implement",
            "label": "子阶段 B",
            "goal": "验收 B",
            "model": {"provider_key": "p", "model_id": "m"},
            "position": {"x": 140.0, "y": 460.0},
        },
    ]
    raw["edges"] = list(raw["edges"]) + [
        {"from_node": parent_id, "to_node": "child_a"},
        {"from_node": "child_a", "to_node": "child_b"},
    ]
    raw["meta"] = {
        "canvas_groups": {
            parent_id: {
                "expanded": True,
                "children": ["child_a", "child_b"],
                "label": raw["nodes"][1]["label"],
            }
        }
    }
    nested = OrchestrationSpec.from_value(raw)
    stored = to_pipeline_graph(nested, graph_id="g_nested_keep")
    assert graph_has_canvas_nesting(stored)

    # Simulate plan session that lost canvas_groups but kept the same stages.
    flat_raw = nested.as_dict()
    flat_raw["meta"] = {"pipeline_graph_id": "g_nested_keep"}
    flat_raw["nodes"][1]["label"] = "父阶段已改名"
    flat = OrchestrationSpec.from_value(flat_raw)

    synced = sync_orchestration_graph_from_spec(stored, flat)
    assert graph_has_canvas_nesting(synced)
    payload = json.loads(synced.variables["__orch.payload__"])
    assert payload["nodes"][parent_id].get("refine_group") is True
    assert payload["nodes"]["child_a"].get("parent_group") == parent_id
    assert payload["nodes"]["child_b"].get("parent_group") == parent_id
    parent_node = next(node for node in synced.nodes if node.id == parent_id)
    assert parent_node.label == "父阶段已改名"
    child = next(node for node in synced.nodes if node.id == "child_a")
    assert child.position.x == 140.0
    assert child.position.y == 320.0
    back = from_pipeline_graph(synced)
    assert parent_id in (back.meta or {}).get("canvas_groups", {})
    assert (back.meta or {})["canvas_groups"][parent_id]["children"] == [
        "child_a",
        "child_b",
    ]


def test_propose_applies_bindings():
    binding = {"provider_key": "p1", "model_id": "m1"}
    result = propose_orchestration(
        "前端和后端一起改",
        bindings={
            "manager": binding,
            "planner": binding,
            "executor": binding,
            "auditor": binding,
            "frontend": {"provider_key": "fe", "model_id": "ui-model"},
            "backend": {"provider_key": "be", "model_id": "api-model"},
        },
    )
    assert result["template_id"] == "fe_be_parallel"
    nodes = {n["node_id"]: n for n in result["spec"]["nodes"]}
    assert nodes["fe_impl"]["model"]["model_id"] == "ui-model"
    assert nodes["be_impl"]["model"]["model_id"] == "api-model"
    # runnable after bindings
    warnings = validate_spec(
        OrchestrationSpec.from_value(result["spec"]),
        require_bindings=True,
    )
    assert isinstance(warnings, list)


def test_apply_profile_bindings_bind_later_gate():
    spec = get_template("linear")
    filled = apply_profile_bindings(
        spec,
        manager={"provider_key": "p", "model_id": "m"},
        executor={"provider_key": "p", "model_id": "m"},
        auditor={"provider_key": "p", "model_id": "m"},
        planner={"provider_key": "p", "model_id": "m"},
    )
    gate = next(n for n in filled.nodes if n.kind == "gate")
    assert gate.model is None
    plan = next(n for n in filled.nodes if n.kind == "plan")
    assert plan.model is not None and plan.model.is_complete


def test_audit_failure_policy_routed():
    flow = to_expert_flow(get_template("linear"))
    auditor = next(n for n in flow.nodes if n.role == "auditor")
    assert auditor.failure_policy.action == "route"
    assert auditor.failure_policy.target_node_id == "implement"
    assert auditor.failure_policy.max_attempts <= 10


def _bind_all_nodes(spec_dict: dict) -> dict:
    for node in spec_dict["nodes"]:
        if node.get("kind") == "gate":
            continue
        node["model"] = {
            "provider_key": "p",
            "model_id": f"m-{node['node_id']}",
        }
    return spec_dict


def test_pick_template_research_write():
    assert pick_template_id("帮我实现一篇论文的撰写流程") == "research_write"
    assert pick_template_id("整理相关文献") == "research_write"
    assert pick_template_id("撰写调研报告") == "research_write"


def test_research_write_template_shape():
    spec = get_template("research_write", goal="写一篇综述")
    assert len(spec.nodes) >= 3
    kinds = {node.kind for node in spec.nodes}
    assert "plan" in kinds
    assert "implement" in kinds
    assert "audit" in kinds
    write_nodes = [
        node.node_id
        for node in spec.nodes
        if node.kind == "implement" and node.prefers == "copy"
    ]
    assert len(write_nodes) >= 2
    plan = build_control_plan(spec)
    assert any(stage.mode == "parallel" for stage in plan.stages)
    validate_spec(spec)


def test_propose_returns_control_plan_preview():
    result = propose_orchestration("修一个小 bug")
    assert result["confirmed"] is False
    assert result["control_plan"]["stages"]
    assert result["plan_session"]["status"] in {"drafting", "binding", "ready"}
    ControlPlan.from_value(result["control_plan"])


def test_propose_revise_keeps_bindings_and_marks_applied():
    first = propose_orchestration("修一个小 bug")
    bound = _bind_all_nodes(first["spec"])
    second = propose_orchestration(
        "修一个小 bug",
        previous_spec=bound,
        revision="补充测试步骤",
    )
    assert second["revision_applied"] is True
    assert "已应用修订" in second["message"]
    assert second["spec"]["version"] > first["spec"]["version"]
    nodes = {item["node_id"]: item for item in second["spec"]["nodes"]}
    assert nodes["plan"]["model"]["model_id"] == "m-plan"
    assert nodes["implement"]["model"]["model_id"] == "m-implement"


def test_propose_revise_switches_to_research_template():
    first = propose_orchestration("修一个小 bug")
    bound = _bind_all_nodes(first["spec"])
    second = propose_orchestration(
        "修一个小 bug",
        previous_spec=bound,
        revision="改成论文撰写流程",
    )
    assert second["template_id"] == "research_write"
    assert second["revision_applied"] is True
    survey = next(item for item in second["spec"]["nodes"] if item["kind"] == "plan")
    assert survey["model"]["model_id"] == "m-plan"
    write_ids = {
        item["node_id"]
        for item in second["spec"]["nodes"]
        if item["kind"] == "implement" and item.get("prefers") == "copy"
    }
    assert write_ids
    assert first["spec"]["nodes"] != second["spec"]["nodes"]


def test_propose_raw_planner_json_and_try_parse():
    parsed = try_parse_planner_json(
        'here {"spec_id": "from-json", "label": "json", "nodes": ['
        '{"node_id": "plan", "kind": "plan", "label": "方案"},'
        '{"node_id": "summarize", "kind": "reduce", "label": "总结"}'
        '], "edges": [{"from": "plan", "to": "summarize"}]}'
    )
    assert parsed is not None
    result = propose_orchestration("从 JSON 来", raw_spec=parsed)
    assert result["spec"]["spec_id"] == "from-json"
    ids = {item["node_id"] for item in result["spec"]["nodes"]}
    assert ids == {"plan", "summarize"}


def test_control_plan_retry_degrade_and_checkpoint():
    spec = get_template("linear", goal="x")
    plan = build_control_plan(spec, draft=True)
    assert plan.draft is True
    assert any(item.kind == "gate" for item in plan.checkpoints)
    assert any(rule.node_id == "implement" and rule.action == "retry" for rule in plan.retries)
    assert any(
        rule.node_id == "audit" and rule.target_node_id == "implement"
        for rule in plan.degrade
    )
    assert "重试" in plan.summary() or "检查点" in plan.summary()
    fe = build_control_plan(get_template("fe_be_parallel"))
    assert any(stage.mode == "parallel" and set(stage.node_ids) >= {"fe_impl", "be_impl"} for stage in fe.stages)


def test_confirm_requires_bindings():
    spec = get_template("linear", goal="x")
    assert unbound_node_ids(spec)
    with pytest.raises(OrchestrationError, match=MISSING_BINDINGS_PREFIX):
        validate_spec(spec, require_bindings=True)
    filled = apply_profile_bindings(
        spec,
        manager={"provider_key": "p", "model_id": "m"},
        planner={"provider_key": "p", "model_id": "m"},
        executor={"provider_key": "p", "model_id": "m"},
        auditor={"provider_key": "p", "model_id": "m"},
    )
    warnings = validate_spec(filled, require_bindings=True)
    assert isinstance(warnings, list)
    ready = build_control_plan(filled, draft=False)
    assert ready.draft is False
    flow = to_expert_flow(filled)
    assert flow.finalizer_node_id == filled.finalizer_node_id


def test_plan_session_status_and_store(tmp_path):
    spec = get_template("linear")
    assert infer_plan_status(spec) == "drafting"
    filled = apply_profile_bindings(
        spec,
        planner={"provider_key": "p", "model_id": "m"},
    )
    assert infer_plan_status(filled) == "binding"
    complete = apply_profile_bindings(
        spec,
        manager={"provider_key": "p", "model_id": "m"},
        planner={"provider_key": "p", "model_id": "m"},
        executor={"provider_key": "p", "model_id": "m"},
        auditor={"provider_key": "p", "model_id": "m"},
    )
    assert infer_plan_status(complete) == "ready"
    assert infer_plan_status(complete, activate=True) == "running"
    store = PlanSessionStore(tmp_path / "orchestration-sessions.json")
    session = propose_orchestration("修 bug")["plan_session"]
    saved = store.put(tmp_path / "proj", session)
    loaded = store.get(tmp_path / "proj")
    assert loaded is not None
    assert loaded.session_id == saved.session_id
    assert loaded.spec is not None


def test_format_dialogue_and_user_turn():
    messages = [
        Message(role="system", content="ignore"),
        Message(role="user", content="帮我写论文流程"),
        Message(role="assistant", content="可以拆成调研与撰写"),
    ]
    text = format_dialogue_transcript(messages)
    assert "用户：帮我写论文流程" in text
    assert "助手：可以拆成调研与撰写" in text
    assert dialogue_has_user_turn(messages)
    assert not dialogue_has_user_turn([Message(role="assistant", content="hi")])
    assert try_parse_planner_json(
        '```json\n{"goal":"x","nodes":[{"node_id":"plan","kind":"plan","label":"p"}]}\n```'
    )["goal"] == "x"


def test_format_dialogue_transcript_tail_on_overflow():
    messages = [Message(role="user", content="原始要求" * 5000)]
    with pytest.raises(OrchestrationError, match="未截断前文"):
        format_dialogue_transcript(messages)
    result = format_dialogue_transcript(messages, tail_on_overflow=True)
    assert result.startswith("…(前文已截断)\n")
    assert len(result) <= 16000 + len("…(前文已截断)\n")


@pytest.mark.asyncio
async def test_refill_plan_briefs_refuses_truncated_history(monkeypatch):
    current = _spec_with_two_implement_stages(
        code_id="write_code", docs_id="write_docs", with_models=True
    )
    briefs_raw = {
        "stages": [
            {"stage_id": "write_code", "goal": "实现功能", "known_inputs": [], "open_questions": []},
            {"stage_id": "write_docs", "goal": "补文档", "known_inputs": [], "open_questions": []},
        ]
    }

    async def fake_collect(client, *, system, user_text, **kwargs):
        pytest.fail("Long history must be rejected before calling the model")

    import mewcode.orchestration.extract as extract_mod

    monkeypatch.setattr(extract_mod, "collect_model_text", fake_collect)
    with pytest.raises(OrchestrationError, match="未截断前文"):
        await refill_plan_briefs_from_dialogue(
            object(), [Message(role="user", content="原始要求" * 5000)], spec=current,
        )


@pytest.mark.asyncio
async def test_collect_model_text_reports_live_stream_progress(monkeypatch):
    async def fake_stream(client, conversation, system="", tools=None, **_schedule):
        yield StreamStatus("accepted", "Provider 已接收请求。")
        yield ThinkingDelta("推理中", kind="summary")
        yield TextDelta('{"nodes":')
        yield TextDelta("[]}")
        yield StreamEnd("stop", input_tokens=12, output_tokens=8)

    import mewcode.orchestration.extract as extract_mod

    monkeypatch.setattr(extract_mod, "scheduled_client_stream", fake_stream)
    events = []
    text = await collect_model_text(
        object(),
        system="生成阶段图",
        user_text="测试",
        progress=events.append,
    )

    assert text == '{"nodes":[]}'
    assert [event["stage"] for event in events] == [
        "requesting",
        "accepted",
        "thinking",
        "streaming",
        "streaming",
        "received",
    ]
    assert events[-1]["output_chars"] == len(text)
    assert events[-1]["output_chunks"] == 2
    assert events[-1]["output_preview"] == text
    assert events[-1]["output_truncated"] is False
    assert events[-1]["thinking_summary_chars"] == len("推理中")
    assert events[-1]["thinking_preview"] == "推理中"
    assert events[-1]["thinking_truncated"] is False
    assert events[-1]["input_tokens"] == 12
    assert events[-1]["provider_events"] == 5


@pytest.mark.asyncio
async def test_collect_model_text_never_exposes_private_reasoning(monkeypatch):
    async def fake_stream(client, conversation, system="", tools=None, **_schedule):
        yield ThinkingDelta("private chain of thought")
        yield TextDelta('{"nodes":[]}')
        yield StreamEnd("stop", input_tokens=12, output_tokens=8)

    import mewcode.orchestration.extract as extract_mod

    monkeypatch.setattr(extract_mod, "scheduled_client_stream", fake_stream)
    events = []
    await collect_model_text(
        object(),
        system="生成阶段图",
        user_text="测试",
        progress=events.append,
    )

    assert max(int(event.get("thinking_chars") or 0) for event in events) > 0
    assert all(not event.get("thinking_preview") for event in events)


@pytest.mark.asyncio
async def test_collect_model_text_times_out_a_silent_first_event(monkeypatch):
    async def silent_stream(client, conversation, system="", tools=None, **_schedule):
        await asyncio.sleep(10)
        yield TextDelta("too late")

    import mewcode.orchestration.extract as extract_mod

    monkeypatch.setattr(extract_mod, "scheduled_client_stream", silent_stream)
    events = []
    with pytest.raises(OrchestrationStreamTimeout) as caught:
        await collect_model_text(
            object(),
            system="生成阶段图",
            user_text="测试",
            progress=events.append,
            first_event_timeout=0.01,
        )

    assert caught.value.phase == "first_event"
    assert events[-1]["stage"] == "stalled"


@pytest.mark.asyncio
async def test_collect_model_text_times_out_only_after_activity_becomes_idle(
    monkeypatch,
):
    async def stalled_stream(client, conversation, system="", tools=None, **_schedule):
        yield TextDelta('{"nodes":')
        await asyncio.sleep(10)
        yield TextDelta("[]}")

    import mewcode.orchestration.extract as extract_mod

    monkeypatch.setattr(extract_mod, "scheduled_client_stream", stalled_stream)
    events = []
    with pytest.raises(OrchestrationStreamTimeout) as caught:
        await collect_model_text(
            object(),
            system="生成阶段图",
            user_text="测试",
            progress=events.append,
            first_event_timeout=0.1,
            idle_timeout=0.01,
        )

    assert caught.value.phase == "stream_idle"
    assert caught.value.output_chars == len('{"nodes":')
    assert events[-1]["stalled_phase"] == "stream_idle"


@pytest.mark.asyncio
async def test_collect_model_text_allows_silent_reasoning_beyond_stream_idle(
    monkeypatch,
):
    async def reasoning_stream(client, conversation, system="", tools=None, **_schedule):
        yield StreamStatus("accepted", "Provider 已接收请求。")
        yield ThinkingDelta("正在规划", kind="summary")
        await asyncio.sleep(0.03)
        yield TextDelta('{"nodes":[]}')
        yield StreamEnd("stop", input_tokens=8, output_tokens=4)

    import mewcode.orchestration.extract as extract_mod

    monkeypatch.setattr(extract_mod, "scheduled_client_stream", reasoning_stream)
    events = []
    text = await collect_model_text(
        object(),
        system="生成阶段图",
        user_text="测试",
        progress=events.append,
        first_event_timeout=0.08,
        reasoning_idle_timeout=0.08,
        idle_timeout=0.01,
    )

    assert text == '{"nodes":[]}'
    assert not any(event["stage"] == "stalled" for event in events)


@pytest.mark.asyncio
async def test_collect_model_text_reports_a_distinct_reasoning_idle_timeout(
    monkeypatch,
):
    async def reasoning_stream(client, conversation, system="", tools=None, **_schedule):
        yield StreamStatus("accepted", "Provider 已接收请求。")
        yield ThinkingDelta("正在规划", kind="summary")
        await asyncio.sleep(10)
        yield TextDelta('{"nodes":[]}')

    import mewcode.orchestration.extract as extract_mod

    monkeypatch.setattr(extract_mod, "scheduled_client_stream", reasoning_stream)
    events = []
    with pytest.raises(OrchestrationStreamTimeout) as caught:
        await collect_model_text(
            object(),
            system="生成阶段图",
            user_text="测试",
            progress=events.append,
            first_event_timeout=0.08,
            reasoning_idle_timeout=0.01,
            idle_timeout=0.005,
        )

    assert caught.value.phase == "reasoning_idle"
    assert events[-1]["stalled_phase"] == "reasoning_idle"
    assert "不会自动重复提交" in events[-1]["detail"]


@pytest.mark.asyncio
async def test_collect_model_text_refreshes_idle_watchdog_for_tool_activity(
    monkeypatch,
):
    async def active_stream(client, conversation, system="", tools=None, **_schedule):
        yield StreamStatus("accepted", "Provider 已接收请求。")
        await asyncio.sleep(0.03)
        yield ToolCallStart("lookup", "call-1")
        await asyncio.sleep(0.03)
        yield ToolCallDelta('{"query":"stage graph"}')
        await asyncio.sleep(0.03)
        yield ToolCallComplete("call-1", "lookup", {"query": "stage graph"})
        await asyncio.sleep(0.03)
        yield TextDelta('{"nodes":[]}')
        yield StreamEnd("stop", input_tokens=8, output_tokens=4)

    import mewcode.orchestration.extract as extract_mod

    monkeypatch.setattr(extract_mod, "scheduled_client_stream", active_stream)
    events = []
    text = await collect_model_text(
        object(),
        system="生成阶段图",
        user_text="测试",
        progress=events.append,
        first_event_timeout=0.08,
        idle_timeout=0.08,
    )

    assert text == '{"nodes":[]}'
    assert events[-1]["provider_events"] == 6
    assert any(event["stage"] == "tooling" for event in events)
    assert max(
        int(event.get("tool_call_chars") or 0) for event in events
    ) == len('{"query":"stage graph"}')


@pytest.mark.asyncio
async def test_extract_orchestration_from_dialogue_parses_model_json(monkeypatch):
    sample = {
        "goal": "论文撰写流程",
        "nodes": [
            {"node_id": "plan", "kind": "plan", "label": "规划", "goal": "定大纲"},
            {"node_id": "implement", "kind": "implement", "label": "撰写", "goal": "写正文"},
            {
                "node_id": "audit",
                "kind": "audit",
                "label": "审查",
                "goal": "审稿",
                "audit_target": "implement",
            },
        ],
        "edges": [
            {"from": "plan", "to": "implement"},
            {"from": "implement", "to": "audit"},
        ],
    }

    async def fake_collect(client, *, system, user_text):
        assert "阶段图" in system or "编排" in system
        assert "用户：" in user_text
        return json.dumps(sample, ensure_ascii=False)

    import mewcode.orchestration.extract as extract_mod

    monkeypatch.setattr(extract_mod, "collect_model_text", fake_collect)
    messages = [
        Message(role="user", content="帮我实现论文撰写与审批"),
        Message(role="assistant", content="建议先规划再撰写再审查"),
    ]
    result = await extract_orchestration_from_dialogue(object(), messages)
    assert result["spec"]["goal"] == "论文撰写流程"
    assert len(result["spec"]["nodes"]) >= 3
    assert result["control_plan"]
    assert any(node.get("plan_brief") for node in result["spec"]["nodes"])


def test_repair_extracted_audit_cycle_uses_audit_target():
    raw = {
        "goal": "实现并审核",
        "nodes": [
            {"node_id": "plan", "kind": "plan", "label": "规划"},
            {"node_id": "implement", "kind": "implement", "label": "实现"},
            {"node_id": "audit", "kind": "audit", "label": "审核"},
        ],
        "edges": [
            {"from": "plan", "to": "implement"},
            {"from": "implement", "to": "audit"},
            {"from": "audit", "to": "implement"},
        ],
    }

    repaired, warnings = repair_extracted_graph_cycles(raw)

    assert repaired["nodes"][2]["audit_target"] == "implement"
    assert {(_edge["from"], _edge["to"]) for _edge in repaired["edges"]} == {
        ("plan", "implement"),
        ("implement", "audit"),
    }
    assert warnings and "audit_target=implement" in warnings[0]
    validate_spec(OrchestrationSpec.from_value(repaired))


def test_from_pipeline_graph_repairs_canvas_loop_to_audit_target():
    from mewcode.pipeline.models import GraphEdge

    raw = _bind_all_nodes(get_template("linear", goal="画布循环").as_dict())
    spec = OrchestrationSpec.from_value(raw)
    graph = to_pipeline_graph(spec, graph_id="g_canvas_loop")
    graph = graph.model_copy(
        update={
            "edges": list(graph.edges)
            + [
                GraphEdge(
                    id="e_audit_implement_loop",
                    from_node="audit",
                    to_node="implement",
                    label="fail",
                )
            ]
        }
    )
    back = from_pipeline_graph(graph)
    validate_spec(back)
    assert back.node_map()["audit"].audit_target == "implement"
    pairs = {(edge.from_node, edge.to_node) for edge in back.edges}
    assert ("implement", "audit") in pairs
    assert ("audit", "implement") not in pairs


def test_from_pipeline_graph_nested_loop_label_does_not_fail_validate():
    from mewcode.pipeline.models import GraphEdge

    raw = _bind_all_nodes(
        {
            "spec_id": "nested-loop",
            "label": "修订实验",
            "nodes": [
                {"node_id": "parent", "kind": "implement", "label": "修订实验"},
                {"node_id": "cp01", "kind": "audit", "label": "审计1"},
                {"node_id": "cp04", "kind": "implement", "label": "实现1"},
                {"node_id": "cp02", "kind": "audit", "label": "审计2"},
                {"node_id": "cp05", "kind": "implement", "label": "实现2"},
            ],
            "edges": [
                {"from": "parent", "to": "cp01"},
                {"from": "cp01", "to": "cp04", "condition": "pass"},
                {"from": "cp04", "to": "cp02"},
                {"from": "cp02", "to": "cp05", "condition": "pass"},
            ],
            "finalizer_node_id": "cp05",
            "meta": {
                "canvas_groups": {
                    "parent": {
                        "expanded": True,
                        "children": ["cp01", "cp04", "cp02", "cp05"],
                        "label": "修订实验",
                    }
                }
            },
        }
    )
    graph = to_pipeline_graph(OrchestrationSpec.from_value(raw), graph_id="g_nested_loop")
    graph = graph.model_copy(
        update={
            "edges": list(graph.edges)
            + [
                GraphEdge(id="e_loop_cp04_cp01", from_node="cp04", to_node="cp01", label="循环"),
                GraphEdge(id="e_fail_cp02_cp01", from_node="cp02", to_node="cp01", label="fail"),
            ]
        }
    )
    back = from_pipeline_graph(graph)
    validate_spec(back)
    pairs = {(edge.from_node, edge.to_node) for edge in back.edges}
    assert ("cp04", "cp01") not in pairs
    assert back.node_map()["cp01"].audit_target == "cp04"
    assert ("cp01", "cp04") in pairs
    assert ("cp02", "cp01") not in pairs
    assert back.meta.get("canvas_loop_rewrites")
    flow = to_expert_flow(back)
    policy = flow.node_map["cp01"].failure_policy
    assert policy.action == "route"
    assert policy.target_node_id == "cp04"


def test_from_pipeline_graph_fills_missing_checkpoint_spine():
    from mewcode.pipeline.models import GraphEdge

    raw = _bind_all_nodes(
        {
            "spec_id": "nested-spine",
            "label": "修订实验",
            "nodes": [
                {"node_id": "parent", "kind": "implement", "label": "修订实验"},
                {"node_id": "cp01", "kind": "audit", "label": "审计1"},
                {"node_id": "cp02", "kind": "implement", "label": "实现1"},
                {"node_id": "cp03", "kind": "audit", "label": "审计2"},
                {"node_id": "cp04", "kind": "implement", "label": "实现2"},
            ],
            "edges": [
                {"from": "parent", "to": "cp01"},
                {"from": "cp01", "to": "cp02", "condition": "pass"},
                {"from": "cp03", "to": "cp04", "condition": "pass"},
            ],
            "finalizer_node_id": "cp04",
            "meta": {
                "canvas_groups": {
                    "parent": {
                        "expanded": True,
                        "children": ["cp01", "cp02", "cp03", "cp04"],
                        "label": "修订实验",
                    }
                }
            },
        }
    )
    graph = to_pipeline_graph(OrchestrationSpec.from_value(raw), graph_id="g_fill_spine")
    graph = graph.model_copy(
        update={
            "edges": [
                edge
                for edge in graph.edges
                if not (
                    (edge.from_node, edge.to_node) == ("cp02", "cp03")
                    or (edge.from_node, edge.to_node) == ("cp04", "cp03")
                )
            ]
            + [
                GraphEdge(id="e_loop_cp02_cp01", from_node="cp02", to_node="cp01", label="循环"),
            ]
        }
    )
    back = from_pipeline_graph(graph)
    validate_spec(back)
    pairs = {(edge.from_node, edge.to_node) for edge in back.edges}
    assert ("cp02", "cp01") not in pairs
    assert ("cp01", "cp02") in pairs
    assert ("cp02", "cp03") in pairs
    assert ("cp03", "cp04") in pairs
    assert back.node_map()["cp01"].audit_target == "cp02"
    flow = to_expert_flow(back)
    scheduled = {node.node_id for node in flow.nodes}
    assert scheduled >= {"parent", "cp01", "cp02", "cp03", "cp04"}
    pairs = {(edge.from_node, edge.to_node) for edge in flow.edges}
    assert ("cp02", "parent") not in pairs
    assert ("cp04", "parent") not in pairs


def test_to_expert_flow_keeps_whole_group_when_any_child_branches():
    raw = _bind_all_nodes(
        {
            "spec_id": "mixed-group",
            "label": "修订实验",
            "nodes": [
                {"node_id": "parent", "kind": "implement", "label": "修订实验"},
                {"node_id": "cp01", "kind": "audit", "label": "审计"},
                {"node_id": "cp02", "kind": "implement", "label": "实现"},
                {"node_id": "cp03", "kind": "audit", "label": "再审计"},
            ],
            "edges": [
                {"from": "parent", "to": "cp01"},
                {"from": "cp01", "to": "cp02", "condition": "pass"},
                {"from": "cp02", "to": "cp03", "condition": "always"},
            ],
            "finalizer_node_id": "cp03",
            "meta": {
                "canvas_groups": {
                    "parent": {
                        "expanded": True,
                        "children": ["cp01", "cp02", "cp03"],
                        "label": "修订实验",
                    }
                }
            },
        }
    )
    spec = OrchestrationSpec.from_value(raw)
    flow = to_expert_flow(spec)
    scheduled = {node.node_id for node in flow.nodes}
    assert scheduled >= {"parent", "cp01", "cp02", "cp03"}
    pairs = {(edge.from_node, edge.to_node) for edge in flow.edges}
    assert ("cp02", "parent") not in pairs
    assert ("parent", "cp01") in pairs
    assert ("cp01", "cp02") in pairs
    assert ("cp02", "cp03") in pairs


def test_from_pipeline_graph_nested_return_to_group_does_not_fail_validate():
    from mewcode.pipeline.models import GraphEdge

    raw = _bind_all_nodes(
        {
            "spec_id": "nested-return",
            "label": "修订实验",
            "nodes": [
                {"node_id": "parent", "kind": "implement", "label": "修订实验"},
                {"node_id": "cp01", "kind": "audit", "label": "审计1"},
                {"node_id": "cp02", "kind": "implement", "label": "实现1"},
                {"node_id": "cp03", "kind": "audit", "label": "审计2"},
                {"node_id": "cp04", "kind": "implement", "label": "实现2"},
            ],
            "edges": [
                {"from": "parent", "to": "cp01"},
                {"from": "cp01", "to": "cp02", "condition": "pass"},
                {"from": "cp03", "to": "cp04", "condition": "pass"},
            ],
            "finalizer_node_id": "cp04",
            "meta": {
                "canvas_groups": {
                    "parent": {
                        "expanded": True,
                        "children": ["cp01", "cp02", "cp03", "cp04"],
                        "label": "修订实验",
                    }
                }
            },
        }
    )
    graph = to_pipeline_graph(OrchestrationSpec.from_value(raw), graph_id="g_return_group")
    payload = json.loads(graph.variables["__orch.payload__"])
    nodes = payload.get("nodes") if isinstance(payload.get("nodes"), dict) else {}
    payload["nodes"] = {
        "parent": nodes["parent"],
        "cp01": nodes["cp01"],
        "cp03": nodes["cp03"],
        "cp02": nodes["cp02"],
        "cp04": nodes["cp04"],
    }
    graph = graph.model_copy(
        update={
            "variables": {
                **graph.variables,
                "__orch.payload__": json.dumps(payload, ensure_ascii=False),
            },
            "edges": [
                edge
                for edge in graph.edges
                if (edge.from_node, edge.to_node) != ("cp02", "cp03")
            ]
            + [
                GraphEdge(id="e_loop_cp02_cp01", from_node="cp02", to_node="cp01", label="循环"),
                GraphEdge(id="e_fail_up", from_node="cp03", to_node="cp02", label="错误"),
                GraphEdge(id="e_last_to_group", from_node="cp04", to_node="parent", label=""),
            ],
        }
    )
    back = from_pipeline_graph(graph)
    validate_spec(back)
    pairs = {(edge.from_node, edge.to_node) for edge in back.edges}
    assert ("cp02", "cp01") not in pairs
    assert ("cp03", "cp02") not in pairs
    assert ("cp04", "parent") not in pairs
    assert ("cp01", "cp02") in pairs
    assert ("cp02", "cp03") in pairs
    assert ("cp03", "cp04") in pairs
    assert back.node_map()["cp01"].audit_target == "cp02"
    assert back.node_map()["cp03"].audit_target == "cp02"
    flow = to_expert_flow(back)
    flow_pairs = {(edge.from_node, edge.to_node) for edge in flow.edges}
    assert ("cp02", "parent") not in flow_pairs
    assert ("cp04", "parent") not in flow_pairs


def test_repair_extracted_non_audit_cycle_and_self_loop():
    raw = {
        "nodes": [
            {"node_id": "plan", "kind": "plan", "label": "规划"},
            {"node_id": "build", "kind": "implement", "label": "实现"},
        ],
        "edges": [
            {"from": "plan", "to": "plan"},
            {"from": "plan", "to": "build"},
            {"from": "build", "to": "plan"},
        ],
    }

    with pytest.raises(OrchestrationError, match="condition=loop"):
        repair_extracted_graph_cycles(raw)
    assert len(raw["edges"]) == 3  # Never silently delete authored dependencies.


@pytest.mark.asyncio
async def test_extract_repairs_model_audit_back_edge(monkeypatch):
    sample = {
        "goal": "实现并审核",
        "nodes": [
            {"node_id": "implement", "kind": "implement", "label": "实现"},
            {"node_id": "audit", "kind": "audit", "label": "审核"},
        ],
        "edges": [
            {"from": "implement", "to": "audit"},
            {"from": "audit", "to": "implement"},
        ],
    }

    async def fake_collect(client, *, system, user_text):
        assert "condition=loop" in system
        assert "meta.loops" in system
        assert "condition=fail" in system
        return json.dumps(sample, ensure_ascii=False)

    import mewcode.orchestration.extract as extract_mod

    monkeypatch.setattr(extract_mod, "collect_model_text", fake_collect)
    result = await extract_orchestration_from_dialogue(
        object(), [Message(role="user", content="实现功能并审核")]
    )

    assert result["normalization_warnings"]
    assert "自动修复循环依赖" in result["message"]
    spec = OrchestrationSpec.from_value(result["spec"])
    assert spec.node_map()["audit"].audit_target == "implement"
    validate_spec(spec)


@pytest.mark.asyncio
async def test_inventory_then_extract_attaches_plan_brief(monkeypatch):
    inventory_raw = {
        "goal": "审查一篇论文",
        "stages": [
            {
                "stage_id": "review_paper",
                "label": "审查论文",
                "goal": "对指定论文做方法与写作审查并给出书面意见",
                "known_inputs": ["用户要做学术审查"],
                "open_questions": ["论文地址或 PDF 在哪？"],
            }
        ],
    }
    graph_raw = {
        "goal": "审查一篇论文",
        "nodes": [
            {
                "node_id": "review_paper",
                "kind": "implement",
                "label": "审查论文",
                "goal": "对指定论文做方法与写作审查并给出书面意见",
            }
        ],
        "edges": [],
    }

    async def fake_collect(client, *, system, user_text):
        if "盘点" in system:
            return json.dumps(inventory_raw, ensure_ascii=False)
        return json.dumps(graph_raw, ensure_ascii=False)

    import mewcode.orchestration.extract as extract_mod

    monkeypatch.setattr(extract_mod, "collect_model_text", fake_collect)
    messages = [Message(role="user", content="帮我审一篇论文")]
    inventory_result = await inventory_orchestration_from_dialogue(object(), messages)
    inventory = inventory_result["inventory"]
    assert inventory["stages"][0]["open_questions"][0].startswith("论文地址")
    result = await extract_orchestration_from_dialogue(
        object(), messages, inventory=inventory
    )
    nodes = {item["node_id"]: item for item in result["spec"]["nodes"]}
    brief = nodes["review_paper"]["plan_brief"]
    assert "论文地址" in " ".join(brief["open_questions"])
    assert result["needs_clarify"] is True
    assert "论文地址" in result["clarify_prompt"]


@pytest.mark.asyncio
async def test_reextract_keeps_answered_questions_cleared(monkeypatch):
    """Stale Pass-1 inventory must not put 待问 back after dialogue answered them."""
    stale_inventory = {
        "goal": "审查一篇论文",
        "stages": [
            {
                "stage_id": "review_paper",
                "label": "审查论文",
                "goal": "对指定论文做方法与写作审查并给出书面意见",
                "known_inputs": ["用户要做学术审查"],
                "open_questions": ["论文地址或 PDF 在哪？"],
            }
        ],
    }
    graph_raw = {
        "goal": "审查一篇论文",
        "nodes": [
            {
                "node_id": "review_paper",
                "kind": "implement",
                "label": "审查论文",
                "goal": "对指定论文做方法与写作审查并给出书面意见",
                "plan_brief": {
                    "summary": "对 arXiv 论文做方法与写作审查并给出书面意见",
                    "known_inputs": [
                        "用户要做学术审查",
                        "论文地址 https://arxiv.org/abs/1234.5678",
                    ],
                    "open_questions": [],
                },
            }
        ],
        "edges": [],
    }

    async def fake_collect(client, *, system, user_text):
        return json.dumps(graph_raw, ensure_ascii=False)

    import mewcode.orchestration.extract as extract_mod

    monkeypatch.setattr(extract_mod, "collect_model_text", fake_collect)
    messages = [
        Message(role="user", content="帮我审一篇论文"),
        Message(role="assistant", content="待问：论文地址或 PDF 在哪？"),
        Message(role="user", content="论文在 https://arxiv.org/abs/1234.5678"),
    ]
    result = await extract_orchestration_from_dialogue(
        object(), messages, inventory=stale_inventory
    )
    nodes = {item["node_id"]: item for item in result["spec"]["nodes"]}
    brief = nodes["review_paper"]["plan_brief"]
    assert brief["open_questions"] == []
    assert any("arxiv.org" in item for item in brief["known_inputs"])
    assert result["needs_clarify"] is False
    assert result["inventory"]["stages"][0]["open_questions"] == []
    assert any(
        "arxiv.org" in item
        for item in result["inventory"]["stages"][0]["known_inputs"]
    )


def test_normalize_inventory_and_clarify_prompt():
    inventory = normalize_inventory({
        "goal": "审论文",
        "stages": [
            {
                "stage_id": "review",
                "label": "审查",
                "goal": "给出审稿意见",
                "known_inputs": ["中文论文"],
                "open_questions": ["论文 URL？"],
            }
        ],
    })
    spec = OrchestrationSpec.from_value({
        "spec_id": "t",
        "label": "t",
        "goal": inventory["goal"],
        "nodes": [
            {
                "node_id": "review",
                "kind": "implement",
                "label": "审查",
                "goal": "给出审稿意见",
                "plan_brief": {
                    "summary": "给出审稿意见",
                    "known_inputs": ["中文论文"],
                    "open_questions": ["论文 URL？"],
                },
            }
        ],
        "edges": [],
    })
    questions = collect_open_questions(spec)
    assert questions[0]["question"] == "论文 URL？"
    prompt = build_clarify_prompt(spec)
    assert "论文 URL" in prompt
    empty = OrchestrationSpec.from_value({
        "spec_id": "t2",
        "label": "t2",
        "nodes": [{"node_id": "a", "kind": "plan", "label": "规划", "goal": "已齐"}],
        "edges": [],
    })
    assert build_clarify_prompt(empty) == ""


def test_plan_brief_survives_pipeline_bridge_and_bindings():
    spec = OrchestrationSpec.from_value({
        "spec_id": "brief",
        "label": "brief",
        "goal": "审论文",
        "nodes": [
            {
                "node_id": "review",
                "kind": "implement",
                "label": "审查",
                "goal": "审稿",
                "plan_brief": {
                    "summary": "审稿",
                    "known_inputs": ["用户要审查"],
                    "open_questions": ["论文地址？"],
                },
            }
        ],
        "edges": [],
    })
    graph = to_pipeline_graph(spec, graph_id="g_brief")
    back = from_pipeline_graph(graph)
    brief = back.node_map()["review"].plan_brief
    assert brief is not None
    assert brief.open_questions == ("论文地址？",)
    filled = apply_profile_bindings(
        spec,
        executor={"provider_key": "p", "model_id": "m"},
    )
    assert filled.node_map()["review"].plan_brief.open_questions == ("论文地址？",)
    flow = to_expert_flow(filled)
    review = next(node for node in flow.nodes if node.node_id == "review")
    assert "论文地址" in review.responsibility
    assert "决策指南（执行期必须遵守）" not in review.responsibility
    assert "立刻停问" not in review.responsibility  # Shared rules live in runtime, not every stored plan.


def test_extract_prompts_forbid_default_stop_and_ask():
    from mewcode.orchestration.extract import (
        EXTRACT_SYSTEM,
        INVENTORY_SYSTEM,
        REFILL_SYSTEM,
    )

    for text in (INVENTORY_SYSTEM, EXTRACT_SYSTEM, REFILL_SYSTEM):
        assert "立刻停问" in text
        assert "先" in text and ("自助" in text or "自助尝试" in text)


def test_gate_stage_skips_decision_guidelines():
    spec = OrchestrationSpec.from_value({
        "spec_id": "g",
        "label": "g",
        "goal": "x",
        "nodes": [
            {
                "node_id": "confirm",
                "kind": "gate",
                "label": "确认",
                "goal": "确认开跑",
            }
        ],
        "edges": [],
    })
    flow = to_expert_flow(spec)
    gate = next(node for node in flow.nodes if node.node_id == "confirm")
    assert gate.responsibility.startswith("[GATE]")
    assert "先自助再问" not in gate.responsibility


def test_plan_brief_dataclass_roundtrip():
    brief = PlanBrief.from_value({
        "summary": "审查论文",
        "known_inputs": ["学术审查"],
        "open_questions": ["论文地址在哪？"],
    })
    assert brief.needs_clarify
    node = OrchestrationSpec.from_value({
        "spec_id": "x",
        "label": "x",
        "nodes": [{
            "node_id": "n",
            "kind": "plan",
            "label": "规划",
            "goal": "审查论文",
            "plan_brief": brief.as_dict(),
        }],
        "edges": [],
    }).nodes[0]
    assert node.plan_brief.open_questions[0] == "论文地址在哪？"


def _spec_with_two_implement_stages(
    *,
    code_id: str,
    docs_id: str,
    with_models: bool,
) -> OrchestrationSpec:
    def model_for(name: str) -> dict[str, str] | None:
        return {"provider_key": "p", "model_id": name} if with_models else None

    return OrchestrationSpec.from_value({
        "spec_id": "two_impl",
        "label": "两个实现阶段",
        "goal": "写代码并写文档",
        "nodes": [
            {
                "node_id": code_id,
                "kind": "implement",
                "label": "写代码",
                "goal": "实现功能",
                "model": model_for("m-code"),
                "position": {"x": 120, "y": 240},
            },
            {
                "node_id": docs_id,
                "kind": "implement",
                "label": "写文档",
                "goal": "补文档",
                "model": model_for("m-docs"),
                "position": {"x": 480, "y": 240},
            },
        ],
        "edges": [{"from": code_id, "to": docs_id}],
    })


def test_merge_node_models_matches_by_label_when_node_id_changes():
    """Renamed node_ids must not collapse both implement stages onto one model."""
    source = _spec_with_two_implement_stages(
        code_id="write_code", docs_id="write_docs", with_models=True
    )
    target = _spec_with_two_implement_stages(
        code_id="impl_1", docs_id="impl_2", with_models=False
    )
    merged = merge_node_models(target, source).node_map()
    assert merged["impl_1"].model.model_id == "m-code"
    assert merged["impl_2"].model.model_id == "m-docs"


@pytest.mark.asyncio
async def test_reextract_carries_over_models_layout_and_groups(monkeypatch):
    """Regenerating structure keeps bindings, positions, groups and canvas identity."""
    previous = _spec_with_two_implement_stages(
        code_id="write_code", docs_id="write_docs", with_models=True
    )
    previous = OrchestrationSpec.from_value({
        **previous.as_dict(),
        "meta": {
            "pipeline_graph_id": "g_keep_me",
            "session_id": "sess_keep",
            "canvas_groups": {
                "write_code": {
                    "expanded": True,
                    "children": ["write_docs"],
                    "label": "写代码",
                },
                "dropped_stage": {"expanded": True, "children": [], "label": "已删除"},
            },
        },
    })
    # Model re-emits the same stages without positions, models or canvas meta.
    graph_raw = {
        "goal": "写代码并写文档",
        "nodes": [
            {
                "node_id": "write_code",
                "kind": "implement",
                "label": "写代码",
                "goal": "实现功能",
            },
            {
                "node_id": "write_docs",
                "kind": "implement",
                "label": "写文档",
                "goal": "补文档",
            },
        ],
        "edges": [{"from": "write_code", "to": "write_docs"}],
    }

    async def fake_collect(client, *, system, user_text):
        return json.dumps(graph_raw, ensure_ascii=False)

    import mewcode.orchestration.extract as extract_mod

    monkeypatch.setattr(extract_mod, "collect_model_text", fake_collect)
    result = await extract_orchestration_from_dialogue(
        object(),
        [Message(role="user", content="写代码再写文档")],
        previous_spec=previous.as_dict(),
    )
    spec = OrchestrationSpec.from_value(result["spec"])
    nodes = spec.node_map()
    assert nodes["write_code"].model.model_id == "m-code"
    assert nodes["write_docs"].model.model_id == "m-docs"
    assert nodes["write_code"].position == {"x": 120.0, "y": 240.0}
    assert nodes["write_docs"].position == {"x": 480.0, "y": 240.0}
    assert spec.spec_id == "two_impl"
    assert spec.meta["pipeline_graph_id"] == "g_keep_me"
    # Groups survive, but stages that no longer exist are dropped.
    assert "write_code" in spec.meta["canvas_groups"]
    assert "dropped_stage" not in spec.meta["canvas_groups"]


@pytest.mark.asyncio
async def test_refill_plan_briefs_keeps_structure_and_bindings(monkeypatch):
    """Refilling briefs updates Plan text only — never stages, edges or models."""
    current = _spec_with_two_implement_stages(
        code_id="write_code", docs_id="write_docs", with_models=True
    )
    briefs_raw = {
        "stages": [
            {
                "stage_id": "write_code",
                "goal": "按已确认的接口实现功能",
                "known_inputs": ["接口已定稿"],
                "open_questions": [],
            },
            {
                "stage_id": "write_docs",
                "goal": "补齐使用文档",
                "known_inputs": [],
                "open_questions": ["文档发布到哪里？"],
            },
        ]
    }
    async def fake_collect(client, *, system, user_text):
        return json.dumps(briefs_raw, ensure_ascii=False)

    import mewcode.orchestration.extract as extract_mod

    monkeypatch.setattr(extract_mod, "collect_model_text", fake_collect)
    result = await refill_plan_briefs_from_dialogue(
        object(),
        [Message(role="user", content="接口已经定稿了")],
        spec=current,
    )
    spec = OrchestrationSpec.from_value(result["spec"])
    nodes = spec.node_map()
    assert set(nodes) == {"write_code", "write_docs"}
    assert len(spec.edges) == 1
    assert nodes["write_code"].model.model_id == "m-code"
    assert nodes["write_docs"].model.model_id == "m-docs"
    assert nodes["write_code"].position == {"x": 120.0, "y": 240.0}
    assert nodes["write_code"].plan_brief.known_inputs == ("接口已定稿",)
    assert nodes["write_code"].plan_brief.open_questions == ()
    assert nodes["write_docs"].plan_brief.open_questions == ("文档发布到哪里？",)
    assert result["needs_clarify"] is True
    assert "文档发布到哪里" in result["clarify_prompt"]


def test_carry_over_remaps_canvas_groups_when_node_id_changes():
    """Refine groups are keyed by node_id; renamed stages must keep their groups."""
    previous = _spec_with_two_implement_stages(
        code_id="write_code", docs_id="write_docs", with_models=True
    )
    previous = OrchestrationSpec.from_value({
        **previous.as_dict(),
        "meta": {
            "pipeline_graph_id": "g_keep_me",
            "canvas_groups": {
                "write_code": {
                    "expanded": True,
                    "children": ["write_docs"],
                    "label": "写代码",
                },
            },
        },
    })
    renamed = _spec_with_two_implement_stages(
        code_id="impl_1", docs_id="impl_2", with_models=False
    )
    carried = carry_over_user_edits(renamed, previous)
    nodes = carried.node_map()
    assert nodes["impl_1"].position == {"x": 120.0, "y": 240.0}
    assert nodes["impl_2"].position == {"x": 480.0, "y": 240.0}
    groups = carried.meta["canvas_groups"]
    assert "impl_1" in groups
    assert "write_code" not in groups
    assert groups["impl_1"]["children"] == ["impl_2"]
    assert carried.meta["pipeline_graph_id"] == "g_keep_me"


def test_sync_keeps_canvas_only_implement_nodes():
    """Reopening with a stale spec must not delete stages added on the canvas."""
    spec = OrchestrationSpec.from_value(_bind_all_nodes(get_template("linear", goal="修订实验").as_dict()))
    graph = to_pipeline_graph(spec, graph_id="g_keep_extra")
    extra = {
        "id": "revise_1",
        "type": "task",
        "title": "[implement] 修订实验",
        "label": "修订实验",
        "goal": "按审计意见改实验",
        "position": {"x": 420.0, "y": 280.0},
    }
    nodes = list(graph.nodes)
    end_index = next(i for i, node in enumerate(nodes) if node.type.value == "end")
    from mewcode.pipeline.models import GraphNode

    nodes.insert(end_index, GraphNode.model_validate(extra))
    payload = json.loads(graph.variables["__orch.payload__"])
    payload["nodes"]["revise_1"] = {"kind": "implement", "model": {}}
    graph = graph.model_copy(
        update={
            "nodes": nodes,
            "variables": {
                **graph.variables,
                "__orch.payload__": json.dumps(payload, ensure_ascii=False),
            },
        }
    )
    synced = sync_orchestration_graph_from_spec(graph, spec)
    ids = {node.id for node in synced.nodes if node.type.value == "task"}
    assert "revise_1" in ids
    back = from_pipeline_graph(synced)
    assert "revise_1" in back.node_map()
    assert back.node_map()["revise_1"].kind == "implement"


def test_audit_fail_edge_is_a_normal_branch_not_failure_route():
    raw = _bind_all_nodes(get_template("linear", goal="审计分流").as_dict())
    audit_id = next(node["node_id"] for node in raw["nodes"] if node["kind"] == "audit")
    next_id = raw["nodes"][-1]["node_id"]
    raw["nodes"] = list(raw["nodes"]) + [
        {
            "node_id": "revise_exp",
            "kind": "implement",
            "label": "修订实验",
            "goal": "按审计意见修改",
            "model": {"provider_key": "p", "model_id": "m-revise"},
        }
    ]
    raw["edges"] = [
        edge
        for edge in raw["edges"]
        if not (edge.get("from_node") == audit_id or edge.get("from") == audit_id)
    ] + [
        {"from_node": audit_id, "to_node": next_id, "condition": "pass"},
        {"from_node": audit_id, "to_node": "revise_exp", "condition": "fail"},
        {"from_node": "revise_exp", "to_node": next_id, "condition": "always"},
    ]
    spec = OrchestrationSpec.from_value(raw)
    validate_spec(spec, require_bindings=False)
    flow = to_expert_flow(spec)
    auditor = next(node for node in flow.nodes if node.role == "auditor")
    assert auditor.failure_policy.action != "route"
    pairs = {(edge.from_node, edge.to_node, edge.condition) for edge in flow.edges}
    assert (audit_id, next_id, "pass") in pairs
    assert (audit_id, "revise_exp", "fail") in pairs
    scheduled = {node.node_id for node in flow.nodes}
    assert "revise_exp" in scheduled


def test_repair_keeps_forward_audit_supplement_edge():
    raw = {
        "goal": "审计后补充修订",
        "nodes": [
            {"node_id": "plan", "kind": "plan", "label": "规划"},
            {"node_id": "implement", "kind": "implement", "label": "实现"},
            {"node_id": "audit", "kind": "audit", "label": "审核"},
            {"node_id": "revise", "kind": "implement", "label": "修订实验"},
            {"node_id": "pack", "kind": "reduce", "label": "收束"},
        ],
        "edges": [
            {"from": "plan", "to": "implement"},
            {"from": "implement", "to": "audit"},
            {"from": "audit", "to": "pack", "condition": "pass"},
            {"from": "audit", "to": "revise", "condition": "fail"},
            {"from": "revise", "to": "pack"},
        ],
    }
    repaired, warnings = repair_extracted_graph_cycles(raw)
    pairs = {(edge.get("from"), edge.get("to")) for edge in repaired["edges"]}
    assert ("audit", "revise") in pairs
    assert ("audit", "implement") not in pairs
    assert not any("audit_target=revise" in item for item in warnings)
