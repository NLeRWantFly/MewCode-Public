from __future__ import annotations

import asyncio
import logging
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

import httpx
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from mewcode.config import MCPServerConfig, build_child_env, resolve_env_vars

logger = logging.getLogger(__name__)

MCP_CONNECT_TIMEOUT_SECONDS = 30.0
MCP_CALL_TIMEOUT_SECONDS = 120.0
MCP_CLOSE_TIMEOUT_SECONDS = 5.0


@dataclass(slots=True)
class _OwnerRequest:
    operation: str
    arguments: tuple[Any, ...]
    future: asyncio.Future[Any]


class MCPClient:
    """A task-owned MCP connection with bounded request lifetimes.

    AnyIO's stdio transport opens task-affine cancel scopes. The task that
    enters those scopes must also exit them, so public callers submit work to
    one long-lived owner task instead of touching the session directly.
    """

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        connect_timeout: float = MCP_CONNECT_TIMEOUT_SECONDS,
        call_timeout: float = MCP_CALL_TIMEOUT_SECONDS,
        close_timeout: float = MCP_CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        self.config = config
        self.name = config.name
        self._connect_timeout = connect_timeout
        self._call_timeout = call_timeout
        self._close_timeout = close_timeout
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self._alive = False
        self._owner_task: asyncio.Task[None] | None = None
        self._owner_queue: asyncio.Queue[_OwnerRequest] | None = None
        self._lifecycle_lock = asyncio.Lock()

    @property
    def is_alive(self) -> bool:
        return self._alive

    async def connect(self) -> None:
        if self._alive:
            return
        await self._submit("connect", timeout=self._connect_timeout)

    async def _open_connection(
        self,
    ) -> tuple[AsyncExitStack, ClientSession]:
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            if self.config.is_stdio:
                read, write = await self._connect_stdio(stack)
            else:
                read, write = await self._connect_http(stack)

            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            return stack, session
        except BaseException:
            await self._close_stack(stack)
            raise

    async def _connect_stdio(
        self, stack: AsyncExitStack
    ) -> tuple[Any, Any]:
        assert self.config.command is not None

        params = StdioServerParameters(
            command=self.config.command,
            args=self.config.args,
            env=build_child_env(self.config.env),
        )
        devnull = open(os.devnull, "w")
        stack.callback(devnull.close)
        read, write = await stack.enter_async_context(
            stdio_client(params, errlog=devnull)
        )
        return read, write

    async def _connect_http(
        self, stack: AsyncExitStack
    ) -> tuple[Any, Any]:
        assert self.config.url is not None

        resolved_headers = {
            key: resolve_env_vars(value)
            for key, value in self.config.headers.items()
        }
        http_client = httpx.AsyncClient(
            headers=resolved_headers,
            follow_redirects=True,
        )
        await stack.enter_async_context(http_client)

        result = await stack.enter_async_context(
            streamable_http_client(self.config.url, http_client=http_client)
        )
        read, write = result[0], result[1]
        return read, write

    async def list_tools(self) -> list[types.Tool]:
        result = await self._submit(
            "list_tools", timeout=self._connect_timeout
        )
        return list(result.tools)

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> types.CallToolResult:
        return await self._submit(
            "call_tool", name, arguments, timeout=self._call_timeout
        )

    async def close(self) -> None:
        task = self._owner_task
        if task is None:
            self._alive = False
            self._session = None
            self._stack = None
            return
        await self._stop_owner(task)

    async def _ensure_owner(
        self,
    ) -> tuple[asyncio.Task[None], asyncio.Queue[_OwnerRequest]]:
        async with self._lifecycle_lock:
            task = self._owner_task
            queue = self._owner_queue
            if task is not None and not task.done() and queue is not None:
                return task, queue

            if task is not None and task.done():
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.debug(
                        "MCP owner task for '%s' exited with an error",
                        self.name,
                        exc_info=True,
                    )

            queue = asyncio.Queue()
            task = asyncio.create_task(
                self._run_owner(queue), name=f"mcp-owner:{self.name}"
            )
            self._owner_queue = queue
            self._owner_task = task
            return task, queue

    async def _submit(
        self,
        operation: str,
        *arguments: Any,
        timeout: float,
    ) -> Any:
        task, queue = await self._ensure_owner()
        future = asyncio.get_running_loop().create_future()
        queue.put_nowait(_OwnerRequest(operation, arguments, future))
        try:
            async with asyncio.timeout(timeout):
                return await asyncio.shield(future)
        except TimeoutError as exc:
            future.cancel()
            await self._stop_owner(task)
            label = (
                "connection"
                if operation in {"connect", "list_tools"}
                else "tool call"
            )
            raise TimeoutError(
                f"MCP server '{self.name}' {label} timed out after {timeout:g}s"
            ) from exc
        except asyncio.CancelledError:
            future.cancel()
            await self._stop_owner(task)
            raise

    async def _run_owner(
        self, queue: asyncio.Queue[_OwnerRequest]
    ) -> None:
        stack: AsyncExitStack | None = None
        session: ClientSession | None = None
        current: _OwnerRequest | None = None
        try:
            while True:
                current = await queue.get()
                try:
                    if session is None:
                        stack, session = await self._open_connection()
                        self._stack = stack
                        self._session = session
                        self._alive = True
                        logger.info("MCP server '%s' connected", self.name)

                    if current.operation == "connect":
                        result: Any = None
                    elif current.operation == "list_tools":
                        result = await session.list_tools()
                    elif current.operation == "call_tool":
                        result = await session.call_tool(*current.arguments)
                    else:
                        raise RuntimeError(
                            f"Unknown MCP owner operation: {current.operation}"
                        )
                except asyncio.CancelledError:
                    if not current.future.done():
                        current.future.cancel()
                    raise
                except Exception as exc:
                    stack, session = await self._drop_connection(stack)
                    if not current.future.done():
                        current.future.set_exception(exc)
                else:
                    if not current.future.done():
                        current.future.set_result(result)
                finally:
                    current = None
        except asyncio.CancelledError:
            pass
        finally:
            await self._drop_connection(stack)
            while not queue.empty():
                pending = queue.get_nowait()
                if not pending.future.done():
                    pending.future.cancel()
            task = asyncio.current_task()
            if self._owner_task is task:
                self._owner_task = None
                self._owner_queue = None

    async def _drop_connection(
        self, stack: AsyncExitStack | None
    ) -> tuple[None, None]:
        self._alive = False
        self._session = None
        if stack is not None:
            await self._close_stack(stack)
        if self._stack is stack:
            self._stack = None
        return None, None

    async def _close_stack(self, stack: AsyncExitStack) -> None:
        try:
            async with asyncio.timeout(self._close_timeout):
                await stack.aclose()
        except TimeoutError:
            logger.error(
                "Timed out closing MCP stack for '%s' after %ss",
                self.name,
                self._close_timeout,
            )
        except RuntimeError as exc:
            logger.warning(
                "MCP stack cleanup failed for '%s': %s", self.name, exc
            )
        except Exception:
            logger.debug(
                "Error closing MCP stack for '%s'", self.name, exc_info=True
            )

    async def _stop_owner(self, task: asyncio.Task[None]) -> None:
        async with self._lifecycle_lock:
            if self._owner_task is not task:
                return
            task.cancel()

        try:
            async with asyncio.timeout(self._close_timeout + 1):
                await asyncio.shield(task)
        except asyncio.CancelledError:
            pass
        except TimeoutError:
            logger.error(
                "Timed out stopping MCP owner task for '%s' after %ss",
                self.name,
                self._close_timeout,
            )
        finally:
            async with self._lifecycle_lock:
                if self._owner_task is task:
                    self._owner_task = None
                    self._owner_queue = None
                    self._alive = False
                    self._session = None
                    self._stack = None
