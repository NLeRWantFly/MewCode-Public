import hashlib
import json
import subprocess
import asyncio

import pytest

from mewcode.tools.verify_artifacts import (
    VerifyArtifacts,
    VerifyArtifactsParams,
    VerifyPatchApplicability,
    VerifyPatchParams,
    sources_digest,
)
from mewcode.tools.repair_work_batch import RepairWorkBatchParams
from tests.test_repair_work_batch import candidates, fixture, freeze


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_candidate_artifacts_match_frozen_receipt_and_have_stable_identity(tmp_path):
    _, worker, store, run_id, ids = await fixture(tmp_path)
    batch = await freeze(worker, ids[:1])
    payload = candidates(tmp_path, ids[:1])
    submitted = await worker.execute(RepairWorkBatchParams(action="submit", reviewer_node_id="check",
        batch_id=batch["batch_id"], candidates=payload))
    assert not submitted.is_error, submitted.output
    saved = store.load(run_id)["review_queues"]["check"]["repair_candidates"][ids[0]]
    tool = VerifyArtifacts(store, run_id, "check", tmp_path, "frozen")
    params = VerifyArtifactsParams(item_id=ids[0], candidate_revision=saved["candidate_revision"],
                                   sources=saved["sources"], issue_ids=saved["issue_ids"])

    first = await tool.execute(params)
    second = await tool.execute(params)
    assert not first.is_error, first.output
    assert first.output == second.output
    result = json.loads(first.output)
    assert result["status"] == "PASS"
    assert result["identity"]["object"] == f"review-item:{ids[0]}"
    assert result["identity"]["version"] == saved["candidate_revision"]


@pytest.mark.asyncio
async def test_draft_ledger_mismatches_fail_closed_for_revision_source_and_issues(tmp_path):
    _, worker, store, run_id, ids = await fixture(tmp_path)
    batch = await freeze(worker, ids[:1])
    payload = candidates(tmp_path, ids[:1])
    result = await worker.execute(RepairWorkBatchParams(action="submit", reviewer_node_id="check",
        batch_id=batch["batch_id"], candidates=payload))
    assert not result.is_error, result.output
    saved = store.load(run_id)["review_queues"]["check"]["repair_candidates"][ids[0]]
    tool = VerifyArtifacts(store, run_id, "check", tmp_path, "frozen")
    valid = VerifyArtifactsParams(item_id=ids[0], candidate_revision=saved["candidate_revision"],
                                  sources=saved["sources"], issue_ids=saved["issue_ids"])

    revision_result = await tool.execute(valid.model_copy(update={"candidate_revision": "0" * 64}))
    source_result = await tool.execute(VerifyArtifactsParams(item_id=ids[0],
        candidate_revision=saved["candidate_revision"],
        sources=[{**saved["sources"][0], "sha256": "0" * 64}],
        issue_ids=saved["issue_ids"]))
    issue_result = await tool.execute(valid.model_copy(update={"issue_ids": ["invented-issue"]}))
    assert revision_result.is_error and "candidate_revision" in revision_result.output
    assert source_result.is_error and "来源 SHA256" in source_result.output
    assert issue_result.is_error and "issue_ids" in issue_result.output


@pytest.mark.asyncio
async def test_artifact_hash_tampering_fails_closed(tmp_path):
    _, worker, store, run_id, ids = await fixture(tmp_path)
    batch = await freeze(worker, ids[:1])
    payload = candidates(tmp_path, ids[:1])
    result = await worker.execute(RepairWorkBatchParams(action="submit", reviewer_node_id="check",
        batch_id=batch["batch_id"], candidates=payload))
    assert not result.is_error
    saved = store.load(run_id)["review_queues"]["check"]["repair_candidates"][ids[0]]
    path = tmp_path / saved["diff"]["path"]
    path.write_text("tampered", encoding="utf-8")
    tool = VerifyArtifacts(store, run_id, "check", tmp_path, "frozen")
    params = VerifyArtifactsParams(item_id=ids[0], candidate_revision=saved["candidate_revision"],
                                   sources=saved["sources"], issue_ids=saved["issue_ids"])
    assert (await tool.execute(params)).is_error


@pytest.mark.asyncio
async def test_evidence_json_source_digest_and_issue_ids_are_checked(tmp_path):
    _, worker, store, run_id, ids = await fixture(tmp_path)
    batch = await freeze(worker, ids[:1])
    payload = candidates(tmp_path, ids[:1])
    item = store.load(run_id)["review_queues"]["check"]["repair_batches"][batch["batch_id"]]["items"][0]
    payload[0]["evidence"]["path"] = "evidence.json"
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"source_sha256": sources_digest(item["sources"]),
                                    "issue_ids": payload[0]["issue_ids"]}), encoding="utf-8")
    payload[0]["evidence"]["sha256"] = digest(evidence)
    submitted = await worker.execute(RepairWorkBatchParams(action="submit", reviewer_node_id="check",
        batch_id=batch["batch_id"], candidates=payload))
    assert not submitted.is_error, submitted.output
    saved = store.load(run_id)["review_queues"]["check"]["repair_candidates"][ids[0]]
    verified = await VerifyArtifacts(store, run_id, "check", tmp_path, "frozen").execute(
        VerifyArtifactsParams(item_id=ids[0], candidate_revision=saved["candidate_revision"],
                              sources=saved["sources"], issue_ids=saved["issue_ids"]))
    assert not verified.is_error, verified.output


def test_patch_applicability_is_a_bounded_no_shell_git_dry_run(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    target = tmp_path / "sample.txt"
    target.write_text("before\n", encoding="utf-8")
    patch = "--- a/sample.txt\n+++ b/sample.txt\n@@ -1 +1 @@\n-before\n+after\n"
    patch_path = tmp_path / "diff.patch"
    patch_path.write_text(patch, encoding="utf-8")
    tool = VerifyPatchApplicability(tmp_path)
    valid = VerifyPatchParams(patch_path="diff.patch", expected_sha256=digest(patch_path))

    first = asyncio.run(tool.execute(valid))
    second = asyncio.run(tool.execute(valid))
    assert not first.is_error, first.output
    assert first.output == second.output
    assert json.loads(first.output)["applicable"] is True
    assert target.read_text(encoding="utf-8") == "before\n"

    patch_path.write_text(patch.replace("before", "absent"), encoding="utf-8")
    failed = asyncio.run(tool.execute(VerifyPatchParams(patch_path="diff.patch")))
    assert failed.is_error
    assert json.loads(failed.output)["status"] == "FAIL"


def test_patch_paths_cannot_escape_the_repository(tmp_path):
    patch = tmp_path / "diff.patch"
    patch.write_text("--- a/../../outside.txt\n+++ b/../../outside.txt\n@@ -1 +1 @@\n-a\n+b\n",
                     encoding="utf-8")
    result = asyncio.run(
        VerifyPatchApplicability(tmp_path).execute(VerifyPatchParams(patch_path="diff.patch")))
    assert result.is_error
    assert "路径必须是根目录内" in result.output
