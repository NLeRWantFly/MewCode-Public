"""Capability snapshot adapters for runtime Codex and Grok manifests."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Sequence

from .coord_protocol import CoordinationError, normalize_capabilities


def _grok_environment(cwd: str | Path | None, grok_home: str | Path | None = None) -> dict[str, str]:
    """Use the explicit shared Grok home, or the project-scoped default."""
    environment = dict(os.environ)
    selected = grok_home or environment.get("MEWCODE_GROK_HOME")
    if selected is None and cwd is not None:
        selected = Path(cwd).resolve() / ".mewcode" / "grok-home"
    if selected is not None:
        environment["GROK_HOME"] = str(Path(selected).expanduser().resolve())
    return environment


def codex_manifest(
    *,
    agent_id: str,
    model_id: str,
    role: str,
    tools: Sequence[dict[str, Any] | str],
    skills: Sequence[dict[str, Any] | str],
    mcp_servers: Sequence[dict[str, Any] | str],
    source: str = "codex-runtime",
) -> dict[str, Any]:
    """Build the connector-supplied Codex manifest without adding permissions."""
    return normalize_capabilities({
        "agent_id": agent_id,
        "model_id": model_id,
        "role": role,
        "tools": list(tools),
        "skills": list(skills),
        "mcp_servers": list(mcp_servers),
        "source": source,
    })


def grok_inspect_manifest(
    payload: str | bytes | dict[str, Any],
    *,
    agent_id: str = "grok-executor-01",
    model_id: str = "grok-4.6",
    role: str = "executor",
    source: str = "grok-inspect",
) -> dict[str, Any]:
    """Adapt ``grok inspect --json`` output and expose the terminal adapter."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise CoordinationError("grok inspect output is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise CoordinationError("grok inspect output must be an object")
    raw = payload.get("capabilities") if isinstance(payload.get("capabilities"), dict) else payload
    tools = raw.get("tools", raw.get("runtime_tools", []))
    if isinstance(tools, list):
        has_terminal_adapter = any(
            (item if isinstance(item, str) else item.get("name", "")) == "run_terminal_cmd"
            for item in tools
            if isinstance(item, (str, dict))
        )
        if not has_terminal_adapter:
            tools = [*tools, "run_terminal_cmd"]
    result = {
        "agent_id": agent_id,
        "model_id": model_id,
        "role": role,
        "tools": tools,
        "skills": raw.get("skills", []),
        "mcp_servers": raw.get("mcp_servers", raw.get("mcpServers", [])),
        "source": source,
    }
    normalized = normalize_capabilities(result)

    # Normalization sorts entries for stable manifests.  The inspect adapter
    # represents an ordered runtime list, so restore that list's order here.
    if isinstance(tools, list):
        by_name: dict[str, list[dict[str, Any]]] = {}
        for item in normalized["tools"]:
            by_name.setdefault(str(item["name"]), []).append(item)
        ordered: list[dict[str, Any]] = []
        for item in tools:
            name = item if isinstance(item, str) else item.get("name", "")
            ordered.append(by_name[str(name)].pop(0))
        normalized["tools"] = ordered
    return normalized


def run_grok_inspect(
    *,
    executable: str = "grok",
    cwd: str | Path | None = None,
    grok_home: str | Path | None = None,
    timeout: float = 20,
) -> dict[str, Any]:
    """Query the local Grok installation; no model prompt is sent."""
    try:
        completed = subprocess.run(
            [executable, "inspect", "--json"],
            cwd=str(cwd) if cwd else None,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_grok_environment(cwd, grok_home),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CoordinationError(f"grok inspect failed: {exc}") from exc
    return grok_inspect_manifest(completed.stdout)
