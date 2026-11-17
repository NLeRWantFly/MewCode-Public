"""Fixed .NET thread controls for the synthetic Windows payload."""

from pathlib import Path
import subprocess
import sys
import tempfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "native/windows/MewCode.SyntheticPayload.cs"
CSC = Path("C:/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe")
STEPS = ("DIRECT", "STA", "THREADPOOL", "TASK")


def test_source_uses_only_fixed_background_workers_and_bounded_signals() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    body = source.split("private sealed class ThreadStepSignal", 1)[1].split(
        "private static int ProbePowerShellInitialization(bool", 1
    )[0]
    assert 'args[0], "dotnet-threadpool"' in source
    assert "args.Length == 1" in source
    assert "private const int ThreadStepWaitMs = 2000;" in source
    assert "new ManualResetEvent(false)" in body
    assert "worker.IsBackground = true;" in body
    assert "worker.SetApartmentState(ApartmentState.STA);" in body
    assert "ThreadPool.QueueUserWorkItem(RunFixedThreadCallback, signal)" in body
    assert "Task.Factory.StartNew(" in body
    assert "TaskScheduler.Default" in body
    assert "signal.Ready.WaitOne(remaining)" in body
    assert "Thread.CurrentThread.IsBackground" in body
    assert "Thread.CurrentThread.GetApartmentState()" in body
    assert "signal.Ready.Set();" in body
    assert "Ready.Dispose" not in body and "Ready.Close" not in body
    assert "ex.HResult" in body and "DiagnosticTypeToken(ex.GetType())" in body
    for forbidden in (
        "System.Management.Automation", "RunspaceFactory", "File.", "Directory.",
        "TcpClient", "WebRequest",
        "Environment.Exit", "Thread.Abort", "ThreadPool.SetMinThreads",
        "ThreadPool.SetMaxThreads", "Process.Start", "Kill(",
    ):
        assert forbidden not in body


@pytest.fixture(scope="module")
def compiled_payload():
    if sys.platform != "win32" or not CSC.is_file():
        pytest.skip("Windows .NET Framework compiler is unavailable")
    with tempfile.TemporaryDirectory(prefix="mewcode-dotnet-threadpool-", dir=ROOT / ".tmp") as directory:
        executable = Path(directory) / "synthetic-payload.exe"
        command = [str(CSC), "/nologo", "/platform:x64", "/target:exe",
                   "/out:" + str(executable), str(SOURCE)]
        compiled = subprocess.run(command, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=30, check=False)
        assert compiled.returncode == 0, compiled.stdout + compiled.stderr
        assert executable.is_file()
        yield executable


def test_host_positive_control_all_four_callbacks_complete(compiled_payload: Path) -> None:
    completed = subprocess.run([str(compiled_payload), "dotnet-threadpool"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=10, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stderr == ""
    lines = set(completed.stdout.splitlines())
    assert "DOTNET_THREADPOOL_BEGIN" in lines
    assert "DOTNET_THREADPOOL_ALL_OK_TRUE" in lines
    assert "DOTNET_THREADPOOL_DONE" in lines
    for step in STEPS:
        prefix = "DOTNET_" + step + "_"
        for marker in ("ACCEPTED_TRUE", "SIGNALED_TRUE", "CALLBACK_COMPLETED_TRUE",
                       "BACKGROUND_TRUE", "EXCEPTION_TYPE_NONE", "EXCEPTION_HR_0", "OK_TRUE"):
            assert prefix + marker in lines
    assert "DOTNET_DIRECT_APARTMENT_MTA" in lines
    assert "DOTNET_STA_APARTMENT_STA" in lines


def test_diagnostic_mode_refuses_extra_arguments(compiled_payload: Path) -> None:
    completed = subprocess.run([str(compiled_payload), "dotnet-threadpool", "unexpected"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=3, check=False)
    assert completed.returncode == 2
    assert "DOTNET_THREADPOOL_BEGIN" not in completed.stdout
    assert completed.stderr.startswith("USAGE:")
