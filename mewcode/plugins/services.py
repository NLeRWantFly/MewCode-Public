"""Stable service and slot names shared by MewCode plugins."""
from __future__ import annotations

from typing import Any

from mewcode.cordis import ServiceDefinition

HOST = ServiceDefinition[Any]("plugin_host")
WORK_DIR = ServiceDefinition[str]("work_dir")
TOOLS = ServiceDefinition[Any]("tools")
COMMANDS = ServiceDefinition[Any]("commands")
SKILLS = ServiceDefinition[Any]("skills")
AGENT_CATALOG = ServiceDefinition[Any]("agent_catalog")
HOOKS = ServiceDefinition[Any]("hooks")
SESSIONS = ServiceDefinition[Any]("sessions")
LLM = ServiceDefinition[Any]("llm")
AGENT = ServiceDefinition[Any]("agents")
PROVIDERS = ServiceDefinition[Any]("providers")
MODEL_CATALOG = ServiceDefinition[Any]("model_catalog")
CONTEXT_ROUTER = ServiceDefinition[Any]("context_router")
CLIENT_FACTORY = ServiceDefinition[Any]("client_factory")
AGENT_FACTORY = ServiceDefinition[Any]("agent_factory")
CONVERSATION = ServiceDefinition[Any]("conversation")
CONVERSATION_FACTORY = ServiceDefinition[Any]("conversation_factory")
MEMORY = ServiceDefinition[Any]("memory")
PERMISSIONS = ServiceDefinition[Any]("permissions")
SESSION_FACTORY = ServiceDefinition[Any]("session_factory")
TASK_FACTORY = ServiceDefinition[Any]("task_factory")
TRACE_FACTORY = ServiceDefinition[Any]("trace_factory")
TASKS = ServiceDefinition[Any]("tasks")
TRACES = ServiceDefinition[Any]("traces")
WORKTREES = ServiceDefinition[Any]("worktrees")
TEAMS = ServiceDefinition[Any]("teams")
USAGE = ServiceDefinition[Any]("usage")
RUNTIME_SCHEDULER = ServiceDefinition[Any]("runtime_scheduler")
CACHE_PROVIDERS = ServiceDefinition[Any]("cache_providers")
EXPERTS = ServiceDefinition[Any]("experts")
EXPERT_FLOWS = ServiceDefinition[Any]("expert_flows")
WEB_APP = ServiceDefinition[Any]("web.app")
PIPELINE = ServiceDefinition[Any]("pipeline")
COMPLETION = ServiceDefinition[Any]("completion")

TOOL_SLOT = "core.tools"
DECISIONS = ServiceDefinition[Any]("decisions")
DECISION_SPEC_SLOT = "core.decisions.specs"
JUDGMENTS = ServiceDefinition[Any]("judgments")
JUDGMENT_SPEC_SLOT = "core.judgments.specs"
TOOL_HISTORY_SHADOW = ServiceDefinition[Any]("tool_history_shadow")
COMMAND_SLOT = "core.commands"
SKILL_SOURCE_SLOT = "core.skills.sources"
AGENT_SOURCE_SLOT = "core.agents.sources"
PROVIDER_SLOT = "core.providers"
CLIENT_FACTORY_SLOT = "core.client.factories"
AGENT_FACTORY_SLOT = "core.agent.factories"
CACHE_PROVIDER_SLOT = "core.cache.providers"
EXPERT_SLOT = "core.experts"
EXPERT_ROLE_SLOT = EXPERT_SLOT
EXPERT_FLOW_SLOT = "core.expert.flows"
EXPERT_ROUTER_SLOT = "core.expert.routers"
EXPERT_REDUCER_SLOT = "core.expert.reducers"
EXPERT_FINALIZER_SLOT = "core.expert.finalizers"
ORCHESTRATION_TEMPLATE_SLOT = "core.orchestration.templates"
ORCHESTRATION_NODE_SLOT = "core.orchestration.nodes"
ORCHESTRATION_GATE_SLOT = "core.orchestration.gates"
PIPELINE_ACTION_SLOT = "core.pipeline.actions"
RUNTIME_FEATURE_SLOT = "runtime.features"
WEB_API_SLOT = "web.apis"
WEB_ASSET_SLOT = "web.assets"
WEB_MENU_SLOT = "web.menus"
WEB_SETTINGS_SLOT = "web.settings"
UI_ROOT_SLOT = "ui.root"
UI_CONVERSATION_SLOT = "ui.conversation"
UI_TRAJECTORY_SLOT = "ui.trajectory"

CORE_SLOTS: tuple[tuple[str, str], ...] = (
    (DECISION_SPEC_SLOT, "keyed"),
    (JUDGMENT_SPEC_SLOT, "keyed"),
    (TOOL_SLOT, "keyed"),
    (COMMAND_SLOT, "keyed"),
    (SKILL_SOURCE_SLOT, "list"),
    (AGENT_SOURCE_SLOT, "list"),
    (PROVIDER_SLOT, "keyed"),
    (CLIENT_FACTORY_SLOT, "chain"),
    (AGENT_FACTORY_SLOT, "chain"),
    (CACHE_PROVIDER_SLOT, "keyed"),
    (EXPERT_SLOT, "keyed"),
    (EXPERT_FLOW_SLOT, "keyed"),
    (EXPERT_ROUTER_SLOT, "keyed"),
    (EXPERT_REDUCER_SLOT, "keyed"),
    (EXPERT_FINALIZER_SLOT, "keyed"),
    (ORCHESTRATION_TEMPLATE_SLOT, "keyed"),
    (ORCHESTRATION_NODE_SLOT, "keyed"),
    (ORCHESTRATION_GATE_SLOT, "keyed"),
    (PIPELINE_ACTION_SLOT, "keyed"),
    (RUNTIME_FEATURE_SLOT, "list"),
    (WEB_API_SLOT, "list"),
    (WEB_ASSET_SLOT, "list"),
    (WEB_MENU_SLOT, "list"),
    (WEB_SETTINGS_SLOT, "list"),
    (UI_ROOT_SLOT, "single"),
    (UI_CONVERSATION_SLOT, "chain"),
    (UI_TRAJECTORY_SLOT, "chain"),
)


__all__ = [
    "DECISIONS",
    "DECISION_SPEC_SLOT",
    "JUDGMENTS",
    "JUDGMENT_SPEC_SLOT",
    "TOOL_HISTORY_SHADOW",
    "AGENT",
    "AGENT_CATALOG",
    "AGENT_FACTORY",
    "AGENT_FACTORY_SLOT",
    "AGENT_SOURCE_SLOT",
    "CACHE_PROVIDERS",
    "CACHE_PROVIDER_SLOT",
    "CLIENT_FACTORY",
    "CLIENT_FACTORY_SLOT",
    "COMMANDS",
    "COMMAND_SLOT",
    "COMPLETION",
    "CONTEXT_ROUTER",
    "CONVERSATION",
    "CONVERSATION_FACTORY",
    "CORE_SLOTS",
    "EXPERTS",
    "EXPERT_FLOWS",
    "EXPERT_FINALIZER_SLOT",
    "EXPERT_FLOW_SLOT",
    "EXPERT_REDUCER_SLOT",
    "EXPERT_ROLE_SLOT",
    "EXPERT_ROUTER_SLOT",
    "EXPERT_SLOT",
    "ORCHESTRATION_GATE_SLOT",
    "ORCHESTRATION_NODE_SLOT",
    "ORCHESTRATION_TEMPLATE_SLOT",
    "HOOKS",
    "HOST",
    "LLM",
    "MEMORY",
    "MODEL_CATALOG",
    "PERMISSIONS",
    "PIPELINE",
    "PIPELINE_ACTION_SLOT",
    "PROVIDERS",
    "PROVIDER_SLOT",
    "RUNTIME_FEATURE_SLOT",
    "RUNTIME_SCHEDULER",
    "SESSIONS",
    "SESSION_FACTORY",
    "TASK_FACTORY",
    "SKILLS",
    "SKILL_SOURCE_SLOT",
    "TASKS",
    "TEAMS",
    "TOOLS",
    "TOOL_SLOT",
    "TRACES",
    "TRACE_FACTORY",
    "UI_CONVERSATION_SLOT",
    "UI_ROOT_SLOT",
    "UI_TRAJECTORY_SLOT",
    "USAGE",
    "WEB_API_SLOT",
    "WEB_ASSET_SLOT",
    "WEB_APP",
    "WEB_MENU_SLOT",
    "WEB_SETTINGS_SLOT",
    "WORK_DIR",
    "WORKTREES",
]
