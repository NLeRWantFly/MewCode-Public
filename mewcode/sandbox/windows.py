"""Fail-closed P0 adapter for the installed Codex native Windows sandbox.

This adapter is deliberately not a claim that workspace scripts are safe to
run or skip review. The installed Windows backend requires broad read access,
and a real probe showed that its network-disabled profile still permits host
loopback connections. The runtime and fast path open only through the
evidence-driven gate in ``mewcode.sandbox.native_gate``: an independently
enforced replacement boundary must be installed, attested by the acceptance
matrix, and explicitly enabled on this machine.
"""
from __future__ import annotations

import os
from pathlib import Path


class NativeBoundaryUnavailable(RuntimeError):
    """No trustworthy native command backend is available on this host."""


def find_codex_sandbox_cli() -> Path:
    """Use the desktop-installed binary, never a model-controlled PATH entry."""
    if os.name != "nt":
        raise NativeBoundaryUnavailable("P0 native command boundary is Windows-only")
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if not local:
        raise NativeBoundaryUnavailable("LOCALAPPDATA is unavailable")
    base = (Path(local) / "OpenAI" / "Codex" / "bin").resolve(strict=False)
    try:
        candidates = [p for p in base.glob("*/codex.exe")
                      if p.is_file() and not p.is_symlink()
                      and p.resolve(strict=True).is_relative_to(base)]
    except (OSError, RuntimeError, ValueError) as exc:
        raise NativeBoundaryUnavailable("Codex native sandbox binary cannot be verified") from exc
    if not candidates:
        raise NativeBoundaryUnavailable("Codex native sandbox binary is not installed")
    try:
        return max(candidates, key=lambda p: p.stat().st_mtime_ns)
    except OSError as exc:
        raise NativeBoundaryUnavailable("Codex native sandbox binary cannot be inspected") from exc


def _native_denied_paths(work_dir: Path, env: dict[str, str]) -> tuple[Path, ...]:
    home = Path(env.get("USERPROFILE") or Path.home())
    appdata = Path(env.get("APPDATA") or home / "AppData" / "Roaming")
    local = Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local")
    protected = [
        home / ".ssh", home / ".aws", home / ".azure",
        home / ".config" / "gcloud", home / ".docker" / "config.json",
        home / ".git-credentials", home / ".netrc", home / ".codex" / "auth.json",
        appdata / "com.mewcode.desktop", appdata / "Mozilla" / "Firefox" / "Profiles",
        appdata / "Microsoft" / "Credentials", appdata / "Microsoft" / "Protect",
        local / "Google" / "Chrome" / "User Data",
        local / "Microsoft" / "Edge" / "User Data",
        local / "Microsoft" / "Credentials",
        work_dir / ".mewcode" / "permissions.grants.json",
        work_dir / ".mewcode" / "permissions.yaml",
        work_dir / ".mewcode" / "permissions.local.yaml",
        work_dir / ".mewcode" / "rules",
    ]
    return tuple(path.resolve(strict=False) for path in protected)


def _toml_string(value: str) -> str:
    # TOML basic strings share JSON escaping for paths, quotes and Unicode.
    import json
    return json.dumps(value, ensure_ascii=False)


def native_profile_override(work_dir: Path, env: dict[str, str] | None = None) -> str:
    """Build a bounded profile; static path checks are not a substitute for it."""
    if os.name != "nt":
        raise NativeBoundaryUnavailable("P0 native command boundary is Windows-only")
    root = work_dir.resolve(strict=True)
    if not root.is_dir():
        raise NativeBoundaryUnavailable("Command workspace does not exist")
    values = dict(os.environ if env is None else env)
    denied = sorted({path.as_posix() for path in _native_denied_paths(root, values)})
    filesystem = ",".join(f"{_toml_string(path)}=\"deny\"" for path in denied)
    return (
        'permissions.mewcode-p0={extends=":workspace",'
        f"filesystem={{{filesystem}}},network={{enabled=false,allow_local_binding=false}}}}"
    )


def native_argv(command: str, *, work_dir: str) -> list[str]:
    """Return an argv that cannot silently degrade to a host shell."""
    if not command.strip():
        raise NativeBoundaryUnavailable("Empty command")
    root = Path(work_dir).resolve(strict=True)
    cli = find_codex_sandbox_cli()
    from mewcode.tools.bash import _shell_command
    shell, shell_args = _shell_command(command)
    return [
        str(cli), "sandbox", "-P", "mewcode-p0", "-C", str(root),
        "-c", 'windows.sandbox="elevated"',
        "-c", native_profile_override(root),
        "--", shell, *shell_args,
    ]


def native_fastpath_ready() -> bool:
    """Open only when the installed boundary is attested *and* enabled.

    Delegates to the evidence-driven gate state machine; see
    ``mewcode.sandbox.native_gate``. Reviewed scripts may skip review only
    when the gate is in the ``enabled`` state on this machine.
    """
    from mewcode.sandbox.native_gate import native_gate_status
    return native_gate_status().ready


def native_runtime_ready() -> bool:
    """Open only when the installed boundary is attested *and* enabled.

    Delegates to the evidence-driven gate state machine; see
    ``mewcode.sandbox.native_gate``. The gate consumes host-side acceptance
    evidence bound to the installed bundle identity and this machine; a
    bundle's own manifest can never open it.
    """
    from mewcode.sandbox.native_gate import native_gate_status
    return native_gate_status().ready
