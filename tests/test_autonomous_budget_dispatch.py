"""Budget wiring for dispatch/complete: reserve, the real start boundary, conservation."""
from __future__ import annotations

import copy
import json

import pytest

from mewcode.kernel import collaboration as ledger
from mewcode.kernel import lineage_budget
from tests.test_collaboration_mode import accept, core, passing_report


def lineage_id(value, node="work"):
    return value["contracts"][node].get("lineage_id") or node


def test_dispatch_reserves_in_the_lineage_before_writing_the_attempt():
    value = core()
    attempt = ledger.dispatch(value, "work")
    budget = value["lineage_budget"]
    expected = lineage_id(value)
    assert attempt["lineage_id"] == expected
    assert attempt["budget_reservation_id"] == "attempt:" + attempt["id"]
    assert value["attempts"][attempt["id"]]["budget_reservation_id"] == "attempt:" + attempt["id"]
    reservation = budget["reservations"]["attempt:" + attempt["id"]]
    assert reservation["lineage_id"] == expected
    assert reservation["status"] == "reserved"
    assert reservation["amount"] == 1
    assert budget["root"]["reserved"]["attempts"] == 1
    assert budget["root"]["used"]["attempts"] == 0


def test_start_then_complete_settles_exactly_one_unit_and_stays_idempotent():
    value = core()
    attempt = ledger.dispatch(value, "work")

    started = ledger.mark_started(value, attempt["id"])
    assert started["budget_started"] is True
    budget = value["lineage_budget"]
    assert budget["reservations"]["attempt:" + attempt["id"]]["status"] == "settled"
    assert budget["reservations"]["attempt:" + attempt["id"]]["actual"] == 1
    assert budget["root"]["reserved"]["attempts"] == 0
    assert budget["root"]["used"]["attempts"] == 1

    again = ledger.mark_started(value, attempt["id"])
    assert again["budget_started"] is True
    assert value["lineage_budget"]["root"]["used"]["attempts"] == 1
    assert value["lineage_budget"]["root"]["reserved"]["attempts"] == 0

    report = passing_report(value, "work")
    certificate = ledger.complete(value, attempt["id"], report)
    assert ledger.complete(value, attempt["id"], report) == certificate
    assert ledger.mark_started(value, attempt["id"])["budget_started"] is True
    assert value["lineage_budget"]["root"]["used"]["attempts"] == 1
    assert value["lineage_budget"]["root"]["reserved"]["attempts"] == 0


def test_json_restart_does_not_charge_the_same_attempt_twice():
    value = core()
    attempt = ledger.dispatch(value, "work")
    ledger.mark_started(value, attempt["id"])

    restarted = json.loads(json.dumps(value))
    replayed = ledger.mark_started(restarted, attempt["id"])
    assert replayed["budget_started"] is True
    budget = restarted["lineage_budget"]
    assert budget["root"]["used"]["attempts"] == 1
    assert budget["root"]["reserved"]["attempts"] == 0
    assert budget["reservations"]["attempt:" + attempt["id"]]["status"] == "settled"


def test_legacy_ledger_migrates_before_the_new_attempt_is_written():
    value = core()
    value.pop("lineage_budget")
    attempt = ledger.dispatch(value, "work")
    assert "lineage_budget" in value
    assert value["attempts"][attempt["id"]]["budget_reservation_id"] == "attempt:" + attempt["id"]
    assert value["lineage_budget"]["reservations"]["attempt:" + attempt["id"]]["status"] == "reserved"


def test_start_refuses_an_attempt_that_already_reached_a_terminal_state():
    value = core()
    attempt = ledger.dispatch(value, "work")
    ledger.mark_started(value, attempt["id"])
    ledger.complete(value, attempt["id"], {"passed": False})
    snapshot = copy.deepcopy(value)
    with pytest.raises(ValueError):
        ledger.mark_started(value, attempt["id"])
    assert value == snapshot


def test_invalid_acceptance_report_does_not_charge_or_start():
    value = core()
    attempt = ledger.dispatch(value, "work")
    report = passing_report(value, "work")
    assert report["checks"], "基础夹具必须提供至少一个可校验的检查项"
    report["checks"][0]["evidence"] = {}
    snapshot = copy.deepcopy(value)
    with pytest.raises(ValueError):
        ledger.complete(value, attempt["id"], report)
    assert value == snapshot
    assert value["lineage_budget"]["root"]["used"]["attempts"] == 0
    assert value["lineage_budget"]["root"]["reserved"]["attempts"] == 1
    assert "budget_started" not in value["attempts"][attempt["id"]]


def test_local_attempt_limit_still_fires_before_any_charge():
    value = core()
    value["policy"]["max_attempts"] = 3
    for _ in range(3):
        attempt = ledger.dispatch(value, "work")
        ledger.mark_started(value, attempt["id"])
        ledger.complete(value, attempt["id"], {"passed": False})
    snapshot = copy.deepcopy(value)
    with pytest.raises(ledger.AttemptBudgetExhausted):
        ledger.dispatch(value, "work")
    assert value == snapshot


def test_one_lineage_is_not_reset_by_spec_version_or_retry_generation():
    value = core()
    value["policy"]["max_attempts"] = 1000
    started = 0
    while started < 12:
        snapshot = copy.deepcopy(value)
        try:
            attempt = ledger.dispatch(value, "work")
        except lineage_budget.BudgetExhausted:
            assert value == snapshot
            break
        ledger.mark_started(value, attempt["id"])
        ledger.complete(value, attempt["id"], {"passed": False})
        started += 1
        value["spec_version"] += 1
        value.setdefault("retry_generations", {})["work"] = started
    else:
        pytest.fail("同一谱系的尝试预算没有在 12 次内耗尽")
    assert started == 9
    budget = value["lineage_budget"]
    assert budget["lineages"][lineage_id(value)]["used"]["attempts"] == started
    assert budget["root"]["reserved"]["attempts"] == 0


def test_shared_fixture_accept_path_charges_exactly_one_unit():
    value = core()
    attempt, _report = accept(value)
    budget = value["lineage_budget"]
    assert budget["reservations"]["attempt:" + attempt["id"]]["status"] == "settled"
    assert budget["root"]["used"]["attempts"] == 1
    assert budget["root"]["reserved"]["attempts"] == 0


def test_bad_contract_fingerprint_cannot_leave_a_budget_reservation():
    value = core()
    value["contracts"]["work"]["requirement_ids"].append("missing_entry")
    before = copy.deepcopy(value)
    with pytest.raises(KeyError):
        ledger.dispatch(value, "work")
    assert value == before


def test_mark_started_migrates_an_old_attempt_without_resetting_consumption():
    value = core()
    attempt = ledger.dispatch(value, "work")
    value.pop("lineage_budget")
    value["attempts"][attempt["id"]].pop("budget_reservation_id")
    ledger.mark_started(value, attempt["id"])
    assert value["lineage_budget"]["root"]["used"]["attempts"] == 1
    ledger.mark_started(value, attempt["id"])
    assert value["lineage_budget"]["root"]["used"]["attempts"] == 1
