"""Subagent model binding and background-task ownership regressions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from mewcode.agents.parser import AgentDef
from mewcode.agents.task_manager import BackgroundTask, TaskManager
from mewcode.config import ProviderConfig
from mewcode.model_registry import ProviderRegistry
from mewcode.provider_sources import ProviderSlot
from mewcode.tools.agent_tool import AgentTool, AgentToolParams


class _Client:
    def __init__(self, config: ProviderConfig) -> None:
        self.model = config.model
        self.recorder = None

    def set_usage_recorder(self, recorder) -> None:
        self.recorder = recorder

    def get_usage_recorder(self):
        return self.recorder

    def set_effort(self, _effort: str) -> None:
        pass


def _registry(*, codex: bool) -> ProviderRegistry:
    registry = ProviderRegistry(client_factory=_Client)
    go = ProviderSlot(
        provider=ProviderConfig(
            name="opencode-go", protocol="openai-compat",
            provider_type="opencode-go", base_url="https://go.example/v1",
            model="gpt-6-luna", api_key="test",
        ),
        source="config", models=["gpt-6-luna"],
    )
    slots = [go]
    if codex:
        slots.append(ProviderSlot(
            provider=ProviderConfig(
                name="codex-custom", protocol="openai",
                base_url="https://codex.example/v1",
                model="gpt-6-sol", api_key="test",
            ),
            source="codex", models=["gpt-6-sol", "gpt-6-luna"],
        ))
    registry.refresh_discovered(slots)
    return registry


def _agent_tool(registry: ProviderRegistry) -> AgentTool:
    tool = AgentTool.__new__(AgentTool)
    tool._provider_registry = registry
    tool._provider_config = registry.get("config:opencode-go").provider
    tool._parent_agent = SimpleNamespace(client=_Client(tool._provider_config))
    return tool


def test_model_only_luna_uses_codex_custom_instead_of_parent_go() -> None:
    registry = _registry(codex=True)
    tool = _agent_tool(registry)
    runtime = tool._select_runtime(
        AgentToolParams(prompt="review", description="review", model="gpt-6-luna"),
        AgentDef(agent_type="review", when_to_use="review"),
    )
    assert runtime.provider_key == "codex:codex-custom"
    assert runtime.config.base_url == "https://codex.example/v1"
    assert runtime.client.model == "gpt-6-luna"


def test_model_only_gpt_61_sol_prefers_configured_codex_provider() -> None:
    registry = _registry(codex=True)
    relay = ProviderSlot(
        provider=ProviderConfig(
            name="relay", protocol="openai-compat", base_url="https://relay.example/v1",
            model="gpt-6.1-sol", api_key="test",
        ),
        source="config", models=["gpt-6.1-sol"],
    )
    codex = ProviderSlot(
        provider=registry.get("codex:codex-custom").provider,
        source="codex", models=["gpt-6-sol", "gpt-6.1-sol"],
    )
    registry.refresh_discovered([relay, codex])
    tool = AgentTool.__new__(AgentTool)
    tool._provider_registry = registry
    tool._provider_config = relay.provider
    tool._parent_agent = SimpleNamespace(client=_Client(relay.provider))
    runtime = tool._select_runtime(
        AgentToolParams(prompt="check", description="check", model="gpt-6.1-sol"),
        AgentDef(agent_type="review", when_to_use="review"),
    )
    assert runtime.provider_key == "codex:codex-custom"
    assert runtime.config.base_url == "https://codex.example/v1"
    assert runtime.client.model == "gpt-6.1-sol"


def test_model_only_luna_never_silently_falls_back_to_go() -> None:
    tool = _agent_tool(_registry(codex=False))
    with pytest.raises(ValueError, match="没有可用的非 OpenCode Go Provider"):
        tool._select_runtime(
            AgentToolParams(prompt="review", description="review", model="gpt-6-luna"),
            AgentDef(agent_type="review", when_to_use="review"),
        )


def test_display_name_provider_alias_resolves_to_unique_codex_key() -> None:
    tool = _agent_tool(_registry(codex=True))
    runtime = tool._select_runtime(
        AgentToolParams(
            prompt="review", description="review", model="gpt-6-luna",
            provider_key="codex-custom",
        ),
        AgentDef(agent_type="review", when_to_use="review"),
    )
    assert runtime.provider_key == "codex:codex-custom"


def test_background_task_owner_roundtrips(tmp_path: Path) -> None:
    state = tmp_path / "tasks.json"
    manager = TaskManager(state)
    manager._tasks["child"] = BackgroundTask(
        id="child", name="review", agent=None, task="review",
        status="completed", owner_session_id="conversation",
        owner_task_id="task-a", owner_task_title="核查证据",
    )
    manager._persist_state()
    restored = TaskManager(state)
    assert restored.get("child").owner_task_id == "task-a"
    assert restored.get("child").owner_task_title == "核查证据"


def test_background_panel_groups_children_by_parent_task(tmp_path: Path) -> None:
    from frontend import server

    session = server.WebSession.__new__(server.WebSession)
    session.session = SimpleNamespace(session_id="conversation")
    session.scope = "project"
    session.work_dir = str(tmp_path)
    session._agent_tool = None
    session.task_manager = TaskManager()
    for task_id, owner_task_id, title in (
        ("child-a", "task-a", "核查证据"),
        ("child-b", "task-a", "核查证据"),
        ("child-c", "task-b", "修复接口"),
    ):
        session.task_manager._tasks[task_id] = BackgroundTask(
            id=task_id, name=task_id, agent=None, task=title,
            owner_session_id="conversation", owner_store_dir=str(tmp_path),
            owner_task_id=owner_task_id, owner_task_title=title,
        )
    groups = session.agent_tasks_payload()["groups"]
    assert len(groups) == 2
    by_key = {group["key"]: group for group in groups}
    assert {row["task_id"] for row in by_key["task:task-a"]["rows"]} == {
        "child-a", "child-b",
    }
    assert by_key["task:task-b"]["title"] == "任务 · 修复接口"


@pytest.mark.asyncio
async def test_cancel_endpoint_rejects_other_conversation_task(monkeypatch, tmp_path: Path) -> None:
    from frontend import server

    manager = TaskManager()
    manager._tasks["other"] = BackgroundTask(
        id="other", name="other", agent=None, task="other",
        owner_session_id="other-conversation", owner_store_dir=str(tmp_path),
    )
    monkeypatch.setattr(server, "SESSION", SimpleNamespace(
        task_manager=manager,
        session=SimpleNamespace(session_id="conversation"),
        _conversation_store_dir=lambda: str(tmp_path),
    ))
    response = await server.api_cancel_task("other")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_cancel_endpoint_stops_owned_running_child_after_chat_interrupt(
    monkeypatch, tmp_path: Path,
) -> None:
    from frontend import server

    task = BackgroundTask(
        id="old-child", name="old child", agent=None, task="review",
        owner_session_id="conversation", owner_store_dir=str(tmp_path),
    )
    cancelled: list[str] = []
    manager = SimpleNamespace(
        get=lambda task_id: task if task_id == task.id else None,
        cancel=lambda task_id: cancelled.append(task_id) or True,
    )
    monkeypatch.setattr(server, "SESSION", SimpleNamespace(
        task_manager=manager,
        session=SimpleNamespace(session_id="conversation"),
        _conversation_store_dir=lambda: str(tmp_path),
    ))
    response = await server.api_cancel_task(task.id)
    assert response.status_code == 200
    assert cancelled == ["old-child"]
