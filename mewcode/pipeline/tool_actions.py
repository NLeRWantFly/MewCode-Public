"""Adapters that expose the live Agent tool registry as Pipeline Actions.

Creative-mode graphs store only an action id and JSON parameters.  This module
keeps the bridge to the normal :class:`~mewcode.tools.base.Tool` contract in
one place so a saved graph never receives a direct reference to a mutable tool
instance.  The registry and permission checker are resolved again when an
action is invoked; switching providers or toggling a tool therefore takes
effect without restarting the Pipeline API.
"""
from __future__ import annotations

import copy
import hashlib
import inspect
import logging
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from pydantic import BaseModel, ValidationError

from mewcode.pipeline.actions import PipelineAction, PipelineActionRequest
from mewcode.tools.base import ToolResult


TOOL_ACTION_PREFIX = "tool."
ALLOW_UNATTENDED_FIELD = "allow_unattended"
_SAFE_SLUG = re.compile(r"[^a-z0-9]+")
_INTERACTIVE_KINDS = frozenset({
    "browser",
    "computer",
    "computer_use",
    "human",
    "human_oncall",
    "interactive",
})
_SENSITIVE_KEY = re.compile(
    r"(?:^|[_-])(api[_-]?key|authorization|cookie|credential|password|secret|token)"
    r"(?:$|[_-])",
    re.IGNORECASE,
)
_MAX_EVENT_OUTPUT = 2_000
_MAX_EVENT_VALUE = 2_000
log = logging.getLogger(__name__)


class PipelineToolActionError(RuntimeError):
    """A tool Action could not be validated, authorized, or executed."""


def tool_side_effects(tool: Any) -> str:
    """Map the Agent tool category to the Pipeline side-effect contract."""

    category = str(getattr(tool, "category", "") or "").strip().lower()
    if category == "read":
        return "none"
    if category == "write":
        return "workspace"
    # Commands, harnesses, and unknown third-party categories are deliberately
    # conservative: a creative graph must opt in before scheduling them.
    return "external"


def is_interactive_tool(tool: Any) -> bool:
    """Return whether a tool needs a live user/device interaction."""

    kind = str(getattr(tool, "interaction_kind", "") or "").strip().lower()
    if not kind or kind in {"none", "noninteractive", "non_interactive"}:
        return False
    if kind in _INTERACTIVE_KINDS:
        return True
    # Plugin-defined interaction labels should fail closed rather than become
    # a schedulable tool merely because the label is new.
    return True


def _slug_tool_name(tool_name: str) -> str:
    """Create an action-id-safe slug without making identity case-sensitive."""

    slug = _SAFE_SLUG.sub("-", str(tool_name).strip().casefold()).strip("-")
    return slug or "unnamed"


def _short_digest(tool_name: str) -> str:
    return hashlib.sha256(str(tool_name).encode("utf-8")).hexdigest()[:10]


def stable_tool_action_id(
    tool_name: str,
    *,
    reserved_ids: Iterable[str] = (),
) -> str:
    """Return a stable, valid action id for one tool name.

    Punctuation and case are normalized for readable ids, while a digest of
    the original tool name keeps identity stable if a colliding tool is added
    later.  Reserved-id collisions use a deterministic numeric suffix rather
    than registry iteration order.
    """

    # Always include the original-name digest.  Adding a second tool whose
    # punctuation/case normalizes to the same slug must never rename an action
    # that is already stored in a graph.
    suffix = f"-{_short_digest(tool_name)}"
    room = max(1, 128 - len(suffix))
    base = f"{TOOL_ACTION_PREFIX}{_slug_tool_name(tool_name)}"
    base = f"{base[:room]}{suffix}"
    reserved = {str(item) for item in reserved_ids}
    if base not in reserved:
        return base

    # An installed plugin can deliberately reserve even the hashed tool id.
    # Keep the fallback deterministic and fail after a bounded search.
    for index in range(2, 100):
        extra = f"-{index}"
        candidate = f"{base[:128 - len(extra)]}{extra}"
        if candidate not in reserved:
            return candidate
    raise ValueError(f"could not allocate a stable action id for tool {tool_name!r}")


def stable_tool_action_ids(
    tool_names: Iterable[str],
    *,
    reserved_ids: Iterable[str] = (),
) -> dict[str, str]:
    """Allocate deterministic unique ids for a set of tool names."""

    names = sorted({str(name) for name in tool_names}, key=lambda item: (item.casefold(), item))
    reserved = {str(item) for item in reserved_ids}
    result: dict[str, str] = {}
    for name in names:
        action_id = stable_tool_action_id(name, reserved_ids=reserved)
        result[name] = action_id
        reserved.add(action_id)
    return result


def _schema_for_tool(tool: Any) -> dict[str, Any]:
    """Read a defensive JSON-schema copy from a Tool-like object."""

    getter = getattr(tool, "get_schema", None)
    schema: Any = getter().get("input_schema", {}) if callable(getter) else {}
    if not isinstance(schema, Mapping):
        schema = {}
    value = copy.deepcopy(dict(schema))
    if value.get("type") != "object":
        value["type"] = "object"
    properties = value.get("properties")
    if not isinstance(properties, Mapping):
        value["properties"] = {}
    else:
        value["properties"] = dict(properties)
    return value


def _with_unattended_schema(schema: Mapping[str, Any], *, side_effects: str) -> dict[str, Any]:
    value = copy.deepcopy(dict(schema))
    if side_effects == "none":
        return value
    properties = value.setdefault("properties", {})
    if not isinstance(properties, dict):
        properties = {}
        value["properties"] = properties
    properties.setdefault(
        ALLOW_UNATTENDED_FIELD,
        {
            "type": "boolean",
            "title": "允许无人值守运行",
            "description": "允许定时触发执行有副作用的工具；默认关闭。",
            "default": False,
        },
    )
    return value


def _redact_event_value(value: Any, *, key: str = "") -> Any:
    """Keep telemetry useful while avoiding obvious secret-shaped fields."""

    if _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(k): _redact_event_value(v, key=str(k))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_event_value(item, key=key) for item in value]
    if isinstance(value, str):
        return value[:_MAX_EVENT_VALUE]
    return value


def _event_content_blocks(value: Any) -> list[dict[str, Any]]:
    """Return bounded content-block metadata for tool_result telemetry."""

    if not isinstance(value, (list, tuple)):
        return []
    blocks: list[dict[str, Any]] = []
    for block in value:
        if not isinstance(block, Mapping):
            continue
        # Do not stream image/base64 payloads or arbitrary plugin fields into
        # the event log.  The complete blocks remain in the node output.
        safe: dict[str, Any] = {}
        for key in ("type", "mime_type", "mimeType", "name", "text"):
            if key in block:
                safe[key] = _redact_event_value(block[key], key=key)
        blocks.append(safe)
    return blocks


def _work_dir_value(provider: Callable[[], str | None] | str | None) -> str | None:
    if callable(provider):
        return provider()
    return provider


class PipelineToolAction:
    """A live-registry-backed Pipeline Action for one Agent tool name."""

    def __init__(
        self,
        registry: Any,
        tool_name: str,
        *,
        action_id: str | None = None,
        permission_checker: Any = None,
        permission_checker_provider: Callable[[], Any] | None = None,
        work_dir: Callable[[], str | None] | str | None = None,
    ) -> None:
        self.registry = registry
        self.tool_name = str(tool_name)
        self.permission_checker = permission_checker
        self.permission_checker_provider = permission_checker_provider
        self.work_dir = work_dir
        current = registry.get(self.tool_name) if registry is not None else None
        if current is None:
            raise ValueError(f"tool is not registered: {self.tool_name}")
        self.action_id = action_id or stable_tool_action_id(self.tool_name)
        self.side_effects = tool_side_effects(current)
        schema = _with_unattended_schema(
            _schema_for_tool(current), side_effects=self.side_effects
        )
        self._input_schema = schema

    @property
    def tool(self) -> Any | None:
        return self.registry.get(self.tool_name) if self.registry is not None else None

    def action(self) -> PipelineAction:
        tool = self.tool
        title = str(getattr(tool, "name", self.tool_name) or self.tool_name)
        description = str(getattr(tool, "description", "") or "")
        return PipelineAction(
            id=self.action_id,
            title=title,
            description=description,
            category="工具",
            layer="tool",
            icon="wrench",
            config_schema=copy.deepcopy(self._input_schema),
            input_schema=copy.deepcopy(self._input_schema),
            permissions=("tools.use",),
            side_effects=self.side_effects,
            execute=self.execute,
        )

    def _checker(self) -> Any:
        if self.permission_checker_provider is not None:
            return self.permission_checker_provider()
        return self.permission_checker

    async def execute(self, request: PipelineActionRequest) -> dict[str, Any]:
        """Resolve, validate, authorize and invoke the currently live Tool."""

        tool_id = f"{request.run_id}:{request.node_id}:{self.tool_name}"
        raw_params = dict(request.params or {})
        raw_params.pop(ALLOW_UNATTENDED_FIELD, None)

        def emit_use() -> None:
            if request.emit:
                request.emit({
                    "type": "tool_use",
                    "tool_id": tool_id,
                    "tool": self.tool_name,
                    "args": _redact_event_value(raw_params),
                })

        def emit_result(
            output: str,
            *,
            is_error: bool,
            content_blocks: Any = (),
        ) -> None:
            if request.emit:
                request.emit({
                    "type": "tool_result",
                    "tool_id": tool_id,
                    "tool": self.tool_name,
                    "output": str(output or "")[:_MAX_EVENT_OUTPUT],
                    "is_error": bool(is_error),
                    "content_blocks": _event_content_blocks(content_blocks),
                })

        emit_use()
        tool = self.tool
        if tool is None:
            message = f"Tool '{self.tool_name}' is no longer registered"
            emit_result(message, is_error=True)
            raise PipelineToolActionError(message)
        if not self.registry.is_enabled(self.tool_name):
            message = f"Tool '{self.tool_name}' is disabled in current mode"
            emit_result(message, is_error=True)
            raise PipelineToolActionError(message)

        raw_input = dict(request.params or {})
        allow_unattended = bool(raw_input.pop(ALLOW_UNATTENDED_FIELD, False))
        side_effects = tool_side_effects(tool)
        if request.unattended and side_effects == "external" and not allow_unattended:
            message = (
                f"External tool '{self.tool_name}' requires "
                "allow_unattended=true for scheduled runs"
            )
            emit_result(message, is_error=True)
            raise PipelineToolActionError(message)

        params_model = getattr(tool, "params_model", None)
        if not isinstance(params_model, type) or not issubclass(params_model, BaseModel):
            message = f"Tool '{self.tool_name}' has no valid Pydantic params_model"
            emit_result(message, is_error=True)
            raise PipelineToolActionError(message)
        try:
            params = params_model.model_validate(raw_input)
        except ValidationError as exc:
            message = f"Parameter validation error for '{self.tool_name}': {exc}"
            emit_result(message, is_error=True)
            raise PipelineToolActionError(message) from exc
        arguments = params.model_dump(mode="python")

        checker = self._checker()
        if checker is not None:
            decision = checker.check(tool, arguments)
            effect = str(getattr(decision, "effect", "allow") or "allow")
            if effect == "deny":
                reason = str(getattr(decision, "reason", "permission denied"))
                message = f"Permission denied: {reason}"
                emit_result(message, is_error=True)
                raise PipelineToolActionError(message)
            # A manually started node plus its explicit Run action is the user
            # approval for ask/review decisions.  Scheduled runs may only use
            # that path after the side-effect opt-in above.
            if request.unattended and effect in {"ask", "review"} and side_effects != "none":
                if not allow_unattended:
                    message = (
                        f"Unattended permission approval required for '{self.tool_name}'; "
                        "set allow_unattended=true"
                    )
                    emit_result(message, is_error=True)
                    raise PipelineToolActionError(message)

        try:
            result = tool.run(
                params,
                work_dir=_work_dir_value(self.work_dir),
            )
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            result = ToolResult(
                output=f"Tool execution error: {exc}",
                is_error=True,
            )

        if not isinstance(result, ToolResult):
            result = ToolResult(output=str(result))
        output = str(result.output or "")
        content_blocks = [
            copy.deepcopy(block)
            for block in (result.content_blocks or [])
            if isinstance(block, Mapping)
        ]
        emit_result(output, is_error=bool(result.is_error), content_blocks=content_blocks)
        if result.is_error:
            raise PipelineToolActionError(
                f"Tool '{self.tool_name}' failed: {output or 'unknown error'}"
            )
        return {
            "output": output,
            "content_blocks": content_blocks,
        }


def build_tool_actions(
    registry: Any,
    *,
    reserved_ids: Iterable[str] = (),
    permission_checker: Any = None,
    permission_checker_provider: Callable[[], Any] | None = None,
    work_dir: Callable[[], str | None] | str | None = None,
) -> dict[str, PipelineAction]:
    """Build Actions for all currently enabled, non-interactive registry tools."""

    if registry is None:
        return {}
    tools = [
        tool
        for tool in registry.list_tools()
        if registry.is_enabled(tool.name)
        and not is_interactive_tool(tool)
    ]
    tools.sort(key=lambda item: (str(item.name).casefold(), str(item.name)))
    ids = stable_tool_action_ids(
        (str(tool.name) for tool in tools),
        reserved_ids=reserved_ids,
    )
    actions: dict[str, PipelineAction] = {}
    for tool in tools:
        try:
            adapter = PipelineToolAction(
                registry,
                str(tool.name),
                action_id=ids[str(tool.name)],
                permission_checker=permission_checker,
                permission_checker_provider=permission_checker_provider,
                work_dir=work_dir,
            )
            action = adapter.action()
            actions[action.id] = action
        except (AttributeError, TypeError, ValueError):
            log.exception("Skipping malformed Pipeline tool action: %r", tool)
    return actions


def merge_pipeline_actions(
    slot_actions: Mapping[str, PipelineAction] | None,
    tool_actions: Mapping[str, PipelineAction] | None,
) -> dict[str, PipelineAction]:
    """Merge Cordis/plugin actions with tool actions without silent overwrite."""

    merged: dict[str, PipelineAction] = {}
    if isinstance(slot_actions, Mapping):
        merged.update({str(key): value for key, value in slot_actions.items()})
    if isinstance(tool_actions, Mapping):
        for key, action in tool_actions.items():
            if key not in merged:
                merged[str(key)] = action
    return merged


__all__ = [
    "ALLOW_UNATTENDED_FIELD",
    "PipelineToolAction",
    "PipelineToolActionError",
    "build_tool_actions",
    "is_interactive_tool",
    "merge_pipeline_actions",
    "stable_tool_action_id",
    "stable_tool_action_ids",
    "tool_side_effects",
]
