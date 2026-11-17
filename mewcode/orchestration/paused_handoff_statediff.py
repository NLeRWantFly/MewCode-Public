"""Deterministic file and ledger state diffs for paused handoffs.

The module only proves changes represented by an explicit set of writable
file/directory roots. It never interprets an unrecognized queue or receipt
change as safe. Directory traversal and file hashing are bounded so a large
or unreadable scope fails closed instead of producing a partial baseline.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
MAX_WRITABLE_ROOTS = 128
MAX_FILES = 256
MAX_PATHS = 512
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_QUEUES = 128
MAX_RECEIPTS = 200
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_ID = re.compile(r"^[0-9a-f]{32}$")


def _is_link_or_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


class _StateDiffFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _stable_id(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()[:32]


def _path_text(raw: str | os.PathLike[str], work_dir: str | os.PathLike[str] | None,
               *, require_existing: bool = False) -> str:
    if not isinstance(raw, (str, os.PathLike)):
        raise _StateDiffFailure("invalid_path")
    value = os.fspath(raw)
    if not value or not value.strip():
        raise _StateDiffFailure("invalid_path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        base = Path(work_dir).expanduser() if work_dir is not None else Path.cwd()
        path = base / path
    try:
        resolved = path.resolve(strict=require_existing)
    except (OSError, RuntimeError, ValueError):
        raise _StateDiffFailure("path_unreadable") from None
    return os.path.normpath(str(resolved))


def _root_path_text(raw: str | os.PathLike[str],
                    work_dir: str | os.PathLike[str] | None) -> str:
    """Normalize an explicit root while still detecting a root symlink."""
    if not isinstance(raw, (str, os.PathLike)):
        raise _StateDiffFailure("invalid_path")
    value = os.fspath(raw)
    if not value or not value.strip():
        raise _StateDiffFailure("invalid_path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        base = Path(work_dir).expanduser() if work_dir is not None else Path.cwd()
        path = base / path
    lexical = Path(os.path.abspath(os.fspath(path)))
    try:
        info = lexical.lstat()
    except FileNotFoundError:
        info = None
    except (OSError, ValueError):
        raise _StateDiffFailure("writable_root_unreadable") from None
    if info is not None and _is_link_or_reparse(info):
        raise _StateDiffFailure("symlink_writable_root")
    try:
        return os.path.normpath(str(lexical.resolve(strict=False)))
    except (OSError, RuntimeError, ValueError):
        raise _StateDiffFailure("writable_root_unreadable") from None


def _path_key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _is_within(path: str, root: str) -> bool:
    try:
        return _path_key(os.path.commonpath((path, root))) == _path_key(root)
    except (ValueError, OSError):
        return False


def _valid_limits(*, max_files: int, max_paths: int,
                  max_file_bytes: int,
                  max_total_bytes: int) -> tuple[int, int, int, int]:
    limits = (max_files, max_paths, max_file_bytes, max_total_bytes)
    ceilings = (MAX_FILES, MAX_PATHS, MAX_FILE_BYTES, MAX_TOTAL_BYTES)
    for value, ceiling in zip(limits, ceilings):
        if (isinstance(value, bool) or not isinstance(value, int)
                or value < 1 or value > ceiling):
            raise _StateDiffFailure("invalid_or_excessive_limits")
    return limits


def _queue_revisions(review_queues: Mapping[str, Any]) -> dict[str, int]:
    if not isinstance(review_queues, Mapping) or len(review_queues) > MAX_QUEUES:
        raise _StateDiffFailure("queue_revision_unavailable")
    revisions: dict[str, int] = {}
    for raw_name, value in review_queues.items():
        if not isinstance(raw_name, str) or not raw_name or len(raw_name) > 256:
            raise _StateDiffFailure("queue_revision_unavailable")
        revision = value.get("revision") if isinstance(value, Mapping) else value
        if (isinstance(revision, bool) or not isinstance(revision, int)
                or revision < 0):
            raise _StateDiffFailure("queue_revision_unavailable")
        revisions[raw_name] = revision
    return dict(sorted(revisions.items()))


def _receipt_ids(receipts: Sequence[Mapping[str, Any]]) -> list[str]:
    if (not isinstance(receipts, Sequence) or isinstance(receipts, (str, bytes))
            or len(receipts) > MAX_RECEIPTS):
        raise _StateDiffFailure("receipt_inventory_unavailable")
    ids: list[str] = []
    for item in receipts:
        if not isinstance(item, Mapping):
            raise _StateDiffFailure("receipt_inventory_unavailable")
        identity = item.get("id")
        if not isinstance(identity, str) or not identity or len(identity) > 128:
            raise _StateDiffFailure("receipt_inventory_unavailable")
        ids.append(identity)
    if len(set(ids)) != len(ids):
        raise _StateDiffFailure("receipt_inventory_unavailable")
    return sorted(ids)


def _read_file(path: Path, *, max_file_bytes: int) -> dict[str, Any]:
    try:
        before = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise _StateDiffFailure("non_regular_file_in_scope")
        if before.st_size > max_file_bytes:
            raise _StateDiffFailure("file_size_limit_exceeded")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(min(64 * 1024, max_file_bytes + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > max_file_bytes:
                    raise _StateDiffFailure("file_size_limit_exceeded")
                digest.update(chunk)
        after = path.stat(follow_symlinks=False)
    except _StateDiffFailure:
        raise
    except (OSError, ValueError, RuntimeError):
        raise _StateDiffFailure("file_unreadable") from None
    if (size != before.st_size or size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ino != after.st_ino):
        raise _StateDiffFailure("file_changed_during_capture")
    return {"size": size, "sha256": digest.hexdigest()}


def _scan_roots(raw_roots: Sequence[str | os.PathLike[str]], *,
                work_dir: str | os.PathLike[str] | None,
                max_files: int, max_paths: int,
                max_file_bytes: int, max_total_bytes: int) -> tuple[list[dict[str, str]],
                                               dict[str, dict[str, Any]],
                                               dict[str, str]]:
    if (not isinstance(raw_roots, Sequence)
            or isinstance(raw_roots, (str, bytes))
            or not 1 <= len(raw_roots) <= MAX_WRITABLE_ROOTS):
        raise _StateDiffFailure("writable_scope_unavailable")

    root_paths = [_root_path_text(item, work_dir) for item in raw_roots]
    root_map: dict[str, str] = {}
    for path in root_paths:
        root_map.setdefault(_path_key(path), path)
    roots = sorted(root_map.values(), key=lambda item: (len(Path(item).parts), _path_key(item)))
    root_records: list[dict[str, str]] = []
    files: dict[str, dict[str, Any]] = {}
    directories: dict[str, str] = {}
    visited: set[str] = set()
    total_bytes = 0

    def add_directory(path: Path) -> None:
        canonical = _path_text(path, None, require_existing=True)
        key = _path_key(canonical)
        directories.setdefault(key, canonical)
        if len(files) + len(directories) > max_paths:
            raise _StateDiffFailure("path_count_limit_exceeded")

    def add_file(path: Path) -> None:
        nonlocal total_bytes
        canonical = _path_text(path, None, require_existing=True)
        key = _path_key(canonical)
        if key in files:
            return
        if len(files) >= max_files:
            raise _StateDiffFailure("file_count_limit_exceeded")
        if len(files) + len(directories) + 1 > max_paths:
            raise _StateDiffFailure("path_count_limit_exceeded")
        remaining_bytes = max_total_bytes - total_bytes
        if remaining_bytes < 0:
            raise _StateDiffFailure("total_byte_limit_exceeded")
        state = _read_file(Path(canonical),
                           max_file_bytes=min(max_file_bytes, remaining_bytes))
        total_bytes += state["size"]
        if total_bytes > max_total_bytes:
            raise _StateDiffFailure("total_byte_limit_exceeded")
        files[key] = {"path": canonical, **state}

    def scan_directory(path: Path) -> None:
        canonical = _path_text(path, None, require_existing=True)
        key = _path_key(canonical)
        if key in visited:
            return
        visited.add(key)
        add_directory(Path(canonical))
        try:
            with os.scandir(canonical) as entries:
                children = sorted(entries, key=lambda entry: entry.name)
        except (OSError, ValueError):
            raise _StateDiffFailure("directory_unreadable") from None
        for entry in children:
            try:
                if entry.is_symlink():
                    raise _StateDiffFailure("symlink_in_scope")
                entry_info = entry.stat(follow_symlinks=False)
                if _is_link_or_reparse(entry_info):
                    raise _StateDiffFailure("symlink_in_scope")
                mode = entry_info.st_mode
                child = Path(entry.path)
            except _StateDiffFailure:
                raise
            except (OSError, ValueError):
                raise _StateDiffFailure("directory_entry_unreadable") from None
            if stat.S_ISDIR(mode):
                scan_directory(child)
            elif stat.S_ISREG(mode):
                add_file(child)
            else:
                raise _StateDiffFailure("special_file_in_scope")

    for root in roots:
        candidate = Path(root)
        try:
            initial = candidate.lstat()
        except FileNotFoundError:
            root_records.append({"path": root, "kind": "missing"})
            continue
        except (OSError, ValueError):
            raise _StateDiffFailure("writable_root_unreadable") from None
        if _is_link_or_reparse(initial):
            raise _StateDiffFailure("symlink_writable_root")
        canonical = _path_text(candidate, None, require_existing=True)
        mode = Path(canonical).stat(follow_symlinks=False).st_mode
        if stat.S_ISDIR(mode):
            kind = "directory"
            scan_directory(Path(canonical))
        elif stat.S_ISREG(mode):
            kind = "file"
            add_file(Path(canonical))
        else:
            raise _StateDiffFailure("special_writable_root")
        root_records.append({"path": canonical, "kind": kind})

    root_records.sort(key=lambda item: _path_key(item["path"]))
    return (root_records,
            dict(sorted(files.items())),
            dict(sorted(directories.items())))


def _baseline_payload(*, roots: list[dict[str, str]],
                      files: Mapping[str, Mapping[str, Any]],
                      directories: Mapping[str, str],
                      queue_revisions: Mapping[str, int],
                      receipt_ids: Sequence[str],
                      limits: Mapping[str, int]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ready",
        "writable_roots": roots,
        "files": [dict(files[key]) for key in sorted(files)],
        "directories": [directories[key] for key in sorted(directories)],
        "queue_revisions": dict(sorted(queue_revisions.items())),
        "receipt_ids": list(sorted(receipt_ids)),
        "limits": dict(sorted(limits.items())),
    }


def freeze_paused_handoff_baseline(
        writable_paths: Sequence[str | os.PathLike[str]], *,
        work_dir: str | os.PathLike[str] | None = None,
        review_queues: Mapping[str, Any],
        receipts: Sequence[Mapping[str, Any]],
        max_files: int = MAX_FILES,
        max_paths: int = MAX_PATHS,
        max_file_bytes: int = MAX_FILE_BYTES,
        max_total_bytes: int = MAX_TOTAL_BYTES) -> dict[str, Any]:
    """Freeze file, queue-revision, and receipt-ID state for explicit roots.

    A root may name a file or directory. Directory roots are recursively
    inventoried without following symlinks. Any incomplete or over-limit scan
    returns an unavailable baseline; callers must not use a partial result.
    """
    try:
        limits = _valid_limits(max_files=max_files, max_paths=max_paths,
                               max_file_bytes=max_file_bytes,
                               max_total_bytes=max_total_bytes)
        roots, files, directories = _scan_roots(
            writable_paths, work_dir=work_dir, max_files=limits[0],
            max_paths=limits[1], max_file_bytes=limits[2],
            max_total_bytes=limits[3])
        revisions = _queue_revisions(review_queues)
        ids = _receipt_ids(receipts)
        result = _baseline_payload(
            roots=roots, files=files, directories=directories,
            queue_revisions=revisions, receipt_ids=ids,
            limits={"max_files": limits[0], "max_paths": limits[1],
                    "max_file_bytes": limits[2], "max_total_bytes": limits[3]})
        result["baseline_id"] = _stable_id(result)
        result["clearance"] = None
        return result
    except _StateDiffFailure as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "unavailable",
            "baseline_id": None,
            "reason": exc.code,
            "clearance": {
                "code": "capture_complete_authoritative_baseline",
                "condition": "Restore readable writable roots, queue revisions, and receipt IDs, then freeze again.",
            },
        }


def _validate_baseline(baseline: Any) -> tuple[dict[str, Any], dict[str, dict[str, Any]],
                                                dict[str, str]]:
    if not isinstance(baseline, Mapping) or baseline.get("status") != "ready":
        raise _StateDiffFailure("baseline_missing_or_unavailable")
    if baseline.get("schema_version") != SCHEMA_VERSION:
        raise _StateDiffFailure("baseline_schema_unsupported")
    required = ("writable_roots", "files", "directories", "queue_revisions",
                "receipt_ids", "limits", "baseline_id")
    if any(key not in baseline for key in required):
        raise _StateDiffFailure("baseline_incomplete")

    roots = baseline.get("writable_roots")
    files_raw = baseline.get("files")
    directories_raw = baseline.get("directories")
    queue_revisions = baseline.get("queue_revisions")
    receipt_ids = baseline.get("receipt_ids")
    limits = baseline.get("limits")
    if (not isinstance(roots, list) or not roots
            or len(roots) > MAX_WRITABLE_ROOTS
            or not isinstance(files_raw, list) or len(files_raw) > MAX_FILES
            or not isinstance(directories_raw, list) or len(directories_raw) > MAX_PATHS
            or not isinstance(queue_revisions, Mapping)
            or not isinstance(receipt_ids, list) or len(receipt_ids) > MAX_RECEIPTS
            or not isinstance(limits, Mapping)):
        raise _StateDiffFailure("baseline_incomplete")

    try:
        max_files, max_paths, max_file_bytes, max_total_bytes = _valid_limits(
            max_files=limits.get("max_files"), max_paths=limits.get("max_paths"),
            max_file_bytes=limits.get("max_file_bytes"),
            max_total_bytes=limits.get("max_total_bytes"))
    except _StateDiffFailure:
        raise _StateDiffFailure("baseline_limits_invalid") from None
    if len(files_raw) > max_files or len(files_raw) + len(directories_raw) > max_paths:
        raise _StateDiffFailure("baseline_exceeds_limits")

    normalized_roots: list[dict[str, str]] = []
    for root in roots:
        if (not isinstance(root, Mapping) or not isinstance(root.get("path"), str)
                or root.get("kind") not in {"file", "directory", "missing"}):
            raise _StateDiffFailure("baseline_scope_invalid")
        normalized_roots.append({"path": root["path"], "kind": root["kind"]})
    if len({_path_key(root["path"]) for root in normalized_roots}) != len(normalized_roots):
        raise _StateDiffFailure("baseline_scope_invalid")

    files: dict[str, dict[str, Any]] = {}
    for item in files_raw:
        if (not isinstance(item, Mapping) or not isinstance(item.get("path"), str)
                or not isinstance(item.get("size"), int)
                or isinstance(item.get("size"), bool)
                or item["size"] < 0 or item["size"] > max_file_bytes
                or not isinstance(item.get("sha256"), str)
                or not _SHA256.fullmatch(item["sha256"])):
            raise _StateDiffFailure("baseline_file_entry_invalid")
        key = _path_key(item["path"])
        if key in files:
            raise _StateDiffFailure("baseline_file_entry_invalid")
        files[key] = {"path": item["path"], "size": item["size"],
                      "sha256": item["sha256"]}
    if sum(item["size"] for item in files.values()) > max_total_bytes:
        raise _StateDiffFailure("baseline_exceeds_limits")

    directories: dict[str, str] = {}
    for path in directories_raw:
        if not isinstance(path, str) or not path:
            raise _StateDiffFailure("baseline_directory_entry_invalid")
        key = _path_key(path)
        if key in directories:
            raise _StateDiffFailure("baseline_directory_entry_invalid")
        directories[key] = path

    if any(not any(_is_within(item["path"], root["path"])
                   for root in normalized_roots) for item in files.values()):
        raise _StateDiffFailure("baseline_file_outside_scope")
    if any(not any(_is_within(path, root["path"])
                   for root in normalized_roots) for path in directories.values()):
        raise _StateDiffFailure("baseline_directory_outside_scope")

    try:
        revisions = _queue_revisions(queue_revisions)
        normalized_ids = _receipt_ids([{"id": item} for item in receipt_ids])
    except _StateDiffFailure:
        raise _StateDiffFailure("baseline_ledger_invalid") from None

    payload = _baseline_payload(
        roots=normalized_roots, files=files, directories=directories,
        queue_revisions=revisions, receipt_ids=normalized_ids,
        limits={"max_files": max_files, "max_paths": max_paths,
                "max_file_bytes": max_file_bytes,
                "max_total_bytes": limits["max_total_bytes"]})
    expected_id = _stable_id(payload)
    if baseline.get("baseline_id") != expected_id:
        raise _StateDiffFailure("baseline_integrity_invalid")
    payload["baseline_id"] = expected_id
    return payload, files, directories


def _receipt_record_id_is_valid(item: Mapping[str, Any], path: str) -> bool:
    identity = item.get("id")
    if not isinstance(identity, str) or not _RECEIPT_ID.fullmatch(identity):
        return False
    if item.get("source") not in {"successful_write", "reported_existing"}:
        return False
    if item.get("accepted") is not False:
        return False
    digest = item.get("sha256")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        return False
    size = item.get("size")
    if (isinstance(size, bool) or not isinstance(size, int)
            or not 0 <= size <= MAX_FILE_BYTES):
        return False
    tool_id = item.get("tool_id")
    node_id = item.get("node_id")
    member_id = item.get("member_id", "")
    attempt = item.get("node_attempt")
    if (not isinstance(tool_id, str) or not tool_id
            or not isinstance(node_id, str) or not node_id
            or not isinstance(member_id, str)
            or isinstance(attempt, bool) or not isinstance(attempt, int)
            or attempt < 1):
        return False
    raw = f"{node_id}\0{member_id}\0{attempt}\0{tool_id}\0{path}\0{digest}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32] == identity


def _hold_result(baseline_id: str | None, code: str) -> dict[str, Any]:
    body = {
        "schema_version": SCHEMA_VERSION,
        "baseline_id": baseline_id,
        "classification": "hold",
        "decision": "hold",
        "can_auto_resume": False,
        "reasons": [{"code": code}],
        "changes": [],
        "clearance": {
            "code": "restore_complete_authoritative_state_and_retry",
            "condition": "Re-read a complete baseline and authoritative current state, then rerun the diff.",
        },
    }
    body["diff_id"] = _stable_id(body)
    return body


def compare_paused_handoff_state(
        baseline: Mapping[str, Any] | None, *,
        review_queues: Mapping[str, Any],
        receipts: Sequence[Mapping[str, Any]],
        observed_changed_paths: Sequence[str | os.PathLike[str]] = (),
        work_dir: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Compare current state with a frozen baseline and fail closed if unsure.

    File and directory additions/deletions under every frozen root are found by
    a fresh bounded scan; callers do not need to enumerate in-scope changes.
    ``observed_changed_paths`` is only an optional signal for known paths
    outside the frozen roots. Queue revision deltas remain unknown host effects
    until a separately integrated ledger reconciliation proves their meaning.
    """
    try:
        payload, baseline_files, baseline_dirs = _validate_baseline(baseline)
    except _StateDiffFailure as exc:
        baseline_id = (baseline.get("baseline_id")
                       if isinstance(baseline, Mapping)
                       and isinstance(baseline.get("baseline_id"), str)
                       else None)
        return _hold_result(baseline_id, exc.code)

    roots = payload["writable_roots"]
    limits = payload["limits"]
    try:
        raw_roots = [item["path"] for item in roots]
        _, current_files, current_dirs = _scan_roots(
            raw_roots, work_dir=work_dir, max_files=limits["max_files"],
            max_paths=limits["max_paths"], max_file_bytes=limits["max_file_bytes"],
            max_total_bytes=limits["max_total_bytes"])
        revisions = _queue_revisions(review_queues)
        if not isinstance(receipts, Sequence) or isinstance(receipts, (str, bytes)):
            raise _StateDiffFailure("receipt_inventory_unavailable")
        current_ids = _receipt_ids(receipts)
    except _StateDiffFailure as exc:
        return _hold_result(payload["baseline_id"], exc.code)

    old_ids = set(payload["receipt_ids"])
    current_id_set = set(current_ids)
    if not old_ids.issubset(current_id_set):
        return _hold_result(payload["baseline_id"],
                            "receipt_inventory_truncated_or_replaced")

    current_receipts_by_id = {item["id"]: item for item in receipts}
    new_receipts = [current_receipts_by_id[identity]
                    for identity in sorted(current_id_set - old_ids)]
    def in_scope(path: str) -> bool:
        return any(_is_within(path, root["path"]) for root in roots)

    receipt_rows: dict[str, list[Mapping[str, Any]]] = {}
    external_receipts: list[dict[str, Any]] = []
    unknown_receipts: list[dict[str, Any]] = []
    for receipt in new_receipts:
        if not isinstance(receipt, Mapping):
            return _hold_result(payload["baseline_id"], "receipt_inventory_unavailable")
        source = receipt.get("source")
        try:
            receipt_path = _path_text(receipt.get("path"), work_dir)
        except _StateDiffFailure:
            unknown_receipts.append({"receipt_id": receipt.get("id")})
            continue
        if not _receipt_record_id_is_valid(receipt, receipt_path):
            unknown_receipts.append({"receipt_id": receipt.get("id")})
            continue
        if source == "reported_existing":
            # A validated report of an existing artifact is not evidence of a write.
            continue
        if in_scope(receipt_path):
            receipt_rows.setdefault(_path_key(receipt_path), []).append(receipt)
        else:
            external_receipts.append({"path": receipt_path,
                                      "receipt_id": receipt["id"]})

    if unknown_receipts:
        result = _hold_result(payload["baseline_id"], "unrecognized_new_receipt")
        result["unknown_receipts"] = sorted(
            unknown_receipts, key=lambda item: str(item.get("receipt_id") or ""))
        result["diff_id"] = _stable_id({key: value for key, value in result.items()
                                         if key != "diff_id"})
        return result

    # Hash all changed paths and construct deterministic per-path records.
    changes: list[dict[str, Any]] = []
    all_file_keys = sorted(set(baseline_files) | set(current_files))
    for key in all_file_keys:
        before = baseline_files.get(key)
        after = current_files.get(key)
        if before is not None and after is not None:
            if before["sha256"] == after["sha256"] and before["size"] == after["size"]:
                continue
            change_kind = "modified"
        elif before is None:
            change_kind = "added"
        else:
            change_kind = "deleted"
        path = (after or before)["path"]
        candidates = receipt_rows.get(key, [])
        matching = [item for item in candidates
                    if after is not None and item.get("sha256") == after["sha256"]
                    and item.get("size") == after["size"]]
        if matching:
            classification = "in_scope_receipt_matched"
            receipt_ids_for_change = sorted(item["id"] for item in matching)
            clearance = {"code": "host_receipt_verified",
                         "condition": "A successful host write receipt matches the current path and hash."}
            proposal = None
        else:
            classification = "in_scope_missing_receipt_proposal"
            receipt_ids_for_change = []
            clearance = {"code": "review_import_or_void",
                         "condition": "The review node must decide whether to import or void this change."}
            proposal = {"id": _stable_id({"baseline_id": payload["baseline_id"],
                                           "path": path, "change": change_kind,
                                           "sha256": (after or {}).get("sha256")}),
                        "action": "import_or_void",
                        "status": "pending_review_node",
                        "requires_human_queue": False}
        row = {
            "path": path, "kind": "file", "change": change_kind,
            "baseline_sha256": (before or {}).get("sha256"),
            "current_sha256": (after or {}).get("sha256"),
            "receipt_ids": receipt_ids_for_change,
            "classification": classification,
            "clearance": clearance,
        }
        if proposal is not None:
            row["proposal"] = proposal
        row["id"] = _stable_id(row)
        changes.append(row)

    all_dir_keys = sorted(set(baseline_dirs) | set(current_dirs))
    for key in all_dir_keys:
        before = baseline_dirs.get(key)
        after = current_dirs.get(key)
        if before is not None and after is not None:
            continue
        path = after or before
        change_kind = "added" if before is None else "deleted"
        proposal = {"id": _stable_id({"baseline_id": payload["baseline_id"],
                                       "path": path, "change": change_kind,
                                       "kind": "directory"}),
                    "action": "import_or_void",
                    "status": "pending_review_node",
                    "requires_human_queue": False}
        row = {
            "path": path, "kind": "directory", "change": change_kind,
            "baseline_sha256": None, "current_sha256": None,
            "receipt_ids": [],
            "classification": "in_scope_missing_receipt_proposal",
            "clearance": {"code": "review_import_or_void",
                          "condition": "The review node must decide whether to import or void this directory change."},
            "proposal": proposal,
        }
        row["id"] = _stable_id(row)
        changes.append(row)

    # A verified receipt can document a byte-identical successful rewrite.
    changed_file_keys = {row_key for row_key in all_file_keys
                         if baseline_files.get(row_key) is None
                         or current_files.get(row_key) is None
                         or baseline_files[row_key]["sha256"] != current_files[row_key]["sha256"]
                         or baseline_files[row_key]["size"] != current_files[row_key]["size"]}
    for key in sorted(set(receipt_rows) - changed_file_keys):
        after = current_files.get(key)
        matching = [item for item in receipt_rows[key]
                    if after is not None and item.get("sha256") == after["sha256"]
                    and item.get("size") == after["size"]]
        if not matching:
            continue
        path = after["path"]
        row = {
            "path": path, "kind": "file", "change": "receipt_only",
            "baseline_sha256": (baseline_files.get(key) or {}).get("sha256"),
            "current_sha256": after["sha256"],
            "receipt_ids": sorted(item["id"] for item in matching),
            "classification": "in_scope_receipt_matched",
            "clearance": {"code": "host_receipt_verified",
                          "condition": "A successful host write receipt matches the current path and hash."},
        }
        row["id"] = _stable_id(row)
        changes.append(row)

    # New valid write receipts with no matching current in-scope file still
    # describe an effect and must produce a review proposal, never admission.
    represented_ids = {identity for row in changes for identity in row.get("receipt_ids", [])}
    for key in sorted(receipt_rows):
        unmatched = [item for item in receipt_rows[key]
                     if item["id"] not in represented_ids]
        if not unmatched:
            continue
        path = unmatched[0].get("path")
        current = current_files.get(key)
        proposal = {"id": _stable_id({"baseline_id": payload["baseline_id"],
                                       "path": path, "change": "receipt_mismatch",
                                       "receipt_ids": sorted(item["id"] for item in unmatched)}),
                    "action": "import_or_void",
                    "status": "pending_review_node",
                    "requires_human_queue": False}
        row = {
            "path": path, "kind": "file", "change": "receipt_mismatch",
            "baseline_sha256": (baseline_files.get(key) or {}).get("sha256"),
            "current_sha256": (current or {}).get("sha256"),
            "receipt_ids": sorted(item["id"] for item in unmatched),
            "classification": "in_scope_missing_receipt_proposal",
            "clearance": {"code": "review_import_or_void",
                          "condition": "The review node must resolve a receipt that does not match current file bytes."},
            "proposal": proposal,
        }
        row["id"] = _stable_id(row)
        changes.append(row)

    queue_changes = []
    old_revisions = payload["queue_revisions"]
    for name in sorted(set(old_revisions) | set(revisions)):
        old = old_revisions.get(name)
        current = revisions.get(name)
        if old == current:
            continue
        row = {"queue": name, "baseline_revision": old,
               "current_revision": current,
               "classification": "unknown_external_effect",
               "clearance": {"code": "reconcile_authoritative_queue_revision",
                             "condition": "Reconcile the queue change against its authoritative host ledger before retrying."}}
        row["id"] = _stable_id(row)
        queue_changes.append(row)
        changes.append(row)

    outside_paths: set[str] = set()
    try:
        if (not isinstance(observed_changed_paths, Sequence)
                or isinstance(observed_changed_paths, (str, bytes))):
            raise _StateDiffFailure("observed_path_inventory_invalid")
        for raw_path in observed_changed_paths:
            path = _path_text(raw_path, work_dir)
            if not in_scope(path):
                outside_paths.add(path)
    except _StateDiffFailure as exc:
        return _hold_result(payload["baseline_id"], exc.code)
    outside_paths.update(item["path"] for item in external_receipts)
    for path in sorted(outside_paths, key=_path_key):
        row = {"path": path, "kind": "outside_scope", "change": "observed",
               "classification": "outside_scope",
               "clearance": {"code": "excluded_outside_frozen_scope",
                             "condition": "This observed path is outside the frozen writable roots."}}
        row["id"] = _stable_id(row)
        changes.append(row)

    changes.sort(key=lambda item: (str(item.get("path") or item.get("queue") or ""),
                                  str(item.get("change") or ""), item["id"]))
    has_unknown = bool(queue_changes)
    has_proposal = any(item.get("classification") == "in_scope_missing_receipt_proposal"
                       for item in changes)
    has_match = any(item.get("classification") == "in_scope_receipt_matched"
                    for item in changes)
    has_outside = any(item.get("classification") == "outside_scope" for item in changes)

    if has_unknown:
        classification = "hold"
        decision = "hold"
        can_auto_resume = False
        clearance = {"code": "reconcile_unknown_external_effect",
                     "condition": "Resolve queue revision changes against the authoritative ledger before resuming."}
    elif has_proposal:
        classification = "in_scope_missing_receipt_proposal"
        decision = "propose_import_or_void"
        can_auto_resume = False
        clearance = {"code": "review_import_or_void",
                     "condition": "The review node must decide whether to import or void each proposed change."}
    elif has_match:
        classification = "in_scope_receipt_matched"
        decision = "resume"
        can_auto_resume = True
        clearance = {"code": "host_receipt_verified",
                     "condition": "Every in-scope file change matches a successful host receipt."}
    elif has_outside:
        classification = "outside_scope"
        decision = "resume"
        can_auto_resume = True
        clearance = {"code": "excluded_outside_frozen_scope",
                     "condition": "Observed changes are outside the frozen writable roots."}
    else:
        classification = "no_change"
        decision = "resume"
        can_auto_resume = True
        clearance = {"code": "no_change_detected",
                     "condition": "Tracked files, queue revisions, and receipt inventory are unchanged."}

    result = {
        "schema_version": SCHEMA_VERSION,
        "baseline_id": payload["baseline_id"],
        "classification": classification,
        "decision": decision,
        "can_auto_resume": can_auto_resume,
        "changes": changes,
        "clearance": clearance,
    }
    result["diff_id"] = _stable_id(result)
    return result


__all__ = [
    "MAX_FILE_BYTES", "MAX_FILES", "MAX_PATHS", "MAX_RECEIPTS",
    "MAX_TOTAL_BYTES",
    "MAX_WRITABLE_ROOTS", "compare_paused_handoff_state",
    "freeze_paused_handoff_baseline",
]
