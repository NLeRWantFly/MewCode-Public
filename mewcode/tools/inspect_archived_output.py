from __future__ import annotations

import asyncio
from typing import Callable

from pydantic import BaseModel, Field

from mewcode.context.working_set import OutputArchive
from mewcode.tools.base import Tool, ToolResult


class InspectArchivedOutputParams(BaseModel):
    ref_id: str = Field(pattern=r"^[0-9a-f]{64}$", description="Reference from an archived tool result.")
    start_line: int = Field(default=1, ge=1, description="One-based original line number.")
    limit: int = Field(default=40, ge=1, le=100)
    contains: str = Field(default="", max_length=200, description="Optional literal search, not a regular expression.")
    char_offset: int = Field(default=0, ge=0, description="Continuation offset for a long line, returned by the previous read.")


class InspectArchivedOutput(Tool):
    name = "InspectArchivedOutput"
    description = (
        "Read a bounded slice of a tool output archived in this conversation. "
        "Use the exact ref_id, optionally search with contains, and follow next_line/"
        "next_char_offset for more. This reads evidence only; it never repeats a tool action."
    )
    category = "read"
    params_model = InspectArchivedOutputParams
    is_concurrency_safe = True
    consumes_runtime_slot = False

    def __init__(self, archive: Callable[[], OutputArchive]):
        self._archive = archive

    async def execute(self, params: InspectArchivedOutputParams) -> ToolResult:
        try:
            output = await asyncio.to_thread(
                self._archive().inspect, params.ref_id, params.start_line,
                params.limit, params.contains, params.char_offset,
            )
            return ToolResult(output)
        except (OSError, ValueError):
            return ToolResult(
                "Archived output is unavailable or invalid in this conversation. "
                "Do not infer its contents or repeat a mutating action to recover it.",
                is_error=True,
            )
