"""The real failure topology: findings -> decision -> repair -> final proof."""
import copy
import json
from types import SimpleNamespace

import pytest

from mewcode.expert_group import ExpertGroupOrchestrator, ExpertRoleResult
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.experts.outcome import acceptance_recheck_nodes, effective_run_status, unresolved_audits
from mewcode.experts.stage_context import collect_stage_inputs, review_snapshot, stage_inputs_prompt
from mewcode.tools.read_stage_result import ReadStageResult, ReadStageResultParams
from mewcode.tools.submit_handoff import HandoffHolder, SubmitHandoff, SubmitHandoffParams
from tests.test_expert_flow import _profile, _pass_verdict


def findings_flow():
    return ExpertFlowSpec.from_value({"flow_id": "findings", "version": 161, "label": "single review",
        "finalizer_node_id": "done", "nodes": [
            {"node_id": "plan", "role": "planner", "label": "semantics"},
            {"node_id": "style", "role": "planner", "label": "visual style"},
            {"node_id": "technical", "role": "auditor", "label": "one initial review"},
            {"node_id": "decision", "role": "auditor", "label": "can revise"},
            {"node_id": "repair", "role": "executor", "label": "revise"},
            {"node_id": "proof", "role": "auditor", "label": "final proof"},
            {"node_id": "done", "role": "manager", "label": "deliver"}],
        "edges": [{"from": "plan", "to": "technical"}, {"from": "style", "to": "technical"},
            {"from": "technical", "to": "decision", "condition": "fail"},
            {"from": "technical", "to": "repair", "condition": "pass"},
            {"from": "decision", "to": "repair", "condition": "pass"},
            {"from": "repair", "to": "proof"}, {"from": "proof", "to": "done"}],
        "meta": {"node_kinds": {"decision": "decision"}, "shared_constraints": ["旧规划以 v155 为基线"]}})


def findings():
    return {"verdict": "NEEDS_ATTENTION", "issues": [{"issue_id": "FIX-01", "description": "retain semantics",
        "acceptance_test": "algorithm matches implementation", "file": "paper.tex"}], "evidence": []}


def seed_run(tmp_path, paused="repair"):
    flow = findings_flow()
    store = ExpertRunStore(tmp_path / "runs")
    run_id = "d" * 32
    store.create(run_id=run_id, conversation_id="c", task="revise", attachments=[],
        profile=_profile().as_dict(), strategy="stable", flow=flow)
    states = {
        "plan": {"status": "completed", "text": "SEMANTICS"},
        "style": {"status": "completed", "text": "VISUAL RULES"},
        "technical": {"status": "completed", "text": "technical report", "verdict": findings(), "passed": False,
                      "messages": [{"role": "assistant", "content": "prior review"}],
                      "attempt_history": [{"summary": "fixed findings"}]},
        "decision": {"status": "completed", "text": "can revise", "verdict": _pass_verdict()},
        "repair": {"status": "paused", "text": "partial repair", "messages": [{"role": "user", "content": "retained question"}],
            "pending_interaction": {"kind": "human_oncall", "request_id": "q", "tool": "human_oncall", "questions": []},
            "attempt_history": [{"summary": "previous repair", "changed_files": ["paper.tex"]}]}}
    if paused == "decision":
        states.pop("repair")
        states["decision"].update(status="blocked", verdict=None, text="provider stalled")
    store.mutate(run_id, lambda s: s.update(status="paused", stage_cursor=paused, node_states=states))
    return store, run_id, flow


@pytest.mark.parametrize("paused", ["repair", "decision"])
def test_continue_keeps_findings_and_paused_messages(tmp_path, paused):
    store, run_id, flow = seed_run(tmp_path, paused)
    before = store.load(run_id)
    assert acceptance_recheck_nodes(flow.as_dict(), before["node_states"]) == []
    resumed = store.resume(run_id, strategy="stable")
    assert resumed["stage_cursor"] == paused
    assert resumed["node_states"] == before["node_states"]
    assert not resumed.get("acceptance_rechecks")


@pytest.mark.asyncio
@pytest.mark.parametrize("resuming", [False, True])
async def test_findings_once_then_repair_and_proof_complete(tmp_path, resuming):
    store, run_id, flow = seed_run(tmp_path)
    if resuming:
        store.mutate(run_id, lambda value: value["node_states"]["repair"].update(
            handoff={"status": "needs_attention", "summary": "current structured handoff",
                     "open_items": ["finish the requested repair"]}))
        snapshot = store.resume(run_id, strategy="stable")
    else:
        snapshot = {}
    calls, prompts, events = [], {}, []

    async def runner(node, model, prompt, policy, member=None):
        calls.append(node.node_id)
        prompts[node.node_id] = prompt
        if node.node_id == "technical":
            return ExpertRoleResult("technical report", verdict=findings())
        if node.node_id == "repair":
            assert "SEMANTICS" in prompt and "VISUAL RULES" in prompt and "FIX-01" in prompt
            assert '"flow_version": 161' in prompt and run_id in prompt
            assert "snapshot_hash" in prompt and "RUNTIME DISPATCH" in prompt
            sources = policy.stage_inputs
            assert set(sources) >= {"decision", "technical", "plan", "style"}
            assert sources["technical"]["verdict"] == findings()
            receipt = sources["technical"]["audit_snapshot"]
            assert receipt["run_id"] == run_id and len(receipt["snapshot_hash"]) == 64
            return ExpertRoleResult("modified", handoff={"status": "completed", "summary": "modified"})
        return ExpertRoleResult({"plan": "SEMANTICS", "style": "VISUAL RULES"}.get(node.node_id, "ok"),
            verdict={**_pass_verdict(), "reviewed_snapshots": policy.required_review_snapshots or {}}
            if node.role == "auditor" else None)

    result = await ExpertGroupOrchestrator(runner=runner, node_runner=runner, archive_root=tmp_path / "archive",
        event_sink=lambda event: events.append(copy.deepcopy(event))).run("revise", _profile(), {},
            strategy="stable", flow=flow, run_id=run_id, checkpoint=snapshot)
    assert result.status == "completed"
    assert calls.count("technical") == (0 if resuming else 1)
    assert calls.count("repair") == calls.count("proof") == 1
    if resuming:
        assert calls == ["repair", "proof", "done"]
        assert "current structured handoff" in prompts["repair"]
        assert "previous repair" not in prompts["repair"]
    else:
        technical = next(e for e in events if e.get("type") == "expert_node_completed" and e.get("node_id") == "technical")
        assert technical["audit_snapshot"]["verdict"] == findings()


@pytest.mark.parametrize("proof_status,verdict,expected", [("completed", "PASS", []),
    ("completed", "NEEDS_ATTENTION", ["technical", "proof"]), ("pending", None, ["technical"]),
    ("skipped", None, ["technical"]), ("blocked", None, ["technical", "proof"])])
def test_initial_findings_only_discharged_by_real_final_review(proof_status, verdict, expected):
    receipt = review_snapshot(run_id="r", flow_version=161, node_id="technical", verdict=findings())
    states = {"technical": {"status": "completed", "verdict": findings(), "passed": False, "audit_snapshot": receipt},
              "proof": {"status": proof_status, "verdict": {"verdict": verdict,
                  "reviewed_snapshots": {"technical": receipt["snapshot_hash"]}} if verdict else None}}
    assert unresolved_audits(findings_flow().as_dict(), states) == expected
    assert effective_run_status({"status": "completed", "locked_flow": findings_flow().as_dict(),
        "node_states": states}) == ("completed" if not expected else "needs_attention")
    assert states["technical"]["verdict"] == findings()  # Classification never rewrites old findings.


def test_inactive_sibling_results_not_injected():
    results = {"a": ExpertRoleResult("allowed"), "secret": ExpertRoleResult("unselected branch")}
    sources = collect_stage_inputs([{"from_node": "a"}], {}, results)
    assert list(sources) == ["a"]
    assert "unselected branch" not in stage_inputs_prompt(sources)


@pytest.mark.asyncio
async def test_large_upstream_record_is_paginated_and_frozen():
    sources = {"a": {"text": "real evidence " * 5000, "verdict": findings()}}
    reader = ReadStageResult(sources)
    sources["a"]["verdict"] = {"verdict": "PASS"}
    encoded = reader.sources["a"]
    parts = []
    offset = 0
    while offset < len(encoded):
        result = await reader.execute(ReadStageResultParams(node_id="a", offset=offset, limit=24000))
        assert len(result.output) <= 10000
        parts.append(result.output.split("\n", 5)[5])
        cursor = result.host_observation["read_cursor"]
        assert cursor["next_offset"] > offset
        offset = cursor["next_offset"]
    assert json.loads("".join(parts))["verdict"] == findings()
    denied = await reader.execute(ReadStageResultParams(node_id="other-run/a"))
    assert denied.is_error


@pytest.mark.asyncio
async def test_declared_blocker_stops_before_proof(tmp_path):
    flow = findings_flow()
    calls = []
    async def runner(node, *_args):
        calls.append(node.node_id)
        if node.node_id == "repair":
            return ExpertRoleResult("handoff saved", handoff={"status": "blocked", "blockers": ["missing input"]})
        return ExpertRoleResult("ok", verdict=findings() if node.node_id == "technical" else
                                _pass_verdict() if node.role == "auditor" else None)
    result = await ExpertGroupOrchestrator(runner=runner, node_runner=runner, archive_root=tmp_path).run(
        "revise", _profile(), {}, strategy="stable", flow=flow)
    assert result.status != "completed" and "proof" not in calls
    assert "missing input" in result.final_summary


@pytest.mark.asyncio
async def test_handoff_rejects_completed_with_blockers():
    holder = HandoffHolder()
    result = await SubmitHandoff(holder).execute(SubmitHandoffParams(summary="not done", blockers=["missing permission"]))
    assert result.is_error and holder.read() is None


@pytest.mark.parametrize("kind", ["absent", "empty", "blocked_json", "outside", "valid"])
def test_completion_checks_actual_declared_artifact(tmp_path, kind):
    from mewcode.experts.completion import handoff_blocker
    path = tmp_path / "handoff.json"
    if kind in {"empty", "blocked_json", "valid"}:
        path.write_text("" if kind == "empty" else json.dumps({"status": "BLOCKED_MODEL_AVAILABILITY_UNKNOWN"}
                        if kind == "blocked_json" else {"status": "completed"}), encoding="utf-8")
    raw = "../outside.json" if kind == "outside" else path.name
    reason = handoff_blocker({"status": "completed", "artifacts": [{"path": raw}]}, work_dir=tmp_path)
    assert bool(reason) == (kind != "valid")


def test_attempt_memory_uses_structured_handoff_without_replaying_legacy_messages_or_approval():
    from mewcode.experts.attempt_memory import checkpoint_attempt_histories
    from mewcode.experts.node_ledger import ledger_for_run

    old_state = {"status": "blocked", "text": "old dependency failure", "messages": [{"content": "do not replay"}],
        "tools": {"edit": {"name": "EditFile", "status": "completed", "args": {"file_path": "kept.tex"}}}}
    checkpoint = {"acceptance_rechecks": [{"previous": {"repair": old_state}}],
        "node_states": {"repair": {"status": "paused", "attempt_history": [{"summary": "new failure"}]}}}
    before = copy.deepcopy(checkpoint)
    entries = checkpoint_attempt_histories(checkpoint)["repair"]
    assert entries[0]["changed_files"] == ["kept.tex"]
    assert entries[0]["summary"] == ""
    assert entries[-1]["summary"] == "new failure"
    assert "old dependency failure" not in json.dumps(entries)
    assert "do not replay" not in json.dumps(entries)
    assert checkpoint == before

    current = {"node_ledgers": {}, "node_states": {"repair": {"attempt_history": [{"summary": "stale note"}]}}}
    ledger_for_run(current).record("repair", "handoff", {
        "status": "needs_attention", "summary": "current structured progress", "open_items": ["verify the edit"]},
        source="host", identity="current-handoff")
    current_entries = checkpoint_attempt_histories(current)["repair"]
    assert current_entries[0]["summary"] == "current structured progress"
    assert current_entries[0]["open_items"] == ["verify the edit"]
    assert "stale note" not in json.dumps(current_entries)


def test_review_receipt_is_detached_from_mutable_verdict():
    verdict = findings()
    receipt = review_snapshot(run_id="r", flow_version=1, node_id="a", verdict=verdict)
    verdict["issues"].clear()
    assert receipt["verdict"]["issues"]


@pytest.mark.parametrize("role", ["manager", "planner", "executor", "auditor"])
def test_stage_data_and_visual_tools_respect_explicit_capability_limits(tmp_path, role):
    from dataclasses import replace
    from mewcode.expert_group import ExpertExecutionPolicy
    from mewcode.expert_web import _role_registry
    from mewcode.tools import ToolRegistry
    session = SimpleNamespace(work_dir=str(tmp_path), agent=SimpleNamespace(registry=ToolRegistry()))
    policy = ExpertExecutionPolicy(strategy="stable", allow_delegation=False, max_direct_agents=0,
        stage_inputs={"prior": {"text": "upstream"}})
    registry = _role_registry(session, role, policy)
    assert registry.get("ReadStageResult") is not None and registry.get("ReadAuditArtifact") is not None
    assert registry.get("RequestAssistance") is not None
    limited_registry = _role_registry(session, role, replace(policy, node_tools=()))
    assert limited_registry.get("ReadStageResult") is not None
    assert limited_registry.get("RequestAssistance") is not None
    assert limited_registry.get("ReadAuditArtifact") is None
    denied_registry = _role_registry(session, role,
        replace(policy, deny_tools=("ReadStageResult", "ReadAuditArtifact")))
    assert denied_registry.get("ReadStageResult") is None
    assert denied_registry.get("ReadAuditArtifact") is None
    assert denied_registry.get("RequestAssistance") is not None


def test_retry_work_invalidates_old_proof_but_keeps_its_own_messages(tmp_path):
    store, run_id, flow = seed_run(tmp_path)
    store.mutate(run_id, lambda s: s["node_states"].update({
        "proof": {"status": "completed", "verdict": _pass_verdict()},
        "done": {"status": "completed", "text": "old final"}}))
    before = store.load(run_id)
    resumed = store.resume(run_id, strategy="stable", retry_node_id="repair")
    assert resumed["stage_cursor"] == "repair"
    for node_id in ("plan", "style", "technical", "decision", "repair"):
        assert resumed["node_states"][node_id] == before["node_states"][node_id]
    for node_id in ("proof", "done"):
        assert resumed["node_states"][node_id]["status"] == "pending"
        assert not resumed["node_states"][node_id].get("verdict")


@pytest.mark.asyncio
async def test_handoff_invalid_artifact_clears_previous_completion(tmp_path):
    holder = HandoffHolder()
    tool = SubmitHandoff(holder, work_dir=tmp_path)
    assert not (await tool.execute(SubmitHandoffParams(summary="ok"))).is_error
    assert holder.read()
    result = await tool.execute(SubmitHandoffParams(summary="claim", artifacts=[{"path": "missing.pdf"}]))
    assert result.is_error and holder.read() is None


def test_blocked_data_file_is_not_mistaken_for_stage_status(tmp_path):
    from mewcode.experts.completion import handoff_blocker
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps({"status": "BLOCKED"}), encoding="utf-8")
    assert not handoff_blocker({"status": "completed", "artifacts": [{"path": path.name}]}, work_dir=tmp_path)


@pytest.mark.parametrize("reference", [None, "wrong-hash"])
def test_unrelated_or_stale_pass_cannot_discharge_findings(reference):
    receipt = review_snapshot(run_id="r", flow_version=161, node_id="technical", verdict=findings())
    states = {"technical": {"status": "completed", "verdict": findings(), "audit_snapshot": receipt},
        "proof": {"status": "completed", "verdict": {"verdict": "PASS", "reviewed_snapshots": {"technical": reference}}}}
    assert unresolved_audits(findings_flow().as_dict(), states) == ["technical"]


@pytest.mark.asyncio
async def test_final_verdict_requires_exact_review_receipt():
    from mewcode.tools.submit_verdict import SubmitVerdictTool, SubmitVerdictParams, VerdictHolder
    holder = VerdictHolder()
    tool = SubmitVerdictTool(holder, required_reviews={"technical": "frozen"})
    assert (await tool.execute(SubmitVerdictParams(verdict="PASS"))).is_error
    assert holder.read() is None
    assert (await tool.execute(SubmitVerdictParams(verdict="PASS", reviewed_snapshots={"technical": "stale"}))).is_error
    assert not (await tool.execute(SubmitVerdictParams(verdict="PASS", reviewed_snapshots={"technical": "frozen"}))).is_error
    assert holder.read()["reviewed_snapshots"] == {"technical": "frozen"}


def test_discharged_findings_projection_and_cache_preserve_original_verdict(tmp_path):
    store, run_id, flow = seed_run(tmp_path)
    receipt = review_snapshot(run_id=run_id, flow_version=161, node_id="technical", verdict=findings())
    def finish(snapshot):
        snapshot.update(status="completed", flow_state={"phase": "done", "status": "completed"})
        states = snapshot["node_states"]
        states["technical"]["audit_snapshot"] = receipt
        states["repair"]["status"] = "completed"
        states["proof"] = {"status": "completed", "verdict": {**_pass_verdict(),
            "reviewed_snapshots": {"technical": receipt["snapshot_hash"]}}}
        states["done"] = {"status": "completed"}
    store.mutate(run_id, finish)
    before = store.load(run_id)
    for public in (store.public(run_id), store.public(run_id, role="technical"), store.public_for_conversation("c")[0]):
        assert public["status"] == "completed"
        assert public["discharged_audit_node_ids"] == ["technical"]
        assert public["node_states"]["technical"]["findings_discharged"]
        assert public["node_states"]["technical"]["verdict"] == findings()
        assert not public["resume_requires_retry"]
    assert store.load(run_id) == before


def test_completed_failed_audit_without_a_repair_branch_requires_explicit_retry(tmp_path):
    from tests.test_acceptance_completion import branch_flow
    flow = branch_flow().as_dict()
    flow["edges"] = [edge for edge in flow["edges"] if edge.get("condition") != "fail"]
    store = ExpertRunStore(tmp_path / "runs")
    run_id = "e" * 32
    store.create(run_id=run_id, conversation_id="c", task="deliver", attachments=[],
        profile=_profile().as_dict(), strategy="stable", flow=flow)
    store.mutate(run_id, lambda s: s.update(status="blocked", stage_cursor="audit", node_states={
        "work": {"status": "completed"}, "audit": {"status": "completed", "verdict": findings(), "passed": False}}))
    with pytest.raises(ValueError, match="明确重试"):
        store.resume(run_id, strategy="stable")
    assert store.public(run_id)["resume_requires_retry"]
    assert store.resume(run_id, strategy="stable", retry_node_id="audit")["stage_cursor"] == "audit"


@pytest.mark.asyncio
async def test_unselected_review_branch_does_not_block_discharging_findings(tmp_path):
    flow = findings_flow().as_dict()
    flow["nodes"].insert(-1, {"node_id": "unused_proof", "role": "auditor", "label": "unused review"})
    for edge in flow["edges"]:
        if edge["from_node"] == "repair" and edge["to_node"] == "proof":
            edge["condition"] = "pass"
    flow["edges"].extend([{"from": "repair", "to": "unused_proof", "condition": "fail"},
                          {"from": "unused_proof", "to": "done"}])
    calls, events = [], []

    async def runner(node, model, prompt, policy, member=None):
        calls.append(node.node_id)
        return ExpertRoleResult("ok", verdict=(findings() if node.node_id == "technical" else
            {**_pass_verdict(), "reviewed_snapshots": policy.required_review_snapshots or {}})
            if node.role == "auditor" else None)

    result = await ExpertGroupOrchestrator(runner=runner, node_runner=runner, archive_root=tmp_path,
        event_sink=lambda event: events.append(copy.deepcopy(event))).run(
            "repair", _profile(), {}, strategy="stable", flow=ExpertFlowSpec.from_value(flow))
    assert result.status == "completed"
    assert "unused_proof" not in calls
    assert any(e.get("node_id") == "unused_proof" and e.get("reason") == "branch_not_taken" for e in events)
    final_state = next(e for e in reversed(events) if e.get("type") == "expert_flow_state")
    assert final_state["discharged_audit_node_ids"] == ["technical"]


@pytest.mark.asyncio
async def test_custom_runner_cannot_publish_pass_without_required_review_receipt(tmp_path):
    calls, events = [], []
    flow = findings_flow().as_dict()
    for edge in flow["edges"]:
        if edge["from_node"] == "proof":
            edge["condition"] = "pass"

    async def runner(node, *_args):
        calls.append(node.node_id)
        return ExpertRoleResult("ok", verdict=(findings() if node.node_id == "technical" else _pass_verdict())
            if node.role == "auditor" else None)

    result = await ExpertGroupOrchestrator(runner=runner, node_runner=runner, archive_root=tmp_path,
        event_sink=lambda event: events.append(copy.deepcopy(event))).run(
            "repair", _profile(), {}, strategy="stable", flow=ExpertFlowSpec.from_value(flow))
    assert result.status != "completed" and "done" not in calls
    assert calls.count("technical") == calls.count("repair") == 1
    assert calls.count("proof") == 2  # One protocol-only repair, no upstream work retry.
    assert not any(e.get("type") == "expert_handoff" and e.get("from_node") == "proof" for e in events)


@pytest.mark.asyncio
async def test_rejected_verdict_replacement_does_not_reuse_previous_pass():
    from mewcode.tools.submit_verdict import SubmitVerdictTool, SubmitVerdictParams, VerdictHolder
    holder = VerdictHolder()
    tool = SubmitVerdictTool(holder, required_reviews={"technical": "frozen"})
    assert not (await tool.execute(SubmitVerdictParams(verdict="PASS", reviewed_snapshots={"technical": "frozen"}))).is_error
    assert (await tool.execute(SubmitVerdictParams(verdict="PASS", reviewed_snapshots={"technical": "stale"}))).is_error
    assert holder.read() is None
    assert len(holder.submissions) == 1
