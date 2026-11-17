import copy
import os
from pathlib import Path

import pytest

from mewcode.sandbox import staged_workspace as stage
from mewcode.sandbox.metadata_changes import classify_metadata_changes


class _Scope:
    def __init__(self, workspace: Path, writable: bool = True):
        self.workspace = workspace
        self.writable = writable

    def allows(self, path: Path, *, write: bool) -> bool:
        return self.writable and path.is_relative_to(self.workspace)


def _snapshot(kind: str, *, size: int = 0) -> dict:
    return {
        "schema": "mewcode-workspace-metadata-v1",
        "kind": kind,
        "capture_consistent": True,
        "attributes": 0x80 if kind == "file" else 0x10,
        "created": 10,
        "written": 20,
        "security": {
            "owner_group_dacl": {
                "state": "captured", "sddl": "O:SYG:SYD:(A;OICI;FA;;;SY)",
            },
            "sacl": {"state": "captured", "present": False,
                     "null": False, "sddl": None},
        },
        "streams": {"state": "captured", "items": ([
            {"name": "::$DATA", "size": size, "allocation_size": 4096},
        ] if kind == "file" else [])},
        "extended_attributes": {"state": "captured", "size": 0},
    }


def _setup(tmp_path: Path, *, writable: bool = True):
    source = tmp_path / "source"
    workspace = tmp_path / "candidate"
    source.mkdir()
    workspace.mkdir()
    original = b"unchanged bytes"
    source_file, candidate_file = source / "a.txt", workspace / "a.txt"
    source_file.write_bytes(original)
    candidate_file.write_bytes(original)
    root_source = source.lstat()
    file_source = source_file.lstat()
    baseline = (
        stage.Entry(".", "directory", 0, "", root_source.st_dev,
                    root_source.st_ino, writable),
        stage.Entry("a.txt", "file", len(original), stage._hash(original),
                    file_source.st_dev, file_source.st_ino, writable),
    )
    plan = stage.StagedPlan(
        tmp_path / "transaction", source, workspace, "a" * 64, baseline,
        "b" * 64, 10, 1024 * 1024,
    )
    root_before = _snapshot("directory")
    file_before = _snapshot("file", size=len(original))
    inventory = {
        "sources": {".": copy.deepcopy(root_before), "a.txt": copy.deepcopy(file_before)},
        "candidates": {".": copy.deepcopy(root_before), "a.txt": copy.deepcopy(file_before)},
    }
    current = {".": copy.deepcopy(root_before), "a.txt": copy.deepcopy(file_before)}

    def capture(path: Path) -> dict:
        return copy.deepcopy(current[path.relative_to(workspace).as_posix()])

    verified = stage.VerifiedChanges((), "c" * 64)
    return plan, _Scope(source, writable), inventory, current, capture, verified, candidate_file


@pytest.mark.parametrize("sddl", ["S:", "S:AI"])
def test_disposable_empty_audit_acl_materialization_does_not_change_source_policy(tmp_path, sddl):
    plan, scope, inventory, current, capture, verified, _ = _setup(tmp_path)
    current["a.txt"]["security"]["sacl"].update(present=True, sddl=sddl)
    current["a.txt"]["written"] += 1
    result = classify_metadata_changes(plan, scope, inventory, verified, capture=capture)
    assert result.changes == (stage.Change("a.txt", "modify", plan.baseline[1].sha256, plan.baseline[1].sha256),)
    assert inventory["sources"]["a.txt"]["security"]["sacl"]["present"] is False
    assert current["a.txt"]["security"]["sacl"]["present"] is True


@pytest.mark.parametrize("sacl", [
    {"state": "captured", "present": True, "null": True, "sddl": None},
    {"state": "captured", "present": True, "null": False, "sddl": "S:P"},
    {"state": "captured", "present": True, "null": False, "sddl": "S:(AU;SAFA;FR;;;WD)"},
    {"state": "unavailable", "present": None, "null": None, "sddl": None},
])
def test_candidate_audit_normalization_does_not_accept_unknown_null_protected_or_rules(tmp_path, sacl):
    plan, scope, inventory, current, capture, verified, _ = _setup(tmp_path)
    current["a.txt"]["security"]["sacl"] = sacl
    with pytest.raises(stage.StagingRejected, match="security metadata changed"):
        classify_metadata_changes(plan, scope, inventory, verified, capture=capture)


def test_basic_file_metadata_delta_adds_host_verified_same_hash_change(tmp_path):
    plan, scope, inventory, current, capture, verified, _ = _setup(tmp_path)
    current["a.txt"]["attributes"] |= 1
    current["a.txt"]["created"] += 1
    current["a.txt"]["written"] += 2

    result = classify_metadata_changes(plan, scope, inventory, verified, capture=capture)

    original_hash = next(entry.sha256 for entry in plan.baseline if entry.relative == "a.txt")
    assert result.changes == (stage.Change("a.txt", "modify", original_hash, original_hash),)
    assert result.staged_digest != verified.staged_digest
    assert result.state == "verified_unpublished"


def test_dacl_ai_and_id_materialization_is_the_only_security_normalization(tmp_path):
    plan, scope, inventory, current, capture, verified, _ = _setup(tmp_path)
    current["a.txt"]["attributes"] |= 1
    current["a.txt"]["security"]["owner_group_dacl"]["sddl"] = (
        "O:SYG:SYD:AI(A;OICIID;FA;;;SY)"
    )

    result = classify_metadata_changes(plan, scope, inventory, verified, capture=capture)

    assert [change.relative for change in result.changes] == ["a.txt"]


@pytest.mark.parametrize("field, mutate", [
    ("security", lambda value: value["owner_group_dacl"].update(sddl="O:SYG:SYD:(A;OICI;FR;;;SY)")),
    ("streams", lambda value: value["items"].append(
        {"name": ":extra:$DATA", "size": 1, "allocation_size": 4096})),
    ("extended_attributes", lambda value: value.update(size=1)),
])
def test_security_stream_or_ea_delta_is_rejected_even_with_content_change(
        tmp_path, field, mutate):
    plan, scope, inventory, current, capture, verified, candidate_file = _setup(tmp_path)
    updated = b"changed bytes"
    candidate_file.write_bytes(updated)
    current["a.txt"]["streams"]["items"][0]["size"] = len(updated)
    current["a.txt"]["streams"]["items"][0]["allocation_size"] = 8192
    mutate(current["a.txt"][field])
    old_hash = next(entry.sha256 for entry in plan.baseline if entry.relative == "a.txt")
    verified = stage.VerifiedChanges(
        (stage.Change("a.txt", "modify", old_hash, stage._hash(updated)),), "c" * 64,
    )

    with pytest.raises(stage.StagingRejected):
        classify_metadata_changes(plan, scope, inventory, verified, capture=capture)


def test_default_data_stream_size_tracks_verified_content_change(tmp_path):
    plan, scope, inventory, current, capture, _, candidate_file = _setup(tmp_path)
    updated = b"new content with another length"
    candidate_file.write_bytes(updated)
    current["a.txt"]["streams"]["items"][0]["size"] = len(updated)
    current["a.txt"]["streams"]["items"][0]["allocation_size"] = 8192
    old_hash = next(entry.sha256 for entry in plan.baseline if entry.relative == "a.txt")
    verified = stage.VerifiedChanges(
        (stage.Change("a.txt", "modify", old_hash, stage._hash(updated)),), "c" * 64,
    )

    result = classify_metadata_changes(plan, scope, inventory, verified, capture=capture)

    assert result.changes == verified.changes
    assert result.staged_digest != verified.staged_digest


def test_unreported_content_change_is_not_promoted_to_metadata_change(tmp_path):
    plan, scope, inventory, current, capture, verified, candidate_file = _setup(tmp_path)
    candidate_file.write_bytes(b"different bytes")
    current["a.txt"]["attributes"] |= 1

    with pytest.raises(stage.StagingRejected, match="content changed"):
        classify_metadata_changes(plan, scope, inventory, verified, capture=capture)


@pytest.mark.parametrize("frozen_writable, current_writable", [(False, True), (True, False)])
def test_metadata_change_requires_frozen_and_current_write_scope(
        tmp_path, frozen_writable, current_writable):
    plan, scope, inventory, current, capture, verified, _ = _setup(
        tmp_path, writable=frozen_writable,
    )
    scope.writable = current_writable
    current["a.txt"]["attributes"] |= 1

    with pytest.raises(stage.StagingRejected, match="writable scope"):
        classify_metadata_changes(plan, scope, inventory, verified, capture=capture)


def test_directory_written_time_is_ignored_but_other_directory_metadata_is_rejected(tmp_path):
    plan, scope, inventory, current, capture, verified, _ = _setup(tmp_path)
    current["."]["written"] += 100

    result = classify_metadata_changes(plan, scope, inventory, verified, capture=capture)
    assert result.changes == ()

    current["."]["attributes"] |= 1
    with pytest.raises(stage.StagingRejected, match="Directory metadata-only"):
        classify_metadata_changes(plan, scope, inventory, verified, capture=capture)


def test_missing_original_candidate_requires_host_verified_delete(tmp_path):
    plan, scope, inventory, current, capture, verified, candidate_file = _setup(tmp_path)
    candidate_file.unlink()

    with pytest.raises(stage.StagingRejected, match="path is missing"):
        classify_metadata_changes(plan, scope, inventory, verified, capture=capture)

    old_hash = next(entry.sha256 for entry in plan.baseline if entry.relative == "a.txt")
    verified = stage.VerifiedChanges(
        (stage.Change("a.txt", "delete", old_hash, None),), "c" * 64,
    )
    result = classify_metadata_changes(plan, scope, inventory, verified, capture=capture)
    assert result.changes == verified.changes


def test_hardlinked_candidate_and_unknown_metadata_fields_are_rejected(tmp_path):
    plan, scope, inventory, current, capture, verified, candidate_file = _setup(tmp_path)
    os.link(candidate_file, candidate_file.with_name("alias.txt"))
    current["a.txt"]["attributes"] |= 1

    with pytest.raises(stage.StagingRejected, match="regular file"):
        classify_metadata_changes(plan, scope, inventory, verified, capture=capture)

    candidate_file.with_name("alias.txt").unlink()
    current["a.txt"]["future_field"] = "different"
    with pytest.raises(stage.StagingRejected, match="unknown or missing fields"):
        classify_metadata_changes(plan, scope, inventory, verified, capture=capture)


def test_complex_attribute_flags_are_rejected(tmp_path):
    plan, scope, inventory, current, capture, verified, _ = _setup(tmp_path)
    current["a.txt"]["attributes"] = 0x80 | 0x400

    with pytest.raises(stage.StagingRejected, match="complex file attributes"):
        classify_metadata_changes(plan, scope, inventory, verified, capture=capture)
