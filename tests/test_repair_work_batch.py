import copy
import json

import pytest
from PIL import Image

from mewcode.expert_runs import ExpertRunStore
from mewcode.tools.repair_work_batch import RepairWorkBatch, RepairWorkBatchParams as Batch
from mewcode.tools.review_work_queue import ReviewWorkQueue, ReviewWorkQueueParams as Review
from tests.test_review_work_queue import setup_queue, digest


async def fixture(root):
    reviewer, init, store, run_id = setup_queue(root)
    assert not (await reviewer.execute(init)).is_error
    queue = store.load(run_id)["review_queues"]["check"]
    ids = [i["item_id"] for i in queue["items"]]
    def seed(s):
        q = s["review_queues"]["check"]
        for item_id in ids:
            q["records"][item_id] = {"state": "needs_repair", "issue_ids": [item_id + "-issue"],
                "review_round": 0, "images": [], "evidence": [{"path": "left.md", "sha256": digest(root / "left.md")} ]}
        q["revision"] = 2
    store.mutate(run_id, seed, durable=True)
    worker = RepairWorkBatch(store, run_id, ["check"], "frozen", root, "repair")
    return reviewer, worker, store, run_id, ids


async def freeze(worker, ids, revision=2):
    result = await worker.execute(Batch(action="freeze", reviewer_node_id="check",
                                      expected_revision=revision, item_ids=ids))
    assert not result.is_error, result.output
    return json.loads(result.output)


@pytest.mark.asyncio
async def test_plan_is_read_only_and_freeze_compatible(tmp_path):
    _, worker, store, run_id, ids = await fixture(tmp_path)
    before = store.load(run_id)["review_queues"]["check"]
    result = await worker.execute(Batch(action="plan", reviewer_node_id="check"))
    assert not result.is_error, result.output
    plan = json.loads(result.output)
    assert plan["status"] == "ready"
    assert plan["counts"]["planned"] == len(ids)
    assert store.load(run_id)["review_queues"]["check"] == before
    batch = plan["batches"][0]
    frozen = await worker.execute(Batch(action="freeze", reviewer_node_id="check",
        expected_revision=batch["expected_revision"], item_ids=batch["item_ids"],
        item_revisions=batch["item_revisions"]))
    assert not frozen.is_error, frozen.output
    assert json.loads(frozen.output)["batch_id"] == batch["batch_id"]


def candidates(root, ids):
    result = []
    for item_id in ids:
        row = {"item_id": item_id, "issue_ids": [item_id + "-issue"]}
        preview = root / f"{item_id}-preview.png"
        Image.new("RGB", (4, 4), (0, 120, 0)).save(preview)
        for kind in ("candidate", "diff", "evidence"):
            path = root / f"{item_id}-{kind}{'.md' if kind == 'candidate' else '.txt'}"
            content = (f"candidate: repaired text\n\n![preview]({preview.name})\n"
                       if kind == "candidate" else f"{kind}: repaired text")
            path.write_text(content, encoding="utf-8")
            row[kind] = {"path": path.name, "sha256": digest(path)}
        result.append(row)
    return result


@pytest.mark.asyncio
async def test_snapshot_restart_idempotence_and_exclusive_claim(tmp_path):
    _, worker, store, run_id, ids = await fixture(tmp_path)
    first = await freeze(worker, ids[:1])
    assert await freeze(worker, ids[:1]) == first
    other = RepairWorkBatch(store, run_id, ["check"], "frozen", tmp_path, "other")
    assert (await other.execute(Batch(action="freeze", reviewer_node_id="check", expected_revision=2, item_ids=ids))).is_error
    store.mutate(run_id, lambda s: s["review_queues"]["check"].update(revision=3), durable=True)
    restored = RepairWorkBatch(ExpertRunStore(tmp_path / "runs"), run_id, ["check"], "frozen", tmp_path, "repair")
    read = await restored.execute(Batch(action="read", reviewer_node_id="check", batch_id=first["batch_id"]))
    assert json.loads(read.output) == first
    assert (await worker.execute(Batch(action="freeze", reviewer_node_id="check", expected_revision=2, item_ids=ids[1:]))).is_error


@pytest.mark.asyncio
async def test_atomic_submission_no_acceptance_and_targeted_review(tmp_path):
    reviewer, worker, store, run_id, ids = await fixture(tmp_path)
    batch = await freeze(worker, ids)
    payload = candidates(tmp_path, ids)
    invalid = copy.deepcopy(payload)
    invalid[-1]["diff"]["sha256"] = "0" * 64
    submit = dict(action="submit", reviewer_node_id="check", batch_id=batch["batch_id"])
    assert (await worker.execute(Batch(**submit, candidates=invalid))).is_error
    assert not store.load(run_id)["review_queues"]["check"].get("repair_candidates")
    result = await worker.execute(Batch(**submit, candidates=payload))
    assert not result.is_error, result.output
    assert (await worker.execute(Batch(**submit, candidates=payload))).output == result.output
    queue = store.load(run_id)["review_queues"]["check"]
    assert queue["revision"] == 2
    assert all(r["state"] == "needs_repair" for r in queue["records"].values())
    assert len(reviewer._actionable(queue)) == 2
    # A new review round without a new candidate does not re-review the item.
    def reviewed(s):
        q = s["review_queues"]["check"]
        q["records"][ids[0]]["candidate_revision"] = q["repair_candidates"][ids[0]]["candidate_revision"]
    store.mutate(run_id, reviewed)
    reviewer.review_round = 99
    assert [i["item_id"] for i in reviewer._actionable(store.load(run_id)["review_queues"]["check"])] == ids[1:]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["source", "record", "overlap", "outside", "issues"])
async def test_submission_preflight_fail_closed(tmp_path, failure):
    _, worker, store, run_id, ids = await fixture(tmp_path)
    batch = await freeze(worker, ids)
    payload = candidates(tmp_path, ids)
    if failure == "source":
        (tmp_path / "left.md").write_text("changed", encoding="utf-8")
    elif failure == "record":
        store.mutate(run_id, lambda s: s["review_queues"]["check"]["records"][ids[0]].update(summary="changed"))
    elif failure == "overlap":
        payload[1]["candidate"] = payload[0]["candidate"]
    elif failure == "outside":
        payload[0]["candidate"]["path"] = "../outside.txt"
    else:
        payload[0]["issue_ids"] = ["invented"]
    result = await worker.execute(Batch(action="submit", reviewer_node_id="check", batch_id=batch["batch_id"], candidates=payload))
    assert result.is_error
    assert not store.load(run_id)["review_queues"]["check"].get("repair_candidates")


@pytest.mark.asyncio
async def test_disjoint_batches_and_release(tmp_path):
    _, worker, store, run_id, ids = await fixture(tmp_path)
    first = await freeze(worker, ids[:1])
    second = await freeze(worker, ids[1:])
    assert first["batch_id"] != second["batch_id"]
    assert not (await worker.execute(Batch(action="release", reviewer_node_id="check", batch_id=first["batch_id"]))).is_error
    replacement = await freeze(worker, ids[:1])
    assert replacement["batch_id"] != first["batch_id"]
    assert await freeze(worker, ids[:1]) == replacement


@pytest.mark.asyncio
async def test_only_submitted_item_becomes_actionable(tmp_path):
    reviewer, worker, store, run_id, ids = await fixture(tmp_path)
    batch = await freeze(worker, ids[:1])
    reviewer.review_round = 10
    assert not reviewer._actionable(store.load(run_id)["review_queues"]["check"])
    result = await worker.execute(Batch(action="submit", reviewer_node_id="check", batch_id=batch["batch_id"],
                                        candidates=candidates(tmp_path, ids[:1])))
    assert not result.is_error
    assert [i["item_id"] for i in reviewer._actionable(store.load(run_id)["review_queues"]["check"])] == ids[:1]


@pytest.mark.asyncio
async def test_candidate_requires_actual_read_and_current_revision_before_closure(tmp_path):
    from mewcode.tools.read_audit_artifact import ReadAuditArtifact, ReadAuditArtifactParams
    reviewer, worker, store, run_id, ids = await fixture(tmp_path)
    batch = await freeze(worker, ids[:1])
    payload = candidates(tmp_path, ids[:1])
    submitted = await worker.execute(Batch(action="submit", reviewer_node_id="check", batch_id=batch["batch_id"], candidates=payload))
    candidate = json.loads(submitted.output)["candidates"][ids[0]]
    queue = store.load(run_id)["review_queues"]["check"]
    item = next(i for i in queue["items"] if i["item_id"] == ids[0])
    reader = ReadAuditArtifact([str(tmp_path)])
    reader.image_observer = reviewer.observe_image
    assert not (await reader.execute(ReadAuditArtifactParams(
        file_path=str(tmp_path / item["image_path"]), criterion_id=ids[0]))).is_error
    refs = []
    for source in ("left.md", candidate["candidate"]["path"]):
        result = await reviewer.execute(Review(action="evidence", item_id=ids[0], source_path=source))
        refs.append(json.loads(result.output)["evidence_id"])
    params = Review(action="record", item_id=ids[0], state="complete", summary="independent visual review", evidence_ids=refs)
    assert (await reviewer.execute(params)).is_error
    good = params.model_copy(update={"candidate_revision": candidate["candidate_revision"]})
    result = await reviewer.execute(good)
    assert not result.is_error, result.output
    assert store.load(run_id)["review_queues"]["check"]["records"][ids[0]]["state"] == "pass"
    (tmp_path / candidate["diff"]["path"]).write_text("changed after review", encoding="utf-8")
    assert (await reviewer.execute(good)).is_error
    assert reviewer.exit_blocker()
    assert store.load(run_id)["review_queues"]["check"]["records"][ids[0]]["state"] == "unknown"


@pytest.mark.asyncio
async def test_unrelated_review_progress_does_not_invalidate_frozen_item(tmp_path):
    _, worker, store, run_id, ids = await fixture(tmp_path)
    batch = await freeze(worker, ids[:1])
    def progress(s):
        q = s["review_queues"]["check"]
        q["records"][ids[1]].update(summary="unrelated update")
        q["revision"] += 1
    store.mutate(run_id, progress)
    assert not (await worker.execute(Batch(action="submit", reviewer_node_id="check", batch_id=batch["batch_id"],
                                           candidates=candidates(tmp_path, ids[:1])))).is_error


@pytest.mark.asyncio
async def test_repair_node_progress_counts_batches_it_submitted_into_reviewer_queue(tmp_path):
    """The executor must be credited for work that lands in the auditor's queue."""
    from mewcode.experts.recovery_state import advanced, progress_marker
    _, worker, store, run_id, ids = await fixture(tmp_path)
    baseline = progress_marker(store.load(run_id), "repair")
    batch = await freeze(worker, ids[:1])
    assert not (await worker.execute(Batch(action="submit", reviewer_node_id="check",
        batch_id=batch["batch_id"], candidates=candidates(tmp_path, ids[:1])))).is_error
    after = progress_marker(store.load(run_id), "repair")
    assert after["submitted_items"] == ids[:1]
    assert advanced(baseline, after)


@pytest.mark.asyncio
async def test_stale_revision_after_new_review_still_submits_frozen_batch(tmp_path):
    """A frozen order stays valid when only unrelated review records moved on."""
    reviewer, worker, store, run_id, ids = await fixture(tmp_path)
    batch = await freeze(worker, ids[:1])
    def unrelated(s):
        q = s["review_queues"]["check"]
        q["records"][ids[1]]["summary"] = "another page reviewed later"
        q["revision"] += 1
    store.mutate(run_id, unrelated)
    result = await worker.execute(Batch(action="submit", reviewer_node_id="check",
        expected_revision=batch["revision"], batch_id=batch["batch_id"],
        candidates=candidates(tmp_path, ids[:1])))
    assert not result.is_error, result.output
    assert json.loads(result.output)["status"] == "submitted"


@pytest.mark.asyncio
async def test_metadata_backfill_does_not_invalidate_snapshot(tmp_path):
    _, worker, store, run_id, ids = await fixture(tmp_path)
    batch = await freeze(worker, ids[:1])
    def backfill(s):
        receipt = s["review_queues"]["check"]["records"][ids[0]]["evidence"][0]
        receipt.update(digest_kind="raw_file_sha256_v1", file_sha256=receipt["sha256"])
    store.mutate(run_id, backfill)
    assert not (await worker.execute(Batch(action="submit", reviewer_node_id="check", batch_id=batch["batch_id"],
                                           candidates=candidates(tmp_path, ids[:1])))).is_error
