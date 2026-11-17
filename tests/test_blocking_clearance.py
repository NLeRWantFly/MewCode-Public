from mewcode.experts.blocking_policy import (
    BLOCKER_CLASS, BLOCKER_CLEARANCE, CLEARANCE_PRODUCERS, refresh_clearances,
)
from mewcode.experts.node_ledger import ledger_for_run


def test_every_blocker_has_classification_and_clearance_producer():
    assert set(BLOCKER_CLEARANCE) == set(BLOCKER_CLASS)
    assert set(BLOCKER_CLEARANCE.values()) <= set(CLEARANCE_PRODUCERS)
    assert all(callable(producer) for producer in CLEARANCE_PRODUCERS.values())


def test_provider_recovers_after_a_real_host_completed_response():
    run = {}
    ledger = ledger_for_run(run)
    blocker = ledger.record("N", "blocker", {"kind": "provider_error", "ref": "model1"})
    ledger.record("N", "progress", {"model_response_completed": True}, source="node")
    assert not refresh_clearances(run, "N")
    ledger.record("N", "progress", {"model_response_completed": True, "response_id": "model2"})
    receipts = refresh_clearances(run, "N")
    assert len(receipts) == 1 and receipts[0].blocker_fact_id == blocker.fact_id
    assert not ledger.view("N").active_blockers


def test_capability_requires_available_tool_and_successful_bound_probe():
    run = {}
    ledger = ledger_for_run(run)
    blocker = ledger.record("N", "blocker", {"kind": "capability_missing", "ref": "probe"})
    ledger.record("N", "receipt", {"request_id": "other", "probe_success": True, "required_tools_available": True})
    assert not refresh_clearances(run, "N")
    ledger.record("N", "receipt", {"request_id": "probe", "probe_success": True, "required_tools_available": True})
    assert refresh_clearances(run, "N")[0].blocker_fact_id == blocker.fact_id


def test_business_question_cannot_be_cleared_by_model_claim_or_unbound_answer():
    run = {}
    ledger = ledger_for_run(run)
    ledger.record("N", "blocker", {"kind": "human_question", "ref": "original"})
    ledger.record("N", "answer", {"request_id": "original", "answer": "yes"}, source="node")
    ledger.record("N", "answer", {"request_id": "other", "answer": "yes"}, source="user")
    assert not refresh_clearances(run, "N")
    ledger.record("N", "answer", {"request_id": "original", "answer": "yes"}, source="user")
    assert refresh_clearances(run, "N")


def test_real_permission_response_clears_only_the_exact_operation():
    from mewcode.agent import PermissionResponse
    from mewcode.experts.blocking_policy import record_permission_response
    run = {'node_states': {'N': {'pending_permission': {'tool_id': 'original'}}}}
    ledger = ledger_for_run(run)
    ledger.record('N', 'blocker', {'kind': 'permission', 'ref': 'other'})
    record_permission_response(run, 'N', 'original', PermissionResponse.DENY)
    assert {b.payload['ref'] for b in ledger.view('N').active_blockers} == {'original', 'other'}
    record_permission_response(run, 'N', 'original', PermissionResponse.ALLOW)
    assert {b.payload['ref'] for b in ledger.view('N').active_blockers} == {'other'}


def test_reviewed_start_retires_only_matching_review_never_unknown_effect():
    from mewcode.experts.blocking_policy import record_reviewed_tool_start
    run = {'node_states': {'N': {'tools': {
        'old': {'name': 'Bash', 'args': {'command': 'git status --short'}, 'status': 'failed'},
        'different': {'name': 'Bash', 'args': {'command': 'git push'}, 'status': 'failed'}}}}}
    ledger = ledger_for_run(run)
    for kind, ref in [('legacy_safety_review', 'old'), ('legacy_safety_review', 'different'), ('effect_unknown', 'old')]:
        ledger.record('N', 'blocker', {'kind': kind, 'ref': ref})
    record_reviewed_tool_start(run, 'N', 'new', 'Bash', {'command': 'git status --short'})
    assert {(b.payload['kind'], b.payload['ref']) for b in ledger.view('N').active_blockers} == {
        ('legacy_safety_review', 'different'), ('effect_unknown', 'old')}
