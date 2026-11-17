# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time
import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_SNAPSHOTS = 100
MAX_ROLLBACK_BYTES = 4 * 1024 * 1024
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_PROTECTED_DIRECTORIES = {".git", ".mewcode", ".codex", ".agents"}
_PROTECTED_FILES = {
    "agents.md", "agents.override.md", "permissions.yaml", "permissions.local.yaml",
    "permissions.grants.json", ".mcp.json", "mcp.json",
}
_SENSITIVE_DIRECTORIES = {".ssh", ".aws", ".azure", ".docker", "credentials", "secrets"}
_SENSITIVE_FILES = {
    ".netrc", ".git-credentials", ".npmrc", ".pypirc", "auth.json",
    "credentials", "credentials.json", "token", "token.json", "tokens.json",
    "secrets", "secrets.json",
}
_SENSITIVE_NAME = re.compile(
    r"(?:^|[._-])(token|password|secret|credential|api[_-]?key)(?:[._-]|$)", re.I
)


@dataclass
class Backup:
    backup_path: str
    version: int
    timestamp: float
    existed: bool | None = None
    sha256: str | None = None
    size: int | None = None


@dataclass
class Snapshot:
    message_index: int
    user_text: str
    backups: dict[str, Backup] = field(default_factory=dict)
    timestamp: float = 0.0
    changes: list[dict[str, str]] = field(default_factory=list)


class FileHistory:

    def __init__(self, base_dir: str, session_id: str) -> None:
        self._base_dir = Path(base_dir).resolve()
        self._session_dir = self._create_session_directory(session_id)
        self._tracked: dict[str, int] = {}
        self._snapshots: list[Snapshot] = []
        self._lock = threading.Lock()
        self._turn_paths: list[str] = []
        self._turn_pre_existed: dict[str, bool] = {}
        self._turn_prepared: dict[str, dict[str, Any]] = {}
        self._active_prepared_by_path: dict[str, dict[str, Any]] = {}
        self._prepared_by_path: dict[str, dict[str, Any]] = {}
        self._prepared_by_id: dict[str, dict[str, Any]] = {}
        self.last_turn_changes: list[dict[str, str]] = []
        self._turn_changes_path = self._session_dir / "turn_changes.jsonl"

    def _create_session_directory(self, session_id: str) -> Path:
        """Create the session backup path one safe workspace directory at a time."""
        if not isinstance(session_id, str) or not _SAFE_SESSION_ID.fullmatch(session_id):
            raise ValueError("invalid file history session id")

        # Keep the existing behavior for a caller-provided workspace that has
        # not been created yet, while resolving it once before adding internal
        # backup metadata below.
        self._base_dir.mkdir(parents=True, exist_ok=True)
        if not self._base_dir.is_dir() or self._is_reparse_or_symlink(self._base_dir):
            raise OSError("unsafe file history workspace directory")

        current = self._base_dir
        for component in (".mewcode", "file-history", session_id):
            current = current / component
            try:
                info = current.lstat()
            except FileNotFoundError:
                try:
                    current.mkdir()
                except FileExistsError:
                    # A concurrent creator may have won. Reinspect it below.
                    pass
                info = current.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise OSError("unsafe file history directory link")
            if not stat.S_ISDIR(info.st_mode):
                raise NotADirectoryError("file history path component is not a directory")

        try:
            current.resolve(strict=True).relative_to(self._base_dir)
        except (OSError, RuntimeError, ValueError) as exc:
            raise OSError("file history directory escaped workspace") from exc
        return current

    def begin_turn(self) -> None:
        with self._lock:
            self._turn_paths = []
            self._turn_pre_existed = {}
            self._turn_prepared = {}
            self._active_prepared_by_path = {}
            self.last_turn_changes = []

    @staticmethod
    def _is_reparse_or_symlink(path: Path) -> bool:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
        )

    @staticmethod
    def _has_sensitive_name(parts: tuple[str, ...]) -> bool:
        lowered = tuple(part.casefold() for part in parts)
        if any(part in _SENSITIVE_DIRECTORIES for part in lowered):
            return True
        if any(part == ".config" and index + 1 < len(lowered) and lowered[index + 1] == "gcloud"
               for index, part in enumerate(lowered)):
            return True
        filename = lowered[-1] if lowered else ""
        if (
            filename in _SENSITIVE_FILES
            or filename == ".env"
            or filename.startswith(".env.")
            or filename.endswith((".pem", ".key", ".pfx"))
            or filename.startswith(("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"))
            or bool(_SENSITIVE_NAME.search(filename))
        ):
            return True
        return False

    def _safe_target(self, path: str | os.PathLike[str]) -> tuple[Path, bool] | None:
        """Resolve a lexical workspace file path without following links."""
        try:
            supplied = Path(path)
            candidate = supplied if supplied.is_absolute() else self._base_dir / supplied
            absolute = Path(os.path.abspath(os.fspath(candidate)))
            relative = absolute.relative_to(self._base_dir)
        except (OSError, TypeError, ValueError, RuntimeError):
            return None
        parts = relative.parts
        if not parts:
            return None
        lowered = tuple(part.casefold() for part in parts)
        if (
            any(part in _PROTECTED_DIRECTORIES for part in lowered)
            or lowered[-1] in _PROTECTED_FILES
            or self._has_sensitive_name(parts)
            or any(":" in part or "\x00" in part for part in parts)
        ):
            return None

        current = self._base_dir
        exists = False
        for index, part in enumerate(parts):
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                # Missing files and missing parent directories are acceptable
                # evidence of a pre-write absence; no later component exists.
                return absolute, False
            except OSError:
                return None
            if stat.S_ISLNK(info.st_mode) or (
                getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
            ):
                return None
            if index < len(parts) - 1:
                if not stat.S_ISDIR(info.st_mode):
                    return None
            else:
                if not stat.S_ISREG(info.st_mode):
                    return None
                exists = True
        return absolute, exists

    def _safe_backup_directory(self) -> bool:
        try:
            relative = self._session_dir.relative_to(self._base_dir)
            resolved = self._session_dir.resolve(strict=False)
            resolved.relative_to(self._base_dir)
        except (OSError, RuntimeError, ValueError):
            return False
        if ".." in relative.parts:
            return False
        current = self._base_dir
        for part in relative.parts:
            current = current / part
            try:
                info = current.lstat()
            except OSError:
                return False
            if (
                stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
                or not stat.S_ISDIR(info.st_mode)
            ):
                return False
        return True

    @staticmethod
    def _read_regular_bytes(path: Path, max_bytes: int) -> bytes | None:
        try:
            before = path.lstat()
            if (
                stat.S_ISLNK(before.st_mode)
                or getattr(before, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
                or not stat.S_ISREG(before.st_mode)
                or before.st_size > max_bytes
            ):
                return None
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as handle:
                opened = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or getattr(opened, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
                    or opened.st_size > max_bytes
                ):
                    return None
                data = handle.read(max_bytes + 1)
            after = path.lstat()
            if (
                len(data) > max_bytes
                or stat.S_ISLNK(after.st_mode)
                or getattr(after, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
                or not stat.S_ISREG(after.st_mode)
                or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            ):
                return None
            return data
        except (OSError, ValueError):
            return None

    def _current_target_state(self, path: str, max_bytes: int) -> tuple[bool, bytes, str] | None:
        safe = self._safe_target(path)
        if safe is None or str(safe[0]) != path:
            return None
        target, existed = safe
        if not existed:
            return False, b"", hashlib.sha256(b"").hexdigest()
        data = self._read_regular_bytes(target, max_bytes)
        if data is None:
            return None
        return True, data, hashlib.sha256(data).hexdigest()

    def _backup_data(self, prepared: dict[str, Any]) -> bytes | None:
        if not self._safe_backup_directory():
            return None
        backup_path = Path(prepared["backup_path"])
        receipt = prepared["receipt"]
        if backup_path.parent != self._session_dir or backup_path.name != receipt["backup_id"]:
            return None
        data = self._read_regular_bytes(backup_path, prepared["size"])
        if data is None or len(data) != prepared["size"]:
            return None
        if hashlib.sha256(data).hexdigest() != prepared["sha256"]:
            return None
        return data

    def _verify_prepared_unlocked(self, prepared: dict[str, Any]) -> bool:
        if self._backup_data(prepared) is None:
            return False
        state = self._current_target_state(prepared["path"], prepared["max_bytes"])
        if state is None:
            return False
        existed, data, digest = state
        return (
            existed == prepared["existed"]
            and len(data) == prepared["size"]
            and digest == prepared["sha256"]
        )

    def prepare_rollback(
        self,
        path: str | os.PathLike[str],
        max_bytes: int = MAX_ROLLBACK_BYTES,
    ) -> dict[str, Any] | None:
        """Create bounded, verifiable evidence for one ordinary workspace file.

        The receipt proves file bytes and whether the file existed before a
        write. It does not cover parent directories, permissions/ACLs, or
        network and other external side effects.
        """
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
            return None
        if max_bytes < 0 or max_bytes > MAX_ROLLBACK_BYTES:
            return None
        safe = self._safe_target(path)
        if safe is None:
            return None
        target, existed = safe
        target_text = str(target)

        with self._lock:
            if not self._safe_backup_directory():
                return None
            state = self._current_target_state(target_text, max_bytes)
            if state is None:
                return None
            existed, data, digest = state
            previous = self._prepared_by_path.get(target_text)
            if (
                previous is not None
                and previous["existed"] == existed
                and previous["size"] == len(data)
                and previous["sha256"] == digest
                and self._verify_prepared_unlocked(previous)
            ):
                self._active_prepared_by_path[target_text] = previous
                return copy.deepcopy(previous["receipt"])

            if existed and len(data) > max_bytes:
                return None
            path_digest = hashlib.sha256(target_text.encode("utf-8")).hexdigest()[:32]
            version = (previous["version"] + 1) if previous is not None else 1
            backup_path = self._session_dir / f"{path_digest}@prepared-v{version}"
            while backup_path.exists():
                version += 1
                if version > 10000:
                    return None
                backup_path = self._session_dir / f"{path_digest}@prepared-v{version}"
            try:
                with backup_path.open("xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError:
                try:
                    backup_path.unlink(missing_ok=True)
                except OSError:
                    pass
                return None

            stored = self._read_regular_bytes(backup_path, len(data))
            current = self._current_target_state(target_text, max_bytes)
            if (
                stored is None
                or len(stored) != len(data)
                or hashlib.sha256(stored).hexdigest() != digest
                or current is None
                or current[0] != existed
                or len(current[1]) != len(data)
                or current[2] != digest
            ):
                try:
                    backup_path.unlink(missing_ok=True)
                except OSError:
                    pass
                return None

            backup_id = backup_path.name
            receipt = {
                "path": target_text,
                "backup_version": version,
                "backup_id": backup_id,
                "sha256": digest,
                "size": len(data),
                "existed": existed,
                "kind": "verified_file_snapshot",
                "scope": "file_bytes_and_existence_only",
                "exclusions": [
                    "directory layout",
                    "permissions and ACLs",
                    "network or other external side effects",
                ],
            }
            prepared = {
                "path": target_text,
                "backup_path": str(backup_path),
                "version": version,
                "existed": existed,
                "sha256": digest,
                "size": len(data),
                "max_bytes": max_bytes,
                "receipt": receipt,
            }
            self._prepared_by_path[target_text] = prepared
            self._prepared_by_id[backup_id] = prepared
            self._active_prepared_by_path[target_text] = prepared
            return copy.deepcopy(receipt)

    def verify_rollback(self, receipt: dict[str, Any]) -> bool:
        """Verify backup integrity and that the target still matches pre-write state."""
        if not isinstance(receipt, dict):
            return False
        backup_id = receipt.get("backup_id")
        if not isinstance(backup_id, str):
            return False
        with self._lock:
            prepared = self._prepared_by_id.get(backup_id)
            if prepared is None or receipt != prepared["receipt"]:
                return False
            return self._verify_prepared_unlocked(prepared)

    def _backup_name(self, file_path: str, version: int) -> str:
        h = hashlib.sha256(file_path.encode()).hexdigest()[:16]
        return f"{h}@v{version}"

    def _rel_path(self, abs_path: str) -> str:
        try:
            return Path(abs_path).resolve().relative_to(self._base_dir).as_posix()
        except ValueError:
            return Path(abs_path).as_posix()

    def track_edit(self, path: str) -> None:
        with self._lock:
            abs_path = str(Path(path).resolve())
            if abs_path not in self._turn_pre_existed:
                prepared = self._active_prepared_by_path.get(abs_path)
                if prepared is not None:
                    self._turn_pre_existed[abs_path] = prepared["existed"]
                    self._turn_prepared[abs_path] = prepared
                else:
                    self._turn_pre_existed[abs_path] = Path(abs_path).exists()
                self._turn_paths.append(abs_path)
            ver = self._tracked.get(abs_path, 0)
            new_ver = ver + 1

            try:
                data = Path(abs_path).read_bytes()
                bp = self._session_dir / self._backup_name(abs_path, new_ver)
                bp.write_bytes(data)
            except FileNotFoundError:
                pass

            self._tracked[abs_path] = new_ver

    def describe_turn_changes(self) -> list[dict[str, str]]:
        with self._lock:
            return self._describe_turn_changes_unlocked()

    def _describe_turn_changes_unlocked(self) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for abs_path in self._turn_paths:
            exists = Path(abs_path).exists()
            pre = self._turn_pre_existed.get(abs_path, False)
            if pre and exists:
                kind = "modify"
            elif (not pre) and exists:
                kind = "add"
            elif pre and (not exists):
                kind = "delete"
            else:
                continue
            out.append({"path": self._rel_path(abs_path), "kind": kind})
        return out

    def _persist_turn_changes(self, msg_index: int, changes: list[dict[str, str]]) -> None:
        if not changes:
            return
        record = {"message_index": msg_index, "changes": changes, "timestamp": time.time()}
        with self._turn_changes_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def load_turn_changes(self) -> dict[int, list[dict[str, str]]]:
        result: dict[int, list[dict[str, str]]] = {}
        if not self._turn_changes_path.is_file():
            return result
        try:
            text = self._turn_changes_path.read_text(encoding="utf-8")
        except OSError:
            return result
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            try:
                index = int(payload.get("message_index"))
            except (TypeError, ValueError):
                continue
            changes = payload.get("changes")
            if isinstance(changes, list):
                cleaned = [
                    {"path": str(item.get("path") or ""), "kind": str(item.get("kind") or "modify")}
                    for item in changes
                    if isinstance(item, dict) and item.get("path")
                ]
                if cleaned:
                    result[index] = cleaned
        return result

    def make_snapshot(self, msg_index: int, user_text: str) -> list[dict[str, str]]:
        with self._lock:
            changes = self._describe_turn_changes_unlocked()
            backups: dict[str, Backup] = {}
            for path, ver in self._tracked.items():
                prepared = self._turn_prepared.get(path)
                if prepared is not None and self._backup_data(prepared) is not None:
                    backups[path] = Backup(
                        backup_path=prepared["backup_path"],
                        version=prepared["version"],
                        timestamp=time.time(),
                        existed=prepared["existed"],
                        sha256=prepared["sha256"],
                        size=prepared["size"],
                    )
                    continue
                bp = self._session_dir / self._backup_name(path, ver)
                if not bp.exists():
                    try:
                        data = Path(path).read_bytes()
                        bp.write_bytes(data)
                    except (FileNotFoundError, OSError):
                        pass
                backups[path] = Backup(
                    backup_path=str(bp), version=ver, timestamp=time.time(),
                )

            self._snapshots.append(Snapshot(
                message_index=msg_index,
                user_text=user_text,
                backups=backups,
                timestamp=time.time(),
                changes=list(changes),
            ))
            if len(self._snapshots) > MAX_SNAPSHOTS:
                self._snapshots = self._snapshots[-MAX_SNAPSHOTS:]
            self._persist_turn_changes(msg_index, changes)
            self.last_turn_changes = list(changes)
            self._turn_paths = []
            self._turn_pre_existed = {}
            self._turn_prepared = {}
            self._active_prepared_by_path = {}
            return list(changes)

    def get_snapshots(self) -> list[Snapshot]:
        with self._lock:
            return list(self._snapshots)

    def has_snapshots(self) -> bool:
        with self._lock:
            return len(self._snapshots) > 0

    def rewind(self, snapshot_index: int) -> list[str]:
        with self._lock:
            if snapshot_index < 0 or snapshot_index >= len(self._snapshots):
                return []

            target = self._snapshots[snapshot_index]
            changed: list[str] = []

            for file_path, backup in target.backups.items():
                bp = Path(backup.backup_path)
                try:
                    backup_data = bp.read_bytes()
                except FileNotFoundError:
                    fp = Path(file_path)
                    if backup.existed is False and backup.sha256 is not None:
                        safe = self._safe_target(file_path)
                        if safe is not None and safe[1]:
                            fp.unlink()
                            changed.append(file_path)
                    elif backup.existed is True and backup.sha256 is not None:
                        continue
                    elif fp.exists():
                        fp.unlink()
                        changed.append(file_path)
                    continue

                if backup.sha256 is not None and (
                    len(backup_data) != backup.size
                    or hashlib.sha256(backup_data).hexdigest() != backup.sha256
                ):
                    continue

                fp = Path(file_path)
                if backup.existed is False and backup.sha256 is not None:
                    safe = self._safe_target(file_path)
                    if safe is not None and safe[1]:
                        fp.unlink()
                        changed.append(file_path)
                    continue
                if backup.existed is True and backup.sha256 is not None:
                    safe = self._safe_target(file_path)
                    if safe is None or not safe[1]:
                        continue
                try:
                    current_data = fp.read_bytes()
                except FileNotFoundError:
                    current_data = b""

                if current_data != backup_data:
                    fp.parent.mkdir(parents=True, exist_ok=True)
                    fp.write_bytes(backup_data)
                    changed.append(file_path)

            self._snapshots = self._snapshots[: snapshot_index + 1]
            for file_path, backup in target.backups.items():
                self._tracked[file_path] = backup.version

            return changed
