"""Bounded task-design coverage includes full history and explicit exclusions."""
import copy
import json

import pytest

from mewcode.orchestration.effective_history import apply_history_patch, coverage_sources, new_basis
from mewcode.orchestration.graph_review import (
    REVIEW_SYSTEM, build_review_request, review_coverage,
)
from mewcode.orchestration.workflow_draft import empty_document


def source(source_id, text, role="user", **extra):
    return {"id": source_id, "role": role, "kind": "message", "text": text,
            "line": 1, **extra}


def entry(entry_id, kind="goal", *, body=None, data=None):
    return {"id": entry_id, "kind": kind, "title": entry_id,
            "body": body if body is not None else entry_id,
            "state": "active", "data": data or {}, "sources": []}


def doc(*entries):
    return {"schema_version": 1, "entries": list(entries), "ignored": []}


class InspectingReview:
    def __init__(self, reply=None, fail_calls=()):
        self.calls = []
        self.reply = reply
        self.fail_calls = set(fail_calls)

    async def __call__(self, system, prompt):
        self.calls.append((system, prompt))
        call_number = len(self.calls)
        if call_number in self.fail_calls:
            raise RuntimeError("review failed")
        payload = json.loads(prompt)
        if callable(self.reply):
            return self.reply(payload, call_number)
        return self.reply or '{"gaps":[],"notes":""}'


def test_request_contains_full_mixed_message_and_current_exclusion_context():
    old = source("u0", "需要整理五层目录；运行错误：ValueError")
    current = source("u1", "最终采用 SQLite 并输出目录清单")
    sources = [old, current]
    basis = new_basis(empty_document(), ["u0", "u1"])
    old_entry = entry("database", "constraint", body="数据库使用 MySQL")
    old_entry["sources"] = [{"source_id": "u0", "quote": "需要整理五层目录"}]
    basis = apply_history_patch(basis,
        {"upserts": [old_entry], "ignored": [
            {"source_id": "u0", "quote": "运行错误：ValueError",
             "reason": "错误日志", "category": "runtime"}]}, [old], sources)
    current_entry = entry("database", "constraint", body="数据库使用 SQLite")
    current_entry["sources"] = [{"source_id": "u1", "quote": "最终采用 SQLite"}]
    basis = apply_history_patch(basis, {"upserts": [current_entry], "ignored": []}, [current], sources)

    selected = coverage_sources(basis, sources)
    request = json.loads(build_review_request(selected, basis["document"]))
    assert request["requirements"] == [
        {"source_id": "u0", "text": old["text"]},
        {"source_id": "u1", "text": current["text"]},
    ]
    assert any(row["status"] == "superseded" and row["body"] == "数据库使用 MySQL"
               for row in request["history_context"])
    assert any(row["status"] == "current" and row["body"] == "数据库使用 SQLite"
               for row in request["history_context"])
    assert any(row["status"] == "excluded" and row.get("quote") == "运行错误：ValueError"
               for row in request["history_context"])
    assert "history_context 标明当前方案" in REVIEW_SYSTEM


@pytest.mark.parametrize("boundary", [None, "current-user"])
def test_ignored_projection_uses_only_selected_user_sources(boundary):
    sources = [
        source("old-user", "旧业务要求；错误日志"),
        source("assistant-log", "只读检查进度", role="assistant"),
        source("assistant-material", "助手素材说明", role="assistant", purpose="workflow_material"),
        source("current-user", "保留完整正文；附运行报错"),
        source("control-user", "请修图", purpose="graph_repair_protocol"),
        source("future-user", "冻结范围之外的新要求"),
    ]
    basis = new_basis(doc(entry("goal")), [row["id"] for row in sources[:-1]])
    basis["document"]["ignored"] = [
        {"source_id": row["id"], "quote": row["text"], "reason": "历史记录", "partial": True}
        for row in sources
    ] + [{"source_id": "missing", "reason": "未知来源"}]
    before = copy.deepcopy((basis, sources))
    projected = coverage_sources(basis, sources, start_source_id=boundary)
    expected = [row for row in sources[:4] if row["role"] == "user"
                and (boundary is None or row["id"] == boundary)]
    assert [(row["id"], row["text"]) for row in projected] == [
        (row["id"], row["text"]) for row in expected]
    for selected in projected:
        ignored = [row for row in selected["coverage_context"] if row.get("kind") == "ignored"]
        assert [row["source_id"] for row in ignored] == [row["id"] for row in expected]
        assert all(row["partial"] is True for row in ignored)
    assert (basis, sources) == before


def test_nonuser_ignored_filter_preserves_adopted_entries_and_provenance():
    assistant = source("assistant-plan", "已采纳方案及原始引文", role="assistant")
    user = source("adopting-user", "采用该方案，并保留原始引文")
    refs = [{"source_id": assistant["id"], "quote": assistant["text"]}]
    entries = [entry(name, body="方案内容 " + name) for name in ("current", "retired", "manual")]
    for row in entries:
        row.update(sources=copy.deepcopy(refs), reviewed=True)
    entries[1]["state"] = "retired"
    basis = new_basis(doc(*entries), [assistant["id"], user["id"]])
    basis["excluded"]["manual"] = True
    basis["current_plan"] = {"source_ids": [assistant["id"]], "mode": "adopted",
        "reason": "用户采用", "adopted_by": [{"source_id": user["id"], "quote": "采用该方案"}]}
    old = entry("old", body="被替代的完整方案")
    old["sources"] = copy.deepcopy(refs)
    basis["superseded"] = [{"entry": old, "reason": "已被替代", "source_refs": copy.deepcopy(refs),
        "field_sources": {"body": copy.deepcopy(refs)}}]
    basis["entry_field_sources"] = {row["id"]: {"body": copy.deepcopy(refs)} for row in entries}
    basis["document"]["ignored"] = [{"source_id": assistant["id"], "reason": "助手消息非用户要求"},
        {"source_id": user["id"], "quote": "并保留原始引文", "partial": True, "reason": "混合来源"}]
    before = copy.deepcopy((basis, assistant, user))
    context = coverage_sources(basis, [assistant, user])[0]["coverage_context"]
    assert context[0] == {"status": "current_plan", **basis["current_plan"]}
    indexed = {row["entry_id"]: row for row in context if "entry_id" in row}
    assert {key: row["status"] for key, row in indexed.items()} == {
        "current": "current", "retired": "retired", "manual": "excluded", "old": "superseded"}
    for key, row in indexed.items():
        original = old if key == "old" else next(item for item in entries if item["id"] == key)
        assert row["body"] == original["body"]
        assert row["source_refs"] == refs
        assert row["field_sources"] == {"body": refs}
    assert [row["source_id"] for row in context if row.get("kind") == "ignored"] == [user["id"]]
    assert (basis, assistant, user) == before


@pytest.mark.asyncio
async def test_large_user_source_is_covered_in_bounded_contiguous_chunks():
    phrase = "最终要求：必须输出一份可读报告"
    text = "项目背景与过程说明。" + ("详细材料内容。" * 5000) + phrase
    user = source("u-large", text)

    def reply(payload, _call_number):
        requirement = payload["requirements"][0]
        if phrase in requirement["text"]:
            return json.dumps({"gaps": [{"source_id": "u-large", "quote": phrase,
                                         "summary": "缺少报告交付节点"}], "notes": "尾段已检查"})
        return '{"gaps":[],"notes":""}'

    reviewer = InspectingReview(reply)
    max_chars = 24000
    result = await review_coverage([user], doc(entry("goal")), ask_review=reviewer,
                                   max_chars=max_chars)

    assert not result["error"] and result["model_calls"] > 1
    assert result["gaps"] == [{"source_id": "u-large", "quote": phrase,
                               "summary": "缺少报告交付节点"}]
    assert result["batches"] and all(row["status"] == "reviewed" for row in result["batches"])
    assert len(result["batches"]) == len(reviewer.calls)

    ranges = []
    full_graph = None
    for (_, prompt), record in zip(reviewer.calls, result["batches"]):
        assert len(prompt) <= max_chars
        payload = json.loads(prompt)
        if full_graph is None:
            full_graph = payload["graph"]
        assert payload["graph"] == full_graph, "the full graph must appear in every batch"
        requirement = payload["requirements"][0]
        chunk = requirement["chunk"]
        assert chunk["count"] == result["batches"][0]["parts"][0]["chunk_count"]
        assert requirement["text"] == text[chunk["start"]:chunk["end"]]
        ranges.append((chunk["start"], chunk["end"]))
    ranges.sort()
    assert ranges[0][0] == 0 and ranges[-1][1] == len(text)
    assert all(start <= prior_end for (_, prior_end), (start, _) in zip(ranges, ranges[1:]))


@pytest.mark.asyncio
async def test_failed_batch_is_recorded_as_failure_while_successful_gaps_are_aggregated():
    first_text = "必须有目录清单。" + ("甲" * 28000)
    second_text = "必须生成可读报告。" + ("乙" * 28000)
    users = [source("u1", first_text), source("u2", second_text)]

    def reply(payload, call_number):
        requirements = payload["requirements"]
        if any("必须有目录清单。" in row["text"] for row in requirements):
            return json.dumps({"gaps": [{"source_id": "u1", "quote": "必须有目录清单。",
                                         "summary": "缺目录清单"}], "notes": "已发现目录缺口"})
        return '{"gaps":[],"notes":""}'

    reviewer = InspectingReview(reply, fail_calls={2})
    result = await review_coverage(users, doc(entry("goal")), ask_review=reviewer,
                                   max_chars=24000)
    assert result["error"] == "RuntimeError"
    assert result["model_calls"] == len(reviewer.calls) and result["model_calls"] > 1
    assert result["gaps"] == [{"source_id": "u1", "quote": "必须有目录清单。",
                               "summary": "缺目录清单"}]
    assert any(batch["status"] == "failed" and batch["error"] == "RuntimeError"
               for batch in result["batches"])
    assert all(len(prompt) <= 24000 for _, prompt in reviewer.calls)


def expand_history_citations(payload):
    result = copy.deepcopy(payload)
    if result.pop("history_text_refs", False):
        texts = result.pop("history_texts", {})

        def expand(item):
            if isinstance(item, dict):
                if set(item) == {"graph_text_path"}:
                    literal = result["graph"]
                    for part in item["graph_text_path"]:
                        literal = literal[part]
                    assert isinstance(literal, str)
                    return literal
                if set(item) == {"history_text_id"}:
                    return texts[item["history_text_id"]]
                return {key: expand(child) for key, child in item.items()}
            if isinstance(item, list):
                return [expand(child) for child in item]
            return item

        for key in ("history_context", "history_context_tables", "history_citations", "history_quote_prefixes"):
            if key in result:
                result[key] = expand(result[key])
    if "history_context_tables" in result:
        result["history_context"] = [
            {**dict(zip(table["columns"], row)), **table.get("shared", {})}
            for table in result.pop("history_context_tables") for row in table["rows"]
        ]
    if result.pop("history_field_sources_grouped", False):
        for row in result.get("history_context", []):
            if "field_source_groups" in row:
                row["field_sources"] = {
                    field: copy.deepcopy(group["references"])
                    for group in row.pop("field_source_groups") for field in group["fields"]
                }
    citations = result.pop("history_citations", {})
    prefixes = result.pop("history_quote_prefixes", {})
    for citation in citations.values():
        quote = citation.get("quote")
        if isinstance(quote, dict) and set(quote) == {"prefix_id", "suffix"}:
            citation["quote"] = prefixes[quote["prefix_id"]] + quote["suffix"]
    for row in result.get("history_context", []):
        fields = row.get("field_sources", {})
        for refs in [row.get("source_refs", []), *fields.values()]:
            for index, ref in enumerate(refs):
                if set(ref) == {"citation_id"}:
                    refs[index] = copy.deepcopy(citations[ref["citation_id"]])
    return result


def test_history_text_references_preserve_unicode_metadata_and_current_graph():
    from mewcode.orchestration.graph_review import _factor_history_texts, _encode_review_value

    shared = '当前图原文😀 "x"\\path\n' * 80
    historical = "仅历史中重复的完整原文，不是当前授权。" * 70
    refs = [{"source_id": "source-original", "quote": shared, "extra": {"raw": historical}},
            {"source_id": "source-other", "quote": historical, "extra": {"version": 2}}]
    context = [{"status": "retired", "body": shared, "source_refs": refs,
                "field_sources": {"body": refs}, "opaque": {"version": 3, "values": [None, True]}}]
    sources = [source("user", shared, coverage_context=context)]
    document = doc(entry("goal", body=shared))
    original = json.loads(build_review_request(sources, document))
    before = copy.deepcopy(original)
    factored = _factor_history_texts(original)
    assert factored["history_text_refs"] and factored["history_texts"]
    assert factored["history_context"][0]["body"] == {"graph_text_path": ["goal"]}
    assert expand_history_citations(factored) == original == before
    for key in ("requirements", "graph", "runtime_semantics"):
        assert factored[key] == original[key]
    # Force the full serializer through its earlier history encodings too.
    limit = len(_encode_review_value(factored, compact=True))
    encoded = json.loads(build_review_request(sources, document, max_chars=limit))
    assert encoded["history_text_refs"]
    assert expand_history_citations(encoded) == original


@pytest.mark.parametrize("extra", [
    {"history_text_refs": False}, {"history_texts": {"old": "preserve"}},
    {"history_context": [{"opaque": {"graph_text_path": ["unrelated"]}}]},
    {"history_context": [{"opaque": {"history_text_id": "unrelated"}}]},
])
def test_history_text_references_do_not_reinterpret_reserved_values(extra):
    from mewcode.orchestration.graph_review import _factor_history_texts

    value = {"graph": {"goal": "完整原文" * 100}, "history_context": [{"body": "完整原文" * 100}], **extra}
    before = copy.deepcopy(value)
    assert _factor_history_texts(value) is None
    assert value == before


def test_history_text_references_skip_nonprofitable_and_distinct_values():
    from mewcode.orchestration.graph_review import _factor_history_texts

    value = {"graph": {"unnecessarily-long-path-segment" * 10: "a" * 40},
             "history_context": [{"body": "a" * 40}, {"body": "b" * 1000}]}
    before = copy.deepcopy(value)
    assert _factor_history_texts(value) is None
    assert value == before


def test_history_quote_prefixes_preserve_distinct_sources_and_exact_characters():
    from mewcode.orchestration.graph_review import (
        _factor_history_citations, _factor_history_quote_prefixes, _encode_review_value,
    )

    prefix = '原图😀与公式 "x"\\path\n' * 150
    refs = [{"source_id": "source_"+str(i), "quote": prefix+suffix,
             "extra": {"version": i}} for i, suffix in enumerate(("采用甲。", "撤回乙。", "保留丙。"))]
    context = [{"status": status, "entry_id": str(i), "source_refs": [refs[i]],
                "field_sources": {"body": [refs[i]], "data.steps": [refs[i]]}}
               for i, status in enumerate(("current", "superseded", "retired"))]
    sources = [source("user", "完整要求不变", coverage_context=context)]
    document = doc(entry("goal"))
    original = json.loads(build_review_request(sources, document))
    before = copy.deepcopy(original)
    factored = _factor_history_citations(original)
    prefixed = _factor_history_quote_prefixes(factored)
    assert prefixed is not None and original == before
    assert expand_history_citations(prefixed) == original
    limit = len(_encode_review_value(prefixed, compact=True))
    assert limit < len(_encode_review_value(factored, compact=True))
    encoded = json.loads(build_review_request(sources, document, max_chars=limit))
    assert encoded["history_quote_prefixes"]
    assert encoded["requirements"] == original["requirements"]
    assert encoded["graph"] == original["graph"]
    assert expand_history_citations(encoded) == original


def test_history_quote_prefixes_skip_unhelpful_and_already_encoded_values():
    from mewcode.orchestration.graph_review import _factor_history_quote_prefixes

    for value in ({}, {"history_citations": {"c": {"quote": "短文"}}},
                  {"history_citations": {"a": {"quote": {"unexpected": "shape"}}}},
                  {"history_citations": {}, "history_quote_prefixes": {"existing": "preserve"}}):
        before = copy.deepcopy(value)
        assert _factor_history_quote_prefixes(value) is None
        assert value == before


@pytest.mark.asyncio
async def test_repeated_history_citations_fit_without_losing_context_or_user_text():
    reference = {"source_id": "original", "quote": '保留原页😀、公式与图块。"原文"\n' * 100,
                 "extra": {"version": 3}}
    context = [{"status": "retired", "entry_id": "old-plan", "body": "旧提案已撤回，不恢复为要求",
                "source_refs": [reference], "field_sources": {
                    name: [reference] for name in ("body", "data.inputs", "data.outputs", "data.steps", "data.acceptance")}}]
    user = source("u", "当前要求完整保留原页追溯。", coverage_context=context)
    document = doc(entry("goal"))
    original = copy.deepcopy((user, document))
    unbounded = json.loads(build_review_request([user], document))
    limit = 10000
    assert len(build_review_request([user], document)) > limit

    def reply(payload, _call_number):
        assert payload["history_citations"]
        assert expand_history_citations(payload) == unbounded
        return json.dumps({"gaps": [{"source_id": "u", "quote": "完整保留原页追溯",
                                     "summary": "缺少追溯验收"}]}, ensure_ascii=False)

    reviewer = InspectingReview(reply)
    result = await review_coverage([user], document, ask_review=reviewer, max_chars=limit)
    assert not result["error"] and result["model_calls"] == 1
    assert result["gaps"][0]["quote"] == "完整保留原页追溯"
    assert len(reviewer.calls[0][1]) <= limit
    assert result["batches"][0]["status"] == "reviewed"
    assert (user, document) == original


@pytest.mark.asyncio
async def test_factored_history_remains_complete_in_every_source_chunk():
    reference = {"source_id": "old", "quote": "保留旧方案被替代的证据。" * 100}
    context = [{"status": "superseded", "entry_id": "old-plan", "body": "旧方案已由新方案替代",
                "source_refs": [reference], "field_sources": {"body": [reference] * 8}}]
    text = "当前完整要求。" * 2200
    user = source("u-long", text, coverage_context=context)
    document = doc(entry("goal"))
    original = copy.deepcopy((user, document))
    reviewer = InspectingReview()
    result = await review_coverage([user], document, ask_review=reviewer, max_chars=10000)
    assert not result["error"] and result["model_calls"] > 1
    ranges = []
    for _, prompt in reviewer.calls:
        assert len(prompt) <= 10000
        payload = expand_history_citations(json.loads(prompt))
        assert payload["history_context"] == context
        requirement = payload["requirements"][0]
        chunk = requirement["chunk"]
        assert requirement["text"] == text[chunk["start"]:chunk["end"]]
        ranges.append((chunk["start"], chunk["end"]))
    assert ranges[0][0] == 0 and ranges[-1][1] == len(text)
    assert all(start <= prior_end for (_, prior_end), (start, _) in zip(ranges, ranges[1:]))
    assert (user, document) == original


@pytest.mark.asyncio
async def test_unique_oversized_history_still_fails_without_review_or_truncation():
    user = source("u", "完整原文", coverage_context=[
        {"status": "superseded", "body": "不可删除的历史内容" * 2000}])
    original = copy.deepcopy(user)
    reviewer = InspectingReview()
    result = await review_coverage([user], doc(entry("goal")), ask_review=reviewer, max_chars=10000)
    assert result["error"] == "ReviewRequestTooLarge"
    assert result["model_calls"] == 0 and not reviewer.calls
    assert user == original


@pytest.mark.asyncio
async def test_many_history_exclusions_fit_losslessly_in_source_batches():
    reference = {"source_id": "old", "quote": "保留旧方案的原始引文。" * 100}
    context = [{"status": "current", "body": "保留业务选择", "source_refs": [reference],
                "field_sources": {"body": [reference] * 8}}]
    context += [{"status": "excluded", "kind": "message", "category": "runtime", "quote": "",
                 "source_id": f"history-{index}", "partial": index % 2 == 0,
                 "reason": f"运行日志 {index} 不构成新要求"} for index in range(300)]
    context += [{"status": "superseded", "body": "旧方案已替代", "source_refs": [reference]}]
    text = "保留完整追溯及用户选择。😀\n" * 2000
    user = source("u-table", text, coverage_context=context)
    document = doc(entry("goal"))
    original = copy.deepcopy((user, document))
    expected = json.loads(build_review_request([user], document))
    reviewer = InspectingReview()
    result = await review_coverage([user], document, ask_review=reviewer, max_chars=24000)
    assert not result["error"] and result["model_calls"] > 1
    ranges = []
    for _, prompt in reviewer.calls:
        assert len(prompt) <= 24000
        encoded = json.loads(prompt)
        assert encoded["history_context_tables"] and encoded["history_citations"]
        expanded = expand_history_citations(encoded)
        assert expanded["history_context"] == expected["history_context"]
        assert expanded["graph"] == expected["graph"]
        assert expanded["runtime_semantics"] == expected["runtime_semantics"]
        requirement = expanded["requirements"][0]
        chunk = requirement["chunk"]
        assert requirement["text"] == text[chunk["start"]:chunk["end"]]
        ranges.append((chunk["start"], chunk["end"]))
    assert ranges[0][0] == 0 and ranges[-1][1] == len(text)
    assert all(start <= previous_end for (_, previous_end), (start, _) in zip(ranges, ranges[1:]))
    assert (user, document) == original


@pytest.mark.asyncio
async def test_repeated_field_provenance_fits_without_losing_field_ownership():
    from mewcode.orchestration.graph_review import _factor_history_citations, _table_history_context

    reference = {"source_id": "original", "quote": '完整原文😀、表格和公式。\n"保留"',
                 "extra": {"version": 3, "bounds": [2, 19]}}
    other = {"source_id": "revision", "quote": "保留后续修订证据"}
    refs = [reference, other, reference]
    context = [
        {"status": status, "entry_id": f"entry-{index}", "body": f"{status} 原文",
         "source_refs": refs, "field_sources": {f"data.field_{field}": refs for field in range(40)}}
        for index, status in enumerate(("current", "superseded", "retired", "excluded"))
    ]
    context.append({"status": "excluded", "field_sources": {}, "quote": "未采用内容仍保留"})
    user = source("u-fields", "保留全部内容及各字段来源，不恢复已排除方案。", coverage_context=context)
    document = doc(entry("worker", "node", data={
        "node_kind": "implement", "model": {"model_id": "original-model"},
        "steps": ["遵守人工门禁"], "acceptance": ["完整回读"],
    }))
    before = copy.deepcopy((user, document))
    expected = json.loads(build_review_request([user], document))
    old_encoding = _table_history_context(_factor_history_citations(expected))
    limit = len(json.dumps(old_encoding, ensure_ascii=False, separators=(",", ":"))) - 1

    def reply(payload, _call_number):
        assert payload["history_field_sources_grouped"] is True
        assert expand_history_citations(payload) == expected
        return '{"gaps":[]}'

    reviewer = InspectingReview(reply)
    result = await review_coverage([user], document, ask_review=reviewer, max_chars=limit)
    assert not result["error"] and result["model_calls"] == 1
    assert len(reviewer.calls[0][1]) <= limit
    assert (user, document) == before


@pytest.mark.parametrize("fields", [{"body": []}, {"body": {"opaque": True}}, {7: []}])
def test_field_grouping_leaves_unhelpful_or_unrecognized_shapes_unchanged(fields):
    from mewcode.orchestration.graph_review import _group_history_field_sources

    payload = {"history_context": [{"status": "current", "field_sources": fields}]}
    before = copy.deepcopy(payload)
    assert _group_history_field_sources(payload) is None
    assert payload == before


def test_field_grouping_does_not_overwrite_existing_reserved_metadata():
    from mewcode.orchestration.graph_review import _group_history_field_sources

    payload = {"history_context": [{
        "status": "retired", "field_source_groups": {"opaque": "original value"},
        "field_sources": {f"field-{index}": [{"citation_id": "same"}] for index in range(30)},
    }]}
    before = copy.deepcopy(payload)
    assert _group_history_field_sources(payload) is None
    assert payload == before


def test_review_batch_places_varying_requirements_after_the_stable_context():
    context = [{"status": "excluded", "quote": "保留未采用记录"}]
    document = doc(entry("goal"))
    first = build_review_request([source("u1", "第一组要求", coverage_context=context)], document)
    second = build_review_request([source("u2", "第二组要求", coverage_context=context)], document)
    assert first.rsplit('"requirements":', 1)[0] == second.rsplit('"requirements":', 1)[0]
    assert list(json.loads(first))[-3:] == ["runtime_semantics", "graph", "requirements"]
    assert json.loads(first)["requirements"] == [{"source_id": "u1", "text": "第一组要求"}]


@pytest.mark.parametrize("with_text_refs", [False, True])
def test_capacity_preflight_expands_grouped_fields_before_lossless_comparison(monkeypatch, with_text_refs):
    from types import SimpleNamespace
    from mewcode.orchestration import draft_sources, draft_store, effective_history
    from mewcode.tools.task_design import review_request_preflight

    context = []
    shared = "当前图的完整原文与历史状态分别保留😀。" * 1500
    for index in range(24):
        reference = {"source_id": f"history-{index}", "quote": f"第{index}条完整原文😀"}
        refs = [reference, reference, reference]
        context.append({
            "status": ("current", "superseded", "retired", "excluded")[index % 4],
            "body": shared if with_text_refs else f"保留第{index}条记录的归属与状态",
            "source_refs": refs,
            "field_sources": {f"data.field_{field}": refs for field in range(160)},
        })
        if with_text_refs:
            context[-1]["extra"] = {"version": index, "original_text": shared}
    user = source("u", "保留全文和全部字段来源。", coverage_context=context)
    document = doc(entry("goal", body=shared)) if with_text_refs else doc(entry("goal"))
    state = {
        "task_design": {"task_id": "task", "revision": 1},
        "candidate": {"id": "candidate", "sequence": 1, "document": document,
                      "history_basis": {"document": document}, "history_source_ids": ["u"]},
    }
    before = copy.deepcopy((state, user))
    checks = []
    blocks = SimpleNamespace(
        source_path="unused", project="unused", conversation_id="fixture",
        _check=lambda: checks.append(True),
    )
    monkeypatch.setattr(draft_sources, "read_sources", lambda path: ([user], [], {"complete": True}))
    monkeypatch.setattr(effective_history, "coverage_sources", lambda *args, **kwargs: [user])
    monkeypatch.setattr(draft_store.DraftStore, "read_state", lambda *args: copy.deepcopy(state))
    assert json.loads(build_review_request([user], document, max_chars=250000))[
        "history_field_sources_grouped"] is True
    if with_text_refs:
        assert json.loads(build_review_request([user], document, max_chars=250000))["history_text_refs"] is True

    result = review_request_preflight(blocks, state)
    assert result["ready"] and result["lossless_roundtrip"]
    assert result["all_requirement_ranges_covered"] and result["batch_count"] == 1
    assert result["model_calls"] == 0 and result["max_request_chars"] <= 250000
    assert checks == [True, True]
    assert (state, user) == before
