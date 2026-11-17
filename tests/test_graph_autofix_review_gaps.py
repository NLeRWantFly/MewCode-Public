"""Regression tests for gaps found in independent review of the second batch."""
import asyncio
import json

from mewcode.orchestration.graph_autofix import autofix
from mewcode.orchestration.graph_readiness import readiness


def entry(entry_id, kind="node", *, data=None, evidence="user_source", **more):
    return {"id": entry_id, "kind": kind, "title": entry_id, "body": "用户要求",
            "state": "active", "data": data if data is not None else {}, "sources": [],
            "evidence": evidence, **more}


def step(entry_id, node_kind="implement"):
    return entry(entry_id, data={"node_kind": node_kind, "outputs": ["产出" + entry_id],
                                 "acceptance": ["验收" + entry_id]})


def ready_doc():
    return {"schema_version": 1, "ignored": [], "entries": [
        step("a", "plan"), step("b", "reduce"),
        entry("e1", "edge", data={"from_node": "a", "to_node": "b", "condition": "always"}),
    ]}


class Model:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), 0

    async def __call__(self, system, user):
        self.calls += 1
        return self.replies.pop(0)


def run(document, *replies, **options):
    model = Model(*replies)
    result = asyncio.run(autofix(document, None, conversation_id="c", revision=1,
                                 ask_model=model, **options))
    return result, model


def test_ledger_part_that_would_break_readiness_is_discarded(monkeypatch):
    """A ledger entry must never be adopted if adopting it makes the graph worse."""
    import mewcode.orchestration.graph_autofix as module

    real = module.readiness

    def worse_after_ledger(document, base, **kwargs):
        result = real(document, base, **kwargs)
        if any(e["id"] == "assume" for e in document["entries"]):
            result = {**result, "ready": False, "diagnostics": [
                *result["diagnostics"],
                {"code": "projection_invalid", "path": "$", "entry_ids": ["assume"],
                 "message": "账本条目让图无法投影"}]}
        return result

    monkeypatch.setattr(module, "readiness", worse_after_ledger)
    reply = json.dumps({"upserts": [{"id": "assume", "kind": "decision", "title": "默认顺序",
                                     "body": "先写后查", "data": {"category": "designed"},
                                     "rationale": "记录设计决策"}]}, ensure_ascii=False)
    before = ready_doc()
    result, _ = run(before, reply, max_repair_rounds=0)
    first = result["rounds"][0]["ledger"]
    assert first["adopted"] is False and first["reason"] == "new_errors"
    assert all(e["id"] != "assume" for e in result["document"]["entries"])


def test_ledger_part_that_keeps_the_graph_healthy_is_adopted():
    reply = json.dumps({"upserts": [{"id": "assume", "kind": "decision", "title": "默认顺序",
                                     "body": "先写后查", "data": {"category": "designed"},
                                     "rationale": "记录设计决策"}]}, ensure_ascii=False)
    result, _ = run(ready_doc(), reply, max_repair_rounds=0)
    assert result["rounds"][0]["ledger"]["adopted"] is True
    assert any(e["id"] == "assume" for e in result["document"]["entries"])
    assert readiness(result["document"], None, conversation_id="c", revision=1)["ready"]
