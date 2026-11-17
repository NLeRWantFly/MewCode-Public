"""Freeze existing project Python dependencies without publishing them as edits.

No environment creation, interpreter substitution, links, or execution occurs
here. Commands still use the normal permission and native runtime checks.
"""
from __future__ import annotations

import os
from pathlib import Path

from mewcode.sandbox.lpac_runtime import checked_path
from mewcode.sandbox.staged_workspace import MAX_BYTES, MAX_ENTRIES, _file_digest


def runtime_snapshot(source: Path, destination: Path | None = None) -> dict:
    """Bounded content snapshot of the existing venv trees; optionally copy them."""
    result = {}
    remaining = MAX_BYTES
    pending = [source / name for name in ("venv", ".venv")
               if os.path.lexists(source / name)]
    while pending:
        path = pending.pop()
        checked_path(path)
        relative = path.relative_to(source)
        if len(result) + len(pending) >= MAX_ENTRIES:
            raise ValueError("候选 Python 环境超过文件数量上限")
        target = destination / relative if destination is not None else None
        if path.is_dir():
            if target is not None:
                target.mkdir()
            result[relative.as_posix()] = {"kind": "directory"}
            # Bound enumeration itself, including empty directories.
            children = []
            for child in path.iterdir():
                children.append(child)
                if len(result) + len(pending) + len(children) > MAX_ENTRIES:
                    raise ValueError("候选 Python 环境超过文件数量上限")
            pending.extend(sorted(children, reverse=True))
        else:
            digest, size = _file_digest(path, remaining, target)
            remaining -= size
            result[relative.as_posix()] = {"kind": "file", "size": size, "sha256": digest}
    return result


def freeze_runtime(source: Path, candidate: Path) -> dict:
    frozen = runtime_snapshot(source, candidate)
    if runtime_snapshot(source) != frozen or runtime_snapshot(candidate) != frozen:
        raise ValueError("复制 Python 环境时依赖发生变化，不能沿用该候选")
    return frozen


def verify_runtime(source: Path, candidate: Path, frozen: dict | None) -> None:
    # None denotes an older transaction that did not freeze runtime inputs.
    if frozen is not None and (runtime_snapshot(source) != frozen or runtime_snapshot(candidate) != frozen):
        raise ValueError("候选 Python 环境或原环境已变化，请重新建立候选")
