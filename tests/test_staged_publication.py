from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import struct
import sys
import time

import pytest

from mewcode.orchestration.write_lease import WriteLeaseStore, WriteLeaseError
from mewcode.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine
from mewcode.sandbox import staged_workspace as stage
from mewcode.sandbox import staged_publication as publication
from mewcode.sandbox.lpac_runtime import NativeScope
from mewcode.sandbox.publication_io import FrozenSecurity, PinnedPath
from mewcode.sandbox.receipt import NativeExecutionReceipt

pytestmark = pytest.mark.skipif(os.name != "nt", reason="real Windows handle semantics required")
REVISION = hashlib.sha256(b"publication-host-policy").hexdigest()
IDENTITY = {"owner_id": "publisher", "run_id": "synthetic-run", "conversation_id": "synthetic-conversation"}


def checker_for(root):
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(root)), RuleEngine(), PermissionMode.AUTO)
    checker.task_id = "publication-task"
    return checker


@pytest.fixture
def workspace(tmp_path):
    root, private = tmp_path / "源 project", tmp_path / "private-host"
    root.mkdir()
    private.mkdir()
    (root / "existing.txt").write_bytes(b"before")
    (root / "other.txt").write_bytes(b"untouched")
    (root / "outputs").mkdir()
    checker = checker_for(root)
    scope = NativeScope.from_checker(checker)
    plan = stage.prepare_workspace(scope, private, policy_revision=REVISION)
    (plan.workspace / "existing.txt").write_bytes(b"after")
    leases = WriteLeaseStore(private / "leases.json", sandbox=checker.sandbox)
    acquired = leases.acquire(**IDENTITY, exact_paths=[root / "existing.txt", root / "other.txt"],
                              directory_paths=[root / "outputs"], ttl_seconds=300)
    return plan, scope, leases, acquired["lease"]["lease_id"]


def publish(workspace, **kwargs):
    plan, scope, leases, lease_id = workspace
    receipt = NativeExecutionReceipt(status="exited", exit_code=0, cleanup_confirmed=True, backend="mewcode-windows-lpac")
    return publication.publish_workspace(plan, scope, receipt, leases=leases, lease_id=lease_id,
                                         identity=IDENTITY, policy_revision=lambda: REVISION, **kwargs)


def recover(workspace):
    plan, scope, leases, lease_id = workspace
    return publication.recover_publication(plan, scope, leases=leases, lease_id=lease_id,
                                          identity=IDENTITY, policy_revision=lambda: REVISION, rollback_published=True)


def test_windows_handle_rename_does_not_replace_and_keeps_original_identity(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"one")
    target.write_bytes(b"two")
    with PinnedPath(source, movable=True) as handle:
        original_id = handle.file_id
        assert handle.digest(100) == hashlib.sha256(b"one").hexdigest()
        with pytest.raises(OSError):
            source.write_bytes(b"writer")
        with pytest.raises(OSError):
            handle.rename_absent(target)
        handle.rename_absent(tmp_path / "fresh")
        assert handle.file_id == original_id
    assert target.read_bytes() == b"two"
    assert (tmp_path / "fresh").read_bytes() == b"one"


@pytest.mark.parametrize("explicit_creation_change", [False, True])
def test_publication_distinguishes_staging_creation_from_explicit_change(workspace, explicit_creation_change):
    plan, *_ = workspace
    with PinnedPath(plan.source / "existing.txt") as source:
        original_created = source.basic_metadata()["created"]
    with PinnedPath(plan.workspace / "existing.txt", metadata_write=True) as candidate:
        initial = candidate.basic_metadata()
        updated = dict(initial)
        if explicit_creation_change:
            updated["created"] += 100_000_000
            candidate.set_basic_metadata(updated)
    result = publish(workspace, metadata_verifier=lambda verified: verified,
                     candidate_inventory={"existing.txt": {"created": initial["created"]}})
    assert result.state == "published"
    with PinnedPath(plan.source / "existing.txt") as current:
        expected = updated["created"] if explicit_creation_change else original_created
        assert current.basic_metadata()["created"] == expected
    assert recover(workspace).state == "rolled_back"
    with PinnedPath(plan.source / "existing.txt") as restored:
        assert restored.basic_metadata()["created"] == original_created


def test_verified_existing_readonly_file_replacement_keeps_original_flags(workspace):
    plan, *_ = workspace
    with PinnedPath(plan.source / "existing.txt", metadata_write=True) as original:
        saved = original.basic_metadata()
        original.set_basic_metadata({**saved, "attributes": saved["attributes"] | 1})
        original_id = original.file_id
    with PinnedPath(plan.workspace / "existing.txt", metadata_write=True) as candidate:
        before = candidate.basic_metadata()
        candidate.set_basic_metadata({**before, "attributes": before["attributes"] | 1})
    assert publish(workspace, metadata_verifier=lambda v: v,
        candidate_inventory={"existing.txt": before}).state == "published"
    with PinnedPath(plan.source / "existing.txt") as current:
        assert current.basic_metadata()["attributes"] & 1
    assert recover(workspace).state == "rolled_back"
    with PinnedPath(plan.source / "existing.txt") as original:
        assert original.file_id == original_id
        assert original.basic_metadata()["attributes"] & 1


def test_recovery_detects_tunneled_metadata_after_interrupted_restore(workspace, monkeypatch):
    plan, *_ = workspace
    with PinnedPath(plan.workspace / "existing.txt", metadata_write=True) as candidate:
        initial = candidate.basic_metadata()
        candidate.set_basic_metadata({**initial, "created": initial["created"] + 100_000_000})
    publish(workspace, metadata_verifier=lambda verified: verified,
            candidate_inventory={"existing.txt": {"created": initial["created"]}})
    original_set = PinnedPath.set_basic_metadata
    def fail_restore(self, metadata):
        raise OSError("synthetic interruption after restore rename")
    monkeypatch.setattr(PinnedPath, "set_basic_metadata", fail_restore)
    assert recover(workspace).state == "recovery_conflict"
    monkeypatch.setattr(PinnedPath, "set_basic_metadata", original_set)
    assert recover(workspace).state == "recovery_conflict"
    assert not (plan.transaction / "publication/rolled-back.json").exists()


@pytest.mark.parametrize("rolled_back", [False, True])
def test_completion_markers_do_not_hide_basic_metadata_drift(workspace, rolled_back):
    plan, scope, leases, lease_id = workspace
    publish(workspace)
    if rolled_back:
        assert recover(workspace).state == "rolled_back"
    with PinnedPath(plan.source / "existing.txt", metadata_write=True) as current:
        original = current.basic_metadata()
        current.set_basic_metadata({**original, "created": original["created"] + 100_000_000})
    result = publication.recover_publication(plan, scope, leases=leases, lease_id=lease_id,
        identity=IDENTITY, policy_revision=lambda: REVISION, rollback_published=rolled_back)
    assert result.state == ("recovery_conflict" if rolled_back else "published_drift")
    assert result.conflicts == ("existing.txt",)


def test_publish_modify_create_delete_and_new_directories_then_restore(workspace):
    plan, *_ = workspace
    (plan.workspace / "other.txt").unlink()
    (plan.workspace / "outputs/新建").mkdir()
    (plan.workspace / "outputs/新建/output.txt").write_bytes(b"result")
    result = publish(workspace)
    assert result.state == "published"
    assert (plan.source / "existing.txt").read_bytes() == b"after"
    assert not (plan.source / "other.txt").exists()
    assert (plan.source / "outputs/新建/output.txt").read_bytes() == b"result"
    assert recover(workspace).state == "rolled_back"
    assert (plan.source / "existing.txt").read_bytes() == b"before"
    assert (plan.source / "other.txt").read_bytes() == b"untouched"
    assert not (plan.source / "outputs/新建").exists()
    assert recover(workspace).state == "rolled_back"


def test_conflicting_lease_is_not_equivalent_to_permission(workspace):
    plan, scope, leases, lease_id = workspace
    leases.release(lease_id, **IDENTITY)
    leases.acquire(owner_id="other", run_id="other", conversation_id="other", exact_paths=[plan.source / "existing.txt"])
    with pytest.raises(WriteLeaseError, match="owner_mismatch"):
        publish(workspace)
    assert (plan.source / "existing.txt").read_bytes() == b"before"
    assert not (plan.transaction / "publication").exists()


def test_no_lease_does_not_grant_publication(workspace):
    plan, scope, leases, _ = workspace
    with pytest.raises(WriteLeaseError, match="owner_mismatch"):
        publish((plan, scope, leases, "missing"))


def test_source_drift_before_publish_is_not_overwritten(workspace):
    plan, *_ = workspace
    (plan.source / "existing.txt").write_bytes(b"external")
    with pytest.raises(stage.StagingRejected, match="Original workspace changed"):
        publish(workspace)
    assert (plan.source / "existing.txt").read_bytes() == b"external"


def test_source_write_during_publish_is_refused_by_os(workspace, monkeypatch):
    plan, *_ = workspace
    original = publication._event
    blocked = []
    def event(root, state, **fields):
        if state == "prepared":
            with pytest.raises(OSError):
                (plan.source / "existing.txt").write_bytes(b"external")
            blocked.append(True)
        return original(root, state, **fields)
    monkeypatch.setattr(publication, "_event", event)
    assert publish(workspace).state == "published"
    assert blocked == [True]


def test_namespace_race_in_rename_gap_is_not_overwritten(workspace, monkeypatch):
    plan, *_ = workspace
    original = PinnedPath.rename_absent
    def rename(handle, destination):
        original(handle, destination)
        if destination.parent.name == "old":
            (plan.source / "existing.txt").write_bytes(b"external in gap")
    monkeypatch.setattr(PinnedPath, "rename_absent", rename)
    with pytest.raises(OSError):
        publish(workspace)
    monkeypatch.setattr(PinnedPath, "rename_absent", original)
    assert (plan.source / "existing.txt").read_bytes() == b"external in gap"
    result = recover(workspace)
    assert result.state == "recovery_conflict"
    assert result.conflicts == ("existing.txt",)
    assert (plan.source / "existing.txt").read_bytes() == b"external in gap"
    assert list((plan.transaction / "publication/old").iterdir())


def test_cancel_in_rename_gap_retains_backup_and_recovers(workspace, monkeypatch):
    plan, *_ = workspace
    original, stopped = PinnedPath.rename_absent, []
    def rename(handle, destination):
        original(handle, destination)
        if destination.parent.name == "old":
            stopped.append(True)
    monkeypatch.setattr(PinnedPath, "rename_absent", rename)
    with pytest.raises(stage.StagingRejected, match="cancelled"):
        publish(workspace, cancelled=lambda: bool(stopped))
    monkeypatch.setattr(PinnedPath, "rename_absent", original)
    assert not (plan.source / "existing.txt").exists()
    assert recover(workspace).state == "rolled_back"
    assert (plan.source / "existing.txt").read_bytes() == b"before"


def test_recovery_wont_overwrite_external_edit_or_same_content_replacement(workspace):
    plan, *_ = workspace
    publish(workspace)
    temp = plan.source / "replacement"
    temp.write_bytes(b"after")
    os.replace(temp, plan.source / "existing.txt")
    result = recover(workspace)
    assert result.state == "recovery_conflict"
    assert (plan.source / "existing.txt").read_bytes() == b"after"


def test_recovery_repeat_is_idempotent(workspace):
    publish(workspace)
    assert recover(workspace).state == "rolled_back"
    assert recover(workspace).state == "rolled_back"


def test_metadata_mismatch_is_rejected_before_source_mutation(workspace, monkeypatch):
    plan, *_ = workspace
    original = PinnedPath.security_string
    def descriptor(handle, mask=7):
        return original(handle, mask) + ("different" if handle.path.parent.name == "incoming" else "")
    monkeypatch.setattr(PinnedPath, "security_string", descriptor)
    with pytest.raises(stage.NativeBoundaryUnavailable, match="owner/group differs"):
        publish(workspace)
    assert (plan.source / "existing.txt").read_bytes() == b"before"


def test_invalid_backup_does_not_remove_valid_published_candidate(workspace):
    plan, *_ = workspace
    publish(workspace)
    next((plan.transaction / "publication/old").iterdir()).write_bytes(b"corrupt backup")
    assert recover(workspace).state == "recovery_conflict"
    assert (plan.source / "existing.txt").read_bytes() == b"after"


def test_restart_after_rollback_does_not_republish(workspace):
    plan, scope, leases, lease_id = workspace
    publish(workspace)
    recover(workspace)
    result = publication.recover_publication(plan, scope, leases=leases, lease_id=lease_id,
                                            identity=IDENTITY, policy_revision=lambda: REVISION)
    assert result.state == "rolled_back"
    assert (plan.source / "existing.txt").read_bytes() == b"before"
    assert recover(workspace).state == "rolled_back"


def test_restart_preserves_a_successfully_published_result(workspace):
    plan, scope, leases, lease_id = workspace
    publish(workspace)
    result = publication.recover_publication(plan, scope, leases=leases, lease_id=lease_id,
                                            identity=IDENTITY, policy_revision=lambda: REVISION)
    assert result.state == "published"
    assert (plan.source / "existing.txt").read_bytes() == b"after"
    (plan.source / "existing.txt").write_bytes(b"external later")
    result = publication.recover_publication(plan, scope, leases=leases, lease_id=lease_id,
                                            identity=IDENTITY, policy_revision=lambda: REVISION)
    assert result.state == "published_drift"
    assert (plan.source / "existing.txt").read_bytes() == b"external later"


def test_revoked_task_blocks_recovery_and_retains_backups(workspace):
    plan, scope, *_ = workspace
    publish(workspace)
    scope.checker.task_id = "revoked"
    with pytest.raises(stage.StagingRejected, match="task or policy changed"):
        recover(workspace)
    assert (plan.source / "existing.txt").read_bytes() == b"after"
    assert list((plan.transaction / "publication/old").iterdir())


def test_failed_second_step_is_not_reported_as_published(workspace, monkeypatch):
    plan, *_ = workspace
    (plan.workspace / "other.txt").write_bytes(b"second change")
    original = publication._event
    count = []
    def event(root, state, **fields):
        original(root, state, **fields)
        if state == "applied":
            count.append(True)
            if len(count) == 1:
                raise OSError("synthetic disk failure")
    monkeypatch.setattr(publication, "_event", event)
    with pytest.raises(OSError, match="disk failure"):
        publish(workspace)
    assert not (plan.transaction / "publication/published.json").exists()
    monkeypatch.setattr(publication, "_event", original)
    assert recover(workspace).state == "rolled_back"
    assert (plan.source / "existing.txt").read_bytes() == b"before"
    assert (plan.source / "other.txt").read_bytes() == b"untouched"


def test_directory_recovery_preserves_new_external_children(workspace):
    plan, *_ = workspace
    (plan.workspace / "outputs/new").mkdir()
    publish(workspace)
    (plan.source / "outputs/new/external.txt").write_bytes(b"external")
    result = recover(workspace)
    assert "outputs/new" in result.conflicts
    assert (plan.source / "outputs/new/external.txt").read_bytes() == b"external"
    assert recover(workspace).state == "recovery_conflict"


def test_restore_plan_requires_independent_expected_digest(workspace):
    plan, *_ = workspace
    restored = publication.restore_staged_plan(plan.transaction, source=plan.source, manifest_sha256=plan.manifest_sha256)
    assert restored == plan
    with pytest.raises(stage.StagingRejected, match="host record"):
        publication.restore_staged_plan(plan.transaction, source=plan.source, manifest_sha256="0" * 64)


@pytest.mark.parametrize("stop_after", ["*", "outputs/old/nested"])
def test_real_process_exit_mid_publication_can_be_recovered(workspace, stop_after):
    if stop_after != "*":
        workspace = deleted_tree(workspace)
    plan, scope, leases, lease_id = workspace
    script = '''
import os, sys
from pathlib import Path
from mewcode.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine
from mewcode.orchestration.write_lease import WriteLeaseStore
from mewcode.sandbox import staged_publication as p
from mewcode.sandbox.lpac_runtime import NativeScope
from mewcode.sandbox.receipt import NativeExecutionReceipt
root, transaction, digest, revision, lease_id, stop_after = sys.argv[1:]
checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(root), RuleEngine(), PermissionMode.AUTO)
checker.task_id = "publication-task"
plan = p.restore_staged_plan(Path(transaction), source=Path(root), manifest_sha256=digest)
store = WriteLeaseStore(Path(transaction).parent / "leases.json", sandbox=checker.sandbox)
event = p._event
def interrupted(root, state, **fields):
    event(root, state, **fields)
    if state == "applied" and (stop_after == "*" or fields.get("relative") == stop_after): os._exit(73)
p._event = interrupted
p.publish_workspace(plan, NativeScope.from_checker(checker), NativeExecutionReceipt(status="exited", exit_code=0, cleanup_confirmed=True, backend="mewcode-windows-lpac"), leases=store, lease_id=lease_id, identity={"owner_id":"publisher", "run_id":"synthetic-run", "conversation_id":"synthetic-conversation"}, policy_revision=lambda: revision)
'''
    result = subprocess.run([sys.executable, "-c", script, str(plan.source), str(plan.transaction),
                             plan.manifest_sha256, REVISION, lease_id, stop_after], capture_output=True, timeout=40,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 73, result.stderr.decode(errors="replace")
    restored = publication.restore_staged_plan(plan.transaction, source=plan.source, manifest_sha256=plan.manifest_sha256)
    assert recover((restored, scope, leases, lease_id)).state == "rolled_back"
    assert (plan.source / "existing.txt").read_bytes() == b"before"
    if stop_after != "*":
        assert (plan.source / "outputs/old/nested/input.txt").read_bytes() == b"retained old version"


def deleted_tree(workspace):
    plan, scope, leases, lease_id = workspace
    (plan.source / "outputs/old/nested").mkdir(parents=True)
    (plan.source / "outputs/old/nested/input.txt").write_bytes(b"retained old version")
    plan = stage.prepare_workspace(scope, plan.transaction.parent, policy_revision=REVISION)
    (plan.workspace / "outputs/old/nested/input.txt").unlink()
    (plan.workspace / "outputs/old/nested").rmdir()
    (plan.workspace / "outputs/old").rmdir()
    return plan, scope, leases, lease_id


def test_deleted_directory_tree_restores_files_and_permissions(workspace):
    workspace = deleted_tree(workspace)
    plan, *_ = workspace
    with PinnedPath(plan.source / "outputs/old", directory=True) as handle:
        original_access = handle.access_signature()
    assert publish(workspace).state == "published"
    assert not (plan.source / "outputs/old").exists()
    assert recover(workspace).state == "rolled_back"
    assert (plan.source / "outputs/old/nested/input.txt").read_bytes() == b"retained old version"
    with PinnedPath(plan.source / "outputs/old", directory=True) as handle:
        assert handle.access_signature() == original_access
    assert recover(workspace).state == "rolled_back"


def test_directory_delete_kernel_preserves_concurrent_child(workspace, monkeypatch):
    workspace = deleted_tree(workspace)
    plan, *_ = workspace
    original = PinnedPath.remove_empty_directory
    def remove(handle):
        if handle.path.name == "nested":
            (handle.path / "external.txt").write_bytes(b"concurrent")
        original(handle)
    monkeypatch.setattr(PinnedPath, "remove_empty_directory", remove)
    with pytest.raises(OSError):
        publish(workspace)
    assert (plan.source / "outputs/old/nested/external.txt").read_bytes() == b"concurrent"
    monkeypatch.setattr(PinnedPath, "remove_empty_directory", original)
    assert recover(workspace).state == "rolled_back"
    assert (plan.source / "outputs/old/nested/input.txt").read_bytes() == b"retained old version"
    assert (plan.source / "outputs/old/nested/external.txt").read_bytes() == b"concurrent"


def test_new_items_inherit_destination_not_private_storage(workspace):
    plan, *_ = workspace
    (plan.workspace / "outputs/new").mkdir()
    (plan.workspace / "outputs/new/file.txt").write_bytes(b"new")
    with PinnedPath(plan.source / "outputs", directory=True) as parent:
        expected_directory = FrozenSecurity.inherited(parent, directory=True)
        expected_file = FrozenSecurity.inherited(expected_directory, directory=False)
    assert publish(workspace).state == "published"
    with PinnedPath(plan.source / "outputs/new", directory=True) as directory:
        assert directory.access_signature() == expected_directory.access_signature()
    with PinnedPath(plan.source / "outputs/new/file.txt") as file:
        assert file.access_signature() == expected_file.access_signature()
    assert recover(workspace).state == "rolled_back"


@pytest.mark.parametrize("protected", [False, True])
def test_exact_dacl_copy_never_adds_parent_entries(tmp_path, protected):
    source, replacement = tmp_path / "source", tmp_path / "replacement"
    source.write_bytes(b"original")
    replacement.write_bytes(b"replacement")
    with PinnedPath(source) as original:
        descriptor = bytearray(original.security_descriptor())
    # A valid explicit-only ACL with the original rights. Git can create this
    # even without DACL protection; SetSecurityInfo would duplicate its ACEs.
    control = struct.unpack_from("<H", descriptor, 2)[0]
    struct.pack_into("<H", descriptor, 2, (control & ~0x1500) | (0x1000 if protected else 0))
    offset = struct.unpack_from("<L", descriptor, 16)[0]
    count = struct.unpack_from("<H", descriptor, offset + 4)[0]
    offset += 8
    for _ in range(count):
        descriptor[offset + 1] &= ~0x10  # INHERITED_ACE only, not access rights.
        offset += struct.unpack_from("<H", descriptor, offset + 2)[0]
    expected = FrozenSecurity(bytes(descriptor))
    with PinnedPath(replacement, security_write=True) as target:
        target.copy_dacl_from(expected)
        target.copy_dacl_from(expected, final=True)
        assert target.access_signature() == expected.access_signature()
        # Reapplying must remain idempotent, not accumulate inherited rules.
        target.copy_dacl_from(expected, final=True)
        assert target.access_signature() == expected.access_signature()


def test_directory_deletion_interruption_restores_before_children(workspace, monkeypatch):
    workspace = deleted_tree(workspace)
    plan, *_ = workspace
    original = publication._event
    def event(root, state, **fields):
        original(root, state, **fields)
        if state == "applied" and fields.get("relative") == "outputs/old/nested":
            raise OSError("interrupted after directory removal")
    monkeypatch.setattr(publication, "_event", event)
    with pytest.raises(OSError, match="interrupted"):
        publish(workspace)
    monkeypatch.setattr(publication, "_event", original)
    assert recover(workspace).state == "rolled_back"
    assert (plan.source / "outputs/old/nested/input.txt").read_bytes() == b"retained old version"


def test_recovery_never_populates_external_replacement_directory(workspace):
    workspace = deleted_tree(workspace)
    plan, *_ = workspace
    publish(workspace)
    (plan.source / "outputs/old/nested").mkdir(parents=True)
    result = recover(workspace)
    assert result.state == "recovery_conflict"
    assert "outputs/old" in result.conflicts
    assert not (plan.source / "outputs/old/nested/input.txt").exists()
    assert list((plan.transaction / "publication/old").iterdir())


def test_directory_restore_preserves_original_timestamps(workspace):
    workspace = deleted_tree(workspace)
    plan, *_ = workspace
    old_time = 1600000000123456700
    path = plan.source / "outputs/old/nested"
    os.utime(path, ns=(old_time, old_time))
    assert publish(workspace).state == "published"
    assert recover(workspace).state == "rolled_back"
    assert path.stat().st_mtime_ns == old_time


@pytest.mark.parametrize("location", ["source", "workspace"])
def test_named_streams_are_not_silently_discarded(workspace, location):
    plan, *_ = workspace
    path = getattr(plan, location) / "existing.txt"
    Path(str(path) + ":synthetic-metadata").write_bytes(b"must not disappear")
    with pytest.raises(stage.NativeBoundaryUnavailable, match="stream"):
        publish(workspace)
    assert (plan.source / "existing.txt").read_bytes() == b"before"
    assert Path(str(path) + ":synthetic-metadata").read_bytes() == b"must not disappear"


def test_invalid_frozen_descriptor_offsets_are_rejected():
    import struct
    raw = struct.pack("<BBHLLLL", 1, 0, 0x8000, 65532, 0, 0, 0)
    with pytest.raises(stage.NativeBoundaryUnavailable, match="offset"):
        FrozenSecurity(raw).security_descriptor()


def test_new_readonly_output_keeps_attributes_and_can_be_rolled_back(workspace):
    import stat
    plan, *_ = workspace
    path = plan.workspace / "outputs/object"
    path.write_bytes(b"immutable object")
    path.chmod(stat.S_IREAD)
    assert publish(workspace).state == "published"
    assert (plan.source / "outputs/object").stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
    assert recover(workspace).state == "rolled_back"
    assert not (plan.source / "outputs/object").exists()


def test_updated_file_keeps_creation_time_and_candidate_write_time(workspace):
    plan, *_ = workspace
    with PinnedPath(plan.source / "existing.txt") as original:
        creation = original.basic_metadata()["created"]
    written = 1610000000123456700
    os.utime(plan.workspace / "existing.txt", ns=(written, written))
    assert publish(workspace).state == "published"
    with PinnedPath(plan.source / "existing.txt") as current:
        assert current.basic_metadata()["created"] == creation
    assert (plan.source / "existing.txt").stat().st_mtime_ns == written
    assert recover(workspace).state == "rolled_back"


def test_directory_recovery_itself_is_restartable(workspace, monkeypatch):
    workspace = deleted_tree(workspace)
    plan, *_ = workspace
    publish(workspace)
    original = publication._event
    def event(root, state, **fields):
        original(root, state, **fields)
        if state == "restored" and fields.get("relative") == "outputs/old":
            raise SystemExit(74)
    monkeypatch.setattr(publication, "_event", event)
    with pytest.raises(SystemExit):
        recover(workspace)
    monkeypatch.setattr(publication, "_event", original)
    assert recover(workspace).state == "rolled_back"
    assert (plan.source / "outputs/old/nested/input.txt").read_bytes() == b"retained old version"


def test_unexpected_child_in_private_directory_backup_is_preserved(workspace):
    import json
    workspace = deleted_tree(workspace)
    plan, *_ = workspace
    publish(workspace)
    journal = plan.transaction / "publication"
    saved = json.loads((journal / "plan.json").read_text(encoding="utf-8"))
    entry = next(e for e in saved["entries"] if e["relative"] == "outputs/old")
    foreign = journal / "old" / entry["slot"] / "unexpected.txt"
    foreign.write_bytes(b"do not move")
    assert recover(workspace).state == "recovery_conflict"
    assert foreign.read_bytes() == b"do not move"
    assert not (plan.source / "outputs/old").exists()


def test_expired_and_narrow_lease_fail_closed(workspace):
    plan, scope, leases, lease_id = workspace
    leases.release(lease_id, **IDENTITY)
    expired = leases.acquire(**IDENTITY, exact_paths=[plan.source / "existing.txt"], now=time.time() - 10, ttl_seconds=1)
    with pytest.raises(WriteLeaseError, match="expired"):
        publish((plan, scope, leases, expired["lease"]["lease_id"]))
    narrow = leases.acquire(**IDENTITY, exact_paths=[plan.source / "other.txt"])
    with pytest.raises(WriteLeaseError, match="outside_lease"):
        publish((plan, scope, leases, narrow["lease"]["lease_id"]))
