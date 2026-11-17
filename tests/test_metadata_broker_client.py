from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct

import pytest

from mewcode.sandbox import metadata_broker as broker


def test_duplicate_response_fields_are_rejected():
    with pytest.raises(broker.BrokerError, match="Duplicate"):
        broker._unique([("ok", True), ("ok", False)])


def test_old_bundle_does_not_implicitly_elevate(tmp_path):
    helper = tmp_path / "helper.exe"
    helper.write_bytes(b"synthetic")
    assert broker.installed_artifact(helper) is None
    (tmp_path / "manifest.json").write_text(json.dumps({"protocols": ["mewcode-lpac-request-v1"]}))
    assert broker.installed_artifact(helper) is None


def test_declared_broker_requires_verified_bytes(tmp_path):
    helper = tmp_path / "helper.exe"
    helper.write_bytes(b"synthetic")
    executable = tmp_path / "broker/MewCode.MetadataBroker.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"broker-bytes")
    sha = hashlib.sha256(executable.read_bytes()).hexdigest()
    (tmp_path / "manifest.json").write_text(json.dumps({
        "protocols": ["mewcode-metadata-broker-v1"],
        "files": {"broker/MewCode.MetadataBroker.exe": sha}}))
    assert broker.installed_artifact(helper) == (executable.resolve(), sha)
    executable.write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="drift"):
        broker.installed_artifact(helper)


class Transport:
    def WaitForSingleObject(self, *_):
        return 258

    def WriteFile(self, handle, data, size, count, _):
        count._obj.value = size
        return True


def response_client(response):
    client = broker.Broker.__new__(broker.Broker)
    client.process = 1
    client.pipe = 2
    client.k = Transport()
    client.nonce = "a" * 64
    client.sequence = 0
    raw = json.dumps(response).encode()
    frames = iter((struct.pack("<I", len(raw)), raw))
    client._read = lambda _: next(frames)
    return client


@pytest.mark.parametrize("fields", [{"nonce": "wrong"}, {"seq": 2}, {"seq": True}, {"ok": 1}])
def test_response_identity_and_boolean_are_exact(fields):
    client = response_client({"nonce": "a" * 64, "seq": 1, "ok": True, **fields})
    with pytest.raises(broker.BrokerError):
        client.request("snapshot", handle=123, file_id=[1, 2])


def test_write_requires_active_host_scope():
    client = broker.Broker.__new__(broker.Broker)
    client.scope_active = False
    with pytest.raises(broker.BrokerError, match="host transaction"):
        client.apply(None, b"invalid")


def test_expiring_session_refuses_publication_before_mutation(monkeypatch):
    client = broker.Broker.__new__(broker.Broker)
    client.process, client.k, client.started = 1, Transport(), 0
    monkeypatch.setattr(broker.time, "monotonic", lambda: broker.SESSION_SECONDS - 100)
    with pytest.raises(broker.BrokerError, match="lifetime"):
        client.require_remaining(150)
    client.require_remaining(90)


def test_reentrant_scope_cannot_replace_or_end_active_transaction(monkeypatch, tmp_path):
    from types import SimpleNamespace
    active = SimpleNamespace(scope_active=True)
    monkeypatch.setattr(broker, "_SESSION", active)
    with pytest.raises(broker.BrokerError, match="Another metadata transaction"):
        with broker.transaction_scope(tmp_path, tmp_path, executable=tmp_path / "other.exe", sha256="a" * 64):
            pytest.fail("A second scope must never be entered")
    assert broker._SESSION is active
