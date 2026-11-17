from __future__ import annotations

import pytest

from mewcode.orchestration.graph_completion import completion_contract
from mewcode.orchestration.workflow_draft import (
    ENTRY_DATA_FIELDS,
    DraftPatchError,
    apply_patch,
    apply_patch_fragments,
    empty_document,
    entry_contract,
    validate_entry,
)


def _source(source_id, text):
    return {"id": source_id, "role": "user", "text": text}


def _entry(entry_id, kind, source, **fields):
    text = source["text"]
    return {"id": entry_id, "kind": kind, "title": entry_id, "body": text,
            "state": "active", "data": {},
            "sources": [{"source_id": source["id"], "quote": text}], **fields}


def test_host_contract_covers_every_supported_entry_and_matches_whitelist():
    contract = entry_contract()
    assert set(contract["entry_kinds"]) == {
        "goal", "node", "edge", "loop", "constraint", "decision", "question",
        "input", "artifact", "progress",
    }
    assert contract["data_fields"] == {
        kind: sorted(fields) for kind, fields in ENTRY_DATA_FIELDS.items()
    }
    assert contract["field_types"]["decision"]["value"].startswith("字符串")
    assert "longer" not in contract["field_types"]["decision"]["value"]
    assert contract["top_level_fields"]["limits"]["body_chars"] == 6000
    assert "options" not in contract["data_fields"]["decision"]
    assert "options" in contract["data_fields"]["question"]

    completion = completion_contract()
    assert completion["data_fields"]["decision"] == contract["data_fields"]["decision"]
    assert "confirmed" not in completion["field_types"]["decision"]["category"]
    assert "gate" not in completion["field_types"]["node"]["node_kind"]
    assert not set(completion["data_fields"]["loop"]) & {
        "max_rounds", "token_budget", "time_budget_seconds", "max_iterations",
        "timeout_seconds", "max_stalled_rounds", "node_max_iterations",
        "reviewer_max_iterations",
    }


def test_decision_local_ocr_hardware_reports_unknown_field_and_size_separately():
    unknown = {"id": "decision_local_ocr_hardware", "kind": "decision",
               "title": "OCR hardware", "body": "设备约束的完整上下文" * 80,
               "data": {"category": "designed", "value": "先用本地 OCR", "options": ["A", "B"]},
               "sources": []}
    with pytest.raises(DraftPatchError) as unknown_error:
        validate_entry(unknown)
    assert unknown_error.value.diagnostic["entry_id"] == "decision_local_ocr_hardware"
    assert unknown_error.value.diagnostic["path"] == "data"
    assert unknown_error.value.diagnostic["code"] == "unsupported_data_field"
    assert unknown_error.value.diagnostic["allowed_fields"] == sorted(ENTRY_DATA_FIELDS["decision"])
    assert unknown_error.value.diagnostic["unsupported_fields"] == ["options"]

    oversized = {**unknown, "data": {"category": "designed", "value": "先用本地 OCR",
                                      "node_id": "n" * 12001}}
    with pytest.raises(DraftPatchError) as size_error:
        validate_entry(oversized)
    assert size_error.value.diagnostic["entry_id"] == "decision_local_ocr_hardware"
    assert size_error.value.diagnostic["path"] == "data"
    assert size_error.value.diagnostic["code"] == "data_too_large"
    assert size_error.value.diagnostic["measured_size"] > size_error.value.diagnostic["limit"]
    assert "不会截断" in str(size_error.value)

    valid = {**unknown, "body": "较长上下文 " * 250,
             "data": {"category": "designed", "value": "先用本地 OCR"}}
    assert len(validate_entry(valid)["body"]) > 500


def test_independent_valid_node_survives_malformed_decision_and_source_coverage_is_truthful():
    node_source = _source("s_node", "保留现有流程节点")
    decision_source = _source("s_decision", "本地 OCR 受硬件限制")
    node = _entry("keep_node", "node", node_source)
    decision = _entry("decision_local_ocr_hardware", "decision", decision_source,
                      data={"category": "designed", "value": ["本地 OCR"]})

    result = apply_patch_fragments(empty_document(), {"upserts": [node, decision], "ignored": []},
                                   [node_source, decision_source])

    assert [item["id"] for item in result.document["entries"]] == ["keep_node"]
    assert result.accepted_entry_ids == ["keep_node"]
    assert [item["id"] for item in result.accepted_patch["upserts"]] == ["keep_node"]
    assert result.pending_issues[0]["entry_id"] == "decision_local_ocr_hardware"
    assert result.pending_issues[0]["path"] == "data.value"
    assert result.pending_issues[0]["code"] == "invalid_type"
    assert result.coverage == {"total": 2, "covered": 1,
                               "covered_source_ids": ["s_node"],
                               "unresolved_source_ids": ["s_decision"]}


def test_scoped_malformed_decision_does_not_discard_its_valid_target_node():
    node_source = _source("s_node", "建立本地 OCR 硬件评估节点")
    decision_source = _source("s_decision", "本地 OCR 受硬件限制")
    node = _entry("hardware_assessment", "node", node_source)
    decision = _entry("hardware_choice", "decision", decision_source,
                      data={"category": "designed", "value": "待评估", "options": ["A", "B"],
                            "node_id": "hardware_assessment"})

    result = apply_patch_fragments(empty_document(), {"upserts": [decision, node], "ignored": []},
                                   [node_source, decision_source])

    assert [item["id"] for item in result.document["entries"]] == ["hardware_assessment"]
    assert result.accepted_entry_ids == ["hardware_assessment"]
    assert [(item["entry_id"], item["code"]) for item in result.pending_issues] == [
        ("hardware_choice", "unsupported_data_field")]
    assert result.coverage == {"total": 2, "covered": 1,
                               "covered_source_ids": ["s_node"],
                               "unresolved_source_ids": ["s_decision"]}


def test_invalid_target_keeps_dependent_edge_pending_as_one_dependency_group():
    source = _source("s_group", "节点和依赖仍需核对")
    target = _entry("new_node", "node", source,
                    data={"unsupported": "unknown data fields are never discarded"})
    edge = _entry("new_edge", "edge", source,
                  data={"from_node": "new_node", "to_node": "existing_node", "condition": "always"})
    existing = {"id": "existing_node", "kind": "node", "title": "Existing", "body": "",
                "state": "active", "data": {}, "sources": []}
    document = {"schema_version": 1, "entries": [existing], "ignored": []}

    result = apply_patch_fragments(document, {"upserts": [edge, target], "ignored": []}, [source])

    assert [item["id"] for item in result.document["entries"]] == ["existing_node"]
    assert result.accepted_entry_ids == []
    assert {issue["entry_id"] for issue in result.pending_issues} == {"new_node", "new_edge"}
    assert any(issue["entry_id"] == "new_edge" and issue["code"] == "dependency_pending"
               for issue in result.pending_issues)
    assert result.coverage["covered_source_ids"] == []
    assert result.coverage["unresolved_source_ids"] == ["s_group"]


def test_loop_dependencies_are_applied_target_first_without_partial_dangling_loop():
    source = _source("s_loop", "实现后复核并按验收标准返修")
    implementation = _entry("implement", "node", source,
                            data={"node_kind": "implement", "outputs": ["实现结果"], "acceptance": ["实现结果符合标准"]})
    review = _entry("review", "node", source,
                    data={"node_kind": "audit", "outputs": ["复核结论"], "acceptance": ["逐项核对标准"]})
    loop = _entry("quality_loop", "loop", source,
                  data={"loop_id": "quality_loop", "entry_node_id": "implement", "check_node_id": "review",
                        "node_ids": ["implement", "review"], "reviewer_node_ids": ["review"],
                        "exit_condition": "全部标准通过", "criteria": [
                            {"criterion_id": "C1", "acceptance_test": "实现结果符合标准"}],
                        "control_mode": "team"})

    result = apply_patch_fragments(empty_document(), {"upserts": [loop, review, implementation], "ignored": []}, [source])

    assert set(result.accepted_entry_ids) == {"implement", "review", "quality_loop"}
    assert {item["id"] for item in result.document["entries"]} == {"implement", "review", "quality_loop"}
    assert not result.pending_issues
    assert result.coverage["covered_source_ids"] == ["s_loop"]


def test_legacy_apply_patch_still_rejects_the_entire_mixed_patch_atomically():
    good_source = _source("s_good", "新增一个有效阶段")
    bad_source = _source("s_bad", "裁决使用了错误形状")
    good = _entry("valid_node", "node", good_source)
    bad = _entry("bad_decision", "decision", bad_source,
                 data={"category": "designed", "value": {"must": "be a string"}})
    original = empty_document()

    with pytest.raises(DraftPatchError) as error:
        apply_patch(original, {"upserts": [good, bad], "ignored": []}, [good_source, bad_source])

    assert error.value.diagnostic["entry_id"] == "bad_decision"
    assert original["entries"] == []
