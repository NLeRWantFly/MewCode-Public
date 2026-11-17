import json

import pytest

from mewcode.tools.repair_work_batch import RepairWorkBatchParams as Batch
from mewcode.tools.review_work_queue import ReadReviewQueue, ReadReviewQueueParams
from tests.test_repair_work_batch import fixture


@pytest.mark.asyncio
async def test_unrelated_queue_revision_does_not_invalidate_item_lock(tmp_path):
    _, worker, store, run_id, ids = await fixture(tmp_path)
    reader = ReadReviewQueue(store, run_id, ["check"], "frozen", tmp_path)
    response = await reader.execute(ReadReviewQueueParams(reviewer_node_id="check"))
    rows = {row["item_id"]: row for row in json.loads(response.output)["items"]}
    item = ids[0]
    old_global = store.load(run_id)["review_queues"]["check"]["revision"]
    def unrelated(snapshot):
        queue = snapshot["review_queues"]["check"]
        queue["records"][ids[1]]["summary"] = "changed unrelated review"
        queue["revision"] += 1
    store.mutate(run_id, unrelated, durable=True)
    result = await worker.execute(Batch(action="freeze", reviewer_node_id="check",
        expected_revision=old_global, item_ids=[item],
        item_revisions={item: rows[item]["record_revision"]}))
    assert not result.is_error, result.output
    frozen = json.loads(result.output)
    assert frozen["revision"] == old_global + 1
    assert frozen["item_revisions"][item] == rows[item]["record_revision"]
    # The legacy global-lock call remains conservative.
    legacy = await worker.execute(Batch(action="freeze", reviewer_node_id="check",
        expected_revision=old_global, item_ids=[ids[1]]))
    assert legacy.is_error


@pytest.mark.asyncio
async def test_same_item_revision_change_rejects_stale_claim(tmp_path):
    _, worker, store, run_id, ids = await fixture(tmp_path)
    reader = ReadReviewQueue(store, run_id, ["check"], "frozen", tmp_path)
    response = await reader.execute(ReadReviewQueueParams(reviewer_node_id="check"))
    old = next(row["record_revision"] for row in json.loads(response.output)["items"]
               if row["item_id"] == ids[0])
    store.mutate(run_id, lambda snapshot: snapshot["review_queues"]["check"]
        ["records"][ids[0]].update(summary="changed same item"), durable=True)
    denied = await worker.execute(Batch(action="freeze", reviewer_node_id="check",
        item_ids=[ids[0]], item_revisions={ids[0]: old}))
    assert denied.is_error and "record_revision" in denied.output
