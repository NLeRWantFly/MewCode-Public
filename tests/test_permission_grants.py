from __future__ import annotations

import json
from pathlib import Path

import pytest

from mewcode.permissions.grants import GrantKind, GrantStore, GrantStoreError


class FakeClock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def confirmation(store: GrantStore, kind: GrantKind | str, value, *, ttl: int = 3600,
                 confirmation_id: str = "confirm-00000001", confirmed_at: float | None = None):
    return {
        "confirmed": True,
        "actor": "user",
        "confirmation_id": confirmation_id,
        "confirmed_at": store.clock() if confirmed_at is None else confirmed_at,
        "scope": store.preview_scope(kind, value, ttl),
    }


def add(store: GrantStore, kind: GrantKind | str, value, *, ttl: int = 3600,
        confirmation_id: str = "confirm-00000001"):
    return store.grant(
        kind, value, ttl_seconds=ttl,
        confirmation=confirmation(store, kind, value, ttl=ttl, confirmation_id=confirmation_id),
    )


def test_typed_grants_persist_with_exact_confirmation_and_scope(tmp_path: Path) -> None:
    clock = FakeClock()
    folder = tmp_path / "source"
    folder.mkdir()
    store = GrantStore(tmp_path, clock=clock)

    grant = add(store, GrantKind.READ_DIRECTORY, folder)
    saved = GrantStore(tmp_path, clock=clock).list_grants()

    assert len(saved) == 1
    assert saved[0] == grant
    assert grant.kind is GrantKind.READ_DIRECTORY
    assert grant.value == str(folder.resolve())
    assert grant.expires_at - grant.created_at == 3600
    assert grant.version == 1
    assert grant.confirmed_by == "user"
    assert grant.confirmed_scope == {
        "kind": "read-directory", "value": str(folder.resolve()), "ttl_seconds": 3600,
    }
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["revision"] == 1


def test_grant_lookup_respects_type_subtree_siblings_and_exact_host(tmp_path: Path) -> None:
    clock = FakeClock()
    folder = tmp_path / "authorized"
    sibling = tmp_path / "authorized-neighbor"
    folder.mkdir()
    sibling.mkdir()
    read_store = GrantStore(tmp_path, clock=clock)
    read_grant = add(read_store, "read-directory", folder)

    assert read_store.matching_grant("read-directory", folder / "nested" / "file.md") == read_grant
    assert read_store.matching_grant("read-directory", sibling / "file.md") is None
    assert read_store.matching_grant("write-directory", folder / "file.md") is None

    host_store = GrantStore(tmp_path, relative_path=".mewcode/hosts.json", clock=clock)
    host_grant = add(host_store, "host", "Pypi.org", confirmation_id="confirm-00000002")
    assert host_grant.value == "pypi.org"
    assert host_store.matching_grant("host", "pypi.org") == host_grant
    assert host_store.matching_grant("host", "files.pypi.org") is None
    assert host_store.matching_grant("host", "https://pypi.org/simple") is None


def test_command_prefix_is_tokenized_and_matches_only_argv_prefix(tmp_path: Path) -> None:
    clock = FakeClock()
    store = GrantStore(tmp_path, clock=clock)
    grant = add(store, "command-prefix", ["python", "-m", "pytest"], confirmation_id="confirm-00000003")

    assert store.matching_grant("command-prefix", ["python", "-m", "pytest", "-q", "tests"]) == grant
    assert store.matching_grant("command-prefix", ["python", "-m", "ruff", "check"]) is None
    assert store.matching_grant("command-prefix", "python -m pytest") is None


@pytest.mark.parametrize("kind,value", [
    ("read-directory", "relative/path"),
    ("write-directory", "C:/some/*/folder"),
    ("host", "*.example.com"),
    ("host", "https://example.com"),
    ("host", "example.com:443"),
    ("command-prefix", ["python"]),
    ("command-prefix", ["python", "-m"]),
    ("command-prefix", ["python", "-c", "print(1)"]),
    ("command-prefix", ["python", "-m", "pip", "install", "requests"]),
    ("command-prefix", ["powershell", "-Command", "Get-ChildItem"]),
    ("command-prefix", ["git", "push", "origin; Remove-Item x"]),
    ("command-prefix", ["curl", "https://example.com", "Bearer supersecretvalue"]),
])
def test_rejects_wildcard_relative_or_broad_grants(tmp_path: Path, kind, value) -> None:
    store = GrantStore(tmp_path, clock=FakeClock())
    with pytest.raises(GrantStoreError):
        store.preview_scope(kind, value, 3600)


def test_rejects_filesystem_root_grant(tmp_path: Path) -> None:
    store = GrantStore(tmp_path, clock=FakeClock())
    with pytest.raises(GrantStoreError, match="root"):
        store.preview_scope("read-directory", Path(tmp_path.anchor), 3600)


def test_requires_fresh_exact_user_confirmation(tmp_path: Path) -> None:
    clock = FakeClock()
    folder = tmp_path / "data"
    folder.mkdir()
    store = GrantStore(tmp_path, clock=clock)
    base = confirmation(store, "read-directory", folder)

    bad_confirmations = [
        {**base, "confirmed": False},
        {**base, "actor": "model"},
        {**base, "scope": {**base["scope"], "ttl_seconds": 7200}},
        {**base, "scope": {**base["scope"], "value": str(tmp_path)}},
        {**base, "confirmed_at": clock.now - 301},
        {**base, "unreviewed": "extra metadata must not be persisted"},
    ]
    for index, metadata in enumerate(bad_confirmations):
        with pytest.raises(GrantStoreError):
            store.grant("read-directory", folder, ttl_seconds=3600, confirmation=metadata)

    assert store.list_grants() == []


def test_ttl_expires_and_revoke_is_persisted_and_versioned(tmp_path: Path) -> None:
    clock = FakeClock()
    folder = tmp_path / "data"
    folder.mkdir()
    store = GrantStore(tmp_path, clock=clock)
    grant = add(store, "write-directory", folder, ttl=120, confirmation_id="confirm-00000004")
    assert store.matching_grant("write-directory", folder / "file.txt") == grant

    clock.now += 121
    assert store.matching_grant("write-directory", folder / "file.txt") is None
    clock.now -= 121
    revoked = store.revoke(grant.grant_id, request_id="revoke-00000001")

    assert revoked.version == 2
    assert revoked.revoked_by == "user"
    assert revoked.revocation_id == "revoke-00000001"
    assert store.matching_grant("write-directory", folder / "file.txt") is None
    assert GrantStore(tmp_path, clock=clock).list_grants()[0] == revoked
    assert store.revoke(grant.grant_id, request_id="revoke-00000001") == revoked


def test_directory_grant_remains_revokeable_if_directory_disappears(tmp_path: Path) -> None:
    clock = FakeClock()
    folder = tmp_path / "vanishing"
    folder.mkdir()
    store = GrantStore(tmp_path, clock=clock)
    grant = add(store, "read-directory", folder, confirmation_id="confirm-00000005")
    folder.rmdir()

    loaded = GrantStore(tmp_path, clock=clock).list_grants()[0]
    assert loaded == grant
    assert store.revoke(grant.grant_id, request_id="revoke-00000002").revoked_at == clock.now


def test_invalid_scope_and_ttl_cannot_be_persisted(tmp_path: Path) -> None:
    clock = FakeClock()
    folder = tmp_path / "data"
    folder.mkdir()
    store = GrantStore(tmp_path, clock=clock)
    for ttl in (0, 59, 365 * 24 * 60 * 60 + 1, 60.5, True):
        with pytest.raises(GrantStoreError):
            store.preview_scope("read-directory", folder, ttl)
    with pytest.raises(GrantStoreError):
        GrantStore(tmp_path, relative_path="../outside.json", clock=clock)
    with pytest.raises(GrantStoreError):
        GrantStore(tmp_path, relative_path=".", clock=clock)


def test_corrupt_store_fails_closed_without_overwriting(tmp_path: Path) -> None:
    store = GrantStore(tmp_path, clock=FakeClock())
    store.path.parent.mkdir(parents=True)
    original = '{"schema_version":1,"schema_version":1}'
    store.path.write_text(original, encoding="utf-8")
    folder = tmp_path / "data"
    folder.mkdir()

    with pytest.raises(GrantStoreError):
        add(store, "read-directory", folder)
    assert store.path.read_text(encoding="utf-8") == original


def test_atomic_write_failure_keeps_previous_grant_and_cleans_temp(tmp_path: Path, monkeypatch) -> None:
    import mewcode.permissions.grants as grants_module

    clock = FakeClock()
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    store = GrantStore(tmp_path, clock=clock)
    add(store, "read-directory", first, confirmation_id="confirm-00000006")
    real_replace = grants_module.os.replace

    def fail_replace(*_args, **_kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(grants_module.os, "replace", fail_replace)
    with pytest.raises(GrantStoreError, match="atomically"):
        add(store, "write-directory", second, confirmation_id="confirm-00000007")
    monkeypatch.setattr(grants_module.os, "replace", real_replace)

    assert len(store.list_grants()) == 1
    assert list(store.path.parent.glob("*.tmp")) == []


def test_grant_store_never_serializes_unconfirmed_extra_fields(tmp_path: Path) -> None:
    clock = FakeClock()
    store = GrantStore(tmp_path, clock=clock)
    grant = add(store, "host", "example.org", confirmation_id="confirm-00000008")
    text = store.path.read_text(encoding="utf-8")

    assert "secret" not in text.lower()
    assert "user" in text
    assert grant.confirmation_id in text
    assert "permissions.grants.json" in str(store.path)
