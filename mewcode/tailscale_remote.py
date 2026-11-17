"""Safe, read-only Tailscale node discovery for the web frontend.

This module deliberately exposes only the output of the fixed
``tailscale status --json`` command.  It does not accept remote commands,
URLs, credentials, or arbitrary subprocess arguments.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

from .config import TailscaleConfig

log = logging.getLogger(__name__)

_MAX_STATUS_BYTES = 2 * 1024 * 1024
_MAX_PROBE_NODES = 64
_MAX_PROBE_ATTEMPTS = 16
_DISCOVERED_REMOTE_NODES: dict[str, dict[str, Any]] = {}


def _observed_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _node_payload(raw: Any, *, self_node: bool, expose_addresses: bool) -> dict[str, Any]:
    """Project a Tailscale node to non-sensitive, stable UI fields."""
    item = raw if isinstance(raw, dict) else {}
    node: dict[str, Any] = {
        "id": _string(item.get("ID")),
        "hostname": _string(item.get("HostName")),
        "dns_name": _string(item.get("DNSName")),
        "os": _string(item.get("OS")),
        "online": bool(item.get("Online")) if "Online" in item else None,
        "relay": _string(item.get("Relay")),
        "last_seen": _string(item.get("LastSeen")),
        "self": self_node,
    }
    if expose_addresses:
        addresses = item.get("TailscaleIPs")
        node["addresses"] = [
            value.strip()
            for value in addresses
            if isinstance(value, str) and value.strip()
        ] if isinstance(addresses, list) else []
    return node


def parse_tailscale_status(
    payload: dict[str, Any], *, expose_addresses: bool = False
) -> dict[str, Any]:
    """Normalize ``tailscale status --json`` output for the frontend."""
    self_node = _node_payload(
        payload.get("Self"), self_node=True, expose_addresses=expose_addresses
    )
    peers = payload.get("Peer")
    nodes = [self_node]
    if isinstance(peers, dict):
        for key, value in sorted(peers.items(), key=lambda pair: str(pair[0])):
            if not isinstance(value, dict):
                continue
            node = _node_payload(
                value, self_node=False, expose_addresses=expose_addresses
            )
            # Some Tailscale versions omit ID from the peer body. Never expose
            # the raw map key; a short digest is enough for UI reconciliation.
            if not node["id"] and isinstance(key, str):
                node["id"] = "peer-" + hashlib.sha256(
                    key.encode("utf-8", errors="replace")
                ).hexdigest()[:16]
            nodes.append(node)
    return {
        "backend_state": _string(payload.get("BackendState")),
        "version": _string(payload.get("Version")),
        "self": self_node,
        "nodes": nodes,
    }


def _resolve_cli(cli_path: str) -> str:
    # Absolute paths are allowed for managed installations.  Bare names are
    # resolved through PATH; subprocess is still called without a shell.
    if cli_path:
        resolved = shutil.which(cli_path)
        if resolved:
            return resolved
    return cli_path


def collect_tailscale_status(
    config: TailscaleConfig,
    *,
    run: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Collect a bounded local Tailscale status snapshot.

    The response is intentionally useful to callers: UI clients can
    distinguish disabled, unavailable, and connected states without guessing.
    ``run`` is injectable for tests and never receives user input.
    """
    base: dict[str, Any] = {
        "enabled": bool(config.enabled),
        "available": False,
        "connected": False,
        "backend_state": "",
        "version": "",
        "self": None,
        "nodes": [],
        "observed_at": _observed_at(),
        "error": "",
    }
    if not config.enabled:
        return base

    command = [_resolve_cli(config.cli_path), "status", "--json"]
    try:
        completed = run(
            command,
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
            check=False,
        )
    except FileNotFoundError:
        base["error"] = "tailscale CLI not found"
        return base
    except subprocess.TimeoutExpired:
        base["error"] = "tailscale status timed out"
        return base
    except OSError as exc:
        log.info("tailscale status unavailable: %s", exc)
        base["error"] = "tailscale CLI unavailable"
        return base

    stdout = completed.stdout if isinstance(completed.stdout, str) else ""
    if len(stdout.encode("utf-8", errors="replace")) > _MAX_STATUS_BYTES:
        base["error"] = "tailscale status response too large"
        return base
    if getattr(completed, "returncode", 1) != 0:
        base["error"] = "tailscale status failed"
        return base
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError):
        base["error"] = "tailscale status returned invalid JSON"
        return base
    if not isinstance(payload, dict):
        base["error"] = "tailscale status returned an invalid payload"
        return base

    # Keep addresses available for bounded probing even when the response
    # hides them from the browser. They are removed again before returning.
    normalized = parse_tailscale_status(payload, expose_addresses=True)
    base.update(normalized)
    base["available"] = True
    base["connected"] = normalized["backend_state"].lower() == "running"
    if config.auto_discover and base["connected"]:
        discovered = discover_mewcode_nodes(base, config)
        selectable = [item for item in discovered if item.get("selectable")]
        remember_discovered_nodes(selectable)
        base["mewcode_nodes"] = selectable
    else:
        base["mewcode_nodes"] = []
    if not config.expose_addresses:
        for node in base.get("nodes", []):
            if isinstance(node, dict):
                node.pop("addresses", None)
        if isinstance(base.get("self"), dict):
            base["self"].pop("addresses", None)
    return base


def _tailnet_host(value: Any) -> str:
    host = _string(value).rstrip(".").lower()
    if host.endswith(".ts.net"):
        return host
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return ""
    if address in ipaddress.ip_network("100.64.0.0/10") or address in ipaddress.ip_network("fd7a:115c:a1e0::/48"):
        return host
    return ""


def _probe_candidates(node: dict[str, Any], config: TailscaleConfig) -> list[tuple[str, str]]:
    hosts: list[tuple[str, bool]] = []
    dns_name = _tailnet_host(node.get("dns_name"))
    if dns_name:
        hosts.append((dns_name, False))
    for value in node.get("addresses") or []:
        host = _tailnet_host(value)
        if host and all(host != existing for existing, _ in hosts):
            hosts.append((host, True))
    candidates: list[tuple[str, str]] = []
    for host, is_ip in hosts:
        for port in config.probe_ports:
            if port == 443:
                candidates.append((host, f"https://{host}"))
            else:
                candidates.append((host, f"https://{host}:{port}"))
                # A direct tailnet IP can be served by the local HTTP backend;
                # this path is restricted to encrypted tailnet addresses and
                # is never generated for public DNS names.
                if is_ip:
                    candidates.append((host, f"http://{host}:{port}"))
    return candidates


def _node_discovery_item(
    node: dict[str, Any],
    *,
    node_id: str,
    status: str,
    url: str = "",
    info: dict[str, Any] | None = None,
    token_env: str = "",
    token_configured: bool = False,
) -> dict[str, Any]:
    selectable = status in {"reachable", "auth_required"}
    payload = info if isinstance(info, dict) else {}
    return {
        "id": node_id,
        "hostname": _string(node.get("hostname")),
        "dns_name": _string(node.get("dns_name")),
        "url": url,
        "token_env": token_env,
        "token_configured": token_configured,
        "reachable": selectable,
        "auth_required": status == "auth_required",
        "selectable": selectable,
        "status": status,
        "capabilities": payload.get("capabilities") if isinstance(payload.get("capabilities"), dict) else {},
        "source": "tailscale-auto",
    }


def discover_mewcode_nodes(
    snapshot: dict[str, Any],
    config: TailscaleConfig,
    *,
    opener: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    """Probe fixed MewCode metadata endpoints on Tailscale peers.

    Discovery never scans arbitrary ports or paths. It only tries the
    configured bounded port list and ``/api/remote/info`` on Tailscale DNS/IP
    identities. A 401/403 still proves that a MewCode endpoint exists but that
    a bearer token is required; conversation content is never fetched during
    discovery. Online peers that time out stay in the list as not selectable.
    """
    if not config.enabled or not config.auto_discover:
        return []
    open_url = opener or urllib.request.urlopen
    token = os.environ.get(config.remote_token_env, "")
    selectable: list[dict[str, Any]] = []
    others: list[dict[str, Any]] = []
    attempts = 0
    for node in snapshot.get("nodes", []):
        if not isinstance(node, dict) or node.get("self"):
            continue
        node_id = _string(node.get("id")) or _string(node.get("hostname"))
        if not node_id:
            continue
        candidates = _probe_candidates(node, config)
        if not candidates:
            continue
        if len(selectable) + len(others) >= _MAX_PROBE_NODES:
            break
        if node.get("online") is False:
            others.append(_node_discovery_item(node, node_id=node_id, status="offline"))
            continue
        found: dict[str, Any] | None = None
        last_status = "timeout"
        for _host, base_url in candidates:
            attempts += 1
            if attempts > _MAX_PROBE_ATTEMPTS:
                break
            headers = {"Accept": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            request = urllib.request.Request(
                base_url + "/api/remote/info", headers=headers, method="GET"
            )
            info: dict[str, Any] = {}
            try:
                with open_url(request, timeout=config.probe_timeout_seconds) as response:
                    raw = response.read(256 * 1024 + 1)
                    if len(raw) > 256 * 1024:
                        raise ValueError("response too large")
                    parsed = json.loads(raw.decode("utf-8"))
                    if isinstance(parsed, dict):
                        info = parsed
                    status = "reachable"
            except urllib.error.HTTPError as exc:
                if exc.code in {401, 403}:
                    status = "auth_required"
                else:
                    last_status = "unreachable"
                    continue
            except TimeoutError:
                last_status = "timeout"
                continue
            except urllib.error.URLError as exc:
                last_status = "timeout" if isinstance(getattr(exc, "reason", None), TimeoutError) else "unreachable"
                continue
            except (OSError, ValueError, json.JSONDecodeError):
                last_status = "unreachable"
                continue
            found = _node_discovery_item(
                node, node_id=node_id, status=status, url=base_url, info=info,
                token_env=config.remote_token_env, token_configured=bool(token),
            )
            break
        if found is not None:
            selectable.append(found)
        else:
            others.append(_node_discovery_item(node, node_id=node_id, status=last_status))
        if attempts > _MAX_PROBE_ATTEMPTS:
            break
    return (selectable + others)[:_MAX_PROBE_NODES]


def remember_discovered_nodes(nodes: list[dict[str, Any]]) -> None:
    for item in nodes:
        node_id = _string(item.get("id"))
        if node_id and item.get("url"):
            _DISCOVERED_REMOTE_NODES[node_id] = dict(item)


def discovered_remote_nodes() -> list[dict[str, Any]]:
    return [dict(item) for item in _DISCOVERED_REMOTE_NODES.values()]


__all__ = [
    "collect_tailscale_status", "parse_tailscale_status", "discover_mewcode_nodes",
    "remember_discovered_nodes", "discovered_remote_nodes",
]


def configured_remote_nodes(config: TailscaleConfig) -> list[dict[str, Any]]:
    """Return public metadata for explicitly configured MewCode peers."""
    result = []
    for item in config.remote_nodes:
        node_id = item.get("id", "")
        token_env = item.get("token_env", config.remote_token_env)
        result.append({
            "id": node_id,
            "url": item.get("url", ""),
            "token_configured": bool(os.environ.get(token_env, "")),
            "capabilities": {
                "history": True,
                "create_conversation": True,
                "send_prompt": True,
            },
        })
    return result


def remote_node_request(
    config: TailscaleConfig,
    node_id: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call one configured peer using a bounded, capability-scoped request."""
    allowed = {
        ("GET", "/api/remote/info"),
        ("GET", "/api/remote/history"),
        ("GET", "/api/remote/history/item"),
        ("GET", "/api/remote/live"),
        ("POST", "/api/remote/conversations"),
        ("POST", "/api/remote/conversations/activate"),
        ("POST", "/api/remote/conversations/message"),
        ("POST", "/api/remote/conversations/interrupt"),
        ("POST", "/api/remote/conversations/resume"),
        ("POST", "/api/remote/prompts"),
    }
    if not isinstance(path, str) or len(path) > 600 or ".." in path:
        raise ValueError("invalid remote path")
    normalized_path = path.split("?", 1)[0]
    method = method.upper()
    operation_path = normalized_path.startswith("/api/remote/operations/") and normalized_path.count("/") == 4
    if (method, normalized_path) not in allowed and not (method == "GET" and operation_path):
        raise ValueError("remote operation is not allowed")
    item = next((value for value in config.remote_nodes if value.get("id") == node_id), None)
    if item is None:
        item = _DISCOVERED_REMOTE_NODES.get(node_id)
    if item is None:
        raise ValueError("remote node is not configured")
    base_url = str(item.get("url") or "").rstrip("/")
    url = base_url + path
    token_env = str(item.get("token_env") or config.remote_token_env)
    token = os.environ.get(token_env, "")
    if not token:
        raise ValueError("remote node token is not configured")
    body = None
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=8.0) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("remote response too large")
            value = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ValueError(f"remote node returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ValueError("remote node unavailable") from exc
    if not isinstance(value, dict):
        raise ValueError("remote node returned an invalid payload")
    return value


__all__ += ["configured_remote_nodes", "remote_node_request"]
