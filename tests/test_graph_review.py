"""Independent coverage review is source-bound and never mutates the graph."""
import asyncio
import copy
import json

import pytest

from mewcode.orchestration.graph_completion_protocol import COMPLETION_SYSTEM
from mewcode.orchestration.graph_review import (
    REVIEW_SYSTEM, ReviewParseError, ReviewRequestTooLarge,
    build_review_request, gap_diagnostics, parse_review_response,
    review_coverage, verify_gaps,
)


def source(source_id, text, role="user", **extra):
    return {"id": source_id, "role": role, "kind": "message", "text": text,
            "line": 1, **extra}


def entry(entry_id, kind, data=None, *, state="active", body=None):
    return {"id": entry_id, "kind": kind, "title": entry_id,
            "body": body if body is not None else entry_id + " 正文",
            "state": state, "data": data or {}, "sources": [], "evidence": "manual"}


def document(*entries):
    return {"schema_version": 1, "entries": list(entries), "ignored": []}


class FakeReview:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def __call__(self, system, user):
        self.calls.append((system, user))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


@pytest.mark.parametrize("phrase", [
    "独立审查员，只找覆盖缺口", "不批准执行，不修改图，不重画",
    "requirements 是用户原话来源，graph 是当前图摘要",
    "用户明确提出、而图中没有对应的节点、约束、验收或确认点",
    "措辞风格、实现细节偏好、助手自己的建议",
    "已被节点/约束/验收覆盖", "已在 inputs 中声明为运行参数",
    "source_id", "从该来源原文逐字复制的引文，不得改写",
    "没有缺口就返回空数组，最多 10 条", "只输出 JSON",
    '"gaps"', '"quote"', '"summary"', '"suggested_fix"', '"notes"',
])
def test_review_system_declares_independent_boundary(phrase):
    assert phrase in REVIEW_SYSTEM
    assert REVIEW_SYSTEM != COMPLETION_SYSTEM
    assert "只根据给定诊断和图补全结构" not in REVIEW_SYSTEM
    assert "独立审查员，只找覆盖缺口" not in COMPLETION_SYSTEM


def test_build_review_request_filters_sources_and_summarizes_active_graph():
    sources = [source("u1", "最后导出 PDF"), source("a", "助手建议加一层", "assistant"),
               source("control", "修图请求", purpose="graph_repair_protocol"),
               source("u2", "删除前确认")]
    doc = document(
        entry("goal", "goal", body="完成报告"),
        entry("goal2", "goal", body="归档"),
        entry("work", "node", {"node_kind": "implement", "outputs": ["报告"],
                               "acceptance": ["检查通过"]}),
        entry("gate", "node", {"node_kind": "gate", "outputs": [], "acceptance": []}),
        entry("edge", "edge", {"from_node": "work", "to_node": "gate", "condition": "always"}),
        entry("loop", "loop", {"loop_id": "L", "entry_node_id": "work",
                               "check_node_id": "gate", "exit_condition": "通过"}),
        entry("limit", "constraint", {"node_id": "work"}, body="最多三轮"),
        entry("pdf", "artifact", {}, body="PDF 文件"),
        entry("assumption", "decision", {"category": "designed"}, body="默认先检查"),
        entry("uncategorized", "decision", {}, body="不属于假设"),
        entry("theme", "input", {"name": "主题"}),
        entry("question", "question", {}, body="是否归档"),
        entry("retired", "node", {"node_kind": "plan"}, state="retired"),
    )
    original = copy.deepcopy((sources, doc))
    payload = json.loads(build_review_request(sources, doc))
    assert payload["requirements"] == [{"source_id": "u1", "text": "最后导出 PDF"},
                                       {"source_id": "u2", "text": "删除前确认"}]
    graph = payload["graph"]
    assert graph["goal"] == "完成报告\n归档"
    assert graph["nodes"] == [
        {"id": "work", "title": "work", "kind": "implement", "gate": False,
         "goal": "work 正文", "inputs": [], "steps": [], "parent_id": "",
         "outputs": ["报告"], "acceptance": ["检查通过"]},
        {"id": "gate", "title": "gate", "kind": "gate", "gate": True,
         "goal": "gate 正文", "inputs": [], "steps": [], "parent_id": "",
         "outputs": [], "acceptance": []},
    ]
    assert graph["edges"] == [{"from": "work", "to": "gate", "condition": "always"}]
    assert graph["loops"] == [{"loop_id": "L", "entry": "work", "check": "gate",
                               "exit_condition": "通过"}]
    assert graph["constraints"] == [
        {"id": "limit", "kind": "constraint", "body": "最多三轮", "node_id": "work"},
        {"id": "pdf", "kind": "artifact", "body": "PDF 文件", "node_id": ""},
        {"id": "uncategorized", "kind": "decision", "body": "不属于假设", "node_id": ""},
    ]
    assert graph["ledger"] == {"assumptions": [{"body": "默认先检查", "category": "designed", "value": "", "node_id": ""}],
                               "inputs": [{"id": "theme", "name": "主题", "description": "", "required": True, "default": "",
                                           "collect_at":"start", "required_when":None}], "questions": ["是否归档"]}
    assert (sources, doc) == original


def test_review_preserves_parameter_binding_identity_and_required_loop_members():
    doc = document(
        entry("i_mapping", "input", {"name": "p_mapping", "description": "文档归属",
            "collect_at": "receive", "required": True, "default": None}),
        entry("receive", "node", {"inputs": ["p_mapping"],
            "input_bindings": {"p_mapping": "i_mapping"}}),
        entry("audit", "node", {"node_kind": "audit"}),
        entry("check", "node", {"node_kind": "decision"}),
        entry("loop", "loop", {"loop_id": "L", "entry_node_id": "receive",
            "check_node_id": "check", "node_ids": ["receive", "audit", "check"],
            "required_node_ids": ["audit"], "exit_condition": "本批审核完成"}),
        entry("old_mapping", "input", {"name": "p_old"}, state="retired"),
    )
    before = copy.deepcopy(doc)
    graph = json.loads(build_review_request([source("u", "逐批审核并保持归属")], doc))["graph"]
    inputs = {row["id"]: row for row in graph["ledger"]["inputs"]}
    for name, target in graph["nodes"][0]["input_bindings"].items():
        assert inputs[target]["name"] == name
    assert inputs["i_mapping"]["default"] is None
    assert inputs["i_mapping"]["collect_at"] == "receive"
    assert "old_mapping" not in inputs
    loop = graph["loops"][0]
    assert loop["required_node_ids"] == ["audit"]
    assert set(loop["required_node_ids"]) <= set(loop["node_ids"])
    assert doc == before


def test_build_review_request_reports_actual_size_without_truncation():
    sources = [source("u", "a" * 200)]
    original = json.loads(build_review_request(sources, document()))
    size = len(json.dumps(original, ensure_ascii=False, separators=(",", ":")))
    assert json.loads(build_review_request(sources, document(), max_chars=size)) == original
    with pytest.raises(ReviewRequestTooLarge) as error:
        build_review_request(sources, document(), max_chars=size - 1)
    assert str(size) in str(error.value) and str(size - 1) in str(error.value)


def test_review_binding_semantics_agree_with_transitive_host_validation():
    from mewcode.orchestration.workflow_draft import check_entry_refs

    doc = document(
        entry("producer", "node", {"outputs": ["原始交接"]}),
        entry("middle", "node"),
        entry("consumer", "node", {"inputs": ["原始交接"],
            "input_bindings": {"原始交接": "producer"}}),
        entry("first", "edge", {"from_node": "producer", "to_node": "middle", "condition": "always"}),
        entry("second", "edge", {"from_node": "middle", "to_node": "consumer", "condition": "always"}),
    )
    check_entry_refs(doc["entries"])
    payload = json.loads(build_review_request([source("u", "绑定须有前向依赖")], doc))
    rule = next(row["detail"] for row in payload["runtime_semantics"] if row["rule"] == "task_inputs")
    assert "允许经过中间节点，不要求生产者与消费者直接相连" in rule
    assert payload["graph"]["nodes"][2]["input_bindings"] == {"原始交接": "producer"}
    doc["entries"][-1]["data"]["condition"] = "loop"
    with pytest.raises(ValueError, match="非 loop 前向边"):
        check_entry_refs(doc["entries"])


def test_review_distinguishes_coordinator_from_bound_execution_child():
    coordinator = entry("organize", "node", {"node_kind":"implement", "execution_mode":"coordinate",
        "model":{"provider_key":"codex:test", "model_id":"gpt-6.1-sol", "effort":"xhigh"}})
    worker = entry("write", "node", {"node_kind":"implement", "parent_id":"organize",
        "model":{"provider_key":"desktop:test", "model_id":"deepseek-flash", "effort":"max"}})
    payload = json.loads(build_review_request([source("u", "由执行模型写库")], document(coordinator, worker)))
    parent, child = payload["graph"]["nodes"]
    assert parent["execution_mode"] == "coordinate"
    assert child["parent_id"] == parent["id"]
    assert parent["model"]["model_id"] != child["model"]["model_id"]
    assert any(row["rule"] == "coordination" for row in payload["runtime_semantics"])


@pytest.mark.parametrize("blocker", [
    "capability_missing", "insufficient_evidence", "semantic_risk", "missing_authorization",
])
def test_review_exposes_host_recovery_without_legalizing_unreachable_graph_resolvers(blocker):
    from types import SimpleNamespace

    from mewcode.kernel.blockers import policy
    from mewcode.kernel.capabilities import resolver_diagnostics

    scope = {"possible_blockers": [blocker], "resolvers": {}}
    audit = entry("audit", "node", {"node_kind": "audit", "capabilities": scope})
    payload = json.loads(build_review_request([source("u", "阻塞必须有真实恢复方式")], document(audit)))
    recovery = next(row for row in payload["runtime_semantics"] if row["rule"] == "blocker_recovery")
    actual = policy(blocker)
    assert recovery["policies"][blocker] == {
        "owner": actual.owner, "category": actual.category, "clearance": actual.clearance,
    }
    producer = SimpleNamespace(node_id="prepare", capabilities=None)
    consumer = SimpleNamespace(node_id="audit", capabilities=copy.deepcopy(scope))
    flow = SimpleNamespace(nodes=[producer, consumer], edges=[
        SimpleNamespace(from_node="prepare", to_node="audit"),
    ])
    assert resolver_diagnostics(flow) == []
    consumer.capabilities["resolvers"][blocker] = "prepare"
    issue, = resolver_diagnostics(flow)
    assert issue["code"] == "unreachable_blocker_resolver"
    assert issue["resolver"] == "prepare" and issue["blocker"] == blocker
    assert payload["graph"]["nodes"][0]["capabilities"] == scope


@pytest.mark.parametrize("raw", [
    '{"gaps":[],"notes":""}',
    '```json\n{"gaps":[],"notes":"围栏"}\n```',
    '审查如下：\n{"gaps":[],"notes":"说明"}\n以上是结果。',
])
def test_parse_review_response_accepts_plain_fenced_and_surrounded_json(raw):
    parsed = parse_review_response(raw)
    assert parsed["gaps"] == [] and parsed["truncated"] == 0


@pytest.mark.parametrize("raw", [
    "没有 JSON", '{"notes":"缺 gaps"}', '{"gaps":{}}',
    '{"gaps":[4]}', '{"gaps":[{"source_id":"u","quote":"原文"}]}',
    '{"gaps":[{"source_id":"u","quote":"原文","summary":"缺口","suggested_fix":4}]}',
])
def test_parse_review_response_rejects_malformed_gaps(raw):
    with pytest.raises(ReviewParseError):
        parse_review_response(raw)


def test_parse_review_response_truncates_count_and_display_fields_only():
    gap = {"source_id": "u", "quote": "q" * 1501,
           "summary": "s" * 305, "suggested_fix": "f" * 301}
    parsed = parse_review_response(json.dumps({"gaps": [gap] * 12, "notes": "完成"}))
    assert len(parsed["gaps"]) == 10 and parsed["truncated"] == 2
    assert len(parsed["gaps"][0]["quote"]) == 1501
    assert len(parsed["gaps"][0]["summary"]) == 300
    assert len(parsed["gaps"][0]["suggested_fix"]) == 300
    assert parsed["notes"] == "完成"


def test_verify_gaps_drops_four_invalid_sources_and_accepts_format_noise():
    sources = [source("u", "请**最后导出 PDF，**再归档。"),
               source("a", "助手建议", role="assistant"),
               source("control", "请修图", purpose="graph_repair_protocol")]
    gaps = [{"source_id": "missing", "quote": "请", "summary": "缺"},
            {"source_id": "a", "quote": "助手建议", "summary": "缺"},
            {"source_id": "control", "quote": "请修图", "summary": "缺"},
            {"source_id": "u", "quote": "凭空捏造", "summary": "缺"},
            {"source_id": "u", "quote": "最后导出 PDF,再归档", "summary": "缺 PDF"}]
    verified, dropped = verify_gaps(gaps, sources)
    assert verified == [gaps[-1]]
    assert dropped == [
        {"source_id": "missing", "reason": "unknown_source"},
        {"source_id": "a", "reason": "not_user_source"},
        {"source_id": "control", "reason": "control_source"},
        {"source_id": "u", "reason": "quote_not_found"},
    ]


def test_gap_diagnostics_preserves_required_shape_and_bounds_quote():
    gap = {"source_id": "u", "quote": "甲" * 130, "summary": "没有 PDF",
           "suggested_fix": "添加导出节点"}
    assert gap_diagnostics([gap]) == [{
        "code": "coverage_gap", "path": "sources/u", "entry_ids": [],
        "message": f'用户要求「{"甲" * 120}」在图中没有对应内容：没有 PDF；建议：添加导出节点',
    }]


@pytest.mark.asyncio
async def test_review_coverage_uses_independent_model_and_does_not_mutate_inputs():
    sources = [source("u", "最后导出 PDF")]
    doc = document(entry("goal", "goal"))
    original = copy.deepcopy((sources, doc))
    reviewer = FakeReview(json.dumps({"gaps": [
        {"source_id": "u", "quote": "最后导出 PDF", "summary": "缺少导出节点"},
        {"source_id": "no", "quote": "捏造", "summary": "假缺口"},
    ], "notes": "一处缺口"}))
    completion = FakeReview("unused")
    result = await review_coverage(sources, doc, ask_review=reviewer)
    assert result == {"gaps": [{"source_id": "u", "quote": "最后导出 PDF",
                                "summary": "缺少导出节点"}],
                      "dropped": [{"source_id": "no", "reason": "unknown_source"}],
                      "truncated": 0, "model_calls": 1, "skipped": "", "error": "", "notes": "一处缺口"}
    assert len(reviewer.calls) == 1 and reviewer.calls[0][0] == REVIEW_SYSTEM
    assert completion.calls == [] and (sources, doc) == original


@pytest.mark.asyncio
async def test_review_coverage_skips_without_usable_requirements():
    reviewer = FakeReview("unused")
    result = await review_coverage([source("a", "建议", role="assistant"),
                                    source("c", "修图", purpose="graph_repair_protocol")],
                                   document(), ask_review=reviewer)
    assert result["skipped"] == "no_requirements" and result["model_calls"] == 0
    assert reviewer.calls == []


@pytest.mark.asyncio
async def test_review_coverage_accepts_large_input_and_reports_errors_without_leaks():
    reviewer = FakeReview('{"gaps":[],"notes":"完整核对"}')
    large = await review_coverage([source("u", "a" * 210000)], document(), ask_review=reviewer)
    assert large["error"] == "" and large["model_calls"] == 1
    assert "a" * 210000 in reviewer.calls[0][1]
    for response, error in [("bad json", "ReviewParseError"),
                            (RuntimeError("credential=secret"), "RuntimeError")]:
        result = await review_coverage([source("u", "真实要求")], document(),
                                       ask_review=FakeReview(response))
        assert result["error"] == error and result["gaps"] == []
        assert result["model_calls"] == 1 and "secret" not in str(result)


@pytest.mark.asyncio
async def test_review_coverage_propagates_cancellation():
    with pytest.raises(asyncio.CancelledError):
        await review_coverage([source("u", "真实要求")], document(),
                              ask_review=FakeReview(asyncio.CancelledError()))
