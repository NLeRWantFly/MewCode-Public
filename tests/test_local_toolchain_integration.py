"""Opt-in local compatibility tests; no model calls, listeners or network.

Creates only a new pytest fixture with a pip-free venv and an empty Git repo.
These exercise host tool compatibility, NOT production AUTO enablement.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import venv
from pathlib import Path

import pytest

from mewcode.tools.bash import Bash, Params
from mewcode.tools.run_verification import RunVerification, RunVerificationParams


pytestmark = pytest.mark.skipif(
    os.environ.get("MEWCODE_TEST_LOCAL_TOOLCHAIN") != "1",
    reason="explicit local toolchain compatibility test",
)


@pytest.fixture(scope="module")
def local_project(tmp_path_factory):
    root = tmp_path_factory.mktemp("local-toolchain") / "中文 project"
    root.mkdir()
    venv.EnvBuilder(with_pip=False).create(root / ".venv")
    (root / "test_local.py").write_text(
        "import pathlib, sys, unittest\n"
        "class LocalTest(unittest.TestCase):\n"
        "    def test_runtime(self):\n"
        "        self.assertEqual(pathlib.Path(sys.prefix).resolve(), pathlib.Path('.venv').resolve())\n"
        "        self.assertTrue(pathlib.Path('input.txt').is_file())\n"
        "        pathlib.Path('output.txt').write_text('local-ok', encoding='utf-8')\n",
        encoding="utf-8",
    )
    (root / "input.txt").write_text("local fixture", encoding="utf-8")
    return root


@pytest.mark.asyncio
@pytest.mark.parametrize("bare", [True, False])
async def test_verification_uses_project_interpreter(local_project, bare):
    executable = "python" if bare else (
        r".\.venv\Scripts\python.exe" if os.name == "nt" else "./.venv/bin/python"
    )
    result = await RunVerification().run(
        RunVerificationParams(command=f'{executable} -m unittest -q test_local'),
        work_dir=str(local_project),
    )
    assert not result.is_error, result.output
    assert json.loads(result.output)["exit_code"] == 0
    assert (local_project / "output.txt").read_text(encoding="utf-8") == "local-ok"


@pytest.mark.asyncio
async def test_git_status_on_fresh_local_repository(local_project):
    git = shutil.which("git")
    if not git:
        pytest.skip("Git executable is not installed")
    subprocess.run([git, "init", "--quiet", str(local_project)],
        check=True, capture_output=True, timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    result = await RunVerification().run(
        RunVerificationParams(command="git status --short"), work_dir=str(local_project),
    )
    assert not result.is_error, result.output
    assert "test_local.py" in json.loads(result.output)["output"]


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell compatibility")
async def test_powershell_invokes_venv_in_unicode_working_directory(local_project):
    result = await Bash().run(
        Params(command=r"& '.\.venv\Scripts\python.exe' -m unittest -q test_local", timeout=20),
        work_dir=str(local_project),
    )
    assert not result.is_error, result.output
    assert "EXIT CODE: 0" in result.output


@pytest.mark.asyncio
async def test_cancel_verification_stops_owned_test_process(tmp_path):
    # A standard unittest child records its PID, then blocks. Only that exact
    # tool-owned process tree is stopped; there is no process-name matching.
    (tmp_path / "test_wait.py").write_text(
        "import os, pathlib, time, unittest\n"
        "class Wait(unittest.TestCase):\n"
        "    def test_wait(self):\n"
        "        pathlib.Path('started.pid').write_text(str(os.getpid()))\n"
        "        time.sleep(30)\n",
        encoding="utf-8",
    )
    task = asyncio.create_task(RunVerification().run(
        RunVerificationParams(command="python -m unittest -q test_wait"), work_dir=str(tmp_path),
    ))
    try:
        for _ in range(100):
            if (tmp_path / "started.pid").exists() or task.done():
                break
            await asyncio.sleep(.05)
        assert (tmp_path / "started.pid").exists(), "owned unittest process did not start"
        pid = int((tmp_path / "started.pid").read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=12)
        if os.name == "nt":
            import ctypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            handle = kernel.OpenProcess(0x1000, False, pid)
            if handle:
                code = ctypes.c_ulong()
                try:
                    assert kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
                    assert code.value != 259, "owned verification process remained alive"
                finally:
                    kernel.CloseHandle(ctypes.c_void_p(handle))
        else:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
