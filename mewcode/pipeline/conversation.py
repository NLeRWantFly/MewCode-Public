"""Run-scoped, stateful conversation action for creative pipelines."""
from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from mewcode.pipeline.actions import PipelineAction, PipelineActionRequest


ConversationCreate = Callable[[PipelineActionRequest], Any | Awaitable[Any]]
ConversationTurn = Callable[
    [Any, str, PipelineActionRequest],
    tuple[str, dict | None] | Awaitable[tuple[str, dict | None]],
]
ConversationClose = Callable[[Any], Any | Awaitable[Any]]


@dataclass
class _ConversationState:
    backend: Any
    turn: int = 0


class PipelineConversationAction:
    """Own persistent Agent conversations for the lifetime of one Pipeline run.

    The first node uses ``operation=start`` and returns a ``conversation_id``.
    Later nodes interpolate that ID and use ``operation=continue``.  The
    backend Agent and its ConversationManager stay alive between those nodes,
    preserving real message history and provider cache continuity.
    """

    ACTION_ID = "conversation.chat"

    def __init__(
        self,
        *,
        create: ConversationCreate,
        send: ConversationTurn,
        close: ConversationClose,
    ) -> None:
        self._create = create
        self._send = send
        self._close = close
        self._states: dict[tuple[str, str], _ConversationState] = {}

    def action(self) -> PipelineAction:
        return PipelineAction(
            id=self.ACTION_ID,
            title="发起或继续对话",
            description="创建一条可在后续节点继续的真实多轮 Agent 对话",
            category="对话",
            layer="conversation",
            icon="message-circle",
            config_schema={
                "type": "object",
                "required": ["operation", "prompt"],
                "properties": {
                    "operation": {
                        "type": "string",
                        "title": "操作",
                        "enum": ["start", "continue"],
                        "enumNames": ["新建对话", "继续对话"],
                        "default": "start",
                    },
                    "conversation_id": {
                        "type": "string",
                        "title": "对话 ID",
                        "description": "继续对话时填写上游输出，例如 {{ research.conversation_id }}",
                    },
                    "prompt": {
                        "type": "string",
                        "title": "消息",
                        "format": "textarea",
                    },
                    "tools": {
                        "type": "array",
                        "title": "允许工具",
                        "items": {"type": "string"},
                    },
                    "mode": {
                        "type": "string",
                        "title": "执行模式",
                        "enum": ["react", "plan", "go"],
                        "default": "react",
                    },
                    "boundary": {
                        "type": "string",
                        "title": "操作边界",
                        "description": "使用 ComputerUse 时必须选择 go 并明确边界",
                        "format": "textarea",
                    },
                },
            },
            output_schema={
                "conversation_id": "string",
                "message": "string",
                "turn": "integer",
            },
            permissions=("agent.run", "tools.use"),
            side_effects="workspace",
            resumable=False,
            uses_model=True,
            execute=self.execute,
            cleanup_run=self.close_run,
        )

    async def execute(self, request: PipelineActionRequest) -> tuple[dict[str, Any], dict | None]:
        operation = str(request.params.get("operation") or "start").strip().lower()
        if operation not in {"start", "continue"}:
            raise ValueError("conversation operation must be start or continue")
        prompt = str(request.params.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("conversation prompt is required")

        conversation_id = str(request.params.get("conversation_id") or "").strip()
        if operation == "start":
            conversation_id = conversation_id or f"conv_{uuid.uuid4().hex[:12]}"
            key = (request.run_id, conversation_id)
            if key in self._states:
                raise ValueError(f"conversation '{conversation_id}' already exists in this run")
            value = self._create(request)
            backend = await value if inspect.isawaitable(value) else value
            self._states[key] = _ConversationState(backend=backend)
            if request.emit:
                request.emit({
                    "type": "conversation_started",
                    "conversation_id": conversation_id,
                })
        else:
            if not conversation_id:
                raise ValueError("continue conversation requires conversation_id")
            key = (request.run_id, conversation_id)
            if key not in self._states:
                raise ValueError(
                    f"conversation '{conversation_id}' is not active in run '{request.run_id}'"
                )

        state = self._states[(request.run_id, conversation_id)]
        result = self._send(state.backend, prompt, request)
        message, usage = await result if inspect.isawaitable(result) else result
        state.turn += 1
        if request.emit:
            request.emit({
                "type": "conversation_turn_completed",
                "conversation_id": conversation_id,
                "turn": state.turn,
            })
        return {
            "conversation_id": conversation_id,
            "message": str(message),
            "turn": state.turn,
        }, usage

    async def close_run(self, run_id: str) -> None:
        keys = [key for key in self._states if key[0] == run_id]
        for key in keys:
            state = self._states.pop(key)
            value = self._close(state.backend)
            if inspect.isawaitable(value):
                await value


__all__ = ["PipelineConversationAction"]
