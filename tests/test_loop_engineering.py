"""Loop authoring regression and isolated real-engine two-round acceptance demo."""
import copy
import json

import pytest

from mewcode.expert_group import ExpertGroupOrchestrator, ExpertProfile, ExpertRoleResult
from mewcode.model_registry import ModelRef
from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.extract import repair_extracted_graph_cycles
from mewcode.orchestration.loops import LOOP_BUDGET_LIMITS, LoopContract
from mewcode.orchestration.pipeline_bridge import to_pipeline_graph, from_pipeline_graph
from mewcode.orchestration.spec import OrchestrationSpec, OrchestrationError
from mewcode.orchestration.validate import validate_spec
from tests.test_generic_loops import spec_for, report


@pytest.mark.parametrize("field", list(LOOP_BUDGET_LIMITS))
@pytest.mark.parametrize("value", [None, "", 0, True, float("inf"), "unlimited"])
def test_budget_error_identifies_loop_field_value_and_range(field, value):
    raw = {**spec_for().meta["loops"][0], field: value}
    with pytest.raises(ValueError) as error:
        LoopContract.from_value(raw)
    assert "improve" in str(error.value) and field in str(error.value) and "允许" in str(error.value)


def test_budget_boundaries_and_all_errors_reported():
    raw = spec_for().meta["loops"][0]
    for key, rule in LOOP_BUDGET_LIMITS.items():
        for boundary in ("min", "max"):
            assert getattr(LoopContract.from_value({**raw, key: rule[boundary]}), key) == rule[boundary]
    with pytest.raises(ValueError, match="token_budget"):
        LoopContract.from_value({**raw, "token_budget": 1000})
    with pytest.raises(ValueError) as error:
        LoopContract.from_value({**raw, "max_iterations": 2.5, "timeout_seconds": 90000})
    assert "max_iterations" in str(error.value) and "timeout_seconds" in str(error.value)


@pytest.mark.parametrize("condition", ["fail", "always", "", "false"])
def test_declared_return_is_normalized_without_deleting_or_mutating_edges(condition):
    raw = spec_for().as_dict()
    raw["edges"][2]["condition"] = condition
    original = copy.deepcopy(raw)
    repaired, warnings = repair_extracted_graph_cycles(raw)
    assert raw == original and warnings
    assert len(repaired["edges"]) == len(raw["edges"])
    assert repaired["edges"][2]["condition"] == "loop"
    assert repaired["edges"][3]["condition"] == "pass"
    validate_spec(OrchestrationSpec.from_value(repaired))


def test_undeclared_cycle_names_edges_and_preserves_draft():
    raw = spec_for().as_dict()
    raw["meta"] = {"control_flow_version": 2}
    raw["edges"][2]["condition"] = "fail"
    original = copy.deepcopy(raw)
    with pytest.raises(OrchestrationError) as error:
        repair_extracted_graph_cycles(raw)
    assert "work → check" in str(error.value) and "check → work" in str(error.value)
    assert raw == original


def test_explicit_loop_label_is_not_misdiagnosed_as_ordinary_cycle():
    raw = spec_for().as_dict()
    raw["edges"][2].pop("condition")
    raw["edges"][2]["label"] = "循环"
    repaired, warnings = repair_extracted_graph_cycles(raw)
    assert warnings and repaired["edges"][2]["condition"] == "loop"
    validate_spec(OrchestrationSpec.from_value(repaired))


@pytest.mark.asyncio
async def test_canvas_receives_backend_budget_schema():
    from frontend.server import api_get_orchestration_kinds
    response = await api_get_orchestration_kinds()
    assert json.loads(response.body)["loop_budget_limits"] == LOOP_BUDGET_LIMITS


@pytest.mark.asyncio
async def test_two_round_loop_engineering_demo(tmp_path):
    # Start with the exact generator mismatch: a declared return labelled fail.
    raw = spec_for("循环工程验收：数据修复").as_dict()
    raw["edges"][2]["condition"] = "fail"
    labels = {"prepare": "准备验证数据", "work": "修复数据", "check": "检查验收条件", "done": "汇总通过结果"}
    for node in raw["nodes"]:
        node["label"] = labels[node["node_id"]]
    raw, warnings = repair_extracted_graph_cycles(raw)
    spec = OrchestrationSpec.from_value(raw)
    validate_spec(spec)
    graph = to_pipeline_graph(spec, graph_id="loop-engineering-demo")
    spec = from_pipeline_graph(graph)
    validate_spec(spec)
    (tmp_path / "fixture.json").write_text(graph.model_dump_json(by_alias=True), encoding="utf-8")
    artifact = tmp_path / "data.json"
    calls, events = [], []

    async def runner(node, model, prompt, policy, member=None):
        calls.append(node.node_id)
        if node.node_id == "prepare":
            artifact.write_text(json.dumps([3, 1, 1]), encoding="utf-8")
        elif node.node_id == "work":
            values = json.loads(artifact.read_text(encoding="utf-8"))
            # Round one only sorts; round two removes the remaining duplicate.
            values = sorted(values) if calls.count("work") == 1 else sorted(set(values))
            artifact.write_text(json.dumps(values), encoding="utf-8")
        elif node.node_id == "check":
            values = json.loads(artifact.read_text(encoding="utf-8"))
            passed = values == [1, 3]
            decision = report(policy.loop_context, "PASS" if passed else "PARTIAL")
            decision["checks"][0].update(evidence=f"实际数据={values}；预期=[1,3]", location=str(artifact))
            return ExpertRoleResult(text="数据验收", input_tokens=12, output_tokens=8, loop_decision=decision)
        elif node.node_id == "done":
            assert json.loads(artifact.read_text(encoding="utf-8")) == [1, 3]
        return ExpertRoleResult(text=labels[node.node_id], input_tokens=12, output_tokens=8)

    ref = ModelRef("isolated", "deterministic-test", "")
    engine = ExpertGroupOrchestrator(runner=lambda *_: None, node_runner=runner, archive_root=tmp_path,
                                    event_sink=lambda event: events.append(copy.deepcopy(event)))
    result = await engine.run("排序且去重，验收通过才退出", ExpertProfile(ref, ref, ref), {}, strategy="stable", flow=to_expert_flow(spec))
    assert result.status == "completed"
    assert calls == ["prepare", "work", "check", "work", "check", "done"]
    state = [e["loop_state"] for e in events if e.get("loop_state")][-1]
    assert state["round"] == 2 and state["tokens"] == 80 and state["status"] == "completed"
    summary = {"status": result.status, "calls": calls, "rounds": state["round"],
               "tokens": state["tokens"], "output": [1, 3], "normalization": warnings,
               "model_calls": "injected deterministic runner; no external provider"}
    (tmp_path / "loop-report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
