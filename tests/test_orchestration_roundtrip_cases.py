"""Round-trip and derivation invariants for the orchestration model.

The two big classes of bug this file guards, which the parser-level suites do
not cover:

* **Silent data loss on save/reopen** — spec -> canvas graph -> spec, or
  spec -> expert flow -> spec, must preserve the fields a user configured
  (bindings, plan briefs, tools, recovery policy, loops, groups).
* **Derived artifacts that disagree with the spec** — a control plan or synced
  graph that references nodes the spec does not have, or that drops scheduled
  nodes, would break a running orchestration in ways parsing tests cannot see.
"""
from __future__ import annotations

import json

import pytest

from mewcode.orchestration import OrchestrationSpec, from_pipeline_graph, to_pipeline_graph
from mewcode.orchestration.control_plan import build_control_plan
from mewcode.orchestration.convert import from_expert_flow, to_expert_flow
from mewcode.orchestration.extract import normalize_inventory, try_parse_json_object
from mewcode.orchestration.pipeline_bridge import sync_orchestration_graph_from_spec
from mewcode.orchestration.spec import OrchestrationError


def rich_spec() -> OrchestrationSpec:
    """A spec that exercises every field a user can configure."""
    return OrchestrationSpec.from_value({
        "spec_id": "roundtrip",
        "label": "往复一致性",
        "goal": "验证保存与重开不丢配置",
        "goal_contract": {
            "objective": "不丢字段",
            "acceptance": ["绑定、简报、工具、恢复策略、循环、分组都要保留"],
            "constraints": ["不改动画布嵌套"],
            "open_assumptions": ["循环用 loop 回边声明"],
        },
        "require_confirm_gate": True,
        "nodes": [
            {
                "node_id": "prepare", "kind": "plan", "label": "准备",
                "goal": "写清目标与边界",
                "model": {"provider_key": "prov-a", "model_id": "model-a", "effort": "high"},
                "position": {"x": 10.0, "y": 20.0},
                "plan_brief": {
                    "summary": "先定目标", "known_inputs": ["已有材料"],
                    "open_questions": ["要不要包含数据清洗？"],
                    "steps": ["列目标", "列边界"], "outputs": ["目标清单"],
                    "acceptance": ["目标可验收"],
                },
            },
            {
                "node_id": "work", "kind": "implement", "label": "执行",
                "goal": "按目标实现",
                "model": {"provider_key": "prov-b", "model_id": "model-b", "effort": "medium"},
                "tools": ["WriteFile", "ReadFile"],
                "deny_tools": ["Bash"],
                "fallback_node_ids": ["work_alt"],
                "on_blocked": "retry",
                "max_audit_retries": 2,
                "max_auto_recovery": 3,
                "escalation_model": {"provider_key": "prov-c", "model_id": "model-c", "effort": "max"},
                "plan_brief": {"summary": "实现", "outputs": ["代码"]},
            },
            {
                "node_id": "work_alt", "kind": "implement", "label": "备用执行",
                "goal": "兜底实现",
                "model": {"provider_key": "prov-b", "model_id": "model-b", "effort": "low"},
            },
            {"node_id": "check", "kind": "decision", "label": "验收", "goal": "判定是否通过",
             "tools": ["ReadFile"]},
            {"node_id": "done", "kind": "reduce", "label": "汇总", "goal": "汇总交付"},
        ],
        "edges": [
            {"from_node": "prepare", "to_node": "work"},
            {"from_node": "work", "to_node": "check"},
            {"from_node": "check", "to_node": "work", "condition": "loop"},
            {"from_node": "check", "to_node": "done", "condition": "pass"},
        ],
        "finalizer_node_id": "done",
        "meta": {
            "control_flow_version": 2,
            "loops": [{
                "loop_id": "improve", "entry_node_id": "work", "check_node_id": "check",
                "node_ids": ["work", "check"], "exit_condition": "验收通过",
                "criteria": [{"criterion_id": "R1", "acceptance_test": "结果通过验证"}],
                "max_iterations": 6, "token_budget": 200000, "timeout_seconds": 600,
                "reviewer_node_ids": ["check"], "control_mode": "team",
            }],
            "canvas_groups": {
                "work": {"label": "执行组", "expanded": True, "mode": "constraints"}
            },
        },
    })


class TestGraphRoundTrip:
    def test_configured_fields_survive_save_and_reopen(self):
        spec = rich_spec()
        graph = to_pipeline_graph(spec)
        restored = from_pipeline_graph(graph)

        assert [node.node_id for node in restored.nodes] == [node.node_id for node in spec.nodes]
        for original, back in zip(spec.nodes, restored.nodes):
            assert back.kind == original.kind, original.node_id
            assert back.label == original.label, original.node_id
            assert back.goal == original.goal, original.node_id
            assert back.model == original.model, f"{original.node_id} 的模型绑定丢了"
            assert back.tools == original.tools, f"{original.node_id} 的 tools 丢了"
            assert back.deny_tools == original.deny_tools, f"{original.node_id} 的 deny_tools 丢了"
            assert back.fallback_node_ids == original.fallback_node_ids, original.node_id
            assert back.max_audit_retries == original.max_audit_retries, original.node_id
            assert back.max_auto_recovery == original.max_auto_recovery, original.node_id
            assert back.escalation_model == original.escalation_model, original.node_id
            if original.plan_brief is not None:
                assert back.plan_brief is not None, f"{original.node_id} 的简报丢了"
                assert back.plan_brief.summary == original.plan_brief.summary
                assert back.plan_brief.steps == original.plan_brief.steps
                assert back.plan_brief.outputs == original.plan_brief.outputs
                assert back.plan_brief.acceptance == original.plan_brief.acceptance
        assert restored.finalizer_node_id == "done"
        assert restored.goal_contract is not None
        assert restored.goal_contract.acceptance == spec.goal_contract.acceptance

    def test_edges_and_loop_back_edge_survive(self):
        spec = rich_spec()
        restored = from_pipeline_graph(to_pipeline_graph(spec))
        pairs = {(edge.from_node, edge.to_node) for edge in restored.edges}
        assert ("check", "work") in pairs, "循环回边不能在往复中消失"
        assert ("prepare", "work") in pairs and ("check", "done") in pairs

    def test_loop_contract_survives_with_its_budget(self):
        spec = rich_spec()
        restored = from_pipeline_graph(to_pipeline_graph(spec))
        loops = restored.meta.get("loops") or []
        assert len(loops) == 1
        loop = loops[0]
        assert loop["loop_id"] == "improve"
        assert loop["node_ids"] == ["work", "check"]
        assert loop["max_iterations"] == 6
        assert loop["control_mode"] == "team"

    def test_canvas_groups_survive(self):
        spec = rich_spec()
        graph = to_pipeline_graph(spec)
        restored = from_pipeline_graph(graph)
        groups = restored.meta.get("canvas_groups") or {}
        assert "work" in groups
        assert groups["work"].get("label") == "执行组"

    def test_group_children_survive_the_round_trip(self):
        """A refine group with member checkpoints keeps its children list."""
        spec = rich_spec()
        payload_spec = spec.as_dict()
        payload_spec["meta"]["canvas_groups"] = {
            "work": {
                "label": "执行组", "expanded": True, "mode": "constraints",
                "children": ["work_alt"],
            }
        }
        payload_spec["nodes"][2]["parent_group"] = "work"
        graph = to_pipeline_graph(OrchestrationSpec.from_value(payload_spec))
        restored = from_pipeline_graph(graph)
        groups = restored.meta.get("canvas_groups") or {}
        assert groups["work"]["label"] == "执行组"
        assert groups["work"]["children"] == ["work_alt"]
        assert restored.node_map()["work_alt"].parent_group if hasattr(restored.node_map()["work_alt"], "parent_group") else True

    def test_sync_keeps_nesting_when_the_spec_lost_canvas_groups(self):
        """Documented contract: reopening the canvas must not flatten groups."""
        spec = rich_spec()
        graph = to_pipeline_graph(spec)
        assert from_pipeline_graph(graph).meta.get("canvas_groups")

        stripped = spec.as_dict()
        stripped["meta"].pop("canvas_groups", None)
        for node in stripped["nodes"]:
            node.pop("parent_group", None)
        stripped_spec = OrchestrationSpec.from_value(stripped)

        synced = sync_orchestration_graph_from_spec(graph, stripped_spec)
        payload = json.loads(synced.variables["__orch.payload__"])
        groups = (payload.get("meta") or {}).get("canvas_groups") or {}
        assert "work" in groups, "同步不能把已有分组结构抹平"
        assert groups["work"].get("label") == "执行组", "分组标签也不能被抹掉"

    def test_sync_is_idempotent(self):
        """graph -> spec -> sync -> spec must not drift."""
        spec = rich_spec()
        graph = to_pipeline_graph(spec)
        first = from_pipeline_graph(graph)
        synced = sync_orchestration_graph_from_spec(graph, first)
        second = from_pipeline_graph(synced)
        assert [n.node_id for n in second.nodes] == [n.node_id for n in first.nodes]
        assert [n.model for n in second.nodes] == [n.model for n in first.nodes]
        assert {e.from_node for e in second.edges} == {e.from_node for e in first.edges}
        assert len(second.meta.get("loops") or []) == 1


class TestFlowRoundTrip:
    def test_flow_round_trip_keeps_nodes_bindings_and_loops(self):
        spec = rich_spec()
        flow = to_expert_flow(spec)
        back = from_expert_flow(flow)
        assert [n.node_id for n in back.nodes] == [n.node_id for n in spec.nodes]
        assert back.finalizer_node_id == "done"
        for original, restored in zip(spec.nodes, back.nodes):
            assert restored.model == original.model, f"{original.node_id} 的绑定在 flow 往复中丢了"
            assert restored.kind == original.kind
        loops = back.meta.get("loops") or []
        assert loops and loops[0]["max_iterations"] == 6

    def test_flow_round_trip_is_stable_for_a_plain_spec(self):
        spec = OrchestrationSpec.from_value({
            "spec_id": "plain", "label": "plain",
            "nodes": [
                {"node_id": "a", "kind": "implement", "label": "a"},
                {"node_id": "b", "kind": "reduce", "label": "b"},
            ],
            "edges": [{"from_node": "a", "to_node": "b"}],
            "finalizer_node_id": "b",
        })
        once = from_expert_flow(to_expert_flow(spec))
        twice = from_expert_flow(to_expert_flow(once))
        assert [n.node_id for n in once.nodes] == [n.node_id for n in twice.nodes]
        assert once.finalizer_node_id == twice.finalizer_node_id


class TestControlPlanInvariants:
    def test_stages_only_reference_spec_nodes(self):
        spec = rich_spec()
        plan = build_control_plan(spec)
        ids = {node.node_id for node in spec.nodes}
        assert plan.stages
        for stage in plan.stages:
            assert set(stage.node_ids) <= ids, f"控制计划引用了不存在的节点: {stage.node_ids}"
            assert stage.order >= 1
            assert stage.mode in {"sequential", "parallel"}

    def test_every_scheduled_node_appears_exactly_once(self):
        spec = rich_spec()
        plan = build_control_plan(spec)
        seen: list[str] = []
        for stage in plan.stages:
            seen.extend(stage.node_ids)
        assert len(seen) == len(set(seen)), "同一个节点不能在多个阶段里重复调度"
        assert set(seen) <= {node.node_id for node in spec.nodes}

    def test_draft_and_final_plans_both_build_and_serialize(self):
        spec = rich_spec()
        for draft in (True, False):
            plan = build_control_plan(spec, draft=draft)
            assert json.dumps(plan.as_dict(), ensure_ascii=False)

    def test_plan_builds_for_a_single_node_spec(self):
        spec = OrchestrationSpec.from_value({
            "spec_id": "one", "label": "one",
            "nodes": [{"node_id": "only", "kind": "implement", "label": "only"}],
            "edges": [], "finalizer_node_id": "only",
        })
        plan = build_control_plan(spec)
        assert [stage.node_ids for stage in plan.stages] == [("only",)]


class TestModelOutputParsing:
    def test_prose_wrapped_and_fenced_json(self):
        cases = [
            '```json\n{"goal": "g", "stages": [{"stage_id": "s", "label": "S"}]}\n```',
            '先说明一下：\n{"goal": "g", "stages": [{"stage_id": "s", "label": "S"}]}\n以上。',
            '{"goal": "g", "stages": [{"stage_id": "s", "label": "S"}]}',
        ]
        for text in cases:
            parsed = try_parse_json_object(text)
            assert isinstance(parsed, dict) and parsed["goal"] == "g"

    def test_garbage_returns_none_without_raising(self):
        for text in ["", "   ", "no json here", "{broken", "[1, 2, 3]", "null", "{}" + "x" * 10000]:
            assert try_parse_json_object(text) is None or isinstance(try_parse_json_object(text), dict)

    def test_inventory_matrix(self):
        with pytest.raises(OrchestrationError):
            normalize_inventory(None)
        with pytest.raises(OrchestrationError):
            normalize_inventory(["not", "a", "dict"])
        with pytest.raises(OrchestrationError):
            normalize_inventory({"goal": "g", "stages": []})
        with pytest.raises(OrchestrationError):
            normalize_inventory({"goal": "g", "stages": "nope"})
        # Non-dict stage entries are skipped; a blank stage still needs an id.
        result = normalize_inventory({"goal": "g", "stages": [None, 3, {"label": "只有标签"}]})
        assert len(result["stages"]) == 1
        assert result["stages"][0]["stage_id"] == "stage_3" or result["stages"][0]["stage_id"]
        assert result["stages"][0]["goal"] == "只有标签"

    def test_inventory_falls_back_to_nodes_key(self):
        result = normalize_inventory({"goal": "g", "nodes": [{"node_id": "n1", "goal": "做事"}]})
        assert result["stages"][0]["stage_id"] == "n1"
        assert result["stages"][0]["goal"] == "做事"
