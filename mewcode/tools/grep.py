from __future__ import annotations

import asyncio
import itertools
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from pydantic import BaseModel, Field

from mewcode.tools.base import MAX_OUTPUT_CHARS, Tool, ToolResult, resolve_tool_path
from mewcode.tools.file_scan import iter_matching_files
from mewcode.runtime_workers import io_call


class Params(BaseModel):
    pattern: str = Field(description="Regex pattern to search for")
    path: str = Field(default=".", description="Base directory to search from")
    include: str = Field(default="", description="Glob filter for filenames (e.g. '*.py')")


class Grep(Tool):
    name = "Grep"
    description = "Search file contents using a regex pattern, returning file:line:content matches."
    params_model = Params
    category = "read"
    is_concurrency_safe = True


    async def execute(self, params: Params) -> ToolResult:
        base = resolve_tool_path(params.path)
        if not base.exists():
            return ToolResult(output=f"Error: path not found: {params.path}", is_error=True)

        try:
            regex = re.compile(params.pattern)
        except re.error as e:
            return ToolResult(output=f"Error: invalid regex: {e}", is_error=True)

        glob_pattern = params.include if params.include else "**/*"
        if not glob_pattern.startswith("**/"):
            glob_pattern = "**/" + glob_pattern

        cancel_requested = threading.Event()
        try:
            output = await io_call(
                _grep_files,
                base,
                regex,
                glob_pattern,
                cancel_requested,
            )
        except asyncio.CancelledError:
            # Cancelling ``to_thread`` cannot forcibly kill its OS thread.  The
            # cooperative flag stops traversal/line reading at the next safe
            # boundary while the chat task itself returns immediately.
            cancel_requested.set()
            raise
        return ToolResult(output=output)


def _grep_files(
    base: Path,
    regex: re.Pattern[str],
    glob_pattern: str,
    cancel_requested: threading.Event,
) -> str:
    results: list[str] = []
    output_chars = 0
    files = iter_matching_files(base, glob_pattern, cancel_requested)
    for file_path, relative in _candidate_files(files, regex, cancel_requested):
        try:
            with file_path.open("r", encoding="utf-8", errors="ignore") as handle:
                for line_num, raw_line in enumerate(handle, 1):
                    if cancel_requested.is_set():
                        return "\n".join(results) or "No matches found."
                    line = raw_line.rstrip("\r\n")
                    if not regex.search(line):
                        continue
                    rendered = f"{relative}:{line_num}:{line}"
                    # The agent truncates tool output at MAX_OUTPUT_CHARS. Stop
                    # doing disk work once additional matches cannot be seen.
                    if output_chars + len(rendered) + 1 > MAX_OUTPUT_CHARS:
                        results.append("… (output truncated)")
                        return "\n".join(results)
                    results.append(rendered)
                    output_chars += len(rendered) + 1
        except (OSError, UnicodeError):
            continue
    return "\n".join(results) if results else "No matches found."


def _candidate_files(files, regex, cancelled):
    """Native prefilter for plain ASCII literals; Python remains authoritative.

    Invalid UTF-8 is ignored by the legacy reader. Allow non-ASCII bytes
    between letters so ripgrep cannot miss a match created by that decoding.
    General Python regexes retain the original backend and semantics.
    """
    literal = regex.pattern
    rg = shutil.which("rg")
    if (not rg or not 1 <= len(literal) <= 128 or not literal.isascii()
            or any(c in literal for c in ".^$*+?{}[]\\|()\r\n")
            or regex.flags & ~re.UNICODE):
        yield from files
        return
    pattern = r"[^\x00-\x7f]*".join(f"\\x{ord(c):02x}" for c in literal)
    while not cancelled.is_set():
        batch = list(itertools.islice(files, 64))
        if not batch:
            return
        # Every path has passed the existing sensitive-path filter before rg
        # receives it. Explicit files preserve hidden/include/ignore behavior.
        process = None
        try:
            process = subprocess.Popen(
                [rg, "--no-config", "--files-with-matches", "--null", "--text", "--no-messages",
                 "--no-unicode", "--encoding", "none", "-e", pattern, "--",
                 *(str(path) for path, _ in batch)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            while True:
                if cancelled.is_set():
                    return
                try:
                    output, _ = process.communicate(timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode not in (0, 1):
                yield from batch
                continue
            matched = {os.fsdecode(path) for path in output.split(b"\0") if path}
            yield from (entry for entry in batch if str(entry[0]) in matched)
        except OSError:
            yield from batch
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()
