"""插槽式 Provider 切换测试——配置源发现、校验、/provider 命令。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from mewcode.commands.handlers.provider import handle_provider
from mewcode.commands.registry import CommandContext
from mewcode.config import ProviderConfig
from mewcode.provider_sources import (
    ProviderSlot,
    discover_provider_slots,
    validate_slot,
)

# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------

def _config_provider(name: str = "main") -> ProviderConfig:
    return ProviderConfig(
        name=name,
        protocol="anthropic",
        base_url="https://api.anthropic.com",
        model="claude-legacy-test",
        api_key="sk-test",
    )


@pytest.fixture()
def fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


@pytest.fixture()
def work_dir(tmp_path: Path) -> Path:
    work = tmp_path / "project"
    work.mkdir()
    return work


# ---------------------------------------------------------------------------
# 配置源发现
# ---------------------------------------------------------------------------

class TestDiscoverSlots:
    def test_discovery_only_config_preserves_settings(self, fake_home: Path, work_dir: Path) -> None:
        from mewcode.config import load_config

        config_path = work_dir / "config.yaml"
        config_path.write_text(
            "providers: []\npermission_mode: plan\nenable_verification_agent: true\n"
            "redis:\n  enabled: true\n  key_prefix: discovery-test\n", encoding="utf-8",
        )
        codex_dir = fake_home / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text('model = "gpt-6.1-sol"\n', encoding="utf-8")
        (codex_dir / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": "sk-test"}), encoding="utf-8",
        )

        config = load_config(config_path)
        slots = discover_provider_slots(config.providers, work_dir=work_dir, home=fake_home)

        assert config.providers == []
        assert config.permission_mode == "plan" and config.enable_verification_agent
        assert config.redis.enabled and config.redis.key_prefix == "discovery-test"
        assert [(slot.source, slot.provider.model) for slot in slots] == [("codex", "gpt-6.1-sol")]

    @pytest.mark.parametrize("providers", [None, {}, "auto"])
    def test_discovery_only_config_still_rejects_invalid_provider_types(self, providers) -> None:
        from mewcode.validator import ConfigError, validate_providers

        with pytest.raises(ConfigError, match="must be a list"):
            validate_providers(providers)

    def test_config_providers_only(self, fake_home: Path, work_dir: Path) -> None:
        slots = discover_provider_slots(
            [_config_provider()], work_dir=work_dir, home=fake_home
        )
        assert len(slots) == 1
        assert slots[0].source == "config"
        assert slots[0].key == "config:main"

    def test_codex_source(self, fake_home: Path, work_dir: Path) -> None:
        codex_dir = fake_home / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(
            'model = "gpt-5-codex"\n'
            'model_provider = "myproxy"\n'
            '[model_providers.myproxy]\n'
            'name = "My Proxy"\n'
            'base_url = "https://proxy.example.com/v1"\n'
            'env_key = "MYPROXY_API_KEY"\n',
            encoding="utf-8",
        )
        (codex_dir / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": "sk-codex"}), encoding="utf-8"
        )
        slots = discover_provider_slots([], work_dir=work_dir, home=fake_home)
        assert len(slots) == 1
        slot = slots[0]
        assert slot.source == "codex"
        assert slot.provider.protocol == "openai-compat"
        assert slot.provider.base_url == "https://proxy.example.com/v1"
        assert slot.provider.model == "gpt-5-codex"
        # env_key 未设置时回退到 auth.json
        assert slot.provider.api_key == "sk-codex"

    def test_codex_default_slot_from_auth(self, fake_home: Path, work_dir: Path) -> None:
        codex_dir = fake_home / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")
        (codex_dir / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": "sk-codex"}), encoding="utf-8"
        )
        slots = discover_provider_slots([], work_dir=work_dir, home=fake_home)
        assert len(slots) == 1
        assert slots[0].key == "codex:codex-openai"
        assert slots[0].provider.model == "gpt-5"

    def test_codex_chatgpt_oauth_uses_responses_backend(
        self, fake_home: Path, work_dir: Path
    ) -> None:
        codex_dir = fake_home / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(
            'model = "gpt-5.6-sol"\n'
            'model_provider = "custom"\n'
            'model_reasoning_effort = "medium"\n'
            'service_tier = "fast"\n'
            '[model_providers.custom]\n'
            'requires_openai_auth = true\n'
            'wire_api = "responses"\n',
            encoding="utf-8",
        )
        (codex_dir / "auth.json").write_text(
            json.dumps({
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "access_token": "oauth-access-token",
                    "account_id": "account-123",
                },
            }),
            encoding="utf-8",
        )

        slots = discover_provider_slots([], work_dir=work_dir, home=fake_home)

        assert len(slots) == 1
        slot = slots[0]
        assert slot.provider.protocol == "openai"
        assert slot.provider.base_url == "https://chatgpt.com/backend-api/codex"
        assert slot.provider.model == "gpt-5.6-sol"
        assert slot.provider.api_key == "oauth-access-token"
        assert slot.provider.default_headers == {
            "ChatGPT-Account-Id": "account-123"
        }
        assert slot.default_effort == "medium"
        assert slot.provider.service_tier == "fast"

    def test_codex_oauth_includes_cached_terra_and_luna_models(
        self, fake_home: Path, work_dir: Path
    ) -> None:
        codex_dir = fake_home / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(
            'model = "gpt-5.6-sol"\n'
            'model_provider = "custom"\n'
            '[model_providers.custom]\n'
            'requires_openai_auth = true\n'
            'wire_api = "responses"\n'
            'enabled-reasoning-efforts = ["low", "high", "ultra"]\n',
            encoding="utf-8",
        )
        (codex_dir / "auth.json").write_text(
            json.dumps({
                "tokens": {
                    "access_token": "oauth-access-token",
                    "account_id": "account-123",
                },
            }),
            encoding="utf-8",
        )
        (codex_dir / "models_cache.json").write_text(
            json.dumps({
                "models": [
                    {
                        "slug": "gpt-5.6-luna",
                        "visibility": "list",
                        "supported_in_api": True,
                        "priority": 3,
                        "supported_reasoning_levels": [
                            {"effort": "low"}, {"effort": "medium"},
                        ],
                    },
                    {
                        "slug": "gpt-5.6-terra",
                        "visibility": "list",
                        "supported_in_api": True,
                        "priority": 2,
                        "supported_reasoning_levels": [
                            {"effort": "low"}, {"effort": "xhigh"},
                        ],
                    },
                    {
                        "slug": "hidden-model",
                        "visibility": "hide",
                        "supported_in_api": True,
                    },
                ],
            }),
            encoding="utf-8",
        )

        slot = discover_provider_slots([], work_dir=work_dir, home=fake_home)[0]

        assert slot.models == [
            "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"
        ]
        assert slot.effort_levels == ["low", "high", "ultra"]
        assert slot.model_efforts["gpt-5.6-sol"] == ["low", "high", "ultra"]
        assert slot.model_efforts["gpt-5.6-terra"] == ["low", "xhigh"]
        assert "hidden-model" not in slot.models

    def test_codex_custom_proxy_uses_local_model_cache(
        self, fake_home: Path, work_dir: Path
    ) -> None:
        codex_dir = fake_home / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(
            'model = "gpt-5.4"\n'
            '[model_providers.myproxy]\n'
            'name = "My Proxy"\n'
            'base_url = "https://proxy.example.com/v1"\n'
            'wire_api = "chat"\n',
            encoding="utf-8",
        )
        (codex_dir / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": "sk-codex"}), encoding="utf-8"
        )
        (codex_dir / "models_cache.json").write_text(
            json.dumps({
                "models": [
                    {
                        "slug": "gpt-5.4",
                        "visibility": "list",
                        "supported_in_api": True,
                        "priority": 1,
                    },
                    {
                        "slug": "gpt-5.3-codex",
                        "visibility": "list",
                        "supported_in_api": True,
                        "priority": 2,
                    },
                    {
                        "slug": "gpt-5.2",
                        "visibility": "list",
                        "supported_in_api": True,
                        "priority": 3,
                    },
                    {
                        "slug": "gpt-5.1",
                        "visibility": "list",
                        "supported_in_api": True,
                        "priority": 4,
                    },
                    {
                        "slug": "hidden-model",
                        "visibility": "hide",
                        "supported_in_api": True,
                    },
                ],
            }),
            encoding="utf-8",
        )
        slot = discover_provider_slots([], work_dir=work_dir, home=fake_home)[0]
        assert slot.source == "codex"
        assert slot.models == [
            "gpt-5.4", "gpt-5.3-codex", "gpt-5.2", "gpt-5.1",
        ]
        assert "hidden-model" not in slot.models

    def test_cc_switch_codex_slot_merges_local_model_cache(
        self, fake_home: Path, work_dir: Path
    ) -> None:
        (fake_home / ".codex").mkdir()
        (fake_home / ".codex" / "models_cache.json").write_text(
            json.dumps({
                "models": [
                    {
                        "slug": "gpt-5.4",
                        "visibility": "list",
                        "supported_in_api": True,
                        "priority": 1,
                    },
                    {
                        "slug": "gpt-5.3-codex",
                        "visibility": "list",
                        "supported_in_api": True,
                        "priority": 2,
                    },
                ],
            }),
            encoding="utf-8",
        )
        bindings = [{
            "app_type": "codex",
            "name": "dieqiyun",
            "protocol": "openai-compat",
            "base_url": "https://relay.example/v1",
            "model": "gpt-iso",
            "api_key": "sk-relay-codex",
            "slot_name": "cc-switch-codex",
        }]
        slots = discover_provider_slots(
            [],
            work_dir=work_dir,
            home=fake_home,
            cc_switch_bindings=bindings,
        )
        slot = next(item for item in slots if item.source == "cc-switch")
        assert slot.models == ["gpt-iso", "gpt-5.4", "gpt-5.3-codex"]

    def test_claude_code_source_with_project_override(
        self, fake_home: Path, work_dir: Path
    ) -> None:
        (fake_home / ".claude").mkdir()
        (fake_home / ".claude" / "settings.json").write_text(
            json.dumps({
                "env": {
                    "ANTHROPIC_BASE_URL": "https://cc.example.com",
                    "ANTHROPIC_AUTH_TOKEN": "sk-cc-global",
                },
            }),
            encoding="utf-8",
        )
        (work_dir / ".claude").mkdir()
        (work_dir / ".claude" / "settings.json").write_text(
            json.dumps({
                "env": {"ANTHROPIC_MODEL": "claude-opus-4-1"},
                "model": "ignored-by-env",
            }),
            encoding="utf-8",
        )
        slots = discover_provider_slots([], work_dir=work_dir, home=fake_home)
        assert len(slots) == 1
        p = slots[0].provider
        assert slots[0].source == "claude-code"
        assert p.protocol == "anthropic"
        assert p.base_url == "https://cc.example.com"
        assert p.api_key == "sk-cc-global"
        # 项目级 env 覆盖模型
        assert p.model == "claude-opus-4-1"

    def test_project_env_source(self, fake_home: Path, work_dir: Path) -> None:
        (work_dir / ".env").write_text(
            "# comment\n"
            "export ANTHROPIC_API_KEY='sk-env-a'\n"
            "ANTHROPIC_MODEL=claude-haiku-4\n"
            'OPENAI_API_KEY="sk-env-o"\n'
            "OPENAI_BASE_URL=https://oai.example.com/v1\n",
            encoding="utf-8",
        )
        slots = discover_provider_slots([], work_dir=work_dir, home=fake_home)
        keys = {s.key for s in slots}
        assert keys == {"project-env:env-anthropic", "project-env:env-openai"}
        by_key = {s.key: s.provider for s in slots}
        assert by_key["project-env:env-anthropic"].api_key == "sk-env-a"
        assert by_key["project-env:env-anthropic"].model == "claude-haiku-4"
        assert by_key["project-env:env-openai"].base_url == "https://oai.example.com/v1"

    def test_anthropic_credentials_do_not_invent_model(self, fake_home: Path, work_dir: Path) -> None:
        (fake_home / ".claude").mkdir()
        (fake_home / ".claude" / "settings.json").write_text(
            json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "sk-test", "ANTHROPIC_BASE_URL": "https://relay.example"}}),
            encoding="utf-8",
        )
        (work_dir / ".env").write_text("ANTHROPIC_API_KEY=sk-test\n", encoding="utf-8")
        slots = discover_provider_slots([], work_dir=work_dir, home=fake_home, cc_switch_bindings=[])
        assert not [slot for slot in slots if slot.provider.protocol == "anthropic"]

    def test_project_env_exposes_official_xai_provider(
        self, fake_home: Path, work_dir: Path
    ) -> None:
        (work_dir / ".env").write_text(
            "XAI_API_KEY=sk-xai-test\n",
            encoding="utf-8",
        )

        slots = discover_provider_slots([], work_dir=work_dir, home=fake_home)

        slot = next(item for item in slots if item.key == "project-env:env-grok")
        assert slot.provider.protocol == "openai"
        assert slot.provider.base_url == "https://api.x.ai/v1"
        assert slot.provider.model == "grok-4.6"
        assert slot.effort_levels == ["low", "medium", "high", "xhigh"]
        assert slot.default_effort == "high"
        assert validate_slot(slot) is None

    def test_missing_sources_are_skipped(self, fake_home: Path, work_dir: Path) -> None:
        # 不存在任何外部配置源时只剩 config 插槽，不抛异常
        slots = discover_provider_slots(
            [_config_provider()], work_dir=work_dir, home=fake_home
        )
        assert [s.source for s in slots] == ["config"]

    def test_broken_source_files_are_skipped(
        self, fake_home: Path, work_dir: Path
    ) -> None:
        (fake_home / ".codex").mkdir()
        (fake_home / ".codex" / "config.toml").write_text(
            "not [valid toml", encoding="utf-8"
        )
        (fake_home / ".claude").mkdir()
        (fake_home / ".claude" / "settings.json").write_text(
            "{broken json", encoding="utf-8"
        )
        slots = discover_provider_slots(
            [_config_provider()], work_dir=work_dir, home=fake_home
        )
        assert [s.source for s in slots] == ["config"]


class TestCcSwitchSlots:
    def test_grok_binding_keeps_models_and_reasoning_efforts(
        self, fake_home: Path, work_dir: Path
    ) -> None:
        bindings = [{
            "app_type": "grokbuild",
            "name": "Grok Build",
            "protocol": "openai-compat",
            "base_url": "https://api.x.ai/v1",
            "model": "grok-4.6",
            "models": ["grok-4.6"],
            "api_key": "sk-xai-test",
            "slot_name": "cc-switch-grokbuild",
        }]

        slots = discover_provider_slots(
            [],
            work_dir=work_dir,
            home=fake_home,
            cc_switch_bindings=bindings,
        )

        slot = next(item for item in slots if item.source == "cc-switch")
        assert slot.provider.model == "grok-4.6"
        assert slot.effort_levels == ["low", "medium", "high", "xhigh"]
        assert slot.model_efforts["grok-4.6"] == ["low", "medium", "high", "xhigh"]
        assert validate_slot(slot) is None

    def test_injects_current_bindings_and_enriches_claude_code(
        self, fake_home: Path, work_dir: Path
    ) -> None:
        (fake_home / ".claude").mkdir()
        (fake_home / ".claude" / "settings.json").write_text(
            json.dumps({"model": "claude-old", "env": {}}),
            encoding="utf-8",
        )
        bindings = [{
            "app_type": "claude",
            "name": "示例中转",
            "protocol": "anthropic",
            "base_url": "https://relay.example/anthropic",
            "model": "claude-sonnet-4-5",
            "api_key": "sk-relay-claude",
            "slot_name": "cc-switch-claude",
        }]
        slots = discover_provider_slots(
            [],
            work_dir=work_dir,
            home=fake_home,
            cc_switch_bindings=bindings,
        )
        by_key = {slot.key: slot for slot in slots}
        injected = by_key["cc-switch:cc-switch-claude"]
        assert validate_slot(injected) is None
        assert injected.provider.api_key == "sk-relay-claude"
        claude = by_key["claude-code:claude-code"]
        assert claude.provider.api_key == "sk-relay-claude"
        assert claude.provider.base_url == "https://relay.example/anthropic"
        assert claude.provider.model == "claude-sonnet-4-5"
        assert validate_slot(claude) is None

    def test_enriches_custom_codex_without_smearing_oauth(
        self, fake_home: Path, work_dir: Path
    ) -> None:
        codex_dir = fake_home / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(
            'model = "gpt-5.6-sol"\n'
            '[model_providers.openai]\n'
            'requires_openai_auth = true\n'
            'wire_api = "responses"\n'
            '[model_providers.custom]\n'
            'base_url = "https://relay.example/v1"\n'
            'wire_api = "chat"\n',
            encoding="utf-8",
        )
        (codex_dir / "auth.json").write_text(
            json.dumps({
                "tokens": {
                    "access_token": "oauth-access-token",
                    "account_id": "account-123",
                },
            }),
            encoding="utf-8",
        )
        bindings = [{
            "app_type": "codex",
            "name": "dieqiyun",
            "protocol": "openai-compat",
            "base_url": "https://relay.example/v1",
            "model": "gpt-iso",
            "api_key": "sk-relay-codex",
            "slot_name": "cc-switch-codex",
        }]
        slots = discover_provider_slots(
            [],
            work_dir=work_dir,
            home=fake_home,
            cc_switch_bindings=bindings,
        )
        by_key = {slot.key: slot for slot in slots}
        oauth = by_key["codex:codex-openai"]
        assert oauth.provider.api_key == "oauth-access-token"
        custom = by_key["codex:codex-custom"]
        assert custom.provider.api_key == "sk-relay-codex"
        assert custom.provider.model == "gpt-iso"
        assert validate_slot(custom) is None


# ---------------------------------------------------------------------------
# 插槽校验
# ---------------------------------------------------------------------------

class TestValidateSlot:
    def test_explicit_registry_key_survives_display_name_changes(self) -> None:
        slot = ProviderSlot(
            provider=_config_provider("Editable display name"),
            source="desktop",
            registry_key="desktop:stable-profile-id",
        )

        assert slot.key == "desktop:stable-profile-id"

    def test_valid_slot(self) -> None:
        slot = ProviderSlot(provider=_config_provider(), source="config")
        assert validate_slot(slot) is None

    def test_missing_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        p = _config_provider()
        p.api_key = ""
        slot = ProviderSlot(provider=p, source="config")
        err = validate_slot(slot)
        assert err is not None and "API key" in err

    def test_invalid_base_url(self) -> None:
        p = _config_provider()
        p.base_url = "not-a-url"
        err = validate_slot(ProviderSlot(provider=p, source="config"))
        assert err is not None and "base_url" in err

    def test_missing_model(self) -> None:
        p = _config_provider()
        p.model = ""
        err = validate_slot(ProviderSlot(provider=p, source="config"))
        assert err is not None and "model" in err


# ---------------------------------------------------------------------------
# /provider 命令
# ---------------------------------------------------------------------------

class SwitchableMockUI:
    """模拟实现了插槽切换接口的 TUI。"""

    def __init__(self, slots: list[ProviderSlot], switch_error: str | None = None):
        self.messages: list[str] = []
        self.menu_shown = 0
        self.switched: list[ProviderSlot] = []
        self._slots = slots
        self._switch_error = switch_error
        self._selected_provider = slots[0].provider if slots else None

    def add_system_message(self, text: str) -> None:
        self.messages.append(text)

    def get_provider_slots(self, refresh: bool = False) -> list[ProviderSlot]:
        return self._slots

    def show_provider_menu(self) -> None:
        self.menu_shown += 1

    def switch_provider(self, slot: ProviderSlot) -> str | None:
        if self._switch_error:
            return self._switch_error
        self.switched.append(slot)
        self._selected_provider = slot.provider
        return None


def _ctx(args: str, ui) -> CommandContext:
    return CommandContext(
        args=args,
        agent=None,
        conversation=None,
        session=None,
        session_manager=None,
        memory_manager=None,
        ui=ui,
        config={},
    )


def _two_slots() -> list[ProviderSlot]:
    return [
        ProviderSlot(provider=_config_provider("main"), source="config"),
        ProviderSlot(
            provider=ProviderConfig(
                name="env-openai",
                protocol="openai-compat",
                base_url="https://oai.example.com/v1",
                model="gpt-4o",
                api_key="sk-o",
            ),
            source="project-env",
        ),
    ]


class TestProviderCommand:
    def test_no_args_shows_menu(self) -> None:
        ui = SwitchableMockUI(_two_slots())
        asyncio.run(handle_provider(_ctx("", ui)))
        assert ui.menu_shown == 1

    def test_list_shows_slots_with_current_marker(self) -> None:
        ui = SwitchableMockUI(_two_slots())
        asyncio.run(handle_provider(_ctx("list", ui)))
        assert len(ui.messages) == 1
        text = ui.messages[0]
        assert "config:main" in text or "main" in text
        assert "env-openai" in text
        assert "*" in text  # 当前 provider 标记

    def test_switch_by_index(self) -> None:
        ui = SwitchableMockUI(_two_slots())
        asyncio.run(handle_provider(_ctx("2", ui)))
        assert len(ui.switched) == 1
        assert ui.switched[0].provider.name == "env-openai"

    def test_switch_by_name(self) -> None:
        ui = SwitchableMockUI(_two_slots())
        asyncio.run(handle_provider(_ctx("env-openai", ui)))
        assert len(ui.switched) == 1
        assert ui.switched[0].key == "project-env:env-openai"

    def test_switch_by_key(self) -> None:
        ui = SwitchableMockUI(_two_slots())
        asyncio.run(handle_provider(_ctx("project-env:env-openai", ui)))
        assert len(ui.switched) == 1

    def test_unknown_target(self) -> None:
        ui = SwitchableMockUI(_two_slots())
        asyncio.run(handle_provider(_ctx("nope", ui)))
        assert not ui.switched
        assert any("未找到" in m for m in ui.messages)

    def test_switch_error_reported(self) -> None:
        ui = SwitchableMockUI(_two_slots(), switch_error="无法解析 API key")
        asyncio.run(handle_provider(_ctx("2", ui)))
        assert any("切换失败" in m for m in ui.messages)

    def test_unsupported_ui(self) -> None:
        class PlainUI:
            def __init__(self) -> None:
                self.messages: list[str] = []

            def add_system_message(self, text: str) -> None:
                self.messages.append(text)

        ui = PlainUI()
        asyncio.run(handle_provider(_ctx("", ui)))
        assert any("不支持" in m for m in ui.messages)


# ---------------------------------------------------------------------------
# TUI 回归：初始 Provider 初始化失败后输入仍可用
# ---------------------------------------------------------------------------

class TestStartupFailureRecovery:
    """初始 provider 缺 API key 时，TUI 不能卡死：
    输入框保持可输入，本地命令（如 /provider）仍然可执行。"""

    @pytest.mark.asyncio
    async def test_input_usable_after_auth_failure(self) -> None:
        from mewcode.app import MewCodeApp

        bad = ProviderConfig(
            name="broken",
            protocol="anthropic",
            base_url="https://api.anthropic.com",
            model="claude-legacy-test",
            api_key="",  # 无密钥且环境变量未设置 -> create_client 抛 AuthenticationError
        )
        app = MewCodeApp(providers=[bad])
        async with app.run_test() as pilot:
            await pilot.pause()
            # agent 未初始化，但输入框可输入
            assert app.agent is None
            chat_input = app.query_one("#chat-input")
            assert chat_input.disabled is False

            # 普通文本提示 Provider 未就绪，而不是静默丢弃
            await app._dispatch_command("hello")
            assert app.agent is None

            # 本地命令仍然可执行（agent=None 不会阻断 / 命令分发）
            await app._dispatch_command("/provider list")

    @pytest.mark.asyncio
    async def test_switch_recovers_full_init(self, tmp_path) -> None:
        """agent 为 None 时 switch_provider 走完整初始化恢复会话。"""
        import os
        from mewcode.app import MewCodeApp

        bad = ProviderConfig(
            name="broken",
            protocol="anthropic",
            base_url="https://api.anthropic.com",
            model="claude-legacy-test",
            api_key="",
        )
        cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            app = MewCodeApp(providers=[bad])
            async with app.run_test() as pilot:
                await pilot.pause()
                assert app.agent is None

                good_slot = ProviderSlot(
                    provider=ProviderConfig(
                        name="recovered",
                        protocol="anthropic",
                        base_url="https://api.anthropic.com",
                        model="claude-legacy-test",
                        api_key="sk-test",
                    ),
                    source="project-env",
                )
                error = app.switch_provider(good_slot)
                await pilot.pause()
                assert error is None
                assert app.agent is not None
                assert app.agent.client is app.client
                assert app._selected_provider is good_slot.provider
        finally:
            os.chdir(cwd)
