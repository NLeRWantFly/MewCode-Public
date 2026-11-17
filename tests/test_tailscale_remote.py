from __future__ import annotations

import json
import subprocess
import urllib.error
from types import SimpleNamespace

from mewcode.config import TailscaleConfig
from mewcode.tailscale_remote import collect_tailscale_status, discover_mewcode_nodes


def test_collect_status_discovers_only_online_tailnet_peer(monkeypatch):
    payload = {
        "BackendState": "Running",
        "Version": "1.80.0",
        "Self": {"ID": "self", "HostName": "local", "TailscaleIPs": ["100.64.1.1"]},
        "Peer": {
            "peer-a": {
                "ID": "peer-a", "HostName": "remote", "DNSName": "remote.tail123.ts.net.",
                "Online": True, "TailscaleIPs": ["100.64.1.2"],
            },
            "peer-offline": {
                "ID": "peer-offline", "HostName": "offline", "DNSName": "offline.tail123.ts.net.",
                "Online": False, "TailscaleIPs": ["100.64.1.3"],
            },
        },
    }
    calls: list[str] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return json.dumps({"node_id": "remote", "capabilities": {"history": True}}).encode()

    def opener(request, timeout):
        calls.append(request.full_url)
        return Response()

    monkeypatch.setattr("mewcode.tailscale_remote.urllib.request.urlopen", opener)
    result = collect_tailscale_status(
        TailscaleConfig(enabled=True, auto_discover=True, probe_ports=[443]),
        run=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, json.dumps(payload), ""),
    )
    assert result["connected"] is True
    assert result["nodes"][1].get("addresses") is None
    assert result["mewcode_nodes"][0]["id"] == "peer-a"
    assert all("offline" not in url for url in calls)


def test_discovery_rejects_public_hosts():
    config = TailscaleConfig(enabled=True, auto_discover=True, probe_ports=[443])
    snapshot = {"nodes": [{"id": "public", "self": False, "online": True, "dns_name": "example.com", "addresses": ["192.0.2.1"]}]}
    assert discover_mewcode_nodes(snapshot, config, opener=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError())) == []


def test_discovery_lists_timeout_and_offline_without_selecting_them():
    config = TailscaleConfig(enabled=True, auto_discover=True, probe_ports=[443])
    snapshot = {
        "nodes": [
            {"id": "live", "self": False, "online": True, "hostname": "desk", "dns_name": "desk.tail123.ts.net.", "addresses": ["100.64.1.2"]},
            {"id": "slow", "self": False, "online": True, "hostname": "laptop", "dns_name": "laptop.tail123.ts.net.", "addresses": ["100.64.1.4"]},
            {"id": "gone", "self": False, "online": False, "hostname": "phone", "dns_name": "phone.tail123.ts.net.", "addresses": ["100.64.1.5"]},
        ]
    }

    def opener(request, timeout):
        if "laptop" in request.full_url or "100.64.1.4" in request.full_url:
            raise TimeoutError("probe timed out")
        raise urllib.error.HTTPError(request.full_url, 401, "auth", hdrs=None, fp=None)

    nodes = discover_mewcode_nodes(snapshot, config, opener=opener)
    by_id = {item["id"]: item for item in nodes}
    assert by_id["live"]["selectable"] is True
    assert by_id["live"]["auth_required"] is True
    assert "capabilities" in by_id["live"]
    assert by_id["slow"]["selectable"] is False
    assert by_id["slow"]["status"] == "timeout"
    assert by_id["slow"]["hostname"] == "laptop"
    assert by_id["gone"]["status"] == "offline"
    assert by_id["gone"]["selectable"] is False
    assert nodes[0]["id"] == "live"

