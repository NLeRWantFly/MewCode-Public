"""Loaded SOPs must reach the next real provider request in the same turn."""
import json

import pytest

from mewcode.agent import Agent, ErrorEvent
from mewcode.client import LLMClient
from mewcode.conversation import ConversationManager
from mewcode.serialization import build_anthropic_messages, build_chat_completion_messages
from mewcode.skills.loader import SkillLoader
from mewcode.skills.runtime import attach_skills
from mewcode.tools import ToolRegistry
from mewcode.tools.base import StreamEnd, TextDelta, ToolCallComplete


@pytest.mark.asyncio
@pytest.mark.parametrize('completion', [False, True])
@pytest.mark.parametrize('protocol', ['anthropic', 'openai-compat'])
@pytest.mark.parametrize('compact_after_load', [False, True])
async def test_loaded_skill_visible_on_next_provider_round(tmp_path, monkeypatch, completion, protocol, compact_after_load):
    skill = tmp_path / '.mewcode/skills/report/SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('---\nname: report\ndescription: Report SOP\nallowedTools: [WriteFile]\n---\n'
                     'Include UNIQUE_SOP_MARKER_834291 in the report.', encoding='utf-8')
    snapshots = []
    if compact_after_load:
        from mewcode.context.manager import CompactEvent
        async def compact(conversation, *args, **kwargs):
            if len(snapshots) == 1:
                conversation.history = [m for m in conversation.history if not m.transient]
                conversation.env_injected = conversation.ltm_injected = False
                return CompactEvent(before_tokens=100)
            return None
        monkeypatch.setattr('mewcode.agent.auto_compact', compact)

    class Client(LLMClient):
        async def stream(self, conversation, system='', tools=None):
            wire = (build_anthropic_messages if protocol == 'anthropic' else build_chat_completion_messages)(conversation.get_messages())
            snapshots.append(json.dumps(wire, ensure_ascii=False))
            if len(snapshots) == 1:
                yield ToolCallComplete('load-1', 'LoadSkill', {'name': 'report'})
            else:
                yield TextDelta('Report follows the loaded SOP.')
            yield StreamEnd('end_turn', input_tokens=10, output_tokens=10)

    registry = ToolRegistry()
    agent = Agent(Client(), registry, protocol, str(tmp_path), max_iterations=3)
    loader = SkillLoader(str(tmp_path))
    loader._user_dir = tmp_path / 'no-user-skills'
    attach_skills(str(tmp_path), agent, registry, loader=loader, instruction_only=True)
    conversation = ConversationManager()
    conversation.add_user_message('Make a report.')
    if completion:
        await agent.run_to_completion('Load the matching skill.', conversation)
    else:
        events = [event async for event in agent.run(conversation)]
        assert not [event for event in events if isinstance(event, ErrorEvent)]
    assert len(snapshots) == 2
    assert 'UNIQUE_SOP_MARKER_834291' not in snapshots[0]
    assert 'UNIQUE_SOP_MARKER_834291' in snapshots[1]
    assert registry.get('WriteFile') is None
    assert sum(m.context_kind == 'environment' for m in conversation.history) == 1


def test_environment_refresh_preserves_real_history_and_repairs_missing_slot():
    conversation = ConversationManager()
    conversation.add_user_message('Saved user input')
    original = conversation.history[0]
    conversation.inject_environment('old SOP')
    conversation.refresh_environment('new SOP')
    assert original in conversation.history and original.content == 'Saved user input'
    assert [m.content for m in conversation.history if m.context_kind == 'environment'] == ['new SOP']
    conversation.history = [original]  # Simulate a restored/compacted history without the transient slot.
    assert conversation.env_injected
    conversation.refresh_environment('restored SOP')
    assert len(conversation.history) == 2
    assert conversation.history[0].transient and conversation.history[0].content == 'restored SOP'
    assert original is conversation.history[1]
