"""Host wiring for conversation-bound workflow planning tools."""
from types import SimpleNamespace

import pytest

from frontend import server
from mewcode.orchestration.workflow_blocks import WORKFLOW_BLOCK_GUIDANCE
from mewcode.tools import ToolRegistry
from mewcode.tools.workflow_blocks import (
    ReadWorkflowBlocks,
    ReadWorkflowBlocksParams,
    SubmitWorkflowBlocks,
    SubmitWorkflowBlocksParams,
)


def test_plain_chat_removes_design_pipeline_without_reading_design_stores(tmp_path, monkeypatch):
    from mewcode.workflow_core.chat_tools import DESIGN_TOOLS
    agent = SimpleNamespace(registry=ToolRegistry())
    for name in DESIGN_TOOLS:
        agent.registry.register(SimpleNamespace(name=name))
    agent.registry.register(SimpleNamespace(name='ReadFile'))
    owner = object.__new__(server.WebSession)
    owner.agent, owner.session = agent, SimpleNamespace(session_id='plain-chat')
    owner.scope, owner.work_dir = 'project', str(tmp_path)
    owner.orchestration_planning = False
    monkeypatch.setattr(server, '_conversation_plan_store', lambda *args: pytest.fail('ordinary chat loaded design'))
    owner._sync_agent_conversation_owner()
    assert all(agent.registry.get(name) is None for name in DESIGN_TOOLS)
    assert agent.registry.get('ReadFile') is not None


@pytest.mark.asyncio
async def test_web_session_binds_planning_tools_before_graph_and_rejects_old_session(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    first_session_dir = tmp_path / "sessions-first"
    second_session_dir = tmp_path / "sessions-second"
    first_id = "conversation-first"
    second_id = "conversation-second"
    store_calls = []

    class EmptyStore:
        def get(self, project_path):
            store_calls.append(("get", str(project_path)))
            return None

    def plan_store(conversation_id):
        store_calls.append(("plan", conversation_id))
        return EmptyStore()

    def flow_store(conversation_id):
        store_calls.append(("flow", conversation_id))
        return EmptyStore()

    class EmptyPipelineStore:
        def __init__(self, project_path):
            self.project_path = str(project_path)
            store_calls.append(("pipeline", self.project_path))

        def list_pipelines(self):
            return []

        def get_pipeline(self, graph_id):
            raise AssertionError("No graph ID should be queried when no graph exists")

    routes = [{"provider_key": "test-provider", "model_id": "test-model"}]
    monkeypatch.setattr(server, "_conversation_plan_store", plan_store)
    monkeypatch.setattr(server, "_conversation_flow_store", flow_store)
    monkeypatch.setattr(server, "PipelineStore", EmptyPipelineStore)
    monkeypatch.setattr(
        "mewcode.orchestration.graph_models.available_model_routes",
        lambda _registry: routes,
    )

    agent = SimpleNamespace(
        registry=ToolRegistry(),
        session_id="stale-before-sync",
        work_dir=str(project.resolve()),
        _chat_turn_id="turn-first",
    )
    session = object.__new__(server.WebSession)
    session.agent = agent
    session.session = SimpleNamespace(
        session_id=first_id,
        _sessions_dir=first_session_dir,
    )
    session.scope = "project"
    session.work_dir = str(project.resolve())
    session.orchestration_planning = True

    # The fake agent has no _install_runtime_tool, so wiring must use the real
    # ToolRegistry.register fallback without constructing a provider/runtime.
    session._sync_agent_conversation_owner()
    read_tool = agent.registry.get("ReadWorkflowBlocks")
    submit_tool = agent.registry.get("SubmitWorkflowBlocks")
    assert isinstance(read_tool, ReadWorkflowBlocks)
    assert isinstance(submit_tool, SubmitWorkflowBlocks)
    assert read_tool.blocks is submit_tool.blocks
    assert read_tool.blocks.project == str(project.resolve())
    assert read_tool.blocks.conversation_id == first_id
    assert read_tool.blocks.source_path == first_session_dir / f"{first_id}.jsonl"
    assert agent.session_id == first_id
    assert agent._conversation_scope == "project"
    assert agent._conversation_store_dir == str(project.resolve())
    assert read_tool.blocks.guard()

    owner_fields = {"project", "conversation_id", "session_id", "owner"}
    for tool in (read_tool, submit_tool):
        assert owner_fields.isdisjoint(tool.params_model.model_fields)
        assert owner_fields.isdisjoint(tool.get_schema()["input_schema"].get("properties", {}))

    # An empty set of conversation-owned stores and an empty pipeline inventory
    # prove the tools are available before any saved plan, flow, or graph exists.
    assert read_tool.blocks.base_spec() is None
    view = read_tool.blocks.read()
    assert view["available_model_routes"] == routes
    assert {tuple(sorted(service.items())) for service in view["available_tool_services"]} == {
        (("enabled", True), ("name", "ReadWorkflowBlocks")),
        (("enabled", True), ("name", "AdvanceTaskDesign")),
        (("enabled", True), ("name", "SubmitWorkflowBlocks")),
    }
    assert all("provider_key" in route and "name" not in route
               for route in view["available_model_routes"])
    assert all("name" in service and "provider_key" not in service
               for service in view["available_tool_services"])
    assert ("plan", first_id) in store_calls and ("flow", first_id) in store_calls
    assert ("pipeline", str(project.resolve())) in store_calls

    assert "每轮可用 ReadWorkflowBlocks 查询" in WORKFLOW_BLOCK_GUIDANCE
    assert "用 SubmitWorkflowBlocks 保存" in WORKFLOW_BLOCK_GUIDANCE
    assert "先读再改" in WORKFLOW_BLOCK_GUIDANCE

    old_submit = submit_tool
    session.session = SimpleNamespace(
        session_id=second_id,
        _sessions_dir=second_session_dir,
    )
    session._sync_agent_conversation_owner()
    current_read = agent.registry.get("ReadWorkflowBlocks")
    current_submit = agent.registry.get("SubmitWorkflowBlocks")
    assert current_read is not read_tool and current_submit is not submit_tool
    assert current_read.blocks.conversation_id == second_id
    assert current_read.blocks.source_path == second_session_dir / f"{second_id}.jsonl"
    assert current_read.blocks.guard()
    assert not read_tool.blocks.guard()
    assert not old_submit.blocks.guard()

    stale_read = await read_tool.execute(ReadWorkflowBlocksParams())
    assert stale_read.is_error and "项目或对话已切换" in stale_read.output
    stale_submit = await old_submit.execute(SubmitWorkflowBlocksParams(
        expected_sequence=0,
        source_hash="stale-source",
        base_graph_hash="stale-canvas",
    ))
    assert stale_submit.is_error and "项目或对话已切换" in stale_submit.output
