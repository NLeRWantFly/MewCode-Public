from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from mewcode.filehistory import FileHistory


def _history(tmp_path: Path, session: str = "rollback-evidence") -> tuple[Path, FileHistory]:
    work = tmp_path / "workspace"
    work.mkdir()
    return work, FileHistory(str(work), session)


def test_existing_untracked_file_receipt_is_verified_and_not_overwritten_by_track_edit(tmp_path: Path) -> None:
    work, history = _history(tmp_path)
    target = work / "notes.bin"
    original = b"\x00before\xff\n"
    target.write_bytes(original)
    assert str(target) not in history._tracked

    receipt = history.prepare_rollback(target)

    assert receipt is not None
    assert receipt["path"] == str(target)
    assert receipt["backup_version"] == 1
    assert receipt["backup_id"].endswith("@prepared-v1")
    assert receipt["sha256"] == hashlib.sha256(original).hexdigest()
    assert receipt["size"] == len(original)
    assert receipt["existed"] is True
    assert receipt["kind"] == "verified_file_snapshot"
    assert receipt["scope"] == "file_bytes_and_existence_only"
    assert "directory layout" in receipt["exclusions"]
    assert "permissions and ACLs" in receipt["exclusions"]
    assert "network or other external side effects" in receipt["exclusions"]
    assert history.verify_rollback(receipt) is True

    backup_path = Path(history._prepared_by_id[receipt["backup_id"]]["backup_path"])
    same_receipt = history.prepare_rollback(target)
    assert same_receipt == receipt
    assert len(list(history._session_dir.glob("*@prepared-v*"))) == 1
    assert backup_path.read_bytes() == original

    history.track_edit(str(target))
    assert backup_path.read_bytes() == original
    target.write_bytes(b"after-write")
    assert history.verify_rollback(receipt) is False

    changes = history.make_snapshot(4, "edited one file")
    assert changes == [{"path": "notes.bin", "kind": "modify"}]
    assert len(history.get_snapshots()) == 1
    changed = history.rewind(0)
    assert changed == [str(target)]
    assert target.read_bytes() == original


def test_missing_target_receipt_records_nonexistence_and_rewind_removes_created_file(tmp_path: Path) -> None:
    work, history = _history(tmp_path)
    target = work / "new.bin"

    receipt = history.prepare_rollback(target)

    assert receipt is not None
    assert receipt["existed"] is False
    assert receipt["sha256"] == hashlib.sha256(b"").hexdigest()
    assert receipt["size"] == 0
    assert history.verify_rollback(receipt) is True

    history.track_edit(str(target))
    target.write_bytes(b"new content")
    assert history.verify_rollback(receipt) is False
    assert history.make_snapshot(1, "create one file") == [{"path": "new.bin", "kind": "add"}]
    assert history.rewind(0) == [str(target)]
    assert not target.exists()


def test_repeated_prepare_reuses_unchanged_backup_and_changed_file_gets_new_version(tmp_path: Path) -> None:
    work, history = _history(tmp_path)
    target = work / "config.txt"
    target.write_bytes(b"one")

    first = history.prepare_rollback(target)
    repeated = history.prepare_rollback(target)
    target.write_bytes(b"two")
    second = history.prepare_rollback(target)

    assert first == repeated
    assert second is not None
    assert second["backup_version"] == first["backup_version"] + 1
    assert second["backup_id"] != first["backup_id"]
    assert second["sha256"] == hashlib.sha256(b"two").hexdigest()
    assert history.verify_rollback(second) is True
    assert history.verify_rollback(first) is False


def test_receipt_verification_fails_when_backup_bytes_are_tampered(tmp_path: Path) -> None:
    work, history = _history(tmp_path)
    target = work / "record.txt"
    target.write_bytes(b"unchanged target")
    receipt = history.prepare_rollback(target)
    assert receipt is not None

    backup_path = Path(history._prepared_by_id[receipt["backup_id"]]["backup_path"])
    backup_path.write_bytes(b"changed backup")

    assert history.verify_rollback(receipt) is False
    assert history.verify_rollback({**receipt, "sha256": "0" * 64}) is False


def test_prepare_refuses_outside_directories_permissions_metadata_and_secret_paths(tmp_path: Path) -> None:
    work, history = _history(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    directory = work / "folder"
    directory.mkdir()
    git_target = work / ".git" / "config"
    git_target.parent.mkdir()
    git_target.write_text("metadata", encoding="utf-8")
    mewcode_target = work / ".mewcode" / "permissions.local.yaml"
    mewcode_target.parent.mkdir(exist_ok=True)
    mewcode_target.write_text("permission metadata", encoding="utf-8")
    host_target = work / "AGENTS.md"
    host_target.write_text("host instructions", encoding="utf-8")
    secret_target = work / ".env.local"
    secret_target.write_text("should never be read", encoding="utf-8")
    aws_credentials = work / ".aws" / "credentials"
    aws_credentials.parent.mkdir()
    aws_credentials.write_text("should never be read", encoding="utf-8")
    cloud_credentials = work / ".config" / "gcloud" / "application_default_credentials.json"
    cloud_credentials.parent.mkdir(parents=True)
    cloud_credentials.write_text("should never be read", encoding="utf-8")

    for candidate in (
        outside, directory, git_target, mewcode_target, host_target, secret_target,
        aws_credentials, cloud_credentials,
    ):
        assert history.prepare_rollback(candidate) is None


def test_prepare_refuses_symlink_and_oversized_file(tmp_path: Path) -> None:
    work, history = _history(tmp_path)
    external = tmp_path / "external.txt"
    external.write_bytes(b"external")
    link = work / "linked.txt"
    try:
        link.symlink_to(external)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable: {type(exc).__name__}")

    oversized = work / "large.txt"
    oversized.write_bytes(b"0123456789")

    assert history.prepare_rollback(link) is None
    assert history.prepare_rollback(oversized, max_bytes=9) is None
    assert history.prepare_rollback(oversized, max_bytes=MAX_ROLLBACK_BYTES + 1) is None


def test_prepare_returns_none_on_target_read_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work, history = _history(tmp_path)
    target = work / "unreadable.txt"
    target.write_bytes(b"content")
    original_open = os.open

    def fail_target_open(path, flags, *args, **kwargs):
        if os.fspath(path) == str(target):
            raise PermissionError("synthetic read failure")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", fail_target_open)
    assert history.prepare_rollback(target) is None


@pytest.mark.parametrize("session_id", ["../escape", r"..\escape", "C:\\outside", ""])
def test_constructor_rejects_session_path_escape_before_creating_metadata(
    tmp_path: Path,
    session_id: str,
) -> None:
    work = tmp_path / "workspace"
    work.mkdir()

    with pytest.raises(ValueError):
        FileHistory(str(work), session_id)

    assert not (work / ".mewcode").exists()
    assert not (tmp_path / "escape").exists()


def test_constructor_rejects_workspace_metadata_link_before_creating_backup_directory(tmp_path: Path) -> None:
    work = tmp_path / "workspace"
    work.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    metadata_link = work / ".mewcode"
    try:
        metadata_link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable: {type(exc).__name__}")

    with pytest.raises(OSError):
        FileHistory(str(work), "safe-session")

    assert not (outside / "file-history").exists()


# The public default is intentionally repeated here so the test documents the
# configured cap without importing an implementation-only constant.
MAX_ROLLBACK_BYTES = 4 * 1024 * 1024
