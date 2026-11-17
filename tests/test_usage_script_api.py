from __future__ import annotations

import json

import httpx
import pytest

import frontend.server as server
from mewcode.config import ProviderConfig
from mewcode.model_registry import ProviderRecord
from mewcode.usage.runtime import UsageRuntime


@pytest.mark.asyncio
async def test_public_slots_and_single_py_install_keep_secret_values_out(tmp_path, monkeypatch):
    runtime = UsageRuntime(tmp_path)
    monkeypatch.setattr(server, "USAGE_RUNTIME", runtime)
    monkeypatch.setattr(server, "DESKTOP_TOKEN", "")
    record = ProviderRecord(
        key="desktop:relay",
        provider=ProviderConfig(
            name="Relay", protocol="openai-compat",
            base_url="https://user:pass@relay.example/v1?token=leak",
            model="relay-model", api_key="super-secret-key",
        ),
        source="desktop", label="Relay", brand="custom",
    )
    monkeypatch.setattr(server.MODEL_REGISTRY, "records", lambda: [record])
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        slots = (await client.get("/api/usage/slots")).json()["slots"]
        assert slots[0]["provider_key"] == "desktop:relay"
        assert slots[0]["base_url"] == "https://relay.example/v1"
        assert slots[0]["has_secret"] is True
        assert "super-secret-key" not in json.dumps(slots)

        script = b"async def collect(query, context):\n    return {'status': 'ok', 'financials': []}\n"
        form = {
            "provider_key": "desktop:relay",
            "allowed_origins": "https://relay.example",
            "allow_browser": "true",
            "allow_provider_secret": "true",
        }
        checked = await client.post(
            "/api/usage/plugins/install", data=form,
            files={"package": ("usage.py", script, "text/x-python")},
        )
        assert checked.status_code == 200
        inspection = checked.json()["inspection"]
        assert inspection["script_sha256"]
        assert inspection["bound_provider_key"] == "desktop:relay"
        assert "provider_api_key" in inspection["secret_schema"]["properties"]
        assert "super-secret-key" not in checked.text

        installed = await client.post(
            "/api/usage/plugins/install?trusted=true", data=form,
            files={"package": ("usage.py", script, "text/x-python")},
        )
        assert installed.status_code == 201
        assert installed.json()["plugin"]["source_kind"] == "single_py"
