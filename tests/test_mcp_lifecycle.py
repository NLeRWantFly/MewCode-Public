from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from mewcode.config import MCPServerConfig
from mewcode.mcp.client import MCPClient


class _RecordingStack(AbstractAsyncContextManager["_RecordingStack"]):
    def __init__(self, opened_by: asyncio.Task[Any]) -> None:
        self.opened_by = opened_by
        self.closed_by: asyncio.Task[Any] | None = None
        self.closed = asyncio.Event()

    async def __aexit__(self, *args: object) -> None:
        self.closed_by = asyncio.current_task()
        self.closed.set()

    async def aclose(self) -> None:
        await self.__aexit__(None, None, None)


class _Session:
    def __init__(self, *, block_calls: bool = False) -> None:
        self.block_calls = block_calls
        self.called_by: asyncio.Task[Any] | None = None
        self.call_started = asyncio.Event()

    async def list_tools(self) -> Any:
        return SimpleNamespace(tools=[])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        self.called_by = asyncio.current_task()
        self.call_started.set()
        if self.block_calls:
            await asyncio.Event().wait()
        return f"{name}:{arguments['value']}"


@pytest.mark.asyncio
async def test_connection_calls_and_cleanup_stay_on_owner_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MCPClient(MCPServerConfig(name="test", command="ignored"))
    session = _Session()
    opened_by: asyncio.Task[Any] | None = None
    stack: _RecordingStack | None = None

    async def open_connection() -> tuple[_RecordingStack, Any]:
        nonlocal opened_by, stack
        opened_by = asyncio.current_task()
        assert opened_by is not None
        stack = _RecordingStack(opened_by)
        return stack, session

    monkeypatch.setattr(client, "_open_connection", open_connection)

    await client.connect()
    result = await asyncio.create_task(
        client.call_tool("demo", {"value": "ok"})
    )
    await asyncio.create_task(client.close())

    assert result == "demo:ok"
    assert stack is not None
    assert opened_by is session.called_by is stack.closed_by
    assert not client.is_alive


@pytest.mark.asyncio
async def test_timed_out_call_stops_and_cleans_owner_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MCPClient(
        MCPServerConfig(name="slow", command="ignored"),
        call_timeout=0.02,
        close_timeout=1,
    )
    session = _Session(block_calls=True)
    stack: _RecordingStack | None = None

    async def open_connection() -> tuple[_RecordingStack, Any]:
        nonlocal stack
        owner = asyncio.current_task()
        assert owner is not None
        stack = _RecordingStack(owner)
        return stack, session

    monkeypatch.setattr(client, "_open_connection", open_connection)

    with pytest.raises(TimeoutError, match="tool call timed out after"):
        await client.call_tool("hang", {"value": "never"})

    assert stack is not None
    assert stack.closed.is_set()
    assert stack.opened_by is stack.closed_by
    assert not client.is_alive


@pytest.mark.asyncio
async def test_protocol_failure_drops_connection_before_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MCPClient(MCPServerConfig(name="retry", command="ignored"))
    stacks: list[_RecordingStack] = []
    sessions = [_Session(), _Session()]

    async def first_failure(name: str, arguments: dict[str, Any]) -> str:
        raise RuntimeError("Connection closed")

    sessions[0].call_tool = first_failure  # type: ignore[method-assign]

    async def open_connection() -> tuple[_RecordingStack, Any]:
        owner = asyncio.current_task()
        assert owner is not None
        stack = _RecordingStack(owner)
        stacks.append(stack)
        return stack, sessions[len(stacks) - 1]

    monkeypatch.setattr(client, "_open_connection", open_connection)

    with pytest.raises(RuntimeError, match="Connection closed"):
        await client.call_tool("first", {"value": "x"})
    assert stacks[0].closed.is_set()
    assert not client.is_alive

    assert await client.call_tool("second", {"value": "ok"}) == "second:ok"
    assert len(stacks) == 2
    await client.close()
    assert stacks[1].closed.is_set()


@pytest.mark.asyncio
async def test_real_stdio_transport_can_close_from_a_different_caller_task(
    tmp_path: Path,
) -> None:
    server = tmp_path / "stdio_server.py"
    server.write_text(
        """
import asyncio
from mcp.server.fastmcp import FastMCP

server = FastMCP("lifecycle-test", log_level="WARNING")

@server.tool()
def ping(value: str) -> str:
    return "pong:" + value

if __name__ == "__main__":
    asyncio.run(server.run_stdio_async())
""".strip(),
        encoding="utf-8",
    )
    client = MCPClient(
        MCPServerConfig(
            name="real-stdio",
            command=sys.executable,
            args=[str(server)],
        ),
        connect_timeout=10,
        call_timeout=10,
        close_timeout=5,
    )

    await asyncio.create_task(client.connect())
    tools = await asyncio.create_task(client.list_tools())
    assert [tool.name for tool in tools] == ["ping"]
    result = await asyncio.create_task(client.call_tool("ping", {"value": "ok"}))
    assert result.content[0].text == "pong:ok"
    await asyncio.create_task(client.close())

    assert not client.is_alive
    assert client._owner_task is None
