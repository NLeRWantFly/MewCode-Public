from __future__ import annotations

import io
import json
import zipfile
import hashlib
from datetime import datetime, timezone

import pytest

from mewcode.usage.ledger import UsageLedger
from mewcode.usage.models import UsageAccount, UsagePeriod
from mewcode.usage.plugins import (
    UsagePluginManager,
    UsagePluginRunner,
    validate_host_request,
)
from mewcode.usage.backend import UsageBackendError


UTC = timezone.utc


def _archive(files: dict[str, str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        for name, value in files.items():
            bundle.writestr(name, value)
    return output.getvalue()


def _manifest(**changes):
    value = {
        "id": "relay.example",
        "name": "Relay Example",
        "version": "1.0.0",
        "protocol_version": "1",
        "entrypoint": "plugin.py",
        "operations": ["collect", "normalize"],
        "config_schema": {"type": "object"},
        "secret_schema": {
            "type": "object",
            "properties": {"password": {"type": "string"}},
        },
        "permissions": {
            "browser": True,
            "interactive_login": True,
            "origins": ["https://relay.example"],
        },
    }
    value.update(changes)
    return json.dumps(value)


def test_plugin_package_rejects_path_traversal_native_files_and_unconfirmed_install(tmp_path):
    manager = UsagePluginManager(tmp_path / "plugins", UsageLedger(tmp_path))
    with pytest.raises(ValueError, match="unsafe"):
        manager.inspect_package(_archive({
            "usage-plugin.json": _manifest(), "plugin.py": "", "../escape.py": "",
        }))
    with pytest.raises(ValueError, match="forbidden"):
        manager.inspect_package(_archive({
            "usage-plugin.json": _manifest(), "plugin.py": "", "native.pyd": "",
        }))
    valid = _archive({"usage-plugin.json": _manifest(), "plugin.py": ""})
    with pytest.raises(PermissionError):
        manager.install_package(valid, trusted=False)


def test_plugin_manifest_rejects_external_json_schema_references(tmp_path):
    manager = UsagePluginManager(tmp_path / "plugins", UsageLedger(tmp_path))
    archive = _archive({
        "usage-plugin.json": _manifest(config_schema={
            "$ref": "file:///etc/passwd",
        }),
        "plugin.py": "",
    })
    with pytest.raises(ValueError, match="references must be local"):
        manager.inspect_package(archive)


@pytest.mark.asyncio
async def test_single_file_style_plugin_allows_collect_without_normalize(tmp_path):
    manager = UsagePluginManager(tmp_path / "plugins", UsageLedger(tmp_path))
    script = b'''\nasync def collect(query, context):\n    return {"status": "ok", "financials": [], "warnings": [context.config["provider_slot"]["name"]]}\n'''
    manifest = json.loads(_manifest(
        id="script.single", source_kind="single_py",
        bound_provider_key="desktop:relay",
        bound_slot={"provider_key": "desktop:relay", "name": "Relay", "has_secret": True},
        script_sha256=hashlib.sha256(script).hexdigest(),
    ))
    installed = manager.install_package(_archive({
        "usage-plugin.json": json.dumps(manifest), "plugin.py": script.decode(),
    }), trusted=True)
    account = UsageAccount(
        id="usage:single", name="Single", adapter_id="plugin", plugin_id="script.single",
    )
    period = UsagePeriod(
        start=datetime(2026, 8, 1, tzinfo=UTC), end=datetime(2026, 8, 2, tzinfo=UTC),
    )
    from mewcode.usage.plugins import PluginUsageAdapter
    adapter = PluginUsageAdapter(
        manager, UsagePluginRunner(timeout=10),
        host_handler_factory=lambda _manifest, _account: (
            lambda _method, _params: (_ for _ in ()).throw(AssertionError("unexpected host call"))
        ),
    )
    snapshot = await adapter.fetch_snapshot(account, period)
    assert snapshot.warnings == ["Relay"]
    assert installed["script_sha256"] == hashlib.sha256(script).hexdigest()


@pytest.mark.asyncio
async def test_plugin_runs_collect_then_normalize_with_vendored_python_dependency(tmp_path):
    manager = UsagePluginManager(tmp_path / "plugins", UsageLedger(tmp_path))
    plugin = '''
import helper

async def collect(query, context):
    return {"amount": helper.AMOUNT, "config": context.config["tenant"]}

async def normalize(query, artifact):
    return {
        "status": "ok",
        "financials": [{
            "currency": "USD",
            "used": {"value": artifact["amount"], "provenance": "provider_portal", "quality": "exact"}
        }],
        "warnings": [artifact["config"]]
    }
'''
    installed = manager.install_package(_archive({
        "usage-plugin.json": _manifest(),
        "plugin.py": plugin,
        "helper.py": 'AMOUNT = "3.1400"\n',
    }), trusted=True)
    account = UsageAccount(
        id="usage:relay", name="Relay", adapter_id="plugin",
        plugin_id="relay.example", config={"tenant": "team-a"},
    )
    period = UsagePeriod(
        start=datetime(2026, 8, 1, tzinfo=UTC),
        end=datetime(2026, 8, 2, tzinfo=UTC),
    )

    async def host_handler(_method, _params):
        raise AssertionError("plugin did not request the host")

    snapshot = await UsagePluginRunner(timeout=10).run(
        installed, account, period, host_handler=host_handler
    )
    assert snapshot.account_id == account.id
    assert snapshot.period == period
    assert str(snapshot.financials[0].used.value) == "3.1400"
    assert snapshot.warnings == ["team-a"]


def test_plugin_browser_permissions_enforce_origin_and_keep_secret_reference(tmp_path):
    manifest = json.loads(_manifest())
    account = UsageAccount(
        id="usage:relay", name="Relay", adapter_id="plugin",
        secret_values={"password": "top-secret"},
    )
    clean = validate_host_request(
        manifest, account, "browser.fill",
        {"selector": "#password", "secret_ref": "password"},
    )
    assert clean == {"selector": "#password", "secret_ref": "password"}
    assert "top-secret" not in json.dumps(clean)
    with pytest.raises(PermissionError, match="not declared"):
        validate_host_request(
            manifest, account, "browser.goto", {"url": "https://evil.example/login"}
        )
    with pytest.raises(PermissionError, match="credential/session storage"):
        validate_host_request(
            manifest, account, "browser.evaluate", {"script": "document.cookie"}
        )
    provider_manifest = json.loads(_manifest(
        bound_provider_key="desktop:relay",
        secret_schema={"type": "object", "properties": {
            "provider_api_key": {"type": "string"},
        }},
    ))
    assert validate_host_request(
        provider_manifest, account, "browser.fill",
        {"selector": "#key", "secret_ref": "provider_api_key"},
    )["secret_ref"] == "provider_api_key"
    with pytest.raises(PermissionError, match="only be used"):
        validate_host_request(
            provider_manifest, account, "secret.resolve", {"name": "provider_api_key"}
        )


@pytest.mark.asyncio
async def test_plugin_timeout_terminates_worker_and_redacts_declared_secrets(tmp_path):
    manager = UsagePluginManager(tmp_path / "plugins", UsageLedger(tmp_path))
    period = UsagePeriod(
        start=datetime(2026, 8, 1, tzinfo=UTC),
        end=datetime(2026, 8, 2, tzinfo=UTC),
    )
    sleepy = manager.install_package(_archive({
        "usage-plugin.json": _manifest(id="relay.sleepy"),
        "plugin.py": '''
import asyncio
async def collect(query, context):
    await asyncio.sleep(5)
    return {}
async def normalize(query, artifact):
    return {}
''',
    }), trusted=True)
    account = UsageAccount(
        id="usage:sleepy", name="Sleepy", adapter_id="plugin",
        plugin_id="relay.sleepy",
    )

    async def unused(_method, _params):
        raise AssertionError("unexpected host call")

    with pytest.raises(UsageBackendError, match="timed out"):
        await UsagePluginRunner(timeout=0.05).run(
            sleepy, account, period, host_handler=unused
        )

    leaking = manager.install_package(_archive({
        "usage-plugin.json": _manifest(id="relay.redact"),
        "plugin.py": '''
async def collect(query, context):
    password = await context.secret("password")
    raise RuntimeError("password=" + password)
async def normalize(query, artifact):
    return {}
''',
    }), trusted=True)
    secret_account = UsageAccount(
        id="usage:redact", name="Redact", adapter_id="plugin",
        plugin_id="relay.redact", secret_values={"password": "top-secret-value"},
    )

    async def secret_handler(method, params):
        assert method == "secret.resolve" and params["name"] == "password"
        return "top-secret-value"

    with pytest.raises(UsageBackendError) as caught:
        await UsagePluginRunner(timeout=5).run(
            leaking, secret_account, period, host_handler=secret_handler
        )
    assert "top-secret-value" not in str(caught.value)
    assert "[REDACTED]" in str(caught.value)
