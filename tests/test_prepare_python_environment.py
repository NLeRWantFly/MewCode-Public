import json
import asyncio
import threading

import pytest

from mewcode.permissions import PermissionMode
from mewcode.permissions.policy import build_permission_checker
from mewcode.permissions.rules import Rule
from mewcode.tools.prepare_python_environment import PreparePythonEnvironment, PreparePythonEnvironmentParams


def test_environment_preparation_is_reviewed_and_obeys_write_scope(tmp_path):
    allowed = tmp_path / "outputs"
    allowed.mkdir()
    tool = PreparePythonEnvironment()
    owner = build_permission_checker(tmp_path, mode=PermissionMode.AUTO, writable_scope=[tmp_path])
    assert owner.check(tool, {}).effect == "review"
    child = build_permission_checker(tmp_path, parent_policy=owner, writable_scope=[allowed])
    child.rule_engine.append_session_rule(Rule(tool.name, "*", "allow"))
    decision = child.check(tool, {})
    assert decision.effect == "deny" and decision.source == "child_write_scope"
    assert not (tmp_path / ".venv").exists()


def test_tool_accepts_distribution_names_and_rejects_paths_extras_and_alias_duplicates():
    params = PreparePythonEnvironmentParams(packages=["PyMuPDF", "sample_package.v2"])
    assert params.packages == ["PyMuPDF", "sample_package.v2"]
    with pytest.raises(ValueError):
        PreparePythonEnvironmentParams(packages=["../outside"])
    with pytest.raises(ValueError):
        PreparePythonEnvironmentParams(packages=["helper[extra]"])
    with pytest.raises(ValueError):
        PreparePythonEnvironmentParams(packages=["sample_package", "sample.package"])


@pytest.mark.asyncio
async def test_tool_binds_preparation_to_project_and_does_not_claim_validation(tmp_path, monkeypatch):
    from mewcode.sandbox import project_python
    calls = []
    def prepare(root, *, packages):
        calls.append((root, packages))
        return {"ok": True, "prepared": True, "ready": False, "validation_required": True}
    monkeypatch.setattr(project_python, "prepare_project_python", prepare)
    tool = PreparePythonEnvironment()
    result = await tool.run(PreparePythonEnvironmentParams(packages=["pymupdf"]), work_dir=str(tmp_path))
    assert calls == [(str(tmp_path), ("pymupdf",))]
    assert not result.is_error and json.loads(result.output)["ready"] is False
    assert result.host_observation["project_python_preparation"]["validation_required"]
    missing = await tool.execute(PreparePythonEnvironmentParams())
    assert missing.is_error and len(calls) == 1


@pytest.mark.asyncio
async def test_cancel_waits_for_file_preparation_to_settle(tmp_path, monkeypatch):
    from mewcode.sandbox import project_python
    entered, release = threading.Event(), threading.Event()
    def prepare(root, *, packages):
        entered.set()
        assert release.wait(5)
        return {"ok": True}
    monkeypatch.setattr(project_python, "prepare_project_python", prepare)
    task = asyncio.create_task(PreparePythonEnvironment().run(PreparePythonEnvironmentParams(), work_dir=str(tmp_path)))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(.02)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
