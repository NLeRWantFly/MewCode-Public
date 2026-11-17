"""Ownership-checked, bounded readback for a paused handoff review view."""
from __future__ import annotations

import re
from pathlib import Path

from mewcode.orchestration.paused_handoff_compression import expand_visible_reference
from mewcode.orchestration.paused_handoff_index import PausedHandoffIndex
from mewcode.orchestration.paused_handoff_reconciliation import (
    HostReconciliationLedger, reconciliation_worklist,
)


_RUN_ID = re.compile(r"^[a-f0-9]{32}$")
_SOURCE_ID = re.compile(r"^[a-f0-9]{64}$")


def _authorized_index(
    *, store_root: Path, snapshot: dict, run_id: str,
    conversation_id: str, source_id: str = "",
) -> PausedHandoffIndex:
    if (not _RUN_ID.fullmatch(run_id)
            or snapshot.get("run_id") != run_id
            or snapshot.get("conversation_id") != conversation_id):
        raise PermissionError("handoff source does not belong to this conversation")
    pending = snapshot.get("pending_dialogue_handoff") or {}
    decisions = [pending]
    decisions.extend(item.get("decision") for item in
        reversed(snapshot.get("retired_paused_handoff_reviews") or []) if isinstance(item, dict))
    decisions.extend(item.get("decision") for item in
        reversed(snapshot.get("dialogue_handoff_reviews") or []) if isinstance(item, dict))
    sources = []
    for decision in decisions:
        progress = decision.get("handoff_review_progress") if isinstance(decision, dict) else None
        bound = progress.get("source_id") if isinstance(progress, dict) else None
        if isinstance(bound, str) and _SOURCE_ID.fullmatch(bound) and bound not in sources:
            sources.append(bound)
    selected = source_id or (sources[0] if sources else "")
    if not isinstance(selected, str) or selected not in sources:
        raise PermissionError("no bound handoff source")
    path = Path(store_root) / "handoff-reviews" / run_id / f"{selected}.index.json"
    index = PausedHandoffIndex.load(path)
    if (index.manifest["id"] != selected
            or index.manifest["source"]["run_id"] != run_id
            or index.manifest["source"]["conversation_id"] != conversation_id):
        raise ValueError("handoff source identity mismatch")
    return index


def read_authorized_handoff_excerpt(
    *, store_root: Path, snapshot: dict, run_id: str,
    conversation_id: str, reference_id: str,
    view_offset: int = 0, source_id: str = "",
) -> dict:
    index = _authorized_index(store_root=store_root, snapshot=snapshot,
                              run_id=run_id, conversation_id=conversation_id, source_id=source_id)
    return {**expand_visible_reference(index, reference_id,
                                      view_offset=view_offset, max_chars=4096),
            "source_id": index.manifest["id"]}


def read_authorized_handoff_worklist(
    *, store_root: Path, snapshot: dict, run_id: str,
    conversation_id: str, offset: int = 0,
    work_dir: str | Path | None = None, source_id: str = "",
) -> dict:
    index = _authorized_index(store_root=store_root, snapshot=snapshot,
                              run_id=run_id, conversation_id=conversation_id, source_id=source_id)
    return reconciliation_worklist(index, snapshot, offset=offset,
                                   work_dir=work_dir)


def reconcile_authorized_handoff_page(
    *, store_root: Path, snapshot: dict, run_id: str,
    conversation_id: str, offset: int = 0,
    work_dir: str | Path | None = None,
) -> dict:
    """Host-check one bounded page; the client supplies no verdict or evidence."""
    if snapshot.get("status") not in {"paused", "blocked", "needs_attention"}:
        raise PermissionError("cannot mutate handoff evidence for an active or finished run")
    index = _authorized_index(store_root=store_root, snapshot=snapshot,
                              run_id=run_id, conversation_id=conversation_id)
    page = reconciliation_worklist(index, snapshot, offset=offset,
                                   work_dir=work_dir)
    ledger = HostReconciliationLedger(index, snapshot, work_dir=work_dir)
    receipts = ledger.reconcile_many([item["record_id"] for item in page["items"]])
    attempts = [{"record_id": receipt["record_id"],
                 "status": receipt["status"], "reason": receipt["reason"]}
                for receipt in receipts]
    return {"page": reconciliation_worklist(index, snapshot, offset=offset,
                                             work_dir=work_dir),
            "attempts": attempts}
