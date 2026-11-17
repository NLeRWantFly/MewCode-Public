"""/effort 与 /model 功能测试——client effort 映射、命令处理器、TUI 切换。"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from mewcode.client import (
    EFFORT_LEVELS,
    AnthropicClient,
    OpenAIClient,
    OpenAICompatClient,
)
from mewcode.commands.handlers.effort import handle_effort
from mewcode.commands.handlers.model import handle_model
from mewcode.commands.registry import CommandContext
from mewcode.config import ProviderConfig


def _anthropic_config(**kw) -> ProviderConfig:
    base = dict(
        name="main",
        protocol="anthropic",
        base_url="https://api.anthropic.com",
        model="claude-legacy-test",
        api_key="sk-test",
    )
    base.update(kw)
    return ProviderConfig(**base)


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


# ---------------------------------------------------------------------------
# client effort 行为
# ---------------------------------------------------------------------------

class TestClientEffort:
    def test_levels_cover_all_protocols(self) -> None:
        for proto in ("anthropic", "openai", "openai-compat"):
            assert EFFORT_LEVELS[proto], proto
        assert "off" in EFFORT_LEVELS["anthropic"]
        assert "off" not in EFFORT_LEVELS["openai"]

    def test_anthropic_effort_from_thinking_config(self) -> None:
        c = AnthropicClient(_anthropic_config(thinking=True))
        assert c.effort == "high"
        c2 = AnthropicClient(_anthropic_config(thinking=False))
        assert c2.effort == "off"

    def test_anthropic_set_effort_syncs_thinking_flag(self) -> None:
        c = AnthropicClient(_anthropic_config(thinking=False))
        assert c.thinking is False
        c.set_effort("medium")
        assert c.effort == "medium"
        assert c.thinking is True
        c.set_effort("off")
        assert c.effort == "off"
        assert c.thinking is False

    def test_anthropic_invalid_level_ignored(self) -> None:
        c = AnthropicClient(_anthropic_config(thinking=False))
        c.set_effort("ultra")
        assert c.effort == "off"

    def test_openai_effort_default_empty(self) -> None:
        c = OpenAIClient(ProviderConfig(
            name="o", protocol="openai",
            base_url="https://api.openai.com/v1",
            model="gpt-5", api_key="sk-test",
        ))
        assert c.effort == ""
        c.set_effort("high")
        assert c.effort == "high"
        c.set_effort("off")  # openai 无 off，忽略
        assert c.effort == "high"

    def test_openai_compat_effort(self) -> None:
        c = OpenAICompatClient(ProviderConfig(
            name="oc", protocol="openai-compat",
            base_url="https://proxy.example.com/v1",
            model="gpt-5", api_key="sk-test",
        ))
        c.set_effort("low")
        assert c.effort == "low"

    @pytest.mark.asyncio
    async def test_openai_responses_disables_server_storage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import mewcode.client as client_module
        from mewcode.conversation import ConversationManager
        from mewcode.tools.base import StreamEnd

        captured: dict = {}

        class FakeResponses:
            async def create(self, **kwargs):
                captured.update(kwargs)

                async def events():
                    yield SimpleNamespace(
                        type="response.completed",
                        response=SimpleNamespace(
                            usage=SimpleNamespace(
                                input_tokens=1,
                                output_tokens=1,
                                input_tokens_details=None,
                            )
                        ),
                    )

                return events()

        class FakeOpenAI:
            def __init__(self, **kwargs):
                captured["client_kwargs"] = kwargs
                self.responses = FakeResponses()

        monkeypatch.setattr(client_module, "AsyncOpenAI", FakeOpenAI)
        client = OpenAIClient(ProviderConfig(
            name="codex",
            protocol="openai",
            base_url="https://chatgpt.com/backend-api/codex",
            model="gpt-5.6-sol",
            api_key="oauth-token",
            default_headers={"ChatGPT-Account-Id": "account-123"},
        ))
        conversation = ConversationManager()
        conversation.add_user_message("hello")

        events = [event async for event in client.stream(conversation)]

        assert captured["store"] is False
        assert captured["stream"] is True
        assert captured["client_kwargs"]["default_headers"] == {
            "ChatGPT-Account-Id": "account-123",
            "User-Agent": "MewCode/0.1",
        }
        stream_end = next(event for event in events if isinstance(event, StreamEnd))
        assert stream_end.cache_usage_available is False

    @pytest.mark.asyncio
    async def test_openai_responses_exposes_xai_lifecycle_and_reasoning_stream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import mewcode.client as client_module
        from mewcode.conversation import ConversationManager
        from mewcode.tools.base import StreamEnd, StreamStatus, TextDelta, ThinkingDelta

        class FakeResponses:
            async def create(self, **_kwargs):
                async def events():
                    yield SimpleNamespace(type="response.created")
                    yield SimpleNamespace(type="response.in_progress")
                    yield SimpleNamespace(
                        type="response.reasoning_summary_text.delta",
                        delta="正在规划节点",
                    )
                    yield SimpleNamespace(
                        type="response.output_text.done",
                        text='{"nodes":[]}',
                    )
                    yield SimpleNamespace(type="response.output_item.done")
                    yield SimpleNamespace(
                        type="response.completed",
                        response=SimpleNamespace(
                            usage=SimpleNamespace(
                                input_tokens=10,
                                output_tokens=4,
                                input_tokens_details=None,
                            )
                        ),
                    )

                return events()

        class FakeOpenAI:
            def __init__(self, **_kwargs):
                self.responses = FakeResponses()

        monkeypatch.setattr(client_module, "AsyncOpenAI", FakeOpenAI)
        client = OpenAIClient(ProviderConfig(
            name="xai",
            protocol="openai",
            base_url="https://api.x.ai/v1",
            model="grok-4.6",
            api_key="xai-test",
        ))
        conversation = ConversationManager()
        conversation.add_user_message("生成阶段图")

        events = [event async for event in client.stream(conversation)]

        assert [event.stage for event in events if isinstance(event, StreamStatus)] == [
            "accepted", "processing", "activity"
        ]
        assert [event.text for event in events if isinstance(event, ThinkingDelta)] == [
            "正在规划节点"
        ]
        assert [event.kind for event in events if isinstance(event, ThinkingDelta)] == [
            "summary"
        ]
        assert [event.text for event in events if isinstance(event, TextDelta)] == [
            '{"nodes":[]}'
        ]
        assert any(isinstance(event, StreamEnd) for event in events)

    @pytest.mark.asyncio
    async def test_openai_responses_recovers_completed_item_text_and_keeps_raw_reasoning_private(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import mewcode.client as client_module
        from mewcode.conversation import ConversationManager
        from mewcode.tools.base import StreamEnd, TextDelta, ThinkingDelta

        class FakeResponses:
            async def create(self, **_kwargs):
                async def events():
                    yield SimpleNamespace(
                        type="response.reasoning.delta",
                        delta="internal reasoning",
                    )
                    yield SimpleNamespace(
                        type="response.output_item.done",
                        item=SimpleNamespace(
                            type="reasoning",
                            summary=[SimpleNamespace(
                                type="summary_text",
                                text="public summary",
                            )],
                        ),
                    )
                    yield SimpleNamespace(
                        type="response.output_item.done",
                        item=SimpleNamespace(
                            type="message",
                            content=[SimpleNamespace(
                                type="output_text",
                                text='{"nodes":[]}',
                            )],
                        ),
                    )
                    yield SimpleNamespace(
                        type="response.completed",
                        response=SimpleNamespace(
                            usage=SimpleNamespace(
                                input_tokens=10,
                                output_tokens=4,
                                input_tokens_details=None,
                            )
                        ),
                    )

                return events()

        class FakeOpenAI:
            def __init__(self, **_kwargs):
                self.responses = FakeResponses()

        monkeypatch.setattr(client_module, "AsyncOpenAI", FakeOpenAI)
        client = OpenAIClient(ProviderConfig(
            name="xai",
            protocol="openai",
            base_url="https://api.x.ai/v1",
            model="grok-4.6",
            api_key="xai-test",
        ))
        conversation = ConversationManager()
        conversation.add_user_message("生成阶段图")

        events = [event async for event in client.stream(conversation)]

        reasoning = [event for event in events if isinstance(event, ThinkingDelta)]
        assert [(event.text, event.kind) for event in reasoning] == [
            ("internal reasoning", "private"),
            ("public summary", "summary"),
        ]
        assert [event.text for event in events if isinstance(event, TextDelta)] == [
            '{"nodes":[]}'
        ]
        assert any(isinstance(event, StreamEnd) for event in events)

    @pytest.mark.asyncio
    async def test_public_gpt_5_6_uses_explicit_stable_prefix_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import mewcode.client as client_module
        from mewcode.conversation import ConversationManager
        from mewcode.tools.base import StreamEnd

        captured: dict = {}

        class FakeResponses:
            async def create(self, **kwargs):
                captured.update(kwargs)

                async def events():
                    yield SimpleNamespace(
                        type="response.completed",
                        response=SimpleNamespace(
                            usage=SimpleNamespace(
                                input_tokens=1400,
                                output_tokens=25,
                                input_tokens_details=SimpleNamespace(
                                    cached_tokens=1024,
                                    model_extra={"cache_write_tokens": 64},
                                ),
                            )
                        ),
                    )

                return events()

        class FakeOpenAI:
            def __init__(self, **kwargs):
                self.responses = FakeResponses()

        monkeypatch.setattr(client_module, "AsyncOpenAI", FakeOpenAI)
        client = OpenAIClient(ProviderConfig(
            name="openai",
            protocol="openai",
            base_url="https://api.openai.com/v1",
            model="gpt-5.6-sol",
            api_key="sk-test",
            service_tier="fast",
        ))
        conversation = ConversationManager()
        conversation.add_user_message(
            "Current working directory: C:\\repo\nOperating system: Windows"
        )
        conversation.add_user_message(
            "<system-reminder>\nAs you answer the user's questions, use this "
            "stable context.\n</system-reminder>"
        )
        conversation.add_user_message("dynamic question")

        events = [event async for event in client.stream(
            conversation,
            system="stable system",
            tools=[{"type": "function", "name": "ReadFile"}],
        )]

        assert captured["prompt_cache_key"].startswith("mewcode:")
        assert captured["service_tier"] == "fast"
        assert captured["extra_body"] == {
            "prompt_cache_options": {"mode": "implicit", "ttl": "30m"}
        }
        stable_block = captured["input"][1]["content"][0]
        assert stable_block["prompt_cache_breakpoint"] == {"mode": "explicit"}
        rolling_block = captured["input"][2]["content"][0]
        assert rolling_block["text"] == "dynamic question"
        assert rolling_block["prompt_cache_breakpoint"] == {"mode": "explicit"}
        stream_end = next(event for event in events if isinstance(event, StreamEnd))
        assert stream_end.input_tokens == 312
        assert stream_end.cache_read == 1024
        assert stream_end.cache_creation == 64
        assert stream_end.cache_usage_available is True
        assert stream_end.cache_mode == "implicit+explicit"
        assert stream_end.cache_breakpoints == 2
        assert stream_end.cache_key == captured["prompt_cache_key"]

        first_key = captured["prompt_cache_key"]
        second = ConversationManager()
        second.add_user_message(
            "Current working directory: C:\\repo\nOperating system: Windows"
        )
        second.add_user_message(
            "<system-reminder>\nAs you answer the user's questions, use this "
            "stable context.\n</system-reminder>"
        )
        second.add_user_message("a different dynamic question")
        _ = [event async for event in client.stream(
            second,
            system="stable system",
            tools=[{"type": "function", "name": "ReadFile"}],
        )]
        assert captured["prompt_cache_key"] == first_key

    @pytest.mark.asyncio
    async def test_codex_backend_omits_public_prompt_cache_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import mewcode.client as client_module
        from mewcode.conversation import ConversationManager

        captured: dict = {}

        class FakeResponses:
            async def create(self, **kwargs):
                captured.update(kwargs)

                async def events():
                    yield SimpleNamespace(
                        type="response.completed",
                        response=SimpleNamespace(
                            usage=SimpleNamespace(
                                input_tokens=1200,
                                output_tokens=8,
                                input_tokens_details=SimpleNamespace(
                                    cached_tokens=0
                                ),
                            )
                        ),
                    )

                return events()

        class FakeOpenAI:
            def __init__(self, **kwargs):
                self.responses = FakeResponses()

        monkeypatch.setattr(client_module, "AsyncOpenAI", FakeOpenAI)
        client = OpenAIClient(ProviderConfig(
            name="codex",
            protocol="openai",
            base_url="https://chatgpt.com/backend-api/codex",
            model="gpt-5.6-sol",
            api_key="oauth-token",
            service_tier="default",
        ))
        conversation = ConversationManager()
        conversation.add_user_message("Current working directory: C:\\repo")
        conversation.add_user_message("dynamic question")

        _ = [event async for event in client.stream(conversation)]

        assert captured["prompt_cache_key"].startswith("mewcode:")
        assert captured["service_tier"] == "default"
        assert "extra_body" not in captured
        assert isinstance(captured["input"][0]["content"], str)
        assert captured["input"][0]["content"].startswith(
            "Current working directory:"
        )
        assert "prompt_cache_breakpoint" not in str(captured["input"])

    @pytest.mark.asyncio
    async def test_pre_gpt_5_6_does_not_send_new_cache_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import mewcode.client as client_module
        from mewcode.conversation import ConversationManager
        from mewcode.tools.base import StreamEnd

        captured: dict = {}

        class FakeResponses:
            async def create(self, **kwargs):
                captured.update(kwargs)

                async def events():
                    yield SimpleNamespace(
                        type="response.completed",
                        response=SimpleNamespace(
                            usage=SimpleNamespace(
                                input_tokens=20,
                                output_tokens=2,
                                input_tokens_details=SimpleNamespace(
                                    cached_tokens=0
                                ),
                            )
                        ),
                    )

                return events()

        class FakeOpenAI:
            def __init__(self, **kwargs):
                self.responses = FakeResponses()

        monkeypatch.setattr(client_module, "AsyncOpenAI", FakeOpenAI)
        client = OpenAIClient(ProviderConfig(
            name="openai",
            protocol="openai",
            base_url="https://api.openai.com/v1",
            model="gpt-5.5",
            api_key="sk-test",
        ))
        conversation = ConversationManager()
        conversation.add_user_message("Current working directory: C:\\repo")
        conversation.add_user_message("question")

        events = [event async for event in client.stream(conversation)]

        assert "prompt_cache_key" not in captured
        assert "extra_body" not in captured
        assert captured["input"][0]["content"].startswith(
            "Current working directory:"
        )
        stream_end = next(event for event in events if isinstance(event, StreamEnd))
        assert stream_end.cache_read == 0
        assert stream_end.cache_usage_available is True


# ---------------------------------------------------------------------------
# /effort 命令
# ---------------------------------------------------------------------------

class EffortMockUI:
    def __init__(self, levels: list[str] | None = None):
        self.messages: list[str] = []
        self.menu_shown = 0
        self.effort_set: list[str] = []
        self._levels = levels if levels is not None else ["off", "low", "medium", "high"]
        self._effort = ""

    def add_system_message(self, text: str) -> None:
        self.messages.append(text)

    def get_effort_levels(self) -> list[str]:
        return list(self._levels)

    def show_effort_menu(self) -> None:
        self.menu_shown += 1

    def set_effort(self, level: str) -> str | None:
        if level not in self._levels:
            return f"无效 effort 级别: {level}"
        self._effort = level
        self.effort_set.append(level)
        return None


class TestEffortCommand:
    def test_no_args_shows_menu(self) -> None:
        ui = EffortMockUI()
        asyncio.run(handle_effort(_ctx("", ui)))
        assert ui.menu_shown == 1

    def test_list_marks_current(self) -> None:
        ui = EffortMockUI()
        ui._effort = "low"
        asyncio.run(handle_effort(_ctx("list", ui)))
        text = ui.messages[0]
        assert "* low" in text
        assert "high" in text

    def test_set_valid_level(self) -> None:
        ui = EffortMockUI()
        asyncio.run(handle_effort(_ctx("high", ui)))
        assert ui.effort_set == ["high"]
        assert any("high" in m for m in ui.messages)

    def test_set_invalid_level(self) -> None:
        ui = EffortMockUI()
        asyncio.run(handle_effort(_ctx("ultra", ui)))
        assert not ui.effort_set
        assert any("无效" in m for m in ui.messages)

    def test_provider_not_ready(self) -> None:
        ui = EffortMockUI(levels=[])
        asyncio.run(handle_effort(_ctx("", ui)))
        assert any("未就绪" in m for m in ui.messages)

    def test_unsupported_ui(self) -> None:
        class PlainUI:
            def __init__(self) -> None:
                self.messages: list[str] = []

            def add_system_message(self, text: str) -> None:
                self.messages.append(text)

        ui = PlainUI()
        asyncio.run(handle_effort(_ctx("", ui)))
        assert any("不支持" in m for m in ui.messages)


# ---------------------------------------------------------------------------
# /model 命令
# ---------------------------------------------------------------------------

class ModelMockUI:
    def __init__(self, models: list[str] | None = None, with_provider: bool = True):
        self.messages: list[str] = []
        self.menu_models: list[str] | None = None
        self.switched: list[str] = []
        self._models = models
        self._selected_provider = (
            _anthropic_config() if with_provider else None
        )

    def add_system_message(self, text: str) -> None:
        self.messages.append(text)

    async def list_available_models(self) -> list[str] | None:
        return self._models

    def show_model_menu(self, models: list[str]) -> None:
        self.menu_models = models

    def switch_model(self, model: str) -> str | None:
        self.switched.append(model)
        return None


class TestModelCommand:
    def test_no_args_fetches_and_shows_menu(self) -> None:
        ui = ModelMockUI(models=["m1", "m2"])
        asyncio.run(handle_model(_ctx("", ui)))
        assert ui.menu_models == ["m1", "m2"]

    def test_no_args_list_fetch_failed(self) -> None:
        ui = ModelMockUI(models=None)
        asyncio.run(handle_model(_ctx("", ui)))
        assert ui.menu_models is None
        assert any("未能" in m and "获取模型列表" in m for m in ui.messages)

    def test_list_marks_current(self) -> None:
        ui = ModelMockUI(models=["claude-legacy-test", "other"])
        asyncio.run(handle_model(_ctx("list", ui)))
        text = ui.messages[-1]
        assert "* claude-legacy-test" in text
        assert "other" in text

    def test_direct_switch(self) -> None:
        ui = ModelMockUI()
        asyncio.run(handle_model(_ctx("claude-opus-4-1", ui)))
        assert ui.switched == ["claude-opus-4-1"]

    def test_no_provider(self) -> None:
        ui = ModelMockUI(with_provider=False)
        asyncio.run(handle_model(_ctx("", ui)))
        assert any("未就绪" in m for m in ui.messages)


# ---------------------------------------------------------------------------
# TUI 集成：状态栏、effort、model 切换
# ---------------------------------------------------------------------------

class TestAppEffortModel:
    @pytest.mark.asyncio
    async def test_effort_and_model_flow(self, tmp_path, monkeypatch) -> None:
        import mewcode.app as app_module
        from mewcode.app import MewCodeApp

        async def offline_context_window(_provider) -> None:
            return None

        # MewCodeApp normally probes Anthropic's models endpoint in a
        # background worker. Keep this integration test fully offline.
        monkeypatch.setattr(app_module, "resolve_context_window", offline_context_window)
        monkeypatch.setenv("MEWCODE_USER_DATA_DIR", str(tmp_path / "user-data"))

        cfg = _anthropic_config()
        cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            app = MewCodeApp(providers=[cfg])
            async with app.run_test() as pilot:
                await pilot.pause()
                assert app.agent is not None

                # 状态栏新组件存在
                app.query_one("#context-label")
                app.query_one("#effort-label")
                app.query_one("#model-label")

                # effort：级别来自协议，设置后同步到 client 与状态栏
                assert app.get_effort_levels() == EFFORT_LEVELS["anthropic"]
                assert app.client.effort == "off"  # config.thinking 默认 False
                err = app.set_effort("low")
                assert err is None
                assert app._effort == "low"
                assert app.client.effort == "low"
                assert "effort:low" in str(app.query_one("#effort-label").render())

                err = app.set_effort("ultra")
                assert err is not None
                assert app._effort == "low"  # 未变

                # model：切换后 provider/client/agent 全部重连
                old_client = app.client
                err = app.switch_model("claude-opus-4-1")
                assert err is None
                assert cfg.model == "claude-legacy-test"  # External config is not rewritten.
                assert cfg._fetched_context_window == 0
                assert app._selected_provider is not None
                assert app._selected_provider.model == "claude-opus-4-1"
                assert app.client is not old_client
                assert app.agent.client is app.client
                assert app.client.model == "claude-opus-4-1"
                # 切换后 effort 重应用到新 client
                assert app.client.effort == "low"
                assert "claude-opus-4-1" in str(
                    app.query_one("#model-label").render()
                )

                # 切回同名模型：无操作，不报错
                assert app.switch_model("claude-opus-4-1") is None
        finally:
            os.chdir(cwd)

    @pytest.mark.asyncio
    async def test_switch_model_requires_ready_provider(self, tmp_path) -> None:
        from mewcode.app import MewCodeApp

        bad = _anthropic_config(api_key="")
        cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            app = MewCodeApp(providers=[bad])
            async with app.run_test() as pilot:
                await pilot.pause()
                assert app.agent is None
                err = app.switch_model("claude-opus-4-1")
                assert err is not None
                assert "未就绪" in err
        finally:
            os.chdir(cwd)
