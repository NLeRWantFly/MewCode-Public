from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from mewcode.config import MCPServerConfig
from mewcode.runtime_paths import user_data_root

PLAYWRIGHT_SERVER_NAME = "playwright"
PLAYWRIGHT_PACKAGE = "@playwright/mcp@0.0.82"
BLOCKED_MCP_TOOLS = frozenset({"browser_run_code_unsafe"})
UNATTENDED_BLOCKED_BROWSER_TOOLS = frozenset({"browser_click", "browser_type"})
BrowserSessionMode = Literal["ephemeral", "persistent"]


def uses_canonical_mcp_names(server_name: str) -> bool:
    return server_name == PLAYWRIGHT_SERVER_NAME


def is_blocked_mcp_tool(name: str) -> bool:
    return name in BLOCKED_MCP_TOOLS


def is_trusted_auto_browser_tool(name: str) -> bool:
    return name == "browser_session" or name.startswith("browser_")


def is_unattended_blocked_browser_tool(name: str) -> bool:
    return name in UNATTENDED_BLOCKED_BROWSER_TOOLS


def select_pipeline_session_tools(
    tools: Sequence[Any],
    *,
    enabled: Any = None,
    allowed_tools: Sequence[str] | None = None,
    allow_computer_use: bool = True,
    unattended: bool = False,
    matches: Any = None,
) -> list[Any]:
    selected: list[Any] = []
    allowed = list(allowed_tools) if allowed_tools else None
    for tool in tools:
        name = str(getattr(tool, "name", ""))
        if enabled is not None and not enabled(name):
            continue
        kind = str(getattr(tool, "interaction_kind", ""))
        if kind == "human_oncall":
            continue
        if allowed is not None and matches is not None and not matches(tool, allowed):
            continue
        if allowed is not None and matches is None and name not in allowed:
            continue
        if not allow_computer_use and kind == "computer_use":
            continue
        if unattended and is_unattended_blocked_browser_tool(name):
            continue
        selected.append(tool)
    return selected


def persistent_profile_dir() -> Path:
    return user_data_root() / "browser-profile"


def proxy_server_from_env(env: Mapping[str, str] | None = None) -> str:
    source = env if env is not None else os.environ
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY"):
        value = str(source.get(key, "") or "").strip()
        if value:
            return value
    return ""


def should_install_builtin_playwright(
    existing: Sequence[MCPServerConfig] | None,
) -> bool:
    return all(cfg.name != PLAYWRIGHT_SERVER_NAME for cfg in (existing or ()))


def merge_mcp_server_configs(
    existing: Sequence[MCPServerConfig] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    mode: BrowserSessionMode = "ephemeral",
) -> tuple[list[MCPServerConfig], bool]:
    user = list(existing or [])
    if not should_install_builtin_playwright(user):
        return user, False
    return [build_playwright_config(mode, env=env), *user], True


def build_playwright_config(
    mode: BrowserSessionMode = "ephemeral",
    *,
    env: Mapping[str, str] | None = None,
) -> MCPServerConfig:
    args = ["-y", "--prefer-offline", PLAYWRIGHT_PACKAGE]
    if mode == "persistent":
        profile = persistent_profile_dir()
        profile.mkdir(parents=True, exist_ok=True)
        args.extend(
            [
                "--browser",
                persistent_browser_channel(env),
                "--user-data-dir",
                str(profile),
            ]
        )
    else:
        args.extend(["--headless", "--isolated"])
        executable = installed_browser_executable(env)
        if executable:
            args.extend(["--executable-path", executable])
    proxy = proxy_server_from_env(env)
    if proxy:
        args.append(f"--proxy-server={proxy}")
    return MCPServerConfig(
        name=PLAYWRIGHT_SERVER_NAME,
        command="npx",
        args=args,
    )


def installed_browser_executable(env=None):
    """Use an installed Chromium browser without touching its user profile."""
    source = os.environ if env is None else env
    if os.name != "nt":
        return ""
    for base in (source.get("LOCALAPPDATA"), source.get("PROGRAMFILES"), source.get("PROGRAMFILES(X86)")):
        if not base:
            continue
        for relative in ("Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe"):
            path = Path(base) / relative
            if path.is_file():
                return str(path)
    return ""


def persistent_browser_channel(env: Mapping[str, str] | None = None) -> str:
    """Pick a Chromium channel the host can actually launch for the login profile."""
    executable = installed_browser_executable(env)
    return "msedge" if Path(executable).name.lower() == "msedge.exe" else "chrome"


def format_mcp_connect_error(name: str, exc: BaseException) -> str:
    detail = f"{type(exc).__name__}: {exc}"
    if name == PLAYWRIGHT_SERVER_NAME:
        hint = " Check Node/npx installation and PATH." if isinstance(exc, FileNotFoundError) else ""
        return f"MCP server '{name}': Playwright MCP initialization failed. {detail}{hint}"
    return f"MCP server '{name}': {detail}"


async def install_runtime_mcp(
    runtime: Any,
    user_configs: Sequence[MCPServerConfig] | None = None,
    *,
    include_builtin: bool = True,
) -> list[str]:
    """Install MCP servers into an AgentRuntime. Failures are notices, not fatal."""
    from mewcode.plugins.features import mcp_plugin

    await runtime.ainstall(
        mcp_plugin(list(user_configs or []), include_builtin=include_builtin)
    )
    errors = runtime.context.get("mcp.errors", None)
    if errors is None:
        run = runtime.context.plugin_run("runtime.mcp")
        detail = "MCP initialization did not publish its result"
        if run is not None:
            detail += f" (state={run.state.value})"
            if run.error:
                detail += f": {run.error}"
        return [format_mcp_connect_error(PLAYWRIGHT_SERVER_NAME, RuntimeError(detail))]
    return list(errors)
