"""Independent offline copies can progress without inventing old cleanup proof."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from mewcode.sandbox.independent_execution import private_cleanup_dependency
from mewcode.sandbox import lpac_runtime as lpac, staged_execution as execution
from mewcode.sandbox.transaction_registry import workspace_command_block
from mewcode.tools import command_sandbox as adapter
from mewcode.tools.bash import Bash, Params
from test_native_readonly_execution import readonly_setup, transport


@pytest.fixture
def deferred(readonly_setup):
    scope, registry, _ = readonly_setup
    row = registry.begin(scope.workspace, task_id=scope.task_id, policy_revision="old")
    private = registry.root / "staging/old"
    candidate = private / "workspace"
    candidate.mkdir(parents=True)
    (private / "cleanup").mkdir()
    envelope = {"schema": "mewcode-lpac-staged-request-v1", "request_id": "old-request",
        "original_workspace": str(scope.workspace), "request": {
            "schema": "mewcode-lpac-network-request-v1", "runtime_network": True,
            "request_id": "old-request", "workspace": str(candidate),
            "read_paths": [str(candidate)], "write_paths": [str(candidate)],
            "arguments": ["-NoProfile", "-Command", "old-unknown-operation"]}}
    def freeze():
        raw = json.dumps(envelope).encode()
        (private / "cleanup/request.bin").write_bytes(raw)
        return registry.update(row["id"], "cleanup_unknown", publication_mode="discard",
            metadata_broker_required=False, transaction=str(private), request_id="old-request",
            request_sha256=hashlib.sha256(raw).hexdigest())
    return freeze(), envelope, freeze


@pytest.mark.asyncio
async def test_new_copy_finishes_without_changing_old_record(readonly_setup, deferred, monkeypatch):
    scope, registry, helper = readonly_setup
    old = deferred[0]
    scope = replace(scope, independent_offline=True)
    transport(monkeypatch, lambda workspace: (workspace / "input.txt").write_text("scratch"))
    result = await execution.execute_staged("new-local-test", scope=scope, registry=registry,
                                            helper=helper, timeout=3)
    assert not result.is_error, result.detail
    assert result.command_started and result.cleanup_confirmed
    assert result.publication_state == "discarded"
    assert registry.get(old["id"]) == old
    assert registry.pending() == [old]
    new = registry.get(result.transaction_id)
    assert new["execution_lane"] == "offline_discard"
    assert new["deferred_cleanup"] == [{key: old[key] for key in ("id", "request_id", "request_sha256")}]
    assert (scope.workspace / "input.txt").read_text() == "source unchanged"
    assert workspace_command_block(scope.workspace)
    assert not workspace_command_block(scope.workspace, independent_command="another-local-test")


@pytest.mark.parametrize("hazard", ["source", "shared_staging", "external_write", "corrupt", "publish", "broker", "replay"])
def test_dependent_or_unbound_work_stays_blocked(readonly_setup, deferred, hazard):
    scope, registry, helper = readonly_setup
    row, envelope, freeze = deferred
    command = "new-local-test"
    if hazard == "source":
        envelope["request"]["read_paths"].append(str(scope.workspace))
    elif hazard == "shared_staging":
        envelope["request"]["read_paths"].append(str(Path(row["transaction"]).parent))
    elif hazard == "external_write":
        envelope["request"]["write_paths"].append(str(helper.parent))
    elif hazard == "publish":
        row["publication_mode"] = "publish"
    elif hazard == "broker":
        row["metadata_broker_required"] = True
    elif hazard == "replay":
        command = "old-unknown-operation; exit $LASTEXITCODE"
    if hazard in {"source", "shared_staging", "external_write"}:
        row = freeze()
    if hazard == "corrupt":
        (Path(row["transaction"]) / "cleanup/request.bin").write_bytes(b"different")
    assert private_cleanup_dependency(row, scope.workspace, command) is None
    with pytest.raises(RuntimeError, match="unsettled"):
        registry.begin(scope.workspace, task_id=scope.task_id, policy_revision="new")


@pytest.mark.asyncio
@pytest.mark.parametrize("override", ["runner", "request_transform"])
async def test_independent_lane_rejects_network_transport(readonly_setup, deferred, override):
    scope, registry, helper = readonly_setup
    with pytest.raises(RuntimeError, match="offline disposable"):
        await execution.execute_staged("new-local-test", scope=replace(scope, independent_offline=True),
            registry=registry, helper=helper, timeout=3, **{override: lambda *args: None})
    assert registry.pending() == [deferred[0]]


@pytest.mark.asyncio
async def test_offline_routes_to_native_and_restores_context(readonly_setup, monkeypatch):
    scope = readonly_setup[0]
    monkeypatch.setattr("mewcode.sandbox.windows.native_runtime_ready", lambda: True)
    monkeypatch.setattr("mewcode.permissions.network_policy.policy_for", lambda _: type("Policy", (), {"enabled": True})())
    native = AsyncMock(return_value=lpac.NativeExecutionReceipt("exited", exit_code=0, cleanup_confirmed=True))
    network = AsyncMock(side_effect=AssertionError("offline command reached network runtime"))
    monkeypatch.setattr(lpac, "execute_lpac", native)
    monkeypatch.setattr("mewcode.sandbox.srt_runtime.execute_srt", network)
    with adapter.native_auto_command(scope.checker):
        result = await Bash().run(Params(command="new-local-test", network="offline", publication="discard"),
                                  work_dir=str(scope.workspace))
    assert not result.is_error
    assert native.await_args.kwargs["scope"].independent_offline is True
    assert native.await_args.kwargs["scope"].publication_mode == "discard"
    assert not adapter._offline_disposable.get()
    assert adapter._native_publication_override.get() is None
    network.assert_not_called()


def test_offline_cannot_publish(readonly_setup):
    with pytest.raises(ValidationError):
        Params(command="new-local-test", network="offline")
    readonly_setup[0].checker.native_publication_mode = "publish"
    with pytest.raises(RuntimeError, match="disposable"):
        lpac.NativeScope.from_checker(readonly_setup[0].checker, independent_offline=True, publication_mode="publish")


def test_permission_check_does_not_reconcile_independent_dependency(readonly_setup, deferred, monkeypatch):
    checker = readonly_setup[0].checker
    monkeypatch.setattr("mewcode.sandbox.windows.native_runtime_ready", lambda: True)
    def forbidden(*args, **kwargs):
        raise AssertionError("independent work must not wait for old recovery")
    monkeypatch.setattr("mewcode.sandbox.native_recovery.recover_unpublished_transactions", forbidden)
    decision = checker.check(Bash(), {"command": "pwd", "publication": "discard", "network": "offline"})
    assert decision.source != "native_transaction_recovery"
    assert readonly_setup[1].get(deferred[0]["id"]) == deferred[0]
