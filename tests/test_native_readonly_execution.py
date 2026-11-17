"""Read-only native observations must never enter privileged publication."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from mewcode.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine
from mewcode.sandbox import lpac_runtime as lpac
from mewcode.sandbox import staged_execution as execution
from mewcode.sandbox.receipt import NativeExecutionReceipt
from mewcode.sandbox.transaction_registry import TransactionRegistry, workspace_write_block, workspace_command_block


@pytest.fixture
def readonly_setup(tmp_path, monkeypatch):
    source, private = tmp_path / "source", tmp_path / "private"
    source.mkdir()
    private.mkdir()
    (source / "input.txt").write_text("source unchanged")
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(source)), RuleEngine(), PermissionMode.AUTO)
    checker.task_id = "host-readonly"
    checker.native_publication_mode = "discard"
    scope = lpac.NativeScope.from_checker(checker)
    registry = TransactionRegistry(private)
    helper = private / "bundle/runtime.exe"
    helper.parent.mkdir()
    (helper.parent / "git").mkdir()
    (helper.parent / "git/git.exe").write_bytes(b"synthetic")
    monkeypatch.setattr("mewcode.sandbox.transaction_registry.default_root", lambda: private)
    monkeypatch.setattr(lpac, "HostCancelEvent", Mock(return_value=Mock()))
    monkeypatch.setattr(lpac, "build_runtime_request", lambda command, mapped, runtime, **kwargs:
        {"request_id": "b" * 32, "timeout_ms": 3000, "workspace": str(mapped.workspace)})
    monkeypatch.setattr("mewcode.sandbox.metadata_broker.installed_artifact", lambda _: (helper, "a" * 64))
    forbidden = Mock(side_effect=AssertionError("readonly execution must not invoke metadata or publication"))
    monkeypatch.setattr("mewcode.sandbox.metadata_broker.transaction_scope", forbidden)
    monkeypatch.setattr(execution, "metadata_inventory", forbidden)
    monkeypatch.setattr(execution.publication, "publish_workspace", forbidden)
    monkeypatch.setattr(execution, "_leases", forbidden)
    return scope, registry, helper


def transport(monkeypatch, action=None, **fields):
    async def invoke(helper, envelope, cancel, on_receipt=None):
        if action:
            action(Path(envelope["request"]["workspace"]))
        receipt = NativeExecutionReceipt("exited", exit_code=0, backend=lpac.BACKEND,
            cleanup_confirmed=True, stdout="host stdout", **fields)
        on_receipt(receipt)
        return receipt
    monkeypatch.setattr(lpac, "invoke_helper", invoke)


async def run(setup):
    scope, registry, helper = setup
    return await execution.execute_staged("synthetic", scope=scope, registry=registry, helper=helper, timeout=3)


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [False, True])
async def test_discard_never_elevates_or_publishes_even_when_child_writes(readonly_setup, monkeypatch, changes):
    def change(workspace):
        (workspace / "input.txt").write_text("private replacement")
        (workspace / "temporary.txt").write_text("private scratch")
    transport(monkeypatch, change if changes else None)
    result = await run(readonly_setup)
    assert not result.is_error, result.detail
    assert result.publication_state == "discarded"
    assert result.stdout == "host stdout" and result.exit_code == 0
    assert (readonly_setup[0].workspace / "input.txt").read_text() == "source unchanged"
    assert not (readonly_setup[0].workspace / "temporary.txt").exists()
    assert not readonly_setup[1].pending()


@pytest.mark.asyncio
async def test_discard_still_revalidates_source_and_preserves_actual_execution(readonly_setup, monkeypatch):
    transport(monkeypatch, lambda _: (readonly_setup[0].workspace / "input.txt").write_text("external drift"))
    result = await run(readonly_setup)
    assert result.is_error and "Original workspace changed" in result.detail
    assert result.command_started is True and result.exit_code == 0
    assert result.stdout == "host stdout"
    assert not readonly_setup[1].pending()


@pytest.mark.asyncio
async def test_discard_transport_loss_fences_instead_of_repeating(readonly_setup, monkeypatch):
    async def lost(*args, **kwargs):
        raise RuntimeError("lost transport")
    monkeypatch.setattr(lpac, "invoke_helper", lost)
    result = await run(readonly_setup)
    assert result.is_error and result.command_started is None
    assert result.exit_code is None
    assert not workspace_write_block(readonly_setup[0].workspace)
    assert workspace_command_block(readonly_setup[0].workspace)


@pytest.mark.asyncio
async def test_unknown_private_cleanup_allows_edits_but_not_commands(readonly_setup, monkeypatch):
    from mewcode.tools.bash import Bash
    from mewcode.tools.write_file import WriteFile, Params
    from mewcode.sandbox.transaction_registry import private_candidate_only
    async def lost(*args, **kwargs):
        raise RuntimeError("lost transport")
    monkeypatch.setattr(lpac, "invoke_helper", lost)
    await run(readonly_setup)
    scope, registry, _ = readonly_setup
    record = registry.pending()[0]
    assert private_candidate_only(record)
    path = scope.workspace / "input.txt"
    args = {"file_path": str(path), "content": "continued editing"}
    for mode in (PermissionMode.AUTO, PermissionMode.BYPASS):
        scope.checker.mode = mode
        assert scope.checker.check(WriteFile(), args).effect == "allow"
        decision = scope.checker.check(Bash(), {"command": "pwd"})
        assert decision.effect == "deny" and decision.subcondition == "unsettled_command"
    result = await WriteFile().run(Params(**args), work_dir=str(scope.workspace))
    assert not result.is_error and path.read_text() == "continued editing"
    assert registry.pending()[0]["state"] == "cleanup_unknown"
    assert registry.pending()[0].get("native_receipt") is None
    from mewcode.tools.runtime_doctor import RuntimeDoctor, RuntimeDoctorParams
    monkeypatch.setattr("mewcode.sandbox.native_recovery.diagnose_native_runtime", lambda: {"ready": True})
    monkeypatch.setattr("mewcode.sandbox.project_python.inspect_project_python", lambda _: {})
    report = await RuntimeDoctor().run(RuntimeDoctorParams(), work_dir=str(scope.workspace))
    facts = report.host_observation["runtime_recovery"]
    assert facts["workspace_ready"] is True and facts["commands_ready"] is False
    assert "可以继续" in facts["command_blocker"]
    item = facts["workspace_transactions"]["items"][0]
    assert item["id"] == record["id"] and item["state"] == "cleanup_unknown"
    assert item["diagnostic_code"] == "cleanup_evidence_missing"
    # Neither metadata-capable work nor any source publication can use this exception.
    assert not private_candidate_only({**record, "metadata_broker_required": True})
    assert not private_candidate_only({**record, "publication_mode": "publish"})
    assert not private_candidate_only({**record, "state": "publishing"})
    assert not private_candidate_only({**record, "transaction": str(scope.workspace)})
    (Path(record["transaction"]) / "publication").mkdir()
    assert workspace_write_block(scope.workspace)


@pytest.mark.asyncio
async def test_network_prelaunch_failure_does_not_leave_a_workspace_fence(readonly_setup, monkeypatch):
    from mewcode.sandbox import srt_runtime
    from mewcode.sandbox.execution_conditions import classify_execution_condition
    def unavailable():
        raise RuntimeError("network runtime is unavailable before command launch")
    monkeypatch.setattr(srt_runtime, "runtime_paths", unavailable)
    scope, registry, helper = readonly_setup
    result = await execution.execute_staged("synthetic", scope=scope, registry=registry, helper=helper, timeout=3,
        runner=lambda envelope, cancel, observed: srt_runtime.invoke_srt(envelope,
            checker=scope.checker, cancel=cancel, on_receipt=observed))
    assert result.is_error and result.command_started is False and result.cleanup_confirmed is True
    assert result.publication_state == "rejected"
    assert not registry.pending() and not workspace_write_block(scope.workspace)
    assert not workspace_command_block(scope.workspace)
    assert classify_execution_condition(result)["kind"] == "environment_unavailable"


def test_host_forced_discard_cannot_be_upgraded_by_tool_request(readonly_setup):
    scope = readonly_setup[0]
    assert scope.publication_mode == "discard"
    with pytest.raises(RuntimeError, match="publication"):
        lpac.NativeScope.from_checker(scope.checker, publication_mode="publish")
    scope.checker.native_publication_mode = "invalid"
    assert not scope.current()


def test_policy_revision_binds_publication_mode(readonly_setup):
    scope = readonly_setup[0]
    published = replace(scope, publication_mode="publish")
    assert execution.policy_revision(scope) != execution.policy_revision(published)
