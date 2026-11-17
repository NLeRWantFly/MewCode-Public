from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult

if __import__("typing").TYPE_CHECKING:
    from mewcode.tools import ToolRegistry


class InvokeDeferredToolParams(BaseModel):
    name: str = Field(description="Exact deferred tool name returned by ToolSearch")
    arguments: dict[str, Any] = Field(
        default_factory=dict, description="Arguments for the discovered tool"
    )


class InvokeDeferredTool(Tool):
    """Stable provider-facing gateway for dynamically discovered tools.

    Agent resolves this wrapper to the real target before validation,
    permissions, hooks, rate limiting, and execution.  Keeping that work in the
    Agent preserves the normal safety pipeline while the model-visible tool call
    remains InvokeDeferredTool for cache stability.
    """

    name = "InvokeDeferredTool"
    description = (
        "Invoke a deferred tool previously returned by ToolSearch. Pass the exact "
        "tool name and arguments matching the schema returned by ToolSearch."
        ' The host resolves the target and runs its normal argument validation, permission checks and execution; '
        'the response is the target tool result, not a new grant of authority. '
        'If the tool is undiscovered, search its exact name first; if disabled or unavailable, report the capability gap. '
        'For schema errors, correct arguments using the returned schema; never blindly repeat effectful calls after an unknown outcome.'
    )
    params_model = InvokeDeferredToolParams
    category = "harness"
    is_concurrency_safe = False
    should_defer = False

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def resolve(self, name: str) -> Tool | None:
        tool = self._registry.get(name)
        if (
            tool is None
            or not getattr(tool, "should_defer", False)
            or not self._registry.is_discovered(name)
            or not self._registry.is_enabled(name)
        ):
            return None
        return tool

    async def execute(self, params: BaseModel) -> ToolResult:
        return ToolResult(
            output="InvokeDeferredTool must be executed through the Agent safety pipeline",
            is_error=True,
        )
