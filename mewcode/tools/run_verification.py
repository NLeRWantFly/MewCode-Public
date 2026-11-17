"""Whitelisted verification-command runner for the auditor role.

The auditor may run real tests and read-only checks, but never arbitrary
commands. Two boundaries protect it: shell metacharacters are refused, and the
remaining command must start with a fixed whitelist prefix followed by a word
boundary. Anything else is refused without execution.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult, active_work_dir
from mewcode.tools.bash import _child_env, _terminate_process_tree

# Lowercase command prefixes with a word-boundary rule: the character after
# the prefix must be whitespace or end-of-string, so "python -m pytesty" never
# rides in behind "python -m pytest".
_WHITELIST: tuple[tuple[str, str], ...] = (
    ("pytest", "pytest"),
    ("python -m pytest", "python -m pytest"),
    ("python.exe -m pytest", "python -m pytest"),
    ("python -m unittest", "python -m unittest"),
    ("python.exe -m unittest", "python -m unittest"),
    ("npm test", "npm test"),
    ("npm run test", "npm run test"),
    ("yarn test", "yarn test"),
    ("pnpm test", "pnpm test"),
    ("cargo test", "cargo test"),
    ("go test", "go test"),
    ("git diff", "git diff"),
    ("git status", "git status"),
    ("git log", "git log"),
    ("ruff", "ruff"),
    ("flake8", "flake8"),
    ("eslint", "eslint"),
    ("tsc", "tsc"),
    ("mypy", "mypy"),
    ("uv run pytest", "uv run pytest"),
)

# Backslashes are Windows path separators, not command separators. Parsing
# and the single-command check below preserve them instead of using POSIX
# shlex (which silently turns .\\.venv\\Scripts\\python.exe into another name).
_SHELL_METACHARS = frozenset(";&|<>" + chr(96) + "$%" + chr(10) + chr(13))

MAX_OUTPUT_CHARS = 12000
COMMAND_TIMEOUT_SECONDS = 300


def verification_argv(command: str, *, windows: bool | None = None) -> tuple[list[str], str]:
    """Parse exactly one verification invocation without invoking a shell.

    Only the project's conventional relative venv interpreter is an additional
    executable spelling; arbitrary paths ending in python.exe are not aliases.
    This validates invocation shape, not project code or execution permissions.
    """
    from mewcode.permissions.command_rules import parse_command

    windows = os.name == "nt" if windows is None else windows
    stripped = (command or "").strip()
    if not stripped:
        raise ValueError("命令为空")
    # PowerShell uses & to invoke a quoted executable. Accept just that one
    # leading operator; the rest must still be a single literal invocation.
    if windows and stripped.startswith("& "):
        stripped = stripped[2:].lstrip()
    if any(char in _SHELL_METACHARS for char in stripped):
        raise ValueError("命令含 shell 危险字符")
    parsed = parse_command(stripped, shell="powershell" if windows else "bash")
    if (not parsed.parseable or len(parsed.segments) != 1
            or parsed.segments[0].redirections):
        raise ValueError("命令含 shell 危险字符")
    argv = list(parsed.segments[0].arguments)
    if not argv:
        raise ValueError("命令为空")
    normalized = [value.lower() for value in argv]
    executable = normalized[0].replace("\\", "/") if windows else normalized[0]
    if executable.startswith("./"):
        executable = executable[2:]
    venv_names = {".venv/scripts/python.exe"} if windows else {".venv/bin/python", ".venv/bin/python3"}
    if executable in venv_names:
        normalized[0] = "python"
    for prefix, label in _WHITELIST:
        words = prefix.split()
        if normalized[:len(words)] == words:
            # Once Python has consumed '-m pytest/unittest', flags such as
            # pytest's '-c config.ini' belong to that module, not Python.
            module_flags = (words[:2] in (["python", "-m"], ["python.exe", "-m"])
                            and set(parsed.opaque_reasons) == {"interpreter inline or stdin payload"})
            if parsed.opaque and not module_flags:
                raise ValueError("命令含 shell 危险字符")
            return argv, label
    raise ValueError("命令不在校验白名单内")


def check_command(command: str) -> tuple[bool, str]:
    """Return (allowed, detail): the whitelist label or the refusal reason."""
    try:
        _, label = verification_argv(command)
        return True, label
    except ValueError as exc:
        return False, str(exc)


class RunVerificationParams(BaseModel):
    network: Literal["inherit", "offline"] = Field(default="inherit", description=(
        "offline runs local checks in a fresh disposable native copy with no network access; "
        "independent private-copy cleanup does not hold up these checks."))
    command: str = Field(
        description=(
            "A whitelisted verification command, e.g. pytest tests/test_x.py "
            "or git diff. Shell metacharacters are refused."
        )
    )


class RunVerification(Tool):
    """Run a whitelisted verification command and return real evidence."""

    name = "RunVerification"
    description = (
        "Run a verification command from a fixed whitelist (test runners, "
        "linters, git diff/status/log) and return its exit code and output "
        "as audit evidence. Arbitrary or shell-injected commands are refused."
    )
    params_model = RunVerificationParams
    # Test runners execute project code and can modify files or access networks.
    category = "command"
    is_concurrency_safe = False

    async def execute(self, params: RunVerificationParams) -> ToolResult:
        allowed, detail = check_command(params.command)
        if not allowed:
            return ToolResult(
                output=(
                    f"拒绝执行：{detail}。只允许测试、静态检查与 git 只读命令。"
                ),
                is_error=True,
            )
        from mewcode.tools.command_sandbox import (
            native_auto_scope, execute_native_command, sandbox_scope, run_isolated_command,
        )
        if native_auto_scope():
            from mewcode.tools.command_sandbox import disposable_native_command, native_receipt_observation
            with disposable_native_command(offline=params.network == "offline"):
                native = await execute_native_command(
                # This branch is a single validated native executable call.
                # PowerShell otherwise folds arbitrary nonzero exit codes to 1.
                    params.command + "; exit $LASTEXITCODE", work_dir=active_work_dir(), timeout=COMMAND_TIMEOUT_SECONDS,
                )
            return ToolResult(output=json.dumps({
                "command": params.command, "cwd": active_work_dir(),
                "status": "FAIL" if native.is_error else "PASS",
                "isolation": native.backend, "summary": detail,
                "execution_status": native.status, "exit_code": native.exit_code,
                "diagnostic_only": native.diagnostic_only, "output": native.render(),
            }, ensure_ascii=False), is_error=native.is_error, host_observation=native_receipt_observation(native))
        if params.network == "offline":
            return ToolResult(output="Offline verification requires the native disposable boundary; command not started.",
                is_error=True, host_observation={"command_execution": {"command_started": False, "status": "denied"}})
        if sandbox_scope() is not False:
            isolated = await run_isolated_command(
                params.command, work_dir=active_work_dir(), timeout=COMMAND_TIMEOUT_SECONDS,
            )
            return ToolResult(output=json.dumps({
                "command": params.command, "cwd": active_work_dir(),
                "status": "FAIL" if isolated.is_error else "PASS",
                "isolation": "docker", "summary": detail,
                "output": isolated.output,
            }, ensure_ascii=False), is_error=isolated.is_error)
        label = detail
        try:
            argv, _ = verification_argv(params.command)
        except ValueError as exc:
            return ToolResult(
                output=f"拒绝执行：命令解析失败（{exc}）", is_error=True
            )
        # Direct exec retains ownership of the actual process. A cancelled
        # run_in_executor future cannot stop subprocess.run's worker thread.
        work_dir = active_work_dir()
        env = _child_env(work_dir)
        if work_dir and ("/" in argv[0] or "\\" in argv[0]):
            executable = Path(argv[0])
            if not executable.is_absolute():
                argv[0] = str(Path(work_dir) / executable)
        else:
            # Windows executable lookup need not use the child's PATH from
            # CreateProcess's environment block. Resolve against our venv-first
            # PATH explicitly, not a global Python or WindowsApps alias.
            argv[0] = shutil.which(argv[0], path=env.get("PATH")) or argv[0]
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=work_dir,
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=COMMAND_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            if proc is not None:
                await _terminate_process_tree(proc)
            return ToolResult(output=f"校验命令执行超时（{COMMAND_TIMEOUT_SECONDS}s）；已请求进程清理。", is_error=True)
        except asyncio.CancelledError:
            if proc is not None:
                await _terminate_process_tree(proc)
            raise
        except Exception as exc:
            return ToolResult(output=f"校验命令执行失败: {exc}", is_error=True)
        assert proc is not None
        output = (stdout or b"").decode(errors="replace")
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + chr(10) + "…(输出已截断)"
        return ToolResult(
            output=json.dumps({"command": params.command, "cwd": work_dir,
                "exit_code": proc.returncode,
                "status": "PASS" if proc.returncode == 0 else "FAIL",
                "summary": f"[{label}] 退出码 {proc.returncode}",
                "output": output or "(无输出)"}, ensure_ascii=False),
            is_error=proc.returncode != 0,
        )
