from __future__ import annotations

import logging
import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from mewcode.agent import Agent
    from mewcode.agents.loader import AgentLoader
    from mewcode.agents.parser import AgentDef
    from mewcode.agents.task_manager import TaskManager
    from mewcode.agents.trace import TraceManager
    from mewcode.client import LLMClient

log = logging.getLogger(__name__)


def provider_route_fingerprint(config: Any) -> str:
    if config is None:
        return ""
    values = [getattr(config, key, "") for key in ("name", "protocol", "base_url")]
    if not all(isinstance(value, str) for value in values):
        return ""
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


class AgentToolParams(BaseModel):
    prompt: str
    description: str
    subagent_type: str | None = None
    provider_key: str | None = None
    model: str | None = None
    effort: str | None = None
    run_in_background: bool = False
    name: str | None = None
    isolation: str | None = None
    team_name: str | None = Field(
        default=None,
        description=(
            "REQUIRED when creating team members. Spawns the agent as a long-running "
            "teammate under this team (created via TeamCreate). Unlike regular sub-agents, "
            "team members run in their own terminal, persist after the lead returns, and "
            "communicate with each other via SendMessage. Without team_name the agent "
            "runs as a one-shot sub-agent that blocks and returns inline."
        ),
    )


PERMISSION_MODE_MAP = {
    "default": "DEFAULT",
    "acceptEdits": "ACCEPT_EDITS",
    "dontAsk": "DONT_ASK",
}


@dataclass(frozen=True)
class _AgentRuntime:
    """Provider-specific runtime inherited by a newly-created child agent."""

    client: Any
    protocol: str
    context_window: int
    auxiliary_client: Any
    config: Any = None
    provider_key: str = ""
    model: str = ""
    effort: str = ""


def _sanitize_agent_name(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]", "-", name.strip().lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "worker"


# 单团队成员上限：每个 teammate 都是一个独立 worktree + 一条 LLM 消耗流，
# 无上限的 spawn 等于放任 token/磁盘被烧穿
MAX_TEAMMATES_PER_TEAM = 8


def _prune_stale_member(team: Any, name: str) -> bool:
    """移除同名且已退出的成员记录，腾出名字与名额。返回是否清理了。"""
    from mewcode.teams.registry import AgentNameRegistry

    stale = team.get_member(name)
    if stale is None or stale.is_active is not False:
        return False
    team.remove_member(stale.name)
    team.save()
    AgentNameRegistry.instance().unregister(stale.name)
    return True


# teammate 必须知道自己是谁、队里有谁——否则同伴发现全靠 lead 手写 prompt
TEAMMATE_ADDENDUM_TEMPLATE = (
    "\n\nIMPORTANT: You are running as agent '{name}' in team '{team}'.\n"
    "Current team roster:\n{roster}\n"
    "Just writing a response in text is not visible to others on your team — "
    "you MUST use the SendMessage tool. Address the team lead as 'lead'. "
    "Use TeamRoster to re-check who is on the team.\n"
    "The user interacts primarily with the team lead. Your work is "
    "coordinated through the shared task board (TaskList/TaskUpdate) "
    "and teammate messaging.\n\n"
    "You are working in an isolated Git worktree. "
    "All file paths you use MUST be relative to your current working directory. "
    "Do NOT use absolute paths from the original project — they are outside your sandbox and will be rejected."
)


class AgentTool(Tool):
    name = "Agent"
    description = (
        "Launch a sub-agent to handle a task in an isolated context. "
        "Use subagent_type to select a predefined agent type (e.g. Explore, Plan, general-purpose), "
        "or leave it empty to fork the current conversation. "
        "Use team_name to spawn a teammate in an existing team."
    )
    params_model = AgentToolParams
    category = "command"
    # The agent harness waits for child model/tool leaves, which acquire their
    # own permits. Holding one here would deadlock nested orchestration.
    consumes_runtime_slot = False
    is_concurrency_safe = False


    def __init__(
        self,
        agent_loader: AgentLoader,
        task_manager: TaskManager,
        trace_manager: TraceManager,
        parent_agent: Agent,
        enable_fork: bool = False,
        provider_config: Any = None,
        worktree_manager: Any = None,
        team_manager: Any = None,
        teammate_permission_mode: str = "",
        provider_registry: Any = None,
        max_direct_spawns: int | None = None,
        allow_child_delegation: bool = True,
        child_event_callback: Any = None,
        task_origin: str = "chat",
    ) -> None:
        self._agent_loader = agent_loader
        self._task_manager = task_manager
        self._trace_manager = trace_manager
        self._parent_agent = parent_agent
        self._enable_fork = enable_fork
        self._provider_config = provider_config
        self._worktree_manager = worktree_manager
        self._team_manager = team_manager
        self._teammate_permission_mode = teammate_permission_mode
        self._provider_registry = provider_registry
        self._max_direct_spawns = max_direct_spawns
        self._allow_child_delegation = allow_child_delegation
        self._child_event_callback = child_event_callback
        self._task_origin = task_origin or "chat"
        self._direct_spawn_count = 0
        # 运行注册表：后台任务面板的遥测数据源。key = 子 agent 的 agent_id，
        # value 持有活体 Agent 引用，消费方轮询时直接读它的实时计数器
        #（total_input_tokens / total_output_tokens / total_tool_calls）
        self.runs: dict[str, dict[str, Any]] = {}

    # ---------- 运行注册表（遥测） ----------

    _MAX_RUNS = 60

    def _register_run(
        self, agent: Agent, name: str, agent_type: str, task: str,
        mode: str, team: str = "", task_id: str = "",
    ) -> str:
        """登记一次子 agent 运行。mode: fg（前台阻塞）/ bg（后台）/ teammate。

        后台/队友运行的权威状态在 TaskManager（用 task_id 关联），
        前台运行由 _finish_run 收尾。超出上限时从最旧的已结束记录淘汰。"""
        if len(self.runs) >= self._MAX_RUNS:
            for rid, r in list(self.runs.items()):
                if r["status"] != "running":
                    del self.runs[rid]
                    if len(self.runs) < self._MAX_RUNS:
                        break
        self.runs[agent.agent_id] = {
            "agent": agent,
            "name": name,
            "agent_type": agent_type,
            "task": (task or "")[:200],
            "mode": mode,
            "team": team,
            "task_id": task_id,
            "start": time.monotonic(),
            "end": None,
            "started_at": time.time(),
            "completed_at": None,
            "status": "running",
            "owner_session_id": (
                getattr(self._parent_agent, "session_id", "") or ""
            ),
            "owner_store_dir": (
                getattr(self._parent_agent, "_conversation_store_dir", "")
                or getattr(self._parent_agent, "work_dir", "")
            ),
            "owner_task_id": getattr(self._parent_agent, "_chat_task_id", "") or "",
            "owner_task_title": getattr(self._parent_agent, "_chat_task_title", "") or "",
        }
        return agent.agent_id

    @staticmethod
    def _enforce_direct_child_boundary(registry: Any) -> None:
        """Runtime guard: a direct member can never regain delegation tools."""
        for name in ("Agent", "TeamCreate", "TeamDelete"):
            registry.unregister(name)
        leaked = {
            tool.name for tool in registry.list_tools()
            if tool.name in {"Agent", "TeamCreate", "TeamDelete"}
        }
        if leaked:
            raise RuntimeError(
                "recursive delegation boundary violation: " + ", ".join(sorted(leaked))
            )

    def _finish_run(self, agent_id: str, status: str) -> None:
        r = self.runs.get(agent_id)
        if r is not None and r["status"] == "running":
            r["status"] = status
            r["end"] = time.monotonic()
            r["completed_at"] = time.time()

    def _task_owner(self) -> dict[str, str]:
        parent = self._parent_agent
        turn_id = getattr(parent, "_chat_turn_id", "")
        return {
            "owner_session_id": getattr(parent, "session_id", "") or "",
            "owner_scope": (
                getattr(parent, "_conversation_scope", "") or "project"
            ),
            "owner_store_dir": (
                getattr(parent, "_conversation_store_dir", "")
                or getattr(parent, "work_dir", "")
            ),
            "owner_task_id": getattr(parent, "_chat_task_id", "") or "",
            "owner_task_title": getattr(parent, "_chat_task_title", "") or "",
            "dispatch_turn_id": turn_id if isinstance(turn_id, str) else "",
        }

    def _parent_permission_policy(self) -> Any:
        """Return the live parent policy or a bounded root policy for legacy parents."""
        from pathlib import Path

        from mewcode.permissions import PermissionMode
        from mewcode.permissions.grants import GrantStore
        from mewcode.permissions.policy import PermissionRulePaths, build_permission_checker

        work_dir = self._parent_agent.work_dir
        parent = getattr(self._parent_agent, "permission_checker", None)
        if parent is not None:
            return parent

        # The absence of a live checker does not grant the child a bypass.
        # Load the same user/project/local rule files as a normal runtime.
        project = Path(work_dir)
        parent_mode = getattr(self._parent_agent, "permission_mode", None)
        if parent_mode is None:
            parent_mode = PermissionMode.AUTO
        return build_permission_checker(
            work_dir,
            mode=parent_mode,
            rule_paths=PermissionRulePaths(
                user=Path.home() / ".mewcode" / "permissions.yaml",
                project=project / ".mewcode" / "permissions.yaml",
                local=project / ".mewcode" / "permissions.local.yaml",
            ),
            writable_scope=[work_dir],
            grant_store=GrantStore(work_dir),
        )

    def _build_child_checker(
        self,
        definition: AgentDef,
        work_dir: str,
        *,
        profile: str = "standard",
        mode_override: str | None = None,
    ) -> Any:
        from mewcode.permissions import PermissionMode
        from mewcode.permissions.policy import build_permission_checker

        parent = self._parent_permission_policy()
        declared_mode = mode_override if mode_override is not None else definition.permission_mode
        declared_mode = declared_mode or "default"
        requested_mode = (
            None if declared_mode in {"default", "inherit"}
            else PermissionMode(declared_mode)
        )
        writable_scope = (
            [work_dir] if profile == "worktree"
            else getattr(parent, "policy_writable_roots", None)
        )

        return build_permission_checker(
            work_dir,
            parent_policy=parent,
            mode=requested_mode,
            profile=profile,
            writable_scope=writable_scope,
        )

    def _build_regular_child_checker(self, definition: AgentDef) -> Any:
        return self._build_child_checker(definition, self._parent_agent.work_dir)

    async def _cleanup_unstarted_worktree(self, name: str, worktree: Any) -> str:
        """Clean only this unlaunched worktree, preserving any user changes."""
        try:
            result = await self._worktree_manager.auto_cleanup(name, worktree.head_commit)
        except Exception as exc:
            return f" Worktree cleanup could not be confirmed: {exc}"
        if result.kept:
            return f" Worktree preserved at {result.path} because it has changes."
        return ""

    async def execute(self, params: BaseModel) -> ToolResult:
        p: AgentToolParams = params  # type: ignore[assignment]

        if p.team_name and p.team_name.startswith('v2:'):
            return ToolResult(output='EXTERNAL_WORKER_REQUIRED: v2 tasks use the fixed Grok worker; Agent cannot spawn or impersonate a v2 teammate.', is_error=True)

        if (
            self._max_direct_spawns is not None
            and self._direct_spawn_count >= self._max_direct_spawns
        ):
            return ToolResult(
                output=(
                    "The direct sub-agent limit for this run has been reached "
                    f"({self._max_direct_spawns}). Reuse the current agents or "
                    "finish the task in the executor."
                ),
                is_error=True,
            )
        self._direct_spawn_count += 1

        if p.team_name:
            return await self._execute_as_teammate(p)

        isolation = ""
        if p.subagent_type:
            defn = self._agent_loader.get(p.subagent_type)
            if defn and defn.isolation:
                isolation = defn.isolation

        if isolation == "worktree":
            return await self._execute_with_worktree(p)

        from mewcode.agents.fork import ForkError, build_forked_messages
        from mewcode.agents.parser import AgentDef
        from mewcode.agents.tool_filter import resolve_agent_tools
        from mewcode.conversation import ConversationManager

        definition: AgentDef | None = None
        conversation: ConversationManager

        if p.subagent_type:
            definition = self._agent_loader.get(p.subagent_type)
            if definition is None:
                return ToolResult(
                    output=f"Unknown agent type: '{p.subagent_type}'. "
                    f"Available types: {', '.join(t for t, _ in self._agent_loader.list_agents())}",
                    is_error=True,
                )
        else:
            if not self._enable_fork:
                return ToolResult(
                    output="Fork mode is not enabled. "
                    "Set 'enable_fork: true' in config.yaml to use fork, "
                    "or specify a subagent_type parameter.",
                    is_error=True,
                )
            parent_conv = getattr(self._parent_agent, '_current_conversation', None)
            if parent_conv is None:
                return ToolResult(
                    output="Cannot fork: no active conversation in parent agent.",
                    is_error=True,
                )

            definition = AgentDef(
                agent_type="fork",
                when_to_use="Forked from parent agent",
                system_prompt="",
                disallowed_tools=[],
                model="inherit",
                max_turns=self._parent_agent.max_iterations,
                permission_mode="default",
                source="builtin",
            )

        # Reject a broader child mode or write scope before client/child creation.
        try:
            checker = self._build_regular_child_checker(definition)
        except (AttributeError, TypeError, ValueError) as exc:
            return ToolResult(
                output=f"Sub-agent permission policy rejected: {exc}",
                is_error=True,
            )

        if p.subagent_type:
            conversation = self._new_conversation()
        else:
            try:
                conversation = build_forked_messages(parent_conv, p.prompt)
            except ForkError as e:
                return ToolResult(output=str(e), is_error=True)

        # 选择 LLM 客户端
        try:
            runtime = self._select_runtime(p, definition)
        except (TypeError, ValueError) as exc:
            return ToolResult(output=f"Unable to resolve sub-agent model: {exc}", is_error=True)

        # 判断是否后台运行
        is_background = p.run_in_background or definition.background
        if self._enable_fork:
            is_background = True

        # 过滤工具（coordinator 模式可能缩减了注册表，这里用完整注册表）
        _base_registry = getattr(self._parent_agent, '_full_registry', None) or self._parent_agent.registry
        filtered_registry = resolve_agent_tools(
            _base_registry, definition, is_background
        )
        if not self._allow_child_delegation:
            self._enforce_direct_child_boundary(filtered_registry)

        # 创建子 agent
        sub_agent = self._create_child_agent(
            runtime,
            filtered_registry,
            work_dir=self._parent_agent.work_dir,
            max_iterations=definition.max_turns,
            permission_checker=checker,
            instructions_content=definition.system_prompt,
            conversation=conversation,
        )
        sub_agent.parent_id = self._parent_agent.agent_id
        sub_agent.trace_id = self._parent_agent.trace_id or self._parent_agent.agent_id

        # fork 子 agent 继承父 agent 的替换状态，确保共享的 tool_use_id 做出一致的
        # 决策——这样父子共享的 prompt cache 前缀才能保持字节级一致
        if p.subagent_type is None:
            from mewcode.context import clone_replacement_state
            sub_agent.replacement_state = clone_replacement_state(
                self._parent_agent.replacement_state
            )

        # 注册追踪节点
        trace_node = self._trace_manager.create(
            agent_type=definition.agent_type,
            parent_id=self._parent_agent.agent_id,
            trace_id=sub_agent.trace_id,
        )
        sub_agent.agent_id = trace_node.agent_id

        agent_name = p.name or p.subagent_type or f"agent-{trace_node.agent_id}"
        is_fork = p.subagent_type is None

        if is_background:
            if is_fork:
                sub_agent._fork_conversation = conversation
            task_id = self._task_manager.launch(
                agent=sub_agent,
                task="" if is_fork else p.prompt,
                name=agent_name,
                fork_conversation=conversation if is_fork else None,
                recovery_spec=({**p.model_dump(), "run_in_background": True,
                                "model": runtime.model or p.model,
                                "provider_key": runtime.provider_key or p.provider_key,
                                "effort": runtime.effort or p.effort or (
                                    getattr(runtime.client, "effort", "")
                                    if isinstance(getattr(runtime.client, "effort", ""), str) else ""),
                                "_route_fingerprint": provider_route_fingerprint(runtime.config)}
                               if not is_fork and not p.team_name else {}),
                **self._task_owner(),
                origin=self._task_origin,
                event_callback=(
                    (lambda event, child=sub_agent: self._child_event_callback(
                        child.agent_id, event
                    )) if self._child_event_callback is not None else None
                ),
            )
            self._register_run(
                sub_agent, agent_name, definition.agent_type,
                p.prompt, mode="bg", task_id=task_id,
            )
            return ToolResult(
                output=f"Sub-agent launched in background.\n"
                f"Task ID: {task_id}\n"
                f"Parent task ID: {self._task_manager.get(task_id).owner_task_id or 'unknown'}\n"
                f"Dispatch turn ID: {self._task_manager.get(task_id).dispatch_turn_id or 'unknown'}\n"
                f"Agent: {agent_name}\n"
                f"Type: {definition.agent_type}\n"
                f"The system will notify automatically when it completes.\n"
                f"Continue independent work. If this result gates completion, use BackgroundTasks with a bounded wait; do not claim completion before receiving it.",
            )

        # 前台同步执行
        self._register_run(
            sub_agent, agent_name, definition.agent_type, p.prompt, mode="fg",
        )
        try:
            if is_fork:
                result_text = await sub_agent.run_to_completion("", conversation)
            else:
                result_text = await sub_agent.run_to_completion(p.prompt)
        except asyncio.CancelledError:
            await self._cancel_foreground_child(sub_agent, trace_node.agent_id)
            raise
        except Exception as e:
            self._finish_run(sub_agent.agent_id, "failed")
            self._trace_manager.complete(trace_node.agent_id, "failed")
            await self._close_child_agent(sub_agent)
            return ToolResult(
                output=f"Sub-agent failed: {e}", is_error=True
            )

        self._finish_run(sub_agent.agent_id, "completed")
        self._trace_manager.update(
            trace_node.agent_id,
            input_tokens=sub_agent.total_input_tokens,
            output_tokens=sub_agent.total_output_tokens,
        )
        self._trace_manager.complete(trace_node.agent_id, "completed")
        await self._close_child_agent(sub_agent)

        return ToolResult(output=result_text or "(sub-agent returned no output)")

    async def _execute_as_teammate(self, p: AgentToolParams) -> ToolResult:
        if self._team_manager is None:
            return ToolResult(output="TeamManager not configured.", is_error=True)
        if self._worktree_manager is None:
            return ToolResult(output="WorktreeManager not configured for team spawn.", is_error=True)

        from mewcode.agents.fork import ForkError, build_forked_messages
        from mewcode.agents.parser import AgentDef
        from mewcode.agents.tool_filter import build_teammate_tools
        from mewcode.conversation import ConversationManager
        from mewcode.permissions import PermissionMode
        from mewcode.teams.models import BackendType, TeammateInfo
        from mewcode.teams.registry import AgentNameRegistry

        team = self._team_manager.get_team(p.team_name)
        if team is None:
            return ToolResult(output=f"Team '{p.team_name}' not found. Create it first with TeamCreate.", is_error=True)

        # 上限只统计活跃成员——已退出成员会在下方同名重生时清位复用，
        # 不应占用名额把团队慢慢卡死
        active_count = len([m for m in team.members if m.is_active is not False])
        if active_count >= MAX_TEAMMATES_PER_TEAM:
            return ToolResult(
                output=(
                    f"Team '{p.team_name}' already has {active_count} active "
                    f"members (limit {MAX_TEAMMATES_PER_TEAM}). Reuse an active "
                    f"teammate via SendMessage, or delete the team and start over."
                ),
                is_error=True,
            )

        backend = self._team_manager.detect_backend()
        if backend in (BackendType.TMUX, BackendType.ITERM2):
            try:
                parent_mode = PermissionMode(self._parent_permission_policy().mode)
            except (AttributeError, TypeError, ValueError) as exc:
                return ToolResult(output=f"Teammate permission policy rejected: {exc}", is_error=True)
            if parent_mode in {
                PermissionMode.AUTO, PermissionMode.STRICT_ISOLATION, PermissionMode.PLAN
            }:
                return ToolResult(
                    output=(
                        f"Teammate backend {backend.value} cannot carry the parent's "
                        f"{parent_mode.value} permission policy across processes; "
                        "use the in-process backend."
                    ),
                    is_error=True,
                )

        # 队友名会进入邮箱目录名、tmux 命令行和名字注册表——必须消毒，
        # 与团队名使用同一套 slug 规则
        base_name = _sanitize_agent_name(p.name or p.subagent_type or "worker")

        # 同名成员已退出（is_active=False）→ 清位复用原名：SendMessage 的
        # "Spawn it again with name=..." 指引才真正可行，否则重生会被静默
        # 改名为 name-2，原名永远解析到死 agent_id
        _prune_stale_member(team, base_name)

        existing_names = {m.name for m in team.members}
        teammate_name = base_name
        if teammate_name in existing_names:
            counter = 2
            while f"{base_name}-{counter}" in existing_names:
                counter += 1
            teammate_name = f"{base_name}-{counter}"

        # 1. 加载 agent 定义
        definition: AgentDef
        conversation: ConversationManager | None = None
        is_fork = False

        if p.subagent_type:
            defn = self._agent_loader.get(p.subagent_type)
            if defn is None:
                return ToolResult(
                    output=f"Unknown agent type: '{p.subagent_type}'. "
                    f"Available: {', '.join(t for t, _ in self._agent_loader.list_agents())}",
                    is_error=True,
                )
            definition = defn
        else:
            if self._enable_fork:
                try:
                    parent_conv = getattr(self._parent_agent, '_current_conversation', None)
                    if parent_conv is None:
                        return ToolResult(output="Cannot fork: no active conversation.", is_error=True)
                    conversation = build_forked_messages(parent_conv, p.prompt)
                    is_fork = True
                except ForkError as e:
                    return ToolResult(output=str(e), is_error=True)

            definition = AgentDef(
                agent_type="teammate",
                when_to_use="Team member",
                system_prompt="",
                disallowed_tools=[],
                model="inherit",
                max_turns=self._parent_agent.max_iterations,
                permission_mode="default",
                source="builtin",
            )

        if backend in (BackendType.TMUX, BackendType.ITERM2):
            try:
                # Validate mode narrowing even though a pane cannot receive the
                # in-process checker or its parent rule/grant context.
                pane_checker = self._build_child_checker(
                    definition,
                    self._parent_agent.work_dir,
                    mode_override=self._teammate_permission_mode or None,
                )
            except (AttributeError, TypeError, ValueError) as exc:
                return ToolResult(output=f"Teammate permission policy rejected: {exc}", is_error=True)
            if pane_checker.mode in {
                PermissionMode.AUTO, PermissionMode.STRICT_ISOLATION, PermissionMode.PLAN
            }:
                return ToolResult(
                    output=(
                        f"Teammate backend {backend.value} cannot carry the requested "
                        f"{pane_checker.mode.value} permission policy across processes; "
                        "use the in-process backend."
                    ),
                    is_error=True,
                )

        # 2. 创建 worktree
        wt_name = f"team-{p.team_name}/{teammate_name}"
        try:
            wt = await self._worktree_manager.create(wt_name, "HEAD")
        except Exception as e:
            return ToolResult(output=f"Failed to create worktree for teammate: {e}", is_error=True)

        checker = None
        if backend not in (BackendType.TMUX, BackendType.ITERM2):
            try:
                checker = self._build_child_checker(
                    definition,
                    wt.path,
                    profile="worktree",
                    mode_override=self._teammate_permission_mode or None,
                )
            except (AttributeError, TypeError, ValueError) as exc:
                cleanup_note = await self._cleanup_unstarted_worktree(wt_name, wt)
                return ToolResult(
                    output=(
                        f"Teammate permission policy rejected: {exc}. "
                        f"Authorize the exact worktree directory {wt.path} in the parent "
                        f"writable scope before retrying.{cleanup_note}"
                    ),
                    is_error=True,
                )

        # 3. 选择 LLM
        try:
            runtime = self._select_runtime(p, definition)
        except (TypeError, ValueError) as exc:
            cleanup_note = await self._cleanup_unstarted_worktree(wt_name, wt)
            return ToolResult(output=f"Unable to resolve teammate model: {exc}.{cleanup_note}", is_error=True)

        # 5. 构建队友的工具集
        trace_node = self._trace_manager.create(
            agent_type=definition.agent_type,
            parent_id=self._parent_agent.agent_id,
            trace_id=self._parent_agent.trace_id or self._parent_agent.agent_id,
        )
        agent_id = trace_node.agent_id

        _has_full = getattr(self._parent_agent, '_full_registry', None) is not None
        full_registry = getattr(self._parent_agent, '_full_registry', None) or self._parent_agent.registry
        _full_tools = [t.name for t in full_registry.list_tools()]
        log.info(
            "[teammate] has_full_registry=%s full_tools=%d names=%s backend=%s def_tools=%s def_disallowed=%s",
            _has_full, len(_full_tools), _full_tools,
            backend.value,
            getattr(definition, 'tools', []),
            getattr(definition, 'disallowed_tools', []),
        )
        teammate_registry = build_teammate_tools(
            parent_registry=full_registry,
            team_manager=self._team_manager,
            team_name=p.team_name,
            agent_id=agent_id,
            agent_name=teammate_name,
            backend_type=backend.value,
            definition=definition,
        )
        if not self._allow_child_delegation:
            self._enforce_direct_child_boundary(teammate_registry)
        _tm_tools = [t.name for t in teammate_registry.list_tools()]
        log.info("[teammate] result_tools=%d names=%s", len(_tm_tools), _tm_tools)

        # 6. 创建子 agent 并附加队友专属指令（注入身份与花名册；
        #    此时 team.members 尚未包含自己，故单独补一行 "(you)"）
        roster_lines = "\n".join(
            ["- lead (the coordinator; message it as 'lead')"]
            + [f"- {m.name} ({m.agent_type or 'worker'})" for m in team.members]
            + [f"- {teammate_name} (you)"]
        )
        instructions = (definition.system_prompt or "") + TEAMMATE_ADDENDUM_TEMPLATE.format(
            name=teammate_name, team=p.team_name, roster=roster_lines
        )

        # Pane workers run in another process and cannot receive this checker.
        # Only the in-process route constructs a child with the inherited policy.
        sub_agent = None
        if checker is not None:
            sub_agent = self._create_child_agent(
                runtime,
                teammate_registry,
                work_dir=wt.path,
                max_iterations=definition.max_turns,
                permission_checker=checker,
                instructions_content=instructions,
                conversation=conversation,
            )
            sub_agent.parent_id = self._parent_agent.agent_id
            sub_agent.trace_id = self._parent_agent.trace_id or self._parent_agent.agent_id
            sub_agent.agent_id = agent_id
            sub_agent.team_name = p.team_name
            sub_agent._team_manager = self._team_manager

        # 7. 注册名称和成员信息
        AgentNameRegistry.instance().register(teammate_name, agent_id)

        member = TeammateInfo(
            name=teammate_name,
            agent_id=agent_id,
            agent_type=definition.agent_type,
            model=runtime.model or p.model or definition.model,
            worktree_path=wt.path,
            backend_type=backend.value,
            is_active=True,
        )
        try:
            self._team_manager.register_member(p.team_name, member)
        except Exception as e:
            AgentNameRegistry.instance().unregister(teammate_name)
            team.remove_member(teammate_name)
            team.save()
            cleanup_note = await self._cleanup_unstarted_worktree(wt_name, wt)
            self._trace_manager.complete(trace_node.agent_id, "failed")
            if sub_agent is not None:
                await self._close_child_agent(sub_agent)
            return ToolResult(output=f"Failed to register teammate: {e}.{cleanup_note}", is_error=True)

        # 8. 按后端类型启动队友
        if backend in (BackendType.TMUX, BackendType.ITERM2):
            result = self._spawn_pane_teammate(
                p, team, member, backend, wt, agent_id, teammate_name
            )
            if result.is_error:
                # 回滚：不留下永久 is_active=True 的幽灵成员和孤儿 worktree
                # （不回滚的话 delete_team 会因 "active members" 永久拒删）
                team.remove_member(teammate_name)
                team.save()
                AgentNameRegistry.instance().unregister(teammate_name)
                await self._cleanup_unstarted_worktree(wt_name, wt)
                self._trace_manager.complete(trace_node.agent_id, "failed")
            return result

        # 进程内模式：直接用 task_manager 执行并通知结果。
        # progress 挂到 member 上（TUI 进度树读取），事件回调由 task_manager 接线
        from mewcode.teams.progress import TeammateProgress, random_verb
        progress = TeammateProgress(
            name=teammate_name,
            team_name=p.team_name,
            spinner_verb=random_verb(),
        )
        member.progress = progress

        task_id = self._task_manager.launch(
            agent=sub_agent,
            task="" if is_fork else p.prompt,
            name=teammate_name,
            fork_conversation=conversation if is_fork else None,
            progress=progress,
            **self._task_owner(),
            origin=self._task_origin,
            event_callback=(
                (lambda event, child=sub_agent: self._child_event_callback(
                    child.agent_id, event
                )) if self._child_event_callback is not None else None
            ),
        )
        self._team_manager.register_task_handle(agent_id, self._task_manager, task_id)
        self._register_run(
            sub_agent, teammate_name, definition.agent_type,
            p.prompt, mode="teammate", team=p.team_name, task_id=task_id,
        )

        return ToolResult(
            output=(
                f"Teammate '{teammate_name}' spawned in team '{p.team_name}'.\n"
                f"Agent ID: {agent_id}\n"
                f"Backend: {backend.value}\n"
                f"Worktree: {wt.path}\n"
                f"Task ID: {task_id}\n"
                f"The system will notify when it completes."
            )
        )


    def _spawn_pane_teammate(
        self, p: Any, team: Any, member: Any, backend: Any, wt: Any,
        agent_id: str, teammate_name: str,
    ) -> ToolResult:
        from mewcode.teams.models import BackendType

        mailbox = self._team_manager.get_mailbox(p.team_name)
        mailbox_dir = str(mailbox._base_dir) if mailbox else ""

        try:
            if backend == BackendType.TMUX:
                from mewcode.teams.spawn_tmux import spawn_tmux_teammate
                pane_info = spawn_tmux_teammate(
                    team_name=p.team_name,
                    teammate_name=teammate_name,
                    worktree_path=wt.path,
                    prompt=p.prompt,
                    agent_type=p.subagent_type or "",
                    model=p.model or "",
                    mailbox_dir=mailbox_dir,
                )
                self._team_manager.register_pane_id(agent_id, pane_info.pane_id)
            elif backend == BackendType.ITERM2:
                from mewcode.teams.spawn_iterm2 import spawn_iterm2_teammate
                pane_info = spawn_iterm2_teammate(
                    team_name=p.team_name,
                    teammate_name=teammate_name,
                    worktree_path=wt.path,
                    prompt=p.prompt,
                    agent_type=p.subagent_type or "",
                    model=p.model or "",
                    mailbox_dir=mailbox_dir,
                )
        except Exception as e:
            log.warning("Pane spawn failed, falling back to in-process: %s", e)
            return ToolResult(
                output=f"Pane spawn failed ({e}), teammate not started. Retry or set teammate_mode to in-process.",
                is_error=True,
            )

        return ToolResult(
            output=(
                f"Teammate '{teammate_name}' spawned in team '{p.team_name}'.\n"
                f"Agent ID: {agent_id}\n"
                f"Backend: {backend.value} (pane)\n"
                f"Worktree: {wt.path}\n"
                f"The teammate is running in an independent process."
            )
        )


    def _select_llm(
        self,
        params: AgentToolParams,
        definition: AgentDef,
    ) -> LLMClient:
        """Compatibility wrapper retained for integrations that select only a client."""
        return self._select_runtime(params, definition).client

    def _select_runtime(
        self,
        params: AgentToolParams,
        definition: AgentDef,
    ) -> _AgentRuntime:
        from mewcode.agents.parser import AgentDef

        provider_key = params.provider_key or getattr(definition, "provider_key", None)
        model_override = params.model or (
            definition.model if definition.model != "inherit" else None
        )
        effort = params.effort or getattr(definition, "effort", None) or ""
        default_deepseek_effort = (
            "high"
            if definition.agent_type.strip().lower() in {"plan", "planner"}
            else "low"
        )

        if provider_key:
            if self._provider_registry is None:
                raise ValueError(
                    f"provider registry is unavailable; cannot resolve {provider_key}"
                )
            if self._provider_registry.get(provider_key) is None:
                matches = [
                    record.key for record in self._provider_registry.records()
                    if record.provider.name == provider_key and not record.error
                ]
                if len(matches) == 1:
                    provider_key = matches[0]
            if not model_override or model_override == "inherit":
                record = self._provider_registry.get(provider_key)
                if record is None:
                    raise ValueError(f"provider is unavailable: {provider_key}")
                model_override = record.provider.model
            from mewcode.model_registry import ModelRef

            resolved = self._provider_registry.resolve(
                ModelRef(provider_key, model_override, effort),
                usage_recorder=self._parent_agent.client.get_usage_recorder(),
            )
            child_effort = effort
            if (
                not child_effort
                and getattr(
                    getattr(resolved.client, "capabilities", None),
                    "context_channel",
                    "",
                )
                == "deepseek"
            ):
                child_effort = default_deepseek_effort
                resolved.client.set_effort(child_effort)
            return _AgentRuntime(
                client=resolved.client,
                protocol=resolved.protocol,
                context_window=resolved.context_window,
                auxiliary_client=resolved.client,
                config=resolved.config,
                provider_key=provider_key,
                model=model_override,
                effort=child_effort,
            )

        if model_override and model_override != "inherit":
            if model_override.lower() == "gpt-6-luna" and self._provider_registry is None:
                raise ValueError("gpt-6-luna 需要可用的 Provider 注册表，不能沿用父 Agent 路由")
            if self._provider_registry is not None and model_override not in {"haiku", "sonnet", "opus"}:
                from mewcode.model_registry import ModelRef

                finder = getattr(self._provider_registry, "find_subagent_provider_key", None)
                selected_key = finder(model_override) if callable(finder) else ""
                if model_override.lower() == "gpt-6-luna" and not selected_key:
                    raise ValueError(
                        "gpt-6-luna 没有可用的非 OpenCode Go Provider；"
                        "请先配置 codex:codex-custom，或显式指定 provider_key"
                    )
                if selected_key:
                    resolved = self._provider_registry.resolve(
                        ModelRef(selected_key, model_override, effort),
                        usage_recorder=self._parent_agent.client.get_usage_recorder(),
                    )
                    return _AgentRuntime(
                        client=resolved.client,
                        protocol=resolved.protocol,
                        context_window=resolved.context_window,
                        auxiliary_client=resolved.client,
                        config=resolved.config,
                        provider_key=selected_key,
                        model=model_override,
                        effort=resolved.effort,
                    )
            client = self._create_client_for_model(model_override)
            if client is not None:
                from mewcode.opencode_go import resolve_effective_provider_config

                child_effort = effort
                if (
                    not child_effort
                    and getattr(
                        getattr(client, "capabilities", None),
                        "context_channel",
                        "",
                    )
                    == "deepseek"
                ):
                    child_effort = default_deepseek_effort
                if child_effort:
                    client.set_effort(child_effort)
                config = self._provider_config
                effective_config = (
                    resolve_effective_provider_config(config, model_override)
                    if config is not None
                    else None
                )
                return _AgentRuntime(
                    client=client,
                    protocol=(
                        effective_config.protocol
                        if effective_config is not None
                        else self._parent_agent.protocol
                    ),
                    context_window=(
                        effective_config.get_context_window()
                        if effective_config is not None
                        else self._parent_agent.context_window
                    ),
                    auxiliary_client=client,
                    config=effective_config,
                    model=model_override,
                    effort=child_effort,
                )

        parent_capabilities = getattr(
            self._parent_agent, "model_capabilities", None
        )
        if (
            getattr(parent_capabilities, "context_channel", "") == "deepseek"
            and self._provider_config is not None
        ):
            child_client = self._create_client_for_model(
                self._provider_config.model
            )
            if child_client is not None:
                child_effort = effort or default_deepseek_effort
                child_client.set_effort(child_effort)
                return _AgentRuntime(
                    client=child_client,
                    protocol=self._provider_config.protocol,
                    context_window=self._provider_config.get_context_window(),
                    auxiliary_client=child_client,
                    config=self._provider_config,
                    model=self._provider_config.model,
                    effort=child_effort,
                )

        return _AgentRuntime(
            client=self._parent_agent.client,
            protocol=self._parent_agent.protocol,
            context_window=self._parent_agent.context_window,
            auxiliary_client=self._parent_agent.auxiliary_client,
            config=self._provider_config,
            model=getattr(self._provider_config, "model", ""),
            effort=effort,
        )

    def _create_child_agent(
        self,
        runtime: _AgentRuntime,
        registry: Any,
        *,
        work_dir: str,
        max_iterations: int,
        permission_checker: Any,
        instructions_content: str,
        conversation: Any = None,
    ) -> Any:
        runtime_context = getattr(self._parent_agent, "runtime_context", None)
        fallback_host = None
        provider = runtime.config or self._provider_config
        if runtime_context is None:
            from mewcode.plugins import ApplicationHost

            fallback_host = ApplicationHost(work_dir, discover_plugins=False).start()
            runtime_context = fallback_host.context
        if provider is None:
            from mewcode.config import ProviderConfig

            provider = ProviderConfig(
                name="inherited",
                protocol=runtime.protocol,
                base_url="",
                model=getattr(runtime.client, "model", "inherited"),
            )
        if runtime_context is not None:
            from mewcode.plugins.runtime import AgentRuntimeRequest
            from mewcode.plugins.services import AGENT_FACTORY, USAGE

            scope = runtime_context.get(AGENT_FACTORY).create(AgentRuntimeRequest(
                provider=provider,
                work_dir=work_dir,
                client=runtime.client,
                auxiliary_client=runtime.auxiliary_client,
                usage_service=(self._parent_agent.runtime_context.get(USAGE, None)
                               if getattr(self._parent_agent, "runtime_context", None) is not None else None),
                permission_checker=permission_checker,
                conversation=conversation,
                memory_manager=self._parent_agent.memory_manager,
                hook_engine=self._parent_agent.hook_engine,
                max_iterations=max_iterations,
                context_window=runtime.context_window,
                instructions_content=instructions_content,
                 enable_skills=False,
                 enable_tool_search=False,
                 include_default_tools=False,
                 inherit_parent_tools=False,
                 extra_tools=tuple(registry.list_tools()),
            ))
            agent = scope.agent
            # Child model receipts belong to the originating conversation, not
            # to an empty/new session. Agent IDs still distinguish the workers.
            agent.session_id = getattr(self._parent_agent, "session_id", "")
            for tool in registry.list_tools():
                if registry.is_discovered(tool.name) and agent.registry.is_enabled(tool.name):
                    agent.registry.mark_discovered(tool.name)
            agent._cordis_runtime = scope
            agent._cordis_host = fallback_host
            return agent

        raise RuntimeError("Agent runtime factory is unavailable")

    @staticmethod
    async def _close_child_agent(agent: Any) -> None:
        scope = getattr(agent, "_cordis_runtime", None)
        if scope is not None:
            await scope.aclose()
            agent._cordis_runtime = None
        host = getattr(agent, "_cordis_host", None)
        if host is not None:
            await host.aclose()
            agent._cordis_host = None

    async def _cancel_foreground_child(self, agent: Any, trace_id: str) -> None:
        status = "cancelled"
        try:
            await self._close_child_agent(agent)
        except (asyncio.CancelledError, Exception) as exc:
            status = "failed"
            agent._native_cleanup_error = f"Foreground child cleanup unconfirmed: {exc}"
            self._parent_agent._native_cleanup_error = agent._native_cleanup_error
            log.exception("Foreground child cleanup failed after cancellation")
        finally:
            self._finish_run(agent.agent_id, status)
            self._trace_manager.complete(trace_id, status)

    def _new_conversation(self) -> Any:
        runtime_context = getattr(self._parent_agent, "runtime_context", None)
        if runtime_context is not None:
            from mewcode.plugins.services import CONVERSATION_FACTORY

            factory = runtime_context.get(CONVERSATION_FACTORY, None)
            if factory is not None:
                return factory.create()
        from mewcode.conversation import ConversationManager

        return ConversationManager()


    async def _execute_with_worktree(self, p: AgentToolParams) -> ToolResult:
        if self._worktree_manager is None:
            return ToolResult(
                output="Worktree isolation is not available: WorktreeManager not configured.",
                is_error=True,
            )

        from mewcode.agents.parser import AgentDef
        from mewcode.agents.tool_filter import resolve_agent_tools
        from mewcode.worktree.integration import (
            build_worktree_notice,
            generate_worktree_name,
        )

        definition: AgentDef | None = None
        if p.subagent_type:
            definition = self._agent_loader.get(p.subagent_type)
            if definition is None:
                return ToolResult(
                    output=f"Unknown agent type: '{p.subagent_type}'. "
                    f"Available types: {', '.join(t for t, _ in self._agent_loader.list_agents())}",
                    is_error=True,
                )
        else:
            definition = AgentDef(
                agent_type="worktree-agent",
                when_to_use="Isolated worktree agent",
                system_prompt="",
                disallowed_tools=[],
                model="inherit",
                max_turns=self._parent_agent.max_iterations,
                permission_mode="default",
                source="builtin",
            )

        wt_name = generate_worktree_name()
        try:
            wt = await self._worktree_manager.create(wt_name, "HEAD")
        except Exception as e:
            return ToolResult(
                output=f"Failed to create worktree: {e}",
                is_error=True,
            )

        try:
            checker = self._build_child_checker(definition, wt.path, profile="worktree")
        except (AttributeError, TypeError, ValueError) as exc:
            cleanup_note = await self._cleanup_unstarted_worktree(wt_name, wt)
            return ToolResult(
                output=(
                    f"Worktree permission policy rejected: {exc}. "
                    f"Authorize the exact worktree directory {wt.path} in the parent "
                    f"writable scope before retrying.{cleanup_note}"
                ),
                is_error=True,
            )

        notice = build_worktree_notice(self._parent_agent.work_dir, wt.path)
        task = notice + "\n\n" + p.prompt

        try:
            runtime = self._select_runtime(p, definition)
        except (TypeError, ValueError) as exc:
            cleanup_note = await self._cleanup_unstarted_worktree(wt_name, wt)
            return ToolResult(output=f"Unable to resolve worktree model: {exc}.{cleanup_note}", is_error=True)

        _base_registry = getattr(self._parent_agent, '_full_registry', None) or self._parent_agent.registry
        filtered_registry = resolve_agent_tools(
            _base_registry, definition, False
        )
        if not self._allow_child_delegation:
            self._enforce_direct_child_boundary(filtered_registry)

        sub_agent = self._create_child_agent(
            runtime,
            filtered_registry,
            work_dir=wt.path,
            max_iterations=definition.max_turns,
            permission_checker=checker,
            instructions_content=definition.system_prompt,
        )
        sub_agent.parent_id = self._parent_agent.agent_id
        sub_agent.trace_id = self._parent_agent.trace_id or self._parent_agent.agent_id

        trace_node = self._trace_manager.create(
            agent_type=definition.agent_type,
            parent_id=self._parent_agent.agent_id,
            trace_id=sub_agent.trace_id,
        )
        sub_agent.agent_id = trace_node.agent_id

        wt_agent_name = p.name or p.subagent_type or f"agent-{trace_node.agent_id}"
        self._register_run(
            sub_agent, wt_agent_name, definition.agent_type, p.prompt, mode="fg",
        )
        try:
            result_text = await sub_agent.run_to_completion(task)
        except asyncio.CancelledError:
            # Preserve the worktree on interruption; its partial edits belong
            # to the user and may still be needed when this task resumes.
            await self._cancel_foreground_child(sub_agent, trace_node.agent_id)
            raise
        except Exception as e:
            self._finish_run(sub_agent.agent_id, "failed")
            self._trace_manager.complete(trace_node.agent_id, "failed")
            await self._close_child_agent(sub_agent)
            return ToolResult(
                output=f"Sub-agent in worktree failed: {e}",
                is_error=True,
            )

        self._finish_run(sub_agent.agent_id, "completed")
        self._trace_manager.update(
            trace_node.agent_id,
            input_tokens=sub_agent.total_input_tokens,
            output_tokens=sub_agent.total_output_tokens,
        )
        self._trace_manager.complete(trace_node.agent_id, "completed")
        await self._close_child_agent(sub_agent)

        cleanup = await self._worktree_manager.auto_cleanup(wt_name, wt.head_commit)
        if cleanup.kept:
            result_text = (result_text or "") + (
                f"\n[Worktree preserved at {cleanup.path}, branch {cleanup.branch}]"
            )

        return ToolResult(output=result_text or "(sub-agent returned no output)")


    def _create_client_for_model(self, model_alias: str) -> LLMClient | None:
        if self._provider_config is None:
            return None

        from mewcode.config import ProviderConfig

        model_map = {
            "haiku": "claude-haiku-4-5-20251001",
            "sonnet": "claude-sonnet-5",
            "opus": "claude-opus-5",
        }
        # 别名映射到的是 Claude 家族模型，只对 anthropic 协议 Provider 有意义；
        # 其他协议（如 codex/OpenAI 中转）上请求必然报"模型不存在"——
        # 子 agent 首次 LLM 调用即失败（agent 定义里的 model: haiku 正是这样
        # 炸掉全部子 agent 的）。返回 None 让 _select_llm 回退到继承父模型。
        if model_alias in model_map and self._provider_config.protocol != "anthropic":
            log.info(
                "[subagent] model alias '%s' unavailable on protocol '%s', "
                "inheriting parent model",
                model_alias, self._provider_config.protocol,
            )
            return None
        model_id = model_map.get(model_alias, model_alias)

        config = ProviderConfig(
            name=f"sub-{model_alias}",
            protocol=self._provider_config.protocol,
            base_url=self._provider_config.base_url,
            model=model_id,
            provider_type=self._provider_config.provider_type,
            api_key=self._provider_config.api_key,
            thinking=self._provider_config.thinking,
            context_channel=self._provider_config.context_channel,
            context_window=self._provider_config.context_window,
            max_output_tokens=self._provider_config.max_output_tokens,
            service_tier=self._provider_config.service_tier,
            default_headers=dict(self._provider_config.default_headers),
            usage=self._provider_config.usage,
        )
        try:
            runtime_context = getattr(self._parent_agent, "runtime_context", None)
            if runtime_context is not None:
                from mewcode.plugins.services import CLIENT_FACTORY

                client = runtime_context.get(CLIENT_FACTORY).create(config)
            else:
                from mewcode.plugins import ApplicationHost
                from mewcode.plugins.services import CLIENT_FACTORY

                host = ApplicationHost(
                    self._parent_agent.work_dir,
                    discover_plugins=False,
                ).start()
                try:
                    client = host.context.get(CLIENT_FACTORY).create(config)
                finally:
                    host.close()
            client.set_usage_recorder(
                self._parent_agent.client.get_usage_recorder()
            )
            return client
        except Exception:
            return None
