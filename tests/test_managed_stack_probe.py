"""Guard the read-only CLR observer without attaching to any live process."""

import json
from pathlib import Path
import subprocess
import tempfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "native/windows/MewCode.ManagedStackProbe.cs"
PROBE = ROOT / ".tmp/clrmd-probe-1.1.142101/MewCode.ManagedStackProbe.exe"


def _run(*args: str) -> tuple[int, dict]:
    if not PROBE.is_file():
        pytest.skip("Local ClrMD diagnostic binary has not been compiled")
    completed = subprocess.run(
        [str(PROBE), *args], cwd=ROOT, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=3,
        check=False,
    )
    return completed.returncode, json.loads(completed.stdout)


def test_source_only_uses_passive_local_dac_and_bounded_method_names() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    assert "DataTarget.AttachToProcess(pid, 3000, AttachFlag.Passive)" in source
    assert "CreateRuntime(dacPath, false)" in source
    assert 'target.SymbolLocator.SymbolPath = packageRoot;' in source
    assert '"Framework64", "v4.0.30319", "mscordacwks.dll"' in source
    assert "private const int MaxThreads = 24;" in source
    assert "private const int MaxFrames = 32;" in source
    assert "private const int WatchdogMs = 8000;" in source
    assert "if (framesSeen++ >= MaxFrames) break;" in source
    assert "method.Type.Name" in source and "method.Name" in source
    assert source.index("ValidateFixture(fixture") < source.index("Process.GetProcessById(pid)")
    for forbidden in (
        "AttachFlag.Invasive", "AttachFlag.NonInvasive", "CreateSnapshotAndAttach",
        "ReadProcessMemory", "EnumerateStackObjects", "GetHeap(",
        "CurrentException", "SuspendThread", "TerminateProcess", "DebugActiveProcess",
        "GetCommandLineArgs", "GetEnvironmentVariables", "GetFullSignature",
    ):
        assert forbidden not in source


@pytest.mark.parametrize(
    "arguments",
    [
        (),
        ("--pid", "0", "--fixture", "C:\\Windows"),
        ("--pid", "2147483647", "--fixture", "C:\\Windows"),
        ("--pid", "2147483647", "--fixture", "..\\mewcode-lpac-probe-forged"),
    ],
)
def test_refuses_invalid_arguments_and_nonfixture_paths_before_attach(arguments: tuple[str, ...]) -> None:
    exit_code, report = _run(*arguments)
    assert exit_code == 2
    assert report["status"] == "invalid_input"
    assert report["diagnostic_only"] is True
    assert report["threads"] == []


def test_refuses_forged_sentinel_before_process_lookup() -> None:
    with tempfile.TemporaryDirectory(prefix="mewcode-lpac-probe-", dir=ROOT / ".tmp") as directory:
        fixture = Path(directory)
        (fixture / "workspace").mkdir()
        (fixture / ".mewcode-synthetic-fixture").write_bytes(b"forged\n")
        exit_code, report = _run("--pid", "2147483647", "--fixture", str(fixture))

    assert exit_code == 2
    assert report["status"] == "invalid_input"
    assert report["reason"] == "fixture_rejected"
    assert report["threads"] == []
