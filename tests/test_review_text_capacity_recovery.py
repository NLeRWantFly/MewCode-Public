"""Restore constraints and scope verified legacy service holds to their operation."""
import copy

import pytest

from mewcode.conversation import Message, ToolResultBlock, ToolUseBlock
from mewcode.expert_runs import deserialize_messages, serialize_messages
from tests.test_auto_effect_checkpoint import remember
from tests.test_effect_recovery_integration import _make_agent, _invoke, _write, _deny


GENERAL = 'AUTO_REVIEW_UNAVAILABLE: 完整审查请求超过文本容量；授权和原操作未截断。\n旧视觉输入提示'
OBSERVER = 'AUTO_REVIEW_UNAVAILABLE: 看图取证请求中的文本超出容量；授权和原操作未截断。\n旧视觉输入提示'


async def stopped_history(tmp_path, monkeypatch, reason):
    agent, checker, _ = _make_agent(tmp_path, monkeypatch, [_deny('policy_violation')])
    checker.review_workspace_writes = True
    denied_call = _write(tmp_path / 'target.txt', 'must remain absent')
    results, _ = await _invoke(agent, denied_call)
    remember(agent, denied_call, results[0])
    call = ToolUseBlock('size-fail', 'ReadFile', {'file_path': str(tmp_path / 'input.txt')})
    result = ToolResultBlock('size-fail', reason, is_error=True)
    conversation = agent._current_conversation
    conversation.history.extend([Message('assistant', '', tool_uses=[call]),
                                 Message('user', '', tool_results=[result])])
    agent._safety_review_unavailable = reason
    agent._stamp_review_checkpoint(result)
    # Fix/Update persist through the ordinary host-only message serializer.
    conversation.history = deserialize_messages(serialize_messages(conversation.history))
    return agent, denied_call, conversation


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', [GENERAL, OBSERVER])
async def test_removed_guard_restores_all_constraints_and_requires_fresh_review(tmp_path, monkeypatch, reason):
    old, call, conversation = await stopped_history(tmp_path, monkeypatch, reason)
    before = copy.deepcopy(old._review_recovery.checkpoint(conversation))
    agent, checker, reviewer = _make_agent(tmp_path, monkeypatch, [_deny('policy_violation')])
    checker.review_workspace_writes = True
    agent._current_conversation = conversation
    agent._restore_safety_review_state(conversation)
    assert not agent._safety_review_unavailable
    assert agent._review_recovery.checkpoint(conversation) == before
    assert conversation.history[-2].tool_results[0].content == reason
    assert '原操作尚未获授权' in conversation.history[-1].content
    reviewer._ask_model.assert_not_awaited()
    assert not (tmp_path / 'target.txt').exists()
    results, _ = await _invoke(agent, call)
    assert results[0].is_error and 'AUTO_DENIED' in results[0].output
    reviewer._ask_model.assert_awaited_once()
    assert reviewer._ask_model.call_args.kwargs['arguments'] == call.arguments
    assert not (tmp_path / 'target.txt').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('defect', ['missing_call', 'mismatched_result', 'invalid_checkpoint'])
async def test_other_or_unverifiable_stops_remain_blocked(tmp_path, monkeypatch, defect):
    reason = GENERAL
    _, _, conversation = await stopped_history(tmp_path, monkeypatch, reason)
    if defect == 'missing_call':
        conversation.history[-2].tool_uses.clear()
    elif defect == 'mismatched_result':
        conversation.history[-1].tool_results[0].content += 'changed'
    elif defect == 'invalid_checkpoint':
        conversation.history[-1].tool_results[0].host_observation['auto_review_checkpoint']['schema'] = 'invalid'
    agent, _, reviewer = _make_agent(tmp_path, monkeypatch)
    agent._current_conversation = conversation
    agent._restore_safety_review_state(conversation)
    assert agent._safety_review_unavailable
    if defect == 'invalid_checkpoint':
        assert agent._safety_review_unavailable.startswith('AUTO_REVIEW_CHECKPOINT_INVALID:')
    else:
        assert agent._safety_review_unavailable == reason
    reviewer._ask_model.assert_not_awaited()
    assert not (tmp_path / 'target.txt').exists()
