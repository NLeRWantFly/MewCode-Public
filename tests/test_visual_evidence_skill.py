from types import SimpleNamespace

import pytest

from mewcode.config import ProviderConfig
from mewcode.expert_group import ExpertExecutionPolicy
from mewcode.expert_web import _build_role_agent
from mewcode.model_registry import ModelRef
from mewcode.plugins import ApplicationHost
from mewcode.plugins.runtime import AgentRuntimeRequest
from mewcode.plugins.services import AGENT_FACTORY
from mewcode.tools.load_skill import LoadSkillParams


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["executor", "auditor"])
async def test_visual_skill_is_discoverable_and_loadable_in_chat_and_graph_without_grant(tmp_path, role):
    class Client:
        model = "test"
        base_url = "https://unit.invalid"
        def set_usage_recorder(self, recorder):
            self.recorder = recorder
        def get_usage_recorder(self):
            return getattr(self, "recorder", None)
    config = ProviderConfig(name="test", protocol="openai-compat", model="test", base_url=Client.base_url)
    host = ApplicationHost(str(tmp_path), discover_plugins=False).start()
    root = child = None
    try:
        root = host.context.get(AGENT_FACTORY).create(AgentRuntimeRequest(
            provider=config, client=Client(), auxiliary_client=Client(), work_dir=str(tmp_path), enable_memory=False))
        assert "visual-evidence" in root.agent._skill_catalog
        root_tools = {tool.name for tool in root.agent.registry.list_tools()}
        result = await root.agent.registry.get("LoadSkill").execute(LoadSkillParams(name="visual-evidence"))
        assert not result.is_error and "visual-evidence" in root.agent.active_skills
        assert {tool.name for tool in root.agent.registry.list_tools()} == root_tools
        owner = SimpleNamespace(context=host.context, session=SimpleNamespace(session_id="chat"), agent=root.agent,
            client=root.client, agent_runtime=root, conversation=root.conversation, conversation_runs={},
            scope="project", work_dir=str(tmp_path), slot=None, _effective_effort=lambda: "auto",
            context_tier=64000, multi_agent=False, expert_mode=True, active_goal=lambda: "",
            task_manager=None, trace_manager=None, _agent_tool=None, worktree_manager=None,
            team_manager=None, permission_before_plan="auto", usage_service=None)
        registry = SimpleNamespace(resolve=lambda *a, **k: SimpleNamespace(config=config, client=Client(), context_window=64000))
        policy = ExpertExecutionPolicy("stable", False, 0, node_tools=("ReadFile", "LoadSkill", "FindSkills"))
        agent, _, child = _build_role_agent(owner, registry, role, ModelRef("local", "test"), policy)
        assert "visual-evidence" in agent._skill_catalog
        before = {tool.name for tool in agent.registry.list_tools()}
        mode = agent.permission_checker.mode
        load = agent.registry.get("LoadSkill")
        assert load.instruction_only
        assert not (await load.execute(LoadSkillParams(name="visual-evidence"))).is_error
        assert "visual-evidence" in agent.active_skills
        assert {tool.name for tool in agent.registry.list_tools()} == before
        assert agent.permission_checker.mode == mode
        assert not agent.registry.is_enabled("Bash")
    finally:
        if child:
            await child.aclose()
        if root:
            await root.aclose()
        await host.aclose()
