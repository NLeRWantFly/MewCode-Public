from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from mewcode.mcp.manager import MCPManager
from mewcode.mcp.playwright_browser import (
    BrowserSessionMode,
    build_playwright_config,
    persistent_browser_channel,
    persistent_profile_dir,
)
from mewcode.tools import ToolRegistry
from mewcode.tools.base import Tool, ToolResult, ToolRiskProfile


class BrowserSessionParams(BaseModel):
    mode: Literal["ephemeral", "persistent"] = Field(
        description=(
            "ephemeral: isolated headless Chromium with no user cookies. "
            "persistent: headed Chromium (Chrome or Edge) using the MewCode "
            "browser-profile directory."
        )
    )


class BrowserSessionTool(Tool):
    name = "browser_session"
    description = (
        "Switch the real Playwright browser between isolated headless Chromium "
        "and a persistent Chromium profile (Chrome or Edge, for login). Switching "
        "restarts the browser process and closes the current page. Tool names stay "
        "the same."
    )
    params_model = BrowserSessionParams
    category = "harness"
    is_concurrency_safe = False
    should_defer = False
    interaction_kind = "browser"

    def __init__(self, manager: MCPManager, registry: ToolRegistry) -> None:
        self._manager = manager
        self._registry = registry

    def permission_risk(self, arguments: dict) -> ToolRiskProfile:
        return ToolRiskProfile(
            side_effect="external",
            network=True,
            reversible=True,
            target_type="browser",
        )

    async def execute(self, params: BrowserSessionParams) -> ToolResult:
        mode: BrowserSessionMode = params.mode
        config = build_playwright_config(mode)
        errors = await self._manager.replace_server(config, self._registry)
        if errors:
            return ToolResult(output="\n".join(errors), is_error=True)
        if mode == "persistent":
            detail = f"persistent {persistent_browser_channel()} profile at {persistent_profile_dir()}"
        else:
            detail = "isolated headless Chromium (no user cookies)"
        return ToolResult(
            output=(
                f"Browser session is now {mode} ({detail}). "
                "The previous page was closed. Continue with browser_navigate."
            )
        )
