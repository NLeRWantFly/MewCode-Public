import json
import threading

import pytest

from mewcode.remote_pairing import (
    REQUEST_TTL_SECONDS,
    PairingError,
    RemotePairingStore,
    pairing_peer_allowed,
    validate_endpoint,
)


def test_pending_request_roundtrip_and_never_persists_token(tmp_path):
    path = tmp_path / "pairing.json"
    store = RemotePairingStore(path)
    pending = store.submit_request("android-1", "node-phone", label="Pixel")
    assert pending.status == "pending"
    assert pending.request_id
    assert pending.token is None
    listed = store.list_requests()
    assert listed[0]["device_id"] == "android-1"
    assert listed[0]["node_id"] == "node-phone"
    assert "token" not in listed[0]
    assert "code" not in listed[0]

    approved = store.decide(pending.request_id, "once")
    assert approved.status == "approved"
    assert approved.allow == "once"
    assert store.validate_token(approved.token)["device_id"] == "android-1"
    polled = store.get_request(pending.request_id)
    assert polled.token == approved.token
    saved = path.read_text(encoding="utf-8")
    assert approved.token not in saved
    assert "once" not in saved or '"allow":"once"' not in saved
    payload = json.loads(saved)
    assert payload["always"] == {}
    assert payload["devices"] == {}
    assert payload["requests"] == {}


def test_allow_once_is_session_only_and_dies_with_new_store(tmp_path):
    path = tmp_path / "pairing.json"
    store = RemotePairingStore(path)
    pending = store.submit_request("android-1", "node-phone")
    approved = store.decide(pending.request_id, "once")
    assert store.list_devices()[0]["allow"] == "once"
    assert "token" not in store.list_devices()[0]
    restarted = RemotePairingStore(path)
    assert restarted.validate_token(approved.token) is None
    assert restarted.list_devices() == []
    dumped = json.loads(path.read_text(encoding="utf-8"))
    assert dumped["always"] == {}
    assert dumped["devices"] == {}


def test_always_allow_auto_approves_same_node(tmp_path):
    store = RemotePairingStore(tmp_path / "pairing.json")
    pending = store.submit_request("android-1", "node-phone", label="Pixel")
    first = store.decide(pending.request_id, "always")
    assert first.allow == "always"
    dumped = json.loads((tmp_path / "pairing.json").read_text(encoding="utf-8"))
    assert dumped["always"]
    assert first.token not in (tmp_path / "pairing.json").read_text(encoding="utf-8")

    second = store.submit_request("android-1", "node-phone")
    assert second.status == "approved"
    assert second.token
    assert second.request_id == ""
    assert store.list_requests() == []
    assert store.validate_token(second.token)["node_id"] == "node-phone"


def test_reject_does_not_blacklist_and_can_retry(tmp_path):
    store = RemotePairingStore(tmp_path / "pairing.json")
    first = store.submit_request("android-1", "node-phone")
    denied = store.decide(first.request_id, "deny")
    assert denied.status == "rejected"
    assert store.get_request(first.request_id).status == "rejected"
    retry = store.submit_request("android-1", "node-phone")
    assert retry.status == "pending"
    assert retry.request_id != first.request_id
    approved = store.decide(retry.request_id, "once")
    assert store.validate_token(approved.token) is not None


def test_same_node_reuses_pending_request(tmp_path):
    store = RemotePairingStore(tmp_path / "pairing.json")
    first = store.submit_request("android-1", "node-phone")
    second = store.submit_request("android-1", "node-phone", label="Renamed")
    assert first.request_id == second.request_id
    assert len(store.list_requests()) == 1


def test_request_expiry_cannot_be_decided(tmp_path):
    now = [1_000.0]
    store = RemotePairingStore(tmp_path / "pairing.json", clock=lambda: now[0])
    pending = store.submit_request("android-1", "node-phone")
    now[0] += REQUEST_TTL_SECONDS + 1
    with pytest.raises(PairingError, match="invalid or expired"):
        store.decide(pending.request_id, "once")
    store.cleanup()
    assert json.loads((tmp_path / "pairing.json").read_text(encoding="utf-8"))["requests"] == {}


def test_revoke_invalidates_always_and_session(tmp_path):
    store = RemotePairingStore(tmp_path / "pairing.json")
    pending = store.submit_request("android-1", "node-phone")
    always = store.decide(pending.request_id, "always")
    other = store.submit_request("android-2", "node-tablet")
    session = store.decide(other.request_id, "once")
    assert store.revoke_device("android-1") is True
    assert store.validate_token(always.token) is None
    assert store.validate_token(session.token) is not None
    again = store.submit_request("android-1", "node-phone")
    assert again.status == "pending"
    assert store.revoke_device("node-tablet") is True
    assert store.validate_token(session.token) is None


def test_list_devices_omits_secrets(tmp_path):
    store = RemotePairingStore(tmp_path / "pairing.json")
    pending = store.submit_request("android-1", "node-phone")
    device = store.decide(pending.request_id, "always")
    rows = store.list_devices()
    assert rows[0]["device_id"] == "android-1"
    assert rows[0]["node_id"] == "node-phone"
    assert rows[0]["allow"] == "always"
    assert "token" not in rows[0]
    assert "code" not in rows[0]
    assert device.token not in (tmp_path / "pairing.json").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("https://desk.tailnet.ts.net/", "https://desk.tailnet.ts.net"),
        ("https://100.100.10.2:8443/", "https://100.100.10.2:8443"),
        ("https://[fd7a:115c:a1e0::10]/", "https://[fd7a:115c:a1e0::10]"),
        ("http://100.64.1.2:8765/", "http://100.64.1.2:8765"),
        ("http://[fd7a:115c:a1e0::5]:8765", "http://[fd7a:115c:a1e0::5]:8765"),
    ],
)
def test_tailnet_endpoints_are_normalized(endpoint, expected):
    assert validate_endpoint(endpoint) == expected


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://desk.example.ts.net",
        "https://example.com",
        "https://192.168.1.2",
        "http://192.0.2.1:8765",
        "https://desk.example.ts.net/?token=secret",
        "https://desk.example.ts.net/proxy",
        "https://user:secret@desk.example.ts.net",
    ],
)
def test_non_tailnet_or_unsafe_endpoints_are_rejected(endpoint):
    with pytest.raises(PairingError):
        validate_endpoint(endpoint)


def test_pairing_peer_allows_tailnet_and_rejects_public():
    assert pairing_peer_allowed("100.64.1.8") is True
    assert pairing_peer_allowed("127.0.0.1") is True
    assert pairing_peer_allowed("testclient") is True
    assert pairing_peer_allowed("192.0.2.1") is False
    assert pairing_peer_allowed("8.8.8.8") is False
    assert pairing_peer_allowed("example.com") is False


def test_concurrent_decide_allows_exactly_one_consumer(tmp_path):
    store = RemotePairingStore(tmp_path / "pairing.json")
    pending = store.submit_request("android-1", "node-phone")
    results = []

    def decide():
        try:
            results.append(store.decide(pending.request_id, "once"))
        except PairingError:
            results.append(None)

    workers = [threading.Thread(target=decide) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert sum(result is not None for result in results) == 1
