"""Read-only boundary checks for the native WCT probe."""

import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "native" / "windows" / "MewCode.WaitChainProbe.cs"
EXE = ROOT / ".tmp" / "mewcode-waitchain.exe"


def _probe(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(EXE), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=4,
        check=False,
    )


def _json_output(result: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(result.stdout)


def test_source_uses_async_wct_with_bounded_scope() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    assert "AsyncOpenFlag = 0x1" in source
    assert "OpenThreadWaitChainSession(AsyncOpenFlag, CallbackRoot)" in source
    assert "PerThreadWaitMs = 2000" in source
    assert "MaxThreads = 8" in source
    assert "WaitOne(waitMs)" in source
    assert "CloseThreadWaitChainSession(session)" in source
    assert "Do not follow or enumerate threads in other processes." in source
    for forbidden in (
        "ReadProcessMemory",
        "WriteProcessMemory",
        "AdjustTokenPrivileges",
        "RegSetValue",
        "TerminateProcess",
    ):
        assert forbidden not in source


@pytest.mark.skipif(not EXE.is_file(), reason="compile the probe to .tmp/mewcode-waitchain.exe first")
def test_invalid_arguments_are_rejected_before_wct() -> None:
    for arguments in ((), ("--pid", "nope"), ("--pid", "0"), ("--pid", "12", "--unknown")):
        result = _probe(*arguments)
        payload = _json_output(result)
        assert result.returncode == 2
        assert payload["ok"] is False
        assert payload["error"]["code"] == "invalid_arguments"
        assert "threads" not in payload


@pytest.mark.skipif(not EXE.is_file(), reason="compile the probe to .tmp/mewcode-waitchain.exe first")
def test_unavailable_or_mismatched_pid_is_rejected_without_wait_chain_query() -> None:
    result = _probe("--pid", "2147483647", "--expected-process-name", "__mewcode_no_match__")
    payload = _json_output(result)
    assert result.returncode == 3
    assert payload["ok"] is False
    assert payload["error"]["code"] in {"process_not_found", "process_name_mismatch"}
    assert "threads" not in payload


@pytest.mark.skipif(not EXE.is_file(), reason="compile the probe to .tmp/mewcode-waitchain.exe first")
def test_unsafe_expected_process_name_is_rejected() -> None:
    result = _probe("--pid", "12", "--expected-process-name", "..\\powershell")
    payload = _json_output(result)
    assert result.returncode == 2
    assert payload["error"]["code"] == "invalid_arguments"
    assert "threads" not in payload
