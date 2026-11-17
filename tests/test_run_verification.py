from __future__ import annotations

import subprocess
import sys

import pytest

from mewcode.tools.run_verification import (
    MAX_OUTPUT_CHARS,
    RunVerification,
    check_command,
)


def _subprocess_available() -> bool:
    try:
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


SUBPROCESS_AVAILABLE = _subprocess_available()


@pytest.mark.parametrize(
    "command",
    [
        "pytest",
        "pytest tests/test_x.py -q",
        "python -m pytest",
        "python.exe -m pytest tests/",
        "python -m unittest",
        "npm test",
        "npm run test",
        "cargo test",
        "go test ./...",
        "git diff",
        "git status",
        "git status --short",
        "git log --oneline -5",
        "ruff check .",
        "flake8 mewcode",
        "eslint src",
        "tsc --noEmit",
        "mypy mewcode",
        "uv run pytest",
    ],
)
def test_whitelisted_commands_are_allowed(command) -> None:
    allowed, label = check_command(command)
    assert allowed is True
    assert label


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "git commit -m x",
        "git push",
        "pip install requests",
        "python -c 'print(1)'",
        "echo hi",
        "pytest; rm -rf /",
        "git diff; rm -rf /",
        "git status && git push",
        "git diff | bash",
        "pytest $(rm -rf /)",
        "pytest" + chr(96) + "whoami" + chr(96),
        "",
        "python -m pytesty",
    ],
)
def test_dangerous_or_unknown_commands_are_rejected(command) -> None:
    allowed, detail = check_command(command)
    assert allowed is False
    assert detail in (
        "命令含 shell 危险字符",
        "命令不在校验白名单内",
        "命令为空",
    )


@pytest.mark.asyncio
async def test_tool_refuses_non_whitelisted_without_executing() -> None:
    tool = RunVerification()
    result = await tool.execute(
        tool.params_model(command="git push origin main")
    )
    assert result.is_error is True
    assert "拒绝执行" in result.output
    assert "不在校验白名单内" in result.output


@pytest.mark.asyncio
async def test_tool_refuses_shell_metacharacters() -> None:
    tool = RunVerification()
    result = await tool.execute(
        tool.params_model(command="git diff; rm -rf /")
    )
    assert result.is_error is True
    assert "shell 危险字符" in result.output


@pytest.mark.skipif(
    not SUBPROCESS_AVAILABLE, reason="sandbox denies piped subprocesses"
)
@pytest.mark.asyncio
async def test_tool_runs_whitelisted_command_and_returns_evidence() -> None:
    tool = RunVerification()
    result = await tool.execute(tool.params_model(command="git status --short"))
    assert result.is_error is False
    assert "退出码 0" in result.output
    assert "[git status]" in result.output


def test_max_output_chars_is_bounded() -> None:
    assert MAX_OUTPUT_CHARS == 12000
