"""Host-handled resolution of deferred Auto operations; never grants access."""
from typing import Literal

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult


class ResolveAutoReviewParams(BaseModel):
    action: Literal["abandon"] = "abandon"
    review_call_refs: list[str] = Field(min_length=1, max_length=100,
        description="Host-issued review_call_ref values for operations you will not perform.")
    reason: str = Field(min_length=1, max_length=2000,
        description="Explain what will remain undone or the materially safer alternative. This does not grant permission or erase prior effects.")


class ResolveAutoReview(Tool):
    name = "ResolveAutoReview"
    description = (
        "Explicitly abandon pending Auto operations using their host review_call_ref. "
        "Allows independent work and an honest partial conclusion; related alternatives still need review. "
        "Never use abandonment to claim the original work completed or to erase a denial."
    )
    params_model = ResolveAutoReviewParams
    category = "read"
    is_system_tool = True
    should_defer = True

    async def execute(self, params):
        return ToolResult("ResolveAutoReview requires the owning agent's host handler", is_error=True)
