from __future__ import annotations

import asyncio
import json

import pytest

from mewcode.kernel import collaboration as ledger
from mewcode.orchestration.collab_compile import compile_plan
from mewcode.orchestration.collab_coordinator import CollaborationCoordinator
from tests.test_collaboration_mode import CATALOG, RUN_ID, flow, store_for


class Ask:
    """替身协调服务：记录每次派发的输入。"""

    def __init__(self, reply=None):
        self.payloads = []
        self.reply = reply or {"action": "wait", "reason": "等待宿主推进"}

    async def __call__(self, model, payload, system):
        self.payloads.append(payload)
        return json.dumps(self.reply, ensure_ascii=False)


def prepared(tmp_path, *, budget=None):
    store = store_for(tmp_path)

    def initialize(record):
        ledger.initialize(record, compile_plan(flow()))
        if budget is not None:
            ledger.state(record)["policy"]["context_char_budget"] = budget

    store.mutate(RUN_ID, initialize, durable=True)
    assert ledger.state(store.load(RUN_ID))["contracts"]["work"]
    return store


def decide(store, ask, *, event_id, node_id="work", evidence=None):
    coordinator = CollaborationCoordinator(store, RUN_ID, CATALOG, ask=ask)
    return asyncio.run(coordinator.decide(event_id=event_id, node_id=node_id, evidence=evidence))


def test_projection_carries_mandatory_constraints_without_unrelated_timeline(tmp_path):
    store = prepared(tmp_path)
    constraint = {"id": "keep", "kind": "constraint", "body": "保留用户指定方法和全部数据" * 60,
                  "version": 3, "origin": "user", "strength": "hard",
                  "sources": [{"source_id": "original", "quote": "必须完整保留"}]}
    def add_constraint(record):
        value = ledger.state(record)
        ledger.event(value, "unrelated_decision", marker="TIMELINE-NOISE-9911")
        value["entries"]["keep"] = constraint
        value["contracts"]["work"]["constraint_ids"].append("keep")
    store.mutate(RUN_ID, add_constraint, durable=True)
    ask = Ask({"action": "retry", "reason": "按原合同继续"})

    result = decide(store, ask, event_id="event-projection", evidence={"host": "observed"})

    assert result == {"action": "retry", "reason": "按原合同继续"}
    assert len(ask.payloads) == 1
    payload = ask.payloads[0]
    assert set(payload) == {"event_id", "context"}
    assert payload["event_id"] == "event-projection"
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "交付测试结果" in encoded
    assert "coordinator" in encoded
    assert "TIMELINE-NOISE-9911" not in encoded
    contract = ledger.state(store.load(RUN_ID))["contracts"]["work"]
    mandatory = set(contract["requirement_ids"] + contract["constraint_ids"])
    assert mandatory
    assert all(key in encoded for key in mandatory)
    assert payload["context"]["objective"]["constraints"] == [constraint]
    assert len(encoded) <= ledger.state(store.load(RUN_ID))["policy"]["context_char_budget"]


def test_repeated_event_id_reuses_the_recorded_decision(tmp_path):
    store = prepared(tmp_path)
    ask = Ask({"action": "retry", "reason": "按原合同继续"})

    first = decide(store, ask, event_id="event-dup", evidence=[])
    second = decide(store, ask, event_id="event-dup", evidence=[])

    assert first == second
    assert len(ask.payloads) == 1
    value = ledger.state(store.load(RUN_ID))
    diagnostic_id = next(key for key, row in value["diagnostics"].items() if "event-dup" in row["event_ids"])
    calls = [call for call in value["calls"].values() if call["event_id"] == diagnostic_id]
    assert [call["status"] for call in calls] == ["finished"]


@pytest.mark.parametrize("budget", [1, 80])
def test_oversized_mandatory_context_never_reaches_the_model(tmp_path, budget):
    store = prepared(tmp_path, budget=budget)
    ask = Ask()

    with pytest.raises(ValueError, match="预算"):
        decide(store, ask, event_id="event-over")

    assert ask.payloads == []


def test_evidence_none_dict_list_and_string_are_normalized(tmp_path):
    store = prepared(tmp_path, budget=20000)
    ask = Ask()

    decide(store, ask, event_id="event-none", evidence=None)
    decide(store, ask, event_id="event-dict", evidence={"note": "DICT-SHAPE"})
    decide(store, ask, event_id="event-text", evidence="TEXT-SHAPE-文本")
    decide(store, ask, event_id="event-list", evidence=[{"note": "LIST-ONE"}, {"note": "LIST-TWO"}])
    duplicate = decide(store, ask, event_id="event-dict-as-list", evidence=[{"note": "DICT-SHAPE"}])

    assert len(ask.payloads) == 4
    assert duplicate["no_progress"] is True
    contexts = [payload["context"] for payload in ask.payloads]
    encoded = [json.dumps(payload, ensure_ascii=False) for payload in ask.payloads]
    assert "DICT-SHAPE" in encoded[1]
    assert "TEXT-SHAPE-文本" in encoded[2]
    assert "LIST-ONE" in encoded[3] and "LIST-TWO" in encoded[3]
    assert encoded[1] != encoded[3]
    diagnostics = ledger.state(store.load(RUN_ID))["diagnostics"]
    assert diagnostics[duplicate["diagnostic_id"]]["event_ids"] == ["event-dict", "event-dict-as-list"]
    assert contexts[0] != contexts[1]
