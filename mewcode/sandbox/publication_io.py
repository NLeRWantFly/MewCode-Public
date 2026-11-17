"""Windows handle operations for host publication; no ACL or shell fallback.

Sources are pinned without write/delete sharing. Renames never replace an
existing destination. This is per-object atomicity, not a multi-file atomic
filesystem transaction; the publisher keeps a durable recovery plan.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import struct

from mewcode.sandbox.lpac_runtime import checked_path
from mewcode.sandbox.windows import NativeBoundaryUnavailable


class _Info(C.Structure):
    _fields_ = [("attributes", W.DWORD), ("created", W.FILETIME), ("accessed", W.FILETIME),
                ("written", W.FILETIME), ("volume", W.DWORD), ("size_high", W.DWORD),
                ("size_low", W.DWORD), ("links", W.DWORD), ("index_high", W.DWORD), ("index_low", W.DWORD)]


class _Rename(C.Structure):
    _fields_ = [("flags", W.DWORD), ("root", W.HANDLE), ("length", W.DWORD), ("name", W.WCHAR * 1)]


class _Basic(C.Structure):
    _fields_ = [("created", C.c_longlong), ("accessed", C.c_longlong),
                ("written", C.c_longlong), ("changed", C.c_longlong), ("attributes", W.DWORD)]


def _api():
    if os.name != "nt":
        raise NativeBoundaryUnavailable("Host publication requires Windows file handles")
    api = C.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateFileW": ([W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE], W.HANDLE),
        "CloseHandle": ([W.HANDLE], W.BOOL),
        "GetFileInformationByHandle": ([W.HANDLE, C.POINTER(_Info)], W.BOOL),
        "GetFileInformationByHandleEx": ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
        "SetFileInformationByHandle": ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
        "SetFilePointerEx": ([W.HANDLE, C.c_longlong, C.c_void_p, W.DWORD], W.BOOL),
        "ReadFile": ([W.HANDLE, C.c_void_p, W.DWORD, C.POINTER(W.DWORD), C.c_void_p], W.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = args, result
    return api


class _SecurityView:
    def security_string(self, mask=7):
        api = C.WinDLL("advapi32", use_last_error=True)
        convert = api.ConvertSecurityDescriptorToStringSecurityDescriptorW
        convert.argtypes = [C.c_void_p, W.DWORD, W.DWORD, C.POINTER(C.c_void_p), C.c_void_p]
        convert.restype = W.BOOL
        raw = C.create_string_buffer(self.security_descriptor())
        result = C.c_void_p()
        if not convert(raw, 1, mask, C.byref(result), None):
            raise C.WinError(C.get_last_error())
        free = _api().LocalFree
        free.argtypes, free.restype = [C.c_void_p], C.c_void_p
        try:
            return C.wstring_at(result.value)
        finally:
            free(result)

    def access_signature(self):
        # AI records completed automatic inheritance, not access rights.
        return re.sub(r"D:([A-Z]*)(?=\(|$)", lambda m: "D:" + m[1].replace("AI", ""), self.security_string())

    def audit_signature(self):
        return re.sub(r"S:([A-Z]*)(?=\(|$)", lambda m: "S:" + m[1].replace("AI", ""), self.security_string(8))


@dataclass(frozen=True)
class FrozenSecurity(_SecurityView):
    """Host-captured security descriptor retained for publication/recovery."""
    descriptor: bytes

    def security_descriptor(self):
        if not 20 <= len(self.descriptor) <= 65536:
            raise NativeBoundaryUnavailable("Invalid saved publication permissions")
        # Validate every self-relative offset before passing bytes to Win32.
        # IsValidSecurityDescriptor is not a substitute for buffer bounds.
        for index, offset in enumerate(struct.unpack_from("<LLLL", self.descriptor, 4)):
            if not offset:
                continue
            if offset < 20 or offset + 8 > len(self.descriptor):
                raise NativeBoundaryUnavailable("Invalid saved descriptor offset")
            size = (8 + self.descriptor[offset + 1] * 4 if index < 2
                    else struct.unpack_from("<H", self.descriptor, offset + 2)[0])
            if size < 8 or offset + size > len(self.descriptor):
                raise NativeBoundaryUnavailable("Invalid saved descriptor size")
        raw = C.create_string_buffer(self.descriptor)
        api = C.WinDLL("advapi32", use_last_error=True)
        valid = api.IsValidSecurityDescriptor
        valid.argtypes, valid.restype = [C.c_void_p], W.BOOL
        # Only host-produced self-relative descriptors, never worker input.
        if not struct.unpack_from("<H", self.descriptor, 2)[0] & 0x8000 or not valid(raw):
            raise NativeBoundaryUnavailable("Invalid saved publication permissions")
        return self.descriptor

    @classmethod
    def inherited(cls, parent: _SecurityView, *, directory: bool):
        """Ask Windows to derive default child access; never invent ACEs.

        Uses the effective host identity with privilege checks enabled. This
        computes a descriptor only, without creating or changing a target.
        An active metadata broker supplies the parent's SACL as well; without
        it this adapter makes no claim to audit-policy preservation.
        """
        parent_raw = C.create_string_buffer(parent.security_descriptor())
        api, kernel = C.WinDLL("advapi32", use_last_error=True), _api()
        token, result = W.HANDLE(), C.c_void_p()
        open_thread = api.OpenThreadToken
        open_thread.argtypes, open_thread.restype = [W.HANDLE, W.DWORD, W.BOOL, C.POINTER(W.HANDLE)], W.BOOL
        if not open_thread(W.HANDLE(-2), 8, True, C.byref(token)):
            if C.get_last_error() != 1008:  # ERROR_NO_TOKEN, not access denied.
                raise C.WinError(C.get_last_error())
            open_process = api.OpenProcessToken
            open_process.argtypes, open_process.restype = [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)], W.BOOL
            if not open_process(W.HANDLE(-1), 8, C.byref(token)):
                raise C.WinError(C.get_last_error())
        create = api.CreatePrivateObjectSecurityEx
        create.argtypes = [C.c_void_p, C.c_void_p, C.POINTER(C.c_void_p), C.c_void_p,
                           W.BOOL, W.DWORD, W.HANDLE, C.c_void_p]
        create.restype = W.BOOL
        destroy = api.DestroyPrivateObjectSecurity
        destroy.argtypes, destroy.restype = [C.POINTER(C.c_void_p)], W.BOOL
        length = api.GetSecurityDescriptorLength
        length.argtypes, length.restype = [C.c_void_p], W.DWORD
        mapping = (W.DWORD * 4)(0x120089, 0x120116, 0x1200a0, 0x1f01ff)
        try:
            from mewcode.sandbox.metadata_broker import CURRENT
            flags = 3 if CURRENT.get() is not None else 1
            if not create(parent_raw, None, C.byref(result), None, directory, flags, token, mapping):
                raise C.WinError(C.get_last_error())
            size = length(result)
            if not 20 <= size <= 65536:
                raise NativeBoundaryUnavailable("Inherited descriptor exceeds bound")
            frozen = cls(C.string_at(result, size))
            frozen.security_descriptor()
            return frozen
        finally:
            if result.value:
                destroy(C.byref(result))
            kernel.CloseHandle(token)


class PinnedPath(_SecurityView):
    def __init__(self, path: Path, *, movable=False, directory=False, exclusive=False,
                 security_write=False, metadata_write=False):
        self.api, self.handle = _api(), None
        self.path = checked_path(path, file=not directory)
        before = self.path.stat()
        self.directory = directory
        access = (0x20080 if directory else 0x80000000) | (0x10000 if movable else 0) | (0x40000 if security_write else 0)
        if metadata_write:
            access |= 0x100  # FILE_WRITE_ATTRIBUTES, only host-created objects.
        share = 0 if exclusive else 3 if directory else 1
        handle = self.api.CreateFileW(str(self.path), access, share, None, 3, 0x02200000, None)
        if handle == C.c_void_p(-1).value:
            raise C.WinError(C.get_last_error())
        self.handle = handle
        try:
            info = self.info()
            if (bool(info.attributes & 0x10) != directory or info.attributes & 0x400
                    or not directory and info.links != 1
                    or self.file_id[1] != before.st_ino & ((1 << 64) - 1)):
                raise NativeBoundaryUnavailable("Publication object changed or contains a link")
        except BaseException:
            self.close()
            raise

    def info(self):
        info = _Info()
        if not self.api.GetFileInformationByHandle(self.handle, C.byref(info)):
            raise C.WinError(C.get_last_error())
        return info

    @property
    def file_id(self):
        info = self.info()
        return info.volume, (info.index_high << 32) | info.index_low

    def basic_metadata(self):
        info = _Basic()
        if not self.api.GetFileInformationByHandleEx(self.handle, 0, C.byref(info), C.sizeof(info)):
            raise C.WinError(C.get_last_error())
        return {key: getattr(info, key) for key in ("created", "accessed", "written", "attributes")}

    def set_basic_metadata(self, metadata):
        expected = {"created", "accessed", "written", "attributes"}
        if (not isinstance(metadata, dict) or set(metadata) != expected
                or any(type(value) is not int or not 0 < value < (1 << 63) for value in metadata.values())
                or metadata["attributes"] & ~0x20b7):
            raise NativeBoundaryUnavailable("Unsupported publication basic metadata")
        # Directory/normal flags are structural. The caller only sets these
        # fields on host-created payloads, never clears an original's flags.
        attributes = metadata["attributes"] & ~0x90
        info = _Basic(metadata["created"], metadata["accessed"], metadata["written"], 0, attributes or 0x80)
        if not self.api.SetFileInformationByHandle(self.handle, 0, C.byref(info), C.sizeof(info)):
            raise C.WinError(C.get_last_error())

    def require_plain_metadata(self, *, allow_readonly=False):
        """Reject metadata this byte-copy transport would silently drop."""
        if self.info().attributes & ~(0x20b7 if allow_readonly else 0x20b6):
            raise NativeBoundaryUnavailable("Unsupported publication file attributes")
        buffer = C.create_string_buffer(65536)
        C.set_last_error(0)
        success = self.api.GetFileInformationByHandleEx(self.handle, 7, buffer, len(buffer))
        error = C.get_last_error()
        if error == 38:  # ERROR_HANDLE_EOF: directories with no named streams.
            return
        if not success:
            raise NativeBoundaryUnavailable(f"Cannot verify publication streams ({error})")
        offset = 0
        while True:
            next_offset, length = struct.unpack_from("<LL", buffer, offset)
            if length % 2 or length < 2 or offset + 24 + length > len(buffer):
                raise NativeBoundaryUnavailable("Invalid publication stream record")
            name = buffer.raw[offset + 24:offset + 24 + length].decode("utf-16-le")
            if name != "::$DATA":
                raise NativeBoundaryUnavailable("Named publication streams require a metadata adapter")
            if not next_offset:
                break
            if next_offset < 24 + length or next_offset % 8 or offset + next_offset + 24 > len(buffer):
                raise NativeBoundaryUnavailable("Invalid publication stream offset")
            offset += next_offset

    def digest(self, limit: int):
        if self.directory:
            return ""
        if not self.api.SetFilePointerEx(self.handle, 0, None, 0):
            raise C.WinError(C.get_last_error())
        digest, total = hashlib.sha256(), 0
        buffer, count = C.create_string_buffer(1024 * 1024), W.DWORD()
        while True:
            if not self.api.ReadFile(self.handle, buffer, len(buffer), C.byref(count), None):
                raise C.WinError(C.get_last_error())
            if not count.value:
                return digest.hexdigest()
            total += count.value
            if total > limit:
                raise NativeBoundaryUnavailable("Publication file exceeds host bound")
            digest.update(buffer.raw[:count.value])

    def security_descriptor(self):
        """Owner/group/DACL only; no privileged SACL read or permission edit."""
        from mewcode.sandbox.metadata_broker import CURRENT
        broker = CURRENT.get()
        if broker is not None:
            import base64
            descriptor = base64.b64decode(broker.snapshot(self.handle, self.file_id)["descriptor"], validate=True)
            return FrozenSecurity(descriptor).security_descriptor()
        api = C.WinDLL("advapi32", use_last_error=True)
        get = api.GetKernelObjectSecurity
        get.argtypes = [W.HANDLE, W.DWORD, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)]
        get.restype = W.BOOL
        needed = W.DWORD()
        get(self.handle, 7, None, 0, C.byref(needed))
        if not 0 < needed.value <= 65536:
            raise NativeBoundaryUnavailable("Cannot verify publication file permissions")
        buffer = C.create_string_buffer(needed.value)
        if not get(self.handle, 7, buffer, len(buffer), C.byref(needed)):
            raise C.WinError(C.get_last_error())
        return buffer.raw[:needed.value]

    def copy_sacl_from(self, original, *, final=False):
        """Only registered private replacements may receive preserved SACLs."""
        from mewcode.sandbox.metadata_broker import CURRENT
        broker = CURRENT.get()
        if broker is not None:
            broker.apply(self, original.security_descriptor(), final=final)
            if final and self.audit_signature() != original.audit_signature():
                raise NativeBoundaryUnavailable("Published audit permissions differ from original")

    def copy_dacl_from(self, original, *, final=False):
        """Keep exact original access entries on a private replacement only.

        No owner/group changes, no source ACL edits, no new grants. While in
        private storage, protect the copied DACL against that parent's rules;
        after moving to the target restore the original inheritance policy.
        The publisher must check the final full descriptor before success.
        """
        if self.security_string(3) != original.security_string(3):
            raise NativeBoundaryUnavailable("Publication owner/group differs from original")
        api = C.WinDLL("advapi32", use_last_error=True)
        raw = C.create_string_buffer(original.security_descriptor())
        dacl, present, defaulted, control, revision = C.c_void_p(), W.BOOL(), W.BOOL(), W.WORD(), W.DWORD()
        get = api.GetSecurityDescriptorDacl
        get.argtypes, get.restype = [C.c_void_p, C.POINTER(W.BOOL), C.POINTER(C.c_void_p), C.POINTER(W.BOOL)], W.BOOL
        controls = api.GetSecurityDescriptorControl
        controls.argtypes, controls.restype = [C.c_void_p, C.POINTER(W.WORD), C.POINTER(W.DWORD)], W.BOOL
        if not get(raw, C.byref(present), C.byref(dacl), C.byref(defaulted)) or not controls(raw, C.byref(control), C.byref(revision)):
            raise C.WinError(C.get_last_error())
        if not present.value or not dacl.value:
            raise NativeBoundaryUnavailable("Unrestricted publication DACL is unsupported")
        # SetSecurityInfo recomputes inheritance and adds parent ACEs even
        # when the original unprotected DACL intentionally has explicit-only
        # entries (e.g. Git-created files). Preserve the captured ACL exactly
        # instead. This uses only the replacement handle's existing WRITE_DAC;
        # owner, group and SACL are not part of this operation.
        protected = not final or bool(control.value & 0x1000)
        set_control = api.SetSecurityDescriptorControl
        set_control.argtypes, set_control.restype = [C.c_void_p, W.WORD, W.WORD], W.BOOL
        if not set_control(raw, 0x1000, 0x1000 if protected else 0):
            raise C.WinError(C.get_last_error())
        native = C.WinDLL("ntdll")
        setter = native.NtSetSecurityObject
        setter.argtypes, setter.restype = [W.HANDLE, W.DWORD, C.c_void_p], C.c_long
        status = setter(self.handle, 4 | (0x80000000 if protected else 0x20000000), raw)
        if status < 0:
            convert = native.RtlNtStatusToDosError
            convert.argtypes, convert.restype = [C.c_long], W.DWORD
            raise C.WinError(convert(status))

    def rename_absent(self, destination: Path):
        checked_path(destination.parent)
        if not destination.is_absolute() or ".." in destination.parts or ":" in destination.name:
            raise NativeBoundaryUnavailable("Invalid publication destination")
        encoded = str(destination).encode("utf-16-le")
        size = max(C.sizeof(_Rename), _Rename.name.offset + len(encoded) + 2)
        buffer = C.create_string_buffer(size)
        info = _Rename.from_buffer(buffer)
        info.flags, info.root, info.length = 0, None, len(encoded)  # ReplaceIfExists = FALSE
        C.memmove(C.addressof(buffer) + _Rename.name.offset, encoded, len(encoded))
        if not self.api.SetFileInformationByHandle(self.handle, 3, buffer, size):
            raise C.WinError(C.get_last_error())
        self.path = destination

    def remove_empty_directory(self):
        if not self.directory:
            raise NativeBoundaryUnavailable("Only empty directories can be discarded")
        delete = C.c_ubyte(1)
        if not self.api.SetFileInformationByHandle(self.handle, 4, C.byref(delete), C.sizeof(delete)):
            raise C.WinError(C.get_last_error())
        self.close()  # Kernel refuses disposition for nonempty directories.

    def pin_renamed_directory(self):
        """Drop DELETE access before putting children into a new directory.

        Windows otherwise rejects child renames due to the parent's sharing
        interlock. Reopen without DELETE and compare the exact directory ID;
        no child operation occurs in the close/reopen interval.
        """
        expected = self.file_id
        self.close()
        replacement = PinnedPath(self.path, directory=True)
        if replacement.file_id != expected:
            replacement.close()
            raise NativeBoundaryUnavailable("New publication directory was replaced")
        self.handle, replacement.handle = replacement.handle, None

    def prepare_directory_removal(self):
        """Acquire DELETE only after children moved; never rename a tree.

        Close/reopen is necessary for Windows sharing semantics. An external
        directory replacement in this gap is rejected by exact file identity.
        remove_empty_directory then lets the kernel check namespace emptiness.
        """
        if not self.directory:
            raise NativeBoundaryUnavailable("Expected a directory for removal")
        expected = self.file_id
        self.close()
        replacement = PinnedPath(self.path, directory=True, movable=True)
        if replacement.file_id != expected:
            replacement.close()
            raise NativeBoundaryUnavailable("Publication directory was replaced")
        self.handle, replacement.handle = replacement.handle, None

    def close(self):
        if self.handle is not None:
            handle, self.handle = self.handle, None
            if not self.api.CloseHandle(handle):
                raise C.WinError(C.get_last_error())

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
