from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from mewcode.orchestration.paused_handoff_statediff import (
    compare_paused_handoff_state,
    freeze_paused_handoff_baseline,
)


def _receipt(path: Path, data: bytes, *, tool_id: str = "write-1") -> dict:
    path = path.resolve()
    digest = hashlib.sha256(data).hexdigest()
    identity = f"node-a\0\0{1}\0{tool_id}\0{path}\0{digest}"
    return {
        "id": hashlib.sha256(identity.encode()).hexdigest()[:32],
        "node_id": "node-a", "member_id": "", "node_attempt": 1,
        "tool_id": tool_id, "path": str(path), "size": len(data),
        "sha256": digest, "source": "successful_write", "accepted": False,
    }


def _baseline(root: Path, *, queues: dict | None = None,
              receipts: list | None = None, **kwargs) -> dict:
    return freeze_paused_handoff_baseline(
        [root], review_queues=queues if queues is not None else {"audit": {"revision": 4}},
        receipts=receipts if receipts is not None else [], **kwargs,
    )


def test_unchanged_state_is_stable_and_resumes(tmp_path: Path) -> None:
    root = tmp_path / "writable"
    root.mkdir()
    (root / "a.txt").write_text("before", encoding="utf-8")
    baseline = _baseline(root)
    same_baseline = _baseline(root)

    first = compare_paused_handoff_state(
        baseline, review_queues={"audit": {"revision": 4}}, receipts=[])
    second = compare_paused_handoff_state(
        baseline, review_queues={"audit": {"revision": 4}}, receipts=[])

    assert baseline["status"] == "ready"
    assert baseline["baseline_id"]
    assert baseline["baseline_id"] == same_baseline["baseline_id"]
    assert first["classification"] == "no_change"
    assert first["decision"] == "resume"
    assert first["can_auto_resume"] is True
    assert first["diff_id"] == second["diff_id"]


def test_known_out_of_scope_change_is_ignored(tmp_path: Path) -> None:
    root = tmp_path / "writable"
    root.mkdir()
    (root / "a.txt").write_text("before", encoding="utf-8")
    elsewhere = tmp_path / "unrelated.txt"
    elsewhere.write_text("outside", encoding="utf-8")
    baseline = _baseline(root)

    result = compare_paused_handoff_state(
        baseline, review_queues={"audit": {"revision": 4}}, receipts=[],
        observed_changed_paths=[elsewhere],
    )

    assert result["classification"] == "outside_scope"
    assert result["decision"] == "resume"
    assert result["can_auto_resume"] is True
    assert not result.get("reasons")
    assert result["changes"][0]["path"] == str(elsewhere.resolve())


def test_in_scope_change_with_matching_receipt_is_cleared(tmp_path: Path) -> None:
    root = tmp_path / "writable"
    root.mkdir()
    target = root / "a.txt"
    target.write_bytes(b"before")
    baseline = _baseline(root)
    target.write_bytes(b"after")

    result = compare_paused_handoff_state(
        baseline, review_queues={"audit": {"revision": 4}},
        receipts=[_receipt(target, b"after")],
    )

    assert result["classification"] == "in_scope_receipt_matched"
    assert result["decision"] == "resume"
    assert result["can_auto_resume"] is True
    change = next(item for item in result["changes"] if item["kind"] == "file")
    assert change["classification"] == "in_scope_receipt_matched"
    assert change["receipt_ids"] == [_receipt(target, b"after")["id"]]


def test_matching_receipt_for_byte_identical_rewrite_is_still_observable(
        tmp_path: Path) -> None:
    root = tmp_path / "writable"
    root.mkdir()
    target = root / "a.txt"
    target.write_bytes(b"same bytes")
    baseline = _baseline(root)

    result = compare_paused_handoff_state(
        baseline, review_queues={"audit": {"revision": 4}},
        receipts=[_receipt(target, b"same bytes")],
    )

    assert result["classification"] == "in_scope_receipt_matched"
    assert result["changes"][0]["change"] == "receipt_only"
    assert result["changes"][0]["classification"] == "in_scope_receipt_matched"


def test_in_scope_change_without_receipt_proposes_import_or_void(tmp_path: Path) -> None:
    root = tmp_path / "writable"
    root.mkdir()
    target = root / "a.txt"
    target.write_bytes(b"before")
    baseline = _baseline(root)
    target.write_bytes(b"after")

    result = compare_paused_handoff_state(
        baseline, review_queues={"audit": {"revision": 4}}, receipts=[])

    assert result["classification"] == "in_scope_missing_receipt_proposal"
    assert result["decision"] == "propose_import_or_void"
    assert result["can_auto_resume"] is False
    assert result["clearance"]["code"] == "review_import_or_void"
    proposal = result["changes"][0]["proposal"]
    assert proposal["action"] == "import_or_void"
    assert proposal["status"] == "pending_review_node"
    assert proposal["requires_human_queue"] is False
    assert "unresolved" not in proposal["status"]


@pytest.mark.parametrize("mutation", ["add", "delete"])
def test_directory_root_discovers_new_and_deleted_files_without_hints(
        tmp_path: Path, mutation: str) -> None:
    root = tmp_path / "writable"
    root.mkdir()
    target = root / "nested" / "a.txt"
    target.parent.mkdir()
    target.write_bytes(b"baseline")
    baseline = _baseline(root)

    if mutation == "add":
        (root / "nested" / "new.txt").write_bytes(b"new")
    else:
        target.unlink()

    result = compare_paused_handoff_state(
        baseline, review_queues={"audit": {"revision": 4}}, receipts=[])

    assert result["classification"] == "in_scope_missing_receipt_proposal"
    file_changes = [item for item in result["changes"] if item["kind"] == "file"]
    assert file_changes
    assert file_changes[0]["change"] == ("added" if mutation == "add" else "deleted")
    assert file_changes[0]["proposal"]["action"] == "import_or_void"


def test_queue_revision_change_is_unknown_external_effect_and_holds(tmp_path: Path) -> None:
    root = tmp_path / "writable"
    root.mkdir()
    baseline = _baseline(root)

    result = compare_paused_handoff_state(
        baseline, review_queues={"audit": {"revision": 5}}, receipts=[])

    assert result["classification"] == "hold"
    assert result["decision"] == "hold"
    assert result["can_auto_resume"] is False
    assert result["changes"][0]["classification"] == "unknown_external_effect"
    assert result["clearance"]["code"] == "reconcile_unknown_external_effect"


@pytest.mark.parametrize("baseline", [None, {}, {"status": "unavailable"}])
def test_missing_or_unreadable_baseline_fails_closed_with_clearance(
        tmp_path: Path, baseline: dict | None) -> None:
    result = compare_paused_handoff_state(
        baseline, review_queues={}, receipts=[])

    assert result["classification"] == "hold"
    assert result["decision"] == "hold"
    assert result["can_auto_resume"] is False
    assert result["clearance"]["condition"]


def test_tampered_baseline_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "writable"
    root.mkdir()
    baseline = _baseline(root)
    baseline["queue_revisions"]["audit"] = 100

    result = compare_paused_handoff_state(
        baseline, review_queues={"audit": {"revision": 4}}, receipts=[])

    assert result["classification"] == "hold"
    assert result["reasons"][0]["code"] == "baseline_integrity_invalid"
    assert result["clearance"]["code"] == "restore_complete_authoritative_state_and_retry"


def test_over_limit_file_makes_baseline_unavailable(tmp_path: Path) -> None:
    root = tmp_path / "writable"
    root.mkdir()
    (root / "too-large.bin").write_bytes(b"12345")

    baseline = _baseline(root, max_file_bytes=4)
    result = compare_paused_handoff_state(
        baseline, review_queues={"audit": {"revision": 4}}, receipts=[])

    assert baseline["status"] == "unavailable"
    assert baseline["clearance"]["code"] == "capture_complete_authoritative_baseline"
    assert result["classification"] == "hold"
    assert result["can_auto_resume"] is False


def test_over_limit_file_count_makes_baseline_unavailable(tmp_path: Path) -> None:
    root = tmp_path / "writable"
    root.mkdir()
    (root / "one.txt").write_bytes(b"1")
    (root / "two.txt").write_bytes(b"2")

    baseline = _baseline(root, max_files=1)

    assert baseline["status"] == "unavailable"
    assert baseline["reason"] == "file_count_limit_exceeded"
    assert baseline["clearance"]["condition"]


def test_over_limit_aggregate_bytes_makes_baseline_unavailable(tmp_path: Path) -> None:
    root = tmp_path / "writable"
    root.mkdir()
    (root / "one.txt").write_bytes(b"123")
    (root / "two.txt").write_bytes(b"456")

    baseline = _baseline(root, max_total_bytes=5)

    assert baseline["status"] == "unavailable"
    assert baseline["reason"] == "file_size_limit_exceeded"
    assert baseline["clearance"]["condition"]


def test_unrecognized_new_receipt_is_not_auto_admitted(tmp_path: Path) -> None:
    root = tmp_path / "writable"
    root.mkdir()
    baseline = _baseline(root)

    result = compare_paused_handoff_state(
        baseline, review_queues={"audit": {"revision": 4}},
        receipts=[{"id": "new", "path": str(root / "x"), "source": "unknown"}],
    )

    assert result["classification"] == "hold"
    assert result["decision"] == "hold"
    assert result["reasons"][0]["code"] == "unrecognized_new_receipt"
