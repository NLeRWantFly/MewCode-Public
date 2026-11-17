"""Host-owned, durable path leases for orchestration write coordination.

This is an application-level coordination primitive. It does not replace or
claim to be an operating-system/kernel sandbox. Every lease request is checked
against the supplied PathSandbox's existing writable scope; acquiring a lease
never adds a file or directory grant.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import stat
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1
MAX_LEASE_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_EXACT_PATHS = 256
MAX_DIRECTORY_PATHS = 32
MAX_TOTAL_SCOPES = 256
MAX_STORED_LEASES = 4096
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_IDENTITY_FIELDS = ("owner_id", "run_id", "conversation_id")
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class WriteLeaseError(RuntimeError):
    """Invalid lease request or unreadable persistent lease state."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _stable_id(value: Any, *, length: int = 32) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()[:length]


def _path_key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _reject_parent_segments(path: Path) -> None:
    if ".." in path.parts:
        raise WriteLeaseError("parent_segment_forbidden")


def _is_reparse(info: os.stat_result) -> bool:
    return (stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0)
                    & _FILE_ATTRIBUTE_REPARSE_POINT))


def _check_components(path: Path, *, allow_missing: bool) -> bool:
    """Reject symlink/reparse components and return whether the target exists."""
    parts = path.parts
    if not parts:
        raise WriteLeaseError("path_invalid")
    current = Path(parts[0])
    missing = False
    for part in parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            missing = True
            if not allow_missing:
                raise WriteLeaseError("path_missing") from None
            continue
        except (OSError, ValueError, RuntimeError):
            raise WriteLeaseError("path_unreadable") from None
        if missing:
            raise WriteLeaseError("path_component_reappeared")
        if _is_reparse(info):
            raise WriteLeaseError("symlink_or_reparse_path")
    if missing:
        return False
    try:
        info = path.lstat()
    except (OSError, ValueError, RuntimeError):
        raise WriteLeaseError("path_unreadable") from None
    if _is_reparse(info):
        raise WriteLeaseError("symlink_or_reparse_path")
    return True


def _as_sequence(value: Sequence[str | os.PathLike[str]], *, field: str,
                 max_count: int) -> list[str | os.PathLike[str]]:
    if (not isinstance(value, Sequence) or isinstance(value, (str, bytes))
            or len(value) > max_count):
        raise WriteLeaseError(f"{field}_invalid")
    return list(value)


def _identity(owner_id: str, run_id: str,
              conversation_id: str) -> dict[str, str]:
    result = {}
    for name, value in zip(_IDENTITY_FIELDS, (owner_id, run_id, conversation_id)):
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise WriteLeaseError(f"{name}_invalid")
        result[name] = value
    return result


def _same_identity(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(left.get(name) == right.get(name) for name in _IDENTITY_FIELDS)


def _overlap(left_path: str, left_kind: str,
             right_path: str, right_kind: str) -> bool:
    left_key, right_key = _path_key(left_path), _path_key(right_path)
    left_directory = left_kind == "directory"
    right_directory = right_kind == "directory"
    if not left_directory and not right_directory:
        # Exact files and entry leases both reserve only the named filesystem
        # object. An entry lease on a directory does not reserve its children.
        return left_key == right_key
    if left_directory and right_directory:
        return (_is_relative_to(left_path, right_path)
                or _is_relative_to(right_path, left_path))
    if left_directory:
        return _is_relative_to(right_path, left_path)
    return _is_relative_to(left_path, right_path)


def _is_relative_to(path: str, root: str) -> bool:
    try:
        Path(path).relative_to(Path(root))
        return True
    except (ValueError, OSError):
        return False


def _scope_records(lease: Mapping[str, Any]) -> list[tuple[str, str]]:
    result = [(path, "exact") for path in lease.get("exact_paths", [])]
    result.extend((path, "entry") for path in lease.get("entry_paths", []))
    result.extend((path, "directory") for path in lease.get("directory_paths", []))
    return result


def _thread_lock_for(path: Path) -> threading.RLock:
    key = _path_key(str(path))
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[key] = lock
        return lock


class WriteLeaseStore:
    """Persist and compare-and-swap write leases in one host-owned JSON file.

    ``sandbox`` must be the same PathSandbox used to authorize the work. Exact
    paths may name a file that does not exist yet, but its parent must exist;
    directory leases require an existing non-root directory. Symlink and
    Windows reparse components are rejected before a path is recorded.
    """

    def __init__(self, state_path: str | os.PathLike[str], *, sandbox,
                 max_ttl_seconds: int = MAX_LEASE_TTL_SECONDS):
        if (isinstance(max_ttl_seconds, bool) or not isinstance(max_ttl_seconds, int)
                or not 1 <= max_ttl_seconds <= MAX_LEASE_TTL_SECONDS):
            raise WriteLeaseError("max_ttl_invalid")
        candidate = Path(state_path).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        try:
            _reject_parent_segments(candidate)
            # Keep the lexical path so component checks can reject a symlink
            # supplied by the caller instead of silently following it.
            self.path = Path(os.path.abspath(str(candidate)))
        except (OSError, RuntimeError, ValueError):
            raise WriteLeaseError("state_path_invalid") from None
        if self.path.name in {"", ".", ".."} or self.path.parent == self.path:
            raise WriteLeaseError("state_path_invalid")
        if self.path.parent == Path(self.path.anchor):
            raise WriteLeaseError("state_path_at_filesystem_root")
        self.lock_path = Path(str(self.path) + ".lock")
        self.sandbox = sandbox
        self.max_ttl_seconds = max_ttl_seconds
        self._thread_lock = _thread_lock_for(self.path)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._thread_lock.acquire()
        handle = None
        locked = False
        try:
            try:
                self._reject_reparse_store_path(self.path.parent, allow_missing=False)
                if not self.path.parent.is_dir():
                    raise WriteLeaseError("lease_store_parent_not_directory")
                self._reject_reparse_store_path(self.path, allow_missing=True)
                if self.path.exists() and not self.path.is_file():
                    raise WriteLeaseError("lease_store_path_invalid")
                self._reject_reparse_store_path(self.lock_path, allow_missing=True)
                if self.lock_path.exists() and not self.lock_path.is_file():
                    raise WriteLeaseError("lease_store_lock_path_invalid")
                handle = open(self.lock_path, "a+b")
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                locked = True
            except WriteLeaseError:
                raise
            except (OSError, ValueError, RuntimeError):
                raise WriteLeaseError("lease_store_lock_failed") from None
            yield
        finally:
            if handle is not None:
                if locked:
                    try:
                        if os.name == "nt":
                            import msvcrt
                            handle.seek(0)
                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
                handle.close()
            self._thread_lock.release()

    @staticmethod
    def _reject_reparse_store_path(path: Path, *, allow_missing: bool) -> None:
        absolute = Path(os.path.abspath(str(path)))
        try:
            _check_components(absolute, allow_missing=allow_missing)
        except WriteLeaseError as exc:
            raise WriteLeaseError("lease_store_path_unsafe", str(exc)) from None

    def _read_state(self) -> dict[str, Any]:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return {"schema_version": SCHEMA_VERSION, "revision": 0, "leases": []}
        except (OSError, ValueError, RuntimeError):
            raise WriteLeaseError("lease_state_unreadable") from None
        if _is_reparse(info) or not stat.S_ISREG(info.st_mode):
            raise WriteLeaseError("lease_state_unsafe")
        if info.st_size > 16 * 1024 * 1024:
            raise WriteLeaseError("lease_state_too_large")
        try:
            raw = self.path.read_bytes()
            data = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            raise WriteLeaseError("lease_state_unreadable") from None
        if (not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION
                or isinstance(data.get("revision"), bool)
                or not isinstance(data.get("revision"), int)
                or data["revision"] < 0
                or not isinstance(data.get("leases"), list)
                or len(data["leases"]) > MAX_STORED_LEASES):
            raise WriteLeaseError("lease_state_invalid")
        seen_ids: set[str] = set()
        for lease in data["leases"]:
            self._validate_stored_lease(lease)
            if lease["lease_id"] in seen_ids:
                raise WriteLeaseError("lease_state_invalid")
            seen_ids.add(lease["lease_id"])
        for index, left in enumerate(data["leases"]):
            for right in data["leases"][index + 1:]:
                if (not _same_identity(left["holder"], right["holder"])
                        and any(_overlap(left_path, left_kind, right_path, right_kind)
                                for left_path, left_kind in _scope_records(left)
                                for right_path, right_kind in _scope_records(right))):
                    raise WriteLeaseError("lease_state_conflict")
        return data

    def _validate_stored_lease(self, lease: Any) -> None:
        if not isinstance(lease, dict):
            raise WriteLeaseError("lease_state_invalid")
        lease_id = lease.get("lease_id")
        holder = lease.get("holder")
        created_at, expires_at = lease.get("created_at"), lease.get("expires_at")
        exact_paths, directory_paths = lease.get("exact_paths"), lease.get("directory_paths")
        # Missing entry_paths is the version-one durable representation.
        entry_paths = lease.get("entry_paths", [])
        if (not isinstance(lease_id, str) or not lease_id
                or not isinstance(holder, dict)
                or any(not isinstance(holder.get(field), str) or not holder[field]
                       for field in _IDENTITY_FIELDS)
                or isinstance(created_at, bool) or not isinstance(created_at, (int, float))
                or not math.isfinite(created_at)
                or isinstance(expires_at, bool) or not isinstance(expires_at, (int, float))
                or not math.isfinite(expires_at) or expires_at <= created_at
                or not isinstance(exact_paths, list)
                or not isinstance(entry_paths, list)
                or not isinstance(directory_paths, list)
                or len(exact_paths) + len(entry_paths) + len(directory_paths) > MAX_TOTAL_SCOPES):
            raise WriteLeaseError("lease_state_invalid")
        if (len(exact_paths) > MAX_EXACT_PATHS or len(entry_paths) > MAX_TOTAL_SCOPES
                or len(directory_paths) > MAX_DIRECTORY_PATHS):
            raise WriteLeaseError("lease_state_invalid")
        scopes: list[tuple[str, str]] = []
        for kind, paths in (("exact", exact_paths), ("entry", entry_paths),
                            ("directory", directory_paths)):
            for stored_path in paths:
                if not isinstance(stored_path, str) or not stored_path:
                    raise WriteLeaseError("lease_state_invalid")
                path = Path(stored_path)
                if not path.is_absolute():
                    raise WriteLeaseError("lease_state_invalid")
                try:
                    self._check_stored_scope_path(path, kind)
                except WriteLeaseError:
                    raise WriteLeaseError("lease_state_unsafe") from None
                scopes.append((stored_path, kind))
        if not scopes:
            raise WriteLeaseError("lease_state_invalid")
        for index, (left_path, left_kind) in enumerate(scopes):
            if any(_overlap(left_path, left_kind, right_path, right_kind)
                   for right_path, right_kind in scopes[index + 1:]):
                raise WriteLeaseError("lease_state_invalid")

    def _check_stored_scope_path(self, path: Path, kind: str) -> None:
        absolute = Path(os.path.abspath(str(path)))
        exists = _check_components(absolute, allow_missing=True)
        if kind == "directory" and exists:
            try:
                if not absolute.is_dir():
                    raise WriteLeaseError("stored_directory_not_directory")
            except OSError:
                raise WriteLeaseError("stored_directory_unreadable") from None
        if kind == "exact" and exists:
            try:
                if not absolute.is_file():
                    raise WriteLeaseError("stored_exact_not_file")
            except OSError:
                raise WriteLeaseError("stored_exact_unreadable") from None
        if kind == "entry" and exists:
            try:
                if not (absolute.is_file() or absolute.is_dir()):
                    raise WriteLeaseError("stored_entry_not_file_or_directory")
            except OSError:
                raise WriteLeaseError("stored_entry_unreadable") from None
        try:
            canonical = absolute.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            raise WriteLeaseError("stored_scope_unreadable") from None
        if _path_key(str(canonical)) != _path_key(str(path)):
            raise WriteLeaseError("stored_scope_changed")
        if kind == "entry":
            self._reject_broad_entry_target(canonical)

    def _normalize_scope_path(self, raw: str | os.PathLike[str], *,
                              kind: str) -> str:
        if not isinstance(raw, (str, os.PathLike)):
            raise WriteLeaseError("path_invalid")
        text = os.fspath(raw)
        if not text or not text.strip():
            raise WriteLeaseError("path_invalid")
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = self.sandbox.project_root / candidate
        _reject_parent_segments(candidate)
        absolute = Path(os.path.abspath(str(candidate)))
        if kind == "entry":
            # Reject known broad roots before walking the target. A volume or
            # home root may be inaccessible to the current process, but it is
            # still an invalid lease target rather than merely unreadable.
            self._reject_broad_entry_target(absolute)
        exists = _check_components(absolute, allow_missing=(kind in {"exact", "entry"}))
        if kind == "directory" and not exists:
            raise WriteLeaseError("directory_missing")
        if kind == "exact" and not exists:
            if not absolute.parent.is_dir():
                raise WriteLeaseError("exact_path_parent_missing")
        if exists:
            try:
                if kind == "directory" and not absolute.is_dir():
                    raise WriteLeaseError("path_kind_mismatch")
                if kind == "exact" and not absolute.is_file():
                    raise WriteLeaseError("path_kind_mismatch")
                if kind == "entry" and not (absolute.is_file() or absolute.is_dir()):
                    raise WriteLeaseError("path_kind_mismatch")
            except OSError:
                raise WriteLeaseError("path_unreadable") from None
        try:
            canonical = absolute.resolve(strict=exists)
        except (OSError, RuntimeError, ValueError):
            raise WriteLeaseError("path_unreadable") from None
        if kind == "entry":
            self._reject_broad_entry_target(canonical)
        if not self.sandbox.check(str(canonical), read_only=False)[0]:
            raise WriteLeaseError("path_not_writable_in_sandbox")
        if kind == "directory":
            if canonical == Path(canonical.anchor):
                raise WriteLeaseError("broad_root_forbidden")
            allowed_roots = list(getattr(self.sandbox, "_allowed_roots", []) or [])
            allowed_roots += [Path(value) for value in
                              (getattr(self.sandbox, "dir_grants", {}) or {})]
            for root in allowed_roots:
                try:
                    normalized_root = Path(root).resolve(strict=False)
                except (OSError, RuntimeError, ValueError):
                    continue
                if canonical == normalized_root:
                    raise WriteLeaseError("broad_root_forbidden")
        return os.path.normpath(str(canonical))

    def _reject_broad_entry_target(self, canonical: Path) -> None:
        """An entry lease reserves one object, but may not name a broad root."""
        roots = [Path(canonical.anchor), Path.home()]
        project_root = getattr(self.sandbox, "project_root", None)
        if project_root is not None:
            roots.append(Path(project_root))
        roots.extend(Path(root) for root in (getattr(self.sandbox, "_allowed_roots", []) or []))
        roots.extend(Path(root) for root in (getattr(self.sandbox, "dir_grants", {}) or {}))
        candidate_keys = {_path_key(os.path.abspath(str(canonical)))}
        try:
            candidate_keys.add(_path_key(str(canonical.resolve(strict=False))))
        except (OSError, RuntimeError, ValueError):
            pass
        for root in roots:
            root_keys = {_path_key(os.path.abspath(str(root)))}
            try:
                root_keys.add(_path_key(str(root.resolve(strict=False))))
            except (OSError, RuntimeError, ValueError):
                pass
            if candidate_keys & root_keys:
                raise WriteLeaseError("broad_root_forbidden")

    def _normalize_scopes(self, *, exact_paths: Sequence[str | os.PathLike[str]],
                          directory_paths: Sequence[str | os.PathLike[str]],
                          entry_paths: Sequence[str | os.PathLike[str]]) -> tuple[list[str], list[str], list[str]]:
        exact_values = _as_sequence(exact_paths, field="exact_paths",
                                    max_count=MAX_EXACT_PATHS)
        directory_values = _as_sequence(directory_paths, field="directory_paths",
                                        max_count=MAX_DIRECTORY_PATHS)
        entry_values = _as_sequence(entry_paths, field="entry_paths",
                                     max_count=MAX_TOTAL_SCOPES)
        if (not exact_values and not directory_values and not entry_values
                or len(exact_values) + len(entry_values) + len(directory_values) > MAX_TOTAL_SCOPES):
            raise WriteLeaseError("lease_scope_empty_or_too_large")
        exact = sorted({self._normalize_scope_path(item, kind="exact")
                        for item in exact_values}, key=_path_key)
        entries_list = [self._normalize_scope_path(item, kind="entry") for item in entry_values]
        entries = sorted(set(entries_list), key=_path_key)
        if len(entries) != len(entries_list):
            raise WriteLeaseError("requested_scopes_overlap")
        directories = sorted({self._normalize_scope_path(item, kind="directory")
                              for item in directory_values}, key=_path_key)
        scopes = [(path, "exact") for path in exact]
        scopes.extend((path, "entry") for path in entries)
        scopes.extend((path, "directory") for path in directories)
        for index, (left_path, left_kind) in enumerate(scopes):
            if any(_overlap(left_path, left_kind, right_path, right_kind)
                   for right_path, right_kind in scopes[index + 1:]):
                raise WriteLeaseError("requested_scopes_overlap")
        return exact, entries, directories

    @staticmethod
    def _now(value: float | None) -> float:
        now = time.time() if value is None else value
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise WriteLeaseError("clock_value_invalid")
        return float(now)

    def _persist(self, state: Mapping[str, Any]) -> None:
        payload = _canonical_json(state) + b"\n"
        temp_path = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            fd = os.open(str(temp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
            if os.name != "nt":
                try:
                    directory_fd = os.open(str(self.path.parent), os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
        except (OSError, ValueError):
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise WriteLeaseError("lease_state_persist_failed") from None

    @staticmethod
    def _active_leases(state: Mapping[str, Any], now: float) -> list[dict[str, Any]]:
        return [lease for lease in state["leases"] if lease["expires_at"] > now]

    @staticmethod
    def _purge_expired(state: dict[str, Any], now: float) -> bool:
        active = WriteLeaseStore._active_leases(state, now)
        changed = len(active) != len(state["leases"])
        if changed:
            state["leases"] = active
        return changed

    @staticmethod
    def _holder(lease: Mapping[str, Any]) -> dict[str, Any]:
        return {**copy.deepcopy(lease["holder"]),
                "lease_id": lease["lease_id"],
                "expires_at": lease["expires_at"]}

    def snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        """Reload durable lease state; expired leases are omitted, not renewed."""
        current_time = self._now(now)
        with self._locked():
            state = self._read_state()
            return {"schema_version": SCHEMA_VERSION,
                    "revision": state["revision"],
                    "leases": copy.deepcopy(self._active_leases(state, current_time))}

    def acquire(self, *, owner_id: str, run_id: str, conversation_id: str,
                exact_paths: Sequence[str | os.PathLike[str]] = (),
                directory_paths: Sequence[str | os.PathLike[str]] = (),
                entry_paths: Sequence[str | os.PathLike[str]] = (),
                ttl_seconds: int | float = 300,
                expected_revision: int | None = None,
                now: float | None = None) -> dict[str, Any]:
        """Acquire a non-overlapping lease, or return the current holder."""
        holder = _identity(owner_id, run_id, conversation_id)
        current_time = self._now(now)
        if (isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float))
                or not math.isfinite(ttl_seconds) or not 0 < ttl_seconds <= self.max_ttl_seconds):
            raise WriteLeaseError("ttl_invalid")
        if expected_revision is not None and (
                isinstance(expected_revision, bool)
                or not isinstance(expected_revision, int) or expected_revision < 0):
            raise WriteLeaseError("expected_revision_invalid")
        exact, entries, directories = self._normalize_scopes(
            exact_paths=exact_paths, directory_paths=directory_paths, entry_paths=entry_paths)
        requested_scopes = [(path, "exact") for path in exact]
        requested_scopes.extend((path, "entry") for path in entries)
        requested_scopes.extend((path, "directory") for path in directories)
        requested_scopes.sort(key=lambda item: (_path_key(item[0]), item[1]))
        expiry = current_time + float(ttl_seconds)

        with self._locked():
            state = self._read_state()
            actual_revision = state["revision"]
            if expected_revision is not None and expected_revision != actual_revision:
                return {"acquired": False, "revision": actual_revision,
                        "conflict": {"kind": "revision_mismatch", "holder": None,
                                     "expected_revision": expected_revision,
                                     "actual_revision": actual_revision}}
            expired_removed = self._purge_expired(state, current_time)
            current_leases = self._active_leases(state, current_time)

            exact_scope_match = next((lease for lease in current_leases
                if _same_identity(lease["holder"], holder)
                and lease["exact_paths"] == exact
                and lease.get("entry_paths", []) == entries
                and lease["directory_paths"] == directories), None)
            if exact_scope_match is not None:
                renewed_expiry = max(float(exact_scope_match["expires_at"]), expiry)
                changed = expired_removed or renewed_expiry != exact_scope_match["expires_at"]
                exact_scope_match["expires_at"] = renewed_expiry
                if changed:
                    state["revision"] = actual_revision + 1
                    self._persist(state)
                return {"acquired": True, "renewed": True,
                        "revision": state["revision"],
                        "lease": copy.deepcopy(exact_scope_match)}

            conflicts = []
            for lease in current_leases:
                if _same_identity(lease["holder"], holder):
                    continue
                if any(_overlap(path, kind, leased_path, leased_kind)
                       for path, kind in requested_scopes
                       for leased_path, leased_kind in _scope_records(lease)):
                    conflicts.append(lease)
            if conflicts:
                if expired_removed:
                    state["revision"] = actual_revision + 1
                    self._persist(state)
                conflict_lease = sorted(conflicts, key=lambda lease: lease["lease_id"])[0]
                overlap_path = next(path for path, kind in requested_scopes
                    if any(_overlap(path, kind, leased_path, leased_kind)
                           for leased_path, leased_kind in _scope_records(conflict_lease)))
                return {"acquired": False, "revision": state["revision"],
                        "conflict": {"kind": "path_in_use", "path": overlap_path,
                                     "holder": self._holder(conflict_lease)}}

            if len(current_leases) >= MAX_STORED_LEASES:
                raise WriteLeaseError("lease_capacity_exceeded")
            next_revision = actual_revision + 1
            lease = {
                "lease_id": _stable_id({"holder": holder, "exact_paths": exact,
                                         "entry_paths": entries, "directory_paths": directories,
                                         "created_at": current_time,
                                         "generation": next_revision}),
                "holder": holder,
                "exact_paths": exact,
                "entry_paths": entries,
                "directory_paths": directories,
                "created_at": current_time,
                "expires_at": expiry,
            }
            state["leases"] = current_leases + [lease]
            state["revision"] = next_revision
            self._persist(state)
            return {"acquired": True, "renewed": False,
                    "revision": next_revision, "lease": copy.deepcopy(lease)}

    def renew_owned_for_recovery(self, lease_id: str, *, owner_id: str, run_id: str,
                                 conversation_id: str, ttl_seconds: int = 300) -> dict[str, Any]:
        """Renew an existing recorded scope, including its now-deleted targets.

        No paths/ownership can be supplied or widened by this operation. The
        coordinator also checks its immutable task policy and publication
        journal. Another active owner always wins; a purged lease is not
        reconstructed from untrusted filesystem contents.
        """
        requester = _identity(owner_id, run_id, conversation_id)
        if type(ttl_seconds) is not int or not 0 < ttl_seconds <= self.max_ttl_seconds:
            raise WriteLeaseError("ttl_invalid")
        now = self._now(None)
        with self._locked():
            state = self._read_state()
            lease = next((item for item in state["leases"] if item["lease_id"] == lease_id), None)
            if lease is None or not _same_identity(lease["holder"], requester):
                raise WriteLeaseError("recovery_lease_owner_mismatch")
            scopes = _scope_records(lease)
            for path, _kind in scopes:
                self._normalize_target_path(path)
            for other in self._active_leases(state, now):
                if other["lease_id"] == lease_id or _same_identity(other["holder"], requester):
                    continue
                if any(_overlap(p, k, q, j) for p, k in scopes for q, j in _scope_records(other)):
                    raise WriteLeaseError("recovery_lease_conflict")
            lease["expires_at"] = now + ttl_seconds
            state["revision"] += 1
            self._persist(state)
            return copy.deepcopy(lease)

    def release(self, lease_id: str, *, owner_id: str, run_id: str,
                conversation_id: str, expected_revision: int | None = None,
                now: float | None = None) -> dict[str, Any]:
        """Explicitly release one lease owned by the full matching identity."""
        if not isinstance(lease_id, str) or not lease_id:
            raise WriteLeaseError("lease_id_invalid")
        requester = _identity(owner_id, run_id, conversation_id)
        current_time = self._now(now)
        if expected_revision is not None and (
                isinstance(expected_revision, bool)
                or not isinstance(expected_revision, int) or expected_revision < 0):
            raise WriteLeaseError("expected_revision_invalid")
        with self._locked():
            state = self._read_state()
            actual_revision = state["revision"]
            lease = next((item for item in state["leases"]
                          if item["lease_id"] == lease_id), None)
            if expected_revision is not None and expected_revision != actual_revision:
                return {"released": False, "revision": actual_revision,
                        "conflict": {"kind": "revision_mismatch",
                                     "holder": self._holder(lease) if lease else None,
                                     "expected_revision": expected_revision,
                                     "actual_revision": actual_revision}}
            if lease is None:
                return {"released": False, "revision": actual_revision,
                        "reason": "lease_not_found"}
            if not _same_identity(lease["holder"], requester):
                return {"released": False, "revision": actual_revision,
                        "conflict": {"kind": "lease_owner_mismatch",
                                     "holder": self._holder(lease)}}
            state["leases"] = [item for item in state["leases"]
                               if item["lease_id"] != lease_id]
            state["revision"] = actual_revision + 1
            self._persist(state)
            return {"released": True, "lease_id": lease_id,
                    "revision": state["revision"],
                    "expired": lease["expires_at"] <= current_time}

    @contextmanager
    def hold_owned_paths(self, lease_id: str, paths: Sequence[str | os.PathLike[str]], *,
                         owner_id: str, run_id: str, conversation_id: str):
        """Hold the ledger lock while a host publisher uses an existing lease.

        Unlike check_write, this requires positive, unexpired ownership of
        every target. The yielded check must run before each filesystem step;
        the ledger lock prevents concurrent release/reassignment, not expiry.
        It grants no filesystem or application permission.
        """
        requester = _identity(owner_id, run_id, conversation_id)
        if not isinstance(lease_id, str) or not lease_id:
            raise WriteLeaseError("lease_id_invalid")
        if not isinstance(paths, Sequence) or isinstance(paths, (str, bytes)) or not paths:
            raise WriteLeaseError("publication_paths_invalid")
        targets = [self._normalize_target_path(path) for path in paths]
        with self._locked():
            state = self._read_state()
            lease = next((item for item in state["leases"] if item["lease_id"] == lease_id), None)
            if lease is None or not _same_identity(lease["holder"], requester):
                raise WriteLeaseError("publication_lease_owner_mismatch")
            scopes = _scope_records(lease)
            if not all(any(_overlap(target, "exact", path, kind) for path, kind in scopes)
                       for target in targets):
                raise WriteLeaseError("publication_outside_lease")

            def check():
                if lease["expires_at"] <= self._now(None):
                    raise WriteLeaseError("publication_lease_expired")

            check()
            yield check

    def check_write(self, path: str | os.PathLike[str], *, owner_id: str,
                    run_id: str, conversation_id: str,
                    now: float | None = None) -> dict[str, Any]:
        """Report whether a lease owned by another identity conflicts with path.

        ``lease_conflict=False`` means only that this lease ledger has no
        conflicting owner; the caller must still apply the normal PathSandbox
        permission decision before writing.
        """
        requester = _identity(owner_id, run_id, conversation_id)
        current_time = self._now(now)
        target = self._normalize_target_path(path)
        with self._locked():
            state = self._read_state()
            for lease in sorted(self._active_leases(state, current_time),
                                key=lambda item: item["lease_id"]):
                if _same_identity(lease["holder"], requester):
                    continue
                if any(_overlap(target, "exact", scope_path, scope_kind)
                       for scope_path, scope_kind in _scope_records(lease)):
                    return {"lease_conflict": True, "path": target,
                            "holder": self._holder(lease),
                            "reason": "path_held_by_another_writer"}
        return {"lease_conflict": False, "path": target, "holder": None}

    def _normalize_target_path(self, raw: str | os.PathLike[str]) -> str:
        if not isinstance(raw, (str, os.PathLike)):
            raise WriteLeaseError("path_invalid")
        text = os.fspath(raw)
        if not text or not text.strip():
            raise WriteLeaseError("path_invalid")
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = self.sandbox.project_root / candidate
        _reject_parent_segments(candidate)
        absolute = Path(os.path.abspath(str(candidate)))
        _check_components(absolute, allow_missing=True)
        try:
            canonical = absolute.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            raise WriteLeaseError("path_unreadable") from None
        if not self.sandbox.check(str(canonical), read_only=False)[0]:
            raise WriteLeaseError("path_not_writable_in_sandbox")
        return os.path.normpath(str(canonical))


__all__ = ["WriteLeaseError", "WriteLeaseStore"]
