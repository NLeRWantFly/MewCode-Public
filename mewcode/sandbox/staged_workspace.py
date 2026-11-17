"""Host-owned preparation and validation for an isolated writable copy.

This module never executes commands, grants ACLs, or publishes to the source.
A verified plan is NOT a permission or a commit: publication still needs a
live write lease, path-handle guards, CAS revalidation and a recovery journal.
Keeping that distinction explicit lets the native writable-copy transport be
tested without weakening the production gate or the original workspace ACLs.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from mewcode.sandbox.lpac_runtime import NativeScope, checked_path
from mewcode.sandbox.receipt import NativeExecutionReceipt
from mewcode.sandbox.windows import NativeBoundaryUnavailable

SCHEMA = "mewcode-staged-workspace-v1"
MAX_ENTRIES = 12000
MAX_BYTES = 512 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
_RUNTIME_NAMES = frozenset({".venv", "venv"})


class StagingRejected(NativeBoundaryUnavailable):
    pass


@dataclass(frozen=True)
class Entry:
    relative: str
    kind: str
    size: int
    sha256: str
    device: int
    inode: int
    writable: bool

    @property
    def content(self) -> tuple:
        return self.kind, self.size, self.sha256


@dataclass(frozen=True)
class Change:
    relative: str
    operation: str
    before_sha256: str | None
    after_sha256: str | None


@dataclass(frozen=True)
class StagedPlan:
    transaction: Path
    source: Path
    workspace: Path
    policy_revision: str
    baseline: tuple[Entry, ...]
    manifest_sha256: str
    max_entries: int
    max_bytes: int


@dataclass(frozen=True)
class VerifiedChanges:
    changes: tuple[Change, ...]
    staged_digest: str
    # No "approved"/"committed" field: this result cannot masquerade as a
    # successful host write receipt or release the original write ownership.
    state: str = "verified_unpublished"


def _encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _revision(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise StagingRejected("A host policy revision SHA-256 is required")
    return value


def _write_new(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _event(transaction: Path, state: str, **fields) -> None:
    path = transaction / "events.jsonl"
    # A private host path; no stage worker ever receives this directory.
    if os.path.lexists(path):
        checked_path(path, file=True)
    with path.open("ab") as stream:
        stream.write(_encoded({"state": state, **fields}) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def _signature(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _identity_and_content_stamp(info: os.stat_result) -> tuple:
    # Windows Python's path and fd stat APIs can report different meanings
    # for ctime (creation vs metadata-change time). Compare ctime only within
    # the same API; never weaken the file ID, size or modification checks.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _file_digest(path: Path, budget: int, destination: Path | None = None) -> tuple[str, int]:
    """Bounded streaming copy; no copytree, link following, or opaque metadata."""
    before = path.lstat()
    if before.st_size > budget:
        raise StagingRejected("Workspace exceeds the byte budget; split the task")
    digest = hashlib.sha256()
    size = 0
    output = None
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _identity_and_content_stamp(opened) != _identity_and_content_stamp(before):
                raise StagingRejected("Input identity changed while opening the snapshot")
            if destination is not None:
                output = destination.open("xb")
            while chunk := stream.read(min(CHUNK_BYTES, budget - size + 1)):
                size += len(chunk)
                if size > budget:
                    raise StagingRejected("Workspace exceeds the byte budget; split the task")
                digest.update(chunk)
                if output:
                    output.write(chunk)
            if (_signature(os.fstat(stream.fileno())) != _signature(opened)
                    or _signature(path.lstat()) != _signature(before)):
                raise StagingRejected("Input changed while taking the snapshot")
            if output:
                output.flush()
                os.fsync(output.fileno())
    finally:
        if output:
            output.close()
    return digest.hexdigest(), size


def _walk(root: Path, scope: NativeScope, *, copy_to: Path | None = None,
          staged: bool = False, max_entries: int = MAX_ENTRIES,
          max_bytes: int = MAX_BYTES) -> tuple[Entry, ...]:
    total = 0
    entries: list[Entry] = []
    pending = [root]
    while pending:
        path = pending.pop()
        relative = path.relative_to(root)
        original = scope.workspace / relative
        if not scope.allows(original, write=False):
            if staged:
                raise StagingRejected("Staged output contains an unreadable or protected path")
            continue
        checked_path(path)
        info = path.lstat()
        directory = stat.S_ISDIR(info.st_mode)
        destination = copy_to / relative if copy_to is not None else None
        digest, size = "", 0
        if directory:
            if destination is not None and relative != Path("."):
                destination.mkdir()
            children = []
            for child in path.iterdir():
                children.append(child)
                if len(children) + len(entries) + len(pending) >= max_entries:
                    raise StagingRejected("Workspace exceeds the entry budget; split the task")
            children.sort(reverse=True)
            pending.extend(children)
        else:
            digest, size = _file_digest(path, max_bytes - total, destination)
            total += size
        writable = scope.allows(original, write=True) and not any(
            part.casefold() in _RUNTIME_NAMES for part in relative.parts)
        entries.append(Entry(relative.as_posix(), "directory" if directory else "file",
                             size, digest, info.st_dev, info.st_ino, writable))
        if len(entries) > max_entries:
            raise StagingRejected("Workspace exceeds the entry budget; split the task")
    if not entries or entries[0].relative != ".":
        raise StagingRejected("The workspace is not readable")
    return tuple(sorted(entries, key=lambda entry: entry.relative))


def prepare_workspace(scope: NativeScope, host_parent: Path, *, policy_revision: str,
                      max_entries: int = MAX_ENTRIES, max_bytes: int = MAX_BYTES) -> StagedPlan:
    """Copy readable inputs to a fresh private transaction, leaving originals intact.

    policy_revision must come from the host's complete policy snapshot, never
    a tool argument. This preparer is not a substitute for handle-based
    publication guards; it exposes no write-back or production execution API.
    """
    import tempfile
    revision = _revision(policy_revision)
    if not scope.current():
        raise StagingRejected("The host execution scope changed")
    source = checked_path(scope.workspace)
    parent = checked_path(host_parent)
    if (not parent.is_dir() or parent == source or parent.is_relative_to(source)
            or source.is_relative_to(parent) or parent == Path(parent.anchor)):
        raise StagingRejected("Host staging and source must be disjoint bounded directories")
    if (type(max_entries) is not int or type(max_bytes) is not int
            or not (0 < max_entries <= MAX_ENTRIES and 0 < max_bytes <= MAX_BYTES)):
        raise StagingRejected("Invalid staging bounds")
    transaction = Path(tempfile.mkdtemp(prefix="stage-", dir=parent))
    workspace = transaction / "workspace"
    workspace.mkdir()
    _event(transaction, "preparing")
    try:
        baseline = _walk(source, scope, copy_to=workspace,
                         max_entries=max_entries, max_bytes=max_bytes)
        if not scope.current() or baseline != _walk(source, scope, max_entries=max_entries, max_bytes=max_bytes):
            raise StagingRejected("Source or policy changed during preparation")
        manifest = _encoded({"schema": SCHEMA, "source": str(source), "workspace": str(workspace),
                             "max_entries": max_entries, "max_bytes": max_bytes,
                             "policy_revision": revision, "baseline": [asdict(e) for e in baseline]})
        _write_new(transaction / "manifest.json", manifest)
        _event(transaction, "prepared", manifest_sha256=_hash(manifest))
        return StagedPlan(transaction, source, workspace, revision, baseline, _hash(manifest), max_entries, max_bytes)
    except BaseException:
        _event(transaction, "preparation_failed")
        raise


def verify_workspace(plan: StagedPlan, scope: NativeScope, receipt: NativeExecutionReceipt, *,
                     policy_revision: str) -> VerifiedChanges:
    """Validate candidate changes only; never apply them or authorize a commit."""
    try:
        if (receipt.status != "exited" or receipt.exit_code != 0
                or receipt.cleanup_confirmed is not True or receipt.output_truncated
                or receipt.backend not in {"mewcode-windows-lpac", "mewcode-windows-srt"}):
            raise StagingRejected("Successful native execution and confirmed cleanup are required")
        if (not scope.current() or scope.workspace != plan.source
                or _revision(policy_revision) != plan.policy_revision):
            raise StagingRejected("The host policy or task changed")
        manifest_path = checked_path(plan.transaction / "manifest.json", file=True)
        if manifest_path.stat().st_size > 8 * 1024 * 1024:
            raise StagingRejected("The private manifest is oversized")
        with manifest_path.open("rb") as stream:
            manifest = stream.read(8 * 1024 * 1024 + 1)
        if _hash(manifest) != plan.manifest_sha256:
            raise StagingRejected("The private manifest changed")
        bounds = {"max_entries": plan.max_entries, "max_bytes": plan.max_bytes}
        if _walk(plan.source, scope, **bounds) != plan.baseline:
            raise StagingRejected("Original workspace changed; reconciliation is required")
        staged = _walk(checked_path(plan.workspace), scope, staged=True, **bounds)
        before = {entry.relative: entry for entry in plan.baseline}
        after = {entry.relative: entry for entry in staged}
        changes = []
        for relative in sorted(before.keys() | after.keys()):
            old, new = before.get(relative), after.get(relative)
            if old and new and old.content == new.content:
                continue
            if old and new and old.kind != new.kind:
                raise StagingRejected("File/directory type changes require explicit reconciliation")
            if (old is not None and not old.writable) or (new is not None and not new.writable):
                raise StagingRejected("Candidate changes a read-only or protected path")
            if old is None:
                # New descendants need an already writable ancestor from the
                # frozen baseline as well as the current policy decision.
                ancestors = Path(relative).parents
                if not any((entry := before.get(p.as_posix())) is not None
                           and entry.kind == "directory" and entry.writable for p in ancestors):
                    raise StagingRejected("New output has no frozen writable parent")
            changes.append(Change(relative, "create" if old is None else "delete" if new is None else "modify",
                                  old.sha256 if old else None, new.sha256 if new else None))
        digest = _hash(_encoded([asdict(entry) for entry in staged]))
        result = VerifiedChanges(tuple(changes), digest)
        _event(plan.transaction, result.state, staged_digest=digest,
               changes=[asdict(change) for change in changes])
        return result
    except (OSError, ValueError, NativeBoundaryUnavailable):
        _event(plan.transaction, "verification_rejected")
        raise
