"""Compaction must reclaim usable input space without losing working evidence."""
from __future__ import annotations

import copy

import pytest

from mewcode.agent import Agent, CompactNotification, ErrorEvent
from mewcode.client import LLMClient
from mewcode.config import ProviderConfig
from mewcode.context import CompactBlocked, CompactCircuitBreaker
from mewcode.context.manager import (
    CompactEvent, apply_tool_result_budget, auto_compact, compute_compact_threshold,
    create_replacement_state,
)
from mewcode.context.routing import DEEPSEEK_CONTEXT_POLICY
from mewcode.conversation import ConversationManager, Message, ToolResultBlock, ToolUseBlock
from mewcode.model_capabilities import resolve_model_capabilities
from mewcode.model_catalog import DEFAULT_MODEL_CATALOG
from mewcode.opencode_go import resolve_effective_provider_config
from mewcode.tools import ToolRegistry
from mewcode.tools.base import StreamEnd, TextDelta


class SummaryClient(LLMClient):
    def __init__(self, text="The task and its requirements remain unchanged."):
        self.text = text
        self.calls = 0

    async def stream(self, conversation, system="", tools=None):
        self.calls += 1
        yield TextDelta(f"<summary>{self.text}</summary>")
        yield StreamEnd("end_turn", input_tokens=100, output_tokens=20)


def provider(model="deepseek-v4.1-flash", **kwargs):
    return ProviderConfig("fixture", "openai-compat", "https://example.invalid/v1", model, **kwargs)


def conversation_with_old_work():
    conv = ConversationManager()
    conv.add_user_message("Preserve the requested output and scope.")
    for i in range(8):
        conv.history.append(Message("assistant", f"old-{i}: " + "x" * 21_000))
    conv.add_assistant_message("read current state", [ToolUseBlock("recent", "ReadWorkflowBlocks", {})])
    conv.add_tool_results_message([ToolResultBlock("recent", "CURRENT_REVISION_14 " * 350)])
    conv.record_usage_anchor(85_000)
    return conv


@pytest.mark.parametrize("model", ["deepseek-v4.1-flash", "deepseek-flash", "deepseek-v4-flash"])
def test_flash_ids_resolve_capacity_without_rewriting_wire_model(model):
    config = provider(model)
    assert config.get_context_window() == 1_000_000
    assert config.get_max_output_tokens() == 64_000
    assert resolve_model_capabilities(model).context_window == 1_000_000
    assert DEFAULT_MODEL_CATALOG.describe(model).vision
    assert resolve_effective_provider_config(config).model == model


def test_go_accepts_v41_and_explicit_capacity_overrides_remain_authoritative():
    config = provider(provider_type="opencode-go")
    route = resolve_effective_provider_config(config)
    assert route.model == "deepseek-v4.1-flash"
    assert route.protocol == "openai-compat"
    assert route.get_context_window() == 1_000_000
    config.context_window = 128_000
    assert resolve_effective_provider_config(config).get_context_window() == 128_000
    assert provider("deepseek-future-unknown").get_context_window() == 128_000


@pytest.mark.asyncio
async def test_observed_working_set_no_longer_compacts_each_provider_round(tmp_path):
    config = provider()
    client = SummaryClient()
    conv = conversation_with_old_work()
    for i in range(3):
        conv.record_usage_anchor(35_157, 125, 2_048)
        conv.add_user_message(f"tool batch {i}: " + "data " * 1_000)
        result = await auto_compact(conv, client, config.get_context_window(), tmp_path,
                                    policy=DEEPSEEK_CONTEXT_POLICY, fixed_prefix_tokens=20_000,
                                    output_reserve_tokens=config.get_max_output_tokens())
        assert result is None
    assert client.calls == 0


@pytest.mark.asyncio
async def test_explicit_128k_window_keeps_recent_tools_and_separates_output_reserve(tmp_path):
    conv = conversation_with_old_work()
    client = SummaryClient()
    threshold = compute_compact_threshold(128_000, policy=DEEPSEEK_CONTEXT_POLICY,
                                          output_reserve_tokens=64_000)
    assert threshold + 64_000 + 13_000 <= 128_000
    result = await auto_compact(conv, client, 128_000, tmp_path, policy=DEEPSEEK_CONTEXT_POLICY,
                                fixed_prefix_tokens=20_000, output_reserve_tokens=64_000)
    assert isinstance(result, CompactEvent)
    assert result.before_tokens == 85_000  # Reserve is not counted as input.
    assert result.output_reserve_tokens == 64_000
    assert result.after_tokens < threshold
    assert any(r.tool_use_id == "recent" for m in result.boundary.keep for r in m.tool_results)
    assert any(c.tool_use_id == "recent" for m in result.boundary.keep for c in m.tool_uses)
    assert "85,000 →" in result.message and "输出预留 64,000" in result.message
    conv.record_usage_anchor(35_157, 125, 2_048)
    conv.add_user_message("new observation " * 400)
    assert await auto_compact(conv, client, 128_000, tmp_path, policy=DEEPSEEK_CONTEXT_POLICY,
                             fixed_prefix_tokens=20_000, output_reserve_tokens=64_000) is None
    assert client.calls == 1


@pytest.mark.asyncio
async def test_reinjected_host_context_participates_in_validation_and_preserves_original_on_failure(tmp_path):
    conv = conversation_with_old_work()
    checkpoint = {"auto_review_checkpoint": {"user_context": "ORIGINAL", "denied": ["must-stay"]}}
    conv.history[-1].tool_results[0].host_observation = copy.deepcopy(checkpoint)
    original = copy.deepcopy(conv.history)
    client, breaker = SummaryClient(), CompactCircuitBreaker()

    def restore(candidate):
        candidate.inject_environment("required skill and environment " * 12_000)

    result = await auto_compact(conv, client, 128_000, tmp_path, policy=DEEPSEEK_CONTEXT_POLICY,
                                fixed_prefix_tokens=20_000, output_reserve_tokens=64_000,
                                restore_context=restore, breaker=breaker)
    assert isinstance(result, CompactBlocked)
    assert conv.history == original and conv.baseline_tokens == 85_000
    assert conv.history[-1].tool_results[0].host_observation == checkpoint
    # Retrying unchanged input does not call the summary model again.
    again = await auto_compact(conv, client, 128_000, tmp_path, policy=DEEPSEEK_CONTEXT_POLICY,
                               fixed_prefix_tokens=20_000, output_reserve_tokens=64_000,
                               restore_context=restore, breaker=breaker)
    assert isinstance(again, CompactBlocked) and client.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "x" * 165_000], ids=["empty", "no-reduction"])
async def test_empty_or_nonshrinking_summary_is_not_adopted_and_new_input_can_retry(tmp_path, text):
    conv = conversation_with_old_work()
    conv.record_usage_anchor(200_000)
    original = copy.deepcopy(conv.history)
    client, breaker = SummaryClient(text), CompactCircuitBreaker()
    result = await auto_compact(conv, client, 200_000, tmp_path, breaker=breaker)
    assert isinstance(result, CompactBlocked)
    assert conv.history == original
    assert await auto_compact(conv, client, 200_000, tmp_path, breaker=breaker) == result
    rejected_calls = 3 if text else 1
    assert client.calls == rejected_calls
    conv.add_user_message("Clarified scope: keep only the current deliverable.")
    client.text = "The clarified requirement and remaining work."
    result = await auto_compact(conv, client, 200_000, tmp_path, breaker=breaker)
    assert isinstance(result, CompactEvent) and client.calls == rejected_calls + 1
    assert not breaker.blocked_fingerprint


@pytest.mark.asyncio
async def test_verbose_summary_retries_without_replacing_history_or_copying_host_catalog(tmp_path):
    conv = conversation_with_old_work()
    conv.inject_environment("REGENERATED_CATALOG " * 1_000)
    original = copy.deepcopy(conv.history)
    recent = copy.deepcopy(conv.history[-1])

    class RetryingSummary(SummaryClient):
        async def stream(self, conversation, system="", tools=None):
            self.calls += 1
            assert conv.history == original  # No draft is adopted before validation.
            assert not any("REGENERATED_CATALOG" in m.content for m in conversation.history)
            assert any("old-0:" in m.content for m in conversation.history)
            assert "最多" in conversation.history[-1].content
            if self.calls == 1:
                yield TextDelta("<summary>" + "oversized rejected draft " * 10_000 + "</summary>")
            else:
                assert not any("oversized rejected draft" in m.content for m in conversation.history)
                yield TextDelta("<summary>Preserve requested scope. Existing results are saved; continue current revision 14.</summary>")
            yield StreamEnd("end_turn", input_tokens=100, output_tokens=20)

    client = RetryingSummary()
    result = await auto_compact(conv, client, 100_000, tmp_path,
        restore_context=lambda candidate: candidate.inject_environment("REGENERATED_CATALOG " * 1_000))
    assert isinstance(result, CompactEvent) and client.calls == 2
    assert result.after_tokens < 40_000
    assert recent in conv.history
    assert sum(m.context_kind == "environment" for m in conv.history) == 1


@pytest.mark.asyncio
async def test_plain_chat_tool_guidance_survives_real_agent_compaction(tmp_path):
    from types import SimpleNamespace
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.workflow_core.chat_tools import install

    conv = conversation_with_old_work()
    agent = Agent(SummaryClient(), ToolRegistry(), "openai-compat", str(tmp_path),
                  context_window=200_000, auxiliary_client=SummaryClient())
    agent.permission_checker = SimpleNamespace(task_id="current-task")
    runtime = SimpleNamespace(agent=agent, conversation=conv, conversation_id="chat", work_dir=str(tmp_path),
        session=SimpleNamespace(meta=SimpleNamespace(dialogue_focus=dict(
            kind="chat", task_id="current-task", task="行测章节"))))
    install(SimpleNamespace(EXPERT_RUNS=ExpertRunStore(tmp_path / 'runs', storage='sqlite')), runtime)
    # Simulate an earlier compaction losing the original tool reminder.
    conv.history = [m for m in conv.history if m.context_kind != "chat-workflow-tools"]
    result = await agent.manual_compact(conv)
    assert isinstance(result, CompactNotification)
    restored = [m for m in conv.history if m.context_kind == "chat-workflow-tools"]
    assert len(restored) == 1 and "UpdateWorkflowTasks" in restored[0].content
    assert "SaveWorkflow" in restored[0].content
    assert "节点内部的读取与断点" in restored[0].content
    assert "按主题与用途去重整合" in restored[0].content
    assert "用户明确要求逐页核验" in restored[0].content


@pytest.mark.asyncio
async def test_usable_headroom_accepts_saving_below_five_percent_of_fixed_prompt(tmp_path):
    from mewcode.conversation import estimate_tokens

    conv = ConversationManager()
    conv.add_user_message("old task facts " * 800)
    for i in range(5):
        conv.history.append(Message("assistant", f"recent checkpoint {i}"))
    conv.record_usage_anchor(180_000)
    before_estimate = estimate_tokens(conv.history) + 120_000
    result = await auto_compact(conv, SummaryClient(), 200_000, tmp_path,
                                fixed_prefix_tokens=120_000)
    assert isinstance(result, CompactEvent)
    assert 512 <= before_estimate - result.after_tokens < before_estimate * 0.05
    assert result.after_tokens < compute_compact_threshold(200_000) * 0.90


@pytest.mark.asyncio
async def test_impossible_reservation_stops_before_summary_without_destroying_history(tmp_path):
    conv = conversation_with_old_work()
    original = copy.deepcopy(conv.history)
    client = SummaryClient()
    result = await auto_compact(conv, client, 64_000, tmp_path, policy=DEEPSEEK_CONTEXT_POLICY,
                                output_reserve_tokens=64_000)
    assert isinstance(result, CompactBlocked)
    assert client.calls == 0 and conv.history == original


@pytest.mark.asyncio
async def test_persisted_tool_files_survive_compaction_and_remain_readable(tmp_path):
    conv = conversation_with_old_work()
    conv.history[-1].tool_results[0].content = "retained bytes " * 5_000
    state = create_replacement_state()
    directory = tmp_path / "tool-results"
    directory.mkdir()
    apply_tool_result_budget(conv, directory, state)
    backing = directory / "recent.txt"
    original = backing.read_bytes()
    # Shared directory can also contain older transcript / sibling-agent evidence.
    archived = directory / "other-agent.txt"
    archived.write_text("previous receipt", encoding="utf-8")
    conv.record_usage_anchor(200_000)
    result = await auto_compact(conv, SummaryClient(), 200_000, directory)
    assert isinstance(result, CompactEvent)
    view, _ = apply_tool_result_budget(conv, directory, state)
    assert any(str(backing) in r.content for m in view.history for r in m.tool_results)
    assert backing.read_bytes() == original
    assert archived.read_text(encoding="utf-8") == "previous receipt"


@pytest.mark.asyncio
@pytest.mark.parametrize("completion", [False, True])
async def test_agent_stops_before_business_provider_when_compaction_cannot_make_room(tmp_path, completion):
    main, summary = SummaryClient("must never be called"), SummaryClient()
    agent = Agent(main, ToolRegistry(), "openai-compat", str(tmp_path),
                  context_window=200_000, auxiliary_client=summary, max_iterations=2)
    agent._skill_catalog = "required host catalog " * 40_000
    conv = conversation_with_old_work()
    conv.record_usage_anchor(200_000)
    if completion:
        with pytest.raises(RuntimeError, match="上下文压缩未能腾出"):
            await agent.run_to_completion("", conv)
    else:
        events = [event async for event in agent.run(conv)]
        assert any(isinstance(e, ErrorEvent) and e.kind == "context_compaction" for e in events)
        assert not any(isinstance(e, CompactNotification) for e in events)
    assert main.calls == 0
    assert any("CURRENT_REVISION_14" in r.content for m in conv.history for r in m.tool_results)


@pytest.mark.asyncio
async def test_manual_agent_compaction_reports_full_restored_size_without_duplicate_host_context(tmp_path):
    main, summary = SummaryClient(), SummaryClient()
    agent = Agent(main, ToolRegistry(), "openai-compat", str(tmp_path),
                  context_window=200_000, auxiliary_client=summary,
                  instructions_content="Preserve this mandatory instruction.")
    conv = conversation_with_old_work()
    conv.inject_environment("old environment")
    conv.inject_long_term_memory(agent.instructions_content, "")
    result = await agent.manual_compact(conv)
    assert isinstance(result, CompactNotification)
    assert result.after_tokens == conv.current_tokens() + agent._compact_fixed_prefix_tokens()
    assert sum(m.context_kind == "environment" for m in conv.history) == 1
    assert sum(m.context_kind == "long_term_memory" for m in conv.history) == 1
    assert sum(m.context_kind == "runtime" for m in conv.history) == 1
    assert "Preserve this mandatory instruction." in "\n".join(m.content for m in conv.history)
