"""Credential rotation through the real SDK transport, without network calls."""
import base64
import json
import time
from contextlib import asynccontextmanager

import httpx
import pytest
from openai import AsyncOpenAI

from mewcode.client import AuthenticationError, OpenAIClient
from mewcode.codex_credentials import CodexLoginRequired, current_codex_headers
from mewcode.config import ProviderConfig
from mewcode.conversation import ConversationManager
from mewcode.provider_sources import _load_codex_slots


BASE = "https://chatgpt.com/backend-api/codex"


def save_login(path, token="first-token", account="selected-account"):
    path.write_text(json.dumps({"tokens": {"access_token": token, "account_id": account}}),
                    encoding="utf-8")


@asynccontextmanager
async def client_for(path, handler, *, dynamic=True, base_url=BASE):
    config = ProviderConfig("codex", "openai", base_url, "gpt-6.1-sol", api_key="startup-token",
                            default_headers={"ChatGPT-Account-Id": "selected-account"},
                            _codex_auth_path=str(path) if dynamic else "")
    client = OpenAIClient(config)
    await client._client.close()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        client._client = AsyncOpenAI(api_key="startup-token", base_url=base_url,
                                    http_client=transport, max_retries=0)
        yield client


async def consume(client):
    conversation = ConversationManager()
    conversation.add_user_message("Reply OK")
    return [event async for event in client.stream(conversation)]


def done():
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content='data: {"type":"response.output_text.delta","delta":"OK"}\n\ndata: [DONE]\n\n')


def expired():
    return httpx.Response(401, json={"error": {"code": "token_expired", "message": "Token expired"}})


@pytest.mark.asyncio
async def test_existing_client_uses_latest_login_for_each_request(tmp_path):
    path = tmp_path / "auth.json"
    save_login(path)
    seen = []
    def handler(request):
        seen.append((request.headers["Authorization"], request.headers["ChatGPT-Account-Id"]))
        return done()
    async with client_for(path, handler) as client:
        assert (await consume(client))[0].text == "OK"
        save_login(path, "refreshed-token")
        assert (await consume(client))[0].text == "OK"
        assert client._client.api_key == "startup-token"  # No shared mutation.
    assert seen == [("Bearer first-token", "selected-account"),
                    ("Bearer refreshed-token", "selected-account")]


@pytest.mark.asyncio
@pytest.mark.parametrize("rotates, succeeds, count", [(True, True, 2), (False, False, 1), (True, False, 2)])
async def test_401_retries_only_a_changed_token_once(tmp_path, rotates, succeeds, count):
    path = tmp_path / "auth.json"
    save_login(path)
    seen = []
    def handler(request):
        seen.append(request.headers["Authorization"])
        if len(seen) == 1 and rotates:
            save_login(path, "new-token")
        return done() if len(seen) == 2 and succeeds else expired()
    async with client_for(path, handler) as client:
        if succeeds:
            assert (await consume(client))[0].text == "OK"
        else:
            with pytest.raises(AuthenticationError, match="Codex/ChatGPT 登录认证失败") as error:
                await consume(client)
            assert error.value.status_code == 401
    assert len(seen) == count
    if rotates:
        assert seen[-1] == "Bearer new-token"


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["missing", "invalid", "expired", "account_changed", "wrong_route"])
async def test_invalid_login_fails_before_sending_or_serializing(tmp_path, monkeypatch, condition):
    path = tmp_path / "auth.json"
    save_login(path)
    if condition == "missing":
        path.unlink()
    elif condition == "invalid":
        path.write_text("{incomplete", encoding="utf-8")
    elif condition == "expired":
        payload = base64.urlsafe_b64encode(json.dumps({"exp": time.time()-10}).encode()).decode().rstrip("=")
        save_login(path, f"header.{payload}.signature")
    elif condition == "account_changed":
        save_login(path, account="different-account")
    def forbidden(*args, **kwargs):
        pytest.fail("No network request or history serialization is allowed")
    monkeypatch.setattr("mewcode.client.build_openai_input", forbidden)
    async with client_for(path, forbidden,
                          base_url="https://relay.invalid/v1" if condition == "wrong_route" else BASE) as client:
        with pytest.raises(AuthenticationError):
            await consume(client)


@pytest.mark.asyncio
async def test_static_provider_never_reads_or_substitutes_codex_credentials(tmp_path):
    seen = []
    def handler(request):
        seen.append(request.headers["Authorization"])
        return done()
    async with client_for(tmp_path / "missing.json", handler, dynamic=False) as client:
        await consume(client)
    assert seen == ["Bearer startup-token"]


@pytest.mark.parametrize("base, bound", [(BASE, True), ("https://relay.invalid/v1", False),
                                      ("http://chatgpt.com/backend-api/codex", False)])
def test_discovery_binds_only_the_selected_official_login(tmp_path, base, bound):
    folder = tmp_path / ".codex"
    folder.mkdir()
    (folder / "config.toml").write_text(
        'model = "gpt-6.1-sol"\n[model_providers.custom]\n'
        f'base_url = "{base}"\nrequires_openai_auth = true\nwire_api = "responses"\n',
        encoding="utf-8")
    save_login(folder / "auth.json")
    config = _load_codex_slots(tmp_path)[0].provider
    assert bool(config._codex_auth_path) is bound


def test_reader_does_not_modify_the_login_store(tmp_path):
    path = tmp_path / "auth.json"
    save_login(path)
    before = path.read_bytes()
    assert current_codex_headers(str(path), base_url=BASE, account_id="selected-account")
    assert path.read_bytes() == before
    with pytest.raises(CodexLoginRequired):
        current_codex_headers(str(path), base_url=BASE, account_id="another-account")
    assert path.read_bytes() == before
