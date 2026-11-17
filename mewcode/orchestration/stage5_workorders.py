"""Read-only deterministic planning for Stage 5 repair work orders.

The planner consumes an already-recorded review queue. It does not mutate the
queue, approve business changes, or create files. Its batches use the same
identity and ``batch_id`` recipe as :class:`RepairWorkBatch`; the caller can
pass each plan's ``item_ids``, ``item_revisions``, and ``expected_revision`` to
that host tool's ``freeze`` action.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from mewcode.experts.repair_lifecycle import item_record_revision


SCHEMA_VERSION = 1
MAX_BATCH_SIZE = 10
MAX_QUEUE_ITEMS = 10_000
MAX_BATCHES = 10_000


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def _revision(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _id(value: Any) -> str:
    return _revision(value)[:32]


def _page_key(item: Mapping[str, Any]) -> tuple[str, int, str]:
    page = item.get("pdf_page")
    page_number = (page if isinstance(page, int) and not isinstance(page, bool)
                   else 2**63 - 1)
    return (str(item.get("doc_id") or "").casefold(), page_number,
            str(item.get("item_id") or ""))


def _blocked(code: str, condition: str, *, expected_revision: int | None = None,
             counts: Mapping[str, int] | None = None,
             context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    blocker: dict[str, Any] = {
        "code": code,
        "clearance": {"code": code, "condition": condition},
    }
    if context:
        blocker.update(context)
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "blocked",
        "expected_revision": expected_revision,
        "counts": dict(counts or {"eligible": 0, "ungenerated": 0,
                                   "frozen": 0, "submitted": 0,
                                   "planned": 0, "planned_batches": 0}),
        "batches": [],
        "blockers": [blocker],
        "clearance": blocker["clearance"],
    }
    body["plan_id"] = _id(body)
    return body


def _stored_batch_items(batch: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows = batch.get("items")
    if not isinstance(rows, list):
        raise ValueError("batch_items_invalid")
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("batch_items_invalid")
        item_id = row.get("item_id")
        if not isinstance(item_id, str) or not item_id or item_id in result:
            raise ValueError("batch_items_invalid")
        result[item_id] = row
    return result


def _existing_generation(batches: Mapping[str, Any], identity: Mapping[str, Any]) -> int:
    expected_ids = identity["item_ids"]
    count = 0
    for batch in batches.values():
        if not isinstance(batch, Mapping):
            continue
        if (batch.get("owner") == identity["owner"]
                and batch.get("contract") == identity["contract"]
                and batch.get("item_ids") == expected_ids
                and batch.get("item_revisions") == identity["item_revisions"]):
            count += 1
    return count


def _components(eligible: Mapping[str, Mapping[str, Any]]) -> list[list[str]]:
    parent = {item_id: item_id for item_id in eligible}

    def find(item_id: str) -> str:
        while parent[item_id] != item_id:
            parent[item_id] = parent[parent[item_id]]
            item_id = parent[item_id]
        return item_id

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            # Deterministic representative, independent of queue iteration order.
            low, high = sorted((left_root, right_root))
            parent[high] = low

    first_for_issue: dict[str, str] = {}
    for item_id in sorted(eligible):
        for issue_id in eligible[item_id]["issue_ids"]:
            if issue_id in first_for_issue:
                union(item_id, first_for_issue[issue_id])
            else:
                first_for_issue[issue_id] = item_id
    groups: dict[str, list[str]] = {}
    for item_id in sorted(eligible):
        groups.setdefault(find(item_id), []).append(item_id)
    return sorted(groups.values(), key=lambda group: min(
        _page_key(eligible[item_id]["queue_item"]) for item_id in group))


def plan_stage5_workorders(
        queue: Mapping[str, Any], *, owner: str,
        contract_hash: str | None = None,
        batch_size: int = MAX_BATCH_SIZE) -> dict[str, Any]:
    """Plan every current needs_repair/unknown item not already work-ordered.

    ``owner`` must be the same owner string passed to ``RepairWorkBatch``.
    ``contract_hash`` defaults to the queue's frozen contract hash. Shared
    ``issue_ids`` connect items into indivisible groups; a connected group over
    ten items or a group split across existing work orders returns ``blocked``
    with no partial batch plan.

    Each returned batch is directly consumable by ``RepairWorkBatch(action=
    "freeze")``: pass its ``item_ids``, ``item_revisions`` and current queue
    ``expected_revision``. The item revisions provide per-record locks, so a
    later unrelated queue write does not stale the whole batch.
    """
    if not isinstance(queue, Mapping):
        return _blocked("review_queue_invalid", "Read a complete review queue before planning.")
    queue_revision = queue.get("revision")
    if (isinstance(queue_revision, bool) or not isinstance(queue_revision, int)
            or queue_revision < 0):
        return _blocked("queue_revision_invalid", "Read the current nonnegative queue revision.")
    if not isinstance(owner, str) or not owner.strip():
        return _blocked("owner_invalid", "Supply the exact RepairWorkBatch owner identity.",
                        expected_revision=queue_revision)
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or not 1 <= batch_size <= MAX_BATCH_SIZE:
        return _blocked("batch_size_invalid", "Use a batch size from 1 through 10.",
                        expected_revision=queue_revision)

    queue_contract = queue.get("contract_hash")
    contract = contract_hash if contract_hash is not None else queue_contract
    if not isinstance(contract, str) or not contract:
        return _blocked("contract_hash_unavailable", "Read the queue's frozen contract hash.",
                        expected_revision=queue_revision)
    if queue_contract is not None and contract != queue_contract:
        return _blocked("contract_hash_mismatch", "Use the contract hash frozen in this queue.",
                        expected_revision=queue_revision)
    items = queue.get("items")
    records = queue.get("records", {})
    batches = queue.get("repair_batches", {})
    if (not isinstance(items, list) or len(items) > MAX_QUEUE_ITEMS
            or not isinstance(records, Mapping) or not isinstance(batches, Mapping)
            or len(batches) > MAX_BATCHES):
        return _blocked("review_queue_incomplete", "Read a complete bounded queue and batch ledger.",
                        expected_revision=queue_revision)

    queue_items: dict[str, Mapping[str, Any]] = {}
    for item in items:
        if not isinstance(item, Mapping):
            return _blocked("queue_item_invalid", "Repair malformed queue items before planning.",
                            expected_revision=queue_revision)
        item_id = item.get("item_id")
        if not isinstance(item_id, str) or not item_id or item_id in queue_items:
            return _blocked("queue_item_identity_invalid", "Ensure queue item IDs are unique and stable.",
                            expected_revision=queue_revision)
        queue_items[item_id] = item

    eligible: dict[str, dict[str, Any]] = {}
    try:
        for item_id, queue_item in queue_items.items():
            record = records.get(item_id)
            if not isinstance(record, Mapping) or record.get("state") not in {"needs_repair", "unknown"}:
                continue
            issue_ids = record.get("issue_ids", [])
            if (not isinstance(issue_ids, list)
                    or any(not isinstance(issue_id, str) or not issue_id
                           for issue_id in issue_ids)
                    or len(issue_ids) > 30
                    or len(issue_ids) != len(set(issue_ids))):
                return _blocked("issue_ids_invalid", "Repair the affected records' issue IDs before planning.",
                                expected_revision=queue_revision)
            eligible[item_id] = {
                "record": record,
                "record_revision": item_record_revision(record),
                "issue_ids": sorted(issue_ids),
                "queue_item": queue_item,
            }
    except (TypeError, ValueError, OverflowError):
        return _blocked("record_revision_unavailable",
                        "Read valid deterministic review records before planning.",
                        expected_revision=queue_revision)

    # Determine which current record revisions already have an active or
    # submitted work order. A stale submitted version does not cover a changed
    # review decision; a stale frozen order does block new dispatch until it is
    # explicitly resolved.
    current_batches: dict[str, list[tuple[str, str]]] = {}
    for batch_key, batch in batches.items():
        if not isinstance(batch, Mapping):
            return _blocked("batch_ledger_invalid", "Repair the work-order ledger before planning.",
                            expected_revision=queue_revision)
        status = batch.get("status")
        if status not in {"frozen", "submitted", "released"}:
            return _blocked("batch_status_invalid", "Reconcile the work-order status before planning.",
                            expected_revision=queue_revision,
                            context={"batch_id": str(batch_key)})
        if status == "released":
            continue
        try:
            rows = _stored_batch_items(batch)
        except ValueError as exc:
            return _blocked(str(exc), "Repair batch item rows before planning.",
                            expected_revision=queue_revision,
                            context={"batch_id": str(batch_key)})
        for item_id, row in rows.items():
            current = eligible.get(item_id)
            if current is None:
                continue
            record_revision = row.get("record_revision")
            if (not isinstance(record_revision, str) or len(record_revision) != 64
                    or any(char not in "0123456789abcdef" for char in record_revision)):
                return _blocked("batch_record_revision_missing",
                                "Re-read the existing batch's per-item record revision.",
                                expected_revision=queue_revision,
                                context={"batch_id": str(batch_key), "item_id": item_id})
            if status == "frozen":
                if record_revision != current["record_revision"]:
                    return _blocked("active_batch_revision_stale",
                                    "Submit or release the stale frozen batch, then reread the queue.",
                                    expected_revision=queue_revision,
                                    context={"batch_id": str(batch_key), "item_id": item_id})
                current_batches.setdefault(item_id, []).append((str(batch_key), status))
            elif record_revision == current["record_revision"]:
                current_batches.setdefault(item_id, []).append((str(batch_key), status))

    frozen_ids: set[str] = set()
    submitted_ids: set[str] = set()
    for item_id, matches in current_batches.items():
        if len(matches) != 1:
            return _blocked("item_has_ambiguous_workorders",
                            "Reconcile duplicate current work orders before planning.",
                            expected_revision=queue_revision,
                            context={"item_id": item_id,
                                     "batch_ids": sorted(batch_id for batch_id, _ in matches)})
        batch_id, status = matches[0]
        (frozen_ids if status == "frozen" else submitted_ids).add(item_id)

    counts = {
        "eligible": len(eligible),
        "ungenerated": len(eligible) - len(frozen_ids) - len(submitted_ids),
        "frozen": len(frozen_ids),
        "submitted": len(submitted_ids),
        "planned": 0,
        "planned_batches": 0,
    }

    groups = _components(eligible)
    packed: list[list[str]] = []
    current_group: list[str] = []
    for group in groups:
        if len(group) > batch_size:
            return _blocked("coupled_issue_group_exceeds_batch_limit",
                            "Reconcile or split this coupled issue upstream; the planner will not split it.",
                            expected_revision=queue_revision, counts=counts,
                            context={"item_ids": sorted(group),
                                     "issue_ids": sorted({issue_id for item_id in group
                                                          for issue_id in eligible[item_id]["issue_ids"]})})
        claimed = {item_id for item_id in group
                   if item_id in frozen_ids or item_id in submitted_ids}
        if claimed:
            claimed_batches = {current_batches[item_id][0][0] for item_id in claimed}
            if len(claimed) != len(group) or len(claimed_batches) != 1:
                return _blocked("coupled_issue_group_split_by_existing_batch",
                                "Reconcile existing work orders so every coupled issue group stays together.",
                                expected_revision=queue_revision, counts=counts,
                                context={"item_ids": sorted(group),
                                         "batch_ids": sorted(claimed_batches)})
            continue
        if current_group and len(current_group) + len(group) > batch_size:
            packed.append(current_group)
            current_group = []
        current_group.extend(group)
    if current_group:
        packed.append(current_group)

    planned_batches: list[dict[str, Any]] = []
    for group in packed:
        item_ids = sorted(group)
        item_revisions = {item_id: eligible[item_id]["record_revision"]
                          for item_id in item_ids}
        identity = {"owner": owner, "item_ids": item_ids,
                    "contract": contract, "item_revisions": item_revisions}
        generation = _existing_generation(batches, identity)
        batch_id = "repair-" + _revision({**identity, "generation": generation})[:24]
        issue_ids = sorted({issue_id for item_id in item_ids
                            for issue_id in eligible[item_id]["issue_ids"]})
        batch_plan = {
            **identity,
            "batch_id": batch_id,
            "generation": generation,
            "expected_revision": queue_revision,
            "issue_ids": issue_ids,
            "items": [{"item_id": item_id,
                       "record_revision": item_revisions[item_id],
                       "issue_ids": eligible[item_id]["issue_ids"],
                       "doc_id": eligible[item_id]["queue_item"].get("doc_id"),
                       "pdf_page": eligible[item_id]["queue_item"].get("pdf_page")}
                      for item_id in item_ids],
        }
        planned_batches.append(batch_plan)

    planned_batches.sort(key=lambda batch: batch["batch_id"])
    counts["planned"] = sum(len(batch["item_ids"]) for batch in planned_batches)
    counts["planned_batches"] = len(planned_batches)
    body = {
        "schema_version": SCHEMA_VERSION,
        "status": "ready",
        "owner": owner,
        "contract_hash": contract,
        "expected_revision": queue_revision,
        "counts": counts,
        "batches": planned_batches,
        "blockers": [],
        "clearance": {"code": "none_required",
                      "condition": "Freeze each planned batch with its item revisions and expected queue revision."},
    }
    body["plan_id"] = _id(body)
    return body


__all__ = ["MAX_BATCH_SIZE", "plan_stage5_workorders"]
