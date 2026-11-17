from __future__ import annotations

import fnmatch
import os
import threading
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

from mewcode.permissions.sensitive_paths import SensitiveReadFilter
from mewcode.tools.base import SKIP_DIRS, active_work_dir


def _skip_directory(name: str) -> bool:
    """Prune generated trees before walking them.

    Checking a path only after ``Path.glob`` yields it is too late: glob has
    already traversed the complete subtree.  Some workspaces contain hundreds
    of temporary test/artifact directories, which used to monopolize the web
    server's event loop for minutes.
    """

    # Match ripgrep's useful default: a search rooted at the workspace ignores
    # hidden directories.  A caller can still search one explicitly by using it
    # as ``base``; only descendants are pruned here.
    return (
        name in SKIP_DIRS
        or name.startswith(".")
        or name == "out"
        or name.startswith("out-")
    )


def _matches_glob(relative_path: Path, pattern: str) -> bool:
    """Match a project-relative path with pathlib-style ``**`` semantics."""

    path_parts = tuple(relative_path.as_posix().split("/"))
    normalized = pattern.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    pattern_parts = tuple(part for part in normalized.split("/") if part)
    if not pattern_parts:
        return False

    @lru_cache(maxsize=None)
    def match(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        part = pattern_parts[pattern_index]
        if part == "**":
            return match(path_index, pattern_index + 1) or (
                path_index < len(path_parts)
                and match(path_index + 1, pattern_index)
            )
        return (
            path_index < len(path_parts)
            and fnmatch.fnmatchcase(path_parts[path_index], part)
            and match(path_index + 1, pattern_index + 1)
        )

    return match(0, 0)


def iter_matching_files(
    base: Path,
    pattern: str,
    cancel_requested: threading.Event,
) -> Iterator[tuple[Path, Path]]:
    """Yield ``(absolute, relative)`` files without entering ignored trees.

    This iterator is intentionally synchronous: callers run it in a worker
    thread.  The cancellation event lets that thread finish promptly after the
    owning asyncio task has been cancelled.
    """

    sensitive = SensitiveReadFilter(workspace_root=active_work_dir()).classify
    if base.is_file():
        relative = Path(base.name)
        if (not cancel_requested.is_set()
                and not sensitive(base).sensitive
                and _matches_glob(relative, pattern)):
            yield base, relative
        return

    def ignore_walk_error(_error: OSError) -> None:
        return None

    for root, directories, filenames in os.walk(
        base,
        topdown=True,
        onerror=ignore_walk_error,
        followlinks=False,
    ):
        if cancel_requested.is_set():
            return
        directories[:] = sorted(
            name for name in directories
            if not _skip_directory(name)
            and not sensitive(Path(root) / name).sensitive
        )
        root_path = Path(root)
        for filename in sorted(filenames):
            if cancel_requested.is_set():
                return
            absolute = root_path / filename
            if sensitive(absolute).sensitive:
                continue
            try:
                relative = absolute.relative_to(base)
            except ValueError:
                continue
            if _matches_glob(relative, pattern):
                yield absolute, relative
