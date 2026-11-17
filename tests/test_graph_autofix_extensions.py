"""Optional autofix controls preserve old behavior and allow exact resumption."""
import asyncio
import copy
import json

import pytest

from mewcode.orchestration import graph_autofix
from mewcode.orchestration.graph_completion_protocol import CompletionRequestTooLarge


def entry(entry_id, kind="node", *, data=None, evidence="manual", body="执行"):
    return {"id": entry_id, "kind": kind, "title": entry_id, "body": body,
            "state": "active", "data": data if data is not None else {},
            "sources": [], "evidence": evidence}


def node(node_id, kind, *, outputs=None, acceptance=None):
    return entry(node_id, data={"node_kind": kind, "inputs": [],
                                "outputs": ["产物"] if outputs is None else outputs,
                                "acceptance": ["验收"] if acceptance is None else acceptance})


def edge(edge_id, start, end):
    return entry(edge_id, "edge", data={"from_node": start, "to_node": end,
                                          "condition": "always"})


def document(*entries):
    return {"schema_version": 1, "entries": list(entries), "ignored": []}


def graph(*, ready=False):
    entries = [node("plan", "plan"), node("audit", "audit"), node("finish", "reduce"),
               edge("first", "plan", "audit")]
    if ready:
        entries.append(edge("last", "audit", "finish"))
    return document(*entries)


BASE = {"finalizer_node_id": "finish"}


def proposal(*upserts):
    return json.dumps({"upserts": list(upserts), "notes": ""}, ensure_ascii=False)


def finish_edge():
    return {"id": "last", "kind": "edge", "title": "进入终验", "body": "复核后终验",
            "data": {"from_node": "audit", "to_node": "finish", "condition": "always"},
            "rationale": "补齐依赖"}


def extra_node():
    return {"id": "helper", "kind": "node", "title": "辅助", "body": "辅助处理",
            "data": {"node_kind": "implement", "outputs": ["辅助产物"],
                     "acceptance": ["辅助验收"]}, "rationale": "增加辅助路径"}


def extra_edge(entry_id, start, end):
    return {"id": entry_id, "kind": "edge", "title": entry_id, "body": "依赖",
            "data": {"from_node": start, "to_node": end, "condition": "always"},
            "rationale": "连接辅助路径"}


class FakeModel:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, system, user):
        self.calls.append((system, user))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


async def run(doc, fake, **kwargs):
    return await graph_autofix.autofix(doc, BASE, conversation_id="c", revision=1,
                                       ask_model=fake, **kwargs)


@pytest.mark.asyncio
async def test_explicit_defaults_match_legacy_path_and_record_shape():
    doc = graph()
    expected = await run(doc, FakeModel(proposal(finish_edge())))
    actual = await run(doc, FakeModel(proposal(finish_edge())),
                       extra_diagnostics=(), require_improvement=True,
                       locked_baseline=None, on_round=None, resume=None)
    assert actual == expected
    assert "extra_diagnostics" not in actual["rounds"][0]


@pytest.mark.asyncio
async def test_extra_diagnostics_append_only_to_requests_and_record_count():
    extra = [{"code": "coverage_gap", "path": "sources/u", "entry_ids": [],
              "message": "遗漏导出 PDF"}]
    fake = FakeModel(proposal())
    result = await run(graph(ready=True), fake, max_repair_rounds=0,
                       extra_diagnostics=extra)
    request = json.loads(fake.calls[0][1])
    assert request["diagnostics"][-1] == extra[0]
    assert result["status"] == "ready" and result["remaining"] == []
    assert result["rounds"][0]["diagnostics_before"] == 0
    assert result["rounds"][0]["diagnostics_after"] == 0
    assert result["rounds"][0]["extra_diagnostics"] == 1


@pytest.mark.asyncio
async def test_no_regression_mode_adopts_safe_equal_atoms_while_default_does_not():
    doc = graph(ready=True)
    response = proposal(extra_node(), extra_edge("branch_in", "plan", "helper"),
                        extra_edge("branch_out", "helper", "audit"))
    strict = await run(doc, FakeModel(response), max_repair_rounds=0)
    relaxed = await run(doc, FakeModel(response), max_repair_rounds=0,
                        require_improvement=False)
    assert strict["document"] == doc
    assert strict["rounds"][0]["structural"]["reason"] == "no_improvement"
    assert relaxed["rounds"][0]["structural"]["reason"] == "no_regression"
    assert relaxed["rounds"][0]["structural"]["adopted"] is True
    assert relaxed["status"] == "ready"
    assert {e["id"] for e in relaxed["document"]["entries"]} >= {
        "helper", "branch_in", "branch_out"}


@pytest.mark.asyncio
async def test_no_regression_mode_still_rejects_new_atoms():
    doc = graph(ready=True)
    response = proposal({"id": "orphan", "kind": "node", "title": "孤点",
                         "body": "孤立", "data": {"node_kind": "implement",
                                                  "outputs": [], "acceptance": []},
                         "rationale": "尝试新增"})
    result = await run(doc, FakeModel(response), max_repair_rounds=0,
                       require_improvement=False)
    assert result["document"] == doc
    assert result["rounds"][0]["structural"]["reason"] == "new_errors"


def test_locked_snapshot_contains_only_locked_entries_and_is_deep_copy():
    doc = document(entry("user", evidence="user_source", data={"items": ["原值"]}),
                   entry("saved", evidence="saved_graph"),
                   entry("manual", evidence="manual"),
                   entry("reviewed", evidence="proposal"),
                   entry("free", evidence="proposal"))
    doc["entries"][3]["reviewed"] = True
    snapshot = graph_autofix.locked_snapshot(doc)
    assert set(snapshot) == {"user", "saved", "manual", "reviewed"}
    snapshot["user"]["data"]["items"].append("修改快照")
    assert doc["entries"][0]["data"]["items"] == ["原值"]


@pytest.mark.asyncio
async def test_supplied_locked_baseline_rejects_preexisting_violation_and_rolls_back_round():
    original = graph()
    baseline = graph_autofix.locked_snapshot(original)
    persisted = copy.deepcopy(original)
    persisted["entries"][0]["body"] = "越权改写"
    before = copy.deepcopy((persisted, baseline))
    result = await run(persisted, FakeModel(proposal(finish_edge())),
                       max_repair_rounds=0, locked_baseline=baseline)
    assert result["document"] == persisted
    assert result["rounds"][0]["structural"]["reason"] == "guard_failed"
    assert result["rounds"][0]["structural"]["adopted"] is False
    assert (persisted, baseline) == before


@pytest.mark.asyncio
async def test_on_round_fires_once_per_completed_round_with_detached_snapshots():
    seen = []

    def callback(record, current, calls):
        seen.append((record["round"], record["phase"], calls))
        record["error"] = "callback mutation"
        current["entries"].clear()

    result = await run(graph(), FakeModel("bad json", proposal(finish_edge())),
                       max_repair_rounds=1, on_round=callback)
    assert seen == [(1, "completion", 1), (2, "repair", 2)]
    assert result["status"] == "ready"
    assert result["rounds"][0]["error"] == "CompletionParseError"
    assert result["rounds"][1]["error"] == ""
    assert len(result["document"]["entries"]) == 5


@pytest.mark.asyncio
async def test_on_round_awaits_async_callback_and_propagates_exception():
    seen = []

    async def callback(record, current, calls):
        await asyncio.sleep(0)
        seen.append((record["round"], calls, len(current["entries"])))

    await run(graph(), FakeModel(proposal(finish_edge())), on_round=callback)
    assert seen == [(1, 1, 5)]

    failure = RuntimeError("checkpoint unavailable")

    def fail(*_):
        raise failure

    with pytest.raises(RuntimeError) as error:
        await run(graph(), FakeModel(proposal(finish_edge())), on_round=fail)
    assert error.value is failure

    def cancel(*_):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await run(graph(), FakeModel(proposal(finish_edge())), on_round=cancel)


@pytest.mark.asyncio
async def test_on_round_includes_model_error_and_request_too_large(monkeypatch):
    seen = []
    failed = await run(graph(), FakeModel(RuntimeError("credential=secret")),
                       on_round=lambda record, current, calls: seen.append((record, calls)))
    assert failed["reason"] == "model_error"
    assert len(seen) == 1 and seen[0][0]["structural"]["reason"] == "model_error"
    assert seen[0][1] == 1

    def too_large(*_, **_options):
        raise CompletionRequestTooLarge("too long")

    monkeypatch.setattr(graph_autofix, "build_completion_request", too_large)
    seen.clear()
    large = await run(graph(), FakeModel(),
                      on_round=lambda record, current, calls: seen.append((record, calls)))
    assert large["reason"] == "request_too_large"
    assert len(seen) == 1 and seen[0][0]["error"] == "CompletionRequestTooLarge"
    assert seen[0][1] == 0


class Crash(Exception):
    pass


@pytest.mark.asyncio
async def test_resume_matches_uninterrupted_run_at_every_round_boundary():
    initial = graph()
    next(e for e in initial["entries"] if e["id"] == "plan")["data"]["outputs"] = []
    next(e for e in initial["entries"] if e["id"] == "audit")["data"]["acceptance"] = []
    script = [
        proposal({"id": "plan", "data": {"outputs": ["纲要"]}, "rationale": "补产出"}),
        proposal({"id": "audit", "data": {"acceptance": ["复核通过"]},
                  "rationale": "补验收"}),
        proposal(finish_edge()),
    ]
    baseline = graph_autofix.locked_snapshot(initial)
    whole = await run(initial, FakeModel(*script), max_repair_rounds=2,
                      locked_baseline=baseline)
    assert whole["status"] == "ready"
    assert [row["phase"] for row in whole["rounds"]] == ["completion", "repair", "repair"]
    assert whole["model_calls"] == 3

    for k in range(len(whole["rounds"]) + 1):
        if k:
            records = []
            saved = {}

            def checkpoint(record, current, calls):
                records.append(record)
                if len(records) == k:
                    saved.update(document=current, resume={"rounds": copy.deepcopy(records),
                                                           "model_calls": calls})
                    raise Crash()

            with pytest.raises(Crash):
                await run(initial, FakeModel(*script), max_repair_rounds=2,
                          locked_baseline=baseline, on_round=checkpoint)
        else:
            saved = {"document": copy.deepcopy(initial),
                     "resume": {"rounds": [], "model_calls": 0}}
        before = copy.deepcopy(saved)
        continuation = FakeModel(*script[k:])
        continued_rounds = []
        resumed = await run(saved["document"], continuation, max_repair_rounds=2,
                            locked_baseline=baseline, resume=saved["resume"],
                            on_round=lambda record, current, calls: continued_rounds.append(record["round"]))
        for key in ("document", "status", "reason", "rounds", "model_calls"):
            assert resumed[key] == whole[key], (k, key)
        assert len(continuation.calls) == whole["model_calls"] - k
        assert continued_rounds == list(range(k + 1, len(whole["rounds"]) + 1))
        assert saved == before


@pytest.mark.asyncio
async def test_resume_terminal_model_error_stops_without_new_call():
    first = await run(graph(), FakeModel(RuntimeError("offline")))
    fake = FakeModel(proposal(finish_edge()))
    resumed = await run(first["document"], fake, resume={"rounds": first["rounds"],
                                                             "model_calls": first["model_calls"]})
    assert resumed == first and fake.calls == []


@pytest.mark.asyncio
async def test_resume_exhausted_repairs_and_ready_completion_make_no_new_call():
    failures = await run(graph(), FakeModel(*(proposal() for _ in range(3))),
                         max_repair_rounds=2)
    fake = FakeModel(proposal(finish_edge()))
    exhausted = await run(failures["document"], fake, max_repair_rounds=2,
                          resume={"rounds": failures["rounds"],
                                  "model_calls": failures["model_calls"]})
    assert exhausted == failures and fake.calls == []

    ready = await run(graph(), FakeModel(proposal(finish_edge())))
    fake = FakeModel(proposal())
    again = await run(ready["document"], fake, resume={"rounds": ready["rounds"],
                                                      "model_calls": ready["model_calls"]})
    assert again == ready and fake.calls == []


@pytest.mark.asyncio
async def test_resume_counts_parse_failed_completion_toward_completed_rounds():
    first = await run(graph(), FakeModel("bad json", proposal(finish_edge())),
                      max_repair_rounds=1)
    assert [row["structural"]["reason"] for row in first["rounds"]] == [
        "parse_failed", "improved"]
    fake = FakeModel(proposal())
    resumed = await run(first["document"], fake, max_repair_rounds=1,
                        resume={"rounds": first["rounds"],
                                "model_calls": first["model_calls"]})
    assert resumed == first and fake.calls == []


@pytest.mark.asyncio
async def test_resume_request_too_large_terminal_round_stops_without_new_call(monkeypatch):
    def too_large(*_, **_options):
        raise CompletionRequestTooLarge("too long")

    monkeypatch.setattr(graph_autofix, "build_completion_request", too_large)
    first = await run(graph(), FakeModel())
    assert first["reason"] == "request_too_large"
    fake = FakeModel(proposal(finish_edge()))
    resumed = await run(first["document"], fake,
                        resume={"rounds": first["rounds"],
                                "model_calls": first["model_calls"]})
    assert resumed == first and fake.calls == []
