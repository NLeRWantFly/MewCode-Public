from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path, PureWindowsPath

from pydantic import BaseModel, ConfigDict, Field

from mewcode.tools.base import MAX_OUTPUT_CHARS, Tool, ToolResult, active_work_dir


GIT_COMMAND_TIMEOUT_SECONDS = 15
GIT_CLEANUP_TIMEOUT_SECONDS = 3


class InspectGitStateParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: str = Field(
        description="One of: status, diff, staged_diff, diff_stat"
    )
    path: str = Field(
        default=".",
        description=(
            "Relative directory inside the active workspace. Git state is limited "
            "to this directory; select the repository subdirectory explicitly."
        ),
    )


class _GitCommandTimeout(Exception):
    pass


async def _stop_and_reap(
    process: asyncio.subprocess.Process,
    communication: asyncio.Task[tuple[bytes, bytes]],
) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass

    try:
        await asyncio.wait_for(
            asyncio.shield(communication), timeout=GIT_CLEANUP_TIMEOUT_SECONDS
        )
        return
    except asyncio.CancelledError:
        # The child is already killed. Keep reaping it even if cancellation is
        # requested again while cleanup is in progress.
        pass
    except (asyncio.TimeoutError, Exception):
        pass

    if not communication.done():
        communication.cancel()
    try:
        await asyncio.wait_for(process.wait(), timeout=GIT_CLEANUP_TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
        pass


async def _communicate_bounded(
    process: asyncio.subprocess.Process,
) -> tuple[bytes, bytes]:
    communication = asyncio.create_task(process.communicate())
    try:
        return await asyncio.wait_for(
            asyncio.shield(communication), timeout=GIT_COMMAND_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError as exc:
        await _stop_and_reap(process, communication)
        raise _GitCommandTimeout from exc
    except asyncio.CancelledError:
        await asyncio.shield(_stop_and_reap(process, communication))
        raise
    except Exception:
        await _stop_and_reap(process, communication)
        raise


def _git_environment(workspace: Path) -> dict[str, str]:
    # Git environment variables can redirect repository, index, config, and
    # object lookup. Start without any inherited Git-specific selectors.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    env.update(
        {
            "GIT_CEILING_DIRECTORIES": str(workspace),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _find_repository_marker(selected: Path, workspace: Path) -> Path | None:
    """Find only the nearest .git marker from selected up through workspace."""
    current = selected
    while True:
        marker = current / ".git"
        if marker.exists() or marker.is_symlink():
            return current
        if current == workspace:
            return None
        current = current.parent


def _is_reparse_path(path: Path, resolved: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction is not None and is_junction(path):
        return True
    # Older Python versions do not expose isjunction. A resolved spelling
    # change catches junction/reparse targets on those runtimes while
    # normcase avoids treating Windows case normalization as a link.
    normalize = lambda item: os.path.normcase(os.path.normpath(str(item)))
    return normalize(path) != normalize(resolved)


def _executable_filter_keys(config_output: str) -> list[str]:
    keys: list[str] = []
    for record in config_output.split("\0"):
        if not record:
            continue
        if "\n" not in record:
            raise ValueError("Git returned an unrecognized filter configuration record")
        key, value = record.split("\n", 1)
        normalized_key = key.lower()
        if not normalized_key.startswith("filter.") or not normalized_key.endswith(
            (".clean", ".process")
        ):
            raise ValueError("Git returned an unexpected filter configuration key")
        if not normalized_key[len("filter.") :].rsplit(".", 1)[0]:
            raise ValueError("Git returned a filter key without a driver name")
        if value.strip():
            keys.append(key)
    return keys


def _command_prefix() -> list[str]:
    # These overrides prevent local repository settings from starting helpers
    # during status/diff. Diff flags below also disable external diff and
    # textconv on every diff operation.
    return [
        "git",
        "--no-pager",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.pager=cat",
        "-c",
        "diff.external=",
    ]


class InspectGitState(Tool):
    """Auditor-safe Git evidence without accepting arbitrary shell text."""

    name = "InspectGitState"
    description = (
        "Inspect the current Git status or diff using a fixed read-only operation. "
        "Choose a repository subdirectory with path when the workspace root is "
        "not itself a Git repository. Submodule-internal dirty state is not "
        "inspected. This tool cannot run arbitrary shell text."
    )
    params_model = InspectGitStateParams
    category = "read"
    is_concurrency_safe = True

    _ARGS = {
        "status": (
            "status",
            "--short",
            "--branch",
            "--untracked-files=all",
            "--ignore-submodules=all",
            "--",
            ".",
        ),
        "diff": (
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=all",
            "--relative",
            "--",
            ".",
        ),
        "staged_diff": (
            "diff",
            "--cached",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=all",
            "--relative",
            "--",
            ".",
        ),
        "diff_stat": (
            "diff",
            "--stat",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=all",
            "--relative",
            "--",
            ".",
        ),
    }

    async def _run_git(
        self, args: tuple[str, ...] | list[str], *, cwd: Path, env: dict[str, str]
    ) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            *_command_prefix(),
            *args,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await _communicate_bounded(process)
        return (
            int(process.returncode or 0),
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )

    @staticmethod
    def _format_result(
        *,
        workspace: Path,
        selected: Path,
        repo_root: Path | None,
        operation: str,
        result: str,
        exit_code: int | None,
        is_error: bool,
    ) -> ToolResult:
        command_result = (
            {"exit_code": exit_code, "output": result or "(no changes)"}
            if exit_code is not None
            else None
        )
        payload = {
            "workspace": str(workspace),
            "selected_path": str(selected),
            "repository_root": str(repo_root) if repo_root is not None else None,
            "operation": operation,
            "submodule_state": "not_inspected",
            "command_result": command_result,
            # Keep error output in exactly one field. A non-zero command's
            # stderr remains available in command_result.output.
            "error": result if is_error and exit_code is None else None,
        }
        output = json.dumps(payload, ensure_ascii=False)
        if len(output) > MAX_OUTPUT_CHARS:
            if command_result is not None:
                target = command_result["output"]
                setter = lambda value: command_result.__setitem__("output", value)
            elif isinstance(payload["error"], str):
                target = payload["error"]
                setter = lambda value: payload.__setitem__("error", value)
            else:
                target = ""
                setter = lambda value: None

            marker = "\n…(output truncated)"
            low, high = 0, len(target)
            best = ""
            while low <= high:
                middle = (low + high) // 2
                candidate = target[:middle] + marker
                setter(candidate)
                serialized = json.dumps(payload, ensure_ascii=False)
                if len(serialized) <= MAX_OUTPUT_CHARS:
                    best = candidate
                    low = middle + 1
                else:
                    high = middle - 1
            setter(best if best else "(output truncated)")
            output = json.dumps(payload, ensure_ascii=False)
            if len(output) > MAX_OUTPUT_CHARS:
                # Extremely long paths could exceed the limit without any
                # command output left to trim. Return a valid bounded error.
                output = json.dumps(
                    {"error": "Inspection metadata exceeds the output limit."}
                )
        return ToolResult(output=output, is_error=is_error)

    async def execute(self, params: InspectGitStateParams) -> ToolResult:
        args = self._ARGS.get(params.operation)
        if args is None:
            return ToolResult(
                output="Invalid operation. Use status, diff, staged_diff, or diff_stat.",
                is_error=True,
            )

        work_dir = active_work_dir()
        if not work_dir:
            return ToolResult(
                output=(
                    "Git inspection requires an active workspace directory; no "
                    "host working directory or repository will be assumed."
                ),
                is_error=True,
            )

        try:
            workspace = Path(work_dir).resolve(strict=True)
            if not workspace.is_dir():
                raise NotADirectoryError(str(workspace))
        except (OSError, RuntimeError) as exc:
            return ToolResult(
                output=f"Git inspection requires an existing active workspace directory: {exc}",
                is_error=True,
            )

        raw_path = params.path
        windows_path = PureWindowsPath(raw_path)
        relative_path = Path(raw_path)
        if (
            not raw_path.strip()
            or relative_path.is_absolute()
            or windows_path.is_absolute()
            or bool(windows_path.drive)
        ):
            return ToolResult(
                output=(
                    f"Selected path must be relative to the active workspace: {raw_path!r}."
                ),
                is_error=True,
            )

        try:
            selected = (workspace / relative_path).resolve(strict=True)
            if not selected.is_dir():
                raise NotADirectoryError(str(selected))
        except (OSError, RuntimeError) as exc:
            return ToolResult(
                output=f"Selected path must be an existing directory inside the workspace: {exc}",
                is_error=True,
            )
        if not _inside(selected, workspace):
            return ToolResult(
                output=(
                    f"Selected path escapes the active workspace: {raw_path!r}. "
                    "Use a relative directory inside the workspace."
                ),
                is_error=True,
            )

        marker_root = _find_repository_marker(selected, workspace)
        if marker_root is None:
            return self._format_result(
                workspace=workspace,
                selected=selected,
                repo_root=None,
                operation=params.operation,
                result=(
                    "No Git repository was found from the selected path up to the "
                    "workspace root. Explicitly select the repository subdirectory "
                    "with path; this tool does not scan or choose a repository."
                ),
                exit_code=None,
                is_error=True,
            )

        marker = marker_root / ".git"
        try:
            resolved_marker = marker.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            return self._format_result(
                workspace=workspace,
                selected=selected,
                repo_root=None,
                operation=params.operation,
                result=f"Git metadata marker could not be resolved safely: {exc}",
                exit_code=None,
                is_error=True,
            )
        if not _inside(resolved_marker, workspace):
            return self._format_result(
                workspace=workspace,
                selected=selected,
                repo_root=None,
                operation=params.operation,
                result=(
                    "Git metadata resolves outside the active workspace; "
                    "inspection was refused."
                ),
                exit_code=None,
                is_error=True,
            )
        if _is_reparse_path(marker, resolved_marker):
            return self._format_result(
                workspace=workspace,
                selected=selected,
                repo_root=None,
                operation=params.operation,
                result=(
                    "Repository .git metadata is a symbolic link or reparse point. "
                    "This tool refuses linked metadata so Git cannot read outside "
                    "the active workspace."
                ),
                exit_code=None,
                is_error=True,
            )
        if marker.is_file():
            return self._format_result(
                workspace=workspace,
                selected=selected,
                repo_root=None,
                operation=params.operation,
                result=(
                    "Repository uses a .git pointer file or symlink (such as a linked "
                    "worktree/submodule), which this tool does not inspect because "
                    "its metadata location cannot be safely bound to the workspace."
                ),
                exit_code=None,
                is_error=True,
            )

        env = _git_environment(workspace)
        try:
            discovery_code, discovery_out, discovery_err = await self._run_git(
                ["rev-parse", "--show-toplevel"],
                cwd=selected,
                env=env,
            )
        except _GitCommandTimeout:
            return self._format_result(
                workspace=workspace,
                selected=selected,
                repo_root=None,
                operation=params.operation,
                result=f"Git repository discovery timed out ({GIT_COMMAND_TIMEOUT_SECONDS}s).",
                exit_code=None,
                is_error=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._format_result(
                workspace=workspace,
                selected=selected,
                repo_root=None,
                operation=params.operation,
                result=f"Git repository discovery failed: {exc}",
                exit_code=None,
                is_error=True,
            )

        if discovery_code != 0:
            detail = discovery_err.strip() or discovery_out.strip() or "Git returned no repository root."
            return self._format_result(
                workspace=workspace,
                selected=selected,
                repo_root=None,
                operation=params.operation,
                result=f"Git repository discovery failed: {detail}",
                exit_code=discovery_code,
                is_error=True,
            )

        try:
            repo_root = Path(discovery_out.strip()).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            return self._format_result(
                workspace=workspace,
                selected=selected,
                repo_root=None,
                operation=params.operation,
                result=f"Git returned an invalid repository root: {exc}",
                exit_code=discovery_code,
                is_error=True,
            )
        if not _inside(repo_root, workspace) or not _inside(selected, repo_root):
            return self._format_result(
                workspace=workspace,
                selected=selected,
                repo_root=repo_root,
                operation=params.operation,
                result=(
                    "Git repository root is outside the active workspace or does not "
                    "contain the selected path; inspection was refused."
                ),
                exit_code=discovery_code,
                is_error=True,
            )
        if repo_root != marker_root:
            return self._format_result(
                workspace=workspace,
                selected=selected,
                repo_root=repo_root,
                operation=params.operation,
                result=(
                    "Git resolved a repository root different from the nearest .git "
                    "marker; inspection was refused."
                ),
                exit_code=discovery_code,
                is_error=True,
            )

        try:
            filter_code, filter_output, filter_error = await self._run_git(
                [
                    "config",
                    "--null",
                    "--includes",
                    "--get-regexp",
                    r"^filter\..*\.(clean|process)$",
                ],
                cwd=selected,
                env=env,
            )
        except _GitCommandTimeout:
            return self._format_result(
                workspace=workspace,
                selected=selected,
                repo_root=repo_root,
                operation=params.operation,
                result=(
                    "Git filter configuration inspection timed out; status/diff "
                    "was not run."
                ),
                exit_code=None,
                is_error=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._format_result(
                workspace=workspace,
                selected=selected,
                repo_root=repo_root,
                operation=params.operation,
                result=f"Git filter configuration inspection failed: {exc}",
                exit_code=None,
                is_error=True,
            )
        if filter_code not in (0, 1):
            detail = filter_error.strip() or "Git could not read filter configuration."
            return self._format_result(
                workspace=workspace,
                selected=selected,
                repo_root=repo_root,
                operation=params.operation,
                result=f"Git filter configuration inspection failed: {detail}",
                exit_code=filter_code,
                is_error=True,
            )
        try:
            executable_filters = _executable_filter_keys(filter_output)
        except ValueError as exc:
            return self._format_result(
                workspace=workspace,
                selected=selected,
                repo_root=repo_root,
                operation=params.operation,
                result=f"Git filter configuration could not be checked safely: {exc}",
                exit_code=None,
                is_error=True,
            )
        if executable_filters:
            filter_list = ", ".join(executable_filters)
            return self._format_result(
                workspace=workspace,
                selected=selected,
                repo_root=repo_root,
                operation=params.operation,
                result=(
                    "Configured Git clean/process filters can execute commands; "
                    f"inspection was refused for: {filter_list}."
                ),
                exit_code=None,
                is_error=True,
            )

        try:
            exit_code, stdout, stderr = await self._run_git(
                args,
                cwd=selected,
                env=env,
            )
        except _GitCommandTimeout:
            return self._format_result(
                workspace=workspace,
                selected=selected,
                repo_root=repo_root,
                operation=params.operation,
                result=f"Git command timed out ({GIT_COMMAND_TIMEOUT_SECONDS}s).",
                exit_code=None,
                is_error=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._format_result(
                workspace=workspace,
                selected=selected,
                repo_root=repo_root,
                operation=params.operation,
                result=f"Git command failed: {exc}",
                exit_code=None,
                is_error=True,
            )

        result = stdout.strip()
        if stderr.strip():
            result = f"{result}\n{stderr.strip()}" if result else stderr.strip()
        return self._format_result(
            workspace=workspace,
            selected=selected,
            repo_root=repo_root,
            operation=params.operation,
            result=result,
            exit_code=exit_code,
            is_error=exit_code != 0,
        )
