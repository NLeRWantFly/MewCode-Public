from __future__ import annotations

from typing import Any

from mcp import types as mcp_types
from pydantic import BaseModel, create_model

from mewcode.mcp.client import MCPClient
from mewcode.tools.base import Tool, ToolResult


def _build_params_model(
    tool_name: str, input_schema: dict[str, Any]
) -> type[BaseModel]:
    properties = input_schema.get("properties", {})
    required = set(input_schema.get("required", []))

    field_definitions: dict[str, Any] = {}
    for name, prop in properties.items():
        py_type = _json_type_to_python(prop.get("type", "string"))
        if name in required:
            field_definitions[name] = (py_type, ...)
        else:
            field_definitions[name] = (py_type | None, None)

    return create_model(f"{tool_name}Params", **field_definitions)


def _json_type_to_python(json_type: str) -> type:
    mapping: dict[str, type] = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "object": dict,
        "array": list,
    }
    return mapping.get(json_type, str)


def _extract_text(content: list[Any]) -> str:
    parts: list[str] = []
    for block in content:
        if isinstance(block, mcp_types.TextContent):
            parts.append(block.text)
        elif isinstance(block, mcp_types.ImageContent):
            parts.append(f"[image: {block.mimeType}]")
        elif isinstance(block, mcp_types.EmbeddedResource):
            resource = block.resource
            if hasattr(resource, "text"):
                parts.append(resource.text)
            else:
                parts.append(f"[binary resource: {resource.uri}]")
    return "\n".join(parts) if parts else "(no output)"

def _extract_content(content: list[Any]) -> tuple[str, list[dict[str, Any]]]:
    text_parts: list[str] = []
    blocks: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, mcp_types.TextContent):
            text_parts.append(block.text)
        elif isinstance(block, mcp_types.ImageContent):
            blocks.append({
                "type": "image", "mime_type": block.mimeType,
                "data": block.data, "name": "MCP screenshot",
            })
        elif isinstance(block, mcp_types.EmbeddedResource):
            resource = block.resource
            if hasattr(resource, "text"):
                text_parts.append(resource.text)
            else:
                text_parts.append(f"[binary resource: {resource.uri}]")
    text = "\n".join(text_parts)
    return (text or ("(image output)" if blocks else "(no output)")), blocks



class MCPToolWrapper(Tool):
    def __init__(
        self,
        server_name: str,
        tool_def: mcp_types.Tool,
        client: MCPClient,
        *,
        canonical_names: bool = False,
    ) -> None:
        self._server_name = server_name
        self._tool_def = tool_def
        self._client = client
        self.canonical_names = canonical_names
        if canonical_names:
            self.name = tool_def.name
            self.should_defer = False
        else:
            self.name = f"mcp_{server_name}_{tool_def.name}"
            self.should_defer = True
        self.description = tool_def.description or tool_def.name
        self.category = "command"
        self.is_concurrency_safe = False
        self.params_model = _build_params_model(
            tool_def.name, tool_def.inputSchema
        )
        identity = f"{server_name} {tool_def.name}".lower()
        if any(word in identity for word in ("computer", "desktop", "mouse", "keyboard")):
            self.interaction_kind = "computer_use"
            self.risk_level = "highest"
        elif any(word in identity for word in ("browser", "playwright", "chromium")):
            self.interaction_kind = "browser"
            self.risk_level = "high"
        else:
            self.interaction_kind = ""
            self.risk_level = "normal"

    @property
    def mcp_tool_name(self) -> str:
        return self._tool_def.name


    def get_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self._tool_def.inputSchema,
        }


    async def run(self, params: BaseModel, *, work_dir: str | None = None) -> ToolResult:
        if self.interaction_kind == "browser":
            from mewcode.decisions.browser import browser_transaction
            # Acquire the resource lock BEFORE a leaf scheduler permit. Otherwise
            # waiters can occupy every permit while the loop owning the browser
            # waits for one of those permits to observe/act: a lock inversion.
            async with browser_transaction(self._client):
                return await super().run(params, work_dir=work_dir)
        return await super().run(params, work_dir=work_dir)

    async def execute(self, params: BaseModel) -> ToolResult:
        if self.interaction_kind == "browser":
            from mewcode.decisions.browser import browser_transaction
            async with browser_transaction(self._client):
                return await self._execute_connected(params)
        return await self._execute_connected(params)

    async def _execute_connected(self, params: BaseModel) -> ToolResult:
        if not self._client.is_alive:
            try:
                await self._client.connect()
            except Exception as e:
                return ToolResult(
                    output=f"MCP server '{self._server_name}' reconnect failed: {e}",
                    is_error=True,
                )

        try:
            result = await self._client.call_tool(
                self._tool_def.name, params.model_dump(exclude_none=True)
            )
        except Exception as e:
            return ToolResult(
                output=f"MCP tool call failed: {e}",
                is_error=True,
            )

        text, blocks = _extract_content(result.content)
        return ToolResult(
            output=text, is_error=bool(result.isError),
            content_blocks=blocks,
        )
