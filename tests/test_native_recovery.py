"""Synthetic gate fixtures test policy boundaries; these are not live attestations."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from mewcode.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine
from mewcode.sandbox import native_gate as gate, native_recovery as recovery
from mewcode.sandbox.lpac_runtime import decode_receipt
from mewcode.tools.bash import Bash, Params as BashParams
from mewcode.tools.runtime_doctor import RuntimeDoctor, RuntimeDoctorParams
from scripts import manage_native_runtime as installer
from tests.test_native_gate import MACHINE, _attestation, _enablement, _install_bundle
from tests.test_native_readonly_execution import readonly_setup  # noqa: F401


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("MEWCODE_USER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(gate, "machine_guid", lambda: MACHINE)
    root = _install_bundle(tmp_path)
    _attestation(root)
    _enablement(root)
    return root


def authorized(bundle):
    assert recovery.authorize_recovery(bundle)["ready"]
    (bundle / gate.ENABLEMENT_NAME).unlink()


def test_authorized_recovery_restores_once_and_revalidates_full_gate(bundle):
    authorized(bundle)
    assert not recovery.diagnose_native_runtime()["ready"]
    assert not (bundle / gate.ENABLEMENT_NAME).exists()  # Inspection never repairs.
    result = recovery.recover_native_runtime()
    assert result["ready"] and result["action"] == recovery.ACTION
    assert result["command_started"] is False
    assert gate.native_gate_status(bundle).ready
    before = (bundle / gate.ENABLEMENT_NAME).read_bytes()
    assert recovery.recover_native_runtime()["action"] == "already_ready"
    assert recovery.recover_native_runtime()["changed"] is False
    assert (bundle / gate.ENABLEMENT_NAME).read_bytes() == before


@pytest.mark.parametrize("blocker", ["no_policy", "bundle_drift", "report_drift", "wrong_machine",
    "wrong_bundle", "invalid_policy", "duplicate_field", "missing_attestation", "explicit_disable"])
def test_recovery_never_creates_missing_evidence_or_overrides_host_disable(bundle, monkeypatch, blocker):
    authorized(bundle)
    policy = bundle / recovery.POLICY_NAME
    if blocker == "no_policy":
        policy.unlink()
    elif blocker == "bundle_drift":
        (bundle / "git/git.exe").write_bytes(b"drift")
    elif blocker == "report_drift":
        (bundle.parents[1] / "acceptance.json").write_text("{}")
    elif blocker == "wrong_machine":
        monkeypatch.setattr(gate, "machine_guid", lambda: "other")
    elif blocker == "missing_attestation":
        (bundle / gate.ATTESTATION_NAME).unlink()
    elif blocker == "explicit_disable":
        installer.disable_gate(bundle.parents[1])
        assert not policy.exists()
    else:
        record = json.loads(policy.read_text())
        if blocker == "wrong_bundle":
            record["bundle_identity"] = "0" * 64
        elif blocker == "invalid_policy":
            record["allowed_actions"] += ["install_and_attest"]
        content = json.dumps(record)
        if blocker == "duplicate_field":
            content = content[:-1] + ', "schema": "' + recovery.POLICY_SCHEMA + '"}'
        policy.write_text(content)
    result = recovery.recover_native_runtime()
    assert not result["ready"] and result["action"] == "blocked"
    assert not (bundle / gate.ENABLEMENT_NAME).exists()


def test_opt_in_requires_previous_explicit_enablement(bundle):
    (bundle / gate.ENABLEMENT_NAME).unlink()
    with pytest.raises(ValueError, match="already enabled"):
        recovery.authorize_recovery(bundle)
    assert not (bundle / recovery.POLICY_NAME).exists()


def test_recovery_does_not_override_an_active_host_control_operation(bundle):
    authorized(bundle)
    with recovery.host_gate_lock(bundle):
        result = recovery.recover_native_runtime(bundle)
    assert not result["ready"] and result["error_code"] == "native_recovery_blocked"
    assert not (bundle / gate.ENABLEMENT_NAME).exists()


@pytest.mark.asyncio
async def test_doctor_recovery_is_host_bound_and_plan_mode_remains_read_only(bundle):
    authorized(bundle)
    doctor = RuntimeDoctor()
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(bundle.parents[1])), RuleEngine(), PermissionMode.PLAN)
    assert checker.check(doctor, {"action": "inspect"}).effect == "allow"
    assert checker.check(doctor, {"action": "recover"}).effect == "deny"
    checker.mode = PermissionMode.AUTO
    assert checker.check(doctor, {"action": "recover"}).effect == "allow"
    with pytest.raises(ValidationError):
        RuntimeDoctorParams(action="recover", bundle_root="other", model="fake")
    result = await doctor.execute(RuntimeDoctorParams(action="recover"))
    assert not result.is_error
    assert result.host_observation["runtime_recovery"]["action"] == recovery.ACTION


@pytest.mark.asyncio
async def test_discard_unavailable_backend_never_silently_runs_on_original(tmp_path, monkeypatch):
    forbidden = Mock(side_effect=AssertionError("no host shell may start"))
    monkeypatch.setattr("asyncio.create_subprocess_exec", forbidden)
    result = await Bash().run(BashParams(command="synthetic", publication="discard"), work_dir=str(tmp_path))
    assert result.is_error and result.host_observation["command_execution"]["command_started"] is False
    forbidden.assert_not_called()


def test_missing_receipt_cannot_authorize_replay():
    result = decode_receipt(b"", "a" * 32, 0)
    assert result.is_error and result.execution_started is None
    assert result.error_code == "native_receipt_unverified"


@pytest.mark.asyncio
async def test_helper_create_process_failure_is_known_not_started(monkeypatch):
    from mewcode.sandbox import lpac_runtime as lpac
    monkeypatch.setattr(lpac, "encode_request", lambda _: b"synthetic")

    async def failed(*args, **kwargs):
        raise OSError("synthetic helper creation failure")

    monkeypatch.setattr("asyncio.create_subprocess_exec", failed)
    result = await lpac.invoke_helper(None, {}, None)
    assert result.execution_started is False and result.cleanup_confirmed is True
    assert result.error_code == "native_helper_start_failed"


@pytest.mark.asyncio
async def test_native_adapter_repairs_before_execution_admission(bundle, monkeypatch):
    from mewcode.tools.command_sandbox import execute_native_command, native_auto_command
    from mewcode.sandbox import lpac_runtime as lpac
    from mewcode.sandbox.receipt import NativeExecutionReceipt
    authorized(bundle)
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(bundle.parents[1])), RuleEngine(), PermissionMode.AUTO)
    checker.task_id = "repair-test"
    from mewcode.permissions.network_policy import NetworkPolicy
    checker.network_policy = NetworkPolicy(False)  # Exercise the original offline runtime repair.
    called = []

    async def command(*args, **kwargs):
        assert gate.native_gate_status().ready
        called.append(kwargs["scope"])
        return NativeExecutionReceipt("exited", exit_code=0, cleanup_confirmed=True)

    monkeypatch.setattr(lpac, "execute_lpac", command)
    with native_auto_command(checker):
        result = await execute_native_command("pwd", work_dir=str(bundle.parents[1]), timeout=3)
    assert not result.is_error and len(called) == 1
    assert called[0].publication_mode == "discard"


@pytest.mark.parametrize("state", ["preparing", "cleanup_unknown", "different_task", "busy"])
def test_permission_guard_settles_proven_unpublished_transactions_across_tasks(tmp_path, monkeypatch, state):
    from mewcode.sandbox import staged_execution as execution, lpac_runtime as lpac
    from mewcode.sandbox.transaction_registry import TransactionRegistry, workspace_write_block
    source, private = tmp_path / "source", tmp_path / "native"
    source.mkdir()
    private.mkdir()
    monkeypatch.setattr("mewcode.sandbox.transaction_registry.default_root", lambda: private)
    monkeypatch.setattr("mewcode.sandbox.windows.native_runtime_ready", lambda: True)
    monkeypatch.setattr("mewcode.sandbox.metadata_broker.transaction_scope",
                        Mock(side_effect=AssertionError("automatic reconciliation cannot request elevation")))
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(source)), RuleEngine(), PermissionMode.AUTO)
    checker.task_id = "original-host-task"
    from mewcode.permissions.network_policy import NetworkPolicy
    checker.network_policy = NetworkPolicy(False)
    scope = lpac.NativeScope.from_checker(checker, publication_mode="discard")
    registry = TransactionRegistry(private)
    record = registry.begin(source, task_id=scope.task_id, policy_revision=execution.policy_revision(scope))
    registry.update(record["id"], "cleanup_unknown" if state == "cleanup_unknown" else "preparing",
                    publication_mode="discard")
    assert workspace_write_block(source)
    if state == "different_task":
        checker.task_id = "other-task"
    if state == "busy":
        with registry.claim(record["id"]):
            decision = checker.check(Bash(), {"command": "pwd"})
    else:
        decision = checker.check(Bash(), {"command": "pwd"})
    assert decision.effect == ("allow" if state in {"preparing", "different_task"} else "deny")
    assert bool(workspace_write_block(source)) == (state not in {"preparing", "different_task"})


@pytest.mark.parametrize("condition", ["verified", "no_cleanup", "different_task", "unbound", "pending"])
def test_agent_cleanup_fence_requires_settled_host_proof_not_old_task_grants(tmp_path, monkeypatch, condition):
    from mewcode.agent import Agent
    from mewcode.sandbox import staged_execution as execution, lpac_runtime as lpac
    from mewcode.sandbox.receipt import NativeCleanupUnconfirmed
    from mewcode.sandbox.transaction_registry import TransactionRegistry
    source, private = tmp_path / "source", tmp_path / "native"
    source.mkdir()
    private.mkdir()
    monkeypatch.setattr("mewcode.sandbox.transaction_registry.default_root", lambda: private)
    monkeypatch.setattr("mewcode.sandbox.windows.native_runtime_ready", lambda: True)
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(source)), RuleEngine(), PermissionMode.AUTO)
    checker.task_id = "cleanup-owner-task"
    scope = lpac.NativeScope.from_checker(checker)
    registry = TransactionRegistry(private)
    record = registry.begin(source, task_id=scope.task_id, policy_revision=execution.policy_revision(scope))
    registry.update(record["id"], "cleanup_unknown" if condition == "pending" else "rejected",
        native_receipt={"cleanup_confirmed": condition != "no_cleanup"})
    owner = object.__new__(Agent)
    owner.permission_checker = checker
    owner._native_cleanup_error = "previous helper cleanup unknown"
    owner._native_cleanup_transaction_ids = {record["id"]}
    owner._native_cleanup_unbound = condition == "unbound"
    if condition == "different_task":
        checker.task_id = "new-task"
    if condition in {"verified", "different_task"}:
        owner._assert_native_cleanup_settled()
        assert not owner._native_cleanup_error
    else:
        with pytest.raises(NativeCleanupUnconfirmed):
            owner._assert_native_cleanup_settled()
        assert owner._native_cleanup_error


def test_cleanup_observer_records_active_host_transaction_without_weakening_unbound_error(tmp_path):
    from mewcode.tools import command_sandbox
    from mewcode.sandbox.transaction_registry import CURRENT_TRANSACTION
    from mewcode.sandbox.receipt import NativeCleanupUnconfirmed, NativeExecutionReceipt
    owner = SimpleNamespace(_native_cleanup_error="")
    with pytest.raises(NativeCleanupUnconfirmed):
        with command_sandbox.native_auto_command(owner=owner):
            observe = command_sandbox._native_receipt_observer.get()
            observe(NativeExecutionReceipt("spawn_failed", cleanup_confirmed=False))
            token = CURRENT_TRANSACTION.set("a" * 32)
            try:
                observe(NativeExecutionReceipt("spawn_failed", cleanup_confirmed=False))
            finally:
                CURRENT_TRANSACTION.reset(token)
    assert owner._native_cleanup_transaction_ids == {"a" * 32}
    assert owner._native_cleanup_unbound


@pytest.mark.asyncio
async def test_late_bound_cleanup_proof_recovers_agent_without_replaying_command(readonly_setup, monkeypatch):
    from mewcode.agent import Agent
    from mewcode.tools import command_sandbox
    from mewcode.sandbox import staged_execution as execution, lpac_runtime as lpac
    from mewcode.sandbox.receipt import NativeCleanupUnconfirmed, NativeExecutionReceipt
    scope, registry, helper = readonly_setup
    monkeypatch.setattr("mewcode.sandbox.windows.native_runtime_ready", lambda: True)
    owner = object.__new__(Agent)
    owner.permission_checker = scope.checker
    owner._native_cleanup_error = ""
    launches = []

    async def lost_cleanup(helper, request, cancel, on_receipt=None):
        launches.append(request["request_id"])
        result = NativeExecutionReceipt("exited", backend=lpac.BACKEND, exit_code=0, cleanup_confirmed=False)
        on_receipt(result)
        return result

    monkeypatch.setattr(lpac, "invoke_helper", lost_cleanup)
    with command_sandbox.native_auto_command(scope.checker, owner=owner):
        result = await execution.execute_staged("synthetic", scope=scope, helper=helper, registry=registry,
            timeout=3, on_receipt=command_sandbox._native_receipt_observer.get())
    assert result.is_error
    owner._assert_native_cleanup_settled()  # Isolated cleanup must not abort the whole conversation.
    assert owner._native_cleanup_error
    with pytest.raises(NativeCleanupUnconfirmed):
        with command_sandbox.native_auto_command(scope.checker, owner=owner):
            pytest.fail("A second command must remain blocked")
    record = registry.pending()[0]
    assert owner._native_cleanup_transaction_ids == {record["id"]}
    proof = {"schema": "mewcode-native-cleanup-v1", "request_id": record["request_id"],
        "request_sha256": record["request_sha256"], "backend": lpac.BACKEND,
        "completed": True, "job_cleanup_ok": True, "acl_cleanup_ok": True, "profile_cleanup_ok": True}
    from pathlib import Path
    (Path(record["transaction"]) / "cleanup/receipt.json").write_text(json.dumps(proof), encoding="utf-8")
    scope.checker.task_id = "new-task"
    owner._assert_native_cleanup_settled()
    assert not owner._native_cleanup_error and not registry.pending()
    assert len(launches) == 1
