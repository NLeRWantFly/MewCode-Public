from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mewcode.tools import inspect_git_state
from mewcode.tools.inspect_git_state import (
    MAX_OUTPUT_CHARS,
    InspectGitState,
    InspectGitStateParams,
)


def _clean_git_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=_clean_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _init_repository(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "--quiet")
    _git(root, "config", "user.name", "Synthetic Test")
    _git(root, "config", "user.email", "synthetic@example.invalid")


def _commit_all(root: Path, message: str = "initial") -> None:
    _git(root, "add", "--all")
    _git(root, "commit", "--quiet", "-m", message)


def _payload(result: object) -> dict[str, object]:
    return json.loads(getattr(result, "output"))


def _synthetic_nested_workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    parent_repo = tmp_path / "synthetic-parent-repository"
    _init_repository(parent_repo)
    (parent_repo / "parent-secret.txt").write_text(
        "parent repository content\n", encoding="utf-8"
    )
    _commit_all(parent_repo)

    work_dir = parent_repo / "AUTO独立工作区 fixture" / "中文 workspace 空格"
    work_dir.mkdir(parents=True)
    repo = work_dir / "repository"
    _init_repository(repo)
    (repo / "sibling.txt").write_text("baseline sibling\n", encoding="utf-8")
    (repo / "subdir").mkdir()
    (repo / "subdir" / "selected.txt").write_text(
        "baseline selected\n", encoding="utf-8"
    )
    _commit_all(repo)
    return work_dir, repo, parent_repo


def _synthetic_workspace_with_filtered_dirty_submodule(
    tmp_path: Path,
) -> tuple[Path, Path]:
    work_dir = tmp_path / "synthetic-submodule-workspace"
    repo = work_dir / "repository"
    _init_repository(repo)

    child_source = tmp_path / "synthetic-child-source"
    _init_repository(child_source)
    (child_source / ".gitattributes").write_text(
        "selected.txt filter=tripwire\n", encoding="utf-8"
    )
    (child_source / "selected.txt").write_text("child baseline\n", encoding="utf-8")
    _commit_all(child_source)

    added = subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(child_source),
            "vendor/child",
        ],
        cwd=repo,
        env=_clean_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert added.returncode == 0, added.stderr
    _commit_all(repo, "add synthetic submodule")

    marker = work_dir / "submodule-filter-command-was-run.txt"
    script = work_dir / "submodule_filter_tripwire.py"
    script.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    filter_command = subprocess.list2cmdline([sys.executable, str(script)])
    child_checkout = repo / "vendor" / "child"
    _git(child_checkout, "config", "filter.tripwire.clean", filter_command)
    (child_checkout / "selected.txt").write_text(
        "dirty child state\n", encoding="utf-8"
    )
    return work_dir, marker


@pytest.mark.asyncio
async def test_default_path_refuses_parent_repository_before_starting_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_dir, _, _ = _synthetic_nested_workspace(tmp_path)

    async def fail_if_git_starts(*args: object, **kwargs: object) -> None:
        raise AssertionError("Git must not start when the workspace has no .git marker")

    monkeypatch.setattr(inspect_git_state.asyncio, "create_subprocess_exec", fail_if_git_starts)
    result = await InspectGitState().run(
        InspectGitStateParams(operation="status"), work_dir=str(work_dir)
    )

    assert result.is_error
    payload = _payload(result)
    assert "No Git repository was found" in payload["error"]
    assert "Explicitly select the repository subdirectory" in payload["error"]
    assert "parent-secret.txt" not in result.output


@pytest.mark.asyncio
async def test_explicit_nested_repository_is_selected_without_parent_status(
    tmp_path: Path,
) -> None:
    work_dir, repo, _ = _synthetic_nested_workspace(tmp_path)
    (repo / "subdir" / "selected.txt").write_text("changed selected\n", encoding="utf-8")

    result = await InspectGitState().run(
        InspectGitStateParams(operation="status", path="repository"),
        work_dir=str(work_dir),
    )

    assert not result.is_error
    payload = _payload(result)
    assert payload["workspace"] == str(work_dir.resolve())
    assert payload["selected_path"] == str(repo.resolve())
    assert payload["repository_root"] == str(repo.resolve())
    assert "selected.txt" in payload["command_result"]["output"]
    assert "parent-secret.txt" not in result.output
    assert "AUTO独立工作区 fixture" not in payload["command_result"]["output"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../sibling", "../synthetic-parent-repository"])
async def test_parent_traversal_is_rejected(
    tmp_path: Path, path: str
) -> None:
    work_dir, _, _ = _synthetic_nested_workspace(tmp_path)
    result = await InspectGitState().run(
        InspectGitStateParams(operation="status", path=path),
        work_dir=str(work_dir),
    )

    assert result.is_error
    assert "inside the workspace" in result.output or "escapes" in result.output


@pytest.mark.asyncio
async def test_absolute_path_is_rejected(tmp_path: Path) -> None:
    work_dir, _, _ = _synthetic_nested_workspace(tmp_path)
    outside = tmp_path / "absolute-outside"
    outside.mkdir()

    result = await InspectGitState().run(
        InspectGitStateParams(operation="status", path=str(outside)),
        work_dir=str(work_dir),
    )

    assert result.is_error
    assert "must be relative" in result.output


@pytest.mark.asyncio
async def test_inherited_git_environment_cannot_redirect_repository_or_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_dir, repo, _ = _synthetic_nested_workspace(tmp_path)
    poison_repo = tmp_path / "poison-repository"
    _init_repository(poison_repo)
    (poison_repo / "poison-only.txt").write_text("poison\n", encoding="utf-8")
    _commit_all(poison_repo)
    (poison_repo / "poison-only.txt").write_text("poison changed\n", encoding="utf-8")

    injected = {
        "GIT_DIR": str(poison_repo / ".git"),
        "GIT_WORK_TREE": str(poison_repo),
        "GIT_INDEX_FILE": str(poison_repo / ".git" / "index"),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.worktree",
        "GIT_CONFIG_VALUE_0": str(poison_repo),
        "GIT_CONFIG_PARAMETERS": "'core.worktree=poison'",
        "GIT_CONFIG_GLOBAL": str(poison_repo / "poison-global-config"),
        "GIT_CONFIG_SYSTEM": str(poison_repo / "poison-system-config"),
    }
    for key, value in injected.items():
        monkeypatch.setenv(key, value)

    original = inspect_git_state.asyncio.create_subprocess_exec
    child_envs: list[dict[str, str]] = []

    async def capture_child_env(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        child_env = kwargs.get("env")
        assert isinstance(child_env, dict)
        child_envs.append(child_env)
        return await original(*args, **kwargs)

    monkeypatch.setattr(inspect_git_state.asyncio, "create_subprocess_exec", capture_child_env)
    result = await InspectGitState().run(
        InspectGitStateParams(operation="status", path="repository"),
        work_dir=str(work_dir),
    )

    assert not result.is_error
    payload = _payload(result)
    assert payload["repository_root"] == str(repo.resolve())
    assert "poison-only.txt" not in result.output
    assert len(child_envs) == 3  # root discovery, filter config, and status
    for child_env in child_envs:
        for key, value in injected.items():
            assert child_env.get(key) != value
        assert child_env["GIT_CEILING_DIRECTORIES"] == str(work_dir.resolve())
        assert child_env["GIT_OPTIONAL_LOCKS"] == "0"
        assert child_env["GIT_CONFIG_NOSYSTEM"] == "1"


@pytest.mark.asyncio
async def test_status_and_diffs_only_report_selected_subdirectory(
    tmp_path: Path,
) -> None:
    work_dir, repo, _ = _synthetic_nested_workspace(tmp_path)
    selected_file = repo / "subdir" / "selected.txt"
    selected_file.write_text("staged selected\n", encoding="utf-8")
    _git(repo, "add", "subdir/selected.txt")
    selected_file.write_text("unstaged selected\n", encoding="utf-8")
    (repo / "sibling.txt").write_text("changed outside selection\n", encoding="utf-8")
    (repo / "sibling-untracked.txt").write_text("untracked outside selection\n", encoding="utf-8")

    for operation in ("status", "diff", "staged_diff", "diff_stat"):
        result = await InspectGitState().run(
            InspectGitStateParams(operation=operation, path="repository/subdir"),
            work_dir=str(work_dir),
        )
        assert not result.is_error, (operation, result.output)
        command_output = _payload(result)["command_result"]["output"]
        assert "selected.txt" in command_output, (operation, result.output)
        assert "sibling.txt" not in command_output, (operation, result.output)
        assert "sibling-untracked.txt" not in command_output, (operation, result.output)
        assert "changed outside selection" not in command_output
        assert "untracked outside selection" not in command_output


@pytest.mark.asyncio
async def test_included_clean_filter_is_refused_without_running_its_command(
    tmp_path: Path,
) -> None:
    work_dir, repo, _ = _synthetic_nested_workspace(tmp_path)
    marker = work_dir / "filter-command-was-run.txt"
    script = work_dir / "filter_tripwire.py"
    script.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    filter_command = subprocess.list2cmdline([sys.executable, str(script)])
    include_config = repo / "included-filter-config"
    (repo / "subdir" / ".gitattributes").write_text(
        "selected.txt filter=tripwire\n", encoding="utf-8"
    )
    _commit_all(repo, "add filter attributes")
    _git(
        repo,
        "config",
        "--file",
        str(include_config),
        "filter.tripwire.clean",
        filter_command,
    )
    _git(repo, "config", "include.path", str(include_config))
    (repo / "subdir" / "selected.txt").write_text(
        "modified to require clean filter\n", encoding="utf-8"
    )

    result = await InspectGitState().run(
        InspectGitStateParams(operation="diff", path="repository"),
        work_dir=str(work_dir),
    )

    assert result.is_error
    payload = _payload(result)
    assert "filter.tripwire.clean" in payload["error"]
    assert payload["command_result"] is None
    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["status", "diff", "staged_diff", "diff_stat"])
async def test_parent_operations_do_not_inspect_dirty_submodule_filter(
    tmp_path: Path, operation: str
) -> None:
    work_dir, marker = _synthetic_workspace_with_filtered_dirty_submodule(tmp_path)

    result = await InspectGitState().run(
        InspectGitStateParams(operation=operation, path="repository"),
        work_dir=str(work_dir),
    )

    assert not result.is_error, (operation, result.output)
    payload = _payload(result)
    assert payload["submodule_state"] == "not_inspected"
    assert not marker.exists(), operation


def test_long_backslash_and_newline_error_is_json_bounded_and_not_duplicated(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    selected = workspace / "repository"
    workspace.mkdir()
    selected.mkdir()
    long_error = "line\\segment\n" * 4000

    result = InspectGitState._format_result(
        workspace=workspace,
        selected=selected,
        repo_root=selected,
        operation="diff",
        result=long_error,
        exit_code=1,
        is_error=True,
    )

    assert result.is_error
    assert len(result.output) <= MAX_OUTPUT_CHARS
    payload = json.loads(result.output)
    assert payload["error"] is None
    assert payload["command_result"]["output"].endswith("…(output truncated)")


@pytest.mark.asyncio
async def test_default_path_still_works_when_workspace_is_repository_root(
    tmp_path: Path,
) -> None:
    work_dir = tmp_path / "workspace-repository"
    _init_repository(work_dir)
    (work_dir / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _commit_all(work_dir)
    (work_dir / "tracked.txt").write_text("changed\n", encoding="utf-8")

    result = await InspectGitState().run(
        InspectGitStateParams(operation="status"), work_dir=str(work_dir)
    )

    assert not result.is_error
    payload = _payload(result)
    assert payload["workspace"] == str(work_dir.resolve())
    assert payload["selected_path"] == str(work_dir.resolve())
    assert payload["repository_root"] == str(work_dir.resolve())
    assert "tracked.txt" in payload["command_result"]["output"]


@pytest.mark.asyncio
async def test_missing_active_work_directory_fails_closed() -> None:
    result = await InspectGitState().run(InspectGitStateParams(operation="status"))

    assert result.is_error
    assert "requires an active workspace directory" in result.output


@pytest.mark.asyncio
async def test_git_metadata_junction_is_refused_before_git_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_dir, repo, _ = _synthetic_nested_workspace(tmp_path)
    marker = repo / ".git"

    async def fail_if_git_starts(*args: object, **kwargs: object) -> None:
        raise AssertionError("Git must not start when metadata is a reparse point")

    monkeypatch.setattr(
        inspect_git_state.os.path,
        "isjunction",
        lambda path: Path(path) == marker,
    )
    monkeypatch.setattr(
        inspect_git_state.asyncio, "create_subprocess_exec", fail_if_git_starts
    )
    result = await InspectGitState().run(
        InspectGitStateParams(operation="status", path="repository"),
        work_dir=str(work_dir),
    )

    assert result.is_error
    assert "symbolic link or reparse point" in _payload(result)["error"]


class _HangingGitProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.killed = False
        self._stopped = asyncio.Event()

    async def communicate(self) -> tuple[bytes, bytes]:
        await self._stopped.wait()
        return b"", b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._stopped.set()

    async def wait(self) -> int:
        await self._stopped.wait()
        assert self.returncode is not None
        return self.returncode


@pytest.mark.asyncio
async def test_timeout_kills_and_reaps_git_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_dir = tmp_path / "workspace"
    (work_dir / "repository" / ".git").mkdir(parents=True)
    process = _HangingGitProcess()

    async def start_hanging_git(*args: object, **kwargs: object) -> _HangingGitProcess:
        return process

    monkeypatch.setattr(inspect_git_state, "GIT_COMMAND_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(inspect_git_state.asyncio, "create_subprocess_exec", start_hanging_git)
    result = await InspectGitState().run(
        InspectGitStateParams(operation="status", path="repository"),
        work_dir=str(work_dir),
    )

    assert result.is_error
    assert "timed out" in _payload(result)["error"]
    assert process.killed
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_cancellation_kills_and_reaps_git_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_dir = tmp_path / "workspace"
    (work_dir / "repository" / ".git").mkdir(parents=True)
    process = _HangingGitProcess()
    started = asyncio.Event()

    async def start_hanging_git(*args: object, **kwargs: object) -> _HangingGitProcess:
        started.set()
        return process

    monkeypatch.setattr(inspect_git_state.asyncio, "create_subprocess_exec", start_hanging_git)
    task = asyncio.create_task(
        InspectGitState().run(
            InspectGitStateParams(operation="status", path="repository"),
            work_dir=str(work_dir),
        )
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed
    assert process.returncode is not None
