"""Owned, reversible Win32 resources for explicit synthetic probes only.

Not imported by production execution. A DOS alias grants no filesystem rights;
it is a diagnostic for PowerShell's inaccessible-volume-root initialization.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import uuid


def _kernel():
    if os.name != "nt":
        raise RuntimeError("Synthetic Win32 resources require Windows")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateEventW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p)
    api.CreateEventW.restype = ctypes.c_void_p
    api.SetEvent.argtypes = (ctypes.c_void_p,)
    api.SetEvent.restype = ctypes.c_int
    api.CloseHandle.argtypes = (ctypes.c_void_p,)
    api.CloseHandle.restype = ctypes.c_int
    api.QueryDosDeviceW.argtypes = (ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_ulong)
    api.QueryDosDeviceW.restype = ctypes.c_ulong
    api.DefineDosDeviceW.argtypes = (ctypes.c_ulong, ctypes.c_wchar_p, ctypes.c_wchar_p)
    api.DefineDosDeviceW.restype = ctypes.c_int
    return api


class ProbeCancelEvent:
    """Host-owned manual-reset event; never opens/reuses an existing object."""

    def __init__(self):
        self.name = "MewCodeProbeCancel_" + uuid.uuid4().hex
        self._api = _kernel()
        self._handle = self._api.CreateEventW(None, True, False, self.name)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:
            self.close()
            raise RuntimeError("Cancellation event already exists; refusing to reuse it")

    def signal(self):
        if not self._handle:
            raise RuntimeError("Cancellation event is closed")
        if not self._api.SetEvent(self._handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self._handle:
            if not self._api.CloseHandle(self._handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *ignored):
        self.close()


class ProbeDriveAlias:
    """Map a free local drive to exactly this fresh fixture's workspace.

    Deletion always matches our full target, never removes by drive name alone.
    Existing mappings, physical drives and reparse-point targets are rejected.
    """

    def __init__(self, fixture: Path):
        from mewcode.sandbox.appcontainer_policy import _validate_fixture_root, _existing_path

        root = _validate_fixture_root(fixture)
        expected_parent = Path(__file__).resolve().parents[1] / ".tmp"
        if root.parent != expected_parent.resolve(strict=True):
            raise ValueError("DOS alias fixture must be a direct child of project .tmp")
        workspace = _existing_path(root / "workspace", "alias workspace", directory=True)
        if workspace.parent != root:
            raise ValueError("DOS alias target must be the exact fixture workspace")
        self.target = "\\??\\" + str(workspace)
        self._api = _kernel()
        self.drive = None
        self.cleanup_ok = False

    def _query(self, drive):
        buffer = ctypes.create_unicode_buffer(32768)
        size = self._api.QueryDosDeviceW(drive, buffer, len(buffer))
        if size:
            return buffer[:size].rstrip("\0").split("\0")
        error = ctypes.get_last_error()
        if error == 2:
            return []
        raise ctypes.WinError(error)

    def __enter__(self):
        for letter in reversed("GHIJKLMNOPQRSTUVWXYZ"):
            drive = letter + ":"
            if self._query(drive):
                continue
            if not self._api.DefineDosDeviceW(0x1 | 0x8, drive, self.target):
                raise ctypes.WinError(ctypes.get_last_error())
            self.drive = drive
            try:
                if self._query(drive) != [self.target]:
                    raise RuntimeError("DOS alias changed concurrently; refusing to use it")
            except BaseException:
                self.close()
                raise
            return self
        raise RuntimeError("No unused local drive letter available for the synthetic control")

    def close(self):
        if self.drive is None:
            return
        # RAW_TARGET_PATH | REMOVE_DEFINITION | EXACT_MATCH_ON_REMOVE |
        # NO_BROADCAST_SYSTEM: cannot delete another task's different mapping.
        if not self._api.DefineDosDeviceW(0x1 | 0x2 | 0x4 | 0x8, self.drive, self.target):
            raise ctypes.WinError(ctypes.get_last_error())
        self.cleanup_ok = self.target not in self._query(self.drive)
        self.drive = None
        if not self.cleanup_ok:
            raise RuntimeError("Our synthetic DOS alias remains after exact-target removal")

    def __exit__(self, *ignored):
        self.close()
