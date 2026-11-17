import pytest

from mewcode.expert_group import ExpertGroupOrchestrator, ExpertRoleResult
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.tools.submit_handoff import HandoffHolder, SubmitHandoff, SubmitHandoffParams, fallback_handoff
from tests.test_expert_flow import _profile


@pytest.mark.asyncio
async def test_handoff_tool_and_fallback_never_claim_acceptance():
    holder = HandoffHolder()
    result = await SubmitHandoff(holder).execute(SubmitHandoffParams(summary="prepared",
        artifacts=[{"path": "plan.json"}], open_items=["audit pending"]))
    assert not result.is_error and holder.read()["open_items"] == ["audit pending"]
    fallback = fallback_handoff("output", tools={"a": {"name": "WriteFile", "status": "completed", "args": {"file_path": "a.txt"}},
        "b": {"name": "EditFile", "status": "failed", "args": {"file_path": "b.txt"}}})
    assert [a["path"] for a in fallback["artifacts"]] == ["a.txt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("before,after,repairs", [(["A", "B", "C"], ["C", "D"], 2),
                                                (["A", "B"], ["B", "C", "D"], 1)])
async def test_repair_keeps_plan_and_last_handoff_and_requires_net_progress(tmp_path, before, after, repairs):
    flow = ExpertFlowSpec.from_value({"flow_id": "handoff", "label": "handoff", "finalizer_node_id": "done",
        "nodes": [{"node_id": "plan", "role": "planner", "label": "plan"},
                  {"node_id": "work", "role": "executor", "label": "work"},
                  {"node_id": "audit", "role": "auditor", "label": "audit", "failure_policy": {
                      "action": "route", "target_node_id": "work", "max_attempts": 3}},
                  {"node_id": "done", "role": "manager", "label": "done"}],
        "edges": [{"from": "plan", "to": "work"}, {"from": "work", "to": "audit"},
                  {"from": "audit", "to": "done"}]})
    calls, prompts = [], {}

    async def runner(node, model, prompt, policy, member=None):
        calls.append(node.node_id)
        prompts[node.node_id] = prompt
        if node.node_id == "plan":
            return ExpertRoleResult("short plan", handoff={"summary": "PLANNING MEMORY", "artifacts": [{"path": "plans/details.json"}], "open_items": ["check contracts"]})
        if node.node_id == "work":
            assert "plans/details.json" in prompt
            if calls.count("work") > 1:
                assert not policy.resume_messages and "WORK MEMORY" in prompt
            return ExpertRoleResult("short work", handoff={"summary": "WORK MEMORY", "open_items": []})
        if node.node_id == "audit":
            count = calls.count("audit")
            ids = before if count == 1 else after if count == 2 else []
            return ExpertRoleResult("audit", verdict={"verdict": "NEEDS_ATTENTION" if ids else "PASS",
                "issues": [{"issue_id": item, "description": item} for item in ids]})
        assert "ALL STAGE HANDOFFS" in prompt and "PLANNING MEMORY" in prompt
        return ExpertRoleResult("done")

    result = await ExpertGroupOrchestrator(runner=lambda *_: None, node_runner=runner, archive_root=tmp_path).run(
        "task", _profile(), {}, strategy="stable", flow=flow)
    assert calls.count("work") == repairs + 1
    assert (result.status == "completed") == (repairs == 2)
