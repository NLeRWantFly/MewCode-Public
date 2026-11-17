from pathlib import Path

import pytest

from scripts.build_isolated_stage5_worklist import (
    relative_file, unique_pages, verify_existing_worklist,
)


def test_relative_file_stays_inside_frozen_workspace(tmp_path: Path) -> None:
    file = tmp_path / "source.txt"
    file.write_text("evidence", encoding="utf-8")
    assert relative_file(tmp_path, "source.txt") == file
    for unsafe in ("../source.txt", "C:\\private.txt", "/outside.txt", "missing.txt"):
        with pytest.raises((ValueError, FileNotFoundError)):
            relative_file(tmp_path, unsafe)


def test_unique_pages_rejects_duplicate_identity() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        unique_pages([
            {"doc_id": "book", "pdf_page": 1},
            {"doc_id": "book", "pdf_page": "1"},
        ], "scope")


def test_existing_worklist_allows_only_unrelated_snapshot_byte_drift() -> None:
    old = {"source": {"current_snapshot_sha256": "a" * 64,
                       "current_flow_fingerprint": "flow-v11",
                       "host_queue_revision_hint": 269},
           "policy": {"prior_stage5_states_are_untrusted_hints": True},
           "items": [{"doc_id": "book", "pdf_page": 1,
                      "revalidation_state": "pending_independent_visual_review"}]}
    assert verify_existing_worklist(old, old) == "exact"
    current = {**old, "source": {**old["source"],
                                "current_snapshot_sha256": "b" * 64}}
    assert verify_existing_worklist(old, current) == "snapshot_metadata_drift_only"
    changed_item = {**current, "items": [{**current["items"][0], "pdf_page": 2}]}
    with pytest.raises(ValueError, match="differs"):
        verify_existing_worklist(old, changed_item)
    changed_queue = {**current, "source": {**current["source"],
                                            "host_queue_revision_hint": 270}}
    with pytest.raises(ValueError, match="differs"):
        verify_existing_worklist(old, changed_queue)
