import pytest

from mewcode.agent import (
    Agent, ErrorEvent, OutputChunkEvent, RetryEvent, ToolExecutionStartEvent,
    ToolResultEvent,
)
from mewcode.conversation import ConversationManager, Message
from mewcode.experts.output_chunk_ledger import append_output_chunk
from mewcode.experts.output_window import next_output_limit
from mewcode.experts.recovery_escalation import recovery_upward_report
from mewcode.experts.recovery_diagnosis import diagnose_segment
from mewcode.tools import create_default_registry
from mewcode.tools.base import StreamEnd, TextDelta, ToolCallComplete
from tests.test_agent import MockLLMClient


class OutputClient(MockLLMClient):
    def __init__(self, responses, cap=64_000):
        super().__init__(responses)
        self.max_output_tokens = 8_192
        self.response_token_limit = 8_192
        self.configured_cap = cap
        self.limits = []

    def set_max_output_tokens(self, value):
        self.max_output_tokens = value
        self.limits.append(value)


def test_output_limit_doubles_within_configured_and_remaining_caps():
    assert next_output_limit(8_192, remaining_budget=40_000,
                             configured_cap=64_000) == 16_384
    assert next_output_limit(16_384, remaining_budget=20_000,
                             configured_cap=64_000) == 20_000
    assert next_output_limit(20_000, remaining_budget=20_000,
                             configured_cap=64_000) is None
    assert next_output_limit(8_192, remaining_budget=None,
                             configured_cap=8_192) is None
    assert next_output_limit(32_000, remaining_budget=None,
                             configured_cap=128_000) == 64_000


@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_invalid_output_budget_never_buys_an_escalation(value):
    with pytest.raises(ValueError):
        next_output_limit(8_192, remaining_budget=value, configured_cap=64_000)


def test_repeated_output_truncation_reports_window_not_business_failure():
    health = {"sampling": "tool_complete", "terminal_status": "needs_attention",
              "work_delta": {"known": True}, "terminal_calls": 0,
              "repeated_reads": 0}
    diagnosis = diagnose_segment(health, error_kind="output_window_limit")
    report = recovery_upward_report(error_kind="output_window_limit", diagnosis=diagnosis,
        health=health, remaining_tokens=10_000, remaining_seconds=90)
    assert diagnosis["primary_kind"] == "output_window_limit"
    assert report["action"] == "split_output_or_request_larger_declared_output"
    assert report["automatic_total_increase"] is False


@pytest.mark.asyncio
async def test_expert_agent_output_probes_raise_one_bounded_step_at_a_time():
    client = OutputClient([
        [TextDelta("part 1"), StreamEnd("max_tokens", input_tokens=100, output_tokens=8_192)],
        [TextDelta("part 2"), StreamEnd("max_tokens", input_tokens=110, output_tokens=16_384)],
        [TextDelta("done"), StreamEnd("end_turn", input_tokens=120, output_tokens=10)],
    ])
    agent = Agent(client, create_default_registry(), "anthropic")
    agent.expert_role = "executor"
    agent.expert_output_configured_cap = 64_000
    agent.expert_output_remaining_budget = 20_000
    conversation = ConversationManager()
    conversation.add_user_message("Complete the bounded answer")
    events = [event async for event in agent.run(conversation)]
    assert client.limits == [16_384, 20_000]
    assert len([event for event in events if isinstance(event, RetryEvent)]) == 2
    assert not [event for event in events if isinstance(event, ErrorEvent)]
    chunks = [event for event in events if isinstance(event, OutputChunkEvent)]
    assert [event.complete for event in chunks] == [False, False, True]
    ledger = []
    for event in chunks:
        append_output_chunk(ledger, event=event, history=conversation.history)
    assert len(ledger) == 3 and ledger[-1]["business_accepted"] is False
    assert append_output_chunk(ledger, event=chunks[0], history=conversation.history) == ledger[0]


@pytest.mark.asyncio
async def test_expert_agent_repeated_truncation_raises_typed_error_not_success():
    client = OutputClient([
        [TextDelta("partial"), StreamEnd("max_tokens", input_tokens=100, output_tokens=8_192)]
        for _ in range(4)
    ])
    agent = Agent(client, create_default_registry(), "anthropic")
    agent.expert_role = "executor"
    agent.expert_output_configured_cap = 8_192
    agent.expert_output_remaining_budget = 100_000
    conversation = ConversationManager()
    conversation.add_user_message("Answer")
    events = [event async for event in agent.run(conversation)]
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert errors and errors[-1].kind == "output_window_limit"
    assert client.limits == []
    assert all(not event.complete for event in events if isinstance(event, OutputChunkEvent))


def test_output_chunk_must_match_exact_retained_assistant_source():
    import hashlib
    text = "complete source"
    digest = hashlib.sha256(text.encode()).hexdigest()
    event = OutputChunkEvent("chunk-1", 0, digest, len(text), True)
    ledger = []
    assert append_output_chunk(ledger, event=event, history=[Message("assistant", text)])
    with pytest.raises(ValueError, match="exact assistant"):
        append_output_chunk([], event=event, history=[Message("assistant", "changed source")])
    with pytest.raises(ValueError, match="conflicting"):
        append_output_chunk(ledger, event=OutputChunkEvent("chunk-1", 0, digest, len(text), False),
                            history=[Message("assistant", text)])
    # Compaction may reuse message index zero for a genuinely new response.
    second = OutputChunkEvent("chunk-2", 0, digest, len(text), True)
    assert append_output_chunk(ledger, event=second, history=[Message("assistant", text)])["ordinal"] == 2


@pytest.mark.asyncio
async def test_truncated_tool_call_is_never_dispatched_as_complete_action():
    client = OutputClient([[ToolCallComplete("call-1", "ReadFile", {"file_path": "README.md"}),
                            StreamEnd("max_tokens", input_tokens=100, output_tokens=8_192)]])
    agent = Agent(client, create_default_registry(), "anthropic")
    agent.expert_role = "executor"
    conversation = ConversationManager()
    conversation.add_user_message("Read only if complete")
    events = [event async for event in agent.run(conversation)]
    assert any(isinstance(event, ErrorEvent) and event.kind == "output_window_limit"
               for event in events)
    assert not any(isinstance(event, (ToolExecutionStartEvent, ToolResultEvent))
                   for event in events)
