"""MewCode Web 前端服务（FastAPI + WebSocket）。

复用 mewcode 核心（Agent / 工具注册表 / 权限 / Provider 插槽 / 会话持久化），
用浏览器界面替代终端 TUI——原生解决中文 IME、渲染与滚动问题。

运行：uv run python frontend/server.py  →  http://127.0.0.1:8765
"""
from __future__ import annotations

import asyncio
import argparse
import copy
import fnmatch
import hashlib
import ipaddress
import io
import json
import logging
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import zipfile
from contextlib import aclosing, asynccontextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal
from urllib.parse import quote, urlsplit, urlunsplit

_NATIVE_CONTROL_BUSY = threading.Event()

# 直接 `python frontend/server.py` 运行时，确保项目根在 sys.path 中
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import uvicorn
from fastapi import FastAPI, File, Form, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field as PydanticField

from mewcode.agent import (
    Agent,
    AskUserRequest,
    CompactNotification,
    ErrorEvent,
    HumanOnCallRequest,
    LoopComplete,
    PermissionRequest,
    PermissionResponse,
    RetryEvent,
    StreamRetryEvent,
    StreamText,
    ThinkingText,
    ToolResultEvent,
    ToolUseEvent,
    TurnComplete,
    UsageEvent,
)
from mewcode.attachments import (
    MAX_FILE_BYTES,
    MAX_MESSAGE_BYTES,
    AttachmentError,
    AttachmentStore,
)
from mewcode.auxiliary import create_auxiliary_client
from mewcode.agents.notification import format_task_notification
from mewcode.agents.task_manager import BackgroundTask, TaskManager
from mewcode.agents.trace import TraceManager
from mewcode.cache import FileCache
from mewcode.client import (
    LLMClient,
    LLMError,
    create_client,
    model_effort_levels,
    model_supports_vision,
    resolve_context_window,
)
from mewcode.cordis import Context, PluginSpec
from mewcode.config import ConfigError, ProviderConfig, TailscaleConfig, load_config
from mewcode.context.manager import ensure_session_dir
from mewcode.conversation import ConversationManager, Message
from mewcode.memory import (
    MemoryManager,
    Session,
    SessionManager,
    SessionMeta,
    load_instructions,
    make_compact_boundary,
)
from mewcode.expert_group import (
    EXPERT_ROLE_CONTRACTS,
    ExpertProfile,
    ExpertProfileStore,
    relocate_expert_profile,
    select_capability_executor_model,
)
from mewcode.expert_web import expert_flow_model_error, expert_resume_model_error, relocate_expert_flow, run_expert_chat as _run_expert_chat_pass
from mewcode.orchestration.chat_routing import (
    bind_task_permission_scope, dialogue_focus, prepare_chat_focus, requests_orchestration_resume, set_dialogue_focus,
    validate_chat_continuation,
)
from mewcode.orchestration.activity import ActivityStore, record_pipeline_activity
from mewcode.continuation import (
    ContinuousChat, control_intent, hold as hold_continuation, make_reviewer,
    prepare_turn as prepare_continuous_turn, public_state as continuation_status,
    state_for as continuation_state,
)
from mewcode.expert_runs import ExpertRunStore, is_resumable_snapshot, parse_resume_message
from mewcode.experts.flow import ExpertFlowError, ExpertFlowSpec, ExpertFlowStore
from mewcode.experts.lessons import FlowLessonStore
from mewcode.model_intelligence import ModelIntelligenceService
from mewcode.model_registry import ModelRef
from mewcode.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from mewcode.permissions.modes import EXECUTION_PERMISSION_MODES, public_permission_mode
from mewcode.provider_sources import (
    ProviderSlot,
    discover_provider_slots,
    validate_slot,
)
from mewcode.opencode_go import (
    is_opencode_go_provider,
    ordered_go_model_ids,
    resolve_effective_provider_config,
)
from mewcode.runtime_paths import expert_runs_root, user_data_root
from mewcode.runtime_scheduler import runtime_scheduler_for_agent, bind_runtime_scheduler, reset_runtime_scheduler
from mewcode.desktop_providers import bootstrap_desktop_providers, clobber_block_reason
from mewcode.tools import ToolRegistry
from mewcode.tools.ask_user import AskUserTool
from mewcode.tools.human_oncall import validate_answer_notes, validate_human_answers
from mewcode.tools.agent_tool import AgentTool
from mewcode.tools.impl.tool_search import ToolSearchTool
from mewcode.tools.task_stop import TaskStopTool
from mewcode.tools.team_create import TeamCreateTool
from mewcode.usage.models import (
    AttributionStatus,
    EvidenceLevel,
    RequestUsage,
    TokenUsage,
    UsageAccount,
)
from mewcode.usage.plugin_worker import main as usage_plugin_worker_main
from mewcode.usage.plugins import MAX_PLUGIN_BYTES
from mewcode.usage.runtime import UsageRuntime
from mewcode.usage.codex import discover_codex_usage_account
from mewcode.tools.team_delete import TeamDeleteTool
from mewcode.teams.manager import TeamManager
from mewcode.usage.service import UsageService
from mewcode.usage.ledger import DEFAULT_LEDGER_PATH
from mewcode.worktree.manager import WorktreeManager
from mewcode.tailscale_remote import (
    collect_tailscale_status,
    configured_remote_nodes,
    discovered_remote_nodes,
    remote_node_request,
)
from mewcode.external_agents import ExternalAgentStore
from mewcode.filehistory import FileHistory
from mewcode.cc_switch import (
    cc_switch_bindings_fingerprint,
    cc_switch_family,
    read_cc_switch_runtime_bindings,
    read_cc_switch_snapshot,
    select_cc_switch_mirror_target,
)
from mewcode.usage.provider_meters import collect_provider_meters
from mewcode.orchestration import (
    OrchestrationError,
    OrchestrationSpec,
    build_control_plan,
    from_expert_flow,
    from_pipeline_graph,
    get_plan_session_store,
    get_template,
    is_orchestration_graph,
    list_templates,
    merge_model_provider_keys,
    propose_orchestration,
    sync_orchestration_graph_from_spec,
    to_expert_flow,
    to_pipeline_graph,
    try_parse_planner_json,
    validate_spec,
)
from mewcode.orchestration.change_explain import node_change_report
from mewcode.orchestration.chat_reports import ChatReportStore
from mewcode.orchestration.convert import apply_profile_bindings
from mewcode.orchestration.extract import (
    build_clarify_prompt,
    coerce_dialogue_messages,
    collect_open_questions,
    dialogue_has_user_turn,
    extract_orchestration_from_dialogue,
    OrchestrationStreamTimeout,
    ORCHESTRATION_FIRST_EVENT_TIMEOUT_SECONDS,
    ORCHESTRATION_IDLE_TIMEOUT_SECONDS,
    ORCHESTRATION_REASONING_IDLE_TIMEOUT_SECONDS,
    ORCHESTRATION_SOFT_IDLE_WARNING_SECONDS,
    overlay_plan_briefs,
    inventory_orchestration_from_dialogue,
    refill_plan_briefs_from_dialogue,
)
from mewcode.orchestration.plan_session import build_plan_session, PlanSessionStore
from mewcode.orchestration.run_inputs import append_run_inputs, resolve_run_inputs
from mewcode.orchestration.revisions import (
    saved_execution_flow, refresh_chat_plan, replace_plan_reminder, CHAT_PLAN_MARKER,
    can_resume_confirmed_flow, canvas_nodes_reminder, refresh_canvas_nodes,
)
from mewcode.pipeline.compiler import PipelineCompiler
from mewcode.pipeline.models import PipelineGraph
from mewcode.remote_pairing import (
    PairingError,
    RemotePairingStore,
    pairing_peer_allowed,
    validate_endpoint,
)
from mewcode.tailscale_serve import ensure_https_proxy
from mewcode.workspace_tree import build_import_text, collect_workspace_tree, sort_history_items
from mewcode.completion.api import register_completion_api
from mewcode.pipeline.api import register_pipeline_api
from mewcode.pipeline.actions import PipelineActionRequest, normalize_actions
from mewcode.pipeline.conversation import PipelineConversationAction
from mewcode.pipeline.store import PipelineStore, VersionConflictError
from mewcode.pipeline.tool_actions import build_tool_actions, merge_pipeline_actions
from mewcode.pipeline_events import (
    PipelineEventStore,
    PipelineRecorder,
    legacy_entries_to_pipeline,
    load_pipeline_items,
)
from mewcode.plugins import ApplicationHost
from mewcode.plugins.fastapi import fastapi_plugin
from mewcode.plugins.features import MultiAgentOptions, multi_agent_plugin
from mewcode.plugins.runtime import AgentRuntime, AgentRuntimeRequest
from mewcode.plugins.services import (
    AGENT_CATALOG,
    AGENT_FACTORY,
    CONVERSATION_FACTORY,
    CONTEXT_ROUTER,
    EXPERTS,
    EXPERT_FLOWS,
    EXPERT_FINALIZER_SLOT,
    EXPERT_REDUCER_SLOT,
    EXPERT_ROUTER_SLOT,
    MODEL_CATALOG,
    ORCHESTRATION_GATE_SLOT,
    ORCHESTRATION_NODE_SLOT,
    ORCHESTRATION_TEMPLATE_SLOT,
    PROVIDERS,
    PROVIDER_SLOT,
    COMPLETION,
    PIPELINE,
    PIPELINE_ACTION_SLOT,
    RUNTIME_SCHEDULER,
    SESSION_FACTORY,
    TASK_FACTORY,
    TRACE_FACTORY,
    TASKS,
    TEAMS,
    TRACES,
    WORKTREES,
)

log = logging.getLogger("mewcode.web")


def _context_policy_payload(agent: Agent | None) -> dict[str, Any]:
    policy = getattr(agent, "context_policy", None) if agent is not None else None
    return {
        "context_policy": str(getattr(policy, "name", "") or "legacy"),
        "context_channel": str(
            getattr(agent, "context_channel", "") if agent is not None else ""
        ),
        "context_stable_prefix": bool(
            policy is not None
            and (getattr(policy, "stable_system", False)
                 or getattr(policy, "stable_tools", False))
        ),
        "effort_changes_cache_affinity": bool(
            policy is not None
            and getattr(policy, "effort_affects_cache_affinity", False)
        ),
    }


def _cache_observability_payload(agent: Agent | None) -> dict[str, Any]:
    """Expose provider-neutral current-request and cache-slot diagnostics."""
    if agent is None:
        return {
            "cache_miss": 0,
            "cache_request_read": 0,
            "cache_request_miss": 0,
            "cache_request_creation": 0,
            "cache_request_hit_rate": None,
            "cache_request_usage_available": False,
            "cache_cumulative_read": 0,
            "cache_cumulative_miss": 0,
            "cache_cumulative_creation": 0,
            "cache_cumulative_hit_rate": None,
            "cache_cumulative_usage_available": False,
            "cache_slot": "",
            "cache_provider": "",
            "cache_type": "",
            "cache_epoch": 0,
            "cache_invalidation_reason": "",
            "cache_request_cost_usd": 0.0,
            "cache_cost_usd": 0.0,
            "cache_system_hash": "",
            "cache_tools_hash": "",
            "cache_lcp_tokens": 0,
        }
    observations = getattr(agent, "cache_observations", [])
    latest = observations[-1] if observations else None
    request_available = bool(
        latest
        and getattr(
            latest,
            "cache_usage_available",
            latest.cache_hit_tokens > 0 or latest.cache_miss_tokens > 0,
        )
    )
    totals = getattr(agent, "cache_totals", None)
    legacy_hit = int(getattr(agent, "total_deepseek_cache_hit_tokens", 0) or 0)
    legacy_miss = int(getattr(agent, "total_deepseek_cache_miss_tokens", 0) or 0)
    cumulative_hit = int(getattr(totals, "hit_tokens", legacy_hit) or 0)
    cumulative_miss = int(getattr(totals, "miss_tokens", legacy_miss) or 0)
    cumulative_creation = int(getattr(totals, "creation_tokens", 0) or 0)
    cumulative_prompt = int(
        getattr(
            totals,
            "prompt_tokens",
            cumulative_hit + cumulative_miss + cumulative_creation,
        )
        or 0
    )
    cumulative_available = bool(
        getattr(totals, "available", False)
        or (
            totals is None
            and getattr(agent, "cache_usage_available", False)
        )
    )
    slot = getattr(getattr(agent, "cache_usage_provider", None), "slot", None)
    return {
        "cache_miss": cumulative_miss,
        "cache_request_read": latest.cache_hit_tokens if latest else 0,
        "cache_request_miss": latest.cache_miss_tokens if latest else 0,
        "cache_request_creation": (
            getattr(latest, "cache_creation_tokens", 0) if latest else 0
        ),
        "cache_request_hit_rate": (
            latest.cache_hit_rate if request_available else None
        ),
        "cache_request_usage_available": request_available,
        "cache_cumulative_read": cumulative_hit,
        "cache_cumulative_miss": cumulative_miss,
        "cache_cumulative_creation": cumulative_creation,
        "cache_cumulative_hit_rate": (
            cumulative_hit / cumulative_prompt if cumulative_prompt else 0.0
        ) if cumulative_available else None,
        "cache_cumulative_usage_available": cumulative_available,
        "cache_slot": str(getattr(slot, "name", "") or ""),
        "cache_provider": str(getattr(slot, "provider", "") or ""),
        "cache_type": str(getattr(slot, "cache_type", "") or ""),
        "cache_epoch": int(getattr(agent, "cache_epoch", 0) or 0),
        "cache_invalidation_reason": (
            getattr(latest, "invalidation_reason", "") if latest else ""
        ),
        "cache_request_cost_usd": (
            float(getattr(latest, "request_cost_usd", 0.0) or 0.0)
            if latest else 0.0
        ),
        "cache_cost_usd": float(
            getattr(agent, "total_estimated_cost_usd", 0.0) or 0.0
        ),
        "cache_system_hash": getattr(latest, "system_hash", "") if latest else "",
        "cache_tools_hash": getattr(latest, "tools_hash", "") if latest else "",
        "cache_lcp_tokens": (
            int(getattr(latest, "longest_common_prefix_tokens", 0) or 0)
            if latest else 0
        ),
    }


def _resolve_static_dir(frozen_root: str | Path | None = None) -> Path:
    """Return the shared renderer assets in source and PyInstaller layouts."""
    if frozen_root is None:
        frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / "frontend" / "static"
    return Path(__file__).resolve().parent / "static"


STATIC_DIR = _resolve_static_dir()
USER_DATA_ROOT = user_data_root()
STATE_FILE = Path(".mewcode") / "web_state.json"
# 已知项目目录注册表（跨项目侧栏分组用），存在用户级目录
PROJECTS_FILE = USER_DATA_ROOT / "web_projects.json"
SIDEBAR_STATE_FILE = USER_DATA_ROOT / "sidebar-conversation-state.json"
REVIEW_MODEL_FILE = USER_DATA_ROOT / "review-model.json"
# 用量台账（用户级，跨项目）：每轮聊天结束追加一条 token 用量事件，
# 供右上角用量统计面板按天聚合展示
USAGE_FILE = USER_DATA_ROOT / "usage.jsonl"
BACKGROUND_TASKS_FILE = USER_DATA_ROOT / "web_background_tasks.json"
_SAFE_ID_RE = re.compile(r"^[\w-]+$")
DESKTOP_TOKEN = os.environ.get("MEWCODE_DESKTOP_TOKEN", "")
from frontend.browser_launch import BrowserLaunches
BROWSER_LAUNCHES = BrowserLaunches()
REMOTE_PAIRING = RemotePairingStore(USER_DATA_ROOT / "remote-pairing.json")
DESKTOP_COOKIE = "mewcode_desktop_session"
_ORCHESTRATION_ACTIVE_REQUESTS: dict[str, asyncio.Task[Any]] = {}
_ORCHESTRATION_OPERATION_PROGRESS: dict[str, dict[str, Any]] = {}
_ORCHESTRATION_OPERATION_RESULTS: dict[str, dict[str, Any]] = {}
_ORCHESTRATION_OPERATION_FAILURES: dict[str, dict[str, Any]] = {}
_ORCHESTRATION_PROGRESS_RETENTION_SECONDS = 600.0


def _desktop_verification_agent_enabled() -> bool:
    """Honor the normal config switch when building desktop agent catalogs."""
    try:
        return bool(load_config().enable_verification_agent)
    except Exception:
        return False


DESKTOP_VERIFICATION_AGENT_ENABLED = _desktop_verification_agent_enabled()

# Application-wide objects are created by plugins and only projected into
# module names for the existing FastAPI handlers.
APP_HOST = ApplicationHost(
    os.getcwd(),
    discover_plugins=False,
    enable_verification_agent=DESKTOP_VERIFICATION_AGENT_ENABLED,
).start()
APP_CONTEXT = APP_HOST.context
MODEL_REGISTRY = APP_CONTEXT.get(PROVIDERS)
bootstrap_desktop_providers(MODEL_REGISTRY, USER_DATA_ROOT, desktop_authenticated=bool(DESKTOP_TOKEN))
from frontend.fast_mode import FastModeSettings
FAST_MODE_SETTINGS = FastModeSettings(USER_DATA_ROOT / 'fast-mode.json', MODEL_REGISTRY)
MODEL_CAPABILITY_CATALOG = APP_CONTEXT.get(MODEL_CATALOG)
MODEL_CONTEXT_ROUTER = APP_CONTEXT.get(CONTEXT_ROUTER)
APP_RUNTIME_SCHEDULER = APP_CONTEXT.get(RUNTIME_SCHEDULER)


def _usage_provider_name(provider_key: str) -> str:
    record = MODEL_REGISTRY.get(provider_key)
    return record.provider.name if record is not None else provider_key


def _apply_web_runtime_state(ctx: Context) -> None:
    usage_runtime = UsageRuntime(
        USER_DATA_ROOT,
        provider_name_resolver=_usage_provider_name,
    )
    ctx.provide("web.usage_runtime", usage_runtime)
    ctx.provide(
        "web.legacy_usage",
        UsageService(USER_DATA_ROOT, ledger_path=usage_runtime.ledger.path),
    )
    ctx.provide(
        "web.model_intelligence",
        ModelIntelligenceService(
            cache_path=USER_DATA_ROOT / "model-intelligence.json"
        ),
    )
    ctx.provide(
        "web.expert_profiles",
        ExpertProfileStore(USER_DATA_ROOT / "expert-profiles.json"),
    )
    ctx.provide(
        "web.expert_runs",
        ExpertRunStore(expert_runs_root(USER_DATA_ROOT), storage='sqlite'),
    )
    ctx.provide(
        "web.expert_flows",
        ExpertFlowStore(USER_DATA_ROOT / "expert-flows.json"),
    )
    ctx.provide(
        "web.attachments",
        AttachmentStore(USER_DATA_ROOT / "chat-attachments"),
    )


APP_CONTEXT.register_plugin(PluginSpec(
    "web.runtime_state",
    _apply_web_runtime_state,
    requires=("core.providers",),
    purpose="Usage, expert, intelligence, and attachment stores",
))
APP_CONTEXT.load("web.runtime_state")
USAGE_RUNTIME = APP_CONTEXT.get("web.usage_runtime")
LEGACY_USAGE_SERVICE = APP_CONTEXT.get("web.legacy_usage")
MODEL_INTELLIGENCE = APP_CONTEXT.get("web.model_intelligence")
EXPERT_PROFILES = APP_CONTEXT.get("web.expert_profiles")
EXPERT_RUNS = APP_CONTEXT.get("web.expert_runs")
EXPERT_FLOWS_STORE = APP_CONTEXT.get("web.expert_flows")
EXPERT_LESSONS = FlowLessonStore(USER_DATA_ROOT / "flow-lessons.json")
ATTACHMENTS = APP_CONTEXT.get("web.attachments")


def _session_usage_service(work_dir: str | Path) -> UsageService:
    """Use the desktop ledger only for an authenticated Electron sidecar.

    Source web/CLI sessions keep their historical workspace-local ledger;
    Electron supplies ``MEWCODE_DESKTOP_TOKEN`` and consolidates telemetry in
    its userData directory.
    """

    return UsageService(
        work_dir,
        ledger_path=USAGE_RUNTIME.ledger.path if DESKTOP_TOKEN else None,
    )

# 前端展示三种执行权限级别及只读计划模式。
# ``custom`` / ``dontAsk`` 是兼容无交互任务的内部模式，不能在普通 Web
# 会话中把它们当作“严格 / 宽松”公开出来。
# 注意：危险命令黑名单拥有最高优先级，任何模式下都会被直接拒绝，
# 不进入档位判断流程（见 mewcode/permissions/checker.py Layer 0）。
PERMISSION_MODE_CHOICES = [
    {"value": "default", "label": "授权运行", "desc": "读取自动执行；编辑、命令按授权运行，需要时确认"},
    {"value": "plan", "label": "计划（Plan）", "desc": "只读分析和制定计划；仅允许写入当前计划文件"},
    {"value": "auto", "label": "Auto模式", "desc": "常规操作自动执行；外部文件修改和不确定操作自动审核"},
    {"value": "bypassPermissions", "label": "完全放行【危险操作停止】", "desc": "普通文件（含工作区外）和命令直接执行；危险操作停止"},
]

AUTO_REVIEW_MARKER = "[MEWCODE_AUTO_SUBAGENT_REVIEW]"
AUTO_REVIEW_REMINDER = """\
Auto 模式已启用独立子 Agent 审查。若本轮产生代码、配置或其他工作区文件改动，
在宣告完成前必须使用 Agent 工具启动一个新的 Verification 子 Agent，让它独立运行构建、
测试及针对性回归检查；不得用主 Agent 自检替代。Verification 在后台完成后会自动把结果
送回本对话：VERDICT: PASS 时汇总后完成；VERDICT: FAIL/PARTIAL 时先修复，再启动一次新的
Verification 复审。纯问答、只读调查或没有实际文件改动的轮次无需启动审查。"""

_LEGACY_WEB_PERMISSION_MODE_MIGRATIONS = {
    "custom": PermissionMode.DEFAULT.value,
    "dontAsk": PermissionMode.DEFAULT.value,
    "acceptEdits": PermissionMode.DEFAULT.value,
    "strictIsolation": PermissionMode.DEFAULT.value,
}


def _orchestration_permission_mode(payload: dict[str, Any]) -> str:
    raw = payload.get("permission_mode")
    if raw is None:
        mode = getattr(SESSION, "permission_mode", PermissionMode.DEFAULT)
        raw = (
            getattr(SESSION, "permission_before_plan", PermissionMode.DEFAULT.value)
            if mode == PermissionMode.PLAN else getattr(mode, "value", mode)
        )
    if not isinstance(raw, str):
        raw = PermissionMode.DEFAULT.value
    raw = raw.strip()
    if raw.lower() == PermissionMode.PLAN.value:
        return public_permission_mode(getattr(SESSION, "permission_before_plan", None)).value
    by_lower = {item.value.lower(): item.value for item in EXECUTION_PERMISSION_MODES}
    return by_lower.get(raw.lower(), PermissionMode.DEFAULT.value)


def _design_permission_mode(mode, before_plan="", before_orch="") -> PermissionMode:
    """Remove the legacy Plan overlay, preserving the conversation's chosen level."""
    return public_permission_mode(before_orch or before_plan) if mode == PermissionMode.PLAN else mode


def _apply_runtime_permission_mode(runtime: Any, mode: str | PermissionMode) -> None:
    pm = mode if isinstance(mode, PermissionMode) else PermissionMode(str(mode))
    agent = getattr(runtime, "agent", None)
    if agent is None:
        return
    setter = getattr(agent, "set_permission_mode", None)
    if callable(setter):
        try:
            setter(pm)
        except TypeError:
            setter(pm.value)
        return
    agent.permission_mode = pm
    checker = getattr(agent, "permission_checker", None)
    if checker is not None:
        checker.mode = pm


def _set_multi_agent_tools(agent: Any, enabled: bool) -> None:
    registry = getattr(agent, "registry", None)
    if registry is None:
        return
    for tool_name in ("Agent", "TaskStop", "TaskRetry", "TeamCreate", "TeamDelete"):
        if enabled:
            registry.enable(tool_name)
        else:
            registry.disable(tool_name)


def _apply_runtime_multi_agent(runtime: Any, enabled: bool) -> None:
    runtime.multi_agent = bool(enabled)
    _set_multi_agent_tools(getattr(runtime, "agent", None), runtime.multi_agent)

# 上下文窗口档位约束（上限）。0 表示自动使用模型窗口；正数才是手动上限。
# 其余档位取自各厂商公布的真实窗口：204,800 对应 MiniMax M2.5/M2.7；
# 256,000 对应 Kimi K2.x / 混元 Hy3 / MiMo v2-omni；272k 是 GPT-5.6 的
# 长输入加价阈值（该系列实际窗口 1.05M），保留为可选上限；400k / 500k 分别
# 对应 gpt-5 系列与 Grok 4.5；1M 对应 DeepSeek V4 / GLM-5.2/5.3 / Kimi K3 /
# Qwen3.x / MiMo v2.5；1,050,000 是 GPT-5.6 / GPT-6 的整窗口，也覆盖 ox-alpha 与
# Muse Spark 的 1,048,576（有效窗口取 min，所以高一档不会越界）。
# 已保存的旧档位值不要从列表里删掉；已有手动上限必须保留。
# 前端 app.js 的 runtime 档位按钮与本列表必须保持一致。
CONTEXT_TIERS = [0, 200_000, 204_800, 256_000, 272_000, 400_000, 500_000, 1_000_000, 1_050_000]


def _effective_context_window(context_tier: int, detected_window: int) -> int:
    """Use the model's full window unless the conversation has a manual cap."""
    if detected_window <= 0:
        return 0
    return min(context_tier, detected_window) if context_tier > 0 else detected_window


def _reconcile_agent_context_window(
    agent: Any, context_tier: int, detected_window: int
) -> int:
    """Keep compaction and status on the same provider-derived window."""
    if detected_window > 0:
        window = _effective_context_window(context_tier, detected_window)
        if agent is not None:
            agent.context_window = window
        return window
    return int(getattr(agent, "context_window", 0) or 0)


def _parse_context_tier(value: object) -> int:
    """Keep malformed wire values distinct from the valid auto tier (zero)."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return -1
    try:
        return int(value)
    except ValueError:
        return -1

from mewcode.orchestration.maintenance import COMMANDS as MAINTENANCE_COMMANDS, command_result as maintenance_command_result, parse_maintenance_command

WEB_SLASH_COMMANDS = [
    *MAINTENANCE_COMMANDS,
    {"name": "goal", "aliases": ["g"], "usage": "/goal <目标>|off", "description": "设定当前对话的持续目标并开始执行"},
    {"name": "plan", "aliases": ["p"], "usage": "/plan [任务]", "description": "进入只读计划模式"},
    {"name": "do", "aliases": ["execute"], "usage": "/do [任务]", "description": "退出计划模式并恢复之前的权限"},
    {"name": "auto", "aliases": [], "usage": "/auto", "description": "切换到自动权限模式"},
    {"name": "manual", "aliases": [], "usage": "/manual", "description": "切换到手动权限模式"},
    {"name": "multi", "aliases": [], "usage": "/multi [on|off]", "description": "开启或关闭多 Agent 模式"},
    {"name": "expert", "aliases": [], "usage": "/expert [on|off]", "description": "开启或关闭多模型编排（专家团）：先提案绑定，再按图执行"},
    {"name": "help", "aliases": ["?"], "usage": "/help", "description": "显示桌面端斜杠命令"},
]

WEB_SLASH_ALIASES = {
    alias: command["name"]
    for command in WEB_SLASH_COMMANDS
    for alias in [command["name"], *command["aliases"]]
}


def _build_auxiliary_client(provider: ProviderConfig) -> LLMClient | None:
    """Resolve the global light tier without making provider switching fragile."""
    try:
        config = load_config()
    except Exception:
        config = None
    client = create_auxiliary_client(provider, config)
    return client


def _fmt_duration(secs: float) -> str:
    secs = max(int(secs), 0)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60:02d}s"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


def _load_web_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_web_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _load_projects() -> list[str]:
    try:
        data = json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
        return [d for d in data.get("projects", []) if isinstance(d, str)]
    except Exception:
        return []


def _save_projects(projects: list[str]) -> None:
    try:
        PROJECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROJECTS_FILE.write_text(
            json.dumps({"projects": projects[-20:]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _register_project(dir_path: str) -> None:
    """把目录登记到已知项目列表（供侧栏跨项目分组）。"""
    projects = _load_projects()
    norm = os.path.abspath(dir_path)
    if norm in [os.path.abspath(d) for d in projects]:
        return
    projects.append(norm)
    _save_projects(projects)


def _unregister_project(dir_path: str) -> None:
    norm = os.path.abspath(dir_path)
    projects = [d for d in _load_projects() if os.path.abspath(d) != norm]
    _save_projects(projects)


# ---------------------------------------------------------------------------
# 用量台账：记录 / 聚合
# ---------------------------------------------------------------------------

def _append_usage_event(
    model: str, input_tokens: int, output_tokens: int,
    session_id: str, messages: int, *, provider_name: str = "",
    protocol: str = "", base_url: str = "",
) -> None:
    """Compatibility fallback for clients without request-level telemetry."""
    try:
        USAGE_RUNTIME.ledger.append(RequestUsage(
            provider_name=provider_name,
            protocol=protocol,
            base_url=base_url,
            configured_model=model,
            response_model=model,
            tokens=TokenUsage(
                input_tokens=max(int(input_tokens), 0),
                output_tokens=max(int(output_tokens), 0),
            ),
            evidence=EvidenceLevel.LOCAL_ESTIMATE,
            attribution=AttributionStatus.CONFIGURED,
            session_id=session_id,
            metadata={"legacy_messages": max(int(messages), 1)},
        ))
    except Exception:
        pass


def _load_usage_events() -> list[dict]:
    try:
        return USAGE_RUNTIME.ledger.activity_events()
    except Exception:
        return []


_FALLBACK_USAGE_CACHE: dict[str, Any] = {"key": None, "expires": 0.0, "events": []}


def _fallback_usage_events(known_sids: set[str]) -> list[dict]:
    """台账启用前的历史会话兜底：用 session meta 合成近似事件。

    每个不在台账里的会话合成一条事件，时间取 last_active，token 取
    meta.total_tokens（近似值，前端标注为估算），模型未知。
    旧版 run_chat 不会把 total_tokens 落盘（赋值后无人保存），大量历史
    会话的 meta 里是 0；对这类有消息但无 token 的会话，退而求其次用
    jsonl 内容体量估算（≈ 字节数/4，只覆盖正文不含系统提示与重复上下文，
    偏保守但好过显示 0）。"""
    dirs: set[str] = {
        os.path.abspath(str(Path.home())),
        os.path.abspath(SESSION.work_dir),
        *(os.path.abspath(d) for d in _load_projects()),
    }
    cache_key = (tuple(sorted(dirs)), tuple(sorted(known_sids)))
    now = time.monotonic()
    if (
        _FALLBACK_USAGE_CACHE["key"] == cache_key
        and now < float(_FALLBACK_USAGE_CACHE["expires"])
    ):
        return copy.deepcopy(_FALLBACK_USAGE_CACHE["events"])
    out: list[dict] = []
    for d in dirs:
        sess_dir = Path(d) / ".mewcode" / "sessions"
        if not sess_dir.is_dir():
            continue
        for meta_path in sess_dir.glob("*.meta"):
            meta = SessionMeta.load(meta_path)
            if meta is None or meta.id in known_sids:
                continue
            if meta.total_tokens <= 0 and meta.message_count <= 0:
                continue
            tokens = max(meta.total_tokens, 0)
            if tokens <= 0:
                try:
                    jsonl = sess_dir / f"{meta.id}.jsonl"
                    tokens = jsonl.stat().st_size // 4
                except OSError:
                    tokens = 0
            out.append({
                "ts": meta.last_active.astimezone(),
                "model": "",
                "in": 0,
                "out": 0,
                "tokens": tokens,
                "sid": meta.id,
                "msgs": max(meta.message_count, 1),
                "approx": True,
            })
    _FALLBACK_USAGE_CACHE.update({
        "key": cache_key, "expires": now + 300.0, "events": copy.deepcopy(out),
    })
    return out


def _usage_stats(days: int) -> dict:
    """聚合用量统计：days<=0 表示全部，否则只看最近 days 天（含今天）。"""
    today = datetime.now().astimezone().date()
    cutoff = today - timedelta(days=days - 1) if days > 0 else None
    since = (
        datetime.combine(cutoff, datetime.min.time()).astimezone()
        if cutoff is not None else None
    )
    try:
        aggregate = USAGE_RUNTIME.ledger.activity_aggregate(since=since)
    except Exception:
        aggregate = {"days": {}, "hours": {}, "models": {}, "sessions": [], "totals": {}}
    known_sids = set(aggregate.get("sessions") or [])
    events = _fallback_usage_events(known_sids)
    if days > 0:
        events = [e for e in events if e["ts"].date() >= cutoff]

    per_day: dict[str, dict] = copy.deepcopy(aggregate.get("days") or {})
    sessions: set[str] = set(known_sids)
    hour_tokens = [0] * 24
    for hour, tokens in (aggregate.get("hours") or {}).items():
        if 0 <= int(hour) < 24:
            hour_tokens[int(hour)] = int(tokens)
    model_tokens: dict[str, int] = dict(aggregate.get("models") or {})
    totals = aggregate.get("totals") or {}
    total_in = int(totals.get("in") or 0)
    total_out = int(totals.get("out") or 0)
    total_tokens = int(totals.get("tokens") or 0)
    total_msgs = int(totals.get("messages") or 0)

    for e in events:
        tokens = e.get("tokens", e["in"] + e["out"])
        day = e["ts"].date().isoformat()
        rec = per_day.setdefault(day, {
            "tokens": 0, "in": 0, "out": 0, "messages": 0, "approx": False,
        })
        rec["tokens"] += tokens
        rec["in"] += e["in"]
        rec["out"] += e["out"]
        rec["messages"] += e["msgs"]
        rec["approx"] = rec["approx"] or e["approx"]
        if e["sid"]:
            sessions.add(e["sid"])
        hour_tokens[e["ts"].hour] += tokens
        if e["model"]:
            model_tokens[e["model"]] = model_tokens.get(e["model"], 0) + tokens
        total_in += e["in"]
        total_out += e["out"]
        total_tokens += tokens
        total_msgs += e["msgs"]

    # 连续活跃天数：最长连击，以及截至今天/昨天的当前连击
    active = sorted(datetime.fromisoformat(d).date() for d in per_day)
    longest = streak = 0
    for i, d in enumerate(active):
        streak = streak + 1 if i > 0 and (d - active[i - 1]).days == 1 else 1
        longest = max(longest, streak)
    current = 0
    if active and (today - active[-1]).days <= 1:
        current = 1
        for i in range(len(active) - 1, 0, -1):
            if (active[i] - active[i - 1]).days == 1:
                current += 1
            else:
                break

    peak_hour = -1
    if any(hour_tokens):
        peak_hour = hour_tokens.index(max(hour_tokens))
    favorite = ""
    if model_tokens:
        favorite = max(model_tokens.items(), key=lambda kv: kv[1])[0]

    return {
        "summary": {
            "sessions": len(sessions),
            "messages": total_msgs,
            "total_tokens": total_tokens,
            "total_in": total_in,
            "total_out": total_out,
            "active_days": len(per_day),
            "current_streak": current,
            "longest_streak": longest,
            "peak_hour": peak_hour,
            "favorite_model": favorite,
        },
        "days": per_day,
        "today": today.isoformat(),
    }


def messages_to_display(
    messages: list[Message],
    turn_changes: dict[int, list[dict[str, str]]] | None = None,
) -> list[dict]:
    """把会话历史转成前端展示条目：tool_use 与 tool_result 按 id 配对。

    两遍扫描：先收集全部 tool_result（它们在消息序列中位于对应的
    assistant tool_use 之后），再构建展示条目。"""
    results: dict[str, tuple[str, bool]] = {}
    for msg in messages:
        if msg.tool_results:
            for tr in msg.tool_results:
                results[tr.tool_use_id] = (tr.content, tr.is_error)

    changes_by_index = turn_changes or {}
    entries: list[dict] = []
    for index, msg in enumerate(messages):
        if msg.tool_results:
            continue
        if msg.role == "user":
            text = msg.content if isinstance(msg.content, str) else ""
            # 系统提醒不进展示流
            if (
                text
                and not text.startswith("<system-reminder>")
                and not text.startswith("<task-notification>")
            ):
                entries.append({
                    "role": "user",
                    "text": text,
                    "attachments": ATTACHMENTS.display_refs(msg.content_blocks),
                })
            elif msg.content_blocks:
                entries.append({
                    "role": "user",
                    "text": "",
                    "attachments": ATTACHMENTS.display_refs(msg.content_blocks),
                })
        elif msg.role == "assistant":
            tools = []
            for tu in msg.tool_uses:
                content, is_error = results.get(tu.tool_use_id, ("", False))
                tools.append({
                    "name": tu.tool_name,
                    "args": tu.arguments,
                    "result": content[:4000],
                    "is_error": is_error,
                })
            thinking = "".join(
                block.thinking for block in (msg.thinking_blocks or []) if block.thinking
            )
            # make_snapshot 使用 len(history)（1-based 末尾），对齐 index+1。
            changes = (
                changes_by_index.get(index + 1)
                or changes_by_index.get(index)
                or []
            )
            entries.append({
                "role": "assistant",
                "text": msg.content if isinstance(msg.content, str) else "",
                "thinking": thinking,
                "tools": tools,
                "changes": changes,
            })
    return entries


MAX_VISIBLE_CHAT_QUEUE = 20
MAX_QUEUED_CHAT_TEXT_BYTES = 200_000
_CHAT_QUEUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@dataclass(frozen=True)
class QueuedChatTurn:
    """One user-visible follow-up waiting behind a running conversation turn."""

    id: str
    text: str
    attachment_ids: tuple[str, ...] = ()
    attachments: tuple[dict[str, Any], ...] = ()
    continuation: bool = False

    def payload(self, position: int) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "attachments": [dict(item) for item in self.attachments],
            "position": position,
        }


@dataclass
class ConversationRuntime:
    """A running turn detached from whichever conversation is visible now.

    The desktop UI may navigate to another conversation while this runtime keeps
    streaming.  Every mutable object used by the turn is captured here so output
    can never be appended to the newly selected conversation.
    """

    owner: "WebSession"
    conversation_id: str
    scope: str
    work_dir: str
    session: Session
    conversation: ConversationManager
    agent: Agent
    client: LLMClient
    slot: ProviderSlot | None
    effort: str
    context_tier: int
    multi_agent: bool
    expert_mode: bool
    goal: str
    task_manager: TaskManager
    trace_manager: TraceManager
    agent_tool: AgentTool | None
    worktree_manager: WorktreeManager | None
    team_manager: TeamManager | None
    # Capture the ledger route too: background graph nodes must not fall back
    # to a project ledger or borrow the newly selected conversation's service.
    usage_service: UsageService | None = None
    cordis_runtime: AgentRuntime | None = None
    task: asyncio.Task | None = None
    # A turn is considered live from the moment its runtime is captured until
    # the worker task is attached.  This closes the await gap while the
    # initial turn_started event is being delivered.
    starting: bool = False
    permission_future: asyncio.Future[PermissionResponse] | None = None
    permission_request: dict[str, Any] = field(default_factory=dict)
    permission_request_epoch: int = 0
    human_oncall_request: HumanOnCallRequest | None = None
    ask_user_request: AskUserRequest | None = None
    expert_strategy_future: asyncio.Future[str] | None = None
    expert_run_id: str = ""
    # Authorization the user expressed in words but that is not yet an explicit
    # grant.  The canvas turns each entry into a one-click grant; nothing here
    # is effective until the user confirms through the grant endpoint.
    pending_file_grants: list[dict[str, Any]] = field(default_factory=list)
    file_access_grants: dict[str, bool] = field(default_factory=dict)
    # Directory grants, keyed by normcased absolute path, value = writable.
    # A folder cannot be expressed as a file grant, so exploring a directory
    # outside the workspace needs its own explicit, user-confirmed entry.
    dir_access_grants: dict[str, bool] = field(default_factory=dict)
    expert_control_action: str = "pause"
    expert_takeover_controller: Any = None
    execution_kind: str = "chat"
    continuation_controller: Any = None
    continuation_intervention: bool = False
    continuation_side_turn: bool = False
    continuation_requests: dict[str, str] = field(default_factory=dict)
    # Interrupts can arrive in the small window after the runtime is reserved
    # but before its worker task is attached.  Remember the request so the
    # freshly-created task cannot become an un-stoppable zombie turn.
    cancel_requested: bool = False
    permission_before_plan: str = PermissionMode.DEFAULT.value
    permission_before_orch: str = ""
    orchestration_planning: bool = False
    orchestration_run_inputs: dict[str, object] = field(default_factory=dict)
    # Internal command continuations remain plain strings for compatibility.
    # User-authored waiting-area entries carry identity and attachment metadata.
    queued_prompts: list[str | QueuedChatTurn] = field(default_factory=list)
    queued_request_fingerprints: dict[str, tuple[str, tuple[str, ...]]] = field(
        default_factory=dict
    )
    last_send_now_id: str = ""
    chat_queue_revision: int = 0
    turn_history_start: int = 0

    def running(self) -> bool:
        return (self.starting or (self.task is not None and not self.task.done())
                or any(not task.done() for task in getattr(self, '_goal_run_tasks', {}).values())
                or any(not task.done() for task in getattr(self, '_case_resume_tasks', {}).values()))

    def chat_queue_payload(self) -> list[dict[str, Any]]:
        visible = [item for item in self.queued_prompts if isinstance(item, QueuedChatTurn)]
        return [item.payload(index) for index, item in enumerate(visible, 1)]

    def tasks_payload(self) -> list[dict]:
        return self.owner.tasks_payload()

    def status_payload(self) -> dict:
        provider = self.slot.provider if self.slot else None
        detected = provider.get_context_window() if provider is not None else 0
        window = _reconcile_agent_context_window(
            self.agent, self.context_tier, detected
        )
        cache_read = self.agent.total_cache_read_tokens
        cache_creation = self.agent.total_cache_creation_tokens
        cache_available = (
            getattr(self.agent, "cache_usage_available", False)
            or cache_read > 0
            or cache_creation > 0
        )
        prompt_tokens = self.agent.total_input_tokens + cache_read + cache_creation
        effective_effort = self.effort or getattr(self.client, "effort", "") or "auto"
        return {
            "model": provider.model if provider else "",
            "provider": provider.name if provider else "",
            "provider_key": self.slot.key if self.slot else "",
            "provider_error": getattr(self, "provider_restore_error", ""),
            "window": window,
            "context_tier": self.context_tier,
            "detected_window": detected,
            "effort": effective_effort,
            **FAST_MODE_SETTINGS.status(self.slot, getattr(self, 'client', None)),
            "total_in": self.agent.total_input_tokens,
            "total_out": self.agent.total_output_tokens,
            "cache_read": cache_read,
            "cache_creation": cache_creation,
            "cache_usage_available": cache_available,
            "cache_hit_rate": (
                cache_read / prompt_tokens
                if cache_available and prompt_tokens
                else (0.0 if cache_available else None)
            ),
            **_context_policy_payload(self.agent),
            **_cache_observability_payload(self.agent),
            "prompt_tokens": prompt_tokens,
            "session": _fmt_duration(time.monotonic() - self.owner.session_start),
            "conversation_id": self.conversation_id,
            "conversation_title": self.session.meta.title or "新对话",
            "project_name": Path(self.work_dir).name or self.work_dir,
            "scope": self.scope,
            "permission_mode": self.agent.permission_mode.value,
            "permission_level": (
                public_permission_mode(self.permission_before_plan).value
                if self.agent.permission_mode == PermissionMode.PLAN
                else public_permission_mode(self.agent.permission_mode).value
            ),
            "permission_before_plan": self.permission_before_plan,
            "planning": self.agent.permission_mode == PermissionMode.PLAN,
            "work_dir": self.work_dir,
            "multi_agent": self.multi_agent,
            "expert_mode": self.expert_mode,
            "execution_kind": self.execution_kind,
            "continuation": continuation_status(self),
            "orchestration_planning": getattr(self, "orchestration_planning", False)
            or getattr(self.owner, "orchestration_planning", False),
            "goal": self.goal,
            "goal_mode": bool(self.goal),
        }

    # expert_web expects these legacy WebSession attribute names.
    @property
    def context(self):
        """Proxy the host WebSession cordis/app context for expert_web."""
        return self.owner.context

    @property
    def _perm_future(self) -> asyncio.Future[PermissionResponse] | None:
        return self.permission_future

    @_perm_future.setter
    def _perm_future(self, value: asyncio.Future[PermissionResponse] | None) -> None:
        self.permission_future = value

    @property
    def _human_oncall_request(self) -> HumanOnCallRequest | None:
        return self.human_oncall_request

    @_human_oncall_request.setter
    def _human_oncall_request(self, value: HumanOnCallRequest | None) -> None:
        self.human_oncall_request = value

    @property
    def _ask_user_request(self) -> AskUserRequest | None:
        return self.ask_user_request

    @_ask_user_request.setter
    def _ask_user_request(self, value: AskUserRequest | None) -> None:
        self.ask_user_request = value

    @property
    def _expert_strategy_future(self) -> asyncio.Future[str] | None:
        return self.expert_strategy_future

    @_expert_strategy_future.setter
    def _expert_strategy_future(self, value: asyncio.Future[str] | None) -> None:
        self.expert_strategy_future = value


class ConversationWebSocket:
    """Tag every stream event so the renderer can ignore inactive chats."""

    def __init__(self, websocket: Any, conversation_id: str) -> None:
        self._websocket = websocket
        self.conversation_id = conversation_id

    async def send_json(self, payload: dict[str, Any]) -> None:
        tagged = dict(payload)
        tagged.setdefault("conversation_id", self.conversation_id)
        await self._websocket.send_json(tagged)


class WebSocketEventHub:
    """Keeps detached conversation streams alive across renderer reconnects."""

    def __init__(self, *, send_timeout: float = 0.5) -> None:
        self._connections: set[WebSocket] = set()
        self._send_locks: dict[WebSocket, asyncio.Lock] = {}
        self._send_timeout = max(float(send_timeout), 0.01)

    def connect(self, websocket: WebSocket) -> None:
        self._connections.add(websocket)
        self._send_locks.setdefault(websocket, asyncio.Lock())

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)
        self._send_locks.pop(websocket, None)

    async def send_json(self, payload: dict[str, Any]) -> None:
        if isinstance(payload, dict):
            record_remote_live_event(str(payload.get("conversation_id") or ""), payload)
            notifier = globals().get('WORKBENCH_NOTIFIER')
            if notifier is not None:
                notifier.observe(payload)
        connections = list(self._connections)

        async def send_one(websocket: WebSocket) -> WebSocket | None:
            lock = self._send_locks.setdefault(websocket, asyncio.Lock())
            try:
                async with asyncio.timeout(self._send_timeout):
                    async with lock:
                        if websocket not in self._connections:
                            return None
                        await websocket.send_json(payload)
            except Exception:
                return websocket
            return None

        stale = await asyncio.gather(
            *(send_one(websocket) for websocket in connections)
        )
        for websocket in stale:
            if websocket is not None:
                self.disconnect(websocket)


WS_EVENTS = WebSocketEventHub()
EXTERNAL_AGENTS = ExternalAgentStore()


class RemoteOperationSink:
    """Collect a remote prompt's bounded event history for HTTP clients."""

    def __init__(self, operation_id: str, conversation_id: str) -> None:
        self.operation_id = operation_id
        self.conversation_id = conversation_id
        self.events: list[dict[str, Any]] = []
        self.status = "running"
        self.started_at = datetime.now().astimezone().isoformat()

    async def send_json(self, payload: dict[str, Any]) -> None:
        event = dict(payload)
        event.setdefault("conversation_id", self.conversation_id)
        self.events.append(event)
        if len(self.events) > 500:
            del self.events[:-500]
        if event.get("type") == "done":
            self.status = "interrupted" if event.get("interrupted") else "completed"
        elif event.get("type") == "error":
            self.status = "error"


class FanoutJsonSink:
    """Copy one chat stream to the phone poll buffer and the desktop websocket."""

    def __init__(self, *sinks: Any) -> None:
        self._sinks = sinks

    async def send_json(self, payload: dict[str, Any]) -> None:
        for sink in self._sinks:
            try:
                await sink.send_json(payload)
            except Exception:
                log.warning("remote collab fanout sink failed", exc_info=True)


REMOTE_OPERATIONS: dict[str, RemoteOperationSink] = {}
REMOTE_LIVE_LIMIT = 500
_REMOTE_LIVE_SKIP_TYPES = frozenset({"remote_conversation_focus", "state"})
REMOTE_LIVES: dict[str, list[dict[str, Any]]] = {}


def record_remote_live_event(conversation_id: str, payload: dict[str, Any]) -> None:
    cid = str(conversation_id or "").strip()
    if not cid or not isinstance(payload, dict):
        return
    if str(payload.get("type") or "") in _REMOTE_LIVE_SKIP_TYPES:
        return
    event = dict(payload)
    event.setdefault("conversation_id", cid)
    bucket = REMOTE_LIVES.setdefault(cid, [])
    bucket.append(event)
    overflow = len(bucket) - REMOTE_LIVE_LIMIT
    if overflow > 0:
        del bucket[:overflow]


def pending_runtime_prompts(session: "WebSession") -> list[dict[str, Any]]:
    """Re-emit unresolved interactive prompts after a renderer reconnect."""
    prompts: list[dict[str, Any]] = []
    for runtime in session.conversation_runs.values():
        if not runtime.running():
            continue
        if runtime.permission_future is not None and not runtime.permission_future.done():
            prompts.append({
                "type": "permission", "conversation_id": runtime.conversation_id,
                **runtime.permission_request,
            })
        request = runtime.human_oncall_request
        if request is not None and not request.future.done():
            prompts.append({
                "type": "human_oncall", "conversation_id": runtime.conversation_id,
                "request_id": request.request_id, "tool": request.tool_name,
                "questions": request.questions,
                'task_id': (getattr(request, 'task_design_binding', None) or {}).get('task_id', ''),
                'gate': getattr(request, 'host_gate', {}),
                **({"choice_bundle": request.choice_bundle} if getattr(request, "choice_bundle", None) else {}),
                **({"run_id": request.choice_run_id} if getattr(request, "choice_run_id", "") else {}),
            })
        ask = getattr(runtime, "ask_user_request", None)
        if ask is not None and not ask.future.done():
            prompts.append({
                "type": "ask_user", "conversation_id": runtime.conversation_id,
                "request_id": ask.request_id, "tool": ask.tool_name,
                "questions": ask.questions,
                'task_id': (getattr(ask, 'task_design_binding', None) or {}).get('task_id', ''),
                'gate': getattr(ask, 'host_gate', {}),
                **({"choice_bundle": ask.choice_bundle} if getattr(ask, "choice_bundle", None) else {}),
                **({"run_id": ask.choice_run_id} if getattr(ask, "choice_run_id", "") else {}),
            })
    return prompts


def _resume_attachment_blocks(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    ids = [
        str(item.get("attachment_id") or "")
        for item in snapshot.get("attachments") or []
        if isinstance(item, dict) and item.get("attachment_id")
    ]
    if not ids:
        return []
    try:
        refs = ATTACHMENTS.references(str(snapshot.get("conversation_id") or ""), ids)
        return ATTACHMENTS.materialize(refs)
    except AttachmentError:
        return []


class WebSession:
    """本地单用户会话状态：Provider 插槽、client、agent、对话、历史。"""

    def __init__(self) -> None:
        self.web_state = _load_web_state()
        launch_dir = os.path.abspath(os.getcwd())
        saved_dir = self.web_state.get("active_project", "")
        self.work_dir = (
            os.path.abspath(saved_dir)
            if isinstance(saved_dir, str) and os.path.isdir(saved_dir)
            else launch_dir
        )
        self.plugin_host = ApplicationHost(
            self.work_dir,
            context=APP_CONTEXT.scope(f"web-session-{id(self):x}"),
            enable_verification_agent=DESKTOP_VERIFICATION_AGENT_ENABLED,
        ).start()
        self.context = self.plugin_host.context
        self.plugin_host.bind("sessions", self)
        saved_permission_mode = self.web_state.get("permission_mode")
        if isinstance(saved_permission_mode, str):
            saved_permission_mode = _LEGACY_WEB_PERMISSION_MODE_MIGRATIONS.get(
                saved_permission_mode, saved_permission_mode
            )
        else:
            saved_permission_mode = PermissionMode.DEFAULT.value
        try:
            self.permission_mode = public_permission_mode(saved_permission_mode, allow_plan=True)
        except (TypeError, ValueError):
            self.permission_mode = PermissionMode.DEFAULT
        if (
            "permission_mode" in self.web_state
            and self.web_state.get("permission_mode") != self.permission_mode.value
        ):
            self.web_state["permission_mode"] = self.permission_mode.value
            _save_web_state(self.web_state)
        self.slots: list[ProviderSlot] = []
        self._context_slot_disposers = []
        self.slot: ProviderSlot | None = None
        self.provider_restore_error = ""
        self.client: LLMClient | None = None
        self.agent: Agent | None = None
        self.agent_runtime: AgentRuntime | None = None
        self.conversation = self.context.get(CONVERSATION_FACTORY).create()
        saved_effort = self.web_state.get("effort", "")
        self.effort = saved_effort if isinstance(saved_effort, str) else ""
        self.session_start = time.monotonic()
        self.chat_task: asyncio.Task | None = None
        self.conversation_runs: dict[str, ConversationRuntime] = {}
        self._chat_queue_revisions: dict[str, int] = {}
        saved_settings = self.web_state.get("conversation_settings", {})
        self.conversation_settings: dict[str, dict[str, Any]] = (
            {str(key): dict(value) for key, value in saved_settings.items() if isinstance(value, dict)}
            if isinstance(saved_settings, dict) else {}
        )
        self._perm_future: asyncio.Future[PermissionResponse] | None = None
        self._human_oncall_request: HumanOnCallRequest | None = None
        self._ask_user_request: AskUserRequest | None = None
        self._expert_strategy_future: asyncio.Future[str] | None = None
        # 子 Agent / 后台任务
        self.task_manager = self.context.get(TASK_FACTORY).create(BACKGROUND_TASKS_FILE)
        self.task_manager.runtime_scheduler = APP_RUNTIME_SCHEDULER
        self._agent_tool: AgentTool | None = None
        self.trace_manager = self.context.get(TRACE_FACTORY).create()
        self.worktree_manager: WorktreeManager | None = None
        self.team_manager: TeamManager | None = None
        # 会话持久化：项目级（当前目录）与用户级（home）分离
        session_factory = self.context.get(SESSION_FACTORY)
        self.project_sessions = session_factory.create(self.work_dir)
        self.user_sessions = session_factory.create(str(Path.home()))
        # Web requests use the same durable per-request SQLite ledger as the
        # CLI. Previously the browser created clients without binding this
        # recorder, so cached/read/write counters disappeared after each run.
        self.usage_service = _session_usage_service(self.work_dir)
        self.session: Session | None = None
        self.scope: str = "project"
        self.file_history: FileHistory | None = None
        self._file_history_key: tuple[str, str] | None = None
        # Unset/unknown tiers use the model's actual window.  Saved manual
        # caps remain untouched, including older 200k/272k/1M choices.
        tier = self.web_state.get("context_tier", 0)
        self.context_tier: int = tier if tier in CONTEXT_TIERS else 0
        # Preserve explicit user choice. Only a missing setting inherits the
        # historical Auto-mode default.
        if "multi_agent" in self.web_state:
            self.multi_agent = bool(self.web_state.get("multi_agent"))
        else:
            self.multi_agent = self.permission_mode == PermissionMode.AUTO
        if self.web_state.get("multi_agent") != self.multi_agent:
            self.web_state["multi_agent"] = self.multi_agent
            _save_web_state(self.web_state)
        self.expert_mode: bool = bool(self.web_state.get("expert_mode", False))
        self.orchestration_planning: bool = bool(
            self.web_state.get("orchestration_planning", False)
        )
        self.permission_before_orch = str(
            self.web_state.get("permission_before_orch") or ""
        )
        saved_goals = self.web_state.get("conversation_goals", {})
        self.conversation_goals: dict[str, str] = (
            {
                str(key): str(value)
                for key, value in saved_goals.items()
                if isinstance(key, str) and isinstance(value, str) and value.strip()
            }
            if isinstance(saved_goals, dict)
            else {}
        )
        saved_before_plan = self.web_state.get("permission_before_plan", "default")
        self.permission_before_plan = public_permission_mode(saved_before_plan).value
        if self.permission_before_orch:
            self.permission_before_orch = public_permission_mode(self.permission_before_orch).value
        self._mcp_init_task: asyncio.Task[None] | None = None
        self.mcp_errors: list[str] = []

    # ---------- 会话历史 ----------

    def _bind_usage_client(self, client: LLMClient | None) -> None:
        service = getattr(self, "usage_service", None)
        if service is not None and callable(
            getattr(client, "set_usage_recorder", None)
        ):
            service.bind_client(client)

    def _sync_context_faces(self) -> None:
        """Expose runtime objects through disposable binding plugins."""
        self.plugin_host.bind("sessions", self)
        self.plugin_host.bind("llm", self.client)
        self.plugin_host.bind("agents", self.agent)
        if self.agent is not None:
            self.plugin_host.bind("tools", self.agent.registry)

    def chat_busy(self, session_id: str = "") -> bool:
        """Return whether the selected (or explicitly named) chat is running."""
        if _NATIVE_CONTROL_BUSY.is_set():
            return True
        session = getattr(self, "session", None)
        target = session_id or (session.session_id if session else "")
        runtime = getattr(self, "conversation_runs", {}).get(target)
        return runtime is not None and runtime.running()

    def any_chat_busy(self) -> bool:
        return _NATIVE_CONTROL_BUSY.is_set() or any(runtime.running() for runtime in self.conversation_runs.values())

    def running_conversation_ids(self) -> set[str]:
        return {
            conversation_id
            for conversation_id, runtime in self.conversation_runs.items()
            if runtime.running()
        }

    def capture_conversation_runtime(self) -> ConversationRuntime:
        if (
            self.session is None
            or self.agent is None
            or self.client is None
            or self.agent_runtime is None
        ):
            raise RuntimeError("当前对话尚未准备好")
        conversation_id = self.session.session_id
        existing = self.conversation_runs.get(conversation_id)
        if existing is not None and existing.running():
            # Keep the UI preference current; execution authority is supplied
            # separately by confirmation or an explicit checkpoint selection.
            existing.expert_mode = bool(self.expert_mode)
            existing.orchestration_planning = bool(
                getattr(self, "orchestration_planning", False)
            )
            session_mode = getattr(self, "permission_mode", None)
            if session_mode is not None:
                _apply_runtime_permission_mode(existing, session_mode)
            return existing
        runtime = ConversationRuntime(
            owner=self,
            conversation_id=conversation_id,
            scope=self.scope,
            work_dir=os.path.abspath(self.work_dir),
            session=self.session,
            conversation=self.conversation,
            agent=self.agent,
            client=self.client,
            slot=copy.deepcopy(self.slot),
            effort=self._effective_effort(),
            context_tier=self.context_tier,
            multi_agent=self.multi_agent,
            expert_mode=self.expert_mode,
            orchestration_planning=bool(getattr(self, "orchestration_planning", False)),
            goal=self.active_goal(),
            task_manager=self.task_manager,
            trace_manager=self.trace_manager,
            agent_tool=self._agent_tool,
            worktree_manager=self.worktree_manager,
            team_manager=self.team_manager,
            usage_service=self.usage_service,
            cordis_runtime=self.agent_runtime,
            permission_before_plan=self.permission_before_plan,
            permission_before_orch=self.permission_before_orch,
            turn_history_start=len(self.conversation.history),
        )
        self.conversation_runs[conversation_id] = runtime
        return runtime

    @staticmethod
    def begin_conversation_turn(runtime: ConversationRuntime) -> None:
        """Reserve a captured runtime before the first asynchronous send."""
        if _NATIVE_CONTROL_BUSY.is_set():
            raise ValueError("宿主正在配置或恢复原生事务，请等待核验完成")
        runtime._turn_start_token = object()
        runtime.starting = True

    def conversation_queues_payload(self) -> dict[str, list[dict[str, Any]]]:
        return {
            conversation_id: runtime.chat_queue_payload()
            for conversation_id, runtime in self.conversation_runs.items()
            if runtime.chat_queue_payload()
        }

    def conversation_queue_revisions_payload(self) -> dict[str, int]:
        return dict(getattr(self, "_chat_queue_revisions", {}))

    def _current_chat_queue_revision(self, runtime: ConversationRuntime) -> int:
        revisions = getattr(self, "_chat_queue_revisions", None)
        if not isinstance(revisions, dict):
            revisions = {}
            self._chat_queue_revisions = revisions
        conversation_id = str(getattr(runtime, "conversation_id", "") or "")
        if not conversation_id and self.session is not None:
            conversation_id = str(getattr(self.session, "session_id", "") or "")
        current = max(
            int(getattr(runtime, "chat_queue_revision", 0) or 0),
            int(revisions.get(conversation_id, 0) or 0),
        )
        runtime.chat_queue_revision = current
        return current

    def _bump_chat_queue_revision(self, runtime: ConversationRuntime) -> int:
        revision = self._current_chat_queue_revision(runtime) + 1
        runtime.chat_queue_revision = revision
        conversation_id = str(getattr(runtime, "conversation_id", "") or "")
        if not conversation_id and self.session is not None:
            conversation_id = str(getattr(self.session, "session_id", "") or "")
        if conversation_id:
            self._chat_queue_revisions[conversation_id] = revision
        return revision

    def _active_queue_runtime(self, conversation_id: str, *, allow_continuation: bool = False,
                              queue_control: bool = False) -> ConversationRuntime:
        target = str(conversation_id or "").strip()
        visible = self.session.session_id if self.session is not None else ""
        if not target or target != visible:
            raise LookupError("目标对话不是当前活动对话")
        runtime = self.conversation_runs.get(target)
        inserting = bool(runtime and allow_continuation and runtime.continuation_intervention
                         and not continuation_state(runtime).get("held"))
        queued = bool(runtime and queue_control and (runtime.queued_prompts or runtime.last_send_now_id))
        if (runtime is None or (not runtime.running() and not queued)
                or (runtime.cancel_requested and not inserting and not queue_control)):
            raise RuntimeError("当前回复已经结束或正在停止")
        return runtime

    def enqueue_chat_turn(
        self,
        conversation_id: str,
        queue_id: str,
        text: str,
        attachment_blocks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        runtime = self._active_queue_runtime(conversation_id, queue_control=True)
        identity = str(queue_id or "")
        if not _CHAT_QUEUE_ID.fullmatch(identity):
            raise ValueError("无效的等待项 ID")
        if not isinstance(text, str):
            raise ValueError("等待内容必须是文本")
        cleaned = text.strip()
        if not cleaned and not attachment_blocks:
            raise ValueError("等待内容不能为空")
        if len(cleaned.encode("utf-8")) > MAX_QUEUED_CHAT_TEXT_BYTES:
            raise ValueError("等待内容过长")
        attachment_ids = tuple(
            str(block.get("attachment_id") or "") for block in attachment_blocks
        )
        fingerprint = (cleaned, attachment_ids)
        previous = runtime.queued_request_fingerprints.get(identity)
        if previous is not None:
            if previous != fingerprint:
                raise ValueError("等待项 ID 已用于不同内容")
            return {
                "conversation_id": runtime.conversation_id,
                "items": runtime.chat_queue_payload(),
                "revision": self._current_chat_queue_revision(runtime),
                "accepted_id": identity,
                "replayed": True,
            }
        if len(runtime.chat_queue_payload()) >= MAX_VISIBLE_CHAT_QUEUE:
            raise OverflowError(f"等待区最多保留 {MAX_VISIBLE_CHAT_QUEUE} 条消息")
        visible_attachments = tuple(ATTACHMENTS.display_refs(attachment_blocks))
        runtime.queued_prompts.append(QueuedChatTurn(
            id=identity,
            text=cleaned,
            attachment_ids=attachment_ids,
            attachments=visible_attachments,
        ))
        runtime.queued_request_fingerprints[identity] = fingerprint
        revision = self._bump_chat_queue_revision(runtime)
        while len(runtime.queued_request_fingerprints) > MAX_VISIBLE_CHAT_QUEUE * 3:
            runtime.queued_request_fingerprints.pop(
                next(iter(runtime.queued_request_fingerprints))
            )
        return {
            "conversation_id": runtime.conversation_id,
            "items": runtime.chat_queue_payload(),
            "revision": revision,
            "accepted_id": identity,
            "replayed": False,
        }

    def remove_queued_chat_turn(
        self, conversation_id: str, queue_id: str
    ) -> tuple[dict[str, Any], QueuedChatTurn]:
        runtime = self._active_queue_runtime(conversation_id, queue_control=True)
        for index, item in enumerate(runtime.queued_prompts):
            if isinstance(item, QueuedChatTurn) and item.id == queue_id:
                removed = runtime.queued_prompts.pop(index)
                revision = self._bump_chat_queue_revision(runtime)
                return ({
                    "conversation_id": runtime.conversation_id,
                    "items": runtime.chat_queue_payload(),
                    "revision": revision,
                    "removed_id": removed.id,
                }, removed)
        raise KeyError("等待项不存在或已经发送")

    def prioritize_queued_chat_turn(
        self, conversation_id: str, queue_id: str
    ) -> dict[str, Any]:
        runtime = self._active_queue_runtime(conversation_id, queue_control=True)
        if runtime.last_send_now_id == queue_id and (
                runtime.starting or (runtime.task is not None and not runtime.task.done())
                or not any(isinstance(item, QueuedChatTurn) and item.id == queue_id
                           for item in runtime.queued_prompts)):
            return {
                "conversation_id": runtime.conversation_id,
                "items": runtime.chat_queue_payload(),
                "revision": self._current_chat_queue_revision(runtime),
                "send_now_id": queue_id,
                "status": "already_requested",
            }
        selected_index = next((
            index for index, item in enumerate(runtime.queued_prompts)
            if isinstance(item, QueuedChatTurn) and item.id == queue_id
        ), -1)
        if selected_index < 0:
            raise KeyError("等待项不存在或已经发送")
        selected = runtime.queued_prompts.pop(selected_index)
        runtime.queued_prompts.insert(0, selected)
        runtime.last_send_now_id = queue_id
        self._bump_chat_queue_revision(runtime)
        runtime.cancel_requested = True
        hold_continuation(runtime, "优先处理等待区消息；原持续任务保留")
        runtime.continuation_intervention = False
        runtime.expert_control_action = "pause"
        from mewcode.workflow_core.goal_host import interrupt as interrupt_goals
        interrupt_goals(EXPERT_RUNS, runtime.conversation_id, "pause")
        task = runtime.task
        if runtime.starting and task is None:
            status = "cancelled_before_start"
        elif task is None or task.done():
            # A background graph has no main-task callback to launch this turn.
            # Reserve it now; its paused workers can finish saving independently.
            self._continue_conversation_queue(runtime)
            status = "dispatching"
        else:
            if not task.cancelling():
                task.cancel()
            status = "cancelling"
        return {
            "conversation_id": runtime.conversation_id,
            "items": runtime.chat_queue_payload(),
            "revision": self._current_chat_queue_revision(runtime),
            "send_now_id": queue_id,
            "status": status,
        }

    def _continue_conversation_queue(self, runtime: ConversationRuntime) -> None:
        # Both main-turn and background completion can arrive in the same tick.
        # Only one callback may reserve/pop the next message.
        if runtime.starting or (runtime.task is not None and not runtime.task.done()):
            return
        if runtime.task is not None:
            if self.chat_task is runtime.task:
                self.chat_task = None
            runtime.task = None
        if runtime.queued_prompts:
            prompt = runtime.queued_prompts.pop(0)
            if isinstance(prompt, QueuedChatTurn):
                self._bump_chat_queue_revision(runtime)
            # A send-now cancellation targets the completed task, not the next
            # queued turn. Reserve synchronously to close the task-callback gap.
            runtime.cancel_requested = False
            runtime.starting = True
            asyncio.create_task(_start_dequeued_runtime_turn(runtime, prompt))
            return
        if (any(not task.done() for task in getattr(runtime, '_case_resume_tasks', {}).values())
                or any(not task.done() for task in getattr(runtime, '_goal_run_tasks', {}).values())):
            # A case answer owns a pending continuation. Keep its session and
            # agent alive until that delivery starts or is explicitly retired.
            return
        visible = self.session is runtime.session
        if not visible:
            runtime.session.close()
            if runtime.cordis_runtime is not None:
                asyncio.create_task(runtime.cordis_runtime.aclose())
        if self.conversation_runs.get(runtime.conversation_id) is runtime:
            self.conversation_runs.pop(runtime.conversation_id, None)

    def attach_conversation_task(
        self, runtime: ConversationRuntime, task: asyncio.Task
    ) -> None:
        runtime.task = task
        runtime._turn_start_token = None
        runtime.starting = False
        if self.session is runtime.session:
            self.chat_task = task

        def cleanup(done: asyncio.Task) -> None:
            if runtime.task is not done:
                return  # A new turn was attached before this old callback ran.
            visible = self.session is runtime.session
            if visible and self.chat_task is done:
                self.chat_task = None
            runtime.task = None
            runtime.starting = False
            try:
                exc = done.exception()
            except (asyncio.CancelledError, asyncio.InvalidStateError):
                exc = None
            if exc is not None:
                # Confirm-started expert runs used to die silently here
                # (AttributeError on ConversationRuntime.context, etc.).
                asyncio.create_task(WS_EVENTS.send_json({
                    "type": "error",
                    "text": f"专家团执行失败：{exc}",
                    "conversation_id": runtime.conversation_id,
                }))
                asyncio.create_task(WS_EVENTS.send_json({
                    "type": "done",
                    "interrupted": False,
                    "error": str(exc),
                    "elapsed": 0,
                    "conversation_id": runtime.conversation_id,
                    "status": runtime.status_payload(),
                    "tasks": runtime.tasks_payload(),
                }))
            self._continue_conversation_queue(runtime)
            design_done = globals().get("task_design_after_turn")
            if design_done and (runtime.orchestration_planning or runtime.execution_kind == "orchestration"):
                try:
                    outcome = done.result() if not done.cancelled() and exc is None else {}
                    outcome = outcome if isinstance(outcome, dict) else {}
                    design_done(runtime, interrupted=done.cancelled() or outcome.get("interrupted", False),
                                error=bool(exc or outcome.get("error")))
                except (ValueError, OSError, TypeError):
                    log.warning("task design completion checkpoint failed", exc_info=True)

        task.add_done_callback(cleanup)
        if runtime.cancel_requested:
            task.cancel()

    def runtime_for(self, conversation_id: str = "") -> ConversationRuntime | None:
        target = conversation_id or (self.session.session_id if self.session else "")
        runtime = self.conversation_runs.get(target)
        return runtime if runtime is not None and runtime.running() else None

    def interrupt_conversation(
        self, conversation_id: str = "", *, action: str = "pause"
    ) -> dict[str, Any]:
        """Request cancellation and report whether a real worker existed.

        The renderer is optimistic: it marks a turn busy before the WebSocket
        frame is acknowledged.  Therefore an interrupt for a non-running turn
        is a valid recovery operation, not an error; callers use the result to
        clear a stale local busy state.
        """

        target = str(conversation_id or "").strip()
        if not target and self.session is not None:
            target = self.session.session_id
        runtime = self.conversation_runs.get(target)
        if runtime is None or not runtime.running():
            return {
                "conversation_id": target,
                "status": "not_running",
                "cancelled": False,
            }

        visible_queue_count = sum(
            isinstance(item, QueuedChatTurn)
            for item in getattr(runtime, "queued_prompts", [])
        )
        runtime.cancel_requested = True
        from mewcode.workflow_core.goal_host import interrupt as interrupt_goals
        background_interrupted = interrupt_goals(EXPERT_RUNS, target, action)
        hold_continuation(runtime, "用户要求终止" if action == "terminate" else "用户要求暂停",
                          terminate=action == "terminate")
        runtime.continuation_intervention = False
        runtime.queued_prompts.clear()
        queue_revision = (
            self._bump_chat_queue_revision(runtime) if visible_queue_count else 0
        )
        runtime.expert_control_action = (
            "terminate" if action == "terminate" else "pause"
        )
        task = runtime.task
        if task is None or task.done():
            # Background-only cancellation still owns real work. A reserved
            # main turn is cancelled when attach_conversation_task runs.
            return {
                "conversation_id": target,
                "status": ("cancelling" if background_interrupted else
                           "cancelled_before_start" if runtime.starting else "not_running"),
                "cancelled": bool(background_interrupted or runtime.starting),
                **({"queue_revision": queue_revision} if visible_queue_count else {}),
                **({"queue_cleared": visible_queue_count} if visible_queue_count else {}),
            }
        if not getattr(task, "cancelling", lambda: 0)() and not task.cancel():
            return {
                "conversation_id": target,
                "status": "not_running",
                "cancelled": False,
                **({"queue_revision": queue_revision} if visible_queue_count else {}),
                **({"queue_cleared": visible_queue_count} if visible_queue_count else {}),
            }
        return {
            "conversation_id": target,
            "status": "cancelling",
            "cancelled": True,
            **({"queue_revision": queue_revision} if visible_queue_count else {}),
            **({"queue_cleared": visible_queue_count} if visible_queue_count else {}),
        }

    def _release_active_conversation(self) -> None:
        """Release the visible chat without closing a file used by a running turn."""
        self._save_active_conversation_settings()
        runtime = self.runtime_for()
        if runtime is None:
            self._close_session()
            if self.agent_runtime is not None:
                self.agent_runtime.close()
        else:
            self.session = None
        self.agent_runtime = None
        self.chat_task = None
        self.conversation = self.new_conversation_store()
        self.agent = None
        self.client = None
        self._agent_tool = None
        self.worktree_manager = None
        self.team_manager = None
        self.task_manager = self.context.get(TASK_FACTORY).create(BACKGROUND_TASKS_FILE)
        self.task_manager.runtime_scheduler = APP_RUNTIME_SCHEDULER
        self.trace_manager = self.context.get(TRACE_FACTORY).create()

    def _save_active_conversation_settings(self) -> None:
        if self.session is None:
            return
        conversation_id = self.session.session_id
        fallback_mode = getattr(getattr(self, "permission_mode", None), "value", PermissionMode.DEFAULT.value)
        mode = getattr(getattr(self.agent, "permission_mode", None), "value", fallback_mode) if self.agent else fallback_mode
        settings_store = getattr(self, "conversation_settings", None)
        if settings_store is None:
            settings_store = {}
            self.conversation_settings = settings_store
        record = dict(settings_store.get(conversation_id, {}))
        record.update(
            self._conversation_provider_route_settings(
                self.slot, self.client, record
            )
        )
        record.update({
            "effort": getattr(self, "effort", ""),
            "context_tier": getattr(self, "context_tier", 0),
            "permission_mode": mode,
            "multi_agent": getattr(self, "multi_agent", False),
            "expert_mode": getattr(self, "expert_mode", False),
            "orchestration_planning": getattr(self, "orchestration_planning", False),
            "permission_before_plan": getattr(
                self, "permission_before_plan", PermissionMode.AUTO.value
            ),
            "permission_before_orch": getattr(self, "permission_before_orch", ""),
        })
        settings_store[conversation_id] = record
        self.conversation_settings = dict(list(self.conversation_settings.items())[-300:])
        self.web_state["conversation_settings"] = self.conversation_settings
        _save_web_state(self.web_state)

    @staticmethod
    def _conversation_provider_route_settings(
        slot: ProviderSlot | None,
        client: Any,
        previous: dict[str, Any],
    ) -> dict[str, Any]:
        if slot is None:
            route = {
                key: previous[key]
                for key in ("provider_key", "model")
                if key in previous
            }
            if not route:
                route["provider_key"] = ""
            return route
        model = getattr(client, "model", None) if client is not None else None
        if not isinstance(model, str) or not model.strip():
            model = slot.provider.model
        return {
            "provider_key": slot.key,
            "model": str(model or ""),
        }

    @staticmethod
    def _has_explicit_conversation_route(settings: dict[str, Any]) -> bool:
        provider_key = settings.get("provider_key")
        return "model" in settings or (
            isinstance(provider_key, str) and bool(provider_key.strip())
        )

    def _restore_conversation_settings(self, conversation_id: str) -> None:
        settings = self.conversation_settings.get(conversation_id, {})
        if not isinstance(settings, dict):
            settings = {}
        raw_provider_key = settings.get("provider_key")
        provider_key = (
            raw_provider_key.strip()
            if isinstance(raw_provider_key, str)
            else ""
        )
        if self._has_explicit_conversation_route(settings) and not provider_key:
            self.slot = None
            self.provider_restore_error = (
                "Saved conversation model has no valid provider key. "
                "Select a provider explicitly to restore this route."
            )
        elif provider_key:
            source_slot = next(
                (slot for slot in self.slots if slot.key == provider_key), None
            )
            if source_slot is None:
                self.slot = None
                self.provider_restore_error = (
                    f"Saved provider slot '{provider_key}' is unavailable. "
                    "Select that provider again to restore this conversation route."
                )
            else:
                saved_model = settings.get("model")
                try:
                    selected_slot = replace(source_slot)
                    if "model" in settings:
                        if not isinstance(saved_model, str) or not saved_model.strip():
                            raise ValueError(
                                "saved model must be a non-empty string"
                            )
                        provider = resolve_effective_provider_config(
                            source_slot.provider, saved_model.strip()
                        )
                        selected_slot = replace(selected_slot, provider=provider)
                    self.slot = selected_slot
                    self.provider_restore_error = ""
                except Exception as exc:
                    self.slot = None
                    self.provider_restore_error = (
                        f"Saved provider '{provider_key}' model "
                        f"'{saved_model}' could not be restored: {exc}"
                    )
        else:
            self.provider_restore_error = ""
        effort = settings.get("effort")
        if isinstance(effort, str):
            self.effort = effort
        tier = settings.get("context_tier")
        if tier in CONTEXT_TIERS:
            self.context_tier = tier
        try:
            self.permission_mode = public_permission_mode(
                settings.get("permission_mode", self.permission_mode.value), allow_plan=True
            )
        except (TypeError, ValueError):
            pass
        saved_multi_agent = settings.get("multi_agent")
        if isinstance(saved_multi_agent, bool):
            self.multi_agent = saved_multi_agent
        else:
            self.multi_agent = bool(
                settings.get("multi_agent", self.multi_agent)
                or self.permission_mode == PermissionMode.AUTO
            )
        self.web_state["multi_agent"] = self.multi_agent
        # Expert mode is per-conversation. Missing keys must not inherit a sticky
        # True left behind by a previous 「确认开跑」 — that traps normal chat.
        self.expert_mode = bool(settings.get("expert_mode", False))
        self.orchestration_planning = bool(
            settings.get("orchestration_planning", False)
        )
        self.permission_before_plan = public_permission_mode(settings.get("permission_before_plan")).value
        before_orch = settings.get("permission_before_orch")
        self.permission_before_orch = public_permission_mode(before_orch).value if before_orch else ""
        if self.orchestration_planning:
            self.permission_mode = _design_permission_mode(
                self.permission_mode, self.permission_before_plan, self.permission_before_orch)
            self.permission_before_orch = ""
            settings.update(permission_mode=self.permission_mode.value, permission_before_orch="")
            self.conversation_settings[conversation_id] = settings
            self.web_state["conversation_settings"] = self.conversation_settings

    def _save_runtime_settings(self, runtime: ConversationRuntime) -> None:
        record = dict(self.conversation_settings.get(runtime.conversation_id, {}))
        record.update(
            self._conversation_provider_route_settings(
                runtime.slot, runtime.client, record
            )
        )
        record.update({
            "effort": runtime.effort,
            "context_tier": runtime.context_tier,
            "permission_mode": runtime.agent.permission_mode.value,
            "multi_agent": runtime.multi_agent,
            "expert_mode": runtime.expert_mode,
            "orchestration_planning": getattr(runtime, "orchestration_planning", False),
            "permission_before_plan": runtime.permission_before_plan,
            "permission_before_orch": getattr(runtime, "permission_before_orch", ""),
        })
        self.conversation_settings[runtime.conversation_id] = record
        self.web_state["conversation_settings"] = self.conversation_settings
        _save_web_state(self.web_state)

    def _ensure_active_agent(self) -> None:
        if self.agent is not None or self.slot is None:
            return
        try:
            provider = resolve_effective_provider_config(self.slot.provider)
            self.slot = replace(self.slot, provider=provider)
            self._build_agent(provider)
            effort = self._effective_effort()
            if effort and self.client is not None:
                self.client.set_effort(effort)
            self._apply_context_tier()
            self._sync_context_faces()
            self.provider_restore_error = ""
        except Exception as exc:
            log.exception("failed to build an isolated conversation agent")
            self.provider_restore_error = (
                f"Provider '{self.slot.key}' could not be restored: {exc}"
            )
            self.client = None
            self.agent = None

    def session_manager(self, scope: str) -> SessionManager:
        """scope: project（当前激活项目）| user/normal（普通对话）。"""
        return self.project_sessions if scope == "project" else self.user_sessions

    def new_conversation_store(self) -> ConversationManager:
        return self.context.get(CONVERSATION_FACTORY).create()

    def new_session_store(self, work_dir: str | Path) -> SessionManager:
        return self.context.get(SESSION_FACTORY).create(str(work_dir))

    @staticmethod
    def _meta_payload(m, active_id: str = "") -> dict:
        return {
            "id": m.id,
            "title": m.title or "(无标题)",
            "summary": m.summary,
            "message_count": m.message_count,
            "total_tokens": m.total_tokens,
            "last_active": m.last_active.isoformat(),
            "active": m.id == active_id,
        }

    def list_conversations(self, scope: str, query: str = "") -> list[dict]:
        metas = self.session_manager(scope).list()
        q = query.strip().lower()
        active_id = self.session.session_id if self.session else ""
        out = []
        for m in metas:
            if q and q not in m.title.lower() and q not in m.summary.lower():
                continue
            item = self._meta_payload(m, active_id)
            item["running"] = self.chat_busy(m.id)
            out.append(item)
        return out

    def new_conversation(self, scope: str) -> dict:
        self._release_active_conversation()
        self.scope = scope
        # A manual cap belongs to the old conversation.  New chats should
        # start with the selected model's full window, not inherit that cap.
        self.context_tier = 0
        self.web_state["context_tier"] = 0
        self.session = self.session_manager(scope).create()
        self.conversation = self.new_conversation_store()
        self._ensure_file_history()
        self._ensure_active_agent()
        if self.agent is not None:
            self._sync_agent_conversation_owner()
        # Fresh chats start in normal mode. Expert/orchestration activation is
        # owned by the conversation that confirmed a run, not by every later one.
        self.expert_mode = False
        self.orchestration_planning = False
        self.web_state["expert_mode"] = False
        self.web_state["orchestration_planning"] = False
        self._remember_conversation(scope, self.session.session_id)
        self._save_active_conversation_settings()
        _save_web_state(self.web_state)
        return {"id": self.session.session_id, "scope": scope}

    def delete_conversation(self, scope: str, session_id: str, dir_path: str = "") -> dict:
        """删除会话，返回 {deleted, error, active_deleted}。

        dir_path 非空时删除指定项目目录的会话（用于非激活项目）。
        删除当前活动会话时必须先关闭其文件句柄——否则 Windows 下活动
        句柄占用 jsonl 文件，unlink 会报 WinError 32。删除成功后不再
        自动新建顶替会话（否则用户会看到"删了又冒一个"空对话）：
        session 置空，等下次发送消息时惰性创建。"""
        if self.chat_busy(session_id):
            return {"deleted": False, "error": "该对话正在响应中，请先打断", "active_deleted": False}
        was_active = (
            self.session is not None and self.session.session_id == session_id
        )
        try:
            if was_active:
                self._close_session()
            mgr = (
                self.new_session_store(os.path.abspath(dir_path))
                if dir_path
                else self.session_manager(scope)
            )
            ok = mgr.delete(session_id)
            if not ok:
                if was_active:
                    # 删除失败则恢复一个活动会话，避免悬空状态
                    self.new_conversation(self.scope)
                return {"deleted": False, "error": "会话不存在或已被删除"}
            if was_active:
                self.conversation = self.new_conversation_store()
            self.conversation_goals.pop(session_id, None)
            self._chat_queue_revisions.pop(session_id, None)
            self.web_state["conversation_goals"] = self.conversation_goals
            _save_web_state(self.web_state)
            EXPERT_RUNS.delete_conversation(session_id)
            return {"deleted": True, "error": None, "active_deleted": was_active}
        except OSError as e:
            if was_active and self.session is None:
                self.new_conversation(self.scope)
            return {"deleted": False, "error": f"删除失败（文件可能被占用）: {e}"}

    def load_conversation(self, scope: str, session_id: str, *, include_display: bool = True) -> dict | None:
        if self.session is not None and self.session.session_id == session_id:
            active_runtime = self.runtime_for(session_id)
            visible_history = (
                self.conversation.history[:active_runtime.turn_history_start]
                if active_runtime is not None else self.conversation.history
            )
            entries = self._display_entries(visible_history)
            return {
                "id": session_id,
                "scope": self.scope,
                "running": self.chat_busy(session_id),
                "entries": entries,
                "pipeline_events": self.pipeline_events(
                    self.scope, session_id, entries
                ) if include_display else [],
                "expert_runs": EXPERT_RUNS.public_for_conversation(session_id) if include_display else [],
            }
        runtime = self.runtime_for(session_id)
        if runtime is not None:
            self._release_active_conversation()
            self.scope = runtime.scope
            self.work_dir = runtime.work_dir
            self.project_sessions = self.new_session_store(runtime.work_dir)
            self.usage_service = runtime.usage_service or _session_usage_service(runtime.work_dir)
            self.session = runtime.session
            self.conversation = runtime.conversation
            self.agent = runtime.agent
            self.client = runtime.client
            self._bind_usage_client(self.client)
            self._bind_usage_client(
                getattr(runtime.agent, "auxiliary_client", None)
            )
            self.slot = runtime.slot
            self.effort = runtime.effort
            self.context_tier = runtime.context_tier
            self.permission_mode = getattr(
                runtime.agent, "permission_mode", PermissionMode.DEFAULT
            )
            self.multi_agent = runtime.multi_agent
            self.expert_mode = runtime.expert_mode
            self.orchestration_planning = bool(
                getattr(runtime, "orchestration_planning", False)
            )
            self.permission_before_plan = runtime.permission_before_plan
            self.permission_before_orch = getattr(runtime, "permission_before_orch", "")
            self.task_manager = runtime.task_manager
            self.task_manager.runtime_scheduler = APP_RUNTIME_SCHEDULER
            self.trace_manager = runtime.trace_manager
            self._agent_tool = runtime.agent_tool
            self.worktree_manager = runtime.worktree_manager
            self.team_manager = runtime.team_manager
            self.agent_runtime = runtime.cordis_runtime
            self.chat_task = runtime.task
            self._sync_orchestration_flag()
            self._remember_conversation(runtime.scope, session_id)
            _save_web_state(self.web_state)
            hist = runtime.conversation.history[:runtime.turn_history_start]
            entries = self._display_entries(hist)
            return {
                "id": session_id,
                "scope": runtime.scope,
                "running": True,
                "entries": entries,
                "pipeline_events": self.pipeline_events(
                    runtime.scope,
                    session_id,
                    entries,
                ) if include_display else [],
                "expert_runs": EXPERT_RUNS.public_for_conversation(session_id) if include_display else [],
            }
        result = self.session_manager(scope).resume(session_id)
        if result is None:
            return None
        self._release_active_conversation()
        self.scope = scope
        self.session = result.session
        if continuation_state(self).get("status") in {"running", "reviewing", "intervening"}:
            hold_continuation(self, "运行未在当前进程中恢复；检查点已保留，发送继续可恢复")
        self.conversation = self.new_conversation_store()
        self.conversation.restore_history(list(result.messages))
        self._ensure_file_history()
        self._restore_conversation_settings(session_id)
        self._ensure_active_agent()
        if self.agent is not None:
            self._sync_agent_conversation_owner()
        # Restore orphaned task claims once, before exposing a cold session.
        # The active-session/runtime branches above deliberately skip recovery.
        recovery_error = ''
        try:
            recover_task_design_on_session_load(self.work_dir, session_id)  # noqa: F821 - installed by task_design.install
        except (ValueError, OSError, TypeError) as exc:
            # A broken binding must stay blocked, not prevent every chat from
            # opening. Preserve the diagnostic; the task query also fails closed.
            recovery_error = str(exc)
            log.warning('Task recovery unavailable for conversation %s', session_id, exc_info=True)
        self._remember_conversation(scope, session_id)
        _save_web_state(self.web_state)
        entries = self._display_entries()
        return {
            "id": session_id,
            "scope": scope,
            "running": False,
            "task_recovery_error": recovery_error,
            "entries": entries,
            "pipeline_events": self.pipeline_events(
                scope, session_id, entries
            ) if include_display else [],
            "expert_runs": EXPERT_RUNS.public_for_conversation(session_id) if include_display else [],
        }

    def pipeline_events(
        self,
        scope: str,
        session_id: str,
        fallback_entries: list[dict[str, Any]] | None = None,
        *, sessions_dir: Path | None = None,
    ) -> list[dict[str, Any]]:
        """Load the durable DSH-style transcript for a conversation.

        The log is folded to one record per item on the way out.  A long
        thinking block used to persist tens of thousands of lines, and shipping
        all of them made the history view unopenable; the folded projection is
        identical for the renderer but scales with the number of steps.
        """
        try:
            sessions_dir = sessions_dir or self.session_manager(scope)._sessions_dir
            events = load_pipeline_items(sessions_dir, session_id)
            return events or legacy_entries_to_pipeline(fallback_entries or [])
        except (OSError, AttributeError, TypeError):
            return legacy_entries_to_pipeline(fallback_entries or [])

    def _remember_conversation(self, scope: str, session_id: str) -> None:
        last = {"scope": scope, "id": session_id}
        if scope == "project":
            last["dir"] = os.path.abspath(self.work_dir)
        self.web_state["last_conversation"] = last

    def _conversation_store_dir(self, scope: str | None = None) -> str:
        effective_scope = scope or self.scope
        if effective_scope == "project":
            return os.path.abspath(self.work_dir)
        return os.path.abspath(str(Path.home()))

    def _sync_agent_conversation_owner(self) -> None:
        if self.agent is None or self.session is None:
            return
        self.agent.session_id = self.session.session_id
        self.agent._conversation_scope = self.scope
        self.agent._conversation_store_dir = self._conversation_store_dir()
        if not getattr(self, "orchestration_planning", False):
            from mewcode.workflow_core.chat_tools import remove_design_tools
            remove_design_tools(self.agent)
            return
        from mewcode.orchestration.workflow_blocks import WorkflowBlocks
        from mewcode.tools.workflow_blocks import ReadWorkflowBlocks, SubmitWorkflowBlocks
        from mewcode.tools.task_design import AdvanceTaskDesign
        agent, cid, project = self.agent, self.session.session_id, self.work_dir

        def workflow_base():
            plan = _conversation_plan_store(cid).get(project)
            if plan and plan.spec:
                return plan.spec.as_dict()
            flow = _conversation_flow_store(cid).get(project)
            if flow:
                return from_expert_flow(flow).as_dict()
            graph = _stored_orchestration_graph(PipelineStore(project))
            if graph:
                spec = from_pipeline_graph(graph).as_dict()
                if (spec.get("meta") or {}).get("conversation_id") == cid:
                    return spec
            return None

        directory = getattr(self.session, "_sessions_dir", None) or Path(project) / ".mewcode" / "sessions"
        from mewcode.orchestration.graph_models import available_model_routes
        blocks = WorkflowBlocks(project, cid, source_path=Path(directory) / (cid + ".jsonl"),
                                base_spec=workflow_base,
                                run_store=EXPERT_RUNS,
                                guard=lambda: agent.session_id == cid and agent.work_dir == project,
                                model_routes=lambda: available_model_routes(MODEL_REGISTRY),
                                tool_services=lambda: [{"name": tool.name, "enabled": agent.registry.is_enabled(tool.name)}
                                                       for tool in agent.registry.list_tools()])
        if not getattr(agent, "registry", None):
            return
        for tool in (ReadWorkflowBlocks(blocks), AdvanceTaskDesign(blocks), SubmitWorkflowBlocks(blocks,
                     turn_id=lambda: getattr(agent, "_chat_turn_id", ""),
                     judgment_runtime=getattr(agent, "judgment_runtime", None))):
            installer = getattr(agent, "_install_runtime_tool", agent.registry.register)
            installer(tool)

        from frontend.workbench_revision import sync_tool
        sync_tool(sys.modules[__name__], agent=agent, project=project, cid=cid)

    def _close_session(self) -> None:
        if self.session is not None:
            try:
                self.session.close()
            except Exception:
                pass
            self.session = None

    # ---------- 项目管理 ----------

    def activate_project(self, dir_path: str) -> str | None:
        """切换工作项目：更新 work_dir、项目会话管理器，并重建 agent 的
        权限沙箱/检查器与记忆管理器（沙箱随项目根变化）。"""
        norm = os.path.abspath(dir_path)
        if not os.path.isdir(norm):
            return f"目录不存在: {dir_path}"
        if norm == os.path.abspath(self.work_dir):
            return None
        if self.session is not None:
            self._release_active_conversation()
        self.plugin_host.reconfigure_work_dir(norm)
        return self._finish_project_activation(norm)

    async def aactivate_project(self, dir_path: str) -> str | None:
        """Asynchronously switch projects without racing plugin cleanup."""

        norm = os.path.abspath(dir_path)
        if not os.path.isdir(norm):
            return f"目录不存在: {dir_path}"
        if norm == os.path.abspath(self.work_dir):
            return None
        if self.session is not None:
            self._release_active_conversation()
        await self.plugin_host.areconfigure_work_dir(norm)
        return self._finish_project_activation(norm)

    def _finish_project_activation(self, norm: str) -> None:
        self.work_dir = norm
        _register_project(norm)
        self.project_sessions = self.new_session_store(norm)
        self.usage_service = _session_usage_service(norm)
        self.file_history = None
        self._file_history_key = None
        self.web_state["active_project"] = norm
        _save_web_state(self.web_state)

    # ---------- Provider 插槽 ----------

    def client_state_payload(self) -> dict:
        return {
            "slots": self.slots_payload(),
            "efforts": self.efforts_payload(),
            "status": self.status_payload(),
            "ready": self.agent is not None,
            "tasks": self.tasks_payload(),
            "permission_modes": PERMISSION_MODE_CHOICES,
            "running_conversations": sorted(self.running_conversation_ids()),
            "conversation_queues": self.conversation_queues_payload(),
            "conversation_queue_revisions": self.conversation_queue_revisions_payload(),
            "commands": WEB_SLASH_COMMANDS,
            "runtime_load": APP_RUNTIME_SCHEDULER.snapshot(),
        }

    def scan_slots(self) -> str | None:
        try:
            config = load_config()
            providers = config.providers
            if "permission_mode" not in self.web_state:
                try:
                    self.permission_mode = public_permission_mode(config.permission_mode, allow_plan=True)
                except Exception:
                    pass
        except Exception:
            providers = []
        discovered = discover_provider_slots(providers, work_dir=self.work_dir)
        MODEL_REGISTRY.refresh_discovered(discovered)
        desktop = [
            ProviderSlot(
                provider=record.provider,
                source="desktop",
                origin="Electron safeStorage (memory sync)",
                registry_key=record.key,
                effort_levels=list(
                    record.model_efforts.get(record.provider.model)
                    or record.effort_levels
                ),
                default_effort=record.default_effort,
                models=list(record.models),
                model_efforts={
                    model: list(efforts)
                    for model, efforts in record.model_efforts.items()
                },
            )
            for record in MODEL_REGISTRY.records()
            if record.source == "desktop"
        ]
        self.slots = discovered + desktop
        # Provider reload/safeStorage sync can happen after the active client
        # was built.  Rebind the live slot by its stable key so newly synced
        # capability metadata is immediately visible without forcing the user
        # to switch away and back again.
        if self.slot is not None:
            active_model = self.slot.provider.model
            refreshed = next(
                (candidate for candidate in self.slots if candidate.key == self.slot.key),
                None,
            )
            if refreshed is not None:
                refreshed = copy.deepcopy(refreshed)
                if active_model and active_model != refreshed.provider.model:
                    refreshed.provider = resolve_effective_provider_config(
                        refreshed.provider, active_model
                    )
                    refreshed.effort_levels = list(
                        refreshed.model_efforts.get(active_model)
                        or model_effort_levels(
                            refreshed.provider.protocol,
                            active_model,
                            catalog_service=MODEL_CAPABILITY_CATALOG,
                        )
                    )
                self.slot = refreshed
                if self.agent is not None:
                    self._apply_context_tier()
                if self.client is not None and not self.effort:
                    default_effort = self._effective_effort()
                    if default_effort:
                        self.client.set_effort(default_effort)
        for disposer in self._context_slot_disposers:
            disposer.dispose()
        self._context_slot_disposers = [
            self.context.slots.register(
                PROVIDER_SLOT,
                slot,
                plugin="provider-discovery",
                kind="keyed",
                item_key=slot.key,
            )
            for slot in self.slots
        ]
        return self._maybe_mirror_cc_switch()

    def _slot_mirror_family(self, slot: ProviderSlot | None) -> str:
        if slot is None:
            return ""
        if slot.source == "cc-switch":
            origin = slot.origin or ""
            parts = origin.split(":")
            if len(parts) >= 2:
                return cc_switch_family(parts[1])
            name = (slot.provider.name or "").replace("cc-switch-", "")
            return cc_switch_family(name)
        if slot.source == "claude-code":
            return "claude"
        if slot.source == "codex":
            return "codex"
        return ""

    def _find_mirror_slot(self, binding: dict[str, str]) -> ProviderSlot | None:
        family = cc_switch_family(binding.get("app_type") or "")
        current_key = self.slot.key if self.slot else str(self.web_state.get("last_provider") or "")
        if current_key:
            current = next((slot for slot in self.slots if slot.key == current_key), None)
            if (
                current is not None
                and self._slot_mirror_family(current) == family
                and validate_slot(current) is None
            ):
                return current
        slot_key = f"cc-switch:{binding.get('slot_name') or binding.get('name')}"
        match = next((slot for slot in self.slots if slot.key == slot_key), None)
        if match is not None and validate_slot(match) is None:
            return match
        for slot in self.slots:
            if self._slot_mirror_family(slot) == family and validate_slot(slot) is None:
                return slot
        return None

    def _activate_slot(self, slot: ProviderSlot) -> str | None:
        err = validate_slot(slot)
        if err is not None:
            return err
        if self.chat_busy():
            return "当前对话正在响应中，请先停止后再切换模型"
        provider = resolve_effective_provider_config(slot.provider)
        previous_slot = self.slot
        self.slot = copy.deepcopy(slot)
        self.slot.provider = provider
        try:
            self._build_agent(provider)
        except Exception as e:
            self.slot = previous_slot
            return str(e)
        self.provider_restore_error = ""
        effort = self._effective_effort()
        if effort and self.client is not None:
            self.client.set_effort(effort)
        self._apply_context_tier()
        self._sync_context_faces()
        self._remember_provider(slot.key)
        self._save_active_conversation_settings()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            loop.create_task(self._resolve_window(provider))
        return None

    def _cc_switch_mirror_dirty(self, fingerprint: str) -> bool:
        previous = str(self.web_state.get("cc_switch_mirror_fp") or "")
        pending = str(self.web_state.get("cc_switch_mirror_pending_fp") or "")
        return fingerprint != previous or bool(pending)

    def sync_cc_switch_mirror(self) -> str | None:
        """Rescan and silently follow cc-switch when its current fingerprint changes."""
        try:
            fingerprint = cc_switch_bindings_fingerprint()
        except Exception:
            return None
        if not self._cc_switch_mirror_dirty(fingerprint):
            return None
        return self.scan_slots()

    def _maybe_mirror_cc_switch(self) -> str | None:
        try:
            bindings = read_cc_switch_runtime_bindings()
        except Exception:
            return None
        fingerprint = cc_switch_bindings_fingerprint(bindings)
        previous = str(self.web_state.get("cc_switch_mirror_fp") or "")
        current_key = self.slot.key if self.slot else str(self.web_state.get("last_provider") or "")
        current_slot = self.slot or next(
            (slot for slot in self.slots if slot.key == current_key),
            None,
        )
        current_family = self._slot_mirror_family(current_slot)
        current_valid = current_slot is not None and validate_slot(current_slot) is None
        target = select_cc_switch_mirror_target(
            bindings,
            current_family=current_family,
            current_valid=current_valid,
            previous_fp=previous,
            combined_fp=fingerprint,
        )
        if target is None:
            if (
                fingerprint != previous
                or self.web_state.get("cc_switch_mirror_pending_fp")
            ):
                self.web_state["cc_switch_mirror_fp"] = fingerprint
                self.web_state.pop("cc_switch_mirror_pending_fp", None)
                self.web_state.pop("cc_switch_mirror_pending_key", None)
                _save_web_state(self.web_state)
            return None
        match = self._find_mirror_slot(target)
        if match is None:
            return None
        same_live = (
            self.slot is not None
            and self.slot.key == match.key
            and self.slot.provider.model == match.provider.model
            and self.slot.provider.base_url.rstrip("/") == match.provider.base_url.rstrip("/")
            and fingerprint == previous
        )
        if same_live:
            return None
        if self.chat_busy():
            self.web_state["cc_switch_mirror_pending_fp"] = fingerprint
            self.web_state["cc_switch_mirror_pending_key"] = match.key
            _save_web_state(self.web_state)
            return None
        err = self._activate_slot(match)
        if err is not None:
            log.warning("cc-switch silent mirror skipped: %s", err)
            return None
        self.web_state["cc_switch_mirror_fp"] = fingerprint
        self.web_state.pop("cc_switch_mirror_pending_fp", None)
        self.web_state.pop("cc_switch_mirror_pending_key", None)
        _save_web_state(self.web_state)
        log.info("cc-switch silent mirror -> %s [%s]", match.key, match.provider.model)
        return match.key

    def slots_payload(self) -> list[dict]:
        current_key = self.slot.key if self.slot else ""
        recent = self.web_state.get("recent_providers", [])
        return [
            {
                "key": s.key,
                "label": s.label,
                "current": s.key == current_key,
                "recent": s.key in recent,
                "error": validate_slot(s),
            }
            for s in self.slots
        ]

    def _remember_provider(self, key: str) -> None:
        self.web_state["last_provider"] = key
        recent: list[str] = self.web_state.get("recent_providers", [])
        if key in recent:
            recent.remove(key)
        recent.insert(0, key)
        self.web_state["recent_providers"] = recent[:5]
        _save_web_state(self.web_state)

    def effort_levels(self) -> list[str]:
        if self.slot is None:
            return []
        # 插槽来源配置（codex config.toml 的 enabled-reasoning-efforts）
        # 显式声明的档位优先；未声明时回退到协议 + 模型能力识别。
        if self.slot.effort_levels:
            return list(self.slot.effort_levels)
        return model_effort_levels(
            self.slot.provider.protocol,
            self.slot.provider.model,
            catalog_service=MODEL_CAPABILITY_CATALOG,
        )

    def _effective_effort(self) -> str:
        """用户显式设置 > 插槽来源配置的默认档（model_reasoning_effort）。"""
        if self.effort:
            return self.effort
        if (
            self.slot is not None
            and self.slot.default_effort in self.slot.effort_levels
        ):
            return self.slot.default_effort
        return ""

    def efforts_payload(self) -> dict:
        levels = self.effort_levels()
        current = self.effort
        if not current and self.client is not None:
            current = getattr(self.client, "effort", "") or ""
        if not current:
            current = self._effective_effort()
        return {"levels": levels, "current": current or "auto"}

    def status_payload(self) -> dict:
        provider = self.slot.provider if self.slot else None
        detected = provider.get_context_window() if provider is not None else 0
        window = _reconcile_agent_context_window(
            self.agent, self.context_tier, detected
        )
        active_runtime = self.runtime_for(self.session.session_id) if self.session else None
        current_mode = (
            self.agent.permission_mode.value if self.agent is not None
            else self.permission_mode.value
        )
        prompt_tokens = 0
        cache_read = 0
        cache_creation = 0
        cache_usage_available = False
        if self.agent is not None:
            cache_read = self.agent.total_cache_read_tokens
            cache_creation = self.agent.total_cache_creation_tokens
            cache_usage_available = getattr(
                self.agent, "cache_usage_available", False
            ) or cache_read > 0 or cache_creation > 0
            prompt_tokens = (
                self.agent.total_input_tokens + cache_read + cache_creation
            )
        return {
            "model": provider.model if provider else "",
            "provider": provider.name if provider else "",
            "provider_key": self.slot.key if self.slot else "",
            "provider_error": getattr(self, "provider_restore_error", ""),
            "window": window,
            "context_tier": self.context_tier,
            "detected_window": detected,
            "effort": self.efforts_payload()["current"],
            **FAST_MODE_SETTINGS.status(self.slot, getattr(self, 'client', None)),
            "total_in": self.agent.total_input_tokens if self.agent else 0,
            "total_out": self.agent.total_output_tokens if self.agent else 0,
            "cache_read": cache_read,
            "cache_creation": cache_creation,
            "cache_usage_available": cache_usage_available,
            "cache_hit_rate": (
                cache_read / prompt_tokens
                if cache_usage_available and prompt_tokens
                else (0.0 if cache_usage_available else None)
            ),
            **_context_policy_payload(self.agent),
            **_cache_observability_payload(self.agent),
            "prompt_tokens": prompt_tokens,
            "session": _fmt_duration(time.monotonic() - self.session_start),
            "conversation_id": self.session.session_id if self.session else "",
            "conversation_title": (
                self.session.meta.title if self.session is not None else "新对话"
            ) or "新对话",
            "project_name": Path(self.work_dir).name or self.work_dir,
            "scope": self.scope,
            "permission_mode": current_mode,
            "permission_level": (
                public_permission_mode(self.permission_before_plan).value
                if current_mode == PermissionMode.PLAN.value
                else public_permission_mode(current_mode).value
            ),
            "planning": current_mode == PermissionMode.PLAN.value,
            "work_dir": self.work_dir,
            "multi_agent": self.multi_agent,
            "expert_mode": self.expert_mode,
            "orchestration_planning": bool(getattr(self, "orchestration_planning", False)),
            "execution_kind": getattr(active_runtime, "execution_kind", "chat"),
            "continuation": continuation_status(self),
            "goal": self.active_goal(),
            "goal_mode": bool(self.active_goal()),
        }

    def active_goal(self) -> str:
        conversation_id = self.session.session_id if self.session else ""
        return self.conversation_goals.get(conversation_id, "")

    def set_active_goal(self, goal: str) -> str:
        if self.session is None:
            self.new_conversation(self.scope)
        assert self.session is not None
        conversation_id = self.session.session_id
        normalized = goal.strip()[:4000]
        if normalized:
            self.conversation_goals[conversation_id] = normalized
        else:
            self.conversation_goals.pop(conversation_id, None)
            hold_continuation(self, "用户关闭持续目标", terminate=True)
        # Bound stale state while retaining the most recently touched goals.
        self.conversation_goals = dict(list(self.conversation_goals.items())[-200:])
        self.web_state["conversation_goals"] = self.conversation_goals
        _save_web_state(self.web_state)
        return normalized

    def execute_web_command(self, raw_name: str, args: str = "") -> dict:
        name = WEB_SLASH_ALIASES.get(raw_name.strip().lower(), "")
        value = args.strip()
        if not name:
            return {"ok": False, "message": f"未知命令：/{raw_name}。输入 /help 查看可用命令。"}
        if name in {"fix", "update"}:
            return maintenance_command_result(self.session.session_id if self.session else "", name, value)
        if name == "help":
            lines = ["可用斜杠命令："] + [
                f"{item['usage']} — {item['description']}"
                for item in WEB_SLASH_COMMANDS
                if item["name"] != "help"
            ]
            return {"ok": True, "message": "\n".join(lines)}
        if name == "plan":
            if self.permission_mode != PermissionMode.PLAN:
                self.permission_before_plan = self.permission_mode.value
                self.web_state["permission_before_plan"] = self.permission_before_plan
            error = self._set_internal_permission_mode(PermissionMode.PLAN.value)
            return {
                "ok": error is None,
                "message": error or "已进入 Plan 模式：只读分析，仅允许写入计划文件。",
                "prompt": value,
            }
        if name == "do":
            restore = public_permission_mode(self.permission_before_plan).value
            error = self.set_permission_mode(restore)
            if self.agent is not None:
                self.agent._plan_path_cache = None
            return {
                "ok": error is None,
                "message": error or f"已退出 Plan 模式，权限恢复为 {restore}。",
                "prompt": value,
            }
        if name == "goal":
            if value.lower() in {"off", "clear", "stop", "关闭", "清除"}:
                self.set_active_goal("")
                return {"ok": True, "message": "已关闭当前对话的目标模式。"}
            if not value:
                current = self.active_goal()
                return {
                    "ok": bool(current),
                    "message": (
                        f"当前目标：{current}\n输入 /goal off 可关闭。"
                        if current else "用法：/goal <目标>；例如 /goal 修复全部失败测试"
                    ),
                }
            goal = self.set_active_goal(value)
            return {
                "ok": True,
                "message": f"已设定持续目标：{goal}",
                "prompt": goal,
            }
        if name in {"auto", "manual"}:
            mode = PermissionMode.AUTO.value if name == "auto" else PermissionMode.DEFAULT.value
            error = self.set_permission_mode(mode)
            return {"ok": error is None, "message": error or f"权限模式已切换为 {mode}。"}
        if name == "multi":
            on = value.lower() not in {"off", "0", "false", "关闭"}
            self.set_multi_agent(on)
            return {"ok": True, "message": f"多 Agent 模式已{'开启' if on else '关闭'}。"}
        if name == "expert":
            on = value.lower() not in {"off", "0", "false", "关闭"}
            error = self.set_expert_mode(on)
            return {"ok": error is None, "message": error or f"专家团模式已{'开启' if on else '关闭'}。"}
        return {"ok": False, "message": f"暂不支持命令：/{name}"}

    def execute_web_command_for(
        self, conversation_id: str, raw_name: str, args: str = ""
    ) -> dict:
        """Execute a slash command against its explicit conversation owner."""
        active_id = self.session.session_id if self.session else ""
        if raw_name.strip().lower() in {"fix", "update"}:
            if conversation_id != active_id:
                return {"ok": False, "message": "请切换到维护编排所属的对话后重试。"}
            return maintenance_command_result(conversation_id, raw_name.strip().lower(), args)
        runtime = self.runtime_for(conversation_id)
        if runtime is None:
            if conversation_id and conversation_id != active_id:
                return {"ok": False, "message": "目标对话未激活，请先切换后重试。"}
            return self.execute_web_command(raw_name, args)

        visible = active_id == conversation_id

        name = WEB_SLASH_ALIASES.get(raw_name.strip().lower(), "")
        value = args.strip()
        if not name or name == "help":
            return self.execute_web_command(raw_name, args)
        if name == "plan":
            if runtime.agent.permission_mode != PermissionMode.PLAN:
                runtime.permission_before_plan = runtime.agent.permission_mode.value
            # Explicit /plan selects the separate read-only workflow.
            pause_design = globals().get("pause_task_design_for_mode_change")
            if pause_design:
                pause_design(runtime.work_dir, conversation_id)
            runtime.orchestration_planning = False
            runtime.agent.orchestration_planning = False
            runtime.permission_before_orch = ""
            runtime.agent.set_permission_mode(PermissionMode.PLAN)
            if visible:
                self.permission_mode = PermissionMode.PLAN
                self.permission_before_plan = runtime.permission_before_plan
                self.orchestration_planning = False
                self.permission_before_orch = ""
                self.web_state.update(orchestration_planning=False, permission_before_orch="")
            self._save_runtime_settings(runtime)
            return {
                "ok": True,
                "message": "已进入 Plan 模式：只读分析，仅允许写入计划文件。",
                "prompt": value,
            }
        if name == "do":
            try:
                restore = public_permission_mode(runtime.permission_before_plan)
            except ValueError:
                restore = PermissionMode.AUTO
            runtime.agent.set_permission_mode(restore)
            if visible:
                self.permission_mode = restore
                self.permission_before_plan = runtime.permission_before_plan
            runtime.agent._plan_path_cache = None
            self._save_runtime_settings(runtime)
            return {
                "ok": True,
                "message": f"已退出 Plan 模式，权限恢复为 {restore.value}。",
                "prompt": value,
            }
        if name == "goal":
            lowered = value.lower()
            if lowered in {"off", "clear", "stop", "关闭", "清除"}:
                value = ""
            if not value and lowered not in {"off", "clear", "stop", "关闭", "清除"}:
                current = self.conversation_goals.get(conversation_id, "")
                return {
                    "ok": bool(current),
                    "message": f"当前目标：{current}" if current else "用法：/goal <目标>",
                }
            if value:
                if continuation_state(runtime).get("status") in {"running", "reviewing", "intervening"}:
                    self.interrupt_conversation(conversation_id, action="terminate")
                self.conversation_goals[conversation_id] = value[:4000]
                runtime.goal = value[:4000]
                message = f"已设定持续目标：{runtime.goal}"
                prompt = runtime.goal
            else:
                self.conversation_goals.pop(conversation_id, None)
                runtime.goal = ""
                hold_continuation(runtime, "用户关闭持续目标", terminate=True)
                self.interrupt_conversation(conversation_id, action="terminate")
                message = "已关闭当前对话的目标模式。"
                prompt = ""
            self.web_state["conversation_goals"] = self.conversation_goals
            _save_web_state(self.web_state)
            return {"ok": True, "message": message, "prompt": prompt}
        if name in {"auto", "manual"}:
            mode = PermissionMode.AUTO if name == "auto" else PermissionMode.DEFAULT
            runtime.agent.set_permission_mode(mode)
            if mode == PermissionMode.AUTO:
                _apply_runtime_multi_agent(runtime, True)
            if visible:
                self.permission_mode = mode
                if mode == PermissionMode.AUTO:
                    self.multi_agent = True
                    self.web_state["multi_agent"] = True
            self._save_runtime_settings(runtime)
            return {"ok": True, "message": f"权限模式已切换为 {mode.value}。"}
        if name == "multi":
            on = value.lower() not in {"off", "0", "false", "关闭"}
            _apply_runtime_multi_agent(runtime, on)
            if visible:
                self.multi_agent = on
            self._save_runtime_settings(runtime)
            return {"ok": True, "message": f"多 Agent 模式已{'开启' if on else '关闭'}。"}
        if name == "expert":
            on = value.lower() not in {"off", "0", "false", "关闭"}
            if on and EXPERT_PROFILES.get(runtime.work_dir) is None:
                return {"ok": False, "message": "请先为该项目配置专家角色模型。"}
            runtime.expert_mode = on
            if visible:
                self.expert_mode = on
            self._save_runtime_settings(runtime)
            return {"ok": True, "message": f"专家团模式已{'开启' if on else '关闭'}。"}
        return {"ok": False, "message": f"暂不支持命令：/{name}"}

    async def use_slot(self, key: str) -> str | None:
        slot = next((s for s in self.slots if s.key == key), None)
        if slot is None:
            return f"未找到插槽: {key}"
        return self._activate_slot(slot)

    def _ensure_file_history(self) -> FileHistory:
        session_id = self.session.session_id if self.session is not None else "anonymous"
        key = (os.path.abspath(self.work_dir), session_id)
        if self.file_history is None or self._file_history_key != key:
            self.file_history = FileHistory(self.work_dir, session_id)
            self._file_history_key = key
        self._bind_file_history(self.file_history)
        return self.file_history

    def _bind_file_history(self, history: FileHistory | None) -> None:
        if self.agent is None:
            return
        self.agent.file_history = history
        registry = getattr(self.agent, "registry", None)
        if registry is None:
            return
        tools = (
            registry.list_tools()
            if callable(getattr(registry, "list_tools", None))
            else []
        )
        for tool in tools:
            if hasattr(tool, "file_history"):
                tool.file_history = history

    def _turn_changes_map(self) -> dict[int, list[dict[str, str]]]:
        try:
            return self._ensure_file_history().load_turn_changes()
        except Exception:
            return {}

    def _display_entries(self, messages: list[Message] | None = None) -> list[dict]:
        history = messages if messages is not None else self.conversation.history
        return messages_to_display(history, turn_changes=self._turn_changes_map())

    def _build_agent(self, provider: ProviderConfig) -> None:
        if self.slot is not None:
            provider = MODEL_REGISTRY.apply_service_tier(self.slot.key, provider)
        runtime = self.context.get(AGENT_FACTORY).create(AgentRuntimeRequest(
            provider=provider,
            work_dir=self.work_dir,
            permission_mode=self.permission_mode,
            conversation=self.conversation,
            session_manager=self.session_manager(self.scope),
            session=self.session,
            usage_service=self.usage_service,
            file_history=self._ensure_file_history(),
            extra_tools=(AskUserTool(),),
        ))
        runtime.install(multi_agent_plugin(MultiAgentOptions(
            provider=provider,
            task_state_path=BACKGROUND_TASKS_FILE,
            provider_registry=MODEL_REGISTRY,
            enable_fork=False,
            teammate_mode="in-process",
            is_interactive=False,
            enable_coordinator_mode=False,
            enabled=self.multi_agent,
        )))
        old_runtime = self.agent_runtime
        self.agent_runtime = runtime
        self.agent = runtime.agent
        self.client = runtime.client
        if self.slot is not None:
            MODEL_REGISTRY.track_service_tier(self.slot.key, provider.model, self.client)
        self.task_manager = runtime.context.get(TASKS)
        self.task_manager.runtime_scheduler = APP_RUNTIME_SCHEDULER
        self.trace_manager = runtime.context.get(TRACES)
        self.worktree_manager = runtime.context.get(WORKTREES)
        self.team_manager = runtime.context.get(TEAMS)
        self._agent_tool = runtime.context.get("multi_agent.tool")
        agent_catalog = self.context.get(AGENT_CATALOG).list_agents()
        if agent_catalog:
            lines = [
                "## Available Sub-Agent Types",
                "",
                "Use the Agent tool with subagent_type to delegate work:",
                "",
                *(
                    f"- **{name}**: {description}"
                    for name, description in agent_catalog
                ),
            ]
            self.agent.set_agent_catalog("\n".join(lines), catalog_list=agent_catalog)
        from mewcode.permissions.review_client import resolve_safety_review_client
        fallback_aux = getattr(self.agent, "auxiliary_client", None) or self.client
        if getattr(self.agent, "safety_reviewer", None) is not None:
            self.agent.safety_reviewer.client = resolve_safety_review_client(
                MODEL_REGISTRY,
                fallback_client=fallback_aux,
                usage_recorder=self.client.get_usage_recorder() if self.client else None,
            )
        self._sync_agent_conversation_owner()
        self._sync_orchestration_flag()
        if old_runtime is not None:
            old_runtime.close()
        self._schedule_mcp()

    def _user_mcp_servers(self) -> list:
        try:
            return list(load_config().mcp_servers)
        except Exception:
            return []

    def _schedule_mcp(self) -> None:
        if self._mcp_init_task is not None and not self._mcp_init_task.done():
            self._mcp_init_task.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if loop.is_running():
            self._mcp_init_task = loop.create_task(self._init_mcp())

    async def _init_mcp(self) -> None:
        runtime = self.agent_runtime
        if runtime is None:
            return
        from mewcode.mcp.playwright_browser import format_mcp_connect_error, install_runtime_mcp

        try:
            errors = await install_runtime_mcp(runtime, self._user_mcp_servers())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            errors = [format_mcp_connect_error("playwright", exc)]
        if runtime is not self.agent_runtime:
            return
        self.mcp_errors = list(errors)
        for err in errors:
            log.warning("MCP: %s", err)
            try:
                self.conversation.add_system_reminder(err)
            except Exception:
                pass

    async def _resolve_window(self, provider: ProviderConfig) -> None:
        await resolve_context_window(provider)
        if self.agent is not None:
            self.agent.context_window = provider.get_context_window()
        self._apply_context_tier()

    def _apply_context_tier(self) -> None:
        """Apply an optional manual cap to the detected model window.

        agent.context_window 被 auto_compact 在每次迭代动态读取，直接改写
        即可让压缩阈值随档位即时变化；status/usage 展示也随之更新。"""
        if self.agent is None or self.slot is None:
            return
        detected = self.slot.provider.get_context_window()
        _reconcile_agent_context_window(self.agent, self.context_tier, detected)

    def set_context_tier(self, window: int) -> str | None:
        if window not in CONTEXT_TIERS:
            return (
                f"无效上下文档位: {window}"
                f"（可选: {', '.join(str(t) for t in CONTEXT_TIERS)}）"
            )
        self.context_tier = window
        self.web_state["context_tier"] = window
        runtime = self.runtime_for(self.session.session_id) if self.session else None
        if runtime is not None:
            runtime.context_tier = window
        _save_web_state(self.web_state)
        self._apply_context_tier()
        self._save_active_conversation_settings()
        return None

    # ---------- effort / model ----------

    def set_effort(self, level: str) -> str | None:
        levels = self.effort_levels()
        previous = str(getattr(self.client, "effort", "") or self.effort or "")
        if level == "":
            # 重置为 auto：清除显式设置，client 回到来源配置默认档
            #（codex config.toml 的 model_reasoning_effort；无则协议默认）
            self.effort = ""
            if self.client is not None and self.slot is not None:
                p = self.slot.provider
                if p.protocol == "anthropic":
                    self.client.set_effort("high" if p.thinking else "off")
                elif hasattr(self.client, "effort"):
                    # openai 系：回到配置默认档；无默认则置空（不发 reasoning 参数）
                    self.client.effort = self._effective_effort()
            current = str(getattr(self.client, "effort", "") or "")
            if self.agent is not None:
                self.agent.note_effort_changed(previous, current)
            self.web_state["effort"] = ""
            _save_web_state(self.web_state)
            self._save_active_conversation_settings()
            return None
        if not levels:
            return "当前模型不支持 effort 调整（非推理模型或 Provider 未就绪）"
        if level not in levels:
            return f"无效级别: {level}（可选: {', '.join(levels)}）"
        self.effort = level
        if self.client is not None:
            self.client.set_effort(level)
        current = str(getattr(self.client, "effort", "") or level)
        if self.agent is not None:
            self.agent.note_effort_changed(previous, current)
        self.web_state["effort"] = level
        _save_web_state(self.web_state)
        self._save_active_conversation_settings()
        return None

    def switch_model(self, model: str) -> str | None:
        if self.slot is None or self.agent is None:
            return "当前 Provider 未就绪"
        provider = self.slot.provider
        old = provider.model
        model = model.strip()
        if not model or model == old:
            return None
        if self.chat_busy():
            return "当前对话正在响应中，请先停止后再切换模型"
        try:
            provider = resolve_effective_provider_config(provider, model)
            self.slot.provider = provider
            self._build_agent(provider)
        except Exception as e:
            self.slot.provider.model = old
            return str(e)
        effort = self._effective_effort()
        if effort and self.client is not None:
            self.client.set_effort(effort)
        self._apply_context_tier()
        # 换模型后重新评估 effort 适用性：不适用则清除
        if self.effort and self.effort not in self.effort_levels():
            self.effort = ""
            self.web_state["effort"] = ""
            _save_web_state(self.web_state)
        self.provider_restore_error = ""
        self._save_active_conversation_settings()
        asyncio.ensure_future(self._resolve_window(provider))
        return None

    async def use_model_ref(
        self, provider_key: str, model_id: str, effort: str = ""
    ) -> str | None:
        if self.chat_busy():
            return "当前对话正在响应中，请先停止后再切换模型"
        try:
            resolved = MODEL_REGISTRY.resolve({
                "provider_key": provider_key,
                "model_id": model_id,
                "effort": effort,
            })
        except ValueError as exc:
            return str(exc)
        self.slot = ProviderSlot(
            provider=resolved.config,
            source=resolved.provider.source,
            origin=resolved.provider.label,
            registry_key=resolved.provider.key,
            effort_levels=(
                list(resolved.provider.effort_levels)
                if resolved.provider.effort_levels
                else model_effort_levels(
                    resolved.protocol,
                    model_id,
                    catalog_service=MODEL_CAPABILITY_CATALOG,
                )
            ),
            default_effort=resolved.effort,
        )
        self.effort = resolved.effort
        if getattr(self, "context", None) is None:
            self.client = resolved.client
            self.agent.configure_model(
                resolved.client,
                protocol=resolved.protocol,
                context_window=resolved.context_window,
                reason="model_changed",
            )
            auxiliary = _build_auxiliary_client(resolved.config) or resolved.client
            self.agent.auxiliary_client = auxiliary
            from mewcode.permissions.review_client import resolve_safety_review_client
            if getattr(self.agent, "safety_reviewer", None) is not None:
                self.agent.safety_reviewer.client = resolve_safety_review_client(
                    MODEL_REGISTRY, fallback_client=auxiliary,
                    usage_recorder=self.agent.client.get_usage_recorder())
        else:
            try:
                self._build_agent(resolved.config)
            except Exception as exc:
                return str(exc)
        if self.effort and callable(getattr(self.client, "set_effort", None)):
            self.client.set_effort(self.effort)
        self.provider_restore_error = ""
        self._apply_context_tier()
        self.web_state["effort"] = self.effort
        self._remember_provider(self.slot.key)
        self._save_active_conversation_settings()
        asyncio.ensure_future(self._resolve_window(resolved.config))
        return None

    async def list_models(self) -> list[str] | None:
        if self.client is None:
            return None
        try:
            models = await self.client.list_models()
        except Exception:
            return None
        if not models:
            return None
        if self.slot and is_opencode_go_provider(self.slot.provider):
            return ordered_go_model_ids(models)
        return sorted(set(models))

    # ---------- 任务状态 ----------

    def tasks_payload(self) -> list[dict]:
        out = []
        current_id = self.session.session_id if self.session else ""
        current_store = os.path.normcase(os.path.abspath(self._conversation_store_dir()))
        current_scope = str(getattr(self, "scope", "project") or "")
        for t in self.task_manager._tasks.values():
            owner_store = os.path.normcase(os.path.abspath(t.owner_store_dir or current_store))
            if t.owner_session_id and (
                t.owner_session_id != current_id
                or owner_store != current_store
                or (t.owner_scope and t.owner_scope != current_scope)
            ):
                continue
            # Share the task-detail clock; queue time and old wall-clock
            # records must not become execution time in the sidebar API.
            detail = self.task_manager.task_payload(t.id, include_result=False)
            if detail is None:
                continue
            agent_id = str(getattr(t.agent, "agent_id", "") or "")
            scheduler = runtime_scheduler_for_agent(
                t.agent, self.task_manager.runtime_scheduler or APP_RUNTIME_SCHEDULER,
            )
            clock = scheduler.agent_runtime(agent_id) if agent_id else {}
            queue_position = scheduler.queue_position(agent_id=agent_id) if agent_id else 0
            status = t.status
            if status == "running" and queue_position and not clock.get("active"):
                status = "queued"
            elapsed = detail["elapsed"]
            out.append({
                "id": t.id,
                "name": t.name,
                "task": t.task[:80],
                "status": status,
                "elapsed": elapsed,
                "duration_basis": detail["duration_basis"],
                "duration_quality": detail["duration_quality"],
                "legacy_wall_elapsed": detail["legacy_wall_elapsed"],
                "runtime_started": bool(clock.get("started") or (elapsed is not None and elapsed > 0)),
                "runtime_active": bool(clock.get("active")),
                "queue_position": queue_position,
                "owner_task_id": t.owner_task_id,
                "parent_task_id": t.owner_task_id,
                "native_execution": detail["native_execution"],
                "owner_task_title": t.owner_task_title,
                "cancellation_pending": detail["cancellation_pending"],
                "cleanup_failed": detail["cleanup_failed"],
            })
        out.sort(key=lambda x: (
            {"running": 0, "queued": 1}.get(x["status"], 2),
            -(x["elapsed"] if x["elapsed"] is not None else -1),
        ))
        return out

    @staticmethod
    def _has_task_notification(messages: list[Message], task_id: str) -> bool:
        marker = f"Task ID: {task_id}\n"
        return any(
            message.role == "user"
            and isinstance(message.content, str)
            and message.content.startswith("<task-notification>")
            and marker in message.content
            for message in messages
        )

    def collect_task_notifications(self) -> list[BackgroundTask]:
        """把完成结果幂等写回所属会话，返回需要自动续跑的当前会话任务。"""
        completed = self.task_manager.poll_completed()
        if not completed:
            return []

        current_id = self.session.session_id if self.session else ""
        current_store = self._conversation_store_dir()
        active: list[BackgroundTask] = []
        handled: list[str] = []

        for task in completed:
            owner_id = task.owner_session_id or current_id
            owner_store = os.path.abspath(task.owner_store_dir or current_store)
            if (
                task.origin in {"expert", "maintenance"}
                or EXPERT_RUNS.is_expert_child_task(task.id, owner_id)
            ):
                # Expert/Fix children are consumed by their own ledgers. Feeding
                # them into the generic notification loop starts an unrelated
                # chat turn merely because the user opened that conversation.
                handled.append(task.id)
                continue
            is_current = bool(
                self.session
                and owner_id == current_id
                and owner_store == current_store
            )
            notification = format_task_notification(task)

            if is_current:
                if not self._has_task_notification(
                    self.conversation.history, task.id
                ):
                    self.conversation.add_user_message(notification)
                    self.session.append(Message(role="user", content=notification))
                active.append(task)
                handled.append(task.id)
                continue

            if not owner_id:
                handled.append(task.id)
                continue
            sessions_dir = Path(owner_store) / ".mewcode" / "sessions"
            if not sessions_dir.is_dir():
                handled.append(task.id)
                continue
            try:
                manager = self.new_session_store(owner_store)
                restored = manager.resume(owner_id)
                if restored is None:
                    handled.append(task.id)
                    continue
                try:
                    if not self._has_task_notification(
                        restored.messages, task.id
                    ):
                        restored.session.append(
                            Message(role="user", content=notification)
                        )
                finally:
                    restored.session.close()
                handled.append(task.id)
            except OSError:
                self.task_manager.requeue_notification(task.id)

        self.task_manager.mark_notified(handled)
        return active

    def agent_tasks_payload(self) -> dict:
        """后台任务面板数据：子 agent 运行按团队/普通子 Agent 分组聚合。

        数据源是 AgentTool.runs 注册表——每条记录持有活体 Agent 引用，
        轮询时直接读实时计数器（token/工具数随执行逐轮增长）；
        后台/队友运行的权威状态从 TaskManager 按 task_id 取。"""
        tool = self._agent_tool

        groups: dict[str, dict] = {}
        running_total = 0
        queued_total = 0
        seen_task_ids: set[str] = set()
        current_id = self.session.session_id if self.session else ""
        current_store = self._conversation_store_dir()

        def group_identity(task_id: str, title: str, team: str = "", origin: str = "") -> tuple[str, str, str]:
            if origin == "maintenance":
                return task_id, "编排修复 · Fix", "maintenance"
            if task_id:
                label = title.strip()[:72] or task_id[:8]
                return f"task:{task_id}", f"任务 · {label}", "task"
            if team:
                return f"team:{team}", team, "team"
            return "subagents", "旧任务 · 归属未记录", "subagents"

        for run in tool.runs.values() if tool is not None else []:
            owner_id = run.get("owner_session_id", "")
            owner_store = os.path.abspath(
                run.get("owner_store_dir", "") or current_store
            )
            if owner_id and (
                owner_id != current_id or owner_store != current_store
            ):
                continue
            agent = run["agent"]
            status, end = run["status"], run["end"]
            result_available = False
            bg = None
            if run["task_id"]:
                bg = self.task_manager.get(run["task_id"])
                if bg is not None:
                    status, end = bg.status, bg.end_time
                    result_available = status != "running"
                    seen_task_ids.add(bg.id)
            scheduler = runtime_scheduler_for_agent(
                agent, self.task_manager.runtime_scheduler or APP_RUNTIME_SCHEDULER,
            )
            runtime_clock = scheduler.agent_runtime(agent.agent_id)
            elapsed = (self.task_manager._execution_elapsed(bg) if bg is not None
                       else runtime_clock["elapsed"])
            tin, tout = agent.total_input_tokens, agent.total_output_tokens
            row = {
                "id": agent.agent_id,
                "name": run["name"],
                "type": run["agent_type"],
                "mode": run["mode"],
                "status": status,
                "tokens": tin + tout,
                "in": tin,
                "out": tout,
                "tools": agent.total_tool_calls,
                "elapsed": round(elapsed, 1),
                "duration_basis": "active_runtime",
                "duration_quality": bg.duration_quality if bg is not None else "measured",
                "runtime_started": runtime_clock["started"] or elapsed > 0,
                "runtime_active": runtime_clock["active"],
                "runtime_active_slots": runtime_clock["active_slots"],
                "task": run["task"],
                "task_id": run["task_id"],
                "result_available": result_available,
                "cancellation_pending": bool(bg and bg.cancellation_pending),
                "cleanup_confirmed": (
                    bg.cleanup_confirmed if bg and bg.lifecycle_finalized else None
                ),
                "cleanup_failed": bool(
                    bg and bg.lifecycle_finalized and not bg.cleanup_confirmed
                ),
                "cleanup_error": bg.cleanup_error if bg else "",
                "parent_task_id": bg.owner_task_id if bg else str(run.get("owner_task_id") or ""),
                "native_execution": dict(bg.native_execution) if bg else {},
            }
            queue_position = scheduler.queue_position(
                agent_id=agent.agent_id
            )
            if status == "running" and queue_position:
                row["queue_position"] = queue_position
                if not runtime_clock["active"]:
                    row["status"] = "queued"
                    status = "queued"
                    queued_total += 1
            if status == "running":
                running_total += 1

            key, title, kind = group_identity(
                bg.owner_task_id if bg else str(run.get("owner_task_id") or ""),
                bg.owner_task_title if bg else str(run.get("owner_task_title") or ""),
                str(run.get("team") or ""),
                bg.origin if bg else "",
            )
            g = groups.setdefault(key, {
                "key": key,
                "title": title,
                "kind": kind,
                "agents": 0, "tokens": 0, "tools": 0,
                "running": 0, "done": 0, "failed": 0,
                "elapsed": 0.0, "_latest": 0.0,
                "rows": [],
            })
            g["agents"] += 1
            g["tokens"] += row["tokens"]
            g["tools"] += row["tools"]
            g["elapsed"] = max(g["elapsed"], row["elapsed"])
            g["_latest"] = max(g["_latest"], run["start"])
            if status in {"running", "queued"}:
                g["running"] += 1
            elif status == "completed":
                g["done"] += 1
            else:  # failed / cancelled
                g["failed"] += 1
            g["rows"].append(row)

        for bg in self.task_manager.list_tasks():
            if bg.id in seen_task_ids:
                continue
            owner_id = bg.owner_session_id
            owner_store = os.path.abspath(bg.owner_store_dir or current_store)
            if owner_id and (
                owner_id != current_id or owner_store != current_store
            ):
                continue
            elapsed = self.task_manager._execution_elapsed(bg)
            scheduler = runtime_scheduler_for_agent(
                bg.agent, self.task_manager.runtime_scheduler or APP_RUNTIME_SCHEDULER,
            )
            runtime_clock = (
                scheduler.agent_runtime(bg.agent.agent_id)
                if bg.agent is not None and getattr(bg.agent, "agent_id", "")
                else {"started": False, "active": False, "active_slots": 0}
            )
            queue_position = (
                scheduler.queue_position(agent_id=bg.agent.agent_id)
                if bg.agent is not None and getattr(bg.agent, "agent_id", "")
                else 0
            )
            row_status = bg.status
            if row_status == "running" and queue_position:
                if not runtime_clock["active"]:
                    row_status = "queued"
                    queued_total += 1
            row = {
                "id": bg.id,
                "name": bg.name,
                "type": "background",
                "mode": "bg",
                "status": row_status,
                "tokens": (
                    bg.progress.input_tokens + bg.progress.output_tokens
                ),
                "in": bg.progress.input_tokens,
                "out": bg.progress.output_tokens,
                "tools": bg.progress.tool_call_count,
                "elapsed": round(elapsed, 1),
                "duration_basis": bg.duration_basis,
                "duration_quality": bg.duration_quality,
                "runtime_started": runtime_clock["started"] or elapsed > 0,
                "runtime_active": runtime_clock["active"],
                "runtime_active_slots": runtime_clock["active_slots"],
                "task": bg.task,
                "task_id": bg.id,
                "parent_task_id": bg.owner_task_id,
                "native_execution": dict(bg.native_execution),
                "result_available": bg.status != "running",
                "cancellation_pending": bg.cancellation_pending,
                "cleanup_confirmed": (
                    bg.cleanup_confirmed if bg.lifecycle_finalized else None
                ),
                "cleanup_failed": (
                    bg.lifecycle_finalized and not bg.cleanup_confirmed
                ),
                "cleanup_error": bg.cleanup_error,
            }
            if bg.origin == "maintenance":
                fix = getattr(bg.agent, "_maintenance_turn", None)
                info = fix.info if fix else {}
                row.update(maintenance_id=bg.owner_task_id.removeprefix("maintenance:"),
                    project=bg.owner_store_dir, conversation_id=bg.owner_session_id,
                    activity=info.get("detail") or bg.progress.last_activity,
                    waiting_user=bg.status == "running" and info.get("status") == "waiting_user")
            if queue_position and bg.status == "running":
                row["queue_position"] = queue_position
            if row_status == "running":
                running_total += 1
            key, title, kind = group_identity(bg.owner_task_id, bg.owner_task_title, origin=bg.origin)
            g = groups.setdefault(key, {
                "key": key,
                "title": title,
                "kind": kind,
                "agents": 0, "tokens": 0, "tools": 0,
                "running": 0, "done": 0, "failed": 0,
                "elapsed": 0.0, "_latest": 0.0,
                "rows": [],
            })
            g["agents"] += 1
            g["tokens"] += row["tokens"]
            g["tools"] += row["tools"]
            g["elapsed"] = max(g["elapsed"], row["elapsed"])
            g["_latest"] = max(g["_latest"], bg.start_time)
            if row_status in {"running", "queued"}:
                g["running"] += 1
            elif bg.status == "completed":
                g["done"] += 1
            else:
                g["failed"] += 1
            g["rows"].append(row)

        out = []
        for g in groups.values():
            g["status"] = (
                "running" if g["running"]
                else ("failed" if g["failed"] and not g["done"] else "completed")
            )
            # The group header is a live clock, not the longest task in this
            # conversation's history.  Old completed tasks can be hours long
            # while a newly queued child has not begun at all.
            g["elapsed"] = round(max(
                (row["elapsed"] for row in g["rows"] if row["status"] == "running"),
                default=0.0,
            ), 1)
            # 行内排序：运行中在前，其余按名字稳定排列
            g["rows"].sort(key=lambda r: (
                r["status"] not in {"running", "queued"},
                int(r.get("queue_position") or 0),
                r["name"],
            ))
            out.append(g)
        # 组间排序：运行中在前，同档按最新 spawn 时间倒序
        out.sort(key=lambda g: (g["status"] != "running", -g.pop("_latest")))
        return {
            "groups": out,
            "conversation_id": current_id,
            "running_total": running_total,
            "queued_total": queued_total,
            "runtime_load": APP_RUNTIME_SCHEDULER.snapshot(),
        }

    # ---------- 侧栏：项目 / 普通对话 / 最近 ----------

    def sidebar_payload(self) -> dict:
        """两级对话结构：项目对话（按项目目录分组）+ 普通对话（用户级）。"""
        active_id = self.session.session_id if self.session else ""
        cwd_norm = os.path.abspath(self.work_dir)

        projects: list[dict] = []
        dirs = [cwd_norm]
        for d in _load_projects():
            norm = os.path.abspath(d)
            if norm != cwd_norm and norm not in dirs:
                dirs.append(norm)
        for d in dirs:
            convs: list[dict] = []
            if (Path(d) / ".mewcode" / "sessions").is_dir():
                mgr = self.new_session_store(d)
                convs = []
                for meta in mgr.list():
                    item = self._meta_payload(meta, active_id)
                    item["running"] = self.chat_busy(meta.id)
                    convs.append(item)
            projects.append({
                "dir": d,
                "label": Path(d).name or d,
                "active": d == cwd_norm,
                "conversations": convs,
            })

        normal = self.list_conversations("user")
        return {
            "projects": projects,
            "normal": normal,
            "work_dir": cwd_norm,
        }

    def view_conversation(self, dir_path: str, session_id: str) -> dict | None:
        """只读查看其他项目目录的会话（不切换当前活动状态）。

        安全约束：dir 必须属于已登记项目/当前目录/home；session_id 只允许
        单词字符，防止路径遍历；目录下必须已存在 sessions 存储（避免在
        任意路径创建目录）。"""
        if not session_id or not _SAFE_ID_RE.match(session_id):
            return None
        try:
            norm = os.path.abspath(dir_path)
        except Exception:
            return None
        allowed = {
            os.path.abspath(self.work_dir),
            os.path.abspath(str(Path.home())),
            *(os.path.abspath(d) for d in _load_projects()),
        }
        if norm not in allowed:
            return None
        if not (Path(norm) / ".mewcode" / "sessions").is_dir():
            return None
        try:
            mgr = self.new_session_store(norm)
            result = mgr.resume(session_id)
        except Exception:
            return None
        if result is None:
            return None
        try:
            result.session.close()
        except Exception:
            pass
        return {
            "id": session_id,
            "readonly": True,
            "entries": messages_to_display(result.messages),
            "pipeline_events": (
                load_pipeline_items(mgr._sessions_dir, session_id)
                or legacy_entries_to_pipeline(messages_to_display(result.messages))
            ),
        }

    def set_multi_agent(self, on: bool) -> None:
        """多 Agent 模式触发器：开 = 允许主 Agent 派发子 Agent/团队（耗 token），
        关 = 从工具注册表禁用 Agent 工具（schema 不下发，零额外开销）。
        切换对下一次 LLM 请求即时生效；已在运行的子任务不受影响。"""
        self.multi_agent = bool(on)
        self.web_state["multi_agent"] = self.multi_agent
        _save_web_state(self.web_state)
        _set_multi_agent_tools(self.agent, self.multi_agent)
        runtime = self.runtime_for()
        if runtime is not None:
            _apply_runtime_multi_agent(runtime, self.multi_agent)
        self._save_active_conversation_settings()

    def set_expert_mode(self, on: bool, *, resume_checkpoint: dict | None = None) -> str | None:
        if on and resume_checkpoint is not None:
            if (not resume_checkpoint.get('run_id') or not self.session
                    or resume_checkpoint.get('conversation_id') != self.session.session_id):
                return '原任务检查点不属于当前对话，未恢复执行。'
            error = expert_resume_model_error(MODEL_REGISTRY, resume_checkpoint)
            if error:
                return error
        profile = EXPERT_PROFILES.get(self.work_dir) if on and resume_checkpoint is None else None
        if on and resume_checkpoint is None and profile is None:
            return "请先选择管理、执行和审计模型；规划顾问可以自动路由或单独指定"
        if profile is not None:
            try:
                healed = relocate_expert_profile(MODEL_REGISTRY, profile)
                if healed != profile:
                    EXPERT_PROFILES.put(self.work_dir, healed)
                    profile = healed
                for model_ref in (
                    profile.manager,
                    profile.planner,
                    profile.executor,
                    profile.auditor,
                ):
                    if model_ref is not None:
                        MODEL_REGISTRY.resolve(model_ref)
            except ValueError as exc:
                return f"专家角色配置已失效，请重新选择：{exc}"
        if on:
            error = self.set_orchestration_planning(False)
            if error:
                return error
        self.expert_mode = bool(on)
        self.web_state["expert_mode"] = self.expert_mode
        # Synchronize the preference without changing a turn's execution kind.
        for runtime in list(getattr(self, "conversation_runs", {}).values()):
            runtime.expert_mode = self.expert_mode
            if on:
                runtime.orchestration_planning = False
        _save_web_state(self.web_state)
        self._save_active_conversation_settings()
        return None

    def _sync_orchestration_flag(self) -> None:
        flag = bool(getattr(self, "orchestration_planning", False))
        if flag and self.permission_mode == PermissionMode.PLAN:
            # Old web settings and live conversation runtimes may still carry
            # the Plan overlay installed by the former graph-design entry.
            restore = _design_permission_mode(self.permission_mode,
                self.permission_before_plan, getattr(self, "permission_before_orch", ""))
            self.permission_before_orch = ""
            self.web_state["permission_before_orch"] = ""
            self._set_internal_permission_mode(restore.value)
        if self.agent is not None:
            self.agent.orchestration_planning = flag
        runtime = self.runtime_for()
        if runtime is not None:
            runtime.orchestration_planning = flag
            runtime.agent.orchestration_planning = flag
            if flag:
                runtime.permission_before_orch = ""
                _apply_runtime_permission_mode(runtime, self.permission_mode)

    def set_orchestration_planning(self, on: bool) -> str | None:
        """Select task/graph design without imposing read-only Plan permissions."""
        on = bool(on)
        if on and self.expert_mode:
            return "请先关闭专家团执行，再进入编排商量"
        was = bool(getattr(self, "orchestration_planning", False))
        if (on or was) and self.permission_mode == PermissionMode.PLAN:
            restore = _design_permission_mode(self.permission_mode,
                self.permission_before_plan, getattr(self, "permission_before_orch", ""))
            error = self._set_internal_permission_mode(restore.value)
            if error:
                return error
        self.orchestration_planning = on
        # Keep the legacy field readable for migration, but new design sessions
        # do not save a second permission level to restore over later choices.
        self.permission_before_orch = ""
        self.web_state["permission_before_orch"] = ""
        if on:
            self.permission_before_plan = public_permission_mode(self.permission_mode).value
            self.web_state["permission_before_plan"] = self.permission_before_plan
        self._sync_orchestration_flag()
        self._sync_agent_conversation_owner()
        runtime = self.runtime_for()
        if runtime is not None:
            runtime.permission_before_orch = self.permission_before_orch
            if on:
                runtime.permission_before_plan = self.permission_before_plan
        self.web_state["orchestration_planning"] = self.orchestration_planning
        _save_web_state(self.web_state)
        self._save_active_conversation_settings()
        return None

    def set_permission_mode(self, mode: str) -> str | None:
        valid = [c["value"] for c in PERMISSION_MODE_CHOICES]
        if mode not in valid:
            return f"无效权限模式: {mode}（可选: {', '.join(valid)}）"
        return self._set_internal_permission_mode(mode)

    def _set_internal_permission_mode(self, mode: str, *, restoring: bool = False) -> str | None:
        if mode == PermissionMode.PLAN.value and not restoring:
            pause_design = globals().get("pause_task_design_for_mode_change")
            cid = self.session.session_id if self.session else ""
            if pause_design and cid:
                pause_design(self.work_dir, cid)
        if mode == PermissionMode.PLAN.value and not restoring and getattr(self, "orchestration_planning", False):
            # A user's explicit Plan choice remains available as its own mode,
            # with its original restrictions, outside graph design.
            self.orchestration_planning = False
            self.permission_before_orch = ""
            self.web_state.update(orchestration_planning=False, permission_before_orch="")
            self._sync_orchestration_flag()
            runtime = self.runtime_for()
            if runtime is not None:
                runtime.permission_before_orch = ""
        if mode != PermissionMode.PLAN.value:
            mode = public_permission_mode(mode).value
        elif not restoring and self.permission_mode != PermissionMode.PLAN:
            self.permission_before_plan = public_permission_mode(self.permission_mode).value
            self.web_state["permission_before_plan"] = self.permission_before_plan
        try:
            pm = PermissionMode(mode)
        except ValueError:
            return f"无效权限模式: {mode}"
        self.permission_mode = pm
        runtime = self.runtime_for()
        if runtime is not None and pm == PermissionMode.PLAN:
            runtime.permission_before_plan = self.permission_before_plan
        if self.agent is not None:
            self.agent.set_permission_mode(pm)
        self.web_state["permission_mode"] = mode
        if pm == PermissionMode.AUTO:
            self.set_multi_agent(True)
        _save_web_state(self.web_state)
        self._save_active_conversation_settings()
        return None


SESSION = WebSession()


def _conversation_task_owner(session: WebSession, conversation_id: str):
    """Resolve a captured owner identity without guessing across projects."""
    runtime = getattr(session, "conversation_runs", {}).get(conversation_id)
    if runtime is not None:
        scope = str(runtime.scope or "")
        store = (
            os.path.abspath(str(Path.home()))
            if scope == "user"
            else os.path.abspath(runtime.work_dir)
            if scope == "project" and runtime.work_dir
            else ""
        )
        return (scope, store) if scope and store else None

    active_session = getattr(session, "session", None)
    if active_session is not None and active_session.session_id == conversation_id:
        scope = str(getattr(session, "scope", "") or "")
        store = session._conversation_store_dir(scope) if scope else ""
        return (scope, os.path.abspath(store)) if scope and store else None

    manager = getattr(session, "task_manager", None)
    if manager is None:
        return None
    candidates: set[tuple[str, str]] = set()
    for task in manager.list_tasks():
        if task.owner_session_id != conversation_id:
            continue
        scope = str(task.owner_scope or "")
        store = str(task.owner_store_dir or "")
        if scope not in {"project", "user"} or not store:
            # A matching but incomplete owner record makes inference unsafe.
            return None
        candidates.add((scope, os.path.normcase(os.path.abspath(store))))
    if len(candidates) != 1:
        return None
    scope, store = next(iter(candidates))
    return scope, store


async def request_chat_interrupt(
    conversation_id: str = "", *, action: str = "pause"
) -> dict[str, Any]:
    """Cancel a chat turn and always publish a renderer acknowledgement."""

    # Capture ownership before interrupting: completion can detach the runtime,
    # and the visible session may already belong to another conversation.
    requested = str(conversation_id or getattr(getattr(SESSION, "session", None), "session_id", ""))
    runtime = getattr(SESSION, "conversation_runs", {}).get(requested)
    owner = _conversation_task_owner(SESSION, requested) if requested else None
    manager = getattr(runtime, "task_manager", None)
    if manager is None:
        manager = getattr(SESSION, "task_manager", None)
    result = SESSION.interrupt_conversation(conversation_id, action=action)
    target = str(result.get("conversation_id") or conversation_id or "")
    if target != requested:
        owner = _conversation_task_owner(SESSION, target) if target else None
        runtime = getattr(SESSION, "conversation_runs", {}).get(target)
        manager = getattr(runtime, "task_manager", None) or getattr(SESSION, "task_manager", None)
    child_ids: list[str] = []
    pending_child_ids: list[str] = []
    if owner is not None and manager is not None:
        owner_scope, owner_store = owner
        owner_store = os.path.normcase(os.path.abspath(owner_store))
        child_ids = [
            task.id for task in manager.list_tasks()
            if task.status == "running"
            and task.owner_session_id == target
            and task.owner_scope == owner_scope
            and task.owner_store_dir
            and os.path.normcase(os.path.abspath(task.owner_store_dir)) == owner_store
        ]
        pending_child_ids = await manager.settle_tasks(
            child_ids, cancel=True, timeout=2.0
        )
    if child_ids:
        result["child_task_cancellation_requested"] = child_ids
    if pending_child_ids:
        result["child_task_cancellation_pending"] = pending_child_ids
    failed_cleanup_ids = [
        task_id for task_id in child_ids
        if (task := manager.get(task_id)) is not None
        and task.lifecycle_finalized
        and not task.cleanup_confirmed
    ] if manager is not None else []
    if failed_cleanup_ids:
        result["child_task_cleanup_failed"] = failed_cleanup_ids
    scoped_ws = ConversationWebSocket(WS_EVENTS, target)
    status = str(result.get("status") or "not_running")
    if result.get("queue_cleared"):
        await scoped_ws.send_json({
            "type": "chat_queue",
            "items": [],
            "revision": result.get("queue_revision", 0),
        })
    reconciled_run_ids: list[str] = []
    reconciled_status = ""
    if status == "not_running" and target:
        # A backend worker can disappear while its durable expert checkpoint
        # still says ``running`` (for example after a provider/tool hang or an
        # interrupted backend lifecycle).  Treat the checkpoint as the stale
        # side of the disagreement: there is no live runtime left to advance
        # it, so make it resumable instead of leaving a permanent ghost run in
        # the timeline.
        reconciled_status = "terminated" if action == "terminate" else "paused"
        reason = (
            "运行协程已不存在，用户终止时已校正持久化状态"
            if reconciled_status == "terminated"
            else "运行协程已不存在，用户暂停时已保存为可接力状态"
        )
        for snapshot in EXPERT_RUNS.list_for_conversation(target):
            if snapshot.get("status") != "running" or snapshot.get("legacy"):
                continue
            run_id = str(snapshot.get("run_id") or "")
            if not run_id:
                continue
            try:
                EXPERT_RUNS.transition(run_id, reconciled_status, reason=reason)
            except (KeyError, OSError, ValueError):
                continue
            reconciled_run_ids.append(run_id)
        if reconciled_run_ids:
            result["reconciled_expert_run_ids"] = reconciled_run_ids
            result["expert_status"] = reconciled_status
    if status in {"not_running", "cancelled_before_start"}:
        # A lost chat frame leaves the renderer optimistically busy although no
        # worker exists.  A terminal event is the authoritative way to unlock
        # that conversation and discard its empty assistant placeholder.
        done_event = {
            "type": "done",
            "interrupted": True,
            "already_stopped": status == "not_running",
            "elapsed": 0,
            "tasks": SESSION.tasks_payload(),
        }
        if reconciled_run_ids:
            done_event["expert_status"] = reconciled_status
            done_event["run_ids"] = reconciled_run_ids
        await scoped_ws.send_json(done_event)
    else:
        await scoped_ws.send_json({
            "type": "interrupt_ack",
            "status": status,
        })
    return result


# ---------------------------------------------------------------------------
# 聊天执行
# ---------------------------------------------------------------------------

async def run_expert_chat(ws, task, **kwargs):
    """Keep a single conversation task alive across temporary main-agent turns."""
    from mewcode.orchestration.takeover import TakeoverController
    runtime = kwargs["session"]
    runtime.last_expert_run_id = ""
    runtime.execution_kind = "orchestration"
    from frontend.interaction_choices import prepare_for_context
    async def prepare_interaction(*, request, snapshot, node_id, cached=None, refresh=False):
        stage = ((snapshot.get('node_states') or {}).get(node_id) or {})
        instruction = getattr(request, 'choice_instruction', '') or (stage.get('pending_interaction') or {}).get('choice_instruction', '')
        if stage.get('choice_instructions'):
            instruction += '\n' + json.dumps({'actions': stage['choice_instructions'],
                'notes_role': 'context_only', 'notes': stage.get('choice_action_notes', {})}, ensure_ascii=False)
        request.choice_instruction = instruction
        return await prepare_for_context(sys.modules[__name__], runtime=runtime,
            snapshot=snapshot, questions=request.source_questions or request.questions,
            node_id=node_id, cached=cached, refresh=refresh,
            identity={"conversation_id": runtime.conversation_id,
                      "run_id": snapshot.get("run_id", ""), "node_id": node_id,
                      "request_id": request.request_id},
            instruction=instruction)
    async def prepare_stage_parameters(*, questions, snapshot, node_id):
        return await prepare_for_context(sys.modules[__name__], runtime=runtime,
            snapshot=snapshot, questions=questions, node_id=node_id,
            identity={"conversation_id": runtime.conversation_id, "run_id": snapshot.get("run_id", ""),
                      "node_id": node_id, "request_id": "stage-parameters:" + node_id})
    runtime.stage_parameter_preparer = prepare_stage_parameters
    runtime.interaction_choice_preparer = prepare_interaction
    async def prepare_recovery(*, case, snapshot):
        from frontend.interaction_choices import refresh_case_choices
        changed = await refresh_case_choices(sys.modules[__name__], runtime=runtime,
            snapshot=snapshot, case_id=case['case_id'], action='')
        from mewcode.experts.recovery_case import get_case
        return get_case(changed, case['case_id']) or case
    runtime.recovery_choice_preparer = prepare_recovery
    controller = TakeoverController(runtime, kwargs.get("run_store") or EXPERT_RUNS, ws)
    runtime.expert_takeover_controller = controller
    await controller.run(task, flow_runner=_run_expert_chat_pass,
                         dialogue_runner=_run_takeover_dialogue, kwargs=kwargs)


async def _prepare_chat_choice_event(runtime, event, ws):
    if getattr(event, "host_file_access", None):
        return  # Exact host consent choices must never be rewritten by a model.
    if not (getattr(runtime, "orchestration_planning", False)
            or getattr(runtime, "execution_kind", "") == "orchestration"):
        return
    from frontend.interaction_choices import prepare_for_context
    from mewcode.orchestration.task_design import question_binding
    from mewcode.orchestration.draft_store import DraftStore
    event.task_design_binding = question_binding(DraftStore.read_state(runtime.work_dir, runtime.conversation_id) or {})
    event.task_design_owner = (runtime.work_dir, runtime.conversation_id)
    from mewcode.orchestration.interaction_choices import project_questions
    event.source_questions = copy.deepcopy(event.questions)
    await ws.send_json({"type": "interaction_choices_preparing", "active": True,
        "request_id": event.request_id, "text": "正在查找候选／准备处理方案"})
    try:
        bundle = await prepare_for_context(sys.modules[__name__], runtime=runtime,
            questions=event.source_questions,
            identity={"conversation_id": runtime.conversation_id, "request_id": event.request_id,
                      "kind": "planning_question", "task_design": event.task_design_binding})
        event.choice_bundle = bundle
        event.questions = project_questions(event.source_questions, bundle)
    finally:
        await ws.send_json({"type": "interaction_choices_preparing", "active": False,
                            "request_id": event.request_id})


async def _run_takeover_dialogue(ws, runtime, snapshot, item):
    from mewcode.orchestration.takeover import (
        ReturnToOrchestration,
        paused_dialogue_handoff_prompt,
        takeover_prompt,
        _PassSocket,
    )
    tool = ReturnToOrchestration()
    registry = runtime.agent.registry
    previous = registry.get(tool.name)
    was_enabled = registry.is_enabled(tool.name)
    prior_enabled_tools = {item.name for item in registry.list_tools()
                           if registry.is_enabled(item.name)}
    previous_goal = runtime.goal
    previous_conversation = runtime.conversation
    manager = getattr(runtime, "task_manager", None)
    prior_tasks = {task.id for task in manager.list_tasks()} if manager is not None else set()
    sandbox = getattr(getattr(runtime.agent, "permission_checker", None), "sandbox", None)
    previous_grants = getattr(sandbox, "file_grants", None)
    if sandbox is not None and hasattr(runtime, "file_access_grants"):
        sandbox.file_grants = runtime.file_access_grants
    if sandbox is not None and hasattr(runtime, "dir_access_grants"):
        sandbox.dir_grants = runtime.dir_access_grants
    review_window = item.get("pause_dialogue_window") if isinstance(item, dict) else None
    # A delayed handoff is a judgment over a bounded checkpoint and dialogue
    # window, not a continuation of the entire chat. Keep the original history
    # intact while the model reviews this isolated, short context.
    isolated = isinstance(review_window, dict)
    if isolated:
        runtime.conversation = ConversationManager()
    runtime.handoff_failure_kind = ""
    prompt = (
        paused_dialogue_handoff_prompt(snapshot, review_window)
        if isolated
        else takeover_prompt(snapshot)
    )
    runtime.conversation.add_system_reminder(prompt)
    reminder = runtime.conversation.history[-1]
    reminder.transient = True
    reminder.context_kind = "paused_dialogue_handoff" if isolated else "takeover_handoff"
    review_message = None
    if isolated:
        review_message = Message(
            role="user",
            content="请审阅系统提醒中的暂停区间记录，判断能否安全接回原编排，并调用 ReturnToOrchestration。",
            transient=True,
            context_kind="paused_dialogue_handoff",
        )
        runtime.conversation.history.append(review_message)
    else:
        # This message was already persisted at acceptance; append it to the
        # live context only now, after any in-flight request has completed.
        runtime.conversation.add_user_message(item["text"])
    registry.register(tool)
    registry.enable(tool.name)
    if isolated:
        for candidate in registry.list_tools():
            if candidate.name != tool.name:
                registry.disable(candidate.name)
    runtime.goal = ""
    set_dialogue_focus(runtime, "chat", task=item["text"])
    socket = _PassSocket(ws)
    try:
        for attempt in range(2 if isolated else 1):
            result = await run_chat(socket, item["text"], record_user=False, runtime=runtime)
            if manager is not None:
                child_ids = [task.id for task in manager.list_tasks() if task.id not in prior_tasks
                             and task.owner_session_id == runtime.conversation_id]
                await manager.settle_tasks(child_ids, cancel=bool(result and result.get("interrupted")))
            if not result or result.get("interrupted"):
                runtime.handoff_failure_kind = "interrupted"
                return None
            if result.get("error"):
                runtime.handoff_failure_kind = "model_error"
                return None
            if tool.decision is not None:
                return tool.decision
            if attempt == 0 and isolated:
                await ws.send_json({
                    "type": "expert_takeover_state", "run_id": str(snapshot.get("run_id") or ""),
                    "phase": "handling",
                    "text": "接力模型尚未提交结构化决定，正在精简提示后重试一次。",
                })
                runtime.conversation.history.append(Message(
                    role="user",
                    content=("你尚未调用 ReturnToOrchestration。现在只提交一次结构化决定；"
                             "证据不足就选择 hold/needs_user，不要继续执行原任务。"),
                    transient=True,
                    context_kind="paused_dialogue_handoff_retry",
                ))
        runtime.handoff_failure_kind = "no_decision"
        return None
    finally:
        runtime.goal = previous_goal
        if sandbox is not None and previous_grants is not None:
            sandbox.file_grants = previous_grants
        registry.unregister(tool.name)
        if previous is not None:
            registry.register(previous)
            if not was_enabled:
                registry.disable(previous.name)
        if isolated:
            for candidate in registry.list_tools():
                if candidate.name in prior_enabled_tools:
                    registry.enable(candidate.name)
                else:
                    registry.disable(candidate.name)
        # The handoff instructions are scoped to this side task, not future chat.
        runtime.conversation.history[:] = [
            message for message in runtime.conversation.history
            if message is not reminder and message is not review_message
        ]
        if isolated:
            previous_conversation.history.extend(
                message for message in runtime.conversation.history if not message.transient
            )
            runtime.conversation = previous_conversation


def _dialogue_handoff_context(decision: dict, explicit: str = "") -> str:
    parts = [str(explicit or "").strip(), str(decision.get("resume_context") or "").strip()]
    affected = [str(value).strip() for value in decision.get("affected_node_ids") or [] if str(value).strip()]
    if affected:
        parts.append("暂停期间的新信息可能影响节点：" + "、".join(affected))
    return "\n\n".join(part for part in parts if part)


async def _resume_expert_after_paused_dialogue(
    ws,
    runtime: ConversationRuntime,
    snapshot: dict[str, Any],
    *,
    supplemental: str = "",
    resume_after_node: str = "",
    retry_node_id: str = "",
    recovery_action: str = "",
    recovery_info: str = "",
    grant_extra_budget: bool = False,
    sync_canvas: bool = False,
) -> None:
    """Resume via the current host policy, never by reviewing dialogue slices."""
    from mewcode.orchestration.retired_handoff import retire_paused_handoff_review

    run_id = str(snapshot.get("run_id") or "")

    async def hold(text, *, plan_changed=False):
        set_dialogue_focus(runtime, "orchestration", task=str(snapshot.get("task") or ""), run_id=run_id)
        await ws.send_json({"type": "expert_takeover_state", "run_id": run_id,
            "phase": "held", "text": text, "plan_revision_required": plan_changed})
        await ws.send_json({"type": "done", "interrupted": True, "elapsed": 0,
            "run_id": run_id, "expert_status": "paused",
            "status": runtime.status_payload(), "tasks": runtime.tasks_payload()})

    def check_current(value, expected):
        allowed_statuses = {"paused", "blocked", "needs_attention"}
        if resume_after_node:
            allowed_statuses.update({"completed", "terminated"})
        if (not isinstance(value, dict) or value.get("conversation_id") != runtime.conversation_id
                or value.get("run_id") != run_id
                or str(value.get("updated_at") or "") != expected
                or value.get("status") not in allowed_statuses
                or value.get("active_node_ids")):
            raise ValueError("原编排归属、版本或执行状态已变化，请刷新后重新继续。")
        if resume_after_node:
            states = value.get("node_states") or value.get("stages") or {}
            anchor = states.get(resume_after_node) if isinstance(states, dict) else None
            if not isinstance(anchor, dict) or anchor.get("status") != "completed":
                raise ValueError("从检查点重跑需要已完成的锚点节点，请刷新后重试。")

    if getattr(runtime, "cancel_requested", False) or asyncio.current_task().cancelling():
        await hold("恢复已取消；原检查点保留。")
        return
    expected = str(snapshot.get("updated_at") or "")
    try:
        current = EXPERT_RUNS.load(run_id)
        check_current(current, expected)
        candidate = copy.deepcopy(current)
        if retire_paused_handoff_review(candidate):
            def migrate(value):
                check_current(value, expected)
                retire_paused_handoff_review(value)
            current = EXPERT_RUNS.mutate(run_id, migrate, durable=True)
        expected = str(current.get("updated_at") or "")
    except (ValueError, OSError) as exc:
        await hold(str(exc))
        return

    pending = current.get("pending_dialogue_handoff")
    if isinstance(pending, dict) and pending.get("accepted_plan_fingerprint"):
        from mewcode.orchestration.revisions import saved_execution_flow, execution_fingerprint
        try:
            latest = saved_execution_flow(runtime.work_dir,
                flow_store=_conversation_flow_store(runtime.conversation_id),
                plan_store=_conversation_plan_store(runtime.conversation_id))
        except (ValueError, TypeError):
            latest = None
        accepted = pending["accepted_plan_fingerprint"]
        if (latest is not None and execution_fingerprint(latest) == accepted
                and execution_fingerprint(current.get("locked_flow") or {}) == accepted):
            supplemental = _dialogue_handoff_context(pending, supplemental)
            pending = None
        else:
            reopened = {**pending, "action": "hold", "relation": "plan_changed",
                "summary": "已保存计划在接纳后再次变化，请重新核验并接纳修订。"}
            reopened.pop("accepted_plan_fingerprint", None)
            def reopen(value):
                check_current(value, expected)
                value["pending_dialogue_handoff"] = reopened
            try:
                EXPERT_RUNS.mutate(run_id, reopen, durable=True)
            except (ValueError, OSError) as exc:
                await hold(str(exc))
                return
            pending = reopened
    if pending:
        await hold(str(pending.get("summary") or "原编排仍有待处理的用户决定或验收问题。")
            if isinstance(pending, dict) else "原编排待处理决定格式异常，请检查原检查点。",
            plan_changed=isinstance(pending, dict) and pending.get("relation") == "plan_changed")
        return

    if not resume_after_node and not retry_node_id and not str(recovery_action or "").strip():
        from mewcode.experts.resume_advice import resume_advice
        advice = resume_advice(current)
        if advice.get("action") in {"answer_case", "retry_node", "review_cases"}:
            text = str(advice.get("reason") or "当前检查点仍需明确处理。")
            await ws.send_json({
                "type": "error", "error_code": "recovery_resume_blocked",
                "text": text, "error": text, "error_displayed": True,
                "run_id": run_id, "conversation_id": runtime.conversation_id,
                "resume_advice": advice,
                "snapshot": EXPERT_RUNS._public_value(current, include_details=False),
            })
            await ws.send_json({
                "type": "done", "interrupted": False, "rejected": True,
                "error": text, "error_code": "recovery_resume_blocked",
                "error_displayed": True, "elapsed": 0, "run_id": run_id,
                "status": runtime.status_payload(), "tasks": runtime.tasks_payload(),
            })
            return

    await ws.send_json({"type": "expert_takeover_state", "run_id": run_id,
        "phase": "returning", "text": "正在从原检查点进入宿主恢复检查。"})
    try:
        check_current(EXPERT_RUNS.load(run_id), expected)
    except (ValueError, OSError) as exc:
        await hold(str(exc))
        return
    if getattr(runtime, "cancel_requested", False) or asyncio.current_task().cancelling():
        await hold("恢复已取消；原检查点保留。")
        return
    await run_expert_chat(
        ws, str(current.get("task") or ""), session=runtime,
        content_blocks=_resume_attachment_blocks(current),
        registry_service=MODEL_REGISTRY, profile_store=EXPERT_PROFILES,
        run_store=EXPERT_RUNS, resume_run_id=run_id,
        flow_store=_conversation_flow_store(runtime.conversation_id),
        plan_store=_conversation_plan_store(runtime.conversation_id),
        pipeline_agent_factory=_pipeline_agent_factory,
        pipeline_capabilities_provider=_pipeline_capabilities,
        supplemental=supplemental,
        resume_after_node=resume_after_node,
        retry_node_id=retry_node_id,
        recovery_action=recovery_action,
        recovery_info=recovery_info,
        grant_extra_budget=grant_extra_budget,
        sync_canvas=sync_canvas,
    )


async def _control_chat_events(conversation, action):
    answer = "已终止持续任务。" if action == "terminate" else "已暂停持续任务；不会自动恢复。"
    conversation.add_assistant_message(answer)
    yield StreamText(answer)
    yield TurnComplete(turn=0)


async def run_chat(
    ws: WebSocket, text: str, *, record_user: bool = True,
    content_blocks: list[dict[str, Any]] | None = None,
    runtime: ConversationRuntime | None = None,
    continuation_task_id: str | None = None,
) -> dict:
    session = runtime or SESSION.capture_conversation_runtime()
    assert session.agent is not None
    provider = session.slot.provider if session.slot is not None else None
    detected_window = provider.get_context_window() if provider is not None else 0
    _reconcile_agent_context_window(
        session.agent, session.context_tier, detected_window
    )
    owner = getattr(session, "owner", None)
    mcp_task = getattr(owner, "_mcp_init_task", None) if owner is not None else None
    if mcp_task is None:
        mcp_task = getattr(SESSION, "_mcp_init_task", None)
    if mcp_task is not None and not mcp_task.done():
        with suppress(asyncio.CancelledError, Exception):
            await mcp_task
    # Recheck after startup awaits and before history, focus or model execution.
    if continuation_task_id is not None:
        try:
            validate_chat_continuation(session, continuation_task_id)
        except ValueError as exc:
            await ws.send_json({"type": "error", "text": str(exc)})
            await ws.send_json({"type": "done", "rejected": True, "interrupted": False, "elapsed": 0})
            return {"rejected": True}
    # 用量台账基准：本轮结束后用差值记一条事件
    in0 = session.agent.total_input_tokens
    if getattr(session, "orchestration_planning", False) or getattr(session, "expert_takeover_controller", None):
        from frontend.workbench_revision import sync_tool
        sync_tool(sys.modules[__name__], agent=session.agent, project=session.work_dir, cid=session.conversation_id)
    out0 = session.agent.total_output_tokens
    hist0 = len(session.conversation.history)
    # Repair any in-memory tail left by an older interrupted run before a new
    # user turn is appended. Agent.run performs the same repair in its finally
    # block for all current runs.
    session.conversation.repair_incomplete_tool_turns()
    permission_mode = getattr(session.agent, "permission_mode", PermissionMode.DEFAULT)
    auto_review = bool(
        permission_mode == PermissionMode.AUTO
        and getattr(session, "multi_agent", False)
        and session.agent.registry.is_enabled("Agent")
    )
    replace_plan_reminder(
        session.conversation,
        AUTO_REVIEW_MARKER,
        AUTO_REVIEW_REMINDER if auto_review else "",
    )
    if getattr(session, "orchestration_planning", False):
        refresh_chat_plan(session.conversation, session.work_dir,
                          plan_store=_conversation_plan_store(session.conversation_id))
        from mewcode.orchestration.workflow_blocks import WORKFLOW_BLOCK_GUIDANCE
        replace_plan_reminder(session.conversation, "workflow-block-materials", WORKFLOW_BLOCK_GUIDANCE)
    else:
        replace_plan_reminder(session.conversation, CHAT_PLAN_MARKER, "")
        replace_plan_reminder(session.conversation, "workflow-block-materials", "")
    from mewcode.orchestration.task_design import TaskDesign, MARKER as DESIGN_MARKER, task_reminder, host_task_control
    design_service = TaskDesign(session.work_dir, session.conversation_id) if getattr(session, "orchestration_planning", False) else None
    task_design = design_service.read() if design_service else None
    if task_design and record_user:
        task_design = design_service.record_turn(text, control=host_task_control(
            text, has_attachments=bool(content_blocks)))
    replace_plan_reminder(session.conversation, DESIGN_MARKER, task_reminder(task_design))
    try:
        if ((getattr(session, "orchestration_planning", False) or getattr(session, "expert_takeover_controller", None))
                and getattr(session, "conversation_id", "") and session.conversation_id == _active_conversation_id()
                and Path(session.work_dir).resolve() == Path(SESSION.work_dir).resolve()):
            refresh_canvas_nodes(session.conversation,
                                 await asyncio.to_thread(_canvas_nodes_text, session))
        else:
            refresh_canvas_nodes(session.conversation, "")
    except Exception:
        # The reminder is advisory; a store hiccup must never break a turn.
        log.warning("failed to refresh canvas node reminder", exc_info=True)
        try:
            refresh_canvas_nodes(session.conversation, "")
        except Exception:
            pass
    # Restored messages keep only lightweight attachment references. Resolve
    # them just before the provider request so disk history never contains
    # document bodies or image base64.
    for historical in session.conversation.history:
        if any(block.get("type") == "attachment" for block in historical.content_blocks):
            historical.content_blocks = ATTACHMENTS.materialize(historical.content_blocks)
    continuous = False
    if (record_user and continuation_task_id is None and not task_design
            and getattr(session, "expert_takeover_controller", None) is None):
        continuous = prepare_continuous_turn(session, text)
    from mewcode.continuation import MARKER as CONTINUATION_MARKER
    replace_plan_reminder(session.conversation, CONTINUATION_MARKER,
        ("当前有持续目标。阶段完成不等于目标完成，结束前由宿主审核。不要重做已完成工作。"
         "用户当前插话优先；原目标暂停或终止时禁止自动恢复，权限与预算不变。\n原始目标："
         + continuation_state(session)["objective"]) if continuous else "")
    if record_user:
        try:
            if task_design and task_design.get("status") == "designing":
                from mewcode.orchestration.chat_routing import set_dialogue_focus
                set_dialogue_focus(session, "chat", task=task_design["goal"], task_id=task_design["task_id"])
                continuation_task_id = task_design["task_id"]
            prepare_chat_focus(session, continuation_state(session)["objective"] if continuous else text,
                               continuation_task_id=continuation_task_id, latest_request=text)
        except ValueError as exc:
            await ws.send_json({"type": "error", "text": str(exc)})
            await ws.send_json({"type": "done", "rejected": True, "interrupted": False, "elapsed": 0})
            return {"rejected": True}
        session.conversation.add_user_message(text, content_blocks=content_blocks)
        if session.session is not None:
            # Persist lightweight attachment refs (id + metadata) instead of
            # materialized bytes so reload can still render previews while the
            # disk history never stores document bodies or image base64.
            stored_blocks: list[dict[str, Any]] = []
            for block in content_blocks or []:
                attachment_id = str(block.get("attachment_id") or block.get("id") or "")
                if attachment_id:
                    stored_blocks.append({
                        "type": "attachment",
                        "attachment_id": attachment_id,
                        "name": str(block.get("name") or ""),
                    })
            session.session.append(Message(
                role="user", content=text,
                content_blocks=stored_blocks,
            ))
    from frontend.chat_history_persistence import ChatHistoryPersistence
    persisted_history = ChatHistoryPersistence(session.conversation.history)

    def persist_new_history() -> None:
        if session.session is None:
            return
        persisted_history.persist(session.conversation.history, session.session.append)
    start = time.monotonic()
    interrupted = False
    error_text: str | None = None
    last_ctx = 0
    chat_tool_args: dict[str, dict[str, Any]] = {}
    activity_task_id = dialogue_focus(session).get("task_id") or session.conversation_id
    session.agent._chat_task_id = activity_task_id
    session.agent._chat_turn_id = ((dialogue_focus(session).get("turn_id") if record_user else "")
                                  or secrets.token_hex(16))
    manager = getattr(session, "task_manager", None)
    list_tasks = getattr(manager, "list_tasks", None)
    session.agent._chat_turn_baseline_task_ids = frozenset(
        task.id for task in list_tasks()
    ) if callable(list_tasks) else frozenset()
    session.agent._chat_task_title = str(dialogue_focus(session).get("task") or text).strip()[:200]
    bind_task_permission_scope(session, task_id=activity_task_id)
    from mewcode.workflow_core.chat_tools import install as install_chat_workflow
    install_chat_workflow(sys.modules[__name__], session)
    activity_run_id = "chat:" + activity_task_id
    async def observe_activity(event):
        await asyncio.to_thread(record_pipeline_activity, session.work_dir, event)
    pipeline = PipelineRecorder(
        conversation_id=session.conversation_id,
        store=PipelineEventStore.for_session(session.session),
        publish=ws.send_json,
        task_id=activity_task_id,
        observe=observe_activity,
    )
    await pipeline.lifecycle("running", "正在处理当前对话任务")
    if record_user:
        await pipeline.begin_turn(
            text,
            attachments=ATTACHMENTS.display_refs(content_blocks or []),
        )

    controller = None
    old_completion_gate = getattr(session.agent, "completion_gate", None)
    if continuous:
        controller = ContinuousChat(session, make_reviewer(
            MODEL_REGISTRY, getattr(session.client, "get_usage_recorder", lambda: None)(),
        ), ws.send_json)
        session.continuation_controller = controller
        session.agent.completion_gate = controller.gate
    try:
        if controller:
            await controller.announce()
        control = control_intent(text) if record_user else ""
        events = (_control_chat_events(session.conversation, control)
                  if control and not re.search(r"[，,。]\s*\S", text)
                  else session.agent.run(session.conversation))
        async with aclosing(events):
            async for event in events:
                if isinstance(event, StreamText):
                    await pipeline.assistant_text(event.text)
                    await ws.send_json({"type": "delta", "text": event.text})
                elif isinstance(event, ThinkingText):
                    await pipeline.assistant_thinking(event.text)
                    await ws.send_json({"type": "thinking", "text": event.text})
                elif isinstance(event, ToolUseEvent):
                    if isinstance(event.arguments, dict):
                        chat_tool_args[event.tool_id] = event.arguments
                    await pipeline.tool_start(event.tool_id, event.tool_name, event.arguments)
                    await ws.send_json({
                        "type": "tool", "id": event.tool_id,
                        "name": event.tool_name, "args": event.arguments,
                    })
                elif isinstance(event, ToolResultEvent):
                    if controller:
                        controller.evidence(event.tool_id, event.tool_name, event.output, event.is_error)
                    arguments = chat_tool_args.pop(event.tool_id, {})
                    if tool_writes_files(event.tool_name):
                        try:
                            from mewcode.orchestration.process_files import capture_process_file
                            receipt = capture_process_file(tool_name=event.tool_name, arguments=arguments,
                                is_error=event.is_error, work_dir=session.work_dir,
                                sandbox=getattr(getattr(session.agent, "permission_checker", None), "sandbox", None),
                                node_id="", tool_id=event.tool_id, attempt=1)
                            if receipt:
                                ActivityStore(session.work_dir).artifact(session.conversation_id,
                                    activity_run_id, receipt, turn_id=pipeline.turn_id)
                        except Exception:
                            log.warning("failed to capture automatic activity file", exc_info=True)
                    if event.tool_name == "report_step":
                        # Chat progress is mirrored onto the orchestration canvas so
                        # the same node badges / process files show up in chat mode.
                        try:
                            _mirror_chat_report_step(arguments=arguments, output=event.output, runtime=session)
                        except Exception:
                            log.warning("failed to mirror chat report_step", exc_info=True)
                    elif tool_writes_files(event.tool_name):
                        try:
                            _capture_chat_process_file(
                                runtime=session,
                                tool_name=event.tool_name,
                                arguments=arguments,
                                is_error=event.is_error,
                                tool_id=event.tool_id,
                                sandbox=getattr(
                                    getattr(session.agent, "permission_checker", None),
                                    "sandbox",
                                    None,
                                ),
                            )
                        except Exception:
                            log.warning("failed to capture chat process file", exc_info=True)
                    await pipeline.tool_result(
                        event.tool_id,
                        event.output,
                        is_error=event.is_error,
                        elapsed=event.elapsed,
                    )
                    await ws.send_json({
                        "type": "tool_result", "id": event.tool_id,
                        "output": event.output[:4000],
                        "is_error": event.is_error,
                        "elapsed": round(event.elapsed, 2),
                        "dispatch_seconds": (round(event.dispatch_seconds, 2)
                                             if event.dispatch_seconds is not None else None),
                    })
                elif isinstance(event, PermissionRequest):
                    reason = getattr(event, "reason", "") or ""
                    await pipeline.lifecycle("waiting", "等待你确认操作权限")
                    loop = asyncio.get_running_loop()
                    from mewcode.orchestration.inbox import permission_identity
                    session.permission_request = permission_identity(session, {
                        "tool": event.tool_name, "description": event.description,
                        "reason": reason,
                        "source": event.source,
                        "directory_scope": event.directory_scope,
                        "directory_ttl_seconds": event.directory_ttl_seconds,
                    })
                    session._perm_future = loop.create_future()
                    session.permission_request_future = session._perm_future
                    try:
                        await ws.send_json({"type": "permission", **session.permission_request})
                        response = await session._perm_future
                    finally:
                        session._perm_future = None
                        session.permission_request = {}
                    if not event.future.done():
                        event.future.set_result(response)
                    await pipeline.lifecycle("running", "已收到权限答复，继续处理")
                elif isinstance(event, HumanOnCallRequest):
                    await _prepare_chat_choice_event(session, event, ws)
                    await pipeline.lifecycle("waiting", "等待你回答问题")
                    session._human_oncall_request = event
                    await ws.send_json({
                        "type": "human_oncall",
                        "request_id": event.request_id,
                        'task_id': (getattr(event, 'task_design_binding', None) or {}).get('task_id', ''),
                        "tool": event.tool_name,
                        "questions": event.questions,
                        **({"choice_bundle": event.choice_bundle} if event.choice_bundle else {}),
                    })
                    try:
                        await event.future
                    finally:
                        if session._human_oncall_request is event:
                            session._human_oncall_request = None
                    await pipeline.lifecycle("running", "已收到答复，继续处理")
                elif isinstance(event, AskUserRequest):
                    await _prepare_chat_choice_event(session, event, ws)
                    await pipeline.lifecycle("waiting", "等待你补充必要信息")
                    session._ask_user_request = event
                    await ws.send_json({
                        "type": "ask_user",
                        "request_id": event.request_id,
                        'task_id': (getattr(event, 'task_design_binding', None) or {}).get('task_id', ''),
                        "tool": event.tool_name,
                        "questions": event.questions,
                        **({"choice_bundle": event.choice_bundle} if event.choice_bundle else {}),
                    })
                    try:
                        await event.future
                    finally:
                        if session._ask_user_request is event:
                            session._ask_user_request = None
                    await pipeline.lifecycle("running", "已收到补充信息，继续处理")
                elif isinstance(event, RetryEvent):
                    await ws.send_json({"type": "note", "text": f"↻ 重试: {event.reason}"})
                elif isinstance(event, StreamRetryEvent):
                    await ws.send_json({
                        "type": "note",
                        "text": (
                            f"↻ 网络抖动，{event.wait:.0f}s 后重试 "
                            f"({event.attempt}/{event.max_attempts}): {event.reason}"
                        ),
                    })
                elif isinstance(event, CompactNotification):
                    await ws.send_json({"type": "note", "text": event.message})
                    # auto_compact 已原地重写 conversation.history：持久化 boundary
                    # 记录（resume 据此重建压缩态），并把刷盘游标重置到新历史末尾，
                    # 否则旧游标越界导致后续消息静默不落盘。
                    if session.session is not None and event.boundary is not None:
                        session.session.append_record(
                            make_compact_boundary(
                                event.boundary.summary, event.boundary.keep
                            )
                        )
                    persisted_history.reset(session.conversation.history)
                elif isinstance(event, UsageEvent):
                    last_ctx = event.request_input_tokens
                    prompt_tokens = (
                        event.input_tokens
                        + event.cache_read_tokens
                        + event.cache_creation_tokens
                    )
                    await ws.send_json({
                        "type": "usage",
                        "ctx": event.request_input_tokens,
                        "window": _reconcile_agent_context_window(
                            session.agent,
                            session.context_tier,
                            (session.slot.provider.get_context_window()
                             if session.slot is not None else 0),
                        ),
                        "total_in": event.input_tokens,
                        "total_out": event.output_tokens,
                        "cache_read": event.cache_read_tokens,
                        "cache_creation": event.cache_creation_tokens,
                        "cache_usage_available": event.cache_usage_available,
                        "prompt_tokens": prompt_tokens,
                        "cache_hit_rate": (
                            event.cache_read_tokens / prompt_tokens
                            if event.cache_usage_available and prompt_tokens
                            else (0.0 if event.cache_usage_available else None)
                        ),
                        "cache_miss": event.input_tokens,
                        "cache_request_read": event.request_cache_read_tokens,
                        "cache_request_miss": event.request_cache_miss_tokens,
                        "cache_request_creation": event.request_cache_creation_tokens,
                        "cache_request_hit_rate": (
                            event.request_cache_hit_rate
                            if event.request_cache_usage_available
                            else None
                        ),
                        "cache_request_usage_available": (
                            event.request_cache_usage_available
                        ),
                        "cache_cumulative_read": event.cumulative_cache_hit_tokens,
                        "cache_cumulative_miss": event.cumulative_cache_miss_tokens,
                        "cache_cumulative_creation": (
                            event.cumulative_cache_creation_tokens
                        ),
                        "cache_cumulative_hit_rate": (
                            event.cumulative_cache_hit_rate
                            if event.cumulative_cache_usage_available
                            else None
                        ),
                        "cache_cumulative_usage_available": (
                            event.cumulative_cache_usage_available
                        ),
                        "cache_slot": event.cache_slot,
                        "cache_provider": event.cache_provider,
                        "cache_type": event.cache_type,
                        "cache_epoch": event.cache_epoch,
                        "cache_invalidation_reason": event.cache_invalidation_reason,
                        "cache_request_cost_usd": event.request_cost_usd,
                        "cache_cost_usd": event.total_cost_usd,
                        "cache_system_hash": event.system_hash,
                        "cache_tools_hash": event.tools_hash,
                        "cache_lcp_tokens": event.longest_common_prefix_tokens,
                    })
                elif isinstance(event, ErrorEvent):
                    error_text = event.message
                    message = event.message
                    if event.kind == "max_iterations":
                        message = (
                            f"已达到单轮最大迭代次数（{session.agent.max_iterations}），"
                            "本轮已安全停止。可发送「继续」从当前对话上下文接着处理。"
                        )
                    await ws.send_json({"type": "error", "text": message})
                elif isinstance(event, (TurnComplete, LoopComplete)):
                    # 持久化本轮新增的历史消息
                    persist_new_history()
    except asyncio.CancelledError:
        interrupted = True
    except Exception as e:
        error_text = str(e)
    finally:
        if controller:
            session.agent.completion_gate = old_completion_gate
            session.continuation_controller = None
            session.continuation_side_turn = False
            if not getattr(session, "continuation_intervention", False):
                state = continuation_state(session)
                if state.get("status") in {"running", "reviewing"}:
                    hold_continuation(session, error_text or "运行已中断；检查点已保留，未判定完成")

    if controller and continuation_state(session).get("status") != "complete":
        interrupted = True
        await controller.announce()

    await pipeline.finish(interrupted=interrupted, error=error_text)

    # Agent cancellation is repaired before its async generator closes. Flush
    # the complete assistant/tool-result tail even when no TurnComplete event
    # was emitted, so a restart cannot resurrect a broken chain.
    session.conversation.repair_incomplete_tool_turns()
    persist_new_history()

    # 本轮 token 增量：会话 meta 累加 + 用量台账各记一笔
    d_in = session.agent.total_input_tokens - in0
    d_out = session.agent.total_output_tokens - out0

    if session.session is not None:
        # 累加增量而非覆写 agent 终身累计值（后者跨会话，会把别的
        # 会话的用量也算进来），并立即落盘——否则 total_tokens 要等
        # 下一轮 append 才顺带保存，单轮会话 / 重启前最后一轮永远是 0
        session.session.meta.total_tokens += d_in + d_out
        try:
            session.session.save_meta()
        except Exception:
            pass

    # 记录本轮 token 用量到台账（右上角用量统计面板的数据源）
    if (
        (d_in > 0 or d_out > 0)
        and (
            session.client is None
            or session.client.get_usage_recorder() is None
        )
    ):
        provider = session.slot.provider if session.slot else None
        _append_usage_event(
            model=provider.model if provider else "",
            input_tokens=d_in,
            output_tokens=d_out,
            session_id=session.session.session_id if session.session else "",
            messages=len(session.conversation.history) - hist0,
            provider_name=provider.name if provider else "",
            protocol=provider.protocol if provider else "",
            base_url=provider.base_url if provider else "",
        )

    elapsed = time.monotonic() - start
    status = session.status_payload()
    await pipeline.lifecycle("failed" if error_text else "paused" if interrupted else "completed",
        error_text or (continuation_state(session).get("reason") if controller else None)
        or ("对话任务已暂停" if interrupted else "本轮对话处理结束；节点验收状态单独记录"))
    # 本条回复的上下文/模型/effort 快照（展示在消息右下角，可点击修改）
    status["ctx"] = last_ctx
    file_history = getattr(session.agent, "file_history", None) if session.agent else None
    changes = list(getattr(file_history, "last_turn_changes", None) or [])
    if file_history is not None and not changes:
        pending = file_history.describe_turn_changes()
        if pending:
            file_history.make_snapshot(
                len(session.conversation.history),
                "interrupted" if interrupted else "turn",
            )
            changes = list(file_history.last_turn_changes)
    turn_checkpoint = None
    try:
        # Only a side turn actually owned by a live takeover may annotate its
        # run. Unrelated ordinary chat must not mutate an old checkpoint.
        run_id = str(getattr(session, "expert_run_id", "") or "")
        owned = getattr(session, "expert_takeover_controller", None) is not None
        related = EXPERT_RUNS.load(run_id) if owned and run_id else None
        if related and related.get("conversation_id") == session.conversation_id and related.get("status") != "running":
            turn_checkpoint = EXPERT_RUNS.save_turn_checkpoint(
                related["run_id"], kind="conversation_turn", turn_key="chat:" + pipeline.turn_id,
            )
    except (OSError, ValueError) as exc:
        await ws.send_json({"type": "error", "text": f"本轮对话已保存，但编排检查点保存失败：{exc}"})
    try:
        await ws.send_json({
            "type": "done",
            "interrupted": interrupted,
            "error": error_text,
            "elapsed": round(elapsed, 1),
            "status": status,
            "tasks": session.tasks_payload(),
            "changes": changes,
            "checkpoint": turn_checkpoint,
        })
    except Exception:
        pass  # 连接已断开，结果丢弃即可
    return {"interrupted": interrupted, "error": error_text}


async def start_runtime_turn(
    runtime: ConversationRuntime, text: str, *, resume_run_id: str = "",
    retry_node_id: str = "",
    confirmed_new_flow: ExpertFlowSpec | None = None,
    queued_turn: QueuedChatTurn | None = None,
    _reserved: bool = False,
    task_design_dispatch: dict | None = None,
) -> None:
    """Start or queue one turn without changing the conversation visible in UI."""
    orchestration = bool(resume_run_id or confirmed_new_flow is not None)
    if orchestration:
        mode = getattr(runtime, "orchestration_permission_mode", None)
        if mode:
            _apply_runtime_permission_mode(runtime, mode)
    if runtime.running() and not _reserved:
        if resume_run_id or confirmed_new_flow is not None:
            raise ValueError("当前任务仍在运行，请暂停或等待完成后再确认")
        runtime.queued_prompts.append(text)
        return
    checkpoint = EXPERT_RUNS.load(resume_run_id) if resume_run_id else None
    if resume_run_id and (
        checkpoint is None or checkpoint.get("conversation_id") != runtime.conversation_id
    ):
        raise ValueError("原任务检查点不存在或不属于当前对话，未启动任务")
    scoped_ws = ConversationWebSocket(WS_EVENTS, runtime.conversation_id)
    attachment_refs: list[dict[str, Any]] = []
    content_blocks: list[dict[str, Any]] | None = None
    if queued_turn is not None and queued_turn.attachment_ids:
        attachment_refs = ATTACHMENTS.references(
            runtime.conversation_id, queued_turn.attachment_ids
        )
        content_blocks = ATTACHMENTS.materialize(attachment_refs)
    runtime.turn_history_start = len(runtime.conversation.history)
    runtime.execution_kind = "orchestration" if orchestration else "chat"
    runtime.owner.begin_conversation_turn(runtime)
    start_token = getattr(runtime, "_turn_start_token", None) or object()
    runtime._turn_start_token = runtime._turn_start_owner_token = start_token
    runtime._turn_start_owner = asyncio.current_task()
    previous_task = getattr(runtime, "task", None)
    worker_created = False
    try:
        await scoped_ws.send_json({
            "type": "turn_started",
            "text": text,
            "attachments": ATTACHMENTS.display_refs(attachment_refs),
            "queued": True,
            "expert_mode": orchestration,
            "client_echoed": False,
            "resume_run_id": resume_run_id,
        })
        if orchestration:
            from contextlib import nullcontext
            from mewcode.orchestration.task_design import TaskDesign
            if task_design_dispatch and (runtime.conversation_id != _active_conversation_id()
                    or Path(runtime.work_dir).resolve() != Path(SESSION.work_dir).resolve()):
                raise ValueError("任务所属项目或对话已切换，未派发业务执行。")
            fence = (TaskDesign(runtime.work_dir, runtime.conversation_id).dispatch_guard(
                task_design_dispatch, resume_run_id=resume_run_id) if task_design_dispatch else nullcontext())
            with fence:
                task = asyncio.create_task(run_expert_chat(
                scoped_ws,
                str(checkpoint.get("task") or text) if checkpoint else text,
                content_blocks=(
                    _resume_attachment_blocks(checkpoint) if checkpoint
                    else content_blocks
                ),
                resume_run_id=resume_run_id,
                **({'retry_node_id': retry_node_id} if retry_node_id else {}),
                confirmed_new_flow=confirmed_new_flow,
                supplemental=text if checkpoint else "",
                session=runtime,
                registry_service=MODEL_REGISTRY,
                profile_store=EXPERT_PROFILES,
                run_store=EXPERT_RUNS,
                flow_store=_conversation_flow_store(runtime.conversation_id),
                plan_store=_conversation_plan_store(runtime.conversation_id),
                pipeline_agent_factory=_pipeline_agent_factory,
                pipeline_capabilities_provider=_pipeline_capabilities,
                **({"task_design_dispatch": task_design_dispatch} if task_design_dispatch else {}),
                ))
                worker_created = True
                runtime._turn_start_token = None
                runtime.owner.attach_conversation_task(runtime, task)
        else:
            task = asyncio.create_task(run_chat(
                scoped_ws, text, content_blocks=content_blocks, runtime=runtime
            ))
            worker_created = True
            runtime._turn_start_token = None
            runtime.owner.attach_conversation_task(runtime, task)
    except BaseException as exc:
        if task_design_dispatch and not worker_created:
            from mewcode.orchestration.task_design import TaskDesign
            try:
                TaskDesign(runtime.work_dir, runtime.conversation_id).record_execution_not_started(
                    task_design_dispatch, str(exc) or "启动已取消，尚未创建业务任务。")
            except (OSError, ValueError):
                pass  # An uncertain receipt stays fail-closed rather than authorizing a retry.
        if (getattr(runtime, "_turn_start_token", None) is start_token
                and getattr(runtime, "task", None) is previous_task):
            runtime.starting = False
            # The renderer has already seen turn_started. Release only this
            # reservation; a newer turn owns its own token and completion.
            try:
                await scoped_ws.send_json({"type": "done", "rejected": True,
                                           "interrupted": False, "elapsed": 0})
            except (asyncio.CancelledError, Exception):
                pass
        raise


async def _start_dequeued_runtime_turn(
    runtime: ConversationRuntime, prompt: str | QueuedChatTurn
) -> None:
    """Publish a queue pop and safely launch the reserved next turn."""

    scoped_ws = ConversationWebSocket(WS_EVENTS, runtime.conversation_id)
    try:
        await scoped_ws.send_json({
            "type": "chat_queue",
            "items": runtime.chat_queue_payload(),
            "revision": runtime.owner._current_chat_queue_revision(runtime),
            "continuing": True,
        })
        queued_turn = prompt if isinstance(prompt, QueuedChatTurn) else None
        runtime.continuation_intervention = bool(queued_turn and queued_turn.continuation)
        text = queued_turn.text if queued_turn is not None else str(prompt)
        await start_runtime_turn(
            runtime,
            text,
            queued_turn=queued_turn,
            _reserved=True,
        )
    except Exception as exc:
        runtime.starting = False
        await scoped_ws.send_json({
            "type": "error",
            "text": f"等待消息发送失败：{exc}",
        })
        if not runtime.queued_prompts:
            await scoped_ws.send_json({
                "type": "done",
                "interrupted": False,
                "rejected": True,
                "elapsed": 0,
                "status": runtime.status_payload(),
                "tasks": runtime.tasks_payload(),
            })
        runtime.owner._continue_conversation_queue(runtime)


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------


async def _select_startup_provider(
    session: WebSession, conversation_restored: bool
) -> str | None:
    saved_route: dict[str, Any] = {}
    if conversation_restored and session.session is not None:
        candidate = session.conversation_settings.get(
            session.session.session_id, {}
        )
        if isinstance(candidate, dict):
            saved_route = candidate
    if session._has_explicit_conversation_route(saved_route):
        if session.agent is None:
            return (
                getattr(session, "provider_restore_error", "")
                or "Saved conversation provider is not ready; select its provider explicitly."
            )
        return None

    last_key = session.web_state.get("last_provider", "")
    keys = [last_key] + [slot.key for slot in session.slots]
    for key in keys:
        slot = next((candidate for candidate in session.slots if candidate.key == key), None)
        if slot is None or validate_slot(slot) is not None:
            continue
        error = await session.use_slot(key)
        if error is None:
            print(f"Provider: {slot.label}")
            return None
    if session.agent is None:
        return "未找到可用 Provider 插槽，请在页面左上角手动选择"
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    from mewcode.sandbox.transaction_registry import TransactionRegistry
    try:
        app.state.native_transactions = TransactionRegistry().startup_inventory()
    except (OSError, ValueError, RuntimeError):
        logging.exception("Native lifecycle inventory unavailable; workspace write fence remains closed")
        app.state.native_transactions = {"state": "unavailable"}
    await asyncio.to_thread(EXPERT_RUNS.recover_stale_runs)
    try:
        await asyncio.to_thread(EXPERT_RUNS.backfill_latest_checkpoints)
    except (OSError, ValueError):
        logging.exception("Unable to backfill latest orchestration checkpoints")
    await asyncio.to_thread(EXPERT_RUNS.refresh_public_summaries)
    """在 uvicorn 的事件循环内完成初始化——避免 asyncio.run 提前关闭循环
    导致 context window 探测被取消、httpx 客户端清理报异常。"""
    Path(".mewcode").mkdir(parents=True, exist_ok=True)
    USAGE_RUNTIME.import_legacy_desktop_usage(USAGE_FILE)
    USAGE_RUNTIME.set_event_callback(WS_EVENTS.send_json)
    APP_RUNTIME_SCHEDULER.set_event_callback(WS_EVENTS.send_json)
    _register_project(os.getcwd())
    _register_project(SESSION.work_dir)
    last = SESSION.web_state.get("last_conversation") or {}
    if last.get("scope", "project") == "project" and last.get("dir"):
        await SESSION.aactivate_project(last["dir"])
    SESSION.scan_slots()
    codex_account = discover_codex_usage_account(
        linked_provider_keys=[slot.key for slot in SESSION.slots if slot.source == "codex"]
    )
    if codex_account is not None:
        USAGE_RUNTIME.upsert_managed_account(codex_account)
    USAGE_RUNTIME.upsert_managed_account(UsageAccount(
        id="managed:deepseek-portal",
        name="DeepSeek 官方登录",
        adapter_id="deepseek_portal",
        linked_provider_keys=[
            slot.key for slot in SESSION.slots
            if slot.provider.provider_type != "opencode-go"
            and (
                "deepseek" in slot.provider.name.lower()
                or "deepseek" in slot.provider.model.lower()
            )
        ],
        managed=True,
        source_kind="provider_portal",
    ))
    for slot in SESSION.slots:
        if slot.provider.provider_type == "opencode-go":
            # Go has no stable account/quota API. Keep only its local ledger.
            continue
        if not slot.provider.usage.account_tracking:
            continue
        legacy = LEGACY_USAGE_SERVICE.account_from_legacy_provider(
            slot.provider, provider_key=slot.key
        )
        if USAGE_RUNTIME.get_account(legacy.id) is None:
            USAGE_RUNTIME.save_account(
                legacy.to_dict(), secret_values=legacy.secret_values
            )
    asyncio.create_task(MODEL_INTELLIGENCE.get())
    # 新建项目级会话作为初始对话（若存在最近会话可恢复）
    restored = False
    if last.get("id"):
        restored = SESSION.load_conversation(
            last.get("scope", "project"), last["id"]
        ) is not None
    if not restored:
        SESSION.new_conversation("project")

    # Restored conversations keep their saved provider/model route, including
    # an unavailable route that must fail closed instead of falling back.
    startup_provider_error = await _select_startup_provider(SESSION, restored)
    if startup_provider_error:
        print(startup_provider_error)
    start_saved_graph = os.environ.pop('MEWCODE_START_SAVED_GRAPH', '')
    if start_saved_graph:
        from frontend.orch_startup import activate_saved_graph
        async def confirm_startup_graph(payload):
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            async def receive():
                return {'type':'http.request', 'body':body, 'more_body':False}
            request = Request({'type':'http', 'method':'POST', 'path':'/api/orchestration/confirm',
                'headers':[], 'query_string':b''}, receive)
            response = await api_orchestration_confirm(request)
            return {**json.loads(response.body), 'http_status':response.status_code}
        plan = _conversation_plan_store().get(SESSION.work_dir)
        await activate_saved_graph(start_saved_graph, project=SESSION.work_dir,
            conversation_id=_active_conversation_id(), saved_spec=plan.spec if plan else None,
            list_runs=EXPERT_RUNS.list_for_conversation, confirm=confirm_startup_graph,
            permission_mode=SESSION.permission_mode.value)
    resume_gate = os.environ.pop('MEWCODE_RESUME_PENDING_GATE', '')
    if resume_gate:
        from frontend.orch_startup import resume_pending_gate
        await resume_pending_gate(resume_gate, project=SESSION.work_dir,
            conversation_id=_active_conversation_id(), load_run=EXPERT_RUNS.load,
            resume=launch_expert_resume)
    retry_graph = os.environ.pop('MEWCODE_RETRY_GRAPH_GENERATION', '')
    if retry_graph:
        from frontend.orch_startup import retry_graph_generation
        from mewcode.orchestration.draft_store import DraftStore
        async def update_startup_graph(payload):
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            async def receive():
                return {'type': 'http.request', 'body': body, 'more_body': False}
            request = Request({'type': 'http', 'method': 'POST', 'path': '/api/orchestration/graph-autobuild/update',
                'headers': [], 'query_string': b''}, receive)
            endpoint = next(route.endpoint for route in app.routes if getattr(route, 'path', '') == request.url.path
                            and 'POST' in (getattr(route, 'methods', None) or set()))
            response = await endpoint(request)
            value = json.loads(response.body)
            return {'http_status': response.status_code, 'error': value.get('error')}
        await retry_graph_generation(retry_graph, project=SESSION.work_dir,
            conversation_id=_active_conversation_id(), read_state=DraftStore.read_state,
            selected_route=_orchestration_attempt_context, update=update_startup_graph)
    if _backend_listen_port():
        published = await asyncio.to_thread(_publish_pairing_https)
        if published.get("https_published"):
            print(f"Tailscale HTTPS published at {published.get('proxy')}", flush=True)
        else:
            print(f"Tailscale HTTPS not published: {published.get('https_error')}", flush=True)
    external_stop = asyncio.Event()
    await asyncio.to_thread(EXTERNAL_AGENTS.refresh)
    external_watch_task = asyncio.create_task(
        EXTERNAL_AGENTS.watch(WS_EVENTS.send_json, external_stop)
    )

    async def _cc_switch_watch() -> None:
        while not external_stop.is_set():
            try:
                await asyncio.wait_for(external_stop.wait(), timeout=4.0)
                return
            except asyncio.TimeoutError:
                pass
            try:
                applied = await asyncio.to_thread(SESSION.sync_cc_switch_mirror)
                if applied:
                    await WS_EVENTS.send_json({
                        "type": "state",
                        **SESSION.client_state_payload(),
                    })
            except Exception:
                log.exception("cc-switch silent mirror failed")

    mirror_task = asyncio.create_task(_cc_switch_watch())
    browser_launch_file = os.environ.pop("MEWCODE_BROWSER_LAUNCH_FILE", "")
    if browser_launch_file and DESKTOP_TOKEN and _backend_listen_port():
        try:
            BROWSER_LAUNCHES.publish_startup(browser_launch_file, _backend_listen_port())
        except OSError:
            logging.warning("Unable to write browser launch handoff; use the desktop sidebar instead")
    try:
        yield
    finally:
        external_stop.set()
        external_watch_task.cancel()
        mirror_task.cancel()
        with suppress(asyncio.CancelledError):
            await external_watch_task
        with suppress(asyncio.CancelledError):
            await mirror_task
        APP_RUNTIME_SCHEDULER.set_event_callback(None)
        USAGE_RUNTIME.set_event_callback(None)
        team_host = getattr(app.state, 'team_v2_host', None)
        if team_host is not None:
            await team_host.aclose()
        try:
            await APP_HOST.aclose()
        finally:
            # No reusable elevated metadata connection survives host shutdown.
            # Active task cancellation/settlement runs before pipe closure.
            from mewcode.sandbox.metadata_broker import close_session
            close_session()


app = FastAPI(title="MewCode Web", lifespan=lifespan)


def _install_external_web_apis() -> None:
    """Mount route installers contributed by the active session plugins."""
    for service in SESSION.context.slots.resolve("web.apis") or []:
        installer = getattr(service, "install", None)
        if not callable(installer):
            continue
        try:
            installer(app, SESSION)
        except Exception:
            log.exception("external web API failed to install")


_install_external_web_apis()


def _request_has_desktop_token(request: Request) -> bool:
    if not DESKTOP_TOKEN:
        return False
    authorization = request.headers.get("authorization", "")
    bearer = authorization[7:] if authorization.lower().startswith("bearer ") else ""
    cookie = request.cookies.get(DESKTOP_COOKIE, "")
    return any(
        token and secrets.compare_digest(token, DESKTOP_TOKEN)
        for token in (bearer, cookie)
    )


def _request_has_desktop_bearer(request: Request) -> bool:
    """Authenticate main-process-only APIs without accepting renderer cookies."""
    if not DESKTOP_TOKEN:
        return False
    authorization = request.headers.get("authorization", "")
    bearer = authorization[7:] if authorization.lower().startswith("bearer ") else ""
    return bool(bearer and secrets.compare_digest(bearer, DESKTOP_TOKEN))


def _request_has_remote_token(request: Request) -> bool:
    authorization = request.headers.get("authorization", "")
    bearer = authorization[7:] if authorization.lower().startswith("bearer ") else ""
    if bearer:
        try:
            if REMOTE_PAIRING.validate_token(bearer) is not None:
                return True
        except PairingError:
            # A damaged pairing state must fail closed without ever including
            # a caller-provided bearer value in logs or error responses.
            return False
    candidates = [os.environ.get("MEWCODE_REMOTE_TOKEN", "")]
    try:
        candidates.extend(
            os.environ.get(str(item.get("token_env") or ""), "")
            for item in _tailscale_config().remote_nodes
        )
    except Exception:
        pass
    return any(
        value and bearer and secrets.compare_digest(bearer, value)
        for value in candidates
    )


def _request_is_loopback(request: Request) -> bool:
    client = request.client
    if client is None or not client.host:
        return False
    try:
        return ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        return False


def _request_has_pairing_admin_access(request: Request) -> bool:
    """Authorize offer management, never mobile exchange requests."""
    if _request_has_desktop_token(request):
        return True
    configured = os.environ.get("MEWCODE_PAIRING_ADMIN_TOKEN", "")
    authorization = request.headers.get("authorization", "")
    bearer = authorization[7:] if authorization.lower().startswith("bearer ") else ""
    if configured and bearer and secrets.compare_digest(bearer, configured):
        return True
    # A server without Electron's desktop token may still be managed by a
    # local desktop browser. Never grant this exception through a proxy.
    return not DESKTOP_TOKEN and _request_is_loopback(request)


def _pairing_admin_error(request: Request) -> JSONResponse | None:
    if _request_has_pairing_admin_access(request):
        return None
    return JSONResponse({"error": "desktop pairing authorization required"}, status_code=403)


def _automatic_pairing_endpoint() -> str:
    """Derive this node's MagicDNS HTTPS URL without probing peers or Serve."""
    config = copy.copy(_tailscale_config())
    config.auto_discover = False
    snapshot = collect_tailscale_status(config)
    self_node = snapshot.get("self") if isinstance(snapshot.get("self"), dict) else {}
    dns_name = str(self_node.get("dns_name") or "").strip()
    if not snapshot.get("connected") or not dns_name:
        raise PairingError("Tailscale MagicDNS is not available on this desktop")
    return validate_endpoint("https://" + dns_name)


def _backend_listen_port() -> int:
    raw = str(os.environ.get("MEWCODE_BACKEND_PORT") or "").strip()
    if not raw.isdigit():
        return 0
    port = int(raw)
    return port if 1 <= port <= 65535 else 0


def _publish_pairing_https() -> dict:
    """Expose 127.0.0.1 backend on MagicDNS HTTPS so phones can exchange codes."""
    port = _backend_listen_port()
    if not port:
        return {"https_published": False, "https_error": "desktop backend port is unknown", "proxy": ""}
    try:
        result = ensure_https_proxy(port, config=_tailscale_config())
    except PairingError as exc:
        return {"https_published": False, "https_error": str(exc), "proxy": f"http://127.0.0.1:{port}"}
    return {
        "https_published": bool(result.get("https_published")),
        "https_error": str(result.get("https_error") or ""),
        "proxy": str(result.get("proxy") or ""),
    }


def _remote_access_error(request: Request) -> JSONResponse | None:
    """Remote-node endpoints never fall back to browser cookies or localhost."""
    if _request_has_remote_token(request):
        return None
    return JSONResponse({"error": "remote node token required"}, status_code=401)


def _is_public_pairing_path(request: Request) -> bool:
    path = request.url.path
    if path == "/api/pairing/hello":
        return True
    if path == "/api/pairing/requests" and request.method.upper() == "POST":
        return True
    if path.startswith("/api/pairing/requests/") and request.method.upper() == "GET":
        return True
    return False


def _pairing_peer_error(request: Request) -> JSONResponse | None:
    host = request.client.host if request.client is not None else ""
    if pairing_peer_allowed(host):
        return None
    return JSONResponse(
        {"error": "pairing is only available on the Tailscale network"},
        status_code=403,
    )


def _schedule_pairing_event(payload: dict) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(WS_EVENTS.send_json(payload))


_DESKTOP_PAGE_PATHS = {
    "/teams",
    "/",
    "/pipeline",
    "/orch-canvas",
    "/expert-flow",
    "/completion",
}


def _wants_html(request: Request) -> bool:
    accept = (request.headers.get("accept") or "").lower()
    return "text/html" in accept or request.url.path in _DESKTOP_PAGE_PATHS


class ExpertKindScopeMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        from mewcode.orchestration.kinds import expert_kind_registry
        token = expert_kind_registry.set(APP_CONTEXT.get(EXPERTS, None))
        try:
            await self.app(scope, receive, send)
        finally:
            expert_kind_registry.reset(token)


app.add_middleware(ExpertKindScopeMiddleware)


@app.middleware("http")
async def desktop_security(request: Request, call_next):
    is_remote_path = request.url.path.startswith("/api/remote")
    is_public_pairing = _is_public_pairing_path(request)
    if (
        DESKTOP_TOKEN
        and request.url.path not in ("/api/health", "/desktop-boot")
        and not is_public_pairing
        and not _request_has_desktop_token(request)
        and not (is_remote_path and _request_has_remote_token(request))
    ):
        if _wants_html(request):
            html_headers = {"Cache-Control": "no-store"}
            session_help = (
                "此页面未收到有效的桌面登录会话。请在 MewCode 桌面侧栏点击「在浏览器打开」；"
                "若仍出现此提示，请保存当前工作后重新启动桌面应用。"
            )
            return HTMLResponse(
                """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>需要重新打开</title>
<style>
body{margin:0;min-height:100vh;display:grid;place-items:center;font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#141414;color:#f6f6f2}
.card{width:min(460px,92vw);padding:28px;border:1px solid #3b3b3b;border-radius:18px;background:#1e1e1e}
h1{margin:0 0 8px;font-size:20px}p{margin:0 0 18px;color:#b8b8b3}
button{border:0;border-radius:999px;padding:10px 16px;font:inherit;font-weight:650;cursor:pointer;background:#edede9;color:#111}
</style></head><body><div class="card"><h1>桌面会话未就绪</h1>
<p>__SESSION_HELP__</p>
<button onclick="window.close()">关闭</button></div>
<script>
try{if(window.mewcodeDesktop&&mewcodeDesktop.closePipeline){document.querySelector('button').onclick=function(){mewcodeDesktop.closePipeline();};}}catch(e){}
</script></body></html>""".replace("__SESSION_HELP__", session_help),
                status_code=401,
                headers=html_headers,
            )
        return JSONResponse({"error": "desktop session token required"}, status_code=401)
    from frontend.startup_recovery import blocked_response
    blocked = blocked_response(app, request.url.path, wants_html=_wants_html(request))
    if blocked is not None:
        return blocked
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    connected_view = bool(re.fullmatch(r'/workbench/connections/[a-f0-9]{32}', request.url.path))
    response.headers["X-Frame-Options"] = "SAMEORIGIN" if connected_view else "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' "
        "'sha256-ft1UBUj9f6VPUP32PU3AsakEYVJCLiApxe/Nxn8hRpY=' "
        "'sha256-i8uenIh30VvTTNEXxqxnxnSxn7sWro/Df7UvOYSyrR0='; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; connect-src 'self' ws: wss:; "
        "font-src 'self'; object-src 'none'; base-uri 'none'; "
        + ("frame-ancestors 'self'" if connected_view else "frame-ancestors 'none'")
    )
    return response


class ScopeBody(BaseModel):
    scope: str = "project"


class NewConvBody(BaseModel):
    scope: str = "project"  # project | normal
    dir: str = ""


class ChatInterruptBody(BaseModel):
    conversation_id: str = ""
    action: str = "pause"


class ChatQueueBody(BaseModel):
    conversation_id: str
    queue_id: str
    text: str = ""
    attachments: list[str] = PydanticField(default_factory=list)


class OrchestrationInterventionBody(ChatQueueBody):
    mode: Literal['takeover', 'supplement'] = 'takeover'
    expected_run_id: str = ''
    workbench_context: dict | None = None


class ChatQueueItemBody(BaseModel):
    conversation_id: str


class CheckpointRestoreBody(BaseModel):
    conversation_id: str
    expected_updated_at: str


class LoopControlBody(CheckpointRestoreBody):
    mode: str
    command_id: str


class LoopTokenPolicyBody(CheckpointRestoreBody):
    loop_id: str
    ceiling: int
    floor: int = 16_384
    rate_per_minute: int | None = None
    burst_tokens: int | None = None
    command_id: str


class SavedPlanRevisionBody(CheckpointRestoreBody):
    fingerprint: str
    dialogue_offset: int
    repair_loop_id: str = ""


class RemotePromptBody(BaseModel):
    text: str
    project_id: str = ""


class RemoteActivateBody(BaseModel):
    conversation_id: str = ""
    scope: str = ""
    project_id: str = ""


class RemoteInterruptBody(BaseModel):
    conversation_id: str = ""
    project_id: str = ""


class RemoteResumeBody(BaseModel):
    conversation_id: str = ""
    run_id: str = ""
    project_id: str = ""


class RemotePromptReplyBody(BaseModel):
    conversation_id: str = ""
    kind: str = ""
    project_id: str = ""
    request_id: str = ""
    run_id: str = ""
    decision: str = ""
    answers: dict[str, Any] | None = None
    cancelled: bool = False
    recovery_action: str = ""
    recovery_info: str = ""


class PairingRequestBody(BaseModel):
    device_id: str
    node_id: str
    label: str = ""


class PairingDecideBody(BaseModel):
    request_id: str
    decision: str


class PairingRevokeBody(BaseModel):
    device_id: str


class WorkspaceContinueBody(BaseModel):
    source: str
    external_id: str
    node_id: str = "local"


class WorkspaceConversationRef(BaseModel):
    source: str = "mewcode"
    node_id: str = "local"
    external_id: str = ""
    conversation_id: str = ""


class WorkspaceConversationStateBody(BaseModel):
    action: str
    source: str = "mewcode"
    node_id: str = "local"
    external_id: str = ""
    conversation_id: str = ""
    items: list[WorkspaceConversationRef] = []


class DirBody(BaseModel):
    dir: str


class ExpertProfileBody(BaseModel):
    manager: dict[str, str]
    planner: dict[str, str] | None = None
    executor: dict[str, str]
    auditor: dict[str, str]
    capability_executor: dict[str, str] | None = None
    planner_routes: dict[str, list[dict[str, str]]] = {}
    role_models: dict[str, dict[str, str]] = {}
    activate: bool = False
    conversation_id: str = ""
    project: str = ""


class CapabilityExecutorBody(BaseModel):
    conversation_id: str
    project: str
    model: dict[str, str] | None = None


class RecoveryCaseAnswerBody(BaseModel):
    model_config = {'extra': 'forbid'}
    conversation_id: str
    project: str
    run_id: str
    case_id: str
    round: int = PydanticField(ge=0)
    choice: str | dict[str, Any]
    remember_scope: str = 'run'
    answer: str = ''
    note: str = PydanticField(default='', max_length=2000)
    choice_bundle_id: str = ''
    choice_version: str = ''
    choice_inputs: dict[str, Any] = PydanticField(default_factory=dict)


def _recovery_answer_checkpoint(snapshot, case_id, *, require_waiting=False, allow_investigation=False):
    from mewcode.experts.node_ledger import ledger_for_run
    from mewcode.experts.recovery_case import get_case
    case = get_case(snapshot, case_id)
    investigating = bool(case and allow_investigation and case.get('status') == 'open' and case.get('investigation_requested'))
    system_stopped = bool(case and allow_investigation and case.get('status') == 'reported'
        and not case.get('pending') and (case.get('handling') or {}).get('owner') == 'system'
        and (case.get('handling') or {}).get('handling_state') == 'blocked')
    if not (investigating or system_stopped) and (not case or case.get('status') not in {'reported', 'awaiting_user'} or not case.get('pending')):
        raise ValueError('这张恢复卡已不再待答，请按当前检查点继续')
    node_id = case['node_id']
    stage = (snapshot.get('node_states') or {}).get(node_id) or {}
    if stage.get('status') in {'completed', 'skipped', 'branch_not_taken', 'terminated'}:
        raise ValueError('该节点已结束，这张历史恢复卡不能继续操作')
    view = ledger_for_run(snapshot).view(node_id)
    if view.epoch != case['epoch'] or stage.get('input_revision', '') != case['input_revision']:
        raise ValueError('案件输入或代际已变化，请刷新当前检查点')
    waiting = stage.get('pending_recovery_case') or {}
    if waiting.get('case_id') and waiting['case_id'] != case_id:
        raise ValueError('当前节点正在等待另一张恢复卡，请刷新')
    if require_waiting and not investigating and not (stage.get('status') == 'paused' or view.phase == 'waiting_human'
                               or (system_stopped and stage.get('status') in {'needs_attention', 'blocked'})
                               or snapshot.get('status') in {'paused', 'blocked', 'needs_attention'}):
        raise ValueError('该节点仍在处理，尚未生成可回答的恢复检查点')
    return case


def _case_answer_runtime(conversation_id):
    """Capture an idle *current* conversation without switching UI or running it."""
    runtime = SESSION.runtime_for(conversation_id)
    if runtime is None:
        runtime = getattr(SESSION, 'conversation_runs', {}).get(conversation_id)
    if runtime is None:
        if getattr(getattr(SESSION, 'session', None), 'session_id', '') != conversation_id:
            raise ValueError('原对话运行环境尚未准备好；请在原对话中提交选择')
        runtime = SESSION.capture_conversation_runtime()
    if getattr(runtime, 'conversation_id', conversation_id) != conversation_id:
        raise ValueError('回答与接续运行器不属于同一对话')
    return runtime


async def _queue_case_resume(runtime, snapshot, case):
    """Deliver one accepted answer after the prior turn, never a naked flag."""
    from mewcode.experts.recovery_case import decision_version, get_case
    run_id, case_id = snapshot['run_id'], case['case_id']
    decision = decision_version(case)
    delivery_id = hashlib.sha256((run_id + case_id + decision).encode()).hexdigest()
    tasks = getattr(runtime, '_case_resume_tasks', None)
    if tasks is None:
        tasks = runtime._case_resume_tasks = {}
    if delivery_id in tasks:
        return delivery_id if not tasks[delivery_id].done() else ''
    existing = (snapshot.get('recovery_resume_deliveries') or {}).get(delivery_id) or {}
    if existing.get('status') in {'dispatched', 'waiter', 'failed', 'cancelled', 'retired'}:
        return ''
    previous_task = getattr(runtime, 'task', None)
    def record_delivery(record, status, reason=''):
        record.setdefault('recovery_resume_deliveries', {})[delivery_id] = {
            'case_id': case_id, 'decision_version': decision, 'status': status, 'reason': reason}
        if status == 'retired':
            from mewcode.experts.node_ledger import ledger_for_run
            current_case = get_case(record, case_id)
            stage = (record.get('node_states') or {}).get((current_case or {}).get('node_id'), {})
            if (current_case and not current_case.get('closed_by_user') and not current_case.get('pending')
                    and current_case.get('status') == 'open' and decision_version(current_case) == decision
                    and ledger_for_run(record).view(current_case['node_id']).epoch == current_case['epoch']
                    and (stage.get('recovery_case') or {}).get('case_id') == case_id
                    and stage.get('input_revision', '') == current_case.get('input_revision', '')
                    and stage.get('status') in {'pending', 'paused', 'needs_attention'}):
                from mewcode.experts.recovery_case import save_case
                text = ('有更新的对话任务在处理，本次旧接续未启动，选择和检查点已保留。'
                        if reason == 'newer_conversation_turn' else '本次接续已取消或来源已变化，选择和检查点已保留。')
                current_case['handling'] = {'owner': 'system', 'handling_state': 'blocked',
                    'reason_kind': 'resume_retired', 'reason': text}
                save_case(record, current_case)
                stage['status'] = 'paused'
                if not record.get('active_node_ids'):
                    record['status'] = 'paused'
    EXPERT_RUNS.mutate(run_id, lambda r: record_delivery(r, 'queued'), durable=True)
    async def dispatch():
        nonlocal runtime
        try:
            if previous_task is not None and not previous_task.done():
                try:
                    await asyncio.shield(previous_task)
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                except Exception:
                    pass
            registered = getattr(SESSION, 'conversation_runs', {}).get(runtime.conversation_id)
            if registered is not None and registered is not runtime:
                # A newer turn has priority. Never overwrite its ownership or
                # run concurrently with it; the saved answer remains durable.
                EXPERT_RUNS.mutate(run_id, lambda r: record_delivery(r, 'retired', 'newer_conversation_turn'), durable=True)
                return
            active_task = getattr(runtime, 'task', None)
            if active_task is not None and active_task is not previous_task and not active_task.done():
                EXPERT_RUNS.mutate(run_id, lambda r: record_delivery(r, 'retired', 'newer_conversation_turn'), durable=True)
                return
            if hasattr(SESSION, 'conversation_runs'):
                SESSION.conversation_runs[runtime.conversation_id] = runtime
            current = EXPERT_RUNS.load(run_id)
            saved = get_case(current or {}, case_id)
            from mewcode.experts.node_ledger import ledger_for_run
            stage = ((current or {}).get('node_states') or {}).get((saved or {}).get('node_id'), {})
            fresh = bool(saved and saved['node_id'] in (current.get('node_states') or {})
                         and stage.get('status') not in {'completed', 'skipped', 'branch_not_taken', 'terminated'}
                         and (stage.get('recovery_case') or {}).get('case_id') in {None, case_id}
                         and ledger_for_run(current).view(saved['node_id']).epoch == saved['epoch']
                         and stage.get('input_revision', '') == saved.get('input_revision', ''))

            if (not current or current.get('conversation_id') != runtime.conversation_id
                    or not saved or not fresh or saved.get('closed_by_user') or saved.get('status') != 'open'
                    or saved.get('pending') or decision_version(saved) != decision
                    or current.get('status') in {'completed', 'terminated'}
                    or getattr(runtime, 'cancel_requested', False)):
                EXPERT_RUNS.mutate(run_id, lambda r: record_delivery(r, 'retired'), durable=True)
                return
            def starting(record):
                live = get_case(record, case_id)
                live_stage = record.get('node_states', {}).get((live or {}).get('node_id'), {})
                if (record.get('conversation_id') != runtime.conversation_id
                        or record.get('status') in {'completed', 'terminated'}
                        or getattr(runtime, 'cancel_requested', False)
                        or not live or live.get('closed_by_user') or live.get('pending')
                        or live['node_id'] not in record.get('node_states', {})
                        or live_stage.get('status') in {'completed', 'skipped', 'branch_not_taken', 'terminated'}
                        or (live_stage.get('recovery_case') or {}).get('case_id') not in {None, case_id}
                        or decision_version(live) != decision
                        or ledger_for_run(record).view(live['node_id']).epoch != live['epoch']
                        or live_stage.get('input_revision', '') != live.get('input_revision', '')):
                    raise ValueError('接续问题的输入或代际已变化')
                record_delivery(record, 'starting')
                if not record.get('active_node_ids'):
                    record['status'] = 'paused'
            EXPERT_RUNS.mutate(run_id, starting, durable=True)
            starter = getattr(runtime, 'recovery_resume_dispatcher', None)
            if starter is not None:
                await starter(resume_run_id=run_id)
            else:
                await start_runtime_turn(runtime, '', resume_run_id=run_id, _reserved=True)
            EXPERT_RUNS.mutate(run_id, lambda r: record_delivery(r, 'dispatched'), durable=True)
        except asyncio.CancelledError:
            EXPERT_RUNS.mutate(run_id, lambda r: record_delivery(r, 'cancelled'), durable=True)
            raise
        except Exception as exc:
            from mewcode.expert_runs import redact_for_display
            error_name = type(exc).__name__
            detail = str(redact_for_display(str(exc)))[:600] or error_name
            def delivery_failed(record):
                record_delivery(record, 'failed', detail)
                saved = get_case(record, case_id)
                task = getattr(runtime, 'task', None)
                if (saved and saved.get('status') == 'open' and not saved.get('pending')
                        and (task is None or task.done())):
                    from mewcode.experts.recovery_case import save_case
                    saved['handling'] = {'owner': 'system', 'handling_state': 'blocked',
                        'reason_kind': 'resume_delivery_failed', 'reason': '回答已保存，但续跑未启动：' + detail}
                    save_case(record, saved)
                    record['node_states'][saved['node_id']]['status'] = 'paused'
            EXPERT_RUNS.mutate(run_id, delivery_failed, durable=True)
            await WS_EVENTS.send_json({'type': 'error', 'conversation_id': runtime.conversation_id,
                'run_id': run_id, 'error_code': 'recovery_resume_delivery_failed',
                'text': '回答已保存，但系统未能接续处理：' + detail})
    tasks[delivery_id] = asyncio.create_task(dispatch())
    def release_idle(delivered):
        owner = getattr(runtime, 'owner', None)
        cleanup = getattr(owner, '_continue_conversation_queue', None)
        if callable(cleanup) and getattr(runtime, 'task', None) is None and not getattr(runtime, 'starting', False):
            cleanup(runtime)
    tasks[delivery_id].add_done_callback(release_idle)
    return delivery_id


@app.post('/api/orchestration/run/case/answer')
async def api_recovery_case_answer(body: RecoveryCaseAnswerBody) -> JSONResponse:
    if not _capability_executor_scope_matches(body):
        return JSONResponse({'error': '请在案件所属的当前对话中回答'}, status_code=409)
    try:
        snapshot = EXPERT_RUNS.load(body.run_id)
    except ValueError:
        snapshot = None
    if not snapshot or snapshot.get('conversation_id') != body.conversation_id:
        return JSONResponse({'error': '案件运行不属于该对话'}, status_code=404)
    from mewcode.experts.recovery_case import get_case
    saved_case = get_case(snapshot, body.case_id)
    from mewcode.experts.recovery_case import decision_version, digest
    answer_key = digest(body.model_dump())
    from mewcode.experts.node_ledger import ledger_for_run
    if (saved_case and answer_key in saved_case.get('answer_receipts', {})
            and ledger_for_run(snapshot).view(saved_case['node_id']).epoch == saved_case['epoch']
            and (snapshot.get('node_states', {}).get(saved_case['node_id']) or {}).get('input_revision', '')
                == saved_case.get('input_revision', '')):
        delivery_id = saved_case['answer_receipts'][answer_key].get('resume_delivery_id', '')
        delivery = (snapshot.get('recovery_resume_deliveries') or {}).get(delivery_id) or {}
        queued = ''
        if delivery.get('status') in {'pending', 'queued'}:
            try:
                runtime = _case_answer_runtime(body.conversation_id)
            except (ValueError, RuntimeError) as exc:
                return JSONResponse({'error': str(exc), 'error_code': 'recovery_runtime_unavailable',
                    'answer_saved': True}, status_code=409)
            queued = await _queue_case_resume(runtime, snapshot, saved_case)
        return JSONResponse({'snapshot': EXPERT_RUNS._public_value(EXPERT_RUNS.load(body.run_id), include_details=False),
                             'already_submitted': True, 'resume_required': False, 'resume_pending': bool(queued),
                             'resume_delivery_id': queued,
                             **({'already_stopped': True} if body.choice == 'close' and saved_case.get('closed_by_user') else {})})

    if body.choice == 'close' and saved_case and saved_case.get('closed_by_user'):
        return JSONResponse({'snapshot': EXPERT_RUNS._public_value(snapshot, include_details=False),
                             'resume_required': False, 'already_stopped': True})
    try:
        runtime = SESSION.runtime_for(body.conversation_id) if body.choice == 'close' else _case_answer_runtime(body.conversation_id)
    except (ValueError, RuntimeError) as exc:
        return JSONResponse({'error': str(exc), 'error_code': 'recovery_runtime_unavailable'}, status_code=409)
    future = getattr(runtime, '_case_waiters', {}).get(body.case_id)
    if future is not None and future.done():
        future = None
    busy = SESSION.chat_busy(body.conversation_id)
    try:
        _recovery_answer_checkpoint(snapshot, body.case_id, require_waiting=busy and future is None, allow_investigation=body.choice == 'close')
    except ValueError as exc:
        return JSONResponse({'error': str(exc), 'error_code': 'recovery_case_stale',
                             'snapshot': EXPERT_RUNS._public_value(snapshot, include_details=False)}, status_code=409)
    from mewcode.experts.case_runtime import answer_case
    try:
        answered_case = None
        def answered(record):
            nonlocal answered_case
            if record.get('conversation_id') != body.conversation_id:
                raise ValueError('运行所属对话已改变')
            _recovery_answer_checkpoint(record, body.case_id, require_waiting=busy and future is None, allow_investigation=body.choice == 'close')
            wire_choice = json.dumps(body.choice, ensure_ascii=False) if isinstance(body.choice, dict) else body.choice
            answered_case = answer_case(record, body.case_id, round=body.round, choice=wire_choice,
                remember_scope=body.remember_scope, answer=body.answer, note=body.note,
                bundle_id=body.choice_bundle_id, version=body.choice_version,
                choice_inputs=body.choice_inputs)
            if answered_case is not None:
                receipt = {'choice': body.choice, 'round': body.round}
                if answered_case.get('status') == 'open' and not answered_case.get('pending') and body.choice != 'close':
                    decision = decision_version(answered_case)
                    delivery_id = hashlib.sha256((body.run_id + body.case_id + decision).encode()).hexdigest()
                    receipt['resume_delivery_id'] = delivery_id
                    record.setdefault('recovery_resume_deliveries', {}).setdefault(delivery_id, {
                        'case_id': body.case_id, 'decision_version': decision, 'status': 'pending', 'reason': ''})
                    answered_case['handling'] = {'owner': 'system', 'handling_state': 'deciding',
                        'reason_kind': 'answer_accepted', 'reason': '回答已保存，正在接续原步骤。'}
                answered_case.setdefault('answer_receipts', {})[answer_key] = receipt
                from mewcode.experts.recovery_case import save_case
                save_case(record, answered_case)

        changed = EXPERT_RUNS.mutate(body.run_id, answered, durable=True)
    except (ValueError, KeyError, OSError) as exc:
        return JSONResponse({'error': str(exc)}, status_code=409)
    if body.choice == 'close':
        investigation = getattr(runtime, '_case_investigations', {}).get(body.case_id)
        if investigation is not None and not investigation.done():
            investigation.cancel()
    choice_action = (answered_case or {}).get('pending_choice_action') or {}
    if choice_action and choice_action.get('action') not in {'defer', 'manual'}:
        from frontend.interaction_choices import refresh_case_choices
        try:
            changed = await refresh_case_choices(sys.modules[__name__], runtime=runtime,
                snapshot=changed, case_id=body.case_id, instruction=str(choice_action.get('input') or ''),
                action=str(choice_action.get('action') or 'research'))
            from mewcode.experts.recovery_case import get_case
            answered_case = get_case(changed, body.case_id) or answered_case
        except (ValueError, OSError) as exc:
            return JSONResponse({'error': str(exc), 'snapshot': EXPERT_RUNS._public_value(
                changed, include_details=False)}, status_code=409)
    partial = bool(answered_case and answered_case.get('status') == 'awaiting_user'
                   and answered_case.get('pending'))
    if not partial and future is not None and not future.done():
        future.set_result(body.choice)
        def delivered_to_waiter(record):
            delivery_id = (answered_case.get('answer_receipts') or {}).get(answer_key, {}).get('resume_delivery_id')
            if delivery_id:
                record['recovery_resume_deliveries'][delivery_id]['status'] = 'waiter'
        changed = EXPERT_RUNS.mutate(body.run_id, delivered_to_waiter, durable=True)
    resume = not partial and future is None and body.choice != 'close' and not (answered_case or {}).get('pending')
    delivery_id = ''
    if resume:
        delivery_id = await _queue_case_resume(runtime, changed, answered_case)
    public = EXPERT_RUNS._public_value(EXPERT_RUNS.load(body.run_id), include_details=False)
    await WS_EVENTS.send_json({'type': 'expert_checkpoint_restored', 'run_id': body.run_id,
        'conversation_id': body.conversation_id, 'snapshot': public})
    return JSONResponse({'snapshot': public, 'resume_required': False,
                         'resume_pending': bool(delivery_id), 'resume_delivery_id': delivery_id,
                         'resume_delivery': 'queued' if delivery_id else 'waiter' if future is not None else 'waiting'})


class RunCapabilityExecutorBody(CapabilityExecutorBody):
    expected_updated_at: str
    command_id: str


class DifficultNodeModelBody(CapabilityExecutorBody):
    model: dict[str, str]
    node_ids: list[str]
    apply_node_ids: list[str] = []
    expected_updated_at: str
    command_id: str
    record_backup: bool = True


class DesktopProviderBundleBody(BaseModel):
    profiles: list[dict[str, Any]]


class DesktopProviderTestBody(BaseModel):
    profile: dict[str, Any]


class DesktopUsageAccountBundleBody(BaseModel):
    accounts: list[dict[str, Any]]


class UsageBrowserResultBody(BaseModel):
    command_id: str
    result: Any = None
    error: str = ""


async def _validated_capability_executor(
    value: dict[str, str] | ModelRef | None, *, catalog: dict[str, Any] | None = None,
) -> ModelRef | None:
    from mewcode.model_registry import ModelRef
    if value is None:
        return None
    model = ModelRef.from_value(value)
    if catalog is None:
        catalog = await MODEL_REGISTRY.catalog()
    selected, reason = select_capability_executor_model(catalog, model)
    if selected is None:
        raise ValueError(reason)
    resolved = MODEL_REGISTRY.resolve(model)
    if resolved.config.protocol == "typesafe" or "tools" not in resolved.capabilities:
        raise ValueError("该模型不支持辅助执行器所需的工具调用")
    if resolved.ref != model:
        raise ValueError("该辅助模型不支持所选推理强度，请重新选择")
    return model


def _capability_executor_scope_matches(body: CapabilityExecutorBody) -> bool:
    visible = SESSION.session.session_id if SESSION.session is not None else ""
    return bool(body.conversation_id and body.conversation_id == visible
                and body.project and SESSION.work_dir
                and os.path.normcase(str(Path(body.project).resolve()))
                == os.path.normcase(str(Path(SESSION.work_dir).resolve())))


async def _invalid_expert_roles(profile: ExpertProfile) -> list[str]:
    catalog = await MODEL_REGISTRY.catalog()
    providers = {
        str(provider.get("key") or ""): provider
        for provider in catalog.get("providers", [])
    }
    catalog_models = {
        (str(model.get("provider_key") or ""), str(model.get("model_id") or ""))
        for model in catalog.get("models", [])
        if model.get("available") is not False
    }
    invalid: list[str] = []
    for role, model_ref in (
        ("manager", profile.manager),
        ("planner", profile.planner),
        ("executor", profile.executor),
        ("auditor", profile.auditor),
        ("capability_executor", getattr(profile, "capability_executor", None)),
    ):
        if model_ref is None:
            continue
        try:
            if role == "capability_executor":
                await _validated_capability_executor(model_ref, catalog=catalog)
                continue
            model_ref = MODEL_REGISTRY.relocate_model_ref(model_ref)
            MODEL_REGISTRY.resolve(model_ref)
        except ValueError:
            invalid.append(role)
            continue
        provider = providers.get(model_ref.provider_key)
        if provider is None:
            invalid.append(role)
        elif (
            provider.get("models_authoritative")
            and (model_ref.provider_key, model_ref.model_id) not in catalog_models
        ):
            invalid.append(role)
    from mewcode.kernel.models import RoleModels
    if 'models' in catalog and profile.role_models:
        pool = RoleModels(catalog)
        for role, model in profile.role_models.items():
            try:
                pool.require(model, role)
                MODEL_REGISTRY.resolve(model)
            except ValueError:
                invalid.append(role)
    return invalid


def _heal_stored_expert_profile(work_dir: str | None = None) -> ExpertProfile | None:
    target = work_dir if work_dir is not None else SESSION.work_dir
    profile = EXPERT_PROFILES.get(target)
    if profile is None:
        return None
    healed = relocate_expert_profile(MODEL_REGISTRY, profile)
    if healed != profile:
        try:
            EXPERT_PROFILES.put(target, healed)
        except (OSError, ValueError):
            return healed
        return healed
    return profile


async def _disable_invalid_active_expert_profile() -> list[str]:
    if not SESSION.expert_mode:
        return []
    profile = _heal_stored_expert_profile()
    invalid = ["profile"] if profile is None else await _invalid_expert_roles(profile)
    if invalid:
        SESSION.set_expert_mode(False)
    return invalid


@app.get("/api/health")
async def api_health() -> JSONResponse:
    from frontend.startup_recovery import phase
    current = phase(app)
    return JSONResponse({"ok": current != 'failed', "desktop": bool(DESKTOP_TOKEN),
                         "ready": current == 'ready', "startup_state": current},
                        status_code=503 if current == 'failed' else 200)


def _safe_desktop_next(raw: str | None) -> str:
    """Allow only same-origin relative paths for desktop-boot redirects."""
    value = (raw or "/").strip() or "/"
    if not value.startswith("/") or value.startswith("//") or "://" in value:
        return "/"
    if "\\" in value or "\n" in value or "\r" in value:
        return "/"
    return value


@app.post("/api/browser-launch")
async def api_browser_launch(request: Request):
    if not DESKTOP_TOKEN or not _request_has_desktop_token(request):
        return JSONResponse({"error": "请从已登录的桌面窗口打开。"}, status_code=401)
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        return JSONResponse({"error": "只允许从当前客户端打开浏览器。"}, status_code=403)
    return JSONResponse({"path": BROWSER_LAUNCHES.issue()}, headers={"Cache-Control": "no-store"})


@app.get("/desktop-boot")
async def desktop_boot(request: Request):
    """Bootstrap desktop webviews: set session cookie then redirect to target.

    Secondary windows (Pipeline / Expert Flow) previously relied only on an
    initialization script; if that race failed they hit /pipeline with no
    cookie and rendered a blank 401 JSON page. Server-side Set-Cookie + 302
    makes the handoff reliable.
    """
    next_path = _safe_desktop_next(request.query_params.get("next"))
    session = str(request.query_params.get("session") or "")
    if (
        DESKTOP_TOKEN
        and ((session and secrets.compare_digest(session, DESKTOP_TOKEN))
             or BROWSER_LAUNCHES.consume(str(request.query_params.get("ticket") or "")))
    ):
        response = RedirectResponse(url=next_path, status_code=302)
        response.set_cookie(
            key=DESKTOP_COOKIE,
            value=DESKTOP_TOKEN,
            httponly=True,
            samesite="strict",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return response
    # Already authenticated via cookie — continue without requiring session query.
    if DESKTOP_TOKEN and _request_has_desktop_token(request):
        response = RedirectResponse(url=next_path, status_code=302)
        response.headers["Cache-Control"] = "no-store"
        return response
    # Never leave the WebView on a silent placeholder forever. The desktop shell
    # must open /desktop-boot?session=...&next=...; bare /desktop-boot is a bug.
    return HTMLResponse(
        """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>MewCode 启动失败</title>
<style>
body{margin:0;min-height:100vh;display:grid;place-items:center;font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#141414;color:#f2f2ee}
.card{width:min(460px,92vw);padding:28px;border:1px solid #3b3b3b;border-radius:18px;background:#1e1e1e}
h1{margin:0 0 8px;font-size:20px}p{margin:0;color:#b8b8b3}
</style></head>
<body><div class="card"><h1>桌面会话未带上</h1>
<p>主窗口启动参数缺少 session。请关闭 MewCode 后重新打开；若反复出现，说明桌面壳与后端版本不一致，需要重新编译 mewcode-tauri。</p>
</div></body></html>""",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/pairing/hello")
async def api_pairing_hello(request: Request) -> JSONResponse:
    if error := _pairing_peer_error(request):
        return error
    return JSONResponse(
        {"pairing": True, "node_id": _local_node_id()},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/pairing/status")
async def api_pairing_status(request: Request) -> JSONResponse:
    if error := _pairing_admin_error(request):
        return error
    devices = REMOTE_PAIRING.list_devices()
    requests = REMOTE_PAIRING.list_requests()
    config = copy.copy(_tailscale_config())
    config.auto_discover = False
    snapshot = await asyncio.to_thread(collect_tailscale_status, config)
    error_text = str(snapshot.get("error") or "")
    connected = bool(snapshot.get("connected"))
    if connected:
        error_text = error_text
    elif snapshot.get("available") is False:
        error_text = error_text or "tailscale CLI not found"
    else:
        error_text = error_text or "Tailscale is not connected"
    endpoint = ""
    try:
        endpoint = _automatic_pairing_endpoint()
    except PairingError:
        endpoint = ""
    published = (
        _publish_pairing_https()
        if connected
        else {"https_published": False, "https_error": error_text, "proxy": ""}
    )
    return JSONResponse(
        {
            "available": connected,
            "error": error_text,
            "endpoint": endpoint,
            "devices": devices,
            "requests": requests,
            "https_published": published["https_published"],
            "https_error": published["https_error"],
        },
        headers={"Cache-Control": "no-store"},
    )


def _pairing_request_payload(result) -> dict:
    payload = {
        "request_id": result.request_id,
        "status": result.status,
        "device_id": result.device_id,
        "node_id": result.node_id,
        "label": result.label,
        "expires_at": result.expires_at,
        "allow": result.allow,
    }
    if result.token:
        payload["token"] = result.token
    return payload


@app.post("/api/pairing/requests")
async def api_submit_pairing_request(body: PairingRequestBody, request: Request) -> JSONResponse:
    if error := _pairing_peer_error(request):
        return error
    try:
        result = REMOTE_PAIRING.submit_request(
            body.device_id, body.node_id, label=body.label.strip() or None
        )
    except PairingError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    payload = _pairing_request_payload(result)
    if result.status == "pending":
        _schedule_pairing_event({
            "type": "pairing_request",
            "request_id": result.request_id,
            "device_id": result.device_id,
            "node_id": result.node_id,
            "label": result.label,
            "expires_at": result.expires_at,
        })
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.get("/api/pairing/requests/{request_id}")
async def api_get_pairing_request(request_id: str, request: Request) -> JSONResponse:
    if error := _pairing_peer_error(request):
        return error
    try:
        result = REMOTE_PAIRING.get_request(request_id)
    except PairingError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(_pairing_request_payload(result), headers={"Cache-Control": "no-store"})


@app.post("/api/pairing/decide")
async def api_decide_pairing(body: PairingDecideBody, request: Request) -> JSONResponse:
    if error := _pairing_admin_error(request):
        return error
    try:
        result = REMOTE_PAIRING.decide(body.request_id, body.decision)
    except PairingError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(
        {
            "request_id": result.request_id,
            "status": result.status,
            "device_id": result.device_id,
            "node_id": result.node_id,
            "allow": result.allow,
        },
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/pairing/revoke")
async def api_revoke_paired_device(body: PairingRevokeBody, request: Request) -> JSONResponse:
    if error := _pairing_admin_error(request):
        return error
    try:
        revoked = REMOTE_PAIRING.revoke_device(body.device_id)
    except PairingError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"device_id": body.device_id, "revoked": revoked}, headers={"Cache-Control": "no-store"})


def _tailscale_config() -> TailscaleConfig:
    """Load Tailscale settings without making startup depend on config files."""
    try:
        return load_config().tailscale
    except Exception:
        # The collector reports an unavailable CLI without failing startup.
        return TailscaleConfig()


@app.get("/api/tailscale/status")
async def api_remote_tailscale_status() -> JSONResponse:
    snapshot = await asyncio.to_thread(collect_tailscale_status, _tailscale_config())
    config = _tailscale_config()
    snapshot["capabilities"] = {
        "node_discovery": True,
        "auto_mewcode_probe": bool(config.auto_discover),
        "remote_history": bool(config.remote_nodes or discovered_remote_nodes()),
        "remote_conversations": bool(config.remote_nodes or discovered_remote_nodes()),
    }
    return JSONResponse(snapshot, headers={"Cache-Control": "no-store"})


@app.get("/api/external-agents")
async def api_external_agents(source: str = Query("")) -> JSONResponse:
    await asyncio.to_thread(EXTERNAL_AGENTS.refresh)
    return JSONResponse({"items": EXTERNAL_AGENTS.list(source)})


@app.post("/api/external-agents/refresh")
async def api_external_agents_refresh() -> JSONResponse:
    changed = await asyncio.to_thread(EXTERNAL_AGENTS.refresh)
    return JSONResponse({"items": EXTERNAL_AGENTS.list(), "changed": changed})


@app.get("/api/external-agents/{source}/{external_id}")
async def api_external_agent_conversation(source: str, external_id: str) -> JSONResponse:
    await asyncio.to_thread(EXTERNAL_AGENTS.refresh)
    result = EXTERNAL_AGENTS.get(source, external_id)
    if result is None:
        return JSONResponse({"error": "external conversation not found"}, status_code=404)
    return JSONResponse(result)


def _local_node_id() -> str:
    return os.environ.get("MEWCODE_NODE_ID") or os.environ.get("COMPUTERNAME") or "local"


def _remote_mewcode_source_error(source: str) -> JSONResponse | None:
    normalized = (source or "mewcode").strip().lower()
    if normalized in {"", "mewcode"}:
        return None
    return JSONResponse({"error": "remote history only supports mewcode"}, status_code=400)


def _conversation_expert_snapshot(conversation_id: str) -> dict[str, Any] | None:
    target = str(conversation_id or "").strip()
    if not target:
        return None
    try:
        items = EXPERT_RUNS.list_for_conversation(target)
    except Exception:
        return None
    for item in items:
        if item.get("legacy"):
            continue
        if str(item.get("status") or "") == "running":
            return item
    for item in items:
        if is_resumable_snapshot(item):
            return item
    return next((item for item in items if not item.get("legacy")), None)


def _conversation_expert_flags(conversation_id: str) -> dict[str, Any]:
    snapshot = _conversation_expert_snapshot(conversation_id)
    runtime = SESSION.runtime_for(conversation_id) if conversation_id else None
    execution_kind = "chat"
    if runtime is not None:
        execution_kind = str(getattr(runtime, "execution_kind", "chat") or "chat")
    status = str((snapshot or {}).get("status") or "")
    if execution_kind != "orchestration" and status in {
        "running", "paused", "blocked", "needs_attention", "waiting", "pending",
    }:
        execution_kind = "orchestration"
    busy = bool(conversation_id) and SESSION.chat_busy(conversation_id)
    if busy and execution_kind == "orchestration":
        status = "running"
    return {
        "execution_kind": execution_kind,
        "expert_status": status,
        "resumable": bool(snapshot and is_resumable_snapshot(snapshot) and not busy),
        "run_id": str((snapshot or {}).get("run_id") or ""),
    }


def _remote_project_id(directory: str) -> str:
    return hashlib.sha256(
        os.path.normcase(os.path.abspath(directory)).encode("utf-8")
    ).hexdigest()[:24]


def _remote_project_dirs() -> list[str]:
    roots: dict[str, str] = {}
    for directory in [SESSION.work_dir, *_load_projects()]:
        norm = os.path.abspath(directory)
        roots.setdefault(os.path.normcase(norm), norm)
    return list(roots.values())


def _remote_project_dir(project_id: str) -> str | None:
    return next(
        (
            directory
            for directory in _remote_project_dirs()
            if _remote_project_id(directory) == project_id
        ),
        None,
    )


def _remote_scope_normalized(scope: str) -> str:
    return "project" if scope == "project" else "user"


def _remote_conversation_conflicts(
    conversation_id: str, scope: str, project_path: str
) -> bool:
    target = str(conversation_id or "")
    if not target:
        return False
    wanted_scope = _remote_scope_normalized(scope)
    wanted_root = (
        _remote_project_id(project_path)
        if wanted_scope == "project" and project_path
        else ""
    )

    def _mismatch(other_scope: str, other_dir: str) -> bool:
        if _remote_scope_normalized(other_scope) != wanted_scope:
            return True
        if wanted_scope == "project":
            return _remote_project_id(other_dir) != wanted_root
        return False

    if (
        SESSION.session is not None
        and SESSION.session.session_id == target
        and _mismatch(SESSION.scope, SESSION.work_dir)
    ):
        return True
    runtime = SESSION.runtime_for(target)
    return runtime is not None and _mismatch(runtime.scope, runtime.work_dir)


def _pack_mewcode_history_item(
    item: dict[str, Any], scope: str, project_path: str = ""
) -> dict[str, Any]:
    folder = "普通对话"
    root = ""
    if scope == "project":
        root = os.path.abspath(
            project_path or str(getattr(SESSION, "work_dir", "") or "")
        )
        folder = root
    conversation_id = str(item.get("id") or item.get("session_id") or "")
    conflict = _remote_conversation_conflicts(conversation_id, scope, root)
    active = bool(item.get("active")) and not conflict
    if active:
        if _remote_scope_normalized(scope) == "project":
            active = SESSION.scope == "project" and os.path.normcase(
                os.path.abspath(SESSION.work_dir)
            ) == os.path.normcase(root)
        else:
            active = _remote_scope_normalized(SESSION.scope) == "user"
    flags = (
        {"execution_kind": "chat", "expert_status": "", "resumable": False, "run_id": ""}
        if conflict
        else _conversation_expert_flags(conversation_id)
    )
    return {
        "source": "mewcode",
        "external_id": conversation_id,
        "scope": scope,
        "title": item.get("title") or "新对话",
        "project_path": root,
        "parent_folder": folder,
        "project_id": _remote_project_id(root) if root else "",
        "updated_at": item.get("last_active") or item.get("updated_at") or item.get("mtime") or "",
        "message_count": item.get("message_count", 0),
        "controllable": True,
        "active": active,
        "running": bool(item.get("running")) and not conflict,
        **flags,
    }


def _speech_only_messages(messages: list[Any], speech_only: bool) -> list[Any]:
    if not speech_only:
        return list(messages)
    return [
        message
        for message in messages
        if isinstance(message, dict) and str(message.get("kind") or "message") == "message"
    ]


def _live_mewcode_history_item(external_id: str) -> dict[str, Any] | None:
    if (
        SESSION.session is not None
        and SESSION.session.session_id == external_id
        and SESSION.conversation is not None
    ):
        root = (
            os.path.abspath(SESSION.work_dir)
            if SESSION.scope == "project"
            else ""
        )
        return {
            "source": "mewcode",
            "external_id": external_id,
            "scope": SESSION.scope,
            "title": str(getattr(SESSION.session, "title", "") or ""),
            "running": SESSION.chat_busy(external_id),
            "active": True,
            "project_path": root,
            "project_id": _remote_project_id(root) if root else "",
            "messages": messages_to_display(SESSION.conversation.history),
            **_conversation_expert_flags(external_id),
        }
    runtime = SESSION.runtime_for(external_id)
    if runtime is None or runtime.conversation is None:
        return None
    root = (
        os.path.abspath(runtime.work_dir)
        if runtime.scope == "project"
        else ""
    )
    return {
        "source": "mewcode",
        "external_id": external_id,
        "scope": runtime.scope,
        "title": str(getattr(runtime.session, "title", "") or ""),
        "running": True,
        "active": _active_conversation_id() == external_id,
        "project_path": root,
        "project_id": _remote_project_id(root) if root else "",
        "messages": messages_to_display(runtime.conversation.history),
        **_conversation_expert_flags(external_id),
    }


def _disk_mewcode_history_item(
    external_id: str, project_id: str = ""
) -> dict[str, Any] | None:
    if not external_id or _SAFE_ID_RE.fullmatch(external_id) is None:
        return None
    candidates: list[tuple[str, Any, str]] = []
    if project_id:
        directory = _remote_project_dir(project_id)
        if directory is None:
            return None
        if not (Path(directory) / ".mewcode" / "sessions").is_dir():
            return None
        candidates.append(("project", SESSION.new_session_store(directory), directory))
    else:
        candidates.append(
            ("project", SESSION.session_manager("project"), os.path.abspath(SESSION.work_dir))
        )
        candidates.append(("user", SESSION.session_manager("user"), ""))
        current = os.path.normcase(os.path.abspath(SESSION.work_dir))
        for directory in _remote_project_dirs():
            if os.path.normcase(directory) == current:
                continue
            if not (Path(directory) / ".mewcode" / "sessions").is_dir():
                continue
            candidates.append(("project", SESSION.new_session_store(directory), directory))
    for scope, manager, directory in candidates:
        try:
            restored = manager.resume(external_id)
        except Exception:
            continue
        if restored is None:
            continue
        conflict = _remote_conversation_conflicts(external_id, scope, directory)
        active = (
            not conflict
            and _active_conversation_id() == external_id
            and _remote_scope_normalized(SESSION.scope) == _remote_scope_normalized(scope)
            and (
                _remote_scope_normalized(scope) != "project"
                or os.path.normcase(directory)
                == os.path.normcase(os.path.abspath(SESSION.work_dir))
            )
        )
        flags = (
            {"execution_kind": "chat", "expert_status": "", "resumable": False, "run_id": ""}
            if conflict
            else _conversation_expert_flags(external_id)
        )
        try:
            return {
                "source": "mewcode",
                "external_id": external_id,
                "scope": scope,
                "title": str(getattr(restored.session.meta, "title", "") or getattr(restored.session, "title", "") or ""),
                "running": SESSION.chat_busy(external_id) and not conflict,
                "active": active,
                "project_path": directory,
                "project_id": _remote_project_id(directory) if directory else "",
                "messages": messages_to_display(restored.messages),
                **flags,
            }
        finally:
            restored.session.close()
    return None


def _remote_history_item(external_id: str, project_id: str = "") -> dict[str, Any] | None:
    if not external_id or _SAFE_ID_RE.fullmatch(external_id) is None:
        return None
    if not project_id:
        return _live_mewcode_history_item(external_id) or _disk_mewcode_history_item(external_id)
    directory = _remote_project_dir(project_id)
    if directory is None:
        return None
    runtime = SESSION.runtime_for(external_id)
    active_matches = (
        SESSION.session is not None
        and SESSION.session.session_id == external_id
        and SESSION.scope == "project"
        and _remote_project_id(SESSION.work_dir) == project_id
    )
    runtime_matches = (
        runtime is not None
        and runtime.scope == "project"
        and _remote_project_id(runtime.work_dir) == project_id
    )
    if runtime_matches and not active_matches and (
        SESSION.session is not None and SESSION.session.session_id == external_id
    ):
        return _disk_mewcode_history_item(external_id, project_id)
    if active_matches or runtime_matches:
        return _live_mewcode_history_item(external_id) or _disk_mewcode_history_item(external_id, project_id)
    return _disk_mewcode_history_item(external_id, project_id)


async def _activate_remote_mewcode_conversation(
    conversation_id: str, scope_hint: str = "", project_id: str = ""
) -> dict[str, Any] | None:
    target = conversation_id.strip()
    if not target or _SAFE_ID_RE.fullmatch(target) is None:
        return None
    if project_id:
        directory = _remote_project_dir(project_id)
        if directory is None:
            return None
        sessions_dir = Path(directory) / ".mewcode" / "sessions"
        if not (sessions_dir / f"{target}.jsonl").is_file() or not (
            sessions_dir / f"{target}.meta"
        ).is_file():
            return None
        runtime = SESSION.runtime_for(target)
        active_same = SESSION.session is not None and SESSION.session.session_id == target
        if active_same and (
            SESSION.scope != "project"
            or _remote_project_id(SESSION.work_dir) != project_id
        ):
            return None
        if runtime is not None and (
            runtime.scope != "project"
            or _remote_project_id(runtime.work_dir) != project_id
        ):
            return None
        if SESSION.session is not None and SESSION.session.session_id == target:
            return {
                "id": target,
                "scope": SESSION.scope,
                "running": SESSION.chat_busy(target),
            }
        if os.path.abspath(directory) != os.path.abspath(SESSION.work_dir):
            error = await SESSION.aactivate_project(directory)
            if error:
                return None
        return SESSION.load_conversation("project", target)
    if SESSION.session is not None and SESSION.session.session_id == target:
        return {
            "id": target,
            "scope": SESSION.scope,
            "running": SESSION.chat_busy(target),
        }
    scopes: list[str] = []
    hint = "user" if scope_hint in {"user", "normal"} else "project" if scope_hint == "project" else ""
    if hint:
        scopes.append(hint)
    for scope in ("project", "user"):
        if scope not in scopes:
            scopes.append(scope)
    for scope in scopes:
        loaded = SESSION.load_conversation(scope, target)
        if loaded is not None:
            return loaded
    current = os.path.normcase(os.path.abspath(SESSION.work_dir))
    for directory in _remote_project_dirs():
        if os.path.normcase(directory) == current:
            continue
        sessions_dir = Path(directory) / ".mewcode" / "sessions"
        if not (sessions_dir / f"{target}.jsonl").is_file() or not (
            sessions_dir / f"{target}.meta"
        ).is_file():
            continue
        error = await SESSION.aactivate_project(directory)
        if error:
            continue
        loaded = SESSION.load_conversation("project", target)
        if loaded is not None:
            return loaded
    return None


async def _broadcast_remote_conversation_focus(
    conversation_id: str, scope: str, *, mode: str
) -> None:
    await WS_EVENTS.send_json({
        "type": "remote_conversation_focus",
        "conversation_id": conversation_id,
        "scope": scope,
        "source": "mobile",
        "mode": mode,
    })


@app.get("/api/remote/info")
async def api_remote_info(request: Request) -> JSONResponse:
    if error := _remote_access_error(request):
        return error
    return JSONResponse({
        "node_id": _local_node_id(),
        "active_conversation_id": _active_conversation_id(),
        "capabilities": {
            "history": True,
            "create_conversation": True,
            "send_prompt": True,
            "live_sync": True,
            "orchestration_live": True,
            "interrupt": True,
            "resume": True,
            "prompts": True,
            "external_agents": False,
        },
    })


@app.get("/api/remote/history")
async def api_remote_history(request: Request, source: str = Query("")) -> JSONResponse:
    if error := _remote_access_error(request):
        return error
    if error := _remote_mewcode_source_error(source):
        return error
    sidebar = SESSION.sidebar_payload()
    items: list[dict[str, Any]] = []
    projects: list[dict[str, Any]] = []
    for project in sidebar.get("projects") or []:
        directory = str(project.get("dir") or "")
        projects.append({
            "id": _remote_project_id(directory) if directory else "",
            "dir": directory,
            "label": str(project.get("label") or ""),
            "active": bool(project.get("active")),
        })
        for item in project.get("conversations") or []:
            items.append(_pack_mewcode_history_item(item, "project", directory))
    for item in sidebar.get("normal") or []:
        items.append(_pack_mewcode_history_item(item, "user"))
    return JSONResponse({
        "node_id": _local_node_id(),
        "active_conversation_id": _active_conversation_id(),
        "running_conversation_ids": sorted(SESSION.running_conversation_ids()),
        "projects": projects,
        "items": sort_history_items(items),
    })


@app.get("/api/remote/history/item")
async def api_remote_history_item(
    request: Request,
    source: str = Query(""),
    external_id: str = Query(""),
    speech_only: bool = Query(True),
    project_id: str = Query(""),
) -> JSONResponse:
    if error := _remote_access_error(request):
        return error
    if error := _remote_mewcode_source_error(source):
        return error
    result = _remote_history_item(external_id, project_id)
    if result is None:
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    packed = dict(result)
    packed["messages"] = _speech_only_messages(list(packed.get("messages") or []), speech_only)
    return JSONResponse(packed)


@app.post("/api/remote/conversations")
async def api_remote_new_conversation(body: NewConvBody, request: Request) -> JSONResponse:
    if error := _remote_access_error(request):
        return error
    scope = "user" if body.scope == "normal" else "project"
    if body.scope not in {"project", "normal"}:
        return JSONResponse({"error": "invalid conversation scope"}, status_code=400)
    if body.dir:
        return JSONResponse({"error": "remote conversation creation cannot select an arbitrary directory"}, status_code=400)
    created = SESSION.new_conversation(scope)
    await _broadcast_remote_conversation_focus(
        str(created.get("id") or ""), str(created.get("scope") or scope), mode="create"
    )
    return JSONResponse(created)


@app.post("/api/remote/conversations/activate")
async def api_remote_activate_conversation(body: RemoteActivateBody, request: Request) -> JSONResponse:
    if error := _remote_access_error(request):
        return error
    loaded = await _activate_remote_mewcode_conversation(
        body.conversation_id, body.scope, body.project_id
    )
    if loaded is None:
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    await _broadcast_remote_conversation_focus(
        str(loaded.get("id") or body.conversation_id),
        str(loaded.get("scope") or "project"),
        mode="view",
    )
    return JSONResponse(loaded)


@app.post("/api/remote/conversations/message")
async def api_remote_send_prompt(
    body: RemotePromptBody, request: Request, conversation_id: str = Query("")
) -> JSONResponse:
    if error := _remote_access_error(request):
        return error
    text = body.text.strip()
    if not text or len(text) > 120_000:
        return JSONResponse({"error": "prompt must contain 1-120000 characters"}, status_code=400)
    target = conversation_id.strip()
    if not target:
        return JSONResponse({"error": "conversation_id is required"}, status_code=400)
    loaded = await _activate_remote_mewcode_conversation(target, project_id=body.project_id)
    if loaded is None:
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    target = str(loaded.get("id") or target)
    if SESSION.chat_busy(target):
        queue_id = "q" + secrets.token_hex(8)
        try:
            queued = SESSION.enqueue_chat_turn(target, queue_id, text, [])
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc) or "conversation is busy"}, status_code=409)
        except OverflowError as exc:
            return JSONResponse({"error": str(exc)}, status_code=429)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        await _broadcast_remote_conversation_focus(
            target, str(loaded.get("scope") or SESSION.scope or "project"), mode="send"
        )
        await _publish_chat_queue(queued)
        return JSONResponse({
            "conversation_id": target,
            "status": "queued",
            "queued": True,
            "queue_id": queued.get("accepted_id") or queue_id,
        })
    if SESSION.agent is None:
        return JSONResponse({"error": "电脑端模型尚未就绪，请先在电脑上选择可用模型后重试"}, status_code=409)
    try:
        runtime = SESSION.capture_conversation_runtime()
    except RuntimeError:
        return JSONResponse({"error": "电脑端当前对话尚未准备好，请在电脑上打开该对话并确认模型可用后重试"}, status_code=409)
    if runtime.conversation_id != target:
        return JSONResponse({"error": "电脑端当前对话已切换，请重试"}, status_code=409)
    SESSION.begin_conversation_turn(runtime)
    operation_id = secrets.token_urlsafe(12)
    remote_sink = RemoteOperationSink(operation_id, runtime.conversation_id)
    REMOTE_OPERATIONS[operation_id] = remote_sink
    sink = FanoutJsonSink(
        remote_sink,
        ConversationWebSocket(WS_EVENTS, runtime.conversation_id),
    )
    try:
        await _broadcast_remote_conversation_focus(
            target, str(loaded.get("scope") or runtime.scope), mode="send"
        )
        await sink.send_json({
            "type": "turn_started",
            "conversation_id": target,
            "text": text,
            "attachments": [],
            "queued": False,
            "expert_mode": False,
            "client_echoed": False,
        })
        task = asyncio.create_task(run_chat(sink, text, runtime=runtime))
        SESSION.attach_conversation_task(runtime, task)
    except BaseException:
        runtime.starting = False
        REMOTE_OPERATIONS.pop(operation_id, None)
        raise
    return JSONResponse({
        "operation_id": operation_id,
        "conversation_id": runtime.conversation_id,
        "status": remote_sink.status,
    })


@app.get("/api/remote/operations/{operation_id}")
async def api_remote_operation(operation_id: str, request: Request) -> JSONResponse:
    if error := _remote_access_error(request):
        return error
    operation = REMOTE_OPERATIONS.get(operation_id)
    if operation is None:
        return JSONResponse({"error": "operation not found"}, status_code=404)
    return JSONResponse({
        "operation_id": operation.operation_id,
        "conversation_id": operation.conversation_id,
        "status": operation.status,
        "started_at": operation.started_at,
        "events": operation.events,
    })


@app.get("/api/remote/live")
async def api_remote_live(
    request: Request, conversation_id: str = Query(""), project_id: str = Query("")
) -> JSONResponse:
    if error := _remote_access_error(request):
        return error
    target = conversation_id.strip()
    if not target:
        return JSONResponse({"error": "conversation_id is required"}, status_code=400)
    if project_id:
        directory = _remote_project_dir(project_id)
        if directory is None:
            return JSONResponse({"error": "conversation not found"}, status_code=404)
        runtime = SESSION.runtime_for(target)
        active_match = (
            SESSION.session is not None
            and SESSION.session.session_id == target
            and SESSION.scope == "project"
            and _remote_project_id(SESSION.work_dir) == project_id
        )
        runtime_match = (
            runtime is not None
            and runtime.scope == "project"
            and _remote_project_id(runtime.work_dir) == project_id
        )
        if _remote_conversation_conflicts(target, "project", directory):
            return JSONResponse({"error": "conversation not found"}, status_code=404)
        item = _remote_history_item(target, project_id)
        if not (active_match or runtime_match):
            if item is None:
                return JSONResponse({"error": "conversation not found"}, status_code=404)
            return JSONResponse({
                "conversation_id": target,
                "active": False,
                "running": False,
                "execution_kind": str(item.get("execution_kind") or "chat"),
                "expert_status": str(item.get("expert_status") or ""),
                "resumable": bool(item.get("resumable")),
                "run_id": str(item.get("run_id") or ""),
                "run": None,
                "steps": [],
                "current_step": None,
                "events": [],
                "pending": [],
            })
        if item is None:
            return JSONResponse({"error": "conversation not found"}, status_code=404)
        return JSONResponse(_remote_live_payload(target))
    item = _remote_history_item(target)
    runtime = SESSION.runtime_for(target)
    snapshot = _conversation_expert_snapshot(target)
    if item is None and runtime is None and snapshot is None and target not in REMOTE_LIVES:
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    return JSONResponse(_remote_live_payload(target))


@app.post("/api/remote/conversations/interrupt")
async def api_remote_interrupt(body: RemoteInterruptBody, request: Request) -> JSONResponse:
    if error := _remote_access_error(request):
        return error
    loaded = await _activate_remote_mewcode_conversation(
        body.conversation_id, project_id=body.project_id
    )
    if loaded is None:
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    target = str(loaded.get("id") or body.conversation_id)
    await _broadcast_remote_conversation_focus(
        target, str(loaded.get("scope") or "project"), mode="interrupt"
    )
    result = await request_chat_interrupt(target, action="pause")
    return JSONResponse(result)


@app.post("/api/remote/conversations/resume")
async def api_remote_resume(body: RemoteResumeBody, request: Request) -> JSONResponse:
    if error := _remote_access_error(request):
        return error
    loaded = await _activate_remote_mewcode_conversation(
        body.conversation_id, project_id=body.project_id
    )
    if loaded is None:
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    target = str(loaded.get("id") or body.conversation_id)
    if SESSION.chat_busy(target):
        return JSONResponse({"error": "conversation is busy"}, status_code=409)
    snapshot = EXPERT_RUNS.load(body.run_id) if body.run_id.strip() else EXPERT_RUNS.latest_resumable(target)
    if snapshot is None or str(snapshot.get("conversation_id") or "") != target:
        return JSONResponse({"error": "没有可接力的专家团检查点"}, status_code=409)
    if snapshot.get("pending_recovery_revision"):
        return JSONResponse({"error": "有待批准的计划修订，请先在修订卡中决定"}, status_code=409)
    await _broadcast_remote_conversation_focus(
        target, str(loaded.get("scope") or "project"), mode="resume"
    )
    started = await launch_expert_resume(snapshot)
    return JSONResponse(started)


@app.post("/api/remote/prompts")
async def api_remote_prompt_reply(body: RemotePromptReplyBody, request: Request) -> JSONResponse:
    if error := _remote_access_error(request):
        return error
    loaded = await _activate_remote_mewcode_conversation(
        body.conversation_id, project_id=body.project_id
    )
    if loaded is None:
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    target = str(loaded.get("id") or body.conversation_id)
    from frontend.prompt_dispatch import dispatch, PromptError
    values = body.model_dump()
    values['conversation_id'] = target
    if values['kind'] == 'plan_revision' and not values.get('run_id'):
        values['run_id'] = str((_conversation_expert_snapshot(target) or {}).get('run_id') or '')
    try:
        return JSONResponse(await dispatch(sys.modules[__name__], body.kind.strip().lower(), values, transport='remote'))
    except PromptError as exc:
        return JSONResponse({'error': str(exc)}, status_code=exc.status)



@app.get("/api/tailscale/nodes")
async def api_remote_nodes() -> JSONResponse:
    config = _tailscale_config()
    local = await asyncio.to_thread(collect_tailscale_status, config)
    nodes = configured_remote_nodes(config)
    known = {str(item.get("id")) for item in nodes}
    for item in discovered_remote_nodes():
        if str(item.get("id")) not in known:
            nodes.append(item)
    return JSONResponse({"local": local, "nodes": nodes})


async def _proxy_remote_node(
    node_id: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> JSONResponse:
    try:
        value = await asyncio.to_thread(
            remote_node_request, _tailscale_config(), node_id, path,
            method=method, payload=payload,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(value)


@app.get("/api/tailscale/nodes/{node_id}/info")
async def api_remote_node_info(node_id: str) -> JSONResponse:
    return await _proxy_remote_node(node_id, "/api/remote/info")


@app.get("/api/tailscale/nodes/{node_id}/history")
async def api_remote_node_history(node_id: str, source: str = Query("")) -> JSONResponse:
    suffix = f"?source={quote(source, safe='')}" if source else ""
    return await _proxy_remote_node(node_id, "/api/remote/history" + suffix)


@app.get("/api/tailscale/nodes/{node_id}/history/item")
async def api_remote_node_history_item(
    node_id: str, source: str = Query(""), external_id: str = Query("")
) -> JSONResponse:
    suffix = "?source=" + quote(source, safe="") + "&external_id=" + quote(external_id, safe="")
    return await _proxy_remote_node(node_id, "/api/remote/history/item" + suffix)


@app.post("/api/tailscale/nodes/{node_id}/conversations")
async def api_remote_node_new_conversation(node_id: str, body: NewConvBody) -> JSONResponse:
    return await _proxy_remote_node(node_id, "/api/remote/conversations", method="POST", payload=body.model_dump())


@app.post("/api/tailscale/nodes/{node_id}/conversations/activate")
async def api_remote_node_activate_conversation(node_id: str, body: RemoteActivateBody) -> JSONResponse:
    return await _proxy_remote_node(
        node_id, "/api/remote/conversations/activate", method="POST", payload=body.model_dump()
    )


@app.post("/api/tailscale/nodes/{node_id}/conversations/message")
async def api_remote_node_send_prompt(
    node_id: str, conversation_id: str = Query(""), body: RemotePromptBody = ...
) -> JSONResponse:
    path = "/api/remote/conversations/message?conversation_id=" + quote(conversation_id, safe="")
    return await _proxy_remote_node(node_id, path, method="POST", payload=body.model_dump())


@app.get("/api/tailscale/nodes/{node_id}/operations/{operation_id}")
async def api_remote_node_operation(node_id: str, operation_id: str) -> JSONResponse:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", operation_id):
        return JSONResponse({"error": "invalid operation id"}, status_code=400)
    return await _proxy_remote_node(node_id, "/api/remote/operations/" + operation_id)




def _load_review_model() -> dict:
    default = {"model_id": "", "provider_key": "", "source": "manual"}
    try:
        if REVIEW_MODEL_FILE.is_file():
            data = json.loads(REVIEW_MODEL_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                model_id = str(data.get("model_id") or "").strip()
                provider_key = str(data.get("provider_key") or "").strip()
                return {
                    "model_id": model_id,
                    "provider_key": provider_key,
                    "source": str(data.get("source") or "manual"),
                    "vision_model_id": str(data.get("vision_model_id") or "").strip(),
                    "vision_provider_key": str(data.get("vision_provider_key") or "").strip(),
                    "backup_model_id": str(data.get("backup_model_id") or "").strip(),
                    "backup_provider_key": str(data.get("backup_provider_key") or "").strip(),
                }
    except (OSError, TypeError, ValueError):
        pass
    return default


def _save_review_model(model_id: str, provider_key: str = "", *, vision: dict | None = None,
                       backup: dict | None = None) -> dict:
    previous = _load_review_model()
    payload = {
        "model_id": (model_id or "").strip(),
        "provider_key": (provider_key or "").strip(),
        "source": "manual",
        "vision_model_id": str((vision if vision is not None else previous).get("vision_model_id") or "").strip(),
        "vision_provider_key": str((vision if vision is not None else previous).get("vision_provider_key") or "").strip(),
        "backup_model_id": str((backup if backup is not None else previous).get("backup_model_id") or "").strip(),
        "backup_provider_key": str((backup if backup is not None else previous).get("backup_provider_key") or "").strip(),
    }
    REVIEW_MODEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=REVIEW_MODEL_FILE.parent,
                                     prefix=".review-model-", suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    try:
        os.replace(temporary, REVIEW_MODEL_FILE)
    finally:
        temporary.unlink(missing_ok=True)
    return payload




@app.get("/api/cc-switch/current")
async def api_cc_switch_current() -> JSONResponse:
    return JSONResponse(await asyncio.to_thread(read_cc_switch_snapshot), headers={"Cache-Control": "no-store"})


@app.get("/api/settings/review-model")
async def api_get_review_model() -> JSONResponse:
    return JSONResponse(_load_review_model(), headers={"Cache-Control": "no-store"})


@app.post("/api/settings/review-model")
async def api_set_review_model(body: dict) -> JSONResponse:
    from mewcode.model_registry import ModelRef
    model_id = str((body or {}).get("model_id") or "").strip()
    provider_key = str((body or {}).get("provider_key") or "").strip()
    if not model_id or not provider_key:
        return JSONResponse({"error": "审核模型需要同时选择 Provider 和模型"}, status_code=400)
    if model_id:
        try:
            MODEL_REGISTRY.resolve(ModelRef(provider_key, model_id))
        except (ValueError, KeyError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    vision = None
    if "vision_model_id" in body or "vision_provider_key" in body:
        vision = {key: str(body.get(key) or "").strip() for key in ("vision_model_id", "vision_provider_key")}
        if bool(vision["vision_model_id"]) != bool(vision["vision_provider_key"]):
            return JSONResponse({"error": "看图能力需要同时选择 Provider 和模型"}, status_code=400)
        if vision["vision_model_id"]:
            try:
                visual = MODEL_REGISTRY.resolve(ModelRef(vision["vision_provider_key"], vision["vision_model_id"]))
                if "vision" not in visual.capabilities or getattr(getattr(visual, 'client', None), 'review_only', False) is True:
                    raise ValueError("看图能力模型必须支持图片，且不能使用 Jev 或 review_only 模型")
            except (ValueError, KeyError) as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
    backup = None
    if 'backup_model_id' in body or 'backup_provider_key' in body:
        backup = {key: str(body.get(key) or '').strip() for key in ('backup_model_id', 'backup_provider_key')}
        if bool(backup['backup_model_id']) != bool(backup['backup_provider_key']):
            return JSONResponse({'error': '备用审核需要同时选择 Provider 和模型'}, status_code=400)
        if backup['backup_model_id']:
            try:
                MODEL_REGISTRY.resolve(ModelRef(backup['backup_provider_key'], backup['backup_model_id']))
            except (ValueError, KeyError) as exc:
                return JSONResponse({'error': str(exc)}, status_code=400)
    payload = _save_review_model(model_id, provider_key, vision=vision, backup=backup)
    # 立即重绑运行中会话的评审客户端；否则新选择要等重启/切换主模型才生效。
    from mewcode.permissions.review_client import resolve_safety_review_client
    agent = getattr(SESSION, "agent", None)
    reviewer = getattr(agent, "safety_reviewer", None) if agent is not None else None
    if reviewer is not None:
        fallback = getattr(agent, "auxiliary_client", None) or SESSION.client
        reviewer.client = resolve_safety_review_client(
            MODEL_REGISTRY,
            fallback_client=fallback,
            usage_recorder=(
                SESSION.client.get_usage_recorder() if SESSION.client else None
            ),
            settings_path=REVIEW_MODEL_FILE,
        )
        denials = getattr(reviewer, "_denials", None)
        if isinstance(denials, dict):
            denials.clear()
        from mewcode.permissions.review_client import bind_safety_reviewer
        bind_safety_reviewer(agent, MODEL_REGISTRY, settings_path=REVIEW_MODEL_FILE)
    return JSONResponse(payload)


@app.get("/api/models/catalog")
async def api_model_catalog(
    refresh: bool = Query(False), fast: bool = Query(False),
    include_intelligence: bool = Query(True),
) -> JSONResponse:
    mirrored = await asyncio.to_thread(SESSION.sync_cc_switch_mirror)
    if mirrored:
        await WS_EVENTS.send_json({
            "type": "state",
            **SESSION.client_state_payload(),
        })
    if fast or not include_intelligence:
        catalog = await MODEL_REGISTRY.catalog(force=refresh, local_only=fast)
        catalog["capability_catalog"] = MODEL_CAPABILITY_CATALOG.snapshot()
        return JSONResponse(catalog, headers={"Cache-Control": "no-store"})
    catalog, snapshot = await asyncio.gather(
        MODEL_REGISTRY.catalog(force=refresh),
        MODEL_INTELLIGENCE.get(force=refresh),
    )
    for model in catalog.get("models", []):
        model_id = model.get("model_id", "")
        policy = MODEL_CONTEXT_ROUTER.resolve(model_id)
        model["context_policy"] = policy.name
        model["context_stable_prefix"] = bool(
            policy.stable_system or policy.stable_tools
        )
        model["effort_changes_cache_affinity"] = bool(
            policy.effort_affects_cache_affinity
        )
        model["iq"] = MODEL_INTELLIGENCE.model_summary(
            snapshot, model_id, "auto"
        )
        intelligence_by_effort = MODEL_INTELLIGENCE.model_by_effort(
            snapshot, model_id
        )
        model["intelligence_by_effort"] = intelligence_by_effort
        model["iq_by_effort"] = intelligence_by_effort
        model["capabilities"] = [
            "chat", "tools", *( ["vision"] if model.get("vision") else [] )
        ]
    catalog["intelligence"] = snapshot.as_dict()
    catalog["cc_switch"] = await asyncio.to_thread(read_cc_switch_snapshot)
    catalog["review_model"] = _load_review_model()
    capability_catalog = MODEL_CAPABILITY_CATALOG.snapshot()
    for model in capability_catalog["models"]:
        policy = MODEL_CONTEXT_ROUTER.resolve(model["model_id"])
        model["context_policy"] = policy.name
        model["context_stable_prefix"] = bool(
            policy.stable_system or policy.stable_tools
        )
        model["effort_changes_cache_affinity"] = bool(
            policy.effort_affects_cache_affinity
        )
    catalog["capability_catalog"] = capability_catalog
    return JSONResponse(catalog, headers={"Cache-Control": "no-store"})


@app.get("/api/model-intelligence")
async def api_model_intelligence(refresh: bool = Query(False)) -> JSONResponse:
    """Stable server-side proxy for the CodexRadar intelligence snapshot."""
    snapshot = await MODEL_INTELLIGENCE.get(force=refresh)
    return JSONResponse(
        snapshot.as_dict(), headers={"Cache-Control": "no-store"}
    )


@app.post("/api/chat/attachments")
async def api_upload_chat_attachments(
    files: list[UploadFile] = File(...),
) -> JSONResponse:
    if not files:
        return JSONResponse({"error": "请选择附件"}, status_code=400)
    if len(files) > 10:
        return JSONResponse({"error": "每条消息最多添加 10 个附件"}, status_code=400)
    if SESSION.session is None:
        SESSION.new_conversation(SESSION.scope)
    assert SESSION.session is not None
    saved = []
    total = 0
    try:
        for upload in files:
            data = await upload.read(MAX_FILE_BYTES + 1)
            total += len(data)
            if len(data) > MAX_FILE_BYTES:
                raise AttachmentError("单个附件不能超过 20 MiB")
            if total > MAX_MESSAGE_BYTES:
                raise AttachmentError("单条消息的附件总大小不能超过 50 MiB")
            ref = ATTACHMENTS.save(
                SESSION.session.session_id,
                upload.filename or "attachment",
                data,
                upload.content_type or "",
            )
            saved.append(ref)
    except AttachmentError as exc:
        for ref in saved:
            ATTACHMENTS.delete(ref.conversation_id, ref.id)
        return JSONResponse({"error": str(exc)}, status_code=400)
    finally:
        for upload in files:
            await upload.close()
    return JSONResponse({"attachments": [ref.as_dict() for ref in saved]})


@app.get("/api/chat/attachments/{attachment_id}")
async def api_get_chat_attachment(attachment_id: str):
    try:
        ref = ATTACHMENTS.find(attachment_id)
        path = ATTACHMENTS.asset_path(ref)
    except AttachmentError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return FileResponse(
        path,
        media_type=ref.mime_type,
        filename=ref.name,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
    )


@app.delete("/api/chat/attachments/{attachment_id}")
async def api_delete_chat_attachment(attachment_id: str) -> JSONResponse:
    try:
        ref = ATTACHMENTS.find(attachment_id)
        ATTACHMENTS.delete(ref.conversation_id, ref.id)
    except AttachmentError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse({"deleted": True})


@app.get("/api/expert-profile")
async def api_expert_profile() -> JSONResponse:
    profile = _heal_stored_expert_profile()
    invalid = await _invalid_expert_roles(profile) if profile is not None else []
    recommended = None
    if SESSION.slot is not None:
        current = {
            "provider_key": SESSION.slot.key,
            "model_id": SESSION.slot.provider.model,
            "effort": SESSION.efforts_payload()["current"],
        }
        if current["effort"] == "auto":
            current["effort"] = ""
        try:
            current = MODEL_REGISTRY.relocate_model_ref(current).as_dict()
        except ValueError:
            pass
        recommended = {
            "manager": dict(current),
            "executor": dict(current),
            "auditor": dict(current),
            "planner": None,
            "capability_executor": None,
            "planner_routes": {},
        }
    roles = None
    from mewcode.kernel.models import RoleModels
    if profile or recommended:
        try:
            roles = RoleModels(await MODEL_REGISTRY.catalog(local_only=True)).bind(
                profile or ExpertProfile.from_value(recommended))
        except ValueError:
            pass
    return JSONResponse({
        "configured": profile is not None,
        "role_models": roles,
        "project": SESSION.work_dir,
        "profile": profile.as_dict() if profile else None,
        "invalid_roles": invalid,
        "issues": {
            role: "Provider、模型或推理强度不可用"
            for role in invalid
        },
        "recommended_profile": recommended,
        "can_activate": bool((profile or recommended) and not invalid),
        "expert_mode": SESSION.expert_mode,
        "role_contracts": {
            role: {
                "tools": list(contract.tools),
                "responsibility": contract.responsibility,
                "may_write": contract.may_write,
                "may_delegate": contract.may_delegate,
            }
            for role, contract in EXPERT_ROLE_CONTRACTS.items()
        },
    })


@app.put("/api/expert-profile")
async def api_put_expert_profile(body: ExpertProfileBody) -> JSONResponse:
    owner_project = SESSION.work_dir
    owner_conversation = SESSION.session.session_id if SESSION.session is not None else ""
    if body.conversation_id or body.project:
        if not _capability_executor_scope_matches(CapabilityExecutorBody(
            conversation_id=body.conversation_id, project=body.project,
        )):
            return JSONResponse({"error": "对话或工程已切换，未保存专家配置"}, status_code=409)
    try:
        profile = ExpertProfile.from_value(body.model_dump())
        profile = relocate_expert_profile(MODEL_REGISTRY, profile)
        invalid = await _invalid_expert_roles(profile)
        if invalid:
            raise ValueError(
                "unavailable expert role models: " + ", ".join(invalid)
            )
        if (SESSION.work_dir != owner_project
                or (SESSION.session.session_id if SESSION.session is not None else "") != owner_conversation):
            return JSONResponse({"error": "对话或工程已切换，未保存专家配置"}, status_code=409)
        saved = EXPERT_PROFILES.put(owner_project, profile)
        if body.activate:
            error = SESSION.set_expert_mode(True)
            if error:
                raise ValueError(error)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({
        "project": SESSION.work_dir,
        "profile": saved.as_dict(),
        "expert_mode": SESSION.expert_mode,
        "invalid_roles": [],
    })


@app.put("/api/expert-profile/capability-executor")
async def api_put_project_capability_executor(body: CapabilityExecutorBody) -> JSONResponse:
    if not _capability_executor_scope_matches(body):
        return JSONResponse({"error": "对话或工程已切换，请重新打开辅助执行器配置"}, status_code=409)
    try:
        selected = await _validated_capability_executor(body.model)
        if not _capability_executor_scope_matches(body):
            return JSONResponse({"error": "对话或工程已切换，未保存辅助执行器"}, status_code=409)
        saved = EXPERT_PROFILES.set_capability_executor(SESSION.work_dir, selected)
    except (ValueError, OSError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"project": SESSION.work_dir, "conversation_id": body.conversation_id,
                         "capability_executor": saved.capability_executor.as_dict()
                         if saved.capability_executor else None})


def _expert_flow_extensions() -> dict[str, Any]:
    role_registry = APP_CONTEXT.get(EXPERTS)
    flow_registry = APP_CONTEXT.get(EXPERT_FLOWS)
    routers = APP_CONTEXT.slots.resolve(EXPERT_ROUTER_SLOT) or {}
    reducers = APP_CONTEXT.slots.resolve(EXPERT_REDUCER_SLOT) or {}
    finalizers = APP_CONTEXT.slots.resolve(EXPERT_FINALIZER_SLOT) or {}
    pipeline_store = PipelineStore(SESSION.work_dir)
    pipelines: list[dict[str, Any]] = []
    for item in pipeline_store.list_pipelines():
        graph = pipeline_store.get_pipeline(item.graph_id)
        if graph is None:
            continue
        pipelines.append({
            **item.model_dump(mode="json"),
            "nodes": [{
                "id": node.id,
                "type": node.type.value,
                "label": node.get_display_name(),
                "output_key": node.output_key,
            } for node in graph.nodes],
            "edges": [{
                "from_node": edge.from_node,
                "to_node": edge.to_node,
                "label": edge.label,
            } for edge in graph.edges],
        })
    templates = [flow.as_dict() for flow in flow_registry.list()]
    # Orchestration product templates surface in the shared flow picker too.
    seen = {str(item.get("flow_id") or "") for item in templates}
    for item in list_templates():
        try:
            orch = get_template(str(item["template_id"]))
            flow = to_expert_flow(orch)
        except (OrchestrationError, KeyError, TypeError, ValueError):
            continue
        if flow.flow_id in seen:
            continue
        templates.append(flow.as_dict())
        seen.add(flow.flow_id)
    return {
        "roles": [definition.as_dict() for definition in role_registry.list()],
        "templates": templates,
        "routers": ["default", *sorted(routers)],
        "reducers": ["collect", *sorted(reducers)],
        "finalizers": ["model", *sorted(finalizers)],
        "pipelines": pipelines,
    }


@app.get("/api/expert-flow")
async def api_get_expert_flow() -> JSONResponse:
    extensions = _expert_flow_extensions()
    custom = _conversation_flow_store().get(SESSION.work_dir)
    templates = extensions["templates"]
    selected = custom or ExpertFlowSpec.from_value(templates[0])
    return JSONResponse({
        "project": SESSION.work_dir,
        "conversation_id": _active_conversation_id(),
        "custom": custom is not None,
        "flow": selected.as_dict(),
        **extensions,
    })


@app.get("/api/orchestration/kinds")
async def api_get_orchestration_kinds() -> JSONResponse:
    from mewcode.orchestration.kinds import kind_roles
    from mewcode.orchestration.loops import LOOP_BUDGET_LIMITS
    return JSONResponse({"kinds": [{"kind": kind, "role": role} for kind, role in
        kind_roles(APP_CONTEXT.get(EXPERTS, None)).items()], "loop_budget_limits": LOOP_BUDGET_LIMITS})


@app.get("/api/expert/lessons")
async def api_get_expert_lessons(flow_id: str = "") -> JSONResponse:
    if not flow_id:
        return JSONResponse({"error": "flow_id is required"}, status_code=400)
    try:
        return JSONResponse({"flow_id": flow_id, "lessons": EXPERT_LESSONS.list(flow_id)})
    except (OSError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/expert/lessons")
async def api_add_expert_lesson(request: Request, flow_id: str = "") -> JSONResponse:
    try:
        body = await request.json()
        lesson = EXPERT_LESSONS.add(flow_id, str(body.get("scope") or "*"), str(body.get("text") or ""), source="user")
        return JSONResponse({"lesson": lesson})
    except (OSError, ValueError, AttributeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.patch("/api/expert/lessons")
async def api_update_expert_lesson(request: Request, flow_id: str = "") -> JSONResponse:
    try:
        body = await request.json()
        lesson = EXPERT_LESSONS.update(flow_id, str(body.get("lesson_id") or ""), status=body.get("status"),
                                      text=body.get("text"), delete=body.get("delete") is True)
        return JSONResponse({"lesson": lesson})
    except KeyError:
        return JSONResponse({"error": "Lesson not found"}, status_code=404)
    except (OSError, ValueError, AttributeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.put("/api/expert-flow")
async def api_put_expert_flow(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
        raw_flow = payload.get("flow") if isinstance(payload, dict) else None
        flow = ExpertFlowSpec.from_value(raw_flow)
        extensions = _expert_flow_extensions()
        flow.validate(
            roles={str(item.get("name") or "") for item in extensions["roles"]},
            routers=set(extensions["routers"]),
            reducers=set(extensions["reducers"]),
            finalizers=set(extensions["finalizers"]),
            pipelines={
                str(item.get("graph_id") or "")
                for item in extensions["pipelines"]
            },
        )
        saved = _conversation_flow_store().put(SESSION.work_dir, flow)
    except (ExpertFlowError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({
        "project": SESSION.work_dir,
        "custom": True,
        "flow": saved.as_dict(),
    })


@app.delete("/api/expert-flow")
async def api_delete_expert_flow() -> JSONResponse:
    _conversation_flow_store().delete(SESSION.work_dir)
    flow_registry = APP_CONTEXT.get(EXPERT_FLOWS)
    default = flow_registry.get("builtin.default") or flow_registry.list()[0]
    return JSONResponse({
        "project": SESSION.work_dir,
        "custom": False,
        "flow": default.as_dict(),
    })


def _orchestration_plugin_templates() -> list[dict[str, str]]:
    registered = APP_CONTEXT.slots.resolve(ORCHESTRATION_TEMPLATE_SLOT) or {}
    extra: list[dict[str, str]] = []
    for key, value in sorted(registered.items()):
        if isinstance(value, dict):
            extra.append({
                "template_id": str(value.get("template_id") or key),
                "label": str(value.get("label") or key),
                "spec_id": str(value.get("spec_id") or key),
                "node_count": str(value.get("node_count") or ""),
                "source": "plugin",
            })
        else:
            extra.append({
                "template_id": str(key),
                "label": str(key),
                "spec_id": str(key),
                "node_count": "",
                "source": "plugin",
            })
    return extra


@app.get("/api/orchestration/guide")
async def orchestration_guide() -> dict:
    from mewcode.orchestration_guide import orchestration_component_guide

    return {"markdown": orchestration_component_guide()}


@app.get("/api/orchestration/templates")
async def api_orchestration_templates() -> JSONResponse:
    templates = list_templates()
    for item in templates:
        item["source"] = "builtin"
    templates.extend(_orchestration_plugin_templates())
    return JSONResponse({
        "templates": templates,
        "node_kinds": ["plan", "implement", "reduce", "audit", "gate"],
        "plugin_nodes": sorted((APP_CONTEXT.slots.resolve(ORCHESTRATION_NODE_SLOT) or {}).keys()),
        "plugin_gates": sorted((APP_CONTEXT.slots.resolve(ORCHESTRATION_GATE_SLOT) or {}).keys()),
    })


def _orchestration_operation_id(payload: dict[str, Any]) -> str:
    operation_id = str(payload.get("operation_id") or "").strip()
    if not operation_id:
        return ""
    if len(operation_id) > 96 or _SAFE_ID_RE.fullmatch(operation_id) is None:
        raise OrchestrationError("无效的编排操作 id")
    return operation_id


def _prune_orchestration_progress() -> None:
    cutoff = int(
        (time.time() - _ORCHESTRATION_PROGRESS_RETENTION_SECONDS) * 1000
    )
    for operation_id, progress in list(_ORCHESTRATION_OPERATION_PROGRESS.items()):
        updated_at = int(progress.get("updated_at_ms") or 0)
        if updated_at and updated_at < cutoff:
            _ORCHESTRATION_OPERATION_PROGRESS.pop(operation_id, None)
            _ORCHESTRATION_OPERATION_RESULTS.pop(operation_id, None)
            _ORCHESTRATION_OPERATION_FAILURES.pop(operation_id, None)


def _start_orchestration_progress(operation_id: str, *, kind: str) -> None:
    if not operation_id:
        return
    _prune_orchestration_progress()
    now = int(time.time() * 1000)
    _ORCHESTRATION_OPERATION_PROGRESS[operation_id] = {
        "operation_id": operation_id,
        "kind": kind,
        "status": "running",
        "stage": "queued",
        "detail": "编排请求已进入服务端，正在准备模型调用。",
        "started_at_ms": now,
        "updated_at_ms": now,
        "thinking_chars": 0,
        "thinking_summary_chars": 0,
        "thinking_preview": "",
        "thinking_truncated": False,
        "output_chars": 0,
        "output_chunks": 0,
        "provider_events": 0,
        "last_provider_event_at_ms": 0,
        "last_activity_at_ms": now,
        "timeout_mode": "stage_inactivity",
        "first_event_timeout_seconds": int(
            ORCHESTRATION_FIRST_EVENT_TIMEOUT_SECONDS
        ),
        "idle_timeout_seconds": int(ORCHESTRATION_IDLE_TIMEOUT_SECONDS),
        "reasoning_idle_timeout_seconds": int(
            ORCHESTRATION_REASONING_IDLE_TIMEOUT_SECONDS
        ),
        "soft_idle_warning_seconds": int(
            ORCHESTRATION_SOFT_IDLE_WARNING_SECONDS
        ),
        "events": [{
            "stage": "queued",
            "detail": "编排请求已进入服务端，正在准备模型调用。",
            "at_ms": now,
        }],
    }


def _update_orchestration_progress(
    operation_id: str,
    update: dict[str, Any],
) -> None:
    if not operation_id:
        return
    now = int(time.time() * 1000)
    progress = _ORCHESTRATION_OPERATION_PROGRESS.setdefault(
        operation_id,
        {
            "operation_id": operation_id,
            "kind": "graph",
            "status": "running",
            "stage": "queued",
            "detail": "正在准备编排请求。",
            "started_at_ms": now,
            "thinking_chars": 0,
            "thinking_summary_chars": 0,
            "thinking_preview": "",
            "thinking_truncated": False,
            "output_chars": 0,
            "output_chunks": 0,
            "provider_events": 0,
            "last_provider_event_at_ms": 0,
            "last_activity_at_ms": now,
            "timeout_mode": "stage_inactivity",
            "first_event_timeout_seconds": int(
                ORCHESTRATION_FIRST_EVENT_TIMEOUT_SECONDS
            ),
            "idle_timeout_seconds": int(ORCHESTRATION_IDLE_TIMEOUT_SECONDS),
            "reasoning_idle_timeout_seconds": int(
                ORCHESTRATION_REASONING_IDLE_TIMEOUT_SECONDS
            ),
            "soft_idle_warning_seconds": int(
                ORCHESTRATION_SOFT_IDLE_WARNING_SECONDS
            ),
            "events": [],
        },
    )
    values = dict(update)
    provider_event = bool(values.pop("provider_event", False))
    if provider_event and "activity_detail" not in values:
        values["activity_detail"] = ""
    previous_stage = str(progress.get("stage") or "")
    previous_detail = str(progress.get("detail") or "")
    progress.update(values)
    current_stage = str(progress.get("stage") or "")
    current_detail = str(progress.get("detail") or "")
    if current_stage and (
        current_stage != previous_stage or current_detail != previous_detail
    ):
        events = progress.setdefault("events", [])
        if isinstance(events, list):
            events.append({
                "stage": current_stage,
                "detail": current_detail,
                "at_ms": now,
            })
            del events[:-80]
    if provider_event:
        progress["last_provider_event_at_ms"] = now
    progress["last_activity_at_ms"] = now
    progress["updated_at_ms"] = now


def _orchestration_progress_reporter(
    operation_id: str,
) -> Callable[[dict[str, Any]], None] | None:
    if not operation_id:
        return None

    def report(update: dict[str, Any]) -> None:
        if isinstance(update, dict):
            _update_orchestration_progress(operation_id, update)

    return report


def _orchestration_timeout_diagnostic(operation_id: str) -> str:
    progress = _ORCHESTRATION_OPERATION_PROGRESS.get(operation_id) or {}
    output_chars = int(progress.get("output_chars") or 0)
    thinking_chars = int(progress.get("thinking_chars") or 0)
    tool_call_chars = int(progress.get("tool_call_chars") or 0)
    provider_events = int(progress.get("provider_events") or 0)
    stalled_phase = str(progress.get("stalled_phase") or "")
    if stalled_phase == "queue":
        return "本地模型调度队列等待超时，尚未发送 Provider 请求。"
    idle_seconds = int(
        (
            progress.get("reasoning_idle_timeout_seconds")
            or ORCHESTRATION_REASONING_IDLE_TIMEOUT_SECONDS
        )
        if stalled_phase == "reasoning_idle"
        else (
            progress.get("idle_timeout_seconds")
            or ORCHESTRATION_IDLE_TIMEOUT_SECONDS
        )
    )
    if output_chars:
        return (
            f"模型已流式返回 {output_chars} 个正文字符，但此后连续 "
            f"{idle_seconds} 秒没有新活动。"
        )
    if thinking_chars:
        return (
            f"模型已返回 {thinking_chars} 个推理流字符，但此后连续 "
            f"{idle_seconds} 秒没有新活动。"
        )
    if tool_call_chars:
        return (
            f"模型已生成 {tool_call_chars} 个工具参数字符，但此后连续 "
            f"{idle_seconds} 秒没有新活动。"
        )
    if provider_events:
        return (
            f"已收到 {provider_events} 个 Provider 生命周期事件，但此后连续 "
            f"{idle_seconds} 秒没有流或工具活动。"
        )
    return "请求发送后始终没有收到 Provider 流事件，可能仍在排队或建立连接。"


def _orchestration_provider_failure(exc: LLMError) -> dict[str, Any]:
    """Turn known gateway stream failures into actionable UI diagnostics."""

    detail = str(exc).strip()
    lowered = detail.lower()
    if "upstream_stream_idle_timeout" in lowered:
        return {
            "error": (
                "Grok 上游在有效活动窗口内没有返回正文、推理摘要或工具进展，"
                "Grok2API 已结束该流。"
            ),
            "timeout": True,
            "provider_error_code": "upstream_stream_idle_timeout",
            "provider_error_detail": detail,
            "activity_rule": (
                "正文、推理文本、推理摘要或工具调用活动都会重新开始静默计时；"
                "心跳和内部控制事件不会续时。"
            ),
        }
    if "client_stream_interrupted" in lowered:
        return {
            "error": (
                "Grok2API 检测到下游客户端提前断开；若并非手动取消，"
                "请确认 MewCode 已重启到最新等待策略。"
            ),
            "provider_error_code": "client_stream_interrupted",
            "provider_error_detail": detail,
        }
    return {"error": f"编排规划失败：{detail}"}


def _orchestration_attempt_context(effort_override: str = "") -> dict[str, Any]:
    """Freeze provider state so a background planner cannot drift with the UI."""

    if SESSION.slot is None or SESSION.client is None:
        raise OrchestrationError("当前模型未就绪，请先选择模型")
    provider = copy.deepcopy(SESSION.slot.provider)
    levels = SESSION.effort_levels()
    requested = str(
        effort_override
        or SESSION.effort
        or getattr(SESSION.client, "effort", "")
        or SESSION.slot.default_effort
        or ""
    ).strip()
    if requested and levels and requested not in levels:
        raise OrchestrationError(
            f"编排 effort '{requested}' 不可用（可选: {', '.join(levels)}）"
        )

    # Graph extraction is a bounded structured transformation.  Grok xhigh can
    # spend minutes before visible output, so cap the automatic first attempt at
    # high and retain the user's xhigh setting for the main conversation.
    initial = requested
    policy = "follow"
    if provider.model.lower().startswith("grok-4.6") and requested == "xhigh":
        initial = "high" if "high" in levels else requested
        policy = "latency_guard"
    if not initial and levels:
        initial = SESSION.slot.default_effort if SESSION.slot.default_effort in levels else levels[-1]

    attempts = [initial]
    lower = ""
    if initial in {"xhigh", "high"} and "medium" in levels:
        lower = "medium"
    elif initial == "medium" and "low" in levels:
        lower = "low"
    if lower and lower not in attempts:
        attempts.append(lower)
    return {
        "provider": provider,
        "provider_key": SESSION.slot.key,
        "provider_name": provider.name,
        "model": provider.model,
        "requested_effort": requested or "auto",
        "attempts": attempts,
        "policy": policy,
        "usage_service": SESSION.usage_service,
        "work_dir": SESSION.work_dir,
    }


def _new_orchestration_client(
    context: dict[str, Any], effort: str
) -> LLMClient:
    client = create_client(copy.deepcopy(context["provider"]))
    usage_service = context.get("usage_service")
    if usage_service is not None:
        usage_service.bind_client(client)
    if effort:
        client.set_effort(effort)
    return client


async def _run_orchestration_with_recovery(
    operation_id: str,
    context: dict[str, Any],
    invoke: Callable[[LLMClient, Callable[[dict[str, Any]], None] | None], Awaitable[Any]],
) -> Any:
    """Run a planner with a visible one-step lower-effort recovery attempt."""

    attempts = list(context.get("attempts") or [""])
    reporter = _orchestration_progress_reporter(operation_id)
    last_error: OrchestrationError | None = None

    async def invoke_scoped(client):
        token = bind_runtime_scheduler(APP_RUNTIME_SCHEDULER)
        try:
            return await invoke(client, reporter)
        finally:
            reset_runtime_scheduler(token)

    for index, effort in enumerate(attempts, start=1):
        _update_orchestration_progress(operation_id, {
            "status": "running",
            "stage": "requesting",
            "detail": (
                f"正在启动编排规划器（第 {index}/{len(attempts)} 次，"
                f"Effort {effort or 'auto'}）。"
            ),
            "provider_key": context.get("provider_key", ""),
            "provider_name": context.get("provider_name", ""),
            "model": context.get("model", ""),
            "requested_effort": context.get("requested_effort", "auto"),
            "effective_effort": effort or "auto",
            "effort_policy": context.get("policy", "follow"),
            "attempt": index,
            "max_attempts": len(attempts),
            "attempt_started_at_ms": int(time.time() * 1000),
            "first_event_timeout_seconds": int(
                ORCHESTRATION_FIRST_EVENT_TIMEOUT_SECONDS
            ),
            "idle_timeout_seconds": int(ORCHESTRATION_IDLE_TIMEOUT_SECONDS),
            "reasoning_idle_timeout_seconds": int(
                ORCHESTRATION_REASONING_IDLE_TIMEOUT_SECONDS
            ),
            "soft_idle_warning_seconds": int(
                ORCHESTRATION_SOFT_IDLE_WARNING_SECONDS
            ),
            "thinking_chars": 0,
            "thinking_summary_chars": 0,
            "thinking_preview": "",
            "thinking_truncated": False,
            "output_chars": 0,
            "output_chunks": 0,
            "provider_events": 0,
            "last_provider_event_at_ms": 0,
            "output_preview": "",
            "output_truncated": False,
        })
        client = _new_orchestration_client(context, effort)
        try:
            return await invoke_scoped(client)
        except OrchestrationStreamTimeout as exc:
            last_error = exc
            if exc.phase == "queue":
                raise
            if exc.phase == "reasoning_idle":
                _update_orchestration_progress(operation_id, {
                    "status": "running",
                    "stage": "stalled",
                    "detail": (
                        f"{exc}；已收到 Provider 推理响应，"
                        "为避免重复消耗与代理排队，不自动提交第二份请求。"
                    ),
                    "fallback_reason": str(exc),
                    "stalled_phase": exc.phase,
                })
                raise
            if index >= len(attempts):
                raise
            next_effort = attempts[index]
            _update_orchestration_progress(operation_id, {
                "status": "running",
                "stage": "retrying",
                "detail": (
                    f"当前流无进展（{exc}），已终止该请求；"
                    f"将自动用 Effort {next_effort or 'auto'} 重试一次。"
                ),
                "fallback_reason": str(exc),
                "next_effort": next_effort or "auto",
            })
            await asyncio.sleep(0)
        except OrchestrationError as exc:
            # A provider can finish its stream while returning no final text or
            # malformed structured output. Retry that response once at the
            # lower planner effort, but surface graph/config validation errors.
            recoverable_markers = (
                "未返回目标盘点",
                "未返回阶段图内容",
                "目标盘点无法解析为 JSON",
                "返回的内容无法解析为阶段图 JSON",
                "目标盘点没有列出任何阶段",
            )
            if not any(marker in str(exc) for marker in recoverable_markers):
                raise
            last_error = exc
            if index >= len(attempts):
                raise
            next_effort = attempts[index]
            _update_orchestration_progress(operation_id, {
                "status": "running",
                "stage": "retrying",
                "detail": (
                    f"当前响应无法形成有效阶段图（{exc}）；"
                    f"将自动用 Effort {next_effort or 'auto'} 重试一次。"
                ),
                "fallback_reason": str(exc),
                "next_effort": next_effort or "auto",
            })
            await asyncio.sleep(0)
    if last_error is not None:
        raise last_error
    raise OrchestrationError("编排规划器没有可用尝试")


async def _await_orchestration_model_call(
    operation_id: str,
    awaitable: Any,
    *,
    kind: str = "graph",
) -> Any:
    """Bound orchestration LLM latency and expose a cancellable request handle."""
    current = asyncio.current_task()
    if operation_id and current is not None:
        _ORCHESTRATION_ACTIVE_REQUESTS[operation_id] = current
        _start_orchestration_progress(operation_id, kind=kind)
    try:
        # The stream itself owns first-event and idle watchdogs. Do not impose
        # a total wall-clock deadline: continuous reasoning, text, or tool-call
        # activity is valid progress regardless of total duration.
        result = await awaitable
        _update_orchestration_progress(operation_id, {
            "status": "running",
            "stage": "finalizing",
            "detail": "模型结果已通过结构校验，正在保存阶段图。",
        })
        return result
    except asyncio.TimeoutError:
        _update_orchestration_progress(operation_id, {
            "status": "timeout",
            "stage": "timeout",
            "detail": _orchestration_timeout_diagnostic(operation_id),
        })
        raise
    except asyncio.CancelledError:
        _update_orchestration_progress(operation_id, {
            "status": "cancelled",
            "stage": "cancelled",
            "detail": "用户已取消模型请求，本次未完成结果不会保存。",
        })
        raise
    finally:
        if (
            operation_id
            and _ORCHESTRATION_ACTIVE_REQUESTS.get(operation_id) is current
        ):
            _ORCHESTRATION_ACTIVE_REQUESTS.pop(operation_id, None)


@app.get("/api/orchestration/progress/{operation_id}")
async def api_orchestration_progress(operation_id: str) -> JSONResponse:
    _prune_orchestration_progress()
    try:
        operation_id = _orchestration_operation_id({"operation_id": operation_id})
    except OrchestrationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    progress = _ORCHESTRATION_OPERATION_PROGRESS.get(operation_id)
    if progress is None:
        return JSONResponse({"error": "编排操作尚未开始或已过期"}, status_code=404)
    payload = dict(progress)
    if operation_id in _ORCHESTRATION_OPERATION_RESULTS:
        payload["result"] = _ORCHESTRATION_OPERATION_RESULTS[operation_id]
        payload["result_ready"] = True
    failure = _ORCHESTRATION_OPERATION_FAILURES.get(operation_id)
    if failure:
        payload["failure"] = dict(failure)
    return JSONResponse(payload)


async def _run_orchestration_background_operation(
    operation_id: str,
    *,
    kind: str,
    execute: Callable[[], Awaitable[dict[str, Any]]],
) -> None:
    """Own an orchestration job independently from the initiating HTTP request."""

    current = asyncio.current_task()
    if current is not None:
        _ORCHESTRATION_ACTIVE_REQUESTS[operation_id] = current
    try:
        result = await execute()
    except asyncio.CancelledError:
        failure = {
            "error": {
                "graph": "阶段图生成已取消",
                "refill": "Plan 简报回填已取消",
            }.get(kind, "阶段目标盘点已取消"),
            "cancelled": True,
        }
        _ORCHESTRATION_OPERATION_FAILURES[operation_id] = failure
        _update_orchestration_progress(operation_id, {
            "status": "cancelled",
            "stage": "cancelled",
            "detail": failure["error"],
        })
    except asyncio.TimeoutError:
        failure = {
            "error": (
                "编排规划因连续无活动而超时。"
                + _orchestration_timeout_diagnostic(operation_id)
            ),
            "timeout": True,
        }
        _ORCHESTRATION_OPERATION_FAILURES[operation_id] = failure
        _update_orchestration_progress(operation_id, {
            "status": "timeout",
            "stage": "timeout",
            "detail": failure["error"],
        })
    except OrchestrationStreamTimeout as exc:
        failure = {
            "error": str(exc),
            "timeout": True,
            "stalled_phase": exc.phase,
        }
        _ORCHESTRATION_OPERATION_FAILURES[operation_id] = failure
        _update_orchestration_progress(operation_id, {
            "status": "timeout",
            "stage": "timeout",
            "detail": str(exc),
            "stalled_phase": exc.phase,
        })
    except (OrchestrationError, KeyError, TypeError, ValueError) as exc:
        failure = {"error": str(exc)}
        _ORCHESTRATION_OPERATION_FAILURES[operation_id] = failure
        _update_orchestration_progress(operation_id, {
            "status": "failed",
            "stage": "failed",
            "detail": str(exc),
        })
    except LLMError as exc:
        failure = _orchestration_provider_failure(exc)
        _ORCHESTRATION_OPERATION_FAILURES[operation_id] = failure
        status = "timeout" if failure.get("timeout") else "failed"
        _update_orchestration_progress(operation_id, {
            "status": status,
            "stage": status,
            "detail": failure["error"],
            "provider_error_code": failure.get("provider_error_code", ""),
            "provider_error_detail": failure.get("provider_error_detail", ""),
        })
    except Exception as exc:
        log.exception("Background orchestration %s failed", operation_id)
        failure = {"error": f"编排规划失败：{exc}"}
        _ORCHESTRATION_OPERATION_FAILURES[operation_id] = failure
        _update_orchestration_progress(operation_id, {
            "status": "failed",
            "stage": "failed",
            "detail": failure["error"],
        })
    else:
        _ORCHESTRATION_OPERATION_RESULTS[operation_id] = result
        _update_orchestration_progress(operation_id, {
            "status": "completed",
            "stage": "completed",
            "detail": "阶段图已经生成并通过结构校验。",
            "result_ready": True,
        })
    finally:
        if (
            operation_id
            and _ORCHESTRATION_ACTIVE_REQUESTS.get(operation_id) is current
        ):
            _ORCHESTRATION_ACTIVE_REQUESTS.pop(operation_id, None)


@app.post("/api/orchestration/cancel")
async def api_orchestration_cancel(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        operation_id = _orchestration_operation_id(payload)
    except OrchestrationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    target = _ORCHESTRATION_ACTIVE_REQUESTS.get(operation_id)
    if target is None or target.done():
        return JSONResponse({
            "operation_id": operation_id,
            "cancelled": False,
            "message": "编排请求已结束或不存在",
        })
    if target is asyncio.current_task():
        return JSONResponse({"error": "不能取消当前请求"}, status_code=409)
    target.cancel()
    _update_orchestration_progress(operation_id, {
        "status": "cancelling",
        "stage": "cancelling",
        "detail": "正在停止后台模型请求。",
    })
    return JSONResponse({
        "operation_id": operation_id,
        "cancelled": True,
        "message": "已取消编排请求",
    })


@app.post("/api/orchestration/inventory-from-chat")
async def api_orchestration_inventory_from_chat(request: Request) -> JSONResponse:
    """Pass 1: inventory stage goals / known inputs / open questions from chat."""
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        operation_id = _orchestration_operation_id(payload)
    except OrchestrationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    revision = str(payload.get("revision") or payload.get("message") or "").strip()
    if SESSION.chat_busy():
        return JSONResponse(
            {"error": "当前对话正在响应中，请结束后再盘点阶段目标"},
            status_code=409,
        )
    if SESSION.client is None:
        return JSONResponse({"error": "当前模型未就绪，请先选择模型"}, status_code=400)
    client_messages = coerce_dialogue_messages(payload.get("messages"))
    history = client_messages or list(SESSION.conversation.history)
    if not dialogue_has_user_turn(history):
        return JSONResponse(
            {"error": "请先在聊天中与模型商量目标，再盘点阶段目标"},
            status_code=400,
        )
    try:
        attempt_context = _orchestration_attempt_context(
            str(payload.get("orchestration_effort") or "")
        )

        async def execute_inventory() -> dict[str, Any]:
            result = await _await_orchestration_model_call(
                operation_id,
                _run_orchestration_with_recovery(
                    operation_id,
                    attempt_context,
                    lambda client, reporter: inventory_orchestration_from_dialogue(
                        client,
                        history,
                        revision=revision,
                        progress=reporter,
                    ),
                ),
                kind="inventory",
            )
            result["provider_key"] = attempt_context["provider_key"]
            result["model"] = attempt_context["model"]
            result["orchestration_effort"] = (
                _ORCHESTRATION_OPERATION_PROGRESS.get(operation_id, {}).get(
                    "effective_effort", "auto"
                )
            )
            return result

        if payload.get("background") is True:
            if not operation_id:
                return JSONResponse(
                    {"error": "后台编排需要 operation_id"}, status_code=400
                )
            if operation_id in _ORCHESTRATION_ACTIVE_REQUESTS:
                return JSONResponse(
                    {"error": "该编排操作正在进行"}, status_code=409
                )
            _ORCHESTRATION_OPERATION_RESULTS.pop(operation_id, None)
            _ORCHESTRATION_OPERATION_FAILURES.pop(operation_id, None)
            _start_orchestration_progress(operation_id, kind="inventory")
            task = asyncio.create_task(
                _run_orchestration_background_operation(
                    operation_id,
                    kind="inventory",
                    execute=execute_inventory,
                ),
                name=f"orchestration:{operation_id}",
            )
            _ORCHESTRATION_ACTIVE_REQUESTS[operation_id] = task
            return JSONResponse(
                {
                    "accepted": True,
                    "operation_id": operation_id,
                    "progress": _ORCHESTRATION_OPERATION_PROGRESS[operation_id],
                },
                status_code=202,
            )

        result = await execute_inventory()
    except (asyncio.TimeoutError, OrchestrationStreamTimeout) as exc:
        return JSONResponse(
            {
                "error": (
                    str(exc) if isinstance(exc, OrchestrationStreamTimeout)
                    else "盘点阶段目标的 Provider 连接或请求超时。"
                ),
                "timeout": True,
                "progress": _ORCHESTRATION_OPERATION_PROGRESS.get(operation_id),
            },
            status_code=504,
        )
    except asyncio.CancelledError:
        return JSONResponse(
            {"error": "阶段目标盘点已取消", "cancelled": True},
            status_code=499,
        )
    except (OrchestrationError, KeyError, TypeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse(
            {"error": f"盘点阶段目标失败：{exc}"},
            status_code=500,
        )
    return JSONResponse(result)


@app.post("/api/orchestration/refill-briefs")
async def api_orchestration_refill_briefs(request: Request) -> JSONResponse:
    """The old partial writer cannot reconcile topology or full raw history."""
    return JSONResponse(
        {"error": "旧版简报回填已停用：请打开流程底稿，分块核对完整历史与节点、连线差异后再应用；当前图未改动",
         "code": "workflow_draft_required"},
        status_code=409,
    )


@app.post("/api/orchestration/extract-from-chat")
async def api_orchestration_extract_from_chat(request: Request) -> JSONResponse:
    """Use the current session model to extract a stage graph from chat history."""
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    conversation_id = _active_conversation_id()
    project = SESSION.work_dir
    plan_store = _conversation_plan_store(conversation_id)
    if payload.get("conversation_id") and payload["conversation_id"] != conversation_id:
        return JSONResponse({"error": "对话已切换，请重新盘点当前任务"}, status_code=409)
    try:
        operation_id = _orchestration_operation_id(payload)
    except OrchestrationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    revision = str(payload.get("revision") or payload.get("message") or "").strip()
    if SESSION.chat_busy():
        return JSONResponse(
            {"error": "当前对话正在响应中，请结束后再抽出阶段图"},
            status_code=409,
        )
    if SESSION.client is None:
        return JSONResponse({"error": "当前模型未就绪，请先选择模型"}, status_code=400)
    # Prefer the visible chat transcript from the client so extract matches what
    # the user sees (avoids stale project plan_session / wrong session history).
    client_messages = coerce_dialogue_messages(payload.get("messages"))
    history = client_messages or list(SESSION.conversation.history)
    if not dialogue_has_user_turn(history):
        return JSONResponse(
            {"error": "请先在聊天中与模型商量目标，再抽出阶段图"},
            status_code=400,
        )
    previous_spec = (
        payload.get("previous_spec")
        if isinstance(payload.get("previous_spec"), dict)
        else None
    )
    if previous_spec is not None:
        try:
            previous_spec = _own_orchestration_spec(
                OrchestrationSpec.from_value(previous_spec),
                conversation_id=conversation_id, project=project,
            ).as_dict()
        except (OrchestrationError, TypeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
    bindings = payload.get("bindings") if isinstance(payload.get("bindings"), dict) else {}
    inventory = payload.get("inventory") if isinstance(payload.get("inventory"), dict) else None
    profile = EXPERT_PROFILES.get(SESSION.work_dir)
    if profile is not None:
        fe_routes = profile.planner_routes.get("frontend") or ()
        be_routes = profile.planner_routes.get("backend") or ()
        seeded = {
            "manager": profile.manager.as_dict(),
            "planner": profile.planner.as_dict() if profile.planner else None,
            "executor": profile.executor.as_dict(),
            "auditor": profile.auditor.as_dict(),
            "frontend": fe_routes[0].as_dict() if fe_routes else None,
            "backend": be_routes[0].as_dict() if be_routes else None,
        }
        for key, value in seeded.items():
            if value is not None:
                bindings.setdefault(key, value)
    try:
        attempt_context = _orchestration_attempt_context(
            str(payload.get("orchestration_effort") or "")
        )

        async def execute_graph() -> dict[str, Any]:
            result = await _await_orchestration_model_call(
                operation_id,
                _run_orchestration_with_recovery(
                    operation_id,
                    attempt_context,
                    lambda client, reporter: extract_orchestration_from_dialogue(
                        client,
                        history,
                        bindings=bindings,
                        previous_spec=previous_spec,
                        revision=revision,
                        inventory=inventory,
                        progress=reporter,
                    ),
                ),
                kind="graph",
            )
            spec = _own_orchestration_spec(OrchestrationSpec.from_value(result["spec"]),
                                           conversation_id=conversation_id, project=project)
            result["spec"] = spec.as_dict()
            result["plan_session"] = {**result["plan_session"], "spec": spec.as_dict()}
            result["conversation_id"] = conversation_id
            flow = to_expert_flow(spec)
            result["flow"] = flow.as_dict()
            result["orchestration"] = spec.as_dict()
            result["provider_key"] = attempt_context["provider_key"]
            result["model"] = attempt_context["model"]
            result["orchestration_effort"] = (
                _ORCHESTRATION_OPERATION_PROGRESS.get(operation_id, {}).get(
                    "effective_effort", "auto"
                )
            )
            try:
                plan_store.put(
                    attempt_context["work_dir"], result["plan_session"]
                )
            except (OSError, TypeError, ValueError):
                pass
            return result

        if payload.get("background") is True:
            if not operation_id:
                return JSONResponse(
                    {"error": "后台编排需要 operation_id"}, status_code=400
                )
            if operation_id in _ORCHESTRATION_ACTIVE_REQUESTS:
                return JSONResponse(
                    {"error": "该编排操作正在进行"}, status_code=409
                )
            _ORCHESTRATION_OPERATION_RESULTS.pop(operation_id, None)
            _ORCHESTRATION_OPERATION_FAILURES.pop(operation_id, None)
            _start_orchestration_progress(operation_id, kind="graph")
            task = asyncio.create_task(
                _run_orchestration_background_operation(
                    operation_id,
                    kind="graph",
                    execute=execute_graph,
                ),
                name=f"orchestration:{operation_id}",
            )
            _ORCHESTRATION_ACTIVE_REQUESTS[operation_id] = task
            return JSONResponse(
                {
                    "accepted": True,
                    "operation_id": operation_id,
                    "progress": _ORCHESTRATION_OPERATION_PROGRESS[operation_id],
                },
                status_code=202,
            )

        result = await execute_graph()
    except (asyncio.TimeoutError, OrchestrationStreamTimeout) as exc:
        return JSONResponse(
            {
                "error": (
                    str(exc) if isinstance(exc, OrchestrationStreamTimeout)
                    else "生成阶段图的 Provider 连接或请求超时。"
                ),
                "timeout": True,
                "progress": _ORCHESTRATION_OPERATION_PROGRESS.get(operation_id),
            },
            status_code=504,
        )
    except asyncio.CancelledError:
        return JSONResponse(
            {"error": "阶段图生成已取消", "cancelled": True},
            status_code=499,
        )
    except (OrchestrationError, KeyError, TypeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse(
            {"error": f"抽出阶段图失败：{exc}"},
            status_code=500,
        )
    return JSONResponse(result)


@app.post("/api/orchestration/propose")
async def api_orchestration_propose(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "body must be an object"}, status_code=400)
    if payload.get("conversation_id") and payload["conversation_id"] != _active_conversation_id():
        return JSONResponse({"error": "对话已切换，请重新打开当前任务的编排"}, status_code=409)
    goal = str(payload.get("goal") or "").strip()
    template_id = str(payload.get("template_id") or "").strip()
    revision = str(payload.get("revision") or payload.get("message") or "").strip()
    spec_body = payload.get("spec") if isinstance(payload.get("spec"), dict) else None
    previous_spec = (
        payload.get("previous_spec")
        if isinstance(payload.get("previous_spec"), dict)
        else None
    )
    bindings = payload.get("bindings") if isinstance(payload.get("bindings"), dict) else {}
    raw_spec = None
    if revision:
        previous_spec = previous_spec or spec_body
        parsed = try_parse_planner_json(revision)
        if parsed is not None:
            raw_spec = parsed
    elif spec_body is not None:
        raw_spec = spec_body
    # Seed bindings from saved expert profile when caller omits them
    profile = _heal_stored_expert_profile()
    if profile is not None:
        fe_routes = profile.planner_routes.get("frontend") or ()
        be_routes = profile.planner_routes.get("backend") or ()
        seeded = {
            "manager": profile.manager.as_dict(),
            "planner": profile.planner.as_dict() if profile.planner else None,
            "executor": profile.executor.as_dict(),
            "auditor": profile.auditor.as_dict(),
            "frontend": fe_routes[0].as_dict() if fe_routes else None,
            "backend": be_routes[0].as_dict() if be_routes else None,
        }
        for key, value in seeded.items():
            if value is not None:
                bindings.setdefault(key, value)
    try:
        result = propose_orchestration(
            goal,
            template_id=template_id,
            bindings=bindings,
            raw_spec=raw_spec,
            previous_spec=previous_spec,
            revision=revision,
        )
        # Also expose converted expert flow preview for the existing canvas
        spec = _own_orchestration_spec(OrchestrationSpec.from_value(result["spec"]))
        result["spec"] = spec.as_dict()
        result["plan_session"] = {**result["plan_session"], "spec": spec.as_dict()}
        result["conversation_id"] = _active_conversation_id()
        flow = to_expert_flow(spec)
        result["flow"] = flow.as_dict()
        result["orchestration"] = from_expert_flow(flow).as_dict()
        try:
            _conversation_plan_store().put(SESSION.work_dir, result["plan_session"])
        except (OSError, TypeError, ValueError):
            pass
    except (OrchestrationError, KeyError, TypeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(result)


@app.post("/api/orchestration/confirm")
async def api_orchestration_confirm(request: Request) -> JSONResponse:
    """Validate Spec, convert to ExpertFlowSpec, persist as project expert flow."""
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "body must be an object"}, status_code=400)
    raw = payload.get("spec") or payload.get("orchestration")
    activate = bool(payload.get("activate"))
    previous_expert_mode = SESSION.expert_mode
    previous_planning = SESSION.orchestration_planning
    previous_permission = SESSION.permission_mode.value
    previous_before_plan = getattr(SESSION, "permission_before_plan", "default")
    previous_before_orch = getattr(SESSION, "permission_before_orch", "")
    changed_execution_mode = False
    execution_mode_owner = object()
    execution_modes = None
    if payload.get("conversation_id") and payload["conversation_id"] != (
        SESSION.session.session_id if SESSION.session else ""
    ):
        return JSONResponse({"error": "对话已切换，请重新打开当前编排"}, status_code=409)
    try:
        spec = _own_orchestration_spec(OrchestrationSpec.from_value(raw))
        task_dispatch = payload.get("task_design_dispatch")
        retry_node_id = payload.get('retry_node_id', '')
        if not isinstance(retry_node_id, str) or (retry_node_id and (not activate or not task_dispatch)):
            raise ValueError('节点重试必须绑定当前任务的继续请求。')
        if task_dispatch is not None:
            if not isinstance(task_dispatch, dict) or not activate:
                raise ValueError("任务启动绑定无效，未派发业务执行。")
            from mewcode.orchestration.task_design import TaskDesign
            with TaskDesign(SESSION.work_dir, _active_conversation_id()).dispatch_guard(task_dispatch):
                if (spec.meta.get("task_design") or {}).get("task_id") != task_dispatch.get("task_id"):
                    raise ValueError("编排不属于当前任务，未派发业务执行。")
        submitted_inputs = payload.get("run_inputs")
        if activate and payload.get("choice_bundle_id"):
            from frontend.interaction_choices import lookup_startup_bundle
            from mewcode.orchestration.interaction_choices import resolve_answers
            bundle = lookup_startup_bundle(sys.modules[__name__], _active_conversation_id(),
                str(payload["choice_bundle_id"]), str(payload.get("choice_version") or ""), spec)
            selected = payload.get("choice_selections") or {}
            resolved = resolve_answers(bundle, selected, bundle_id=payload["choice_bundle_id"],
                version=payload.get("choice_version", ""), choice_inputs=payload.get("choice_inputs") or {})
            if resolved['actions']:
                raise ValueError("所选处理方式需要先准备或补充信息，尚未启动业务任务")
            submitted_inputs = {**(submitted_inputs or {}), **resolved['values']}
        run_values = resolve_run_inputs(spec.goal_contract.run_inputs if spec.goal_contract else (),
                                        submitted_inputs) if activate else {}
        bindings = payload.get("bindings") if isinstance(payload.get("bindings"), dict) else {}
        if bindings:
            relocated = {}
            for key, value in bindings.items():
                if (
                    isinstance(value, dict)
                    and value.get("provider_key")
                    and (value.get("model_id") or value.get("model"))
                ):
                    try:
                        relocated[key] = (ModelRef.from_value(value) if activate else MODEL_REGISTRY.relocate_model_ref(value)).as_dict()
                    except ValueError:
                        relocated[key] = value
                else:
                    relocated[key] = value
            bindings = relocated
            spec = apply_profile_bindings(
                spec,
                manager=bindings.get("manager"),
                planner=bindings.get("planner"),
                executor=bindings.get("executor"),
                auditor=bindings.get("auditor"),
                frontend=bindings.get("frontend"),
                backend=bindings.get("backend"),
            )
        warnings = validate_spec(spec, require_bindings=True)
        control_plan = build_control_plan(spec, draft=False)
        flow = to_expert_flow(spec) if activate else relocate_expert_flow(MODEL_REGISTRY, to_expert_flow(spec))
        extensions = _expert_flow_extensions()
        flow.validate(
            roles={str(item.get("name") or "") for item in extensions["roles"]},
            routers=set(extensions["routers"]),
            reducers=set(extensions["reducers"]),
            finalizers=set(extensions["finalizers"]),
            pipelines={
                str(item.get("graph_id") or "")
                for item in extensions["pipelines"]
            },
        )
        questions = collect_open_questions(spec)
        clarify_prompt = build_clarify_prompt(spec)
        # Asking once is not proof that the user answered. Only a reconciled
        # Plan with no blocking questions can be activated.
        needs_clarify = bool(questions)
        if activate and needs_clarify:
            if SESSION.chat_busy():
                return JSONResponse({"error": "当前对话仍在运行，请暂停或等待完成后再澄清"}, status_code=409)
            error = SESSION.set_expert_mode(False) or SESSION.set_orchestration_planning(True)
            if error:
                raise ValueError(error)
            activate = False
        permission_mode = _orchestration_permission_mode(payload)
        run_prompt = ""
        started = False
        queued = False
        resume_run_id = ""
        execution_mode = "saved"
        preserved_run_id = ""
        if activate:
            if SESSION.chat_busy():
                return JSONResponse({"error": "当前对话仍在运行，请暂停或等待完成后再确认当前编排"}, status_code=409)
            run_prompt = str(
                payload.get("run_prompt") or spec.goal or spec.label or ""
            ).strip()
            if not run_prompt:
                raise ValueError("编排缺少总目标，无法开跑；请先补上目标再确认")
            run_prompt = append_run_inputs(run_prompt, run_values)
            # Preflight before switching modes. A failed launch must not hide
            # planning controls or silently enqueue an unbound old task.
            try:
                runtime = SESSION.capture_conversation_runtime()
            except RuntimeError as exc:
                raise ValueError(str(exc) or "当前对话尚未准备好") from exc
            if runtime.running():
                return JSONResponse({"error": "当前对话仍在运行，请暂停或等待完成后再确认当前编排"}, status_code=409)
            # Link a launched graph to its host-created design identity; model
            # metadata alone cannot claim another task's saved authorization.
            from mewcode.orchestration.task_design import TaskDesign
            from mewcode.orchestration.draft_sources import digest
            design_service = (TaskDesign(runtime.work_dir, runtime.conversation_id)
                              if spec.meta.get("task_design") else None)
            design = design_service.read() if design_service else None
            if (design and design.get("status") == "ready" and design.get("intent") == "execute"
                    and (spec.meta.get("task_design") or {}).get("task_id") == design["task_id"]):
                # Preserve revision: this is a launch receipt for the exact
                # flow, not a new user decision or specification edit.
                with design_service.store.db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    design_state = design_service.store._get(db)
                    stored_design = design_state.get("task_design") or {}
                    if (stored_design.get("task_id"), stored_design.get("revision")) != (design["task_id"], design["revision"]):
                        raise ValueError("任务规格在启动前变化，未派发。")
                    stored_design["execution_flow_hash"] = digest(flow.as_dict())
                    design_service.store._put(db, design_state)
            # A host-bound new task has no previous execution. Do not load
            # unrelated histories or accidentally resume a different task.
            if task_dispatch:
                unfinished = EXPERT_RUNS.load(task_dispatch['run_id']) if task_dispatch.get('run_id') else None
            else:
                unfinished = EXPERT_RUNS.latest_resumable(runtime.conversation_id)
            if task_dispatch and task_dispatch.get("run_id") and (not unfinished or
                    unfinished.get("conversation_id") != runtime.conversation_id):
                raise ValueError("任务绑定的恢复检查点不存在，未派发业务执行。")
            execution_mode = "new"
            declarations = {item["name"]: item for item in (spec.goal_contract.run_inputs if spec.goal_contract else ())}
            previous_values = (unfinished or {}).get("run_inputs", {})
            same_inputs = all(not value or declarations[name].get("collect_at", "start") != "start"
                              or value == previous_values.get(name, value) for name, value in run_values.items())
            if unfinished and can_resume_confirmed_flow(unfinished, flow) and same_inputs:
                if unfinished.get("pending_recovery_revision"):
                    raise ValueError("有待批准的计划修订，请先在修订卡中决定；本次未启动任务")
                resume_run_id = str(unfinished["run_id"])
                execution_mode = "resume"
            elif unfinished:
                if task_dispatch and task_dispatch.get("run_id"):
                    raise ValueError("当前编排与任务绑定的检查点不同，请先核对；未创建新的业务执行。")
                # Confirmation authorizes this displayed graph, not the last
                # unfinished run in the conversation. Keep that checkpoint
                # untouched and start a separate execution of the new graph.
                preserved_run_id = str(unfinished["run_id"])
                execution_mode = "new_plan"
            binding_error = (expert_resume_model_error(MODEL_REGISTRY, unfinished, flow=flow)
                             if resume_run_id else expert_flow_model_error(MODEL_REGISTRY, flow))
            if binding_error:
                raise ValueError(binding_error)
            if retry_node_id and not resume_run_id:
                raise ValueError('节点重试必须继续已有运行，不能启动新任务。')
        store = _conversation_plan_store()
        plan_session = build_plan_session(
            spec,
            session_id=str(spec.meta.get("session_id") or ""),
            goal=spec.goal,
            bindings=bindings,
            previous=store.get(SESSION.work_dir),
            note=(
                "待澄清后再开跑" if needs_clarify
                else ("已确认开跑" if activate else "已确认编排")
            ),
            confirmed=not needs_clarify,
            activate=activate,
            draft_plan=needs_clarify,
        )
        saved = _conversation_flow_store().put(SESSION.work_dir, flow)
        store.put(SESSION.work_dir, plan_session)
        # Persist the confirmed revision before the resumed runner reads it.
        if activate:
            changed_execution_mode = True
            execution_session = SESSION.session
            execution_project = os.path.abspath(SESSION.work_dir)
            execution_runtime = getattr(SESSION, "conversation_runs", {}).get(runtime.conversation_id)
            execution_task = getattr(runtime, "task", None)
            runtime._orchestration_confirm_owner = execution_mode_owner
            runtime.orchestration_run_inputs = dict(run_values)
            error = SESSION.set_expert_mode(True, **({'resume_checkpoint': unfinished} if resume_run_id else {}))
            if error:
                raise ValueError(error)
            runtime.expert_mode = True
            runtime.orchestration_planning = False
            runtime.orchestration_permission_mode = permission_mode
            _apply_runtime_permission_mode(runtime, permission_mode)
            error = SESSION.set_permission_mode(permission_mode)
            if error:
                raise ValueError(error)
            def mode_snapshot():
                return (SESSION.expert_mode, SESSION.orchestration_planning, SESSION.permission_mode,
                        runtime.expert_mode, runtime.orchestration_planning,
                        getattr(getattr(runtime, "agent", None), "permission_mode", None),
                        getattr(runtime, "permission_before_plan", previous_before_plan),
                        getattr(runtime, "permission_before_orch", previous_before_orch))
            execution_modes = mode_snapshot()
            if resume_run_id:
                await start_runtime_turn(runtime, run_prompt, resume_run_id=resume_run_id,
                    **({'retry_node_id': retry_node_id} if retry_node_id else {}),
                    **({"task_design_dispatch": task_dispatch} if task_dispatch else {}))
            else:
                await start_runtime_turn(runtime, run_prompt, confirmed_new_flow=flow,
                    **({"task_design_dispatch": task_dispatch} if task_dispatch else {}))
            started = True
    except (OrchestrationError, ExpertFlowError, OSError, TypeError, ValueError, asyncio.CancelledError) as exc:
        if (changed_execution_mode
                and SESSION.session is execution_session
                and os.path.abspath(SESSION.work_dir) == execution_project
                and _active_conversation_id() == runtime.conversation_id
                and getattr(SESSION, "conversation_runs", {}).get(runtime.conversation_id) is execution_runtime
                and getattr(runtime, "_orchestration_confirm_owner", None) is execution_mode_owner
                and getattr(runtime, "_turn_start_owner", asyncio.current_task()) is asyncio.current_task()
                and getattr(runtime, "_turn_start_owner_token", None) is getattr(runtime, "_turn_start_token", None)
                and getattr(runtime, "task", None) is execution_task
                and (execution_modes is None or mode_snapshot() == execution_modes)):
            runtime.expert_mode = previous_expert_mode
            runtime.orchestration_planning = previous_planning
            runtime.orchestration_permission_mode = previous_permission
            runtime.permission_before_plan = previous_before_plan
            runtime.permission_before_orch = previous_before_orch
            _apply_runtime_permission_mode(runtime, previous_permission)
            if getattr(SESSION.session, "session_id", "") == runtime.conversation_id:
                SESSION.expert_mode = previous_expert_mode
                SESSION.orchestration_planning = previous_planning
                SESSION.permission_before_plan = previous_before_plan
                SESSION.permission_before_orch = previous_before_orch
                SESSION._set_internal_permission_mode(previous_permission, restoring=True)
                SESSION.web_state.update(
                    expert_mode=previous_expert_mode,
                    orchestration_planning=previous_planning,
                    permission_before_plan=previous_before_plan,
                    permission_before_orch=previous_before_orch,
                )
                SESSION._save_active_conversation_settings()
            SESSION._save_runtime_settings(runtime)
        if isinstance(exc, asyncio.CancelledError):
            raise
        return JSONResponse({"error": str(exc)}, status_code=400)
    finally:
        if changed_execution_mode and getattr(runtime, "_orchestration_confirm_owner", None) is execution_mode_owner:
            runtime._orchestration_confirm_owner = None
    return JSONResponse({
        "project": SESSION.work_dir,
        "confirmed": not needs_clarify,
        "needs_clarify": needs_clarify,
        "clarify_prompt": clarify_prompt,
        "open_questions": questions,
        "warnings": warnings,
        "spec": spec.as_dict(),
        "flow": saved.as_dict(),
        "control_plan": control_plan.as_dict(),
        "plan_session": plan_session.as_dict(),
        "expert_mode": SESSION.expert_mode,
        "started": started,
        "queued": queued,
        "resume_run_id": resume_run_id,
        "execution_mode": execution_mode,
        "preserved_run_id": preserved_run_id,
        "run_prompt": run_prompt,
        "permission_mode": permission_mode,
    })


@app.post('/api/orchestration/capability-preflight')
async def api_capability_preflight(request: Request) -> JSONResponse:
    from mewcode.kernel.capabilities import compile_manifest
    from mewcode.kernel.preflight import inspect
    try:
        payload = await request.json()
        spec = OrchestrationSpec.from_value(payload.get('spec'))
        manifest = compile_manifest(to_expert_flow(spec), workspace=SESSION.work_dir)
        checker = getattr(getattr(SESSION, "agent", None), "permission_checker", None)
        findings = await asyncio.to_thread(inspect, manifest,
            command_backend="host_reviewed" if checker is not None and not checker.uses_native_commands() else "native")
        return JSONResponse({'manifest': manifest, 'findings': findings})
    except (ValueError, TypeError, OrchestrationError) as exc:
        return JSONResponse({'error': str(exc)}, status_code=400)


@app.get("/api/orchestration/current")
async def api_orchestration_current() -> JSONResponse:
    custom = _conversation_flow_store().get(SESSION.work_dir)
    plan_session = _conversation_plan_store().get(SESSION.work_dir)
    canvas_store = PipelineStore(SESSION.work_dir)
    owned_graph = _stored_orchestration_graph(canvas_store)
    unclaimed_graph = _single_unclaimed_graph(canvas_store)
    try:
        if plan_session is not None and plan_session.spec is not None:
            # Plan session keeps canvas-only stages; the confirmed expert flow
            # folds nested checkpoints and must not replace the editor spec.
            spec = plan_session.spec
            flow = to_expert_flow(spec)
        elif custom is not None:
            flow = custom
            spec = from_expert_flow(flow)
        elif owned_graph is not None:
            spec = from_pipeline_graph(owned_graph)
            flow = to_expert_flow(spec)
        else:
            spec = get_template("linear")
            flow = to_expert_flow(spec)
    except (OrchestrationError, ExpertFlowError, TypeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({
        "project": SESSION.work_dir,
        "conversation_id": _active_conversation_id(),
        "canvas_missing": custom is None and plan_session is None and owned_graph is None,
        "unclaimed_graph_id": unclaimed_graph.graph_id if unclaimed_graph is not None else None,
        "custom": custom is not None or owned_graph is not None,
        "spec": spec.as_dict(),
        "flow": flow.as_dict(),
        "control_plan": (
            plan_session.control_plan.as_dict()
            if plan_session is not None and plan_session.control_plan is not None
            else None
        ),
        "plan_session": plan_session.as_dict() if plan_session is not None else None,
        "templates": list_templates(),
    })


_ORCH_RUN_ACTIVE_STATUSES = {"running", "paused", "blocked", "waiting", "pending"}


def _orchestration_run_node_states(snapshot: dict[str, Any]) -> dict[str, Any]:
    states = snapshot.get("node_states") or snapshot.get("stages") or {}
    compact: dict[str, Any] = {}
    for node_id, stage in states.items():
        if not isinstance(stage, dict):
            continue
        entry = {
            "status": str(stage.get("status") or "pending"),
            "attempt": int(stage.get("attempt") or 0),
        }
        for key in ("label", "message", "summary", "text", "started_at", "updated_at",
                    "finished_at", "blocker", "recovery_action", "loop_round", "verification_records", "verdict"):
            value = stage.get(key)
            if value not in (None, ""):
                if key == "text" and isinstance(value, str) and len(value) > 4000:
                    entry[key] = value[-4000:]
                else:
                    entry[key] = value
        compact[str(node_id)] = entry
    return compact


_REMOTE_STAGE_EVENT_TYPES = frozenset({
    "expert_stage", "expert_step",
    "expert_node_started", "expert_node_completed", "expert_node_delta",
    "expert_node_thinking", "expert_node_tool",
    "expert_member_started", "expert_member_completed", "expert_member_delta",
    "expert_member_thinking", "expert_member_tool",
    "expert_pipeline_started", "expert_pipeline_step_started",
    "expert_pipeline_step_completed", "expert_pipeline_step_output",
    "expert_pipeline_step_thinking", "expert_pipeline_step_tool",
    "expert_delta", "expert_thinking", "expert_tool",
    "expert_tool_start", "expert_tool_result", "expert_tool_wait", "expert_tool_state",
    "tool", "tool_result",
    "expert_run_state", "turn_started", "done", "error", "interrupt_ack",
})


def _remote_orch_steps(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(snapshot, dict):
        return []
    states = _orchestration_run_node_states(snapshot)
    locked = snapshot.get("locked_flow") if isinstance(snapshot.get("locked_flow"), dict) else {}
    ordered_ids = [
        str(node.get("node_id") or "").strip()
        for node in (locked.get("nodes") or [])
        if isinstance(node, dict) and str(node.get("node_id") or "").strip()
    ]
    labels = {
        str(node.get("node_id") or ""): str(node.get("label") or node.get("name") or "")
        for node in (locked.get("nodes") or [])
        if isinstance(node, dict)
    }
    if not ordered_ids:
        ordered_ids = list(states)
    seen: set[str] = set()
    steps: list[dict[str, Any]] = []
    for node_id in ordered_ids + [key for key in states if key not in ordered_ids]:
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        entry = states.get(node_id) or {}
        steps.append({
            "id": node_id,
            "label": str(entry.get("label") or labels.get(node_id) or node_id),
            "status": str(entry.get("status") or "pending"),
            "summary": str(entry.get("summary") or entry.get("message") or entry.get("text") or "")[:800],
            "kind": "node",
        })
    members = snapshot.get("members") if isinstance(snapshot.get("members"), dict) else {}
    for member_id, member in members.items():
        if not isinstance(member, dict):
            continue
        member_key = "member:" + str(member_id)
        if member_key in seen or str(member_id) in seen:
            continue
        seen.add(member_key)
        steps.append({
            "id": member_key,
            "label": str(
                member.get("name")
                or member.get("label")
                or member.get("role")
                or member_id
            ),
            "status": str(member.get("status") or "pending"),
            "summary": str(
                member.get("summary")
                or member.get("text")
                or member.get("partial_text")
                or ""
            )[:800],
            "kind": "member",
        })
    return steps


def _raw_orch_stage(snapshot: dict[str, Any], step: dict[str, Any]) -> dict[str, Any]:
    if step.get("kind") == "member":
        members = snapshot.get("members") if isinstance(snapshot.get("members"), dict) else {}
        key = str(step.get("id") or "").removeprefix("member:")
        raw = members.get(key) or members.get(step.get("id")) or {}
        return raw if isinstance(raw, dict) else {}
    states = snapshot.get("node_states") or snapshot.get("stages") or {}
    raw = states.get(step.get("id")) if isinstance(states, dict) else None
    return raw if isinstance(raw, dict) else {}


def _tool_events_from_stage(
    *,
    conversation_id: str,
    run_id: str,
    node_id: str,
    label: str,
    member_id: str = "",
    role: str = "",
    tools: Any,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(tools, dict):
        rows = [item for item in tools.values() if isinstance(item, dict)]
    elif isinstance(tools, list):
        rows = [item for item in tools if isinstance(item, dict)]
    events: list[dict[str, Any]] = []
    for tool in rows[-12:]:
        status = str(tool.get("status") or "")
        event_type = (
            "expert_tool_result" if status in {"completed", "failed"} else "expert_tool_start"
        )
        events.append({
            "type": event_type,
            "conversation_id": conversation_id,
            "run_id": run_id,
            "node_id": node_id,
            "member_id": member_id,
            "role": role,
            "label": label,
            "id": tool.get("tool_id") or tool.get("id"),
            "name": tool.get("name"),
            "args": tool.get("args") or tool.get("arguments") or {},
            "status": status,
            "output": str(tool.get("output") or tool.get("result") or "")[:4000],
            "is_error": bool(tool.get("is_error")),
            "snapshot_aggregate": True,
        })
    return events


def _synthetic_stage_events(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not isinstance(snapshot, dict):
        return events
    conversation_id = str(snapshot.get("conversation_id") or "")
    run_id = str(snapshot.get("run_id") or "")
    for step in _remote_orch_steps(snapshot):
        status = str(step.get("status") or "pending")
        if status in {"", "pending"}:
            continue
        completed = status in {"completed", "failed", "needs_attention", "paused"}
        raw = _raw_orch_stage(snapshot, step)
        member_id = str(raw.get("member_id") or "")
        if step.get("kind") == "member" and not member_id:
            member_id = str(step.get("id") or "").removeprefix("member:")
        node_id = str(raw.get("node_id") or step.get("id") or "")
        role = str(raw.get("role") or "")
        identity = {
            "conversation_id": conversation_id,
            "run_id": run_id,
            "node_id": node_id,
            "member_id": member_id,
            "role": role,
            "label": step["label"],
            "snapshot_aggregate": True,
        }
        text = str(
            step.get("summary")
            or raw.get("text")
            or raw.get("partial_text")
            or raw.get("summary")
            or raw.get("message")
            or ""
        )
        events.append({
            "type": "expert_node_completed" if completed else "expert_node_started",
            "status": status,
            "text": text,
            **identity,
        })
        if text:
            events.append({
                "type": "expert_delta",
                "text": text,
                **identity,
            })
        events.extend(_tool_events_from_stage(
            conversation_id=conversation_id,
            run_id=run_id,
            node_id=node_id,
            label=step["label"],
            member_id=member_id,
            role=role,
            tools=raw.get("tools"),
        ))
    return events


def _checkpoint_display_events(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(snapshot, dict):
        return []
    run_id = str(snapshot.get("run_id") or "")
    public = None
    if run_id:
        try:
            public = EXPERT_RUNS.public(run_id)
        except Exception:
            public = None
    public_events = list((public or {}).get("events") or []) if isinstance(public, dict) else []
    conversation_id = str(snapshot.get("conversation_id") or "")
    for event in public_events:
        if isinstance(event, dict):
            event.setdefault("conversation_id", conversation_id)
            event.setdefault("run_id", run_id)
    synthetic = _synthetic_stage_events(snapshot)
    if not public_events:
        return synthetic
    represented_members = {
        str(event.get("member_id") or "")
        for event in public_events
        if str(event.get("member_id") or "")
    }
    extra = [
        event for event in synthetic
        if str(event.get("member_id") or "") and str(event.get("member_id")) not in represented_members
    ]
    public_has_tools = any(
        str(event.get("type") or "") in {
            "expert_tool", "expert_tool_start", "expert_tool_result",
            "expert_tool_wait", "expert_tool_state", "expert_node_tool", "expert_member_tool",
        } or event.get("name")
        for event in public_events
    )
    if not public_has_tools:
        extra.extend(
            event for event in synthetic
            if event.get("name") and not event.get("member_id")
        )
    return public_events + extra


def _merge_live_and_checkpoint_events(
    conversation_id: str, snapshot: dict[str, Any] | None
) -> list[dict[str, Any]]:
    checkpoint = _checkpoint_display_events(snapshot)
    ring = [
        event for event in list(REMOTE_LIVES.get(conversation_id) or [])
        if str(event.get("type") or "") in _REMOTE_STAGE_EVENT_TYPES
        or str(event.get("text") or "").strip()
        or event.get("name")
        or event.get("tool")
    ]
    if not ring:
        return checkpoint
    live_keys = {
        (
            str(event.get("node_id") or ""),
            str(event.get("member_id") or ""),
            str(event.get("role") or ""),
        )
        for event in ring
        if str(event.get("type") or "") not in {"turn_started", "done", "error", "interrupt_ack", "tool", "tool_result"}
    }
    kept = []
    for event in checkpoint:
        key = (
            str(event.get("node_id") or ""),
            str(event.get("member_id") or ""),
            str(event.get("role") or ""),
        )
        if key in live_keys and key != ("", "", ""):
            continue
        kept.append(event)
    return kept + ring


def _remote_pending_prompts(conversation_id: str) -> list[dict[str, Any]]:
    from frontend.prompt_dispatch import remote_prompt, revision_reply_id
    target = str(conversation_id or "").strip()
    prompts: list[dict[str, Any]] = []
    for item in pending_runtime_prompts(SESSION):
        if str(item.get("conversation_id") or "") == target:
            prompts.append(remote_prompt(SESSION.runtime_for(target), item))
    snapshot = _conversation_expert_snapshot(target)
    revision = (snapshot or {}).get("pending_recovery_revision") if snapshot else None
    if revision:
        prompts.append({
            "type": "plan_revision",
            "conversation_id": target,
            "run_id": str((snapshot or {}).get("run_id") or ""),
            "proposal": revision,
            "request_id": revision_reply_id(target, str((snapshot or {}).get('run_id') or ''), revision),
        })
    return prompts


def _remote_live_payload(conversation_id: str) -> dict[str, Any]:
    flags = _conversation_expert_flags(conversation_id)
    snapshot = _conversation_expert_snapshot(conversation_id)
    live_events = _merge_live_and_checkpoint_events(conversation_id, snapshot)
    running_steps = [step for step in _remote_orch_steps(snapshot) if step.get("status") == "running"]
    current = running_steps[0] if running_steps else None
    if current is None:
        steps = _remote_orch_steps(snapshot)
        current = steps[-1] if steps else None
    return {
        "conversation_id": conversation_id,
        "active": _active_conversation_id() == conversation_id,
        "running": SESSION.chat_busy(conversation_id),
        "execution_kind": flags["execution_kind"],
        "expert_status": flags["expert_status"],
        "resumable": flags["resumable"],
        "run_id": flags["run_id"] or str((snapshot or {}).get("run_id") or ""),
        "run": {
            "run_id": str((snapshot or {}).get("run_id") or ""),
            "status": str((snapshot or {}).get("status") or flags["expert_status"] or ""),
            "task": str((snapshot or {}).get("task") or ""),
            "active_node_ids": list((snapshot or {}).get("active_node_ids") or [step["id"] for step in running_steps]),
        } if snapshot else None,
        "steps": _remote_orch_steps(snapshot),
        "current_step": current,
        "events": live_events,
        "pending": _remote_pending_prompts(conversation_id),
    }


async def dispatch_expert_resume(session, ws, msg):
    """Original desktop resume/retry checks, shared by the workbench adapter."""
    mtype = str(msg.get("type") or "")
    run_id = str(msg.get("run_id") or "")
    conversation_id = str(msg.get("conversation_id") or "")
    visible_id = session.session.session_id if session.session else ""
    if not conversation_id or conversation_id != visible_id:
        await ws.send_json({
            "type": "error",
            "text": "请先切换到该专家任务所属对话，再继续任务。",
        })
        return False
    if session.chat_busy(conversation_id):
        await ws.send_json({"type": "error", "text": "当前对话仍有任务运行"})
        return False
    resume_after_node = str(msg.get("resume_after_node") or "").strip()
    retry_node_id = str(msg.get("retry_node_id") or "").strip() if mtype == "expert_retry" else ""
    if mtype == "expert_retry" and (not retry_node_id or resume_after_node):
        await ws.send_json({"type": "error", "text": "重试必须指定失败节点，不能同时重跑后续阶段"})
        return False
    snapshot = EXPERT_RUNS.load(run_id)
    if snapshot is None or snapshot.get("conversation_id") != conversation_id:
        await ws.send_json({"type": "error", "text": "专家任务不存在或不属于当前对话"})
        return False
    if msg.get("expected_updated_at") is not None and msg["expected_updated_at"] != snapshot.get("updated_at"):
        await ws.send_json({"type": "error", "text": "运行记录已变化，请重新确认本次操作。"})
        return False
    # Resuming a saved workflow migrates once and uses the same controls as the canvas.
    # Historical quality cases and certificates are not execution prerequisites.
    from frontend.workbench_api import Workbench
    from frontend.workflow_core_api import run_action as workflow_action
    try:
        operation = 'continue_from' if resume_after_node else 'rerun' if retry_node_id else 'resume'
        await workflow_action(Workbench(sys.modules[__name__]), {
            'conversation_id': conversation_id, 'run_id': run_id,
            'operation': operation, 'node_id': retry_node_id or resume_after_node,
            'request_id': str(msg.get('request_id') or secrets.token_hex(16)),
        }, snapshot)
        return True
    except (ValueError, LookupError) as exc:
        await ws.send_json({'type': 'error', 'text': str(exc)})
        return False
    if (
        snapshot.get("pending_recovery_revision")
        and mtype == "expert_resume"
        and not str(msg.get("recovery_action") or "").strip()
    ):
        await ws.send_json({"type": "error", "text": "有待批准的计划修订，请先在修订卡中决定"})
        return False
    if resume_after_node:
        if snapshot.get("status") not in {
            "paused", "blocked", "needs_attention", "completed", "terminated",
        }:
            await ws.send_json({
                "type": "error", "text": "任务仍在执行，请先暂停再从检查点恢复。",
            })
            return False
        node_states = snapshot.get("node_states") or snapshot.get("stages") or {}
        stage = node_states.get(resume_after_node) if isinstance(node_states, dict) else None
        if not isinstance(stage, dict) or stage.get("status") != "completed":
            await ws.send_json({
                "type": "error",
                "text": f"resume_after_node requires completed anchor: {resume_after_node}",
            })
            return False
    if (
        mtype == "expert_resume"
        and not resume_after_node
        and not retry_node_id
        and not str(msg.get("recovery_action") or "").strip()
    ):
        from mewcode.experts.resume_advice import resume_advice
        advice = resume_advice(snapshot)
        if advice.get("action") in {"answer_case", "retry_node", "review_cases"}:
            fresh_public = EXPERT_RUNS._public_value(snapshot, include_details=False)
            await ws.send_json({
                "type": "error",
                "error_code": "recovery_resume_blocked",
                "text": str(advice.get("reason") or "当前检查点仍需明确处理。"),
                "error": str(advice.get("reason") or "当前检查点仍需明确处理。"),
                "error_displayed": True,
                "run_id": run_id,
                "conversation_id": conversation_id,
                "resume_advice": advice,
                "snapshot": fresh_public,
            })
            return False
    runtime = session.capture_conversation_runtime()
    scoped_ws = ConversationWebSocket(WS_EVENTS, runtime.conversation_id)
    runtime.execution_kind = "orchestration"
    session.begin_conversation_turn(runtime)
    try:
        await scoped_ws.send_json({
            "type": "turn_started", "text": "", "attachments": [],
            "queued": False, "expert_mode": True,
            "client_echoed": True, "resume_run_id": run_id,
        })
        task = asyncio.create_task(_resume_expert_after_paused_dialogue(
            scoped_ws, runtime, snapshot,
            supplemental=str(msg.get("supplemental") or ""),
            resume_after_node=resume_after_node,
            retry_node_id=retry_node_id,
            recovery_action=str(msg.get("recovery_action") or ""),
            recovery_info=str(msg.get("recovery_info") or ""),
            grant_extra_budget=bool(msg.get("grant_extra_budget")),
            sync_canvas=bool(msg.get("sync_canvas")),
        ))
        session.attach_conversation_task(runtime, task)
    except BaseException:
        runtime.starting = False
        raise
    return True


async def launch_expert_resume(
    snapshot: dict[str, Any],
    *,
    recovery_action: str = "",
    recovery_info: str = "",
    supplemental: str = "",
    resume_after_node: str = "",
    retry_node_id: str = "",
    grant_extra_budget: bool = False,
    sync_canvas: bool = False,
) -> dict[str, Any]:
    run_id = str(snapshot.get("run_id") or "")
    runtime = SESSION.capture_conversation_runtime()
    scoped_ws = ConversationWebSocket(WS_EVENTS, runtime.conversation_id)
    runtime.execution_kind = "orchestration"
    SESSION.begin_conversation_turn(runtime)
    try:
        await scoped_ws.send_json({
            "type": "turn_started", "text": "", "attachments": [],
            "queued": False, "expert_mode": True,
            "client_echoed": True, "resume_run_id": run_id,
        })
        task = asyncio.create_task(run_expert_chat(
            scoped_ws, str(snapshot.get("task") or ""), session=runtime,
            content_blocks=_resume_attachment_blocks(snapshot),
            registry_service=MODEL_REGISTRY, profile_store=EXPERT_PROFILES,
            run_store=EXPERT_RUNS, resume_run_id=run_id,
            flow_store=_conversation_flow_store(runtime.conversation_id),
            plan_store=_conversation_plan_store(runtime.conversation_id),
            pipeline_agent_factory=_pipeline_agent_factory,
            pipeline_capabilities_provider=_pipeline_capabilities,
            supplemental=supplemental,
            resume_after_node=resume_after_node,
            retry_node_id=retry_node_id,
            recovery_action=recovery_action,
            recovery_info=recovery_info,
            grant_extra_budget=grant_extra_budget,
            sync_canvas=sync_canvas,
        ))
        SESSION.attach_conversation_task(runtime, task)
    except BaseException:
        runtime.starting = False
        raise
    return {
        "conversation_id": runtime.conversation_id,
        "run_id": run_id,
        "status": "running",
        "resume_run_id": run_id,
    }


_ORCH_RUN_MATCH_CACHE: dict[str, dict[str, Any]] = {}


def _active_conversation_id() -> str:
    conversation_id = str(getattr(SESSION, "conversation_id", "") or "")
    if not conversation_id:
        session_obj = getattr(SESSION, "session", None)
        conversation_id = str(getattr(session_obj, "session_id", "") or "")
    return conversation_id


def _conversation_plan_store(conversation_id=None):
    from mewcode.orchestration.conversation_scope import ConversationStore
    return ConversationStore(get_plan_session_store(),
                             _active_conversation_id() if conversation_id is None else conversation_id)


def _conversation_flow_store(conversation_id=None):
    from mewcode.orchestration.conversation_scope import ConversationStore
    return ConversationStore(EXPERT_FLOWS_STORE,
                             _active_conversation_id() if conversation_id is None else conversation_id)


def _own_orchestration_spec(spec, *, conversation_id=None, project=None):
    """Validate references before stamping a newly created conversation draft."""
    conv = _active_conversation_id() if conversation_id is None else conversation_id
    owner = str((spec.meta or {}).get("conversation_id") or "")
    if owner and owner != conv:
        raise OrchestrationError("方案属于另一个对话，请重新打开当前对话的编排")
    graph_id = str((spec.meta or {}).get("pipeline_graph_id") or "")
    if conv and graph_id:
        graph = PipelineStore(project or SESSION.work_dir).get_pipeline(graph_id)
        if graph is None or not _canvas_owned_by(graph, conv):
            raise OrchestrationError("方案引用了不属于当前对话的画布；请为当前任务创建独立画布")
    return replace(spec, meta={**spec.meta, "conversation_id": conv}) if conv else spec


def _canvas_owned_by(graph, conversation_id):
    return graph is not None and _canvas_owner(graph) == conversation_id


def _canvas_owner(graph) -> str:
    from mewcode.orchestration.chat_canvas import metadata
    return str(metadata(graph).get("conversation_id") or "") if graph is not None else ""


def _run_flow_node_ids(snapshot: dict[str, Any]) -> set[str]:
    locked = snapshot.get("locked_flow")
    if isinstance(locked, dict):
        return {
            str(node.get("node_id") or "").strip()
            for node in (locked.get("nodes") or [])
            if isinstance(node, dict) and str(node.get("node_id") or "").strip()
        }
    return set()


def _resolve_project_run() -> dict[str, Any] | None:
    """Legacy panel: only the active conversation, never a similar old graph."""
    conversation_id = _active_conversation_id()

    if conversation_id:
        runs = EXPERT_RUNS.list_for_conversation(conversation_id)
        if runs:
            return runs[0]

    return None


CHAT_REPORTS = ChatReportStore()
# Same tool set the orchestration node loop snapshots (see orchestration/process_files.py).
CHAT_FILE_TOOLS = {"WriteFile", "EditFile", "ReportArtifact"}
# conversation_id → last canvas node reported, so a later unlabelled step or
# file lands on the node the chat is actually working on.
_CHAT_REPORT_NODES: dict[str, str] = {}
# Conversations already known to have (or not have) a canvas report. Consulted
# once per conversation so every file write does not re-read the store.
_CHAT_REPORT_SEEN: dict[str, bool] = {}


def tool_writes_files(tool_name: str) -> bool:
    return str(tool_name or "") in CHAT_FILE_TOOLS


def _canvas_nodes_text(session) -> str:
    """Per-turn [MEWCODE_CANVAS_NODES] list for this conversation's own canvas."""
    from frontend.orch_activity import graph_nodes
    graph = _stored_orchestration_graph(PipelineStore(session.work_dir))
    if graph is None:
        return ""
    activity = ActivityStore(session.work_dir)
    nodes = graph_nodes(graph)
    run_id = activity.bind_chat_history(session.conversation_id, graph.graph_id,
                                        nodes=nodes, version=graph.version)
    overview = activity.overview(session.conversation_id, graph.graph_id, run_id)
    latest = {e["node_id"]: e.get("status") for e in overview["latest"] if e.get("node_id")}
    last_reported = activity.last_reported_node(session.conversation_id, run_id)
    return canvas_nodes_reminder(nodes, latest, last_reported)


def _canvas_node_names() -> dict[str, str]:
    """Normalized node name/id → node_id for the active project canvas."""

    try:
        graph = _stored_orchestration_graph(PipelineStore(SESSION.work_dir))
    except Exception:
        graph = None
    index: dict[str, str] = {}
    for node in getattr(graph, "nodes", None) or []:
        node_id = str(getattr(node, "id", "") or "").strip()
        if not node_id:
            continue
        for name in (node_id, getattr(node, "label", ""), getattr(node, "title", "")):
            key = str(name or "").strip().lower()
            if key:
                index.setdefault(key, node_id)
    return index


def _resolve_canvas_node(value: str) -> str:
    """Best-effort map from a reported node name to a real canvas node id.

    Reports written by a model rarely match the canvas byte for byte ("3.2 节
    方向中性化" vs "3.2 节方向中性化（L199-L246）"), so a unique substring match
    is used as a fallback. An unresolvable name is kept verbatim: a report no
    canvas node owns is still better than dropping the progress silently.
    """

    raw = str(value or "").strip()
    if not raw:
        return ""
    index = _canvas_node_names()
    if not index:
        return raw
    key = raw.lower()
    if key in index:
        return index[key]
    hits = {node_id for name, node_id in index.items() if key in name or name in key}
    if len(hits) == 1:
        return hits.pop()
    return raw


def _mirror_chat_report_step(*, arguments: Any, output: str, runtime=None) -> str:
    """Record one chat ``report_step`` call as canvas progress. Returns node id."""

    from mewcode.tools.report_step import expert_step_from_report

    if runtime is not None and (runtime.conversation_id != _active_conversation_id()
                               or Path(runtime.work_dir).resolve() != Path(SESSION.work_dir).resolve()):
        return ""  # Legacy active-canvas mirror must not follow another conversation.
    conversation_id = _active_conversation_id()
    if not conversation_id:
        return ""
    args = arguments if isinstance(arguments, dict) else {}
    step = expert_step_from_report(args=args, output=output, role="chat", node_id="")
    if step is None:
        return ""
    node_id = _resolve_canvas_node(str(args.get("node") or ""))
    if not node_id:
        node_id = _CHAT_REPORT_NODES.get(conversation_id, "")
    if not node_id:
        return ""
    CHAT_REPORTS.report(
        work_dir=SESSION.work_dir,
        conversation_id=conversation_id,
        node_id=node_id,
        label=str(step.get("label") or ""),
        status=str(step.get("status") or "running"),
        summary=str(args.get("summary") or ""),
        details=args,
    )
    _CHAT_REPORT_NODES[conversation_id] = node_id
    _CHAT_REPORT_SEEN[conversation_id] = True
    return node_id


def _capture_chat_process_file(
    *,
    tool_name: str,
    arguments: Any,
    is_error: bool,
    tool_id: str,
    sandbox: Any,
    runtime=None,
) -> None:
    """Keep the same immutable file snapshot the orchestration path keeps."""

    from mewcode.orchestration.process_files import capture_process_file

    if runtime is not None and (runtime.conversation_id != _active_conversation_id()
                               or Path(runtime.work_dir).resolve() != Path(SESSION.work_dir).resolve()):
        return
    conversation_id = _active_conversation_id()
    if not conversation_id:
        return
    if conversation_id not in _CHAT_REPORT_SEEN:
        _CHAT_REPORT_SEEN[conversation_id] = CHAT_REPORTS.snapshot(
            work_dir=SESSION.work_dir, conversation_id=conversation_id
        ) is not None
    if not _CHAT_REPORT_NODES.get(conversation_id) and not _CHAT_REPORT_SEEN[conversation_id]:
        # Nothing reported yet: do not turn every chat into a canvas report.
        return
    record = capture_process_file(
        tool_name=tool_name,
        arguments=arguments if isinstance(arguments, dict) else {},
        is_error=is_error,
        work_dir=SESSION.work_dir,
        sandbox=sandbox,
        node_id=_CHAT_REPORT_NODES.get(conversation_id, ""),
        tool_id=tool_id,
        attempt=1,
    )
    if record is not None:
        CHAT_REPORTS.record_file(
            work_dir=SESSION.work_dir,
            conversation_id=conversation_id,
            record=record,
        )


def _orchestration_live_run(run_id: str = "") -> dict[str, Any] | None:
    """Live node states for the active project's orchestration run.

    Falls back to the conversation's chat reports: chat-driven work has no
    expert run, and the canvas must be able to show it the same way.
    """

    latest = _resolve_project_run()
    if latest is None:
        chat = CHAT_REPORTS.snapshot(
            work_dir=SESSION.work_dir, conversation_id=_active_conversation_id()
        )
        if chat is None:
            return None
        if run_id and str(chat.get("run_id") or "") != str(run_id or "").strip():
            return None
        return chat
    if run_id:
        wanted = str(run_id or "").strip()
        if str(latest.get("run_id") or latest.get("id") or "") != wanted:
            return None
    return {
        "run_id": str(latest.get("run_id") or latest.get("id") or ""),
        "conversation_id": str(latest.get("conversation_id") or ""),
        "process_files": latest.get("process_file_receipts") or [],
        "intervention_receipts": latest.get("intervention_receipts") or [],
        "status": str(latest.get("status") or ""),
        "active": str(latest.get("status") or "") in _ORCH_RUN_ACTIVE_STATUSES,
        "node_states": _orchestration_run_node_states(latest),
        "updated_at": str(latest.get("updated_at") or ""),
        "capability_executor": latest.get("capability_executor"),
    }


@app.get("/api/orchestration/run-state")
async def api_orchestration_run_state(run_id: str = Query(""), project: str = Query(""),
                                      conversation_id: str = Query(""), graph_id: str = Query("")) -> JSONResponse:
    """Live node states for the canvas; the canvas polls and never infers progress."""
    if graph_id:
        from frontend.orch_activity import canvas_activity
        try:
            data = await canvas_activity(sys.modules[__name__], project=project,
                conversation_id=conversation_id, graph_id=graph_id, run_id=run_id)
            data["pending_grants"] = _pending_file_grants()
            return JSONResponse(data)
        except (ValueError, LookupError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
    run = _orchestration_live_run(run_id)
    return JSONResponse({
        "project": SESSION.work_dir,
        "run": run,
        # Spoken authorizations that still need an explicit click.  Listed here
        # so the canvas can offer the grant without the user hunting for the form.
        "pending_grants": _pending_file_grants(),
    })


def _pending_file_grants() -> list[dict[str, Any]]:
    """Offers recorded for the active conversation, newest first."""
    conversation_id = _active_conversation_id()
    runtime = SESSION.conversation_runs.get(conversation_id) if conversation_id else None
    offers = getattr(runtime, "pending_file_grants", None) if runtime is not None else None
    return [dict(item) for item in offers or [] if isinstance(item, dict)]


@app.get("/api/orchestration/node-changes")
async def api_orchestration_node_changes(
    run_id: str = Query(""),
    commit: str = Query(""),
    project: str = Query(""), conversation_id: str = Query(""), graph_id: str = Query(""),
) -> JSONResponse:
    """Per-node change explanations for the canvas.

    Attribution reuses ProcessFile receipts (run/node/attempt bound); diff
    content comes from git only.  Read-only: it never stages, commits, or
    touches the work tree, and reports an explicit unavailable state when the
    project is not a git repository.
    """
    if graph_id:
        from frontend.orch_activity import canvas_activity
        try:
            data = await canvas_activity(sys.modules[__name__], project=project,
                conversation_id=conversation_id, graph_id=graph_id, run_id=run_id)
            run = data["run"]
        except (ValueError, LookupError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
    else:
        run = _orchestration_live_run(run_id)
    report = await node_change_report(
        SESSION.work_dir,
        (run or {}).get("process_files") or [],
        commit=commit,
    )
    report["run_id"] = str((run or {}).get("run_id") or "")
    return JSONResponse(report)


def _orchestration_spec_for_project() -> tuple[OrchestrationSpec, Any]:
    """Return only a plan owned by the active conversation."""
    from mewcode.orchestration.consistent_save import recover_pending_save
    recover_pending_save(SESSION.work_dir, plan_store=_conversation_plan_store(), flow_store=_conversation_flow_store())
    plan_session = _conversation_plan_store().get(SESSION.work_dir)
    custom = _conversation_flow_store().get(SESSION.work_dir)
    if plan_session is not None and plan_session.spec is not None:
        return plan_session.spec, plan_session
    if custom is not None:
        return from_expert_flow(custom), plan_session
    graph = _stored_orchestration_graph(PipelineStore(SESSION.work_dir))
    if graph is not None:
        return from_pipeline_graph(graph), plan_session
    raise OrchestrationError("当前对话尚未创建画布，请先从本对话生成阶段图；不会复用其他任务的画布")


def _expert_resume_flow_for_project(work_dir: str | None = None) -> ExpertFlowSpec | None:
    """Compile the latest saved canvas for resume instead of using a stale lock."""
    from mewcode.orchestration.consistent_save import recover_pending_save
    recover_pending_save(work_dir or SESSION.work_dir, plan_store=_conversation_plan_store(), flow_store=_conversation_flow_store())
    return saved_execution_flow(work_dir or SESSION.work_dir, flow_store=_conversation_flow_store(),
                                plan_store=_conversation_plan_store())


def _resolve_model_provider_key(model_id: str) -> str:
    needle = str(model_id or "").strip()
    if not needle:
        return ""
    found = MODEL_REGISTRY.find_provider_key_for_model(needle)
    if found:
        return found
    try:
        # Last-catalog fallback when registry records have not been loaded yet.
        cache = getattr(MODEL_REGISTRY, "_catalog_cache", None)
        models = (cache or {}).get("models") if isinstance(cache, dict) else None
        if isinstance(models, list):
            wanted = needle.lower()
            for item in models:
                if not isinstance(item, dict):
                    continue
                if str(item.get("model_id") or "").strip().lower() != wanted:
                    continue
                if item.get("available") is False:
                    continue
                key = str(item.get("provider_key") or "").strip()
                if key:
                    return key
    except Exception:
        pass
    return ""


def _stored_orchestration_graph(
    store: PipelineStore,
    graph_id: str = "",
) -> PipelineGraph | None:
    """Resolve only owned graphs. A stale explicit id never falls back."""
    from mewcode.orchestration.chat_canvas import is_chat_canvas
    conv = _active_conversation_id()
    wanted = str(graph_id or "").strip()
    if wanted:
        existing = store.get_pipeline(wanted)
        if (existing is not None and is_orchestration_graph(existing) and not is_chat_canvas(existing)
                and (not conv or _canvas_owned_by(existing, conv) or not _canvas_owner(existing))):
            # An explicit id may also resolve an unclaimed legacy graph; the
            # no-id latest scan below still only returns owned canvases.
            return existing
        return None
    latest: PipelineGraph | None = None
    latest_stamp = ""
    for item in store.list_pipelines():
        graph = store.get_pipeline(item.graph_id)
        if graph is None or not is_orchestration_graph(graph) or is_chat_canvas(graph):
            continue
        if conv and not _canvas_owned_by(graph, conv):
            continue
        stamp = str(item.updated_at or graph.updated_at or "")
        if latest is None or stamp >= latest_stamp:
            latest = graph
            latest_stamp = stamp
    return latest


def _single_unclaimed_graph(store: PipelineStore) -> PipelineGraph | None:
    """The sole non-chat orchestration graph with no owner, else None."""
    from mewcode.orchestration.chat_canvas import is_chat_canvas
    found: PipelineGraph | None = None
    for item in store.list_pipelines():
        graph = store.get_pipeline(item.graph_id)
        if graph is None or not is_orchestration_graph(graph) or is_chat_canvas(graph):
            continue
        if _canvas_owner(graph):
            continue
        if found is not None:
            return None  # More than one unclaimed graph: nothing is safe to open.
        found = graph
    return found


@app.post("/api/orchestration/open-canvas")
async def api_orchestration_open_canvas(request: Request) -> JSONResponse:
    """Materialize / refresh a PipelineGraph for the current orchestration spec."""
    try:
        from mewcode.orchestration.consistent_save import recover_pending_save
        recover_pending_save(SESSION.work_dir, plan_store=_conversation_plan_store(), flow_store=_conversation_flow_store())
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        raw_spec = payload.get("spec")
        posted_graph_id = str(payload.get("graph_id") or "").strip()
        conv = _active_conversation_id()
        if payload.get("conversation_id") and payload["conversation_id"] != conv:
            return JSONResponse({"error": "当前对话已切换，请重新打开画布"}, status_code=409)
        from mewcode.orchestration import chat_canvas
        activity_graph = PipelineStore(SESSION.work_dir).get_pipeline(posted_graph_id) if posted_graph_id else None
        if (posted_graph_id and not chat_canvas.is_chat_canvas(activity_graph)
                and _stored_orchestration_graph(PipelineStore(SESSION.work_dir), posted_graph_id) is None):
            return JSONResponse({"error": "画布不存在或不属于当前对话，请重新打开当前任务的画布"}, status_code=409)
        if payload.get("mode") == "activity" or chat_canvas.is_chat_canvas(activity_graph):
            if activity_graph is not None and chat_canvas.is_chat_canvas(activity_graph):
                chat_canvas.validate(activity_graph, conv)
            # A retired activity link may recover only this conversation's flow.
            store = PipelineStore(SESSION.work_dir)
            graph = _stored_orchestration_graph(store, "")
            if graph is None:
                graph = _single_unclaimed_graph(store)
            if graph is not None:
                body = {"project": SESSION.work_dir, "conversation_id": conv,
                        "graph_id": graph.graph_id, "graph": graph.model_dump(mode="json", by_alias=True),
                        "unclaimed": not _canvas_owner(graph), "warnings": []}
                if posted_graph_id and posted_graph_id != graph.graph_id:
                    body["redirected_from"] = posted_graph_id
                return JSONResponse(body)
            return JSONResponse({"error": "当前对话尚未创建流程画布，请从本对话生成阶段图", "canvas_missing": True}, status_code=409)
        if conv and activity_graph is not None and not raw_spec and not _canvas_owner(activity_graph):
            # Unclaimed legacy canvas: readable, but no progress overlay or
            # plan-session write until the user explicitly claims it.
            return JSONResponse({"project": SESSION.work_dir, "conversation_id": conv,
                                 "graph_id": activity_graph.graph_id,
                                 "graph": activity_graph.model_dump(mode="json", by_alias=True),
                                 "unclaimed": True, "warnings": []})
        plan_session = _conversation_plan_store().get(SESSION.work_dir)
        if raw_spec:
            spec = OrchestrationSpec.from_value(raw_spec)
        else:
            spec, plan_session = _orchestration_spec_for_project()
        spec = _own_orchestration_spec(spec)
        store = PipelineStore(SESSION.work_dir)
        spec_graph_id = str((spec.meta or {}).get("pipeline_graph_id") or "").strip()
        session_graph_id = ""
        if plan_session is not None and getattr(plan_session, "spec", None) is not None:
            session_graph_id = str(
                ((plan_session.spec.meta or {}).get("pipeline_graph_id") or "")
            ).strip()
        graph_id = posted_graph_id or spec_graph_id or session_graph_id
        existing = _stored_orchestration_graph(store, graph_id)
        version = existing.version if existing is not None else 1
        mode = str(payload.get("mode") or "replace").strip()
        if mode not in {"replace", "refresh"}:
            raise OrchestrationError("未知画布同步模式")
        if raw_spec and existing is not None:
            base_version = payload.get("expected_version", (spec.meta or {}).get("pipeline_graph_version"))
            if base_version is not None and int(base_version) != existing.version:
                return JSONResponse({"error": "画布已更新，请重新读取方案后再编辑，避免恢复已删除的内容。",
                                     "current_version": existing.version}, status_code=409)
        stored_spec = from_pipeline_graph(existing) if existing is not None else None
        newer_plan = stored_spec is not None and spec.version > max(stored_spec.version, existing.version)
        reuse_stored = existing is not None and not raw_spec and not newer_plan
        # Never rematerialize a stored canvas from a folded/stale spec. Extra
        # stages live on the PipelineGraph; from_expert_flow drops them.
        if reuse_stored:
            draft = existing
            spec = from_pipeline_graph(existing)
        elif existing is not None and mode == "refresh":
            draft = sync_orchestration_graph_from_spec(existing, spec)
        elif existing is not None and is_orchestration_graph(existing):
            # A full plan is a snapshot, not a union. Omitted stages, edges and
            # groups are deletions; only the explicit legacy refresh may merge.
            draft = to_pipeline_graph(spec, graph_id=existing.graph_id, version=existing.version)
        else:
            draft = to_pipeline_graph(
                spec,
                graph_id=graph_id if existing is not None else "",
                version=version,
            )
        if not reuse_stored and mode == "replace":
            canvas_payload = json.loads(draft.variables["__orch.payload__"])
            old_payload = json.loads(existing.variables["__orch.payload__"]) if existing is not None else {}
            same_topology = existing is not None and (
                {node.id for node in draft.nodes} == {node.id for node in existing.nodes}
                and {(edge.from_node, edge.to_node, edge.label) for edge in draft.edges}
                    == {(edge.from_node, edge.to_node, edge.label) for edge in existing.edges}
                and canvas_payload.get("meta", {}).get("canvas_groups", {})
                    == old_payload.get("meta", {}).get("canvas_groups", {})
            )
            # Binding/content edits do not reset a user's card sizes or layout.
            for node_id, item in canvas_payload.get("nodes", {}).items():
                previous_item = old_payload.get("nodes", {}).get(node_id, {})
                for key in ("canvas_size", "canvas_group_size", "checkpoint_layout_version"):
                    if key in previous_item:
                        item[key] = previous_item[key]
            if same_topology:
                positions = {node.id: node.position for node in existing.nodes}
                spec_nodes = spec.node_map()
                draft = draft.model_copy(update={"nodes": [
                    node if node.id in spec_nodes and spec_nodes[node.id].position is not None
                    else node.model_copy(update={"position": positions[node.id]}) for node in draft.nodes]})
            canvas_payload["meta"] = {**canvas_payload.get("meta", {}), "canvas_auto_layout_pending": not same_topology}
            draft = draft.model_copy(update={"variables": {**draft.variables,
                "__orch.payload__": json.dumps(canvas_payload, ensure_ascii=False)}})
        result = PipelineCompiler.validate(draft)
        if not result.valid:
            return JSONResponse(
                {
                    "error": "编排图画布校验失败",
                    "errors": result.errors,
                    "warnings": result.warnings,
                },
                status_code=400,
            )
        if reuse_stored:
            previous_spec = plan_session.spec if plan_session is not None else None
            spec = merge_model_provider_keys(
                spec,
                previous=previous_spec,
                resolve_provider=_resolve_model_provider_key,
            )
        else:
            # Rehydrate before committing, including nesting and canvas metadata.
            spec = merge_model_provider_keys(
                from_pipeline_graph(draft), previous=spec,
                resolve_provider=_resolve_model_provider_key,
            )
        from mewcode.orchestration.consistent_save import save_consistently
        def commit_canvas():
            return save_consistently(SESSION.work_dir, spec=spec, graph=draft,
                pipeline_store=store, plan_store=_conversation_plan_store(), flow_store=_conversation_flow_store(),
                previous=plan_session, note="打开编排画布", increment_version=not reuse_stored)
        task_expected = payload.get("task_design_expected")
        if task_expected:
            from mewcode.orchestration.task_design import TaskDesign
            # The same durable task transaction fences pause/supersede through
            # the synchronous, journaled canvas commit and records its result.
            with TaskDesign(SESSION.work_dir, conv).commit_build(task_expected) as receipt:
                saved, spec, flow, session = commit_canvas()
                receipt["spec"] = spec.as_dict()
        else:
            saved, spec, flow, session = commit_canvas()
        from frontend.orch_activity import bind_open_canvas
        bind_open_canvas(sys.modules[__name__], saved)
        return JSONResponse({
            "project": SESSION.work_dir,
            "conversation_id": _active_conversation_id(),
            "graph_id": saved.graph_id,
            "graph": saved.model_dump(mode="json", by_alias=True),
            "spec": spec.as_dict(),
            "flow": flow.as_dict(),
            "plan_session": session.as_dict(),
            "warnings": result.warnings,
            "expert_mode": SESSION.expert_mode,
            "unclaimed": not _canvas_owner(saved),
        })
    except VersionConflictError as exc:
        return JSONResponse({"error": "画布已更新，请重新加载后保存", "current_version": exc.current_version}, status_code=409)
    except OSError as exc:
        return JSONResponse({"error": "保存未完成，请重新打开核对：" + str(exc)}, status_code=500)
    except (OrchestrationError, ExpertFlowError, TypeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/orchestration/canvas-sync")
async def api_orchestration_canvas_sync(request: Request) -> JSONResponse:
    """Persist canvas edits in the active project and sync the plan session.

    The generic Pipeline API is registered once at process startup and may still
    point at the launch directory after the desktop switches projects.  The
    orchestration canvas is project-scoped, so both its graph file and plan
    session must be written through ``SESSION.work_dir`` here.
    """
    try:
        from mewcode.orchestration.consistent_save import recover_pending_save
        recover_pending_save(SESSION.work_dir, plan_store=_conversation_plan_store(), flow_store=_conversation_flow_store())
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "body must be an object"}, status_code=400)
        project = SESSION.work_dir
        conv = _active_conversation_id()
        if payload.get("conversation_id") and payload["conversation_id"] != conv:
            return JSONResponse({"error": "当前对话已切换，不能保存其他任务的画布"}, status_code=409)
        requested_project = str(payload.get("project") or "").strip()
        if requested_project and PlanSessionStore._project_key(requested_project) != PlanSessionStore._project_key(project):
            return JSONResponse({"error": "当前项目已切换，请回到打开画布的项目后保存。"}, status_code=409)
        graph_id = str(payload.get("graph_id") or "").strip()
        store = PipelineStore(SESSION.work_dir)
        if payload.get("graph") and isinstance(payload.get("graph"), dict):
            graph = PipelineGraph.model_validate(payload["graph"])
        elif graph_id:
            graph = store.get_pipeline(graph_id)
            if graph is None:
                return JSONResponse({"error": "画布图不存在"}, status_code=404)
        else:
            return JSONResponse({"error": "graph_id or graph required"}, status_code=400)
        if not is_orchestration_graph(graph):
            return JSONResponse(
                {"error": "该 Pipeline 不是专家团编排画布"},
                status_code=400,
            )
        from mewcode.orchestration import chat_canvas
        if chat_canvas.is_chat_canvas(graph) or chat_canvas.is_chat_canvas(store.get_pipeline(graph.graph_id)):
            return JSONResponse({"error": "对话进度是只读活动视图，不能写回执行方案"}, status_code=409)
        persisted = store.get_pipeline(graph.graph_id)
        if conv and (not _canvas_owned_by(graph, conv) or (persisted is not None and not _canvas_owned_by(persisted, conv))):
            return JSONResponse({"error": "画布不属于当前对话；不能认领或覆盖其他任务的画布"}, status_code=409)
        pipeline_validation = PipelineCompiler.validate(graph)
        if not pipeline_validation.valid:
            return JSONResponse(
                {
                    "error": "编排图画布校验失败",
                    "errors": pipeline_validation.errors,
                    "warnings": pipeline_validation.warnings,
                },
                status_code=400,
            )
        previous = None
        plan_session = _conversation_plan_store().get(SESSION.work_dir)
        if plan_session is not None:
            previous = plan_session.spec
        spec = from_pipeline_graph(graph)
        spec = merge_model_provider_keys(
            spec,
            previous=previous,
            resolve_provider=_resolve_model_provider_key,
        )
        warnings = validate_spec(spec, require_bindings=False)
        try:
            from mewcode.orchestration.consistent_save import save_consistently
            graph, spec, flow, session = save_consistently(project, spec=spec, graph=graph,
                pipeline_store=store, plan_store=_conversation_plan_store(),
                flow_store=_conversation_flow_store(), previous=plan_session)
        except VersionConflictError as exc:
            return JSONResponse({"error": "画布已在其他窗口更新，请重新加载后再保存",
                                 "current_version": exc.current_version}, status_code=409)
        except (OSError, TypeError, ValueError) as exc:
            return JSONResponse({"error": f"写回失败: {exc}"}, status_code=500)
        control_plan = session.control_plan
        await WS_EVENTS.send_json({"type": "orchestration_saved", "project": project, "conversation_id": conv,
            "graph_id": graph.graph_id, "spec": spec.as_dict(), "flow": flow.as_dict(),
            "control_plan": control_plan.as_dict()})
        return JSONResponse({
            "project": project,
            "conversation_id": conv,
            "graph_id": graph.graph_id,
            "graph": graph.model_dump(mode="json", by_alias=True),
            "spec": spec.as_dict(),
            "flow": flow.as_dict(),
            "control_plan": control_plan.as_dict(),
            "plan_session": session.as_dict(),
            "warnings": [*pipeline_validation.warnings, *warnings],
            "expert_mode": SESSION.expert_mode,
        })
    except (OrchestrationError, ExpertFlowError, TypeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/client/providers/sync")
async def api_desktop_provider_sync(
    body: DesktopProviderBundleBody, request: Request
) -> JSONResponse:
    if not _request_has_desktop_token(request):
        return JSONResponse(
            {"error": "provider secrets are accepted only from the desktop runtime"},
            status_code=403,
        )
    if not body.profiles:
        blocked = clobber_block_reason(USER_DATA_ROOT)
        if blocked:
            # The desktop keeps the only readable copy of the secrets. If its
            # snapshot exists yet neither side could parse it, an empty sync
            # erases every binding with no recoverable source.
            log.error("refused an empty desktop provider sync: %s", blocked)
            return JSONResponse({"error": blocked}, status_code=409)
    try:
        public_profiles = MODEL_REGISTRY.replace_desktop_profiles(body.profiles)
        SESSION.scan_slots()
        invalid_roles = await _disable_invalid_active_expert_profile()
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({
        "profiles": public_profiles,
        "expert_invalid_roles": invalid_roles,
        "expert_mode": SESSION.expert_mode,
    })


@app.post("/api/client/providers/test")
async def api_desktop_provider_test(
    body: DesktopProviderTestBody, request: Request
) -> JSONResponse:
    if not _request_has_desktop_token(request):
        return JSONResponse(
            {"error": "provider testing is available only in the desktop runtime"},
            status_code=403,
        )
    profile = dict(body.profile)
    # The authenticated renderer can request a saved desktop-profile probe
    # without receiving its secret. Keep lookup and use inside the backend.
    if not str(profile.get("api_key") or "").strip():
        record = MODEL_REGISTRY.get(str(profile.get("provider_key") or ""))
        if record is not None and record.source == "desktop":
            profile["api_key"] = record.provider.resolve_api_key()
    result = await MODEL_REGISTRY.test_profile(profile)
    if (result.get("checks") or {}).get("model_catalog") and not (
        result.get("checks") or {}
    ).get("streaming"):
        result["cost_warning"] = "本次测试只拉取账号模型目录，不会发起推理请求。"
    else:
        result["cost_warning"] = "连接测试会发起短响应及工具调用，可能产生少量模型费用。"
    return JSONResponse(result)


@app.post("/api/client/providers/secret")
async def api_desktop_provider_secret(
    body: dict[str, Any], request: Request
) -> JSONResponse:
    """Resolve a bound Provider key directly to Electron main, never renderer."""
    if not _request_has_desktop_bearer(request):
        return JSONResponse(
            {"error": "provider secret resolution is main-process only"},
            status_code=403,
        )
    provider_key = str(body.get("provider_key") or "")
    record = MODEL_REGISTRY.get(provider_key)
    if record is None:
        return JSONResponse({"error": "provider slot not found"}, status_code=404)
    if record.source == "codex" and "chatgpt.com" in record.provider.base_url.lower():
        return JSONResponse(
            {"error": "OAuth tokens cannot be resolved as provider_api_key"},
            status_code=403,
        )
    value = record.provider.resolve_api_key()
    if not value:
        return JSONResponse({"error": "provider slot has no key"}, status_code=404)
    return JSONResponse(
        {"value": value}, headers={"Cache-Control": "no-store"}
    )


@app.post("/api/client/usage/accounts/sync")
async def api_desktop_usage_accounts_sync(
    body: DesktopUsageAccountBundleBody, request: Request
) -> JSONResponse:
    if not _request_has_desktop_token(request):
        return JSONResponse(
            {"error": "usage secrets are accepted only from the desktop runtime"},
            status_code=403,
        )
    try:
        for value in body.accounts:
            adapter_id = str(value.get("adapter_id") or "")
            USAGE_RUNTIME.adapters.get(adapter_id)
            value["managed"] = False
            value["source_kind"] = "manual"
            if adapter_id == "plugin":
                plugin_id = str(value.get("plugin_id") or "")
                manifest = USAGE_RUNTIME.plugins.get_plugin(plugin_id)
                if manifest is None:
                    raise ValueError(f"usage plugin is not installed: {plugin_id}")
                if manifest.get("source_kind") == "single_py":
                    value["source_kind"] = "custom_script"
        accounts = USAGE_RUNTIME.sync_accounts(body.accounts)
    except (KeyError, TypeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({
        "accounts": [account.to_dict() for account in accounts]
    })


@app.get("/")
async def index() -> FileResponse:
    # HTML 不缓存：避免改版后浏览器拿旧 HTML 配旧 JS（按钮存在但无监听）
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/plugins/ui")
async def api_ui_plugins() -> JSONResponse:
    packages = []
    for item in SESSION.plugin_host.frontend_manifest():
        plugin_name = str(item.get("plugin") or "")
        base = f"/api/plugins/assets/{plugin_name}/"
        packages.append({
            **item,
            "scripts": [base + path for path in item.get("scripts", [])],
            "styles": [base + path for path in item.get("styles", [])],
            "assets": [base + path for path in item.get("assets", [])],
        })
    return JSONResponse(
        {
            "plugins": packages,
            "menus": SESSION.context.slots.resolve("web.menus") or [],
            "settings": SESSION.context.slots.resolve("web.settings") or [],
        },
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/plugins/assets/{plugin_name}/{asset_path:path}")
async def api_ui_plugin_asset(
    plugin_name: str,
    asset_path: str,
) -> Any:
    try:
        path = SESSION.plugin_host.resolve_plugin_asset(plugin_name, asset_path)
    except KeyError:
        return JSONResponse({"error": "plugin asset not found"}, status_code=404)
    media_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/pipeline")
async def pipeline_page() -> RedirectResponse:
    return RedirectResponse('/', status_code=307)


@app.get("/orch-canvas")
async def orch_canvas_page() -> RedirectResponse:
    return RedirectResponse('/?workbench=structure', status_code=307)


@app.get("/expert-flow")
async def expert_flow_page() -> RedirectResponse:
    return RedirectResponse('/', status_code=307)


@app.get("/completion")
async def completion_page() -> FileResponse:
    """Interactive Java completion playground backed by the completion graph."""
    return FileResponse(
        STATIC_DIR / "completion.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/api/state")
async def api_state() -> JSONResponse:
    return JSONResponse({
        **SESSION.client_state_payload(),
    })


@app.get("/api/chat/status")
async def api_chat_status(conversation_id: str = Query("")) -> JSONResponse:
    target = str(conversation_id or "").strip()
    if not target and SESSION.session is not None:
        target = SESSION.session.session_id
    return JSONResponse({
        "conversation_id": target,
        "running": SESSION.chat_busy(target),
        "active_conversation_id": (
            SESSION.session.session_id if SESSION.session is not None else ""
        ),
    })


@app.get("/api/chat/usage")
async def api_chat_usage(conversation_id: str = Query("")) -> JSONResponse:
    target = str(conversation_id or "").strip()
    active = SESSION.session.session_id if SESSION.session is not None else ""
    target = target or active
    if not target or not _SAFE_ID_RE.fullmatch(target):
        return JSONResponse({"error": "请选择有效的当前对话"}, status_code=400)
    runtime = SESSION.runtime_for(target)
    if target != active and runtime is None:
        return JSONResponse({"error": "对话不在当前运行范围"}, status_code=404)
    service = (
        runtime.usage_service or _session_usage_service(runtime.work_dir)
        if runtime is not None else SESSION.usage_service
    )
    work_dir = runtime.work_dir if runtime is not None else SESSION.work_dir
    # Read historical project receipts as well as the captured desktop route.
    # No migration/write here; the ledger deduplicates receipts by event identity.
    data = await asyncio.to_thread(
        service.ledger.conversation_summary, target,
        additional_paths=(Path(work_dir) / DEFAULT_LEDGER_PATH,),
    )
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


@app.post("/api/chat/interrupt")
async def api_chat_interrupt(body: ChatInterruptBody) -> JSONResponse:
    result = await request_chat_interrupt(
        body.conversation_id,
        action=body.action,
    )
    return JSONResponse(result)


@app.post("/api/chat/task-authorization")
async def api_chat_task_authorization(request: Request) -> JSONResponse:
    """Record one explicit user task scope; never synthesize it from model text."""
    if request.headers.get("X-Mew-Task-Authorization") != "1":
        return JSONResponse({"error": "需要从任务授权界面明确确认"}, status_code=403)
    try:
        body = await request.json()
        if not isinstance(body, dict) or SESSION.session is None:
            raise ValueError("没有当前对话")
        conversation_id = SESSION.session.session_id
        focus = dialogue_focus(SESSION)
        task_id = str(focus.get("task_id") or "")
        if not task_id or body.get("conversation_id") != conversation_id or body.get("task_id") != task_id:
            raise ValueError("当前任务已变化，请刷新后重新授权")
        from mewcode.permissions.task_authorization import TaskAuthorization
        authorization = TaskAuthorization.from_user_scope(body, task_id=task_id)
        key = f"{conversation_id}:{task_id}"
        records = dict(SESSION.web_state.get("task_authorizations") or {})
        records[key] = authorization.as_dict()
        SESSION.web_state["task_authorizations"] = records
        _save_web_state(SESSION.web_state)
        checker = getattr(getattr(SESSION, "agent", None), "permission_checker", None)
        if checker is not None:
            checker.set_task_authorization(authorization, task_id=task_id)
        reviewer = getattr(getattr(SESSION, "agent", None), "safety_reviewer", None)
        if reviewer is not None:
            reviewer.authorization_version = authorization.version
        audit = getattr(getattr(SESSION, "agent", None), "audit_logger", None)
        if audit is not None:
            audit.log_decision(
                tool_name="TaskAuthorization", params_summary=task_id,
                decision="scope_update", source_layer="task_scope_ui",
                rule_id=authorization.version,
                authorization_scope=json.dumps(authorization.as_dict(), ensure_ascii=False),
                reason="Explicit task scope confirmed in UI",
            )
        return JSONResponse({"authorization": authorization.as_dict()})
    except (ValueError, TypeError, OSError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.get("/api/chat/task-authorization")
async def api_chat_task_authorization_current() -> JSONResponse:
    if SESSION.session is None:
        return JSONResponse({"error": "没有当前对话"}, status_code=404)
    conversation_id = SESSION.session.session_id
    task_id = str(dialogue_focus(SESSION).get("task_id") or "")
    record = (SESSION.web_state.get("task_authorizations") or {}).get(
        f"{conversation_id}:{task_id}"
    ) if task_id else None
    from mewcode.permissions.task_authorization import TaskAuthorization
    try:
        current = TaskAuthorization.from_record(record).as_dict() if isinstance(record, dict) else None
    except (ValueError, OSError, TypeError):
        current = None
    return JSONResponse({
        "conversation_id": conversation_id, "task_id": task_id,
        "task_kind": dialogue_focus(SESSION).get("kind", ""),
        "task_title": str(dialogue_focus(SESSION).get("task") or "")[:200],
        "authorization": current,
    })


async def _run_native_control(operation, *args, **kwargs):
    # Check and reserve synchronously on the host event loop, before yielding.
    # The actual worker, not the HTTP waiter, releases this reservation.
    if _NATIVE_CONTROL_BUSY.is_set() or SESSION.any_chat_busy():
        raise ValueError("请等待运行中的任务或宿主恢复完成")
    _NATIVE_CONTROL_BUSY.set()
    def worker():
        try:
            return operation(*args, **kwargs)
        finally:
            _NATIVE_CONTROL_BUSY.clear()
    try:
        future = asyncio.get_running_loop().run_in_executor(None, worker)
    except BaseException:
        _NATIVE_CONTROL_BUSY.clear()
        raise
    def observed(done):
        if not done.cancelled():
            done.exception()  # Consume errors even when the HTTP client left.
    future.add_done_callback(observed)
    return await asyncio.shield(future)


def _native_control_context(body=None):
    if SESSION.session is None:
        raise ValueError("没有当前对话")
    conversation_id = SESSION.session.session_id
    task_id = str(dialogue_focus(SESSION).get("task_id") or "")
    if not task_id:
        raise ValueError("请先选择当前任务")
    if body is not None and (not isinstance(body, dict)
            or body.get("conversation_id") != conversation_id or body.get("task_id") != task_id):
        raise ValueError("当前任务已变化，请刷新后重试")
    from mewcode.sandbox.lpac_runtime import checked_path
    workspace = checked_path(Path(SESSION.work_dir))
    return conversation_id, task_id, workspace


@app.get("/api/chat/native-execution")
async def api_chat_native_execution() -> JSONResponse:
    """Host lifecycle summary; no commands, secrets or arbitrary run paths."""
    try:
        conversation_id, task_id, workspace = _native_control_context()
        from mewcode.sandbox.native_gate import native_gate_status
        from mewcode.sandbox.transaction_registry import TransactionRegistry
        from mewcode.sandbox.transaction_diagnostics import transaction_summary
        from mewcode.sandbox.volume_stores import describe
        registry = TransactionRegistry()
        gate = await asyncio.to_thread(native_gate_status)
        pending = await asyncio.to_thread(registry.pending, workspace)
        storage = await asyncio.to_thread(describe, registry, workspace)
        from mewcode.sandbox.transaction_registry import workspace_write_block, workspace_command_block
        checker = getattr(getattr(SESSION, "agent", None), "permission_checker", None)
        native = checker is None or checker.uses_native_commands()
        runtime_ready = gate.ready if native else True
        if native and checker is not None:
            from mewcode.permissions.network_policy import policy_for
            if policy_for(checker).enabled:
                from mewcode.sandbox.srt_runtime import runtime_status
                runtime_ready = (await asyncio.to_thread(runtime_status))["ready"]
        command_blocker = await asyncio.to_thread(workspace_command_block if native else workspace_write_block, workspace)
        return JSONResponse({"conversation_id": conversation_id, "task_id": task_id,
            "execution_backend": "native" if native else "host_reviewed",
            "command_blocker": command_blocker,
            "commands_ready": runtime_ready and not command_blocker,
            "workspace": str(workspace), "gate": {"state": gate.state.value, "reason": gate.reason,
                "missing_evidence": list(gate.missing_evidence)}, "storage": storage,
            "transactions": await asyncio.to_thread(lambda: [transaction_summary(item, task_id=task_id) for item in pending])},
            headers={"Cache-Control": "no-store"})
    except (ValueError, OSError, RuntimeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/chat/native-execution/storage")
async def api_chat_native_storage(request: Request) -> JSONResponse:
    if request.headers.get("X-Mew-Native-Control") != "1":
        return JSONResponse({"error": "需要从原生执行界面明确确认暂存目录"}, status_code=403)
    try:
        body = await request.json()
        conversation_id, _, workspace = _native_control_context(body)
        if SESSION.any_chat_busy():
            raise ValueError("请等待运行中的任务结束后配置暂存目录")
        from mewcode.sandbox.transaction_registry import TransactionRegistry
        from mewcode.sandbox.volume_stores import configure
        result = await _run_native_control(configure, TransactionRegistry(), workspace,
            body.get("directory"), expected_revision=body.get("revision"))
        return JSONResponse({"conversation_id": conversation_id, "storage": result})
    except (ValueError, TypeError, OSError, RuntimeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/chat/native-execution/reconcile")
async def api_chat_native_reconcile(request: Request) -> JSONResponse:
    if request.headers.get("X-Mew-Native-Control") != "1":
        return JSONResponse({"error": "需要明确确认恢复原生事务"}, status_code=403)
    try:
        body = await request.json()
        _, task_id, workspace = _native_control_context(body)
        if SESSION.any_chat_busy():
            raise ValueError("请先停止运行中的任务，再核验恢复事务")
        checker = getattr(getattr(SESSION, "agent", None), "permission_checker", None)
        from mewcode.sandbox.lpac_runtime import NativeScope
        from mewcode.sandbox.staged_execution import reconcile
        scope = NativeScope.from_checker(checker)
        if scope.workspace != workspace or scope.task_id != task_id or not scope.current():
            raise ValueError("请先恢复当前任务的宿主授权；不使用其他任务的权限恢复")
        confirmation = body.get("confirmation")
        if confirmation is None:
            outcomes = await _run_native_control(reconcile, scope)
        else:
            outcomes = await _run_native_control(reconcile, scope, confirmation=confirmation)
        return JSONResponse({"outcomes": outcomes,
            "complete": all(item["state"] in {"published", "rolled_back", "rejected"} for item in outcomes)})
    except (ValueError, TypeError, OSError, RuntimeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


from frontend.sandbox_network import register_network_routes
register_network_routes(app, lambda: SESSION, _run_native_control)


@app.post("/api/chat/native-execution/recovery-preview")
async def api_chat_native_recovery_preview(request: Request) -> JSONResponse:
    if request.headers.get("X-Mew-Native-Control") != "1":
        return JSONResponse({"error": "需要从原生执行界面预览恢复范围"}, status_code=403)
    try:
        body = await request.json()
        _, task_id, workspace = _native_control_context(body)
        from mewcode.sandbox.lpac_runtime import NativeScope
        from mewcode.sandbox.recovery_handoff import preview
        from mewcode.sandbox.transaction_registry import TransactionRegistry
        checker = getattr(getattr(SESSION, "agent", None), "permission_checker", None)
        scope = NativeScope.from_checker(checker)
        if scope.workspace != workspace or scope.task_id != task_id or not scope.current():
            raise ValueError("当前任务授权不可用，请先恢复授权再预览")
        result = await _run_native_control(preview, scope, TransactionRegistry(), body.get("transaction_id"))
        return JSONResponse({"preview": result}, headers={"Cache-Control": "no-store"})
    except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.get("/api/chat/approval-metrics")
async def api_chat_approval_metrics() -> JSONResponse:
    """Current session's approval funnel, including model and human wait."""
    agent = getattr(SESSION, "agent", None)
    audit = getattr(agent, "audit_logger", None)
    session_id = str(getattr(agent, "session_id", "") or "")
    if audit is None or not session_id:
        return JSONResponse({"count": 0, "unavailable": True})
    return JSONResponse(await asyncio.to_thread(audit.summary, session_id=session_id))


async def _publish_chat_queue(payload: dict[str, Any]) -> None:
    conversation_id = str(payload.get("conversation_id") or "")
    if not conversation_id:
        return
    await ConversationWebSocket(WS_EVENTS, conversation_id).send_json({
        "type": "chat_queue",
        "items": payload.get("items") or [],
        "revision": payload.get("revision", 0),
    })


def _chat_queue_header_valid(request: Request) -> bool:
    # The desktop cookie is SameSite=Strict. This non-simple header also keeps
    # cross-origin forms and no-CORS requests from mutating the waiting area.
    return request.headers.get("x-mew-chat-queue") == "1"


def _maintenance_chat_result(conversation_id, text, attachments=()):
    command = parse_maintenance_command(text)
    if command is None:
        return None
    if not conversation_id or conversation_id != _active_conversation_id():
        raise ValueError("维护命令需要明确的当前对话。")
    if attachments:
        raise ValueError("维护面板使用当前编排；附件已保留，请单独处理附件。")
    return maintenance_command_result(conversation_id, command["mode"], command["request"])


@app.post("/api/chat/queue")
async def api_chat_queue(request: Request, body: ChatQueueBody) -> JSONResponse:
    if not _chat_queue_header_valid(request):
        return JSONResponse({"error": "explicit chat queue header required"}, status_code=403)
    try:
        maintenance = _maintenance_chat_result(body.conversation_id, body.text, body.attachments)
        if maintenance is not None:
            return JSONResponse(maintenance)
        runtime = SESSION._active_queue_runtime(body.conversation_id, queue_control=True)
        attachment_refs = ATTACHMENTS.references(
            runtime.conversation_id, body.attachments
        )
        if any(block.get("kind") == "image" for block in attachment_refs):
            provider = runtime.slot.provider if runtime.slot else None
            if provider is None or not model_supports_vision(
                provider.protocol,
                provider.model,
                catalog_service=MODEL_CAPABILITY_CATALOG,
            ):
                raise AttachmentError("当前模型不支持图片输入，请切换到带 Vision 标记的模型")
        result = SESSION.enqueue_chat_turn(
            runtime.conversation_id,
            body.queue_id,
            body.text,
            attachment_refs,
        )
    except LookupError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc), "code": "not_running"}, status_code=409)
    except OverflowError as exc:
        return JSONResponse({"error": str(exc)}, status_code=429)
    except (AttachmentError, UnicodeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    await _publish_chat_queue(result)
    return JSONResponse(result)


def _record_authorization_intent(runtime: Any, text: str) -> str:
    """Turn a spoken authorization into an explicit, one-click offer.

    Verbal permission never takes effect on its own: the runner injects it with
    an explicit "不自动授权文件、命令或外发操作" note and audit criteria never
    contain an "authorized" item, so a sub-model that refuses for lack of
    authorization is behaving as designed.  Instead of letting the user guess,
    the host records what they seem to allow and tells them exactly how to make
    it real.
    """
    try:
        from mewcode.orchestration.authorization_intent import (
            detect_authorization_intent, merge_offer,
        )

        intent = detect_authorization_intent(text)
    except Exception:  # never let a heuristic break the chat path
        return ""
    if intent is None:
        return ""
    runtime.pending_file_grants = merge_offer(getattr(runtime, "pending_file_grants", []), intent)
    import uuid
    for offer in runtime.pending_file_grants:
        offer.setdefault('request_id', uuid.uuid4().hex)
    if intent.needs_path:
        return ("\n\n你在授权某个文件，但没有说出具体路径。请在画布右侧「文件授权」里填写完整路径并选择只读/读写，"
                "或在消息里写明（例如：可以改 docs/03.md）。只写在对话里的允许不会自动变成授权。")
    listed = "、".join(intent.paths)
    access = "读写" if intent.access == "read_write" else "只读"
    return (f"\n\n检测到你在授权：{listed}（{access}）。对话里的允许不会自动生效——"
            f"画布左下角已列出这条待确认授权，点「{access}授权」即可；"
            "也可以到画布右侧「文件授权」手动填写。授权只覆盖这一个文件，不包含同目录其他文件。")


async def _accept_expert_intervention(runtime, identity: str, text: str) -> dict:
    maintenance = _maintenance_chat_result(runtime.conversation_id, text)
    if maintenance is not None:
        return maintenance
    controller = getattr(runtime, "expert_takeover_controller", None)
    if controller is None:
        raise RuntimeError("当前运行尚未启用临时接管；请暂停并恢复该编排后重试")
    result = controller.submit(identity, text)
    if result["new"]:
        message = Message(role="user", content=text.strip())
        runtime.session.append(message)
    return result


async def _accept_expert_supplement(runtime, identity: str, text: str, expected_run_id: str) -> dict:
    controller = getattr(runtime, 'expert_takeover_controller', None)
    if controller is None:
        raise RuntimeError('当前编排尚未就绪，不能追加运行方向')
    from mewcode.kernel.async_commit import durable_call
    result = await durable_call(controller.supplement, identity, text, expected_run_id=expected_run_id)
    if result['new']:
        runtime.session.append(Message(role='user', content=text.strip()))
    return result


async def _accept_chat_intervention(runtime, identity: str, text: str) -> dict:
    maintenance = _maintenance_chat_result(runtime.conversation_id, text)
    if maintenance is not None:
        return maintenance
    state = continuation_state(runtime)
    if runtime.execution_kind != "chat" or state.get("status") not in {"running", "reviewing", "intervening"} or state.get("held"):
        raise RuntimeError("当前没有正在执行的持续对话任务；请直接发送消息或继续")
    if not _CHAT_QUEUE_ID.fullmatch(identity) or not text.strip() or len(text.encode("utf-8")) > 32000:
        raise ValueError("无效的插入消息")
    previous = runtime.continuation_requests.get(identity)
    if previous is not None:
        if previous != text:
            raise ValueError("消息 ID 已用于不同内容")
        return {"new": False, "message": "消息已接收"}
    if len(runtime.queued_prompts) >= MAX_VISIBLE_CHAT_QUEUE:
        raise ValueError("待处理消息已满")
    runtime.continuation_requests[identity] = text
    while len(runtime.continuation_requests) > 60:
        runtime.continuation_requests.pop(next(iter(runtime.continuation_requests)))
    control = control_intent(text)
    if control:
        runtime.owner.interrupt_conversation(runtime.conversation_id, action=control)
        runtime.queued_prompts.append(QueuedChatTurn(id=identity, text=text.strip()))
        runtime.owner._bump_chat_queue_revision(runtime)
        return {"new": True, "control": False, "message": "已终止持续任务" if control == "terminate" else "已暂停持续任务；不会自动恢复"}
    # Queue before cancelling. Existing task cleanup is the only place that
    # launches the insertion, after tools and history repair have unwound.
    index = sum(isinstance(item, QueuedChatTurn) and item.continuation for item in runtime.queued_prompts)
    runtime.queued_prompts.insert(index, QueuedChatTurn(id=identity, text=text.strip(), continuation=True))
    runtime.owner._bump_chat_queue_revision(runtime)
    already_interrupting = runtime.continuation_intervention
    runtime.continuation_intervention = True
    state["status"] = "intervening"
    from mewcode.continuation import save_state
    save_state(runtime, state)
    if not already_interrupting and not runtime.continuation_side_turn:
        runtime.cancel_requested = True
        if runtime.task is not None:
            runtime.task.cancel()
    return {"new": True, "message": "已接收：暂停当前执行，主模型先处理消息，再审核是否接回原任务"}


@app.post("/api/chat/interventions")
async def api_chat_intervention(request: Request, body: ChatQueueBody) -> JSONResponse:
    if not _chat_queue_header_valid(request):
        return JSONResponse({"error": "explicit chat queue header required"}, status_code=403)
    try:
        maintenance = _maintenance_chat_result(body.conversation_id, body.text, body.attachments)
        if maintenance is not None:
            return JSONResponse(maintenance)
        runtime = SESSION._active_queue_runtime(body.conversation_id, allow_continuation=True)
        if body.attachments:
            raise ValueError("带附件消息请先暂停再发送，或使用等待区")
        return JSONResponse(await _accept_chat_intervention(runtime, body.queue_id, body.text))
    except (LookupError, RuntimeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)


@app.post("/api/orchestration/interventions")
async def api_orchestration_intervention(request: Request, body: OrchestrationInterventionBody) -> JSONResponse:
    if not _chat_queue_header_valid(request):
        return JSONResponse({"error": "explicit chat queue header required"}, status_code=403)
    try:
        mode = getattr(body, 'mode', 'takeover')
        if mode != 'supplement':
            from frontend.workflow_recovery import handle_chat as recover_workflow_chat
            recovered = await recover_workflow_chat(sys.modules[__name__], cid=body.conversation_id,
                text=body.text, request_id=body.queue_id, context=getattr(body, 'workbench_context', None),
                client_echoed=False, attachments=body.attachments)
            if recovered is not None:
                return JSONResponse(recovered)
        if mode == 'supplement':
            runtime = SESSION._active_queue_runtime(body.conversation_id)
            if getattr(runtime, 'execution_kind', 'chat') != 'orchestration' or body.attachments:
                raise ValueError('补充方向入口只接收当前编排的文字要求')
            return JSONResponse(await _accept_expert_supplement(
                runtime, body.queue_id, body.text, body.expected_run_id))
        maintenance = _maintenance_chat_result(body.conversation_id, body.text, body.attachments)
        if maintenance is not None:
            return JSONResponse(maintenance)
        runtime = SESSION._active_queue_runtime(body.conversation_id)
        if getattr(runtime, "execution_kind", "chat") != "orchestration" or body.attachments:
            raise ValueError("此入口接收运行中编排的文字插入任务；附件请放入后续消息等待区")
        return JSONResponse(await _accept_expert_intervention(runtime, body.queue_id, body.text))
    except (LookupError, OSError, RuntimeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)


@app.post("/api/orchestration/file-grants")
async def api_orchestration_file_grant(request: Request) -> JSONResponse:
    if request.headers.get("X-Mew-File-Grant") != "1":
        return JSONResponse({"error": "需要用户从文件授权入口明确确认"}, status_code=403)
    try:
        body = await request.json()
        runtime = SESSION._active_queue_runtime(str(body.get("conversation_id") or ""))
        if getattr(runtime, "execution_kind", "chat") != "orchestration" or not runtime.expert_run_id:
            raise ValueError("此授权入口仅用于当前运行中的编排")
        if body.get("run_id") != runtime.expert_run_id or body.get("project") != runtime.work_dir:
            raise ValueError("编排或项目已切换，请刷新画布后再授权")
        if body.get("access") not in {"read", "read_write"}:
            raise ValueError("请选择只读或读写权限")
        from mewcode.permissions.sandbox import PathSandbox
        sandbox = PathSandbox(runtime.work_dir, file_grants=runtime.file_access_grants)
        resolved = sandbox.grant_file(str(body.get("path") or ""), writable=body["access"] == "read_write")
        target = str(resolved).replace("\\", "/").lower()
        runtime.pending_file_grants = [
            item for item in getattr(runtime, "pending_file_grants", [])
            if str(item.get("path", "")).replace("\\", "/").lower() != target
        ]
        return JSONResponse({"path": resolved, "access": body["access"], "run_id": runtime.expert_run_id,
                             "message": "已授权本轮编排访问这个文件，不包含同目录其他文件；不代替写入审批或放宽角色工具限制。"})
    except (OSError, LookupError, RuntimeError, ValueError, AttributeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/orchestration/dir-grants")
async def api_orchestration_dir_grant(request: Request) -> JSONResponse:
    """Add one directory to this run's sandbox, on explicit user action only."""
    if request.headers.get("X-Mew-File-Grant") != "1":
        return JSONResponse({"error": "需要用户从目录授权入口明确确认"}, status_code=403)
    try:
        body = await request.json()
        runtime = SESSION._active_queue_runtime(str(body.get("conversation_id") or ""))
        if getattr(runtime, "execution_kind", "chat") != "orchestration" or not runtime.expert_run_id:
            raise ValueError("此授权入口仅用于当前运行中的编排")
        if body.get("run_id") != runtime.expert_run_id or body.get("project") != runtime.work_dir:
            raise ValueError("编排或项目已切换，请刷新画布后再授权")
        if body.get("access") not in {"read", "read_write"}:
            raise ValueError("请选择只读或读写权限")
        from mewcode.permissions.sandbox import PathSandbox
        sandbox = PathSandbox(runtime.work_dir, dir_grants=runtime.dir_access_grants)
        resolved = sandbox.grant_directory(
            str(body.get("path") or ""), writable=body["access"] == "read_write"
        )
        writable = body["access"] == "read_write"
        return JSONResponse({
            "path": resolved, "access": body["access"], "run_id": runtime.expert_run_id,
            "message": (
                f"已授权本轮编排{'读写' if writable else '只读'}访问目录 {resolved} 及其子目录；"
                "不包含同级其他目录，也不代替写入审批或放宽角色工具限制。"
            ),
        })
    except (OSError, LookupError, RuntimeError, ValueError, AttributeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.get("/api/permissions/grants")
async def api_project_permission_grants() -> JSONResponse:
    """Show only the selected project's typed, revocable user grants."""
    from mewcode.permissions.grants import GrantStore, GrantStoreError
    try:
        store = GrantStore(SESSION.work_dir)
        return JSONResponse({
            "project": str(store.project_root),
            "grants": [grant.as_dict() for grant in store.list_grants() if grant.active],
        })
    except (OSError, ValueError, GrantStoreError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/permissions/grants/revoke")
async def api_project_permission_grant_revoke(request: Request) -> JSONResponse:
    if request.headers.get("X-Mew-User-Grant") != "1":
        return JSONResponse({"error": "需要在当前项目的授权界面明确撤销"}, status_code=403)
    from mewcode.permissions.grants import GrantStore, GrantStoreError
    try:
        body = await request.json()
        if not isinstance(body, dict) or body.get("project") != str(Path(SESSION.work_dir).resolve()):
            return JSONResponse({"error": "项目已切换，请刷新授权列表"}, status_code=409)
        grant_id = str(body.get("grant_id") or "")
        store = GrantStore(SESSION.work_dir)
        revoked = store.revoke(grant_id, request_id=secrets.token_hex(16))
        return JSONResponse({"ok": True, "grant_id": revoked.grant_id, "version": revoked.version})
    except KeyError:
        return JSONResponse({"error": "授权不存在"}, status_code=404)
    except (OSError, ValueError, GrantStoreError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/chat/queue/{queue_id}/remove")
async def api_chat_queue_remove(
    queue_id: str, request: Request, body: ChatQueueItemBody
) -> JSONResponse:
    if not _chat_queue_header_valid(request):
        return JSONResponse({"error": "explicit chat queue header required"}, status_code=403)
    if not _CHAT_QUEUE_ID.fullmatch(queue_id):
        return JSONResponse({"error": "无效的等待项 ID"}, status_code=422)
    try:
        result, removed = SESSION.remove_queued_chat_turn(
            body.conversation_id, queue_id
        )
    except LookupError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc), "code": "not_running"}, status_code=409)
    except KeyError as exc:
        return JSONResponse({"error": str(exc).strip("'\"")}, status_code=404)
    for attachment_id in removed.attachment_ids:
        ATTACHMENTS.delete(body.conversation_id, attachment_id)
    await _publish_chat_queue(result)
    return JSONResponse(result)


@app.post("/api/chat/queue/{queue_id}/send-now")
async def api_chat_queue_send_now(
    queue_id: str, request: Request, body: ChatQueueItemBody
) -> JSONResponse:
    if not _chat_queue_header_valid(request):
        return JSONResponse({"error": "explicit chat queue header required"}, status_code=403)
    if not _CHAT_QUEUE_ID.fullmatch(queue_id):
        return JSONResponse({"error": "无效的等待项 ID"}, status_code=422)
    try:
        result = SESSION.prioritize_queued_chat_turn(
            body.conversation_id, queue_id
        )
    except LookupError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc), "code": "not_running"}, status_code=409)
    except KeyError as exc:
        return JSONResponse({"error": str(exc).strip("'\"")}, status_code=404)
    await _publish_chat_queue(result)
    if result.get("status") in {"cancelling", "cancelled_before_start"}:
        await ConversationWebSocket(WS_EVENTS, body.conversation_id).send_json({
            "type": "interrupt_ack",
            "status": result["status"],
            "send_now_id": queue_id,
        })
    return JSONResponse(result)


@app.get("/api/sidebar")
async def api_sidebar() -> JSONResponse:
    return JSONResponse(SESSION.sidebar_payload())


def _workspace_nodes() -> list[dict]:
    config = _tailscale_config()
    nodes = configured_remote_nodes(config)
    known = {str(item.get("id")) for item in nodes}
    for item in discovered_remote_nodes():
        if str(item.get("id")) not in known:
            nodes.append(item)
    return nodes




def _sidebar_state_key(source: str, node_id: str, external_id: str, conversation_id: str = "") -> str:
    identity = (external_id or conversation_id or "").strip()
    return "|".join([(source or "mewcode").strip(), (node_id or "local").strip(), identity])


def _load_sidebar_conversation_state() -> dict:
    try:
        if SIDEBAR_STATE_FILE.is_file():
            data = json.loads(SIDEBAR_STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                hidden = data.get("hidden") if isinstance(data.get("hidden"), list) else []
                archived = data.get("archived") if isinstance(data.get("archived"), list) else []
                unread = data.get("unread") if isinstance(data.get("unread"), list) else []
                return {
                    "hidden": [str(x) for x in hidden if str(x).strip()],
                    "archived": [str(x) for x in archived if str(x).strip()],
                    "unread": [str(x) for x in unread if str(x).strip()],
                }
    except (OSError, TypeError, ValueError):
        pass
    return {"hidden": [], "archived": [], "unread": []}


def _save_sidebar_conversation_state(state: dict) -> None:
    SIDEBAR_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "hidden": sorted(set(state.get("hidden") or [])),
        "archived": sorted(set(state.get("archived") or [])),
        "unread": sorted(set(state.get("unread") or [])),
    }
    SIDEBAR_STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _workspace_conversation_keys(body: WorkspaceConversationStateBody) -> list[str]:
    refs = list(body.items or [])
    if not refs:
        refs = [
            WorkspaceConversationRef(
                source=body.source,
                node_id=body.node_id,
                external_id=body.external_id,
                conversation_id=body.conversation_id,
            )
        ]
    keys: list[str] = []
    for ref in refs:
        key = _sidebar_state_key(ref.source, ref.node_id, ref.external_id, ref.conversation_id)
        if key.endswith("|") or key.count("|") != 2 or not key.split("|")[-1]:
            continue
        keys.append(key)
    return keys


def _filter_sidebar_items(items: list[dict]) -> list[dict]:
    state = _load_sidebar_conversation_state()
    blocked = set(state["hidden"]) | set(state["archived"])
    if not blocked:
        return items
    kept = []
    for item in items:
        key = _sidebar_state_key(
            str(item.get("source") or "mewcode"),
            str(item.get("node_id") or "local"),
            str(item.get("external_id") or item.get("id") or ""),
            str(item.get("id") or ""),
        )
        if key not in blocked:
            kept.append(item)
    return kept


@app.get("/api/workspace-tree")
async def api_workspace_tree() -> JSONResponse:
    await asyncio.to_thread(EXTERNAL_AGENTS.refresh)
    sidebar = SESSION.sidebar_payload()
    nodes = _workspace_nodes()
    config = _tailscale_config()

    def fetch_history(node_id: str) -> dict:
        return remote_node_request(config, node_id, "/api/remote/history")

    tree = await asyncio.to_thread(
        collect_workspace_tree,
        sidebar=sidebar,
        external_items=_filter_sidebar_items(EXTERNAL_AGENTS.list()),
        nodes=nodes,
        fetch_history=fetch_history,
    )
    if isinstance(tree, dict):
        tree["unread"] = list(_load_sidebar_conversation_state().get("unread") or [])
    return JSONResponse(tree, headers={"Cache-Control": "no-store"})




@app.post("/api/workspace/conversation-state")
async def api_workspace_conversation_state(body: WorkspaceConversationStateBody) -> JSONResponse:
    action = (body.action or "").strip().lower()
    allowed = {
        "hide", "archive", "unhide", "unarchive",
        "mark_read", "mark_unread", "mark_all_read", "archive_all",
    }
    if action not in allowed:
        return JSONResponse({"error": "unsupported conversation state action"}, status_code=400)
    keys = _workspace_conversation_keys(body)
    if not keys:
        return JSONResponse({"error": "conversation identity required"}, status_code=400)
    state = _load_sidebar_conversation_state()
    hidden = set(state["hidden"])
    archived = set(state["archived"])
    unread = set(state.get("unread") or [])
    if action == "hide":
        hidden.update(keys)
    elif action == "archive":
        archived.update(keys)
        hidden.difference_update(keys)
        unread.difference_update(keys)
    elif action == "archive_all":
        archived.update(keys)
        hidden.difference_update(keys)
        unread.difference_update(keys)
    elif action == "unhide":
        hidden.difference_update(keys)
    elif action == "unarchive":
        archived.difference_update(keys)
    elif action == "mark_read":
        unread.difference_update(keys)
    elif action == "mark_unread":
        unread.update(keys)
    elif action == "mark_all_read":
        unread.difference_update(keys)
    _save_sidebar_conversation_state({
        "hidden": list(hidden),
        "archived": list(archived),
        "unread": list(unread),
    })
    return JSONResponse({
        "ok": True,
        "action": action,
        "count": len(keys),
        "keys": keys,
        "key": keys[0],
    })

@app.post("/api/workspace/continue")
async def api_workspace_continue(body: WorkspaceContinueBody) -> JSONResponse:
    source = body.source.strip()
    external_id = body.external_id.strip()
    node_id = (body.node_id or "local").strip() or "local"
    if source not in {"codex", "claude-code"} or not external_id:
        return JSONResponse({"error": "only Codex or Claude Code conversations can be continued"}, status_code=400)
    detail = None
    source_path = ""
    if node_id != "local":
        try:
            detail = await asyncio.to_thread(
                remote_node_request,
                _tailscale_config(),
                node_id,
                "/api/remote/history/item?source=" + quote(source, safe="") + "&external_id=" + quote(external_id, safe=""),
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    else:
        await asyncio.to_thread(EXTERNAL_AGENTS.refresh)
        detail = EXTERNAL_AGENTS.get(source, external_id)
        if detail:
            source_path = str(detail.get("path") or "")
    if not isinstance(detail, dict):
        return JSONResponse({"error": "external conversation not found"}, status_code=404)
    title = str(detail.get("title") or "未命名对话")
    folder = Path(str(detail.get("parent_folder") or detail.get("project_path") or "")).name
    text, truncated = build_import_text(source, title, detail.get("messages") or [])
    matched_dir = ""
    if folder and folder != "普通对话":
        for project in SESSION.sidebar_payload().get("projects") or []:
            if str(project.get("label") or "") == folder:
                matched_dir = str(project.get("dir") or "")
                break
    if matched_dir:
        err = await SESSION.aactivate_project(matched_dir)
        if err:
            return JSONResponse({"error": err}, status_code=400)
        created = SESSION.new_conversation("project")
    else:
        created = SESSION.new_conversation("user")
    imported = Message(role="user", content=text)
    SESSION.session.append(imported)
    SESSION.conversation.history.append(imported)
    SESSION.session.meta.title = ("继续：" + title)[:50]
    SESSION.session.save_meta()
    return JSONResponse({
        "id": created["id"],
        "scope": "normal" if created["scope"] == "user" else "project",
        "dir": matched_dir,
        "truncated": truncated,
        "source_path": source_path,
        "title": SESSION.session.meta.title,
    })


@app.get("/api/browse")
async def api_browse(path: str = Query("")) -> JSONResponse:
    """目录浏览（项目文件夹选择器）。只列子目录，不暴露文件。"""
    target = os.path.abspath(path) if path else os.path.abspath(os.sep)
    if not os.path.isdir(target):
        return JSONResponse({"error": "目录不存在"}, status_code=404)
    try:
        dirs = sorted(
            entry.name
            for entry in os.scandir(target)
            if entry.is_dir() and not entry.name.startswith(".")
        )[:200]
    except OSError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    parent = os.path.dirname(target)
    return JSONResponse({
        "path": target,
        "parent": parent if parent != target else "",
        "dirs": dirs,
    })


@app.post("/api/projects/add")
async def api_project_add(body: DirBody) -> JSONResponse:
    norm = os.path.abspath(body.dir)
    if not os.path.isdir(norm):
        return JSONResponse({"error": "目录不存在"}, status_code=404)
    _register_project(norm)
    return JSONResponse({"dir": norm, "label": Path(norm).name or norm})


@app.post("/api/projects/remove")
async def api_project_remove(body: DirBody) -> JSONResponse:
    norm = os.path.abspath(body.dir)
    if norm == os.path.abspath(SESSION.work_dir):
        return JSONResponse({"error": "不能移除当前激活项目"}, status_code=400)
    _unregister_project(norm)
    return JSONResponse({"removed": True})


@app.post("/api/projects/activate")
async def api_project_activate(body: DirBody) -> JSONResponse:
    err = await SESSION.aactivate_project(body.dir)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return JSONResponse({"work_dir": SESSION.work_dir, "status": SESSION.status_payload()})


@app.get("/api/view_conversation")
async def api_view_conversation(
    dir: str = Query(""), session_id: str = Query("")
) -> JSONResponse:
    result = SESSION.view_conversation(dir, session_id)
    if result is None:
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    return JSONResponse(result)


@app.get("/api/conversations")
async def api_conversations(
    scope: str = Query("project"), q: str = Query("")
) -> JSONResponse:
    if scope == "normal":
        scope = "user"
    return JSONResponse({"items": SESSION.list_conversations(scope, q)})


@app.post("/api/conversations/new")
async def api_new_conversation(body: NewConvBody) -> JSONResponse:
    if body.scope == "normal":
        return JSONResponse(SESSION.new_conversation("user"))
    # 项目对话：指定目录且非当前项目时先激活该项目
    if body.dir:
        err = await SESSION.aactivate_project(body.dir)
        if err:
            return JSONResponse({"error": err}, status_code=400)
    return JSONResponse(SESSION.new_conversation("project"))


@app.get("/api/conversations/{scope}/{session_id}")
async def api_load_conversation(scope: str, session_id: str) -> JSONResponse:
    if scope == "normal":
        scope = "user"
    # Session ownership stays on the host loop. Large, read-only display data
    # and JSON encoding must not stall WebSocket execution updates.
    result = SESSION.load_conversation(scope, session_id, include_display=False)
    if result is None:
        return JSONResponse({"error": "会话不存在"}, status_code=404)
    sessions_dir = SESSION.session_manager(result['scope'])._sessions_dir
    def display_response():
        result['pipeline_events'] = SESSION.pipeline_events(
            result['scope'], session_id, result['entries'], sessions_dir=sessions_dir)
        result['expert_runs'] = EXPERT_RUNS.chat_for_conversation(session_id)
        return JSONResponse(result)
    return await asyncio.to_thread(display_response)


@app.delete("/api/conversations/{scope}/{session_id}")
async def api_delete_conversation(
    scope: str, session_id: str, dir: str = Query("")
) -> JSONResponse:
    if scope == "normal":
        scope = "user"
    result = SESSION.delete_conversation(scope, session_id, dir_path=dir)
    if result["deleted"]:
        ATTACHMENTS.delete_conversation(session_id)
    status = 200 if result["deleted"] else 400
    return JSONResponse(result, status_code=status)


@app.get("/api/tasks")
async def api_tasks() -> JSONResponse:
    return JSONResponse({"items": SESSION.tasks_payload()})


@app.get("/api/tasks/{task_id}")
async def api_task(task_id: str) -> JSONResponse:
    result = SESSION.task_manager.task_payload(task_id)
    if result is None:
        return JSONResponse({"error": "任务不存在"}, status_code=404)
    return JSONResponse(result)


@app.post("/api/tasks/{task_id}/cancel")
async def api_cancel_task(task_id: str) -> JSONResponse:
    manager = SESSION.task_manager
    task = manager.get(task_id)
    current_id = SESSION.session.session_id if SESSION.session else ""
    current_scope = str(getattr(SESSION, "scope", "project") or "")
    current_store = os.path.normcase(
        os.path.abspath(SESSION._conversation_store_dir())
    )
    if (
        task is None
        or not current_id
        or not task.owner_session_id
        or task.owner_session_id != current_id
        or task.owner_scope != current_scope
        or not task.owner_store_dir
        or os.path.normcase(os.path.abspath(task.owner_store_dir)) != current_store
    ):
        return JSONResponse({"error": "任务不属于当前对话"}, status_code=404)
    if not manager.cancel(task_id):
        return JSONResponse(
            {"error": "task is not running or was not found"}, status_code=409
        )
    settle_tasks = getattr(manager, "settle_tasks", None)
    pending = (
        await settle_tasks([task_id], timeout=1.0)
        if callable(settle_tasks) else []
    )
    task = manager.get(task_id)
    cleanup_pending = bool(pending) or bool(
        task is not None and task.status == "running" and task.cancellation_pending
    )
    cleanup_failed = bool(
        task is not None
        and task.lifecycle_finalized
        and not task.cleanup_confirmed
    )
    return JSONResponse({
        "cancelled": True,
        "status": task.status if task is not None else "cancelled",
        "cancellation_pending": cleanup_pending,
        "cleanup_pending": cleanup_pending,
        "cleanup_failed": cleanup_failed,
        "cleanup_error": task.cleanup_error if task is not None else "",
    })


@app.get("/api/agent_tasks")
async def api_agent_tasks() -> JSONResponse:
    """后台任务面板：子 agent / 团队分组的实时遥测（轮询）。"""
    payload = SESSION.agent_tasks_payload()
    # Refresh both task surfaces from the same conversation snapshot. Background
    # tasks can finish without a chat turn/status broadcast to update the sidebar.
    payload["tasks"] = SESSION.tasks_payload()
    return JSONResponse(payload)


@app.get("/api/expert-runs/{run_id}")
async def api_expert_run(
    run_id: str, role: str = Query(""), node_id: str = Query(""),
    attempt: int = Query(0, ge=0), node_attempt: int = Query(0, ge=0), member_id: str = Query("")
) -> JSONResponse:
    normalized_role = role if role in {
        "manager", "planner", "executor", "auditor",
    } else ""
    snapshot = await asyncio.to_thread(EXPERT_RUNS.read_public, run_id, role=node_id or normalized_role)
    if snapshot is None:
        return JSONResponse({"error": "专家任务不存在"}, status_code=404)
    if node_id or attempt or node_attempt or member_id:
        snapshot["events"] = [event for event in snapshot.get("events") or []
                              if (not node_id or event.get("node_id") == node_id)
                              and (not attempt or int(event.get("attempt") or 0) == attempt)
                              and (not node_attempt or int(event.get("node_attempt") or 0) == node_attempt)
                              and str(event.get("member_id") or "") == member_id]
    return JSONResponse(snapshot)


@app.get('/api/expert-runs/{run_id}/history')
async def api_expert_run_history(run_id: str, after: int = Query(-1, ge=-1), limit: int = Query(50, ge=1, le=100),
                                 node_id: str = '', attempt: int = Query(0, ge=0),
                                 node_attempt: int = Query(0, ge=0), member_id: str | None = None):
    conversation_id = await asyncio.to_thread(EXPERT_RUNS.read_owner, run_id)
    if not conversation_id or conversation_id != _active_conversation_id():
        return JSONResponse({'error': '专家任务不存在'}, status_code=404)
    page = await asyncio.to_thread(EXPERT_RUNS.history_page, run_id, after=after, limit=limit, node_id=node_id,
        attempt=attempt, node_attempt=node_attempt, member_id=member_id)
    if conversation_id != _active_conversation_id():
        return JSONResponse({'error': '专家任务不存在'}, status_code=404)
    return JSONResponse(dict(page, conversation_id=conversation_id))


@app.post('/api/expert-runs/{run_id}/migrate-kernel')
async def api_migrate_kernel(run_id: str, request: Request) -> JSONResponse:
    from mewcode.kernel.migration import migrate
    try:
        body = await request.json()
        source = EXPERT_RUNS.load(run_id)
        if source is None or source.get('conversation_id') != body.get('conversation_id'):
            return JSONResponse({'error': '任务不存在或不属于当前对话。'}, status_code=404)
        if SESSION.chat_busy(source['conversation_id']) or source.get('active_node_ids') or source.get('status') == 'running':
            raise ValueError('请先停止原运行，再迁移。')
        if source.get('updated_at') != body.get('expected_updated_at'):
            raise ValueError('原运行状态已变化，请重新查看后迁移。')
        result = await asyncio.to_thread(migrate, EXPERT_RUNS, source, workspace=SESSION.work_dir)
        return JSONResponse({'snapshot': EXPERT_RUNS.public(result['run_id']), 'source_run_id': run_id,
                             'run_id': result['run_id'], 'model_calls': 0})
    except (ValueError, OSError, KeyError) as exc:
        return JSONResponse({'error': str(exc)}, status_code=409)


@app.get("/api/expert-runs/{run_id}/checkpoints")
async def api_expert_checkpoints(run_id: str, conversation_id: str = Query(...)) -> JSONResponse:
    try:
        snapshot = EXPERT_RUNS.load(run_id)
        if snapshot is None or snapshot.get("conversation_id") != conversation_id:
            return JSONResponse({"error": "任务不存在或不属于该对话"}, status_code=404)
        from mewcode.expert_runs import redact_for_display
        return JSONResponse(redact_for_display({
            "run_id": run_id, "conversation_id": conversation_id,
            "updated_at": snapshot.get("updated_at"),
            "can_restore": not SESSION.chat_busy(conversation_id) and snapshot.get("status") != "running",
            "checkpoints": EXPERT_RUNS.checkpoint_archive.list(run_id),
        }))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.get("/api/expert-runs/{run_id}/files/{file_id}")
async def api_expert_process_file(run_id: str, file_id: str, conversation_id: str = Query(...)) -> JSONResponse:
    """Return only a captured receipt; never accept a filesystem path from the UI."""
    visible_id = SESSION.session.session_id if SESSION.session is not None else ""
    if not visible_id or visible_id != conversation_id:
        return JSONResponse({"error": "请切换到文件所属对话"}, status_code=403)
    if run_id.startswith("chat:"):
        # Chat-driven report: the receipt lives in the conversation's report
        # store, not in an expert run, so serve it from there.
        record = CHAT_REPORTS.preview(
            work_dir=SESSION.work_dir, conversation_id=conversation_id, file_id=file_id
        )
        if record is None:
            return JSONResponse({"error": "过程文件快照不存在"}, status_code=404)
        return JSONResponse({"file": record}, headers={"Cache-Control": "no-store"})
    try:
        snapshot = EXPERT_RUNS.load(run_id)
    except ValueError:
        snapshot = None
    if snapshot is None or snapshot.get("conversation_id") != conversation_id:
        return JSONResponse({"error": "任务不存在或不属于该对话"}, status_code=404)
    record = EXPERT_RUNS.process_file_preview(run_id, file_id)
    if record is None and file_id.startswith('collab:'):
        from frontend.workbench_files import collaborative_file
        try:
            record = collaborative_file(snapshot, file_id, SESSION.work_dir)
        except (OSError, ValueError) as exc:
            return JSONResponse({'error': str(exc)}, status_code=409)
    if record is None:
        return JSONResponse({"error": "过程文件快照不存在"}, status_code=404)
    return JSONResponse({"file": record}, headers={"Cache-Control": "no-store"})


@app.post("/api/expert-runs/{run_id}/capability-executor")
async def api_put_run_capability_executor(run_id: str, body: RunCapabilityExecutorBody) -> JSONResponse:
    if not _capability_executor_scope_matches(body) or SESSION.chat_busy(body.conversation_id):
        return JSONResponse({"error": "请在原对话暂停执行后调整辅助执行器"}, status_code=409)
    try:
        snapshot = EXPERT_RUNS.load(run_id)
    except ValueError:
        snapshot = None
    if snapshot is None or snapshot.get("conversation_id") != body.conversation_id:
        return JSONResponse({"error": "任务不存在或不属于该对话"}, status_code=404)
    try:
        selected = await _validated_capability_executor(body.model)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    # Model discovery may await a provider. Recheck ownership and runtime
    # activity before the store's atomic checkpoint-version comparison.
    if not _capability_executor_scope_matches(body) or SESSION.chat_busy(body.conversation_id):
        return JSONResponse({"error": "对话已切换或任务已开始执行，未修改辅助执行器"}, status_code=409)
    try:
        changed = EXPERT_RUNS.set_capability_executor(
            run_id, conversation_id=body.conversation_id,
            model=selected.as_dict() if selected else None,
            expected_updated_at=body.expected_updated_at, command_id=body.command_id,
            reason="用户在编排配置中选择本次运行的辅助执行器",
        )
    except (ValueError, OSError, KeyError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    public = EXPERT_RUNS._public_value(changed, include_details=False)
    await WS_EVENTS.send_json({"type": "expert_checkpoint_restored", "run_id": run_id,
                               "conversation_id": body.conversation_id, "snapshot": public})
    return JSONResponse({"snapshot": public})


@app.get("/api/expert-runs/{run_id}/node-model")
async def api_get_node_model(run_id: str, node_id: str, conversation_id: str, project: str) -> JSONResponse:
    scope = CapabilityExecutorBody(project=project, conversation_id=conversation_id)
    if not _capability_executor_scope_matches(scope):
        return JSONResponse({"error": "请在原项目和对话中读取节点模型"}, status_code=409)
    try:
        from mewcode.kernel.collaboration_graph import effective_flow
        from mewcode.kernel.collaboration import valid_certificate
        from mewcode.kernel.collaboration_recovery import unresolved_calls
        from mewcode.experts.model_route_recovery import current_node_model
        snapshot = EXPERT_RUNS.load(run_id)
        if snapshot is None or snapshot.get("conversation_id") != conversation_id:
            return JSONResponse({"error": "任务不存在或不属于该对话"}, status_code=404)
        item = effective_flow(snapshot).node_map.get(node_id)
        if item is None:
            raise ValueError("节点已变化，请刷新编排图")
        collaboration = snapshot.get("collaboration") or {}
        reason = ""
        if item.kind != "role" or collaboration.get("contracts", {}).get(node_id, {}).get("join_children"):
            reason = "此节点由宿主处理，请选择实际执行的节点。"
        elif ((snapshot.get("node_states") or {}).get(node_id, {}).get("status") == "completed"
              or collaboration and valid_certificate(collaboration, node_id)):
            reason = "已完成节点的模型保留为执行记录。"
        elif snapshot.get("status") not in {"paused", "blocked", "needs_attention"} or snapshot.get("active_node_ids"):
            reason = "请先暂停流程，再修改未完成节点的模型。"
        elif unresolved_calls(snapshot, [node_id]):
            reason = "节点仍有未结算调用，请先核对执行结果。"
        binding = current_node_model(snapshot, item.as_dict())
        return JSONResponse({"node_id": node_id, "run_id": run_id, "conversation_id": conversation_id,
            "model": ModelRef.from_value(binding).as_dict() if binding else None,
            "updated_at": snapshot["updated_at"], "editable": not reason, "reason": reason})
    except (ValueError, KeyError, OSError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)


@app.post("/api/expert-runs/{run_id}/difficult-node-model")
async def api_put_difficult_node_model(run_id: str, body: DifficultNodeModelBody) -> JSONResponse:
    if not _capability_executor_scope_matches(body) or SESSION.chat_busy(body.conversation_id):
        return JSONResponse({"error": "请在原对话暂停执行后调整困难节点模型"}, status_code=409)
    try:
        snapshot = EXPERT_RUNS.load(run_id)
        if snapshot is None or snapshot.get("conversation_id") != body.conversation_id:
            return JSONResponse({"error": "任务不存在或不属于该对话"}, status_code=404)
        from mewcode.experts.model_route_recovery import validate_difficulty_model, current_node_model
        selected = ModelRef.from_value(body.model)
        validate_difficulty_model(MODEL_REGISTRY, selected)
        from mewcode.kernel.collaboration_graph import effective_flow
        nodes = {node.node_id: node.as_dict() for node in effective_flow(snapshot).nodes}
        for node_id in body.node_ids:
            node = nodes.get(node_id) or {}
            current = current_node_model(snapshot, node) if node else None
            if current:
                validate_difficulty_model(MODEL_REGISTRY, selected, current, allow_unavailable_current=True)
        if not _capability_executor_scope_matches(body) or SESSION.chat_busy(body.conversation_id):
            return JSONResponse({"error": "对话已切换或任务开始执行，未修改模型"}, status_code=409)
        changed = EXPERT_RUNS.set_difficult_node_model(run_id, conversation_id=body.conversation_id,
            model=selected.as_dict(), node_ids=body.node_ids, apply_node_ids=body.apply_node_ids,
            expected_updated_at=body.expected_updated_at, command_id=body.command_id,
            reason="用户明确选择本次运行的困难节点模型" if body.record_backup else "用户在编排图中修改节点模型",
            record_backup=body.record_backup)
    except (ValueError, OSError, KeyError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    public = EXPERT_RUNS._public_value(changed, include_details=False)
    await WS_EVENTS.send_json({"type": "expert_checkpoint_restored", "run_id": run_id,
                               "conversation_id": body.conversation_id, "snapshot": public})
    return JSONResponse({"snapshot": public})


@app.post("/api/expert-runs/{run_id}/verifier-model")
async def api_put_verifier_model(run_id: str, body: RunCapabilityExecutorBody) -> JSONResponse:
    if not _capability_executor_scope_matches(body) or SESSION.chat_busy(body.conversation_id):
        return JSONResponse({'error': '请在原对话暂停执行后调整核查模型'}, status_code=409)
    try:
        snapshot = EXPERT_RUNS.load(run_id)
        if not snapshot or snapshot.get('conversation_id') != body.conversation_id:
            return JSONResponse({'error': '任务不存在或不属于该对话'}, status_code=404)
        from mewcode.experts.model_route_recovery import validate_difficulty_model
        selected = ModelRef.from_value(body.model)
        previous = (snapshot.get('role_models') or {}).get('roles', {}).get('verifier')
        validate_difficulty_model(MODEL_REGISTRY, selected, previous, allow_unavailable_current=True)
        catalog = await MODEL_REGISTRY.catalog(local_only=True)
        if not _capability_executor_scope_matches(body) or SESSION.chat_busy(body.conversation_id):
            return JSONResponse({'error': '对话已切换或任务已开始执行，未修改核查模型'}, status_code=409)
        changed = EXPERT_RUNS.set_verifier_model(run_id, conversation_id=body.conversation_id,
            model=selected.as_dict(), catalog=catalog, expected_updated_at=body.expected_updated_at, command_id=body.command_id)
    except (ValueError, OSError, KeyError) as exc:
        return JSONResponse({'error': str(exc)}, status_code=409)
    public = EXPERT_RUNS._public_value(changed, include_details=False)
    await WS_EVENTS.send_json({'type': 'expert_checkpoint_restored', 'run_id': run_id,
        'conversation_id': body.conversation_id, 'snapshot': public})
    return JSONResponse({'snapshot': public})


@app.post("/api/expert-runs/{run_id}/loop-control")
async def api_expert_loop_control(run_id: str, body: LoopControlBody) -> JSONResponse:
    visible_id = SESSION.session.session_id if SESSION.session is not None else ""
    if body.conversation_id != visible_id or SESSION.chat_busy(body.conversation_id):
        return JSONResponse({"error": "请在原对话暂停执行后调整运行方式"}, status_code=409)
    snapshot = EXPERT_RUNS.load(run_id)
    if snapshot is None or snapshot.get("conversation_id") != body.conversation_id:
        return JSONResponse({"error": "任务不存在或不属于该对话"}, status_code=404)
    try:
        changed = EXPERT_RUNS.set_loop_control_mode(run_id, mode=body.mode,
            expected_updated_at=body.expected_updated_at, command_id=body.command_id,
            reason="用户在运行设置中选择：" + body.mode)
    except (ValueError, OSError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    public = EXPERT_RUNS._public_value(changed, include_details=False)
    await WS_EVENTS.send_json({"type": "expert_checkpoint_restored", "run_id": run_id,
                               "conversation_id": body.conversation_id, "snapshot": public})
    return JSONResponse({"snapshot": public})  # No implicit resume or provider call.


@app.post("/api/expert-runs/{run_id}/loop-token-policy")
async def api_expert_loop_token_policy(run_id: str, body: LoopTokenPolicyBody) -> JSONResponse:
    visible_id = SESSION.session.session_id if SESSION.session is not None else ""
    if body.conversation_id != visible_id or SESSION.chat_busy(body.conversation_id):
        return JSONResponse({"error": "请在原对话暂停执行后授权 Token 总额"}, status_code=409)
    snapshot = EXPERT_RUNS.load(run_id)
    if snapshot is None or snapshot.get("conversation_id") != body.conversation_id:
        return JSONResponse({"error": "任务不存在或不属于该对话"}, status_code=404)
    try:
        changed = EXPERT_RUNS.set_loop_token_policy(
            run_id, loop_id=body.loop_id, expected_updated_at=body.expected_updated_at,
            command_id=body.command_id, reason="用户明确授权有限自适应 Token 总额",
            ceiling=body.ceiling, floor=body.floor,
            rate_per_minute=body.rate_per_minute,
            burst_tokens=body.burst_tokens,
        )
    except (ValueError, OSError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    public = EXPERT_RUNS._public_value(changed, include_details=False)
    await WS_EVENTS.send_json({"type": "expert_checkpoint_restored", "run_id": run_id,
                               "conversation_id": body.conversation_id, "snapshot": public})
    return JSONResponse({"snapshot": public})  # Authorization does not resume the run.


@app.get("/api/expert-runs/{run_id}/saved-plan-revision")
async def api_saved_plan_revision(run_id: str, conversation_id: str = Query(...), repair_loop_id: str = "") -> JSONResponse:
    from mewcode.orchestration.saved_revision import revision_preview
    from mewcode.orchestration.revisions import saved_execution_flow
    from mewcode.orchestration.takeover import pause_dialogue_anchor
    visible = SESSION.session.session_id if SESSION.session else ""
    snapshot = EXPERT_RUNS.load(run_id)
    if visible != conversation_id or not snapshot or snapshot.get("conversation_id") != conversation_id:
        return JSONResponse({"error": "请切换到原任务所属对话"}, status_code=409)
    try:
        flow = saved_execution_flow(SESSION.work_dir,
            flow_store=_conversation_flow_store(conversation_id), plan_store=_conversation_plan_store(conversation_id))
        proposal = revision_preview(snapshot, flow, repair_loop_id=repair_loop_id)
        proposal["dialogue_offset"] = pause_dialogue_anchor(SESSION.session, snapshot)["record_offset"]
        if proposal["dialogue_offset"] is None:
            raise ValueError("无法读取暂停对话记录，请重新打开当前对话")
        return JSONResponse(proposal)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)


@app.post("/api/expert-runs/{run_id}/saved-plan-revision")
async def api_accept_saved_plan_revision(run_id: str, body: SavedPlanRevisionBody) -> JSONResponse:
    from mewcode.orchestration.saved_revision import adopt_revision
    from mewcode.orchestration.revisions import saved_execution_flow
    from mewcode.orchestration.takeover import pause_dialogue_anchor
    visible = SESSION.session.session_id if SESSION.session else ""
    if visible != body.conversation_id or SESSION.chat_busy(body.conversation_id):
        return JSONResponse({"error": "请在原对话暂停后接纳修订"}, status_code=409)
    try:
        flow = saved_execution_flow(SESSION.work_dir,
            flow_store=_conversation_flow_store(body.conversation_id),
            plan_store=_conversation_plan_store(body.conversation_id))
        def apply(snapshot):
            if snapshot.get("conversation_id") != body.conversation_id:
                raise ValueError("任务不属于当前对话")
            candidate = adopt_revision(snapshot, flow, body.expected_updated_at, body.fingerprint,
                                       repair_loop_id=body.repair_loop_id, root=SESSION.work_dir)
            anchor = pause_dialogue_anchor(SESSION.session, candidate)
            if anchor["record_offset"] != body.dialogue_offset:
                raise ValueError("核验后新增了对话记录，请重新核对修订")
            backup = EXPERT_RUNS.checkpoint_archive.save(snapshot, turn_key="before-saved-plan:" + body.fingerprint,
                                                       kind="before_saved_plan_revision")
            candidate["saved_plan_adoptions"][-1]["checkpoint_id"] = backup["checkpoint_id"]
            candidate["pause_dialogue_anchor"] = anchor
            snapshot.clear()
            snapshot.update(candidate)
        changed = EXPERT_RUNS.mutate(run_id, apply, durable=True)
    except (ValueError, OSError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    public = EXPERT_RUNS._public_value(changed, include_details=False)
    await WS_EVENTS.send_json({"type": "expert_checkpoint_restored", "run_id": run_id,
        "conversation_id": body.conversation_id, "snapshot": public})
    return JSONResponse({"snapshot": public})


@app.post("/api/expert-runs/{run_id}/checkpoints/{checkpoint_id}/restore")
async def api_restore_expert_checkpoint(run_id: str, checkpoint_id: str, body: CheckpointRestoreBody) -> JSONResponse:
    visible_id = SESSION.session.session_id if SESSION.session is not None else ""
    if body.conversation_id != visible_id or SESSION.chat_busy(body.conversation_id):
        return JSONResponse({"error": "请切换到原对话并暂停执行后再恢复检查点"}, status_code=409)
    try:
        restored = EXPERT_RUNS.restore_turn_checkpoint(
            run_id, checkpoint_id, conversation_id=body.conversation_id,
            expected_updated_at=body.expected_updated_at,
        )
    except (ValueError, OSError, EOFError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    snapshot = EXPERT_RUNS._public_value(restored, include_details=False)
    await WS_EVENTS.send_json({"type": "expert_checkpoint_restored", "run_id": run_id,
                               "conversation_id": body.conversation_id, "snapshot": snapshot})
    return JSONResponse({"snapshot": snapshot})


@app.get("/api/usage/stats")
async def api_usage_stats(days: int = Query(0)) -> JSONResponse:
    """用量统计聚合：days=0 全部，30/7 为最近 N 天（含今天）。"""
    return JSONResponse(_usage_stats(days))


@app.get("/api/usage/summary")
async def api_usage_summary(
    period: str = Query("current_month"),
) -> JSONResponse:
    if period != "current_month":
        return JSONResponse(
            {"error": "v1 only supports period=current_month"}, status_code=422
        )
    return JSONResponse(USAGE_RUNTIME.summary())


@app.get("/api/usage/providers")
async def api_usage_providers() -> JSONResponse:
    """Per-configured-Provider metering cards (official APIs + relay probes)."""
    try:
        usage_service = _session_usage_service(SESSION.work_dir)
        payload = await collect_provider_meters(
            list(MODEL_REGISTRY.records()),
            usage_service=usage_service,
        )
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})
    except Exception as exc:  # noqa: BLE001 — surface probe wiring failures to UI
        return JSONResponse(
            {"error": f"Provider 计量失败: {exc}", "providers": [], "count": 0},
            status_code=500,
            headers={"Cache-Control": "no-store"},
        )


@app.get("/api/usage/accounts")
async def api_usage_accounts() -> JSONResponse:
    return JSONResponse({
        "accounts": [item.to_dict() for item in USAGE_RUNTIME.list_accounts()],
        "adapters": USAGE_RUNTIME.adapters.ids(),
    })


def _save_public_usage_account(payload: dict[str, Any], account_id: str = ""):
    value = dict(payload)
    if "secret_values" in value:
        raise ValueError("usage secrets must be saved through Electron safeStorage")
    if account_id:
        value["id"] = account_id
    value["managed"] = False
    value["source_kind"] = "manual"
    adapter_id = str(value.get("adapter_id") or "")
    if adapter_id:
        USAGE_RUNTIME.adapters.get(adapter_id)
    if adapter_id == "plugin":
        plugin_id = str(value.get("plugin_id") or "")
        manifest = USAGE_RUNTIME.plugins.get_plugin(plugin_id) if plugin_id else None
        if plugin_id and manifest is None:
            raise ValueError(f"usage plugin is not installed: {plugin_id}")
        if manifest and manifest.get("source_kind") == "single_py":
            value["source_kind"] = "custom_script"
    return USAGE_RUNTIME.save_account(value)


@app.post("/api/usage/accounts")
async def api_create_usage_account(body: dict[str, Any]) -> JSONResponse:
    try:
        account = _save_public_usage_account(body)
    except (KeyError, TypeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(account.to_dict(), status_code=201)


@app.patch("/api/usage/accounts/{account_id}")
async def api_patch_usage_account(
    account_id: str, body: dict[str, Any]
) -> JSONResponse:
    if USAGE_RUNTIME.get_account(account_id) is None:
        return JSONResponse({"error": "usage account not found"}, status_code=404)
    try:
        account = _save_public_usage_account(body, account_id)
    except (KeyError, TypeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(account.to_dict())


@app.delete("/api/usage/accounts/{account_id}")
async def api_delete_usage_account(account_id: str) -> JSONResponse:
    if not USAGE_RUNTIME.delete_account(account_id):
        return JSONResponse({"error": "usage account not found"}, status_code=404)
    return JSONResponse({"deleted": True})


@app.post("/api/usage/accounts/{account_id}/refresh")
async def api_refresh_usage_account(
    account_id: str, force: bool = Query(False)
) -> JSONResponse:
    try:
        run = await USAGE_RUNTIME.start_refresh(account_id, force=force)
    except KeyError:
        return JSONResponse({"error": "usage account not found"}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    return JSONResponse(
        {"run_id": run.id, "run": run.to_dict()}, status_code=202
    )


@app.get("/api/usage/runs/{run_id}")
async def api_usage_run(run_id: str) -> JSONResponse:
    run = USAGE_RUNTIME.get_run(run_id)
    if run is None:
        return JSONResponse({"error": "usage run not found"}, status_code=404)
    return JSONResponse(run.to_dict())


@app.post("/api/usage/runs/{run_id}/cancel")
async def api_cancel_usage_run(run_id: str) -> JSONResponse:
    if not USAGE_RUNTIME.cancel_run(run_id):
        return JSONResponse(
            {"error": "usage run is not active"}, status_code=409
        )
    return JSONResponse({"cancelled": True})


@app.post("/api/usage/browser/result")
async def api_usage_browser_result(
    body: UsageBrowserResultBody, request: Request
) -> JSONResponse:
    if DESKTOP_TOKEN and not _request_has_desktop_token(request):
        return JSONResponse({"error": "desktop session token required"}, status_code=403)
    resolved = USAGE_RUNTIME.resolve_browser_command(
        body.command_id, result=body.result, error=body.error
    )
    if not resolved:
        return JSONResponse({"error": "browser command is no longer pending"}, status_code=404)
    return JSONResponse({"resolved": True})


@app.get("/api/usage/plugins")
async def api_usage_plugins() -> JSONResponse:
    return JSONResponse({"plugins": USAGE_RUNTIME.plugins.list_plugins()})


def _public_usage_slots() -> list[dict[str, Any]]:
    slots: list[dict[str, Any]] = []
    for record in MODEL_REGISTRY.records():
        provider = record.provider
        is_codex_oauth = (
            record.source == "codex"
            and "chatgpt.com" in provider.base_url.lower()
        )
        parsed_url = urlsplit(provider.base_url)
        netloc = parsed_url.hostname or ""
        if parsed_url.port:
            netloc += f":{parsed_url.port}"
        public_base_url = urlunsplit((
            parsed_url.scheme,
            netloc,
            parsed_url.path,
            "",
            "",
        ))
        slots.append({
            "provider_key": record.key,
            "name": provider.name,
            "protocol": provider.protocol,
            "provider_type": provider.provider_type,
            "base_url": public_base_url,
            "model": provider.model,
            "capabilities": {
                "chat": True,
                "reasoning": bool(record.effort_levels) or bool(
                    model_effort_levels(
                        provider.protocol,
                        provider.model,
                        catalog_service=MODEL_CAPABILITY_CATALOG,
                    )
                ),
                "vision": model_supports_vision(
                    provider.protocol,
                    provider.model,
                    catalog_service=MODEL_CAPABILITY_CATALOG,
                ),
                "tools": True,
                "usage_account_tracking": (
                    False
                    if provider.provider_type == "opencode-go"
                    else bool(provider.usage.account_tracking)
                ),
                "local_estimate_only": provider.provider_type == "opencode-go",
            },
            "has_secret": bool(provider.resolve_api_key()) and not is_codex_oauth,
            "secret_ref_supported": bool(provider.resolve_api_key()) and not is_codex_oauth,
        })
    return sorted(slots, key=lambda item: (item["name"].lower(), item["provider_key"]))


@app.get("/api/usage/slots")
async def api_usage_slots() -> JSONResponse:
    """Public slot definitions. Credential values are deliberately omitted."""
    return JSONResponse({"slots": _public_usage_slots()})


def _script_plugin_archive(
    script: bytes,
    *,
    filename: str,
    provider_key: str,
    allowed_origins: str,
    allow_browser: bool,
    allow_provider_secret: bool,
) -> bytes:
    if not filename.lower().endswith(".py"):
        raise ValueError("single-file usage script must have a .py extension")
    if len(script) > MAX_PLUGIN_BYTES:
        raise ValueError("usage script exceeds 20 MiB")
    try:
        source = script.decode("utf-8")
        compile(source, filename, "exec")
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise ValueError(f"usage script is not valid UTF-8 Python: {exc}") from exc
    if not re.search(r"(?m)^\s*(?:async\s+)?def\s+collect\s*\(", source):
        raise ValueError("usage script must export collect(query, context)")
    slot = next((item for item in _public_usage_slots() if item["provider_key"] == provider_key), None)
    if slot is None:
        raise ValueError("bound Provider slot was not found")
    origins = [value.strip() for value in allowed_origins.split(",") if value.strip()]
    digest = hashlib.sha256(script).hexdigest()
    stem = re.sub(r"[^a-z0-9._-]+", "-", Path(filename).stem.lower()).strip("-._")
    plugin_id = f"script.{(stem or 'collector')[:80]}.{digest[:12]}"
    secret_properties = (
        {"provider_api_key": {"type": "string", "title": "Provider API Key"}}
        if allow_provider_secret else {}
    )
    manifest = {
        "id": plugin_id,
        "name": f"{slot['name']} 自定义用量脚本",
        "version": "1.0.0",
        "protocol_version": "1",
        "entrypoint": "plugin.py",
        "operations": ["collect", "normalize"],
        "config_schema": {"type": "object"},
        "secret_schema": {
            "type": "object", "properties": secret_properties,
            "additionalProperties": False,
        },
        "artifact_schema": {},
        "permissions": {
            "browser": allow_browser,
            "interactive_login": allow_browser,
            "origins": origins,
        },
        "bound_provider_key": provider_key,
        "bound_slot": slot,
        "script_sha256": digest,
        "source_kind": "single_py",
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("usage-plugin.json", json.dumps(manifest, ensure_ascii=False))
        bundle.writestr("plugin.py", script)
    return output.getvalue()


@app.post("/api/usage/plugins/install")
async def api_install_usage_plugin(
    package: UploadFile = File(...),
    trusted: bool = Query(False),
    provider_key: str = Form(""),
    allowed_origins: str = Form(""),
    allow_browser: bool = Form(False),
    allow_provider_secret: bool = Form(False),
) -> JSONResponse:
    archive = await package.read(MAX_PLUGIN_BYTES + 1)
    try:
        is_script = str(package.filename or "").lower().endswith(".py")
        if is_script:
            archive = _script_plugin_archive(
                archive,
                filename=str(package.filename or "collector.py"),
                provider_key=provider_key,
                allowed_origins=allowed_origins,
                allow_browser=allow_browser,
                allow_provider_secret=allow_provider_secret,
            )
        if trusted:
            manifest = USAGE_RUNTIME.plugins.install_package(archive, trusted=True)
            return JSONResponse({
                "installed": True,
                "plugin": USAGE_RUNTIME.plugins.public_manifest(manifest),
            }, status_code=201)
        inspection = USAGE_RUNTIME.plugins.inspect_package(archive)
        return JSONResponse({
                "installed": False,
                "inspection": USAGE_RUNTIME.plugins.public_manifest(inspection),
                "bound_slot": next(
                    (slot for slot in _public_usage_slots()
                     if slot["provider_key"] == inspection.get("bound_provider_key")),
                    None,
                ),
                "warning": (
                "该 ZIP 会以受限子进程运行，但这不是恶意 Python 代码安全沙箱；"
                "确认 SHA-256 与权限后才能安装。"
            ),
        })
    except (PermissionError, TypeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.delete("/api/usage/plugins/{plugin_id}")
async def api_delete_usage_plugin(plugin_id: str) -> JSONResponse:
    linked = [
        account.name for account in USAGE_RUNTIME.list_accounts()
        if account.plugin_id == plugin_id
    ]
    if linked:
        return JSONResponse({
            "error": "plugin is still used by usage accounts: " + ", ".join(linked)
        }, status_code=409)
    try:
        deleted = USAGE_RUNTIME.plugins.delete_plugin(plugin_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if not deleted:
        return JSONResponse({"error": "usage plugin not found"}, status_code=404)
    return JSONResponse({"deleted": True})


from frontend.team_v2_api import install_team_v2_api
from mewcode.teams.host import TeamHost
from mewcode.mcp.coord_server import project_root as _team_project_root

try:
    _team_provider_configs = tuple(load_config().providers)
except (OSError, ValueError, ConfigError):
    _team_provider_configs = ()

install_team_v2_api(
    app, TeamHost(USER_DATA_ROOT if os.environ.get("MEWCODE_DESKTOP_PROFILE_ISOLATED") == "1" else _team_project_root(),
                  provider_configs=_team_provider_configs), static_dir=STATIC_DIR,
    authorize=lambda request: _request_has_desktop_token(request) or (
        not DESKTOP_TOKEN and _request_is_loopback(request)
        and request.url.hostname in {'localhost', '127.0.0.1', '::1'}),
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ---------------------------------------------------------------------------
# Pipeline API 注册
# ---------------------------------------------------------------------------


def _pipeline_tool_matches(tool: Any, patterns: list[str]) -> bool:
    kind = str(getattr(tool, "interaction_kind", ""))
    for pattern in patterns:
        normalized = pattern.strip().lower()
        if fnmatch.fnmatch(tool.name.lower(), normalized):
            return True
        if normalized in {"browser", "browser.*"} and kind == "browser":
            return True
        if normalized in {"computer", "computer.*", "computer_use", "computer_use.*"} and kind == "computer_use":
            return True
    return False


def _pipeline_action_provider() -> dict[str, Any]:
    """Merge live Cordis actions with the current Agent's enabled tools.

    This function intentionally resolves both sources on every call.  The web
    session replaces ``SESSION.agent`` when a provider/model changes and the
    tool registry can be enabled/disabled while a graph editor remains open;
    a one-time catalog would otherwise leave saved nodes pointing at stale
    tool objects or omit newly available actions.
    """

    slot_actions = normalize_actions(
        APP_CONTEXT.slots.resolve(PIPELINE_ACTION_SLOT) or {}
    )
    session_agent = SESSION.agent
    if session_agent is None:
        return slot_actions
    registry = session_agent.registry
    tool_actions = build_tool_actions(
        registry,
        reserved_ids=slot_actions.keys(),
        permission_checker_provider=lambda: getattr(
            getattr(SESSION, "agent", None), "permission_checker", None
        ),
        work_dir=lambda: SESSION.work_dir,
    )
    return merge_pipeline_actions(slot_actions, tool_actions)


def _build_pipeline_agent(
    model: str | None = None,
    effort: str | None = None,
    allowed_tools: list[str] | None = None,
    allow_computer_use: bool = True,
    unattended: bool = False,
) -> AgentRuntime:
    """为一次 pipeline 调用构建独立的 Agent 实例。

    与 SESSION.agent 共享工具注册表与权限体系，但不共享可变状态
    （client / conversation / token 计数）：
    - 并发节点（parallel 分支）互不干扰，也不会踩到网页聊天的会话状态；
    - 节点级 model / effort 覆盖真正生效（老实现直接忽略这两个参数，
      judgment 的 effort="low" 从未生效过）。
    """
    session = SESSION
    if session.agent is None or session.slot is None:
        raise RuntimeError("No agent available. Please select a provider first.")

    provider = session.slot.provider
    model = (model or "").strip() or None
    if model and model != provider.model:
        provider = resolve_effective_provider_config(provider, model)
    else:
        provider = resolve_effective_provider_config(provider)

    # effort 适用性检查（对齐 switch_model「换模型后不适用则清除」）：
    # 插槽显式声明的档位仅对插槽自身模型有效；覆盖模型按协议+模型名识别。
    # 不适用的档位不发送（回退模型默认行为），避免底层客户端收到非法值
    eff = effort or session._effective_effort()
    if eff:
        if session.slot.effort_levels and provider.model == session.slot.provider.model:
            levels = list(session.slot.effort_levels)
        else:
            levels = model_effort_levels(
                provider.protocol,
                provider.model,
                catalog_service=MODEL_CAPABILITY_CATALOG,
            )
        if eff not in levels:
            eff = ""
    from mewcode.mcp.playwright_browser import select_pipeline_session_tools

    selected_tools = select_pipeline_session_tools(
        session.agent.registry.list_tools(),
        enabled=session.agent.registry.is_enabled,
        allowed_tools=allowed_tools,
        allow_computer_use=allow_computer_use,
        unattended=unattended,
        matches=_pipeline_tool_matches,
    )
    if allowed_tools and not selected_tools:
        raise RuntimeError(
            f"No tools match pipeline whitelist: {', '.join(allowed_tools)}"
        )

    runtime = session.context.get(AGENT_FACTORY).create(AgentRuntimeRequest(
        provider=provider,
        work_dir=session.work_dir,
        permission_checker=session.agent.permission_checker,
        context_window=provider.get_context_window() or session.agent.context_window,
        instructions_content=session.agent.instructions_content,
        memory_manager=session.agent.memory_manager,
        hook_engine=None,
        usage_service=session.usage_service,
        enable_skills=False,
        enable_tool_search=False,
        extra_tools=tuple(selected_tools),
    ))
    if eff:
        runtime.client.set_effort(eff)
    for tool in selected_tools:
        if session.agent.registry.is_discovered(tool.name):
            runtime.registry.mark_discovered(tool.name)
    return runtime


_PIPELINE_INTERACTIVE_LOCK = asyncio.Lock()


async def _pipeline_agent_events(
    agent: Agent, conversation: ConversationManager, interactive: bool
):
    if interactive:
        async with _PIPELINE_INTERACTIVE_LOCK:
            async for event in agent.run(conversation):
                yield event
    else:
        async for event in agent.run(conversation):
            yield event


def _pipeline_interaction_kinds(
    agent: Agent,
    *,
    unattended: bool,
    mode: str,
    boundary: str,
) -> tuple[set[str], bool]:
    """Validate interactive tools and return (kinds, needs_global_lock)."""

    kinds = {
        str(getattr(tool, "interaction_kind", ""))
        for tool in agent.registry.list_tools()
    }
    if "computer_use" in kinds:
        if unattended:
            raise RuntimeError("ComputerUse is disabled for scheduled/unattended pipeline runs")
        if mode != "go" or not boundary.strip():
            raise RuntimeError("ComputerUse requires go mode with an explicit boundary")
    return kinds, bool(kinds & {"browser", "computer_use"})


async def _run_pipeline_agent_turn(
    agent: Agent,
    conversation: ConversationManager,
    *,
    interactive: bool,
    on_event: Callable[[dict], None] | None = None,
) -> tuple[str, dict | None]:
    """Drive one Agent turn while preserving the supplied conversation."""

    result_parts: list[str] = []
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read": 0,
        "cache_creation": 0,
        "cache_usage_available": False,
    }
    completed = False
    fatal_error: str | None = None

    async for event in _pipeline_agent_events(agent, conversation, interactive):
        if isinstance(event, StreamText):
            result_parts.append(event.text)
        elif isinstance(event, StreamRetryEvent):
            if event.discard_chars > 0:
                joined = "".join(result_parts)
                result_parts = (
                    [joined[: -event.discard_chars]]
                    if event.discard_chars < len(joined) else []
                )
            if on_event:
                on_event({
                    "type": "llm_retry",
                    "attempt": event.attempt,
                    "max_attempts": event.max_attempts,
                    "reason": event.reason,
                    "wait": round(event.wait, 1),
                })
        elif isinstance(event, ThinkingText):
            if on_event:
                on_event({"type": "thinking", "text": event.text})
        elif isinstance(event, ToolUseEvent):
            if on_event:
                on_event({
                    "type": "tool_use",
                    "tool_id": event.tool_id,
                    "tool": event.tool_name,
                    "args": event.arguments,
                })
        elif isinstance(event, ToolResultEvent):
            if on_event:
                on_event({
                    "type": "tool_result",
                    "tool_id": event.tool_id,
                    "tool": event.tool_name,
                    "output": (event.output or "")[:2000],
                    "is_error": event.is_error,
                    "elapsed": round(event.elapsed, 2),
                    "dispatch_seconds": (round(event.dispatch_seconds, 2)
                                         if event.dispatch_seconds is not None else None),
                    "content_blocks": list(event.content_blocks),
                })
        elif isinstance(event, UsageEvent):
            usage["input_tokens"] = event.input_tokens
            usage["output_tokens"] = event.output_tokens
            usage["cache_read"] = event.cache_read_tokens
            usage["cache_creation"] = event.cache_creation_tokens
            usage["cache_usage_available"] = event.cache_usage_available
        elif isinstance(event, PermissionRequest):
            if not event.future.done():
                event.future.set_result(PermissionResponse.ALLOW)
        elif isinstance(event, LoopComplete):
            completed = True
        elif isinstance(event, TurnComplete):
            pass
        elif isinstance(event, ErrorEvent):
            fatal_error = event.message
            if on_event:
                on_event({"type": "agent_error", "text": event.message})

    if not completed:
        raise RuntimeError(
            f"Agent terminated abnormally: {fatal_error or 'no completion signal'}"
        )
    return "".join(result_parts), usage


async def _pipeline_agent_factory(
    prompt: str,
    schema=None,
    model: str | None = None,
    effort: str | None = None,
    isolation: str | None = None,
    on_event: Callable[[dict], None] | None = None,
    attachments: list[dict[str, Any]] | None = None,
    tools: list[str] | None = None,
    mode: str = "react",
    boundary: str = "",
    unattended: bool = False,
    **_: Any,
) -> tuple[str, dict | None]:
    """Pipeline 任务执行：驱动完整 Agent 工具循环（ReAct）。

    使用独立 Agent 实例（共享注册表/权限），让任务能真实调用
    WebFetch / Bash / WriteFile 等工具。思考、工具调用、工具结果
    都通过 on_event 实时推送到运行控制台。

    可靠性语义：
    - LLM 流中断由 agent.run 内部原地重试（StreamRetryEvent），
      对话与工具进度不丢失；本函数收到该事件时丢弃已重复的部分正文。
    - Agent 异常终止（迭代上限 / 连续未知工具）→ 抛异常，
      节点失败而不是带着残缺输出「成功」。
    """
    runtime = _build_pipeline_agent(
        model=model, effort=effort, allowed_tools=tools,
        allow_computer_use=not unattended,
        unattended=unattended,
    )
    try:
        agent = runtime.agent
        _, interactive = _pipeline_interaction_kinds(
            agent, unattended=unattended, mode=mode, boundary=boundary
        )
        conv = ConversationManager()
        conv.add_user_message(prompt, attachments)
        return await _run_pipeline_agent_turn(
            agent, conv, interactive=interactive, on_event=on_event
        )
    finally:
        await runtime.aclose()


@dataclass
class _PipelineConversationBackend:
    runtime: AgentRuntime
    conversation: ConversationManager
    interactive: bool
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _create_pipeline_conversation(
    request: PipelineActionRequest,
) -> _PipelineConversationBackend:
    raw_tools = request.params.get("tools") or []
    tools = [str(item) for item in raw_tools] if isinstance(raw_tools, list) else []
    mode = str(request.params.get("mode") or "react")
    boundary = str(request.params.get("boundary") or "")
    runtime = _build_pipeline_agent(
        model=request.model,
        effort=request.effort,
        allowed_tools=tools,
        allow_computer_use=not request.unattended,
        unattended=request.unattended,
    )
    try:
        _, interactive = _pipeline_interaction_kinds(
            runtime.agent,
            unattended=request.unattended,
            mode=mode,
            boundary=boundary,
        )
    except Exception:
        runtime.close()
        raise
    return _PipelineConversationBackend(
        runtime=runtime,
        conversation=ConversationManager(),
        interactive=interactive,
    )


async def _send_pipeline_conversation(
    backend: _PipelineConversationBackend,
    prompt: str,
    request: PipelineActionRequest,
) -> tuple[str, dict | None]:
    async with backend.lock:
        backend.conversation.add_user_message(prompt, list(request.attachments))
        return await _run_pipeline_agent_turn(
            backend.runtime.agent,
            backend.conversation,
            interactive=backend.interactive,
            on_event=request.emit,
        )


async def _close_pipeline_conversation(
    backend: _PipelineConversationBackend,
) -> None:
    await backend.runtime.aclose()


PIPELINE_CONVERSATIONS = PipelineConversationAction(
    create=_create_pipeline_conversation,
    send=_send_pipeline_conversation,
    close=_close_pipeline_conversation,
)


def _codex_pipeline_model_catalog() -> list[dict[str, Any]]:
    """读取 Codex 本机目录中可在 API 使用的 GPT 5.5/5.6 系列。"""
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    path = codex_home / "models_cache.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []

    catalog: list[dict[str, Any]] = []
    for item in payload.get("models", []):
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("slug") or "")
        if not re.match(r"^gpt-5\.(?:5|6)(?:-|$)", model_id):
            continue
        if item.get("visibility") != "list" or not item.get("supported_in_api"):
            continue
        efforts = [
            str(level.get("effort"))
            for level in item.get("supported_reasoning_levels", [])
            if isinstance(level, dict) and level.get("effort")
        ]
        catalog.append({
            "id": model_id,
            "efforts": efforts,
            "name": str(item.get("display_name") or model_id),
            "description": str(item.get("description") or ""),
            "priority": int(item.get("priority") or 999),
        })
    catalog.sort(key=lambda item: (item["priority"], item["id"]))
    return catalog


async def _pipeline_capabilities() -> dict:
    """Pipeline 能力枚举：模型（含 effort 档位）、工具、技能、执行模式。

    - 模型列表复用聊天侧的 list_models 通道（provider 不支持列表端点或
      拉取失败时回退为当前模型单项）；每个模型的 effort 档位按
      协议 + 模型能力识别（model_effort_levels）；
    - 工具取当前 Agent 的工具注册表；
    - 技能取 SkillLoader 全量扫描（项目 / 用户 / 内置，按名去重）；
    - 模式为固定的三种执行模式。
    """
    session = SESSION
    protocol = ""
    current_model = ""
    if session.slot is not None:
        protocol = session.slot.provider.protocol
        current_model = session.slot.provider.model

    provider_model_ids = await session.list_models() or []
    codex_catalog = _codex_pipeline_model_catalog()
    catalog_by_id = {item["id"]: item for item in codex_catalog}
    modern_gpt = bool(
        current_model
        and re.match(r"^gpt-5\.(?:5|6)(?:-|$)", current_model)
        and codex_catalog
    )
    if modern_gpt:
        # Codex 的 /models 通道有时只回当前模型；本机目录才是完整可选范围。
        model_ids = [item["id"] for item in codex_catalog]
        for model_id in provider_model_ids:
            if (
                re.match(r"^gpt-5\.(?:5|6)(?:-|$)", model_id)
                and model_id not in model_ids
            ):
                model_ids.append(model_id)
    else:
        model_ids = list(provider_model_ids)
    if current_model and current_model not in model_ids:
        model_ids.insert(0, current_model)

    # effort 档位口径与 _build_pipeline_agent 的适用性检查同源：
    # 插槽显式声明的档位（codex config.toml enabled-reasoning-efforts）
    # 仅对插槽自身模型有效且优先（session.effort_levels 即该逻辑），
    # 其余模型按协议 + 模型名识别。否则 auto_policy 依 capabilities
    # 收敛出的档位可能在实际请求前被清空——events.jsonl 里的
    # resolved_effort 与真实发送的参数不符。
    def _efforts_for(m: str) -> list[str]:
        catalog_item = catalog_by_id.get(m)
        if catalog_item is not None and catalog_item["efforts"]:
            return list(catalog_item["efforts"])
        if m == current_model:
            return session.effort_levels()
        return model_effort_levels(
            protocol,
            m,
            catalog_service=MODEL_CAPABILITY_CATALOG,
        )

    models = [
        {
            "id": m,
            "efforts": _efforts_for(m),
            "vision": model_supports_vision(
                protocol,
                m,
                catalog_service=MODEL_CAPABILITY_CATALOG,
            ),
            "name": catalog_by_id.get(m, {}).get("name", m),
            "description": catalog_by_id.get(m, {}).get("description", ""),
        }
        for m in model_ids
    ]

    tools: list[str] = []
    if session.agent is not None:
        tools = [t.name for t in session.agent.registry.list_tools()]

    try:
        loader = session.context.get("skills")
        skills = sorted(loader.load_all().keys())
    except Exception:
        log.exception("Failed to enumerate skills for pipeline capabilities")
        skills = []

    return {
        "models": models,
        "current_model": current_model,
        "tools": tools,
        "skills": skills,
        "modes": ["react", "plan", "go"],
    }


def _install_cordis_inspect_api() -> None:
    @app.get("/api/plugins", tags=["cordis"])
    async def api_plugins() -> dict[str, Any]:
        return {
            "application": APP_HOST.snapshot(),
            "session": SESSION.plugin_host.snapshot(),
        }


def _install_builtin_pipeline_actions(ctx: Context) -> None:
    """Publish built-in creative actions through the same slot as plugins."""

    action = PIPELINE_CONVERSATIONS.action()
    ctx.slots.register(
        PIPELINE_ACTION_SLOT,
        action,
        item_key=action.id,
    )


APP_CONTEXT.register_plugin(PluginSpec(
    "web.pipeline.actions",
    _install_builtin_pipeline_actions,
    requires=("core.runtime",),
    version="1",
    purpose="Built-in stateful conversation action for creative pipelines",
))
APP_CONTEXT.register_plugin(
    fastapi_plugin(
        "web.pipeline",
        app,
        lambda: register_pipeline_api(
            app,
            work_dir=os.getcwd(),
            agent_factory=_pipeline_agent_factory,
            tool_executor=None,
            skill_executor=None,
            action_provider=_pipeline_action_provider,
            capabilities_provider=_pipeline_capabilities,
        ),
        purpose="Pipeline graph execution and trajectory API",
        service_definition=PIPELINE,
    )
)
APP_CONTEXT.register_plugin(
    fastapi_plugin(
        "web.completion",
        app,
        lambda: register_completion_api(
            app,
            work_dir_provider=lambda: SESSION.work_dir,
            user_store_path=(
                Path(os.environ["MEWCODE_COMPLETION_STORE"])
                if os.environ.get("MEWCODE_COMPLETION_STORE") else None
            ),
        ),
        purpose="Async code completion API",
        service_definition=COMPLETION,
    )
)
APP_CONTEXT.register_plugin(
    fastapi_plugin(
        "web.cordis.inspect",
        app,
        lambda: _install_cordis_inspect_api(),
        purpose="Plugin and slot runtime inspection",
    )
)
APP_CONTEXT.load("web.pipeline.actions")
APP_CONTEXT.load("web.pipeline")
APP_CONTEXT.load("web.completion")
APP_CONTEXT.load("web.cordis.inspect")


# ---------------------------------------------------------------------------
# WebSocket（聊天流）
# ---------------------------------------------------------------------------


def _resolve_prepared_response(request, message, answers):
    from mewcode.orchestration.task_design import validate_question_binding
    if getattr(request, "task_design_binding", None) is not None:
        validate_question_binding(*request.task_design_owner, request.task_design_binding)
    """Check the frozen displayed bundle before touching its live Future."""
    bundle = getattr(request, "choice_bundle", None)
    if not bundle or answers is None or message.get("cancelled"):
        return answers
    if message.get("choice_bundle_id") != bundle.get("id") or message.get("choice_version") != bundle.get("version"):
        raise ValueError("候选选项已变化，请刷新当前问题后重新选择")
    signature = getattr(request, 'choice_state_signature', None)
    if callable(signature) and signature() != getattr(request, 'choice_display_signature', None):
        raise ValueError("当前节点代际、输入、授权或问题已变化，请重新准备选项")
    from mewcode.orchestration.interaction_choices import resolve_answers
    choice_inputs = message.get("choice_inputs") or {}
    resolved = resolve_answers(bundle, answers, bundle_id=message["choice_bundle_id"],
        version=message["choice_version"], choice_inputs=choice_inputs)
    request.choice_inputs = copy.deepcopy(choice_inputs)
    if getattr(request, "choice_resolution", "response") == "wait":
        return answers
    if resolved["actions"]:
        raise ValueError("此选项需要先准备新的候选；请使用选项上的处理按钮，原问题已保留")
    # Ordinary planning tools receive their original canonical strings. Stage
    # inputs are resolved by waits instead, where typed lists remain lists.
    return {name: ", ".join(value) if isinstance(value, list) else value
            for name, value in resolved["values"].items()}


def _human_oncall_response_result(
    runtime: ConversationRuntime | None,
    message: dict[str, Any],
) -> dict[str, Any]:
    """Validate one desktop HumanOnCall response and return its correlated ack.

    The browser keeps the card until this result arrives.  Keeping the
    validation and Future transition together prevents a stale window from
    looking successful when the task has already been paused or replaced.
    """

    request_id = str(message.get("request_id") or "")
    conversation_id = str(message.get("conversation_id") or "")
    cancelled = bool(message.get("cancelled"))
    result: dict[str, Any] = {
        "type": "human_oncall_response_result",
        "request_id": request_id,
        "conversation_id": conversation_id,
        "ok": False,
        "cancelled": cancelled,
    }
    request = runtime.human_oncall_request if runtime is not None else None
    future = request.future if request is not None else None
    if request is None or future is None or future.done():
        result.update({
            "error": "当前没有可响应的活动人工选择；请恢复任务后再响应，不会自动恢复任务。",
            "error_type": "no_live_request",
        })
        return result
    if request_id != str(request.request_id):
        result.update({
            "error": "该人工选择已过期，请响应当前显示的选项。",
            "error_type": "stale_request",
        })
        return result

    raw_answers = None if cancelled else message.get("answers")
    try:
        validation_answers = raw_answers
        if getattr(request, 'choice_bundle', None) and isinstance(raw_answers, dict):
            validation_answers = {name: (', '.join(value['custom']) if isinstance(value.get('custom'), list)
                else value['custom']) if isinstance(value, dict) and set(value) == {'custom'} else value
                for name, value in raw_answers.items()}
        answers = validate_human_answers(request.questions, validation_answers)
    except ValueError as exc:
        result.update({"error": str(exc), "error_type": "invalid_answer"})
        return result
    try:
        notes = {} if cancelled else validate_answer_notes(
            request.questions, answers, message.get("notes")
        )
        answers = _resolve_prepared_response(request, message,
            raw_answers if getattr(request, 'choice_bundle', None) else answers)
    except ValueError as exc:
        result.update({"error": str(exc), "error_type": "invalid_notes"})
        return result
    try:
        request.answer_notes = notes
        future.set_result(answers)
    except (asyncio.InvalidStateError, RuntimeError):
        # A task can finish between done() and set_result().  Treat that race
        # as an expired response; never claim that it was accepted.
        result.update({
            "error": "当前人工选择已结束，请恢复任务后再响应。",
            "error_type": "no_live_request",
        })
        return result
    result["ok"] = True
    return result


def _should_route_expert_resume(
    text: str, *, focus: dict[str, Any],
) -> bool:
    """A mode preference or a historical checkpoint cannot claim new chat."""
    return requests_orchestration_resume(text, focus)


def _focused_resume_checkpoint(conversation_id: str, focus: dict) -> dict | None:
    if focus.get("kind") == "orchestration" and focus.get("run_id"):
        checkpoint = EXPERT_RUNS.load(str(focus["run_id"]))
        # Never substitute a different run when the focused one has ended.
        return checkpoint if checkpoint and checkpoint.get("conversation_id") == conversation_id and is_resumable_snapshot(checkpoint) else None
    candidates = [run for run in EXPERT_RUNS.list_for_conversation(conversation_id)
                  if is_resumable_snapshot(run)]
    if len(candidates) > 1:
        raise ValueError("当前对话有多个可恢复编排，请在对应任务卡上点击「继续」，以明确恢复哪一次任务。")
    return candidates[0] if candidates else None


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    if DESKTOP_TOKEN:
        cookie = ws.cookies.get(DESKTOP_COOKIE, "")
        if not cookie or not secrets.compare_digest(cookie, DESKTOP_TOKEN):
            await ws.close(code=4401, reason="desktop session token required")
            return
    from frontend.startup_recovery import phase
    if phase(app) != 'ready':
        await ws.close(code=1013, reason="saved task recovery is not ready")
        return
    await ws.accept()
    WS_EVENTS.connect(ws)
    notifier = globals().get("WORKBENCH_NOTIFIER")
    if notifier is not None:
        notifier.attach_loop()
    session = SESSION

    async def send_state() -> None:
        await ws.send_json({
            "type": "state",
            **session.client_state_payload(),
        })

    async def notification_loop() -> None:
        while True:
            await asyncio.sleep(1)
            if session.chat_busy() or session.agent is None:
                continue
            completed = session.collect_task_notifications()
            if not completed:
                continue
            names = "、".join(task.name for task in completed)
            await ws.send_json({
                "type": "note",
                "text": f"后台任务已完成，正在汇总: {names}",
            })
            runtime = session.capture_conversation_runtime()
            session.begin_conversation_turn(runtime)
            scoped_ws = ConversationWebSocket(WS_EVENTS, runtime.conversation_id)
            try:
                task = asyncio.create_task(
                    run_chat(
                        scoped_ws, "", record_user=False, runtime=runtime
                    )
                )
                session.attach_conversation_task(runtime, task)
            except BaseException:
                runtime.starting = False
                raise

    notification_task = None

    try:
        await send_state()
        for prompt in pending_runtime_prompts(session):
            await ws.send_json(prompt)
        notification_task = asyncio.create_task(notification_loop())
        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")

            if mtype == "command":
                conversation_id = str(msg.get("conversation_id") or "")
                if not conversation_id and session.session is not None:
                    conversation_id = session.session.session_id
                result = session.execute_web_command_for(
                    conversation_id,
                    str(msg.get("name") or ""),
                    str(msg.get("args") or ""),
                )
                runtime = session.runtime_for(conversation_id)
                prompt = str(result.get("prompt") or "")
                if result.get("ok") and prompt and runtime is not None:
                    runtime.queued_prompts.append(prompt)
                    result = {**result, "prompt": "", "queued": True}
                await WS_EVENTS.send_json({
                    "type": "command_result",
                    "conversation_id": conversation_id,
                    **result,
                })
                await send_state()

            elif mtype == "chat":
                text = (msg.get("text") or "").strip()
                _, continuation_supplemental = parse_resume_message(text)
                raw_attachment_ids = msg.get("attachments") or []
                if not isinstance(raw_attachment_ids, list):
                    raw_attachment_ids = []
                attachment_ids = [str(value) for value in raw_attachment_ids]
                if not text and not attachment_ids:
                    continue
                active_id = str(msg.get("conversation_id") or "")
                visible_id = session.session.session_id if session.session else ""
                if active_id and active_id != visible_id:
                    scoped_ws = ConversationWebSocket(WS_EVENTS, active_id)
                    await scoped_ws.send_json({
                        "type": "error",
                        "text": "目标对话不是当前活动对话，请切换后重试。",
                    })
                    # The renderer marks the turn busy before sending the chat
                    # frame.  Every rejection must therefore be terminal, or
                    # the composer remains locked forever waiting for `done`.
                    await scoped_ws.send_json({
                        "type": "done",
                        "interrupted": True,
                        "rejected": True,
                        "elapsed": 0,
                        "tasks": session.tasks_payload(),
                    })
                    continue
                active_id = active_id or visible_id
                if 'continuation_task_id' not in msg:
                    from frontend.workflow_recovery import handle_chat as recover_workflow_chat
                    try:
                        recovered = await recover_workflow_chat(sys.modules[__name__], cid=active_id,
                            text=text, request_id=str(msg.get('request_id') or ''),
                            context=msg.get('workbench_context'), attachments=attachment_ids)
                    except (ValueError, LookupError, RuntimeError, OSError) as exc:
                        scoped_ws = ConversationWebSocket(WS_EVENTS, active_id)
                        busy = session.chat_busy(active_id)
                        await scoped_ws.send_json({'type': 'note' if busy else 'error', 'text': str(exc)})
                        if not busy:
                            await scoped_ws.send_json({'type': 'done', 'interrupted': False, 'elapsed': 0,
                                                       'rejected': True})
                        continue
                    if recovered is not None:
                        continue
                if parse_maintenance_command(text) is not None:
                    try:
                        result = _maintenance_chat_result(active_id, text, attachment_ids)
                    except ValueError as exc:
                        result = {"ok": False, "message": str(exc)}
                    await WS_EVENTS.send_json({"type": "command_result", "conversation_id": active_id, **result})
                    await WS_EVENTS.send_json({"type": "done", "conversation_id": active_id,
                        "maintenance_only": True, "interrupted": False, "elapsed": 0})
                    continue
                continuation_task_id = msg.get("continuation_task_id")
                if "continuation_task_id" in msg:
                    try:
                        if not msg.get("conversation_id") or session.chat_busy(active_id):
                            raise ValueError("续接需要明确的当前对话，且须等待当前执行结束。")
                        validate_chat_continuation(session, continuation_task_id)
                    except ValueError as exc:
                        scoped_ws = ConversationWebSocket(WS_EVENTS, active_id)
                        await scoped_ws.send_json({"type": "error", "text": str(exc)})
                        await scoped_ws.send_json({"type": "done", "rejected": True,
                                                   "interrupted": False, "elapsed": 0})
                        continue
                if continuation_task_id is None and not attachment_ids and not session.chat_busy(active_id):
                    focus = dialogue_focus(session)
                    if _should_route_expert_resume(
                        text, focus=focus,
                    ):
                        scoped_ws = ConversationWebSocket(WS_EVENTS, active_id)
                        try:
                            paused = _focused_resume_checkpoint(active_id, focus)
                        except ValueError as exc:
                            await scoped_ws.send_json({"type": "error", "text": str(exc)})
                            await scoped_ws.send_json({"type": "done", "interrupted": False, "rejected": True, "elapsed": 0})
                            continue
                        if session.chat_busy(active_id):
                            await scoped_ws.send_json({"type": "error", "text": "当前对话仍有任务运行，请先暂停或等待完成"})
                            await scoped_ws.send_json({"type": "done", "interrupted": False, "elapsed": 0,
                                                       "status": session.status_payload(), "tasks": session.tasks_payload()})
                            continue
                        if paused is None:
                            await scoped_ws.send_json({
                                "type": "error", "text": "当前任务没有可恢复的编排检查点；如需恢复其他编排，请从对应任务卡选择。"
                            })
                            await scoped_ws.send_json({
                                "type": "done", "interrupted": False, "elapsed": 0,
                                "status": session.status_payload(),
                                "tasks": session.tasks_payload(),
                            })
                            continue
                        runtime = session.capture_conversation_runtime()
                        runtime.execution_kind = "orchestration"
                        session.begin_conversation_turn(runtime)
                        try:
                            await scoped_ws.send_json({
                                "type": "turn_started", "text": "",
                                "attachments": [], "queued": False,
                                "expert_mode": True, "client_echoed": True,
                                "resume_run_id": paused.get("run_id"),
                            })
                            task = asyncio.create_task(_resume_expert_after_paused_dialogue(
                                scoped_ws, runtime, paused,
                                supplemental=continuation_supplemental or text,
                            ))
                            session.attach_conversation_task(runtime, task)
                        except BaseException:
                            runtime.starting = False
                            raise
                        continue
                if session.chat_busy(active_id):
                    scoped_ws = ConversationWebSocket(WS_EVENTS, active_id)
                    runtime = session.runtime_for(active_id)
                    if runtime is not None and runtime.execution_kind == "chat" and continuation_state(runtime).get("status") in {"running", "reviewing", "intervening"} and text.strip() and not attachment_ids:
                        try:
                            result = await _accept_chat_intervention(runtime, "chat-" + secrets.token_hex(16), text)
                            await scoped_ws.send_json({"type": "note", "text": result["message"]})
                        except (LookupError, RuntimeError, ValueError) as exc:
                            await scoped_ws.send_json({"type": "note", "text": str(exc)})
                        continue
                    if runtime is not None and runtime.execution_kind == "orchestration" and text.strip() and not attachment_ids:
                        guide = _record_authorization_intent(runtime, text)
                        try:
                            result = await _accept_expert_intervention(runtime, "chat-" + secrets.token_hex(16), text)
                            await scoped_ws.send_json({"type": "note", "text": result["message"] + guide})
                        except (LookupError, RuntimeError, ValueError) as exc:
                            await scoped_ws.send_json({"type": "note", "text": str(exc)})
                        continue
                    await scoped_ws.send_json({
                        "type": "error",
                        "text": (
                            "正在响应中，请先点击「暂停任务」或等待完成"
                            if runtime is not None and runtime.execution_kind == "orchestration"
                            else "正在响应中，请先点击「打断」或等待完成"
                        ),
                    })
                    # 补发 done，让前端复位 streaming 状态（前端在发送前
                    # 已置 streaming=true，没有 done 会永久卡死发送按钮）
                    await scoped_ws.send_json({
                        "type": "done", "interrupted": False, "error": None,
                        "elapsed": 0, "status": session.status_payload(),
                        "tasks": session.tasks_payload(),
                    })
                    continue
                if session.agent is None:
                    await ws.send_json({
                        "type": "error",
                        "text": (
                            session.provider_restore_error
                            or "Provider 未就绪，请先在左上角选择可用插槽"
                        ),
                    })
                    await ws.send_json({
                        "type": "done", "interrupted": False, "error": None,
                        "elapsed": 0, "status": session.status_payload(),
                        "tasks": session.tasks_payload(),
                    })
                    continue
                # 活动会话可能刚被删除（session 置空）：发送前惰性创建
                if session.session is None:
                    session.new_conversation(session.scope)
                assert session.session is not None
                try:
                    attachment_refs = ATTACHMENTS.references(
                        session.session.session_id, attachment_ids
                    )
                    has_images = any(
                        block.get("kind") == "image" for block in attachment_refs
                    )
                    if has_images:
                        provider = session.slot.provider if session.slot else None
                        if provider is None or not model_supports_vision(
                            provider.protocol,
                            provider.model,
                            catalog_service=MODEL_CAPABILITY_CATALOG,
                        ):
                            raise AttachmentError(
                                "当前模型不支持图片输入，请切换到带 Vision 标记的模型"
                            )
                    content_blocks = ATTACHMENTS.materialize(attachment_refs)
                except AttachmentError as exc:
                    await ws.send_json({"type": "error", "text": str(exc)})
                    await ws.send_json({
                        "type": "done", "interrupted": False, "error": None,
                        "elapsed": 0, "status": session.status_payload(),
                        "tasks": session.tasks_payload(),
                    })
                    continue
                runtime = session.capture_conversation_runtime()
                scoped_ws = ConversationWebSocket(WS_EVENTS, runtime.conversation_id)
                runtime.execution_kind = "chat"
                session.begin_conversation_turn(runtime)
                try:
                    await scoped_ws.send_json({
                        "type": "turn_started",
                        "text": text,
                        "attachments": ATTACHMENTS.display_refs(attachment_refs),
                        "queued": False,
                        "expert_mode": False,
                        "client_echoed": True,
                    })
                    task = asyncio.create_task(run_chat(
                        scoped_ws, text, content_blocks=content_blocks,
                        runtime=runtime,
                        continuation_task_id=continuation_task_id,
                    ))
                    session.attach_conversation_task(runtime, task)
                except BaseException:
                    runtime.starting = False
                    raise

            elif mtype == "interrupt":
                await request_chat_interrupt(
                    str(msg.get("conversation_id") or ""),
                    action="pause",
                )

            elif mtype == "expert_terminate":
                run_id = str(msg.get("run_id") or "")
                runtime = session.runtime_for(str(msg.get("conversation_id") or ""))
                if runtime is not None and runtime.expert_run_id == run_id and runtime.task:
                    runtime.expert_control_action = "terminate"
                    runtime.task.cancel()
                else:
                    snapshot = EXPERT_RUNS.load(run_id)
                    if snapshot is None:
                        await ws.send_json({"type": "error", "text": "专家任务不存在"})
                    elif snapshot.get("status") in {"paused", "blocked"}:
                        EXPERT_RUNS.transition(run_id, "terminated", reason="用户终止")
                        await ws.send_json({
                            "type": "expert_run_state", "run_id": run_id,
                            "status": "terminated",
                        })

            elif mtype in {"expert_resume", "expert_retry"}:
                await dispatch_expert_resume(session, ws, msg)

            elif mtype == "handoff_review_expand":
                from mewcode.expert_runs import RUN_ID_PATTERN
                from mewcode.orchestration.paused_handoff_ui import read_authorized_handoff_excerpt

                run_id = str(msg.get("run_id") or "")
                conversation_id = str(msg.get("conversation_id") or "")
                visible_id = session.session.session_id if session.session else ""
                snapshot = EXPERT_RUNS.load(run_id) if RUN_ID_PATTERN.fullmatch(run_id) else None
                if not snapshot or conversation_id != visible_id:
                    await ws.send_json({"type": "error", "text": "当前对话没有可展开的接力审阅来源。"})
                    continue
                try:
                    view = read_authorized_handoff_excerpt(
                        store_root=EXPERT_RUNS.root, snapshot=snapshot,
                        run_id=run_id, conversation_id=conversation_id,
                        reference_id=str(msg.get("reference_id") or ""),
                        view_offset=int(msg.get("view_offset") or 0),
                        source_id=str(msg.get("source_id") or ""),
                    )
                except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError, PermissionError):
                    await ws.send_json({"type": "error", "text": "接力原文无法安全回读；请刷新任务状态。"})
                    continue
                await ws.send_json({"type": "handoff_review_excerpt", "run_id": run_id,
                                    "conversation_id": conversation_id,
                                    "view": view})

            elif mtype == "handoff_review_worklist":
                from mewcode.expert_runs import RUN_ID_PATTERN
                from mewcode.orchestration.paused_handoff_ui import (
                    read_authorized_handoff_worklist,
                )

                run_id = str(msg.get("run_id") or "")
                conversation_id = str(msg.get("conversation_id") or "")
                visible_id = session.session.session_id if session.session else ""
                snapshot = EXPERT_RUNS.load(run_id) if RUN_ID_PATTERN.fullmatch(run_id) else None
                if not snapshot or conversation_id != visible_id:
                    await ws.send_json({"type": "error", "text": "当前对话没有可读取的对账清单。"})
                    continue
                try:
                    page = read_authorized_handoff_worklist(
                        store_root=EXPERT_RUNS.root, snapshot=snapshot,
                        run_id=run_id, conversation_id=conversation_id,
                        offset=msg.get("offset", 0),
                        work_dir=session.work_dir,
                        source_id=str(msg.get("source_id") or ""),
                    )
                except (OSError, ValueError, KeyError, TypeError, PermissionError):
                    await ws.send_json({"type": "error", "text": "对账来源无法安全分页读取；请刷新任务状态。"})
                    continue
                await ws.send_json({"type": "handoff_review_worklist", "run_id": run_id,
                                    "conversation_id": conversation_id,
                                    "page": page})

            elif mtype in {"handoff_review_authorize", "handoff_review_escalate",
                           "handoff_review_reconcile_page"}:
                await ws.send_json({"type": "error", "error_code": "retired_paused_handoff_review",
                    "text": "旧暂停接力分段审阅已停用。请使用“继续任务”进入当前宿主恢复检查；历史来源可只读回看。"})

            elif mtype == "select_provider":
                err = await session.use_slot(msg.get("key", ""))
                if err:
                    await ws.send_json({"type": "error", "text": f"切换失败: {err}"})
                await send_state()

            elif mtype == "reload_providers":
                session.scan_slots()
                await send_state()

            elif mtype == "set_effort":
                err = session.set_effort(msg.get("level", ""))
                if err:
                    await ws.send_json({"type": "error", "text": err})
                await send_state()

            elif mtype == "switch_model":
                err = session.switch_model(msg.get("model", ""))
                if err:
                    await ws.send_json({"type": "error", "text": f"切换失败: {err}"})
                await send_state()

            elif mtype == "select_model_ref":
                err = await session.use_model_ref(
                    str(msg.get("provider_key") or ""),
                    str(msg.get("model_id") or ""),
                    str(msg.get("effort") or ""),
                )
                if err:
                    await ws.send_json({"type": "error", "text": f"切换失败: {err}"})
                await send_state()

            elif mtype == "list_models":
                models = await session.list_models()
                await ws.send_json({"type": "models", "models": models or []})

            elif mtype == "set_permission_mode":
                err = session.set_permission_mode(msg.get("mode", ""))
                if err:
                    await ws.send_json({"type": "error", "text": err})
                await send_state()

            elif mtype == "set_context_tier":
                err = session.set_context_tier(_parse_context_tier(msg.get("window")))
                if err:
                    await ws.send_json({"type": "error", "text": err})
                await send_state()

            elif mtype == "set_multi_agent":
                session.set_multi_agent(bool(msg.get("on", False)))
                runtime = session.runtime_for(str(msg.get("conversation_id") or ""))
                if runtime is not None and runtime.expert_run_id:
                    await ws.send_json({
                        "type": "note",
                        "conversation_id": runtime.conversation_id,
                        "text": "多 Agent 设置将在下次接力或新专家任务生效；当前 attempt 拓扑不变。",
                    })
                await send_state()

            elif mtype == "set_expert_mode":
                err = session.set_expert_mode(bool(msg.get("on", False)))
                if err:
                    await ws.send_json({"type": "error", "text": err})
                await send_state()

            elif mtype == "set_orchestration_planning":
                err = session.set_orchestration_planning(bool(msg.get("on", False)))
                if err:
                    await ws.send_json({"type": "error", "text": err})
                await send_state()

            elif mtype in {"permission_response", "human_oncall_response", "ask_user_response", "plan_revision_response"}:
                from frontend.prompt_dispatch import dispatch, PromptError
                kind = mtype.removesuffix('_response')
                try:
                    result = await dispatch(sys.modules[__name__], kind, msg)
                    if kind == 'human_oncall':
                        await ws.send_json(result)
                    elif kind == 'plan_revision' and result.get('status') == 'rejected':
                        await ws.send_json({'type': 'expert_run_state', 'run_id': result['run_id'],
                            'conversation_id': msg.get('conversation_id'), 'status': 'blocked', 'reason': '修订未采用，已保留现场'})
                except PromptError as exc:
                    if kind == 'human_oncall':
                        await ws.send_json({'type': 'human_oncall_response_result', 'request_id': msg.get('request_id'),
                            'conversation_id': msg.get('conversation_id'), 'ok': False, 'error': str(exc), 'error_type': exc.code})
                    else:
                        await ws.send_json({'type': 'error', 'text': str(exc), 'error_type': exc.code})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.exception("ws error: %s", e)
    finally:
        WS_EVENTS.disconnect(ws)
        if notification_task is not None:
            notification_task.cancel()
            with suppress(asyncio.CancelledError):
                await notification_task


def main() -> None:
    parser = argparse.ArgumentParser(description="MewCode local web/desktop backend")
    parser.add_argument(
        "--usage-plugin-worker",
        metavar="ENTRYPOINT",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.usage_plugin_worker:
        raise SystemExit(usage_plugin_worker_main([args.usage_plugin_worker]))
    url = f"http://{args.host}:{args.port}"
    os.environ["MEWCODE_BACKEND_HOST"] = args.host
    os.environ["MEWCODE_BACKEND_PORT"] = str(args.port)
    print(
        "MEWCODE_BACKEND_READY "
        + json.dumps({"url": url, "host": args.host, "port": args.port}),
        flush=True,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


from frontend.orch_activity import install as _install_orch_activity
_install_orch_activity(app, sys.modules[__name__])
from frontend.orch_drafts import install as _install_orch_drafts
_install_orch_drafts(app, sys.modules[__name__])
from frontend.orch_maintenance import install as _install_orch_maintenance
_install_orch_maintenance(app, sys.modules[__name__])
from frontend.interaction_choices import install as _install_interaction_choices
_install_interaction_choices(app, sys.modules[__name__])
from frontend.task_design import install as _install_task_design
_install_task_design(app, sys.modules[__name__])
from frontend.workbench_api import install as _install_workbench
_install_workbench(app, sys.modules[__name__])
from frontend.file_references import install as _install_file_references
_install_file_references(app, sys.modules[__name__])

from frontend.startup_recovery import install as _install_startup_recovery
_install_startup_recovery(app)
from frontend.fast_mode import install as _install_fast_mode
_install_fast_mode(app, sys.modules[__name__])

if __name__ == "__main__":
    main()
