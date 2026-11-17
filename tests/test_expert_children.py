from __future__ import annotations

from dataclasses import replace

import pytest

from mewcode.agents.parser import AgentDef
from mewcode.agents.tool_filter import resolve_agent_tools
from mewcode.expert_group import ExpertExecutionPolicy
from mewcode.expert_web import _role_registry
from mewcode.experts.node_expansion import (
    NodeExpansionContext,
    NodeExpansionHolder,
)
from mewcode.permissions.sandbox import PathSandbox
from mewcode.tools import ToolRegistry
from mewcode.tools.agent_tool import AgentTool


class FakeTool:
    """Minimal tool stand-in with just the attributes the filters touch."""

    def __init__(self, name: str, interaction_kind: str = "", category: str = "read") -> None:
        self.name = name
        self.interaction_kind = interaction_kind
        self.category = category


def make_registry(*names: str) -> ToolRegistry:
    registry = ToolRegistry()
    for name in names:
        registry.register(FakeTool(name))
    return registry


class _FakeAgentHost:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry


class FakeSession:
    """The slice of the web session that _role_registry reads."""

    def __init__(self, registry: ToolRegistry) -> None:
        self.agent = _FakeAgentHost(registry)


def executor_policy() -> ExpertExecutionPolicy:
    return ExpertExecutionPolicy(
        strategy="fast", allow_delegation=True, max_direct_agents=8
    )


def expansion_holder(work_dir, *, parent_node_id="impl-1") -> NodeExpansionHolder:
    return NodeExpansionHolder(context=NodeExpansionContext(
        parent_node_id=parent_node_id,
        flow_version=1,
        input_revision="rev-1",
        node_attempt=1,
        work_dir=work_dir,
        allowed_tools=frozenset(),
        available_input_refs=frozenset({"parent"}),
        sandbox=PathSandbox(str(work_dir)),
    ))


def test_only_plain_executor_receives_node_expansion_tool(tmp_path):
    parent = make_registry("ReadFile", "EditFile", "Agent", "TeamCreate")

    holder = expansion_holder(tmp_path)
    registry = _role_registry(FakeSession(parent), "executor",
        replace(executor_policy(), expansion_holder=holder))
    assert registry.get("ProposeNodeExpansion") is not None
    assert not registry.is_discovered("ProposeNodeExpansion")
    allowed = holder.context.allowed_tools
    assert "ReadFile" in allowed and "EditFile" in allowed
    assert "Agent" not in allowed and "TeamCreate" not in allowed
    assert "ProposeNodeExpansion" not in allowed

    for role in ("manager", "planner", "auditor"):
        registry = _role_registry(FakeSession(parent), role,
            replace(executor_policy(), expansion_holder=expansion_holder(tmp_path)))
        assert registry.get("ProposeNodeExpansion") is None

    blocked = [
        dict(loop_context={"contract_hash": "x"}),
        dict(audit_contract={"artifact_dir": str(tmp_path)}),
        dict(work_dir=str(tmp_path)),
        dict(parallel_readonly=True),
        dict(deny_tools=("ProposeNodeExpansion",)),
        dict(deny_tools=("*",)),
        dict(node_tools=()),
    ]
    for override in blocked:
        registry = _role_registry(FakeSession(parent), "executor",
            replace(executor_policy(), expansion_holder=expansion_holder(tmp_path),
                    **override))
        assert registry.get("ProposeNodeExpansion") is None, override

    registry = _role_registry(FakeSession(parent), "executor",
        replace(executor_policy(), expansion_holder=expansion_holder(tmp_path),
                node_tools=("ProposeNodeExpansion",)))
    assert registry.get("ProposeNodeExpansion") is not None


def test_executor_role_registry_excludes_delegation_tools() -> None:
    """The executor's base toolset must not leak delegation tools.

    Delegation is opt-in: _build_role_agent injects the Agent tool (and team
    tools) through a dedicated path, so the generic registry must never carry
    them.
    """
    parent = make_registry(
        "ReadFile", "EditFile", "Agent", "TeamCreate", "TeamDelete"
    )
    registry = _role_registry(FakeSession(parent), "executor", executor_policy())
    names = {tool.name for tool in registry.list_tools()}
    assert "ReadFile" in names
    assert "EditFile" in names  # executor keeps the full write toolset
    assert "Agent" not in names
    assert "TeamCreate" not in names
    assert "TeamDelete" not in names


def test_role_registry_registers_ask_user_for_experts() -> None:
    parent = make_registry("ReadFile", "Glob", "Grep")
    for role in ("manager", "planner", "executor", "auditor"):
        registry = _role_registry(FakeSession(parent), role, executor_policy())
        assert registry.get("AskUserQuestion") is not None
        assert registry.is_discovered("AskUserQuestion")
        assert registry.get("human_oncall") is not None
        assert registry.is_discovered("human_oncall")
        assert registry.get("report_step") is not None
        assert registry.is_discovered("report_step")


def test_role_registry_does_not_reuse_parent_hitl_instances() -> None:
    parent = make_registry("ReadFile")
    parent.register(FakeTool("AskUserQuestion"))
    parent.register(FakeTool("human_oncall", interaction_kind="human_oncall"))
    registry = _role_registry(FakeSession(parent), "executor", executor_policy())
    assert type(registry.get("AskUserQuestion")).__name__ != "FakeTool"
    assert type(registry.get("human_oncall")).__name__ != "FakeTool"


def test_resolve_agent_tools_strips_agent_for_children() -> None:
    """Layer 1 of the tool filter removes Agent from every child agent."""
    parent = make_registry("ReadFile", "Agent", "TeamCreate", "TeamDelete")
    definition = AgentDef(agent_type="Explore", when_to_use="test")
    child = resolve_agent_tools(parent, definition)
    names = {tool.name for tool in child.list_tools()}
    assert "Agent" not in names
    assert "ReadFile" in names


def test_full_child_boundary_strips_all_delegators() -> None:
    """The v3 invariant end-to-end: executor children (allow_child_delegation
    disabled) end up with no way to spawn further agents or teams.
    """
    parent = make_registry(
        "ReadFile", "EditFile", "Agent", "TeamCreate", "TeamDelete"
    )
    definition = AgentDef(agent_type="Explore", when_to_use="test")
    child = resolve_agent_tools(parent, definition)
    AgentTool._enforce_direct_child_boundary(child)
    names = {tool.name for tool in child.list_tools()}
    assert "Agent" not in names
    assert "TeamCreate" not in names
    assert "TeamDelete" not in names
    assert "ReadFile" in names
    assert "EditFile" in names


def test_boundary_raises_when_a_delegator_leaks() -> None:
    """The guard must escalate instead of letting a silent leak pass."""

    class StubbornRegistry:
        def __init__(self) -> None:
            self._tools = {"Agent": FakeTool("Agent")}

        def unregister(self, name: str) -> None:
            pass  # broken: nothing is removed

        def list_tools(self) -> list[FakeTool]:
            return list(self._tools.values())

    with pytest.raises(
        RuntimeError, match="recursive delegation boundary violation"
    ):
        AgentTool._enforce_direct_child_boundary(StubbornRegistry())
