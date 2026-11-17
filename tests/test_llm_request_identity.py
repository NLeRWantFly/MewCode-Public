"""LLM clients must identify as MewCode, not the generic Python SDK."""
from __future__ import annotations

from unittest.mock import Mock

import pytest

import mewcode.client as client_module
from mewcode.client import create_client
from mewcode.config import ProviderConfig


def _header(headers: dict[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


@pytest.mark.parametrize(
    "name,protocol",
    [
        ("AnthropicClient", "anthropic"),
        ("OpenAIClient", "openai"),
        ("OpenAICompatClient", "openai-compat"),
    ],
)
def test_sdk_clients_send_mewcode_user_agent(monkeypatch, name, protocol) -> None:
    factory = Mock()
    monkeypatch.setattr(client_module, "AsyncAnthropic", factory)
    monkeypatch.setattr(client_module, "AsyncOpenAI", factory)
    getattr(client_module, name)(
        ProviderConfig(
            name="relay",
            protocol=protocol,
            base_url="https://relay.example/v1",
            model="claude-sonnet-4-6",
            api_key="sk-test",
        )
    )
    headers = factory.call_args.kwargs["default_headers"]
    assert _header(headers, "User-Agent") == "MewCode/0.1"


def test_configured_user_agent_is_preserved(monkeypatch) -> None:
    factory = Mock()
    monkeypatch.setattr(client_module, "AsyncAnthropic", factory)
    client_module.AnthropicClient(
        ProviderConfig(
            name="relay",
            protocol="anthropic",
            base_url="https://relay.example/v1",
            model="claude-sonnet-4-6",
            api_key="sk-test",
            default_headers={"User-Agent": "MewCode-diagnostic/1"},
        )
    )
    headers = factory.call_args.kwargs["default_headers"]
    assert _header(headers, "User-Agent") == "MewCode-diagnostic/1"


def test_anthropic_sdk_does_not_restore_python_user_agent() -> None:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(
        api_key="sk-test",
        default_headers={"User-Agent": "MewCode/0.1"},
    )
    assert _header(dict(client.default_headers), "User-Agent") == "MewCode/0.1"


def test_openrouter_authorization_survives_identity_header(monkeypatch) -> None:
    captured: dict = {}

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(client_module, "AsyncOpenAI", FakeAsyncOpenAI)
    create_client(
        ProviderConfig(
            name="OpenRouter",
            protocol="openai-compat",
            base_url="https://openrouter.ai/api/v1",
            model="anthropic/claude-sonnet-4",
            api_key="sk-or-v1-test",
        )
    )
    headers = captured["default_headers"]
    assert headers["Authorization"] == "Bearer sk-or-v1-test"
    assert _header(headers, "User-Agent") == "MewCode/0.1"
