from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mewcode.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine, Rule
from mewcode.sandbox import lpac_runtime as lpac
from mewcode.sandbox import staged_execution as execution
from mewcode.sandbox import staged_publication as publication
from mewcode.sandbox.receipt import NativeExecutionReceipt
from mewcode.sandbox.transaction_registry import TransactionRegistry, CURRENT_TRANSACTION, workspace_write_block
from mewcode.tools.write_file import WriteFile


@pytest.mark.asyncio
async def test_network_git_override_cannot_select_an_unverified_directory(tmp_path, monkeypatch):
    from mewcode.sandbox import network_git
    verified, arbitrary = tmp_path / 'verified', tmp_path / 'arbitrary'
    verified.mkdir()
    arbitrary.mkdir()
    monkeypatch.setattr(network_git, 'runtime_git_directory', lambda: verified, raising=False)
    with pytest.raises(execution.stage.StagingRejected, match='verified host runtime'):
        await execution.execute_staged('synthetic', scope=SimpleNamespace(publication_mode='discard'),
            helper=tmp_path/'unused-helper.exe', timeout=2, runtime_git_dir=arbitrary,
            request_transform=lambda envelope: None)


@pytest.mark.asyncio
async def test_network_git_override_requires_the_network_request_transform(tmp_path, monkeypatch):
    from mewcode.sandbox import network_git
    verified = tmp_path / 'verified'
    verified.mkdir()
    monkeypatch.setattr(network_git, 'runtime_git_directory', lambda: verified, raising=False)
    with pytest.raises(execution.stage.StagingRejected, match='verified host runtime'):
        await execution.execute_staged('synthetic', scope=SimpleNamespace(publication_mode='discard'),
            helper=tmp_path/'unused-helper.exe', timeout=2, runtime_git_dir=verified)


@pytest.mark.asyncio
async def test_network_git_override_requires_package_verification_before_staging(tmp_path, monkeypatch):
    from mewcode.sandbox import network_git
    verified = tmp_path / 'verified'
    verified.mkdir()
    monkeypatch.setattr(network_git, 'runtime_git_directory', lambda: verified)
    def corrupt_package():
        raise RuntimeError('fixed network package failed hash validation')
    monkeypatch.setattr(network_git, 'paths', corrupt_package)
    with pytest.raises(RuntimeError, match='failed hash validation'):
        await execution.execute_staged('synthetic', scope=SimpleNamespace(publication_mode='discard'),
            helper=tmp_path/'unused-helper.exe', timeout=2, runtime_git_dir=verified,
            request_transform=lambda envelope: None)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root, private = tmp_path / "source", tmp_path / "private"
    root.mkdir()
    private.mkdir()
    (root / "a.txt").write_text("original")
    (root / "outputs").mkdir()
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(root)), RuleEngine(), PermissionMode.AUTO)
    checker.task_id = "host-task"
    scope = lpac.NativeScope.from_checker(checker)
    registry = TransactionRegistry(private)
    monkeypatch.setattr("mewcode.sandbox.transaction_registry.default_root", lambda: private)
    helper = private / "bundle/runtime.exe"
    helper.parent.mkdir()
    (helper.parent / "git").mkdir()
    (helper.parent / "git/git.exe").write_bytes(b"synthetic-only")
    monkeypatch.setattr(lpac, "HostCancelEvent", Mock(return_value=Mock(name="synthetic-cancel")))
    monkeypatch.setattr(lpac, "build_runtime_request", lambda command, mapped, runtime, **kwargs:
                        {"request_id": "a" * 32, "timeout_ms": 3000, "workspace": str(mapped.workspace)})
    return scope, registry, helper


def native(**kwargs):
    return NativeExecutionReceipt("exited", exit_code=0, backend=lpac.BACKEND, cleanup_confirmed=True, **kwargs)


def fake_transport(monkeypatch, action=None, receipt=None):
    async def invoke(helper, envelope, cancel, on_receipt=None):
        assert envelope["schema"] == execution.PROTOCOL
        workspace = Path(envelope["request"]["workspace"])
        assert not Path(envelope["original_workspace"]).is_relative_to(workspace)
        if action:
            action(workspace)
        result = receipt if receipt is not None else native()
        on_receipt(result)
        return result
    monkeypatch.setattr(lpac, "invoke_helper", invoke)


async def run(setup):
    scope, registry, helper = setup
    return await execution.execute_staged("synthetic", scope=scope, registry=registry, helper=helper, timeout=3)


@pytest.mark.asyncio
async def test_readonly_lifecycle_settles_without_write_lease(setup, monkeypatch):
    fake_transport(monkeypatch)
    result = await run(setup)
    assert not result.is_error
    assert result.publication_state == "no_changes"
    assert result.transaction_id
    assert not setup[1].pending()
    assert "publication=no_changes" in result.render()


@pytest.mark.asyncio
async def test_oversized_envelope_is_rejected_without_unknown_cleanup_fence(setup, monkeypatch):
    from unittest.mock import AsyncMock
    transport = AsyncMock()
    monkeypatch.setattr(lpac, "invoke_helper", transport)
    builder = lpac.build_runtime_request
    def small_limit(*args, **kwargs):
        nested = builder(*args, **kwargs)
        monkeypatch.setattr(lpac, "MAX_REQUEST", len(json.dumps(nested, ensure_ascii=True).encode()) + 1)
        assert lpac.encode_request(nested)
        return nested
    monkeypatch.setattr(lpac, "build_runtime_request", small_limit)
    result = await run(setup)
    assert result.is_error
    assert "transport bound" in result.detail
    assert result.publication_state == "rejected"
    transport.assert_not_called()
    assert not setup[1].pending()
    assert not workspace_write_block(setup[0].workspace)


@pytest.mark.asyncio
async def test_real_publication_after_native_receipt(setup, monkeypatch):
    # Coordinator unit test: publication uses real handles, while metadata
    # capability is isolated. This is NOT a production acceptance receipt.
    monkeypatch.setattr(execution, "metadata_preflight", lambda *args: None)
    fake_transport(monkeypatch, lambda w: (w / "a.txt").write_text("published"))
    result = await run(setup)
    assert not result.is_error, result.detail
    assert result.publication_state == "published"
    assert (setup[0].workspace / "a.txt").read_text() == "published"
    assert not setup[1].pending()


@pytest.mark.asyncio
async def test_unknown_original_metadata_does_not_publish(setup, monkeypatch):
    from mewcode.sandbox import workspace_metadata as metadata
    capture = metadata.capture_metadata
    def unknown(path):
        result = capture(path)
        result["security"]["sacl"] = {"state": "unavailable", "present": None, "null": None, "sddl": None}
        return result
    monkeypatch.setattr(metadata, "capture_metadata", unknown)
    fake_transport(monkeypatch, lambda w: (w / "a.txt").write_text("candidate"))
    result = await run(setup)
    assert result.is_error and "SACL" in result.detail
    assert (setup[0].workspace / "a.txt").read_text() == "original"


@pytest.mark.asyncio
async def test_metadata_only_candidate_is_not_silently_discarded(setup, monkeypatch):
    import os
    def change(w):
        path = w / "a.txt"
        os.utime(path, (10000, 10000))
    fake_transport(monkeypatch, change)
    result = await run(setup)
    assert result.is_error and "Metadata-only" in result.detail


@pytest.mark.asyncio
async def test_restart_after_publication_preserves_committed_result(setup, monkeypatch):
    monkeypatch.setattr(execution, "metadata_preflight", lambda *args: None)
    fake_transport(monkeypatch, lambda w: (w / "a.txt").write_text("committed"))
    original = publication.publish_workspace
    def committed_then_transport_lost(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated process loss after durable publication marker")
    monkeypatch.setattr(publication, "publish_workspace", committed_then_transport_lost)
    assert (await run(setup)).is_error
    assert setup[1].pending()[0]["state"] == "recovery_required"
    outcome = execution.reconcile(setup[0], TransactionRegistry(setup[1].root))
    assert outcome[0]["state"] == "published", outcome
    assert not setup[1].pending()
    assert (setup[0].workspace / "a.txt").read_text() == "committed"


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", [replace(native(), exit_code=2), replace(native(), cleanup_confirmed=False),
                                     replace(native(), output_truncated=True)])
async def test_incomplete_native_never_publishes(setup, monkeypatch, receipt):
    fake_transport(monkeypatch, lambda w: (w / "a.txt").write_text("candidate"), receipt)
    result = await run(setup)
    assert result.is_error
    assert (setup[0].workspace / "a.txt").read_text() == "original"
    assert bool(setup[1].pending()) == (receipt.cleanup_confirmed is not True)


@pytest.mark.asyncio
async def test_transport_loss_fences_even_without_any_receipt(setup, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("transport lost")
    monkeypatch.setattr(lpac, "invoke_helper", fail)
    assert (await run(setup)).is_error
    assert setup[1].pending()[0]["state"] == "cleanup_unknown"
    assert workspace_write_block(setup[0].workspace)


@pytest.mark.asyncio
@pytest.mark.parametrize("proof_state", ["valid", "wrong_hash", "false", "missing"])
async def test_restart_imports_only_bound_final_helper_cleanup_facts(setup, monkeypatch, proof_state):
    import hashlib
    async def lost_ack(helper, envelope, cancel, **kwargs):
        assert not Path(envelope["cleanup_receipt_path"]).is_relative_to(Path(envelope["request"]["workspace"]))
        if proof_state != "missing":
            proof = {"schema": "mewcode-native-cleanup-v1", "request_id": envelope["request_id"],
                "request_sha256": hashlib.sha256(lpac.encode_request(envelope)).hexdigest(),
                "backend": lpac.BACKEND, "completed": True,
                "job_cleanup_ok": True, "profile_cleanup_ok": True, "acl_cleanup_ok": proof_state != "false"}
            if proof_state == "wrong_hash":
                proof["request_sha256"] = "0" * 64
            Path(envelope["cleanup_receipt_path"]).write_text(json.dumps(proof))
        raise RuntimeError("host transport lost after helper completion")
    monkeypatch.setattr(lpac, "invoke_helper", lost_ack)
    result = await run(setup)
    assert result.publication_state == "cleanup_unknown"
    outcome = execution.reconcile(setup[0], TransactionRegistry(setup[1].root))
    assert outcome[0]["state"] == ("rejected" if proof_state == "valid" else "recovery_required"), outcome
    assert bool(setup[1].pending()) == (proof_state != "valid")
    assert (setup[0].workspace / "a.txt").read_text() == "original"
    if proof_state == "valid":
        record = setup[1].get(result.transaction_id)
        assert record["cleanup_evidence"]["completed"] is True
        assert "exit_code" not in record["native_receipt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [True, False])
async def test_cancel_releases_only_when_cleanup_confirmed(setup, monkeypatch, confirmed):
    async def cancel(*args, on_receipt, **kwargs):
        if confirmed:
            on_receipt(replace(native(), status="cancelled"))
        raise asyncio.CancelledError
    monkeypatch.setattr(lpac, "invoke_helper", cancel)
    with pytest.raises(asyncio.CancelledError):
        await run(setup)
    assert bool(setup[1].pending()) is not confirmed


@pytest.mark.asyncio
async def test_policy_change_prevents_publication(setup, monkeypatch):
    def mutate(w):
        (w / "a.txt").write_text("candidate")
        setup[0].checker.rule_engine.append_session_rule(Rule("WriteFile", "*", "deny"))
    fake_transport(monkeypatch, mutate)
    result = await run(setup)
    assert result.is_error
    assert (setup[0].workspace / "a.txt").read_text() == "original"


@pytest.mark.asyncio
async def test_source_race_preserved(setup, monkeypatch):
    def mutate(w):
        (w / "a.txt").write_text("candidate")
        (setup[0].workspace / "a.txt").write_text("external")
    fake_transport(monkeypatch, mutate)
    result = await run(setup)
    assert result.is_error
    assert (setup[0].workspace / "a.txt").read_text() == "external"


def test_registry_rejects_concurrent_workspace_and_allows_another(setup, tmp_path):
    scope, registry, _ = setup
    registry.begin(scope.workspace, task_id=scope.task_id, policy_revision=execution.policy_revision(scope))
    with pytest.raises(RuntimeError, match="unsettled"):
        registry.begin(scope.workspace, task_id="other", policy_revision="a" * 64)
    other = tmp_path / "another"
    other.mkdir()
    registry.begin(other, task_id="other", policy_revision="a" * 64)
    assert len(registry.pending()) == 2


def test_restart_inventory_does_not_invent_cleanup_or_authority(setup):
    scope, registry, _ = setup
    record = registry.begin(scope.workspace, task_id=scope.task_id, policy_revision=execution.policy_revision(scope))
    registry.update(record["id"], "executing")
    restarted = TransactionRegistry(registry.root)
    assert restarted.startup_inventory()["pending"] == 1
    assert restarted.startup_inventory()["transactions"][0]["cleanup_confirmed"] is None
    assert execution.reconcile(scope, restarted)[0]["state"] == "recovery_required"
    assert workspace_write_block(scope.workspace)


def test_unsettled_record_blocks_structured_writes_not_reads(setup):
    scope, registry, _ = setup
    record = registry.begin(scope.workspace, task_id=scope.task_id, policy_revision=execution.policy_revision(scope))
    registry.update(record["id"], "cleanup_unknown")
    decision = scope.checker.check(WriteFile(), {"file_path": str(scope.workspace / "a.txt"), "content": "x"})
    assert decision.source == "native_transaction_recovery"
    token = CURRENT_TRANSACTION.set(record["id"])
    try:
        assert scope.allows(scope.workspace / "a.txt", write=True)
    finally:
        CURRENT_TRANSACTION.reset(token)


def test_settled_receipt_cannot_be_reopened(setup):
    scope, registry, _ = setup
    record = registry.begin(scope.workspace, task_id=scope.task_id, policy_revision=execution.policy_revision(scope))
    registry.update(record["id"], "rejected")
    with pytest.raises(RuntimeError, match="cannot be reopened"):
        registry.update(record["id"], "publishing")


async def interrupted_publication(setup, monkeypatch, *, committed):
    monkeypatch.setattr(execution, "metadata_preflight", lambda *args: None)
    fake_transport(monkeypatch, lambda w: (w / "a.txt").write_text("candidate"))
    if committed:
        original = publication.publish_workspace
        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("lost acknowledgement")
        monkeypatch.setattr(publication, "publish_workspace", crash)
    else:
        original = publication._event
        def event(root, state, **fields):
            original(root, state, **fields)
            if state == "applied":
                raise RuntimeError("interrupted before commit marker")
        monkeypatch.setattr(publication, "_event", event)
    assert (await run(setup)).is_error
    return setup[1].pending()[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_explicit_handoff_recovers_under_new_policy_without_rewriting_history(setup, monkeypatch, committed):
    from mewcode.sandbox.recovery_handoff import preview
    record = await interrupted_publication(setup, monkeypatch, committed=committed)
    old_policy, old_task = record["policy_revision"], record["task_id"]
    manifest = Path(record["transaction"]) / "manifest.json"
    frozen = manifest.read_bytes()
    setup[0].checker.task_id = "new-current-task"
    current = lpac.NativeScope.from_checker(setup[0].checker)
    assert execution.reconcile(current, setup[1])[0]["state"] == "recovery_required"
    offer = preview(current, setup[1], record["id"])
    assert offer["targets"] == ["a.txt"]
    assert offer["original_task_id"] == old_task
    result = execution.reconcile(current, setup[1], confirmation=offer)
    assert result[0]["state"] == ("published" if committed else "rolled_back"), result
    assert (current.workspace / "a.txt").read_text() == ("candidate" if committed else "original")
    assert manifest.read_bytes() == frozen
    saved = setup[1].get(record["id"])
    assert saved["policy_revision"] == old_policy and saved["task_id"] == old_task
    assert Path(saved["last_recovery_handoff"]).is_file()
    with pytest.raises(RuntimeError, match="no longer pending"):
        execution.reconcile(current, setup[1], confirmation=offer)


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["policy", "journal", "receipt", "target"])
async def test_recovery_confirmation_does_not_survive_drift(setup, monkeypatch, drift):
    from mewcode.sandbox.recovery_handoff import preview
    record = await interrupted_publication(setup, monkeypatch, committed=True)
    current = setup[0]
    offer = preview(current, setup[1], record["id"])
    if drift == "policy":
        current.checker._approved_calls.add("new-unrelated-call")
    elif drift == "journal":
        journal = Path(record["transaction"]) / "publication/plan.json"
        journal.write_bytes(journal.read_bytes() + b"\n")
    elif drift == "receipt":
        setup[1].update(record["id"], record["state"], reason="changed host record")
    else:
        (current.workspace / "a.txt").write_text("external")
    outcome = execution.reconcile(current, setup[1], confirmation=offer)
    assert outcome[0]["state"] == "recovery_required", outcome
    assert (current.workspace / "a.txt").read_text() == ("external" if drift == "target" else "candidate")
    assert setup[1].pending()


@pytest.mark.asyncio
async def test_handoff_never_overrides_current_denial_or_unknown_cleanup(setup, monkeypatch):
    from mewcode.sandbox.recovery_handoff import preview
    record = await interrupted_publication(setup, monkeypatch, committed=True)
    setup[0].checker.rule_engine.append_session_rule(Rule("WriteFile", "*", "deny"))
    with pytest.raises(RuntimeError, match="does not authorize"):
        preview(setup[0], setup[1], record["id"])
    setup[0].checker.rule_engine._session_rules.clear()
    setup[1].update(record["id"], record["state"], native_receipt={"cleanup_confirmed": False})
    offer = preview(setup[0], setup[1], record["id"])
    assert "cleanup" in execution.reconcile(setup[0], setup[1], confirmation=offer)[0]["reason"].lower()
    assert setup[1].pending()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_handoff_receipt_append_before_ledger_link_is_crash_safe(setup, monkeypatch, changed):
    from mewcode.sandbox.recovery_handoff import preview, record_confirmation
    record = await interrupted_publication(setup, monkeypatch, committed=True)
    offer = preview(setup[0], setup[1], record["id"])
    receipt = Path(record_confirmation(setup[1], offer))
    # Simulate process loss after durable append, before the registry link.
    assert not setup[1].get(record["id"]).get("last_recovery_handoff")
    if changed:
        receipt.write_bytes(receipt.read_bytes() + b"\n")
    saved = receipt.read_bytes()
    fresh = TransactionRegistry(setup[1].root)
    outcome = execution.reconcile(setup[0], fresh, confirmation=offer)
    assert receipt.read_bytes() == saved
    assert outcome[0]["state"] == ("recovery_required" if changed else "published"), outcome
    assert bool(fresh.pending()) is changed
    assert (setup[0].workspace / "a.txt").read_text() == "candidate"


@pytest.mark.asyncio
async def test_handoff_receipt_partial_write_never_poisons_confirmation(setup, monkeypatch):
    from mewcode.sandbox import recovery_handoff as handoff
    record = await interrupted_publication(setup, monkeypatch, committed=True)
    offer = handoff.preview(setup[0], setup[1], record["id"])
    original_write = handoff.stage._write_new
    def partial_write(path, raw):
        if path.suffix == ".pending":
            original_write(path, b'{"schema":')
            raise OSError("synthetic loss during receipt write")
        original_write(path, raw)
    monkeypatch.setattr(handoff.stage, "_write_new", partial_write)
    first = execution.reconcile(setup[0], setup[1], confirmation=offer)
    assert first[0]["state"] == "recovery_required"
    directory = setup[1].root / "recovery-confirmations"
    assert not (directory / (offer["token"] + ".json")).exists()
    remnants = list(directory.glob("*.pending"))
    assert len(remnants) == 1 and remnants[0].read_bytes() == b'{"schema":'
    assert handoff.preview(setup[0], setup[1], record["id"]) == offer
    monkeypatch.setattr(handoff.stage, "_write_new", original_write)
    second = execution.reconcile(setup[0], TransactionRegistry(setup[1].root), confirmation=offer)
    assert second[0]["state"] == "published", second
    assert remnants[0].read_bytes() == b'{"schema":'


def test_policy_fingerprint_tracks_rules_grants_and_authorization(setup):
    scope, _, _ = setup
    initial = execution.policy_revision(scope)
    scope.checker.sandbox.file_grants["synthetic"] = True
    assert execution.policy_revision(scope) != initial
    other = execution.policy_revision(scope)
    scope.checker.rule_engine.append_session_rule(Rule("WriteFile", "*", "deny"))
    assert execution.policy_revision(scope) != other


def test_staging_parent_rejects_nested_workspace(setup):
    scope, registry, _ = setup
    with pytest.raises(RuntimeError, match="disjoint"):
        execution.staging_parent(scope.workspace, registry, configured=scope.workspace / "outputs")


def test_staging_parent_respects_confirmed_store_even_on_default_volume(setup, tmp_path):
    from mewcode.sandbox.volume_stores import configure, describe
    scope, registry, _ = setup
    directory = tmp_path / "selected-store"
    directory.mkdir()
    revision = describe(registry, scope.workspace)["revision"]
    configure(registry, scope.workspace, str(directory), expected_revision=revision)
    assert execution.staging_parent(scope.workspace, registry) == directory


def test_child_exit_zero_is_not_a_publication_receipt():
    assert replace(native(), transaction_id="a", publication_state="verified_unpublished").is_error
    assert not replace(native(), transaction_id="a", publication_state="published").is_error
    pending = replace(native(stdout="OK"), transaction_id="a", publication_state="verified_unpublished")
    assert not pending.confirms_stdout_line("OK")
    assert replace(pending, publication_state="published").confirms_stdout_line("OK")


def test_active_execution_claim_prevents_recovery(setup):
    scope, registry, _ = setup
    record = registry.begin(scope.workspace, task_id=scope.task_id, policy_revision=execution.policy_revision(scope))
    with registry.claim(record["id"]):
        result = execution.reconcile(scope, registry)
        assert result[0]["state"] == "busy"
        assert registry.pending()[0]["state"] == "preparing"
    assert execution.reconcile(scope, registry)[0]["state"] == "rejected"
    assert not registry.pending()


def test_candidate_acl_normalization_preserves_actual_permissions():
    def security(sddl):
        return {"owner_group_dacl": {"state": "captured", "sddl": sddl},
                "sacl": {"state": "unavailable"}}
    before = security("O:SYG:SYD:(A;OICI;FA;;;SY)")
    materialized = security("O:SYG:SYD:AI(A;OICIID;FA;;;SY)")
    assert execution._candidate_security(before) == execution._candidate_security(materialized)
    for sddl in ("O:SYG:SYD:P(A;OICI;FA;;;SY)", "O:SYG:SYD:(A;OICI;FR;;;SY)",
                 "O:SYG:SYD:(D;OICI;FA;;;SY)", "O:SYG:SYD:(A;CI;FA;;;SY)"):
        assert execution._candidate_security(before) != execution._candidate_security(security(sddl))


def test_registry_only_counts_unsettled_records(setup):
    scope, registry, _ = setup
    for _ in range(3):
        record = registry.begin(scope.workspace, task_id=scope.task_id, policy_revision=execution.policy_revision(scope))
        registry.update(record["id"], "rejected")
    assert registry.pending() == []


@pytest.mark.asyncio
async def test_prepublication_crash_releases_registered_lease_on_recovery(setup, monkeypatch):
    monkeypatch.setattr(execution, "metadata_preflight", lambda *args: None)
    fake_transport(monkeypatch, lambda w: (w / "a.txt").write_text("candidate"))
    def crash(*args, **kwargs):
        raise KeyboardInterrupt("process exit simulation before journal")
    monkeypatch.setattr(publication, "publish_workspace", crash)
    with pytest.raises(KeyboardInterrupt):
        await run(setup)
    record = setup[1].pending()[0]
    assert record["state"] == "publishing"
    assert execution.reconcile(setup[0], setup[1])[0]["state"] == "rejected"
    assert not execution._leases(setup[1], setup[0]).snapshot()["leases"]


@pytest.mark.asyncio
async def test_new_top_level_directory_does_not_need_project_subtree_lease(setup, monkeypatch):
    monkeypatch.setattr(execution, "metadata_preflight", lambda *args: None)
    def create(w):
        (w / "new/nested").mkdir(parents=True)
        (w / "new/nested/data.txt").write_text("new")
    fake_transport(monkeypatch, create)
    result = await run(setup)
    assert not result.is_error, result.detail
    record = setup[1].get(result.transaction_id)
    assert set(record["lease_scopes"]) == {"entry_paths"}
    assert len(record["lease_scopes"]["entry_paths"]) == 3
    assert str(setup[0].workspace) not in record["lease_scopes"]["entry_paths"]
    assert (setup[0].workspace / "new/nested/data.txt").read_text() == "new"
