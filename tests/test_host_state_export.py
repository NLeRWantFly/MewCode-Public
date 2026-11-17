import hashlib
import json
from pathlib import Path

import pytest

from mewcode.tools.export_host_state import ExportHostState, ExportHostStateParams, HostStateExporter


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ReadOnlyStore:
    def __init__(self, queue):
        self.queue = queue
        self.reads = []
        self.mutations = 0

    def load_review_queue(self, run_id, node_id):
        self.reads.append((run_id, node_id))
        return self.queue

    def load(self, *_args):
        raise AssertionError("exporter must not load the private full snapshot")

    def mutate(self, *_args, **_kwargs):
        self.mutations += 1
        raise AssertionError("exporter must be read-only")


def queue_fixture():
    contract = sha("contract")
    manifest = sha("manifest")
    candidate_revision = sha("candidate-revision")
    return {
        "revision": 7,
        "contract_hash": contract,
        "manifest_sha256": manifest,
        "manifest_path": "private/path/manifest.json",
        "items": [
            {"item_id": "review-a", "identity": {"doc_id": "secret-doc-name"},
             "image_path": "private/images/a.png", "input": {"private": "raw business text"}},
            {"item_id": "review-b", "identity": {"doc_id": "doc-b"},
             "image_path": "private/images/b.png"},
        ],
        "records": {
            "review-a": {
                "state": "needs_repair",
                "summary": "PRIVATE REVIEW SUMMARY",
                "evidence": [{"path": "private/source.txt", "sha256": sha("evidence"),
                              "quote": "PRIVATE QUOTE"}],
                "images": [{"path": "private/image.png", "sha256": sha("image"),
                            "pixel_sha256": sha("pixels")}],
                "rendered_images": [{"path": "private/render.png", "sha256": sha("render")}],
            },
        },
        "text_receipts": {"review-a": {"text-1": {"path": "private/evidence.json",
                                                      "sha256": sha("text receipt"),
                                                      "text": "PRIVATE RECEIPT TEXT"}}},
        "image_receipts": {"review-a": [{"path": "private/source.png",
                                           "sha256": sha("image receipt")} ]},
        "repair_candidates": {
            "review-a": {"candidate_revision": candidate_revision,
                         "batch_id": "repair-1", "status": "awaiting_review",
                         "candidate": {"path": "private/candidate.md", "sha256": sha("candidate")}},
        },
        "repair_batches": {
            "repair-1": {"status": "submitted", "revision": 7,
                          "item_ids": ["review-a"], "manifest_sha256": manifest,
                          "owner": "private-owner", "items": [{"private": "row"}],
                          "candidates": {"review-a": {"candidate_revision": candidate_revision,
                                                         "status": "awaiting_review",
                                                         "candidate": {"path": "private/candidate.md"}}}},
            "repair-2": {"status": "frozen", "revision": 7,
                          "item_ids": ["review-b"], "manifest_sha256": manifest,
                          "owner": "private-owner"},
            "repair-old": {"status": "released", "revision": 6,
                           "item_ids": ["review-b"], "manifest_sha256": manifest},
        },
        "full_messages": [{"content": "PRIVATE FULL SNAPSHOT MESSAGE"}],
    }


def test_export_is_allowlisted_and_contains_review_queue_index(tmp_path):
    store = ReadOnlyStore(queue_fixture())
    exporter = HostStateExporter(store, "run-1", "reviewer-1", tmp_path,
                                 contract_hash=sha("contract"))

    payload = exporter.build_payload()
    encoded = json.dumps(payload, ensure_ascii=False)
    assert payload["queue"]["revision"] == 7
    assert payload["queue"]["contract_hash"] == sha("contract")
    assert payload["queue"]["manifest_sha256"] == sha("manifest")
    assert len(payload["queue"]["fingerprint"]) == 64
    assert payload["summary"] == {
        "complete": 0, "needs_repair": 1, "unknown": 0, "unrecorded": 1,
    }
    assert payload["items"][0]["item_id"] == "review-a"
    assert payload["items"][0]["state"] == "needs_repair"
    assert payload["items"][0]["candidate_binding"] == {
        "candidate_revision": sha("candidate-revision"),
        "batch_id": "repair-1",
        "status": "awaiting_review",
    }
    assert payload["items"][1]["state"] == "unrecorded"
    assert {batch["status"] for batch in payload["repair_batches"]} == {
        "submitted", "frozen", "released",
    }
    for private_value in (
        "PRIVATE REVIEW SUMMARY", "PRIVATE QUOTE", "PRIVATE RECEIPT TEXT",
        "private/path/manifest.json", "private/candidate.md", "secret-doc-name",
        "private-owner", "PRIVATE FULL SNAPSHOT MESSAGE",
    ):
        assert private_value not in encoded
    assert store.mutations == 0
    assert store.reads == [("run-1", "reviewer-1")]


def test_export_writes_deterministic_json_and_matching_sha256_sidecar(tmp_path):
    store = ReadOnlyStore(queue_fixture())
    exporter = HostStateExporter(store, "run-1", "reviewer-1", tmp_path)

    first = exporter.export()
    first_json = first.snapshot_path.read_bytes()
    first_checksum = first.checksum_path.read_bytes()
    second = exporter.export()

    assert first_json == second.snapshot_path.read_bytes()
    assert first_checksum == second.checksum_path.read_bytes()
    assert first.sha256 == hashlib.sha256(first_json).hexdigest()
    assert first_checksum == f"{first.sha256}  host_snapshot.json\n".encode("ascii")
    assert first.snapshot_path == tmp_path / "reports" / "host_snapshot" / "host_snapshot.json"
    assert first.checksum_path.name == "host_snapshot.sha256"
    assert store.mutations == 0
    assert store.reads == [("run-1", "reviewer-1"), ("run-1", "reviewer-1")]


def test_fingerprint_changes_when_receipt_changes_even_if_queue_revision_does_not(tmp_path):
    queue = queue_fixture()
    store = ReadOnlyStore(queue)
    exporter = HostStateExporter(store, "run-1", "reviewer-1", tmp_path)
    before = exporter.build_payload()["queue"]["fingerprint"]

    queue["image_receipts"]["review-a"][0]["sha256"] = sha("changed receipt")
    after = exporter.build_payload()["queue"]["fingerprint"]

    assert before != after


def test_export_rejects_contract_mismatch_and_invalid_queue_shapes(tmp_path):
    store = ReadOnlyStore(queue_fixture())
    exporter = HostStateExporter(store, "run-1", "reviewer-1", tmp_path,
                                 contract_hash=sha("other contract"))
    with pytest.raises(ValueError, match="合同"):
        exporter.build_payload()

    store.queue["records"]["review-a"]["state"] = "accepted_by_model"
    exporter = HostStateExporter(store, "run-1", "reviewer-1", tmp_path)
    with pytest.raises(ValueError, match="状态"):
        exporter.build_payload()


def test_export_requires_queue_specific_read_api_and_rejects_path_traversal_ids(tmp_path):
    class FullSnapshotOnly:
        def load(self, *_args):
            return {"review_queues": queue_fixture()}

    with pytest.raises(ValueError, match="load_review_queue"):
        HostStateExporter(FullSnapshotOnly(), "run-1", "reviewer-1", tmp_path).build_payload()

    with pytest.raises(ValueError, match="run_id"):
        HostStateExporter(ReadOnlyStore(queue_fixture()), "..\\outside", "reviewer-1", tmp_path)


def test_export_refuses_output_directory_that_resolves_outside_workspace(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    reports = tmp_path / "reports"
    try:
        reports.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available in this Windows environment")

    exporter = HostStateExporter(ReadOnlyStore(queue_fixture()), "run-1", "reviewer-1", tmp_path)
    with pytest.raises(ValueError, match="工作区内"):
        exporter.export()
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_host_tool_is_bounded_and_auto_policy_allows_only_matching_workspace(tmp_path):
    from mewcode.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine

    tool = ExportHostState(ReadOnlyStore(queue_fixture()), "run-1", "reviewer-1", tmp_path,
                           contract_hash=sha("contract"))
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(tmp_path)),
                                RuleEngine(), PermissionMode.AUTO)
    assert checker.check(tool, {}).source == "host_state_export"
    result = await tool.execute(ExportHostStateParams())
    assert not result.is_error
    assert Path(json.loads(result.output)["snapshot_path"]).parts == (
        "reports", "host_snapshot", "host_snapshot.json",
    )
    checker.mode = PermissionMode.PLAN
    assert checker.check(tool, {}).effect == "deny"
