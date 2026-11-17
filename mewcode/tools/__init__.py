from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING, Any

from mewcode.tools.base import Tool

if TYPE_CHECKING:
    from mewcode.cache import FileCache


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._disabled: set[str] = set()
        self._discovered: set[str] = set()

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)
        self._disabled.discard(name)
        self._discovered.discard(name)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)


    def is_enabled(self, name: str) -> bool:
        return name in self._tools and name not in self._disabled

    def enable(self, name: str) -> None:
        self._disabled.discard(name)


    def disable(self, name: str) -> None:
        if name in self._tools:
            self._disabled.add(name)

    def enable_all(self) -> None:
        self._disabled.clear()


    def mark_discovered(self, name: str) -> None:
        self._discovered.add(name)

    def is_discovered(self, name: str) -> bool:
        return name in self._discovered


    def get_deferred_tool_names(self) -> list[str]:
        return [
            name
            for name, tool in self._tools.items()
            if getattr(tool, "should_defer", False)
            and name not in self._discovered
            and name not in self._disabled
        ]

    def search_deferred(
        self, query: str, max_results: int, protocol: str = "anthropic"
    ) -> list[dict[str, Any]]:
        query_lower = query.lower()
        scored: list[tuple[int, str, Tool]] = []
        for name, tool in self._tools.items():
            if not getattr(tool, "should_defer", False):
                continue
            if name in self._disabled:
                continue
            score = 0
            name_lower = name.lower()
            desc_lower = (tool.description or "").lower()
            if query_lower in name_lower:
                score += 10
            if query_lower in desc_lower:
                score += 5
            for word in query_lower.split():
                if word in name_lower:
                    score += 3
                if word in desc_lower:
                    score += 1
            if score > 0:
                scored.append((score, name, tool))
        scored.sort(key=lambda x: x[0], reverse=True)
        results: list[dict[str, Any]] = []
        for _, _name, tool in scored[:max_results]:
            base = tool.get_schema()
            if protocol in ("openai", "openai-compat"):
                results.append({
                    "type": "function",
                    "name": base["name"],
                    "description": base["description"],
                    "parameters": base["input_schema"],
                })
            else:
                results.append(base)
        return results

    def find_deferred_by_names(
        self, names: list[str], protocol: str = "anthropic"
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for name in names:
            if not self.is_enabled(name):
                continue
            tool = self._tools.get(name)
            if tool is None:
                continue
            if not getattr(tool, "should_defer", False):
                continue
            base = tool.get_schema()
            if protocol in ("openai", "openai-compat"):
                results.append({
                    "type": "function",
                    "name": base["name"],
                    "description": base["description"],
                    "parameters": base["input_schema"],
                })
            else:
                results.append(base)
        return results

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())


    def get_all_schemas(self, protocol: str = "anthropic") -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        for name, tool in self._tools.items():
            if name == "InvokeDeferredTool":
                continue
            if name in self._disabled:
                continue
            if getattr(tool, "should_defer", False) and name not in self._discovered:
                continue
            base = tool.get_schema()
            if protocol in ("openai", "openai-compat"):
                schemas.append({
                    "type": "function",
                    "name": base["name"],
                    "description": base["description"],
                    "parameters": base["input_schema"],
                })
            else:
                schemas.append(base)
        return schemas

    def get_context_schemas(
        self, protocol: str = "anthropic", context_channel: str = "generic"
    ) -> list[dict[str, Any]]:
        """Return the model-visible tool set for a context channel.

        DeepSeek sessions deliberately keep the provider-facing tool set fixed:
        deferred tools are reached through InvokeDeferredTool after ToolSearch has
        placed their names on the session allow-list.  Canonical JSON sorting and
        a defensive copy make the frozen schema byte-stable across requests.
        """
        if context_channel != "deepseek" or not self.is_enabled("InvokeDeferredTool"):
            return self.get_all_schemas(protocol)

        schemas: list[dict[str, Any]] = []
        for name, tool in self._tools.items():
            if name in self._disabled or getattr(tool, "should_defer", False):
                continue
            base = tool.get_schema()
            if protocol in ("openai", "openai-compat"):
                schema = {
                    "type": "function",
                    "name": base["name"],
                    "description": base["description"],
                    "parameters": base["input_schema"],
                }
            else:
                schema = base
            schemas.append(schema)

        schemas.sort(key=lambda schema: str(schema.get("name", "")))
        canonical = json.dumps(
            schemas, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return copy.deepcopy(json.loads(canonical))


# Codex-trained models call snake_case; MewCode schemas use PascalCase.
TOOL_NAME_ALIASES: dict[str, str] = {
    "WebSearch": "web_search",
    "web_search": "WebSearch",
    "Browser": "browser",
    "browser": "Browser",
    "WebFetch": "web_fetch",
    "web_fetch": "WebFetch",
}


def expand_tool_aliases(names: set[str]) -> set[str]:
    """Keep both PascalCase and Codex snake_case names on an allowlist."""
    if "*" in names:
        return names
    extra = {TOOL_NAME_ALIASES[name] for name in names if name in TOOL_NAME_ALIASES}
    return names | extra


def create_default_registry(file_cache: FileCache | None = None, file_history: Any = None) -> ToolRegistry:
    from mewcode.tools.bash import Bash
    from mewcode.tools.browser import Browser, BrowserAlias, WebFetch, WebFetchAlias
    from mewcode.tools.edit_file import EditFile
    from mewcode.tools.file_state_cache import FileStateCache
    from mewcode.tools.glob import Glob
    from mewcode.tools.grep import Grep
    from mewcode.tools.human_oncall import HumanOnCallTool
    from mewcode.tools.read_file import ReadFile
    from mewcode.tools.report_step import ReportStepTool
    from mewcode.tools.web_search import WebSearch, WebSearchAlias
    from mewcode.tools.write_file import WriteFile

    file_state_cache = FileStateCache()

    registry = ToolRegistry()
    from mewcode.tools.review_actions import ResolveAutoReview
    registry.register(ResolveAutoReview())
    from mewcode.tools.inspect_visual_evidence import InspectVisualEvidence
    registry.register(InspectVisualEvidence())
    registry.register(ReadFile(file_cache=file_cache, file_state_cache=file_state_cache))
    registry.register(WriteFile(file_cache=file_cache, file_history=file_history, file_state_cache=file_state_cache))
    registry.register(EditFile(file_cache=file_cache, file_history=file_history, file_state_cache=file_state_cache))
    registry.register(Bash())
    from mewcode.tools.terminal import TerminalSessions, TerminalStart, TerminalRead, TerminalInput
    from mewcode.tools.inspect_git_state import InspectGitState
    from mewcode.tools.run_verification import RunVerification
    terminals = TerminalSessions()
    registry.register(TerminalStart(terminals))
    registry.register(TerminalRead(terminals))
    registry.register(TerminalInput(terminals))
    registry.register(InspectGitState())
    registry.register(RunVerification())
    from mewcode.tools.runtime_doctor import RuntimeDoctor
    registry.register(RuntimeDoctor())
    from mewcode.tools.prepare_python_environment import PreparePythonEnvironment
    registry.register(PreparePythonEnvironment())
    registry.register(Glob())
    registry.register(Grep())
    registry.register(HumanOnCallTool())
    registry.register(ReportStepTool())
    search = WebSearch(browser_available=lambda: registry.get("browser_navigate") is not None and registry.get("browser_snapshot") is not None)
    registry.register(search)
    registry.register(WebSearchAlias(delegate=search))
    browser = Browser()
    registry.register(browser)
    registry.register(BrowserAlias(browser))
    registry.register(WebFetch(browser=browser))
    registry.register(WebFetchAlias(browser=browser))
    return registry

# Host-bound control tool, installed afresh for every orchestration role.
from mewcode.tools.request_assistance import RequestAssistance  # noqa: F401,E402
