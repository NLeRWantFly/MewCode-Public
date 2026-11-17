from __future__ import annotations

from types import SimpleNamespace

from mewcode.experts.recovery_classify import (
    DIAGNOSIS_KINDS,
    BLOCKER_COMMAND,
    BLOCKER_CRITERION,
    BLOCKER_EVIDENCE,
    BLOCKER_PROVIDER,
    CHANGE_METHOD,
    CHANGE_SCOPE,
    CHANGE_WORDING,
    classify_blocker,
    classify_criterion_change,
    format_stop_reason,
    looks_like_provider_failure,
    normalize_blocker,
)


def test_format_stop_reason_uses_fixed_labels():
    text = format_stop_reason(BLOCKER_CRITERION, "不要再修一次")
    assert "验收契约不匹配" in text
    assert "再修一次" in text
    assert "供应失败" in format_stop_reason("provider_failure", "保留暂停")


def test_legacy_blocker_labels_normalize_to_unique_diagnosis_kinds():
    mappings = {
        "provider_failure": BLOCKER_PROVIDER,
        "command_error": BLOCKER_COMMAND,
        "missing_evidence": BLOCKER_EVIDENCE,
        "criterion_mismatch": BLOCKER_CRITERION,
    }
    assert len(set(mappings.values())) == len(mappings)
    for legacy, canonical in mappings.items():
        assert canonical in DIAGNOSIS_KINDS
        assert normalize_blocker(legacy) == canonical
        assert normalize_blocker(canonical) == canonical
        assert classify_blocker({"blocker": legacy}) == canonical


def test_provider_failure_from_api_status():
    assert looks_like_provider_failure("API error (502): upstream", status="failed")
    assert not looks_like_provider_failure("API error (401): unauthorized", status="failed")


def test_classify_missing_scene_evidence():
    state = {
        "accepted": {
            "checks": [{
                "criterion_id": "REQ-01", "state": "UNKNOWN",
                "acceptance_test": "图5场景标签与图像内容一致",
                "evidence": "灰度图无法确认场景",
            }]
        }
    }
    assert classify_blocker(state) == BLOCKER_EVIDENCE


def test_classify_scene_fail_is_criterion_mismatch():
    state = {
        "rubric": {"criteria": [{
            "criterion_id": "REQ-01",
            "acceptance_test": "图5场景标签与图像内容一致",
        }]},
        "accepted": {
            "checks": [{
                "criterion_id": "REQ-01", "state": "FAIL",
                "acceptance_test": "图5场景标签与图像内容一致",
                "evidence": "图像未能建立场景类别",
            }]
        },
    }
    assert classify_blocker(state) == BLOCKER_CRITERION


def test_classify_command_error_from_tool_output():
    repaired = SimpleNamespace(
        status="failed",
        text="",
        verification_records=[{"command": "python missing.png", "is_error": True,
                               "output": "No such file or directory"}],
        activity_records=[],
    )
    assert classify_blocker({}, repaired) == BLOCKER_COMMAND


def test_classify_provider_overrides_stall():
    repaired = SimpleNamespace(
        status="failed", text="API error (429): rate limit",
        verification_records=[], activity_records=[],
    )
    assert classify_blocker({"accepted": {"checks": []}}, repaired) == BLOCKER_PROVIDER


def test_classify_criterion_change_kinds():
    old = [{"criterion_id": "FIG-01", "severity": "major",
            "acceptance_test": "图例正确，可用文件哈希核对"}]
    method = [{"criterion_id": "FIG-01", "severity": "major",
               "acceptance_test": "图例正确，必须通过 ReadAuditArtifact 读取像素"}]
    wording = [{"criterion_id": "FIG-01", "severity": "major",
                "acceptance_test": "图例含义写清楚，不混淆 GT 与预测"}]
    scope = [
        {"criterion_id": "FIG-LEGEND", "severity": "major", "acceptance_test": "图例正确"},
        {"criterion_id": "FIG-SCENE", "severity": "blocking", "acceptance_test": "场景可追溯"},
    ]
    assert classify_criterion_change(old, method) == CHANGE_METHOD
    assert classify_criterion_change(old, wording) == CHANGE_WORDING
    assert classify_criterion_change(old, scope) == CHANGE_SCOPE
