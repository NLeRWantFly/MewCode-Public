from __future__ import annotations

import copy

from mewcode.orchestration.planning_issues import (
    PREPARATION_QUESTION_GUIDANCE,
    triage_preparation_questions,
)


def _question(question_id, title, body, quote, *, issue_type="unclassified",
              evidence="proposal", options=None, **extra):
    data = {"issue_type": issue_type, "blocking": True}
    if options is not None:
        data["options"] = options
    data.update(extra)
    return {"id": question_id, "kind": "question", "title": title, "body": body,
            "state": "active", "evidence": evidence, "data": data,
            "sources": [{"source_id": f"source_{question_id}", "quote": quote}]}


def test_glm_ocr_route_gap_stays_unresolved_nonblocking_capability_question():
    question = _question(
        "ocr_service", "GLM-OCR 接入", "GLM-OCR 服务不在 available_model_routes；需要核实独立服务配置。",
        "扫描件需要 GLM-OCR 工具服务读取页面。", issue_type="business")
    document = {"entries": [question], "ignored": []}
    before = copy.deepcopy(document)

    result = triage_preparation_questions(document)

    assert document == before
    assert result is not document
    row = result["entries"][0]
    assert row["data"]["issue_type"] == "capability"
    assert row["data"]["blocking"] is False
    assert row["state"] == "active"
    assert row["sources"] == question["sources"]
    assert "options" not in row["data"]
    assert "ready" in PREPARATION_QUESTION_GUIDANCE
    assert "available_model_routes" in PREPARATION_QUESTION_GUIDANCE


def test_missing_physical_page_batches_are_runtime_input_and_items_are_not_invented():
    question = _question(
        "page_batches", "物理页批次", "foreach.items 还没有实际页码，不能形成可运行批次。",
        "请按 PDF 的物理页码分批审核并保留页范围。", issue_type="business")
    result = triage_preparation_questions({"entries": [question], "ignored": []})

    row = result["entries"][0]
    assert row["data"]["issue_type"] == "input"
    assert row["data"]["blocking"] is False
    assert "items" not in row["data"]
    assert row["state"] == "active"
    assert row["sources"] == question["sources"]


def test_bare_foreach_schema_gap_is_technical_preparation():
    question = _question(
        "foreach_shape", "批次定义", "运行要求 foreach.items，但当前未提供 item_id/goal 清单。",
        "foreach 模式必须提供 items。")

    result = triage_preparation_questions({"entries": [question]})

    assert result["entries"][0]["data"]["issue_type"] == "technical"
    assert result["entries"][0]["data"]["blocking"] is False
    assert result["entries"][0]["sources"] == question["sources"]


def test_user_manual_reviewed_and_option_bearing_questions_are_preserved():
    ocr_quote = "需核对 GLM-OCR 服务与模型路由。"
    batch_quote = "根据物理页码建立 foreach.items。"
    rows = [
        _question("user_authored", "GLM-OCR", "服务配置待核对。", ocr_quote,
                  evidence="user_source"),
        _question("manual", "页码批次", "批次未填写。", batch_quote,
                  evidence="manual"),
        {**_question("reviewed", "GLM-OCR", "服务配置待核对。", ocr_quote), "reviewed": True},
        _question("business_options", "OCR服务选择", "请选择产品方向。", ocr_quote,
                  options=[{"value": "local", "label": "本地处理"},
                           {"value": "hosted", "label": "托管服务"}]),
    ]
    document = {"entries": rows, "ignored": []}

    result = triage_preparation_questions(document)

    assert result == document
    assert result is not document


def test_unrelated_unclassified_question_and_unanchored_ocr_are_not_blanket_reclassified():
    unknown = _question("unknown", "确认范围", "范围还需讨论。", "请确认项目范围。")
    no_source = _question("ocr_without_source", "OCR配置", "GLM-OCR 不在 available_model_routes。", "")
    no_source["sources"] = []
    route_available = _question("ocr_available", "OCR服务取舍",
                                 "GLM-OCR 已在 available_model_routes 中，是否采用？",
                                 "可用路由包含 GLM-OCR。")

    result = triage_preparation_questions({"entries": [unknown, no_source, route_available]})

    assert [entry["data"]["issue_type"] for entry in result["entries"]] == [
        "unclassified", "unclassified", "unclassified"
    ]
    assert all(entry["data"]["blocking"] is True for entry in result["entries"])


def test_host_generated_marker_in_source_reference_can_identify_ai_material():
    question = _question("host_ocr", "OCR配置", "服务需要核实。",
                         "GLM-OCR 工具服务未配置。", evidence="proposal")

    row = triage_preparation_questions({"entries": [question]})["entries"][0]

    assert row["data"]["issue_type"] == "capability"
    assert row["data"]["blocking"] is False
