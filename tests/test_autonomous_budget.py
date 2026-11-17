from __future__ import annotations

import json

import pytest

from mewcode.kernel import lineage_budget


def core(attempts=None, contracts=None, limits=None):
    value = {"run_id": "r" * 32, "policy": {}, "attempts": attempts or {}, "contracts": contracts or {}}
    if limits is not None:
        value["policy"]["lineage_budget_limits"] = limits
    return value


def attempt(identity, contract_id="work", **extra):
    row = {"id": identity, "contract_id": contract_id, "status": "accepted",
           "spec_version": 1, "retry_generation": 0}
    row.update(extra)
    return row


def snapshot(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def seeded():
    """A committed ledger with one live reservation, after a JSON round trip."""
    value = core()
    lineage_budget.reserve(value, "r1", lineage_id="lin", kind="revisions", amount=2)
    return json.loads(json.dumps(value))


# --------------------------------------------------------------------------- initialization


def test_initialization_counts_history_across_versions_and_repeat_ensure_keeps_it():
    rows = {
        "a1": attempt("a1"),
        "a2": attempt("a2", spec_version=4, retry_generation=2),
        "a3": attempt("a3", contract_id="other", lineage_id="lineage-x"),
    }
    value = core(attempts=rows, contracts={"work": {"lineage_id": None}})
    budget = lineage_budget.ensure(value)
    assert budget["format_revision"] == 1
    assert budget["root"]["used"]["attempts"] == 3
    assert budget["root"]["reserved"]["attempts"] == 0
    assert budget["lineages"]["work"]["used"]["attempts"] == 2
    assert budget["lineages"]["lineage-x"]["used"]["attempts"] == 1
    migrated = budget["reservations"]["attempt:a3"]
    assert migrated["status"] == "settled" and migrated["actual"] == 1
    assert lineage_budget.ensure(value) == budget

    # A later spec version, a rename and a new limit policy must not reseed it.
    value["contracts"]["work"] = {"lineage_id": None, "version": 9, "title": "renamed"}
    value["policy"]["lineage_budget_limits"] = {"root": {"attempts": 1}}
    assert lineage_budget.ensure(value) == budget
    assert lineage_budget.ensure(value)["limits"]["root"]["attempts"] == 600


def test_historical_usage_over_limit_can_be_read_but_blocks_only_new_reserve():
    rows = {f"a{i}": attempt(f"a{i}") for i in range(3)}
    value = core(attempts=rows, contracts={"work": {}},
                 limits={"root": {"attempts": 2}, "lineage": {"attempts": 1}})
    budget = lineage_budget.ensure(value)
    assert budget["root"]["used"]["attempts"] == 3
    assert budget["lineages"]["work"]["used"]["attempts"] == 3
    with pytest.raises(lineage_budget.BudgetExhausted, match="attempts"):
        lineage_budget.reserve(value, "r", lineage_id="work", kind="attempts")
    assert lineage_budget.ensure(value) == budget


# --------------------------------------------------------------------------- limits


def test_root_and_lineage_limits_are_checked_together_without_partial_commit():
    assert issubclass(lineage_budget.BudgetExhausted, ValueError)
    value = core(limits={"root": {"attempts": 3}, "lineage": {"attempts": 2}})
    lineage_budget.reserve(value, "r1", lineage_id="lin", kind="attempts")
    lineage_budget.reserve(value, "r2", lineage_id="lin", kind="attempts")
    before = lineage_budget.ensure(value)
    with pytest.raises(lineage_budget.BudgetExhausted, match="谱系"):
        lineage_budget.reserve(value, "r3", lineage_id="lin", kind="attempts")
    assert lineage_budget.ensure(value) == before

    lineage_budget.reserve(value, "r4", lineage_id="other", kind="attempts")
    with pytest.raises(lineage_budget.BudgetExhausted, match="根"):
        lineage_budget.reserve(value, "r5", lineage_id="other-two", kind="attempts")
    after = lineage_budget.ensure(value)
    assert after["root"]["reserved"]["attempts"] == 3
    assert "other-two" not in after["lineages"]


def test_grant_extends_only_the_named_scope_and_survives_restart():
    value = core(limits={"lineage": {"attempts": 1}})
    lineage_budget.grant(value, "d-root", kind="attempts", amount=4)
    granted = lineage_budget.grant(value, "d-lineage", kind="attempts", amount=2, lineage_id="lin")
    assert granted["lineage_id"] == "lin" and granted["kind"] == "attempts"

    lineage_budget.reserve(value, "a1", lineage_id="lin", kind="attempts")
    lineage_budget.reserve(value, "a2", lineage_id="lin", kind="attempts")
    lineage_budget.reserve(value, "a3", lineage_id="lin", kind="attempts")
    with pytest.raises(lineage_budget.BudgetExhausted, match="谱系"):
        lineage_budget.reserve(value, "a4", lineage_id="lin", kind="attempts")
    # Every other lineage keeps the untouched default allowance.
    lineage_budget.reserve(value, "b1", lineage_id="other", kind="attempts")
    with pytest.raises(lineage_budget.BudgetExhausted, match="谱系"):
        lineage_budget.reserve(value, "b2", lineage_id="other", kind="attempts")
    assert lineage_budget.ensure(value)["root"]["used"]["attempts"] == 0

    restored = json.loads(json.dumps(value))
    reloaded = lineage_budget.ensure(restored)
    assert reloaded["grants"]["d-root"]["amount"] == 4
    assert reloaded["grants"]["d-lineage"]["lineage_id"] == "lin"
    assert reloaded["lineages"]["lin"]["reserved"]["attempts"] == 3
    lineage_budget.release(restored, "a1")
    assert lineage_budget.ensure(restored)["lineages"]["lin"]["reserved"]["attempts"] == 2


# --------------------------------------------------------------------------- idempotency


def test_reserve_settle_grant_are_idempotent_and_return_isolated_copies():
    value = core()
    first = lineage_budget.reserve(value, "r1", lineage_id="lin", kind="revisions", amount=2)
    assert first["status"] == "reserved" and first["amount"] == 2
    assert lineage_budget.reserve(value, "r1", lineage_id="lin", kind="revisions", amount=2) == first
    with pytest.raises(ValueError, match="另一内容"):
        lineage_budget.reserve(value, "r1", lineage_id="lin", kind="revisions", amount=3)
    with pytest.raises(ValueError, match="另一内容"):
        lineage_budget.reserve(value, "r1", lineage_id="other", kind="revisions", amount=2)
    with pytest.raises(ValueError, match="另一内容"):
        lineage_budget.reserve(value, "r1", lineage_id="lin", kind="attempts", amount=2)
    first["amount"] = 99
    assert lineage_budget.ensure(value)["reservations"]["r1"]["amount"] == 2

    settled = lineage_budget.settle(value, "r1", actual=1)
    assert settled["status"] == "settled" and settled["actual"] == 1
    assert lineage_budget.settle(value, "r1", actual=1) == settled
    with pytest.raises(ValueError, match="数量"):
        lineage_budget.settle(value, "r1", actual=2)
    with pytest.raises(ValueError, match="数量"):
        lineage_budget.settle(value, "r1")
    settled["actual"] = 0
    assert lineage_budget.ensure(value)["reservations"]["r1"]["actual"] == 1

    lineage_budget.reserve(value, "r2", lineage_id="lin", kind="revisions", amount=3)
    assert lineage_budget.settle(value, "r2")["actual"] == 3

    granted = lineage_budget.grant(value, "d1", kind="attempts", amount=5)
    assert lineage_budget.grant(value, "d1", kind="attempts", amount=5) == granted
    with pytest.raises(ValueError, match="另一内容"):
        lineage_budget.grant(value, "d1", kind="attempts", amount=6, lineage_id="lin")
    granted["amount"] = 0
    assert lineage_budget.ensure(value)["grants"]["d1"]["amount"] == 5


def test_ensure_never_hands_out_the_live_budget():
    value = core()
    seen = lineage_budget.ensure(value)
    seen["root"]["used"]["attempts"] = 999
    seen["limits"]["root"]["attempts"] = 1
    fresh = lineage_budget.ensure(value)
    assert fresh["root"]["used"]["attempts"] == 0
    assert fresh["limits"]["root"]["attempts"] == 600


# --------------------------------------------------------------------------- conservation


def test_release_and_partial_settlement_conserve_counts():
    value = core()
    lineage_budget.reserve(value, "r1", lineage_id="lin", kind="attempts")
    lineage_budget.reserve(value, "r2", lineage_id="lin", kind="attempts")
    lineage_budget.release(value, "r1")
    assert lineage_budget.release(value, "r1")["status"] == "released"
    budget = lineage_budget.ensure(value)
    assert budget["root"]["used"]["attempts"] == 0
    assert budget["lineages"]["lin"]["used"]["attempts"] == 0
    assert budget["lineages"]["lin"]["reserved"]["attempts"] == 1
    with pytest.raises(ValueError, match="重新预留"):
        lineage_budget.reserve(value, "r1", lineage_id="lin", kind="attempts")

    assert lineage_budget.settle(value, "r2", actual=0)["actual"] == 0
    budget = lineage_budget.ensure(value)
    assert budget["root"]["reserved"]["attempts"] == 0
    assert budget["root"]["used"]["attempts"] == 0
    with pytest.raises(ValueError, match="不能释放"):
        lineage_budget.release(value, "r2")

    lineage_budget.reserve(value, "r3", lineage_id="lin", kind="attempts", amount=2)
    with pytest.raises(ValueError, match="0 到预留数量"):
        lineage_budget.settle(value, "r3", actual=3)
    with pytest.raises(ValueError, match="0 到预留数量"):
        lineage_budget.settle(value, "r3", actual=-1)
    with pytest.raises(ValueError, match="整数"):
        lineage_budget.settle(value, "r3", actual=True)
    assert lineage_budget.ensure(value)["reservations"]["r3"]["status"] == "reserved"
    lineage_budget.settle(value, "r3", actual=2)
    assert lineage_budget.ensure(value)["root"]["used"]["attempts"] == 2

    with pytest.raises(ValueError, match="未知预留"):
        lineage_budget.settle(value, "missing")
    with pytest.raises(ValueError, match="未知预留"):
        lineage_budget.release(value, "missing")


def test_json_restart_preserves_partial_settlement_and_release():
    value = core()
    lineage_budget.reserve(value, "r1", lineage_id="lin", kind="expansions", amount=3)
    lineage_budget.reserve(value, "r2", lineage_id="lin", kind="expansions", amount=2)
    lineage_budget.settle(value, "r1", actual=1)
    lineage_budget.release(value, "r2")

    restored = json.loads(json.dumps(value))
    budget = lineage_budget.ensure(restored)
    assert budget["root"]["used"]["expansions"] == 1
    assert budget["root"]["reserved"]["expansions"] == 0
    assert budget["lineages"]["lin"]["used"]["expansions"] == 1
    assert budget["lineages"]["lin"]["reserved"]["expansions"] == 0
    assert budget["reservations"]["r1"]["actual"] == 1
    assert budget["reservations"]["r2"]["status"] == "released"

    lineage_budget.reserve(restored, "r3", lineage_id="lin", kind="expansions", amount=5)
    with pytest.raises(lineage_budget.BudgetExhausted, match="谱系"):
        lineage_budget.reserve(restored, "r4", lineage_id="lin", kind="expansions", amount=1)
    with pytest.raises(ValueError, match="重新预留"):
        lineage_budget.reserve(restored, "r2", lineage_id="lin", kind="expansions", amount=2)


# --------------------------------------------------------------------------- failure atomicity


def test_failure_before_first_commit_leaves_the_ledger_untouched():
    value = core()
    before = snapshot(value)
    failures = [
        lambda: lineage_budget.reserve(value, "r", lineage_id="lin", kind="calls"),
        lambda: lineage_budget.reserve(value, "", lineage_id="lin", kind="attempts"),
        lambda: lineage_budget.reserve(value, "r", lineage_id="lin", kind="attempts", amount=0),
        lambda: lineage_budget.settle(value, "missing"),
        lambda: lineage_budget.settle(value, "missing", actual=True),
        lambda: lineage_budget.release(value, "missing"),
        lambda: lineage_budget.grant(value, "d", kind="calls", amount=1),
        lambda: lineage_budget.grant(value, "", kind="attempts", amount=1),
        lambda: lineage_budget.grant(value, "d", kind="attempts", amount=-2),
    ]
    for call in failures:
        with pytest.raises(ValueError):
            call()
        assert "lineage_budget" not in value
        assert snapshot(value) == before


def test_exhausted_first_reserve_does_not_commit_a_budget():
    value = core(limits={"root": {"attempts": 1}})
    before = snapshot(value)
    with pytest.raises(lineage_budget.BudgetExhausted, match="根"):
        lineage_budget.reserve(value, "r", lineage_id="lin", kind="attempts", amount=2)
    assert "lineage_budget" not in value
    assert snapshot(value) == before


def test_failed_operations_leave_an_initialized_budget_identical():
    value = core(limits={"root": {"attempts": 2}})
    lineage_budget.reserve(value, "r1", lineage_id="lin", kind="attempts")
    before = snapshot(value)
    failures = [
        lambda: lineage_budget.reserve(value, "r2", lineage_id="lin", kind="attempts", amount=2),
        lambda: lineage_budget.reserve(value, "r1", lineage_id="lin", kind="revisions"),
        lambda: lineage_budget.settle(value, "missing"),
        lambda: lineage_budget.release(value, "missing"),
        lambda: lineage_budget.grant(value, "d", kind="attempts", amount=0),
        lambda: lineage_budget.grant(value, "d", kind="attempts", amount=True),
    ]
    for call in failures:
        with pytest.raises(ValueError):
            call()
        assert snapshot(value) == before


# --------------------------------------------------------------------------- stored data


CORRUPTIONS = [
    ("unknown-version", lambda budget: budget.update(format_revision=2)),
    ("text-version", lambda budget: budget.update(format_revision="1")),
    ("bool-version", lambda budget: budget.update(format_revision=True)),
    ("missing-version", lambda budget: budget.pop("format_revision")),
    ("missing-grants", lambda budget: budget.pop("grants")),
    ("extra-field", lambda budget: budget.update(extra={})),
    ("missing-usage", lambda budget: budget["root"].pop("used")),
    ("negative-usage", lambda budget: budget["root"]["used"].update(revisions=-1)),
    ("float-usage", lambda budget: budget["root"]["used"].update(revisions=1.5)),
    ("usage-not-backed", lambda budget: budget["root"]["used"].update(revisions=9)),
    ("lineage-not-backed", lambda budget: budget["lineages"]["lin"]["reserved"].update(revisions=0)),
    ("bad-status", lambda budget: budget["reservations"]["r1"].update(status="weird")),
    ("zero-amount", lambda budget: budget["reservations"]["r1"].update(amount=0)),
    ("reserved-with-actual", lambda budget: budget["reservations"]["r1"].update(actual=1)),
    ("settled-without-actual", lambda budget: budget["reservations"]["r1"].update(status="settled")),
    ("settled-over-amount",
     lambda budget: budget["reservations"]["r1"].update(status="settled", amount=1, actual=3)),
    ("unknown-lineage", lambda budget: budget["reservations"]["r1"].update(lineage_id="ghost")),
    ("renamed-reservation", lambda budget: budget["reservations"]["r1"].update(reservation_id="other")),
    ("broken-reservation", lambda budget: budget["reservations"].update(extra={"reservation_id": "extra"})),
    ("grant-unknown-kind",
     lambda budget: budget["grants"].update(d={"decision_id": "d", "kind": "calls", "amount": 1, "lineage_id": None})),
    ("grant-renamed",
     lambda budget: budget["grants"].update(d={"decision_id": "other", "kind": "attempts", "amount": 1, "lineage_id": None})),
    ("grant-zero",
     lambda budget: budget["grants"].update(d={"decision_id": "d", "kind": "attempts", "amount": 0, "lineage_id": None})),
    ("zero-limit", lambda budget: budget["limits"]["root"].update(attempts=0)),
    ("bad-limits", lambda budget: budget["limits"].update(extra={})),
]


@pytest.mark.parametrize("mutate", [case[1] for case in CORRUPTIONS],
                         ids=[case[0] for case in CORRUPTIONS])
def test_corrupt_stored_budget_is_rejected_without_repair(mutate):
    value = seeded()
    mutate(value["lineage_budget"])
    before = snapshot(value)
    calls = [
        lambda: lineage_budget.ensure(value),
        lambda: lineage_budget.reserve(value, "r2", lineage_id="lin", kind="attempts"),
        lambda: lineage_budget.settle(value, "r1"),
        lambda: lineage_budget.release(value, "r1"),
        lambda: lineage_budget.grant(value, "d", kind="attempts", amount=1),
    ]
    for call in calls:
        with pytest.raises(ValueError):
            call()
        assert snapshot(value) == before


# --------------------------------------------------------------------------- bad types


def test_wrong_types_raise_value_error_and_are_never_stored():
    value = core()
    value["policy"] = ["nope"]
    with pytest.raises(ValueError, match="policy"):
        lineage_budget.ensure(value)
    assert "lineage_budget" not in value

    value = core()
    value["attempts"] = ["nope"]
    with pytest.raises(ValueError, match="attempts"):
        lineage_budget.ensure(value)
    assert "lineage_budget" not in value

    value = core(attempts={"bad": "not-a-row"})
    with pytest.raises(ValueError, match="字典"):
        lineage_budget.ensure(value)
    assert "lineage_budget" not in value

    value = core(attempts={"bad": attempt("bad")})
    value["contracts"] = {"work": ["not-a-contract"]}
    with pytest.raises(ValueError, match="合同"):
        lineage_budget.ensure(value)
    assert "lineage_budget" not in value

    value = core()
    for bad in (["attempts"], {"attempts": 1}, 3, None):
        with pytest.raises(ValueError, match="未知预算类型"):
            lineage_budget.reserve(value, "r", lineage_id="lin", kind=bad)
        with pytest.raises(ValueError, match="未知预算类型"):
            lineage_budget.grant(value, "d", kind=bad, amount=1)
    with pytest.raises(ValueError, match="非空文本"):
        lineage_budget.reserve(value, "r", lineage_id=["lin"], kind="attempts")
    with pytest.raises(ValueError, match="非空文本"):
        lineage_budget.grant(value, ["d"], kind="attempts", amount=1)
    assert "lineage_budget" not in value


def test_invalid_kinds_amounts_and_identities_are_rejected():
    value = core()
    with pytest.raises(ValueError, match="未知预算类型"):
        lineage_budget.reserve(value, "r", lineage_id="lin", kind="calls")
    with pytest.raises(ValueError, match="未知预算类型"):
        lineage_budget.grant(value, "d", kind="calls", amount=1)
    with pytest.raises(ValueError, match="正整数"):
        lineage_budget.reserve(value, "r", lineage_id="lin", kind="attempts", amount=0)
    with pytest.raises(ValueError, match="正整数"):
        lineage_budget.reserve(value, "r", lineage_id="lin", kind="attempts", amount=True)
    with pytest.raises(ValueError, match="正整数"):
        lineage_budget.grant(value, "d", kind="attempts", amount=-1)
    with pytest.raises(ValueError, match="非空文本"):
        lineage_budget.reserve(value, "", lineage_id="lin", kind="attempts")
    with pytest.raises(ValueError, match="非空文本"):
        lineage_budget.reserve(value, "r", lineage_id="", kind="attempts")
    with pytest.raises(ValueError, match="非空文本"):
        lineage_budget.grant(value, "", kind="attempts", amount=1)
    assert lineage_budget.ensure(value)["reservations"] == {}
    assert lineage_budget.ensure(value)["grants"] == {}


def test_invalid_policy_limits_are_rejected_without_creating_budget():
    value = core(limits={"root": {"attempts": 0}})
    with pytest.raises(ValueError, match="正整数"):
        lineage_budget.ensure(value)
    assert "lineage_budget" not in value
    value = core(limits={"lineage": {"revisions": True}})
    with pytest.raises(ValueError, match="正整数"):
        lineage_budget.ensure(value)
    value = core(limits={"root": {"calls": 10}})
    with pytest.raises(ValueError, match="只能包含"):
        lineage_budget.ensure(value)
    value = core(limits={"other": {}})
    with pytest.raises(ValueError, match="root 与 lineage"):
        lineage_budget.ensure(value)


def test_attempt_without_any_lineage_identity_is_rejected():
    value = core(attempts={"bad": {"id": "bad", "status": "accepted"}})
    with pytest.raises(ValueError, match="谱系"):
        lineage_budget.ensure(value)
    assert "lineage_budget" not in value


@pytest.mark.parametrize("operation", ["reserve", "settle"])
def test_first_idempotent_historical_operation_persists_migration(operation):
    value = core(attempts={"old": attempt("old")})
    if operation == "reserve":
        lineage_budget.reserve(value, "attempt:old", lineage_id="work", kind="attempts")
    else:
        lineage_budget.settle(value, "attempt:old")
    assert value["lineage_budget"]["root"]["used"]["attempts"] == 1
    value["attempts"] = {}
    assert lineage_budget.ensure(value)["root"]["used"]["attempts"] == 1
