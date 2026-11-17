from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from mewcode.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine
from mewcode.sandbox import lpac_runtime as lpac
from mewcode.sandbox import staged_execution as execution
from mewcode.sandbox import staged_workspace as stage
from mewcode.sandbox.python_runtime import (
    _parse_cfg,
    discover_python_runtime,
    literal_interpreter,
    merge_extra_read,
)
from mewcode.sandbox.receipt import NativeExecutionReceipt
from mewcode.sandbox.transaction_registry import CURRENT_TRANSACTION, TransactionRegistry
from mewcode.sandbox.windows import NativeBoundaryUnavailable


def _scope(workspace: Path, *, allowed=()):
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(workspace)),
                               RuleEngine(), PermissionMode.AUTO)
    checker.sandbox._allowed_roots = [workspace]
    checker.task_id = "python-runtime-test"
    for path in allowed:
        checker.sandbox.grant_directory(str(path), writable=False)
    return lpac.NativeScope.from_checker(checker)


def _python_tree(root: Path):
    (root / "Lib" / "encodings").mkdir(parents=True)
    (root / "python.exe").write_bytes(b"python-runtime")
    (root / "python312.dll").write_bytes(b"python-dll")
    (root / "Lib" / "encodings" / "__init__.py").write_bytes(b"# encodings\n")


def _snapshot_config(workspace: Path, staged_workspace: Path, home: Path, *, metadata="", venv=".venv"):
    cfg = workspace / venv / "pyvenv.cfg"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    content = (f"home = {home}\ninclude-system-site-packages = false\n"
               "version = 3.12.0\n" + metadata).encode("utf-8")
    cfg.write_bytes(content)
    staged_cfg = staged_workspace / venv / "pyvenv.cfg"
    staged_cfg.parent.mkdir(parents=True, exist_ok=True)
    staged_cfg.write_bytes(content)
    info = cfg.stat()
    entry = stage.Entry(Path(venv, "pyvenv.cfg").as_posix(), "file", len(content), hashlib.sha256(content).hexdigest(),
                        info.st_dev, info.st_ino, False)
    return cfg, staged_cfg, (entry,)


@pytest.mark.parametrize("command,expected", [
    (r"& '.venv\Scripts\python.exe' -m unittest", r".venv\Scripts\python.exe"),
    (r'& ".\venv\Scripts\pythonw.exe" script.py', r".\venv\Scripts\pythonw.exe"),
    (r".venv/Scripts/python.exe -c pass", r".venv/Scripts/python.exe"),
    (r"./.venv/Scripts/python.exe -m unittest", r"./.venv/Scripts/python.exe"),
    (r"& venv\Scripts\python.exe script.py", r"venv\Scripts\python.exe"),
    (r"& '.venv\Scripts\python.exe'; Write-Output done", r".venv\Scripts\python.exe"),
    (r"& 'outputs\candidate\.venv\Scripts\python.exe' -B -m unittest", r"outputs\candidate\.venv\Scripts\python.exe"),
    (r'& "./候选 输出/venv/Scripts/python.exe" --version', r"./候选 输出/venv/Scripts/python.exe"),
    (r"outputs/candidate/.venv/Scripts/python.exe -B -c pass", r"outputs/candidate/.venv/Scripts/python.exe"),
])
def test_literal_workspace_venv_command_prefix(command, expected):
    assert literal_interpreter(command) == expected


@pytest.mark.parametrize("command", [
    r"'.venv\Scripts\python.exe' -m unittest",  # A string expression is not invocation.
    r"& '.venv\Scripts\python.exe'junk script.py",
    r"& '$env:VIRTUAL_ENV\Scripts\python.exe' script.py",
    r"& \"$(Get-Location)\.venv\Scripts\python.exe\" script.py",
    r"& (Join-Path '.venv' 'Scripts/python.exe') script.py",
    r"python -m unittest",
    r"Write-Output '.venv\Scripts\python.exe'",
    r"& '..\outside\Scripts\python.exe'",
    r"& '..\outside\.venv\Scripts\python.exe' --version",
    r"& 'outputs\..\outside\.venv\Scripts\python.exe' --version",
    r"& 'outputs\.\candidate\.venv\Scripts\python.exe' --version",
    r"& 'C:\candidate\.venv\Scripts\python.exe' --version",
    r"& '\\server\candidate\.venv\Scripts\python.exe' --version",
    r"& '.codex\candidate\.venv\Scripts\python.exe' --version",
    r"& 'outputs\.agents\.venv\Scripts\python.exe' --version",
    r"& 'outputs\.mewcode\.venv\Scripts\python.exe' --version",
    r"& 'outputs\candidate.\.venv\Scripts\python.exe' --version",
    r"& 'outputs\candidate:stream\.venv\Scripts\python.exe' --version",
    r"& outputs\candidate dir\.venv\Scripts\python.exe --version",
])
def test_dynamic_or_nonleading_python_forms_are_not_derived(command):
    assert literal_interpreter(command) is None


@pytest.mark.parametrize("command", ["python --version", "python.exe -m unittest", "python3 -c pass"])
def test_python_alias_selects_only_the_frozen_project_environment(tmp_path, command):
    source, frozen, home = (tmp_path / name for name in ("source", "frozen", "base-python"))
    for path in (source, frozen, home):
        path.mkdir()
    _python_tree(home)
    _, _, baseline = _snapshot_config(source, frozen, home)
    for root in (source, frozen):
        (root / ".venv/Scripts").mkdir()
        (root / ".venv/Scripts/python.exe").write_bytes(b"synthetic-venv-python")
    dependency = discover_python_runtime(command, staged_workspace=frozen, source_workspace=source,
        baseline=baseline, scope=_scope(source, allowed=(home,)))
    assert dependency.executable == frozen / ".venv/Scripts/python.exe"
    assert dependency.extra_read == (home,)
    dependency.validate_before_launch()


def test_python_alias_without_one_frozen_environment_is_a_typed_prelaunch_failure(tmp_path):
    from mewcode.sandbox.python_runtime import PythonRuntimeUnavailable
    source, frozen = tmp_path / "source", tmp_path / "frozen"
    source.mkdir()
    frozen.mkdir()
    with pytest.raises(PythonRuntimeUnavailable, match="Command not started") as error:
        discover_python_runtime("python --version", staged_workspace=frozen, source_workspace=source,
                                baseline=(), scope=_scope(source))
    assert error.value.error_code == "python_runtime_missing"


def test_explicit_missing_workspace_python_is_also_a_typed_prelaunch_failure(tmp_path):
    from mewcode.sandbox.python_runtime import PythonRuntimeUnavailable
    source, frozen = tmp_path / "source", tmp_path / "frozen"
    source.mkdir()
    frozen.mkdir()
    with pytest.raises(PythonRuntimeUnavailable) as error:
        discover_python_runtime(r"& '.venv\Scripts\python.exe' --version",
            staged_workspace=frozen, source_workspace=source, baseline=(), scope=_scope(source))
    assert error.value.error_code == "python_runtime_missing"


def test_incomplete_environment_cannot_be_used_even_when_config_exists(tmp_path):
    from mewcode.sandbox.python_runtime import PythonRuntimeUnavailable
    source, frozen, home = (tmp_path / name for name in ("source", "frozen", "base-python"))
    for path in (source, frozen, home):
        path.mkdir()
    _, _, baseline = _snapshot_config(source, frozen, home)
    (source / ".venv/.mewcode-python-preparing").write_text("incomplete")
    with pytest.raises(PythonRuntimeUnavailable, match="preparation is incomplete"):
        discover_python_runtime("python --version", staged_workspace=frozen, source_workspace=source,
                                baseline=baseline, scope=_scope(source, allowed=(home,)))


@pytest.mark.parametrize("change", ["none", "source_drift", "frozen_drift", "denied"])
def test_nested_output_environment_retains_frozen_config_and_read_scope_checks(tmp_path, change):
    source, frozen, home = (tmp_path / name for name in ("source", "frozen", "base-python"))
    for path in (source, frozen, home):
        path.mkdir()
    _python_tree(home)
    nested = "outputs/candidate/.venv"
    cfg, staged_cfg, baseline = _snapshot_config(source, frozen, home, venv=nested)
    for root in (source, frozen):
        scripts = root / nested / "Scripts"
        scripts.mkdir()
        (scripts / "python.exe").write_bytes(b"synthetic-venv-python")
    scope = _scope(source, allowed=(home,))
    if change == "source_drift":
        cfg.write_bytes(b"changed config")
    if change == "frozen_drift":
        staged_cfg.write_bytes(b"changed snapshot")
    if change == "denied":
        original = scope

        class DenyConfig:
            current = original.current

            def allows(self, path, *, write):
                return False if Path(path) == cfg else original.allows(path, write=write)

        scope = DenyConfig()
    options = dict(staged_workspace=frozen, source_workspace=source, baseline=baseline, scope=scope)
    command = r"& 'outputs\candidate\.venv\Scripts\python.exe' -B --version"
    if change != "none":
        with pytest.raises(NativeBoundaryUnavailable):
            discover_python_runtime(command, **options)
        return
    dependency = discover_python_runtime(command, **options)
    assert dependency.source_config_path == cfg
    assert dependency.config_path == staged_cfg
    assert dependency.executable == staged_cfg.parent / "Scripts/python.exe"
    dependency.validate_before_launch()


def _runtime_link(alias, target):
    if os.name == "nt":
        # A directory junction needs no elevation; both paths are scratch fixtures.
        quote = lambda path: "'" + str(path).replace("'", "''") + "'"
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "New-Item -ItemType Junction -Path " + quote(alias) + " -Target " + quote(target) + " | Out-Null"],
            capture_output=True, check=True)
    else:
        alias.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize("change", ["none", "retarget", "config_drift"])
def test_configured_runtime_junction_is_pinned_without_changing_original_cfg(tmp_path, change):
    source, frozen, home, other = (tmp_path / name for name in ("source", "frozen", "base", "other"))
    for path in (source, frozen, home, other):
        path.mkdir()
    _python_tree(home)
    _python_tree(other)
    alias = tmp_path / "stable-runtime"
    _runtime_link(alias, home)
    source_cfg, frozen_cfg, baseline = _snapshot_config(source, frozen, alias)
    original = source_cfg.read_bytes()
    modified = frozen_cfg.stat().st_mtime_ns
    dependency = discover_python_runtime("python --version", staged_workspace=frozen, source_workspace=source,
        baseline=baseline, scope=_scope(source, allowed=(home, other)))
    assert dependency.home == home
    assert dependency.receipt["home_canonicalized"]
    assert dependency.extra_read == (home,)
    if change == "retarget":
        if os.name == "nt":
            alias.rmdir()
        else:
            alias.unlink()
        _runtime_link(alias, other)
        with pytest.raises(NativeBoundaryUnavailable, match="target changed"):
            dependency.validate_before_launch()
        assert source_cfg.read_bytes() == frozen_cfg.read_bytes() == original
        return
    dependency.validate_before_launch()
    times = dependency.prepare_execution_config()
    assert source_cfg.read_bytes() == original
    assert ("home = " + str(home)).encode() in frozen_cfg.read_bytes()
    if change == "config_drift":
        frozen_cfg.write_bytes(b"malicious protected config mutation")
        with pytest.raises(NativeBoundaryUnavailable, match="cannot be published"):
            dependency.restore_execution_config(times)
    else:
        dependency.restore_execution_config(times)
        assert frozen_cfg.read_bytes() == original and frozen_cfg.stat().st_mtime_ns == modified
    assert source_cfg.read_bytes() == original


def test_pyvenv_cfg_allows_uv_metadata_but_rejects_unknown_or_unsafe_settings(tmp_path):
    home = tmp_path / "base-python"
    parsed, values = _parse_cfg((f"home = {home}\nimplementation = CPython\nuv = 0.11.32\n"
                                 "include-system-site-packages = false\nversion = 3.12.0\n").encode())
    assert parsed == home
    assert values["implementation"] == "CPython"
    assert values["uv"] == "0.11.32"

    for raw in (
        f"home = {home}\nhome = {home}\n".encode(),
        f"home = {home}\ninclude-system-site-packages = true\n".encode(),
        f"home = {home}\nunknown-path-setting = C:\\secret\n".encode(),
        b"home = C:\\Python312\n\xff",
    ):
        with pytest.raises(NativeBoundaryUnavailable):
            _parse_cfg(raw)


def test_pyvenv_cfg_rejects_unc_traversal_and_stream_paths():
    for value in (r"\\server\share\Python312", r"C:\Python312\..\other", r"C:\Python312:stream"):
        with pytest.raises(NativeBoundaryUnavailable):
            _parse_cfg(f"home = {value}\n".encode())


def test_external_python_home_requires_shape_read_policy_and_is_bound(tmp_path):
    workspace = tmp_path / "source"
    workspace.mkdir()
    staged_workspace = tmp_path / "staged"
    staged_workspace.mkdir()
    home = tmp_path / "base-python"
    home.mkdir()
    _python_tree(home)
    cfg, staged_cfg, baseline = _snapshot_config(
        workspace, staged_workspace, home,
        metadata="implementation = CPython\nuv = 0.11.32\n")
    scope = _scope(workspace, allowed=(home,))

    dependency = discover_python_runtime(
        r"& '.venv\Scripts\python.exe' -m unittest", staged_workspace=staged_workspace,
        source_workspace=workspace, baseline=baseline, scope=scope)
    assert dependency is not None
    assert dependency.extra_read == (home.resolve(),)
    assert dependency.receipt["config_sha256"] == hashlib.sha256(staged_cfg.read_bytes()).hexdigest()
    assert dependency.receipt["home_fingerprint"]["sha256"]
    dependency.validate_before_launch()

    (home / "python.exe").write_bytes(b"replaced-runtime-image")
    with pytest.raises(NativeBoundaryUnavailable, match="base runtime changed"):
        dependency.validate_before_launch()

    # A changed original config cannot authorize launching its old frozen copy.
    cfg.write_text(f"home = {home}\ninclude-system-site-packages = true\n", encoding="utf-8")
    with pytest.raises(NativeBoundaryUnavailable, match="pyvenv.cfg changed"):
        dependency.validate_before_launch()


def test_external_home_is_rejected_when_unreadable_or_too_broad(tmp_path):
    workspace = tmp_path / "source"
    workspace.mkdir()
    staged_workspace = tmp_path / "staged"
    staged_workspace.mkdir()
    home = tmp_path / "base-python"
    home.mkdir()
    _python_tree(home)
    _, _, baseline = _snapshot_config(workspace, staged_workspace, home)

    allowed_scope = _scope(workspace)

    class DenyBasePython:
        current = allowed_scope.current

        def allows(self, path, *, write):
            return False if Path(path).is_relative_to(home) else allowed_scope.allows(path, write=write)

    denied_scope = DenyBasePython()
    with pytest.raises(NativeBoundaryUnavailable, match="not readable"):
        discover_python_runtime(r"& '.venv\Scripts\python.exe'", staged_workspace=staged_workspace,
                                source_workspace=workspace, baseline=baseline, scope=denied_scope)

    # An external root that contains the workspace is too broad, even when it
    # is otherwise readable and has a Python-looking shape.
    _, _, broad_baseline = _snapshot_config(workspace, staged_workspace, tmp_path)
    broad_scope = _scope(workspace, allowed=(tmp_path,))
    with pytest.raises(NativeBoundaryUnavailable, match="overlaps"):
        discover_python_runtime(r"& '.venv\Scripts\python.exe'", staged_workspace=staged_workspace,
                                source_workspace=workspace, baseline=broad_baseline, scope=broad_scope)


def test_external_home_requires_python_runtime_shape(tmp_path):
    workspace, staged_workspace = tmp_path / "source", tmp_path / "staged"
    workspace.mkdir()
    staged_workspace.mkdir()
    home = tmp_path / "incomplete-python"
    home.mkdir()
    (home / "python.exe").write_bytes(b"python")
    (home / "Lib" / "encodings").mkdir(parents=True)
    (home / "Lib" / "encodings" / "__init__.py").write_bytes(b"# encodings\n")
    _, _, baseline = _snapshot_config(workspace, staged_workspace, home)
    scope = _scope(workspace, allowed=(home,))
    with pytest.raises(NativeBoundaryUnavailable, match="python3.*dll"):
        discover_python_runtime(r"& '.venv\Scripts\python.exe'", staged_workspace=staged_workspace,
                                source_workspace=workspace, baseline=baseline, scope=scope)


def test_extra_read_roots_are_preserved_and_deduplicated(tmp_path):
    explicit = tmp_path / "explicit"
    derived = tmp_path / "derived"
    explicit.mkdir()
    derived.mkdir()
    assert merge_extra_read((explicit, str(derived), explicit), (derived,)) == (explicit, derived)


def test_derived_request_scope_is_readonly_complete_and_has_no_authority_subtrees(tmp_path):
    workspace, staged_workspace = tmp_path / "source", tmp_path / "staged"
    workspace.mkdir()
    staged_workspace.mkdir()
    home = tmp_path / "base-python"
    home.mkdir()
    _python_tree(home)
    _, _, baseline = _snapshot_config(workspace, staged_workspace, home)
    scope = _scope(workspace, allowed=(home,))
    dependency = discover_python_runtime(r"& '.venv\Scripts\python.exe'", staged_workspace=staged_workspace,
                                         source_workspace=workspace, baseline=baseline, scope=scope)
    good = [str(home), str(home / "python.exe"), str(home / "python312.dll"),
            str(home / "Lib" / "encodings" / "__init__.py")]
    dependency.validate_request({"read_paths": good, "write_paths": []})
    with pytest.raises(NativeBoundaryUnavailable, match="authority-store"):
        dependency.validate_request({"read_paths": [*good, str(home / ".codex" / "auth.json")],
                                     "write_paths": []})
    with pytest.raises(NativeBoundaryUnavailable, match="read-only"):
        dependency.validate_request({"read_paths": good, "write_paths": [str(home / "python.exe")]})
    with pytest.raises(NativeBoundaryUnavailable, match="missing"):
        dependency.validate_request({"read_paths": good[:2] + good[3:], "write_paths": []})


def _staged_execution_fixture(tmp_path: Path, monkeypatch, *, mutate_cfg=False):
    workspace, private, home = tmp_path / "source", tmp_path / "private", tmp_path / "base-python"
    workspace.mkdir()
    private.mkdir()
    home.mkdir()
    venv = workspace / ".venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "python.exe").write_bytes(b"synthetic-venv-python")
    _python_tree(home)
    cfg = venv / "pyvenv.cfg"
    cfg.write_text(f"home = {home}\ninclude-system-site-packages = false\nversion = 3.12.0\n",
                   encoding="utf-8")
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(workspace)),
                               RuleEngine(), PermissionMode.AUTO)
    checker.sandbox._allowed_roots = [workspace]
    checker.sandbox.grant_directory(str(home), writable=False)
    checker.task_id = "python-runtime-stage-test"
    scope = lpac.NativeScope.from_checker(checker)
    registry = TransactionRegistry(private)
    helper = private / "bundle" / "runtime.exe"
    helper.parent.mkdir(parents=True)
    (helper.parent / "git").mkdir()
    (helper.parent / "git" / "git.exe").write_bytes(b"synthetic-git")
    monkeypatch.setattr(lpac, "HostCancelEvent", Mock(return_value=Mock(close=Mock())))
    request_fields = [home, home / "python.exe", home / "python312.dll",
                      home / "Lib" / "encodings" / "__init__.py"]
    observed = {}

    def build(command, mapped, runtime, **kwargs):
        record = registry.get(CURRENT_TRANSACTION.get())
        assert record["state"] == "preparing"
        observed["preparing_dependency"] = record["python_runtime_dependency"]
        observed["extra_read"] = kwargs["extra_read"]
        if mutate_cfg:
            cfg.write_text(f"home = {home}\ninclude-system-site-packages = true\n", encoding="utf-8")
        return {"request_id": "b" * 32, "timeout_ms": 3000, "workspace": str(mapped.workspace),
                "read_paths": [str(path) for path in request_fields], "write_paths": []}

    monkeypatch.setattr(lpac, "build_runtime_request", build)
    return scope, registry, helper, home, cfg, observed


@pytest.mark.asyncio
async def test_staged_execution_derives_and_records_external_python_home(tmp_path, monkeypatch):
    scope, registry, helper, home, cfg, observed = _staged_execution_fixture(tmp_path, monkeypatch)
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    called = False

    async def invoke(helper, envelope, cancel, on_receipt=None):
        nonlocal called
        called = True
        receipt = NativeExecutionReceipt("exited", exit_code=0, backend=lpac.BACKEND, cleanup_confirmed=True)
        on_receipt(receipt)
        return receipt

    monkeypatch.setattr(lpac, "invoke_helper", invoke)
    result = await execution.execute_staged(
        r"& '.venv\Scripts\python.exe' -m unittest", scope=scope, registry=registry,
        helper=helper, timeout=3, host_parent=registry.root, extra_read=(explicit, home))
    assert called
    assert not result.is_error
    assert observed["extra_read"] == (explicit, home)
    assert observed["preparing_dependency"]["config_sha256"] == hashlib.sha256(cfg.read_bytes()).hexdigest()
    assert observed["preparing_dependency"]["home"] == str(home.resolve())
    assert observed["preparing_dependency"]["home_fingerprint"]
    assert not registry.pending()


@pytest.mark.asyncio
async def test_staged_execution_rejects_config_change_before_launch(tmp_path, monkeypatch):
    scope, registry, helper, home, cfg, observed = _staged_execution_fixture(
        tmp_path, monkeypatch, mutate_cfg=True)
    called = False

    async def invoke(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("changed pyvenv.cfg must be rejected before native launch")

    monkeypatch.setattr(lpac, "invoke_helper", invoke)
    result = await execution.execute_staged(
        r"& '.venv\Scripts\python.exe' -m unittest", scope=scope, registry=registry,
        helper=helper, timeout=3, host_parent=registry.root)
    assert result.is_error
    assert "pyvenv.cfg changed" in result.detail
    assert not called
    assert not registry.pending()
