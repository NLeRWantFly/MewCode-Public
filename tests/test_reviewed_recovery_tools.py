import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.agent import Agent
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.reviewed_tools import invoke_reviewed_tool
from mewcode.permissions import PermissionMode
from mewcode.permissions.policy import build_permission_checker
from mewcode.permissions.rules import Rule
from mewcode.tools import ToolRegistry
from mewcode.tools.prepare_python_environment import PreparePythonEnvironment
from mewcode.tools.base import ToolResult
from tests.test_agent import MockLLMClient
from tests.test_retry_memory import retry_flow


@pytest.mark.asyncio
async def test_host_recovery_uses_real_agent_policy_before_environment_write(tmp_path, monkeypatch):
    registry = ToolRegistry()
    tool = PreparePythonEnvironment()
    execute = AsyncMock(return_value=ToolResult('should not run'))
    monkeypatch.setattr(tool, 'execute', execute)
    registry.register(tool)
    checker = build_permission_checker(tmp_path, mode=PermissionMode.AUTO)
    checker.rule_engine.append_session_rule(Rule(tool.name, '*', 'deny'))
    agent = Agent(MockLLMClient([]), registry, 'anthropic', work_dir=str(tmp_path), permission_checker=checker)
    store = ExpertRunStore(tmp_path / 'runs')
    identity = 'd' * 32
    store.create(run_id=identity, conversation_id='test', task='recover', attachments=[], profile={},
                 strategy='stable', flow=retry_flow())
    result = await invoke_reviewed_tool(agent, tool.name, {}, session=SimpleNamespace(), store=store,
        run_id=identity, node_id='work', emit=AsyncMock())
    assert result.is_error
    execute.assert_not_awaited()
    rows = list(store.load(identity)['node_states']['work']['tools'].values())
    assert len(rows) == 1 and rows[0]['is_error']
    assert result.host_observation['recovery_tool_id'] == rows[0]['tool_id']


@pytest.mark.asyncio
async def test_cancelled_host_tool_preserves_unknown_effect_and_closes_gateway(tmp_path):
    from mewcode.agent import ToolExecutionStartEvent
    entered, closed = asyncio.Event(), []
    async def execute(call):
        try:
            yield ToolExecutionStartEvent(call.tool_name, call.tool_id)
            entered.set()
            await asyncio.Future()
        finally:
            closed.append(True)
    store = ExpertRunStore(tmp_path / 'runs')
    identity = 'e' * 32
    store.create(run_id=identity, conversation_id='test', task='recover', attachments=[], profile={},
                 strategy='stable', flow=retry_flow())
    task = asyncio.create_task(invoke_reviewed_tool(SimpleNamespace(_execute_tool=execute), 'Bash', {'command': 'probe'},
        session=SimpleNamespace(), store=store, run_id=identity, node_id='work', emit=AsyncMock()))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True]
    assert next(iter(store.load(identity)['node_states']['work']['tools'].values()))['status'] == 'outcome_unknown'
