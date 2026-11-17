"""Model-facing completion protocol stays complete and source-safe."""
import copy
import json

import pytest

from mewcode.orchestration.graph_completion_protocol import (
    COMPLETION_SYSTEM, CompletionParseError, CompletionRequestTooLarge,
    build_completion_request, normalize_proposal, parse_completion_response,
)


def entry(entry_id, kind, data=None, *, evidence="manual", state="active", sources=None):
    return {"id": entry_id, "kind": kind, "title": entry_id, "body": entry_id + " 正文",
            "state": state, "data": data or {}, "evidence": evidence, "sources": sources or []}


def doc(*entries):
    return {"schema_version": 1, "entries": list(entries), "ignored": []}


@pytest.mark.parametrize("phrase", [
    "只根据给定诊断和图补全结构", "不重画整图", "不执行任务", "输出只含 JSON",
    "新增依赖", "新增循环声明", "新增连接性节点（非确认节点）", "用户留空的产出、输入、验收、步骤",
    "回边条件标为 loop", "循环体成员", "decision / input / question",
    "不得删除或撤回任何条目", "类型、层级、名称、说明和非空产出/验收", "范围、预算、循环退出标准、来源",
    "不得新增或修改人工确认点（gate）", "不得写 reviewed 或 sources",
    "data.category=designed", "data.category=defaulted", "data.value 写默认值",
    "主题、文件路径、数量", "name/description/required/default", "绝不提问",
    "顺序不明确时采用最合理的顺序", "question.data.issue_type=business", "仅真实业务取舍或无法确定总目标可 blocking=true",
    "非空 rationale", "稳定的英文/数字编号", "同一提案里新增节点可与其依赖一起提交",
    '"upserts"', '"notes"', "不超过 300 字",
])
def test_system_prompt_contains_every_boundary_and_output_rule(phrase):
    assert phrase in COMPLETION_SYSTEM


def test_request_includes_all_active_facts_and_precise_edit_hints_without_quotes():
    document = doc(
        entry("goal", "goal", sources=[{"source_id": "s1", "quote": "SECRET_QUOTE"}]),
        entry("work", "node", {"node_kind": "implement", "inputs": [], "outputs": ["已有产出"],
                               "acceptance": [], "steps": ["已有步骤"]}),
        entry("full", "node", {"node_kind": "audit", "inputs": ["输入"], "outputs": ["输出"],
                               "acceptance": ["验收"], "steps": ["步骤"]}),
        entry("gate", "node", {"node_kind": "gate", "outputs": []}),
        entry("edge", "edge", {"from_node": "work", "to_node": "full", "condition": "always"}),
        entry("loop_edge", "edge", {"from_node": "full", "to_node": "work", "condition": "loop"}),
        entry("loop", "loop", {"loop_id": "L"}),
        entry("decision", "decision", {"category": "designed"}),
        entry("proposal", "node", {"node_kind": "plan"}, evidence="proposal"),
        entry("past", "progress"), entry("retired", "node", state="retired"),
        entry("conflict", "node", state="conflict"),
    )
    diagnostics = [{"code": "disconnected", "path": "nodes", "entry_ids": ["work"], "message": "图不连通"}]
    before = copy.deepcopy(document)
    text = build_completion_request(document, diagnostics)
    payload = json.loads(text)
    assert document == before and "SECRET_QUOTE" not in text
    assert payload["goal"] == "goal 正文" and payload["diagnostics"] == diagnostics
    entries = {row["id"]: row for row in payload["entries"]}
    assert "past" not in entries and "retired" not in entries
    assert entries["conflict"]["state"] == "conflict" and entries["conflict"]["editable"] == {}
    assert entries["goal"]["source_count"] == 1 and entries["goal"]["locked"] is True
    assert entries["work"]["editable"] == {"fill_empty": ["inputs", "acceptance", "model"]}
    assert entries["full"]["editable"] == {"fill_empty": ["model"]}
    assert entries["gate"]["editable"] == {}
    assert entries["edge"]["editable"] == {"condition_to": ["loop"]}
    assert entries["loop_edge"]["editable"] == {}
    assert entries["loop"]["editable"] == {"extend": ["node_ids", "reviewer_node_ids", "repair_node_ids"]}
    assert entries["decision"]["editable"] == {}
    assert entries["proposal"]["editable"] == {"structural": True}


def test_request_over_limit_reports_actual_and_allowed_lengths():
    document = doc(entry("goal", "goal"))
    text = build_completion_request(document, [])
    with pytest.raises(CompletionRequestTooLarge) as error:
        build_completion_request(document, [], max_chars=len(text) - 1)
    assert str(len(text)) in str(error.value) and str(len(text) - 1) in str(error.value)


def test_default_request_has_no_application_character_cap_or_truncation():
    fact = entry("goal", "goal")
    fact["body"] = "完整原始要求" * 40000
    payload = build_completion_request(doc(fact), [])
    assert len(payload) > 200000
    assert json.loads(payload)["entries"][0]["body"] == fact["body"]


@pytest.mark.parametrize("raw", [
    '{"upserts":[],"notes":""}',
    '```json\n{"upserts":[],"notes":"围栏"}\n```',
    '说明：以下是结果。\n{"upserts":[{"id":"a"}],"notes":"前后说明"}\n请查看。',
])
def test_parse_accepts_plain_fenced_and_surrounded_json(raw):
    value = parse_completion_response(raw)
    assert isinstance(value["upserts"], list) and isinstance(value["notes"], str)


@pytest.mark.parametrize("raw", [
    "没有 JSON", '{"notes":"缺列表"}', '{"upserts":[42],"notes":""}',
])
def test_parse_rejects_missing_or_malformed_upserts(raw):
    with pytest.raises(CompletionParseError):
        parse_completion_response(raw)


def test_normalize_downgrades_blocking_when_goal_or_node_exists_without_mutation():
    proposal = {"upserts": [{"id": "q", "kind": "question", "data": {"blocking": True},
                              "rationale": "确认"}], "notes": "保留"}
    before = copy.deepcopy(proposal)
    for document in (doc(entry("g", "goal")), doc(entry("n", "node"))):
        normalized, notes = normalize_proposal(document, proposal)
        assert normalized["upserts"][0]["data"]["blocking"] is False
        assert notes == [{"code": "blocking_downgraded", "entry_id": "q"}]
        assert normalized["notes"] == "保留" and proposal == before


def test_normalize_keeps_blocking_only_with_no_active_goal_or_node():
    proposal = {"upserts": [{"id": "q", "kind": "question", "data": {"blocking": True}}], "notes": ""}
    normalized, notes = normalize_proposal(doc(entry("old", "node", state="retired")), proposal)
    assert normalized == proposal and notes == [] and normalized is not proposal
