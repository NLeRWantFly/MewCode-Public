"""Git write authorization and the bundled-Git production protocol."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from mewcode.permissions import (
    DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine,
)
from mewcode.permissions.task_authorization import TaskAuthorization
from mewcode.sandbox import lpac_runtime as lpac
from mewcode.sandbox.windows import NativeBoundaryUnavailable


def policy(workspace: Path) -> PermissionChecker:
    checker = PermissionChecker(
        DangerousCommandDetector(), PathSandbox(str(workspace)), RuleEngine(), PermissionMode.AUTO)
    checker.set_task_authorization(None, task_id="lpac-git-write-tests")
    return checker


def authorize(checker: PermissionChecker, workspace: Path, behaviors: list[str],
              *, task_id: str = "task", ttl: int = 3600) -> None:
    scope = TaskAuthorization.from_user_scope({
        "read_directories": [str(workspace)],
        "write_directories": [str(workspace)],
        "allowed_behaviors": behaviors,
        "ttl_seconds": ttl,
    }, task_id=task_id)
    checker.set_task_authorization(scope, task_id=task_id)


def make_repo(workspace: Path) -> Path:
    git = workspace / ".git"
    (git / "hooks").mkdir(parents=True)
    (git / "objects" / "ab").mkdir(parents=True)
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "index").write_bytes(b"synthetic-index")
    (git / "config").write_text("[core]\n")
    (git / "hooks" / "pre-commit").write_text("#!/bin/sh\n")
    (git / "objects" / "ab" / "cdef").write_bytes(b"synthetic-object")
    (git / "refs" / "heads" / "main").write_text("0" * 40 + "\n")
    (workspace / "file.txt").write_text("content")
    return git


def runtime_dirs(tmp_path: Path) -> Path:
    runtime = tmp_path / "host-runtime"
    for part in ("temp", "profile/AppData/Local", "profile/AppData/Roaming"):
        (runtime / part).mkdir(parents=True)
    return runtime


def test_git_payload_write_denied_without_authorization(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    git = make_repo(workspace)
    scope = lpac.NativeScope.from_checker(policy(workspace))
    assert scope.allows(git / "index", write=True) is False
    assert scope.allows(git / "index", write=False) is True
    assert scope.allows(workspace / "file.txt", write=True) is True


def test_git_payload_write_denied_without_git_write_behavior(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    git = make_repo(workspace)
    checker = policy(workspace)
    authorize(checker, workspace, ["isolated_command"])
    scope = lpac.NativeScope.from_checker(checker)
    assert scope.allows(git / "index", write=True) is False
    assert scope.allows(workspace / "file.txt", write=True) is True


def test_git_payload_write_allowed_with_git_write_behavior(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    git = make_repo(workspace)
    checker = policy(workspace)
    authorize(checker, workspace, ["isolated_command", "git_write"])
    scope = lpac.NativeScope.from_checker(checker)
    assert scope.allows(git / "index", write=True) is True
    assert scope.allows(git / "objects" / "ab" / "cdef", write=True) is True
    assert scope.allows(git / "refs" / "heads" / "main", write=True) is True


def test_git_control_surfaces_never_writable_even_with_git_write(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    git = make_repo(workspace)
    checker = policy(workspace)
    authorize(checker, workspace, ["isolated_command", "git_write"])
    scope = lpac.NativeScope.from_checker(checker)
    assert scope.allows(git, write=True) is False
    assert scope.allows(git / "hooks", write=True) is False
    assert scope.allows(git / "hooks" / "pre-commit", write=True) is False
    assert scope.allows(git / "config", write=True) is False
    # Control surfaces stay readable; only writes are blocked.
    assert scope.allows(git / "hooks" / "pre-commit", write=False) is True
    assert scope.allows(git / "config", write=False) is True


def test_git_write_denied_on_task_mismatch_or_expiry(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    git = make_repo(workspace)
    checker = policy(workspace)
    authorize(checker, workspace, ["git_write"], task_id="task")
    scope = lpac.NativeScope.from_checker(checker)
    checker.task_authorization = TaskAuthorization(
        "task", "v", (), (), (), (), ("git_write",), time.time() - 1)
    assert scope.allows(git / "index", write=True) is False
    authorize(checker, workspace, ["git_write"], task_id="other-task")
    assert scope.allows(git / "index", write=True) is False


def test_request_exposes_only_authorized_git_payload(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    git = make_repo(workspace)
    runtime = runtime_dirs(tmp_path)
    checker = policy(workspace)
    authorize(checker, workspace, ["isolated_command", "git_write"])
    scope = lpac.NativeScope.from_checker(checker)
    request = lpac.build_runtime_request("git status", scope, runtime, timeout=4, cancel_name="host")
    writes = set(request["write_paths"])
    reads = set(request["read_paths"])
    assert str((git / "index").resolve()) in writes
    assert str((git / "objects" / "ab" / "cdef").resolve()) in writes
    assert str((git / "hooks" / "pre-commit").resolve()) not in writes
    assert str((git / "config").resolve()) not in writes
    assert str((git / "hooks" / "pre-commit").resolve()) in reads
    assert str(git.resolve()) not in writes


def bundled_git(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    git_dir = bundle / "git"
    git_dir.mkdir(parents=True)
    (git_dir / "git.exe").write_bytes(b"synthetic-git")
    (git_dir / "zlib1.dll").write_bytes(b"synthetic-dll")
    return git_dir


def test_bundled_git_enters_path_read_grants_and_profile_config(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "file.txt").write_text("content")
    runtime = runtime_dirs(tmp_path)
    git_dir = bundled_git(tmp_path)
    scope = lpac.NativeScope.from_checker(policy(workspace))
    request = lpac.build_runtime_request(
        "git status", scope, runtime, timeout=4, cancel_name="host", git_dir=git_dir)
    reads = set(request["read_paths"])
    assert str(git_dir.resolve()) in reads
    assert str((git_dir / "git.exe").resolve()) in reads
    assert str((git_dir / "zlib1.dll").resolve()) in reads
    environment = request["environment"]
    assert environment["PATH"].split(";")[0] == str(git_dir.resolve())
    assert environment["PYTHONIOENCODING"] == lpac._oem_python_encoding()
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    config = (runtime / "profile" / ".gitconfig").read_text(encoding="ascii")
    assert "MewCode AUTO" in config and "quotepath = false" in config
    command_text = request["arguments"][-1]
    assert "function global:git" in command_text
    assert "Start-Process" in command_text and "Get-Content -Raw -Encoding UTF8" in command_text
    assert "-WorkingDirectory (Get-Location).ProviderPath" in command_text
    assert "-RedirectStandardInput" in command_text
    captures = list((runtime / "temp").glob("git-*"))
    assert len(captures) == lpac.GIT_CAPTURE_SLOTS * 3
    assert all(str(p) in reads and str(p) in request["write_paths"] for p in captures)
    assert "Git capture capacity exhausted" in command_text


def test_bundled_git_must_stay_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    runtime = runtime_dirs(tmp_path)
    inside = workspace / "tools" / "git"
    inside.mkdir(parents=True)
    (inside / "git.exe").write_bytes(b"synthetic-git")
    scope = lpac.NativeScope.from_checker(policy(workspace))
    with pytest.raises(NativeBoundaryUnavailable, match="outside the command workspace"):
        lpac.build_runtime_request(
            "git status", scope, runtime, timeout=4, cancel_name="host", git_dir=inside)


def test_request_without_bundled_git_keeps_system_path_first(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "file.txt").write_text("content")
    runtime = runtime_dirs(tmp_path)
    scope = lpac.NativeScope.from_checker(policy(workspace))
    request = lpac.build_runtime_request("Write-Output x", scope, runtime, timeout=4,
                                         cancel_name="host")
    assert "git.exe" not in "\n".join(request["read_paths"])
    assert not (runtime / "profile" / ".gitconfig").exists()


def test_tool_runtime_tree_leaves_are_explicitly_read_only(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    venv_scripts = workspace / ".venv" / "Scripts"
    venv_scripts.mkdir(parents=True)
    (venv_scripts / "python.exe").write_bytes(b"synthetic-python")
    (workspace / "file.txt").write_text("content")
    runtime = runtime_dirs(tmp_path)
    scope = lpac.NativeScope.from_checker(policy(workspace))
    request = lpac.build_runtime_request("test", scope, runtime, timeout=4, cancel_name="host")
    reads = set(request["read_paths"])
    writes = set(request["write_paths"])
    venv = (workspace / ".venv").resolve()
    # Native grants do not inherit; explicit leaves are required to execute.
    assert str(venv) in reads
    assert str((venv_scripts / "python.exe").resolve()) in reads
    assert str((venv_scripts / "python.exe").resolve()) not in writes
    assert str(venv) not in writes
    assert str((workspace / "file.txt").resolve()) in writes


def test_workspace_root_named_like_tool_runtime_stays_walkable(tmp_path: Path) -> None:
    workspace = tmp_path / "venv"
    workspace.mkdir()
    (workspace / "file.txt").write_text("content")
    runtime = runtime_dirs(tmp_path)
    scope = lpac.NativeScope.from_checker(policy(workspace))
    request = lpac.build_runtime_request("test", scope, runtime, timeout=4, cancel_name="host")
    assert str((workspace / "file.txt").resolve()) in request["write_paths"]


def test_external_interpreter_leaves_have_only_explicit_read_grants(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    interpreter = tmp_path / "python-runtime"
    (interpreter / "Lib").mkdir(parents=True)
    dll = interpreter / "python314.dll"
    stdlib = interpreter / "Lib" / "os.py"
    dll.write_bytes(b"synthetic-dll")
    stdlib.write_text("# synthetic")
    scope = lpac.NativeScope.from_checker(policy(workspace))
    request = lpac.build_runtime_request("test", scope, runtime_dirs(tmp_path), timeout=4,
                                         cancel_name="host", extra_read=(interpreter,))
    for leaf in (dll, stdlib):
        assert str(leaf.resolve()) in request["read_paths"]
        assert str(leaf.resolve()) not in request["write_paths"]
