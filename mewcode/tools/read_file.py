from __future__ import annotations

import hashlib
import json
import os
import threading
import stat as stat_module
from typing import TYPE_CHECKING
from typing import Literal

from pydantic import BaseModel, Field

from mewcode.file_fingerprints import (
    RAW_FILE_DIGEST_KIND,
    TEXT_DIGEST_KIND,
)
from mewcode.tools.base import Tool, ToolResult, resolve_tool_path
from mewcode.runtime_workers import io_call

if TYPE_CHECKING:
    from mewcode.cache import FileCache
    from mewcode.tools.file_state_cache import FileStateCache


class Params(BaseModel):
    file_path: str = Field(description="Absolute or relative path to the file to read")
    offset: int = Field(default=0, description="Line offset to start reading from (0-based)")
    limit: int = Field(default=2000, ge=0, description="Maximum number of lines to read; use 0 for fingerprint-only reads")
    fingerprint_mode: Literal["none", "raw", "text", "both"] = Field(
        default="none",
        description=("Optional host-computed SHA-256. raw hashes exact file bytes; text hashes UTF-8 text "
                     "after universal-newline normalization. Use both when reconciling review evidence."),
    )


class ReadFile(Tool):
    name = "ReadFile"
    description = ("Read a file and return its contents with line numbers. It can also return exact-byte and "
                   "normalized-text SHA-256 fingerprints without running an interpreter command.")
    params_model = Params
    category = "read"
    is_concurrency_safe = True


    def __init__(self, file_cache: FileCache | None = None, file_state_cache: FileStateCache | None = None) -> None:
        self._cache = file_cache
        self._state_cache = file_state_cache


    async def execute(self, params: Params) -> ToolResult:
        cancelled = threading.Event()
        try:
            result, snapshot = await io_call(_read_snapshot, params, cancelled)
        except BaseException:
            cancelled.set()
            raise
        # Publish only completed reads, on the owning loop. Cancellation cannot
        # grant read-before-edit access after the caller has already left.
        if snapshot is not None:
            resolved, text, mtime_ns, size = snapshot
            if self._cache is not None:
                self._cache.put(resolved, text, file_state=(mtime_ns, size))
            if self._state_cache is not None:
                self._state_cache.record(resolved, text, mtime_ns)
        return result


def _read_snapshot(params: Params, cancelled: threading.Event):
    path = resolve_tool_path(params.file_path)
    resolved = str(path.absolute())
    observation = {"absolute_path": resolved, "exists": None,
                   "is_file": None, "text_sha256": None}

    def result(output, *, error=False, snapshot=None):
        return ToolResult(output=output, is_error=error,
                          host_observation={"file_state": observation}), snapshot

    try:
        resolved = str(path.resolve(strict=False))
        observation["absolute_path"] = resolved
        file_stat = path.stat()
        observation.update(exists=True, is_file=stat_module.S_ISREG(file_stat.st_mode))
        if not observation["is_file"]:
            return result(f"Error: not a file: {params.file_path}", error=True)
        if cancelled.is_set():
            return result("Read cancelled.", error=True)
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            raw = handle.read()
            after = os.fstat(handle.fileno())
        current = path.stat()
        # Windows fstat ctime is change time while path.stat may expose birth
        # time. Identity, size and last-write time are comparable on both APIs.
        version = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
        if version(before) != version(after) or version(after) != version(current):
            return result("Error reading file: file changed during read; read it again.", error=True)
        fingerprints = {"path": resolved, "bytes": len(raw)}
        if params.fingerprint_mode in {"raw", "both"}:
            fingerprints.update(raw_sha256=hashlib.sha256(raw).hexdigest(),
                                raw_digest_kind=RAW_FILE_DIGEST_KIND)
        if params.fingerprint_mode == "raw" and params.limit == 0:
            return result(json.dumps({"fingerprints": fingerprints, "content": ""}, ensure_ascii=False))
        # Display retains the BOM; the digest follows utf-8-sig and removes
        # exactly one initial BOM. Both derive from these same bytes.
        text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        digest = hashlib.sha256(text.removeprefix("\ufeff").encode("utf-8")).hexdigest()
        observation["text_sha256"] = digest
        selected = text.splitlines()[params.offset:params.offset + params.limit]
        content = "\n".join(f"{i + params.offset + 1}\t{line}" for i, line in enumerate(selected))
        if params.fingerprint_mode in {"text", "both"}:
            fingerprints.update(text_sha256=digest, text_digest_kind=TEXT_DIGEST_KIND)
        output = content if params.fingerprint_mode == "none" else json.dumps(
            {"fingerprints": fingerprints, "content": content}, ensure_ascii=False)
        return result(output, snapshot=(resolved, text, after.st_mtime_ns, len(raw)))
    except FileNotFoundError:
        observation.update(exists=False, is_file=False, text_sha256=None)
        return result(f"Error: file not found: {params.file_path}", error=True)
    except Exception as exc:
        observation["text_sha256"] = None
        return result(f"Error reading file: {exc}", error=True)
