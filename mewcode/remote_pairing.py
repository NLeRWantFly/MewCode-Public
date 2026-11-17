"""Persistent pairing state for remote MewCode clients.

Disk stores only SHA-256 digests of device bearer tokens and Tailscale node
identities. Session ("allow once") grants live in memory and vanish when the
desktop process exits. Plaintext tokens are returned to callers and are never
written to disk.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


REQUEST_TTL_SECONDS = 5 * 60
MAX_REQUESTS = 64
MAX_DEVICES = 256
TOKEN_BYTES = 32
ALLOW_ONCE = "once"
ALLOW_ALWAYS = "always"

_TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


class PairingError(ValueError):
    """Raised for invalid pairing input or unavailable pairing records."""


@dataclass(frozen=True)
class PairingRequest:
    request_id: str
    status: str
    device_id: str
    node_id: str
    label: str | None = None
    expires_at: float = 0.0
    token: str | None = None
    allow: str | None = None


@dataclass(frozen=True)
class PairedDevice:
    device_id: str
    node_id: str
    token: str
    allow: str
    expires_at: float | None = None
    label: str | None = None


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_tailscale_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return address in _TAILSCALE_V4 or address in _TAILSCALE_V6


def pairing_peer_allowed(host: str | None) -> bool:
    """True when the TCP peer may submit a pairing request."""
    if not isinstance(host, str) or not host:
        return False
    if host == "testclient":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(address.is_loopback or _is_tailscale_ip(address))


def validate_endpoint(endpoint: str) -> str:
    """Validate and normalize a Tailnet MewCode endpoint.

    MagicDNS names must use HTTPS. Tailscale CGNAT/ULA addresses may use HTTP
    so a published local port can be discovered without MagicDNS.
    """
    if not isinstance(endpoint, str) or len(endpoint) > 512:
        raise PairingError("endpoint must be a short URL")
    parsed = urlsplit(endpoint.strip())
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise PairingError("endpoint must use HTTP or HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise PairingError("endpoint cannot contain credentials, query, or fragment")
    host = parsed.hostname.rstrip(".").lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if not _is_tailscale_ip(address):
            raise PairingError("endpoint IP is not a Tailscale address")
        normalized_host = host
    elif not (host == "ts.net" or host.endswith(".ts.net")):
        raise PairingError("endpoint hostname must be a Tailnet .ts.net name")
    elif scheme != "https":
        raise PairingError("endpoint must use HTTPS")
    else:
        normalized_host = host
    try:
        port = parsed.port
    except ValueError as exc:
        raise PairingError("endpoint port is invalid") from exc
    netloc = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    if port is not None:
        netloc += f":{port}"
    if parsed.path not in {"", "/"}:
        raise PairingError("endpoint cannot contain a path")
    return urlunsplit((scheme, netloc, "", "", ""))


def _validate_identity(value: str, *, field: str) -> str:
    if not isinstance(value, str):
        raise PairingError(f"invalid {field}")
    identity = value.strip()
    if not (1 <= len(identity) <= 128):
        raise PairingError(f"invalid {field}")
    if not identity.isascii() or not identity[0].isalnum():
        raise PairingError(f"invalid {field}")
    if any(not (char.isalnum() or char in ".:_-") for char in identity):
        raise PairingError(f"invalid {field}")
    return identity


def _public_request(item: dict, *, request_id: str) -> dict:
    return {
        "request_id": request_id,
        "device_id": item.get("device_id"),
        "node_id": item.get("node_id"),
        "label": item.get("label"),
        "created_at": item.get("created_at"),
        "expires_at": item.get("expires_at"),
        "status": item.get("status") or "pending",
    }


def _public_device(item: dict) -> dict:
    return {
        "device_id": item.get("device_id"),
        "node_id": item.get("node_id"),
        "label": item.get("label"),
        "created_at": item.get("created_at"),
        "allow": item.get("allow") or ALLOW_ALWAYS,
    }


class RemotePairingStore:
    """Thread-safe store for pairing requests, session grants, and always-allow."""

    def __init__(self, path: str | os.PathLike[str], *, clock=time.time) -> None:
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.RLock()
        self._session_tokens: dict[str, dict] = {}
        self._issued: dict[str, dict] = {}

    def _empty(self) -> dict:
        return {"requests": {}, "always": {}, "devices": {}}

    def _read(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._empty()
        except (OSError, json.JSONDecodeError) as exc:
            raise PairingError("pairing state is unreadable") from exc
        if not isinstance(value, dict):
            raise PairingError("pairing state has invalid shape")
        return {
            "requests": dict(value.get("requests") or {}),
            "always": dict(value.get("always") or {}),
            "devices": dict(value.get("devices") or {}),
        }

    def _write(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(state, handle, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass

    def _clean_memory(self) -> None:
        now = self._clock()
        self._issued = {
            key: value for key, value in self._issued.items()
            if isinstance(value, dict) and float(value.get("expires_at", 0)) > now
        }
        self._session_tokens = {
            key: value for key, value in self._session_tokens.items()
            if isinstance(value, dict) and not value.get("revoked", False)
        }

    def _clean(self, state: dict) -> bool:
        previous_requests = state["requests"]
        previous_devices = state["devices"]
        now = self._clock()
        state["requests"] = {
            key: value for key, value in state["requests"].items()
            if isinstance(value, dict) and float(value.get("expires_at", 0)) > now
            and (value.get("status") or "pending") == "pending"
        }
        state["devices"] = {
            key: value for key, value in state["devices"].items()
            if isinstance(value, dict) and not value.get("revoked", False)
        }
        state["always"] = {
            key: value for key, value in state["always"].items()
            if isinstance(value, dict) and not value.get("revoked", False)
        }
        self._clean_memory()
        return state["requests"] != previous_requests or state["devices"] != previous_devices

    def _always_entry(self, state: dict, node_id: str) -> dict | None:
        item = state["always"].get(_digest(node_id))
        if isinstance(item, dict) and not item.get("revoked", False):
            return item
        return None

    def _mint_token(
        self, *, device_id: str, node_id: str, label: str | None, allow: str
    ) -> tuple[PairedDevice, dict, str]:
        token = secrets.token_urlsafe(TOKEN_BYTES)
        row = {
            "device_id": device_id,
            "node_id": node_id,
            "label": label,
            "created_at": self._clock(),
            "revoked": False,
            "allow": allow,
        }
        return PairedDevice(device_id, node_id, token, allow, None, label), row, _digest(token)

    def _persist_always(self, state: dict, node_id: str, row: dict, token_key: str) -> None:
        if len(state["devices"]) >= MAX_DEVICES:
            raise PairingError("too many paired devices")
        state["always"][_digest(node_id)] = {
            "device_id": row["device_id"],
            "node_id": node_id,
            "label": row.get("label"),
            "created_at": row["created_at"],
            "revoked": False,
        }
        state["devices"][token_key] = dict(row)

    def submit_request(self, device_id: str, node_id: str, *, label: str | None = None) -> PairingRequest:
        device_id = _validate_identity(device_id, field="device id")
        node_id = _validate_identity(node_id, field="node id")
        label = str(label).strip()[:128] if label else None
        with self._lock:
            state = self._read()
            self._clean(state)
            always = self._always_entry(state, node_id)
            if always is not None:
                device, row, token_key = self._mint_token(
                    device_id=device_id, node_id=node_id,
                    label=label or always.get("label"), allow=ALLOW_ALWAYS,
                )
                self._persist_always(state, node_id, row, token_key)
                self._write(state)
                return PairingRequest(
                    request_id="",
                    status="approved",
                    device_id=device_id,
                    node_id=node_id,
                    label=device.label,
                    expires_at=self._clock() + REQUEST_TTL_SECONDS,
                    token=device.token,
                    allow=ALLOW_ALWAYS,
                )
            existing_id = ""
            for request_id, item in state["requests"].items():
                if isinstance(item, dict) and item.get("node_id") == node_id:
                    existing_id = request_id
                    break
            request_id = existing_id or secrets.token_urlsafe(16)
            if not existing_id and len(state["requests"]) >= MAX_REQUESTS:
                raise PairingError("too many pairing requests")
            expires_at = self._clock() + REQUEST_TTL_SECONDS
            record = {
                "device_id": device_id,
                "node_id": node_id,
                "label": label,
                "created_at": self._clock(),
                "expires_at": expires_at,
                "status": "pending",
            }
            state["requests"][request_id] = record
            self._issued.pop(request_id, None)
            self._write(state)
            return PairingRequest(
                request_id=request_id,
                status="pending",
                device_id=device_id,
                node_id=node_id,
                label=label,
                expires_at=expires_at,
            )

    def get_request(self, request_id: str) -> PairingRequest:
        if not isinstance(request_id, str) or not (8 <= len(request_id) <= 128):
            raise PairingError("pairing request is invalid or expired")
        with self._lock:
            self._clean_memory()
            issued = self._issued.get(request_id)
            if isinstance(issued, dict):
                return PairingRequest(
                    request_id=request_id,
                    status=str(issued.get("status") or "approved"),
                    device_id=str(issued.get("device_id") or ""),
                    node_id=str(issued.get("node_id") or ""),
                    label=issued.get("label"),
                    expires_at=float(issued.get("expires_at") or 0),
                    token=issued.get("token"),
                    allow=issued.get("allow"),
                )
            state = self._read()
            self._clean(state)
            item = state["requests"].get(request_id)
            if not isinstance(item, dict):
                raise PairingError("pairing request is invalid or expired")
            return PairingRequest(
                request_id=request_id,
                status="pending",
                device_id=str(item.get("device_id") or ""),
                node_id=str(item.get("node_id") or ""),
                label=item.get("label"),
                expires_at=float(item.get("expires_at") or 0),
            )

    def list_requests(self) -> list[dict]:
        with self._lock:
            state = self._read()
            cleaned = self._clean(state)
            if cleaned:
                self._write(state)
            rows = [
                _public_request(item, request_id=request_id)
                for request_id, item in state["requests"].items()
                if isinstance(item, dict)
            ]
            rows.sort(key=lambda row: float(row.get("created_at") or 0), reverse=True)
            return rows

    def decide(self, request_id: str, decision: str) -> PairingRequest:
        if not isinstance(request_id, str) or not request_id:
            raise PairingError("pairing request is invalid or expired")
        if decision not in {ALLOW_ONCE, ALLOW_ALWAYS, "deny"}:
            raise PairingError("invalid pairing decision")
        with self._lock:
            state = self._read()
            self._clean(state)
            item = state["requests"].pop(request_id, None)
            if not isinstance(item, dict):
                raise PairingError("pairing request is invalid or expired")
            expires_at = self._clock() + REQUEST_TTL_SECONDS
            device_id = str(item.get("device_id") or "")
            node_id = str(item.get("node_id") or "")
            label = item.get("label")
            if decision == "deny":
                self._issued[request_id] = {
                    "status": "rejected",
                    "device_id": device_id,
                    "node_id": node_id,
                    "label": label,
                    "expires_at": expires_at,
                    "token": None,
                    "allow": None,
                }
                self._write(state)
                return PairingRequest(
                    request_id=request_id,
                    status="rejected",
                    device_id=device_id,
                    node_id=node_id,
                    label=label,
                    expires_at=expires_at,
                )
            device, row, token_key = self._mint_token(
                device_id=device_id, node_id=node_id, label=label, allow=decision,
            )
            if decision == ALLOW_ALWAYS:
                self._persist_always(state, node_id, row, token_key)
            else:
                self._session_tokens[token_key] = dict(row)
            self._issued[request_id] = {
                "status": "approved",
                "device_id": device_id,
                "node_id": node_id,
                "label": label,
                "expires_at": expires_at,
                "token": device.token,
                "allow": decision,
            }
            self._write(state)
            return PairingRequest(
                request_id=request_id,
                status="approved",
                device_id=device_id,
                node_id=node_id,
                label=label,
                expires_at=expires_at,
                token=device.token,
                allow=decision,
            )

    def list_devices(self) -> list[dict]:
        with self._lock:
            state = self._read()
            cleaned = self._clean(state)
            if cleaned:
                self._write(state)
            rows: dict[str, dict] = {}
            for item in state["always"].values():
                if not isinstance(item, dict):
                    continue
                key = str(item.get("node_id") or item.get("device_id") or "")
                if key:
                    rows[key] = _public_device({**item, "allow": ALLOW_ALWAYS})
            for item in self._session_tokens.values():
                if not isinstance(item, dict) or item.get("revoked"):
                    continue
                key = str(item.get("node_id") or item.get("device_id") or "")
                if key and key not in rows:
                    rows[key] = _public_device({**item, "allow": ALLOW_ONCE})
            devices = [row for row in rows.values() if row.get("device_id")]
            devices.sort(key=lambda row: float(row.get("created_at") or 0), reverse=True)
            return devices

    def validate_token(self, token: str) -> dict | None:
        if not isinstance(token, str) or not token:
            return None
        with self._lock:
            self._clean_memory()
            session = self._session_tokens.get(_digest(token))
            if isinstance(session, dict) and not session.get("revoked"):
                return dict(session)
            state = self._read()
            cleaned = self._clean(state)
            if cleaned:
                self._write(state)
            item = state["devices"].get(_digest(token))
            if isinstance(item, dict) and not item.get("revoked"):
                return dict(item)
            return None

    def revoke_device(self, device_id: str) -> bool:
        identity = _validate_identity(device_id, field="device id")
        with self._lock:
            state = self._read()
            changed = False
            for item in state["devices"].values():
                if isinstance(item, dict) and not item.get("revoked") and (
                    item.get("device_id") == identity or item.get("node_id") == identity
                ):
                    item["revoked"] = True
                    changed = True
            for key, item in list(state["always"].items()):
                if isinstance(item, dict) and (
                    item.get("device_id") == identity or item.get("node_id") == identity
                ):
                    state["always"].pop(key, None)
                    changed = True
            for item in self._session_tokens.values():
                if isinstance(item, dict) and not item.get("revoked") and (
                    item.get("device_id") == identity or item.get("node_id") == identity
                ):
                    item["revoked"] = True
                    changed = True
            self._clean(state)
            if changed:
                self._write(state)
            return changed

    def cleanup(self) -> None:
        with self._lock:
            state = self._read()
            if self._clean(state):
                self._write(state)

    def clear(self) -> None:
        with self._lock:
            self._session_tokens = {}
            self._issued = {}
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
