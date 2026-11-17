"""Host-owned configuration for private same-volume staging directories.

These helpers are for an explicit host settings action. They are not model
tools and do not grant workspace access or enable native execution.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import uuid
from typing import Mapping

from mewcode.sandbox.lpac_runtime import checked_path
from mewcode.sandbox.publication_io import PinnedPath
from mewcode.sandbox.windows import NativeBoundaryUnavailable

MAX_CONFIG_BYTES = 16 * 1024
MAX_VOLUMES = 32
CONFIG_NAME = "volume-stores.json"
LOCK_NAME = "volume-stores.lock"
_LOCK_CONTENT = b"mewcode-host-volume-store-lock-v1\n"
_MISSING_REVISION = hashlib.sha256(b"mewcode-volume-stores-missing-v1").hexdigest()


class VolumeStoreError(ValueError):
    """Invalid, unsafe, stale, or unavailable host staging configuration."""


def _revision(raw: bytes | None) -> str:
    return _MISSING_REVISION if raw is None else hashlib.sha256(raw).hexdigest()


def _duplicate_checked_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise VolumeStoreError("Volume-store configuration contains a duplicate key")
        result[key] = value
    return result


def _registry_root(registry, *, create: bool = False) -> Path:
    root = Path(registry.root)
    if create and not os.path.lexists(root):
        # The registry root is host-owned application data. Validate its
        # existing ancestor chain before creating only this host directory.
        missing = []
        cursor = root
        while not os.path.lexists(cursor):
            missing.append(cursor)
            if cursor.parent == cursor:
                raise VolumeStoreError("Cannot locate a safe host registry parent")
            cursor = cursor.parent
        try:
            checked_path(cursor)
            for directory in reversed(missing):
                directory.mkdir()
                checked_path(directory)
        except (OSError, NativeBoundaryUnavailable) as exc:
            raise VolumeStoreError("Cannot create the host registry directory") from exc
    try:
        root = checked_path(root)
    except (OSError, NativeBoundaryUnavailable) as exc:
        raise VolumeStoreError("Host registry directory is unavailable") from exc
    if not root.is_dir():
        raise VolumeStoreError("Host registry path is not a directory")
    return root


def _config_path(root: Path) -> Path:
    return root / CONFIG_NAME


def _volume_key(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise VolumeStoreError("Volume-store keys must be absolute volume anchors")
    try:
        path = Path(value)
    except (TypeError, ValueError) as exc:
        raise VolumeStoreError("Invalid volume-store key") from exc
    if (not path.is_absolute() or not path.anchor or ".." in path.parts
            or path != Path(path.anchor)):
        raise VolumeStoreError("Volume-store keys must be volume roots")
    return os.path.normcase(path.anchor)


def _directory_text(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise VolumeStoreError("Volume-store paths must be absolute directory strings")
    try:
        path = Path(value)
    except (TypeError, ValueError) as exc:
        raise VolumeStoreError("Invalid volume-store path") from exc
    if (not path.is_absolute() or ".." in path.parts
            or value.startswith(("\\\\.\\", "\\\\?\\"))):
        raise VolumeStoreError("Volume-store paths must be absolute without traversal")
    return value


def _read_raw(root: Path) -> bytes | None:
    path = _config_path(root)
    if not os.path.lexists(path):
        return None
    try:
        checked_path(path, file=True)
        before = path.stat()
        if before.st_size > MAX_CONFIG_BYTES:
            raise VolumeStoreError("Volume-store configuration exceeds 16 KiB")
        with path.open("rb") as stream:
            raw = stream.read(MAX_CONFIG_BYTES)
            opened = os.fstat(stream.fileno())
        after = path.stat()
    except VolumeStoreError:
        raise
    except (OSError, NativeBoundaryUnavailable) as exc:
        raise VolumeStoreError("Volume-store configuration cannot be read safely") from exc
    if (opened.st_size > MAX_CONFIG_BYTES or len(raw) != opened.st_size
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or (opened.st_dev, opened.st_ino, opened.st_size)
            != (after.st_dev, after.st_ino, after.st_size)):
        raise VolumeStoreError("Volume-store configuration changed while being read")
    return raw


def _parse(raw: bytes | None) -> dict[str, str]:
    if raw is None:
        return {}
    try:
        decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=_duplicate_checked_object)
    except VolumeStoreError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VolumeStoreError("Volume-store configuration is not valid UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise VolumeStoreError("Volume-store configuration must be a volume map")
    if len(decoded) > MAX_VOLUMES:
        raise VolumeStoreError("Volume-store configuration exceeds 32 volumes")
    normalized = {}
    for key, value in decoded.items():
        anchor = _volume_key(key)
        if anchor in normalized:
            raise VolumeStoreError("Volume-store configuration has duplicate normalized volumes")
        normalized[anchor] = _directory_text(value)
    return normalized


def read_stores(registry) -> tuple[str, dict[str, str]]:
    """Return revision and validated legacy-compatible anchor-to-path map."""
    if not os.path.lexists(Path(registry.root)):
        return _revision(None), {}
    root = _registry_root(registry)
    raw = _read_raw(root)
    return _revision(raw), _parse(raw)


def _workspace_path(workspace: Path) -> Path:
    try:
        path = checked_path(Path(workspace))
    except (OSError, NativeBoundaryUnavailable, TypeError, ValueError) as exc:
        raise VolumeStoreError("Workspace must be an existing safe directory") from exc
    if not path.is_dir():
        raise VolumeStoreError("Workspace must be an existing directory")
    return path


def _validated_store(workspace: Path, directory: str) -> Path:
    text = _directory_text(directory)
    try:
        target = checked_path(Path(text))
    except FileNotFoundError as exc:
        raise VolumeStoreError("Staging directory must already exist") from exc
    except (OSError, NativeBoundaryUnavailable) as exc:
        raise VolumeStoreError("Staging directory must be an existing safe directory") from exc
    if not target.is_dir():
        raise VolumeStoreError("Staging directory must already exist")
    if target == Path(target.anchor):
        raise VolumeStoreError("A volume root cannot be used as a private staging directory")
    try:
        workspace_info, target_info = workspace.stat(), target.stat()
    except OSError as exc:
        raise VolumeStoreError("Workspace or staging directory cannot be inspected") from exc
    if workspace_info.st_dev != target_info.st_dev:
        raise VolumeStoreError("Staging directory must be on the workspace volume")
    if (target == workspace or target.is_relative_to(workspace)
            or workspace.is_relative_to(target)):
        raise VolumeStoreError("Workspace and staging directory must be disjoint")
    return target


def _ensure_lock(root: Path) -> Path:
    path = root / LOCK_NAME
    if not os.path.lexists(path):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        except OSError as exc:
            raise VolumeStoreError("Cannot create the host volume-store lock") from exc
        else:
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(_LOCK_CONTENT)
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as exc:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise VolumeStoreError("Cannot initialize the host volume-store lock") from exc
    try:
        checked_path(path, file=True)
        with path.open("rb") as stream:
            content = stream.read(len(_LOCK_CONTENT) + 1)
    except (OSError, NativeBoundaryUnavailable) as exc:
        raise VolumeStoreError("Host volume-store lock is unavailable") from exc
    if content != _LOCK_CONTENT:
        raise VolumeStoreError("Host volume-store lock was not created by this host")
    return path


@contextmanager
def _exclusive_lock(root: Path):
    path = _ensure_lock(root)
    try:
        pinned = PinnedPath(path, exclusive=True)
    except (OSError, NativeBoundaryUnavailable) as exc:
        raise VolumeStoreError("Another host process is configuring volume stores") from exc
    try:
        yield
    finally:
        pinned.close()


def _encode(stores: Mapping[str, str]) -> bytes:
    raw = (json.dumps(stores, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")) + "\n").encode("utf-8")
    if len(raw) > MAX_CONFIG_BYTES:
        raise VolumeStoreError("Updated volume-store configuration exceeds 16 KiB")
    return raw


def _atomic_write(root: Path, raw: bytes) -> None:
    target = _config_path(root)
    if os.path.lexists(target):
        try:
            checked_path(target, file=True)
        except (OSError, NativeBoundaryUnavailable) as exc:
            raise VolumeStoreError("Volume-store configuration target is unsafe") from exc
    temporary = root / (".volume-stores-" + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        checked_path(temporary, file=True)
        os.replace(temporary, target)
        if os.name != "nt":
            descriptor = os.open(root, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except (OSError, NativeBoundaryUnavailable) as exc:
        raise VolumeStoreError("Cannot atomically save volume-store configuration") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def describe(registry, workspace: Path) -> dict[str, str | None]:
    """Describe the current workspace's selected host staging directory."""
    current_workspace = _workspace_path(workspace)
    revision, stores = read_stores(registry)
    volume = os.path.normcase(current_workspace.anchor)
    return {"revision": revision, "directory": stores.get(volume), "volume": volume}


def resolve(registry, workspace: Path) -> Path | None:
    """Resolve and revalidate the configured store for this workspace."""
    current_workspace = _workspace_path(workspace)
    _revision_value, stores = read_stores(registry)
    raw = stores.get(os.path.normcase(current_workspace.anchor))
    if raw is None:
        return None
    return _validated_store(current_workspace, raw)


def configure(registry, workspace: Path, directory: str, *, expected_revision: str
              ) -> dict[str, str]:
    """CAS-update one volume's existing, disjoint same-volume staging path.

    Call only after explicit host confirmation. ``directory`` is host UI input;
    it is not an authorization grant and must never be sourced from a model.
    """
    if not isinstance(directory, str):
        raise ValueError("Staging directory must be an absolute path string")
    if not isinstance(expected_revision, str) or len(expected_revision) != 64:
        raise VolumeStoreError("A current volume-store revision is required")

    # A stale request is rejected before creating the host lock or touching
    # the registry. The authoritative comparison is repeated under the lock.
    initial_revision, _ = read_stores(registry)
    if expected_revision != initial_revision:
        raise VolumeStoreError("Volume-store configuration changed; reload before saving")

    root = _registry_root(registry, create=True)
    selected_workspace = _workspace_path(workspace)
    selected_directory = _validated_store(selected_workspace, directory)
    volume = os.path.normcase(selected_workspace.anchor)
    with _exclusive_lock(root):
        raw = _read_raw(root)
        current_revision = _revision(raw)
        if expected_revision != current_revision:
            raise VolumeStoreError("Volume-store configuration changed; reload before saving")
        stores = _parse(raw)
        stores[volume] = str(selected_directory)
        if len(stores) > MAX_VOLUMES:
            raise VolumeStoreError("Volume-store configuration exceeds 32 volumes")
        encoded = _encode(stores)
        _atomic_write(root, encoded)
        return {"revision": _revision(encoded), "directory": str(selected_directory),
                "volume": volume}
