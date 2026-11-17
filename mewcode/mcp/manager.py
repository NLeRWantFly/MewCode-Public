from __future__ import annotations

import logging
from typing import Any

from mewcode.config import MCPServerConfig
from mewcode.mcp.client import MCPClient
from mewcode.mcp.playwright_browser import (
    format_mcp_connect_error,
    is_blocked_mcp_tool,
    uses_canonical_mcp_names,
)
from mewcode.mcp.tool_wrapper import MCPToolWrapper
from mewcode.plugins.services import TOOL_SLOT, TOOLS
from mewcode.tools import ToolRegistry

logger = logging.getLogger(__name__)


class MCPManager:


    def __init__(self) -> None:
        self._configs: dict[str, MCPServerConfig] = {}
        self._clients: dict[str, MCPClient] = {}
        self._tool_names_by_server: dict[str, list[str]] = {}
        self._slot_ctx: Any = None
        self._slot_disposers: dict[str, list[Any]] = {}


    def attach_context(self, ctx: Any) -> None:
        self._slot_ctx = ctx


    def load_configs(self, configs: list[MCPServerConfig]) -> None:
        for cfg in configs:
            self._configs[cfg.name] = cfg


    def _canonical(self, server_name: str) -> bool:
        return uses_canonical_mcp_names(server_name)


    def _wrap(
        self, server_name: str, tool_def: Any, client: MCPClient
    ) -> MCPToolWrapper:
        return MCPToolWrapper(
            server_name,
            tool_def,
            client,
            canonical_names=self._canonical(server_name),
        )


    async def _open_server(
        self, config: MCPServerConfig
    ) -> tuple[list[MCPToolWrapper], str | None]:
        client: MCPClient | None = None
        try:
            client = MCPClient(config)
            await client.connect()
            tools = await client.list_tools()
            self._clients[config.name] = client
        except Exception as e:
            if client is not None:
                await client.close()
            msg = format_mcp_connect_error(config.name, e)
            logger.warning(msg)
            return [], msg

        wrappers: list[MCPToolWrapper] = []
        for tool_def in tools:
            if is_blocked_mcp_tool(tool_def.name):
                continue
            wrappers.append(self._wrap(config.name, tool_def, client))
        return wrappers, None


    def _unpublish(self, server_name: str, registry: ToolRegistry | None) -> None:
        for disposer in self._slot_disposers.pop(server_name, []):
            try:
                disposer.dispose()
            except Exception:
                logger.debug(
                    "Error disposing MCP slot for '%s'", server_name, exc_info=True
                )
        names = self._tool_names_by_server.pop(server_name, [])
        if self._slot_ctx is None and registry is not None:
            for name in names:
                registry.unregister(name)


    def _publish(
        self,
        server_name: str,
        wrappers: list[MCPToolWrapper],
        registry: ToolRegistry | None,
    ) -> None:
        self._unpublish(server_name, registry)
        names: list[str] = []
        disposers: list[Any] = []
        if self._slot_ctx is not None:
            for wrapper in wrappers:
                names.append(wrapper.name)
                disposers.append(
                    self._slot_ctx.slots.register(
                        TOOL_SLOT, wrapper, item_key=wrapper.name
                    )
                )
        elif registry is not None:
            for wrapper in wrappers:
                names.append(wrapper.name)
                registry.register(wrapper)
        self._tool_names_by_server[server_name] = names
        self._slot_disposers[server_name] = disposers


    async def register_all_tools(self, registry: ToolRegistry) -> list[str]:
        errors: list[str] = []
        for name, config in self._configs.items():
            wrappers, error = await self._open_server(config)
            if error:
                errors.append(error)
                continue
            self._publish(name, wrappers, registry)
            for wrapper in wrappers:
                logger.info("Registered MCP tool: %s", wrapper.name)
        return errors


    async def discover_tools(self) -> tuple[list[MCPToolWrapper], list[str]]:
        wrappers: list[MCPToolWrapper] = []
        errors: list[str] = []
        for name, config in self._configs.items():
            server_wrappers, error = await self._open_server(config)
            if error:
                errors.append(error)
                continue
            if self._slot_ctx is not None:
                registry = self._slot_ctx.get(TOOLS, None)
                self._publish(name, server_wrappers, registry)
            wrappers.extend(server_wrappers)
        return wrappers, errors


    async def replace_server(
        self, config: MCPServerConfig, registry: ToolRegistry
    ) -> list[str]:
        name = config.name
        client: MCPClient | None = None
        try:
            client = MCPClient(config)
            await client.connect()
            tools = await client.list_tools()
        except Exception as e:
            if client is not None:
                await client.close()
            return [format_mcp_connect_error(name, e)]

        old = self._clients.pop(name, None)
        if old is not None:
            try:
                await old.close()
            except Exception:
                logger.debug("Error closing MCP server '%s'", name, exc_info=True)

        self._configs[name] = config
        self._clients[name] = client
        wrappers = [
            self._wrap(name, tool_def, client)
            for tool_def in tools
            if not is_blocked_mcp_tool(tool_def.name)
        ]
        self._publish(name, wrappers, registry)
        return []


    async def get_client(self, name: str) -> MCPClient | None:
        client = self._clients.get(name)
        if client is None:
            config = self._configs.get(name)
            if config is None:
                return None
            client = MCPClient(config)
            await client.connect()
            self._clients[name] = client
            return client

        if not client.is_alive:
            logger.info("Reconnecting MCP server '%s'", name)
            await client.close()
            client = MCPClient(self._configs[name])
            await client.connect()
            self._clients[name] = client

        return client


    async def shutdown(self) -> None:
        for name in list(self._tool_names_by_server):
            self._unpublish(name, None)
        for name, client in self._clients.items():
            try:
                await client.close()
                logger.info("MCP server '%s' closed", name)
            except Exception:
                logger.debug("Error closing MCP server '%s'", name, exc_info=True)
        self._clients.clear()
