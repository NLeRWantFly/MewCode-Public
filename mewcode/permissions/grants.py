"""Project-local, typed permission grants with explicit user confirmation.

This module stores grants; it does not enforce them. Callers must still apply
the hard-deny and sensitive-path policies before consulting ``covers``.
Grant values are deliberately narrow and the on-disk schema does not accept
arbitrary metadata that could accidentally persist credentials.
"""
from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence


class GrantKind(str, Enum):
    READ_DIRECTORY = "read-directory"
    WRITE_DIRECTORY = "write-directory"
    HOST = "host"
    COMMAND_PREFIX = "command-prefix"


_SCHEMA_VERSION = 1
_MAX_GRANTS = 500
_MIN_TTL_SECONDS = 60
_MAX_TTL_SECONDS = 365 * 24 * 60 * 60
_CONFIRMATION_MAX_AGE_SECONDS = 5 * 60
_CONFIRMATION_FUTURE_SKEW_SECONDS = 30
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_GLOB_CHARS = frozenset("*?[]")
_SHELL_CHARS = frozenset(";|&><`$")
_SECRET_TEXT = re.compile(
    r"(?i)(?:\bsk-[A-Za-z0-9_-]{8,}\b|\bbearer\s+\S+|"
    r"\b(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*\S+|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
_INTERPRETER_NAMES = frozenset({
    "python", "python.exe", "pythonw.exe", "py", "py.exe", "node", "node.exe",
    "ruby", "ruby.exe", "perl", "perl.exe", "bash", "bash.exe", "sh", "sh.exe",
    "zsh", "fish", "powershell", "powershell.exe", "pwsh", "pwsh.exe", "cmd", "cmd.exe",
})
_INLINE_EXEC_FLAGS = frozenset({"-c", "-e", "-command", "--command", "/c", "/k"})
_SHELL_EXECUTABLES = frozenset({
    "bash", "bash.exe", "sh", "sh.exe", "zsh", "fish", "powershell",
    "powershell.exe", "pwsh", "pwsh.exe", "cmd", "cmd.exe",
})
_SAFE_PYTHON_MODULES = frozenset({"pytest", "ruff", "mypy", "compileall"})
_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.RLock] = {}


class GrantStoreError(ValueError):
    """A malformed, unsafe, or unreadable permission-grant store."""


def _valid_opaque_id(value: Any) -> bool:
    return (isinstance(value, str) and bool(_ID_PATTERN.fullmatch(value))
            and not _SECRET_TEXT.search(value))


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GrantStoreError(f"{field} must be a finite timestamp")
    result = float(value)
    if not math.isfinite(result):
        raise GrantStoreError(f"{field} must be a finite timestamp")
    return result


def _scope_value(kind: GrantKind, value: Any) -> str | list[str]:
    if kind in {GrantKind.READ_DIRECTORY, GrantKind.WRITE_DIRECTORY}:
        if not isinstance(value, (str, os.PathLike)):
            raise GrantStoreError("directory grant value must be an absolute path")
        raw = os.fspath(value).strip()
        if not raw or any(char in raw for char in _GLOB_CHARS):
            raise GrantStoreError("directory grants do not accept empty paths or wildcards")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise GrantStoreError("directory grants require an absolute path")
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise GrantStoreError("directory grant must name an existing directory") from exc
        if not resolved.is_dir():
            raise GrantStoreError("directory grant must name an existing directory")
        if resolved == Path(resolved.anchor):
            raise GrantStoreError("whole-drive and filesystem-root grants are forbidden")
        return str(resolved)

    if kind is GrantKind.HOST:
        if not isinstance(value, str):
            raise GrantStoreError("host grant must be one exact hostname or IP address")
        host = value.strip().lower().rstrip(".")
        if (not host or any(char in host for char in _GLOB_CHARS)
                or "://" in host or "/" in host or "\\" in host or "@" in host):
            raise GrantStoreError("host grant must be exact and cannot include a URL, port, or wildcard")
        try:
            return ipaddress.ip_address(host).compressed.lower()
        except ValueError:
            pass
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise GrantStoreError("host grant is not a valid exact hostname") from exc
        if len(ascii_host) > 253 or not all(_DNS_LABEL.fullmatch(label) for label in ascii_host.split(".")):
            raise GrantStoreError("host grant is not a valid exact hostname")
        return ascii_host

    if kind is GrantKind.COMMAND_PREFIX:
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise GrantStoreError("command-prefix grants must be an argv token list")
        if any(not isinstance(token, str) for token in value):
            raise GrantStoreError("command-prefix argv tokens must be strings")
        tokens = list(value)
        if len(tokens) < 2:
            raise GrantStoreError("command-prefix grants require an executable and at least one argument")
        if any(not token or len(token) > 1024 for token in tokens):
            raise GrantStoreError("command-prefix tokens must contain 1–1024 characters")
        if any(any(ord(char) < 32 or ord(char) == 127 for char in token) for token in tokens):
            raise GrantStoreError("command-prefix tokens cannot contain control characters")
        if any(any(char in token for char in _GLOB_CHARS | _SHELL_CHARS) for token in tokens):
            raise GrantStoreError("command-prefix grants do not accept wildcards or shell syntax")
        joined = " ".join(tokens)
        if _SECRET_TEXT.search(joined) or re.search(r"(?i)(?:\$env:|%[a-z_][a-z0-9_]*%|\$\{)", joined):
            raise GrantStoreError("command-prefix grants cannot store secrets or environment references")
        executable = Path(tokens[0].replace("\\", "/")).name.lower()
        if executable in _SHELL_EXECUTABLES:
            raise GrantStoreError("persistent command-prefix grants cannot authorize a shell")
        if executable in _INTERPRETER_NAMES and tokens[1].lower() in _INLINE_EXEC_FLAGS:
            raise GrantStoreError("inline interpreter commands cannot receive persistent prefix grants")
        if executable in _INTERPRETER_NAMES:
            if (executable not in {"python", "python.exe", "pythonw.exe", "py", "py.exe"}
                    or len(tokens) < 3 or tokens[1].lower() != "-m"
                    or tokens[2].lower() not in _SAFE_PYTHON_MODULES):
                raise GrantStoreError("interpreter grants require an approved fixed module invocation")
        return tokens

    raise GrantStoreError("unsupported grant kind")


def _kind(value: GrantKind | str) -> GrantKind:
    try:
        return value if isinstance(value, GrantKind) else GrantKind(str(value))
    except ValueError as exc:
        raise GrantStoreError("unsupported grant kind") from exc


@dataclass(frozen=True)
class PermissionGrant:
    grant_id: str
    kind: GrantKind
    value: str | tuple[str, ...]
    created_at: float
    expires_at: float
    version: int
    confirmed_by: str
    confirmation_id: str
    confirmed_at: float
    confirmed_scope: Mapping[str, Any]
    revoked_at: float | None = None
    revoked_by: str | None = None
    revocation_id: str | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None and time.time() < self.expires_at

    def is_active(self, *, now: float) -> bool:
        return self.revoked_at is None and now < self.expires_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "kind": self.kind.value,
            "value": list(self.value) if isinstance(self.value, tuple) else self.value,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "version": self.version,
            "confirmation": {
                "confirmed": True,
                "actor": self.confirmed_by,
                "confirmation_id": self.confirmation_id,
                "confirmed_at": self.confirmed_at,
                "scope": dict(self.confirmed_scope),
            },
            "revocation": None if self.revoked_at is None else {
                "actor": self.revoked_by,
                "request_id": self.revocation_id,
                "revoked_at": self.revoked_at,
            },
        }


class GrantStore:
    """Atomic project-local grant store with strict scope and audit metadata.

    API for a later checker/UI integration:
    ``preview_scope`` → show exact scope → ``grant`` with the resulting scope
    and a user confirmation ID; ``list_grants`` for the UI; ``revoke`` for
    user-driven removal; ``matching_grant`` for exact host, directory subtree,
    or argv-prefix lookup. Hard denies must run before lookup.
    """

    def __init__(
        self,
        project_root: str | os.PathLike[str],
        *,
        relative_path: str | os.PathLike[str] = ".mewcode/permissions.grants.json",
        clock: Callable[[], float] = time.time,
        max_ttl_seconds: int = _MAX_TTL_SECONDS,
    ) -> None:
        try:
            root = Path(project_root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise GrantStoreError("project root must be an existing directory") from exc
        if not root.is_dir():
            raise GrantStoreError("project root must be an existing directory")
        relative = Path(relative_path)
        if (relative.is_absolute() or not relative.parts or relative == Path(".")
                or ".." in relative.parts):
            raise GrantStoreError("grant store path must remain inside the project")
        if not isinstance(max_ttl_seconds, int) or not _MIN_TTL_SECONDS <= max_ttl_seconds <= _MAX_TTL_SECONDS:
            raise GrantStoreError("max TTL must be between 60 seconds and one year")
        self.project_root = root
        self.relative_path = relative
        self.clock = clock
        self.max_ttl_seconds = max_ttl_seconds
        lock_key = os.path.normcase(str(root / relative))
        with _PROCESS_LOCKS_GUARD:
            self._thread_lock = _PROCESS_LOCKS.setdefault(lock_key, threading.RLock())

    @property
    def path(self) -> Path:
        return self.project_root / self.relative_path

    def preview_scope(
        self, kind: GrantKind | str, value: Any, ttl_seconds: int,
    ) -> dict[str, Any]:
        """Canonical scope the UI must display and echo after user confirmation."""
        grant_kind = _kind(kind)
        ttl = self._validate_ttl(ttl_seconds)
        return {"kind": grant_kind.value, "value": _scope_value(grant_kind, value), "ttl_seconds": ttl}

    def grant(
        self,
        kind: GrantKind | str,
        value: Any,
        *,
        ttl_seconds: int,
        confirmation: Mapping[str, Any],
    ) -> PermissionGrant:
        grant_kind = _kind(kind)
        scope = self.preview_scope(grant_kind, value, ttl_seconds)
        now = _finite_number(self.clock(), "clock")
        confirmed = self._validate_confirmation(confirmation, scope, now)
        canonical_value = scope["value"]
        with self._exclusive_lock(create_parent=True):
            state = self._load_state()
            records = state["grants"]
            if len(records) >= _MAX_GRANTS:
                raise GrantStoreError("grant store is full; revoke or archive an existing entry")
            if any(item.confirmation_id == confirmed["confirmation_id"] for item in records):
                raise GrantStoreError("confirmation ID has already been used")
            if any(item.kind is grant_kind and item.value == self._runtime_value(canonical_value)
                   and item.is_active(now=now) for item in records):
                raise GrantStoreError("an active grant already covers this exact scope")
            grant = PermissionGrant(
                grant_id=uuid.uuid4().hex,
                kind=grant_kind,
                value=self._runtime_value(canonical_value),
                created_at=now,
                expires_at=now + scope["ttl_seconds"],
                version=1,
                confirmed_by="user",
                confirmation_id=confirmed["confirmation_id"],
                confirmed_at=confirmed["confirmed_at"],
                confirmed_scope=scope,
            )
            records.append(grant)
            self._write_state({"schema_version": _SCHEMA_VERSION, "revision": state["revision"] + 1,
                               "grants": records})
            return grant

    def list_grants(self, *, include_revoked: bool = True) -> list[PermissionGrant]:
        records = self._load_state()["grants"]
        if include_revoked:
            return records
        return [item for item in records if item.revoked_at is None]

    def revoke(
        self,
        grant_id: str,
        *,
        revoked_by: str = "user",
        request_id: str,
    ) -> PermissionGrant:
        if revoked_by != "user" or not _valid_opaque_id(request_id):
            raise GrantStoreError("revocation requires a user actor and unique request ID")
        now = _finite_number(self.clock(), "clock")
        with self._exclusive_lock(create_parent=True):
            state = self._load_state()
            records = state["grants"]
            target = next((item for item in records if item.grant_id == grant_id), None)
            if target is None:
                raise KeyError("grant not found")
            if target.revoked_at is not None:
                if target.revocation_id == request_id:
                    return target
                raise GrantStoreError("grant is already revoked")
            if any(item.revocation_id == request_id for item in records):
                raise GrantStoreError("revocation request ID has already been used")
            replacement = PermissionGrant(
                grant_id=target.grant_id, kind=target.kind, value=target.value,
                created_at=target.created_at, expires_at=target.expires_at,
                version=target.version + 1, confirmed_by=target.confirmed_by,
                confirmation_id=target.confirmation_id, confirmed_at=target.confirmed_at,
                confirmed_scope=target.confirmed_scope, revoked_at=now,
                revoked_by=revoked_by, revocation_id=request_id,
            )
            records[records.index(target)] = replacement
            self._write_state({"schema_version": _SCHEMA_VERSION, "revision": state["revision"] + 1,
                               "grants": records})
            return replacement

    def matching_grant(self, kind: GrantKind | str, value: Any) -> PermissionGrant | None:
        """Return an active grant covering a request, or ``None``.

        ``value`` is an absolute path for directory grants, a hostname for host
        grants, and an argv sequence for command-prefix grants.
        """
        grant_kind = _kind(kind)
        now = _finite_number(self.clock(), "clock")
        if grant_kind in {GrantKind.READ_DIRECTORY, GrantKind.WRITE_DIRECTORY}:
            requested = self._request_path(value)
            if requested is None:
                return None
            for grant in self._load_state()["grants"]:
                if grant.kind is grant_kind and grant.is_active(now=now):
                    if self._path_is_within(requested, Path(str(grant.value))):
                        return grant
            return None

        if grant_kind is GrantKind.HOST:
            try:
                requested_host = _scope_value(grant_kind, value)
            except GrantStoreError:
                return None
            return next((item for item in self._load_state()["grants"]
                         if item.kind is grant_kind and item.value == requested_host
                         and item.is_active(now=now)), None)

        try:
            requested_prefix = _scope_value(grant_kind, value)
        except GrantStoreError:
            return None
        for grant in self._load_state()["grants"]:
            if grant.kind is not grant_kind or not grant.is_active(now=now):
                continue
            prefix = list(grant.value)
            if len(requested_prefix) >= len(prefix) and requested_prefix[:len(prefix)] == prefix:
                return grant
        return None

    def _validate_ttl(self, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise GrantStoreError("TTL must be an integer number of seconds")
        if not _MIN_TTL_SECONDS <= value <= self.max_ttl_seconds:
            raise GrantStoreError(f"TTL must be between {_MIN_TTL_SECONDS} and {self.max_ttl_seconds} seconds")
        return value

    def _validate_confirmation(
        self, raw: Mapping[str, Any], scope: Mapping[str, Any], now: float,
    ) -> dict[str, Any]:
        if not isinstance(raw, Mapping) or set(raw) != {
            "confirmed", "actor", "confirmation_id", "confirmed_at", "scope",
        }:
            raise GrantStoreError("confirmation must contain exactly the user-confirmed scope metadata")
        if raw.get("confirmed") is not True or raw.get("actor") != "user":
            raise GrantStoreError("persistent grants require explicit confirmation by the user")
        confirmation_id = raw.get("confirmation_id")
        if not _valid_opaque_id(confirmation_id):
            raise GrantStoreError("confirmation ID must be a unique opaque identifier")
        confirmed_at = _finite_number(raw.get("confirmed_at"), "confirmed_at")
        if (confirmed_at > now + _CONFIRMATION_FUTURE_SKEW_SECONDS
                or now - confirmed_at > _CONFIRMATION_MAX_AGE_SECONDS):
            raise GrantStoreError("user confirmation is stale or from the future")
        echoed_scope = raw.get("scope")
        if not isinstance(echoed_scope, Mapping) or dict(echoed_scope) != dict(scope):
            raise GrantStoreError("confirmed scope must exactly match the stored grant and TTL")
        return {"confirmation_id": confirmation_id, "confirmed_at": confirmed_at}

    @staticmethod
    def _runtime_value(value: Any) -> str | tuple[str, ...]:
        return tuple(value) if isinstance(value, list) else str(value)

    def _storage_paths(self, *, create_parent: bool) -> tuple[Path, Path]:
        candidate = self.project_root / self.relative_path
        parent = candidate.parent
        if create_parent:
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(parent, 0o700)
            except OSError:
                # Windows ACLs are inherited from the project directory; chmod
                # is best-effort there and does not provide a security boundary.
                pass
        try:
            resolved_parent = parent.resolve(strict=create_parent)
        except (OSError, RuntimeError, ValueError) as exc:
            raise GrantStoreError("grant store parent cannot be resolved") from exc
        if not resolved_parent.is_relative_to(self.project_root):
            raise GrantStoreError("grant store path escaped the project root")
        target = resolved_parent / candidate.name
        lock_path = resolved_parent / (candidate.name + ".lock")
        for path in (target, lock_path):
            if path.is_symlink():
                raise GrantStoreError("grant store and lock files cannot be symlinks")
        return target, lock_path

    @contextmanager
    def _exclusive_lock(self, *, create_parent: bool) -> Iterator[Path]:
        target, lock_path = self._storage_paths(create_parent=create_parent)
        with self._thread_lock:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(lock_path, flags, 0o600)
            except OSError as exc:
                raise GrantStoreError("cannot open grant store lock") from exc
            with os.fdopen(fd, "r+b", closefd=True) as handle:
                try:
                    if os.fstat(handle.fileno()).st_size == 0:
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    yield target
                finally:
                    try:
                        handle.seek(0)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass

    def _load_state(self) -> dict[str, Any]:
        target, _ = self._storage_paths(create_parent=False)
        if not target.exists():
            return {"schema_version": _SCHEMA_VERSION, "revision": 0, "grants": []}
        if target.is_symlink() or not target.is_file():
            raise GrantStoreError("grant store must be a regular project-local file")
        try:
            raw = target.read_text(encoding="utf-8")
            payload = json.loads(raw, object_pairs_hook=self._unique_object)
        except (OSError, UnicodeError, json.JSONDecodeError, GrantStoreError) as exc:
            raise GrantStoreError("grant store is unreadable or malformed; it was not overwritten") from exc
        if not isinstance(payload, dict) or set(payload) != {"schema_version", "revision", "grants"}:
            raise GrantStoreError("grant store schema is invalid; it was not overwritten")
        if payload["schema_version"] != _SCHEMA_VERSION:
            raise GrantStoreError("unsupported grant store schema version")
        revision = payload["revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise GrantStoreError("grant store revision is invalid")
        raw_grants = payload["grants"]
        if not isinstance(raw_grants, list) or len(raw_grants) > _MAX_GRANTS:
            raise GrantStoreError("grant store entries are invalid or exceed the limit")
        grants = [self._parse_grant(item) for item in raw_grants]
        if len({item.grant_id for item in grants}) != len(grants):
            raise GrantStoreError("grant store contains duplicate IDs")
        if len({item.confirmation_id for item in grants}) != len(grants):
            raise GrantStoreError("grant store contains duplicate confirmation IDs")
        return {"schema_version": _SCHEMA_VERSION, "revision": revision, "grants": grants}

    @staticmethod
    def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise GrantStoreError("grant store contains duplicate JSON keys")
            result[key] = value
        return result

    def _parse_grant(self, raw: Any) -> PermissionGrant:
        expected = {
            "grant_id", "kind", "value", "created_at", "expires_at", "version",
            "confirmation", "revocation",
        }
        if not isinstance(raw, dict) or set(raw) != expected:
            raise GrantStoreError("grant record schema is invalid")
        grant_id = raw["grant_id"]
        if not isinstance(grant_id, str) or not re.fullmatch(r"[0-9a-f]{32}", grant_id):
            raise GrantStoreError("grant ID is invalid")
        grant_kind = _kind(raw["kind"])
        if grant_kind in {GrantKind.READ_DIRECTORY, GrantKind.WRITE_DIRECTORY}:
            stored_path = raw["value"]
            if not isinstance(stored_path, str) or not stored_path:
                raise GrantStoreError("stored directory grant is invalid")
            if any(char in stored_path for char in _GLOB_CHARS):
                raise GrantStoreError("stored directory grant contains a wildcard")
            stored_path_obj = Path(stored_path)
            if (not stored_path_obj.is_absolute()
                    or stored_path_obj == Path(stored_path_obj.anchor)):
                raise GrantStoreError("stored directory grant is not a concrete absolute directory")
            # Keep the path granted at confirmation time. If it is later
            # replaced by a symlink, request paths are resolved before matching
            # and therefore cannot use that link to widen the grant.
            normalized: str | list[str] = stored_path
        else:
            normalized = _scope_value(grant_kind, raw["value"])
        if normalized != raw["value"]:
            raise GrantStoreError("grant value is not canonical")
        value = self._runtime_value(normalized)
        created_at = _finite_number(raw["created_at"], "created_at")
        expires_at = _finite_number(raw["expires_at"], "expires_at")
        version = raw["version"]
        if isinstance(version, bool) or not isinstance(version, int) or version not in {1, 2}:
            raise GrantStoreError("grant version is invalid")
        confirmation = raw["confirmation"]
        if not isinstance(confirmation, dict) or set(confirmation) != {
            "confirmed", "actor", "confirmation_id", "confirmed_at", "scope",
        }:
            raise GrantStoreError("grant confirmation metadata is invalid")
        confirmation_id = confirmation["confirmation_id"]
        if (confirmation.get("confirmed") is not True or confirmation.get("actor") != "user"
                or not _valid_opaque_id(confirmation_id)):
            raise GrantStoreError("grant lacks explicit user confirmation")
        confirmed_at = _finite_number(confirmation["confirmed_at"], "confirmed_at")
        expected_scope = {"kind": grant_kind.value, "value": normalized,
                          "ttl_seconds": int(expires_at - created_at)}
        if not isinstance(confirmation.get("scope"), dict) or dict(confirmation["scope"]) != expected_scope:
            raise GrantStoreError("grant confirmation scope does not match stored grant")
        if (expires_at <= created_at or expires_at - created_at > self.max_ttl_seconds
                or confirmed_at < created_at - _CONFIRMATION_FUTURE_SKEW_SECONDS
                or confirmed_at > created_at + _CONFIRMATION_MAX_AGE_SECONDS):
            raise GrantStoreError("grant timestamps or TTL are invalid")
        revocation = raw["revocation"]
        revoked_at: float | None = None
        revoked_by: str | None = None
        revocation_id: str | None = None
        if revocation is not None:
            if not isinstance(revocation, dict) or set(revocation) != {"actor", "request_id", "revoked_at"}:
                raise GrantStoreError("grant revocation metadata is invalid")
            revoked_by = revocation["actor"]
            revocation_id = revocation["request_id"]
            revoked_at = _finite_number(revocation["revoked_at"], "revoked_at")
            if (version != 2 or revoked_by != "user" or not isinstance(revocation_id, str)
                    or not _ID_PATTERN.fullmatch(revocation_id) or revoked_at < created_at):
                raise GrantStoreError("grant revocation metadata is invalid")
        elif version != 1:
            raise GrantStoreError("unrevoked grant version is invalid")
        return PermissionGrant(
            grant_id=grant_id, kind=grant_kind, value=value,
            created_at=created_at, expires_at=expires_at, version=version,
            confirmed_by="user", confirmation_id=confirmation_id,
            confirmed_at=confirmed_at, confirmed_scope=dict(expected_scope),
            revoked_at=revoked_at, revoked_by=revoked_by, revocation_id=revocation_id,
        )

    def _write_state(self, state: Mapping[str, Any]) -> None:
        target, _ = self._storage_paths(create_parent=True)
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "revision": state["revision"],
            "grants": [item.as_dict() for item in state["grants"]],
        }
        # Convert the public API representation into the strict on-disk form.
        for record in payload["grants"]:
            record["confirmation"] = {
                "confirmed": True,
                "actor": record["confirmation"]["actor"],
                "confirmation_id": record["confirmation"]["confirmation_id"],
                "confirmed_at": record["confirmation"]["confirmed_at"],
                "scope": record["confirmation"]["scope"],
            }
            record["revocation"] = record["revocation"]
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        temp_path: Path | None = None
        try:
            fd, raw_temp = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
            temp_path = Path(raw_temp)
            try:
                os.chmod(temp_path, 0o600)
            except OSError:
                pass
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
            temp_path = None
            try:
                directory_fd = os.open(target.parent, os.O_RDONLY)
            except (OSError, TypeError):
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                except OSError:
                    pass
                finally:
                    os.close(directory_fd)
        except OSError as exc:
            raise GrantStoreError("could not atomically persist permission grant") from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _request_path(value: Any) -> Path | None:
        if not isinstance(value, (str, os.PathLike)):
            return None
        raw = os.fspath(value).strip()
        if not raw or any(char in raw for char in _GLOB_CHARS):
            return None
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            return None
        try:
            return candidate.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return None

    @staticmethod
    def _path_is_within(path: Path, directory: Path) -> bool:
        try:
            path_text = os.path.normcase(str(path.resolve(strict=False)))
            directory_text = os.path.normcase(str(directory))
            common = os.path.commonpath((path_text, directory_text))
            return common == directory_text
        except (OSError, ValueError, RuntimeError):
            return False
