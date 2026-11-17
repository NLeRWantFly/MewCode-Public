"""Bounded, handle-based Windows workspace metadata inventory.

This is an audit adapter, not a write policy. Captures remain useful to
read-only callers when a query is unavailable, but a publisher must call
``require_supported_metadata`` before its first source mutation. In
particular, an unreadable SACL is represented as unknown, never as absent.
No token privileges are enabled here.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import stat
import struct
from typing import Any

from mewcode.sandbox.lpac_runtime import checked_path
from mewcode.sandbox.windows import NativeBoundaryUnavailable

SCHEMA = "mewcode-workspace-metadata-v1"
MAX_SECURITY_DESCRIPTOR = 64 * 1024
MAX_SECURITY_SDDL_CHARS = 512 * 1024
MAX_STREAM_BUFFER = 64 * 1024
MAX_STREAMS = 256
_FILE_READ_EA = 0x0008
_FILE_READ_ATTRIBUTES = 0x0080
_READ_CONTROL = 0x00020000
_ACCESS_SYSTEM_SECURITY = 0x01000000
_FILE_SHARE_READ = 0x00000001
_OPEN_EXISTING = 3
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_ATTRIBUTE_DIRECTORY = 0x0010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_SUPPORTED_ATTRIBUTES = 0x20B7
_DEFAULT_DATA_STREAM = "::$DATA"
_FILE_STREAM_INFO_CLASS = 7
_FILE_EA_INFORMATION_CLASS = 7


class MetadataInventoryError(NativeBoundaryUnavailable):
    """Metadata could not be captured or is not safe for replacement."""


class _FileInformation(C.Structure):
    _fields_ = [
        ("attributes", W.DWORD),
        ("created", W.FILETIME),
        ("accessed", W.FILETIME),
        ("written", W.FILETIME),
        ("volume", W.DWORD),
        ("size_high", W.DWORD),
        ("size_low", W.DWORD),
        ("links", W.DWORD),
        ("index_high", W.DWORD),
        ("index_low", W.DWORD),
    ]


class _BasicInformation(C.Structure):
    _fields_ = [
        ("created", C.c_longlong),
        ("accessed", C.c_longlong),
        ("written", C.c_longlong),
        ("changed", C.c_longlong),
        ("attributes", W.DWORD),
    ]


class _IoStatusBlock(C.Structure):
    # The first member is a pointer-sized union of NTSTATUS / PVOID.
    _fields_ = [("status", C.c_void_p), ("information", C.c_size_t)]


def _apis():
    if os.name != "nt":
        raise MetadataInventoryError("Workspace metadata inventory requires Windows")
    kernel = C.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateFileW": ([W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE], W.HANDLE),
        "CloseHandle": ([W.HANDLE], W.BOOL),
        "GetFileInformationByHandle": ([W.HANDLE, C.POINTER(_FileInformation)], W.BOOL),
        "GetFileInformationByHandleEx": ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result

    advapi = C.WinDLL("advapi32", use_last_error=True)
    get_security = advapi.GetKernelObjectSecurity
    get_security.argtypes = [W.HANDLE, W.DWORD, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)]
    get_security.restype = W.BOOL
    convert = advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes = [C.c_void_p, W.DWORD, W.DWORD, C.POINTER(C.c_void_p), C.c_void_p]
    convert.restype = W.BOOL
    get_sacl = advapi.GetSecurityDescriptorSacl
    get_sacl.argtypes = [C.c_void_p, C.POINTER(W.BOOL), C.POINTER(C.c_void_p), C.POINTER(W.BOOL)]
    get_sacl.restype = W.BOOL
    local_free = kernel.LocalFree
    local_free.argtypes, local_free.restype = [C.c_void_p], C.c_void_p

    ntdll = C.WinDLL("ntdll")
    query_information = ntdll.NtQueryInformationFile
    query_information.argtypes = [W.HANDLE, C.POINTER(_IoStatusBlock), C.c_void_p, W.DWORD, C.c_int]
    query_information.restype = C.c_long
    return kernel, advapi, get_security, convert, get_sacl, local_free, query_information


def _invalid_handle(handle: Any) -> bool:
    return handle is None or handle == -1 or handle == C.c_void_p(-1).value


def _open(path: Path, *, directory: bool, with_sacl: bool):
    kernel, *_ = _apis()
    access = _FILE_READ_ATTRIBUTES | _READ_CONTROL | _FILE_READ_EA
    if with_sacl:
        access |= _ACCESS_SYSTEM_SECURITY
    flags = _FILE_FLAG_OPEN_REPARSE_POINT
    if directory:
        flags |= _FILE_FLAG_BACKUP_SEMANTICS
    handle = kernel.CreateFileW(str(path), access, _FILE_SHARE_READ, None,
                                _OPEN_EXISTING, flags, None)
    if _invalid_handle(handle):
        raise C.WinError(C.get_last_error())
    return handle


def _identity(handle) -> tuple[int, int, int, int]:
    kernel, *_ = _apis()
    info = _FileInformation()
    if not kernel.GetFileInformationByHandle(handle, C.byref(info)):
        raise C.WinError(C.get_last_error())
    return (int(info.volume), (int(info.index_high) << 32) | int(info.index_low),
            int(info.attributes), int(info.links))


def _basic(handle) -> _BasicInformation:
    kernel, *_ = _apis()
    info = _BasicInformation()
    if not kernel.GetFileInformationByHandleEx(handle, 0, C.byref(info), C.sizeof(info)):
        raise C.WinError(C.get_last_error())
    return info


def _security_descriptor(handle, information: int) -> bytes:
    *_, get_security, _convert, _get_sacl, _local_free, _query_information = _apis()
    needed = W.DWORD()
    C.set_last_error(0)
    get_security(handle, information, None, 0, C.byref(needed))
    if not 0 < needed.value <= MAX_SECURITY_DESCRIPTOR:
        raise MetadataInventoryError("Security descriptor exceeds the host bound or is unavailable")
    buffer = C.create_string_buffer(needed.value)
    if not get_security(handle, information, buffer, len(buffer), C.byref(needed)):
        raise C.WinError(C.get_last_error())
    if not 20 <= needed.value <= len(buffer):
        raise MetadataInventoryError("Invalid bounded security descriptor length")
    return buffer.raw[:needed.value]


def _security_sddl(raw: bytes, information: int) -> str:
    _kernel, _advapi, _get_security, convert, _get_sacl, local_free, _query_information = _apis()
    descriptor = C.create_string_buffer(raw)
    result = C.c_void_p()
    if not convert(descriptor, 1, information, C.byref(result), None):
        raise C.WinError(C.get_last_error())
    try:
        value = C.wstring_at(result.value)
        if len(value) > MAX_SECURITY_SDDL_CHARS:
            raise MetadataInventoryError("Security text exceeds the host bound")
        return value
    finally:
        local_free(result)


def _security_piece(raw: bytes) -> dict[str, Any]:
    try:
        return {"state": "captured", "sddl": _security_sddl(raw, 7)}
    except (OSError, ValueError, MetadataInventoryError):
        return {"state": "unavailable", "sddl": None}


def _sacl_piece(raw: bytes) -> dict[str, Any]:
    _kernel, _advapi, _get_security, _convert, get_sacl, _local_free, _query_information = _apis()
    descriptor = C.create_string_buffer(raw)
    present, acl, defaulted = W.BOOL(), C.c_void_p(), W.BOOL()
    if not get_sacl(descriptor, C.byref(present), C.byref(acl), C.byref(defaulted)):
        raise C.WinError(C.get_last_error())
    if not present.value:
        return {"state": "captured", "present": False, "null": False, "sddl": None}
    if not acl.value:
        return {"state": "captured", "present": True, "null": True, "sddl": None}
    return {"state": "captured", "present": True, "null": False,
            "sddl": _security_sddl(raw, 8)}


def _streams(handle) -> dict[str, Any]:
    kernel, *_ = _apis()
    buffer = C.create_string_buffer(MAX_STREAM_BUFFER)
    C.set_last_error(0)
    if not kernel.GetFileInformationByHandleEx(handle, _FILE_STREAM_INFO_CLASS,
                                               buffer, len(buffer)):
        if C.get_last_error() == 38:  # ERROR_HANDLE_EOF: no streams (common for directories).
            return {"state": "captured", "items": []}
        return {"state": "unavailable", "items": []}

    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        if offset < 0 or offset + 24 > len(buffer):
            return {"state": "unavailable", "items": []}
        next_offset, name_bytes = struct.unpack_from("<LL", buffer.raw, offset)
        size, allocation = struct.unpack_from("<qq", buffer.raw, offset + 8)
        if (name_bytes % 2 or name_bytes < 2 or name_bytes > MAX_STREAM_BUFFER
                or offset + 24 + name_bytes > len(buffer) or size < 0 or allocation < 0):
            return {"state": "unavailable", "items": []}
        try:
            name = buffer.raw[offset + 24:offset + 24 + name_bytes].decode("utf-16-le", "strict")
        except UnicodeDecodeError:
            return {"state": "unavailable", "items": []}
        items.append({"name": name, "size": size, "allocation_size": allocation})
        if len(items) > MAX_STREAMS:
            return {"state": "unavailable", "items": []}
        if not next_offset:
            break
        if (next_offset < 24 + name_bytes or next_offset % 8
                or offset + next_offset + 24 > len(buffer)):
            return {"state": "unavailable", "items": []}
        offset += next_offset
    items.sort(key=lambda item: (item["name"].casefold(), item["name"]))
    return {"state": "captured", "items": items}


def _extended_attributes(handle) -> dict[str, Any]:
    *_, query_information = _apis()
    value = W.DWORD()
    status_block = _IoStatusBlock()
    status = query_information(handle, C.byref(status_block), C.byref(value),
                              C.sizeof(value), _FILE_EA_INFORMATION_CLASS)
    if status != 0:
        return {"state": "unavailable", "size": None}
    return {"state": "captured", "size": int(value.value)}


def _capture_security(handle, *, sacl_access: bool) -> dict[str, Any]:
    owner_group_dacl = {"state": "unavailable", "sddl": None}
    sacl = {"state": "unavailable", "present": None, "null": None, "sddl": None}
    try:
        owner_group_dacl = _security_piece(_security_descriptor(handle, 7))
    except (OSError, ValueError, MetadataInventoryError):
        pass
    if sacl_access:
        try:
            sacl = _sacl_piece(_security_descriptor(handle, 15))
        except (OSError, ValueError, MetadataInventoryError):
            pass
    return {"owner_group_dacl": owner_group_dacl, "sacl": sacl}


def capture_metadata(path: Path) -> dict[str, Any]:
    """Capture stable, JSON-serializable metadata from a pinned read handle.

    SACL access is requested without changing token privileges. If Windows
    does not authorize that request, its status is explicitly ``unavailable``
    while owner/group/DACL, streams, EAs and basic metadata remain auditable.
    Access time is intentionally omitted because reads can update it.
    """
    if os.name != "nt":
        raise MetadataInventoryError("Workspace metadata inventory requires Windows")
    requested = Path(path)
    try:
        before = requested.lstat()
    except OSError as exc:
        raise MetadataInventoryError("Workspace object cannot be inspected") from exc
    directory = stat.S_ISDIR(before.st_mode)
    if not directory and not stat.S_ISREG(before.st_mode):
        raise MetadataInventoryError("Unsupported workspace object type")
    checked = checked_path(requested, file=not directory)
    identity_before = (int(before.st_dev), int(before.st_ino) & ((1 << 64) - 1))
    if not directory and before.st_nlink != 1:
        raise MetadataInventoryError("Hard-linked workspace files are unsupported")

    handle = None
    sacl_access = True
    try:
        try:
            handle = _open(checked, directory=directory, with_sacl=True)
        except OSError:
            # Preserve useful read-only audit data; the absent SACL permission
            # remains explicit and is rejected by a write preflight.
            sacl_access = False
            handle = _open(checked, directory=directory, with_sacl=False)

        volume, index, attributes, links = _identity(handle)
        if ((index != identity_before[1])
                or bool(attributes & _FILE_ATTRIBUTE_DIRECTORY) != directory
                or attributes & _FILE_ATTRIBUTE_REPARSE_POINT
                or (not directory and links != 1)):
            raise MetadataInventoryError("Workspace object changed or contains a link")

        first = _basic(handle)
        streams = _streams(handle)
        extended = _extended_attributes(handle)
        def security_snapshot():
            value = _capture_security(handle, sacl_access=sacl_access)
            from mewcode.sandbox.metadata_broker import CURRENT
            broker = CURRENT.get()
            if broker is not None:
                value["sacl"] = broker.snapshot(handle, (volume, index))["sacl"]
            return value
        security = security_snapshot()
        streams_after = _streams(handle)
        extended_after = _extended_attributes(handle)
        security_after = security_snapshot()
        last = _basic(handle)
        same_identity = (volume, index, attributes, links) == _identity(handle)
        stable_basic = ((first.created, first.written, first.changed, first.attributes)
                        == (last.created, last.written, last.changed, last.attributes))
        consistent = (same_identity and stable_basic and streams == streams_after
                      and extended == extended_after and security == security_after)
        snapshot = {
            "schema": SCHEMA,
            "kind": "directory" if directory else "file",
            "capture_consistent": bool(consistent),
            "attributes": int(first.attributes),
            "created": int(first.created),
            "written": int(first.written),
            "security": security,
            "streams": streams,
            "extended_attributes": extended,
        }
        return snapshot
    except MetadataInventoryError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise MetadataInventoryError("Workspace metadata query failed") from exc
    finally:
        if handle is not None:
            kernel, *_ = _apis()
            if not kernel.CloseHandle(handle):
                raise C.WinError(C.get_last_error())


def _captured(section: object) -> bool:
    return isinstance(section, dict) and section.get("state") == "captured"


def require_supported_metadata(snapshot: object) -> None:
    """Reject unknown or unpreserved metadata before original mutation.

    This adapter deliberately does not preserve SACLs, named data streams, or
    NTFS extended attributes. SACL absence is accepted only after a successful
    SACL_SECURITY_INFORMATION query proved the descriptor has no SACL.
    """
    if not isinstance(snapshot, dict) or snapshot.get("schema") != SCHEMA:
        raise MetadataInventoryError("Invalid workspace metadata inventory")
    if (snapshot.get("kind") not in {"file", "directory"}
            or snapshot.get("capture_consistent") is not True
            or type(snapshot.get("attributes")) is not int
            or snapshot["attributes"] & ~_SUPPORTED_ATTRIBUTES
            or type(snapshot.get("created")) is not int
            or type(snapshot.get("written")) is not int):
        raise MetadataInventoryError("Workspace metadata is incomplete or unsupported")

    security = snapshot.get("security")
    if not isinstance(security, dict):
        raise MetadataInventoryError("Workspace security metadata is unavailable")
    owner_group_dacl = security.get("owner_group_dacl")
    sacl = security.get("sacl")
    if not _captured(owner_group_dacl) or not isinstance(owner_group_dacl.get("sddl"), str):
        raise MetadataInventoryError("Owner, group, or DACL metadata is unavailable")
    if not _captured(sacl) or type(sacl.get("present")) is not bool:
        raise MetadataInventoryError("SACL state is unknown; publication cannot preserve it safely")
    from mewcode.sandbox.metadata_broker import CURRENT
    if sacl["present"] and CURRENT.get() is None:
        raise MetadataInventoryError("SACL metadata is not preserved by the publication adapter")

    streams = snapshot.get("streams")
    if not _captured(streams) or not isinstance(streams.get("items"), list):
        raise MetadataInventoryError("Named stream inventory is unavailable")
    if len(streams["items"]) > MAX_STREAMS:
        raise MetadataInventoryError("Named stream inventory exceeds the host bound")
    for item in streams["items"]:
        if (not isinstance(item, dict) or not isinstance(item.get("name"), str)
                or type(item.get("size")) is not int
                or type(item.get("allocation_size")) is not int):
            raise MetadataInventoryError("Invalid named stream inventory")
    names = [item["name"] for item in streams["items"]]
    if len(names) != len(set(names)):
        raise MetadataInventoryError("Duplicate named stream record")
    if snapshot["kind"] == "file":
        if names != [_DEFAULT_DATA_STREAM]:
            raise MetadataInventoryError("Default file stream is missing or named data streams are present")
    elif any(name != _DEFAULT_DATA_STREAM for name in names):
        raise MetadataInventoryError("Named data streams are not supported")

    extended = snapshot.get("extended_attributes")
    if not _captured(extended) or type(extended.get("size")) is not int:
        raise MetadataInventoryError("Extended attribute inventory is unavailable")
    if extended["size"] != 0:
        raise MetadataInventoryError("Extended attributes are not supported")


def metadata_drift(expected: object, actual: object) -> tuple[str, ...]:
    """Return stable metadata fields that changed or could not be compared.

    Unknown fields are reported as drift/uncertainty even when both captures
    are unknown. This prevents an unreadable SACL or failed stream/EA query
    from looking unchanged. Access time is not captured and never participates.
    """
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        return ("inventory",)
    drift: list[str] = []
    if expected.get("schema") != SCHEMA or actual.get("schema") != SCHEMA:
        return ("schema",)
    if expected.get("capture_consistent") is not True or actual.get("capture_consistent") is not True:
        drift.append("capture_consistent")
    for field in ("kind", "attributes", "created", "written"):
        if expected.get(field) != actual.get(field):
            drift.append(field)

    for section, label, value_fields in (
        ("security", "security.owner_group_dacl", ("sddl",)),
        ("security", "security.sacl", ("present", "null", "sddl")),
        ("streams", "streams", ("items",)),
        ("extended_attributes", "extended_attributes", ("size",)),
    ):
        left_parent, right_parent = expected.get(section), actual.get(section)
        if not isinstance(left_parent, dict) or not isinstance(right_parent, dict):
            drift.append(label + ".unavailable")
            continue
        child_key = label.rsplit(".", 1)[-1] if section == "security" else None
        if section == "security":
            left, right = left_parent.get(child_key), right_parent.get(child_key)
        else:
            left, right = left_parent, right_parent
        if not _captured(left) or not _captured(right):
            drift.append(label + ".unavailable")
            continue
        for field in value_fields:
            if left.get(field) != right.get(field):
                drift.append(label + "." + field)
    return tuple(sorted(set(drift)))
