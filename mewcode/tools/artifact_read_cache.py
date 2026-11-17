"""Bounded process-local artifact cache and measured source-file reads.

Cached content is separate from visual acceptance ledgers and never writes
source files. File versions are checked before publishing and on every reuse.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable


@lru_cache(maxsize=1)
def _windows_version_api():
    import ctypes
    from ctypes import wintypes
    class BasicInfo(ctypes.Structure):
        _fields_ = [("created", ctypes.c_longlong), ("accessed", ctypes.c_longlong),
                    ("written", ctypes.c_longlong), ("changed", ctypes.c_longlong),
                    ("attributes", wintypes.DWORD)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateFileW": ([wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                         wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE], wintypes.HANDLE),
        "GetFileInformationByHandleEx": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                          wintypes.DWORD], wintypes.BOOL),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    return kernel, BasicInfo


def _change_time(path, stat):
    if os.name != "nt":
        return stat.st_ctime_ns
    # Windows stat ctime can be birth time. NTFS ChangeTime also detects a
    # same-size rewrite whose original mtime was preserved by the writer.
    import ctypes
    kernel, basic_type = _windows_version_api()
    handle = kernel.CreateFileW(str(path), 0x0080, 0x0007, None, 3, 0, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = basic_type()
        if not kernel.GetFileInformationByHandleEx(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(info.changed)
    finally:
        kernel.CloseHandle(handle)


@dataclass(frozen=True)
class SourceVersion:
    path: str
    state: tuple[int, ...]

    @classmethod
    def read(cls, path: Path):
        stat = path.stat()
        return cls(str(path), (stat.st_dev, stat.st_ino, stat.st_size,
                              stat.st_mtime_ns, stat.st_ctime_ns, _change_time(path, stat)))

    @property
    def size(self):
        return self.state[2]

    def check(self):
        if self != self.read(Path(self.path)):
            raise ValueError("文件在读取过程中发生变化，请重新读取当前版本")


@dataclass(frozen=True)
class ArtifactPayload:
    data: str | bytes
    pages: int = 0
    note: str = ""
    unicode_repaired: bool = False

    @property
    def size_bytes(self):
        data = self.data if isinstance(self.data, bytes) else self.data.encode("utf-8", errors="surrogatepass")
        return len(data) + len(self.note.encode("utf-8"))


@dataclass
class ReadUsage:
    source_read_bytes: int = 0
    source_read_calls: int = 0
    hash_read_bytes: int = 0
    parser_read_bytes: int = 0
    cache_read_bytes: int = 0
    cache_write_bytes: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    coalesced_reads: int = 0
    source_write_bytes: int = 0
    output_text_bytes: int = 0
    output_image_bytes: int = 0
    hash_cache_hit: bool = False
    data_cache_hit: bool = False

    def as_dict(self):
        return asdict(self)


class CountedReader:
    """Seekable binary input; counters measure returned bytes, not physical I/O."""
    def __init__(self, path, usage: ReadUsage, *, hashing=False):
        self.handle = Path(path).open("rb")
        self.usage, self.hashing = usage, hashing

    def _record(self, size):
        self.usage.source_read_calls += 1
        self.usage.source_read_bytes += size
        if self.hashing:
            self.usage.hash_read_bytes += size
        else:
            self.usage.parser_read_bytes += size

    def read(self, size=-1):
        data = self.handle.read(size)
        self._record(len(data))
        return data

    def readinto(self, buffer):
        size = self.handle.readinto(buffer)
        self._record(size or 0)
        return size

    def seek(self, offset, whence=0):
        return self.handle.seek(offset, whence)

    def tell(self):
        return self.handle.tell()

    def __getattr__(self, name):
        return getattr(self.handle, name)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.handle.close()


class ArtifactReadCache:
    def __init__(self, *, max_bytes=64 * 1024 * 1024, max_entry_bytes=8 * 1024 * 1024,
                 max_entries=256, ttl_seconds=600):
        if min(max_bytes, max_entry_bytes, max_entries, ttl_seconds) < 0:
            raise ValueError("缓存容量和期限不能为负数")
        self.max_bytes, self.max_entry_bytes = max_bytes, max_entry_bytes
        self.max_entries, self.ttl_seconds = max_entries, ttl_seconds
        self._entries = OrderedDict()
        self._pending: dict[tuple, Future] = {}
        self._lock = threading.Lock()
        self._bytes = self._generation = 0

    def _remove(self, key):
        payload, _ = self._entries.pop(key)
        self._bytes -= payload.size_bytes

    def get_or_load(self, source: SourceVersion, variant: tuple,
                    loader: Callable[[], ArtifactPayload], usage: ReadUsage, *, mode="use"):
        source.check()
        if mode == "bypass":
            payload = loader()
            source.check()
            return payload, False
        key = (source, variant)
        future = None
        owner = True
        with self._lock:
            stale = [old for old in self._entries if old[0].path == source.path and old[0] != source]
            for old in stale:
                self._remove(old)
            if stale:
                self._generation += 1
            if mode == "use":
                cached = self._entries.get(key)
                if cached and cached[1] > time.monotonic():
                    self._entries.move_to_end(key)
                    usage.cache_hits += 1
                    usage.cache_read_bytes += cached[0].size_bytes
                    return cached[0], True
                if cached:
                    self._remove(key)
                future = self._pending.get(key)
                owner = future is None
                if owner:
                    future = self._pending[key] = Future()
                    usage.cache_misses += 1
            generation = self._generation
        if not owner:
            payload = future.result()
            source.check()
            usage.cache_hits += 1
            usage.coalesced_reads += 1
            usage.cache_read_bytes += payload.size_bytes
            return payload, True
        try:
            payload = loader()
            source.check()
            with self._lock:
                if (mode != "bypass" and generation == self._generation and self.max_entries
                        and self.ttl_seconds and payload.size_bytes <= min(self.max_bytes, self.max_entry_bytes)):
                    if key in self._entries:
                        self._remove(key)
                    self._entries[key] = (payload, time.monotonic() + self.ttl_seconds)
                    self._bytes += payload.size_bytes
                    usage.cache_write_bytes += payload.size_bytes
                    while self._bytes > self.max_bytes or len(self._entries) > self.max_entries:
                        self._remove(next(iter(self._entries)))
                if future is not None:
                    future.set_result(payload)
                    self._pending.pop(key, None)
            return payload, False
        except BaseException as exc:
            if future is not None:
                with self._lock:
                    future.set_exception(exc)
                    self._pending.pop(key, None)
            raise

    @staticmethod
    def _in_scope(source, roots, path):
        candidate = Path(source.path)
        return candidate == path if path is not None else any(candidate.is_relative_to(root) for root in roots)

    def clear(self, roots, *, path=None):
        with self._lock:
            keys = [key for key in self._entries if self._in_scope(key[0], roots, path)]
            for key in keys:
                self._remove(key)
            # In-flight old reads may finish, but must not repopulate cleared cache.
            self._generation += 1
            return len(keys)

    def info(self, roots, *, path=None):
        with self._lock:
            expired = [key for key, (_, expiry) in self._entries.items() if expiry <= time.monotonic()]
            for key in expired:
                self._remove(key)
            payloads = [payload for (source, _), (payload, _) in self._entries.items()
                        if self._in_scope(source, roots, path)]
            return {"entries": len(payloads), "bytes": sum(value.size_bytes for value in payloads),
                    "max_bytes": self.max_bytes, "max_entry_bytes": self.max_entry_bytes,
                    "max_entries": self.max_entries, "ttl_seconds": self.ttl_seconds,
                    "storage": "memory", "source_files_written": False}


SHARED_ARTIFACT_CACHE = ArtifactReadCache()


def source_fingerprint(source: SourceVersion, usage: ReadUsage, *,
                       cache=SHARED_ARTIFACT_CACHE, mode="use") -> str:
    """Reuse the exact raw-byte digest across page reads and review receipts."""
    def load():
        with CountedReader(source.path, usage, hashing=True) as stream:
            return ArtifactPayload(hashlib.file_digest(stream, "sha256").hexdigest())
    digest, usage.hash_cache_hit = cache.get_or_load(source, ("sha256",), load, usage, mode=mode)
    source.check()
    return str(digest.data)
