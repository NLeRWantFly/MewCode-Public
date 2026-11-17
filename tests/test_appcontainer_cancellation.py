from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import uuid

import pytest

from test_appcontainer_helper_validation import (
    _invoke_invalid_request,
    _request,
    helper_executable,
    synthetic_fixture,
)


_PREFIX = "MewCodeProbeCancel_"
_HELPER_SOURCE = (Path(__file__).resolve().parents[1] / "native/windows/MewCode.AppContainerProbe.cs").read_text(
    encoding="utf-8"
)


def _acl_snapshot(*paths: Path) -> tuple[bytes, ...]:
    system_root = Path(os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows")
    icacls = system_root / "System32" / "icacls.exe"
    snapshots = []
    for path in paths:
        completed = subprocess.run(
            [str(icacls), str(path)],
            capture_output=True,
            timeout=10,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        snapshots.append(completed.stdout)
    return tuple(snapshots)


def test_cancellation_checks_precede_profile_creation_and_thread_resume():
    execute = _HELPER_SOURCE.split("private static void Execute(ProbeRequest request", 1)[1].split(
        "private static ValidatedRequest ValidateRequest", 1
    )[0]
    guards = [
        index for index in range(len(execute))
        if execute.startswith("if (CancellationRequested(state.CancelEvent))", index)
    ]
    assert len(guards) == 2
    assert guards[0] < execute.index("CreateAppContainerProfile(")
    assert execute.index("VerifyChildAppContainerToken(") < guards[1] < execute.index("ResumeThread(")

    wait = _HELPER_SOURCE.split("private static bool CancellationRequested(IntPtr cancelEvent)", 1)[1].split(
        "private static void PrepareRuntimeCapabilities", 1
    )[0]
    assert "WaitForSingleObject(cancelEvent, 0)" in wait
    assert "if (wait == WaitFailed)" in wait
    assert 'throw LastWin32("WaitForSingleObject(cancel_event_name) failed.")' in wait
    assert "throw new InvalidOperationException(" in wait


@pytest.mark.parametrize(
    "name",
    [
        "",
        "Global\\" + _PREFIX + "0" * 32,
        "Local\\" + _PREFIX + "0" * 32,
        _PREFIX + "0" * 31,
        _PREFIX + "0" * 33,
        _PREFIX + "g" + "0" * 31,
        _PREFIX + "0" * 31 + "\\",
        "mewcodeProbeCancel_" + "0" * 32,
    ],
)
def test_rejects_invalid_cancel_event_name_before_profile_creation(
    helper_executable, synthetic_fixture, name
):
    receipt = _invoke_invalid_request(
        helper_executable,
        synthetic_fixture,
        _request(synthetic_fixture, cancel_event_name=name),
    )

    assert "cancel_event_name" in receipt["error"]
    assert receipt["cancelled"] is False
    assert receipt["job_cleanup_ok"] is True
    assert receipt["acl_cleanup_ok"] is True
    assert receipt["profile_cleanup_ok"] is True


def test_rejects_missing_but_well_formed_host_event_before_profile_creation(
    helper_executable, synthetic_fixture
):
    # No event is created here: this only verifies that the helper opens an
    # existing host event before it can create a profile or alter fixture ACLs.
    event_name = _PREFIX + uuid.uuid4().hex
    receipt = _invoke_invalid_request(
        helper_executable,
        synthetic_fixture,
        _request(synthetic_fixture, cancel_event_name=event_name),
    )

    assert "OpenEvent(cancel_event_name) failed" in receipt["error"]
    assert receipt["cancelled"] is False
    assert receipt["job_cleanup_ok"] is True
    assert receipt["acl_cleanup_ok"] is True
    assert receipt["profile_cleanup_ok"] is True


def test_presignaled_host_event_cancels_before_profile_or_acl_changes(
    helper_executable, synthetic_fixture
):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    name = _PREFIX + uuid.uuid4().hex
    event = kernel32.CreateEventW(None, True, False, name)
    if not event:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        assert ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
        assert kernel32.SetEvent(event), ctypes.WinError(ctypes.get_last_error())
        fixture = synthetic_fixture
        before_acl = _acl_snapshot(fixture["workspace"], fixture["executable"])
        request_path = fixture["root"] / f"presignaled-{uuid.uuid4().hex}.request.json"
        request_path.write_text(
            json.dumps(_request(fixture, cancel_event_name=name), ensure_ascii=True),
            encoding="utf-8",
        )

        completed = subprocess.run(
            [str(helper_executable), str(request_path)],
            capture_output=True,
            timeout=30,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        receipt = json.loads(completed.stdout.decode("utf-8-sig"))
        after_acl = _acl_snapshot(fixture["workspace"], fixture["executable"])

        assert completed.returncode != 0
        assert receipt["cancelled"] is True
        assert receipt["timed_out"] is False
        assert receipt.get("pid") is None
        assert receipt.get("profile_name") is None
        assert receipt["error"] == ""
        assert receipt["job_cleanup_ok"] is True
        assert receipt["acl_cleanup_ok"] is True
        assert receipt["profile_cleanup_ok"] is True
        assert after_acl == before_acl
    finally:
        assert kernel32.CloseHandle(event), ctypes.WinError(ctypes.get_last_error())
