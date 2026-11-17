"""Lifecycle-owned runtime factories for providers, sessions, and agents."""
from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from mewcode.cordis import Context, PluginSpec
from mewcode.plugins.services import (
    AGENT,
    AGENT_CATALOG,
    CACHE_PROVIDERS,
    CLIENT_FACTORY,
    CLIENT_FACTORY_SLOT,
    CONVERSATION,
    CONVERSATION_FACTORY,
    HOOKS,
    LLM,
    MEMORY,
    PERMISSIONS,
    PROVIDERS,
    SESSIONS,
    SESSION_FACTORY,
    SKILLS,
    TOOLS,
    TOOL_SLOT,
    USAGE,
    WORK_DIR,
)


class RuntimeClientFactory:
    """Resolve protocol-specific clients through the client factory slot."""

    def __init__(self, context: Context) -> None:
        self.context = context

    def create(self, provider: Any) -> Any:
        selected = self.context.slots.resolve(CLIENT_FACTORY_SLOT, owner=provider)
        if selected is not None and selected is not self:
            create = getattr(selected, "create", selected)
            client = create(provider)
        else:
            from mewcode.client import create_client

            client = create_client(provider)
        cache_registry = self.context.get(CACHE_PROVIDERS, None)
        if cache_registry is not None:
            try:
                capabilities = getattr(client, "capabilities", None)
                cache_format = getattr(capabilities, "cache_usage_format", "generic")
                protocol = str(getattr(provider, "protocol", ""))
                client.cache_usage_provider = cache_registry.resolve(cache_format, protocol)
            except (AttributeError, KeyError, TypeError):
                # A third-party client may not expose cache accounting. Its own
                # capability defaults remain valid in that case.
                pass
        return client

    __call__ = create


class ConversationFactory:
    """Create disposable conversation stores through the active plugin graph."""

    def create(self) -> Any:
        from mewcode.conversation import ConversationManager

        return ConversationManager()


class SessionFactory:
    """Create durable session stores without leaking storage policy to callers."""

    def create(self, work_dir: str | Path) -> Any:
        from mewcode.memory.session import SessionManager

        return SessionManager(str(work_dir))


class TaskFactory:
    """Create background task managers inside the active runtime graph."""

    def create(self, state_path: str | Path | None = None) -> Any:
        from mewcode.agents.task_manager import TaskManager

        return TaskManager(state_path)


class TraceFactory:
    """Create trace managers with the same lifecycle boundary as tasks."""

    def create(self) -> Any:
        from mewcode.agents.trace import TraceManager

        return TraceManager()


@dataclass
class AgentRuntimeRequest:
    provider: Any
    work_dir: str = "."
    client: Any = None
    auxiliary_client: Any = None
    config: Any = None
    permission_mode: Any = None
    permission_checker: Any = None
    conversation: Any = None
    memory_manager: Any = None
    enable_memory: bool = True
    session_manager: Any = None
    session: Any = None
    usage_service: Any = None
    hook_engine: Any = None
    file_cache: Any = None
    file_history: Any = None
    instructions_content: str | None = None
    context_window: int | None = None
    max_iterations: int = 0  # 0/negative = unlimited (agent.run main loop)
    create_session: bool = False
    enable_skills: bool = True
    enable_tool_search: bool = True
    include_default_tools: bool = True
    inherit_parent_tools: bool = True
    extra_tools: tuple[Any, ...] = ()
    agent_kwargs: dict[str, Any] = field(default_factory=dict)
    stall_policy: dict[str, Any] | None = field(default_factory=dict)


class AgentRuntime:
    """One disposable Agent graph rooted in a child Cordis Context."""

    def __init__(
        self,
        context: Context,
        plugin_name: str,
        request: AgentRuntimeRequest,
    ) -> None:
        self.context = context
        self.plugin_name = plugin_name
        self.request = request
        self._closed_tool_names: tuple[str, ...] | None = None

    def tool_names_snapshot(self) -> list[str]:
        """Last authorized inventory, not live handles or permission grants.

        Teardown removes slot-owned tools. Persisting that emptied registry as
        execution capability would manufacture a blocker on the next segment.
        """
        if self._closed_tool_names is not None:
            return list(self._closed_tool_names)
        registry = self.agent.registry
        return sorted(t.name for t in registry.list_tools() if registry.is_enabled(t.name))

    @property
    def agent(self) -> Any:
        return self.context.get(AGENT)

    @property
    def client(self) -> Any:
        return self.context.get(LLM)

    @property
    def registry(self) -> Any:
        return self.context.get(TOOLS)

    @property
    def conversation(self) -> Any:
        return self.context.get(CONVERSATION)

    @property
    def memory_manager(self) -> Any:
        return self.context.get(MEMORY)

    @property
    def session_manager(self) -> Any:
        return self.context.get(SESSIONS, None)

    @property
    def session(self) -> Any:
        return self.context.get("session", None)

    def install(self, spec: PluginSpec) -> None:
        self.context.register_plugin(spec, replace=True)
        self.context.load(spec.name)

    async def ainstall(self, spec: PluginSpec) -> None:
        self.context.register_plugin(spec, replace=True)
        await self.context.aload(spec.name)

    def contribute_tools(
        self,
        name: str,
        factory: Callable[[Context], Iterable[Any] | Any],
        *,
        purpose: str = "Runtime tool contribution",
    ) -> None:
        def apply(ctx: Context) -> None:
            produced = factory(ctx)
            tools = produced if isinstance(produced, (list, tuple, set)) else [produced]
            for tool in tools:
                if tool is not None:
                    ctx.slots.register(TOOL_SLOT, tool, item_key=tool.name)

        self.install(PluginSpec(name, apply, purpose=purpose, source="runtime"))

    def configure_model(
        self,
        provider: Any,
        *,
        effort: str = "",
        reason: str = "model_changed",
    ) -> Any:
        from mewcode.auxiliary import create_auxiliary_client

        name = "runtime.model"
        if self.context.plugin_spec(name) is not None:
            state = self.context.plugin_state(name)
            if state.value in {"active", "waiting"}:
                self.context.unload(name)

        def apply(ctx: Context) -> None:
            factory = ctx.get(CLIENT_FACTORY)
            client = factory.create(provider)
            if effort:
                client.set_effort(effort)
            agent = ctx.get(AGENT)
            agent.configure_model(
                client,
                protocol=provider.protocol,
                context_window=provider.get_context_window(),
                reason=reason,
            )
            auxiliary = create_auxiliary_client(
                provider,
                self.request.config,
                client_factory=factory.create,
            ) or client
            agent.auxiliary_client = auxiliary
            from mewcode.permissions.review_client import bind_safety_reviewer
            bind_safety_reviewer(agent, ctx.get(PROVIDERS, None))
            usage = ctx.get(USAGE, None)
            if usage is not None:
                usage.bind_client(client)
                usage.bind_client(auxiliary)
            ctx.provide(LLM, client, replace=True)

        self.install(PluginSpec(
            name,
            apply,
            purpose="Hot-swappable provider client",
            source="runtime",
        ))
        return self.client

    def close(self) -> None:
        if self._closed_tool_names is None:
            self._closed_tool_names = tuple(self.tool_names_snapshot())
        self.context.close()

    async def aclose(self) -> None:
        if self._closed_tool_names is None:
            self._closed_tool_names = tuple(self.tool_names_snapshot())
        await self.context.aclose()


class AgentRuntimeFactory:
    """Create agents entirely inside disposable child plugin graphs."""

    def __init__(self, context: Context) -> None:
        self.context = context

    def create(self, request: AgentRuntimeRequest) -> AgentRuntime:
        child = self.context.scope(f"agent-{uuid.uuid4().hex[:12]}")
        plugin_name = "runtime.agent"
        child.register_plugin(
            PluginSpec(
                plugin_name,
                lambda ctx: self._apply(ctx, request),
                purpose="Session-scoped Agent and dependency graph",
                source="runtime",
            )
        )
        try:
            child.load(plugin_name)
        except BaseException:
            child.close()
            raise
        return AgentRuntime(child, plugin_name, request)

    async def acreate(self, request: AgentRuntimeRequest) -> AgentRuntime:
        child = self.context.scope(f"agent-{uuid.uuid4().hex[:12]}")
        plugin_name = "runtime.agent"
        child.register_plugin(
            PluginSpec(
                plugin_name,
                lambda ctx: self._apply(ctx, request),
                purpose="Session-scoped Agent and dependency graph",
                source="runtime",
            )
        )
        try:
            await child.aload(plugin_name)
        except BaseException:
            await child.aclose()
            raise
        return AgentRuntime(child, plugin_name, request)

    def _apply(self, ctx: Context, request: AgentRuntimeRequest) -> None:
        from mewcode.agent import Agent
        from mewcode.auxiliary import create_auxiliary_client
        from mewcode.conversation import ConversationManager
        from mewcode.memory.auto_memory import MemoryManager
        from mewcode.memory.instructions import load_instructions
        from mewcode.memory.session import SessionManager
        from mewcode.permissions import (
            DangerousCommandDetector,
            PathSandbox,
            PermissionChecker,
            PermissionMode,
            RuleEngine,
        )
        from mewcode.skills.runtime import format_skill_catalog
        from mewcode.tools import ToolRegistry, create_default_registry
        from mewcode.tools.load_skill import LoadSkill
        from mewcode.tools.impl.tool_search import ToolSearchTool
        from mewcode.tools.impl.invoke_deferred import InvokeDeferredTool
        from mewcode.usage.service import UsageService

        work_dir = str(Path(request.work_dir).resolve())
        ctx.provide(WORK_DIR, work_dir, replace=True)
        ctx.slots.declare(TOOL_SLOT, kind="keyed", owner="runtime.agent")

        registry: ToolRegistry = (
            create_default_registry(
                file_cache=request.file_cache,
                file_history=request.file_history,
            )
            if request.include_default_tools
            else ToolRegistry()
        )
        ctx.provide(TOOLS, registry, replace=True)
        if request.include_default_tools:
            terminal = registry.get("TerminalStart")
            if terminal is not None:
                ctx.effect(terminal.sessions.aclose)
        _bind_tool_slot(ctx, registry)
        if request.inherit_parent_tools:
            _mirror_parent_tools(ctx, registry)

        client_factory = ctx.get(CLIENT_FACTORY)
        client = request.client or client_factory.create(request.provider)
        auxiliary_client = request.auxiliary_client
        if auxiliary_client is None:
            auxiliary_client = create_auxiliary_client(
                request.provider,
                request.config,
                client_factory=client_factory.create,
            )
        ctx.provide(LLM, client, replace=True)

        usage_service = request.usage_service or ctx.get(USAGE, None) or UsageService(work_dir)
        if callable(getattr(client, "set_usage_recorder", None)):
            usage_service.bind_client(client)
        if callable(getattr(auxiliary_client, "set_usage_recorder", None)):
            usage_service.bind_client(auxiliary_client)
        ctx.provide(USAGE, usage_service, replace=True)

        conversation_factory = ctx.get(CONVERSATION_FACTORY, None)
        conversation = request.conversation or (
            conversation_factory.create() if conversation_factory is not None
            else ConversationManager()
        )
        memory = (
            request.memory_manager
            if request.memory_manager is not None
            else (MemoryManager(work_dir) if request.enable_memory else None)
        )
        checker = request.permission_checker or _create_permission_checker(
            work_dir,
            request.permission_mode or PermissionMode.DEFAULT,
            DangerousCommandDetector,
            PathSandbox,
            PermissionChecker,
            RuleEngine,
        )
        ctx.provide(CONVERSATION, conversation, replace=True)
        ctx.provide(MEMORY, memory, replace=True)
        ctx.provide(PERMISSIONS, checker, replace=True)

        session_manager = request.session_manager
        session = request.session
        if request.create_session and session_manager is None:
            session_factory = ctx.get(SESSION_FACTORY, None)
            session_manager = (
                session_factory.create(work_dir)
                if session_factory is not None
                else SessionManager(work_dir)
            )
            session_manager.cleanup()
        if request.create_session and session is None and session_manager is not None:
            session = session_manager.create()
        if session_manager is not None:
            ctx.provide(SESSIONS, session_manager, replace=True)
        if session is not None:
            ctx.provide("session", session, replace=True)
            if request.session is None:
                ctx.effect(session.close)

        instructions = (
            request.instructions_content
            if request.instructions_content is not None
            else load_instructions(work_dir)
        )
        agent = Agent(
            client=client,
            registry=registry,
            protocol=request.provider.protocol,
            work_dir=work_dir,
            max_iterations=request.max_iterations,
            permission_checker=checker,
            context_window=(
                request.context_window
                if request.context_window is not None
                else request.provider.get_context_window()
            ),
            instructions_content=instructions,
            memory_manager=memory,
            hook_engine=request.hook_engine or ctx.get(HOOKS, None),
            auxiliary_client=auxiliary_client,
            runtime_context=ctx,
            **{"stall_policy": request.stall_policy, **request.agent_kwargs},
        )
        agent.file_history = request.file_history
        if session is not None:
            agent.session_id = session.session_id
        from mewcode.permissions.audit import AuditLogger
        from mewcode.permissions.review_client import bind_safety_reviewer
        if agent.audit_logger is None:
            agent.audit_logger = AuditLogger(work_dir, session_id=agent.session_id)
        bind_safety_reviewer(agent, ctx.get(PROVIDERS, None))
        ctx.provide(AGENT, agent, replace=True)
        from mewcode.tools.agent_status import AgentStatus
        from mewcode.tools.capabilities import ProbeCapabilities
        from mewcode.tools.evaluate_evidence import EvaluateEvidence
        from mewcode.decisions.runtime import DecisionRuntime
        from mewcode.decisions.spec import DECISION_GUIDANCE
        from mewcode.plugins.services import DECISIONS, JUDGMENTS, TOOL_HISTORY_SHADOW
        from mewcode.context.tool_history_shadow import ToolHistoryShadow
        from mewcode.tools.inspect_tool_history import InspectToolHistory
        from mewcode.tools.inspect_stage_graph import InspectStageGraph
        from mewcode.tools.decide_next_step import DecideNextStep
        from mewcode.decisions.browser import FastBrowse
        decision_runtime = DecisionRuntime(agent, ctx.get(PROVIDERS, None), context=ctx)
        agent.decision_runtime = decision_runtime
        agent.judgment_runtime = decision_runtime
        ctx.provide(DECISIONS, decision_runtime, replace=True)
        ctx.provide(JUDGMENTS, decision_runtime, replace=True)
        ctx.effect(decision_runtime.aclose)
        shadow = ToolHistoryShadow(agent, decision_runtime)
        agent.tool_history_shadow = shadow
        ctx.provide(TOOL_HISTORY_SHADOW, shadow, replace=True)
        ctx.effect(shadow.aclose)
        # Observe the budgeted provider view after other before-model adapters.
        ctx.on("agent/before_model", shadow.before_model, priority=-1000)
        if request.include_default_tools:
            registry.register(AgentStatus(agent))
            registry.register(ProbeCapabilities(agent, ctx.get(PROVIDERS, None)))
            registry.register(EvaluateEvidence(agent, ctx.get(PROVIDERS, None)))
            registry.register(DecideNextStep(decision_runtime))
            registry.register(FastBrowse(agent, decision_runtime))
            registry.register(InspectToolHistory(shadow))
            registry.register(InspectStageGraph(agent))

        extra_names = {tool.name for tool in request.extra_tools}
        if request.enable_tool_search and "ToolSearch" not in extra_names:
            existing_tool_search = registry.get("ToolSearch")
            if existing_tool_search is None:
                ctx.slots.register(
                    TOOL_SLOT,
                    ToolSearchTool(registry, protocol=request.provider.protocol),
                    item_key="ToolSearch",
                )
            elif hasattr(existing_tool_search, "_protocol"):
                existing_tool_search._protocol = request.provider.protocol
        if request.enable_skills:
            loader = ctx.get(SKILLS, None)
            if loader is not None:
                if "LoadSkill" not in extra_names:
                    tool = LoadSkill()
                    tool.set_loader(loader)
                    tool.set_agent(agent)
                    ctx.slots.register(TOOL_SLOT, tool, item_key=tool.name)
                if "FindSkills" not in extra_names:
                    from mewcode.tools.find_skills import FindSkills
                    ctx.slots.register(TOOL_SLOT, FindSkills(loader), item_key="FindSkills")
                catalog = format_skill_catalog(loader)
                if catalog:
                    agent.set_skill_catalog(catalog)
        local_worktrees = None
        for tool in request.extra_tools:
            # Runtime-bound tools must never operate on the parent's registry
            # or dispatch authority after filtering a child/role scope.
            from mewcode.tools.agent_status import AgentStatus, BackgroundTasks, TaskRetry
            from mewcode.tools.enter_worktree import EnterWorktreeTool
            from mewcode.tools.exit_worktree import ExitWorktreeTool
            if isinstance(tool, (EnterWorktreeTool, ExitWorktreeTool)):
                if local_worktrees is None:
                    from mewcode.worktree.manager import WorktreeManager
                    from mewcode.worktree.binding import WorktreeSessionBinding
                    from mewcode.plugins.services import WORKTREES
                    local_worktrees = WorktreeManager(work_dir,
                        symlink_directories=tool._manager.symlink_directories)
                    local_worktrees.session_binding = WorktreeSessionBinding(agent)
                    ctx.provide(WORKTREES, local_worktrees, replace=True)
                tool = type(tool)(local_worktrees)
            if isinstance(tool, TaskRetry):
                continue  # Install only with a locally-owned Agent dispatcher.
            if isinstance(tool, (AgentStatus, BackgroundTasks)):
                tool = type(tool)(agent, tool.manager)
            if isinstance(tool, ToolSearchTool):
                tool = ToolSearchTool(registry, protocol=request.provider.protocol)
            if isinstance(tool, InvokeDeferredTool):
                tool = InvokeDeferredTool(registry)
            if isinstance(tool, ProbeCapabilities):
                tool = ProbeCapabilities(agent, ctx.get(PROVIDERS, None))
            if isinstance(tool, EvaluateEvidence):
                tool = EvaluateEvidence(agent, ctx.get(PROVIDERS, None))
            if isinstance(tool, DecideNextStep):
                tool = DecideNextStep(decision_runtime)
            if isinstance(tool, FastBrowse):
                tool = FastBrowse(agent, decision_runtime)
            if isinstance(tool, InspectToolHistory):
                tool = InspectToolHistory(shadow)
            if isinstance(tool, InspectStageGraph):
                tool = InspectStageGraph(agent)
            # Agent construction may install model-specific runtime tools (for
            # example DeepSeek's ToolSearch and InvokeDeferredTool gateways)
            # into this same keyed slot.  Pipeline runtimes copy selected tools
            # from their parent session, so registering the copied gateway again
            # both collides with the slot and would bind it to the wrong registry.
            # Keep the locally-created instance, which owns this runtime's tools.
            if tool.name in agent._runtime_tool_disposers:
                continue
            if tool.name == "LoadSkill" and request.enable_skills:
                loader = ctx.get(SKILLS, None)
                if loader is not None:
                    tool.set_loader(loader)
                    tool.set_agent(agent)
            ctx.slots.register(TOOL_SLOT, tool, item_key=tool.name)

        if registry.is_enabled("DecideNextStep"):
            agent.instructions_content += "\n" + DECISION_GUIDANCE

        # Web search must see the same live provider registry and browser scope
        # as this agent, including desktop credential rotations/removals.
        from mewcode.tools.web_search import WebSearch
        if not request.include_default_tools and not request.enable_tool_search:
            for name in ("ToolSearch", "InvokeDeferredTool"):
                if name not in extra_names:
                    registry.unregister(name)
        if not registry.is_enabled("ToolSearch"):
            # Explicit MCP-only scopes must not strand permitted tools behind
            # a discovery gateway that this scope intentionally denied.
            for tool in registry.list_tools():
                if registry.is_enabled(tool.name) and getattr(tool, "should_defer", False):
                    registry.mark_discovered(tool.name)
        from mewcode.tools.capabilities import inventory
        search = registry.get("WebSearch")
        if isinstance(search, WebSearch):
            local_search = search.bound_copy(ctx.get(PROVIDERS, None), lambda: inventory(agent)["browser_connected"])
            registry.register(local_search)
            alias = registry.get("web_search")
            if isinstance(alias, WebSearch):
                from mewcode.tools.web_search import WebSearchAlias
                registry.register(WebSearchAlias(delegate=local_search))


def _bind_tool_slot(ctx: Context, registry: Any) -> None:
    installed: dict[str, Any] = {}
    previous: dict[str, Any] = {}

    def sync(values: dict[str, Any] | None) -> None:
        values = values or {}
        for name in list(installed):
            if values.get(name) is installed[name]:
                continue
            registry.unregister(name)
            old = previous.pop(name, None)
            if old is not None:
                registry.register(old)
            installed.pop(name, None)
        for name, tool in values.items():
            if installed.get(name) is tool:
                continue
            previous[name] = registry.get(name)
            registry.register(tool)
            installed[name] = tool

    ctx.slots.inject(TOOL_SLOT, sync)


def _mirror_parent_tools(ctx: Context, registry: Any) -> None:
    parent = ctx.parent
    if parent is None:
        return
    mirrored: dict[str, Any] = {}

    def sync(values: dict[str, Any] | None) -> None:
        values = values or {}
        for name, tool in list(mirrored.items()):
            if values.get(name) is tool:
                continue
            if registry.get(name) is tool:
                registry.unregister(name)
            mirrored.pop(name, None)
        for name, tool in values.items():
            if registry.get(name) is not None:
                continue
            registry.register(tool)
            mirrored[name] = tool

    disposer = parent.slots.inject(TOOL_SLOT, sync)
    ctx.effect(disposer)


def _create_permission_checker(
    work_dir: str,
    mode: Any,
    detector_type: Any,
    sandbox_type: Any,
    checker_type: Any,
    rule_engine_type: Any,
) -> Any:
    home = Path.home()
    return checker_type(
        detector=detector_type(),
        sandbox=sandbox_type(work_dir),
        rule_engine=rule_engine_type(
            user_rules_path=home / ".mewcode" / "permissions.yaml",
            project_rules_path=Path(work_dir) / ".mewcode" / "permissions.yaml",
            local_rules_path=Path(work_dir) / ".mewcode" / "permissions.local.yaml",
        ),
        mode=mode,
    )


def dispose_runtime_value(value: Any) -> Callable[[], Any] | None:
    """Return the strongest cleanup method exposed by a plugin-owned value."""

    for name in ("aclose", "shutdown", "close", "dispose"):
        cleanup = getattr(value, name, None)
        if callable(cleanup):
            return cleanup
    if inspect.isawaitable(value):
        return getattr(value, "cancel", None)
    return None


__all__ = [
    "AgentRuntime",
    "AgentRuntimeFactory",
    "AgentRuntimeRequest",
    "ConversationFactory",
    "RuntimeClientFactory",
    "SessionFactory",
    "TaskFactory",
    "TraceFactory",
    "dispose_runtime_value",
]
