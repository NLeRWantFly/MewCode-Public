"""Exact-command guard for parent-owned read-only capability assistance."""
from __future__ import annotations

from typing import Any


def normalize_command(value: str) -> str:
    """Normalize only insignificant outer whitespace; never rewrite a command."""
    return str(value or "").strip()


def wrap_allowlisted_command(tool: Any, commands: tuple[str, ...], *, receipt_context=None) -> Any:
    """Allow Bash to execute only commands declared in the structured handoff.

    The ordinary permission checker still reviews every allowed command.  This
    wrapper is an additional scope boundary, not a replacement for approval or
    dangerous-command detection.
    """
    from mewcode.tools.base import Tool, ToolResult
    from mewcode.experts.command_receipts import execution_observation
    from mewcode.kernel.events import digest
    from dataclasses import asdict
    import copy

    allowed = frozenset(normalize_command(command) for command in commands if normalize_command(command))
    if not isinstance(tool, Tool) or tool.name != "Bash" or not allowed:
        return tool
    observation = None
    if receipt_context is not None:
        observation = {"version": 1, "dispatch_id": receipt_context.get("dispatch_id"),
            "guarded": True, "allowed_commands": sorted(allowed), "attempted_commands": [], "closed": False}
        receipt_context["dispatch_observation"] = observation

    class AllowlistedCommandGuard(Tool):
        name = tool.name
        description = (
            tool.description
            + " This delegated probe is additionally restricted to the exact commands declared by the parent handoff."
        )
        params_model = tool.params_model
        category = tool.category
        is_concurrency_safe = tool.is_concurrency_safe
        is_system_tool = getattr(tool, "is_system_tool", False)
        should_defer = getattr(tool, "should_defer", False)
        consumes_runtime_slot = getattr(tool, "consumes_runtime_slot", True)

        def __init__(self) -> None:
            self._inner = tool
            self._attempted: set[str] = set()
            self._results: dict[str, ToolResult] = {}

        def permission_risk(self, arguments: dict[str, Any]):
            return self._inner.permission_risk(arguments)

        async def execute(self, params: Any) -> ToolResult:
            command = normalize_command(getattr(params, "command", ""))
            if command not in allowed:
                return ToolResult(
                    output="命令不在父级结构化转派的精确 allowlist 中；未执行。",
                    is_error=True,
                )
            if command in self._results:
                cached = copy.deepcopy(self._results[command])
                prior = (cached.host_observation.get('command_execution')
                    if isinstance(cached.host_observation, dict) else None)
                prior = prior if isinstance(prior, dict) else {}
                source = {'dispatch_id':observation.get('dispatch_id') if observation else None,
                    'command_sha256':digest(command), 'result_sha256':digest(asdict(cached)),
                    'command_execution':{key:prior.get(key) for key in ('status','command_started','exit_code')}}
                definite = (prior.get('command_started') is False or
                    (prior.get('status') == 'exited' and prior.get('command_started') is True
                     and type(prior.get('exit_code')) is int))
                reason = '复用本次辅助派发的既有命令结果；未再次执行，原结果不证明当前环境已重新验证。'
                if not definite:
                    reason += ' 原调用的执行结果仍未明确，不能据此清除未知效果或重放。'
                cached.host_observation = {'command_reused':True, 'reused':True,
                    'command_receipt_reuse':source,
                    'command_execution':execution_observation(
                        'not_started' if definite else 'outcome_unknown',
                        command_started=False if definite else None, reason=reason)}
                cached.output = 'COMMAND_RECEIPT_REUSED: ' + reason + '\n原始结果：\n' + cached.output
                return cached
            if command in self._attempted:
                return ToolResult(output="同一命令已在途或结果未知；未重复执行。", is_error=True,
                    host_observation={"command_execution": execution_observation(
                        "outcome_unknown", reason="同一辅助执行中的命令已派发，尚无可复用结果")})
            self._attempted.add(command)
            if observation is not None:
                observation["attempted_commands"].append(command)
            result = await self._inner.execute(params)
            self._results[command] = copy.deepcopy(result)
            return result

    return AllowlistedCommandGuard()
