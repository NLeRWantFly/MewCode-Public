"""Publish the local MewCode backend on Tailscale HTTPS (MagicDNS).

The Android client can only reach ``https://*.ts.net``. The desktop process
listens on 127.0.0.1, so pairing requires ``tailscale serve`` to proxy that
port. Commands are fixed argument lists; the only variable is our own port.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Callable

from .config import TailscaleConfig
from .remote_pairing import PairingError
from .tailscale_remote import _resolve_cli


def _port_or_raise(local_port: int) -> int:
    if not isinstance(local_port, int) or isinstance(local_port, bool) or not (1 <= local_port <= 65535):
        raise PairingError("invalid local serve port")
    return local_port


def _proxy_url(local_port: int) -> str:
    return f"http://127.0.0.1:{_port_or_raise(local_port)}"


def _run_cli(
    command: list[str],
    *,
    run: Callable[..., Any],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    return run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def serve_status(*, config: TailscaleConfig | None = None, run: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """Return ``tailscale serve status --json``, or an empty mapping on failure."""
    cfg = config or TailscaleConfig()
    command = [_resolve_cli(cfg.cli_path), "serve", "status", "--json"]
    try:
        completed = _run_cli(command, run=run, timeout=max(2.0, float(cfg.timeout_seconds)))
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return {}
    stdout = completed.stdout if isinstance(completed.stdout, str) else ""
    if getattr(completed, "returncode", 1) != 0 or not stdout.strip():
        return {}
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def proxy_is_published(status: dict[str, Any], local_port: int) -> bool:
    """True when HTTPS already proxies ``/`` to this process's local port."""
    target = _proxy_url(local_port)
    web = status.get("Web")
    if not isinstance(web, dict):
        return False
    for site in web.values():
        if not isinstance(site, dict):
            continue
        # Tailscale 1.98 nests path handlers under ``Handlers``. Older
        # releases (and saved status payloads) exposed the path map directly.
        handlers = site.get("Handlers")
        if not isinstance(handlers, dict):
            handlers = site
        root = handlers.get("/")
        if not isinstance(root, dict):
            continue
        proxy = str(root.get("Proxy") or "").rstrip("/")
        if proxy == target:
            return True
    return False


def ensure_https_proxy(
    local_port: int,
    *,
    config: TailscaleConfig | None = None,
    run: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Background-publish ``https://<MagicDNS>/`` to the local backend port."""
    cfg = config or TailscaleConfig()
    target = _proxy_url(local_port)
    current = serve_status(config=cfg, run=run)
    if proxy_is_published(current, local_port):
        return {"https_published": True, "proxy": target, "https_error": ""}
    command = [_resolve_cli(cfg.cli_path), "serve", "--bg", "--yes", str(local_port)]
    try:
        completed = _run_cli(command, run=run, timeout=max(25.0, float(cfg.timeout_seconds)))
    except FileNotFoundError as exc:
        raise PairingError("tailscale CLI not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise PairingError("tailscale serve timed out") from exc
    except OSError as exc:
        raise PairingError("tailscale serve unavailable") from exc
    if getattr(completed, "returncode", 1) != 0:
        stderr = str(completed.stderr or "").strip()[:240]
        raise PairingError(stderr or "tailscale serve failed")
    refreshed = serve_status(config=cfg, run=run)
    if refreshed and not proxy_is_published(refreshed, local_port):
        raise PairingError("tailscale serve did not publish the local backend")
    return {"https_published": True, "proxy": target, "https_error": ""}
