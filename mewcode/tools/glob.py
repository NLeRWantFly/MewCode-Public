from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from pydantic import BaseModel, Field

from mewcode.tools.base import MAX_OUTPUT_CHARS, Tool, ToolResult, resolve_tool_path
from mewcode.tools.file_scan import iter_matching_files
from mewcode.runtime_workers import io_call


class Params(BaseModel):
    pattern: str = Field(description="Glob pattern to match (e.g. '**/*.py')")
    path: str = Field(default=".", description="Base directory to search from")


class Glob(Tool):
    name = "Glob"
    description = "Find files matching a glob pattern, returning relative paths."
    params_model = Params
    category = "read"
    is_concurrency_safe = True


    async def execute(self, params: Params) -> ToolResult:
        base = resolve_tool_path(params.path)
        if not base.exists():
            return ToolResult(output=f"Error: path not found: {params.path}", is_error=True)

        cancel_requested = threading.Event()
        try:
            output = await io_call(
                _glob_files,
                base,
                params.pattern,
                cancel_requested,
            )
        except asyncio.CancelledError:
            cancel_requested.set()
            raise
        except Exception as e:
            return ToolResult(output=f"Error: {e}", is_error=True)
        return ToolResult(output=output)


def _glob_files(
    base: Path,
    pattern: str,
    cancel_requested: threading.Event,
) -> str:
    matches: list[str] = []
    output_chars = 0
    for _absolute, relative in iter_matching_files(base, pattern, cancel_requested):
        rendered = str(relative)
        if output_chars + len(rendered) + 1 > MAX_OUTPUT_CHARS:
            matches.append("… (output truncated)")
            break
        matches.append(rendered)
        output_chars += len(rendered) + 1
    return "\n".join(matches) if matches else "No files matched the pattern."
