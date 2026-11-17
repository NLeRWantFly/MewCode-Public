from __future__ import annotations

import copy

from mewcode.experts.repair_lifecycle import item_record_revision
from mewcode.orchestration.stage5_workorders import plan_stage5_workorders
from mewcode.tools.repair_work_batch import revision as batch_revision


OWNER = "stage5-repair"
CONTRACT = "contract-sha256"


def _queue(count: int, *, revision: int = 269) -> dict:
    items = []
    records = {}
    for page in range(1, count + 1):
        item_id = f"item-{page:04d}"
        items.append({"item_id": item_id, "doc_id": "book", "pdf_page": page})
        records[item_id] = {
            "state": "needs_repair",
            "issue_ids": [f"issue-{page:04d}"],
            "summary": f"Page {page} needs a repair",
            "evidence": [], "images": [],
        }
    return {
        "revision": revision,
        "contract_hash": CONTRACT,
        "items": items,
        "records": records,
        "repair_batches": {},
        "repair_candidates": {},
    }


def _existing_batch(queue: dict, item_ids: list[str], status: str,
                    *, owner: str = OWNER) -> dict:
    ordered = sorted(item_ids)
    item_revisions = {item_id: item_record_revision(queue["records"][item_id])
                      for item_id in ordered}
    return {
        "batch_id": f"{status}-existing",
        "owner": owner,
        "contract": CONTRACT,
        "item_ids": ordered,
        "item_revisions": item_revisions,
        "status": status,
        "items": [{"item_id": item_id,
                   "record_revision": item_revisions[item_id],
                   "record": copy.deepcopy(queue["records"][item_id])}
                  for item_id in ordered],
    }


def test_plan_is_deterministic_capped_and_uses_current_record_revisions() -> None:
    queue = _queue(23)
    queue["records"]["item-0023"]["state"] = "unknown"
    before = copy.deepcopy(queue)

    first = plan_stage5_workorders(queue, owner=OWNER)
    second = plan_stage5_workorders(queue, owner=OWNER)

    assert queue == before  # planning is read-only
    assert first == second
    assert first["status"] == "ready"
    assert first["expected_revision"] == 269
    assert first["counts"] == {
        "eligible": 23, "ungenerated": 23, "frozen": 0,
        "submitted": 0, "planned": 23, "planned_batches": 3,
    }
    assert sorted(len(batch["item_ids"]) for batch in first["batches"]) == [3, 10, 10]
    assert all(batch["expected_revision"] == 269 for batch in first["batches"])
    assert all(len(batch["item_ids"]) <= 10 for batch in first["batches"])
    assert {item_id for batch in first["batches"] for item_id in batch["item_ids"]} == {
        item["item_id"] for item in queue["items"]
    }
    for batch in first["batches"]:
        identity = {"owner": OWNER, "item_ids": batch["item_ids"],
                    "contract": CONTRACT, "item_revisions": batch["item_revisions"]}
        assert batch["batch_id"] == "repair-" + batch_revision(
            {**identity, "generation": batch["generation"]})[:24]
        for item_id in batch["item_ids"]:
            assert batch["item_revisions"][item_id] == item_record_revision(
                queue["records"][item_id])
    assert all(batch["batch_id"].startswith("repair-") for batch in first["batches"])


def test_items_sharing_a_coupled_issue_id_stay_in_one_batch() -> None:
    queue = _queue(12)
    queue["records"]["item-0001"]["issue_ids"] = ["coupled-issue"]
    queue["records"]["item-0012"]["issue_ids"] = ["coupled-issue"]

    plan = plan_stage5_workorders(queue, owner=OWNER)

    assert plan["status"] == "ready"
    containing = [batch for batch in plan["batches"]
                  if {"item-0001", "item-0012"}.issubset(batch["item_ids"])]
    assert len(containing) == 1
    assert "coupled-issue" in containing[0]["issue_ids"]


def test_oversized_coupled_group_fails_closed_without_partial_batches() -> None:
    queue = _queue(11)
    for record in queue["records"].values():
        record["issue_ids"] = ["one-coupled-issue"]

    plan = plan_stage5_workorders(queue, owner=OWNER)

    assert plan["status"] == "blocked"
    assert plan["batches"] == []
    assert plan["counts"]["ungenerated"] == 11
    assert plan["blockers"][0]["code"] == "coupled_issue_group_exceeds_batch_limit"
    assert plan["clearance"]["condition"]


def test_current_frozen_and_submitted_items_are_counted_and_skipped() -> None:
    queue = _queue(3)
    queue["repair_batches"] = {
        "frozen-1": _existing_batch(queue, ["item-0001"], "frozen"),
        "submitted-1": _existing_batch(queue, ["item-0002"], "submitted"),
    }

    plan = plan_stage5_workorders(queue, owner=OWNER)

    assert plan["status"] == "ready"
    assert plan["counts"] == {
        "eligible": 3, "ungenerated": 1, "frozen": 1,
        "submitted": 1, "planned": 1, "planned_batches": 1,
    }
    assert [item_id for batch in plan["batches"] for item_id in batch["item_ids"]] == ["item-0003"]


def test_stage5_sized_queue_plans_exactly_173_after_existing_nine() -> None:
    queue = _queue(182, revision=269)
    existing = [f"item-{page:04d}" for page in range(1, 10)]
    queue["repair_batches"] = {"existing-nine": _existing_batch(queue, existing, "frozen")}

    plan = plan_stage5_workorders(queue, owner=OWNER)

    assert plan["status"] == "ready"
    assert plan["counts"]["eligible"] == 182
    assert plan["counts"]["frozen"] == 9
    assert plan["counts"]["planned"] == 173
    assert plan["counts"]["planned_batches"] == 18
    assert set(item_id for batch in plan["batches"] for item_id in batch["item_ids"]) == {
        f"item-{page:04d}" for page in range(10, 183)
    }


def test_released_matching_generation_gets_the_next_stable_batch_id() -> None:
    queue = _queue(1)
    queue["repair_batches"] = {
        "released-1": _existing_batch(queue, ["item-0001"], "released"),
    }

    plan = plan_stage5_workorders(queue, owner=OWNER)

    batch = plan["batches"][0]
    identity = {"owner": OWNER, "item_ids": batch["item_ids"],
                "contract": CONTRACT, "item_revisions": batch["item_revisions"]}
    assert batch["generation"] == 1
    assert batch["batch_id"] == "repair-" + batch_revision(
        {**identity, "generation": 1})[:24]
    assert plan["counts"]["ungenerated"] == 1
    assert plan["counts"]["frozen"] == 0
    assert plan["counts"]["submitted"] == 0


def test_stale_submitted_work_order_does_not_cover_new_record_revision() -> None:
    queue = _queue(1, revision=270)
    old_batch = _existing_batch(queue, ["item-0001"], "submitted")
    old_revision = old_batch["items"][0]["record_revision"]
    queue["records"]["item-0001"]["summary"] = "Updated review decision"
    queue["repair_batches"] = {"old": old_batch}

    plan = plan_stage5_workorders(queue, owner=OWNER)

    assert plan["status"] == "ready"
    assert plan["expected_revision"] == 270
    batch = plan["batches"][0]
    assert batch["item_revisions"]["item-0001"] == item_record_revision(
        queue["records"]["item-0001"])
    assert batch["item_revisions"]["item-0001"] != old_revision
    assert batch["batch_id"] != old_batch["batch_id"]


def test_stale_frozen_order_blocks_until_reconciled() -> None:
    queue = _queue(1)
    old_batch = _existing_batch(queue, ["item-0001"], "frozen")
    queue["records"]["item-0001"]["summary"] = "Changed after freeze"
    queue["repair_batches"] = {"old": old_batch}

    plan = plan_stage5_workorders(queue, owner=OWNER)

    assert plan["status"] == "blocked"
    assert plan["batches"] == []
    assert plan["blockers"][0]["code"] == "active_batch_revision_stale"


def test_no_unresolved_review_records_produces_empty_complete_plan() -> None:
    queue = _queue(2)
    queue["records"]["item-0001"]["state"] = "complete"
    del queue["records"]["item-0002"]

    plan = plan_stage5_workorders(queue, owner=OWNER)

    assert plan["status"] == "ready"
    assert plan["batches"] == []
    assert plan["counts"] == {
        "eligible": 0, "ungenerated": 0, "frozen": 0,
        "submitted": 0, "planned": 0, "planned_batches": 0,
    }


def test_coupled_members_already_split_across_batches_fail_closed() -> None:
    queue = _queue(2)
    queue["records"]["item-0001"]["issue_ids"] = ["shared"]
    queue["records"]["item-0002"]["issue_ids"] = ["shared"]
    queue["repair_batches"] = {
        "frozen-1": _existing_batch(queue, ["item-0001"], "frozen"),
        "frozen-2": _existing_batch(queue, ["item-0002"], "frozen"),
    }

    plan = plan_stage5_workorders(queue, owner=OWNER)

    assert plan["status"] == "blocked"
    assert plan["batches"] == []
    assert plan["blockers"][0]["code"] == "coupled_issue_group_split_by_existing_batch"
