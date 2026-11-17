from __future__ import annotations

from types import SimpleNamespace

from mewcode.experts.recovery_classify import (
    BLOCKER_COMMAND,
    BLOCKER_CRITERION,
    BLOCKER_EVIDENCE,
    BLOCKER_PROVIDER,
    CHANGE_METHOD,
    CHANGE_SCOPE,
)
from mewcode.experts.recovery_planner import MAX_PROPOSALS, build_proposal, proposal_budget_left, resolve_stall


def _node():
    return SimpleNamespace(
        node_id="stage_lrww81",
        failure_policy=SimpleNamespace(target_node_id="stage_ws0kln"),
    )


def test_figure_proposal_splits_legend_and_scene():
    state = {
        "rubric": {
            "node_id": "stage_lrww81",
            "criteria": [
                {"criterion_id": "REQ-01", "severity": "blocking",
                 "acceptance_test": "图5场景标签与图像内容一致"},
                {"criterion_id": "EVD-01", "severity": "major",
                 "acceptance_test": "每项通过结论都有可定位证据"},
            ],
        }
    }
    proposal = build_proposal(state, node=_node(), blocker="criterion_mismatch")
    assert proposal is not None
    ids = [item["criterion_id"] for item in proposal["rubric_patch"]]
    assert "FIG-SCENE" in ids
    assert any("图例" in item["acceptance_test"] for item in proposal["rubric_patch"])
    assert "批准" in proposal["execution_scope"] or "审计" in proposal["execution_scope"]
    assert proposal["change_kind"] == CHANGE_SCOPE


def test_method_proposal_is_check_method():
    state = {
        "rubric": {"criteria": [
            {"criterion_id": "FIG-01", "severity": "major",
             "acceptance_test": "图例正确，可用文件哈希核对"},
        ]}
    }
    proposal = build_proposal(state, node=_node(), blocker="criterion_mismatch")
    assert proposal["change_kind"] == CHANGE_METHOD
    assert "ReadAuditArtifact" in proposal["rubric_patch"][0]["acceptance_test"]


def test_planner_stops_after_one_proposal():
    state = {
        "recovery_proposal_count": MAX_PROPOSALS,
        "rubric": {"criteria": [
            {"criterion_id": "REQ-01", "severity": "blocking",
             "acceptance_test": "图5场景标签与图像内容一致"},
        ]},
        "accepted": {"checks": [{"criterion_id": "REQ-01", "state": "FAIL",
                                 "acceptance_test": "图5场景标签与图像内容一致",
                                 "evidence": "无法从图像确定场景"}]},
    }
    assert proposal_budget_left(state) is False
    resolved = resolve_stall(state, node=_node())
    assert resolved["action"] == "stop"
    assert "提案已用尽" in resolved["reason"]


def test_reject_does_not_allow_another_proposal():
    state = {"recovery_decision": "reject", "recovery_proposal_count": 0}
    assert proposal_budget_left(state) is False


def test_planner_accepts_legacy_snapshot_blockers_and_writes_canonical_kind():
    cases = [
        ("provider_failure", BLOCKER_PROVIDER, "retry_provider"),
        ("command_error", BLOCKER_COMMAND, "stop"),
        ("missing_evidence", BLOCKER_EVIDENCE, "await_revision"),
        ("criterion_mismatch", BLOCKER_CRITERION, "await_revision"),
    ]
    for legacy, canonical, action in cases:
        state = {
            "blocker": legacy,
            "rubric": {"criteria": [{"criterion_id": "REQ-01", "severity": "major",
                                      "acceptance_test": "结果可由指定来源核验"}]},
        }
        resolved = resolve_stall(state, node=_node())
        assert resolved["blocker"] == canonical
        assert state["blocker"] == canonical
        assert resolved["action"] == action


def test_build_proposal_accepts_legacy_explicit_blocker():
    state = {"rubric": {"criteria": [{"criterion_id": "REQ-01", "severity": "major",
                                       "acceptance_test": "结果可由指定来源核验"}]}}
    legacy = build_proposal(state, node=_node(), blocker="criterion_mismatch")
    canonical = build_proposal(state, node=_node(), blocker=BLOCKER_CRITERION)
    assert legacy == canonical
    assert legacy["blocker"] == BLOCKER_CRITERION
