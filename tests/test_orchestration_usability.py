from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mewcode.conversation import ConversationManager
from mewcode.orchestration import get_template, to_expert_flow, to_pipeline_graph
from mewcode.orchestration.pipeline_bridge import from_pipeline_graph, sync_orchestration_graph_from_spec
from mewcode.orchestration.validate import validate_spec
from mewcode.orchestration.process_files import capture_process_file, add_process_file
from mewcode.orchestration.interventions import enqueue_intervention, deliver_interventions
from mewcode.permissions.sandbox import PathSandbox


def test_executable_subflows_remain_scheduled_after_canvas_roundtrips():
    spec = get_template("nested_delivery")
    validate_spec(spec)
    flow = to_expert_flow(spec)
    assert len(flow.nodes) == len(spec.nodes) == 11
    assert not flow.meta.get("schedule_skipped_node_ids")
    flow.validate()
    graph = to_pipeline_graph(spec)
    for _ in range(3):
        spec = from_pipeline_graph(graph)
        assert spec.meta["canvas_groups"]["sources"]["mode"] == "executable"
        assert len(to_expert_flow(spec).nodes) == 11
        graph = sync_orchestration_graph_from_spec(graph, spec)
    edges = {(edge.from_node, edge.to_node) for edge in to_expert_flow(spec).edges}
    assert ("sources_handoff", "production") in edges
    assert ("production_assets", "production_check") in edges
    assert ("production_draft", "production_check") in edges


def test_process_file_capture_is_verified_bounded_and_immutable(tmp_path):
    target = tmp_path / "report.html"
    target.write_text("<script>alert('not executed')</script>", encoding="utf-8")
    args = dict(tool_name="WriteFile", arguments={"file_path": str(target)}, is_error=False,
                work_dir=str(tmp_path), sandbox=PathSandbox(str(tmp_path)), node_id="draft", tool_id="t1", attempt=2)
    record = capture_process_file(**args)
    assert record["preview_type"] == "text"
    assert record["node_attempt"] == 2
    assert record["sha256"] and record["accepted"] is False
    target.write_text("new content", encoding="utf-8")
    assert "<script>" in record["preview"]
    assert capture_process_file(**{**args, "is_error": True}) is None
    assert capture_process_file(**{**args, "tool_name": "Bash"}) is None
    assert capture_process_file(**{**args, "sandbox": SimpleNamespace(check=lambda *a, **kw: (False, "deny"))}) is None
    snapshot = {}
    add_process_file(snapshot, record)
    add_process_file(snapshot, record)
    assert len(snapshot["process_files"]) == 1
    assert "preview" not in snapshot["process_file_receipts"][0]


def test_reported_file_is_not_labeled_as_generated_or_accepted(tmp_path):
    target = tmp_path / "existing.bin"
    target.write_bytes(b"\x00binary")
    record = capture_process_file(tool_name="ReportArtifact", arguments={"file_path": str(target)},
        is_error=False, work_dir=str(tmp_path), sandbox=PathSandbox(str(tmp_path)), node_id="n", tool_id="t")
    assert record["source"] == "reported_existing" and not record["accepted"]
    assert record["preview_type"] == "none"


def test_exact_file_grants_reach_existing_nodes_without_authorizing_siblings(tmp_path):
    import os
    project = tmp_path / "project"
    external = tmp_path / "external"
    project.mkdir()
    external.mkdir()
    target = external / "chosen.txt"
    sibling = external / "other.txt"
    target.write_text("chosen", encoding="utf-8")
    sibling.write_text("other", encoding="utf-8")
    grants = {}
    first = PathSandbox(str(project), file_grants=grants)
    second = PathSandbox(str(project), file_grants=grants)
    # Exclude the platform temp-root allowance so writes exercise exact grants.
    first._allowed_roots = second._allowed_roots = [project.resolve()]
    assert first.check(str(target), read_only=True)[0]
    assert not first.check(str(target))[0]
    first.grant_file(str(target))
    assert second.check(str(target), read_only=True)[0]
    assert not second.check(str(target))[0]
    # Explicit non-sensitive absolute reads are allowed, but the exact file
    # grant does not authorize writes to a sibling or the parent directory.
    assert second.check(str(sibling), read_only=True)[0]
    assert second.check(str(external), read_only=True)[0]
    assert not second.check(str(sibling))[0]
    assert not second.check(str(external))[0]
    first.grant_file(str(target), writable=True)
    assert second.check(str(target))[0]
    assert not second.check(str(sibling))[0]
    assert not second.check(str(external))[0]
    assert grants == {os.path.normcase(str(target.resolve())): True}
    with pytest.raises(ValueError):
        first.grant_file(str(external), writable=True)


def test_intervention_is_idempotent_delivered_to_each_node_without_approval():
    snapshot = {"status": "running"}
    assert enqueue_intervention(snapshot, "message-1", "请补充误差分析")
    assert not enqueue_intervention(snapshot, "message-1", "请补充误差分析")
    with pytest.raises(ValueError):
        enqueue_intervention(snapshot, "message-1", "其他内容")
    first, second = ConversationManager(), ConversationManager()
    assert deliver_interventions(snapshot, first, node_id="a", node_attempt=1) == ["message-1"]
    assert not deliver_interventions(snapshot, first, node_id="a", node_attempt=1)
    assert deliver_interventions(snapshot, second, node_id="b", node_attempt=1) == ["message-1"]
    assert "不自动授权" in first.history[-1].content
    assert len(snapshot["intervention_receipts"][0]["delivered_to"]) == 2
    snapshot["status"] = "completed"
    with pytest.raises(ValueError):
        enqueue_intervention(snapshot, "message-2", "late")


@pytest.mark.asyncio
@pytest.mark.parametrize("budget, expected_calls", [(2, 2), (1, 1)])
async def test_final_response_mailbox_continues_inside_existing_budget(tmp_path, budget, expected_calls):
    from tests.test_agent import MockLLMClient
    from mewcode.agent import Agent, ErrorEvent
    from mewcode.tools import ToolRegistry
    from mewcode.tools.base import TextDelta, StreamEnd
    client = MockLLMClient([
        [TextDelta("initial answer"), StreamEnd("end_turn", input_tokens=1, output_tokens=1)],
        [TextDelta("answer with new input"), StreamEnd("end_turn", input_tokens=1, output_tokens=1)],
    ])
    agent = Agent(client, ToolRegistry(), "anthropic", work_dir=str(tmp_path), max_iterations=budget)
    conversation = ConversationManager()
    conversation.add_user_message("initial task")
    pending = True
    async def input_callback():
        nonlocal pending
        if pending:
            pending = False
            conversation.add_user_message("new input received during response")
            return True
        return False
    agent.pending_user_input_callback = input_callback
    events = [event async for event in agent.run(conversation)]
    assert client._call_index == expected_calls
    if budget == 1:
        assert any(isinstance(event, ErrorEvent) and event.kind == "max_iterations" for event in events)
    else:
        assert conversation.history[-1].content == "answer with new input"


@pytest.mark.asyncio
async def test_skill_finder_and_instruction_only_load_preserve_tools(tmp_path, monkeypatch):
    from mewcode.skills.loader import SkillLoader
    from mewcode.skills.runtime import attach_skills
    from mewcode.tools import ToolRegistry
    from mewcode.tools.find_skills import FindSkillsParams
    from mewcode.tools.load_skill import LoadSkillParams
    loader = SkillLoader(str(tmp_path))
    agent = MagicMock()
    agent.registry = ToolRegistry()
    attach_skills(str(tmp_path), agent, agent.registry, loader=loader, instruction_only=True)
    found = await agent.registry.get("FindSkills").execute(FindSkillsParams(query="computer"))
    assert "computer-use" in found.output
    register = MagicMock(side_effect=AssertionError("must not install tools"))
    monkeypatch.setattr("mewcode.skills.directory.register_skill_tools", register)
    before = [tool.name for tool in agent.registry.list_tools()]
    result = await agent.registry.get("LoadSkill").execute(LoadSkillParams(name="computer-use"))
    assert not result.is_error
    assert [tool.name for tool in agent.registry.list_tools()] == before
    register.assert_not_called()
    agent.activate_skill.assert_called_once()


def test_main_runtime_registers_find_skills(tmp_path):
    import asyncio
    from mewcode.config import ProviderConfig
    from mewcode.plugins.host import AGENT_FACTORY, ApplicationHost
    from mewcode.plugins.runtime import AgentRuntimeRequest
    from mewcode.tools.find_skills import FindSkillsParams

    class LocalClient:
        model = "test"
        base_url = "http://127.0.0.1"
        def set_usage_recorder(self, recorder):
            self.usage_recorder = recorder
        async def stream(self, *_args, **_kwargs):
            if False:
                yield None

    async def scenario():
        host = ApplicationHost(str(tmp_path), discover_plugins=False)
        await host.astart()
        runtime = None
        try:
            client = LocalClient()
            runtime = await host.context.get(AGENT_FACTORY).acreate(AgentRuntimeRequest(
                provider=ProviderConfig(name="local", protocol="openai-compat", base_url="http://127.0.0.1", model="test"),
                work_dir=str(tmp_path), client=client, auxiliary_client=client,
                enable_memory=False, enable_tool_search=False,
            ))
            tool = runtime.registry.get("FindSkills")
            assert tool is not None
            result = await tool.execute(FindSkillsParams(query="computer"))
            assert "computer-use" in result.output
        finally:
            if runtime is not None:
                await runtime.aclose()
            await host.aclose()

    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_file_preview_and_grants_are_conversation_and_run_scoped(tmp_path, monkeypatch):
    import httpx
    from frontend import server
    from mewcode.expert_runs import ExpertRunStore
    from tests.test_expert_flow import _profile
    store = ExpertRunStore(tmp_path / "runs")
    run_id = "c" * 32
    store.create(run_id=run_id, conversation_id="visible", task="test", attachments=[], profile=_profile().as_dict(), strategy="stable")
    record = {"id": "receipt", "preview": "<script>test</script>", "preview_type": "text"}
    store.mutate(run_id, lambda value: value.update(process_files=[record]))
    monkeypatch.setattr(server, "EXPERT_RUNS", store)
    monkeypatch.setattr(server.SESSION, "session", SimpleNamespace(session_id="visible"))
    runtime = SimpleNamespace(conversation_id="visible", expert_mode=True, expert_run_id=run_id,
                              work_dir=str(tmp_path), file_access_grants={}, execution_kind="orchestration")
    monkeypatch.setattr(server.SESSION, "_active_queue_runtime", lambda identity: runtime)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        url = f"/api/expert-runs/{run_id}/files/receipt"
        assert (await client.get(url, params={"conversation_id": "other"})).status_code == 403
        response = await client.get(url, params={"conversation_id": "visible"})
        assert response.status_code == 200
        assert response.json()["file"] == record
        assert response.headers["cache-control"] == "no-store"
        body = {"conversation_id": "visible", "project": str(tmp_path), "run_id": run_id,
                "path": str(tmp_path), "access": "read"}
        assert (await client.post("/api/orchestration/file-grants", json=body)).status_code == 403
        response = await client.post("/api/orchestration/file-grants", json=body, headers={"X-Mew-File-Grant": "1"})
        assert response.status_code == 400  # directory is never an exact file grant
        target = tmp_path / "chosen.txt"
        target.write_text("ok", encoding="utf-8")
        body["path"] = str(target)
        body["run_id"] = "stale"
        assert (await client.post("/api/orchestration/file-grants", json=body, headers={"X-Mew-File-Grant": "1"})).status_code == 400
        body["run_id"] = run_id
        assert (await client.post("/api/orchestration/file-grants", json=body, headers={"X-Mew-File-Grant": "1"})).status_code == 200
        assert len(runtime.file_access_grants) == 1
