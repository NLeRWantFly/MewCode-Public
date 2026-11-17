from __future__ import annotations

from fastapi.testclient import TestClient

import frontend.server as server
from mewcode.remote_pairing import RemotePairingStore


def _client(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "DESKTOP_TOKEN", "desktop-test-token")
    monkeypatch.setattr(server, "REMOTE_PAIRING", RemotePairingStore(tmp_path / "remote-pairing.json"))
    monkeypatch.setattr(
        server,
        "collect_tailscale_status",
        lambda _config: {
            "available": True,
            "connected": True,
            "self": {"dns_name": "desktop.example.ts.net."},
        },
    )
    return TestClient(server.app)


def test_pairing_request_allow_once_grants_remote_access(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    desktop_headers = {"Authorization": "Bearer desktop-test-token"}

    created = client.post(
        "/api/pairing/requests",
        json={"device_id": "android-123", "node_id": "n123", "label": "Pixel"},
    )
    assert created.status_code == 200
    pending = created.json()
    assert pending["status"] == "pending"
    assert pending["request_id"]
    assert "token" not in pending
    assert "code" not in pending

    assert client.get("/api/remote/info").status_code == 401
    listed = client.get("/api/pairing/status", headers=desktop_headers)
    assert listed.status_code == 200
    status = listed.json()
    assert status["available"] is True
    assert status["requests"][0]["device_id"] == "android-123"
    assert "token" not in status["requests"][0]
    assert "code" not in status["requests"][0]

    decided = client.post(
        "/api/pairing/decide",
        json={"request_id": pending["request_id"], "decision": "once"},
        headers=desktop_headers,
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "approved"
    assert "token" not in decided.json()

    polled = client.get(f"/api/pairing/requests/{pending['request_id']}")
    assert polled.status_code == 200
    token = polled.json()["token"]
    remote_headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/remote/info", headers=remote_headers).status_code == 200

    devices = client.get("/api/pairing/status", headers=desktop_headers).json()["devices"]
    assert devices[0]["device_id"] == "android-123"
    assert devices[0]["allow"] == "once"
    assert "token" not in devices[0]
    assert "code" not in devices[0]

    saved = (tmp_path / "remote-pairing.json").read_text(encoding="utf-8")
    assert token not in saved
    assert json_always_empty(saved)

    assert client.post("/api/pairing/decide", json={"request_id": pending["request_id"], "decision": "once"}).status_code == 401
    offers = client.post("/api/pairing/offers", json={}, headers=desktop_headers)
    assert offers.status_code in {404, 405}
    assert "code" not in (offers.json() if offers.headers.get("content-type", "").startswith("application/json") else {})

    revoked = client.post(
        "/api/pairing/revoke",
        json={"device_id": "android-123"},
        headers=desktop_headers,
    )
    assert revoked.status_code == 200
    assert revoked.json()["revoked"] is True
    assert client.get("/api/remote/info", headers=remote_headers).status_code == 401


def json_always_empty(saved: str) -> bool:
    import json
    payload = json.loads(saved)
    return payload.get("always") == {} and payload.get("devices") == {}


def test_always_allow_auto_approves_without_desktop_prompt(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    desktop_headers = {"Authorization": "Bearer desktop-test-token"}
    first = client.post(
        "/api/pairing/requests",
        json={"device_id": "android-123", "node_id": "n123"},
    ).json()
    client.post(
        "/api/pairing/decide",
        json={"request_id": first["request_id"], "decision": "always"},
        headers=desktop_headers,
    )
    second = client.post(
        "/api/pairing/requests",
        json={"device_id": "android-123", "node_id": "n123"},
    )
    assert second.status_code == 200
    payload = second.json()
    assert payload["status"] == "approved"
    assert payload["token"]
    assert client.get("/api/pairing/status", headers=desktop_headers).json()["requests"] == []
    assert client.get("/api/remote/info", headers={"Authorization": f"Bearer {payload['token']}"}).status_code == 200


def test_reject_then_retry_prompts_again(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    desktop_headers = {"Authorization": "Bearer desktop-test-token"}
    first = client.post(
        "/api/pairing/requests",
        json={"device_id": "android-123", "node_id": "n123"},
    ).json()
    denied = client.post(
        "/api/pairing/decide",
        json={"request_id": first["request_id"], "decision": "deny"},
        headers=desktop_headers,
    )
    assert denied.json()["status"] == "rejected"
    assert client.get(f"/api/pairing/requests/{first['request_id']}").json()["status"] == "rejected"
    retry = client.post(
        "/api/pairing/requests",
        json={"device_id": "android-123", "node_id": "n123"},
    ).json()
    assert retry["status"] == "pending"
    assert retry["request_id"] != first["request_id"]


def test_pairing_status_reports_disconnected_tailscale_without_blocking_devices(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    desktop_headers = {"Authorization": "Bearer desktop-test-token"}
    pending = client.post(
        "/api/pairing/requests",
        json={"device_id": "android-123", "node_id": "n123"},
    ).json()
    client.post(
        "/api/pairing/decide",
        json={"request_id": pending["request_id"], "decision": "always"},
        headers=desktop_headers,
    )
    monkeypatch.setattr(
        server,
        "collect_tailscale_status",
        lambda _config: {"available": True, "connected": False, "self": {}, "error": ""},
    )
    status = client.get("/api/pairing/status", headers=desktop_headers)
    assert status.status_code == 200
    payload = status.json()
    assert payload["available"] is False
    assert payload["error"] == "Tailscale is not connected"
    assert payload["devices"][0]["device_id"] == "android-123"
    assert "code" not in payload["devices"][0]


def test_pairing_hello_is_public_on_tailnet(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    hello = client.get("/api/pairing/hello")
    assert hello.status_code == 200
    assert hello.json()["pairing"] is True
    assert hello.json()["node_id"]


def test_pairing_status_uses_automatic_magicdns_endpoint(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    headers = {"Authorization": "Bearer desktop-test-token"}
    status = client.get("/api/pairing/status", headers=headers)
    assert status.status_code == 200
    payload = status.json()
    assert payload["available"] is True
    assert payload["endpoint"] == "https://desktop.example.ts.net"
    assert payload["devices"] == []
    assert payload["requests"] == []


def test_allow_once_does_not_survive_store_reload(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    desktop_headers = {"Authorization": "Bearer desktop-test-token"}
    pending = client.post(
        "/api/pairing/requests",
        json={"device_id": "android-123", "node_id": "n123"},
    ).json()
    client.post(
        "/api/pairing/decide",
        json={"request_id": pending["request_id"], "decision": "once"},
        headers=desktop_headers,
    )
    token = client.get(f"/api/pairing/requests/{pending['request_id']}").json()["token"]
    reloaded = RemotePairingStore(tmp_path / "remote-pairing.json")
    monkeypatch.setattr(server, "REMOTE_PAIRING", reloaded)
    assert client.get("/api/remote/info", headers={"Authorization": f"Bearer {token}"}).status_code == 401
