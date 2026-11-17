from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from mewcode.agents.parser import AgentDef
from mewcode.agents.tool_filter import build_teammate_tools, resolve_agent_tools
from mewcode.config import ProviderConfig
from mewcode.tools import ToolRegistry
from mewcode.tools.base import Tool, ToolResult
from mewcode.tools.capabilities import ProbeCapabilities, ProbeParams, inventory
from mewcode.tools.impl.invoke_deferred import InvokeDeferredTool
from mewcode.tools.impl.tool_search import ToolSearchParams, ToolSearchTool
from mewcode.tools.web_http import resolve_deepseek_search_auth
from mewcode.tools.web_search import Params, WebSearch


class EmptyParams(BaseModel):
    pass


class FakeTool(Tool):
    name = "mcp__test_read"
    category = "read"
    description = "test read"
    should_defer = True
    params_model = EmptyParams
    async def execute(self, params):
        return ToolResult("synthetic")


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("allow,deny", [("ReadFile", None), (None, "mcp__test_read"), (None, "*")])
def test_explicit_mcp_policy_applies_after_background_filter(background, allow, deny):
    parent = ToolRegistry()
    parent.register(FakeTool())
    definition = AgentDef(agent_type="Tester", when_to_use="test", system_prompt="test",
        tools=[allow] if allow else [], disallowed_tools=[deny] if deny else [])
    assert not resolve_agent_tools(parent, definition, background).is_enabled(FakeTool.name)


@pytest.mark.parametrize("backend", ["in-process", "external"])
def test_teammate_never_reenables_disabled_tool_or_parent_retry(backend):
    parent = ToolRegistry()
    parent.register(FakeTool())
    parent.disable(FakeTool.name)
    parent.register(SimpleNamespace(name="TaskRetry"))
    result = build_teammate_tools(parent, SimpleNamespace(), "team", "id", "worker", backend)
    assert not result.is_enabled(FakeTool.name) and not result.is_enabled("TaskRetry")


@pytest.mark.asyncio
async def test_select_disabled_tool_never_claims_loaded():
    registry = ToolRegistry()
    registry.register(FakeTool())
    registry.disable(FakeTool.name)
    tool = ToolSearchTool(registry)
    result = await tool.execute(ToolSearchParams(query="select:" + FakeTool.name))
    assert not registry.is_discovered(FakeTool.name)
    assert registry.find_deferred_by_names([FakeTool.name]) == []
    assert "<tool" not in result.output


@pytest.mark.asyncio
async def test_probe_distinguishes_registered_disconnected_and_live_ping(tmp_path):
    registry = ToolRegistry()
    transport = SimpleNamespace(is_alive=False, _session=SimpleNamespace(send_ping=AsyncMock()))
    for name in ("browser_snapshot", "browser_navigate"):
        registry.register(SimpleNamespace(name=name, _client=transport))
    agent = SimpleNamespace(registry=registry, work_dir=str(tmp_path), client=SimpleNamespace(model="local"))
    assert inventory(agent)["browser_tools_registered"] and not inventory(agent)["browser_connected"]
    tool = ProbeCapabilities(agent)
    await tool.execute(ProbeParams())
    transport._session.send_ping.assert_not_awaited()
    transport.is_alive = True
    result = json.loads((await tool.execute(ProbeParams(target="browser", live=True))).output)
    assert result["browser_connected"] and result["live_probe"]["status"] == "healthy"
    transport._session.send_ping.assert_awaited_once()
    registry.disable("browser_snapshot")
    assert not inventory(agent)["browser_connected"]


@pytest.mark.asyncio
async def test_models_probe_keeps_model_metadata_ahead_of_large_tool_inventory(tmp_path, monkeypatch):
    registry = ToolRegistry()
    for i in range(100):
        registry.register(SimpleNamespace(name=f"tool_{i:03d}", category="read", should_defer=False))
    model_rows = [{"provider_key": "configured:test", "model_id": "grok-4.7", "available": True,
                   "modalities": {"vision": True}, "review_only": False, "is_jev": False, "external": True}]
    provider = object()
    monkeypatch.setattr("mewcode.experts.capability_broker.model_inventory", lambda value: model_rows if value is provider else [])
    client = SimpleNamespace(model="local", list_models=AsyncMock())
    agent = SimpleNamespace(registry=registry, work_dir=str(tmp_path), client=client)
    result = await ProbeCapabilities(agent, provider).execute(ProbeParams(target="models", live=True))
    value = json.loads(result.output)
    assert value["models"] == model_rows and value["models_complete"] is True
    assert value["model_availability"] == "configured_metadata_only" and value["generation_tested"] is False
    assert "tools" not in value and len(result.output) < 10_000
    client.list_models.assert_not_awaited()
    assert len(registry.list_tools()) == 100


@pytest.mark.asyncio
async def test_models_probe_without_registry_does_not_claim_complete_inventory(tmp_path):
    agent = SimpleNamespace(registry=ToolRegistry(), work_dir=str(tmp_path), client=SimpleNamespace(model="local"))
    value = json.loads((await ProbeCapabilities(agent).execute(ProbeParams(target="models"))).output)
    assert value["models"] == [] and value["models_complete"] is False
    assert value["generation_tested"] is False


@pytest.mark.asyncio
async def test_models_probe_preserves_current_binding_when_catalog_omits_it(tmp_path, monkeypatch):
    monkeypatch.setattr('mewcode.experts.capability_broker.model_inventory', lambda _: [
        {'provider_key': 'relay', 'model_id': 'old-default', 'available': True}])
    client = SimpleNamespace(list_models=AsyncMock(), secret='must-not-leak')
    agent = SimpleNamespace(registry=ToolRegistry(), work_dir=str(tmp_path), client=client,
        runtime_model_ref={'provider_key': 'relay', 'model_id': 'advertised/new-model', 'effort': 'max',
                           'api_key': 'must-not-leak'})
    probe = ProbeCapabilities(agent, object())
    value = json.loads((await probe.execute(ProbeParams(target='models', live=True))).output)
    assert value['models_complete'] is False
    assert value['active_model'] == {'provider_key': 'relay', 'model_id': 'advertised/new-model',
        'effort': 'max', 'source': 'host_runtime_binding', 'present_in_configured_catalog': False,
        'generation_tested': False}
    assert 'must-not-leak' not in json.dumps(value)
    client.list_models.assert_not_awaited()
    agent.runtime_model_ref = {'provider_key': 'relay', 'model_id': 'old-default', 'effort': 'max'}
    fresh = json.loads((await probe.execute(ProbeParams(target='models'))).output)
    assert fresh['models_complete'] is True and fresh['active_model']['model_id'] == 'old-default'


@pytest.mark.asyncio
async def test_large_models_inventory_survives_fallback_and_supports_line_reads(tmp_path, monkeypatch):
    import re
    from pathlib import Path
    from mewcode.agent import Agent
    rows = [{"provider_key": "configured:test", "model_id": f"model-{i:03d}",
             "available": True, "modalities": {"vision": False}, "external": True,
             "review_only": False, "is_jev": False} for i in range(80)]
    monkeypatch.setattr("mewcode.experts.capability_broker.model_inventory", lambda value: rows)
    registry = ToolRegistry()
    runner = SimpleNamespace(registry=registry, session_dir=tmp_path)
    tool = ProbeCapabilities(runner, object())
    result = await tool.execute(ProbeParams(target="models"))
    assert 10_000 < len(result.output) < 50_000
    preview = Agent._maybe_persist_or_truncate(runner, "catalog", result.output)
    path = Path(re.search(r"完整内容已保存到：\n([^\n]+)", preview)[1])
    stored = path.read_text(encoding="utf-8")
    assert json.loads(stored)["models"] == rows
    assert len(stored.splitlines()) > len(rows)


@pytest.mark.asyncio
async def test_search_distinct_queries_overlap_and_identical_queries_coalesce():
    tool = WebSearch(auth_resolver=lambda _: ("", "", ""), tavily_auth_resolver=lambda _: "")
    active, peak, calls = 0, 0, []
    async def search(params):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        calls.append(params.query)
        await asyncio.sleep(.02)
        active -= 1
        return ToolResult(params.query)
    tool._search = search
    await asyncio.gather(*(tool.execute(Params(query=query)) for query in ["one", "one", "two", "three"]))
    assert peak == 3 and calls.count("one") == 1
    assert "cached search" in (await tool.execute(Params(query="one"))).output
    assert not tool._inflight


@pytest.mark.asyncio
async def test_search_waiter_cancellation_does_not_cancel_owner():
    tool = WebSearch(auth_resolver=lambda _: ("", "", ""), tavily_auth_resolver=lambda _: "")
    entered, release = asyncio.Event(), asyncio.Event()
    async def search(params):
        entered.set()
        await release.wait()
        return ToolResult("done")
    tool._search = search
    owner = asyncio.create_task(tool.execute(Params(query="same")))
    await entered.wait()
    waiter = asyncio.create_task(tool.execute(Params(query="same")))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not owner.done()
    release.set()
    assert (await owner).output == "done" and not tool._inflight


@pytest.mark.asyncio
async def test_tavily_payload_and_no_silent_filter_fallback():
    calls = []
    async def post(url, key, body):
        calls.append((url, key, body))
        return {"results": [{"title": "Python documentation", "url": "https://docs.python.org/", "content": "Python documentation release"}]}
    tool = WebSearch(post_json=post, auth_resolver=lambda _: ("", "", ""), tavily_auth_resolver=lambda _: "synthetic-key")
    params = Params(query="Python documentation", provider="tavily", include_domains=["docs.python.org"], time_range="week")
    result = await tool.execute(params)
    assert not result.is_error and "https://docs.python.org/" in result.output
    assert calls[0][0] == "https://api.tavily.com/search"
    assert calls[0][2]["include_domains"] == ["docs.python.org"] and calls[0][2]["time_range"] == "week"
    assert "synthetic-key" not in result.output
    tool = WebSearch(fetch_text=AsyncMock(), auth_resolver=lambda _: ("", "", ""), tavily_auth_resolver=lambda _: "")
    assert (await tool.execute(params.model_copy(update={"provider": "auto"}))).is_error
    tool._fetch_text.assert_not_awaited()


def test_desktop_search_credentials_rotate_and_delete_without_stale_yaml(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_SEARCH_MODEL", raising=False)
    config = ProviderConfig(name="DeepSeek", model="deepseek-search", protocol="openai-compat", api_key="synthetic-one", base_url="https://example.test")
    records = [SimpleNamespace(brand="deepseek", provider=config)]
    providers = SimpleNamespace(records=lambda: records)
    assert resolve_deepseek_search_auth(str(tmp_path), providers)[0] == "synthetic-one"
    config.api_key = "synthetic-two"
    assert resolve_deepseek_search_auth(str(tmp_path), providers)[0] == "synthetic-two"
    records.clear()
    assert resolve_deepseek_search_auth(str(tmp_path), providers)[0] == ""


@pytest.mark.asyncio
async def test_filtered_runtime_metadata_is_local_and_cannot_escape_scope(tmp_path):
    from mewcode.plugins.host import AGENT_FACTORY, ApplicationHost
    from mewcode.plugins.runtime import AgentRuntimeRequest
    from mewcode.tools.agent_status import AgentStatus
    client = SimpleNamespace(model="test", base_url="https://example.test", set_usage_recorder=lambda _: None)
    provider = ProviderConfig(name="local", model="test", protocol="openai-compat", base_url="https://example.test")
    parent = SimpleNamespace(registry=ToolRegistry())
    secret = FakeTool()
    parent.registry.register(secret)
    parent.registry.mark_discovered(secret.name)
    extras = (AgentStatus(parent), ProbeCapabilities(parent), ToolSearchTool(parent.registry), InvokeDeferredTool(parent.registry))
    host = ApplicationHost(str(tmp_path), discover_plugins=False).start()
    runtime = None
    try:
        runtime = host.context.get(AGENT_FACTORY).create(AgentRuntimeRequest(provider=provider, client=client,
            auxiliary_client=client, work_dir=str(tmp_path), include_default_tools=False, inherit_parent_tools=False,
            enable_skills=False, enable_tool_search=False, enable_memory=False, extra_tools=extras))
        assert runtime.registry.get("AgentStatus").agent is runtime.agent
        assert runtime.registry.get("ProbeCapabilities").agent is runtime.agent
        assert runtime.registry.get("ToolSearch")._registry is runtime.registry
        assert runtime.registry.get("InvokeDeferredTool").resolve(secret.name) is None
        assert runtime.registry.get("EvaluateEvidence") is None
    finally:
        if runtime:
            await runtime.aclose()
        await host.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["test", "deepseek-v4-flash"])
async def test_node_local_status_and_explicit_mcp_without_discovery_gateway(tmp_path, monkeypatch, model):
    from mewcode.expert_group import ExpertExecutionPolicy
    from mewcode.expert_web import _build_role_agent
    from mewcode.model_registry import ModelRef
    from mewcode.plugins.host import AGENT_FACTORY, ApplicationHost
    from mewcode.plugins.runtime import AgentRuntimeRequest
    from mewcode.tools.agent_status import TaskRetry
    monkeypatch.setenv("MEWCODE_USER_DATA_DIR", str(tmp_path / "runtime-state"))
    host = ApplicationHost(str(tmp_path), discover_plugins=False).start()
    config = ProviderConfig(name="test", protocol="openai-compat", model=model, base_url="https://example.test")
    client = SimpleNamespace(model=model, base_url="https://example.test", set_usage_recorder=lambda _: None,
                             get_usage_recorder=lambda: None)
    root = child = None
    try:
        root = host.context.get(AGENT_FACTORY).create(AgentRuntimeRequest(provider=config, client=client,
            auxiliary_client=client, work_dir=str(tmp_path), enable_memory=False))
        root.registry.register(FakeTool())
        root.registry.register(TaskRetry(root.agent, None, None))
        session = SimpleNamespace(agent=root.agent, context=root.context, work_dir=str(tmp_path), session=None,
                                  task_manager=None)
        service = SimpleNamespace(resolve=lambda *args, **kwargs: SimpleNamespace(config=config, client=client,
                                                                                  context_window=64000))
        policy = ExpertExecutionPolicy("stable", False, 0, node_tools=(FakeTool.name, "AgentStatus", "ProbeCapabilities"))
        agent, dispatcher, child = _build_role_agent(session, service, "executor", ModelRef("local", model), policy)
        assert dispatcher is None and not agent.registry.is_enabled("TaskRetry")
        assert not agent.registry.is_enabled("ToolSearch") and not agent.registry.is_enabled("InvokeDeferredTool")
        assert agent.registry.get("AgentStatus").agent is agent
        assert agent.registry.get("ProbeCapabilities").agent is agent
        assert agent.runtime_model_ref == {'provider_key': 'local', 'model_id': model, 'effort': ''}
        assert agent.registry.is_discovered(FakeTool.name)
        wire = agent.registry.get_context_schemas("openai-compat", context_channel="deepseek" if "deepseek" in model else "generic")
        assert FakeTool.name in {s["name"] for s in wire}
    finally:
        if child:
            await child.aclose()
        if root:
            await root.aclose()
        await host.aclose()
