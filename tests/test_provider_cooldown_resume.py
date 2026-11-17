"""Short provider cooldowns wait in place without replaying completed work."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from mewcode.agent import Agent, StreamRetryEvent
from mewcode.client import RateLimitError
from mewcode.conversation import ConversationManager
from mewcode.provider_cooldown import record_cooldown
from mewcode.tools import ToolRegistry
from mewcode.tools.base import TextDelta, ToolCallComplete, StreamEnd
from mewcode.tools.read_file import ReadFile
from tests.test_agent import MockLLMClient


@pytest.mark.asyncio
@pytest.mark.parametrize('headless', [False, True])
@pytest.mark.parametrize('outcome', ['success', 'cancel', 'exhausted'])
async def test_short_cooldown_keeps_completed_tool_and_bounds_requests(tmp_path, monkeypatch, headless, outcome):
    source = tmp_path / 'source.txt'
    source.write_text('read once', encoding='utf-8')
    read = ReadFile()
    execute = read.execute
    reads, waits, events = [], [], []

    async def tracked_read(params, **kwargs):
        reads.append(params.file_path)
        return await execute(params, **kwargs)

    monkeypatch.setattr(read, 'execute', tracked_read)
    registry = ToolRegistry()
    registry.register(read)

    class Client(MockLLMClient):
        calls = 0

        async def stream(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield ToolCallComplete('read-once', 'ReadFile', {'file_path': str(source)})
            elif self.calls == 2 or outcome == 'exhausted':
                raise RateLimitError('Rate limited. Retry after 56s.', retry_after=56)
            else:
                yield TextDelta('done')
            yield StreamEnd('end_turn', input_tokens=1, output_tokens=1)

    real_sleep = asyncio.sleep

    async def wait(delay, *args, **kwargs):
        if delay == 56:
            waits.append(delay)
            assert len(reads) == 1
            if outcome == 'cancel':
                raise asyncio.CancelledError()
            return
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(asyncio, 'sleep', wait)
    client = Client([])
    agent = Agent(client, registry, 'anthropic', work_dir=str(tmp_path))
    conv = ConversationManager()
    conv.add_user_message('Read source')

    async def run():
        if headless:
            await agent.run_to_completion('Read source', conv)
        else:
            events.extend([event async for event in agent.run(conv)])

    if outcome == 'success':
        await run()
        assert client.calls == 3 and waits == [56]
        if not headless:
            retries = [event for event in events if isinstance(event, StreamRetryEvent)]
            assert len(retries) == 1 and retries[0].retry_after == 56
    else:
        with pytest.raises(asyncio.CancelledError if outcome == 'cancel' else RateLimitError):
            await run()
        assert client.calls == (2 if outcome == 'cancel' else 6)
        assert waits == [56] * (1 if outcome == 'cancel' else 4)
    assert len(reads) == 1


@pytest.mark.parametrize('seconds, blocked', [(56, False), (300, False), (1440902, True)])
def test_resume_accepts_short_persisted_cooldown_but_blocks_quota_reset(monkeypatch, seconds, blocked):
    from mewcode import expert_web
    model = {'provider_key': 'p', 'model_id': 'm', 'effort': 'max'}
    checkpoint = {'locked_flow': {'flow_id': 'cooldown', 'finalizer_node_id': 'work',
                                 'nodes': [{'node_id': 'work', 'role': 'executor'}], 'edges': []},
                  'locked_models': {'work': model}, 'node_states': {}}
    record_cooldown(checkpoint, model, seconds, now=time.time())
    monkeypatch.setattr(expert_web, 'expert_flow_model_error', lambda *args, **kwargs: '')
    assert bool(expert_web.expert_resume_model_error(SimpleNamespace(), checkpoint)) is blocked
