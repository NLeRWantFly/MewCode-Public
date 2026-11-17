from __future__ import annotations

import json
from pathlib import Path

import pytest

from mewcode.orchestration.paused_handoff_index import PausedHandoffIndex
from mewcode.orchestration.paused_handoff_ui import (
    read_authorized_handoff_excerpt, read_authorized_handoff_worklist,
)


RUN_ID = "a" * 32


def _source(tmp_path: Path) -> tuple[PausedHandoffIndex, dict]:
    log = tmp_path / "chat.jsonl"
    payload = {"type": "assistant", "content": [
        {"type": "thinking", "thinking": "private reasoning", "signature": "private-signature"},
        {"type": "text", "text": "Visible evidence."},
    ]}
    log.write_bytes((json.dumps(payload) + "\n").encode())
    index = PausedHandoffIndex.build(
        log_path=log, root=tmp_path / "handoff-reviews", run_id=RUN_ID,
        conversation_id="chat-a", contract_fingerprint="contract-v1", start=0,
    )
    snapshot = {"run_id": RUN_ID, "conversation_id": "chat-a", "status": "paused",
                "pending_dialogue_handoff": {"handoff_review_progress": {
                    "source_id": index.manifest["id"],
                }}}
    return index, snapshot


def test_only_owner_can_expand_source_and_private_blocks_are_hidden(tmp_path: Path) -> None:
    index, snapshot = _source(tmp_path)
    record_id = index.manifest["records"][0]["id"]
    view = read_authorized_handoff_excerpt(
        store_root=tmp_path, snapshot=snapshot, run_id=RUN_ID,
        conversation_id="chat-a", reference_id=record_id,
    )
    assert "Visible evidence" in view["visible_text"]
    assert "private reasoning" not in view["visible_text"]
    assert "private-signature" not in view["visible_text"]
    with pytest.raises(PermissionError):
        read_authorized_handoff_excerpt(
            store_root=tmp_path, snapshot=snapshot, run_id=RUN_ID,
            conversation_id="chat-other", reference_id=record_id,
        )
    with pytest.raises(KeyError):
        read_authorized_handoff_excerpt(
            store_root=tmp_path, snapshot=snapshot, run_id=RUN_ID,
            conversation_id="chat-a", reference_id="../../outside",
        )


def test_changed_source_cannot_be_expanded(tmp_path: Path) -> None:
    index, snapshot = _source(tmp_path)
    Path(index.manifest["source"]["log_path"]).write_bytes(b"changed\n")
    with pytest.raises(ValueError, match="source"):
        read_authorized_handoff_excerpt(
            store_root=tmp_path, snapshot=snapshot, run_id=RUN_ID,
            conversation_id="chat-a", reference_id=index.manifest["records"][0]["id"],
        )


def test_hard_hold_worklist_is_owned_paged_and_never_certifies_candidate_receipts(
    tmp_path: Path,
) -> None:
    log = tmp_path / "chat.jsonl"
    log.write_bytes(b"".join((json.dumps({
        "type": "tool_result", "tool_use_id": f"call-{i}",
        "content": "unknown result",
    }) + "\n").encode() for i in range(27)))
    index = PausedHandoffIndex.build(
        log_path=log, root=tmp_path / "handoff-reviews", run_id=RUN_ID,
        conversation_id="chat-a", contract_fingerprint="contract-v1", start=0,
    )
    snapshot = {"run_id": RUN_ID, "conversation_id": "chat-a", "status": "paused",
                "process_file_receipts": [{"tool_id": "call-0", "accepted": True}],
                "pending_dialogue_handoff": {"handoff_review_progress": {
                    "source_id": index.manifest["id"],
                }}}
    first = read_authorized_handoff_worklist(
        store_root=tmp_path, snapshot=snapshot, run_id=RUN_ID,
        conversation_id="chat-a",
    )
    assert first["total"] == 27
    assert len(first["items"]) == 25
    assert first["next_offset"] == 25
    assert first["items"][0]["candidate_host_receipts"] == 1
    assert first["items"][0]["status"] == "unresolved"
    assert first["verified_count"] == 0
    second = read_authorized_handoff_worklist(
        store_root=tmp_path, snapshot=snapshot, run_id=RUN_ID,
        conversation_id="chat-a", offset=25,
    )
    assert len(second["items"]) == 2
    assert second["next_offset"] is None
    with pytest.raises(PermissionError):
        read_authorized_handoff_worklist(
            store_root=tmp_path, snapshot=snapshot, run_id=RUN_ID,
            conversation_id="another",
        )
    with pytest.raises(ValueError):
        read_authorized_handoff_worklist(
            store_root=tmp_path, snapshot=snapshot, run_id=RUN_ID,
            conversation_id="chat-a", offset=-1,
        )


@pytest.mark.parametrize("status", ["paused", "running", "completed"])
@pytest.mark.parametrize("archive", ["retired_paused_handoff_reviews", "dialogue_handoff_reviews"])
def test_archived_source_remains_read_only_and_owner_bound(tmp_path, status, archive):
    index, snapshot = _source(tmp_path)
    source_id = index.manifest["id"]
    snapshot[archive] = [{"decision": snapshot.pop("pending_dialogue_handoff")}]
    snapshot["status"] = status
    before = sorted(str(path) for path in tmp_path.rglob("*"))
    view = read_authorized_handoff_excerpt(
        store_root=tmp_path, snapshot=snapshot, run_id=RUN_ID,
        conversation_id="chat-a", source_id=source_id,
        reference_id=index.manifest["records"][0]["id"],
    )
    assert view["source_id"] == source_id
    assert "Visible evidence" in view["visible_text"]
    page = read_authorized_handoff_worklist(
        store_root=tmp_path, snapshot=snapshot, run_id=RUN_ID,
        conversation_id="chat-a", source_id=source_id,
    )
    assert page["source_id"] == source_id
    assert sorted(str(path) for path in tmp_path.rglob("*")) == before
    with pytest.raises(PermissionError):
        read_authorized_handoff_worklist(
            store_root=tmp_path, snapshot=snapshot, run_id=RUN_ID,
            conversation_id="chat-other", source_id=source_id,
        )
    for unbound in ("b" * 64, "../outside"):
        with pytest.raises(PermissionError):
            read_authorized_handoff_worklist(
                store_root=tmp_path, snapshot=snapshot, run_id=RUN_ID,
                conversation_id="chat-a", source_id=unbound,
            )


def test_read_only_archive_ledger_cannot_write_or_import_evidence(tmp_path):
    from mewcode.orchestration.paused_handoff_reconciliation import HostReconciliationLedger

    index, snapshot = _source(tmp_path)
    ledger = HostReconciliationLedger(index, snapshot, read_only=True)
    with pytest.raises(PermissionError, match="cannot record"):
        ledger.reconcile(index.manifest["records"][0]["id"])
    with pytest.raises(PermissionError, match="cannot import"):
        ledger.import_verified_prefix(ledger)
    assert not ledger.path.exists()
    assert not ledger.path.with_suffix(ledger.path.suffix + ".lock").exists()
