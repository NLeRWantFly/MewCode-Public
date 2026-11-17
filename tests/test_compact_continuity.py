"""Compaction preserves observable progress without replaying bulky context or work."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mewcode.agent import Agent
from mewcode.context.manager import (
    CompactBlocked, CompactEvent, RecoveryState, auto_compact,
    build_compact_messages, build_recovery_attachment,
)
from mewcode.conversation import ConversationManager, Message
from mewcode.expert_runs import ExpertRunStore
from mewcode.memory.session import SessionManager, SessionMeta, make_compact_boundary, parse_compact_boundary
from mewcode.orchestration.chat_routing import dialogue_focus, prepare_chat_focus, set_dialogue_focus
from mewcode.tools import ToolRegistry
from mewcode.tools.base import ToolCallComplete, ToolResult
from mewcode.workflow_core import goal_host
from mewcode.workflow_core.chat_tools import ChatWorkflow, ControlParams, ReadParams, RunParams, SaveParams
from tests.test_compaction_regressions import SummaryClient, conversation_with_old_work
from tests.workflow_core.test_chat_tools import host, update_params
from tests.workflow_core.test_exploration import item


def test_recovery_records_only_the_actual_read_slice(tmp_path, monkeypatch):
    agent = Agent(SummaryClient(), ToolRegistry(), 'openai-compat', str(tmp_path))
    path = tmp_path / 'source.txt'
    path.write_text('unseen changed bytes', encoding='utf-8')
    monkeypatch.setattr('builtins.open', Mock(side_effect=AssertionError('must not reopen file')))
    result = ToolResult('101: observed line', host_observation={'file_state': {'absolute_path': str(path)}})
    agent._snapshot_for_recovery(ToolCallComplete('read-101', 'ReadFile',
        dict(file_path='source.txt', offset=100, limit=1)), result)
    record = agent.recovery_state.snapshot_files(1)[0]
    assert record.path == str(path) and record.content == result.output
    assert (record.tool_use_id, record.offset, record.limit) == ('read-101', 100, 1)
    manifest = build_recovery_attachment(agent.recovery_state, None, compact=True)
    assert 'read-101' in manifest and 'output_sha256' in manifest
    assert 'unseen changed bytes' not in manifest and 'observed line' not in manifest


def test_legacy_full_file_snapshot_is_not_claimed_as_observed_output():
    recovery = RecoveryState()
    recovery.record_file_read('/legacy.txt', 'entire file from older client')
    assert build_recovery_attachment(recovery, None, compact=True) == ''


@pytest.mark.asyncio
async def test_repeated_compaction_and_reload_keep_small_recovery_references(tmp_path):
    recovery = RecoveryState()
    for i in range(5):
        recovery.record_file_read(f'/material/{i}.txt', 'FILE_BODY' * 30_000, tool_use_id=f'read-{i}')
        recovery.record_skill_invocation(f'skill-{i}', 'SKILL_BODY' * 30_000)
    conv = conversation_with_old_work()
    schemas = [dict(name='ReadFile', description='SCHEMA_BODY' * 20_000)]
    def restore(candidate):
        candidate.replace_marker_reminder('chat-workflow-tools', 'HOST_GUIDANCE_REGENERATED')
    restore(conv)
    class Client(SummaryClient):
        async def stream(self, conversation, **kwargs):
            text = '\n'.join(m.content for m in conversation.history)
            assert 'FILE_BODY' not in text and 'SKILL_BODY' not in text
            assert 'SCHEMA_BODY' not in text and 'HOST_GUIDANCE_REGENERATED' not in text
            async for event in super().stream(conversation, **kwargs):
                yield event
    lengths = []
    for turn in range(3):
        if turn:
            for i in range(8):
                conv.history.append(Message('assistant', 'new completed work ' * 1000))
        event = await auto_compact(conv, Client(), 200_000, tmp_path, manual=True,
            recovery=recovery, tool_schemas=schemas, restore_context=restore)
        assert isinstance(event, CompactEvent), event
        lengths.append(len(event.boundary.summary))
        summary, tail = parse_compact_boundary(make_compact_boundary(event.boundary.summary, event.boundary.keep))
        conv = ConversationManager()
        conv.replace_history(build_compact_messages(summary, has_keep_tail=bool(tail)) + tail)
        restore(conv)
        text = '\n'.join(m.content for m in conv.history)
        assert 'read-4' in text and 'FILE_BODY' not in text and 'SKILL_BODY' not in text
    assert len(set(lengths)) == 1 and max(lengths) < 4000


@pytest.mark.asyncio
async def test_summary_overflow_never_drops_old_user_constraints(tmp_path):
    class Overflow(SummaryClient):
        async def stream(self, conversation, **kwargs):
            self.calls += 1
            raise RuntimeError('prompt is too long')
            yield
    conv = conversation_with_old_work()
    original = copy.deepcopy(conv.history)
    client = Overflow()
    event = await auto_compact(conv, client, 128_000, tmp_path, manual=True)
    assert isinstance(event, CompactBlocked)
    assert conv.history == original and client.calls == 1


@pytest.mark.asyncio
async def test_workflow_reload_restores_completed_and_paused_receipts_without_execution(tmp_path, monkeypatch):
    srv, runtime, bridge = host(tmp_path, monkeypatch)
    durable = SessionManager(str(tmp_path)).create()
    durable.meta.dialogue_focus = copy.deepcopy(runtime.session.meta.dialogue_focus)
    runtime.session = durable
    calls, started = [], asyncio.Event()
    async def execute(ctx):
        calls.append(ctx.node_id)
        if len(calls) > 1:
            started.set()
            await asyncio.Event().wait()
        return dict(text='已交付')
    monkeypatch.setattr(goal_host, 'AgentExecutor', Mock(return_value=execute))
    await bridge.dispatch(update_params(item('done')))
    await asyncio.wait_for(bridge.wait(ReadParams()), 10)
    delivered = bridge.backend().query(lambda state: state['nodes']['done'])
    recipe = await bridge.save(SaveParams())
    reused = await bridge.run_recipe(RunParams(request_id='reuse', recipe_id=recipe['id'],
        version=recipe['version'], inputs={}))
    await asyncio.wait_for(started.wait(), 10)
    await bridge.control(ControlParams(run_id=reused['run_id'], operation='pause', request_id='pause'))
    await asyncio.wait_for(goal_host.TASKS[reused['run_id']], 10)
    prepare_chat_focus(runtime, '只检查行测章节；已暂停的节点先不要恢复', continuation_task_id=bridge.task_id)
    meta_path = durable._sessions_dir / f'{durable.session_id}.meta'
    durable.close()
    runtime.session = SimpleNamespace(meta=SessionMeta.load(meta_path))
    srv.EXPERT_RUNS = ExpertRunStore(tmp_path/'runs', storage='sqlite')
    monkeypatch.setattr(srv.EXPERT_RUNS, 'load', Mock(side_effect=AssertionError('no full run copies')))
    monkeypatch.setattr(srv.EXPERT_RUNS, 'list_for_conversation', Mock(side_effect=AssertionError('no run scans')))
    reloaded = ChatWorkflow(srv, runtime)
    conversation = ConversationManager()
    conversation.add_user_message('[摘要] earlier work')
    reloaded.restore(conversation)
    reminder = next(m.content for m in conversation.history if m.context_kind == 'chat-workflow-state')
    assert '先不要恢复' in reminder and 'completed' in reminder and 'paused' in reminder
    assert reused['run_id'] in reminder and 'delivery_id' in reminder
    assert delivered['attempt_id'] in reminder and delivered['published_delivery_id'] in reminder
    original = list(conversation.history)
    conversation.record_usage_anchor(25_000)
    for _ in range(3):
        reloaded.sync(conversation)
    assert conversation.history == original
    assert all(a is b for a, b in zip(original, conversation.history))
    assert conversation.current_tokens() == 25_000 and calls == ['done', 'done']
    set_dialogue_focus(runtime, 'chat', task='另一个任务', task_id='different-task')
    assert 'workflow_run_ids' not in dialogue_focus(runtime)
