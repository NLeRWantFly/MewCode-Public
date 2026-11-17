from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from mewcode.sandbox import volume_stores
from mewcode.sandbox.publication_io import PinnedPath
from mewcode.sandbox.transaction_registry import TransactionRegistry


pytestmark = pytest.mark.skipif(os.name != "nt", reason="PinnedPath is a Windows host lock")


@pytest.fixture
def locations(tmp_path: Path):
    root = tmp_path / "host" / "native"
    root.mkdir(parents=True)
    registry = TransactionRegistry(root)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = tmp_path / "private-stage"
    store.mkdir()
    return registry, workspace, store, root


def test_configure_and_resolve_legacy_volume_mapping(locations):
    registry, workspace, store, root = locations
    before = volume_stores.describe(registry, workspace)

    saved = volume_stores.configure(
        registry, workspace, str(store), expected_revision=before["revision"],
    )

    assert saved["volume"] == os.path.normcase(workspace.anchor)
    assert saved["directory"] == str(store.resolve())
    assert saved["revision"] == hashlib.sha256((root / volume_stores.CONFIG_NAME).read_bytes()).hexdigest()
    assert volume_stores.describe(registry, workspace) == saved
    assert volume_stores.resolve(registry, workspace) == store.resolve()
    revision, stores = volume_stores.read_stores(registry)
    assert revision == saved["revision"]
    assert stores == {os.path.normcase(workspace.anchor): str(store.resolve())}


def test_reads_existing_legacy_json_mapping_without_rewriting(locations):
    registry, workspace, store, root = locations
    raw = json.dumps({workspace.anchor: str(store)}, separators=(",", ":")).encode("utf-8")
    config = root / volume_stores.CONFIG_NAME
    config.write_bytes(raw)

    description = volume_stores.describe(registry, workspace)

    assert description == {
        "revision": hashlib.sha256(raw).hexdigest(),
        "directory": str(store),
        "volume": os.path.normcase(workspace.anchor),
    }
    assert config.read_bytes() == raw


@pytest.mark.parametrize("target_kind", ["workspace", "nested", "ancestor", "volume_root", "relative", "traversal"])
def test_unsafe_or_overlapping_staging_paths_are_rejected(locations, target_kind):
    registry, workspace, store, _root = locations
    nested = workspace / "nested"
    nested.mkdir()
    targets = {
        "workspace": workspace,
        "nested": nested,
        "ancestor": workspace.parent,
        "volume_root": Path(workspace.anchor),
        "relative": Path("relative-stage"),
        "traversal": workspace.parent / ".." / workspace.name,
    }
    description = volume_stores.describe(registry, workspace)

    with pytest.raises(volume_stores.VolumeStoreError):
        volume_stores.configure(
            registry, workspace, str(targets[target_kind]),
            expected_revision=description["revision"],
        )


def test_directory_must_already_exist_and_none_is_not_coerced(locations, tmp_path):
    registry, workspace, _store, _root = locations
    missing = tmp_path / "must-not-be-created"
    revision = volume_stores.describe(registry, workspace)["revision"]

    with pytest.raises(volume_stores.VolumeStoreError, match="already exist"):
        volume_stores.configure(registry, workspace, str(missing), expected_revision=revision)
    assert not missing.exists()
    with pytest.raises(ValueError, match="path string"):
        volume_stores.configure(registry, workspace, None, expected_revision=revision)


def test_stale_revision_rejects_without_creating_registry_or_configuration(tmp_path):
    registry = TransactionRegistry(tmp_path / "not-created" / "native")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = tmp_path / "store"
    store.mkdir()

    with pytest.raises(volume_stores.VolumeStoreError, match="changed"):
        volume_stores.configure(registry, workspace, str(store), expected_revision="f" * 64)
    assert not registry.root.exists()


def test_stale_revision_after_an_update_leaves_config_bytes_unchanged(locations):
    registry, workspace, store, root = locations
    first = volume_stores.describe(registry, workspace)
    saved = volume_stores.configure(
        registry, workspace, str(store), expected_revision=first["revision"],
    )
    old_bytes = (root / volume_stores.CONFIG_NAME).read_bytes()
    other_store = root / "other"
    other_store.mkdir()

    with pytest.raises(volume_stores.VolumeStoreError, match="changed"):
        volume_stores.configure(
            registry, workspace, str(other_store), expected_revision=first["revision"],
        )

    assert (root / volume_stores.CONFIG_NAME).read_bytes() == old_bytes
    assert volume_stores.describe(registry, workspace) == saved


@pytest.mark.parametrize("raw", [
    b'{"schema":"unknown"}',
    b'{"C:\\\\": ["not-a-path"]}',
])
def test_unknown_fields_and_wrong_value_types_are_rejected(locations, raw):
    registry, workspace, _store, root = locations
    (root / volume_stores.CONFIG_NAME).write_bytes(raw)

    with pytest.raises(volume_stores.VolumeStoreError):
        volume_stores.describe(registry, workspace)


def test_duplicate_json_keys_are_rejected(locations):
    registry, workspace, store, root = locations
    key = json.dumps(workspace.anchor)
    value = json.dumps(str(store))
    raw = ("{" + key + ":" + value + "," + key + ":" + value + "}").encode("utf-8")
    (root / volume_stores.CONFIG_NAME).write_bytes(raw)

    with pytest.raises(volume_stores.VolumeStoreError, match="duplicate key"):
        volume_stores.describe(registry, workspace)


def test_configuration_rejects_more_than_32_volume_anchors(locations):
    registry, workspace, store, root = locations
    mapping = {workspace.anchor: str(store)}
    for index in range(volume_stores.MAX_VOLUMES):
        mapping[rf"\\host\share{index}" + "\\"] = str(store)
    (root / volume_stores.CONFIG_NAME).write_text(json.dumps(mapping), encoding="utf-8")

    with pytest.raises(volume_stores.VolumeStoreError, match="32 volumes"):
        volume_stores.describe(registry, workspace)


def test_existing_exclusive_host_lock_blocks_configuration(locations):
    registry, workspace, store, root = locations
    first = volume_stores.describe(registry, workspace)
    saved = volume_stores.configure(
        registry, workspace, str(store), expected_revision=first["revision"],
    )
    config = root / volume_stores.CONFIG_NAME
    old_bytes = config.read_bytes()
    alternate = root / "alternate"
    alternate.mkdir()

    with PinnedPath(root / volume_stores.LOCK_NAME, exclusive=True):
        with pytest.raises(volume_stores.VolumeStoreError):
            volume_stores.configure(
                registry, workspace, str(alternate), expected_revision=saved["revision"],
            )

    assert config.read_bytes() == old_bytes


def test_configuration_read_is_bounded(locations):
    registry, workspace, _store, root = locations
    (root / volume_stores.CONFIG_NAME).write_bytes(b" " * (volume_stores.MAX_CONFIG_BYTES + 1))

    with pytest.raises(volume_stores.VolumeStoreError, match="16 KiB"):
        volume_stores.describe(registry, workspace)
