"""Restore the desktop's own plaintext provider snapshot before Agent startup.

Tauri already owns a plaintext providers.json; this never writes or converts
credentials. Electron encrypted records remain desktop-IPC-only.

A snapshot that exists but cannot be read is an incident, not an empty list.
It is reported loudly and recorded, so a later empty provider sync cannot
silently wipe the bindings the agent already holds.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

SNAPSHOT_NAME = "providers.json"
SNAPSHOT_LIMIT_BYTES = 2_000_000

#: Reason the last snapshot load failed, or "" when it succeeded or the file
#: simply does not exist. Consumed by ``clobber_block_reason``.
_last_load_failure = ""


class EncryptedSnapshot(Exception):
    """The snapshot holds Electron-only encrypted records: skip, do not alarm."""


def last_load_failure() -> str:
    return _last_load_failure


def _record_failure(reason: str) -> None:
    global _last_load_failure
    _last_load_failure = reason


def read_snapshot(path: Path) -> list[dict]:
    """Return the validated profile list.

    Raises ``EncryptedSnapshot`` when the file is intentionally opaque to the
    agent, and ``ValueError`` with a payload-free reason when it is unreadable,
    undecodable or structurally invalid.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"unreadable ({exc.strerror or type(exc).__name__})") from exc
    if len(raw) > SNAPSHOT_LIMIT_BYTES:
        raise ValueError(f"oversize ({len(raw)} bytes)")
    try:
        # A UTF-8 BOM is tolerated here: editors and PowerShell 5.1's
        # ``Set-Content -Encoding utf8`` both prepend one. The desktop side
        # (serde_json) rejects it, so a BOM is a real incident to fix, but the
        # agent must never mistake it for an empty provider list.
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"not valid UTF-8 at byte {exc.start}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON at line {exc.lineno} column {exc.colno}") from exc
    if not isinstance(payload, list):
        raise ValueError(f"snapshot is {type(payload).__name__}, expected a list")
    if any(isinstance(row, dict) and row.get("encrypted_api_key") for row in payload):
        # Do not partially restore mixed snapshots or attempt OS decryption.
        raise EncryptedSnapshot()
    if any(not isinstance(row, dict) or not isinstance(row.get("provider_key"), str)
           or not row["provider_key"].startswith("desktop:") for row in payload):
        raise ValueError("unstable provider identity")
    keys = [row["provider_key"] for row in payload]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate provider identity")
    return payload


def bootstrap_desktop_providers(registry, data_root: Path, *, desktop_authenticated: bool) -> int:
    if not desktop_authenticated:
        return 0  # A normal CLI/web session must not import desktop credentials.
    path = Path(data_root) / SNAPSHOT_NAME
    if not path.is_file():
        _record_failure("")
        return 0
    try:
        payload = read_snapshot(path)
    except EncryptedSnapshot:
        _record_failure("")
        log.info("Desktop provider snapshot holds encrypted records; leaving it to desktop IPC")
        return 0
    except ValueError as exc:
        _record_failure(str(exc))
        # Never log provider payloads, parser excerpts or credentials.
        log.error(
            "Desktop provider snapshot exists but cannot be loaded (%s): %s. "
            "It is NOT being treated as an empty provider list; fix or move the "
            "file and restart the desktop app.",
            exc, path,
        )
        return 0
    try:
        registry.replace_desktop_profiles(payload)  # Atomic validation before publication.
    except ValueError as exc:
        _record_failure(f"rejected by registry ({exc})")
        log.error("Desktop provider snapshot rejected by the registry: %s", exc)
        return 0
    _record_failure("")
    log.info("Restored %d desktop provider bindings before session startup", len(payload))
    return len(payload)


def clobber_block_reason(data_root: Path) -> str:
    """Non-empty when an empty provider sync must be refused.

    An empty sync is legitimate only when the desktop really has no profiles.
    If the snapshot exists but we could not read it, an empty sync means the
    desktop lost the same list we did, and applying it would erase every
    in-memory binding without any recoverable source.
    """
    if not _last_load_failure:
        return ""
    path = Path(data_root) / SNAPSHOT_NAME
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return ""
    except OSError:
        return ""
    return (
        f"{SNAPSHOT_NAME} could not be read ({_last_load_failure}); refusing to apply an "
        f"empty provider list. Fix or move {path} and restart the desktop app."
    )
