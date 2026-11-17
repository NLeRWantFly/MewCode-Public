import copy

import pytest

from mewcode.experts.node_ledger import LedgerFact, NodeLedger, migrate_legacy_node_state


def test_epochs_keep_success_and_artifacts_without_old_blockers_or_questions():
    ledger = NodeLedger()
    blocker = ledger.record("N", "blocker", {"kind": "capability_missing"})
    ledger.record("N", "question", {"request_id": "q"})
    ledger.record("N", "receipt", {"request_id": "success", "status": "completed", "exit_code": 0})
    ledger.record("N", "receipt", {"request_id": "unknown", "status": "outcome_unknown", "effect_class": "readonly"})
    ledger.record("N", "artifact", {"path": "result.json"})
    assert ledger.bump_epoch("N", reason="maintenance_applied", source_ref="fix:1") == 1
    view = ledger.view("N")
    assert view.phase == "ready" and view.epoch == 1
    assert not view.active_blockers and view.pending_question is None
    assert [f.payload["request_id"] for f in view.current_receipts] == ["success"]
    assert len(view.artifacts) == 1
    assert next(f for f in view.history if f.fact_id == blocker.fact_id).superseded_by
    assert ledger.storage["N"]["facts"][0]["superseded_by"] is None
    assert ledger.bump_epoch("N", reason="maintenance_applied", source_ref="fix:1") == 1


def test_phase_and_epoch_sources_are_validated():
    ledger = NodeLedger()
    with pytest.raises(ValueError):
        ledger.set_phase("N", "paused", reason="test")
    with pytest.raises(ValueError):
        ledger.append(LedgerFact("bad", "N", 0, "phase_change", {"phase": "unknown"}, "host", 0))
    with pytest.raises(ValueError):
        ledger.bump_epoch("N", reason="continue", source_ref="user")


def test_epoch_change_cannot_erase_unverified_write_effect():
    ledger = NodeLedger()
    ledger.record('N', 'receipt', {'request_id': 'write', 'status': 'outcome_unknown', 'effect_class': 'write'})
    with pytest.raises(ValueError, match='效果未知'):
        ledger.bump_epoch('N', reason='maintenance_applied', source_ref='fix')
    assert ledger.view('N').epoch == 0


def test_completed_node_with_evidence_cannot_be_rebased():
    ledger = NodeLedger()
    ledger.record("N", "artifact", {"path": "result.json"})
    ledger.set_phase("N", "done", reason="verified")
    with pytest.raises(ValueError):
        ledger.bump_epoch("N", reason="maintenance_applied", source_ref="fix")


def test_clearance_must_be_host_verified_and_match_blocker():
    ledger = NodeLedger()
    blocker = ledger.record("N", "blocker", {"kind": "provider_error"})
    ledger.record("N", "clearance", {"blocker_fact_id": blocker.fact_id}, source="node")
    assert ledger.view("N").active_blockers
    ledger.record("N", "clearance", {"blocker_fact_id": blocker.fact_id, "evidence_ref": "response"})
    assert not ledger.view("N").active_blockers


def test_legacy_migration_is_idempotent_paused_and_preserves_evidence():
    run = {"status": "running", "node_states": {
        "done": {"status": "completed", "handoff": {"summary": "done", "artifacts": [{"path": "done.json"}]},
                 "command_receipts": [{"request_id": "r", "status": "completed", "exit_code": 0}]},
        "blocked": {"status": "needs_attention", "blockers": [{"kind": "capability_missing"}],
                    "pending_interaction": {"request_id": "q", "tool": "AskUserQuestion"}}}}
    migrated = migrate_legacy_node_state(run)
    ledger = NodeLedger(migrated["node_ledgers"])
    assert migrated["status"] == "paused" and migrated["ledger_migration"]["model_calls"] == 0
    assert ledger.view("done").phase == "done"
    assert len(ledger.view("done").artifacts) == len(ledger.view("done").current_receipts) == 1
    assert ledger.view("blocked").active_blockers
    before = copy.deepcopy(migrated)
    assert migrate_legacy_node_state(migrated) == before


def test_fact_payload_is_snapshotted_and_duplicate_identity_is_rejected():
    ledger = NodeLedger()
    payload = {"path": "original"}
    fact = LedgerFact("id", "N", 0, "artifact", payload, "host", 0)
    ledger.append(fact)
    payload["path"] = "changed"
    assert ledger.view("N").artifacts[0].payload["path"] == "original"
    with pytest.raises(ValueError):
        ledger.append(fact)
