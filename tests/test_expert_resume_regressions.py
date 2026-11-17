"""Regression cases from stopped timelines and interrupted audit candidates."""
import asyncio
import copy
import json
from pathlib import Path

import pytest

from mewcode.expert_group import ExpertGroupOrchestrator, ExpertProfile, ExpertRoleResult
from mewcode.expert_runs import ExpertRunStore, _normalize_snapshot
from mewcode.experts.audit_workspace import AuditWorkspace
from mewcode.model_registry import ModelRef
from tests.test_audit_protocol import checks, flow_spec
from tests.test_expert_retry import blocked_run


def test_old_terminated_checkpoint_uses_attempt_stop_time_without_disk_mutation(tmp_path):
    store = ExpertRunStore(tmp_path)
    run_id = "b" * 32
    raw = dict(version=2, run_id=run_id, status="terminated", attempt=1,
               updated_at="2026-09-05T20:00:00+08:00", completed_at="2026-09-05T17:26:31+08:00",
               attempts=[dict(attempt=1, ended_at="2026-09-05T17:26:30+08:00")],
               node_states={"executor": dict(status="paused", attempt=1, started_at=1788600380.0)},
               events=[])
    path = tmp_path / f"{run_id}.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    before = path.read_bytes()
    state = store.load(run_id)["node_states"]["executor"]
    assert state["status"] == "terminated"
    assert state["duration_ms"] == 10000
    assert state["completed_at"] == 1788600390.0
    assert path.read_bytes() == before
    # Both the direct endpoint and conversation's raw-JSON cache use the fix.
    public = store._write_summary_cache(copy.deepcopy(raw), path)
    event = next(e for e in public["events"] if e.get("snapshot_aggregate") and e["type"] == "expert_stage")
    assert event["completed_at"] == state["completed_at"]
    assert event["status"] == "terminated"


def test_stop_projection_preserves_actual_completion_and_old_attempt_time():
    snapshot = dict(version=2, status="blocked", attempt=2,
                    attempts=[dict(attempt=1, ended_at="2026-09-05T17:26:30+08:00")],
                    node_states={"executor": dict(status="paused", attempt=1, started_at=1788600380.0),
                                 "planner": dict(status="completed", started_at=1, completed_at=2)},
                    events=[])
    states = _normalize_snapshot(snapshot)["node_states"]
    assert states["executor"]["duration_ms"] == 10000
    assert states["planner"]["status"] == "completed"
    assert states["planner"]["completed_at"] == 2


@pytest.mark.parametrize("interruption", ["budget", "cancel", "legacy"])
@pytest.mark.parametrize("round_limit", [1, 3])
@pytest.mark.asyncio
async def test_repair_resume_uses_retained_candidate_and_context(tmp_path, interruption, round_limit):
    from mewcode.experts.flow import ExpertFlowSpec

    flow_value = flow_spec().as_dict()
    flow_value["nodes"][0]["failure_policy"]["max_attempts"] = round_limit
    flow = ExpertFlowSpec.from_value(flow_value)
    source = tmp_path / "project"
    source.mkdir()
    (source / "paper.txt").write_text("original", encoding="utf-8")
    model = ModelRef("test", "test", "")
    profile = ExpertProfile(manager=model, executor=model, auditor=model)
    events = []
    repairs = []
    prompts = []

    async def runner(node, _model, _prompt, policy, member=None):
        if node.node_id == "done":
            return ExpertRoleResult(text="done")
        if node.node_id == "repair":
            candidate = Path(policy.work_dir) / "paper.txt"
            repairs.append(candidate)
            prompts.append(_prompt)
            if len(repairs) == 1:
                assert not policy.resume_messages
                candidate.write_text("partial revision", encoding="utf-8")
                if interruption == "cancel":
                    raise asyncio.CancelledError()
                return ExpertRoleResult(text="partial revision saved", status="budget_exhausted")
            assert interruption != "budget"
            assert not policy.resume_messages
            assert "当前步骤" in _prompt
            assert "不重新全面盘点" in _prompt
            assert "轻量接续" in _prompt
            if interruption != "cancel":
                assert "partial revision saved" in _prompt
            assert "当前步骤：modify" in _prompt
            assert "pix|REQ-01|full" in _prompt
            assert "paper.txt" in _prompt
            assert "ReadAuditArtifact" in _prompt
            assert "仍允许" in _prompt or "计算哈希" in _prompt or "读 Word" in _prompt or "读段落" in _prompt
            assert candidate == repairs[0]
            assert candidate.read_text(encoding="utf-8") == "partial revision"
            candidate.write_text("finished revision", encoding="utf-8")
            return ExpertRoleResult(text="finished revision")
        contract = policy.audit_contract
        paired = "baseline_hash" in contract
        report = dict(rubric_hash=contract["rubric"]["rubric_hash"], artifact_hash=contract["artifact_hash"],
                      checks=checks("PASS" if paired else "FAIL"))
        if paired:
            report.update(baseline_hash=contract["baseline_hash"], baseline_checks=checks())
        return ExpertRoleResult(text="audit", verdict={
            "verdict": "PASS" if paired else "NEEDS_ATTENTION", "assessment": report,
            "issues": [] if paired else [{"issue_id": "fig", "criterion_id": "REQ-01",
                "acceptance_test": contract["rubric"]["criteria"][0]["acceptance_test"]}],
        })

    orchestrator = ExpertGroupOrchestrator(runner=lambda *_: None, node_runner=runner,
        work_dir=source, archive_root=tmp_path / "archive", event_sink=lambda e: events.append(copy.deepcopy(e)))
    args = ("repair", profile, {"providers": []})
    if interruption == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await orchestrator.run(*args, strategy="stable", flow=flow, run_id="same-run")
    else:
        result = await orchestrator.run(*args, strategy="stable", flow=flow, run_id="same-run")
        assert result.status == "blocked"
    saved = next(e["audit_protocol"] for e in reversed(events) if "audit_protocol" in e)
    assert saved["pending_repair"] and saved["round"] == 1
    assert (source / "paper.txt").read_text(encoding="utf-8") == "original"
    if interruption != "cancel":
        assert "partial revision saved" in str((saved.get("repair_progress") or {}).get("text") or "")
    saved["delivered_evidence"] = ["pix|REQ-01|full"]
    if interruption == "legacy":
        saved.pop("pending_repair")
        saved.pop("repair_budget", None)
    result = await orchestrator.run(*args, strategy="stable", flow=flow, run_id="same-run",
                                    checkpoint={"audit_protocols": {"audit": saved}})
    if interruption == "budget":
        assert result.status == "blocked"
        assert "预算已耗尽" in result.final_summary
        assert len(repairs) == 1
        assert (source / "paper.txt").read_text(encoding="utf-8") == "original"
        protocol = next(e["audit_protocol"] for e in reversed(events) if "audit_protocol" in e)
        assert protocol.get("pending_repair")
        assert "partial revision saved" in str((protocol.get("repair_progress") or {}).get("text") or "")
        return
    assert result.status == "completed"
    assert len(repairs) == 2
    assert next(e["audit_protocol"] for e in reversed(events) if "audit_protocol" in e)["round"] == 1
    assert (source / "paper.txt").read_text(encoding="utf-8") == "finished revision"


@pytest.mark.parametrize("interruption", ["budget_exhausted", "blocked", "cancel", "seal_crash"])
@pytest.mark.asyncio
async def test_finished_candidate_resumes_only_paired_review_even_at_round_limit(tmp_path, interruption):
    from mewcode.experts.flow import ExpertFlowSpec

    source = tmp_path / "project"
    source.mkdir()
    (source / "paper.txt").write_text("original", encoding="utf-8")
    flow_value = flow_spec().as_dict()
    flow_value["nodes"][0]["failure_policy"]["max_attempts"] = 1
    flow = ExpertFlowSpec.from_value(flow_value)
    model = ModelRef("test", "test", "")
    profile = ExpertProfile(manager=model, executor=model, auditor=model)
    events, calls, paths = [], [], []

    async def runner(node, _model, _prompt, policy, member=None):
        if node.node_id == "done":
            return ExpertRoleResult(text="done")
        if node.node_id == "repair":
            calls.append("repair")
            (Path(policy.work_dir) / "paper.txt").write_text("finished", encoding="utf-8")
            return ExpertRoleResult(text="verified candidate", verification_records=[{"output": "ok"}])
        contract = policy.audit_contract
        paired = "baseline_hash" in contract
        calls.append("paired" if paired else "baseline")
        if paired:
            paths.append(policy.work_dir)
            assert contract["repair_evidence"] == "verified candidate"
            assert contract["verification_records"] == [{"output": "ok"}]
            if calls.count("paired") == 1:
                if interruption == "cancel":
                    raise asyncio.CancelledError()
                return ExpertRoleResult(text="review service unavailable", status="blocked" if interruption == "seal_crash" else interruption)
        report = dict(rubric_hash=contract["rubric"]["rubric_hash"], artifact_hash=contract["artifact_hash"],
                      checks=checks("PASS" if paired else "FAIL"))
        if paired:
            report.update(baseline_hash=contract["baseline_hash"], baseline_checks=checks())
        return ExpertRoleResult(text="audit", verdict={"verdict": "PASS" if paired else "NEEDS_ATTENTION",
            "assessment": report, "issues": [] if paired else [{"issue_id": "fig", "criterion_id": "REQ-01",
                "acceptance_test": contract["rubric"]["criteria"][0]["acceptance_test"]}]})

    orchestrator = ExpertGroupOrchestrator(runner=lambda *_: None, node_runner=runner,
        work_dir=source, archive_root=tmp_path / "archive", event_sink=lambda e: events.append(copy.deepcopy(e)))
    args = ("repair", profile, {"providers": []})
    if interruption == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await orchestrator.run(*args, strategy="stable", flow=flow, run_id="same")
    else:
        result = await orchestrator.run(*args, strategy="stable", flow=flow, run_id="same")
        assert result.status == "blocked"
        assert "review service unavailable" in result.final_summary
    saved = next(e["audit_protocol"] for e in reversed(events) if "audit_protocol" in e)
    assert saved["pending_review"] and saved["round"] == 1
    if interruption == "seal_crash":
        # Simulate durable seal preceding the checkpoint phase update.
        saved.pop("pending_review")
        saved["pending_repair"] = True
        record_path = Path(saved["transaction_dir"]) / "transaction.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["state"] = "reviewing"
        record_path.write_text(json.dumps(record), encoding="utf-8")
    assert (source / "paper.txt").read_text(encoding="utf-8") == "original"
    result = await orchestrator.run(*args, strategy="stable", flow=flow, run_id="same",
                                    checkpoint={"audit_protocols": {"audit": saved}})
    assert result.status == "completed"
    assert calls == ["baseline", "repair", "paired", "paired"]
    assert paths[0] == paths[1]
    assert (source / "paper.txt").read_text(encoding="utf-8") == "finished"
    assert len(list((tmp_path / "archive").rglob("transaction.json"))) == 1


def test_sealed_candidate_resume_rejects_changes_and_rejected_versions(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "paper.txt").write_text("original", encoding="utf-8")
    archive = tmp_path / "archive"
    tx = AuditWorkspace(source, archive)
    tx.seal()
    tx.discard("unaccepted")
    assert AuditWorkspace.resume(source, archive, str(tx.root), sealed=True).after == tx.after
    (tx.candidate / "paper.txt").write_text("unreviewed change", encoding="utf-8")
    with pytest.raises(ValueError, match="候选稿已变化"):
        AuditWorkspace.resume(source, archive, str(tx.root), sealed=True)
    tx.discard("rejected")
    with pytest.raises(ValueError, match="不能作为"):
        AuditWorkspace.resume(source, archive, str(tx.root), sealed=True)


@pytest.mark.asyncio
async def test_exhausted_audit_does_not_repeat_baseline_on_continue(tmp_path):
    async def runner(*_args, **_kwargs):
        pytest.fail("An exhausted audit called a model again")
    model = ModelRef("test", "test", "")
    result = await ExpertGroupOrchestrator(runner=runner, node_runner=runner, work_dir=tmp_path,
        archive_root=tmp_path / "archive").run("repair", ExpertProfile(manager=model, executor=model, auditor=model),
        {"providers": []}, strategy="stable", flow=flow_spec(),
        checkpoint={"audit_protocols": {"audit": {"round": 3}}})
    assert result.status == "blocked"
    assert "普通继续不会重复" in result.final_summary


def test_resume_candidate_rejects_changed_baseline_and_unrelated_path(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "paper.txt").write_text("original", encoding="utf-8")
    archive = tmp_path / "archive"
    transaction = AuditWorkspace(source, archive)
    transaction.discard("unaccepted")
    with pytest.raises(ValueError, match="不属于"):
        AuditWorkspace.resume(source, tmp_path / "other", str(transaction.root))
    (source / "paper.txt").write_text("user edit", encoding="utf-8")
    with pytest.raises(ValueError, match="原稿或基线已变化"):
        AuditWorkspace.resume(source, archive, str(transaction.root))
    assert (source / "paper.txt").read_text(encoding="utf-8") == "user edit"


def test_explicit_retry_renews_budget_but_retains_candidate_reference(tmp_path):
    store, run_id, flow = blocked_run(tmp_path)
    store.mutate(run_id, lambda s: s["audit_protocols"]["audit"].update(
        transaction_dir="retained-candidate", pending_repair=True))
    result = store.resume(run_id, strategy="stable", retry_node_id="repair", flow=flow)
    assert result["audit_protocols"]["audit"]["round"] == 0
    assert result["audit_protocols"]["audit"]["pending_repair"] is True
    assert result["audit_protocols"]["audit"]["transaction_dir"] == "retained-candidate"


def test_explicit_retry_retains_review_phase_and_evidence(tmp_path):
    store, run_id, flow = blocked_run(tmp_path)
    retained = dict(transaction_dir="retained-candidate", pending_review=True,
                    baseline_result={"text": "baseline", "verdict": {}},
                    repair_result={"text": "repair evidence", "verification_records": []},
                    repair_step="modify",
                    repair_progress={"text": "绿色框为 GT", "edited_delivery": ["paper.docx"]},
                    delivered_evidence=["pix|FIG-01|full"],
                    changed_files=["paper.docx"])
    store.mutate(run_id, lambda s: s["audit_protocols"]["audit"].update(retained))
    result = store.resume(run_id, strategy="stable", retry_node_id="repair", flow=flow)
    protocol = result["audit_protocols"]["audit"]
    assert protocol["round"] == 0
    assert protocol["no_progress"] == 0
    assert "repair_budget" not in protocol
    assert all(protocol[key] == value for key, value in retained.items())


@pytest.mark.asyncio
async def test_candidate_executor_can_view_images_only_in_its_workspace(tmp_path):
    from dataclasses import replace
    from types import SimpleNamespace
    from PIL import Image

    from mewcode.expert_group import derive_execution_policy
    from mewcode.expert_web import _role_registry
    from mewcode.tools import ToolRegistry
    from mewcode.tools.read_audit_artifact import ReadAuditArtifactParams

    candidate = tmp_path / "candidate"
    candidate.mkdir()
    figure = candidate / "figure.png"
    Image.new("RGB", (10, 10), "white").save(figure)
    outside = tmp_path / "outside.png"
    Image.new("RGB", (10, 10), "black").save(outside)
    session = SimpleNamespace(agent=SimpleNamespace(registry=ToolRegistry()))
    policy = replace(derive_execution_policy(False), work_dir=str(candidate))
    tool = _role_registry(session, "executor", policy).get("ReadAuditArtifact")
    assert tool is not None and tool.is_read_only
    result = await tool.execute(ReadAuditArtifactParams(file_path=str(figure)))
    assert not result.is_error and result.content_blocks[0]["type"] == "image"
    assert (await tool.execute(ReadAuditArtifactParams(file_path=str(outside)))).is_error


@pytest.mark.asyncio
async def test_repair_budget_stop_preserves_image_evidence_without_promoting_candidate(tmp_path):
    from mewcode.experts.audit_evidence import current_evidence_ledger
    from mewcode.experts.flow import ExpertFlowSpec

    flow = flow_spec()
    source = tmp_path / "project"
    source.mkdir()
    (source / "paper.txt").write_text("original", encoding="utf-8")
    model = ModelRef("test", "test", "")
    profile = ExpertProfile(manager=model, executor=model, auditor=model)
    events, calls = [], []

    async def runner(node, _model, _prompt, policy, member=None):
        calls.append(node.node_id)
        if node.node_id == "done":
            return ExpertRoleResult(text="done")
        if node.node_id == "repair":
            ledger = current_evidence_ledger()
            assert ledger is not None
            ledger.record("pix|FIG-01|full")
            return ExpertRoleResult(text="still inspecting figure 5", status="budget_exhausted")
        contract = policy.audit_contract
        paired = "baseline_hash" in contract
        report = dict(rubric_hash=contract["rubric"]["rubric_hash"], artifact_hash=contract["artifact_hash"],
                      checks=checks("PASS" if paired else "FAIL"))
        if paired:
            report.update(baseline_hash=contract["baseline_hash"], baseline_checks=checks())
        return ExpertRoleResult(text="audit", verdict={
            "verdict": "PASS" if paired else "NEEDS_ATTENTION", "assessment": report,
            "issues": [] if paired else [{"issue_id": "fig", "criterion_id": "REQ-01",
                "acceptance_test": contract["rubric"]["criteria"][0]["acceptance_test"]}],
        })

    orchestrator = ExpertGroupOrchestrator(
        runner=lambda *_: None, node_runner=runner,
        work_dir=source, archive_root=tmp_path / "archive",
        event_sink=lambda e: events.append(copy.deepcopy(e)),
    )
    result = await orchestrator.run("repair", profile, {"providers": []}, strategy="stable", flow=flow)
    assert result.status == "blocked"
    stopped = next(event for event in reversed(events) if event.get("phase") == "audit_stopped")
    assert stopped["reason"] == "候选修复预算已耗尽，原稿未合入候选修改"
    saved = next(e["audit_protocol"] for e in reversed(events) if "audit_protocol" in e)
    assert "pix|FIG-01|full" in saved.get("delivered_evidence", [])
    assert saved.get("repair_step") == "locate"
    assert saved["repair_result"]["status"] == "budget_exhausted"
    assert saved["completed"] is False and saved.get("pending_review") is not True
    assert calls == ["audit", "repair"]
    assert (source / "paper.txt").read_text(encoding="utf-8") == "original"


@pytest.mark.asyncio
async def test_controller_retry_reuses_frozen_baseline_before_paired_review(tmp_path):
    flow = flow_spec()
    source = tmp_path / "project"
    source.mkdir()
    (source / "paper.txt").write_text("original", encoding="utf-8")
    model = ModelRef("test", "test", "")
    profile = ExpertProfile(manager=model, executor=model, auditor=model)
    events, calls = [], []

    async def runner(node, _model, _prompt, policy, member=None):
        if node.node_id == "done":
            return ExpertRoleResult(text="done")
        if node.node_id == "repair":
            calls.append("repair")
            if calls.count("repair") == 1:
                return ExpertRoleResult(text="still locating", status="needs_attention")
            (Path(policy.work_dir) / "paper.txt").write_text("finished", encoding="utf-8")
            return ExpertRoleResult(text="finished revision")
        contract = policy.audit_contract
        calls.append("paired" if "baseline_hash" in contract else "baseline")
        report = dict(rubric_hash=contract["rubric"]["rubric_hash"], artifact_hash=contract["artifact_hash"],
                      checks=checks("PASS" if "baseline_hash" in contract else "FAIL"))
        if "baseline_hash" in contract:
            report.update(baseline_hash=contract["baseline_hash"], baseline_checks=checks())
        return ExpertRoleResult(text="audit", verdict={
            "verdict": "PASS" if "baseline_hash" in contract else "NEEDS_ATTENTION",
            "assessment": report,
            "issues": [] if "baseline_hash" in contract else [{"issue_id": "fig", "criterion_id": "REQ-01",
                "acceptance_test": contract["rubric"]["criteria"][0]["acceptance_test"]}],
        })

    orchestrator = ExpertGroupOrchestrator(
        runner=lambda *_: None, node_runner=runner,
        work_dir=source, archive_root=tmp_path / "archive",
        event_sink=lambda e: events.append(copy.deepcopy(e)),
    )
    result = await orchestrator.run(
        "repair", profile, {"providers": []}, strategy="stable", flow=flow, run_id="same",
    )
    assert result.status == "completed"
    assert calls == ["baseline", "repair", "repair", "paired"]
    decision = next(e["controller"] for e in events if e.get("phase") == "audit_recovery")
    assert decision["action"] == "self_diagnose"
    assert (source / "paper.txt").read_text(encoding="utf-8") == "finished"


@pytest.mark.asyncio
async def test_two_empty_repair_attempts_stop_for_no_progress(tmp_path):
    flow = flow_spec()
    source = tmp_path / "project"
    source.mkdir()
    (source / "paper.txt").write_text("original", encoding="utf-8")
    model = ModelRef("test", "test", "")
    profile = ExpertProfile(manager=model, executor=model, auditor=model)
    events, repairs = [], []

    async def runner(node, _model, _prompt, policy, member=None):
        if node.node_id == "done":
            return ExpertRoleResult(text="done")
        if node.node_id == "repair":
            repairs.append("repair")
            return ExpertRoleResult(text="rewrote the progress note", status="needs_attention")
        contract = policy.audit_contract
        report = dict(rubric_hash=contract["rubric"]["rubric_hash"], artifact_hash=contract["artifact_hash"],
                      checks=checks("FAIL"))
        return ExpertRoleResult(text="audit", verdict={
            "verdict": "NEEDS_ATTENTION", "assessment": report,
            "issues": [{"issue_id": "fig", "criterion_id": "REQ-01",
                        "acceptance_test": contract["rubric"]["criteria"][0]["acceptance_test"]}],
        })

    orchestrator = ExpertGroupOrchestrator(
        runner=lambda *_: None, node_runner=runner,
        work_dir=source, archive_root=tmp_path / "archive",
        event_sink=lambda e: events.append(copy.deepcopy(e)),
    )
    result = await orchestrator.run(
        "repair", profile, {"providers": []}, strategy="stable", flow=flow, run_id="same",
    )
    assert result.status == "blocked"
    saved = next(e["audit_protocol"] for e in reversed(events) if "audit_protocol" in e)
    assert len(repairs) == 2
    stopped = next(event for event in reversed(events) if event.get("phase") == "audit_stopped")
    assert saved.get("no_progress") == 2
    assert saved["controller_decision"]["action"] == "assist"
    assert stopped["reason"] == saved["controller_decision"]["reason"]
    assert not saved.get("pending_recovery_revision")
    assert saved.get("recovery_decision") != "auto_method"
    assert saved.get("completed") is False and saved.get("pending_review") is not True
    assert (source / "paper.txt").read_text(encoding="utf-8") == "original"


@pytest.mark.asyncio
async def test_provider_failure_retries_once_without_refilling_budget(tmp_path):
    flow = flow_spec()
    source = tmp_path / "project"
    source.mkdir()
    (source / "paper.txt").write_text("original", encoding="utf-8")
    model = ModelRef("test", "test", "")
    profile = ExpertProfile(manager=model, executor=model, auditor=model)
    events, repairs = [], []

    async def runner(node, _model, _prompt, policy, member=None):
        if node.node_id == "done":
            return ExpertRoleResult(text="done")
        if node.node_id == "repair":
            repairs.append(policy.max_iterations)
            if len(repairs) == 1:
                return ExpertRoleResult(text="API error (502): upstream", status="failed")
            return ExpertRoleResult(text="rewrote the progress note", status="needs_attention")
        contract = policy.audit_contract
        report = dict(rubric_hash=contract["rubric"]["rubric_hash"], artifact_hash=contract["artifact_hash"],
                      checks=checks("FAIL"))
        return ExpertRoleResult(text="audit", verdict={
            "verdict": "NEEDS_ATTENTION", "assessment": report,
            "issues": [{"issue_id": "fig", "criterion_id": "REQ-01",
                        "acceptance_test": contract["rubric"]["criteria"][0]["acceptance_test"]}],
        })

    orchestrator = ExpertGroupOrchestrator(
        runner=lambda *_: None, node_runner=runner,
        work_dir=source, archive_root=tmp_path / "archive",
        event_sink=lambda e: events.append(copy.deepcopy(e)),
    )
    result = await orchestrator.run(
        "repair", profile, {"providers": []}, strategy="stable", flow=flow, run_id="prov",
    )
    assert result.status == "blocked"
    assert len(repairs) == 2
    assert repairs[1] <= repairs[0]
    saved = next(e["audit_protocol"] for e in reversed(events) if "audit_protocol" in e)
    assert int((saved.get("repair_budget") or {}).get("iterations_used") or 0) >= 0
    assert saved.get("pending_recovery_revision") is None or saved.get("blocker") != "model_service_fault"
