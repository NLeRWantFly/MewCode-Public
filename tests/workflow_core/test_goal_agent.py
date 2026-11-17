from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.agent import Agent
from mewcode.conversation import ConversationManager
from mewcode.expert_runs import ExpertRunStore
from mewcode.permissions import PermissionMode
from mewcode.permissions.policy import build_permission_checker
from mewcode.permissions.reviewer import SafetyReviewResult
from mewcode.runtime_scheduler import RuntimeScheduler
from mewcode.tools import ToolRegistry
from mewcode.tools.base import TextDelta, ToolCallComplete, StreamEnd
from mewcode.tools.write_file import WriteFile
from mewcode.tools.read_file import ReadFile
from mewcode.workflow_core.exploration import GoalCore
from mewcode.workflow_core.recipes import RecipeStore
from mewcode.workflow_core.store import ensure
from mewcode.workflow_core.web import AgentExecutor, envelope
from tests.test_agent import MockLLMClient
from tests.workflow_core.test_exploration import goal_state, MODEL


@pytest.mark.asyncio
async def test_real_agent_tools_expand_concurrently_publish_visuals_and_solidify(tmp_path, monkeypatch):
    from mewcode import expert_web
    calls = Counter()
    def build(session, registry, role, model, policy):
        assert model.model_id == 'gpt-6.1-sol' and model.effort == 'max'
        assert policy.max_iterations == 0
        path = policy.execution_node_id
        calls[path] += 1
        if path == 'goal' and calls[path] == 1:
            responses = [
                [ToolCallComplete('add', 'UpdateWorkflowTasks', dict(request_id='first', items=[
                    dict(id=name, label=name, task='处理行测章节并写出结果 ' + str(tmp_path / (name+'.txt')))
                    for name in ('section_a','section_b')])), StreamEnd(stop_reason='tool_use')],
                [ToolCallComplete('wait', 'WaitForWorkflowTasks', dict(node_ids=['section_a','section_b'])),
                 StreamEnd(stop_reason='tool_use')],
                [TextDelta('行测章节工作已交付。'), StreamEnd(stop_reason='end_turn')]]
        elif path == 'goal':
            responses = [[TextDelta('行测章节工作已交付。'), StreamEnd(stop_reason='end_turn')]]
        else:
            responses = [
                [ToolCallComplete('visual', 'PublishWorkflowBlock', dict(block_id='summary',slot='output',kind='table',
                    title='章节结果',columns=['工作项','结果'],rows=[[path,'已处理']])),
                 ToolCallComplete('write','WriteFile',dict(file_path=str(tmp_path/(path+'.txt')),content='行测章节实际工具产物')),
                 StreamEnd(stop_reason='tool_use')],
                [TextDelta('文件与输出块已保存。'), StreamEnd(stop_reason='end_turn')]]
        tools = ToolRegistry()
        tools.register(WriteFile())
        agent = Agent(MockLLMClient(responses), tools, 'anthropic', str(tmp_path),
            permission_checker=build_permission_checker(str(tmp_path), mode=PermissionMode.ACCEPT_EDITS))
        agent.safety_reviewer.review = AsyncMock(return_value=SafetyReviewResult(
            decision='allow',risk='low',reason='Fixture stays inside its own temporary workspace'))
        return agent,None,SimpleNamespace(conversation=ConversationManager(),aclose=AsyncMock())
    monkeypatch.setattr(expert_web,'_build_role_agent',build)
    monkeypatch.setattr(AgentExecutor,'pipeline',AsyncMock(side_effect=AssertionError('Legacy Pipeline must not run')))
    store = ExpertRunStore(tmp_path/'runs', storage='sqlite')
    state, rid = goal_state(), '1'*32
    store.create(run_id=rid,conversation_id='chat',task='行测章节试跑',attachments=[],profile={'executor':MODEL},
        strategy='stable',flow=envelope(state['graph']))
    backend = ensure(store,rid,state['graph'])
    backend.change(lambda saved:saved.update(inputs=state['inputs']))
    executor = AgentExecutor(SimpleNamespace(work_dir=str(tmp_path)),None,store,rid,{'executor':MODEL},AsyncMock())
    result = await GoalCore(backend,executor,concurrency=3,recipe_store=RecipeStore(tmp_path),run_id=rid).run()
    assert result['status'] == 'completed', result['nodes']
    assert calls['section_a'] == calls['section_b'] == 1
    for name in ('section_a','section_b'):
        assert (tmp_path/(name+'.txt')).read_text(encoding='utf-8') == '行测章节实际工具产物'
        attempt = result['attempts'][result['nodes'][name]['attempt_id']]
        assert attempt['presentation_blocks']['output:summary']['rows'] == [[name,'已处理']]
    assert result['solidified_recipe']['version'] == 1 and result['execution_metrics']['peak_active'] >= 2


@pytest.mark.asyncio
async def test_goal_delivers_large_file_to_model_without_fixed_preview_caps(tmp_path, monkeypatch):
    from mewcode import expert_web
    source = tmp_path/'source.txt'
    source.write_text(('source block ' + 'x'*110 + '\n')*1800 + 'FINAL_SOURCE_BOUNDARY', encoding='utf-8')
    seen, original_slots, agents = [], [], []
    scheduler = RuntimeScheduler(limit=2,kind_limits=dict(model=1,tool=1))
    class Client(MockLLMClient):
        async def stream(self, conversation, system='', tools=None):
            if self._call_index == 1:
                receipts = [result.content for message in conversation.history for result in message.tool_results]
                assert len(receipts) == 1 and len(receipts[0]) > 200_000
                assert receipts[0].endswith('FINAL_SOURCE_BOUNDARY')
                seen.append(True)
            async for event in super().stream(conversation, system=system, tools=tools):
                yield event
    def build(session, registry, role, model, policy):
        client = Client([
            [ToolCallComplete('source', 'ReadFile', dict(file_path=str(source))), StreamEnd(stop_reason='tool_use')],
            [TextDelta('完整来源已读取。'), StreamEnd(stop_reason='end_turn')]])
        tools = ToolRegistry()
        tools.register(ReadFile())
        agent = Agent(client, tools, 'anthropic', str(tmp_path), context_window=200_000,
            permission_checker=build_permission_checker(str(tmp_path), mode=PermissionMode.ACCEPT_EDITS))
        original_slots.append(agent.cache_usage_provider.slot)
        agents.append(agent)
        agent.safety_reviewer.review = AsyncMock(return_value=SafetyReviewResult(
            decision='allow',risk='low',reason='Read only the declared temporary source file'))
        return agent,None,SimpleNamespace(conversation=ConversationManager(),aclose=AsyncMock())
    monkeypatch.setattr(expert_web, '_build_role_agent', build)
    state, rid = goal_state(), '3'*32
    state['graph']['nodes'][0]['config']['goal_execution'] = True
    store = ExpertRunStore(tmp_path/'runs',storage='sqlite')
    store.create(run_id=rid,conversation_id='chat',task='读取数学材料',attachments=[],profile={'executor':MODEL},
                 strategy='stable',flow=envelope(state['graph']))
    backend = ensure(store,rid,state['graph'])
    executor = AgentExecutor(SimpleNamespace(work_dir=str(tmp_path),goal_runtime_scheduler=scheduler),
                             None,store,rid,{'executor':MODEL},AsyncMock())
    result = await GoalCore(backend,executor,run_id=rid).run()
    assert result['status'] == 'completed' and seen == [True]
    assert original_slots[0].single_result_char_limit is not None
    assert original_slots[0].aggregate_result_char_limit is not None
    assert agents[0].runtime_scheduler is scheduler
