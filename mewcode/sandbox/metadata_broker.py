"""Host-owned, handle-only connection to the bounded elevated metadata helper.

The helper cannot execute commands. Only the publisher enters a transaction
scope, and only its private replacement objects may receive preserved SACLs.
Ordinary tools never receive this connection or the host's handles.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import base64
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import secrets
import struct
import subprocess
import threading
import time

from mewcode.sandbox.windows import NativeBoundaryUnavailable

CURRENT: ContextVar["Broker | None"] = ContextVar("metadata_broker", default=None)
MAX_FRAME = 256 * 1024
SESSION_SECONDS = 1200
_SESSION = None
_LOCK = threading.RLock()


def installed_artifact(helper: Path):
    """Optional on old bundles; declared capability must be complete and exact."""
    from mewcode.sandbox.lpac_runtime import checked_path, validate_bundle_files
    manifest = helper.parent / "manifest.json"
    if not manifest.exists():
        return None
    manifest = checked_path(manifest, file=True)
    if manifest.stat().st_size > 16384:
        raise BrokerError("Native bundle manifest exceeds bound")
    data = json.loads(manifest.read_text(encoding="utf-8"), object_pairs_hook=_unique)
    if "mewcode-metadata-broker-v1" not in data.get("protocols", []):
        return None
    validate_bundle_files(helper.parent, data.get("files"))
    relative = "broker/MewCode.MetadataBroker.exe"
    digest = data.get("files", {}).get(relative)
    if not isinstance(digest, str):
        raise BrokerError("Native bundle lacks its declared metadata broker")
    return checked_path(helper.parent / relative, file=True), digest


class BrokerError(NativeBoundaryUnavailable):
    pass


class MetadataBrokerElevationRequired(BrokerError):
    """The host can discard computations, but cannot bypass Windows UAC."""
    error_code = "metadata_elevation_required"


class _SecurityAttributes(C.Structure):
    _fields_ = [("length", W.DWORD), ("descriptor", C.c_void_p), ("inherit", W.BOOL)]


class _ShellExecuteInfo(C.Structure):
    _fields_ = [("size", W.DWORD), ("mask", W.ULONG), ("window", W.HWND),
                ("verb", W.LPCWSTR), ("file", W.LPCWSTR), ("parameters", W.LPCWSTR),
                ("directory", W.LPCWSTR), ("show", C.c_int), ("instance", W.HINSTANCE),
                ("id_list", C.c_void_p), ("class_name", W.LPCWSTR), ("class_key", W.HKEY),
                ("hot_key", W.DWORD), ("icon", W.HANDLE), ("process", W.HANDLE)]


def _kernel():
    if os.name != "nt":
        raise BrokerError("The metadata broker requires Windows")
    k = C.WinDLL("kernel32", use_last_error=True)
    functions = {
        "CreateNamedPipeW": ([W.LPCWSTR, W.DWORD, W.DWORD, W.DWORD, W.DWORD, W.DWORD,
                              W.DWORD, C.POINTER(_SecurityAttributes)], W.HANDLE),
        "ConnectNamedPipe": ([W.HANDLE, C.c_void_p], W.BOOL),
        "GetNamedPipeClientProcessId": ([W.HANDLE, C.POINTER(W.ULONG)], W.BOOL),
        "ReadFile": ([W.HANDLE, C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.c_void_p], W.BOOL),
        "WriteFile": ([W.HANDLE, C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.c_void_p], W.BOOL),
        "CloseHandle": ([W.HANDLE], W.BOOL),
        "GetProcessId": ([W.HANDLE], W.DWORD),
        "WaitForSingleObject": ([W.HANDLE, W.DWORD], W.DWORD),
        "LocalFree": ([C.c_void_p], C.c_void_p),
    }
    for name, (args, result) in functions.items():
        getattr(k, name).argtypes, getattr(k, name).restype = args, result
    return k


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BrokerError("Duplicate broker response field")
        result[key] = value
    return result


class Broker:
    """One authenticated parent process, bounded lifetime, sequential scopes."""
    def __init__(self, executable: Path, expected_sha256: str):
        from mewcode.sandbox.publication_io import PinnedPath
        self.pipe = self.process = None
        self.pin = None
        self.sequence = 0
        self.started = time.monotonic()
        self.nonce = secrets.token_hex(32)
        self.scope_active = False
        self.executable = executable
        self.k = _kernel()
        try:
            self.pin = PinnedPath(executable)
            if self.pin.digest(16 * 1024 * 1024) != expected_sha256:
                raise BrokerError("Metadata broker bytes do not match the installed manifest")
            name = "MewCode.Metadata." + secrets.token_hex(24)
            descriptor = C.c_void_p()
            api = C.WinDLL("advapi32", use_last_error=True)
            convert = api.ConvertStringSecurityDescriptorToSecurityDescriptorW
            convert.argtypes = [W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.c_void_p]
            convert.restype = W.BOOL
            if not convert("D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;OW)", 1, C.byref(descriptor), None):
                raise C.WinError(C.get_last_error())
            try:
                sa = _SecurityAttributes(C.sizeof(_SecurityAttributes), descriptor, False)
                # First instance, byte mode, nonblocking, reject remote clients.
                self.pipe = self.k.CreateNamedPipeW("\\\\.\\pipe\\" + name, 3 | 0x80000,
                    1 | 8, 1, MAX_FRAME + 4, MAX_FRAME + 4, 0, C.byref(sa))
                if self.pipe == C.c_void_p(-1).value:
                    self.pipe = None
                    raise C.WinError(C.get_last_error())
            finally:
                self.k.LocalFree(descriptor)
            params = subprocess.list2cmdline(["--pipe", name, "--parent", str(os.getpid()), "--nonce", self.nonce])
            info = _ShellExecuteInfo()
            info.size, info.mask, info.verb, info.file = C.sizeof(info), 0x40 | 0x100, "runas", str(executable)
            info.parameters, info.show = params, 0
            shell = C.WinDLL("shell32", use_last_error=True)
            shell.ShellExecuteExW.argtypes, shell.ShellExecuteExW.restype = [C.POINTER(_ShellExecuteInfo)], W.BOOL
            if not shell.ShellExecuteExW(C.byref(info)):
                raise MetadataBrokerElevationRequired(
                    "Metadata publisher elevation was not granted. Use disposable execution for checks or "
                    "temporary computation; persistent publication requires Windows authorization. "
                    "Do not repeatedly request elevation.") from C.WinError(C.get_last_error())
            self.process = info.process
            # UAC waiting time is not part of the helper's process lifetime.
            self.started = time.monotonic()
            expected_pid = self.k.GetProcessId(self.process)
            if not expected_pid:
                raise BrokerError("Metadata broker process identity is unavailable")
            deadline = time.monotonic() + 45
            while not self.k.ConnectNamedPipe(self.pipe, None):
                error = C.get_last_error()
                if error == 535:  # ERROR_PIPE_CONNECTED
                    break
                if error not in (231, 536) or time.monotonic() >= deadline or not self.alive:
                    raise BrokerError("Metadata broker connection failed")
                time.sleep(0.02)
            actual_pid = W.ULONG()
            if (not self.k.GetNamedPipeClientProcessId(self.pipe, C.byref(actual_pid))
                    or actual_pid.value != expected_pid):
                raise BrokerError("Metadata broker peer identity mismatch")
        except BaseException:
            self.close()
            raise

    @property
    def alive(self):
        return bool(self.process and self.k.WaitForSingleObject(self.process, 0) == 258)

    def _read(self, length):
        output, deadline = bytearray(), time.monotonic() + 15
        while len(output) < length:
            buffer, count = C.create_string_buffer(length - len(output)), W.DWORD()
            if self.k.ReadFile(self.pipe, buffer, len(buffer), C.byref(count), None):
                output.extend(buffer.raw[:count.value])
            elif C.get_last_error() not in (232, 234):
                raise BrokerError("Metadata broker response transport failed")
            if len(output) < length:
                if time.monotonic() >= deadline or not self.alive:
                    raise BrokerError("Metadata broker response timeout")
                time.sleep(0.01)
        return bytes(output)

    def request(self, op, **fields):
        if not self.alive:
            raise BrokerError("Metadata broker is not running")
        self.sequence += 1
        raw = json.dumps({"nonce": self.nonce, "seq": self.sequence, "op": op, **fields},
                         allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_FRAME:
            raise BrokerError("Metadata broker request exceeds bound")
        data = struct.pack("<I", len(raw)) + raw
        deadline = time.monotonic() + 15
        while data:
            count = W.DWORD()
            if not self.k.WriteFile(self.pipe, data, len(data), C.byref(count), None):
                raise BrokerError("Metadata broker request transport failed")
            data = data[count.value:]
            if data and (time.monotonic() >= deadline or not self.alive):
                raise BrokerError("Metadata broker request timeout")
            if data:
                time.sleep(0.01)
        size, = struct.unpack("<I", self._read(4))
        if not 0 < size <= MAX_FRAME:
            raise BrokerError("Metadata broker response exceeds bound")
        result = json.loads(self._read(size).decode("utf-8"), object_pairs_hook=_unique)
        if (not isinstance(result, dict) or result.get("nonce") != self.nonce
                or type(result.get("seq")) is not int or result["seq"] != self.sequence):
            raise BrokerError("Metadata broker response identity mismatch")
        if result.get("ok") is not True:
            raise BrokerError("Metadata broker refused: " + str(result.get("error", "invalid response"))[:300])
        return result

    def snapshot(self, handle, file_id):
        return self.request("snapshot", handle=int(handle), file_id=list(file_id))

    def apply(self, pin, descriptor, *, final=False):
        if not self.scope_active:
            raise BrokerError("Metadata preservation requires a host transaction scope")
        self.request("register", handle=int(pin.handle), file_id=list(pin.file_id))
        return self.request("apply", handle=int(pin.handle), file_id=list(pin.file_id),
                            descriptor=base64.b64encode(descriptor).decode("ascii"), final=final)

    def register_recovery(self, pin, journal):
        from mewcode.sandbox.publication_io import PinnedPath
        with PinnedPath(journal / "plan.json") as plan:
            self.request("register_recovery", handle=int(pin.handle), file_id=list(pin.file_id),
                         plan_handle=int(plan.handle), plan_id=list(plan.file_id),
                         plan_sha256=plan.digest(8 * 1024 * 1024))

    def require_remaining(self, seconds):
        if not self.alive or time.monotonic() - self.started + seconds >= SESSION_SECONDS:
            raise BrokerError("Metadata broker lifetime cannot cover this publication")

    def close(self):
        # Closing the authenticated pipe tells the helper to exit; never kill
        # an unrelated PID or leave a reusable privileged listener behind.
        if self.pipe:
            self.k.CloseHandle(self.pipe)
            self.pipe = None
        if self.process:
            self.k.WaitForSingleObject(self.process, 3000)
            self.k.CloseHandle(self.process)
            self.process = None
        if self.pin:
            self.pin.close()
            self.pin = None


@contextmanager
def transaction_scope(workspace, transaction, *, executable, sha256, reserve_seconds=300):
    """Entered only after the host has validated the current task authority."""
    global _SESSION
    from mewcode.sandbox.publication_io import PinnedPath
    with _LOCK:
        if type(reserve_seconds) is not int or not 120 <= reserve_seconds < SESSION_SECONDS:
            raise BrokerError("Metadata transaction budget is outside the helper lifetime")
        # RLock serializes threads, but can be re-entered by another coroutine
        # on the same thread. Never replace/end another active transaction.
        if _SESSION is not None and _SESSION.scope_active:
            raise BrokerError("Another metadata transaction scope is active")
        if _SESSION is not None and (not _SESSION.alive or _SESSION.executable != executable
                or time.monotonic() - _SESSION.started + reserve_seconds >= SESSION_SECONDS):
            _SESSION.close()
            _SESSION = None
        if _SESSION is None:
            _SESSION = Broker(executable, sha256)
        broker = _SESSION
        with PinnedPath(workspace, directory=True) as source, PinnedPath(transaction, directory=True) as private:
            broker.request("init", workspace_handle=int(source.handle), workspace_id=list(source.file_id),
                           transaction_handle=int(private.handle), transaction_id=list(private.file_id))
        broker.scope_active = True
        token = CURRENT.set(broker)
        try:
            yield broker
        finally:
            CURRENT.reset(token)
            broker.scope_active = False
            try:
                broker.request("end")
            except (OSError, ValueError, RuntimeError):
                broker.close()
                _SESSION = None


def close_session():
    global _SESSION
    with _LOCK:
        if _SESSION is not None:
            _SESSION.close()
            _SESSION = None
