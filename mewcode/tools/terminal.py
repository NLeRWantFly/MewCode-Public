"""Bounded pipe sessions; no PTY, no persistence across application restarts."""
from __future__ import annotations

import asyncio
import codecs
import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult, active_work_dir
from mewcode.tools.bash import _child_env, _shell_command, _terminate_process_tree


@dataclass
class Session:
    process: asyncio.subprocess.Process
    cwd: str
    output: str = ""
    offset: int = 0
    reader: asyncio.Task | None = None
    timer: asyncio.Task | None = None
    timed_out: bool = False


class TerminalSessions:
    def __init__(self):
        self.sessions: dict[str, Session] = {}

    async def start(self, command, lifetime):
        # Bound retained sessions as well as simultaneously running processes.
        for key, session in list(self.sessions.items()):
            if len(self.sessions) < 32:
                break
            if session.reader is not None and session.reader.done():
                del self.sessions[key]
        if len(self.sessions) >= 32:
            raise ValueError("Session limit reached; stop running terminals first.")
        cwd = str(Path(active_work_dir() or os.getcwd()).resolve())
        shell, args = _shell_command(command)
        process = await asyncio.create_subprocess_exec(
            shell, *args, cwd=cwd, env=_child_env(cwd),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        key = uuid.uuid4().hex
        session = Session(process, cwd)
        self.sessions[key] = session

        async def read():
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            try:
                while block := await process.stdout.read(4096):
                    session.output += decoder.decode(block)
                    overflow = max(0, len(session.output) - 200_000)
                    session.offset += overflow
                    session.output = session.output[overflow:]
                session.output += decoder.decode(b"", final=True)
                await process.wait()
            finally:
                if session.timer:
                    session.timer.cancel()

        async def timeout():
            await asyncio.sleep(lifetime)
            session.timed_out = True
            await _terminate_process_tree(process)

        session.reader = asyncio.create_task(read())
        session.timer = asyncio.create_task(timeout())
        return key

    def get(self, key):
        session = self.sessions.get(key)
        cwd = str(Path(active_work_dir() or os.getcwd()).resolve())
        if session is None or session.cwd != cwd:
            raise ValueError("Unknown terminal session in this workspace (sessions do not survive restart).")
        return session

    async def aclose(self):
        for session in self.sessions.values():
            await _terminate_process_tree(session.process)
            if session.timer:
                session.timer.cancel()
            if session.reader:
                session.reader.cancel()
        await asyncio.gather(*(t for s in self.sessions.values() for t in (s.reader, s.timer) if t), return_exceptions=True)


class StartParams(BaseModel):
    command: str
    lifetime_seconds: int = Field(default=600, ge=1, le=3600)


class ReadParams(BaseModel):
    session_id: str
    offset: int = Field(default=0, ge=0)
    wait_seconds: float = Field(default=0, ge=0, le=30)


class InputParams(BaseModel):
    session_id: str
    text: str = ""
    stop: bool = False


class TerminalStart(Tool):
    name = "TerminalStart"
    description = "Start an authorized shell command asynchronously in this workspace. Returns a session ID for TerminalRead/Input. Pipe-based, not a PTY; expires after lifetime_seconds or runtime shutdown."
    params_model = StartParams
    category = "command"

    def __init__(self, sessions):
        self.sessions = sessions

    async def execute(self, params):
        try:
            return ToolResult(json.dumps({"session_id": await self.sessions.start(params.command, params.lifetime_seconds)}))
        except (ValueError, OSError) as exc:
            return ToolResult(str(exc), is_error=True)


class TerminalRead(Tool):
    name = "TerminalRead"
    description = "Read bounded terminal output from an absolute character offset; next_offset supports incremental reads. A timed wait does not stop the process."
    params_model = ReadParams
    category = "read"
    consumes_runtime_slot = False

    def __init__(self, sessions):
        self.sessions = sessions

    async def execute(self, params):
        try:
            session = self.sessions.get(params.session_id)
            if params.wait_seconds and not session.reader.done():
                await asyncio.wait({session.reader}, timeout=params.wait_seconds)
            start = max(params.offset, session.offset)
            output = session.output[max(0, start - session.offset):][:20_000]
            return ToolResult(json.dumps({"output": output, "next_offset": start + len(output),
                "truncated": params.offset < session.offset, "exit_code": session.process.returncode,
                "timed_out": session.timed_out, "running": not session.reader.done()}, ensure_ascii=False))
        except ValueError as exc:
            return ToolResult(str(exc), is_error=True)


class TerminalInput(Tool):
    name = "TerminalInput"
    description = "Send exact text (include newline when needed) to an owned process stdin, or stop its process tree. Input can execute commands and requires the same authorization as starting them."
    params_model = InputParams
    # stdin is interpreted by the existing program, not necessarily by a shell.
    # Do not classify arbitrary input as a harmless standalone shell command.
    category = "harness"

    def __init__(self, sessions):
        self.sessions = sessions

    async def execute(self, params):
        try:
            session = self.sessions.get(params.session_id)
            if params.stop:
                await _terminate_process_tree(session.process)
            else:
                if session.process.returncode is not None:
                    raise ValueError("Process already exited.")
                session.process.stdin.write(params.text.encode("utf-8"))
                await asyncio.wait_for(session.process.stdin.drain(), timeout=5)
            return ToolResult("Stop requested." if params.stop else "Input sent.")
        except (ValueError, OSError, asyncio.TimeoutError) as exc:
            return ToolResult(str(exc), is_error=True)
