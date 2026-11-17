"""Built-in Playwright MCP: canonical names, session switch, Auto trust."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp import types as mcp_types

from mewcode.config import MCPServerConfig, ProviderConfig
from mewcode.expert_group import ExpertExecutionPolicy
from mewcode.expert_web import _role_registry
from mewcode.mcp.manager import MCPManager
from mewcode.mcp.playwright_browser import (
    PLAYWRIGHT_SERVER_NAME,
    build_playwright_config,
    format_mcp_connect_error,
    install_runtime_mcp,
    is_blocked_mcp_tool,
    is_trusted_auto_browser_tool,
    merge_mcp_server_configs,
    persistent_profile_dir,
    select_pipeline_session_tools,
    should_install_builtin_playwright,
)
from mewcode.mcp.tool_wrapper import MCPToolWrapper
from mewcode.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from mewcode.plugins import ApplicationHost
from mewcode.plugins.runtime import AgentRuntimeRequest
from mewcode.plugins.services import AGENT_FACTORY
from mewcode.tools import ToolRegistry
from mewcode.tools.browser_session import BrowserSessionTool


def _mcp_tool(name: str, description: str = "d") -> mcp_types.Tool:
    return mcp_types.Tool(
        name=name,
        description=description,
        inputSchema={"type": "object", "properties": {}},
    )


def _playwright_tools() -> list[mcp_types.Tool]:
    return [
        _mcp_tool("browser_navigate", "nav"),
        _mcp_tool("browser_snapshot", "snap"),
        _mcp_tool("browser_click", "click"),
        _mcp_tool("browser_type", "type"),
        _mcp_tool("browser_run_code_unsafe", "unsafe"),
    ]


def _mock_client(tools: list[mcp_types.Tool] | None = None) -> AsyncMock:
    client = AsyncMock()
    client.is_alive = True
    client.list_tools = AsyncMock(return_value=tools or _playwright_tools())
    client.close = AsyncMock()
    return client


class FakeTool:
    def __init__(self, name: str, interaction_kind: str = "") -> None:
        self.name = name
        self.interaction_kind = interaction_kind
        self.category = "command"


class _FakeAgentHost:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry


class FakeSession:
    def __init__(self, registry: ToolRegistry) -> None:
        self.agent = _FakeAgentHost(registry)


def executor_policy() -> ExpertExecutionPolicy:
    return ExpertExecutionPolicy(
        strategy="fast", allow_delegation=True, max_direct_agents=8
    )


def test_playwright_wrapper_uses_canonical_name() -> None:
    wrapper = MCPToolWrapper(
        "playwright",
        _mcp_tool("browser_navigate", "nav"),
        MagicMock(),
        canonical_names=True,
    )
    assert wrapper.name == "browser_navigate"
    assert wrapper.should_defer is False
    assert wrapper.interaction_kind == "browser"


def test_third_party_wrapper_keeps_prefix() -> None:
    wrapper = MCPToolWrapper(
        "github", _mcp_tool("search_issues"), MagicMock()
    )
    assert wrapper.name == "mcp_github_search_issues"
    assert wrapper.should_defer is True


def test_blocked_unsafe_tool() -> None:
    assert is_blocked_mcp_tool("browser_run_code_unsafe")
    assert not is_blocked_mcp_tool("browser_navigate")


def test_ephemeral_config_is_headless_isolated() -> None:
    cfg = build_playwright_config("ephemeral", env={})
    assert cfg.name == PLAYWRIGHT_SERVER_NAME
    assert cfg.command == "npx"
    assert "--headless" in cfg.args
    assert "--isolated" in cfg.args
    assert "--browser" not in cfg.args


def test_proxy_env_is_forwarded() -> None:
    cfg = build_playwright_config(
        "ephemeral", env={"HTTP_PROXY": "http://127.0.0.1:7897"}
    )
    assert "--proxy-server=http://127.0.0.1:7897" in cfg.args


def test_persistent_config_uses_chrome_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MEWCODE_USER_DATA_DIR", str(tmp_path))
    cfg = build_playwright_config("persistent", env={})
    profile = persistent_profile_dir()
    assert profile == tmp_path / "browser-profile"
    assert profile.is_dir()
    assert "--isolated" not in cfg.args
    assert "--headless" not in cfg.args
    assert "--browser" in cfg.args
    assert "chrome" in cfg.args
    assert "--user-data-dir" in cfg.args
    assert str(profile) in cfg.args


def test_installed_edge_selected_for_persistent_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mewcode.mcp import playwright_browser

    monkeypatch.setattr(playwright_browser.os, "name", "nt")
    monkeypatch.setenv("MEWCODE_USER_DATA_DIR", str(tmp_path / "data"))
    edge = tmp_path / "Microsoft/Edge/Application/msedge.exe"
    edge.parent.mkdir(parents=True)
    edge.touch()
    cfg = playwright_browser.build_playwright_config(
        "persistent", env={"PROGRAMFILES(X86)": str(tmp_path)}
    )
    assert "--browser" in cfg.args
    assert "msedge" in cfg.args


def test_skip_builtin_when_user_has_playwright() -> None:
    existing = [MCPServerConfig(name="playwright", command="echo")]
    assert should_install_builtin_playwright(existing) is False
    merged, builtin = merge_mcp_server_configs(existing)
    assert builtin is False
    assert merged == existing


@pytest.mark.asyncio
async def test_manager_registers_canonical_names_and_blocks_unsafe() -> None:
    manager = MCPManager()
    manager.load_configs([build_playwright_config("ephemeral", env={})])
    registry = ToolRegistry()
    with patch("mewcode.mcp.manager.MCPClient") as mock_cls:
        mock_cls.return_value = _mock_client()
        errors = await manager.register_all_tools(registry)
    assert errors == []
    assert registry.get("browser_navigate") is not None
    assert registry.get("browser_snapshot") is not None
    assert registry.get("mcp_playwright_browser_navigate") is None
    assert registry.get("browser_run_code_unsafe") is None
    names = {schema["name"] for schema in registry.get_all_schemas()}
    assert "browser_navigate" in names
    assert "browser_snapshot" in names


@pytest.mark.asyncio
async def test_replace_server_keeps_one_navigate() -> None:
    manager = MCPManager()
    registry = ToolRegistry()
    created: list[MCPServerConfig] = []

    def make_client(config: MCPServerConfig) -> AsyncMock:
        created.append(config)
        return _mock_client()

    with patch("mewcode.mcp.manager.MCPClient", side_effect=make_client):
        manager.load_configs([build_playwright_config("ephemeral", env={})])
        await manager.register_all_tools(registry)
        errors = await manager.replace_server(
            build_playwright_config("persistent", env={}), registry
        )
    assert errors == []
    tools = [tool for tool in registry.list_tools() if tool.name == "browser_navigate"]
    assert len(tools) == 1
    assert "--isolated" not in created[-1].args
    assert "chrome" in created[-1].args


@pytest.mark.asyncio
async def test_browser_session_switches_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MEWCODE_USER_DATA_DIR", str(tmp_path))
    manager = MCPManager()
    registry = ToolRegistry()
    seen: list[MCPServerConfig] = []

    def make_client(config: MCPServerConfig) -> AsyncMock:
        seen.append(config)
        return _mock_client()

    with patch("mewcode.mcp.manager.MCPClient", side_effect=make_client):
        manager.load_configs([build_playwright_config("ephemeral", env={})])
        await manager.register_all_tools(registry)
        tool = BrowserSessionTool(manager, registry)
        result = await tool.execute(tool.params_model(mode="persistent"))
    assert not result.is_error
    assert seen[-1].name == "playwright"
    assert "--isolated" not in seen[-1].args
    assert str(tmp_path / "browser-profile") in seen[-1].args
    names = [item.name for item in registry.list_tools() if item.name == "browser_navigate"]
    assert names == ["browser_navigate"]


def test_auto_allows_browser_click(tmp_path: Path) -> None:
    wrapper = MCPToolWrapper(
        "playwright",
        _mcp_tool("browser_click", "click"),
        MagicMock(),
        canonical_names=True,
    )
    checker = PermissionChecker(
        DangerousCommandDetector(),
        PathSandbox(str(tmp_path)),
        RuleEngine(),
        PermissionMode.AUTO,
    )
    decision = checker.check(wrapper, {"ref": "e1"})
    assert decision.effect == "allow"
    assert decision.source == "auto_browser_trust"
    assert decision.source != "hitl"


def test_unattended_pipeline_strips_click_type() -> None:
    tools = [
        FakeTool("browser_navigate", "browser"),
        FakeTool("browser_click", "browser"),
        FakeTool("browser_type", "browser"),
        FakeTool("ReadFile"),
    ]
    kept = select_pipeline_session_tools(tools, unattended=True)
    names = {tool.name for tool in kept}
    assert "browser_navigate" in names
    assert "ReadFile" in names
    assert "browser_click" not in names
    assert "browser_type" not in names


def test_manager_planner_do_not_get_playwright_click() -> None:
    parent = ToolRegistry()
    for name in (
        "ReadFile",
        "Glob",
        "Grep",
        "WebSearch",
        "Browser",
        "browser_navigate",
        "browser_click",
    ):
        parent.register(FakeTool(name, "browser" if name.startswith("browser_") else ""))
    session = FakeSession(parent)
    from dataclasses import replace
    readonly = replace(executor_policy(), node_tools=("ReadFile", "Glob", "Grep", "WebSearch", "Browser"))
    manager = _role_registry(session, "manager", readonly)
    planner = _role_registry(session, "planner", readonly)
    executor = _role_registry(session, "executor", executor_policy())
    assert manager.get("WebSearch") is not None
    assert manager.get("Browser") is not None
    assert manager.get("browser_click") is None
    assert planner.get("browser_click") is None
    assert executor.get("browser_navigate") is not None
    assert executor.get("browser_click") is not None


@pytest.mark.asyncio
async def test_install_runtime_mcp_registers_desktop_and_tui_path(
    tmp_path: Path,
) -> None:
    class ScriptedClient:
        def __init__(self) -> None:
            self.usage_recorder = None

        def set_usage_recorder(self, recorder) -> None:
            self.usage_recorder = recorder

        async def stream(self, *_args, **_kwargs):
            if False:
                yield None

    host = ApplicationHost(str(tmp_path), discover_plugins=False)
    await host.astart()
    client = ScriptedClient()
    try:
        with patch("mewcode.mcp.manager.MCPClient") as mock_cls:
            mock_cls.return_value = _mock_client()
            runtime = await host.context.get(AGENT_FACTORY).acreate(
                AgentRuntimeRequest(
                    provider=ProviderConfig(
                        name="test",
                        protocol="openai-compat",
                        base_url="http://localhost",
                        model="test-model",
                    ),
                    work_dir=str(tmp_path),
                    client=client,
                    auxiliary_client=client,
                    memory_manager=SimpleNamespace(load_always_on=lambda: ""),
                )
            )
            errors = await install_runtime_mcp(runtime, [])
            assert errors == []
            assert runtime.registry.get("browser_navigate") is not None
            assert runtime.registry.get("browser_snapshot") is not None
            assert runtime.registry.get("browser_session") is not None
            assert runtime.registry.get("mcp_playwright_browser_navigate") is None
            names = {schema["name"] for schema in runtime.registry.get_all_schemas()}
            assert "browser_navigate" in names
            assert "browser_snapshot" in names
            await runtime.aclose()
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_install_runtime_mcp_skips_second_playwright(tmp_path: Path) -> None:
    class ScriptedClient:
        def __init__(self) -> None:
            self.usage_recorder = None

        def set_usage_recorder(self, recorder) -> None:
            self.usage_recorder = recorder

        async def stream(self, *_args, **_kwargs):
            if False:
                yield None

    host = ApplicationHost(str(tmp_path), discover_plugins=False)
    await host.astart()
    seen: list[str] = []

    def make_client(config: MCPServerConfig) -> AsyncMock:
        seen.append(config.command or "")
        return _mock_client()

    try:
        with patch("mewcode.mcp.manager.MCPClient", side_effect=make_client):
            runtime = await host.context.get(AGENT_FACTORY).acreate(
                AgentRuntimeRequest(
                    provider=ProviderConfig(
                        name="test",
                        protocol="openai-compat",
                        base_url="http://localhost",
                        model="test-model",
                    ),
                    work_dir=str(tmp_path),
                    client=ScriptedClient(),
                    auxiliary_client=ScriptedClient(),
                    memory_manager=SimpleNamespace(load_always_on=lambda: ""),
                )
            )
            errors = await install_runtime_mcp(
                runtime,
                [MCPServerConfig(name="playwright", command="echo", args=["ok"])],
            )
            assert errors == []
            assert seen == ["echo"]
            assert runtime.context.get("mcp.builtin_playwright") is False
            await runtime.aclose()
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_missing_npx_is_soft_failure(tmp_path: Path) -> None:
    class ScriptedClient:
        def __init__(self) -> None:
            self.usage_recorder = None

        def set_usage_recorder(self, recorder) -> None:
            self.usage_recorder = recorder

        async def stream(self, *_args, **_kwargs):
            if False:
                yield None

    host = ApplicationHost(str(tmp_path), discover_plugins=False)
    await host.astart()
    try:
        with patch("mewcode.mcp.manager.MCPClient") as mock_cls:
            broken = AsyncMock()
            broken.connect.side_effect = FileNotFoundError("npx")
            mock_cls.return_value = broken
            runtime = await host.context.get(AGENT_FACTORY).acreate(
                AgentRuntimeRequest(
                    provider=ProviderConfig(
                        name="test",
                        protocol="openai-compat",
                        base_url="http://localhost",
                        model="test-model",
                    ),
                    work_dir=str(tmp_path),
                    client=ScriptedClient(),
                    auxiliary_client=ScriptedClient(),
                    memory_manager=SimpleNamespace(load_always_on=lambda: ""),
                )
            )
            errors = await install_runtime_mcp(runtime, [])
            assert runtime.agent is not None
            assert runtime.registry.get("ReadFile") is not None
            assert runtime.registry.get("browser_navigate") is None
            blob = "\n".join(errors)
            assert any(token in blob for token in ("npx", "Node", "Playwright"))
            await runtime.aclose()
    finally:
        await host.aclose()


def test_internal_mcp_failure_is_not_reported_as_missing_node() -> None:
    message = format_mcp_connect_error("playwright", KeyError("mcp.errors"))
    assert "KeyError" in message
    assert "mcp.errors" in message
    assert "Node/npx" not in message
    missing = format_mcp_connect_error("playwright", FileNotFoundError("npx"))
    assert "Node/npx" in missing


@pytest.mark.asyncio
async def test_install_runtime_missing_result_does_not_raise_keyerror() -> None:
    from mewcode.cordis import Context

    runtime = SimpleNamespace(context=Context(), ainstall=AsyncMock())
    errors = await install_runtime_mcp(runtime, [])
    assert len(errors) == 1
    assert "did not publish its result" in errors[0]
    assert "Node/npx" not in errors[0]
    runtime.context.close()


@pytest.mark.asyncio
async def test_install_runtime_retains_initialization_failure_reason() -> None:
    from mewcode.cordis import Context, PluginSpec

    context = Context()

    def broken(_ctx):
        raise RuntimeError("registration failed")

    with pytest.raises(RuntimeError, match="registration failed"):
        context.load(PluginSpec("runtime.mcp", broken))
    runtime = SimpleNamespace(context=context, ainstall=AsyncMock())
    errors = await install_runtime_mcp(runtime, [])
    assert "state=failed" in errors[0]
    assert "registration failed" in errors[0]
    context.close()


@pytest.mark.asyncio
async def test_desktop_ignores_result_from_replaced_runtime() -> None:
    import ast
    import asyncio
    import logging

    tree = ast.parse(Path("frontend/server.py").read_text(encoding="utf-8"))
    method = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.AsyncFunctionDef) and node.name == "_init_mcp")
    namespace = {"asyncio": asyncio, "log": logging.getLogger(__name__)}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "_init_mcp", "exec"), namespace)
    original = object()
    current = object()
    state = SimpleNamespace(agent_runtime=original, mcp_errors=["current state"],
                            _user_mcp_servers=lambda: [], conversation=MagicMock())

    async def replaced(_runtime, _configs):
        state.agent_runtime = current
        return ["stale startup failure"]

    with patch("mewcode.mcp.playwright_browser.install_runtime_mcp", side_effect=replaced):
        await namespace["_init_mcp"](state)
    assert state.mcp_errors == ["current state"]
    state.conversation.add_system_reminder.assert_not_called()


def test_tui_and_desktop_call_install_runtime_mcp() -> None:
    app_src = Path("mewcode/app.py").read_text(encoding="utf-8")
    server_src = Path("frontend/server.py").read_text(encoding="utf-8")
    assert "install_runtime_mcp" in app_src
    assert "install_runtime_mcp" in server_src


def test_prompts_mention_playwright_and_keep_http_search() -> None:
    text = Path("mewcode/prompts.py").read_text(encoding="utf-8")
    assert "browser_navigate" in text or "Playwright" in text
    assert "WebSearch" in text
    assert "WebFetch" in text


def test_trusted_auto_helper_does_not_match_http_browser_alias() -> None:
    assert is_trusted_auto_browser_tool("browser_click")
    assert is_trusted_auto_browser_tool("browser_session")
    assert not is_trusted_auto_browser_tool("browser")
    assert not is_trusted_auto_browser_tool("Browser")
