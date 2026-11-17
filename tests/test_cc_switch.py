import json
import sqlite3

from mewcode.cc_switch import (
    _runtime_from_settings,
    _toml_assign,
    cc_switch_bindings_fingerprint,
    cc_switch_family,
    preferred_cc_switch_binding,
    read_cc_switch_runtime_bindings,
    read_cc_switch_snapshot,
    select_cc_switch_mirror_target,
    _models_from_settings,
)


def test_models_from_settings_reads_model_and_env():
    models = _models_from_settings({
        "model": "claude-fable-5[1m]",
        "env": {"ANTHROPIC_MODEL": "claude-opus-4-6", "OTHER": "x"},
        "config": 'model = "gpt-5.6-sol"\n',
    })
    assert models[0] == "claude-fable-5[1m]"
    assert "claude-opus-4-6" in models
    assert "gpt-5.6-sol" in models


def test_read_cc_switch_snapshot_shape():
    snap = read_cc_switch_snapshot()
    assert "available" in snap
    assert "current" in snap
    assert "providers" in snap


def test_toml_assign_reads_quoted_keys():
    blob = 'base_url = "https://relay.example/v1"\nmodel = \'gpt-x\'\nwire_api = "chat"\n'
    assert _toml_assign(blob, "base_url") == "https://relay.example/v1"
    assert _toml_assign(blob, "model") == "gpt-x"
    assert _toml_assign(blob, "wire_api") == "chat"


def test_runtime_skips_claude_official_without_key():
    assert _runtime_from_settings(
        app_type="claude",
        name="Claude Official",
        settings={"model": "claude-fable-5[1m]", "env": {}},
    ) is None


def test_grok_runtime_prefers_xai_endpoint_and_current_model():
    runtime = _runtime_from_settings(
        app_type="grokbuild",
        name="Grok Build",
        settings={
            "model": "grok-4.6",
            "env": {
                "XAI_API_KEY": "sk-xai-test",
                "XAI_BASE_URL": "https://api.x.ai/v1",
                "OPENAI_BASE_URL": "https://wrong.example/v1",
            },
        },
    )

    assert runtime is not None
    assert runtime["base_url"] == "https://api.x.ai/v1"
    assert runtime["model"] == "grok-4.6"


def test_runtime_reads_claude_relay_token():
    binding = _runtime_from_settings(
        app_type="claude",
        name="示例中转",
        settings={
            "model": "claude-sonnet-4-5",
            "env": {
                "ANTHROPIC_AUTH_TOKEN": "sk-relay-claude",
                "ANTHROPIC_BASE_URL": "https://relay.example/anthropic",
            },
        },
    )
    assert binding is not None
    assert binding["protocol"] == "anthropic"
    assert binding["api_key"] == "sk-relay-claude"
    assert binding["base_url"] == "https://relay.example/anthropic"
    assert binding["model"] == "claude-sonnet-4-5"
    assert binding["slot_name"] == "cc-switch-claude"


def test_runtime_does_not_invent_claude_model_for_relay_credentials():
    settings = {"env": {"ANTHROPIC_AUTH_TOKEN": "sk-test", "ANTHROPIC_BASE_URL": "https://relay.example"}}
    assert _runtime_from_settings(app_type="claude", name="relay", settings=settings) is None
    settings["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] = "claude-configured"
    binding = _runtime_from_settings(app_type="claude", name="relay", settings=settings)
    assert binding["model"] == "claude-configured"
    assert binding["models"] == ["claude-configured"]


def test_runtime_reads_codex_toml_auth():
    binding = _runtime_from_settings(
        app_type="codex",
        name="dieqiyun",
        settings={
            "auth": {"OPENAI_API_KEY": "sk-codex-relay"},
            "config": (
                'base_url = "https://relay.example/v1"\n'
                'model = "gpt-5.6-sol"\n'
                'wire_api = "responses"\n'
            ),
        },
    )
    assert binding is not None
    assert binding["protocol"] == "openai"
    assert binding["api_key"] == "sk-codex-relay"
    assert binding["base_url"] == "https://relay.example/v1"
    assert binding["model"] == "gpt-5.6-sol"
    assert binding["slot_name"] == "cc-switch-codex"


def test_preferred_binding_skips_missing_claude_and_uses_codex():
    codex = {
        "app_type": "codex",
        "name": "dieqiyun",
        "protocol": "openai-compat",
        "base_url": "https://relay.example/v1",
        "model": "gpt-x",
        "api_key": "sk-codex-relay",
        "slot_name": "cc-switch-codex",
        "fingerprint": "codex-fp",
    }
    assert preferred_cc_switch_binding([codex])["app_type"] == "codex"


def test_select_mirror_follows_family_on_change():
    claude = {
        "app_type": "claude",
        "name": "relay",
        "slot_name": "cc-switch-claude",
        "fingerprint": "claude-fp",
        "protocol": "anthropic",
        "base_url": "https://relay.example",
        "model": "claude-x",
        "api_key": "sk-a",
    }
    codex = {
        "app_type": "codex",
        "name": "dieqi",
        "slot_name": "cc-switch-codex",
        "fingerprint": "codex-fp",
        "protocol": "openai-compat",
        "base_url": "https://relay.example/v1",
        "model": "gpt-x",
        "api_key": "sk-b",
    }
    target = select_cc_switch_mirror_target(
        [claude, codex],
        current_family="codex",
        current_valid=True,
        previous_fp="old",
        combined_fp="claude-fp||codex-fp",
    )
    assert target is not None
    assert target["app_type"] == "codex"


def test_select_mirror_does_not_steal_unrelated_valid_provider():
    codex = {
        "app_type": "codex",
        "name": "dieqi",
        "slot_name": "cc-switch-codex",
        "fingerprint": "codex-fp",
        "protocol": "openai-compat",
        "base_url": "https://relay.example/v1",
        "model": "gpt-x",
        "api_key": "sk-b",
    }
    target = select_cc_switch_mirror_target(
        [codex],
        current_family="",
        current_valid=True,
        previous_fp="old",
        combined_fp="codex-fp",
    )
    assert target is None


def test_select_mirror_repairs_invalid_current():
    codex = {
        "app_type": "codex",
        "name": "dieqi",
        "slot_name": "cc-switch-codex",
        "fingerprint": "codex-fp",
        "protocol": "openai-compat",
        "base_url": "https://relay.example/v1",
        "model": "gpt-x",
        "api_key": "sk-b",
    }
    target = select_cc_switch_mirror_target(
        [codex],
        current_family="claude",
        current_valid=False,
        previous_fp="codex-fp",
        combined_fp="codex-fp",
    )
    assert target is not None
    assert target["app_type"] == "codex"


def test_cc_switch_family_groups_apps():
    assert cc_switch_family("claude-desktop") == "claude"
    assert cc_switch_family("codex") == "codex"
    assert cc_switch_family("grokbuild") == "grok"


def test_runtime_bindings_from_isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_SWITCH_HOME", str(tmp_path))
    db_path = tmp_path / "cc-switch.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "CREATE TABLE providers ("
            "id INTEGER PRIMARY KEY, app_type TEXT, name TEXT, "
            "settings_config TEXT, is_current INTEGER, sort_index INTEGER)"
        )
        connection.execute(
            "INSERT INTO providers VALUES (1, 'claude', 'Claude Official', ?, 1, 0)",
            (json.dumps({"model": "claude-fable-5[1m]", "env": {}}),),
        )
        connection.execute(
            "INSERT INTO providers VALUES (2, 'codex', 'dieqiyun', ?, 1, 1)",
            (json.dumps({
                "auth": {"OPENAI_API_KEY": "sk-isolated-codex"},
                "config": 'base_url = "https://isolated.example/v1"\nmodel = "gpt-iso"\n',
            }),),
        )
        connection.commit()
    finally:
        connection.close()

    bindings = read_cc_switch_runtime_bindings()
    assert len(bindings) == 1
    assert bindings[0]["app_type"] == "codex"
    assert bindings[0]["api_key"] == "sk-isolated-codex"
    assert bindings[0]["model"] == "gpt-iso"
    assert "sk-isolated-codex" not in cc_switch_bindings_fingerprint(bindings)
