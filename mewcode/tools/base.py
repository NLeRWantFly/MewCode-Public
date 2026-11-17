from __future__ import annotations

from abc import ABC, abstractmethod
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from mewcode.runtime_scheduler import (
    GLOBAL_RUNTIME_SCHEDULER,
    current_runtime_scheduler,
)
from mewcode.usage.service import current_usage_scope

SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".pytest_tmp",
    ".ruff_cache",
    ".mewcode",
    ".tmp",
    "build",
    "coverage",
    "dist",
    "gen",
    "output",
    "target",
    "vendor",
}

MAX_OUTPUT_CHARS = 10000
_ACTIVE_WORK_DIR: ContextVar[str | None] = ContextVar(
    "mewcode_active_work_dir", default=None
)

ToolCategory = Literal["read", "write", "command", "harness"]


@dataclass(frozen=True)
class ToolRiskProfile:
    """Machine-readable side-effect declaration used by permission policies."""

    side_effect: Literal[
        "none", "workspace", "external", "destructive", "unknown"
    ] = "none"
    network: bool = False
    cost: bool = False
    reversible: bool = True
    sensitive_data: bool = False
    target_type: str = "local"

    def as_dict(self) -> dict[str, Any]:
        return {
            "side_effect": self.side_effect,
            "network": self.network,
            "cost": self.cost,
            "reversible": self.reversible,
            "sensitive_data": self.sensitive_data,
            "target_type": self.target_type,
        }


@dataclass
class ToolResult:
    output: str
    is_error: bool = False
    content_blocks: list[dict[str, Any]] = field(default_factory=list)
    host_observation: Any = None


def parameter_error_result(tool_name: str, error: Exception) -> ToolResult:
    """Keep schema rejection distinct from execution/permission failures."""
    message = f'Parameter validation error: {error}'
    observation = {'tool_feedback': {'category': 'contract_error', 'code': 'argument_schema_error',
                                    'tool': tool_name, 'next_action': '按实际工具定义纠正参数后重交。'}}
    if tool_name in {'SubmitVerdict', 'SubmitHandoff', 'SubmitLoopDecision', 'SubmitAssistanceReceipt'}:
        observation['submission'] = {'schema_version': 1, 'tool': tool_name, 'status': 'rejected',
            'category': 'contract_error', 'code': 'argument_schema_error', 'waiting_for': 'agent',
            'next_action': message}
    return ToolResult(message, is_error=True, host_observation=observation)


class Tool(ABC):
    name: str
    description: str
    params_model: type[BaseModel]
    category: ToolCategory = "read"
    is_concurrency_safe: bool = False
    is_system_tool: bool = False
    should_defer: bool = False
    # Harness tools can spawn agents/workflows.  They must not hold a leaf
    # permit while waiting for their children.
    consumes_runtime_slot: bool = True
    inline_schema_refs: bool = False

    @property
    def is_read_only(self) -> bool:
        return self.category == "read"

    def permission_risk(self, arguments: dict[str, Any]) -> ToolRiskProfile:
        """Return a conservative default; specialized tools may override it."""
        interaction = str(getattr(self, "interaction_kind", ""))
        if interaction in {"computer_use", "browser"}:
            return ToolRiskProfile(
                side_effect="external",
                network=True,
                reversible=False,
                sensitive_data=True,
                target_type=interaction,
            )
        if self.category == "read":
            return ToolRiskProfile()
        if self.category == "write":
            return ToolRiskProfile(side_effect="workspace")
        if self.category == "harness":
            return ToolRiskProfile(
                side_effect="unknown", reversible=False, target_type="agent_harness"
            )
        return ToolRiskProfile(
            side_effect="unknown", reversible=False, target_type="command"
        )


    def get_schema(self) -> dict[str, Any]:
        schema = self.params_model.model_json_schema()
        schema.pop("title", None)
        if self.inline_schema_refs:
            definitions = schema.pop("$defs", {})

            def inline(value, stack=()):
                if isinstance(value, list):
                    return [inline(item, stack) for item in value]
                if not isinstance(value, dict):
                    return value
                ref = value.get("$ref")
                inherited = {}
                if ref:
                    if not ref.startswith("#/$defs/") or ref in stack:
                        raise ValueError("工具参数包含不可展开的 schema 引用")
                    inherited = inline(definitions[ref.removeprefix("#/$defs/")], (*stack, ref))
                return {**inherited, **{key: inline(item, stack) for key, item in value.items() if key != "$ref"}}

            schema = inline(schema)
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": schema,
        }

    async def run(
        self, params: BaseModel, *, work_dir: str | None = None
    ) -> ToolResult:
        token = _ACTIVE_WORK_DIR.set(work_dir)
        try:
            if self.category == "harness" or not self.consumes_runtime_slot:
                return await self.execute(params)
            scope = current_usage_scope()
            async with current_runtime_scheduler(GLOBAL_RUNTIME_SCHEDULER).slot(
                kind="tool",
                label=self.name,
                session_id=scope.session_id,
                agent_id=scope.agent_id,
            ):
                return await self.execute(params)
        finally:
            _ACTIVE_WORK_DIR.reset(token)

    @abstractmethod
    async def execute(self, params: BaseModel) -> ToolResult: ...


def active_work_dir() -> str | None:
    return _ACTIVE_WORK_DIR.get()


def resolve_tool_path(path: str) -> Path:
    resolved = Path(path).expanduser()
    work_dir = active_work_dir()
    if work_dir and not resolved.is_absolute():
        resolved = Path(work_dir) / resolved
    return resolved


# --- 流式事件 ---


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolCallStart:
    tool_name: str
    tool_id: str


@dataclass
class ToolCallDelta:
    text: str


@dataclass
class ToolCallComplete:
    tool_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass
class ThinkingDelta:
    text: str
    # ``summary`` is Provider-authored, user-visible reasoning summary text.
    # ``private`` covers hidden/internal reasoning streams that must not be
    # rendered verbatim by callers.  The default preserves existing clients.
    kind: str = "private"


@dataclass
class ThinkingComplete:
    thinking: str
    signature: str
    kind: str = "auto"


@dataclass
class StreamStatus:
    """Provider lifecycle signal that carries progress but no model content."""

    stage: str
    detail: str = ""


@dataclass
class StreamEnd:
    stop_reason: str
    input_tokens: int = 0
    output_tokens: int = 0
    # API 返回的 prompt cache 用量。Anthropic 把缓存前缀 token 分为
    # "read"（cache 命中，按 10% 计费）和 "creation"（cache 写入）。
    # input_tokens 已排除这两部分，因此实际 prompt 大小 =
    # input + cache_read + cache_creation。GPT-5.6 还通过
    # *_tokens_details.cache_write_tokens 暴露 cache creation。
    cache_read: int = 0
    cache_creation: int = 0
    # False 表示 provider 没有返回缓存遥测；这与明确返回 0 命中不同。
    cache_usage_available: bool = False
    reasoning_tokens: int = 0
    response_id: str = ""
    request_id: str = ""
    model: str = ""
    service_tier: str = ""
    system_fingerprint: str = ""
    cache_key: str = ""
    cache_mode: str = ""
    cache_breakpoints: int = 0
    provider_stop_reason: str = ""
    provider_effort: str = ""


StreamEvent = (
    TextDelta
    | ThinkingDelta
    | ThinkingComplete
    | StreamStatus
    | ToolCallStart
    | ToolCallDelta
    | ToolCallComplete
    | StreamEnd
)
