from __future__ import annotations

import asyncio
import logging
import os
import random
import time as _time
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from mewcode.askuser_dialog import InlineAskUserWidget
    from mewcode.human_oncall_dialog import InlineHumanOnCallWidget
    from mewcode.permission_dialog import InlinePermissionWidget
    from mewcode.plan_dialog import InlinePlanWidget

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message as TMessage
from textual.widgets import Markdown, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from mewcode.agent import (
    Agent,
    CompactNotification,
    ErrorEvent,
    HookEvent,
    HumanOnCallRequest,
    LoopComplete,
    PermissionRequest,
    PermissionResponse,
    RetryEvent,
    StreamText,
    ThinkingText,
    ToolResultEvent,
    ToolUseEvent,
    TurnComplete,
    UsageEvent,
)
from mewcode.auxiliary import create_auxiliary_client
from mewcode.client import (
    EFFORT_LEVELS,
    AuthenticationError,
    LLMClient,
    LLMError,
    create_client,
    model_effort_levels,
    resolve_context_window,
    scheduled_client_stream,
)
from mewcode.commands import (
    CommandContext,
    CommandRegistry,
    complete,
    parse_command,
)
from mewcode.commands.completion import CompletionPopup
from mewcode.config import MCPServerConfig, ProviderConfig, RedisConfig
from mewcode.provider_sources import (
    ProviderSlot,
    discover_provider_slots,
    validate_slot,
)
from mewcode.opencode_go import (
    is_opencode_go_provider,
    opencode_go_route,
    ordered_go_model_ids,
    resolve_effective_provider_config,
)
from mewcode.hooks import HookContext, HookEngine, load_hooks
from mewcode.conversation import ConversationManager, Message
from mewcode.mcp import MCPManager
from mewcode.memory import (
    MemoryManager,
    Session,
    SessionManager,
    find_relevant_memories,
    generate_session_summary,
    load_instructions,
    make_compact_boundary,
    render_reminder,
)
from mewcode.memory.redis_cache import RedisContextCache
from mewcode.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from mewcode.permissions.modes import PUBLIC_PERMISSION_MODES, PERMISSION_MODE_LABELS, public_permission_mode
from mewcode.agents.loader import AgentLoader
from mewcode.agents.task_manager import TaskManager
from mewcode.agents.trace import TraceManager
from mewcode.agents.notification import inject_task_notifications
from mewcode.commands.handlers.tasks import create_tasks_command
from mewcode.skills.executor import SkillExecutor
from mewcode.skills.loader import SkillLoader
from mewcode.skills.runtime import attach_skills
from mewcode.commands.handlers.skill_register import build_skill_commands
from rich.text import Text as RichText
from textual.theme import Theme
from mewcode.cache import FileCache
from mewcode.tools import ToolRegistry
from mewcode.tools.agent_tool import AgentTool
from mewcode.tools.ask_user import AskUserEvent, AskUserTool
from mewcode.tools.human_oncall import validate_human_answers
from mewcode.tools.impl.tool_search import ToolSearchTool
from mewcode.tools.load_skill import LoadSkill
from mewcode.worktree.cleanup import start_stale_cleanup_task
from mewcode.worktree.manager import WorktreeManager
from mewcode.commands.handlers.worktree import create_worktree_command
from mewcode.teammate_tree import TeammateTree
from mewcode.usage.service import UsageService
from mewcode.plugins import ApplicationHost
from mewcode.cordis import Context, PluginSpec
from mewcode.plugins.features import (
    MultiAgentOptions,
    multi_agent_plugin,
)
from mewcode.plugins.runtime import AgentRuntime, AgentRuntimeRequest
from mewcode.plugins.services import (
    AGENT_FACTORY,
    CLIENT_FACTORY,
    CONVERSATION_FACTORY,
    COMMAND_SLOT,
    SESSIONS,
    TASK_FACTORY,
    TASKS,
    TEAMS,
    TRACES,
    TRACE_FACTORY,
    USAGE,
    WORKTREES,
)

import re

MAX_TRUNCATED_LINES = 20
MAX_AT_REF_BYTES = 10240

_AT_REF_RE = re.compile(r"@([\w./_\-]+(?:\.[\w]+)*)")

_SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".mewcode", "build", ".gradle"}


def scan_files_for_at(prefix: str, work_dir: str, limit: int = 10) -> list[str]:
    matches: list[str] = []
    base = os.path.join(work_dir, os.path.dirname(prefix)) if "/" in prefix else work_dir
    name_prefix = os.path.basename(prefix).lower()
    if not os.path.isdir(base):
        return matches
    try:
        for entry in sorted(os.listdir(base)):
            if entry in _SKIP_DIRS or entry.startswith("."):
                continue
            if entry.lower().startswith(name_prefix):
                rel = os.path.join(os.path.dirname(prefix), entry) if "/" in prefix else entry
                if os.path.isdir(os.path.join(base, entry)):
                    rel += "/"
                matches.append(rel)
                if len(matches) >= limit:
                    break
    except OSError:
        pass
    return matches


def expand_at_refs(text: str, work_dir: str) -> str:
    def _replace(m: re.Match) -> str:
        rel_path = m.group(1)
        full_path = os.path.join(work_dir, rel_path)
        if not os.path.isfile(full_path):
            return m.group(0)
        try:
            content = open(full_path, encoding="utf-8", errors="replace").read(MAX_AT_REF_BYTES)
            return f"[File: {rel_path}]\n```\n{content}\n```"
        except Exception:
            return m.group(0)
    return _AT_REF_RE.sub(_replace, text)


class ChatInput(TextArea):
    BINDINGS = [
        Binding("enter", "submit", "Submit", priority=True),
        Binding("shift+enter", "newline", "Newline", priority=True),
        Binding("ctrl+j", "newline", "Newline", priority=True),
        Binding("tab", "complete", "Complete", priority=True),
        Binding("escape", "dismiss_popup", "Dismiss", priority=True),
        Binding("up", "nav_up", "Navigate up", priority=True),
        Binding("down", "nav_down", "Navigate down", priority=True),
    ]

    class Submitted(TMessage):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class TabComplete(TMessage):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.cursor_blink = False
        self._history: list[str] = []
        self._history_index: int = -1
        self._history_draft: str = ""
        self._history_file: Path | None = None

    def load_history(self, work_dir: str) -> None:
        self._history_file = Path(work_dir) / ".mewcode" / "history"
        if self._history_file.exists():
            try:
                lines = self._history_file.read_text(encoding="utf-8").splitlines()
                self._history = [l for l in lines if l.strip()]
            except Exception:
                pass

    def _persist_entry(self, text: str) -> None:
        if self._history_file is None:
            return
        try:
            self._history_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._history_file, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass

    def _popup(self) -> CompletionPopup | None:
        try:
            return self.app.query_one(CompletionPopup)
        except Exception:
            return None

    def action_submit(self) -> None:
        popup = self._popup()
        if popup is not None and popup.is_visible:
            selected = popup.get_selected()
            popup.hide()
            if selected:
                self._history.append(selected)
                self._persist_entry(selected)
                self._history_index = -1
                self._history_draft = ""
                self.post_message(self.Submitted(selected))
                self.clear()
                return
        text = self.text.strip()
        if text:
            self._history.append(text)
            self._persist_entry(text)
            self._history_index = -1
            self._history_draft = ""
            self.post_message(self.Submitted(text))
            self.clear()

    def action_newline(self) -> None:
        self.insert("\n")

    def action_complete(self) -> None:
        popup = self._popup()
        if popup is not None and popup.is_visible:
            selected = popup.get_selected()
            if selected:
                popup.hide()
                self.clear()
                self.insert(selected + " ")
            return
        text = self.text.strip()
        if text.startswith("/"):
            self.post_message(self.TabComplete(text))
        else:
            self.insert("\t")

    def action_dismiss_popup(self) -> None:
        popup = self._popup()
        if popup is not None:
            popup.hide()

    def action_nav_up(self) -> None:
        popup = self._popup()
        if popup is not None and popup.is_visible:
            popup.move_up()
            return
        if not self._history:
            return
        if self._history_index == -1:
            self._history_draft = self.text
            self._history_index = len(self._history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        else:
            return
        self.clear()
        self.insert(self._history[self._history_index])

    def action_nav_down(self) -> None:
        popup = self._popup()
        if popup is not None and popup.is_visible:
            popup.move_down()
            return
        if self._history_index == -1:
            return
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self.clear()
            self.insert(self._history[self._history_index])
        else:
            self._history_index = -1
            self.clear()
            self.insert(self._history_draft)

    class AtFileRequest(TMessage):
        def __init__(self, prefix: str) -> None:
            super().__init__()
            self.prefix = prefix

    class SlashMenuUpdate(TMessage):
        def __init__(self, prefix: str | None) -> None:
            super().__init__()
            self.prefix = prefix

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        text = self.text
        if text.startswith("/"):
            prefix = text[1:]
            if " " not in prefix and "\n" not in prefix:
                self.post_message(self.SlashMenuUpdate(prefix))
            else:
                self.post_message(self.SlashMenuUpdate(None))
        else:
            self.post_message(self.SlashMenuUpdate(None))

        at_idx = text.rfind("@")
        if at_idx < 0:
            return
        after = text[at_idx + 1:]
        if " " in after or "\n" in after:
            return
        if after:
            self.post_message(self.AtFileRequest(after))


COLLAPSIBLE_TOOLS = {"ReadFile", "Glob", "Grep", "ToolSearch"}


def _fmt_duration(secs: float) -> str:
    """把秒数格式化为紧凑的可读时长：45s / 3m12s / 1h05m。"""
    secs = max(int(secs), 0)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60:02d}s"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


def _is_subagent_tool(tool_name: str) -> bool:
    return tool_name == "Agent"


def _tool_title(tool_name: str, arguments: dict[str, Any]) -> str:
    if tool_name == "ReadFile":
        path = os.path.basename(arguments.get("file_path", ""))
        return f"Read {path}" if path else "Read"
    if tool_name == "WriteFile":
        path = os.path.basename(arguments.get("file_path", ""))
        content = arguments.get("content", "")
        lines = content.count("\n") + 1 if content else 0
        return f"Write {path} ({lines} lines)" if path else "Write"
    if tool_name == "EditFile":
        path = os.path.basename(arguments.get("file_path", ""))
        return f"Edit {path}" if path else "Edit"
    if tool_name == "Bash":
        cmd = arguments.get("command", "")
        short = cmd[:50] + "…" if len(cmd) > 50 else cmd
        return f"Bash: {short}" if short else "Bash"
    if tool_name == "Glob":
        return f"Glob: {arguments.get('pattern', '')}"
    if tool_name == "Grep":
        return f"Grep: {arguments.get('pattern', '')}"
    return tool_name


def _format_detail(tool_name: str, arguments: dict[str, Any], output: str) -> str:
    parts: list[str] = []

    if tool_name == "Bash":
        parts.append(f"  IN   {arguments.get('command', '')}")
        parts.append("")
        for line in output.splitlines():
            parts.append(f"  OUT  {line}")
    elif tool_name in ("ReadFile", "WriteFile", "EditFile"):
        parts.append(f"  {arguments.get('file_path', '')}")
        parts.append("")
        for line in output.splitlines()[:MAX_TRUNCATED_LINES]:
            parts.append(f"  {line}")
        total = output.count("\n") + 1
        if total > MAX_TRUNCATED_LINES:
            parts.append(f"  … ({total - MAX_TRUNCATED_LINES} more lines)")
    else:
        for line in output.splitlines()[:MAX_TRUNCATED_LINES]:
            parts.append(f"  {line}")
        total = output.count("\n") + 1
        if total > MAX_TRUNCATED_LINES:
            parts.append(f"  … ({total - MAX_TRUNCATED_LINES} more lines)")

    return "\n".join(parts)


class ToolCallBlock(Static, can_focus=True):

    def __init__(self, tool_name: str, arguments: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.tool_name = tool_name
        self._arguments = arguments
        self._title = _tool_title(tool_name, arguments)
        self._full_output = ""
        self._is_error = False
        self._elapsed = 0.0
        self._collapsed = True
        self._loading = True
        self._render_loading()

    def _render_loading(self) -> None:
        self.update(f"  ● {self._title} …")
        self.add_class("tool-block-loading")

    def set_result(self, output: str, is_error: bool, elapsed: float) -> None:
        self._full_output = output
        self._is_error = is_error
        self._elapsed = elapsed
        self._loading = False
        self._collapsed = True
        self.remove_class("tool-block-loading")
        if is_error:
            self.add_class("tool-block-error")
        self._render_collapsed()

    def _render_collapsed(self) -> None:
        if self._is_error:
            self.update(f"  ✗ {self._title} ({self._elapsed:.1f}s)")
        else:
            self.update(f"  ✓ {self._title} ({self._elapsed:.1f}s)")

    def _render_expanded(self) -> None:
        if self._is_error:
            header = f"  ✗ {self._title} ({self._elapsed:.1f}s)"
        else:
            header = f"  ✓ {self._title} ({self._elapsed:.1f}s)"
        detail = _format_detail(self.tool_name, self._arguments, self._full_output)
        self.update(f"{header}\n{detail}")

    def on_click(self) -> None:
        if self._loading:
            return
        self._collapsed = not self._collapsed
        if self._collapsed:
            self._render_collapsed()
        else:
            self._render_expanded()


_MODE_CYCLE = list(PUBLIC_PERMISSION_MODES)

_MODE_COLORS = {
    PermissionMode.DEFAULT: "dim",
    PermissionMode.PLAN: "yellow",
    PermissionMode.AUTO: "cyan",
    PermissionMode.BYPASS: "red",
}

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _to_past_tense(verb: str) -> str:
    """把现在进行时动词转换为过去式。"""
    if verb.endswith("ing"):
        stem = verb[:-3]
        if stem.endswith("e"):
            return stem + "d"
        if stem and stem[-1] in "atutitet":
            return stem + "ed"
        return stem + "ed"
    return verb + "ed"


THINKING_VERBS = [
    "Accomplishing", "Architecting", "Baking", "Beboppin'", "Befuddling",
    "Bloviating", "Boogieing", "Boondoggling", "Bootstrapping", "Brewing",
    "Calculating", "Canoodling", "Caramelizing", "Cascading", "Cerebrating",
    "Choreographing", "Churning", "Coalescing", "Cogitating", "Combobulating",
    "Composing", "Computing", "Concocting", "Considering", "Contemplating",
    "Cooking", "Crafting", "Creating", "Crunching", "Crystallizing",
    "Cultivating", "Deciphering", "Deliberating", "Dilly-dallying",
    "Discombobulating", "Doodling", "Elucidating", "Enchanting", "Envisioning",
    "Fermenting", "Finagling", "Flambéing", "Flibbertigibbeting", "Flummoxing",
    "Forging", "Frolicking", "Gallivanting", "Garnishing", "Generating",
    "Germinating", "Grooving", "Harmonizing", "Hatching", "Honking",
    "Hullaballooing", "Ideating", "Imagining", "Improvising", "Incubating",
    "Inferring", "Infusing", "Kneading", "Lollygagging", "Manifesting",
    "Marinating", "Meandering", "Metamorphosing", "Mewing", "Moonwalking",
    "Moseying", "Mulling", "Musing", "Noodling", "Orbiting",
    "Orchestrating", "Percolating", "Philosophising", "Pondering",
    "Pontificating", "Pouncing", "Purring", "Puzzling", "Razzle-dazzling",
    "Ruminating", "Scampering", "Simmering", "Sketching", "Spelunking",
    "Spinning", "Sprouting", "Synthesizing", "Thinking", "Tinkering",
    "Transfiguring", "Transmuting", "Undulating", "Unfurling", "Unravelling",
    "Vibing", "Wandering", "Whisking", "Working", "Wrangling", "Zigzagging",
]  # 共 105 个动词，与 Go 版 internal/tui/verbs.go 完全一致


class ToolGroupSummary(Static, can_focus=True):


    def __init__(self, count: int, total_elapsed: float, **kwargs: Any) -> None:
        label = f"● Done ({count} tool uses · {total_elapsed:.1f}s)  (ctrl+o to expand)"
        super().__init__(label, **kwargs)
        self._count = count
        self._total = total_elapsed
        self._expanded = False

    def _refresh_display(self) -> None:
        if self._expanded:
            self.update(f"▼ Done ({self._count} tool uses · {self._total:.1f}s)")
        else:
            self.update(
                f"● Done ({self._count} tool uses · {self._total:.1f}s)"
                "  (ctrl+o to expand)"
            )

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self._refresh_display()


    def on_click(self) -> None:
        self.toggle()


class SubAgentBlock(Static, can_focus=True):

    def __init__(self, agent_type: str, description: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._agent_type = agent_type or "agent"
        self._description = description[:60] if description else ""
        self._done = False
        self._is_error = False
        self._elapsed = 0.0
        self._collapsed = True
        self._result_preview = ""
        self._tool_count = 0
        self._render_running()

    def _render_running(self) -> None:
        desc = f"({self._description})" if self._description else ""
        self.update(f"● {self._agent_type}{desc}\n     Running…")

    def set_result(self, output: str, is_error: bool, elapsed: float) -> None:
        self._done = True
        self._is_error = is_error
        self._elapsed = elapsed
        self._result_preview = output[:300] if output else ""
        self._parse_stats(output)
        self._render_done()

    def _parse_stats(self, output: str) -> None:
        import re
        m = re.search(r"(\d+)\s+tool", output[:200])
        if m:
            self._tool_count = int(m.group(1))

    def _render_done(self) -> None:
        desc = f"({self._description})" if self._description else ""
        tool_info = f"{self._tool_count} tool uses · " if self._tool_count else ""
        if self._collapsed:
            self.update(
                f"● {self._agent_type}{desc}\n"
                f"    ⎿  Done ({tool_info}{self._elapsed:.1f}s)  (ctrl+o to expand)"
            )
        else:
            self.update(
                f"● {self._agent_type}{desc}\n"
                f"    ⎿  Done ({tool_info}{self._elapsed:.1f}s)\n"
                f"  {self._result_preview}"
            )

    def on_click(self) -> None:
        if not self._done:
            return
        self._collapsed = not self._collapsed
        self._render_done()


_MEWCODE_THEME = Theme(
    name="mewcode",
    primary="#875FFF",
    background="#1a1a1a",
    surface="#1a1a1a",
    panel="#1a1a1a",
    dark=True,
)


class MewCodeApp(App):
    CSS_PATH = "styles.tcss"
    TITLE = "MewCode"
    INLINE_PADDING = 0
    theme = "mewcode"
    BINDINGS = [
        Binding("ctrl+c", "handle_ctrl_c", "Quit", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("shift+tab", "cycle_mode", "Cycle mode", priority=True),
        Binding("ctrl+o", "toggle_tool_blocks", "Toggle tools", priority=True),
    ]


    def __init__(
        self,
        providers: list[ProviderConfig],
        permission_mode: PermissionMode = PermissionMode.DEFAULT,
        mcp_servers: list[MCPServerConfig] | None = None,
        hook_engine: HookEngine | None = None,
        enable_fork: bool = False,
        enable_verification_agent: bool = False,
        worktree_config: Any = None,
        teammate_mode: str = "",
        enable_coordinator_mode: bool = False,
        redis_config: RedisConfig | None = None,
        driver_class: type | None = None,
    ) -> None:
        super().__init__(driver_class=driver_class)
        self.providers = providers
        self._initial_permission_mode = public_permission_mode(permission_mode, allow_plan=True)
        self._mcp_server_configs = mcp_servers or []
        self.hook_engine = hook_engine
        self._enable_fork = enable_fork
        self._enable_verification_agent = enable_verification_agent
        self._worktree_config = worktree_config
        self._teammate_mode = teammate_mode
        self._enable_coordinator_mode = enable_coordinator_mode
        self.context_cache = RedisContextCache(redis_config or RedisConfig(), os.getcwd())
        self._context_cache_lock = asyncio.Lock()
        self.file_cache = FileCache()
        self.plugin_host = ApplicationHost(
            os.getcwd(),
            file_cache=self.file_cache,
            hook_engine=hook_engine,
            enable_verification_agent=enable_verification_agent,
        ).start()
        self.context = self.plugin_host.context
        self.usage_service: UsageService | None = None
        self.client: LLMClient | None = None
        self.conversation = self.context.get(CONVERSATION_FACTORY).create()
        self.registry: ToolRegistry = self.context.tools
        self.agent: Agent | None = None
        self.agent_runtime: AgentRuntime | None = None
        self.mcp_manager: MCPManager | None = None
        self._mcp_init_task: asyncio.Task[None] | None = None
        self._selected_provider: ProviderConfig | None = None
        self._selected_provider_slot_key: str | None = None
        self._provider_slots: list[ProviderSlot] = []
        self._provider_menu_slots: list[ProviderSlot] = []
        # /effort 当前级别（空串表示使用 client 的默认值）
        self._effort: str = ""
        # /model 菜单当前展示的模型列表
        self._model_menu_models: list[str] = []
        # /effort 菜单当前展示的级别列表
        self._effort_menu_levels: list[str] = []
        # 会话统计：TUI 挂载时间（monotonic），用于 Session Summary
        self._session_start: float = 0.0
        self._streaming = False
        self._thinking_start: float = 0.0
        self._thinking_verb: str = ""
        self._spinner_idx: int = 0
        self._spinner_timer = None
        self._spinner_label: Static | None = None
        self._mcp_server_info: str = ""
        self._agent_task: asyncio.Task[None] | None = None
        self._subagent_task: asyncio.Task[None] | None = None
        self._subagent_start_time: float | None = None
        self.session_manager: SessionManager | None = None
        self.session: Session | None = None
        self.memory_manager: MemoryManager | None = None
        self._surfaced_memory_ids: set[str] = set()
        self._instructions_content: str = ""
        self.command_registry: CommandRegistry = self.context.commands
        self.skill_loader: SkillLoader | None = self.context.skills
        self.skill_executor: SkillExecutor | None = None
        self._load_skill_tool: LoadSkill | None = None
        self.agent_loader: AgentLoader | None = self.context.get("agent_catalog", None)
        self.task_manager: TaskManager = self.context.get(TASK_FACTORY).create()
        self.trace_manager: TraceManager = self.context.get(TRACE_FACTORY).create()
        self._notification_check_task: asyncio.Task[None] | None = None
        self.worktree_manager: WorktreeManager | None = None
        self._stale_cleanup_task: asyncio.Task[None] | None = None
        self._current_streaming_label: Static | None = None
        self._current_ai_row: Vertical | None = None
        self._current_accumulated_text: str = ""
        self._mcp_instructions: str = ""
        self._mcp_instructions_ok: bool = False
        self._mcp_connecting: bool = False
        self._teammate_tree: TeammateTree | None = None
        self._teammate_timer = None
        # Harness 配置（由 __main__.py 在构造后设置）
        self._app_config: Any = None
        self._compact_config: Any = None
        self._critic_config: Any = None
        self._rate_limit_config: Any = None
        self._allow_self_modification: bool = False
        # Evolution 配置（由 __main__.py 在构造后设置）
        self._allow_self_evolution: bool = False
        self._evolution_config: Any = None

    @staticmethod
    def _make_banner(model: str = "", work_dir: str = "") -> RichText:
        t = RichText()
        t.append(" /\\_/\\    ", style="bold color(99)")
        t.append("MewCode v0.1.0\n", style="color(242)")
        t.append("( o.o )   ", style="bold color(99)")
        t.append(f"{model}\n" if model else "\n", style="color(242)")
        t.append(" > ^ <    ", style="bold color(99)")
        t.append(work_dir, style="color(242)")
        return t

    def compose(self) -> ComposeResult:
        yield Static(self._make_banner(), id="title-bar")

        if len(self.providers) > 1:
            with Vertical(id="provider-select"):
                yield Static("Select a Provider", id="select-label")
                yield OptionList(
                    *[
                        Option(f"{p.name}  [{p.model}]", id=p.name)
                        for p in self.providers
                    ],
                    id="provider-list",
                )
        yield VerticalScroll(id="chat-area")
        with Vertical(id="input-area"):
            yield ChatInput(id="chat-input")
            with Horizontal(id="status-bar"):
                yield Static("  default", id="mode-label")
                yield Static("", id="teammates-label")
                yield Static("", id="context-label")
                yield Static("", id="effort-label")
                yield Static("", id="model-label")
            yield CompletionPopup()

    def on_mount(self) -> None:
        self.register_theme(_MEWCODE_THEME)
        self.theme = "mewcode"
        self._session_start = _time.monotonic()
        if len(self.providers) == 1:
            self._select_provider(self.providers[0])
        else:
            self.query_one("#chat-area").display = False
            self.query_one("#input-area").display = False

    def _register_runtime_tool(self, tool: Any) -> None:
        if self.agent is None:
            raise RuntimeError("Agent runtime is not initialized")
        self.agent._install_runtime_tool(tool)

    def _register_runtime_command(self, command: Any) -> None:
        if self.agent_runtime is None:
            raise RuntimeError("Agent runtime is not initialized")
        self.agent_runtime.context.slots.register(
            COMMAND_SLOT,
            command,
            item_key=command.name,
        )

    def _select_provider(self, provider: ProviderConfig) -> None:
        selected_provider = provider
        work_dir = os.getcwd()
        from mewcode.tools.exit_plan_mode import ExitPlanModeTool
        from mewcode.tools.synthetic_output import SyntheticOutputTool

        exit_plan_tool = ExitPlanModeTool()
        try:
            provider = resolve_effective_provider_config(provider)
            runtime = self.context.get(AGENT_FACTORY).create(AgentRuntimeRequest(
                provider=provider,
                work_dir=work_dir,
                config=self._app_config,
                permission_mode=self._initial_permission_mode,
                hook_engine=self.hook_engine,
                file_cache=self.file_cache,
                create_session=True,
                extra_tools=(
                    AskUserTool(),
                    exit_plan_tool,
                    SyntheticOutputTool(),
                ),
            ))
        except AuthenticationError as e:
            # 初始化失败：保持 agent 为 None，但输入框仍然可用，
            # 用户可通过 /provider 切换到其他配置源（codex / Claude Code / .env），
            # 或修复配置后重试。不设置 _selected_provider，避免误标当前 provider。
            self._show_error(str(e))
            self._show_system_message(
                "输入 /provider 可切换到其他可用 Provider；"
                "修复配置或设置环境变量后重选即可恢复"
            )
            try:
                self.query_one("#chat-input", ChatInput).focus()
            except Exception:
                pass
            return
        old_runtime = self.agent_runtime
        self.agent_runtime = runtime
        self.client = runtime.client
        self.agent = runtime.agent
        self.registry = runtime.registry
        self.conversation = runtime.conversation
        self.memory_manager = runtime.memory_manager
        self.session_manager = runtime.context.get(SESSIONS)
        self.session = runtime.session
        self.usage_service = runtime.context.get(USAGE)
        if old_runtime is not None:
            old_runtime.close()
        # Keep the caller's slot identity for the provider picker while using
        # the effective copy for client construction and capability probing.
        self._selected_provider = selected_provider
        self._selected_provider_slot_key = self._provider_slot_key_for_provider(
            selected_provider
        )
        self._instructions_content = self.agent.instructions_content

        from mewcode.filehistory import FileHistory
        self.file_history = FileHistory(work_dir, self.session.session_id)
        for tool in self.registry.list_tools():
            if hasattr(tool, "file_history"):
                tool.file_history = self.file_history

        self._load_skill_tool = self.registry.get("LoadSkill")
        self._exit_plan_tool = exit_plan_tool
        self.agent.file_history = self.file_history

        self._exit_plan_tool._is_plan_mode = lambda: self.agent.plan_mode
        self._exit_plan_tool._plan_exists = lambda: self.agent._get_plan_path().exists()

        # Layer 2: 在后台异步拉取模型的 context window，不阻塞启动流程。
        # agent 已经有一个同步解析的窗口值（来自配置 / 映射表 / 默认值）；
        # 如果异步拉取成功，就原地升级为更准确的值。
        self.run_worker(
            self._resolve_context_window(provider), exclusive=False
        )
        if self.context_cache.enabled:
            self.run_worker(self._check_context_cache(), exclusive=False)

        self.skill_loader = self.context.skills

        self.skill_executor = SkillExecutor(
            agent=self.agent,
            client=self.client,
            protocol=provider.protocol,
        )

        for command in build_skill_commands(self.skill_loader, self.skill_executor):
            self._register_runtime_command(command)

        runtime.install(multi_agent_plugin(MultiAgentOptions(
            provider=provider,
            enable_fork=self._enable_fork,
            worktree_config=self._worktree_config,
            teammate_permission_mode=getattr(
                self, "_teammate_permission_mode", ""
            ),
            teammate_mode=self._teammate_mode,
            is_interactive=True,
            enable_coordinator_mode=self._enable_coordinator_mode,
        )))
        self.task_manager = runtime.context.get(TASKS)
        self.trace_manager = runtime.context.get(TRACES)
        self.worktree_manager = runtime.context.get(WORKTREES)
        self.team_manager = runtime.context.get(TEAMS)

        # --- Worktree 系统初始化 ---
        from mewcode.config import WorktreeConfig
        wt_cfg = self._worktree_config or WorktreeConfig()
        restored = self.worktree_manager.restore_session()
        if restored:
            self.agent.work_dir = restored.worktree_path

        wt_command = create_worktree_command(self.worktree_manager)
        self._register_runtime_command(wt_command)

        self._stale_cleanup_task = asyncio.create_task(
            start_stale_cleanup_task(
                self.worktree_manager,
                wt_cfg.stale_cleanup_interval,
                wt_cfg.stale_cutoff_hours,
            )
        )
        runtime.context.effect(self._stale_cleanup_task.cancel)

        # --- 子 agent 系统初始化 ---
        if self.agent_loader is None:
            raise RuntimeError(
                "agent catalog plugin did not provide an AgentLoader"
            )

        agent_catalog = self.agent_loader.list_agents()
        if agent_catalog:
            lines = [
                "## Available Sub-Agent Types",
                "",
                "Use the Agent tool with subagent_type parameter to delegate tasks:",
                "",
            ]
            for agent_type, when_to_use in agent_catalog:
                lines.append(f"- **{agent_type}**: {when_to_use}")
            if self._enable_fork:
                lines.append("")
                lines.append(
                    "Leave subagent_type empty to fork the current conversation "
                    "(inherits full dialog history)."
                )
            lines.append("")
            lines.append(
                "IMPORTANT: Sub-agents run in the background. "
                "After calling the Agent tool, you will get a task ID immediately. "
                "Do NOT wait, sleep, or poll for the result. "
                "Simply report the task ID to the user and end your turn. "
                "The system will automatically notify when the task completes."
            )
            self.agent.set_agent_catalog("\n".join(lines), catalog_list=agent_catalog)

        tasks_cmd = create_tasks_command(self.task_manager)
        self._register_runtime_command(tasks_cmd)

        from mewcode.commands.handlers.trace import create_trace_command
        trace_cmd = create_trace_command(self.trace_manager, self.agent.agent_id)
        self._register_runtime_command(trace_cmd)

        # --- 协调者模式初始化（工具已注册，激活推迟到 TeamCreate 时） ---
        self.agent._team_manager = self.team_manager

        if self.hook_engine:
            asyncio.ensure_future(
                self.hook_engine.run_hooks(
                    "startup", HookContext(event_name="startup")
                )
            )

        if self._mcp_init_task is not None and not self._mcp_init_task.done():
            self._mcp_init_task.cancel()
        self._mcp_init_task = asyncio.create_task(self._init_mcp())

        # --- Loop Engineering 初始化 ---
        runtime.install(PluginSpec(
            "runtime.workflow",
            lambda _ctx: self._init_workflow_engine(provider, work_dir),
            purpose="Workflow engine and tools",
        ))
        runtime.install(PluginSpec(
            "runtime.scheduler",
            lambda _ctx: self._init_scheduler(work_dir),
            purpose="Cron and wakeup scheduler",
        ))

        # --- Harness Engineering 初始化 ---
        runtime.install(PluginSpec(
            "runtime.harness",
            lambda _ctx: self._init_harness(work_dir, provider),
            purpose="Harness, critic, audit, metrics, and evolution",
        ))

        self.query_one("#model-label", Static).update(provider.model)
        work_dir = os.getcwd()
        self.query_one("#title-bar", Static).update(
            self._make_banner(provider.model, work_dir)
        )
        self._update_mode_label()
        self._update_effort_label()
        self._update_context_label()

        select = self.query("#provider-select")
        if select:
            select.first().display = False
        self.query_one("#chat-area").display = True
        self.query_one("#input-area").display = True
        chat_input = self.query_one("#chat-input", ChatInput)
        chat_input.placeholder = "Send a message..."
        chat_input.load_history(work_dir)
        chat_input.focus()

        self._notification_check_task = asyncio.create_task(
            self._start_notification_polling()
        )

    async def _resolve_context_window(self, provider: ProviderConfig) -> None:
        """Layer 2 后台 worker：异步拉取模型的 context window，
        拉到就原地升级 agent 的窗口值。

        尽力而为 — resolve_context_window 不会抛异常；如果拉不到，
        agent 继续使用同步解析得到的窗口值。
        """
        await resolve_context_window(provider)
        if self.agent is not None:
            self.agent.context_window = provider.get_context_window()

    # -----------------------------------------------------------------
    # Loop Engineering 初始化
    # -----------------------------------------------------------------

    async def _run_orchestration_agent(self, **kwargs: Any) -> tuple[Any, dict]:
        """Run workflow and critic calls against the active provider."""
        from mewcode.agent_calls import run_agent_call

        if self.agent is None or self._selected_provider is None:
            raise RuntimeError("LLM provider is not available")
        return await run_agent_call(
            parent_agent=self.agent,
            provider=self._selected_provider,
            worktree_manager=self.worktree_manager,
            **kwargs,
        )

    def _init_workflow_engine(self, provider: ProviderConfig, work_dir: str) -> None:
        """初始化 Workflow 编排引擎并注册相关工具。"""
        from mewcode.workflow.engine import WorkflowEngine
        from mewcode.workflow.tool import ListWorkflowsTool, WorkflowTool

        self.workflow_engine = WorkflowEngine(
            work_dir=work_dir,
            agent_factory=self._run_orchestration_agent,
            on_log=lambda msg: self._show_system_message(f"[workflow] {msg}"),
        )

        # 注册工具
        self._register_runtime_tool(WorkflowTool(
            engine=self.workflow_engine,
            task_manager=self.task_manager,
        ))
        self._register_runtime_tool(ListWorkflowsTool(engine=self.workflow_engine))

        # 注入到 Agent
        if self.agent:
            self.agent.workflow_engine = self.workflow_engine
            if self.agent_runtime is not None:
                self.agent_runtime.context.effect(
                    lambda: setattr(self.agent, "workflow_engine", None)
                )

    def _init_scheduler(self, work_dir: str) -> None:
        """初始化调度系统。"""
        from mewcode.scheduler.store import CronStore
        from mewcode.scheduler.runtime import SchedulerRuntime
        from mewcode.scheduler.wakeup import WakeupScheduler
        from mewcode.scheduler.tools import (
            CronCreateTool, CronDeleteTool, CronListTool, ScheduleWakeupTool,
        )

        self.cron_store = CronStore(work_dir)
        self.wakeup_scheduler = WakeupScheduler()

        self.scheduler_runtime = SchedulerRuntime(
            cron_store=self.cron_store,
            wakeup_scheduler=self.wakeup_scheduler,
            on_fire=lambda job: self._on_scheduled_fire(job),
        )

        # 注册 Cron 工具
        self._register_runtime_tool(CronCreateTool(self.cron_store))
        self._register_runtime_tool(CronDeleteTool(self.cron_store))
        self._register_runtime_tool(CronListTool(self.cron_store))
        self._register_runtime_tool(ScheduleWakeupTool(self.wakeup_scheduler))

        # 启动调度器后台循环
        asyncio.create_task(self.scheduler_runtime.start())
        if self.agent_runtime is not None:
            self.agent_runtime.context.effect(self.scheduler_runtime.shutdown)
            self.agent_runtime.context.provide(
                "scheduler", self.scheduler_runtime, replace=True
            )

    def _on_scheduled_fire(self, job: Any) -> None:
        """调度任务触发时的回调：注入系统消息到对话。"""
        if self.agent and self.conversation:
            reminder = self.scheduler_runtime.inject_job(job)
            self.conversation.add_system_reminder(reminder)
            self._show_system_message(f"[scheduler] job fired: {job.prompt[:60]}")

    # -----------------------------------------------------------------
    # Harness Engineering 初始化
    # -----------------------------------------------------------------

    def _init_harness(self, work_dir: str, provider: ProviderConfig) -> None:
        """初始化 Harness 增强组件。"""
        from mewcode.context.critic import CompletenessCritic
        from mewcode.permissions.audit import AuditLogger
        from mewcode.permissions.rate_limit import RateLimiter
        from mewcode.agents.metrics import MetricsCollector
        from mewcode.harness.hook_manager import HookManager
        from mewcode.harness.config_manager import ConfigManager
        from mewcode.harness.permission_manager import PermissionManager
        from mewcode.harness.tools import (
            AddHookTool, RemoveHookTool, ListHooksTool,
            UpdateConfigTool,
            AddPermissionRuleTool, RemovePermissionRuleTool,
            ManageMemoryTool,
        )

        # 从配置读取
        compact_cfg = getattr(self, '_compact_config', None)
        critic_cfg = getattr(self, '_critic_config', None)
        rate_cfg = getattr(self, '_rate_limit_config', None)
        allow_self = getattr(self, '_allow_self_modification', False)

        # Completeness Critic
        critic_enabled = critic_cfg.enabled if critic_cfg else False
        self.critic = CompletenessCritic(
            enabled=critic_enabled,
            agent_factory=self._run_orchestration_agent,
        )

        # Audit Logger
        session_id = self.session.session_id if self.session else ""
        self.audit_logger = AuditLogger(work_dir, session_id=session_id)

        # Rate Limiter
        rate_enabled = rate_cfg.enabled if rate_cfg else True
        rate_default = rate_cfg.default_max_per_minute if rate_cfg else 30
        rate_per_tool = rate_cfg.per_tool if rate_cfg else {"Bash": 10, "WriteFile": 20}
        self.rate_limiter = RateLimiter(
            enabled=rate_enabled,
            default_max_per_minute=rate_default,
            per_tool_limits=rate_per_tool,
        )

        # Metrics Collector
        self.metrics_collector = MetricsCollector(work_dir, session_id=session_id)

        # Harness Managers
        self.hook_manager = HookManager(hook_engine=self.hook_engine)
        self.config_manager = ConfigManager()
        self.permission_manager_harness = PermissionManager(
            work_dir=work_dir,
            rule_engine=(
                self.agent.permission_checker.rule_engine
                if self.agent and self.agent.permission_checker
                else None
            ),
        )

        # -----------------------------------------------------------------
        # 自进化工具注册（受 allow_self_modification 元权限控制）
        #
        # 当 allow_self_modification: true 时，将 Harness 自进化工具
        # 实例化并注册到 Agent 的 ToolRegistry，使 Agent 能在运行时：
        #   - 增删生命周期 Hook（AddHook / RemoveHook / ListHooks）
        #   - 热更新白名单内配置项（UpdateConfig）
        #   - 管理本地权限规则（AddPermissionRule / RemovePermissionRule）
        #   - 增删查长期记忆条目（ManageMemory）
        #
        # 所有自进化工具 category="harness"，权限检查器据此识别并
        # 叠加 allow_self_modification 元权限校验。
        # 单个工具注册失败不影响其余 — 异常被隔离并记录日志。
        # -----------------------------------------------------------------
        if allow_self:
            import logging as _harness_log
            _log = _harness_log.getLogger(__name__)

            # 构建候选工具列表：(实例, 显示名称)
            _tool_candidates: list[tuple[Any, str]] = []

            # ---- Hook 管理 (依赖 self.hook_manager) ----
            _tool_candidates.extend([
                (AddHookTool(self.hook_manager),    "AddHook"),
                (RemoveHookTool(self.hook_manager), "RemoveHook"),
                (ListHooksTool(self.hook_manager),  "ListHooks"),
            ])

            # ---- 配置管理 (依赖 self.config_manager) ----
            _tool_candidates.append(
                (UpdateConfigTool(self.config_manager), "UpdateConfig")
            )

            # ---- 权限规则管理 (依赖 self.permission_manager_harness) ----
            _tool_candidates.extend([
                (AddPermissionRuleTool(self.permission_manager_harness),
                 "AddPermissionRule"),
                (RemovePermissionRuleTool(self.permission_manager_harness),
                 "RemovePermissionRule"),
            ])

            # ---- Memory 管理 (依赖 self.memory_manager) ----
            _tool_candidates.append(
                (ManageMemoryTool(self.memory_manager), "ManageMemory")
            )

            # 逐个注册，异常隔离
            _registered = 0
            _failed = 0
            for _tool_inst, _tool_name in _tool_candidates:
                try:
                    self._register_runtime_tool(_tool_inst)
                    _registered += 1
                    _log.info(
                        "[harness] tool registered: %s (category=%s)",
                        _tool_name,
                        getattr(_tool_inst, 'category', 'unknown'),
                    )
                except Exception as _exc:
                    _failed += 1
                    _log.error(
                        "[harness] tool registration failed: %s — %s",
                        _tool_name, _exc,
                    )

            _log.info(
                "[harness] self-evolution tools: %d registered, %d failed, %d total",
                _registered, _failed, len(_tool_candidates),
            )

        # 注入到 Agent
        if self.agent:
            self.agent.critic = self.critic
            self.agent.audit_logger = self.audit_logger
            self.agent.rate_limiter = self.rate_limiter
            self.agent.metrics_collector = self.metrics_collector

        # --- Evolution Engineering 初始化 ---
        self._init_evolution(work_dir)

    def _init_evolution(self, work_dir: str) -> None:
        """初始化自进化子系统。"""
        allow_evo = getattr(self, '_allow_self_evolution', False)
        evo_cfg = getattr(self, '_evolution_config', None)

        if not allow_evo or evo_cfg is None:
            return

        from mewcode.harness.evolution.manager import EvolutionManager
        from mewcode.harness.evolution.tools import (
            TriggerEvolutionTool,
            ListEvolutionsTool,
            GetEvolutionDetailTool,
            ListAutoSkillsTool,
            DeprecateSkillTool,
        )

        harness_dir = Path(work_dir) / "mewcode" / "harness"

        # 创建 EvolutionManager
        self.evolution_manager = EvolutionManager(
            harness_dir=harness_dir,
            config=evo_cfg,
            client_factory=self._make_evolution_client(),
            session_manager=self.session_manager,
            memory_manager=self.memory_manager,
            skill_loader=self.skill_loader,
        )

        # 注册进化工具（受 allow_self_evolution 控制）
        _log = logging.getLogger(__name__)
        _tool_candidates: list[tuple[Any, str]] = [
            (TriggerEvolutionTool(self.evolution_manager), "TriggerEvolution"),
            (ListEvolutionsTool(self.evolution_manager), "ListEvolutions"),
            (GetEvolutionDetailTool(self.evolution_manager), "GetEvolutionDetail"),
            (ListAutoSkillsTool(self.evolution_manager.skill_meta_manager), "ListAutoSkills"),
            (DeprecateSkillTool(self.evolution_manager.skill_meta_manager), "DeprecateSkill"),
        ]

        _registered = 0
        _failed = 0
        for _tool_inst, _tool_name in _tool_candidates:
            try:
                self._register_runtime_tool(_tool_inst)
                _registered += 1
            except Exception as _exc:
                _failed += 1
                _log.error("[evolution] tool registration failed: %s — %s", _tool_name, _exc)

        _log.info("[evolution] tools: %d registered, %d failed", _registered, _failed)

        # 将进化子系统注入 Agent，供成功经验路径做 trace 收集、Skill 注入与命中评估
        if getattr(self, "agent", None) is not None:
            self.agent.evolution_manager = self.evolution_manager
            _log.info("[evolution] wired into agent (success path enabled=%s)",
                      self.evolution_manager.success_enabled)

    def _make_evolution_client(self):
        """创建一个轻量级 LLM 客户端工厂，供进化子系统使用。"""
        from mewcode.tools.base import TextDelta, StreamEnd

        async def _client(prompt: str, system: str = "", model_hint: str = "haiku") -> str:
            # 使用已有的 client 进行非流式调用
            if not hasattr(self, 'client') or self.client is None:
                raise RuntimeError("LLM client not available")

            from mewcode.conversation import ConversationManager, Message
            conv = self.context.get(CONVERSATION_FACTORY).create()
            conv.history = [Message(role="user", content=prompt)]

            collected = ""
            try:
                async for event in scheduled_client_stream(
                    self.client, conv, system=system
                ):
                    if isinstance(event, TextDelta):
                        collected += event.text
                    elif isinstance(event, StreamEnd):
                        pass
            except Exception:
                pass

            return collected

        return _client

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "provider-list":
            provider = self.providers[event.option_index]
            self._select_provider(provider)
        elif event.option_list.id == "provider-switch-list":
            slots = self._provider_menu_slots
            idx = event.option_index
            self._close_provider_menu()
            if 0 <= idx < len(slots):
                error = self.switch_provider(slots[idx])
                if error:
                    self._show_system_message(f"切换失败: {error}")
        elif event.option_list.id == "effort-switch-list":
            levels = self._effort_menu_levels
            idx = event.option_index
            self._close_choice_menu("effort-switch")
            if 0 <= idx < len(levels):
                error = self.set_effort(levels[idx])
                if error:
                    self._show_system_message(error)
                else:
                    self._show_system_message(f"Effort 已设置为: {levels[idx]}")
        elif event.option_list.id == "model-switch-list":
            models = self._model_menu_models
            idx = event.option_index
            self._close_choice_menu("model-switch")
            if 0 <= idx < len(models):
                error = self.switch_model(models[idx])
                if error:
                    self._show_system_message(f"切换失败: {error}")

    # -----------------------------------------------------------------
    # Provider 插槽式切换（/provider 命令）
    # -----------------------------------------------------------------

    def get_provider_slots(self, refresh: bool = False) -> list[ProviderSlot]:
        """返回所有可切换的 Provider 插槽（config/codex/Claude Code/.env）。

        结果会缓存；refresh=True 时重新扫描所有配置源，并重新读取
        .mewcode/config.yaml——这样用户在 TUI 运行期间修复了配置文件
        （如填入 API key）后，/provider reload 即可生效，无需重启。"""
        if refresh:
            try:
                from mewcode.config import load_config
                reloaded = load_config()
                if reloaded.providers:
                    self.providers = reloaded.providers
            except Exception:
                pass  # 配置重读失败时沿用启动时的 providers
        if refresh or not self._provider_slots:
            self._provider_slots = discover_provider_slots(
                self.providers, work_dir=os.getcwd()
            )
        return self._provider_slots

    def _provider_slot_key_for_provider(
        self, provider: ProviderConfig
    ) -> str | None:
        """Find the unique discovered slot that owns this provider object.

        Matching the original config object is unambiguous even when another
        source has a provider with the same display name. The resulting stable
        key is kept separately from the effective provider copy used at runtime.
        """
        slots = self.get_provider_slots()
        matches = [slot for slot in slots if slot.provider is provider]
        if len(matches) != 1:
            return None
        key = matches[0].key
        if sum(slot.key == key for slot in slots) != 1:
            return None
        return key

    def _selected_provider_slot(self, slots: list[ProviderSlot]) -> ProviderSlot | None:
        """Return the uniquely matching slot, or none for an ambiguous key."""
        key = self._selected_provider_slot_key
        if not key:
            return None
        matches = [slot for slot in slots if slot.key == key]
        return matches[0] if len(matches) == 1 else None

    def show_provider_menu(self) -> None:
        """在聊天区挂载 Provider 选择菜单（/provider 无参数时调出）。"""
        if self._streaming:
            self._show_system_message("正在等待模型响应，请稍后再切换 Provider")
            return
        if self.query("#provider-switch"):
            return  # 菜单已打开
        slots = self.get_provider_slots(refresh=True)
        if not slots:
            self._show_system_message("未发现任何 Provider 配置源")
            return
        self._provider_menu_slots = slots
        selected_slot = self._selected_provider_slot(slots)

        options: list[Option] = []
        for slot in slots:
            marker = "● " if slot is selected_slot else "  "
            err = validate_slot(slot)
            if err is None:
                options.append(Option(f"{marker}{slot.label}", id=slot.key))
            else:
                # 无效插槽置灰，附带错误原因，不可选
                options.append(Option(
                    f"{marker}{slot.label}  [!] {err}", id=slot.key, disabled=True
                ))

        menu = Vertical(
            Static("切换 Provider（Enter 确认 / Esc 取消）", id="provider-switch-label"),
            OptionList(*options, id="provider-switch-list"),
            id="provider-switch",
        )
        chat = self.query_one("#chat-area", VerticalScroll)
        chat.mount(menu)
        self.call_after_refresh(chat.scroll_end, animate=False)
        try:
            self.query_one("#chat-input").disabled = True
            self.query_one("#provider-switch-list", OptionList).focus()
        except Exception:
            pass

    def _close_provider_menu(self) -> None:
        try:
            self.query_one("#provider-switch").remove()
        except Exception:
            pass
        self._provider_menu_slots = []
        try:
            chat_input = self.query_one("#chat-input", ChatInput)
            chat_input.disabled = False
            chat_input.focus()
        except Exception:
            pass

    def switch_provider(self, slot: ProviderSlot) -> str | None:
        """热切换到目标插槽，成功返回 None，失败返回错误信息。

        会话状态（conversation / session / 工具注册表 / 权限）完全保留，
        仅替换 LLM 客户端及协议相关引用。先静态校验 + 构造客户端，
        全部成功后才原子地应用切换，失败时当前 Provider 不受影响。"""
        if self._streaming:
            return "正在等待模型响应，请稍后再切换"

        # 初始 provider 创建客户端失败（如缺 API key）时 agent 为 None，
        # 走完整初始化路径恢复会话，而不是提示失败。
        if self.agent is None:
            error = validate_slot(slot)
            if error is not None:
                return error
            self._select_provider(slot.provider)
            if self.agent is None:
                return "初始化失败，请查看上方错误信息"
            self._selected_provider_slot_key = slot.key
            return None

        error = validate_slot(slot)
        if error is not None:
            return error

        provider = resolve_effective_provider_config(slot.provider)
        try:
            if self.agent_runtime is None:
                raise RuntimeError("Agent runtime is not initialized")
            client = self.agent_runtime.configure_model(
                provider,
                effort=self._effort,
            )
        except Exception as e:
            return str(e)

        old = self._selected_provider
        self._apply_client(provider, client)
        self._selected_provider_slot_key = slot.key

        old_desc = f"{old.name} [{old.model}]" if old else "(无)"
        self._show_system_message(
            f"Provider 已切换: {old_desc} → {slot.label}\n"
            "会话历史与工具状态保持不变"
        )
        return None

    def _apply_client(self, provider: ProviderConfig, client: LLMClient) -> None:
        """把新建的 client 原子地应用到所有引用方。

        Provider 切换与模型切换共用：更新 app/agent/skill_executor/工具中的
        引用，重应用用户选择的 effort，后台重新解析 context window，并刷新
        状态栏与标题栏。调用前需保证 client 已成功构造。"""
        self.client = client
        self._selected_provider = provider
        if self.skill_executor is not None:
            self.skill_executor.client = client
            self.skill_executor.protocol = provider.protocol
        tool_search = self.registry.get("ToolSearch")
        if tool_search is not None:
            tool_search._protocol = provider.protocol
        agent_tool = self.registry.get("Agent")
        if agent_tool is not None and hasattr(agent_tool, "_provider_config"):
            agent_tool._provider_config = provider

        # Layer 2：后台异步拉取新模型的 context window，不阻塞切换。
        self.run_worker(
            self._resolve_context_window(provider), exclusive=False
        )

        try:
            self.query_one("#model-label", Static).update(provider.model)
            self.query_one("#title-bar", Static).update(
                self._make_banner(provider.model, os.getcwd())
            )
        except Exception:
            pass
        self._update_effort_label()
        self._update_context_label()

    # -----------------------------------------------------------------
    # 状态栏：context 占用 / effort / model
    # -----------------------------------------------------------------

    def _update_context_label(
        self,
        request_tokens: int | None = None,
        usage: UsageEvent | None = None,
    ) -> None:
        """刷新状态栏的上下文占用：ctx 65% (12,800/200,000)。

        request_tokens 为最近一次请求的完整 prompt 大小；为 None 或 0 时
        显示占位符（启动后尚未发出任何请求）。"""
        try:
            label = self.query_one("#context-label", Static)
        except Exception:
            return
        window = 0
        if self.agent is not None:
            window = self.agent.context_window
        elif self._selected_provider is not None:
            window = self._selected_provider.get_context_window()
        if not window:
            label.update("")
            return
        if not request_tokens:
            label.update(f"ctx --/{window:,}")
            return
        pct = min(int(request_tokens / window * 100), 100)
        cache_suffix = ""
        if usage is not None and usage.cumulative_cache_usage_available:
            epoch = f"e{usage.cache_epoch} " if usage.cache_epoch else ""
            cache_suffix = (
                f" · cache {epoch}"
                f"now {usage.request_cache_read_tokens:,}h/"
                f"{usage.request_cache_miss_tokens:,}m "
                f"({usage.request_cache_hit_rate:.0%}) "
                f"${usage.request_cost_usd:.6f} · "
                f"total {usage.cumulative_cache_hit_tokens:,}h/"
                f"{usage.cumulative_cache_miss_tokens:,}m "
                f"({usage.cumulative_cache_hit_rate:.0%}) "
                f"${usage.total_cost_usd:.6f}"
            )
            if usage.cache_invalidation_reason:
                cache_suffix += f" · {usage.cache_invalidation_reason}"
        elif usage is not None:
            cache_suffix = " · cache 未上报"
        elif self.agent is not None:
            cache_totals = self.agent.cache_totals
            if cache_totals.available:
                rate = cache_totals.hit_rate or 0.0
                cache_suffix = f" · cache {rate:.0%}"
        label.update(f"ctx {pct}% ({request_tokens:,}/{window:,}){cache_suffix}")

    def _update_effort_label(self) -> None:
        """刷新状态栏的 effort 显示；空值时隐藏。"""
        try:
            label = self.query_one("#effort-label", Static)
        except Exception:
            return
        effort = self._effort
        if not effort and self.client is not None:
            effort = getattr(self.client, "effort", "") or ""
        label.update(f"effort:{effort}" if effort else "")

    # -----------------------------------------------------------------
    # /effort：调整模型推理强度
    # -----------------------------------------------------------------

    def get_effort_levels(self) -> list[str]:
        """返回当前协议 + 模型能力可用的 effort 级别（动态识别）。"""
        if self._selected_provider is None:
            return []
        return model_effort_levels(
            self._selected_provider.protocol, self._selected_provider.model
        )

    def set_effort(self, level: str) -> str | None:
        """设置 effort 级别，成功返回 None，失败返回错误信息。

        级别会应用到当前 client，并在后续 Provider/模型切换时自动重应用。"""
        levels = self.get_effort_levels()
        if not levels:
            return "当前 Provider 未就绪，无法设置 effort"
        if level not in levels:
            return f"无效 effort 级别: {level}（可选: {', '.join(levels)}）"
        self._effort = level
        if self.client is not None:
            self.client.set_effort(level)
        self._update_effort_label()
        return None

    def show_effort_menu(self) -> None:
        """在聊天区挂载 effort 级别选择菜单（/effort 无参数时调出）。"""
        if self._streaming:
            self._show_system_message("正在等待模型响应，请稍后再调整 effort")
            return
        levels = self.get_effort_levels()
        if not levels:
            self._show_system_message("当前 Provider 未就绪，无法设置 effort")
            return
        current = self._effort
        if not current and self.client is not None:
            current = getattr(self.client, "effort", "") or ""
        self._effort_menu_levels = levels
        items = [
            (lv, f"{'● ' if lv == current else '  '}{lv}", False)
            for lv in levels
        ]
        self._show_choice_menu(
            "effort-switch", "选择 Effort（Enter 确认 / Esc 取消）", items
        )

    # -----------------------------------------------------------------
    # /model：切换当前 Provider 下的模型
    # -----------------------------------------------------------------

    async def list_available_models(self) -> list[str] | None:
        """从当前 provider 拉取可用模型列表；失败或不支持时返回 None。"""
        if self.client is None:
            return None
        try:
            models = await self.client.list_models()
        except Exception:
            return None
        if not models:
            return None
        if self._selected_provider and is_opencode_go_provider(self._selected_provider):
            return ordered_go_model_ids(models)
        return sorted(set(models))

    def show_model_menu(self, models: list[str]) -> None:
        """在聊天区挂载模型选择菜单（/model 无参数时调出）。"""
        if self._streaming:
            self._show_system_message("正在等待模型响应，请稍后再切换模型")
            return
        if not models:
            self._show_system_message(
                "未能获取模型列表，可用 /model <名称> 直接指定"
            )
            return
        current = (
            self._selected_provider.model if self._selected_provider else ""
        )
        # 确保当前模型也在列表里（列表端点可能不含别名/自定义名）
        if current and current not in models:
            models = [current] + list(models)
        self._model_menu_models = models
        items = [
            (
                m,
                f"{'● ' if m == current else '  '}{m}"
                + ("  (missing routing metadata)" if (
                    self._selected_provider
                    and is_opencode_go_provider(self._selected_provider)
                    and opencode_go_route(m) is None
                ) else ""),
                bool(
                    self._selected_provider
                    and is_opencode_go_provider(self._selected_provider)
                    and opencode_go_route(m) is None
                ),
            )
            for m in models
        ]
        self._show_choice_menu(
            "model-switch", "选择模型（Enter 确认 / Esc 取消）", items
        )

    def switch_model(self, model: str) -> str | None:
        """切换当前 Provider 使用的模型，成功返回 None，失败返回错误信息。

        保持 Provider（协议/base_url/密钥）不变，仅更换模型名并重建 client；
        会话历史与工具状态完全保留。"""
        if self._streaming:
            return "正在等待模型响应，请稍后再切换"
        provider = self._selected_provider
        if provider is None or self.agent is None:
            return "当前 Provider 未就绪，无法切换模型"
        model = model.strip()
        if not model:
            return "模型名不能为空"
        old_model = provider.model
        if model == old_model:
            return None
        try:
            provider = resolve_effective_provider_config(provider, model)
            if self.agent_runtime is None:
                raise RuntimeError("Agent runtime is not initialized")
            client = self.agent_runtime.configure_model(
                provider,
                effort=self._effort,
            )
        except Exception as e:
            return str(e)
        self._apply_client(provider, client)
        self._show_system_message(f"模型已切换: {old_model} → {model}")
        return None

    # -----------------------------------------------------------------
    # 通用内联选择菜单（effort / model 共用）
    # -----------------------------------------------------------------

    def _show_choice_menu(
        self, menu_id: str, title: str, items: list[tuple[str, str, bool]]
    ) -> None:
        """挂载一个内联 OptionList 菜单。items 为 (option_id, 文本, 是否禁用)。"""
        if self.query(f"#{menu_id}"):
            return  # 菜单已打开
        options = [
            Option(text, id=oid, disabled=disabled)
            for oid, text, disabled in items
        ]
        menu = Vertical(
            Static(title, id=f"{menu_id}-label"),
            OptionList(*options, id=f"{menu_id}-list"),
            id=menu_id,
        )
        chat = self.query_one("#chat-area", VerticalScroll)
        chat.mount(menu)
        self.call_after_refresh(chat.scroll_end, animate=False)
        try:
            self.query_one("#chat-input").disabled = True
            self.query_one(f"#{menu_id}-list", OptionList).focus()
        except Exception:
            pass

    def _close_choice_menu(self, menu_id: str) -> None:
        try:
            self.query_one(f"#{menu_id}").remove()
        except Exception:
            pass
        try:
            chat_input = self.query_one("#chat-input", ChatInput)
            chat_input.disabled = False
            chat_input.focus()
        except Exception:
            pass

    def add_system_message(self, text: str) -> None:
        self._show_system_message(text)

    def send_user_message(self, text: str) -> None:
        if self._streaming or self.agent is None:
            return
        self._agent_task = asyncio.create_task(self._send_message(text))

    def set_plan_mode(self, enabled: bool) -> None:
        if self.agent is None:
            return
        if enabled:
            if self.agent.permission_mode != PermissionMode.PLAN:
                self._pre_plan_mode = public_permission_mode(self.agent.permission_mode)
            self.agent.set_permission_mode(PermissionMode.PLAN)
        else:
            restore = public_permission_mode(getattr(self, "_pre_plan_mode", PermissionMode.DEFAULT))
            self.agent.set_permission_mode(restore)
        self._update_mode_label()

    def get_token_count(self) -> tuple[int, int]:
        if self.agent:
            return self.agent.total_input_tokens, self.agent.total_output_tokens
        return 0, 0

    def refresh_status(self) -> None:
        self._update_mode_label()

    # -----------------------------------------------------------------
    # 命令分发
    # -----------------------------------------------------------------


    def _build_command_context(self, args: str) -> CommandContext:
        return CommandContext(
            args=args,
            agent=self.agent,
            conversation=self.conversation,
            session=self.session,
            session_manager=self.session_manager,
            memory_manager=self.memory_manager,
            ui=self,
            config={
                "registry": self.command_registry,
                "set_session": self._set_session,
                "set_conversation": self._set_conversation,
                "clear_chat": self._clear_chat,
                "render_restored": self._render_restored_messages,
                "skill_loader": self.skill_loader,
                "skill_executor": self.skill_executor,
                "context_cache": self.context_cache,
                "usage_service": self.usage_service,
                "provider": self._selected_provider,
            },
        )

    def _set_session(self, session: Session) -> None:
        self.session = session
        if self.agent:
            self.agent.session_id = session.session_id

    def _persist_compact_boundary(self, notification: CompactNotification) -> None:
        """Layer-2 compact 后写入 compact_boundary 记录。

        将摘要 + 原样保留的尾部内联到一条记录中，resume 时只需这一条
        就能重建压缩后的状态。之前已写入磁盘的原始前缀不会被重放。
        没有活跃 session 或 compact 未产出 boundary 时直接跳过。
        """
        if not self.session or notification.boundary is None:
            return
        record = make_compact_boundary(
            notification.boundary.summary,
            notification.boundary.keep,
        )
        self.session.append_record(record)

    async def _check_context_cache(self) -> None:
        available = await asyncio.to_thread(self.context_cache.health)
        if available:
            self._show_system_message("Redis 上下文加速已连接")
        else:
            self._show_system_message("Redis 不可用，已回退到 SQLite/JSONL")

    async def _cache_context_snapshot(self) -> None:
        if not self.context_cache.enabled or not self.session:
            return
        async with self._context_cache_lock:
            await asyncio.to_thread(
                self.context_cache.store_context,
                self.session.session_id,
                [
                    message
                    for message in self.conversation.history
                    if not message.transient
                ],
            )

    def _set_conversation(self, conv: ConversationManager) -> None:
        self.conversation = conv
        self._surfaced_memory_ids.clear()

    def _clear_chat(self) -> None:
        chat = self.query_one("#chat-area", VerticalScroll)
        chat.remove_children()

    async def _dispatch_command(self, text: str) -> None:
        name, args, is_command = parse_command(text)

        if not is_command:
            if self._streaming:
                return
            if self.agent is None:
                self._show_system_message(
                    "Provider 未就绪（通常是 API key 未配置）。"
                    "输入 /provider 切换到可用 Provider，或修复配置后重试"
                )
                return
            self._agent_task = asyncio.create_task(self._send_message(text))
            return

        if name == "":
            commands = self.command_registry.list_commands()
            lines = ["可用命令："]
            for cmd in commands:
                aliases_str = ", ".join(f"/{a}" for a in cmd.aliases)
                name_part = f"/{cmd.name}"
                if aliases_str:
                    name_part += f", {aliases_str}"
                lines.append(f"  {name_part:<24} {cmd.description}")
            self._show_system_message("\n".join(lines))
            return

        cmd = self.command_registry.find(name)
        if cmd is None:
            self._show_system_message(f"未知命令：/{name}，输入 /help 查看可用命令")
            return

        if not args and cmd.arg_prompt:
            self._show_system_message(cmd.arg_prompt)
            return

        ctx = self._build_command_context(args)
        try:
            await cmd.handler(ctx)
        except Exception as e:
            self._show_error(f"命令执行失败: {e}")

    # -----------------------------------------------------------------
    # 输入处理
    # -----------------------------------------------------------------

    async def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        text = event.text.strip()
        if self._streaming and not text.startswith("/"):
            await self._interrupt_streaming()
        await self._dispatch_command(text)

    def on_chat_input_tab_complete(self, event: ChatInput.TabComplete) -> None:
        matches = complete(self.command_registry, event.text)
        if not matches:
            return
        popup = self.query_one(CompletionPopup)
        if len(matches) == 1:
            input_widget = self.query_one("#chat-input", ChatInput)
            input_widget.clear()
            input_widget.insert(matches[0][1] + " ")
        else:
            popup.show_pairs(matches)

    def on_chat_input_slash_menu_update(self, event: ChatInput.SlashMenuUpdate) -> None:
        popup = self.query_one(CompletionPopup)
        if event.prefix is None:
            popup.hide()
            return
        matches = complete(self.command_registry, event.prefix)
        if not matches:
            popup.hide()
            return
        popup.show_pairs(matches)

    def on_chat_input_at_file_request(self, event: ChatInput.AtFileRequest) -> None:
        work_dir = self.agent.work_dir if self.agent else os.getcwd()
        matches = scan_files_for_at(event.prefix, work_dir)
        if matches:
            popup = self.query_one(CompletionPopup)
            popup.show([f"@{m}" for m in matches])

    def on_completion_popup_selected(self, event: CompletionPopup.Selected) -> None:
        input_widget = self.query_one("#chat-input", ChatInput)
        selected = event.value
        text = input_widget.text
        if selected.startswith("@"):
            at_idx = text.rfind("@")
            if at_idx >= 0:
                input_widget.clear()
                input_widget.insert(text[:at_idx] + selected + " ")
                input_widget.focus()
                return
        input_widget.clear()
        input_widget.insert(selected + " ")
        input_widget.focus()

    def action_cycle_mode(self) -> None:
        if self.agent is None:
            return
        current = self.agent.permission_mode
        current = public_permission_mode(current, allow_plan=True)
        try:
            idx = _MODE_CYCLE.index(current)
        except ValueError:
            idx = 0
        next_mode = _MODE_CYCLE[(idx + 1) % len(_MODE_CYCLE)]
        if next_mode == PermissionMode.PLAN:
            self.set_plan_mode(True)
        else:
            self.agent.set_permission_mode(next_mode)
        self._update_mode_label()

    def action_toggle_tool_blocks(self) -> None:
        for block in self.query(ToolCallBlock):
            if block._loading:
                continue
            block._collapsed = not block._collapsed
            if block._collapsed:
                block._render_collapsed()
            else:
                block._render_expanded()

        for summary in self.query(ToolGroupSummary):
            was_expanded = summary._expanded
            summary.toggle()
            parent = summary.parent
            if parent:
                for child in parent.children:
                    if isinstance(child, ToolCallBlock) and child.tool_name in COLLAPSIBLE_TOOLS:
                        child.display = summary._expanded

        for block in self.query(SubAgentBlock):
            if block._done:
                block._collapsed = not block._collapsed
                block._render_done()

    async def _interrupt_streaming(self) -> None:
        """打断当前流式响应（Esc / Ctrl+C / 流式中提交新输入时调用）。

        取消 agent 任务并等待其结束——_send_message 的 CancelledError
        处理会把已收到的部分输出以 *[cancelled]* 标记保留下来，随后统一
        清理 streaming 状态并给出可见反馈。"""
        tasks = list(dict.fromkeys(task for task in (
            self._agent_task, self._subagent_task) if task is not None and not task.done()))
        agent = self.agent
        manager = self.task_manager
        owner_id = getattr(agent, "session_id", "")
        store = getattr(agent, "_conversation_store_dir", "") or agent.work_dir
        root_id = getattr(agent, "agent_id", "")
        descendants = {root_id} if root_id else set()
        children = []
        for _ in range(len(manager.list_tasks()) + 1):
            found = [bg for bg in manager.list_tasks() if bg.status == "running" and bg.id not in children
                     and (getattr(bg.agent, "parent_id", None) in descendants
                          or owner_id and bg.owner_session_id == owner_id
                          and bg.owner_scope == (getattr(agent, "_conversation_scope", "") or "project")
                          and bg.owner_store_dir
                          and os.path.normcase(os.path.abspath(bg.owner_store_dir)) == os.path.normcase(os.path.abspath(store)))]
            if not found:
                break
            children.extend(bg.id for bg in found)
            descendants.update(child_id for bg in found
                               if (child_id := getattr(bg.agent, "agent_id", "")))
        if not self._streaming and not tasks and not children:
            return
        for child_id in children:
            manager.cancel(child_id)
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        pending = await manager.settle_tasks(children, timeout=2.0)
        self._finish_streaming()
        failed = [task_id for task_id in children if (bg := manager.get(task_id)) is not None
                  and bg.lifecycle_finalized and not bg.cleanup_confirmed]
        message = "(response interrupted)"
        if pending:
            message += " 子 Agent 中断已请求，仍在清理：" + ", ".join(pending)
        if failed:
            message += " 子 Agent 清理未确认：" + ", ".join(failed)
        self._show_system_message(message)
        try:
            inp = self.query_one("#chat-input", ChatInput)
            inp.disabled = False
            inp.focus()
        except Exception:
            pass

    def action_cancel(self) -> None:
        if self.query("#provider-switch"):
            self._close_provider_menu()
            return
        if self.query("#effort-switch"):
            self._close_choice_menu("effort-switch")
            return
        if self.query("#model-switch"):
            self._close_choice_menu("model-switch")
            return
        popup = self.query_one(CompletionPopup)
        if popup.is_visible:
            popup.hide()
            self.query_one("#chat-input", ChatInput).focus()
            return
        self.run_worker(self._interrupt_streaming(), exclusive=False)

    async def _prefetch_relevant_memories(self, query: str) -> str:
        """Run the recall selector as a side-query with an 8s timeout.

        Creates a fresh LLM client so the selector's system prompt is
        independent of the main conversation's system prompt. Returns the
        rendered system-reminder body, or "" on any failure / timeout.
        """
        if self.memory_manager is None or self._selected_provider is None:
            return ""

        provider = self._selected_provider
        user_dir = self.memory_manager.user_mem_dir
        project_dir = self.memory_manager.project_mem_dir

        if self.context_cache.enabled:
            cached = await asyncio.to_thread(self.context_cache.get_recall, query)
            if cached is not None:
                reminder, memory_ids = cached
                if set(memory_ids).issubset(self._surfaced_memory_ids):
                    return ""
                self._surfaced_memory_ids.update(memory_ids)
                return reminder

        async def selector(system_prompt: str, user_message: str) -> str:
            from mewcode.tools.base import StreamEnd, TextDelta

            side_client = self.context.get(CLIENT_FACTORY).create(provider)
            if self.usage_service is not None:
                self.usage_service.bind_client(side_client)
            mini_conv = self.context.get(CONVERSATION_FACTORY).create()
            mini_conv.history = [Message(role="user", content=user_message)]
            collected = ""
            async for event in scheduled_client_stream(
                side_client, mini_conv, system=system_prompt
            ):
                if isinstance(event, TextDelta):
                    collected += event.text
                elif isinstance(event, StreamEnd):
                    pass
            return collected

        try:
            results = await asyncio.wait_for(
                find_relevant_memories(
                    query=query,
                    user_mem_dir=user_dir,
                    project_mem_dir=project_dir,
                    recent_tools=None,
                    already_surfaced=self._surfaced_memory_ids,
                    selector=selector,
                    memory_manager=self.memory_manager,
                ),
                timeout=8.0,
            )
            self._surfaced_memory_ids.update(
                memory.memory_id for memory in results if memory.memory_id
            )
            reminder = render_reminder(results)
            if reminder and self.context_cache.enabled:
                await asyncio.to_thread(
                    self.context_cache.set_recall,
                    query,
                    reminder,
                    [memory.memory_id for memory in results if memory.memory_id],
                )
            return reminder
        except (asyncio.TimeoutError, Exception):
            return ""

    async def _send_message(self, text: str, is_notification: bool = False) -> None:
        assert self.agent is not None

        if self._mcp_init_task and not self._mcp_init_task.done():
            self._show_system_message("Waiting for MCP servers to connect...")
            await self._mcp_init_task

        self._streaming = True
        chat = self.query_one("#chat-area", VerticalScroll)
        input_widget = self.query_one("#chat-input", ChatInput)

        if text and "@" in text:
            text = expand_at_refs(text, self.agent.work_dir)

        # Start memory recall prefetch before UI work.
        prefetch_task = asyncio.create_task(
            self._prefetch_relevant_memories(text)
        ) if text else None

        if text:
            user_row = Vertical(classes="user-row")
            await chat.mount(user_row)
            from rich.text import Text as RichText
            user_rich = RichText()
            user_rich.append("❯ ", style="bold color(80)")
            user_rich.append(text, style="bold color(255)")
            user_bubble = Static(user_rich, classes="message user-message")
            await user_row.mount(user_bubble)
            self.call_after_refresh(chat.scroll_end, animate=False)

            self.conversation.add_user_message(text)
            if self.session:
                self.session.append(Message(role="user", content=text))

        if self._mcp_instructions and not self._mcp_instructions_ok:
            self.conversation.add_system_reminder(self._mcp_instructions)
            self._mcp_instructions_ok = True

        # Collect prefetched recall with 3s timeout, inject as system-reminder.
        if prefetch_task is not None:
            try:
                reminder = await asyncio.wait_for(prefetch_task, timeout=3.0)
                if reminder:
                    self.conversation.add_system_reminder(reminder)
            except (asyncio.TimeoutError, Exception):
                pass

        persisted_history_ids = {
            id(message) for message in self.conversation.history
        }

        def persist_new_history() -> None:
            if self.session is None:
                return
            for message in self.conversation.history:
                message_id = id(message)
                if message_id in persisted_history_ids:
                    continue
                persisted_history_ids.add(message_id)
                self.session.append(message)

        # 准备 AI 回复区域
        ai_row = Vertical(classes="ai-row")
        await chat.mount(ai_row)
        streaming_label = Static("", classes="message ai-message")
        await ai_row.mount(streaming_label)

        accumulated_text = ""
        tool_blocks: dict[str, ToolCallBlock] = {}

        # 在聊天区底部启动持续旋转的加载动画
        self._thinking_start = _time.monotonic()
        self._thinking_verb = random.choice(THINKING_VERBS)
        self._spinner_idx = 0
        self._spinner_label = Static(
            f"  {SPINNER_FRAMES[0]} {self._thinking_verb}…  (Esc 打断)",
            id="spinner-live",
        )
        await chat.mount(self._spinner_label)

        # Mount teammate tree (initially hidden) below the spinner
        self._teammate_tree = TeammateTree(id="teammate-tree")
        self._teammate_tree.display = False
        await chat.mount(self._teammate_tree)
        self._start_teammate_polling()

        self.call_after_refresh(chat.scroll_end, animate=False)
        self._start_spinner()

        await asyncio.sleep(0)

        try:
            async for event in self.agent.run(self.conversation):
                if isinstance(event, ThinkingText):
                    self.call_after_refresh(chat.scroll_end, animate=False)

                elif isinstance(event, StreamText):
                    if streaming_label is not None and not accumulated_text:
                        await streaming_label.remove()
                        streaming_label = Static("", classes="message ai-message")
                        await ai_row.mount(streaming_label)
                    accumulated_text += event.text
                    from rich.text import Text as RichText
                    t = RichText()
                    t.append("● ", style="bold color(99)")
                    t.append(accumulated_text)
                    streaming_label.update(t)
                    self.call_after_refresh(chat.scroll_end, animate=False)

                elif isinstance(event, RetryEvent):
                    self._show_system_message(f"↻ Retrying: {event.reason}")

                elif isinstance(event, ToolUseEvent):
                    if accumulated_text:
                        if streaming_label is not None:
                            await streaming_label.remove()
                        from rich.text import Text as RichText
                        prefix = Static(RichText("●  ", style="bold color(99)"), classes="message")
                        await ai_row.mount(prefix)
                        md = Markdown(accumulated_text, classes="message ai-message")
                        await ai_row.mount(md)
                        streaming_label = None
                        accumulated_text = ""
                    elif streaming_label is not None:
                        await streaming_label.remove()
                        streaming_label = None

                    if _is_subagent_tool(event.tool_name):
                        agent_type = event.arguments.get("subagent_type", "")
                        desc = event.arguments.get("description", "")
                        block = SubAgentBlock(
                            agent_type or "agent",
                            desc,
                            classes="tool-block subagent-block",
                        )
                    else:
                        block = ToolCallBlock(
                            event.tool_name, event.arguments, classes="tool-block"
                        )
                    await ai_row.mount(block)
                    tool_blocks[event.tool_id] = block
                    self.call_after_refresh(chat.scroll_end, animate=False)

                elif isinstance(event, PermissionRequest):
                    await self._handle_permission_request(event)

                elif isinstance(event, HumanOnCallRequest):
                    await self._handle_human_oncall(event)

                elif isinstance(event, ToolResultEvent):
                    block = tool_blocks.get(event.tool_id)
                    if block:
                        block.set_result(event.output, event.is_error, event.elapsed)
                    self.call_after_refresh(chat.scroll_end, animate=False)

                    ask_tool = self.registry.get("AskUserQuestion")
                    if ask_tool and isinstance(ask_tool, AskUserTool) and ask_tool._pending_event:
                        await self._handle_askuser(ask_tool._pending_event)

                elif isinstance(event, TurnComplete):
                    if self.session:
                        persist_new_history()
                        asyncio.create_task(self._cache_context_snapshot())

                    collapsible = [
                        (tid, blk) for tid, blk in tool_blocks.items()
                        if isinstance(blk, ToolCallBlock)
                        and blk.tool_name in COLLAPSIBLE_TOOLS
                        and not blk._loading
                    ]
                    if len(collapsible) >= 2:
                        total_elapsed = sum(b._elapsed for _, b in collapsible)
                        summary = ToolGroupSummary(
                            len(collapsible), total_elapsed,
                            classes="tool-block tool-group-summary",
                        )
                        for _, blk in collapsible:
                            blk.display = False
                        await ai_row.mount(summary)

                    tool_blocks.clear()
                    ai_row = Vertical(classes="ai-row")
                    await chat.mount(ai_row)
                    streaming_label = Static("", classes="message ai-message")
                    await ai_row.mount(streaming_label)
                    accumulated_text = ""
                    self.call_after_refresh(chat.scroll_end, animate=False)

                elif isinstance(event, UsageEvent):
                    # 用最近一次请求的 prompt 大小刷新状态栏上下文占比
                    if event.request_input_tokens:
                        self._update_context_label(event.request_input_tokens, event)

                elif isinstance(event, HookEvent):
                    status = "✓" if event.success else "✗"
                    self._show_system_message(
                        f"Hook [{event.hook_id}] {status} {event.output}"
                    )

                elif isinstance(event, CompactNotification):
                    self._show_system_message(event.message)
                    # auto_compact 已重写 conversation.history（摘要 +
                    # boundary + 保留尾部）。先持久化 boundary 记录，然后
                    # 将游标推进到重建后的历史末尾，这样 TurnComplete/LoopComplete
                    # 刷盘时只追加 boundary 之后的新消息，不会把已压缩的
                    # 前缀作为普通记录重复写入。
                    self._persist_compact_boundary(event)
                    persisted_history_ids = {
                        id(message) for message in self.conversation.history
                    }
                    asyncio.create_task(self._cache_context_snapshot())

                elif isinstance(event, ErrorEvent):
                    self._show_error(event.message)

                elif isinstance(event, LoopComplete):
                    total_time = _time.monotonic() - self._thinking_start
                    done_label = Static(
                        f"✻ {_to_past_tense(self._thinking_verb)} for {total_time:.1f}s",
                        classes="message thinking-done",
                    )
                    await ai_row.mount(done_label)
                    # 会话统计：每轮结束后显示累计时长与 token 用量
                    if self._session_start:
                        elapsed = _time.monotonic() - self._session_start
                        self._show_system_message(
                            f"Session: {_fmt_duration(elapsed)} · "
                            f"{self.agent.total_input_tokens:,} in / "
                            f"{self.agent.total_output_tokens:,} out"
                        )
                    if self.session:
                        persist_new_history()
                        self.session.meta.total_tokens = (
                            self.agent.total_input_tokens
                            + self.agent.total_output_tokens
                        )
                        asyncio.ensure_future(
                            self._update_session_summary()
                        )
                        asyncio.create_task(self._cache_context_snapshot())
                    if self.agent.plan_mode:
                        asyncio.ensure_future(
                            self._show_plan_approval()
                        )

            # 收尾：渲染剩余的累积文本
            if accumulated_text and streaming_label is not None:
                await streaming_label.remove()
                md = Markdown(accumulated_text, classes="message ai-message")
                await ai_row.mount(md)
            elif streaming_label is not None:
                await streaming_label.remove()

            self.call_after_refresh(chat.scroll_end, animate=False)

        except asyncio.CancelledError:
            if accumulated_text:
                if streaming_label is not None:
                    await streaming_label.remove()
                md = Markdown(
                    accumulated_text + "\n\n*[cancelled]*",
                    classes="message ai-message",
                )
                await ai_row.mount(md)
            self._show_system_message("Operation cancelled")
        except LLMError as e:
            self._show_error(str(e))
        finally:
            self._finish_streaming()
            input_widget.focus()

            await self._process_task_notifications()

    async def _process_task_notifications(self) -> None:
        completed = self.task_manager.poll_completed()
        if not completed or self.agent is None:
            return

        inject_task_notifications(self.conversation, completed)

        for task in completed:
            status_icon = "✓" if task.status == "completed" else "✗"
            self._show_system_message(
                f"{status_icon} 后台任务完成: [{task.id}] {task.name} — {task.status}"
            )

            if hasattr(self, 'team_manager'):
                self.team_manager.on_teammate_completed(task.agent.agent_id)

        # on_teammate_completed 会同步写 idle 信——立刻一并注入本次 turn，
        # 否则同一 tick 里 _process_mailbox_notifications 会再 create_task
        # 一个 _send_message，两个协程并发驱动同一 Agent/conversation
        if hasattr(self, "team_manager") and self.team_manager is not None:
            for note in self.team_manager.drain_lead_mailbox():
                self.conversation.add_system_reminder(note)

        # 在事件循环让出前置位，关闭"已调度未启动"的竞态窗口
        self._streaming = True
        self._agent_task = asyncio.create_task(
            self._send_message("", is_notification=True)
        )

    async def _start_notification_polling(self) -> None:
        while True:
            await asyncio.sleep(2)
            if not self._streaming and self.agent is not None:
                await self._process_task_notifications()
                await self._process_mailbox_notifications()

    async def _process_mailbox_notifications(self) -> None:
        if not hasattr(self, "team_manager") or self.team_manager is None:
            return
        if self._streaming or self.agent is None:
            return
        notes = self.team_manager.drain_lead_mailbox()
        if not notes:
            return
        for note in notes:
            self.conversation.add_system_reminder(note)
        self._streaming = True
        self._agent_task = asyncio.create_task(
            self._send_message("", is_notification=True)
        )

    async def _show_plan_approval(self) -> None:
        from mewcode.plan_dialog import InlinePlanWidget

        chat = self.query_one("#chat-area", VerticalScroll)
        widget = InlinePlanWidget()
        await chat.mount(widget)
        self.call_after_refresh(chat.scroll_end, animate=False)
        try:
            self.query_one("#chat-input").disabled = True
        except Exception:
            pass

    def on_inline_plan_widget_responded(
        self, event: "InlinePlanWidget.Responded"
    ) -> None:
        from mewcode.plan_dialog import InlinePlanWidget, PlanChoice

        try:
            self.query_one("#plan-inline", InlinePlanWidget).remove()
        except Exception:
            pass
        try:
            self.query_one("#chat-input").disabled = False
            self.query_one("#chat-input").focus()
        except Exception:
            pass

        if self.agent is None:
            return

        choice = event.choice
        feedback = event.feedback
        plan_path = self.agent._get_plan_path()
        plan_content = ""
        if plan_path.exists():
            try:
                plan_content = plan_path.read_text(encoding="utf-8")
            except Exception:
                pass

        pre = public_permission_mode(getattr(self, "_pre_plan_mode", PermissionMode.DEFAULT))
        if choice == PlanChoice.YOLO:
            self.agent.set_permission_mode(PermissionMode.BYPASS)
            self._update_mode_label()
            if plan_content:
                self.send_user_message(f"Execute this plan:\n\n{plan_content}")
        elif choice == PlanChoice.MANUAL:
            self.agent.set_permission_mode(pre)
            self._update_mode_label()
            if plan_content:
                self.send_user_message(f"Execute this plan:\n\n{plan_content}")
        elif choice == PlanChoice.FEEDBACK:
            if feedback:
                self.send_user_message(feedback)
            else:
                self._show_system_message("Type your feedback and send.")

    async def _handle_human_oncall(self, event: HumanOnCallRequest) -> None:
        from mewcode.human_oncall_dialog import InlineHumanOnCallWidget

        if not event.questions:
            if not event.future.done():
                event.future.set_result(None)
            return

        chat = self.query_one("#chat-area", VerticalScroll)
        widget = InlineHumanOnCallWidget(event.questions[0])
        self._pending_human_oncall_request = event
        await chat.mount(widget)
        self.call_after_refresh(chat.scroll_end, animate=False)
        try:
            self.query_one("#chat-input").disabled = True
        except Exception:
            pass

    def on_inline_human_on_call_widget_responded(
        self, event: "InlineHumanOnCallWidget.Responded"
    ) -> None:
        from mewcode.human_oncall_dialog import InlineHumanOnCallWidget

        request = getattr(self, "_pending_human_oncall_request", None)
        if request is not None and not request.future.done():
            try:
                answers = validate_human_answers(request.questions, event.answers)
            except ValueError as exc:
                self._show_error(str(exc))
                return
            request.future.set_result(answers)
        self._pending_human_oncall_request = None
        try:
            self.query_one(
                "#human-oncall-inline", InlineHumanOnCallWidget
            ).remove()
        except Exception:
            pass
        try:
            self.query_one("#chat-input").disabled = False
            self.query_one("#chat-input").focus()
        except Exception:
            pass

    async def _handle_askuser(self, event: AskUserEvent) -> None:
        from mewcode.askuser_dialog import InlineAskUserWidget

        chat = self.query_one("#chat-area", VerticalScroll)
        widget = InlineAskUserWidget(event.questions)
        self._pending_askuser_event = event
        await chat.mount(widget)
        self.call_after_refresh(chat.scroll_end, animate=False)
        try:
            self.query_one("#chat-input").disabled = True
        except Exception:
            pass

    def on_inline_ask_user_widget_responded(
        self, event: "InlineAskUserWidget.Responded"
    ) -> None:
        from mewcode.askuser_dialog import InlineAskUserWidget

        req = getattr(self, "_pending_askuser_event", None)
        if req is not None and not req.future.done():
            req.future.set_result(event.answers if event.answers else {})
            self._pending_askuser_event = None
        try:
            self.query_one("#askuser-inline", InlineAskUserWidget).remove()
        except Exception:
            pass
        try:
            self.query_one("#chat-input").disabled = False
            self.query_one("#chat-input").focus()
        except Exception:
            pass

    def _start_spinner(self) -> None:
        """启动 braille spinner 动画（每帧 80ms）。"""
        if self._spinner_timer is not None:
            return
        self._spinner_timer = self.set_interval(0.08, self._tick_spinner)

    def _stop_spinner(self) -> None:
        """停止 spinner 动画。"""
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None

    def _finish_streaming(self) -> None:
        """清理所有 streaming 状态（取消或完成时调用）。"""
        self._streaming = False
        self._stop_spinner()
        self._stop_teammate_polling()
        self._agent_task = None
        pending = getattr(self, "_pending_human_oncall_request", None)
        if pending is not None and not pending.future.done():
            pending.future.cancel()
        self._pending_human_oncall_request = None
        try:
            self.query_one("#human-oncall-inline").remove()
        except Exception:
            pass
        try:
            self.query_one("#chat-input").disabled = False
        except Exception:
            pass
        if self._teammate_tree is not None:
            self._teammate_tree.remove()
            self._teammate_tree = None
        if self._spinner_label is not None:
            self._spinner_label.remove()
            self._spinner_label = None

    def _tick_spinner(self) -> None:
        """推进持久 spinner 标签上的动画帧。"""
        self._spinner_idx += 1
        frame = SPINNER_FRAMES[self._spinner_idx % len(SPINNER_FRAMES)]
        elapsed = _time.monotonic() - self._thinking_start
        if self._spinner_label is not None:
            self._spinner_label.update(
                f"  {frame} {self._thinking_verb}…  ({elapsed:.0f}s · Esc 打断)"
            )
            if self._spinner_idx % 5 == 0:
                try:
                    self.query_one("#chat-area", VerticalScroll).scroll_end(animate=False)
                except Exception:
                    pass

    def _start_teammate_polling(self) -> None:
        """Start polling teammate progress every 0.5s."""
        if self._teammate_timer is not None:
            return
        self._teammate_timer = self.set_interval(0.5, self._tick_teammate_tree)

    def _stop_teammate_polling(self) -> None:
        """Stop the teammate progress polling timer."""
        if self._teammate_timer is not None:
            self._teammate_timer.stop()
            self._teammate_timer = None

    def _tick_teammate_tree(self) -> None:
        """Poll team_manager for teammate progress and update the tree widget."""
        if not hasattr(self, "team_manager") or self.team_manager is None:
            return
        if self._teammate_tree is None:
            return

        progress_list = self.team_manager.get_all_teammate_progress()

        if not progress_list:
            self._teammate_tree.display = False
            self._update_teammates_label(0)
            return

        # Update the reactive properties via mutate_reactive for list
        self._teammate_tree.teammates = list(progress_list)

        # Update leader tokens from main agent
        if self.agent:
            self._teammate_tree.leader_tokens = (
                self.agent.total_input_tokens + self.agent.total_output_tokens
            )

        self._teammate_tree.display = True
        active_count = sum(1 for p in progress_list if p.status == "running")
        self._update_teammates_label(active_count)

    def _update_teammates_label(self, count: int) -> None:
        """Update the teammates count in the status bar."""
        try:
            label = self.query_one("#teammates-label", Static)
            if count > 0:
                label.update(f"[cyan]● {count} teammate{'s' if count != 1 else ''}[/cyan]  ")
            else:
                label.update("")
        except Exception:
            pass

    async def _handle_permission_request(self, request: PermissionRequest) -> None:
        from mewcode.permission_dialog import InlinePermissionWidget

        chat = self.query_one("#chat-area", VerticalScroll)
        widget = InlinePermissionWidget(request.tool_name, request.description)
        self._pending_perm_request = request
        await chat.mount(widget)
        self.call_after_refresh(chat.scroll_end, animate=False)
        # 权限提示弹窗期间禁用输入框
        try:
            self.query_one("#chat-input").disabled = True
        except Exception:
            pass

    def on_inline_permission_widget_responded(
        self, event: "InlinePermissionWidget.Responded"
    ) -> None:
        from mewcode.permission_dialog import InlinePermissionWidget

        req = getattr(self, "_pending_perm_request", None)
        if req is not None:
            req.future.set_result(event.response)
            self._pending_perm_request = None
        # 从聊天区移除权限弹窗组件
        try:
            widget = self.query_one("#perm-inline", InlinePermissionWidget)
            widget.remove()
        except Exception:
            pass
        # 重新启用输入框
        try:
            self.query_one("#chat-input").disabled = False
            self.query_one("#chat-input").focus()
        except Exception:
            pass

    # -----------------------------------------------------------------
    # 恢复 session 的消息渲染
    # -----------------------------------------------------------------

    async def _render_restored_messages(self, messages: list[Message]) -> None:
        chat = self.query_one("#chat-area", VerticalScroll)
        await chat.remove_children()

        for msg in messages:
            if msg.tool_results or not msg.content:
                continue
            if msg.role == "user":
                row = Vertical(classes="user-row")
                await chat.mount(row)
                user_rich = RichText()
                user_rich.append("❯ ", style="bold color(80)")
                user_rich.append(msg.content, style="bold color(255)")
                bubble = Static(user_rich, classes="message user-message")
                await row.mount(bubble)
            elif msg.role == "assistant":
                row = Vertical(classes="ai-row")
                await chat.mount(row)
                md = Markdown(msg.content, classes="message ai-message")
                await row.mount(md)

        self.call_after_refresh(chat.scroll_end, animate=False)

    # -----------------------------------------------------------------
    # Session 摘要（异步后台生成）
    # -----------------------------------------------------------------

    async def _update_session_summary(self) -> None:
        if not self.session or not self.client or not self.agent:
            return
        try:
            summary = await generate_session_summary(
                self.agent.auxiliary_client,
                self.conversation,
                self.agent.protocol,
            )
            if summary:
                self.session.meta.summary = summary
                self.session.meta.save(
                    self.session._sessions_dir / f"{self.session.session_id}.meta"
                )
        except Exception:
            pass

    # -----------------------------------------------------------------
    # MCP
    # -----------------------------------------------------------------

    async def _init_mcp(self) -> None:
        self._mcp_connecting = True
        self._update_mode_label()
        tools_before = len(self.registry.list_tools())
        if self.agent_runtime is None:
            self._mcp_connecting = False
            return
        from mewcode.mcp.playwright_browser import format_mcp_connect_error, install_runtime_mcp

        runtime = self.agent_runtime
        errors: list[str] = []
        manager = None
        try:
            errors = await install_runtime_mcp(runtime, self._mcp_server_configs)
            manager = runtime.context.get("mcp", None)
        except Exception as exc:
            errors = [format_mcp_connect_error("playwright", exc)]
        if runtime is not self.agent_runtime:
            return
        self.mcp_manager = manager
        self._mcp_connecting = False
        self._update_mode_label()
        for err in errors:
            self._show_system_message(f"MCP warning: {err}")
        tools_after = len(self.registry.list_tools())
        mcp_tools = tools_after - tools_before
        server_count = len(manager._clients) if manager is not None else 0
        if server_count > 0:
            self._mcp_server_info = (
                f"Connected to {server_count} MCP server(s), {mcp_tools} tools registered"
            )
        if server_count > 0 and mcp_tools > 0 and manager is not None:
            parts = []
            for cfg in manager._configs.values():
                srv_name = cfg.name if hasattr(cfg, "name") else str(cfg)
                tool_names = [
                    t.name for t in self.registry.list_tools()
                    if t.name.startswith(f"mcp_{srv_name}_")
                    or (
                        srv_name == "playwright"
                        and (
                            t.name.startswith("browser_")
                            or t.name == "browser_session"
                        )
                    )
                ]
                section = f"## {srv_name}\n"
                if tool_names:
                    section += "Available tools: " + ", ".join(tool_names)
                parts.append(section)
            self._mcp_instructions = (
                "# MCP Server Instructions\n\n"
                "The following MCP servers are connected. "
                "Use their tools when the user asks.\n\n"
                + "\n\n".join(parts)
            )

    async def _shutdown_mcp(self) -> None:
        if self._mcp_init_task is not None:
            self._mcp_init_task.cancel()
            try:
                await self._mcp_init_task
            except (asyncio.CancelledError, Exception):
                pass
            self._mcp_init_task = None
        if self.mcp_manager is not None:
            if self.agent_runtime is not None:
                await self.agent_runtime.context.aunload("runtime.mcp")
            else:
                await self.mcp_manager.shutdown()
            self.mcp_manager = None

    # -----------------------------------------------------------------
    # 退出
    # -----------------------------------------------------------------

    async def action_handle_ctrl_c(self) -> None:
        if self._streaming:
            await self._interrupt_streaming()
            return

        if getattr(self, "_exit_requested", False):
            self.exit()
            return
        self._exit_requested = True

        async def _cleanup() -> None:
            tasks: list[asyncio.Task] = []

            if self.agent and self.agent.memory_manager:
                tasks.append(asyncio.create_task(
                    self.agent._extract_memories(self.conversation)
                ))
            if self.hook_engine:
                tasks.append(asyncio.create_task(
                    self.hook_engine.run_hooks(
                        "shutdown", HookContext(event_name="shutdown")
                    )
                ))
            tasks.append(asyncio.create_task(self._shutdown_mcp()))

            if tasks:
                await asyncio.wait(tasks, timeout=3.0)
                for t in tasks:
                    if not t.done():
                        t.cancel()

            if self._stale_cleanup_task and not self._stale_cleanup_task.done():
                self._stale_cleanup_task.cancel()

            if hasattr(self, 'team_manager'):
                for name in list(self.team_manager._teams):
                    try:
                        self.team_manager.delete_team(name, force=True)
                    except Exception:
                        pass

            if self.session:
                self.session.close()

            if getattr(self, "plugin_host", None) is not None:
                await self.plugin_host.aclose()

            # Evolution cleanup: flush traces + check evolution trigger
            if hasattr(self, 'evolution_manager') and self.evolution_manager is not None:
                try:
                    self.evolution_manager.trace_collector.flush()
                    await self.evolution_manager.check_and_evolve()
                    self.evolution_manager.cleanup()
                except Exception:
                    pass

        try:
            await _cleanup()
        except Exception:
            pass
        self.exit()

    def _show_error(self, text: str) -> None:
        chat = self.query_one("#chat-area", VerticalScroll)
        error_widget = Static(f"✖ {text}", classes="message error-message")
        chat.mount(error_widget)
        self.call_after_refresh(chat.scroll_end, animate=False)

    def _show_system_message(self, text: str) -> None:
        chat = self.query_one("#chat-area", VerticalScroll)
        msg = Static(f"  {text}", classes="message system-message")
        chat.mount(msg)
        self.call_after_refresh(chat.scroll_end, animate=False)

    _MODE_DISPLAY = PERMISSION_MODE_LABELS

    def _update_mode_label(self) -> None:
        if self.agent:
            perm = self.agent.permission_mode
            level = public_permission_mode(perm, allow_plan=True)
            display = self._MODE_DISPLAY[level]
            color = _MODE_COLORS.get(perm, "dim")
            label = self.query_one("#mode-label", Static)
            if perm == PermissionMode.DEFAULT:
                label.update(f"[{color}]{display}[/{color}]")
            else:
                label.update(f"[{color}]{display}[/{color}]  (shift+tab to cycle)")
        try:
            model_label = self.query_one("#model-label", Static)
            model_text = self._selected_provider.model if self._selected_provider else ""
            if self._mcp_connecting:
                model_label.update(f"[yellow]MCP connecting…[/yellow]  {model_text}")
            else:
                model_label.update(model_text)
        except Exception:
            pass

    def _update_token_label(self, input_tokens: int, output_tokens: int) -> None:
        pass  # token 标签已从 UI 中移除
