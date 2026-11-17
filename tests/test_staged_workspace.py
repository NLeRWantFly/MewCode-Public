from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from mewcode.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine
from mewcode.permissions.task_authorization import TaskAuthorization
from mewcode.sandbox.lpac_runtime import NativeScope
from mewcode.sandbox.receipt import NativeExecutionReceipt
from mewcode.sandbox import staged_workspace as stage
from mewcode.sandbox.windows import NativeBoundaryUnavailable

REVISION = hashlib.sha256(b"host-policy-snapshot").hexdigest()


def successful_receipt(**overrides):
    return NativeExecutionReceipt(**{
        "status": "exited", "exit_code": 0, "cleanup_confirmed": True,
        "backend": "mewcode-windows-lpac", **overrides,
    })


@pytest.fixture
def inputs(tmp_path):
    source, host = tmp_path / "项目 source", tmp_path / "private-host"
    source.mkdir()
    host.mkdir()
    (source / "input.txt").write_bytes(b"original")
    (source / "empty").mkdir()
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(source)),
                                RuleEngine(), PermissionMode.AUTO)
    checker.task_id = "task-stage"
    return NativeScope.from_checker(checker), host


def prepare(inputs, **kwargs):
    scope, host = inputs
    return stage.prepare_workspace(scope, host, policy_revision=REVISION, **kwargs)


def verify(plan, inputs, **kwargs):
    return stage.verify_workspace(plan, inputs[0], successful_receipt(),
                                  policy_revision=REVISION, **kwargs)


def test_prepare_copies_bytes_to_disjoint_private_tree(inputs):
    plan = prepare(inputs)
    assert (plan.workspace / "input.txt").read_bytes() == b"original"
    assert (plan.workspace / "empty").is_dir()
    assert plan.workspace != plan.source
    manifest = (plan.transaction / "manifest.json").read_bytes()
    assert hashlib.sha256(manifest).hexdigest() == plan.manifest_sha256
    assert b"original" not in manifest
    assert json.loads(manifest)["policy_revision"] == REVISION
    assert not (plan.workspace / "manifest.json").exists()


def test_create_read_modify_and_atomic_replace_only_produce_unpublished_plan(inputs):
    plan = prepare(inputs)
    new_file = plan.workspace / "新建.txt"
    new_file.write_bytes(b"first")
    assert new_file.read_bytes() == b"first"
    new_file.write_bytes(b"second")
    lock = plan.workspace / "input.lock"
    lock.write_bytes(b"replacement")
    os.replace(lock, plan.workspace / "input.txt")
    result = verify(plan, inputs)
    assert result.state == "verified_unpublished"
    assert [(change.relative, change.operation) for change in result.changes] == [
        ("input.txt", "modify"), ("新建.txt", "create")]
    assert (plan.source / "input.txt").read_bytes() == b"original"
    assert not (plan.source / "新建.txt").exists()
    assert not hasattr(result, "approved") and not hasattr(result, "committed")
    events = [json.loads(line) for line in (plan.transaction / "events.jsonl").read_text().splitlines()]
    assert [event["state"] for event in events] == ["preparing", "prepared", "verified_unpublished"]


def test_delete_is_a_candidate_not_an_original_mutation(inputs):
    plan = prepare(inputs)
    (plan.workspace / "input.txt").unlink()
    result = verify(plan, inputs)
    assert result.changes[0].operation == "delete"
    assert (plan.source / "input.txt").exists()


def test_no_change_is_reproducible_without_a_commit(inputs):
    plan = prepare(inputs)
    first, second = verify(plan, inputs), verify(plan, inputs)
    assert first == second
    assert first.changes == ()


@pytest.mark.parametrize("private", [".mewcode", ".codex", ".agents"])
def test_private_authority_is_never_copied_or_admitted(inputs, private):
    source = inputs[0].workspace
    (source / private).mkdir()
    (source / private / "policy.json").write_text("private")
    plan = prepare(inputs)
    assert not (plan.workspace / private).exists()
    (plan.workspace / private).mkdir()
    (plan.workspace / private / "policy.json").write_text("injected")
    with pytest.raises(stage.StagingRejected, match="protected"):
        verify(plan, inputs)


@pytest.mark.parametrize("relative", [".venv/tool.py", "venv/tool.py", ".git/config", ".git/hooks/pre-commit"])
def test_readonly_runtime_and_git_controls_cannot_change(inputs, relative):
    path = inputs[0].workspace / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"protected")
    plan = prepare(inputs)
    (plan.workspace / relative).write_bytes(b"changed")
    with pytest.raises(stage.StagingRejected, match="read-only"):
        verify(plan, inputs)
    assert path.read_bytes() == b"protected"


def test_new_runtime_is_also_readonly(inputs):
    plan = prepare(inputs)
    (plan.workspace / ".venv").mkdir()
    (plan.workspace / ".venv/evil.py").write_text("not executable")
    with pytest.raises(stage.StagingRejected, match="read-only"):
        verify(plan, inputs)


@pytest.mark.parametrize("authorized", [False, True])
def test_git_index_candidate_requires_explicit_git_write(inputs, authorized):
    scope, _ = inputs
    metadata = scope.workspace / ".git"
    metadata.mkdir()
    (metadata / "index").write_bytes(b"old index")
    if authorized:
        authorization = TaskAuthorization.from_user_scope({
            "read_directories": [str(scope.workspace)], "write_directories": [str(scope.workspace)],
            "allowed_behaviors": ["git_write"], "ttl_seconds": 3600,
        }, task_id=scope.task_id)
        scope.checker.set_task_authorization(authorization, task_id=scope.task_id)
    plan = prepare(inputs)
    (plan.workspace / ".git/index").write_bytes(b"new index")
    if authorized:
        assert verify(plan, inputs).changes[0].relative == ".git/index"
    else:
        with pytest.raises(stage.StagingRejected, match="read-only"):
            verify(plan, inputs)
    assert (metadata / "index").read_bytes() == b"old index"


@pytest.mark.parametrize("mutation", ["edit", "delete", "create", "replace-identical"])
def test_source_changes_require_reconciliation(inputs, mutation):
    plan = prepare(inputs)
    path = plan.source / "input.txt"
    if mutation == "edit":
        path.write_bytes(b"externally edited")
    elif mutation == "delete":
        path.unlink()
    elif mutation == "create":
        (plan.source / "outside.txt").write_bytes(b"external")
    else:
        replacement = plan.source / "replacement.txt"
        replacement.write_bytes(b"original")
        os.replace(replacement, path)
    with pytest.raises(stage.StagingRejected, match="Original workspace changed"):
        verify(plan, inputs)


@pytest.mark.parametrize("overrides", [
    {"status": "cancelled"}, {"status": "timed_out"}, {"status": "spawn_failed"},
    {"exit_code": 1}, {"cleanup_confirmed": None}, {"cleanup_confirmed": False},
    {"output_truncated": True}, {"backend": "unrestricted-host"},
])
def test_failure_or_unconfirmed_cleanup_never_admits_changes(inputs, overrides):
    plan = prepare(inputs)
    (plan.workspace / "input.txt").write_bytes(b"candidate")
    with pytest.raises(stage.StagingRejected, match="cleanup"):
        stage.verify_workspace(plan, inputs[0], successful_receipt(**overrides), policy_revision=REVISION)
    assert (plan.source / "input.txt").read_bytes() == b"original"


def test_manifest_corruption_is_not_authority(inputs):
    plan = prepare(inputs)
    (plan.transaction / "manifest.json").write_text('{"approved":true}')
    with pytest.raises(stage.StagingRejected, match="manifest changed"):
        verify(plan, inputs)


def test_policy_change_or_task_change_requires_new_preparation(inputs):
    plan = prepare(inputs)
    with pytest.raises(stage.StagingRejected, match="policy or task"):
        stage.verify_workspace(plan, inputs[0], successful_receipt(), policy_revision="f" * 64)
    inputs[0].checker.task_id = "another-task"
    with pytest.raises(stage.StagingRejected, match="policy or task"):
        verify(plan, inputs)


@pytest.mark.parametrize("revision", ["", "model-approved", "A" * 64, None])
def test_policy_revision_is_required_before_creating_a_transaction(inputs, revision):
    with pytest.raises(stage.StagingRejected, match="policy revision"):
        stage.prepare_workspace(*inputs, policy_revision=revision)
    assert list(inputs[1].iterdir()) == []


def test_entry_and_byte_budgets_fail_closed(inputs):
    with pytest.raises(stage.StagingRejected, match="byte budget"):
        prepare(inputs, max_bytes=2)
    with pytest.raises(stage.StagingRejected, match="entry budget"):
        prepare(inputs, max_entries=1)
    assert (inputs[0].workspace / "input.txt").read_bytes() == b"original"


@pytest.mark.parametrize("bounds,expected", [({"max_bytes": 8}, "byte budget"),
                                           ({"max_entries": 3}, "entry budget")])
def test_output_verification_preserves_tightened_host_bounds(inputs, bounds, expected):
    plan = prepare(inputs, **bounds)
    (plan.workspace / "new.txt").write_bytes(b"too much")
    with pytest.raises(stage.StagingRejected, match=expected):
        verify(plan, inputs)


@pytest.mark.parametrize("value", [True, 1.5, "12", None])
def test_invalid_limit_types_reject_before_creating_a_transaction(inputs, value):
    with pytest.raises(stage.StagingRejected, match="Invalid staging bounds"):
        prepare(inputs, max_entries=value)
    assert list(inputs[1].iterdir()) == []


def test_host_directory_cannot_be_inside_or_contain_source(inputs):
    scope, _ = inputs
    for parent in (scope.workspace, scope.workspace.parent):
        with pytest.raises(stage.StagingRejected, match="disjoint"):
            stage.prepare_workspace(scope, parent, policy_revision=REVISION)


def test_existing_host_transaction_is_not_reused(inputs):
    first = prepare(inputs)
    second = prepare(inputs)
    assert first.transaction != second.transaction
    assert (first.transaction / "manifest.json").exists()


def test_staged_type_change_is_rejected(inputs):
    plan = prepare(inputs)
    path = plan.workspace / "input.txt"
    path.unlink()
    path.mkdir()
    with pytest.raises(stage.StagingRejected, match="type changes"):
        verify(plan, inputs)


def test_hardlinked_input_and_output_are_rejected(inputs, tmp_path):
    external = tmp_path / "synthetic-external.txt"
    external.write_bytes(b"outside")
    plan = prepare(inputs)
    os.link(external, plan.workspace / "linked.txt")
    with pytest.raises(NativeBoundaryUnavailable, match="hard-linked"):
        verify(plan, inputs)
    os.link(external, inputs[0].workspace / "linked.txt")
    with pytest.raises(NativeBoundaryUnavailable, match="hard-linked"):
        prepare(inputs)


def test_staged_traversal_cannot_be_invented_in_a_manifest(inputs):
    plan = prepare(inputs)
    # Validation derives names from actual directory entries, never the
    # child-authored JSON or a proposed destination path list.
    (plan.workspace / "candidate.json").write_text('{"path":"../../outside.txt","approved":true}')
    result = verify(plan, inputs)
    assert [change.relative for change in result.changes] == ["candidate.json"]


def test_path_and_fd_ctime_may_differ_without_identity_drift(inputs, monkeypatch):
    fstat = os.fstat
    def fd_info(fd):
        info = fstat(fd)
        fields = {key: getattr(info, key) for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns")}
        return SimpleNamespace(**fields, st_ctime_ns=123)
    monkeypatch.setattr(stage.os, "fstat", fd_info)
    plan = prepare(inputs)
    assert verify(plan, inputs).changes == ()


def test_prototype_scope_maps_to_original_policy(inputs):
    from scripts.probe_staged_workspace import MappedScope
    plan = prepare(inputs)
    mapped = MappedScope(inputs[0], plan.workspace)
    assert mapped.workspace == plan.workspace
    assert mapped.allows(plan.workspace / "input.txt", write=True)
    assert not mapped.allows(plan.workspace / ".git/config", write=True)
    assert not mapped.allows(plan.workspace / ".codex/config", write=False)
    inputs[0].checker.task_id = "different"
    assert not mapped.current()


def test_prototype_protections_pin_readonly_files_and_git_container(inputs):
    from scripts.probe_staged_workspace import protections
    source = inputs[0].workspace
    (source / ".git").mkdir()
    (source / ".git/config").write_text("[core]\n")
    plan = prepare(inputs)
    readonly, containers = protections(plan)
    assert str(plan.workspace / ".git/config") in readonly
    assert str(plan.workspace / ".git") in containers
    assert str(plan.workspace) not in readonly


def test_native_shared_transport_does_not_admit_prototype_evidence(tmp_path):
    from scripts import build_windows_lpac
    from scripts.probe_staged_workspace import SCHEMA, SOURCES
    from mewcode.sandbox.native_evidence import read_report
    assert any("StagedWorkspacePrototype" in path.name for path in build_windows_lpac.SOURCES)
    assert any("StagedWorkspacePrototype" in path.name for path in SOURCES)
    report = tmp_path / "prototype.json"
    report.write_text(json.dumps({"schema": SCHEMA, "ok": True, "complete": True}))
    with pytest.raises(ValueError, match="structured live probe"):
        read_report(report)
