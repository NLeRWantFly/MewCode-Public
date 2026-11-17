"""Host effect receipts are source-bound and never imply business acceptance."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from mewcode.orchestration.paused_handoff_index import PausedHandoffIndex
from mewcode.orchestration.chat_reports import ChatReportStore
from mewcode.orchestration.paused_handoff_gate import evaluate_handoff_gate
from mewcode.orchestration.paused_handoff_reconciliation import (
    HostReconciliationLedger, reconciliation_worklist, unresolved_hard_holds,
)
from mewcode.orchestration.paused_handoff_review import ReviewLedger, preflight
from mewcode.orchestration.paused_handoff_ui import reconcile_authorized_handoff_page
from mewcode.orchestration.takeover import paused_handoff_boundaries


RUN_ID = "a" * 32


def _case(tmp_path: Path, *, tool: str = "WriteFile", error: bool = False,
          source_only: bool = False, duplicate_result: str = ""):
    tmp_path.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "result.txt"
    target.write_text("verified bytes", encoding="utf-8")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    call_id = "call-exact-1"
    records = [
        {"type": "assistant", "content": [{"type": "tool_use", "id": call_id,
          "name": tool, "input": {"file_path": str(target), "content": "verified bytes"}}]},
        {"type": "tool_result", "tool_use_id": call_id,
         "content": (f"Successfully wrote to {target}" if source_only else "done"),
         "is_error": error},
    ]
    if duplicate_result:
        records.append({"type": "tool_result", "tool_use_id": call_id,
                        "content": (records[1]["content"] if duplicate_result == "same"
                                    else "conflicting result"),
                        "is_error": error, "timestamp": "later"})
    source = tmp_path / "chat.jsonl"
    source.write_bytes(b"".join((json.dumps(record) + "\n").encode()
                                for record in records))
    index = PausedHandoffIndex.build(
        log_path=source, root=tmp_path / "handoff-reviews", run_id=RUN_ID,
        conversation_id="chat-a", contract_fingerprint="contract-v1", start=0,
    )
    identity = f"node-a\0\0{1}\0{call_id}\0{target.resolve()}\0{digest}"
    receipt = {"id": hashlib.sha256(identity.encode()).hexdigest()[:32],
               "node_id": "node-a", "member_id": "", "node_attempt": 1,
               "tool_id": call_id, "path": str(target.resolve()),
               "size": target.stat().st_size, "sha256": digest,
               "source": "successful_write", "accepted": False}
    snapshot = {"run_id": RUN_ID, "conversation_id": "chat-a", "status": "paused",
                "process_file_receipts": [] if source_only else [receipt],
                "pending_dialogue_handoff": {"handoff_review_progress": {
                    "source_id": index.manifest["id"]}}}
    return index, snapshot, target


def test_each_exact_host_effect_gets_a_replayable_receipt(tmp_path: Path) -> None:
    index, snapshot, target = _case(tmp_path)
    before = reconciliation_worklist(index, snapshot)
    assert before["total"] == 2 and before["verified_count"] == 0
    result = reconcile_authorized_handoff_page(
        store_root=tmp_path, snapshot=snapshot, run_id=RUN_ID,
        conversation_id="chat-a",
    )
    assert len(result["attempts"]) == 2
    assert result["page"]["verified_count"] == 2
    assert result["page"]["remaining_count"] == 0
    assert {item["status"] for item in result["page"]["items"]} == {"verified"}
    reopened = HostReconciliationLedger(index, snapshot)
    verified = reopened.verified()
    assert len(verified) == 2
    assert not unresolved_hard_holds(index, verified)
    assert preflight(index, remaining_tokens=0, remaining_seconds=None,
                     reconciled_records=verified)["deterministic_hold_records"] == 0
    assert paused_handoff_boundaries(snapshot, index=index)["unknown_side_effects"] is False
    unreviewed_gate = evaluate_handoff_gate(
        index, ReviewLedger(index), frozen_snapshot={}, current_snapshot={},
        final_decision="resume", reconciled_records=verified,
    )
    assert unreviewed_gate["decision"] == "hold"
    assert "hard_reconciliation_incomplete" not in unreviewed_gate["reasons"]
    assert "classification_incomplete" in unreviewed_gate["reasons"]
    missing_receipt_gate = evaluate_handoff_gate(
        index, ReviewLedger(index), frozen_snapshot={}, current_snapshot={},
        final_decision="resume",
    )
    assert "hard_reconciliation_incomplete" in missing_receipt_gate["reasons"]
    assert all(event["evidence"]["kind"] == "current_host_file_effect"
               for event in reopened.latest.values())
    assert all(event["status"] == "verified" for event in reopened.latest.values())
    # The proof is not a permanent checkbox: current bytes are checked again.
    target.write_text("later edit", encoding="utf-8")
    assert reconciliation_worklist(index, snapshot)["verified_count"] == 0
    assert paused_handoff_boundaries(snapshot, index=index)["unknown_side_effects"] is True


@pytest.mark.parametrize("tool,error", [("Bash", False), ("WriteFile", True)])
def test_open_ended_or_error_effect_remains_unresolved(
    tmp_path: Path, tool: str, error: bool,
) -> None:
    index, snapshot, _target = _case(tmp_path, tool=tool, error=error)
    result = reconcile_authorized_handoff_page(
        store_root=tmp_path, snapshot=snapshot, run_id=RUN_ID,
        conversation_id="chat-a",
    )
    assert result["page"]["verified_count"] == 0
    assert result["page"]["remaining_count"] == 2
    assert {item["status"] for item in result["page"]["items"]} == {"unresolved"}


def test_candidate_receipt_and_wrong_owner_cannot_clear_hold(tmp_path: Path) -> None:
    index, snapshot, _target = _case(tmp_path)
    snapshot["process_file_receipts"][0]["accepted"] = True
    assert reconciliation_worklist(index, snapshot)["verified_count"] == 0
    with pytest.raises(PermissionError):
        reconcile_authorized_handoff_page(
            store_root=tmp_path, snapshot=snapshot, run_id=RUN_ID,
            conversation_id="another",
        )
    result = reconcile_authorized_handoff_page(
        store_root=tmp_path, snapshot=snapshot, run_id=RUN_ID,
        conversation_id="chat-a",
    )
    assert result["page"]["verified_count"] == 0
    assert result["page"]["remaining_count"] == 2
    Path(index.manifest["source"]["log_path"]).write_bytes(b"changed\n")
    with pytest.raises(ValueError, match="source"):
        reconciliation_worklist(index, snapshot)


def test_host_can_reconcile_exact_logged_write_without_prior_file_receipt(
    tmp_path: Path,
) -> None:
    index, snapshot, target = _case(tmp_path, source_only=True)
    page = reconcile_authorized_handoff_page(
        store_root=tmp_path, snapshot=snapshot, run_id=RUN_ID,
        conversation_id="chat-a",
    )["page"]
    assert page["verified_count"] == 2
    assert {event["evidence"]["calls"][0]["proof"]
            for event in HostReconciliationLedger(index, snapshot).latest.values()} == {
                "source_and_current_bytes"}
    target.write_text("wrong bytes", encoding="utf-8")
    assert reconciliation_worklist(index, snapshot)["verified_count"] == 0


def test_current_conversation_chat_receipt_is_host_evidence(tmp_path: Path) -> None:
    index, snapshot, _target = _case(tmp_path)
    receipt = snapshot["process_file_receipts"].pop()
    ChatReportStore().record_file(work_dir=tmp_path, conversation_id="chat-a",
                                  record=receipt)
    assert reconciliation_worklist(index, snapshot)["verified_count"] == 0
    page = reconcile_authorized_handoff_page(
        store_root=tmp_path, snapshot=snapshot, run_id=RUN_ID,
        conversation_id="chat-a", work_dir=tmp_path,
    )["page"]
    assert page["verified_count"] == 2
    assert paused_handoff_boundaries(snapshot, index=index,
                                     work_dir=tmp_path)["unknown_side_effects"] is False


def test_duplicate_host_results_must_agree_exactly(tmp_path: Path) -> None:
    matching, snapshot, _target = _case(
        tmp_path / "matching", source_only=True, duplicate_result="same",
    )
    assert reconcile_authorized_handoff_page(
        store_root=tmp_path / "matching", snapshot=snapshot, run_id=RUN_ID,
        conversation_id="chat-a",
    )["page"]["verified_count"] == 3
    conflicting, other_snapshot, _target = _case(
        tmp_path / "conflicting", source_only=True, duplicate_result="conflict",
    )
    assert reconcile_authorized_handoff_page(
        store_root=tmp_path / "conflicting", snapshot=other_snapshot,
        run_id=RUN_ID, conversation_id="chat-a",
    )["page"]["verified_count"] == 0


def test_pre_anchor_call_is_verified_from_exact_prior_source_bytes(
    tmp_path: Path,
) -> None:
    target = tmp_path / "older.txt"
    target.write_text("older content", encoding="utf-8")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    call_id = "call-before-anchor"
    log = tmp_path / "chat.jsonl"
    call = {"type": "assistant", "content": [{"type": "tool_use", "id": call_id,
            "name": "WriteFile", "input": {"file_path": str(target),
            "content": "older content"}}]}
    log.write_bytes((json.dumps(call) + "\n").encode())
    start = log.stat().st_size
    with log.open("ab") as handle:
        handle.write((json.dumps({"type": "tool_result", "tool_use_id": call_id,
                                  "content": "done", "is_error": False}) + "\n").encode())
    index = PausedHandoffIndex.build(
        log_path=log, root=tmp_path / "handoff-reviews", run_id=RUN_ID,
        conversation_id="chat-a", contract_fingerprint="contract-v1", start=start,
    )
    assert index.manifest["records"][0]["call_provenance"] == "matched_prior_session_record"
    identity = f"node-a\0\0{1}\0{call_id}\0{target.resolve()}\0{digest}"
    receipt = {"id": hashlib.sha256(identity.encode()).hexdigest()[:32],
               "node_id": "node-a", "member_id": "", "node_attempt": 1,
               "tool_id": call_id, "path": str(target.resolve()),
               "size": target.stat().st_size, "sha256": digest,
               "source": "successful_write", "accepted": False}
    snapshot = {"run_id": RUN_ID, "conversation_id": "chat-a", "status": "paused",
                "process_file_receipts": [receipt]}
    ledger = HostReconciliationLedger(index, snapshot)
    assert ledger.reconcile(index.manifest["records"][0]["id"])["status"] == "verified"
    # Changing the pre-anchor source revokes the proof even if the frozen
    # interval itself did not change.
    with log.open("r+b") as handle:
        handle.write(b"X")
    with pytest.raises(ValueError, match="prior handoff call provenance changed"):
        HostReconciliationLedger(index, snapshot)


def test_verified_effect_is_reproved_in_append_only_generation(tmp_path: Path) -> None:
    index, snapshot, target = _case(tmp_path)
    original = HostReconciliationLedger(index, snapshot)
    for item in reconciliation_worklist(index, snapshot)["items"]:
        assert original.reconcile(item["record_id"])["status"] == "verified"
    source = Path(index.manifest["source"]["log_path"])
    with source.open("ab") as handle:
        handle.write(b'{"type":"user","content":"later context"}\n')
    successor = PausedHandoffIndex.build(
        log_path=source, root=tmp_path / "handoff-reviews", run_id=RUN_ID,
        conversation_id="chat-a", contract_fingerprint="contract-v1", start=0,
        generation=2, parent_source_id=index.manifest["id"],
    )
    current = HostReconciliationLedger(successor, snapshot)
    assert current.import_verified_prefix(original) == 2
    assert len(current.verified()) == 2
    assert current.import_verified_prefix(original) == 2  # idempotent
    target.write_text("changed", encoding="utf-8")
    assert current.verified() == {}
    with pytest.raises(ValueError, match="ancestor"):
        original.import_verified_prefix(current)
