"""Local command compatibility and cancellation, without models or network."""
from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, Mock

import pytest

import mewcode.tools.run_verification as runner
from mewcode.sandbox.receipt import NativeExecutionReceipt
from mewcode.tools.command_sandbox import native_auto_command


@pytest.mark.parametrize("executable", [
    r".\.venv\Scripts\python.exe", ".venv/Scripts/python.exe",
    r'".\.venv\Scripts\python.exe"', r"& '.\.venv\Scripts\python.exe'",
])
def test_windows_venv_interpreter_and_quoted_arguments(executable):
    argv, label = runner.verification_argv(
        executable + r' -m pytest "tests\my cases\test_local.py"', windows=True,
    )
    assert argv[0].replace("\\", "/").removeprefix("./") == ".venv/Scripts/python.exe"
    assert argv[1:] == ["-m", "pytest", r"tests\my cases\test_local.py"]
    assert label == "python -m pytest"


def test_posix_venv_and_spaces_are_preserved():
    argv, label = runner.verification_argv(
        './.venv/bin/python -m unittest "tests/my cases/test_local.py"', windows=False,
    )
    assert argv == ["./.venv/bin/python", "-m", "unittest", "tests/my cases/test_local.py"]
    assert label == "python -m unittest"


@pytest.mark.parametrize("windows", [True, False])
def test_pytest_config_flag_is_not_an_interpreter_inline_flag(windows):
    argv, _ = runner.verification_argv("python -m pytest -c pytest.ini", windows=windows)
    assert argv[-2:] == ["-c", "pytest.ini"]


@pytest.mark.parametrize("command", [
    r"..\.venv\Scripts\python.exe -m pytest",
    r"C:\unrelated\python.exe -m pytest",
    r".\.venv\Scripts\other.exe -m pytest",
    r".\.venv\Scripts\python.exe -m pytesty",
    r"& '.\.venv\Scripts\python.exe' -m pytest ; Write-Output extra",
    r"& '.\.venv\Scripts\python.exe' -m pytest | other",
    r"& '.\.venv\Scripts\python.exe' -m pytest $(extra)",
    r'pytest "unclosed',
])
def test_new_path_spelling_does_not_change_allowed_command_shape(command):
    with pytest.raises(ValueError):
        runner.verification_argv(command, windows=True)


@pytest.mark.asyncio
async def test_direct_exec_retains_windows_paths_and_exit_receipt(tmp_path, monkeypatch):
    command = r'.\.venv\Scripts\python.exe -m pytest "tests\my cases.py"' if os.name == "nt" else \
        './.venv/bin/python -m pytest "tests/my cases.py"'
    proc = Mock(returncode=3)
    proc.communicate = AsyncMock(return_value=(b"a test failed", None))
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", spawn)
    result = await runner.RunVerification().run(
        runner.RunVerificationParams(command=command), work_dir=str(tmp_path),
    )
    assert result.is_error
    payload = json.loads(result.output)
    assert payload["exit_code"] == 3 and payload["status"] == "FAIL"
    assert payload["output"] == "a test failed"
    args, options = spawn.call_args
    assert args[0].startswith(str(tmp_path))
    assert args[1:3] == ("-m", "pytest")
    assert args[-1] == (r"tests\my cases.py" if os.name == "nt" else "tests/my cases.py")
    assert options["start_new_session"] is (os.name != "nt")
    assert "shell" not in options


@pytest.mark.asyncio
async def test_timeout_requests_cleanup_of_its_owned_process(monkeypatch):
    proc = Mock(returncode=None)
    proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    cleanup = AsyncMock(return_value=False)
    monkeypatch.setattr(runner, "_terminate_process_tree", cleanup)
    result = await runner.RunVerification().execute(runner.RunVerificationParams(command="pytest"))
    assert result.is_error and "超时" in result.output
    assert "已停止" not in result.output
    cleanup.assert_awaited_once_with(proc)


@pytest.mark.asyncio
async def test_cancellation_waits_for_cleanup_and_propagates(monkeypatch):
    started, cleanup_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def communicate():
        started.set()
        await asyncio.Event().wait()
    async def stop(proc):
        cleanup_started.set()
        await release.wait()
        return True
    proc = Mock(returncode=None, communicate=communicate)
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    cleanup = AsyncMock(side_effect=stop)
    monkeypatch.setattr(runner, "_terminate_process_tree", cleanup)
    task = asyncio.create_task(runner.RunVerification().execute(runner.RunVerificationParams(command="pytest")))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        cleanup.assert_awaited_once_with(proc)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_native_receipt_reports_backend_and_actual_execution_status(monkeypatch):
    receipt = NativeExecutionReceipt("denied", detail="not yet enabled", backend="native-candidate")
    native = AsyncMock(return_value=receipt)
    spawn = AsyncMock()
    monkeypatch.setattr("mewcode.tools.command_sandbox.execute_native_command", native)
    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", spawn)
    with native_auto_command():
        result = await runner.RunVerification().execute(runner.RunVerificationParams(command="pytest"))
    payload = json.loads(result.output)
    assert result.is_error and payload["status"] == "FAIL"
    assert payload["isolation"] == "native-candidate"
    assert payload["execution_status"] == "denied" and payload["exit_code"] is None
    assert payload["diagnostic_only"] is False
    native.assert_awaited_once()
    spawn.assert_not_awaited()
