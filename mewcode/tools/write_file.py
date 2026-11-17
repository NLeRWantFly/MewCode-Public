from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal
import base64

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult, resolve_tool_path

if TYPE_CHECKING:
    from mewcode.cache import FileCache
    from mewcode.tools.file_state_cache import FileStateCache


class Params(BaseModel):
    file_path: str = Field(description="Path to the file to write")
    content: str = Field(description="Content to write to the file")
    encoding: Literal['utf-8', 'base64'] = 'utf-8'
    mode: int | None = Field(default=None, ge=0, le=0o777, description='Optional file mode; omitted preserves current permissions.')


class WriteFile(Tool):
    name = "WriteFile"
    description = (
        "Write content to a file, creating parent directories if needed. Overwrites existing files.\n"
        "You MUST read existing files with ReadFile before overwriting them. This tool will fail otherwise."
    )
    params_model = Params
    category = "write"


    def __init__(self, file_cache: FileCache | None = None, file_history: Any = None, file_state_cache: FileStateCache | None = None) -> None:
        self._cache = file_cache
        self.file_history = file_history
        self._state_cache = file_state_cache


    async def execute(self, params: Params) -> ToolResult:
        path = resolve_tool_path(params.file_path)
        if self.file_history is not None:
            self.file_history.track_edit(str(path))

        if self._state_cache and path.exists():
            resolved = str(path.resolve())
            ok, err_msg = self._state_cache.check(resolved)
            if not ok:
                return ToolResult(output=err_msg, is_error=True, host_observation={
                    'operation_blocked': True, 'phase': 'before_effect',
                    'command_execution': {'status': 'denied', 'command_started': False, 'exit_code': None}})

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if params.encoding == 'base64':
                path.write_bytes(base64.b64decode(params.content, validate=True))
            else:
                path.write_text(params.content, encoding="utf-8")
            if params.mode is not None:
                path.chmod(params.mode)
            if self._cache:
                self._cache.invalidate(str(path.resolve()))
            if self._state_cache:
                self._state_cache.update(str(path.resolve()))
        except Exception as e:
            return ToolResult(output=f"Error writing file: {e}", is_error=True)
        return ToolResult(output=f"Successfully wrote to {params.file_path}")
