"""Install the named stdio coordination MCP in Codex and Grok user configs.

The installer deliberately edits only ``mcp_servers.mewcode-coordination``;
all other TOML text is preserved verbatim.  It never installs a model alias.
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path

SERVER_NAME = "mewcode-coordination"
TRIGGER_WORD = "协同"
ENABLED_TOOLS = [
    "coord_protocol",
    "coord_register_agent",
    "coord_sync_capabilities",
    "coord_get_capabilities",
    "coord_heartbeat",
    "coord_create_task",
    "coord_claim_task",
    "coord_poll_messages",
    "coord_ack_message",
    "coord_post_message",
    "coord_update_task",
    "coord_submit_result",
    "coord_close_task",
    "coord_get_task",
]
CODEX_INSTRUCTIONS = (
    "Coordination only; no shell, file, browser, or model execution. "
    "Use explicit task allowlists and capability hashes. "
    "Approval is host-managed by codex-auto-review; never invoke a Codex subagent or send that route label as a provider model. "
    f"Single-word trigger: when the user sends exactly {TRIGGER_WORD}, enable Codex-Grok collaboration mode for the next task and acknowledge without starting a model call. "
    f"When the user sends {TRIGGER_WORD}: followed by a task, immediately create a bounded task for grok-executor-01, let the always-on worker execute it, and let codex-verifier-01 verify the evidence. "
    "The only submodel for execution is the external Grok Build runner; never fall back to a Codex provider model. "
    "Do not delegate without declared tools, skills, paths, write mode, acceptance criteria, max turns, and deadline."
)


def _fixed_root(root: Path | None = None) -> Path:
    value = (root or Path(__file__).resolve().parents[1]).resolve()
    expected = Path(__file__).resolve().parents[1]
    if value != expected:
        raise ValueError(f"root must be the fixed MewCode project: {expected}")
    return value


def ensure_token(root: Path) -> Path:
    token = root / ".mewcode" / "coordination" / ".bridge-token"
    token.parent.mkdir(parents=True, exist_ok=True)
    if not token.exists():
        token.write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8")
    if not token.read_text(encoding="utf-8").strip():
        raise ValueError(f"token file is empty: {token}")
    return token


def _section_bounds(text: str, section: str) -> tuple[int, int] | None:
    lines = text.splitlines(keepends=True)
    start = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == f"[{section}]":
            start = index
            continue
        if start is not None and stripped.startswith("["):
            # A nested table belongs to the named server block. Remove it
            # together with the parent so stale env entries cannot survive a
            # reinstall and override the generated configuration.
            if stripped.startswith(f"[{section}.") or stripped.startswith(f"[[{section}."):
                continue
            return start, index
    return (start, len(lines)) if start is not None else None


def update_named_server(
    text: str,
    *,
    root: Path,
    token_file: Path,
    target: str = "codex",
) -> str:
    """Replace the named server block using the target client's schema.

    Codex accepts ``enabled_tools`` and ``instructions``.  Grok's current
    stdio configuration only accepts the command, args, and environment
    fields, so keeping those Codex-only keys there creates noisy inspect
    warnings even though the server itself is discoverable.
    """
    if target not in {"codex", "grok"}:
        raise ValueError("target must be 'codex' or 'grok'")
    block = (
        f"[mcp_servers.{SERVER_NAME}]\n"
        "command = \"uv\"\n"
        f"args = [\"run\", \"--project\", {root.as_posix()!r}, \"python\", \"-m\", \"mewcode.mcp.coord_server\"]\n"
        f"env = {{ MEWCODE_BRIDGE_ROOT = {root.as_posix()!r}, MEWCODE_BRIDGE_TOKEN_FILE = {token_file.as_posix()!r} }}\n"
    )
    if target == "codex":
        block += (
            f"enabled_tools = {ENABLED_TOOLS!r}\n"
            f"instructions = {CODEX_INSTRUCTIONS!r}\n"
        )
    # TOML strings use double quotes; repr above is only an escaping helper.
    block = block.replace("'", '\"')
    bounds = _section_bounds(text, f"mcp_servers.{SERVER_NAME}")
    if bounds is None:
        prefix = "" if not text or text.endswith("\n") else "\n"
        return text + prefix + "\n" + block
    lines = text.splitlines(keepends=True)
    start, end = bounds
    replacement = block + ("\n" if end < len(lines) else "")
    return "".join(lines[:start]) + replacement + "".join(lines[end:])


def install_one(
    config_path: Path,
    *,
    root: Path,
    token_file: Path,
    target: str = "codex",
    dry_run: bool = False,
) -> bool:
    original = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    updated = update_named_server(original, root=root, token_file=token_file, target=target)
    if updated == original:
        return False
    if dry_run:
        return True
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if config_path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = config_path.with_name(f"{config_path.name}.bak.{stamp}")
        shutil.copy2(config_path, backup)
    config_path.write_text(updated, encoding="utf-8")
    return True


def install_configs(
    *,
    root: Path | None = None,
    codex_config: Path | None = None,
    grok_config: Path | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    project = _fixed_root(root)
    token = ensure_token(project) if not dry_run else project / ".mewcode" / "coordination" / ".bridge-token"
    user_home = Path(os.environ.get("USERPROFILE") or Path.home())
    codex_path = (codex_config or user_home / ".codex" / "config.toml").resolve()
    grok_path = (grok_config or user_home / ".grok" / "config.toml").resolve()
    changes = {
        "codex": install_one(codex_path, root=project, token_file=token, target="codex", dry_run=dry_run),
        "grok": install_one(grok_path, root=project, token_file=token, target="grok", dry_run=dry_run),
        "root": str(project),
        "token_file": str(token),
        "dry_run": dry_run,
    }
    return changes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--codex-config", type=Path, default=None)
    parser.add_argument("--grok-config", type=Path, default=None)
    args = parser.parse_args()
    print(install_configs(root=args.root, codex_config=args.codex_config, grok_config=args.grok_config, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
