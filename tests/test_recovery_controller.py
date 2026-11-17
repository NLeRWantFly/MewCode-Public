from mewcode.experts.node_ledger import NodeLedger, ledger_for_run
from mewcode.kernel.escalation import (
    RecoveryBudget, RecoveryContext, decide, decide_and_record, record_decision, run_assist_count,
)


def context(ledger=None, classes=(), trigger="error", progress=False, run_count=0):
    return RecoveryContext((ledger or NodeLedger()).view("N"), run_count, trigger, frozenset(classes), progress)


def test_default_budget_is_one_shared_table():
    assert RecoveryBudget() == RecoveryBudget(2, 1, 1, 20, 1, 5, 1)


def test_business_and_explicit_authority_need_user_but_unknown_effect_needs_verification():
    assert decide(context(classes=["business"])).action == "ask_human"
    assert decide(context(classes=["authority"])).action == "ask_human"
    ledger = NodeLedger()
    ledger.record("N", "blocker", {"kind": "effect_unknown"})
    choice = decide(context(ledger, ["authority"]))
    assert (choice.action, choice.assistance_kind) == ("assist", "effect_verification")


def test_progress_and_auto_remediation_continue_before_escalation():
    assert decide(context(progress=True)).action == "progress_continue"
    assert decide(context(classes=["auto"])).action == "progress_continue"


def test_escalation_reserves_budgets_and_does_not_reset_on_continue():
    run = {}
    ledger = ledger_for_run(run)
    choices = []
    for i in range(7):
        choice = decide_and_record(run, "N", trigger="segment_exhausted", identity=f"segment{i}")
        choices.append(choice.action)
    assert choices == ["progress_continue", "self_diagnose", "patch_node", "ask_human", "ask_human", "ask_human", "ask_human"]
    before = run_assist_count(run)
    for i in range(3):
        ledger.record("N", "answer", {"answer": "continue", "id": i}, source="user")
        assert decide(context(ledger, run_count=before)).action == "ask_human"
    assert run_assist_count(run) == before == 0


def test_same_decision_identity_is_not_charged_twice_and_run_limit_applies():
    run = {}
    first = decide_and_record(run, "N", trigger="assistance_requested", identity="request")
    assert decide_and_record(run, "N", trigger="assistance_requested", identity="request") == first
    assert run_assist_count(run) == 1
    choice = decide(context(trigger="replan_execution_method", run_count=20))
    assert choice.action != "assist"


def test_planner_method_change_uses_assistance():
    assert decide(context(trigger="replan_execution_method")).action == "assist"


def test_automatic_blockers_escalate_after_two_unproductive_segments():
    run = {}
    ledger_for_run(run).record("N", "blocker", {"kind": "capability_missing"})
    assert decide_and_record(run, "N", trigger="segment_exhausted", identity="1").action == "progress_continue"
    assert decide_and_record(run, "N", trigger="segment_exhausted", identity="2").action == "self_diagnose"


def test_epoch_limit_requires_user_without_creating_more_epochs():
    ledger = NodeLedger()
    for i in range(5):
        ledger.bump_epoch("N", reason="explicit_rebase", source_ref=str(i))
    assert decide(context(ledger, progress=True)).action == "ask_human"
