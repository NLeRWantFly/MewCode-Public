"""Versioned native runtime bundle: install, verify, rollback, uninstall.

Synthetic bundles only; no .tmp build, real helper, or desktop data directory
is touched. The production hash chain (assemble from the authorized build) is
exercised on the host in the release run, not in this file.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from mewcode.sandbox import lpac_runtime as lpac
from mewcode.sandbox.windows import NativeBoundaryUnavailable
from scripts import manage_native_runtime as mgr

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows native runtime only")

HELPER = "MewCode.AppContainerRuntime.exe"
DLLS = ("libiconv-2.dll", "libintl-8.dll", "libpcre2-8-0.dll", "zlib1.dll")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def write_bundle(directory: Path, *, version: str, git_payload: bytes,
                  extra_files: dict[str, bytes] | None = None) -> Path:
    directory.mkdir(parents=True)
    helper_payload = f"synthetic-helper-{version}".encode()
    (directory / HELPER).write_bytes(helper_payload)
    payloads: dict[str, bytes] = {"git/git.exe": git_payload}
    for name in DLLS:
        payloads[f"git/{name}"] = f"synthetic-{name}".encode()
    for relative, payload in payloads.items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    for relative, payload in (extra_files or {}).items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    manifest = {
        "schema": "mewcode-lpac-bundle-v1",
        "sha256": _sha(helper_payload),
        "production_enabled": False,
        "bundle_version": version,
        "created_utc": "2026-09-26T00:00:00Z",
        "git_version": "synthetic",
        "files": {relative: _sha(payload) for relative, payload in payloads.items()},
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return directory


def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "desktop-data"
    root.mkdir()
    return root


def test_install_status_verify_and_helper_resolution(tmp_path, monkeypatch):
    root = data_root(tmp_path)
    bundle = write_bundle(tmp_path / "bundle-a", version="test.1", git_payload=b"git-exe-a")
    result = mgr.install_bundle(bundle, root)
    assert result["installed_version"] == "test.1"
    assert result["production_enabled"] is False
    state = mgr.installed_state(root)
    assert state["installed"] is True
    assert state["verify_ok"] is True and state["verify_problems"] == []
    assert state["manifest"]["bundle_version"] == "test.1"
    assert state["manifest"]["production_enabled"] is False
    monkeypatch.setenv("MEWCODE_USER_DATA_DIR", str(root))
    helper = lpac.find_runtime_helper()
    assert helper.name == HELPER
    assert helper.parent == root / "native" / "windows-lpac-v1"


def test_install_refuses_tampered_bundle(tmp_path):
    root = data_root(tmp_path)
    bundle = write_bundle(tmp_path / "bundle-bad", version="test.1", git_payload=b"git-exe-b")
    (bundle / "git/git.exe").write_bytes(b"tampered-after-manifest")
    with pytest.raises(mgr.InstallerError, match="verification failed"):
        mgr.install_bundle(bundle, root)
    assert mgr.installed_state(root)["installed"] is False


def test_install_refuses_extra_and_traversal_entries(tmp_path):
    root = data_root(tmp_path)
    bundle = write_bundle(tmp_path / "bundle-extra", version="test.1", git_payload=b"git-exe-c",
                          extra_files={"rogue.txt": b"not declared"})
    with pytest.raises(mgr.InstallerError, match="extra file"):
        mgr.install_bundle(bundle, root)
    traversal = write_bundle(tmp_path / "bundle-traversal", version="test.1", git_payload=b"git-exe-d")
    manifest = json.loads((traversal / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"]["git/../../evil.exe"] = _sha(b"evil")
    (traversal / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(mgr.InstallerError, match="not allowed"):
        mgr.install_bundle(traversal, root)
    assert mgr.installed_state(root)["installed"] is False


def test_upgrade_archives_previous_and_rollback_restores(tmp_path, monkeypatch):
    root = data_root(tmp_path)
    first = write_bundle(tmp_path / "bundle-v1", version="v1", git_payload=b"git-exe-v1")
    second = write_bundle(tmp_path / "bundle-v2", version="v2", git_payload=b"git-exe-v2-different")
    mgr.install_bundle(first, root)
    upgrade = mgr.install_bundle(second, root)
    assert upgrade["installed_version"] == "v2"
    assert upgrade["archived_previous"] is not None
    state = mgr.installed_state(root)
    assert [entry["bundle_version"] for entry in state["versions"]] == ["v1"]
    assert all(entry["verify_ok"] for entry in state["versions"])
    rolled = mgr.rollback(root)
    assert rolled["installed_version"] == "v1"
    assert mgr.installed_state(root)["verify_ok"] is True
    monkeypatch.setenv("MEWCODE_USER_DATA_DIR", str(root))
    target = root / "native" / "windows-lpac-v1"
    assert (target / "git" / "git.exe").read_bytes() == b"git-exe-v1"


def test_uninstall_removes_bundle_and_closes_helper_lookup(tmp_path, monkeypatch):
    root = data_root(tmp_path)
    bundle = write_bundle(tmp_path / "bundle-e", version="test.9", git_payload=b"git-exe-e")
    mgr.install_bundle(bundle, root)
    monkeypatch.setenv("MEWCODE_USER_DATA_DIR", str(root))
    assert lpac.find_runtime_helper().is_file()
    mgr.uninstall(root)
    assert not (root / "native" / "windows-lpac-v1").exists()
    with pytest.raises(NativeBoundaryUnavailable):
        lpac.find_runtime_helper()
    with pytest.raises(mgr.InstallerError, match="No installed"):
        mgr.uninstall(root)


def test_find_helper_rejects_drifted_installed_git(tmp_path, monkeypatch):
    root = data_root(tmp_path)
    bundle = write_bundle(tmp_path / "bundle-f", version="test.2", git_payload=b"git-exe-f")
    mgr.install_bundle(bundle, root)
    monkeypatch.setenv("MEWCODE_USER_DATA_DIR", str(root))
    target = root / "native" / "windows-lpac-v1"
    (target / "git" / "git.exe").write_bytes(b"drifted")
    with pytest.raises(NativeBoundaryUnavailable, match="artifact drift"):
        lpac.find_runtime_helper()
    state = mgr.installed_state(root)
    assert state["verify_ok"] is False


def test_manifest_size_and_production_flags_are_enforced(tmp_path):
    root = data_root(tmp_path)
    bundle = write_bundle(tmp_path / "bundle-g", version="test.3", git_payload=b"git-exe-g")
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["production_enabled"] = True
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(mgr.InstallerError, match="production"):
        mgr.install_bundle(bundle, root)
    oversized = write_bundle(tmp_path / "bundle-h", version="test.4", git_payload=b"git-exe-h")
    manifest = json.loads((oversized / "manifest.json").read_text(encoding="utf-8"))
    manifest["padding"] = "x" * 17000
    (oversized / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(mgr.InstallerError, match="oversized"):
        mgr.install_bundle(oversized, root)
    assert mgr.installed_state(root)["installed"] is False


def test_cli_status_reports_data_root(tmp_path):
    root = data_root(tmp_path)
    bundle = write_bundle(tmp_path / "bundle-i", version="test.5", git_payload=b"git-exe-i")
    mgr.install_bundle(bundle, root)
    script = Path(mgr.__file__).resolve()
    completed = subprocess.run(
        [sys.executable, str(script), "status", "--data-root", str(root)],
        capture_output=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.decode("utf-8"))
    assert payload["installed"] is True
    assert payload["manifest"]["bundle_version"] == "test.5"
    assert payload["verify_ok"] is True


def test_status_does_not_misreport_access_denied_as_missing(tmp_path, monkeypatch):
    root = data_root(tmp_path)
    target = root / "native" / "windows-lpac-v1"
    original = Path.stat
    def denied(path, *args, **kwargs):
        if path == target:
            raise PermissionError("synthetic denied")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", denied)
    state = mgr.installed_state(root)
    assert state["installed"] is None
    assert not state["verify_ok"]
    assert "inaccessible" in state["verify_problems"][0]
