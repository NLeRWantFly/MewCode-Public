from __future__ import annotations

import pytest

from mewcode.validator import ConfigError, validate_providers, validate_usage_config


def _provider(**overrides):
    value = {
        "name": "openai",
        "protocol": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-5.6-sol",
    }
    value.update(overrides)
    return value


def test_provider_accepts_service_tier() -> None:
    provider = validate_providers([_provider(service_tier="fast")])[0]
    assert provider["service_tier"] == "fast"


def test_provider_rejects_unknown_service_tier() -> None:
    with pytest.raises(ConfigError, match="service_tier"):
        validate_providers([_provider(service_tier="turbo")])


def test_usage_config_accepts_openai_account_binding() -> None:
    value = validate_usage_config(
        {
            "enabled": True,
            "account_tracking": True,
            "admin_api_key_env": "OPENAI_ADMIN_KEY",
            "admin_base_url": "https://api.openai.com/v1",
            "upstream_organization_id": "org_real",
            "project_ids": ["proj_mewcode"],
            "api_key_ids": ["key_mewcode"],
            "attribution_mode": "byok-relay",
        },
        provider_index=1,
    )

    assert value["account_tracking"] is True
    assert value["project_ids"] == ["proj_mewcode"]
    assert value["attribution_mode"] == "byok-relay"


@pytest.mark.parametrize(
    "raw",
    [
        {"enabled": "yes"},
        {"project_ids": "proj_not_a_list"},
        {"attribution_mode": "trust-me"},
        {"admin_base_url": "file:///tmp/fake"},
    ],
)
def test_usage_config_rejects_ambiguous_account_claims(raw) -> None:
    with pytest.raises(ConfigError):
        validate_usage_config(raw, provider_index=1)
