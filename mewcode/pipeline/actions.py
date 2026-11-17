"""Plugin-facing action contract for Pipeline creative-mode nodes.

Plugins register :class:`PipelineAction` instances in the keyed Cordis slot
``core.pipeline.actions``.  The graph stores only a stable ``action_id`` and
JSON configuration, keeping saved pipelines independent from plugin objects.
"""
from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping


_ACTION_ID = re.compile(r"^[a-z][a-z0-9_.-]{2,127}$")
_ACTION_LAYERS = frozenset({"conversation", "tool", "control", "plugin"})
ActionExecutor = Callable[["PipelineActionRequest"], Any | Awaitable[Any]]
RunCleanup = Callable[[str], Any | Awaitable[Any]]


@dataclass(frozen=True)
class PipelineActionRequest:
    """One invocation supplied to a plugin action executor."""

    action_id: str
    run_id: str
    node_id: str
    params: Mapping[str, Any]
    trigger_input: Mapping[str, Any]
    context_outputs: Mapping[str, Any]
    attachments: tuple[Mapping[str, Any], ...] = ()
    model: str | None = None
    effort: str | None = None
    unattended: bool = False
    emit: Callable[[dict[str, Any]], None] | None = None


@dataclass(frozen=True)
class PipelineAction:
    """Serializable action descriptor plus its runtime implementation.

    ``side_effects`` is deliberately explicit so the canvas can distinguish a
    read-only creative step from an external mutation before a run starts.
    ``config_schema`` follows a small JSON-Schema-compatible subset consumed by
    the browser inspector (object properties, strings, booleans and enums).
    """

    id: str
    title: str
    execute: ActionExecutor
    description: str = ""
    category: str = "插件"
    icon: str = "plug"
    config_schema: Mapping[str, Any] = field(default_factory=dict)
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] = field(default_factory=dict)
    permissions: tuple[str, ...] = ()
    side_effects: str = "none"
    resumable: bool = True
    uses_model: bool = False
    cleanup_run: RunCleanup | None = None
    # Kept at the end of the dataclass fields so existing positional action
    # constructors retain their pre-layer argument order.
    layer: str = "plugin"

    def __post_init__(self) -> None:
        if not _ACTION_ID.fullmatch(self.id):
            raise ValueError(
                "pipeline action id must use lowercase letters, digits, '.', '_' or '-'"
            )
        if not self.title.strip():
            raise ValueError("pipeline action title is required")
        if self.layer not in _ACTION_LAYERS:
            raise ValueError(
                "pipeline action layer must be conversation, tool, control or plugin"
            )
        if self.side_effects not in {"none", "workspace", "external"}:
            raise ValueError(
                "pipeline action side_effects must be none, workspace or external"
            )

    async def invoke(self, request: PipelineActionRequest) -> Any:
        value = self.execute(request)
        return await value if inspect.isawaitable(value) else value

    async def close_run(self, run_id: str) -> None:
        if self.cleanup_run is None:
            return
        value = self.cleanup_run(run_id)
        if inspect.isawaitable(value):
            await value

    def capability(self) -> dict[str, Any]:
        """Return the safe, JSON-serializable canvas catalog entry."""

        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "category": self.category,
            "layer": self.layer,
            "icon": self.icon,
            "config_schema": dict(self.config_schema),
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
            "permissions": list(self.permissions),
            "side_effects": self.side_effects,
            "resumable": self.resumable,
            "uses_model": self.uses_model,
        }


def normalize_actions(value: Any) -> dict[str, PipelineAction]:
    """Filter an injected Cordis slot value to well-formed action objects."""

    if not isinstance(value, Mapping):
        return {}
    return {
        key: action
        for key, action in value.items()
        if isinstance(key, str)
        and isinstance(action, PipelineAction)
        and key == action.id
    }


__all__ = [
    "PipelineAction",
    "PipelineActionRequest",
    "normalize_actions",
]
