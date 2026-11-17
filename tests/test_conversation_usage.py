from types import SimpleNamespace

import httpx
import pytest

from mewcode.usage.models import RequestUsage, TokenUsage
from mewcode.usage.service import UsageService


def test_grouping_scope_cached_and_reasoning_not_double_counted(tmp_path):
    ledger = UsageService(tmp_path).ledger
    one = RequestUsage(session_id="one", provider_name="provider", configured_model="alias", response_model="actual",
                       agent_id="main", tokens=TokenUsage(100, 20, 10, 40, 8))
    ledger.append(one)
    ledger.append(one)  # Same receipt remains one call.
    ledger.append(RequestUsage(session_id="one", provider_name="provider", response_model="actual", agent_id="child",
                               tokens=TokenUsage(5, 0, 0, 5)))
    ledger.append(RequestUsage(session_id="two", tokens=TokenUsage(999)))
    result = ledger.conversation_summary("one")
    assert result["request_count"] == 2 and result["tokens"]["total_tokens"] == 180
    assert result["tokens"]["reasoning_tokens"] == 8
    assert len(result["models"]) == 1 and result["models"][0]["agent_count"] == 2
    assert not ledger.conversation_summary("")["models"]


def test_missing_tokens_not_fabricated_and_provider_routes_separate(tmp_path):
    ledger = UsageService(tmp_path).ledger
    for provider in ("p1", "p2"):
        ledger.append(RequestUsage(session_id="one", provider_name=provider, configured_model="same"))
    result = ledger.conversation_summary("one")
    assert len(result["models"]) == 2 and result["unreported_token_requests"] == 2
    assert result["tokens"]["total_tokens"] == 0
    assert "base_url" not in str(result) and "credential" not in str(result)


@pytest.mark.asyncio
async def test_api_never_leaks_other_conversation(tmp_path, monkeypatch):
    from frontend import server
    service = UsageService(tmp_path)
    service.ledger.append(RequestUsage(session_id="active", configured_model="unit", tokens=TokenUsage(12)))
    monkeypatch.setattr(server, "SESSION", SimpleNamespace(session=SimpleNamespace(session_id="active"),
        runtime_for=lambda _: None, usage_service=service, work_dir=str(tmp_path)))
    monkeypatch.setattr(server, "DESKTOP_TOKEN", "")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.get("/api/chat/usage?conversation_id=active")
        assert response.headers["cache-control"] == "no-store"
        data = response.json()
        assert data["request_count"] == 1 and data["tokens"]["total_tokens"] == 12
        assert (await client.get("/api/chat/usage?conversation_id=other")).status_code == 404
        assert (await client.get("/api/chat/usage?conversation_id=../x")).status_code == 400


def test_split_ledgers_deduplicate_receipts_and_read_new_writes(tmp_path):
    desktop = UsageService(tmp_path, ledger_path=tmp_path / "desktop.sqlite3").ledger
    project = UsageService(tmp_path).ledger
    receipt = RequestUsage(session_id="one", provider_name="p", response_model="m",
                           agent_id="main", tokens=TokenUsage(100, 20, 10, 40, 8))
    desktop.append(receipt)
    project.append(receipt)  # Copied receipt is not a second request.
    project.append(RequestUsage(session_id="one", provider_name="p", response_model="m",
                                agent_id="child", tokens=TokenUsage(5, 0, 0, 5)))
    project.append(RequestUsage(session_id="other", tokens=TokenUsage(9999)))
    paths = (project.path, desktop.path, project.path)
    result = desktop.conversation_summary("one", additional_paths=paths)
    assert result["request_count"] == 2
    assert result["tokens"]["total_tokens"] == 180
    assert result["tokens"]["reasoning_tokens"] == 8
    assert result["models"][0]["agent_count"] == 2
    assert not result["partial"]
    # Same amount/model but a different identity really is another request.
    project.append(RequestUsage(session_id="one", provider_name="p", response_model="m",
                                agent_id="child", tokens=TokenUsage(5, 0, 0, 5)))
    updated = desktop.conversation_summary("one", additional_paths=paths)
    assert updated["request_count"] == 3 and updated["tokens"]["total_tokens"] == 190
    assert desktop.conversation_summary("", additional_paths=paths)["request_count"] == 0


def test_summary_does_not_create_or_migrate_ledgers_and_reports_partial_reads(tmp_path, monkeypatch):
    ledger = UsageService(tmp_path).ledger
    assert ledger.conversation_summary("one")["request_count"] == 0
    assert not ledger.path.parent.exists()
    ledger.append(RequestUsage(session_id="one", tokens=TokenUsage(7)))
    monkeypatch.setattr(ledger, "_ensure_schema", lambda _: pytest.fail("GET must not migrate"))
    missing = tmp_path / "missing" / "ledger.sqlite3"
    broken = tmp_path / "broken.sqlite3"
    broken.write_bytes(b"not sqlite")
    data = ledger.conversation_summary("one", additional_paths=(missing, broken))
    assert data["tokens"]["total_tokens"] == 7 and data["partial"]
    assert not missing.parent.exists()
    assert str(tmp_path) not in str(data)


@pytest.mark.asyncio
async def test_api_background_conversation_uses_its_captured_route_and_project(tmp_path, monkeypatch):
    from frontend import server

    project = tmp_path / "project-a"
    captured = UsageService(project, ledger_path=tmp_path / "captured.sqlite3")
    old_graph = UsageService(project)
    selected = UsageService(tmp_path / "project-b")
    captured.ledger.append(RequestUsage(session_id="background", tokens=TokenUsage(10)))
    old_graph.ledger.append(RequestUsage(session_id="background", tokens=TokenUsage(20)))
    selected.ledger.append(RequestUsage(session_id="background", tokens=TokenUsage(999)))
    runtime = SimpleNamespace(work_dir=str(project), usage_service=captured)
    monkeypatch.setattr(server, "SESSION", SimpleNamespace(
        session=SimpleNamespace(session_id="active"), work_dir=str(tmp_path / "project-b"),
        runtime_for=lambda key: runtime if key == "background" else None, usage_service=selected,
    ))
    monkeypatch.setattr(server, "DESKTOP_TOKEN", "")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        first = (await client.get("/api/chat/usage?conversation_id=background")).json()
        assert first["request_count"] == 2 and first["tokens"]["total_tokens"] == 30
        old_graph.ledger.append(RequestUsage(session_id="background", tokens=TokenUsage(30)))
        fresh = (await client.get("/api/chat/usage?conversation_id=background")).json()
        assert fresh["request_count"] == 3 and fresh["tokens"]["total_tokens"] == 60


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["executor", "auditor"])
async def test_captured_runtime_keeps_desktop_usage_for_graph_and_shared_aux(tmp_path, role):
    from frontend import server
    from mewcode.config import ProviderConfig
    from mewcode.expert_group import ExpertExecutionPolicy
    from mewcode.expert_web import _build_role_agent
    from mewcode.model_registry import ModelRef
    from mewcode.plugins import ApplicationHost
    from mewcode.plugins.runtime import AgentRuntimeRequest
    from mewcode.plugins.services import AGENT_FACTORY, USAGE

    class Client:
        model = "test"
        base_url = "https://unit.invalid"

        def set_usage_recorder(self, recorder):
            self.recorder = recorder

        def get_usage_recorder(self):
            return getattr(self, "recorder", None)

    host = ApplicationHost(str(tmp_path), discover_plugins=False).start()
    config = ProviderConfig(name="test", protocol="openai-compat", model="test", base_url=Client.base_url)
    service = UsageService(tmp_path, ledger_path=tmp_path / "desktop.sqlite3")
    root = child = None
    try:
        root = host.context.get(AGENT_FACTORY).create(AgentRuntimeRequest(
            provider=config, client=Client(), auxiliary_client=Client(), work_dir=str(tmp_path),
            usage_service=service, enable_memory=False, enable_skills=False,
        ))
        owner = SimpleNamespace(
            context=host.context, session=SimpleNamespace(session_id="chat-a"), agent=root.agent,
            client=root.client, agent_runtime=root, conversation=root.conversation, conversation_runs={},
            scope="project", work_dir=str(tmp_path), slot=None, _effective_effort=lambda: "auto",
            context_tier=64000, multi_agent=False, expert_mode=True, active_goal=lambda: "",
            task_manager=None, trace_manager=None, _agent_tool=None, worktree_manager=None,
            team_manager=None, permission_before_plan="auto", permission_before_orch=None, usage_service=service,
        )
        captured = server.WebSession.capture_conversation_runtime(owner)
        assert captured.usage_service is service
        # Switching the visible chat must not reroute an already running graph.
        owner.usage_service = UsageService(tmp_path / "other")
        owner.work_dir = str(tmp_path / "other")
        registry = SimpleNamespace(resolve=lambda *a, **k: SimpleNamespace(
            config=config, client=Client(), context_window=64000,
        ))
        policy = ExpertExecutionPolicy("stable", False, 0, node_tools=("ReadFile",))
        agent, _, child = _build_role_agent(captured, registry, role, ModelRef("local", "test"), policy)
        assert child.context.get(USAGE) is service
        for client in (agent.client, agent.auxiliary_client, root.agent.auxiliary_client):
            assert client.get_usage_recorder().__self__ is service
        agent.client.get_usage_recorder()(RequestUsage(session_id="chat-a", tokens=TokenUsage(11)))
        root.agent.auxiliary_client.get_usage_recorder()(RequestUsage(session_id="chat-a", tokens=TokenUsage(7)))
        assert service.ledger.conversation_summary("chat-a")["tokens"]["total_tokens"] == 18
        assert not UsageService(tmp_path).ledger.path.exists()
        assert not owner.usage_service.ledger.path.exists()
    finally:
        if child:
            await child.aclose()
        if root:
            await root.aclose()
        await host.aclose()
