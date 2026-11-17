from __future__ import annotations

import pytest

from mewcode.context.manager import compute_compact_threshold
from mewcode.agent import Agent
from mewcode.context.routing import (
    ANTHROPIC_MODERN_CONTEXT_POLICY,
    CONTEXT_POLICY_SLOT,
    DEEPSEEK_CONTEXT_POLICY,
    GEMINI_LONG_CONTEXT_POLICY,
    GENERIC_CONTEXT_POLICY,
    OPENAI_GPT_56_CONTEXT_POLICY,
    ContextPolicy,
    ContextRouteRequest,
    ContextRouter,
    context_router_plugin,
)
from mewcode.cordis import Context
from mewcode.model_capabilities import resolve_model_capabilities


def test_router_matches_builtin_models_and_falls_back() -> None:
    router = ContextRouter()

    assert router.resolve(ContextRouteRequest(model="gpt-5.6-sol")).name == (
        "openai-gpt-5.6"
    )
    assert router.resolve("gpt-6-sol").name == "openai-gpt-6"
    assert router.resolve("gpt-6-luna-latest").name == "openai-gpt-6"
    assert router.resolve("gpt-6-astra").name == "openai-gpt-6-astra"
    assert router.resolve(ContextRouteRequest(model="claude-sonnet-4")).name == (
        "anthropic-modern"
    )
    assert router.resolve(ContextRouteRequest(model="gemini-2.5-pro")).name == (
        "gemini-long-context"
    )
    assert router.resolve(ContextRouteRequest(model="deepseek-v4-pro")).name == (
        "deepseek"
    )
    assert router.resolve("openrouter/deepseek-v4-pro:free").name == "deepseek"
    assert router.resolve("deepseek:deepseek-v4-flash").name == "deepseek"
    assert router.resolve(ContextRouteRequest(model="vendor/unknown-model")).name == (
        "generic"
    )


def test_router_priority_wins_over_registration_order() -> None:
    low = ContextPolicy(
        name="low",
        priority=10,
        matcher=lambda request: request.model.startswith("custom"),
    )
    high = ContextPolicy(
        name="high",
        priority=20,
        matcher=lambda request: request.model.startswith("custom"),
    )
    assert ContextRouter((low, high)).resolve("custom-model").name == "high"


def test_explicit_generic_channel_overrides_deepseek_detection() -> None:
    capabilities = resolve_model_capabilities(
        "deepseek-v4-pro", context_channel="generic"
    )
    request = ContextRouteRequest(
        model="deepseek-v4-pro",
        context_channel=capabilities.context_channel,
        capabilities=capabilities,
    )
    assert ContextRouter().resolve(request) is GENERIC_CONTEXT_POLICY


def test_builtin_policy_fields_cover_cache_and_compaction_controls() -> None:
    assert GENERIC_CONTEXT_POLICY.context_channel == "generic"
    assert DEEPSEEK_CONTEXT_POLICY.context_channel == "deepseek"
    assert DEEPSEEK_CONTEXT_POLICY.stable_system is True
    assert DEEPSEEK_CONTEXT_POLICY.stable_tools is True
    assert DEEPSEEK_CONTEXT_POLICY.move_hook_prompts_to_history is True
    assert DEEPSEEK_CONTEXT_POLICY.compact_trigger_ratio == 0.85
    assert OPENAI_GPT_56_CONTEXT_POLICY.compute_fixed_prefix is True
    assert OPENAI_GPT_56_CONTEXT_POLICY.compact_trigger_tokens is None
    assert OPENAI_GPT_56_CONTEXT_POLICY.effort_affects_cache_affinity is True
    assert ANTHROPIC_MODERN_CONTEXT_POLICY.stable_system is True
    assert GEMINI_LONG_CONTEXT_POLICY.keep_max_tokens == 64_000


@pytest.mark.parametrize("model", ["gpt-5.6-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6.1-sol", "gpt-6-luna"])
def test_gpt_long_context_is_not_compacted_at_the_272k_pricing_boundary(model: str) -> None:
    policy = ContextRouter().resolve(model)
    assert compute_compact_threshold(1_050_000, policy=policy) == 892_500
    assert compute_compact_threshold(272_000, policy=policy) == 231_200


def test_context_router_plugin_provides_service_and_policy_slot() -> None:
    ctx = Context()
    ctx.load(context_router_plugin())

    router = ctx.get("context_router")
    assert isinstance(router, ContextRouter)
    assert isinstance(ctx.slots.resolve(CONTEXT_POLICY_SLOT), list)
    assert router.resolve("gpt-5.6").name == "openai-gpt-5.6"

    extension = ContextPolicy(
        name="custom",
        priority=1_000,
        matcher=lambda request: request.model == "custom-model",
    )
    ctx.slots.register(CONTEXT_POLICY_SLOT, extension, item_key=extension.name)
    assert router.resolve("custom-model") is extension
    ctx.close()


def test_default_compact_threshold_is_unchanged() -> None:
    assert compute_compact_threshold(200_000) == 200_000 - 20_000 - 13_000
    assert compute_compact_threshold(
        1_000_000,
        trigger_ratio=0.85,
    ) == 850_000


def test_openai_long_context_compaction_uses_the_selected_window() -> None:
    assert compute_compact_threshold(
        1_000_000, policy=OPENAI_GPT_56_CONTEXT_POLICY
    ) == 850_000


def test_custom_effort_policy_is_immutable() -> None:
    policy = ContextPolicy(name="test", effort_affects_cache_affinity=True)
    with pytest.raises(AttributeError):
        policy.name = "changed"  # type: ignore[misc]


def test_agent_effort_notification_starts_policy_cache_epoch() -> None:
    agent = object.__new__(Agent)
    agent.context_policy = ANTHROPIC_MODERN_CONTEXT_POLICY
    agent.cache_epoch = 4
    agent.cache_epoch_reason = "session_start"
    agent._last_request_shape = object()

    assert agent.note_effort_changed("high", "max") is True
    assert agent.cache_epoch == 5
    assert agent.cache_epoch_reason == "effort_changed"
    assert agent._last_request_shape is None
    assert agent.note_effort_changed("max", "max") is False
