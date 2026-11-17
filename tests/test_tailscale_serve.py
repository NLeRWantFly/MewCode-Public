from __future__ import annotations

import json
import subprocess

import pytest

from mewcode.remote_pairing import PairingError
from mewcode.tailscale_serve import ensure_https_proxy, proxy_is_published, serve_status


def _status(port: int) -> dict:
    return {
        "Web": {
            "machine.example.ts.net:443": {
                "Handlers": {
                    "/": {"Proxy": f"http://127.0.0.1:{port}"},
                },
            }
        }
    }


def test_proxy_is_published_matches_local_port():
    assert proxy_is_published(_status(8765), 8765) is True
    assert proxy_is_published(_status(8765), 9000) is False
    assert proxy_is_published({}, 8765) is False


def test_proxy_is_published_accepts_legacy_direct_handler_map():
    status = {
        "Web": {
            "machine.example.ts.net:443": {
                "/": {"Proxy": "http://127.0.0.1:8765"},
            }
        }
    }

    assert proxy_is_published(status, 8765) is True


def test_ensure_https_proxy_skips_when_already_published():
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(_status(8765)), "")

    result = ensure_https_proxy(8765, run=run)
    assert result["https_published"] is True
    assert result["proxy"] == "http://127.0.0.1:8765"
    assert len(calls) == 1
    assert calls[0][-3:] == ["serve", "status", "--json"]


def test_ensure_https_proxy_publishes_background_http_target():
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(command)
        if command[-1] == "--json":
            payload = {} if len(calls) == 1 else _status(1509)
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        return subprocess.CompletedProcess(command, 0, "", "")

    result = ensure_https_proxy(1509, run=run)
    assert result["https_published"] is True
    assert any(call[-4:] == ["serve", "--bg", "--yes", "1509"] for call in calls)


def test_ensure_https_proxy_rejects_invalid_port():
    with pytest.raises(PairingError, match="invalid local serve port"):
        ensure_https_proxy(0, run=lambda *args, **kwargs: None)


def test_serve_status_swallows_missing_cli():
    def run(command, **kwargs):
        raise FileNotFoundError("tailscale")

    assert serve_status(run=run) == {}
