import asyncio
import os
import shlex
import sys
from unittest.mock import Mock

import pytest

from mewcode.tools.bash import Bash, Params
import mewcode.tools.bash as bash_module


@pytest.mark.asyncio
async def test_cleanup_wait_is_bounded_even_when_process_will_not_exit(monkeypatch):
    proc = Mock(pid=987654321, returncode=None)
    monkeypatch.setattr(bash_module, "PROCESS_CLEANUP_TIMEOUT", .01)
    # Do not signal any real PID in this defensive failure-path test.
    if os.name == "nt":
        monkeypatch.setattr(bash_module.subprocess, "run", Mock(side_effect=OSError("test")))
    else:
        monkeypatch.setattr(bash_module.os, "killpg", Mock(side_effect=OSError("test")))
    async def forever():
        await asyncio.Event().wait()

    proc.wait = forever
    confirmed = await asyncio.wait_for(bash_module._terminate_process_tree(proc), timeout=1)
    assert confirmed is False
    proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_failed_windows_tree_cleanup_is_not_reported_as_confirmed(monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows taskkill result")
    proc = Mock(pid=987654321, returncode=None)
    async def exited():
        return 1
    proc.wait = exited
    monkeypatch.setattr(
        bash_module.subprocess, "run", Mock(return_value=Mock(returncode=1, stderr=b"denied")),
    )
    assert await bash_module._terminate_process_tree(proc) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_shell_timeout_and_cancel_stop_its_real_child(tmp_path, cancel):
    # The child's delayed file is a sentinel: it must never be written after
    # the tool has stopped. Record PID for a read-only OS liveness check.
    script = tmp_path / "child.py"
    script.write_text("import os, pathlib, time\n"
                      "pathlib.Path('child.pid').write_text(str(os.getpid()))\n"
                      "time.sleep(30)\n"
                      "pathlib.Path('late.txt').write_text('should not run')\n", encoding="utf-8")
    if os.name == "nt":
        command = "& '" + sys.executable.replace("'", "''") + "' '" + str(script).replace("'", "''") + "'"
    else:
        command = shlex.join([sys.executable, str(script)])
    task = asyncio.create_task(Bash().run(Params(command=command, timeout=30 if cancel else 2), work_dir=str(tmp_path)))
    pid_file = tmp_path / "child.pid"
    for _ in range(100):
        if pid_file.exists() or task.done():
            break
        await asyncio.sleep(.05)
    assert pid_file.exists(), "The test child did not start"
    pid = int(pid_file.read_text())
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=12)
    else:
        result = await asyncio.wait_for(task, timeout=12)
        assert result.is_error and "timed out" in result.output
    if os.name == "nt":
        # Query by exact test PID; no broad process-name matching or killing.
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if handle:
            code = ctypes.c_ulong()
            try:
                assert kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
                assert code.value != 259, "Child still active after shell stopped"
            finally:
                kernel.CloseHandle(ctypes.c_void_p(handle))
    else:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert not (tmp_path / "late.txt").exists()
