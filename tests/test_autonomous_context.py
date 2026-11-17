from __future__ import annotations

import builtins
import copy
import hashlib
import importlib
import json

import pytest

from mewcode.kernel import collaboration as ledger
from mewcode.orchestration.task_context import ContextBudgetExceeded, build_task_context

GOAL = "交付测试结果"


def entry(key, kind, body, *, version=1, data=None):
    return {"id": key, "kind": kind, "title": key, "body": body, "state": "active", "data": dict(data or {}),
            "version": version, "origin": "user", "strength": "hard",
            "sources": [{"source_id": "user_1", "quote": body}]}


def make_ledger(*, attempts=None, extra_entries=None):
    entries = {
        "goal": entry("goal", "goal", GOAL),
        "req": entry("req", "requirement", "结果必须包含结论"),
        "con": entry("con", "constraint", "禁止访问网络", version=2),
        "ass": entry("ass", "assumption", "上游已提供输入", data={"value": "有输入"}),
        "branch_ass": entry("branch_ass", "assumption", "另一分支假设BRANCHLEAK"),
        "other": entry("other", "requirement", "另一合同的要求"),
        "free": entry("free", "requirement", "无关条目"),
    }
    entries.update(extra_entries or {})
    contracts = {
        "dep": {"node_id": "dep", "version": 1, "requirement_ids": [], "constraint_ids": [],
                "checks": [], "method": "", "forbidden_changes": [], "bound_dependencies": []},
        "work": {"node_id": "work", "version": 3, "requirement_ids": ["req"], "constraint_ids": ["con", "ass"],
                 "checks": [{"id": "conclusion", "version": 1, "kind": "review"}],
                 "method": "先读输入再产出", "forbidden_changes": ["不得删除约束"],
                 "authorization": ["只读输入"], "bound_dependencies": ["dep"], "iteration": None},
        "deliver": {"node_id": "deliver", "version": 1, "requirement_ids": ["other"], "constraint_ids": [],
                    "checks": [], "method": "", "forbidden_changes": [], "bound_dependencies": ["work"]},
    }
    return {"format": ledger.FORMAT, "task_id": "task-one", "run_id": "a" * 32, "spec_version": 2,
            "plan_version": 2, "coordinator_epoch": 1, "goal": GOAL, "entries": entries,
            "contracts": contracts, "certificates": {}, "attempts": dict(attempts or {}),
            "proposals": {}, "policy": {"context_char_budget": 40000}, "retry_generations": {}}


def attempt(contract_id, number, status, *, reason="", evidence=None, spec_version=2):
    report = {"passed": False, "reason": reason, "evidence": list(evidence or [])} if status == "rejected" else None
    return {"id": f"{contract_id}-{number}", "contract_id": contract_id, "number": number, "status": status,
            "spec_version": spec_version, "retry_generation": 0, "report": report}


def big_source():
    return {"node_id": "upstream", "source_revision": "rev-1", "status": "completed", "text": "x" * 5000,
            "handoff": {"summary": "y" * 3000, "artifacts": [{"path": "candidate.txt"}]}}


def real_core():
    for name in ("tests.test_collaboration_mode", "test_collaboration_mode"):
        try:
            return importlib.import_module(name).core()
        except ImportError:
            continue
    pytest.skip("真实协作 fixture 不可导入")


def expand_shared_sources(payload):
    """Independent decoder for the documented model-facing context format."""
    result = copy.deepcopy(payload)
    if result.get("context_encoding") != "shared_sources_v1":
        return result
    del result["context_encoding"]
    assert result["context_notes"][-1].startswith("context_encoding=shared_sources_v1")
    result["context_notes"].pop()

    def expand_table(item):
        if isinstance(item, list):
            return item
        assert set(item) == {"tables", "order"}
        rows = []
        for table_index, row_index in item["order"]:
            table = item["tables"][table_index]
            row = dict(zip(table["columns"], table["rows"][row_index], strict=True))
            assert not (set(row) & set(table.get("shared", {})))
            rows.append({**row, **copy.deepcopy(table.get("shared", {}))})
        return rows

    citations = expand_table(result.pop("source_citations"))
    objective = result["objective"]
    for name in ("requirements", "constraints"):
        objective[name] = expand_table(objective[name])
    rows = objective["requirements"] + objective["constraints"] + [h["entry"] for h in result["hypotheses"]]
    for row in rows:
        if "field_source_groups" in row:
            row["field_sources"] = {
                field: copy.deepcopy(group["references"])
                for group in row.pop("field_source_groups") for field in group["fields"]
            }
        for references in [row.get("sources", []), *(row.get("field_sources") or {}).values()]:
            if not isinstance(references, list):
                continue
            for index, ref in enumerate(references):
                if isinstance(ref, dict) and set(ref) == {"cite"}:
                    assert type(ref["cite"]) is int and ref["cite"] >= 0
                    references[index] = copy.deepcopy(citations[ref["cite"]])
    return result


def test_oversized_provenance_fits_without_changing_any_mandatory_fact():
    value = make_ledger()
    quote = '来源原文😀 "x"\\path\n' * 400
    first = {"source_id": "original-user", "quote": quote, "extra": {"version": 1}}
    second = {"source_id": "another-source", "quote": quote, "extra": {"version": True}}
    for key in ("req", "con", "ass"):
        value["entries"][key]["sources"] = [first, second, first]
        value["entries"][key]["field_sources"] = {
            "body": [first, first], "data.value": [first, first], "other": [second], "empty": [],
        }
    before = copy.deepcopy(value)
    original = build_task_context(value, "work", char_budget=1000000)
    context = build_task_context(value, "work", char_budget=24000)
    assert context["context_encoding"] == "shared_sources_v1"
    assert context["budget"]["used"] == len(json.dumps(context, ensure_ascii=False)) <= 24000
    assert context["objective"]["current_contract"] == value["contracts"]["work"]
    restored = expand_shared_sources(context)
    # Storage usage is measured on the encoded view; the declared budget is unchanged.
    original["budget"] = copy.deepcopy(context["budget"])
    assert json.dumps(restored, sort_keys=True, ensure_ascii=False) == json.dumps(original, sort_keys=True, ensure_ascii=False)
    assert value == before


@pytest.mark.parametrize("reserved", [
    {"field_source_groups": {"original": "keep"}},
    {"sources": [{"cite": "original-user-data"}]},
    {"field_sources": {"body": [{"cite": "original-user-data"}]}},
])
def test_provenance_encoding_preserves_reserved_user_shapes(reserved):
    from mewcode.orchestration.task_context import _share_provenance

    value = make_ledger()
    value["entries"]["req"].update(reserved)
    original = build_task_context(value, "work")
    before = copy.deepcopy(original)
    assert _share_provenance(original) is None
    assert original == before


def test_record_tables_preserve_noncontiguous_schemas_order_and_json_types():
    from mewcode.orchestration.task_context import _table_entries

    rows = [{"id": "a", "extra": True, "body": "重复原文"},
            {"id": "b", "different": None},
            {"id": "c", "extra": 1, "body": "重复原文"},
            {"id": "d", "different": "😀\n"}]
    before = copy.deepcopy(rows)
    tables = _table_entries(rows)
    restored = []
    for table_id, row_id in tables["order"]:
        table = tables["tables"][table_id]
        restored.append({**dict(zip(table["columns"], table["rows"][row_id], strict=True)),
                         **copy.deepcopy(table.get("shared", {}))})
    assert json.dumps(restored, sort_keys=True) == json.dumps(rows, sort_keys=True)
    assert rows == before


def test_mandatory_facts_are_complete_and_the_ledger_is_not_mutated():
    value = make_ledger()
    snapshot = copy.deepcopy(value)

    context = build_task_context(value, "work", sources={"up": big_source()}, evidence=[{"claim": "done"}])

    assert value == snapshot, "projection must not modify the ledger"
    assert context["identity"] == {
        "task_id": "task-one", "run_id": "a" * 32, "spec_version": 2, "plan_version": 2,
        "contract_id": "work", "contract_version": 3, "coordinator_epoch": 1, "role": "executor",
        "basis_hash": ledger.dependencies(value, "work")}
    assert context["objective"]["goal"] == GOAL
    assert context["objective"]["current_contract"] == value["contracts"]["work"]
    assert context["objective"]["current_contract"]["method"] == "先读输入再产出"
    assert context["objective"]["current_contract"]["forbidden_changes"] == ["不得删除约束"]
    assert context["objective"]["current_contract"]["checks"] == value["contracts"]["work"]["checks"]
    assert context["objective"]["current_contract"]["authorization"] == ["只读输入"]
    assert [row["id"] for row in context["objective"]["requirements"]] == ["req"]
    assert context["objective"]["requirements"][0]["sources"] == value["entries"]["req"]["sources"]
    assert [row["id"] for row in context["objective"]["constraints"]] == ["con", "ass"]
    assert context["objective"]["constraints"][0]["version"] == 2
    assert json.loads(json.dumps(context, ensure_ascii=False)) == context


def test_only_referenced_assumptions_become_hypotheses_never_facts():
    context = build_task_context(make_ledger(), "work")

    assert [row["entry"]["id"] for row in context["hypotheses"]] == ["ass"]
    assert all(row["verification"] == "unverified_assumption" for row in context["hypotheses"])
    referenced = {row["id"] for row in context["objective"]["requirements"] + context["objective"]["constraints"]}
    assert referenced == {"req", "con", "ass"}, "referenced entries stay complete, assumptions included"
    encoded = json.dumps(context, ensure_ascii=False)
    assert "BRANCHLEAK" not in encoded, "unreferenced branch assumptions must not leak into the context"
    assert "branch_ass" not in encoded


def test_bulk_material_is_bounded_and_reports_stay_unverified():
    attempts = {row["id"]: row for row in
                [attempt("work", n, "rejected", reason="证据不足" * 200, evidence=["log-%d" % n]) for n in range(1, 11)]
                + [attempt("work", 11, "superseded_closed", reason="用户换模型")]
                + [attempt("deliver", n, "rejected", reason="其它分支日志LOGLEAK") for n in range(1, 4)]}
    value = make_ledger(attempts=attempts)
    before = copy.deepcopy(value)

    context = build_task_context(value, "work", sources={"up": big_source()},
                                 evidence=[{"claim": "x" * 4000}, "raw note"])

    assert value == before
    assert len(context["recent_failures"]) == 4
    assert [row["number"] for row in context["recent_failures"]] == [8, 9, 10, 11]
    assert {row["attempt_id"] for row in context["recent_failures"]} <= {row["id"] for row in attempts.values()}
    assert all(len(row["reason"]) <= 400 for row in context["recent_failures"])
    assert context["reported_evidence"]["verification"] == "unverified"
    assert all(item["verification"] == "unverified" for item in context["reported_evidence"]["items"])
    assert context["reported_evidence"]["items"][0]["claim"]
    assert all(item.get("trust") != "verified" for item in context["inputs"])
    assert all(len(item.get("summary", "")) <= 600 for item in context["inputs"])
    assert "LOGLEAK" not in json.dumps(context, ensure_ascii=False), "other branches stay out"
    assert len(value["attempts"]) == 14, "raw ledger records must be preserved"


def test_projection_is_deterministic_and_basis_tracks_relevant_facts():
    value = make_ledger()
    first = build_task_context(value, "work", sources={"up": big_source()})
    second = build_task_context(value, "work", sources={"up": big_source()})
    assert first == second

    involved = copy.deepcopy(value)
    involved["entries"]["con"] = {**involved["entries"]["con"], "body": "允许本地缓存"}
    assert (build_task_context(involved, "work")["identity"]["basis_hash"]
            != first["identity"]["basis_hash"])

    certified = copy.deepcopy(value)
    certified["certificates"]["dep"] = {"attempt_id": "a1", "dependencies": "d", "report_hash": "h"}
    assert (build_task_context(certified, "work")["identity"]["basis_hash"]
            != first["identity"]["basis_hash"]), "an upstream certificate change alters the basis"

    unrelated = copy.deepcopy(value)
    unrelated["entries"]["free"] = {**unrelated["entries"]["free"], "body": "另一段无关正文"}
    assert build_task_context(unrelated, "work")["identity"]["basis_hash"] == first["identity"]["basis_hash"]

    assert first["evidence_index"] == [{"source_key": "up", "node_id": "upstream", "source_revision": "rev-1"}]
    assert first["inputs"][0]["source_revision"] == "rev-1"


def test_source_revision_falls_back_to_the_runtime_hash():
    source = {"node_id": "up", "status": "completed", "text": "x" * 10,
              "handoff": {"summary": "s", "artifacts": []}}
    expected = hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    context = build_task_context(make_ledger(), "work", sources={"up_key": source})

    assert context["evidence_index"] == [{"source_key": "up_key", "node_id": "up", "source_revision": expected}]
    assert context["inputs"][0]["source_revision"] == expected
    # A changed source body yields a different derived revision.
    other = {**source, "text": "y" * 10}
    assert (build_task_context(make_ledger(), "work", sources={"up_key": other})["inputs"][0]["source_revision"]
            != expected)
    from mewcode.tools.read_stage_result import ReadStageResult
    for revision in (None, "", 5, "explicit"):
        supplied = {**source, "source_revision": revision}
        projected = build_task_context(make_ledger(), "work", sources={"up": supplied})
        assert projected["evidence_index"][0]["source_revision"] == ReadStageResult({"up": supplied}).revisions["up"]


def test_runtime_source_shape_keeps_only_refs_and_never_copies_bodies():
    source = {
        "node_id": "upstream", "source_revision": "rev-9", "status": "completed", "text": "T" * 4000,
        "handoff": {"summary": "S" * 2000, "inline_outputs": [{"name": "impl", "content": "SECRETLEAK" * 50}]},
        "acceptance_certificate": {"attempt_id": "a1", "contract_version": 2, "report_hash": "h",
                                   "dependencies": "d", "checks": [{"id": "c"}], "invalidated": None},
        "fixed_artifacts": [{"kind": "file", "name": "result", "path": "out.txt", "snapshot_path": "snap/out.txt",
                             "sha256": "abc", "size": 12, "source_revision": "rev-9",
                             "content": "BODYLEAK", "text": "TEXTLEAK", "body": "BODYLEAK",
                             "inline_outputs": [{"content": "INLINELEAK"}]}],
    }
    context = build_task_context(make_ledger(), "work", sources={"up": source})

    item = context["inputs"][0]
    assert item["source_key"] == "up" and item["node_id"] == "upstream"
    assert item["source_revision"] == "rev-9" and item["status"] == "completed"
    assert item["trust"] == "unverified_report"
    assert len(item["summary"]) == 600
    assert item["acceptance_certificate"] == {"attempt_id": "a1", "contract_version": 2,
                                              "report_hash": "h", "dependencies": "d"}
    assert item["fixed_artifacts"] == [{"kind": "file", "name": "result", "path": "out.txt",
                                        "snapshot_path": "snap/out.txt", "sha256": "abc", "size": 12,
                                        "source_revision": "rev-9"}]
    encoded = json.dumps(context, ensure_ascii=False)
    for leak in ("BODYLEAK", "TEXTLEAK", "SECRETLEAK", "INLINELEAK", "T" * 100):
        assert leak not in encoded


def test_certificate_and_artifact_aliases_are_accepted_without_bodies():
    source = {"node_id": "up",
              "certificate": {"attempt_id": "a", "report_hash": "h", "artifacts": [{"content": "LEAKOLD"}]},
              "handoff": {"artifacts": [{"path": "p.txt", "body": "LEAKOLD"}]},
              "artifacts": [{"path": "top.txt"}]}

    item = build_task_context(make_ledger(), "work", sources={"up": source})["inputs"][0]

    assert item["acceptance_certificate"] == {"attempt_id": "a", "report_hash": "h"}
    assert item["fixed_artifacts"] == [{"path": "top.txt"}]
    assert "LEAKOLD" not in json.dumps(item, ensure_ascii=False)


def test_execution_budget_carries_policy_limits_and_current_lineage_only():
    value = make_ledger()
    value["policy"].update(max_attempts=3, max_model_calls=20, max_review_calls=8)
    counters = {"attempts": 0, "revisions": 0, "expansions": 0}
    value["contracts"]["work"]["lineage_id"] = "original-work"
    value["lineage_budget"] = {
        "format_revision": 1,
        "root": {"used": {**counters, "attempts": 101}, "reserved": {**counters, "attempts": 1}},
        "lineages": {"original-work": {"used": {**counters, "attempts": 2}, "reserved": {**counters, "attempts": 1}},
                     "other-branch": {"used": {**counters, "attempts": 99}, "reserved": counters}},
        "limits": {"root": {"attempts": 600, "revisions": 120, "expansions": 40},
                   "lineage": {"attempts": 9, "revisions": 6, "expansions": 6}},
        "grants": {"g1": {"decision_id": "g1", "lineage_id": "original-work", "kind": "attempts", "amount": 5},
                   "g2": {"decision_id": "g2", "lineage_id": "other-branch", "kind": "attempts", "amount": 500},
                   "g3": {"decision_id": "g3", "lineage_id": None, "kind": "attempts", "amount": 2}},
        "reservations": {"r1": {"note": "LEAKRES" * 500}},
    }
    before = copy.deepcopy(value)

    execution = build_task_context(value, "work")["budget"]["execution"]

    assert value == before, "projection must not touch the lineage ledger"
    assert execution["max_attempts"] == 3
    assert execution["max_model_calls"] == 20
    assert execution["max_review_calls"] == 8
    lineage = execution["lineage"]
    assert lineage["lineage_id"] == "original-work"
    assert lineage["root"]["used"]["attempts"] == 101
    assert lineage["counts"]["used"]["attempts"] == 2 and lineage["counts"]["reserved"]["attempts"] == 1
    assert lineage["limits"]["lineage"]["attempts"] == 9
    assert lineage["granted"]["lineage"]["attempts"] == 5
    assert lineage["granted"]["root"]["attempts"] == 2
    assert lineage["grant_decisions"] == ["g1", "g3"]
    lineage["counts"]["used"]["attempts"] = 99
    assert value == before
    encoded = json.dumps(execution, ensure_ascii=False)
    assert "LEAKRES" not in encoded and "other-branch" not in encoded
    assert "LEAKGRANT" not in encoded, "grant summaries keep only locator fields"


def test_budget_fails_for_mandatory_facts_and_omits_optional_material():
    with pytest.raises(ContextBudgetExceeded):
        build_task_context(make_ledger(), "work", char_budget=1)

    required = build_task_context(make_ledger(), "work")
    budget = len(json.dumps(required, ensure_ascii=False)) + 1500
    attempts = {row["id"]: row for row in
                [attempt("work", n, "rejected", reason="失败原因" * 100) for n in range(1, 7)]}
    value = make_ledger(attempts=attempts)

    context = build_task_context(value, "work", sources={"up": big_source()},
                                 evidence=[{"claim": "unverified"}], char_budget=budget)

    encoded = json.dumps(context, ensure_ascii=False)
    assert len(encoded) <= budget, "final JSON must respect the budget without truncation"
    assert json.loads(encoded)["identity"]["contract_id"] == "work"
    assert any("摘要" in note for note in context["context_notes"])
    assert all("summary" not in item for item in context["inputs"])
    assert context["evidence_index"] == [{"source_key": "up", "node_id": "upstream", "source_revision": "rev-1"}]
    assert context["objective"]["current_contract"] == value["contracts"]["work"]
    assert context["objective"]["constraints"][0]["body"] == "禁止访问网络", "constraints are never trimmed"
    assert "失败原因" not in encoded


def test_returned_values_are_deep_copies():
    value = make_ledger()
    sources = {"up": {"node_id": "up"}}
    evidence = [{"claim": "done", "nested": {"k": [1]}}]
    first = build_task_context(value, "work", sources=sources, evidence=evidence)
    second = build_task_context(value, "work", sources=sources, evidence=evidence)

    first["objective"]["constraints"][0]["body"] = "改写"
    first["inputs"][0]["node_id"] = "改写"
    first["reported_evidence"]["items"][0]["nested"]["k"].append(2)

    assert value["entries"]["con"]["body"] == "禁止访问网络"
    assert second["objective"]["constraints"][0]["body"] == "禁止访问网络"
    assert second["inputs"][0]["node_id"] == "up"
    assert second["reported_evidence"]["items"][0]["nested"]["k"] == [1]
    assert first["hypotheses"][0]["entry"] is not second["hypotheses"][0]["entry"]


def test_projection_performs_no_file_io(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("projection must not touch the filesystem")

    monkeypatch.setattr(builtins, "open", boom)
    build_task_context(make_ledger(), "work", sources={"up": big_source()}, evidence=[{"claim": "done"}])


def test_real_collaboration_state_projects_contract():
    value = real_core()
    before = copy.deepcopy(value)
    contract = value["contracts"]["work"]

    context = build_task_context(value, "work")

    assert value == before, "the real ledger must not be modified"
    assert context["identity"]["contract_id"] == "work"
    assert context["identity"]["contract_version"] == contract["version"]
    assert context["identity"]["basis_hash"] == ledger.dependencies(value, "work")
    assert context["objective"]["current_contract"] == contract
    assert {row["id"] for row in context["objective"]["requirements"]} == set(contract["requirement_ids"])
    assert {row["id"] for row in context["objective"]["constraints"]} == set(contract["constraint_ids"])
    assert isinstance(context["hypotheses"], list)
    assert "execution" in context["budget"]
    assert json.loads(json.dumps(context, ensure_ascii=False)) == context


def test_invalid_inputs_fail_clearly():
    value = make_ledger()
    with pytest.raises(ValueError, match="没有合同"):
        build_task_context(value, "ghost")
    with pytest.raises(ValueError, match="字符串"):
        build_task_context(value, 5)
    with pytest.raises(ValueError, match="role"):
        build_task_context(value, "work", role=123)
    with pytest.raises(ValueError, match="正整数"):
        build_task_context(value, "work", char_budget=True)
    with pytest.raises(ValueError, match="正整数"):
        build_task_context(value, "work", char_budget=0)
    with pytest.raises(ValueError, match="正整数"):
        build_task_context(value, "work", char_budget=-5)
    with pytest.raises(ValueError, match="映射"):
        build_task_context(value, "work", sources=["up"])
    with pytest.raises(ValueError, match="列表"):
        build_task_context(value, "work", evidence="not-a-list")
    with pytest.raises(ValueError, match="映射"):
        build_task_context([], "work")

    broken = make_ledger()
    broken["contracts"]["work"]["requirement_ids"] = ["req", "ghost"]
    with pytest.raises(ValueError, match="ghost"):
        build_task_context(broken, "work")
