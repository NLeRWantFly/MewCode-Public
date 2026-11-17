import asyncio
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import threading
from types import SimpleNamespace
import venv

import pytest

from mewcode.experts import candidate_verification as cv
from mewcode.experts.audit_workspace import AuditWorkspace, manifest
from mewcode.tools.verify_candidate import VerifyCandidate, VerifyCandidateParams


def project(tmp_path, *, python_env=False):
    source = tmp_path / "project"
    source.mkdir()
    (source / "app.py").write_text("VALUE = 'old'\n", encoding="utf-8")
    (source / "tests").mkdir()
    (source / "tests/__init__.py").touch()
    site = None
    if python_env:
        venv.EnvBuilder(with_pip=False, symlinks=False).create(source / ".venv")
        site = Path(cv.environment_info(source, source)["python_dependencies"][0])
        (site / "sampledep.py").write_text("VALUE = 'detached'\n", encoding="utf-8")
        # Even an editable-install hook must not put original code first.
        (site / "original.pth").write_text("import sys; sys.path.insert(0, " + repr(str(source)) + ")\n", encoding="utf-8")
    (source / "tests/test_app.py").write_text(
        "import unittest, pathlib, app\n"
        "class Case(unittest.TestCase):\n"
        " def test_candidate(self):\n"
        "  self.assertEqual(app.VALUE, 'fixed')\n"
        "  self.assertEqual(pathlib.Path(app.__file__).parent, pathlib.Path.cwd())\n"
        "  print('IMPORTED_APP: ' + app.__file__)\n"
        + ("  import sampledep\n"
           "  self.assertEqual(sampledep.VALUE, 'detached')\n"
           "  self.assertTrue(pathlib.Path(sampledep.__file__).is_relative_to(pathlib.Path.cwd()))\n"
           "  pathlib.Path(sampledep.__file__).write_text('VALUE = 99\\n', encoding='utf-8')\n" if site else ""), encoding="utf-8")
    tx = AuditWorkspace(source, tmp_path / "archives")
    (tx.candidate / "app.py").write_text("VALUE = 'fixed'\n", encoding="utf-8")
    context = {"source": str(source), "candidate": str(tx.candidate), "baseline": str(tx.baseline)}
    return source, tx, context, site


@pytest.mark.asyncio
@pytest.mark.parametrize("python_env", [False, True])
async def test_actual_unittest_imports_candidate_and_dependencies_are_detached(tmp_path, python_env):
    source, tx, context, site = project(tmp_path, python_env=python_env)
    before = manifest(source)
    result = await VerifyCandidate(context).execute(VerifyCandidateParams(runner="unittest", targets=["tests"]))
    assert not result.is_error, result.output
    proof = json.loads(result.output)
    assert proof["exit_code"] == 0 and proof["source_unchanged"]
    assert proof["cwd"] == str(tx.candidate.resolve())
    assert "IMPORTED_APP: " + str(tx.candidate / "app.py") in proof["output"]
    assert "CANDIDATE_IMPORT_ROOT:" in proof["output"] and "OK" in proof["output"]
    assert proof["changed_files"] == ["app.py"]
    assert "-VALUE = 'old'" in proof["diff"] and "+VALUE = 'fixed'" in proof["diff"]
    assert not (tx.candidate / ".git").exists() and not (tx.candidate / ".venv").exists()
    assert manifest(source) == before
    if site:
        assert (site / "sampledep.py").read_text(encoding="utf-8") == "VALUE = 'detached'\n"
        prepared = cv.prepare_environment(source, tx.candidate, "unittest")
        copied = Path(prepared["copied_python_dependencies"][0])
        assert (copied / "sampledep.py").read_text(encoding="utf-8") == "VALUE = 99\n"
        assert not stat.S_ISLNK(copied.lstat().st_mode)


@pytest.mark.asyncio
async def test_actual_pytest_uses_candidate_root_and_copied_runtime(tmp_path):
    source, tx, context, site = project(tmp_path, python_env=True)
    # Small real pytest runtime, no pip/network or copy of the entire host venv.
    for name in ("pytest", "_pytest", "pluggy", "iniconfig", "packaging", "pygments", "colorama", "py"):
        spec = importlib.util.find_spec(name)
        if spec is None:
            continue
        original = Path(spec.origin)
        if spec.submodule_search_locations is None:
            shutil.copy2(original, site / original.name)
        else:
            shutil.copytree(original.parent, site / name, ignore=shutil.ignore_patterns("__pycache__"))
    result = await VerifyCandidate(context).execute(VerifyCandidateParams(runner="pytest", targets=["tests/test_app.py::Case::test_candidate"]))
    assert not result.is_error, result.output
    proof = json.loads(result.output)
    assert proof["exit_code"] == 0 and "1 passed" in proof["output"]
    assert str(tx.candidate.resolve()) in proof["output"]
    assert (source / "app.py").read_text(encoding="utf-8") == "VALUE = 'old'\n"


@pytest.mark.asyncio
async def test_actual_npm_uses_candidate_and_copied_node_modules(tmp_path):
    if not shutil.which("npm.cmd" if os.name == "nt" else "npm"):
        pytest.skip("Node/npm unavailable")
    source = tmp_path / "node-project"
    source.mkdir()
    (source / "package.json").write_text(json.dumps({"scripts": {"test": "node test.cjs"}, "dependencies": {"sampledep": "1.0.0"}}), encoding="utf-8")
    dep = source / "node_modules/sampledep"
    dep.mkdir(parents=True)
    (dep / "index.js").write_text("module.exports = 7;", encoding="utf-8")
    (source / "app.cjs").write_text("module.exports = 0;", encoding="utf-8")
    (source / "test.cjs").write_text(
        "const assert=require('node:assert'),fs=require('node:fs'),path=require('node:path');"
        "assert.equal(require('./app.cjs'),7);assert.equal(require('sampledep'),7);"
        "assert.equal(path.dirname(require.resolve('./app.cjs')),process.cwd());"
        "fs.writeFileSync(require.resolve('sampledep'),'module.exports=8;');"
        "console.log('CANDIDATE_NODE: '+process.cwd());", encoding="utf-8")
    tx = AuditWorkspace(source, tmp_path / "archives")
    (tx.candidate / "app.cjs").write_text("module.exports = 7;", encoding="utf-8")
    result = await cv.verify_candidate({"source": source, "candidate": tx.candidate, "baseline": tx.baseline}, "npm_test", [], 30)
    assert result["exit_code"] == 0, result["output"]
    assert "CANDIDATE_NODE: " + str(tx.candidate) in result["output"]
    assert result["source_unchanged"] and result["changed_files"] == ["app.cjs"]
    assert (dep / "index.js").read_text(encoding="utf-8") == "module.exports = 7;"


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["../baseline/tests", "tests; echo bad", "--rootdir=../baseline", ".mewcode"])
async def test_target_escape_never_starts_process(tmp_path, monkeypatch, target):
    source, tx, context, _ = project(tmp_path)
    (tx.candidate / ".mewcode").mkdir()
    async def forbidden(*args, **kwargs):
        pytest.fail("unsafe target reached process launch")
    monkeypatch.setattr(cv.asyncio, "create_subprocess_exec", forbidden)
    result = await VerifyCandidate(context).execute(VerifyCandidateParams(runner="unittest", targets=[target]))
    assert result.is_error
    assert manifest(source) == manifest(tx.baseline)


@pytest.mark.asyncio
async def test_changed_dependency_and_missing_runner_are_actionable(tmp_path, monkeypatch):
    source, tx, context, _ = project(tmp_path)
    (tx.candidate / "requirements.txt").write_text("new-dependency==1", encoding="utf-8")
    result = await VerifyCandidate(context).execute(VerifyCandidateParams(runner="unittest", targets=["tests"]))
    assert result.is_error and "依赖声明已改变" in result.output
    result = await VerifyCandidate(context).execute(VerifyCandidateParams(runner="pytest", targets=["tests"]))
    assert result.is_error
    monkeypatch.setattr(cv.shutil, "which", lambda *_: None)
    result = await VerifyCandidate(context).execute(VerifyCandidateParams(runner="npm_test"))
    assert result.is_error and "npm 不可用" in result.output


def test_runner_specific_environment_does_not_copy_unrelated_dependencies(tmp_path):
    source, tx, _, _ = project(tmp_path)
    dep = source / "node_modules"
    dep.mkdir()
    (dep / "never-copy").touch()
    (source / "package.json").write_text('{"dependencies":{"a":"1"}}', encoding="utf-8")
    cv.prepare_environment(source, tx.candidate, "unittest")
    assert not (tx.candidate / "node_modules").exists()
    (source / "pyproject.toml").write_text('[project]\nname="first"\nversion="1"', encoding="utf-8")
    (tx.candidate / "pyproject.toml").write_text('[project]\nname="second"\nversion="2"', encoding="utf-8")
    cv.prepare_environment(source, tx.candidate, "unittest")  # Metadata-only edits are not dependency changes.


def test_excluded_runtime_reparse_point_is_rejected_before_write(tmp_path, monkeypatch):
    source, tx, _, _ = project(tmp_path)
    runtime = tx.candidate / ".mewcode"
    runtime.mkdir()
    original = Path.lstat
    def fake_lstat(path):
        if path == runtime:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        return original(path)
    monkeypatch.setattr(Path, "lstat", fake_lstat)
    with pytest.raises(ValueError, match="重解析点"):
        cv.prepare_environment(source, tx.candidate, "unittest")
    assert not (runtime / "verification").exists()


def test_snapshot_path_cannot_escape_with_parent_segments_or_hardlinks(tmp_path):
    source, tx, _, _ = project(tmp_path)
    with pytest.raises(ValueError, match="离开"):
        cv._local_path(tx.candidate, tx.candidate / ".." / "baseline")
    shared = tx.candidate / ".mewcode"
    shared.mkdir()
    path = shared / "shared.py"
    os.link(source / "app.py", path)
    with pytest.raises(ValueError, match="硬链接"):
        cv._local_path(tx.candidate, path)
    assert (source / "app.py").read_text(encoding="utf-8") == "VALUE = 'old'\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["cancel", "timeout"])
async def test_copy_is_nonblocking_and_cleanup_finishes_before_return(tmp_path, monkeypatch, operation):
    source, tx, _, _ = project(tmp_path)
    started, stopped = threading.Event(), threading.Event()
    def slow_prepare(*args, cancel, **kwargs):
        started.set()
        cancel.wait(3)
        assert cancel.is_set()
        stopped.set()
        raise InterruptedError("copy cancelled")
    monkeypatch.setattr(cv, "prepare_environment", slow_prepare)
    task = asyncio.create_task(cv._prepare_environment(source, tx.candidate, "unittest", .1 if operation == "timeout" else 5))
    while not started.is_set():
        await asyncio.sleep(.005)
    assert not task.done()  # The event loop ran while the copy thread was waiting.
    if operation == "cancel":
        task.cancel()
    with pytest.raises(asyncio.CancelledError if operation == "cancel" else asyncio.TimeoutError):
        await asyncio.wait_for(task, 2)
    assert stopped.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["cancel", "timeout"])
async def test_process_tree_is_awaited_on_timeout_or_cancellation(tmp_path, monkeypatch, operation):
    source, tx, context, _ = project(tmp_path)
    started, stopped = asyncio.Event(), asyncio.Event()
    class Process:
        async def communicate(self):
            started.set()
            if operation == "timeout":
                # Inject the process timeout after launch. An end-to-end 200 ms
                # deadline can expire during disk preparation on a busy host.
                raise asyncio.TimeoutError()
            await asyncio.Event().wait()
    async def launch(*args, **kwargs):
        assert kwargs["cwd"] == tx.candidate.resolve()
        assert "PYTHONPATH" not in kwargs["env"]
        return Process()
    async def terminate(proc):
        await asyncio.sleep(0)
        stopped.set()
    monkeypatch.setattr(cv.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(cv, "_terminate_process_tree", terminate)
    task = asyncio.create_task(cv.verify_candidate(context, "unittest", ["tests"], 30))
    try:
        await asyncio.wait_for(started.wait(), 10)
        if operation == "cancel":
            task.cancel()
        with pytest.raises(asyncio.CancelledError if operation == "cancel" else asyncio.TimeoutError):
            await asyncio.wait_for(task, 5)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert stopped.is_set()


def test_verify_candidate_keeps_command_approval_and_role_boundaries(tmp_path):
    from mewcode.expert_group import derive_execution_policy
    from mewcode.expert_web import _role_registry
    from mewcode.permissions.checker import PermissionChecker
    from mewcode.permissions.dangerous import DangerousCommandDetector
    from mewcode.permissions.modes import PermissionMode
    from mewcode.permissions.rules import RuleEngine
    from mewcode.permissions.sandbox import PathSandbox
    from mewcode.tools import ToolRegistry
    context = {"source": "original", "candidate": "candidate", "baseline": "baseline"}
    tool = VerifyCandidate(context)
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(tmp_path), RuleEngine(), PermissionMode.AUTO)
    assert checker.check(tool, {"runner": "pytest", "targets": ["tests"]}).effect == "review"
    checker.mode = PermissionMode.PLAN
    assert checker.check(tool, {"runner": "pytest", "targets": ["tests"]}).effect == "deny"
    registry = ToolRegistry()
    session = SimpleNamespace(agent=SimpleNamespace(registry=registry), work_dir=str(tmp_path))
    policy = replace(derive_execution_policy(False), work_dir=str(tmp_path), candidate_context=context)
    assert _role_registry(session, "executor", policy).get("VerifyCandidate") is not None
    for role in ("auditor", "manager", "planner"):
        assert _role_registry(session, role, policy).get("VerifyCandidate") is None
    for scoped in (replace(policy, node_tools=()), replace(policy, deny_tools=("VerifyCandidate",)),
                   replace(policy, parallel_readonly=True), replace(policy, audit_contract={"artifact_dir": str(tmp_path)}),
                   replace(policy, loop_context={"reviewer": True})):
        assert _role_registry(session, "executor", scoped).get("VerifyCandidate") is None
