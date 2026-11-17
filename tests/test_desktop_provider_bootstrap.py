import json
from unittest.mock import AsyncMock

import httpx
import pytest

from mewcode.desktop_providers import bootstrap_desktop_providers
from mewcode.model_registry import ModelRef, ProviderRegistry
from mewcode.permissions.review_client import bind_safety_reviewer
from mewcode.permissions.reviewer import SafetyReviewer
from mewcode.plugins import ApplicationHost
from mewcode.plugins.services import PROVIDERS
from tests.test_typesafe_review import make_client, response_for


PROFILE = {"provider_key": "desktop:example_provider_7", "name": "TypeSafe", "brand": "typesafe",
           "protocol": "typesafe", "base_url": "https://api.typesafe.ai/v1", "default_model": "jev-latest",
           "models": ["jev-latest", "jev-preview"], "api_key": "test-bootstrap-secret"}
REF = ModelRef(PROFILE["provider_key"], "jev-latest")


def test_authenticated_desktop_bootstraps_before_session_and_does_not_write(tmp_path):
    path = tmp_path / "providers.json"
    path.write_text(json.dumps([PROFILE]), encoding="utf-8")
    original = path.read_bytes()
    root = ProviderRegistry()
    assert bootstrap_desktop_providers(root, tmp_path, desktop_authenticated=False) == 0
    assert root.get(REF.provider_key) is None
    assert bootstrap_desktop_providers(root, tmp_path, desktop_authenticated=True) == 1
    child = ProviderRegistry(desktop_source=root)
    assert child.resolve(REF).client.review_only
    assert path.read_bytes() == original
    assert "test-bootstrap-secret" not in json.dumps(child.public_desktop_profiles())


@pytest.mark.parametrize("payload", ["bad-json", {}, [PROFILE, PROFILE], [PROFILE, {"protocol": "unknown"}],
    [{**PROFILE, "provider_key": "unstable"}], [{"provider_key": "desktop:encrypted", "encrypted_api_key": "ciphertext"}]])
def test_invalid_or_encrypted_snapshot_never_replaces_live_registry(tmp_path, caplog, payload):
    root = ProviderRegistry()
    root.replace_desktop_profiles([PROFILE])
    old = root.get(REF.provider_key)
    (tmp_path / "providers.json").write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    assert bootstrap_desktop_providers(root, tmp_path, desktop_authenticated=True) == 0
    assert root.get(REF.provider_key) is old
    assert "test-bootstrap-secret" not in caplog.text and "ciphertext" not in caplog.text


@pytest.mark.asyncio
async def test_existing_child_tracks_late_sync_rotation_and_deletion_without_slot_refresh():
    root = ProviderRegistry()
    child = ProviderRegistry(desktop_source=root)
    nested = ProviderRegistry(desktop_source=child)
    assert nested.get(REF.provider_key) is None
    root.replace_desktop_profiles([PROFILE])
    assert nested.resolve(REF).config.api_key == "test-bootstrap-secret"
    child._discovered[REF.provider_key] = root.get(REF.provider_key)  # Old WebSession discovery snapshot.
    root.replace_desktop_profiles([{**PROFILE, "api_key": "rotated-test-secret"}])
    assert nested.resolve(REF).config.api_key == "rotated-test-secret"
    assert (await nested.catalog(local_only=True))["models"]
    root.replace_desktop_profiles([])
    assert nested.get(REF.provider_key) is None and child.records() == []
    with pytest.raises(ValueError, match="unavailable"):
        nested.resolve(REF)
    assert (await nested.catalog(local_only=True))["models"] == []


@pytest.mark.asyncio
async def test_desktop_revision_invalidates_child_catalog_cache():
    root = ProviderRegistry()
    child = ProviderRegistry(desktop_source=root)
    child._catalog_provider = AsyncMock(side_effect=lambda record: {"provider": {"key": record.key}, "models": []})
    assert (await child.catalog())["providers"] == []
    root.replace_desktop_profiles([PROFILE])
    assert len((await child.catalog())["providers"]) == 1
    root.replace_desktop_profiles([])
    assert (await child.catalog())["providers"] == []


@pytest.mark.asyncio
async def test_sync_during_catalog_fetch_cannot_publish_old_generation():
    import asyncio
    root = ProviderRegistry()
    root.replace_desktop_profiles([PROFILE])
    child = ProviderRegistry(desktop_source=root)
    entered, release = asyncio.Event(), asyncio.Event()
    async def fetch(record):
        entered.set()
        await release.wait()
        return {"provider": {"key": record.key}, "models": []}
    child._catalog_provider = fetch
    pending = asyncio.create_task(child.catalog())
    await asyncio.wait_for(entered.wait(), 2)
    root.replace_desktop_profiles([])
    release.set()
    await pending
    assert (await child.catalog())["providers"] == []


@pytest.mark.asyncio
async def test_application_host_session_inherits_live_desktop_source_and_review_calls_jev(tmp_path):
    from types import SimpleNamespace
    from tests.test_live_review_selection import REQUEST

    app = ApplicationHost(str(tmp_path), discover_plugins=False).start()
    session = ApplicationHost(str(tmp_path), context=app.context.scope("web-session"), discover_plugins=False).start()
    try:
        root, child = app.context.get(PROVIDERS), session.context.get(PROVIDERS)
        assert root is not child and child.get(REF.provider_key) is None
        calls, receipts = [], []
        def handler(request):
            calls.append(json.loads(request.content)["model"])
            return httpx.Response(200, json=response_for(request))
        child._client_factory = lambda config: make_client(handler)
        settings = tmp_path / "review.json"
        settings.write_text(json.dumps({"provider_key": REF.provider_key, "model_id": REF.model_id}), encoding="utf-8")
        reviewer = SafetyReviewer(None, str(tmp_path))
        agent = SimpleNamespace(safety_reviewer=reviewer, client=SimpleNamespace(get_usage_recorder=lambda: receipts.append))
        bind_safety_reviewer(agent, child, settings_path=settings)
        root.replace_desktop_profiles([PROFILE])  # Only the process registry receives IPC sync.
        result = await reviewer.review(**REQUEST)
        assert result.allowed and calls == ["jev-latest"]
        assert result.review_route["provider_key"] == REF.provider_key
        assert len(receipts) == 1
        root.replace_desktop_profiles([])
        assert (await reviewer.review(**REQUEST)).unavailable
        assert len(calls) == 1
    finally:
        await session.aclose()
        await app.aclose()
