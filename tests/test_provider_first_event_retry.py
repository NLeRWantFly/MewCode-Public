import pytest

from mewcode.agent import Agent, StreamRetryEvent, ToolResultEvent
from mewcode.conversation import ConversationManager
from mewcode.llm_watchdog import ProviderStreamTimeout
from mewcode.tools import create_default_registry
from mewcode.tools.base import TextDelta, ToolCallComplete, StreamEnd
from tests.test_agent import MockLLMClient


@pytest.mark.asyncio
async def test_first_event_retries_current_request_without_replaying_tool(tmp_path, monkeypatch):
    monkeypatch.setattr('mewcode.agent._stream_retry_delay', lambda *args: 0)
    source = tmp_path / 'source.txt'
    source.write_text('source', encoding='utf-8')
    class Client(MockLLMClient):
        calls = 0
        async def stream(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield ToolCallComplete('read-once', 'ReadFile', {'file_path': str(source)})
                yield StreamEnd('end_turn', input_tokens=1, output_tokens=1)
            elif self.calls in (2, 3):
                raise ProviderStreamTimeout('first_event', 120)
            else:
                yield TextDelta('done')
                yield StreamEnd('end_turn', input_tokens=1, output_tokens=1)
    client = Client([])
    agent = Agent(client, create_default_registry(), 'anthropic', work_dir=str(tmp_path))
    conv = ConversationManager()
    conv.add_user_message('Read source')
    events = [event async for event in agent.run(conv)]
    assert client.calls == 4
    assert len([e for e in events if isinstance(e, ToolResultEvent)]) == 1
    retries = [e for e in events if isinstance(e, StreamRetryEvent)]
    assert len(retries) == 2 and all(e.max_attempts == 2 for e in retries)


@pytest.mark.asyncio
@pytest.mark.parametrize('phase, calls', [('first_event', 3), ('output', 1), ('reasoning', 1)])
async def test_timeout_retries_are_bounded_and_partial_streams_never_replay(tmp_path, monkeypatch, phase, calls):
    monkeypatch.setattr('mewcode.agent._stream_retry_delay', lambda *args: 0)
    class Client(MockLLMClient):
        calls = 0
        async def stream(self, *args, **kwargs):
            self.calls += 1
            if phase == 'output':
                yield TextDelta('partial')
            raise ProviderStreamTimeout(phase, 120)
    client = Client([])
    agent = Agent(client, create_default_registry(), 'anthropic', work_dir=str(tmp_path))
    conv = ConversationManager()
    conv.add_user_message('Inspect')
    with pytest.raises(ProviderStreamTimeout):
        async for _ in agent.run(conv):
            pass
    assert client.calls == calls
