from __future__ import annotations

import asyncio
from collections import Counter
from types import SimpleNamespace

import pytest

from mewcode.agent import Agent
from mewcode.agents.task_manager import TaskCleanupError, TaskManager
from mewcode.sandbox.receipt import NativeCleanupUnconfirmed, NativeExecutionReceipt
from mewcode.tools import command_sandbox
from mewcode.tools.base import ToolCallComplete


def test_native_cleanup_error_is_monotonic_across_later_success_receipt():
    owner = SimpleNamespace(_native_cleanup_error="")

    with pytest.raises(NativeCleanupUnconfirmed, match="helper exit was not confirmed"):
        with command_sandbox.native_auto_command(owner=owner):
            observe = command_sandbox._native_receipt_observer.get()
            assert callable(observe)

            observe(NativeExecutionReceipt(
                "cancelled",
                detail="helper exit was not confirmed",
                cleanup_confirmed=False,
            ))
            assert owner._native_cleanup_error == "helper exit was not confirmed"

            observe(NativeExecutionReceipt(
                "exited",
                exit_code=0,
                cleanup_confirmed=True,
            ))
            assert owner._native_cleanup_error == "helper exit was not confirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entrypoint",
    ["direct", "streaming", "noninteractive"],
)
async def test_agent_tool_entrypoints_fail_before_tool_or_hooks(entrypoint, monkeypatch):
    agent = object.__new__(Agent)
    agent._native_cleanup_error = "native helper cleanup is unconfirmed"
    calls = Counter()

    async def emit_event(*_args, **_kwargs):
        calls["before_or_after_event"] += 1

    async def direct_impl(*_args, **_kwargs):
        calls["tool_pipeline"] += 1
        return None

    async def noninteractive_impl(*_args, **_kwargs):
        calls["tool_pipeline"] += 1
        return None

    async def streaming_impl(*_args, **_kwargs):
        calls["tool_pipeline"] += 1
        yield None

    monkeypatch.setattr(agent, "_emit_runtime_event", emit_event)
    monkeypatch.setattr(agent, "_execute_single_tool_direct_impl", direct_impl)
    monkeypatch.setattr(agent, "_execute_tool_impl", streaming_impl)
    monkeypatch.setattr(agent, "_execute_tool_noninteractive_impl", noninteractive_impl)
    call = ToolCallComplete("call-1", "Bash", {"command": "echo must-not-run"})

    with pytest.raises(NativeCleanupUnconfirmed, match="cleanup is unconfirmed"):
        if entrypoint == "direct":
            await agent._execute_single_tool_direct(call)
        elif entrypoint == "streaming":
            async for _ in agent._execute_tool(call):
                pass
        else:
            await agent._execute_tool_noninteractive(call)

    assert calls == Counter()


class _SuccessfulCordisRuntime:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _NativeCleanupFailedAgent:
    def __init__(self) -> None:
        self.agent_id = "native-owner"
        self._native_cleanup_error = "native helper cleanup is unconfirmed"
        self._cordis_runtime = _SuccessfulCordisRuntime()
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.started = asyncio.Event()

    async def run_to_completion(self, *_args, **_kwargs) -> str:
        self.started.set()
        await asyncio.Event().wait()
        return "unreachable"


@pytest.mark.asyncio
async def test_task_finalization_and_settlement_preserve_native_cleanup_failure():
    manager = TaskManager()
    agent = _NativeCleanupFailedAgent()
    runtime = agent._cordis_runtime
    task_id = manager.adopt_running(agent, "wait for cancellation")
    await agent.started.wait()

    with pytest.raises(TaskCleanupError, match="Native runtime cleanup unconfirmed"):
        await manager.settle_tasks([task_id], cancel=True)

    task = manager.get(task_id)
    assert task is not None
    assert runtime.closed is True
    assert task.status == "failed"
    assert task.lifecycle_finalized is True
    assert task.cleanup_confirmed is False
    assert "native helper cleanup is unconfirmed" in task.cleanup_error
