"""Opt-in Windows srt pilot policy; never an AUTO execution fallback.

This module only builds a per-invocation policy and a trusted argv. It does
not install srt, change host ACLs/WFP rules, or attest the backend. The AUTO
runtime gate remains closed until separate real escape tests pass.
"""
from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Iterable


class SrtPilotUnavailable(RuntimeError):
    """The isolated pilot cannot be constructed from the declared inputs."""


def _directory(raw: str | Path, root: Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = root / path
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SrtPilotUnavailable(f"Pilot directory does not exist: {path}") from exc
    if not resolved.is_dir() or not resolved.is_relative_to(root):
        raise SrtPilotUnavailable("Pilot scope must be a directory inside the workspace")
    return resolved


def build_srt_pilot_settings(
    work_dir: str | Path,
    *,
    read_directories: Iterable[str | Path],
    write_directories: Iterable[str | Path] = (),
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    """Build a narrow, offline settings document for one synthetic run.

    The caller must start a fresh srt process for each policy. On Windows,
    filesystem grants are session-level, not per-command overrides.
    """
    try:
        root = Path(work_dir).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SrtPilotUnavailable("Pilot workspace does not exist") from exc
    if not root.is_dir():
        raise SrtPilotUnavailable("Pilot workspace is not a directory")
    reads = sorted({_directory(path, root) for path in read_directories})
    writes = sorted({_directory(path, root) for path in write_directories})
    if not reads:
        raise SrtPilotUnavailable("Pilot requires an explicit read scope")
    if root in writes:
        raise SrtPilotUnavailable("Pilot cannot grant write access to the workspace root")
    if any(not any(path.is_relative_to(read) for read in reads) for path in writes):
        raise SrtPilotUnavailable("Every write directory must also be readable")

    values = dict(os.environ if env is None else env)
    home = Path(values.get("USERPROFILE") or Path.home()).resolve(strict=False)
    appdata = Path(values.get("APPDATA") or home / "AppData" / "Roaming")
    localappdata = Path(values.get("LOCALAPPDATA") or home / "AppData" / "Local")
    # Deny the caller profile broadly, then re-allow only declared workspace
    # paths. This overlap must be proved by the real Windows escape suite;
    # the policy document alone is not an isolation attestation. Avoid globs:
    # Windows srt expands them only at initialization.
    deny_read = [
        home, home / ".ssh", home / ".aws", home / ".azure",
        home / ".codex" / "auth.json", home / ".git-credentials",
        appdata / "com.mewcode.desktop",
        localappdata / "Google" / "Chrome" / "User Data",
        localappdata / "Microsoft" / "Edge" / "User Data",
        root / ".mewcode", root / ".env", root / ".git" / "config",
    ]
    deny_write = [root / ".mewcode", root / ".git", root / ".env"]
    protected = [path.resolve(strict=False) for path in deny_write]
    if any(write == item or write.is_relative_to(item) for write in writes for item in protected):
        raise SrtPilotUnavailable("Pilot write scope overlaps host authority or repository metadata")

    return {
        "network": {
            "allowedDomains": [],
            "deniedDomains": ["*"],
            "allowLocalBinding": False,
        },
        "filesystem": {
            "denyRead": sorted({str(path.resolve(strict=False)) for path in deny_read}),
            "allowRead": [str(path) for path in reads],
            "allowWrite": [str(path) for path in writes],
            "denyWrite": [str(path) for path in protected],
        },
        "enableWeakerNestedSandbox": False,
        "enableWeakerNetworkIsolation": False,
    }


def srt_pilot_argv(
    command: str,
    *,
    work_dir: str | Path,
    settings_path: str | Path,
    node_exe: str | Path,
    srt_cli: str | Path,
    read_directories: Iterable[str | Path],
    write_directories: Iterable[str | Path] = (),
    env: dict[str, str] | None = None,
) -> list[str]:
    """Use host-selected binaries and verify the policy file; never PATH/npx."""
    if os.name != "nt":
        raise SrtPilotUnavailable("Windows srt pilot is Windows-only")
    if not command.strip():
        raise SrtPilotUnavailable("Empty pilot command")
    try:
        root = Path(work_dir).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SrtPilotUnavailable("Pilot workspace does not exist") from exc
    if not root.is_dir():
        raise SrtPilotUnavailable("Pilot workspace is not a directory")
    checked: list[Path] = []
    for raw, label in ((settings_path, "settings"), (node_exe, "node"), (srt_cli, "srt CLI")):
        try:
            path = Path(raw).expanduser()
            if not path.is_absolute() or path.is_symlink():
                raise SrtPilotUnavailable(f"{label} must be an absolute, non-link file")
            resolved = path.resolve(strict=True)
            if not resolved.is_file() or resolved.is_relative_to(root):
                raise SrtPilotUnavailable(f"{label} must be an existing file outside the workspace")
        except SrtPilotUnavailable:
            raise
        except (OSError, RuntimeError) as exc:
            raise SrtPilotUnavailable(f"{label} is unavailable") from exc
        checked.append(resolved)
    settings, node, cli = checked
    if node.name.lower() != "node.exe" or cli.name.lower() != "cli.js":
        raise SrtPilotUnavailable("Pilot requires explicit node.exe and srt dist/cli.js")
    expected = build_srt_pilot_settings(
        root, read_directories=read_directories,
        write_directories=write_directories, env=env,
    )
    try:
        actual = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SrtPilotUnavailable("Pilot settings file cannot be verified") from exc
    if actual != expected:
        raise SrtPilotUnavailable("Pilot settings differ from the declared read/write scope")
    # CLI -c accepts one command string without its positional escaping pass.
    return [str(node), str(cli), "--settings", str(settings), "-c", command]
