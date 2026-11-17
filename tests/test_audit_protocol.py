from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import pytest

from mewcode.expert_group import ExpertGroupOrchestrator, ExpertProfile, ExpertRoleResult
from mewcode.experts.audit_protocol import (
    content_hash, decide_candidate, freeze_rubric, validate_assessment,
)
from mewcode.experts.audit_workspace import AuditWorkspace, ensure_no_partial_promotion, manifest
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.model_registry import ModelRef


def flow_spec():
    return ExpertFlowSpec.from_value({
        "flow_id": "quantified", "version": 1, "label": "Experiment audit",
        "nodes": [
            {"node_id": "audit", "role": "auditor", "label": "实验审查",
             "responsibility": "图5场景标签与图像内容一致",
             "failure_policy": {"action": "route", "max_attempts": 3, "target_node_id": "repair"}},
            {"node_id": "repair", "role": "executor", "label": "修改实验"},
            {"node_id": "done", "role": "manager", "label": "收束"},
        ],
        "edges": [{"from": "audit", "to": "done", "condition": "pass"},
                  {"from": "audit", "to": "repair", "condition": "fail"},
                  {"from": "repair", "to": "done"}],
        "finalizer_node_id": "done",
    })


def checks(req="FAIL", evidence="PASS", regression="PASS"):
    return [{"criterion_id": key, "state": state, "location": "paper.txt:1",
             "evidence": "图5标签的可定位核验记录"}
            for key, state in zip(("REQ-01", "EVD-01", "REG-01"), (req, evidence, regression))]


def test_discrete_gate_rejects_compensating_regression_and_rating_drift():
    flow = flow_spec()
    rubric = freeze_rubric(flow.node_map["audit"], flow)
    old = {"artifact_hash": "old", "checks": checks()}
    new = {"baseline_hash": "old", "baseline_checks": checks(),
           "checks": checks("PASS", regression="PARTIAL")}
    result = decide_candidate(rubric, old, new)
    assert result.after < result.before  # Aggregate score improved, but gate rejects.
    assert result.outcome == "regression"
    new.update(baseline_checks=checks("PARTIAL"), checks=checks("PASS"))
    assert decide_candidate(rubric, old, new).outcome == "invalid"


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "wrong_hash", "contradiction", "empty_evidence"])
def test_assessment_fails_closed(mutation):
    flow = flow_spec()
    rubric = freeze_rubric(flow.node_map["audit"], flow)
    report = {"rubric_hash": rubric["rubric_hash"], "artifact_hash": "file-hash", "checks": checks()}
    verdict = {"verdict": "NEEDS_ATTENTION", "assessment": report}
    if mutation == "missing":
        report["checks"].pop()
    elif mutation == "duplicate":
        report["checks"].append(report["checks"][0])
    elif mutation == "wrong_hash":
        report["artifact_hash"] = "different"
    elif mutation == "contradiction":
        verdict["verdict"] = "PASS"
    else:
        report["checks"][0]["evidence"] = " "
    with pytest.raises(ValueError):
        validate_assessment(rubric, verdict, "file-hash")


@pytest.mark.parametrize('field', ['checks', 'baseline_checks'])
@pytest.mark.parametrize('invalid', [True, 'PASS'])
def test_invalid_check_feedback_names_fields_without_coercing_verdict(field, invalid):
    from mewcode.tools.submit_verdict import SubmitVerdictParams
    report = {'rubric_hash': 'rubric', 'artifact_hash': 'artifact', 'checks': checks(), field: [invalid]}
    before = copy.deepcopy(report)
    with pytest.raises(ValueError) as error:
        SubmitVerdictParams.model_validate({'verdict': 'PASS', 'assessment': report})
    for name in ('criterion_id', 'state', 'evidence', 'location', 'UNKNOWN'):
        assert name in str(error.value)
    assert report == before


def test_rubric_cannot_silently_change_on_resume():
    flow = flow_spec()
    saved = freeze_rubric(flow.node_map["audit"], flow)
    assert freeze_rubric(flow.node_map["audit"], flow, saved) == saved
    changed = copy.deepcopy(saved)
    changed["criteria"][0]["acceptance_test"] = "new taste"
    with pytest.raises(ValueError, match="冻结"):
        freeze_rubric(flow.node_map["audit"], flow, changed)


def test_method_change_bumps_version_and_cannot_compare_old_scores():
    from mewcode.experts.audit_protocol import bump_rubric, decide_candidate
    from mewcode.experts.recovery_classify import CHANGE_METHOD, CHANGE_SCOPE

    flow = flow_spec()
    saved = freeze_rubric(flow.node_map["audit"], flow)
    method = copy.deepcopy(saved["criteria"])
    method[0]["acceptance_test"] = saved["criteria"][0]["acceptance_test"] + "；必须通过 ReadAuditArtifact 读取像素"
    bumped = bump_rubric(saved, method, change_kind=CHANGE_METHOD, approved=False)
    assert bumped["version"] == 2
    assert bumped["rubric_hash"] != saved["rubric_hash"]
    with pytest.raises(ValueError, match="批准"):
        bump_rubric(saved, method, change_kind=CHANGE_SCOPE, approved=False)
    old = {"rubric_hash": saved["rubric_hash"], "artifact_hash": "old", "checks": checks()}
    new = {"rubric_hash": bumped["rubric_hash"], "baseline_hash": "old",
           "baseline_checks": checks(), "checks": checks("PASS")}
    assert decide_candidate(bumped, old, new).outcome == "invalid"
    assert "不同验收版本" in decide_candidate(bumped, old, new).reason


def test_configured_criteria_survive_canvas_flow_conversion():
    from mewcode.orchestration.convert import from_expert_flow, to_expert_flow

    payload = flow_spec().as_dict()
    payload["meta"] = {"audit_rubrics": {"audit": [
        {"criterion_id": "FIG-01", "severity": "blocking", "acceptance_test": "GT legend matches"},
    ]}}
    flow = ExpertFlowSpec.from_value(payload)
    restored = to_expert_flow(from_expert_flow(flow))
    assert restored.meta["audit_rubrics"] == payload["meta"]["audit_rubrics"]
    assert freeze_rubric(restored.node_map["audit"], restored)["criteria"][0]["criterion_id"] == "FIG-01"


def test_candidate_promotion_preserves_dirty_files_and_rejects_concurrent_edit(tmp_path):
    source = tmp_path / "project"
    source.mkdir()
    (source / "paper.txt").write_text("user dirty draft", encoding="utf-8")
    (source / "untouched.bin").write_bytes(b"\x00\xff")
    tx = AuditWorkspace(source, tmp_path / "versions")
    (tx.candidate / "paper.txt").write_text("candidate", encoding="utf-8")
    tx.seal()
    (source / "paper.txt").write_text("concurrent user edit", encoding="utf-8")
    with pytest.raises(ValueError, match="复审期间"):
        tx.promote()
    assert (source / "paper.txt").read_text(encoding="utf-8") == "concurrent user edit"
    assert (tx.baseline / "paper.txt").read_text(encoding="utf-8") == "user dirty draft"
    assert (source / "untouched.bin").read_bytes() == b"\x00\xff"


def test_candidate_promotes_only_sealed_version_and_keeps_backup(tmp_path):
    source = tmp_path / "project"
    source.mkdir()
    (source / "paper.bin").write_bytes(b"old\x00")
    tx = AuditWorkspace(source, tmp_path / "versions")
    (tx.candidate / "paper.bin").write_bytes(b"new\x00")
    (tx.candidate / "new.txt").write_text("added", encoding="utf-8")
    sealed = tx.seal()
    assert tx.promote() == ["new.txt", "paper.bin"]
    assert content_hash(manifest(source)) == sealed
    assert (tx.baseline / "paper.bin").read_bytes() == b"old\x00"
    tx2 = AuditWorkspace(source, tmp_path / "versions")
    tx2.seal()
    (tx2.candidate / "paper.bin").write_bytes(b"unreviewed")
    with pytest.raises(ValueError, match="复审后"):
        tx2.promote()
    assert (source / "paper.bin").read_bytes() == b"new\x00"


def test_deleted_file_and_interrupted_promotion_are_not_silently_accepted(tmp_path):
    source = tmp_path / "project"
    source.mkdir()
    (source / "paper.txt").write_text("keep", encoding="utf-8")
    tx = AuditWorkspace(source, tmp_path / "versions")
    (tx.candidate / "paper.txt").unlink()
    tx.seal()
    with pytest.raises(ValueError, match="删除"):
        tx.promote()
    assert (source / "paper.txt").read_text(encoding="utf-8") == "keep"
    tx.record("committing")
    with pytest.raises(ValueError, match="合入中断"):
        ensure_no_partial_promotion(tmp_path / "versions")


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,expected_repairs,completed", [
    ("pass", 1, True), ("gradual", 2, True), ("regression", 1, False),
    ("plateau", 3, False), ("repeat", 2, False), ("unknown", 1, False),
    ("drift", 1, False), ("conflict", 1, False), ("budget", 1, False), ("cancel", 1, False),
    ("unknown_baseline_repaired", 1, True), ("unknown_baseline_unresolved", 1, False),
    ("api_failure", 2, False),
])
async def test_real_orchestrator_routes_through_candidate_acceptance(
    tmp_path, scenario, expected_repairs, completed,
):
    source = tmp_path / "project"
    source.mkdir()
    (source / "paper.txt").write_text("original", encoding="utf-8")
    flow = flow_spec()
    model = ModelRef("test", "test", "")
    profile = ExpertProfile(manager=model, planner=model, executor=model, auditor=model)
    repairs = 0
    events = []

    async def runner(node, _model, prompt, policy, member=None):
        nonlocal repairs
        if node.node_id == "done":
            return ExpertRoleResult(text="done", status="completed")
        if node.node_id == "repair":
            repairs += 1
            assert Path(policy.work_dir) != source
            assert not policy.allow_delegation and not policy.resume_messages
            assert policy.max_iterations == 40
            if scenario.startswith("unknown_baseline"):
                assert "不得编造原始数据" in prompt
            Path(policy.work_dir, "paper.txt").write_text(
                "candidate-1" if scenario == "repeat" else f"candidate-{repairs}", encoding="utf-8",
            )
            if scenario == "conflict":
                (source / "paper.txt").write_text("user edit", encoding="utf-8")
            if scenario == "cancel":
                raise asyncio.CancelledError()
            if scenario == "api_failure":
                return ExpertRoleResult(text="API error (502): Upstream access forbidden", status="failed")
            return ExpertRoleResult(text="new approach", status="budget_exhausted" if scenario == "budget" else "completed")
        contract = policy.audit_contract
        assert contract and "冻结验收协议" in prompt
        report = {"rubric_hash": contract["rubric"]["rubric_hash"],
                  "artifact_hash": contract["artifact_hash"], "checks": checks()}
        if scenario.startswith("unknown_baseline"):
            report["checks"] = checks("PARTIAL", evidence="UNKNOWN", regression="FAIL")
        if "baseline_hash" in contract:
            assert "baseline_checks" in prompt
            report.update(baseline_hash=contract["baseline_hash"],
                          baseline_checks=contract["previous_assessment"]["checks"])
            if scenario in {"pass", "conflict", "unknown_baseline_repaired"} or scenario == "gradual" and repairs == 2:
                report["checks"] = checks("PASS")
            elif scenario == "gradual":
                report["checks"] = checks("PARTIAL")
            elif scenario == "regression":
                report["checks"] = checks("PASS", regression="PARTIAL")
            elif scenario == "unknown":
                report["checks"] = checks("UNKNOWN")
            elif scenario == "drift":
                report.update(baseline_checks=checks("PARTIAL"), checks=checks("PASS"))
        passed = all(item["state"] == "PASS" for item in report["checks"])
        return ExpertRoleResult(text="audit evidence", status="completed", verdict={
            "verdict": "PASS" if passed else "NEEDS_ATTENTION", "assessment": report,
            "issues": [{"issue_id": item["criterion_id"] + "@figure5", "description": "label issue",
                        "criterion_id": item["criterion_id"],
                        "acceptance_test": next(c["acceptance_test"] for c in contract["rubric"]["criteria"]
                                                if c["criterion_id"] == item["criterion_id"])}
                       for item in report["checks"] if item["state"] != "PASS"],
        })

    orchestrator = ExpertGroupOrchestrator(
        runner=lambda *_args: None, node_runner=runner,
        work_dir=source, archive_root=tmp_path / "archives",
        event_sink=lambda event: events.append(copy.deepcopy(event)),
    )
    if scenario == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await orchestrator.run("repair experiments", profile, {"providers": []},
                                   strategy="stable", flow=flow)
        assert (source / "paper.txt").read_text(encoding="utf-8") == "original"
        assert any(event.get("phase") == "audit_paused" for event in events)
        assert list((tmp_path / "archives").rglob("transaction.json"))
        return
    result = await orchestrator.run("repair experiments", profile, {"providers": []},
                                   strategy="stable", flow=flow)
    assert repairs == expected_repairs
    assert (result.status == "completed") is completed
    expected_text = f"candidate-{repairs}" if completed else ("user edit" if scenario == "conflict" else "original")
    assert (source / "paper.txt").read_text(encoding="utf-8") == expected_text
    expected_phase = "audit_accepted" if completed else ("audit_paused" if scenario == "cancel" else "audit_stopped")
    assert any(event.get("phase") == expected_phase for event in events)
    # An unaccepted PASS cannot persist as the authoritative audit checkpoint.
    final_audit = [event for event in events if event.get("type") == "expert_node_completed"
                   and event.get("node_id") == "audit"][-1]
    assert final_audit["passed"] is completed
    repair_events = [event for event in events if event.get("type") == "expert_node_completed"
                     and event.get("node_id") == "repair"]
    final_repair = repair_events[-1]
    assert final_repair["status"] != "skipped"
    assert final_repair["passed"] is completed
    if scenario == "api_failure":
        assert all(event["status"] == "failed" for event in repair_events)
        assert final_repair["execution_status"] == "failed"
        assert "Upstream access forbidden" in final_repair["transcript"]
        assert "Upstream access forbidden" in final_audit["transcript"]
    elif scenario == "budget":
        assert all(event["status"] == "budget_exhausted" for event in repair_events)
    else:
        assert final_repair["status"] == ("completed" if completed else "needs_attention")
        assert final_repair["execution_status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [True, False])
async def test_resume_keeps_accepted_checkpoints_but_reaudits_unpromoted_pass(tmp_path, accepted):
    flow = flow_spec()
    source = tmp_path / "project"
    source.mkdir()
    (source / "paper.txt").write_text("original", encoding="utf-8")
    rubric = freeze_rubric(flow.node_map["audit"], flow)
    checkpoint = {
        "node_states": {"audit": {"status": "completed", "text": "saved", "verdict": {"verdict": "PASS"}}},
        "audit_protocols": {"audit": {"rubric": rubric, "completed": accepted, "round": 1}},
    }
    model = ModelRef("test", "test", "")
    profile = ExpertProfile(manager=model, planner=model, executor=model, auditor=model)
    called = []

    async def runner(node, _model, _prompt, policy, member=None):
        called.append(node.node_id)
        if node.node_id == "done":
            return ExpertRoleResult(text="done", status="completed")
        assert node.node_id == "audit" and policy.audit_contract
        contract = policy.audit_contract
        return ExpertRoleResult(text="fresh evidence", status="completed", verdict={
            "verdict": "PASS", "issues": [], "assessment": {
                "rubric_hash": rubric["rubric_hash"], "artifact_hash": contract["artifact_hash"],
                "checks": checks("PASS"),
            },
        })

    orchestrator = ExpertGroupOrchestrator(runner=lambda *_args: None, node_runner=runner,
                                         work_dir=source, archive_root=tmp_path / "archives")
    result = await orchestrator.run("resume", profile, {"providers": []}, strategy="stable",
                                    flow=flow, checkpoint=checkpoint)
    assert result.status == "completed"
    assert called == (["done"] if accepted else ["audit", "done"])


def test_promotion_failure_restores_only_its_own_writes(tmp_path, monkeypatch):
    import mewcode.experts.audit_workspace as workspace

    source = tmp_path / "project"
    source.mkdir()
    for name in ("a.txt", "b.txt"):
        (source / name).write_text("original", encoding="utf-8")
    tx = AuditWorkspace(source, tmp_path / "versions")
    for name in ("a.txt", "b.txt"):
        (tx.candidate / name).write_text("candidate", encoding="utf-8")
    tx.seal()
    real_replace = workspace._replace_file

    def replace_with_failure(src, dst):
        if dst.name == "b.txt":
            (source / "b.txt").write_text("user edit", encoding="utf-8")
            raise OSError("locked file")
        real_replace(src, dst)

    monkeypatch.setattr(workspace, "_replace_file", replace_with_failure)
    with pytest.raises(OSError, match="locked file"):
        tx.promote()
    assert (source / "a.txt").read_text(encoding="utf-8") == "original"
    assert (source / "b.txt").read_text(encoding="utf-8") == "user edit"
    assert tx.state == "rolled_back"


def test_quantified_role_tools_are_local_and_auditor_is_read_only(tmp_path):
    from types import SimpleNamespace
    from mewcode.expert_group import derive_execution_policy
    from mewcode.expert_web import _role_registry
    from mewcode.tools import ToolRegistry
    from mewcode.tools.bash import Bash
    from mewcode.tools.read_file import ReadFile
    from mewcode.tools.write_file import WriteFile
    from dataclasses import replace

    registry = ToolRegistry()
    for tool in (Bash(), ReadFile(), WriteFile()):
        registry.register(tool)
    session = SimpleNamespace(agent=SimpleNamespace(registry=registry))
    policy = replace(derive_execution_policy(False), work_dir=str(tmp_path), audit_contract={
        "artifact_dir": str(tmp_path), "rubric": {}, "artifact_hash": "test",
    })
    auditor = _role_registry(session, "auditor", policy)
    assert auditor.get("ReadAuditArtifact") is not None
    assert auditor.get("Bash") is None and auditor.get("WriteFile") is None
    assert auditor.get("RunVerification") is None
    executor = _role_registry(session, "executor", replace(policy, audit_contract=None))
    assert executor.get("Bash") is not None and executor.get("WriteFile") is not None
