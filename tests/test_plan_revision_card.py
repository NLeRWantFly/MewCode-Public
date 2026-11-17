from __future__ import annotations

import copy
from pathlib import Path

import pytest

from mewcode.experts.audit_protocol import freeze_rubric
from tests.test_audit_protocol import flow_spec
from tests.test_expert_retry import blocked_run


def test_revision_card_is_wired_in_desktop_ui():
    source = (Path(__file__).resolve().parents[1] / "frontend/static/app.js").read_text(encoding="utf-8")
    html = (Path(__file__).resolve().parents[1] / "frontend/static/index.html").read_text(encoding="utf-8")
    assert 'id="plan-revision-panel"' in html
    assert "plan_revision_response" in source
    assert "sendPlanRevision" in source


def test_resume_without_card_is_rejected_when_revision_pending(tmp_path):
    store, run_id, flow = blocked_run(tmp_path)
    store.mutate(run_id, lambda snap: snap.update(
        pending_recovery_revision={"summary": "拆分图示验收", "change_kind": "threshold_or_scope",
                                   "affected_nodes": ["audit", "repair"],
                                   "rubric_patch": [{"criterion_id": "FIG-LEGEND", "severity": "major",
                                                     "acceptance_test": "图例正确"},
                                                    {"criterion_id": "FIG-SCENE", "severity": "blocking",
                                                     "acceptance_test": "场景可追溯"}]},
    ))
    with pytest.raises(ValueError, match="修订卡"):
        store.resume(run_id, strategy="stable")


def test_reject_keeps_paused_work_and_blocks_second_proposal(tmp_path):
    store, run_id, _ = blocked_run(tmp_path)
    store.mutate(run_id, lambda snap: snap.update(
        pending_recovery_revision={"summary": "拆分", "change_kind": "threshold_or_scope",
                                   "affected_nodes": ["audit"]},
        audit_protocols={"audit": {
            **((snap.get("audit_protocols") or {}).get("audit") or {}),
            "transaction_dir": "keep-me",
            "changed_files": ["paper.txt"],
            "recovery_proposal_count": 1,
            "pending_recovery_revision": {"summary": "拆分"},
        }},
    ))
    rejected = store.reject_recovery_revision(run_id)
    assert rejected["status"] == "blocked"
    assert rejected["recovery_decision"] == "reject"
    assert "pending_recovery_revision" not in rejected
    assert rejected["audit_protocols"]["audit"]["transaction_dir"] == "keep-me"
    assert rejected["audit_protocols"]["audit"]["changed_files"] == ["paper.txt"]


def test_approve_split_drops_old_pass_and_keeps_predecessors(tmp_path):
    store, run_id, flow = blocked_run(tmp_path)
    saved = freeze_rubric(
        __import__("mewcode.experts.flow", fromlist=["ExpertFlowSpec"]).ExpertFlowSpec.from_value(flow).node_map["audit"],
        __import__("mewcode.experts.flow", fromlist=["ExpertFlowSpec"]).ExpertFlowSpec.from_value(flow),
    )
    patch = [
        {"criterion_id": "FIG-LEGEND", "severity": "major", "acceptance_test": "GT/预测图例正确"},
        {"criterion_id": "FIG-SCENE", "severity": "blocking", "acceptance_test": "场景标签可追溯"},
        saved["criteria"][1],
        saved["criteria"][2],
    ]
    store.mutate(run_id, lambda snap: snap.update(
        pending_recovery_revision={
            "summary": "拆分图示验收",
            "change_kind": "threshold_or_scope",
            "affected_nodes": ["audit", "repair"],
            "execution_scope": "只重评该审计/修复对",
            "rubric_patch": patch,
        },
    ))
    store.mutate(run_id, lambda snap: snap["audit_protocols"]["audit"].update(
        rubric=saved, accepted={"rubric_hash": saved["rubric_hash"], "checks": [{"state": "PASS"}]},
        score=0, baseline_result={"text": "old pass"},
    ))
    before_prepare = copy.deepcopy(store.load(run_id)["node_states"]["prepare"])
    resumed = store.resume(run_id, strategy="stable", recovery_action="approve")
    protocol = resumed["audit_protocols"]["audit"]
    assert protocol["rubric"]["rubric_hash"] != saved["rubric_hash"]
    assert "accepted" not in protocol
    assert "baseline_result" not in protocol
    assert protocol["recovery_decision"] == "approve"
    assert resumed["node_states"]["prepare"] == before_prepare
    assert resumed["locked_flow"]["meta"]["audit_rubrics"]["audit"][0]["criterion_id"] == "FIG-LEGEND"
    assert resumed.get("recovery_locked") is True


def test_supply_info_keeps_same_rubric_hash(tmp_path):
    store, run_id, flow = blocked_run(tmp_path)
    from mewcode.experts.flow import ExpertFlowSpec
    spec = ExpertFlowSpec.from_value(flow)
    saved = freeze_rubric(spec.node_map["audit"], spec)
    store.mutate(run_id, lambda snap: snap.update(
        pending_recovery_revision={"summary": "请提供场景对照", "change_kind": "",
                                   "affected_nodes": ["audit"], "rubric_patch": None},
    ))
    store.mutate(run_id, lambda snap: snap["audit_protocols"]["audit"].update(rubric=saved))
    resumed = store.resume(
        run_id, strategy="stable", recovery_action="supply_info", recovery_info="室内/室外对照见表1",
    )
    assert resumed["audit_protocols"]["audit"]["rubric"]["rubric_hash"] == saved["rubric_hash"]
    assert resumed["audit_protocols"]["audit"]["no_progress"] == 0
    assert any("室内/室外" in item.get("text", "") for item in resumed["supplemental_requirements"])


def test_approve_does_not_refill_budget_unless_granted(tmp_path):
    store, run_id, _ = blocked_run(tmp_path)
    store.mutate(run_id, lambda snap: snap.update(
        pending_recovery_revision={"summary": "拆分", "change_kind": "wording",
                                   "affected_nodes": ["audit"],
                                   "rubric_patch": [{"criterion_id": "REQ-01", "severity": "blocking",
                                                     "acceptance_test": "图例正确"}]},
    ))
    store.mutate(run_id, lambda snap: snap["audit_protocols"]["audit"].update(
        repair_budget={"round": 3, "iterations_used": 40, "elapsed_seconds": 100, "exhausted": True},
        rubric={"version": 1, "node_id": "audit", "scope": "x", "criteria": [
            {"criterion_id": "REQ-01", "severity": "blocking", "acceptance_test": "旧"}],
            "rubric_hash": "h1"},
    ))
    resumed = store.resume(run_id, strategy="stable", recovery_action="approve")
    assert resumed["audit_protocols"]["audit"]["repair_budget"]["exhausted"] is True
    store.mutate(run_id, lambda snap: snap.update(
        status="blocked",
        pending_recovery_revision={"summary": "拆分", "change_kind": "wording",
                                   "affected_nodes": ["audit"],
                                   "rubric_patch": [{"criterion_id": "REQ-01", "severity": "blocking",
                                                     "acceptance_test": "图例正确"}]},
    ))
    granted = store.resume(run_id, strategy="stable", recovery_action="approve", grant_extra_budget=True)
    assert "repair_budget" not in granted["audit_protocols"]["audit"]


def test_sync_locked_rubrics_is_opt_in(tmp_path):
    from mewcode.experts.flow import ExpertFlowStore
    from mewcode.orchestration.convert import to_expert_flow
    from mewcode.orchestration.plan_session import PlanSessionStore, build_plan_session
    from mewcode.orchestration.revisions import sync_locked_rubrics_to_saved_plan
    from mewcode.orchestration.spec import OrchestrationSpec

    spec = OrchestrationSpec.from_value({
        "spec_id": "editable", "label": "实验流程", "version": 1,
        "require_confirm_gate": False, "finalizer_node_id": "experiment",
        "meta": {"audit_rubrics": {"audit": [
            {"criterion_id": "REQ-01", "severity": "blocking", "acceptance_test": "场景与图像一致"},
        ]}},
        "nodes": [
            {"node_id": "prepare", "kind": "plan", "label": "准备", "goal": "准备",
             "model": {"provider_key": "p", "model_id": "prepare-model"}},
            {"node_id": "experiment", "kind": "reduce", "label": "修改实验", "goal": "实验",
             "model": {"provider_key": "p", "model_id": "old-model"}},
        ],
        "edges": [{"from_node": "prepare", "to_node": "experiment"}],
    })
    project = tmp_path / "project"
    project.mkdir()
    plans = PlanSessionStore(tmp_path / "plans.json")
    flows = ExpertFlowStore(tmp_path / "flows.json")
    plans.put(project, build_plan_session(spec, draft_plan=True))
    flows.put(project, to_expert_flow(spec))
    locked = to_expert_flow(spec).as_dict()
    locked["meta"] = {
        "audit_rubrics": {"audit": [
            {"criterion_id": "FIG-LEGEND", "severity": "major", "acceptance_test": "图例正确"},
            {"criterion_id": "FIG-SCENE", "severity": "blocking", "acceptance_test": "场景可追溯"},
        ]},
    }
    assert plans.get(project).spec.as_dict()["meta"]["audit_rubrics"]["audit"][0]["criterion_id"] == "REQ-01"
    synced = sync_locked_rubrics_to_saved_plan(project, locked, flow_store=flows, plan_store=plans)
    assert synced.meta["audit_rubrics"]["audit"][0]["criterion_id"] == "FIG-LEGEND"
    assert plans.get(project).spec.as_dict()["meta"]["audit_rubrics"]["audit"][0]["criterion_id"] == "FIG-LEGEND"
    assert flows.get(project).meta["audit_rubrics"]["audit"][0]["criterion_id"] == "FIG-LEGEND"


def test_approved_revision_keeps_command_approval_separate(tmp_path):
    from mewcode.experts.recovery_scope import bind_approved_revision, reset_approved_revision
    from mewcode.permissions.checker import PermissionChecker
    from mewcode.permissions.dangerous import DangerousCommandDetector
    from mewcode.permissions.modes import PermissionMode
    from mewcode.permissions.rules import RuleEngine
    from mewcode.permissions.sandbox import PathSandbox
    from mewcode.tools.bash import Bash
    from mewcode.tools.read_file import ReadFile
    from mewcode.tools.write_file import WriteFile

    checker = PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(str(tmp_path)),
        rule_engine=RuleEngine(),
        mode=PermissionMode.DEFAULT,
    )
    target = tmp_path / "paper.txt"
    target.write_text("old", encoding="utf-8")
    token = bind_approved_revision({"execution_scope": "只重评该审计/修复对"})
    try:
        read = checker.check(ReadFile(), {"file_path": str(target)})
        write = checker.check(WriteFile(), {"file_path": str(target), "content": "new"})
        verify = checker.check(Bash(), {"command": "pytest -q"})
        assert read.source == "approved_revision" and read.effect == "allow"
        assert write.source == "approved_revision" and write.effect == "allow"
        # A plan revision is not approval to run arbitrary project tests.
        assert verify.source == "hitl" and verify.effect == "ask"
    finally:
        reset_approved_revision(token)
    asked = checker.check(WriteFile(), {"file_path": str(target), "content": "later"})
    assert asked.effect == "ask"
