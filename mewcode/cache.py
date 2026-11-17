from __future__ import annotations

import os
import threading
from collections import OrderedDict
from dataclasses import dataclass

DEFAULT_MAX_BYTES = 128 * 1024 * 1024
DEFAULT_MAX_ENTRY_BYTES = 16 * 1024 * 1024


@dataclass
class _CacheEntry:
    content: str
    size_bytes: int
    mtime_ns: int | None
    file_size: int | None


class FileCache:
    def __init__(
        self,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_entry_bytes: int = DEFAULT_MAX_ENTRY_BYTES,
        max_entries: int | None = None,
    ) -> None:
        if max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        if max_entry_bytes < 0:
            raise ValueError("max_entry_bytes must be non-negative")
        if max_entries is not None and max_entries < 0:
            raise ValueError("max_entries must be non-negative")
        self.max_bytes = max_bytes
        self.max_entry_bytes = max_entry_bytes
        self.max_entries = max_entries
        self._store: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._size_bytes = 0
        self._lock = threading.Lock()

    @staticmethod
    def _file_state(path: str) -> tuple[int, int] | None:
        try:
            stat = os.stat(path)
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def get(self, path: str) -> str | None:
        with self._lock:
            entry = self._store.get(path)
            if entry is None:
                return None

            if entry.mtime_ns is not None:
                state = self._file_state(path)
                if state is None or state != (entry.mtime_ns, entry.file_size):
                    self._remove_locked(path)
                    return None

            self._store.move_to_end(path)
            return entry.content

    def put(self, path: str, content: str, *, file_state: tuple[int, int] | None = None) -> None:
        size_bytes = len(content.encode("utf-8"))
        with self._lock:
            self._remove_locked(path)
            if size_bytes > self.max_entry_bytes or size_bytes > self.max_bytes:
                return

            state = file_state if file_state is not None else self._file_state(path)
            self._store[path] = _CacheEntry(
                content=content,
                size_bytes=size_bytes,
                mtime_ns=state[0] if state else None,
                file_size=state[1] if state else None,
            )
            self._size_bytes += size_bytes
            self._store.move_to_end(path)
            self._evict_locked()

    def invalidate(self, path: str) -> None:
        with self._lock:
            self._remove_locked(path)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._size_bytes = 0

    @property
    def size_bytes(self) -> int:
        with self._lock:
            return self._size_bytes

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)

    def _remove_locked(self, path: str) -> None:
        entry = self._store.pop(path, None)
        if entry is not None:
            self._size_bytes -= entry.size_bytes

    def _evict_locked(self) -> None:
        while self._store and (
            self._size_bytes > self.max_bytes
            or (
                self.max_entries is not None
                and len(self._store) > self.max_entries
            )
        ):
            path, entry = self._store.popitem(last=False)
            self._size_bytes -= entry.size_bytes
