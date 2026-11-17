from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import native_acceptance_executor as acceptance
from scripts import probe_lpac_production as probe
from mewcode.sandbox.lpac_runtime import NativeExecutionReceipt


def test_diagnostic_paths_cannot_select_production_or_project_root(tmp_path, monkeypatch):
    (tmp_path / ".tmp").mkdir()
    monkeypatch.setattr(acceptance, "ROOT", tmp_path)
    for path in (tmp_path, tmp_path / ".tmp", tmp_path.parent / "production", tmp_path / ".tmp/../other"):
        with pytest.raises(ValueError):
            acceptance.bounded_directory(path, must_exist=False)
    assert acceptance.bounded_directory(tmp_path / ".tmp/new", must_exist=False) == tmp_path / ".tmp/new"


def test_diagnostic_private_state_cannot_overlap_workspace(tmp_path, monkeypatch):
    root = tmp_path / ".tmp" / "data"
    root.mkdir(parents=True)
    monkeypatch.setattr(acceptance, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="disjoint"):
        acceptance.AcceptanceExecutor(root, (root / "fixture",))


def test_external_fixture_requires_the_fixed_synthetic_namespace(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    (checkout / ".tmp").mkdir(parents=True)
    monkeypatch.setattr(acceptance, "ROOT", checkout)
    external = tmp_path / "external"
    external.mkdir()
    monkeypatch.setattr(acceptance, "EXTERNAL_FIXTURE_ROOT", external)
    assert acceptance.bounded_fixture(external / "case-202609270001") == external / "case-202609270001"
    for bad in (external, external / "business", external / "nested/case-202609270001",
                tmp_path / "case-202609270001"):
        with pytest.raises(ValueError):
            acceptance.bounded_fixture(bad)


@pytest.mark.asyncio
async def test_diagnostic_refuses_shared_runtime_acl_changes(tmp_path):
    fixture = tmp_path / "fixture"
    workspace = fixture / "中文 workspace 空格"
    workspace.mkdir(parents=True)
    shared = tmp_path / "shared-python"
    shared.mkdir()
    executor = object.__new__(acceptance.AcceptanceExecutor)
    executor.fixtures = (fixture,)
    with pytest.raises(ValueError, match="copied Python runtime"):
        await executor("test", scope=SimpleNamespace(workspace=workspace),
                       work_dir=str(workspace), timeout=1, extra_read=(shared,))


@pytest.mark.asyncio
async def test_cancel_waits_for_durable_start_then_requires_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.lpac.NativeScope, "from_checker", lambda c: object())
    observations = iter((True, False))
    monkeypatch.setattr(probe, "_process_alive", lambda pid: next(observations))
    cancelled = []
    async def runner(command, **kwargs):
        (tmp_path / "cancel-child.pid").write_text("42", encoding="ascii")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            kwargs["on_receipt"](NativeExecutionReceipt("cancelled", backend=probe.lpac.BACKEND,
                                                        cleanup_confirmed=True))
            raise
    record = await probe.run_cancel_case(None, tmp_path, (), executor=runner, start_timeout=1)
    assert cancelled == [True]
    assert record["ok"] and record["child_reaped"]


@pytest.mark.asyncio
async def test_cancel_without_started_child_does_not_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.lpac.NativeScope, "from_checker", lambda c: object())
    async def runner(command, **kwargs):
        return NativeExecutionReceipt("spawn_failed", cleanup_confirmed=True)
    record = await probe.run_cancel_case(None, tmp_path, (), executor=runner, start_timeout=.1)
    assert not record["ok"]
    assert record["cleanup_confirmed"] is False
    assert any("startup" in failure for failure in record["failures"])


@pytest.mark.asyncio
async def test_cancel_unconfirmed_receipt_never_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.lpac.NativeScope, "from_checker", lambda c: object())
    observations = iter((True, False))
    monkeypatch.setattr(probe, "_process_alive", lambda pid: next(observations))
    async def runner(command, **kwargs):
        (tmp_path / "cancel-child.pid").write_text("42", encoding="ascii")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            kwargs["on_receipt"](NativeExecutionReceipt("cancelled", cleanup_confirmed=False))
            raise
    record = await probe.run_cancel_case(None, tmp_path, (), executor=runner, start_timeout=1)
    assert not record["ok"]
