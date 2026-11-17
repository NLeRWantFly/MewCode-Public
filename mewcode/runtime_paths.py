from __future__ import annotations

import json
import os
from pathlib import Path


DESKTOP_APP_ID = "com.mewcode.desktop"


def _windows_desktop_data_dir() -> Path | None:
    if os.name != "nt":
        return None
    roaming = os.environ.get("APPDATA", "").strip()
    if not roaming:
        return None
    path = Path(roaming) / DESKTOP_APP_ID
    return path if path.is_dir() else None


def _expert_run_snapshot_count(root: Path) -> int:
    folder = expert_runs_root(root)
    if not folder.is_dir():
        return 0
    return sum(
        1
        for path in folder.glob("*.json")
        if path.is_file() and not path.name.endswith(".public.json")
    )


def user_data_root() -> Path:
    """Writable user-level state root, overridable by the desktop sidecar.

    A bare ``python frontend/server.py`` defaults to ``~/.mewcode``. The Tauri
    desktop app instead writes expert-run checkpoints under
    ``%APPDATA%/com.mewcode.desktop``. If the home dir has no checkpoints but
    the desktop dir does, reuse the desktop dir so restarting a web helper
    does not look like expert history vanished.
    """
    configured = os.environ.get("MEWCODE_USER_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    home = Path.home() / ".mewcode"
    desktop = _windows_desktop_data_dir()
    if (
        desktop is not None
        and _expert_run_snapshot_count(home) == 0
        and _expert_run_snapshot_count(desktop) > 0
    ):
        return desktop.resolve()
    return home


def expert_runs_root(profile: Path | None = None) -> Path:
    """Use an explicitly configured host directory without following aliases.

    Run journals take write leases, whose paths must not contain junctions.
    A host storage move therefore records the real destination separately
    from any compatibility link left at the previous location.
    """
    profile = Path(profile) if profile is not None else user_data_root()
    config = profile / "storage-paths.json"
    if not os.path.lexists(config):
        return profile / "expert-runs-v1"
    from mewcode.sandbox.lpac_runtime import checked_path
    checked_path(config, file=True)
    if config.stat().st_size > 16 * 1024:
        raise ValueError("Run storage configuration exceeds 16 KiB")
    value = json.loads(config.read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or set(value) != {"schema_version", "expert_runs"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or not isinstance(value["expert_runs"], str) or not value["expert_runs"].strip()):
        raise ValueError("Invalid run storage configuration")
    target = Path(value["expert_runs"])
    if not target.is_absolute() or target == Path(target.anchor):
        raise ValueError("Run storage must be an absolute non-root directory")
    target = checked_path(target)
    if not target.is_dir():
        raise ValueError("Configured run storage is not a directory")
    return target
