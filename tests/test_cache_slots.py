from types import SimpleNamespace

from mewcode.cache_slots import (
    CacheTotals,
    normalized_cache_usage,
    resolve_cache_usage_provider,
)


def test_deepseek_slot_distinguishes_missing_from_explicit_zero() -> None:
    slot = resolve_cache_usage_provider("deepseek", "openai-compat")

    missing = slot.extract_usage(
        SimpleNamespace(prompt_tokens=120, completion_tokens=5)
    )
    assert missing.available is False
    assert missing.hit_rate is None

    zero = slot.extract_usage(
        SimpleNamespace(
            prompt_tokens=120,
            model_extra={
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 120,
            },
        )
    )
    assert zero.available is True
    assert zero.hit_tokens == 0
    assert zero.miss_tokens == 120
    assert zero.hit_rate == 0.0


def test_deepseek_slot_reconciles_partial_relay_counters() -> None:
    slot = resolve_cache_usage_provider("deepseek", "openai-compat")
    metrics = slot.extract_usage(
        {"prompt_tokens": 1_000, "cache_hit_tokens": 900}
    )
    assert metrics.available is True
    assert metrics.hit_tokens == 900
    assert metrics.miss_tokens == 100
    assert metrics.hit_rate == 0.9


def test_deepseek_slot_accepts_openai_standard_cached_token_details() -> None:
    slot = resolve_cache_usage_provider("deepseek", "openai-compat")
    metrics = slot.extract_usage(
        SimpleNamespace(
            prompt_tokens=1_000,
            prompt_tokens_details=SimpleNamespace(cached_tokens=900),
        )
    )
    assert metrics.available is True
    assert metrics.hit_tokens == 900
    assert metrics.miss_tokens == 100
    assert metrics.hit_rate == 0.9


def test_anthropic_and_openai_slots_normalize_to_same_shape() -> None:
    anthropic = resolve_cache_usage_provider("generic", "anthropic").extract_usage(
        SimpleNamespace(
            input_tokens=100,
            cache_read_input_tokens=900,
            cache_creation_input_tokens=50,
        )
    )
    assert (anthropic.hit_tokens, anthropic.miss_tokens) == (900, 100)
    assert anthropic.creation_tokens == 50
    assert anthropic.available is True

    openai = resolve_cache_usage_provider("generic", "openai").extract_usage(
        SimpleNamespace(
            input_tokens=1_050,
            input_tokens_details=SimpleNamespace(
                cached_tokens=900,
                cache_write_tokens=50,
            ),
        )
    )
    assert (openai.hit_tokens, openai.miss_tokens) == (900, 100)
    assert openai.creation_tokens == 50
    assert openai.available is True
    assert anthropic.hit_rate == openai.hit_rate == 900 / 1_050


def test_deepseek_and_openai_slots_share_canonical_read_miss_output() -> None:
    deepseek = resolve_cache_usage_provider(
        "deepseek", "openai-compat"
    ).extract_usage({
        "prompt_tokens": 1_000,
        "prompt_cache_hit_tokens": 900,
        "prompt_cache_miss_tokens": 100,
    })
    openai = resolve_cache_usage_provider("generic", "openai").extract_usage(
        SimpleNamespace(
            input_tokens=1_000,
            input_tokens_details=SimpleNamespace(cached_tokens=900),
        )
    )

    assert deepseek == openai
    assert deepseek.prompt_tokens == (
        deepseek.hit_tokens
        + deepseek.miss_tokens
        + deepseek.creation_tokens
    )
    assert deepseek.hit_rate == 0.9


def test_cache_totals_keep_unknown_requests_out_of_provider_denominator() -> None:
    provider = resolve_cache_usage_provider("generic", "openai")
    totals = CacheTotals(provider.slot)
    totals.record(normalized_cache_usage(
        input_tokens=500,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        available=False,
    ))
    totals.record(normalized_cache_usage(
        input_tokens=100,
        cache_read_tokens=900,
        cache_creation_tokens=50,
        available=True,
    ))

    assert totals.available is True
    assert totals.reported_requests == 1
    assert totals.prompt_tokens == 1_050
    assert totals.hit_rate == 900 / 1_050
    assert totals.slot.provider == "openai"
