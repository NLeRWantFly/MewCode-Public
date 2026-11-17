"""Bounded, read-only artifact inventory for environment-fault recovery.

No file content enters the recovery prompt. The host resolves relative paths,
checks small magic headers and recommends only tools already available to the
node. This is orientation evidence, never a write grant or business proof.
"""
from __future__ import annotations

import os
from collections import deque
from itertools import islice
from pathlib import Path
from typing import Any

POLICY_VERSION = "artifact-inventory-v1"
SKIP_DIRECTORIES = frozenset({".git", ".venv", "venv", "node_modules", ".mewcode",
                              ".codex", ".agents", ".desktop:example_provider_4", ".ssh",
                              "__pycache__", "dist", "build"})
TEXT_SUFFIXES = frozenset({".txt", ".md", ".json", ".jsonl", ".csv", ".tsv",
                           ".yaml", ".yml", ".xml", ".py", ".js", ".ts", ".html"})


def _kind(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            header = handle.read(32)
    except OSError:
        return "unreadable"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"%PDF-"):
        return "application/pdf"
    if header.startswith(b"PK\x03\x04") and path.suffix.casefold() == ".docx":
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if b"\x00" in header:
        return "binary/unknown"
    if path.suffix.casefold() in TEXT_SUFFIXES:
        return "text/plain"
    return "unknown"


def _tool_for(kind: str, available_tools: set[str]) -> str | None:
    if kind.startswith("image/") or kind == "application/pdf" or kind.startswith("application/vnd.openxmlformats"):
        return "ReadAuditArtifact" if "ReadAuditArtifact" in available_tools or "*" in available_tools else None
    if kind == "text/plain":
        return ("ReadFile" if "ReadFile" in available_tools or "*" in available_tools else
                "ReadAuditArtifact" if "ReadAuditArtifact" in available_tools else None)
    return None


def artifact_inventory(root: str | Path, *, available_tools: list[str] | tuple[str, ...],
                       failed_paths: list[str] | None = None, max_files: int = 48,
                       max_entries: int = 1024, max_depth: int = 6) -> dict[str, Any]:
    """Find bounded workspace files without following symlinks or reading bodies."""
    workspace = Path(root).resolve(strict=True)
    if (not workspace.is_dir() or not 1 <= max_files <= 128
            or not max_files <= max_entries <= 4096 or not 0 <= max_depth <= 8):
        raise ValueError("invalid bounded workspace inventory request")
    targets = {Path(raw.replace("\\", "/")).name.casefold()
               for raw in failed_paths or [] if isinstance(raw, str) and raw}
    pending = deque([(workspace, 0)])
    found: list[dict[str, Any]] = []
    inspected = 0
    truncated = False
    tools = set(available_tools)
    while pending and inspected < max_entries:
        directory, depth = pending.popleft()
        try:
            remaining = max_entries - inspected
            with os.scandir(directory) as stream:
                sampled = list(islice(stream, remaining + 1))
            if len(sampled) > remaining:
                truncated = True
            entries = sorted(sampled[:remaining], key=lambda row: row.name.casefold())
        except OSError:
            continue
        for entry in entries:
            inspected += 1
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.casefold() not in SKIP_DIRECTORIES:
                        if depth < max_depth:
                            pending.append((Path(entry.path), depth + 1))
                        else:
                            truncated = True
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                if entry.name.casefold() == ".env" or entry.name.casefold().startswith(".env."):
                    continue
                path = Path(entry.path)
                relative = path.relative_to(workspace).as_posix()
                size = entry.stat(follow_symlinks=False).st_size
            except (OSError, ValueError):
                continue
            kind = _kind(path)
            found.append({"path": relative, "kind": kind,
                          "size_bytes": size, "read_tool": _tool_for(kind, tools)})
    if pending:
        truncated = True
    found.sort(key=lambda row: (0 if Path(row["path"]).name.casefold() in targets else 1,
                                row["path"].casefold()))
    if len(found) > max_files:
        truncated = True
    return {"schema_version": 1, "policy_version": POLICY_VERSION,
            "files": found[:max_files], "inspected_entries": inspected,
            "truncated": truncated, "matched_failed_basename": sum(
                Path(row["path"]).name.casefold() in targets for row in found[:max_files]),
            "authority_change": False, "business_evidence": False}
