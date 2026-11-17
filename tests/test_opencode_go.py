from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from mewcode.client import AnthropicClient, OpenAIClient, OpenAICompatClient, create_client
from mewcode.agent import Agent
from mewcode.config import ProviderConfig
from mewcode.config import load_config
from mewcode.model_capabilities import estimate_request_cost_usd
from mewcode.model_registry import ModelRef, ProviderRegistry
from mewcode.opencode_go import (
    OPENCODE_GO_DEFAULT_MODEL,
    OPENCODE_GO_MODEL_ROUTES,
    OPENCODE_GO_ROOT,
    ordered_go_model_ids,
    resolve_effective_provider_config,
)
from mewcode.provider_sources import ProviderSlot
from mewcode.tools import ToolRegistry
from mewcode.tools.base import StreamEnd
from mewcode.usage.ledger import UsageLedger


def _go_provider(model: str = OPENCODE_GO_DEFAULT_MODEL, api_key: str = "go-secret"):
    return ProviderConfig(
        name="My Go subscription",
        provider_type="opencode-go",
        protocol="openai-compat",
        base_url=OPENCODE_GO_ROOT,
        model=model,
        api_key=api_key,
    )


@pytest.mark.parametrize(
    "model,protocol,base_url",
    [
        ("deepseek-v4-flash", "openai-compat", f"{OPENCODE_GO_ROOT}/v1"),
        ("deepseek-v4-pro", "openai-compat", f"{OPENCODE_GO_ROOT}/v1"),
        ("deepseek-v4-flash-vision-exp", "openai-compat", f"{OPENCODE_GO_ROOT}/v1"),
        ("muse-spark-1.2-contributor", "openai", f"{OPENCODE_GO_ROOT}/v1"),
        ("kimi-k3", "openai-compat", f"{OPENCODE_GO_ROOT}/v1"),
        ("glm-5.3", "openai-compat", f"{OPENCODE_GO_ROOT}/v1"),
        ("glm-5.2", "openai-compat", f"{OPENCODE_GO_ROOT}/v1"),
        ("mimo-v2.5", "openai-compat", f"{OPENCODE_GO_ROOT}/v1"),
        ("hy3", "openai-compat", f"{OPENCODE_GO_ROOT}/v1"),
        ("ox-alpha-free", "openai-compat", f"{OPENCODE_GO_ROOT}/v1"),
        ("grok-4.5", "openai", f"{OPENCODE_GO_ROOT}/v1"),
        ("gpt-5.6-luna", "openai", f"{OPENCODE_GO_ROOT}/v1"),
        ("minimax-m3", "anthropic", OPENCODE_GO_ROOT),
        ("qwen3.8-max", "anthropic", OPENCODE_GO_ROOT),
    ],
)
def test_effective_route_uses_model_protocol_and_sdk_base(model, protocol, base_url):
    effective = resolve_effective_provider_config(_go_provider(), model)
    assert effective.protocol == protocol
    assert effective.base_url == base_url
    assert effective.model == model


def test_effective_client_class_changes_with_go_model():
    assert isinstance(create_client(_go_provider("deepseek-v4-flash")), OpenAICompatClient)
    assert isinstance(create_client(_go_provider("gpt-5.6-luna")), OpenAIClient)
    assert isinstance(create_client(_go_provider("grok-4.5")), OpenAIClient)
    assert isinstance(create_client(_go_provider("muse-spark-1.2-contributor")), OpenAIClient)
    assert isinstance(create_client(_go_provider("minimax-m3")), AnthropicClient)


def test_go_fallback_catalog_contains_new_routes_and_keeps_deepseek_first():
    models = ordered_go_model_ids()
    deepseek_models = [model for model in models if model.startswith("deepseek-")]
    assert models[0] == OPENCODE_GO_DEFAULT_MODEL
    assert models[: len(deepseek_models)] == deepseek_models
    assert {
        "glm-5.3",
        "deepseek-v4-flash-vision-exp",
        "muse-spark-1.2-contributor",
        "ox-alpha-free",
    } <= set(models)
    assert OPENCODE_GO_MODEL_ROUTES["grok-4.5"].protocol == "openai"


def test_go_routes_carry_the_vendors_published_context_windows():
    """Pin the researched windows so a silent edit becomes a deliberate one.

    Values were read from each vendor's own documentation on 2026-09-10; the
    per-model comments in ``opencode_go`` name the source.  Every routable
    model needs a non-zero window, otherwise the provider falls back to a
    generic guess for models that publish a larger context.
    """
    expected = {
        "deepseek-v4.1-flash": 1_000_000,
        "deepseek-v4-flash": 1_000_000,
        "deepseek-v4-pro": 1_000_000,
        "deepseek-v4-flash-vision-exp": 1_000_000,
        "gpt-5.6-luna": 1_050_000,
        "muse-spark-1.2-contributor": 1_048_576,
        "kimi-k3": 1_000_000,
        "kimi-k2.7-code": 256_000,
        "kimi-k2.6": 256_000,
        "kimi-k2.5": 256_000,
        "glm-5.3": 1_000_000,
        "glm-5.2": 1_000_000,
        "glm-5.1": 200_000,
        "glm-5": 200_000,
        "mimo-v2.5-pro": 1_000_000,
        "mimo-v2.5": 1_000_000,
        "mimo-v2-pro": 1_000_000,
        "mimo-v2-omni": 256_000,
        "hy3": 256_000,
        "hy3-preview": 256_000,
        "ox-alpha-free": 1_048_576,
        "grok-4.5": 500_000,
        "minimax-m3": 1_000_000,
        "minimax-m2.7": 204_800,
        "minimax-m2.5": 204_800,
        "qwen3.8-max": 1_000_000,
        "qwen3.7-max": 1_000_000,
        "qwen3.7-plus": 1_000_000,
        "qwen3.6-plus": 1_000_000,
        "qwen3.5-plus": 1_000_000,
    }
    assert set(expected) == set(OPENCODE_GO_MODEL_ROUTES)
    assert {model: route.context_window for model, route in OPENCODE_GO_MODEL_ROUTES.items()} == expected
    # The resolved provider must hand the route window to the agent.
    for model, window in expected.items():
        effective = resolve_effective_provider_config(_go_provider(), model)
        assert effective.get_context_window() == window, model


def test_gpt_5_6_luna_uses_the_window_not_the_long_prompt_price_threshold():
    """272K is OpenAI's 2x-pricing threshold, not the 1.05M context window."""
    route = OPENCODE_GO_MODEL_ROUTES["gpt-5.6-luna"]
    assert route.context_window > 272_000
    assert resolve_effective_provider_config(_go_provider(), "gpt-5.6-luna").get_context_window() == 1_050_000


def test_unknown_go_model_cannot_be_resolved():
    with pytest.raises(ValueError, match="missing routing metadata"):
        resolve_effective_provider_config(_go_provider(), "future-model")


def test_go_api_key_supports_empty_fallback_and_explicit_env_reference(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "from-opencode-env")
    assert _go_provider(api_key="").resolve_api_key() == "from-opencode-env"
    assert _go_provider(api_key="${OPENCODE_API_KEY}").resolve_api_key() == "from-opencode-env"


def test_yaml_provider_type_and_env_reference_are_loaded(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "yaml-secret")
    path = tmp_path / "config.yaml"
    path.write_text(
        """providers:
  - name: opencode-go
    provider_type: opencode-go
    protocol: openai-compat
    base_url: https://opencode.ai/zen/go
    model: deepseek-v4-flash
    api_key: ${OPENCODE_API_KEY}
""",
        encoding="utf-8",
    )
    provider = load_config(path).providers[0]
    assert provider.provider_type == "opencode-go"
    assert provider.resolve_api_key() == "yaml-secret"


def test_go_deepseek_keeps_deepseek_context_and_usage_source():
    effective = resolve_effective_provider_config(_go_provider("deepseek-v4-flash"))
    client = create_client(effective)
    assert client.capabilities.context_channel == "deepseek"
    assert client.capabilities.requires_reasoning_content_for_tools is True
    assert client._usage_provider_name == "OpenCode Go"
    assert client._usage_protocol == "openai-compat"


def test_desktop_go_profile_is_publicly_typed_but_secret_free():
    public = ProviderRegistry().replace_desktop_profiles([{
        "id": "go-main",
        "brand": "opencode-go",
        "api_key": "never-public",
    }])[0]
    assert public["provider_type"] == "opencode-go"
    assert public["base_url"] == OPENCODE_GO_ROOT
    assert public["default_model"] == "deepseek-v4-flash"
    assert public["has_secret"] is True
    assert "api_key" not in public


def test_go_deepseek_costs_split_cached_uncached_and_output_tokens():
    flash = resolve_effective_provider_config(_go_provider("deepseek-v4-flash"))
    pro = resolve_effective_provider_config(_go_provider("deepseek-v4-pro"))
    flash_client = create_client(flash)
    pro_client = create_client(pro)
    assert estimate_request_cost_usd(
        flash_client.capabilities,
        cache_hit_tokens=1_000_000,
        cache_miss_tokens=1_000_000,
        output_tokens=1_000_000,
    ) == pytest.approx(0.4228)
    assert estimate_request_cost_usd(
        pro_client.capabilities,
        cache_hit_tokens=1_000_000,
        cache_miss_tokens=1_000_000,
        output_tokens=1_000_000,
    ) == pytest.approx(1.308625)


def test_go_usage_ledger_record_contains_only_local_cost_estimate():
    client = create_client(_go_provider("deepseek-v4-flash"))
    records = []
    client.set_usage_recorder(records.append)
    client._record_stream_end(StreamEnd(
        stop_reason="stop",
        input_tokens=1_000_000,
        cache_read=1_000_000,
        output_tokens=1_000_000,
        cache_usage_available=True,
    ))
    record = records[0]
    assert record.provider_name == "OpenCode Go"
    assert record.metadata["estimated_cost_usd"] == pytest.approx(0.4228)
    assert record.metadata["cost_basis"] == "local_estimate"
    assert "remaining" not in record.metadata


def test_go_ledger_exposes_local_estimate_without_quota_fields(tmp_path: Path):
    client = create_client(_go_provider("deepseek-v4-pro"))
    ledger = UsageLedger(tmp_path)
    client.set_usage_recorder(ledger.append)
    client._record_stream_end(StreamEnd(
        stop_reason="stop",
        input_tokens=1_000,
        cache_read=9_000,
        output_tokens=500,
        cache_usage_available=True,
    ))
    item = ledger.recent(provider_name="OpenCode Go")[0]
    assert item["provider_name"] == "OpenCode Go"
    assert item["estimated_cost_usd"] == pytest.approx(0.000902625)
    assert item["cost_basis"] == "local_estimate"
    assert "quota" not in item


class _CatalogClient:
    def __init__(self, models):
        self.models = models

    async def list_models(self):
        if isinstance(self.models, Exception):
            raise self.models
        return self.models

    def set_usage_recorder(self, _recorder):
        return None

    def set_effort(self, _effort):
        return None


@pytest.mark.asyncio
async def test_remote_go_catalog_is_authoritative_and_unknown_models_are_disabled(monkeypatch):
    registry = ProviderRegistry(catalog_ttl=0)
    registry.refresh_discovered([ProviderSlot(provider=_go_provider(), source="config")])
    seen = []

    def fake_create(config):
        seen.append(config)
        return _CatalogClient(["minimax-m3", "future-model", "deepseek-v4-pro"])

    monkeypatch.setattr("mewcode.model_registry.create_client", fake_create)
    catalog = await registry.catalog()
    models = catalog["models"]
    by_id = {item["model_id"]: item for item in models}

    assert seen[0].base_url == f"{OPENCODE_GO_ROOT}/v1"
    assert [item["model_id"] for item in models] == [
        "deepseek-v4-pro", "minimax-m3", "future-model"
    ]
    assert by_id["deepseek-v4-pro"]["protocol"] == "openai-compat"
    assert by_id["minimax-m3"]["protocol"] == "anthropic"
    assert by_id["future-model"]["available"] is False
    assert "routing metadata" in by_id["future-model"]["unavailable_reason"]
    provider = catalog["providers"][0]
    assert provider["models_authoritative"] is True
    assert provider["protocol_mode"] == "per-model"
    assert provider["catalog_stale"] is False


@pytest.mark.asyncio
async def test_remote_go_catalog_reports_new_routes_and_vision_capability(monkeypatch):
    registry = ProviderRegistry(catalog_ttl=0)
    registry.refresh_discovered([ProviderSlot(provider=_go_provider(), source="config")])

    def fake_create(_config):
        return _CatalogClient([
            "glm-5.3",
            "deepseek-v4-flash-vision-exp",
            "muse-spark-1.2-contributor",
            "ox-alpha-free",
            "grok-4.5",
        ])

    monkeypatch.setattr("mewcode.model_registry.create_client", fake_create)
    catalog = await registry.catalog()
    by_id = {item["model_id"]: item for item in catalog["models"]}

    assert {
        "glm-5.3",
        "deepseek-v4-flash-vision-exp",
        "muse-spark-1.2-contributor",
        "ox-alpha-free",
        "grok-4.5",
    } <= set(by_id)
    assert by_id["glm-5.3"]["protocol"] == "openai-compat"
    assert by_id["deepseek-v4-flash-vision-exp"]["protocol"] == "openai-compat"
    assert by_id["deepseek-v4-flash-vision-exp"]["vision"] is True
    assert by_id["muse-spark-1.2-contributor"]["protocol"] == "openai"
    assert by_id["ox-alpha-free"]["protocol"] == "openai-compat"
    assert by_id["grok-4.5"]["protocol"] == "openai"


@pytest.mark.asyncio
async def test_failed_go_catalog_falls_back_to_stale_builtin_catalog(monkeypatch):
    registry = ProviderRegistry(catalog_ttl=0)
    registry.refresh_discovered([ProviderSlot(provider=_go_provider(), source="config")])
    monkeypatch.setattr(
        "mewcode.model_registry.create_client",
        lambda _config: _CatalogClient(RuntimeError("catalog offline")),
    )

    catalog = await registry.catalog()
    provider = catalog["providers"][0]
    assert catalog["models"][0]["model_id"] == "deepseek-v4-flash"
    assert catalog["models"][1]["model_id"] == "deepseek-v4-pro"
    assert provider["available"] is True
    assert provider["models_authoritative"] is False
    assert provider["catalog_stale"] is True
    assert "catalog offline" in provider["catalog_error"]


def test_registry_rebuilds_protocol_client_for_each_go_model(monkeypatch):
    registry = ProviderRegistry()
    registry.refresh_discovered([ProviderSlot(provider=_go_provider(), source="config")])
    captured = []

    def fake_create(config):
        captured.append(config)
        return _CatalogClient(None)

    monkeypatch.setattr("mewcode.model_registry.create_client", fake_create)
    minimax = registry.resolve(ModelRef("config:My Go subscription", "minimax-m3"))
    luna = registry.resolve(ModelRef("config:My Go subscription", "gpt-5.6-luna"))

    assert minimax.protocol == "anthropic"
    assert minimax.config.base_url == OPENCODE_GO_ROOT
    assert luna.protocol == "openai"
    assert luna.config.base_url == f"{OPENCODE_GO_ROOT}/v1"
    assert [config.protocol for config in captured] == ["anthropic", "openai"]


def test_go_protocol_switch_starts_a_new_agent_cache_epoch(tmp_path: Path):
    deepseek = resolve_effective_provider_config(_go_provider("deepseek-v4-flash"))
    minimax = resolve_effective_provider_config(deepseek, "minimax-m3")
    deepseek_client = create_client(deepseek)
    minimax_client = create_client(minimax)
    agent = Agent(
        client=deepseek_client,
        registry=ToolRegistry(),
        protocol=deepseek.protocol,
        work_dir=str(tmp_path),
        context_window=deepseek.get_context_window(),
    )
    old_epoch = agent.cache_epoch
    agent.configure_model(
        minimax_client,
        protocol=minimax.protocol,
        context_window=minimax.get_context_window(),
        reason="model_changed",
    )
    assert agent.cache_epoch == old_epoch + 1
    assert agent.protocol == "anthropic"
    assert agent.context_channel == "generic"


@pytest.mark.skipif(not os.environ.get("OPENCODE_API_KEY"), reason="no OpenCode Go key")
@pytest.mark.asyncio
async def test_live_go_catalog_smoke():
    client = create_client(_go_provider(api_key="${OPENCODE_API_KEY}"))
    models = await client.list_models()
    assert models and "deepseek-v4-flash" in models


def _sdk_default_headers(client) -> dict[str, str]:
    return {k.lower(): v for k, v in client._client.default_headers.items()}


def test_go_clients_always_carry_the_session_headers():
    """The gateway rejects sessionless requests with MissingSessionID (400)."""
    for model in ("deepseek-v4-flash", "minimax-m3", "gpt-5.6-luna"):
        headers = _sdk_default_headers(create_client(_go_provider(model)))
        session = headers.get("x-opencode-session", "")
        assert session, model
        # UUID 可解析：值形态稳定，且进程内兜底会话保持不变。
        uuid.UUID(session)
        assert headers.get("x-opencode-client") == "MewCode/0.1"


def test_non_go_providers_do_not_get_session_headers():
    provider = ProviderConfig(
        name="plain relay",
        protocol="openai-compat",
        base_url="https://relay.example.com/v1",
        model="glm-5.3",
        api_key="secret",
    )
    headers = _sdk_default_headers(create_client(provider))
    assert "x-opencode-session" not in headers
    assert "x-opencode-client" not in headers


def test_explicit_session_header_config_wins_over_default():
    provider = _go_provider()
    provider.default_headers = {"x-opencode-session": "configured-session"}
    headers = _sdk_default_headers(create_client(provider))
    assert headers["x-opencode-session"] == "configured-session"


def test_stream_kwargs_prefer_the_current_session_scope():
    from mewcode.client import _apply_opencode_go_session_headers
    from mewcode.usage.service import usage_trace_scope

    kwargs: dict[str, Any] = {}
    with usage_trace_scope(session_id="conversation-42"):
        _apply_opencode_go_session_headers(kwargs, f"{OPENCODE_GO_ROOT}/v1")
    assert kwargs["extra_headers"]["x-opencode-session"] == "conversation-42"

    kwargs = {}
    _apply_opencode_go_session_headers(kwargs, f"{OPENCODE_GO_ROOT}/v1")
    uuid.UUID(kwargs["extra_headers"]["x-opencode-session"])

    kwargs = {}
    _apply_opencode_go_session_headers(kwargs, "https://relay.example.com/v1")
    assert "extra_headers" not in kwargs
