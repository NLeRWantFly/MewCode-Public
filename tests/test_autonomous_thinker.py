from __future__ import annotations

import asyncio
import copy
import json

import pytest

from mewcode.kernel import collaboration as ledger
from mewcode.orchestration.collab_compile import compile_plan
from mewcode.orchestration.collab_coordinator import CollaborationCoordinator, diagnostic_fingerprint
from tests.test_collaboration_mode import CATALOG, RUN_ID, flow, store_for, core


class Ask:
    """替身协调服务：记录派发次数与载荷。"""

    def __init__(self, reply=None):
        self.payloads = []
        self.reply = reply or {"action": "retry", "reason": "按原合同继续"}

    async def __call__(self, model, payload, system):
        self.payloads.append(payload)
        return json.dumps(self.reply, ensure_ascii=False)


def prepared(tmp_path):
    store = store_for(tmp_path)
    store.mutate(RUN_ID, lambda record: ledger.initialize(record, compile_plan(flow())), durable=True)
    value = ledger.state(store.load(RUN_ID))
    assert value["contracts"]["work"]
    value.setdefault("retry_generations", {})
    return store


def decide(store, ask, *, event_id, node_id="work", evidence=None):
    coordinator = CollaborationCoordinator(store, RUN_ID, CATALOG, ask=ask)
    return asyncio.run(coordinator.decide(event_id=event_id, node_id=node_id, evidence=evidence))


def diagnostics(store):
    return ledger.state(store.load(RUN_ID))["diagnostics"]


def wait_result(**overrides):
    base = {"action": "retry", "reason": "按原合同继续"}
    base.update(overrides)
    return base


def test_diagnostic_fingerprint_ignores_only_volatile_fields():
    value = core()
    value["retry_generations"] = {"work": 2}
    evidence = {"attempt_id": "a-1", "timestamp": 1, "artifact": {"sha256": "S" * 64},
                "checks": [{"id": "c1", "version": 3, "passed": False, "evidence": {"host": "observed"}}],
                "note": "缺少约束"}
    same = copy.deepcopy(evidence)
    same["attempt_id"] = "a-2"
    same["timestamp"] = 99
    same["snapshot_path"] = "runs/other"
    changed_sha = copy.deepcopy(evidence)
    changed_sha["artifact"]["sha256"] = "T" * 64
    changed_check = copy.deepcopy(evidence)
    changed_check["checks"][0]["version"] = 4
    changed_order = copy.deepcopy(evidence)
    changed_order["checks"] = []

    first = diagnostic_fingerprint(value, "work", evidence)
    assert first == diagnostic_fingerprint(value, "work", same)
    assert first != diagnostic_fingerprint(value, "work", changed_sha)
    assert first != diagnostic_fingerprint(value, "work", changed_check)
    assert first != diagnostic_fingerprint(value, "work", changed_order)
    value["retry_generations"]["work"] = 3
    assert first != diagnostic_fingerprint(value, "work", evidence)


def test_same_event_id_is_decided_once(tmp_path):
    store = prepared(tmp_path)
    ask = Ask()

    first = decide(store, ask, event_id="event-dup", evidence={"attempt_id": "a-1"})
    second = decide(store, ask, event_id="event-dup", evidence={"attempt_id": "a-1"})

    assert first == second == wait_result()
    assert len(ask.payloads) == 1
    assert len(diagnostics(store)) == 1
    entry = next(iter(diagnostics(store).values()))
    assert entry["event_ids"] == ["event-dup"]
    assert entry["status"] == "result"
    assert entry["result"] == wait_result()


def test_same_evidence_different_attempt_is_merged_without_calling_model(tmp_path):
    store = prepared(tmp_path)
    ask = Ask()

    first = decide(store, ask, event_id="event-first",
                   evidence={"attempt_id": "a-1", "timestamp": 1, "passed": False, "checks": [{"id": "c1", "passed": False}]})
    second = decide(store, ask, event_id="event-second",
                    evidence={"attempt_id": "a-2", "timestamp": 2, "passed": False, "checks": [{"id": "c1", "passed": False}]})

    assert first == wait_result()
    assert len(ask.payloads) == 1
    assert second["action"] == "wait" and second["no_progress"] is True
    assert second["evidence_refs"] == ["event-first", "event-second"]
    assert second["diagnostic_id"] == next(iter(diagnostics(store)))
    assert decide(store, ask, event_id="event-second",
                  evidence={"attempt_id": "a-2", "timestamp": 2, "passed": False,
                            "checks": [{"id": "c1", "passed": False}]}) == second
    entry = next(iter(diagnostics(store).values()))
    assert entry["event_ids"] == ["event-first", "event-second"]
    assert entry["result"] == wait_result()
    calls = [call for call in ledger.state(store.load(RUN_ID))["calls"].values() if call["kind"] == "coordinator"]
    assert len(calls) == 1 and calls[0]["status"] == "finished"


@pytest.mark.parametrize("mutate", [
    lambda report: report["artifacts"][0].__setitem__("sha256", "B" * 64),
    lambda report: report["checks"][0].__setitem__("passed", False),
    lambda report: report["checks"][0].__setitem__("version", 9),
])
def test_real_progress_is_diagnosed_again(tmp_path, mutate):
    store = prepared(tmp_path)
    ask = Ask()
    report = {"attempt_id": "a-1", "artifacts": [{"name": "结果", "sha256": "A" * 64}],
              "checks": [{"id": "c1", "version": 1, "passed": True}]}

    first = decide(store, ask, event_id="event-1", evidence=copy.deepcopy(report))
    progressed = copy.deepcopy(report)
    mutate(progressed)
    second = decide(store, ask, event_id="event-2", evidence=progressed)

    assert first == second == wait_result()
    assert len(ask.payloads) == 2
    assert len(diagnostics(store)) == 2


def test_user_retry_generation_allows_a_new_diagnosis(tmp_path):
    store = prepared(tmp_path)
    ask = Ask()
    evidence = {"attempt_id": "a-1", "checks": [{"id": "c1", "passed": False}]}

    decide(store, ask, event_id="event-1", evidence=evidence)
    merged = decide(store, ask, event_id="event-2", evidence={**evidence, "attempt_id": "a-2"})
    assert merged["no_progress"] is True and len(ask.payloads) == 1

    store.mutate(RUN_ID, lambda record: ledger.state(record).__setitem__(
        "retry_generations", {"work": 1}), durable=True)
    decide(store, ask, event_id="event-3", evidence=evidence)

    assert len(ask.payloads) == 2
    assert len(diagnostics(store)) == 2


def test_reloaded_json_reuses_the_recorded_merge(tmp_path):
    store = prepared(tmp_path)
    ask = Ask()
    evidence = {"attempt_id": "a-1", "checks": [{"id": "c1", "passed": False}]}
    decide(store, ask, event_id="event-1", evidence=evidence)

    from mewcode.expert_runs import ExpertRunStore
    reloaded = ExpertRunStore(tmp_path / "runs")
    ask_again = Ask()
    result = decide(reloaded, ask_again, event_id="event-2",
                    evidence={"attempt_id": "a-2", "checks": [{"id": "c1", "passed": False}]})

    assert next(iter(diagnostics(reloaded).values()))["event_ids"] == ["event-1", "event-2"]
    assert result["no_progress"] is True
    assert ask_again.payloads == []


def test_pending_diagnosis_keeps_old_call_protection(tmp_path):
    store = prepared(tmp_path)
    ask = Ask()
    decide(store, ask, event_id="event-1", evidence={"attempt_id": "a-1"})

    def simulate_dispatch(record):
        value = ledger.state(record)
        key = next(iter(value["diagnostics"]))
        diagnostic = value["diagnostics"][key]
        diagnostic["status"] = "pending"
        diagnostic["result"] = None
        call = next(call for call in value["calls"].values() if call["kind"] == "coordinator")
        call["status"] = "dispatched"
    store.mutate(RUN_ID, simulate_dispatch, durable=True)

    with pytest.raises(ValueError, match="结果未确认"):
        decide(store, ask, event_id="event-2", evidence={"attempt_id": "a-2"})
    assert len(ask.payloads) == 1


def test_failed_report_is_kept_after_merge(tmp_path):
    store = prepared(tmp_path)
    ask = Ask()
    failing = {"attempt_id": "a-1", "passed": False, "reason": "候选未覆盖约束",
               "checks": [{"id": "c1", "passed": False, "evidence": {"host": "observed"}}]}
    def failed_attempt(record):
        current = ledger.state(record)
        attempt = ledger.dispatch(current, "work")
        ledger.complete(current, attempt["id"], failing)
    store.mutate(RUN_ID, failed_attempt, durable=True)
    decide(store, ask, event_id="event-1", evidence=failing)
    store.mutate(RUN_ID, failed_attempt, durable=True)
    decide(store, ask, event_id="event-2", evidence={**failing, "attempt_id": "a-2", "timestamp": 5})

    value = ledger.state(store.load(RUN_ID))
    events = [event for event in value.get("timeline", []) if event.get("kind") == "diagnostic_event"]
    assert [event["event_id"] for event in events] == ["event-1", "event-2"]
    entry = next(iter(value["diagnostics"].values()))
    assert entry["result"]["action"] == "retry"
    assert entry["status"] == "result"
    assert len(value["attempts"]) == 2
    assert all(row["report"] == failing for row in value["attempts"].values())


def test_requirement_revision_changes_diagnostic_basis(tmp_path):
    from mewcode.orchestration.collab_service import user_revision
    store = prepared(tmp_path)
    ask = Ask()
    evidence = {"checks": [{"id": "check", "passed": False}]}
    decide(store, ask, event_id="first", evidence=evidence)
    revised = user_revision(store, store.load(RUN_ID), entry_id="goal", body="补充新的用户要求",
                            expected_version=1, request_id="diagnostic-revision")
    proposal_id = next(iter(revised["collaboration"]["proposals"]))
    store.mutate(RUN_ID, lambda record: ledger.adopt(record["collaboration"], proposal_id,
        review={"passed": True, "evidence": ["用户要求"]}, proposer_vendor="A", reviewer_vendor="B"), durable=True)
    decide(store, ask, event_id="second", evidence=evidence)
    assert len(ask.payloads) == 2
    assert len(diagnostics(store)) == 2


def test_late_coordinator_response_cannot_restore_old_diagnostic(tmp_path):
    from tests.test_collaboration_mode import EXECUTOR
    store = prepared(tmp_path)
    async def ask(model, payload, system):
        store.mutate(RUN_ID, lambda record: ledger.takeover(record["collaboration"],
            EXECUTOR.as_dict(), source_ref="host-takeover"), durable=True)
        return {"action": "retry", "reason": "旧协调者结果"}
    with pytest.raises(ValueError, match="旧协调者"):
        decide(store, ask, event_id="first", evidence={"reason": "失败"})
    current = ledger.state(store.load(RUN_ID))
    assert all(row["result"] is None for row in current["diagnostics"].values())
    assert any(row["status"] == "stale" for row in current["calls"].values())
