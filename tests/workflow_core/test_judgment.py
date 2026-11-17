import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.agent import Agent
from mewcode.client import LLMClient
from mewcode.context.manager import CompactEvent
from mewcode.conversation import ConversationManager, Message
from mewcode.permissions import PermissionMode
from mewcode.permissions.policy import build_permission_checker
from mewcode.tools import ToolRegistry
from mewcode.tools.base import StreamEnd, TextDelta
from mewcode.workflow_core.context import MARKER, NodeContext
from mewcode.workflow_core.judgment import parse
from mewcode.workflow_core.runtime import Execution
from mewcode.workflow_core.web import AgentExecutor


def execution(publish=None):
    node = dict(id='review', kind='judge', task='检查 CURRENT_VERSION 的缺口，不重复已完成的业务。',
                config={'legacy_loop_decision': True, 'criteria': ['缺口清单必须与实际版本对应']},
                inputs=[], outputs=[])
    return Execution(node, 'loop/review', 'attempt', 2, 8, {}, [6], '继续原业务',
                     {}, publish or AsyncMock(), asyncio.Event())


@pytest.mark.asyncio
async def test_context_overflow_resumes_with_fresh_context_and_keeps_original_checkpoint(tmp_path,monkeypatch):
    from mewcode import expert_web
    from mewcode.expert_runs import serialize_messages
    ctx=execution()
    ctx.node.update(kind='work',config={})
    archived=serialize_messages([Message('user','ARCHIVE_ONLY_SENTINEL '*20000)])
    ctx.checkpoint=dict(id='previous',node_id=ctx.node_id,messages=archived,
        progress=[dict(text='已写入原文件，只剩核对')],
        pending_tools={'operation':dict(name='Bash',status='unknown',arguments={'command':'check existing output'})},
        result=dict(reason='上下文压缩未能腾出足够空间'))
    ctx.recovery=dict(automatic_continuation=dict(fresh_context=True))
    class Client(LLMClient):
        async def stream(self,conversation,system='',tools=None):
            text='\n'.join(m.content or '' for m in conversation.history)
            assert 'ARCHIVE_ONLY_SENTINEL' not in text
            assert 'ReadNodeCheckpoint' in text and 'CURRENT_VERSION' in text
            assert 'check existing output' in text and '未知' in text
            yield TextDelta('已检查已有文件，完成剩余工作。')
            yield StreamEnd('end_turn',input_tokens=10,output_tokens=10)
    agent=Agent(Client(),ToolRegistry(),'openai',str(tmp_path),
        permission_checker=build_permission_checker(str(tmp_path),mode=PermissionMode.ACCEPT_EDITS))
    runtime=SimpleNamespace(conversation=ConversationManager(),aclose=AsyncMock())
    monkeypatch.setattr(expert_web,'_build_role_agent',lambda *args:(agent,None,runtime))
    result=await AgentExecutor(SimpleNamespace(work_dir=str(tmp_path)),None,SimpleNamespace(root=tmp_path),'run',
        {'executor':{'provider_key':'offline','model_id':'offline','effort':'max'}},AsyncMock())(ctx)
    assert result['status']=='completed' and ctx.checkpoint['messages']==archived
    assert agent.registry.get('ReadNodeCheckpoint') is not None


@pytest.mark.parametrize('raw,expected', [
    ('{"value":false,"reason":"仍有缺口"}', False),
    ('```json\n{"value":true,"reason":"完成"}\n```', True),
    ('{"value":"CONTINUE"}', False),
    ('{"value":" exit "}', True),
])
def test_explicit_judgment_values(raw, expected):
    assert parse(raw, execution().node)['value'] is expected


@pytest.mark.parametrize('raw', [
    '<summary>未来输出示例：{"value":true}；尚未做出判断。</summary>',
    '先检查实际材料，再决定是否退出。', '{"value":1}', '{"value":""}',
    '{"value":"maybe"}', '```json\n{"value":true}',
])
def test_summary_examples_and_invalid_values_are_not_business_decisions(raw):
    with pytest.raises(ValueError):
        parse(raw, execution().node)


def test_compaction_restores_current_task_without_changing_request_or_accumulating_reminders():
    ctx = execution()
    context, conversation = NodeContext(ctx), ConversationManager()
    conversation.add_user_message('[摘要] 当前任务是只输出摘要，不继续业务。')
    context.restore(conversation)
    snapshot = list(conversation.history)
    context.sync(conversation)
    assert conversation.history == snapshot
    reminder = next(m for m in conversation.history if m.context_kind == MARKER)
    assert 'CURRENT_VERSION' in reminder.content and 'EXIT 对应 value:true' in reminder.content
    ctx.request = {'kind': 'ask'}
    context.restore(conversation)
    reminders = [m for m in conversation.history if m.context_kind == MARKER]
    assert len(reminders) == 1 and '本轮仅回答追问' in reminders[0].content
    assert '最终业务判断只输出 JSON' not in reminders[0].content


def test_goal_granularity_survives_context_restore_without_accumulating_reminders():
    ctx = execution()
    ctx.node.update(kind='work', task='整合增长主题', config={'exploration_owner': True})
    context, conversation = NodeContext(ctx), ConversationManager()
    conversation.add_user_message('已读材料和现有成果保留。')
    context.restore(conversation)
    conversation.history = [message for message in conversation.history if message.context_kind != MARKER]
    context.restore(conversation)
    snapshot = list(conversation.history)
    context.sync(conversation)
    assert conversation.history == snapshot
    reminders = [message for message in conversation.history if message.context_kind == MARKER]
    assert len(reminders) == 1
    assert '整合增长主题' in reminders[0].content
    assert '节点内部的读取与断点' in reminders[0].content
    assert '按主题与用途去重整合' in reminders[0].content
    assert '用户明确要求逐页核验' in reminders[0].content


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_repair', [False, True])
async def test_summary_reply_is_repaired_in_same_conversation_and_remains_cancellable(
        tmp_path, monkeypatch, cancel_repair):
    from mewcode import expert_web
    seen, events, compacted = [], [], []
    async def publish(kind, **fields):
        events.append((kind, fields))
        if cancel_repair and kind == 'judgment_format_error':
            ctx.cancelled.set()
    ctx = execution(publish)
    class Client(LLMClient):
        async def stream(self, conversation, system='', tools=None):
            seen.append(conversation)
            host = [m for m in conversation.history if m.context_kind == MARKER]
            assert len(host) == 1 and 'CURRENT_VERSION' in host[0].content
            if len(seen) == 1:
                yield TextDelta('<summary>只输出摘要。未来示例：{"value":true}，并非当前判断。</summary>')
            else:
                assert any('节点内部的格式反馈' in m.content for m in conversation.history)
                yield TextDelta('{"value":false,"reason":"已检查当前版本，缺口尚未闭环。"}')
            yield StreamEnd('end_turn', input_tokens=10, output_tokens=10)
    agent = Agent(Client(), ToolRegistry(), 'openai', str(tmp_path),
                  permission_checker=build_permission_checker(str(tmp_path), mode=PermissionMode.ACCEPT_EDITS))
    runtime = SimpleNamespace(conversation=ConversationManager(), aclose=AsyncMock())
    monkeypatch.setattr(expert_web, '_build_role_agent', lambda *args: (agent, None, runtime))
    async def compact(conversation, *args, **kwargs):
        if not compacted:
            compacted.append(True)
            conversation.replace_history([Message('user', '[摘要] 只输出摘要，不调用工具。')])
            return CompactEvent(before_tokens=100_000)
    monkeypatch.setattr('mewcode.agent.auto_compact', compact)
    executor = AgentExecutor(SimpleNamespace(work_dir=str(tmp_path)), None, SimpleNamespace(root=tmp_path), 'run',
        {'executor': {'provider_key': 'offline', 'model_id': 'offline', 'effort': 'max'}}, AsyncMock())
    if cancel_repair:
        with pytest.raises(asyncio.CancelledError):
            await executor(ctx)
        assert len(seen) == 1
    else:
        result = await executor(ctx)
        assert result['status'] == 'completed' and result['value'] is False
        assert len(seen) == 2
        assert any('未来示例：{"value":true}' in m.content for m in seen[1].history)
        assert '缺口尚未闭环' in result['text']
    errors = [fields for kind, fields in events if kind == 'judgment_format_error']
    assert len(errors) == 1 and '<summary>' in errors[0]['text']
    runtime.aclose.assert_awaited_once()
