"""Read-only diagnostics and explicit, provenance-preserving task retries."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
from pathlib import Path

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult


class StatusParams(BaseModel):
    pass


def _owned(agent, task) -> bool:
    return (task.owner_session_id == (getattr(agent, "session_id", "") or "")
            and task.owner_scope == (getattr(agent, "_conversation_scope", "") or "project")
            and Path(task.owner_store_dir).resolve() == Path(getattr(agent, "_conversation_store_dir", "") or agent.work_dir).resolve())


def _turn_id(agent) -> str:
    value = getattr(agent, "_chat_turn_id", "")
    return value if isinstance(value, str) else ""


def _task_observation(agent, manager, task, *, summary: bool = False) -> dict:
    payload = manager.task_payload(task.id, include_result=not summary)
    if summary:
        text = payload.pop("task", "")
        payload.update(task_summary=text[:240], task_summary_truncated=len(text) > 240,
                       detail_available=True)
    current = _turn_id(agent)
    baseline = getattr(agent, "_chat_turn_baseline_task_ids", frozenset())
    baseline = baseline if isinstance(baseline, (set, frozenset)) else frozenset()
    relation = ("current_turn" if current and task.dispatch_turn_id == current else
                "existing_before_turn" if current and task.id in baseline else
                "other_turn" if current and task.dispatch_turn_id else "unknown")
    return {**payload, "current_turn_id": current or None, "dispatch_relation": relation,
            "dispatch_note": {
                "current_turn": "宿主登记为本轮新派发。",
                "existing_before_turn": "本轮开始前已存在；本次读取不是重新派发。",
                "other_turn": "由其他轮次派发；本次读取不是重新派发。",
                "unknown": "缺少派发轮次证据，不得声称本轮派发。",
            }[relation]}


def _current_turn(agent, manager) -> dict:
    current = _turn_id(agent)
    return {"turn_id": current or None,
            "dispatched_task_ids": [t.id for t in manager.list_tasks()
                if current and t.dispatch_turn_id == current and _owned(agent, t)] if manager else [],
            "note": "只列宿主登记的本轮派发；历史任务清单不是本轮执行记录。"}


COMPACT_TASK_COLUMNS = ("id", "name", "status", "dispatch_relation",
                        "cancellation_pending", "cleanup_confirmed", "cleanup_failed",
                        "native_phase", "native_process_started",
                        "native_cleanup_confirmed", "native_exit_code")


def _compact_task_list(agent, manager, *, offset=0, budget_chars=9000) -> dict:
    observations = ([_task_observation(agent, manager, task, summary=True)
                     for task in manager.list_tasks() if _owned(agent, task)]
                    if manager is not None else [])
    result = {"view": "task_list_compact",
        "task_count": len(observations),
        "offset": offset,
        "returned": 0,
        "next_offset": None,
        "current_turn_id": _turn_id(agent) or None,
        "current_turn_task_ids": [obs["id"] for obs in observations
                                  if obs.get("dispatch_relation") == "current_turn"],
        "columns": list(COMPACT_TASK_COLUMNS),
        "tasks": [],
        "note": ("Compact list. Pass task_id for the full record, parent/owner IDs, "
                 "task text and result; use offset=next_offset for more.")}
    for obs in observations[offset:]:
        native = obs.get("native_execution")
        native = native if isinstance(native, dict) else {}
        row = [obs.get("id"), obs.get("name"), obs.get("status"),
               obs.get("dispatch_relation"), obs.get("cancellation_pending"),
               obs.get("cleanup_confirmed"), obs.get("cleanup_failed"),
               native.get("phase"), native.get("process_started"),
               native.get("cleanup_confirmed"), native.get("exit_code")]
        if result["tasks"] and len(json.dumps(
                {**result, "tasks": [*result["tasks"], row]},
                ensure_ascii=False)) > budget_chars:
            break
        result["tasks"].append(row)
    result["returned"] = len(result["tasks"])
    if offset + result["returned"] < len(observations):
        result["next_offset"] = offset + result["returned"]
    return result


def _ui_endpoint() -> dict:
    """Describe the backend's launch configuration, without guessing or probing."""
    host = os.environ.get("MEWCODE_BACKEND_HOST", "")
    port = os.environ.get("MEWCODE_BACKEND_PORT", "")
    url = None
    try:
        if not port.isascii() or not port.isdecimal() or not 0 < int(port) < 65536:
            raise ValueError("Unknown listener port")
        if host == "localhost":
            address = host
        else:
            ip = ipaddress.ip_address(host)
            if "%" in host:
                raise ValueError("Scoped addresses need explicit URL resolution")
            if ip.is_unspecified:
                ip = ipaddress.ip_address("::1" if ip.version == 6 else "127.0.0.1")
            address = f"[{ip}]" if ip.version == 6 else str(ip)
        url = f"http://{address}:{int(port)}/"
    except ValueError:
        pass
    return {"url": url, "source": "backend_listener_configuration", "live_verified": False,
            "note": "Current MewCode UI listener configuration; not a reachability check or authorization. "
                    "If unknown, do not infer a port. Navigation still requires its normal review."}


def _runtime_facts(agent) -> dict:
    """Allowlisted host configuration, not a probe or an upstream attestation.

    Read the current client on each call so a model switch cannot leave a stale
    route. Never stringify client/config objects, which may contain credentials.
    """
    client = getattr(agent, "client", None)

    def label(name):
        value = getattr(client, name, None)
        return value if isinstance(value, str) and value else None

    work_dir = getattr(agent, "work_dir", None)
    workspace = None
    if isinstance(work_dir, (str, Path)) and str(work_dir) and Path(work_dir).is_absolute():
        workspace = str(Path(work_dir).resolve())
    return {
        "host_work_dir": workspace,
        "ui_endpoint": _ui_endpoint(),
        "runtime_route": {
            "provider_name": label("_usage_provider_name"),
            "configured_model": label("model"),
            "effort": label("effort"),
            "source": "active_client_configuration",
            "live_verified": False,
        },
        "workspace_note": (
            "host_work_dir is the bound original workspace. AUTO PowerShell may "
            "report MewWork:\\ for a per-call staged copy. A logical drive name "
            "alone is not a workspace identity or authorization; check the host "
            "transaction binding. InspectGitState requires an explicit relative "
            "path when the target repository is a child directory."
        ),
    }


class AgentStatus(Tool):
    name = "AgentStatus"
    description = "Read bound host workspace, current MewCode UI endpoint, provider/model/effort configuration, enabled tools, permission mode and owned background tasks. Before opening MewCode's own UI, read ui_endpoint.url instead of assuming a default or historical port. Configuration fields are not live availability probes or authorization; missing fields remain unknown. Its tasks field is the first page of the compact task list; use BackgroundTasks for further pages or details."
    params_model = StatusParams
    category = "read"

    def __init__(self, agent, manager=None):
        self.agent, self.manager = agent, manager

    async def execute(self, params):
        registry = self.agent.registry
        tools = {tool.name: registry.is_enabled(tool.name) for tool in registry.list_tools()}
        model = getattr(getattr(self.agent, "client", None), "model", "")
        from mewcode.tools.capabilities import inventory
        capabilities = inventory(self.agent)
        # Compact the per-tool table for this view only; ProbeCapabilities keeps
        # the full row dictionaries from capabilities.inventory().
        capabilities["tool_columns"] = ["name", "enabled", "schema_loaded",
                                        "category", "connection"]
        capabilities["tools"] = [[row.get("name"), row.get("enabled"),
                                  row.get("schema_loaded"), row.get("category"),
                                  row.get("connection")]
                                 for row in capabilities["tools"]]
        capabilities["tool_row_note"] = ("registered=true and "
            "permission=checked_per_call for every row")
        observations = ([_task_observation(self.agent, self.manager, t, summary=True)
                         for t in self.manager.list_tasks() if _owned(self.agent, t)]
                        if self.manager else [])
        payload = {
            **_runtime_facts(self.agent),
            "permission_mode": getattr(self.agent.permission_mode, "value", str(self.agent.permission_mode)),
            "current_turn": _current_turn(self.agent, self.manager),
            "tasks": _compact_task_list(self.agent, self.manager, budget_chars=4000),
            "model_availability": "not_probed",
            "configured_model": model if isinstance(model, str) else "",
            "browser_connected": capabilities["browser_connected"],
            "browser_reconnect_available": tools.get("browser_session", False),
            "tools": tools,
            "capabilities": capabilities,
        }
        if len(json.dumps(payload, ensure_ascii=False)) > 9500:
            payload["capabilities"] = {"omitted_for_size": True,
                                       "tool_count": len(tools),
                                       "use_tool": "ProbeCapabilities"}
        host_observation = {key: payload[key] for key in
            ("host_work_dir", "ui_endpoint", "permission_mode", "runtime_route", "current_turn", "tasks")
            if key in payload}
        if "tasks" in host_observation:
            host_observation["tasks"] = [
                {key: value for key, value in task.items() if key != "task_summary"}
                for task in observations
            ]
        return ToolResult(json.dumps(payload, ensure_ascii=False),
                          host_observation=host_observation)


class BackgroundParams(BaseModel):
    task_id: str | None = None
    wait_seconds: float = Field(default=0, ge=0, le=30)
    acknowledge: bool = False
    offset: int = Field(default=0, ge=0)


class BackgroundTasks(Tool):
    name = "BackgroundTasks"
    description = ("List or read this conversation's child task results; optionally wait up to 30 seconds "
                   "without cancelling the child. parent_task_id is the host-recorded owner, never infer it "
                   "from conversation history. native_execution separates queued/preparing/starting/running/"
                   "cleaning/finished; native phase=running with process_started=true is live startup evidence, "
                   "not task status=running or Bash "
                   "alone. dispatch_relation distinguishes current-turn launches from historical tasks; "
                   "reading an old task never launches it again. Lists are compact column tables (see "
                   "columns) with task_count and current_turn_task_ids; page with offset=next_offset "
                   "until next_offset is null. Pass task_id for the full task record and result. "
                   "Acknowledge only after consuming a finished result.")
    params_model = BackgroundParams
    category = "read"
    consumes_runtime_slot = False

    def __init__(self, agent, manager):
        self.agent, self.manager = agent, manager

    async def execute(self, params):
        if self.manager is None:
            if not params.task_id:
                return ToolResult(json.dumps(
                    _compact_task_list(self.agent, None, offset=params.offset),
                    ensure_ascii=False), host_observation=[])
            return ToolResult("Task manager unavailable in this runtime.", is_error=True)
        if not params.task_id:
            observations = [_task_observation(self.agent, self.manager, t, summary=True)
                            for t in self.manager.list_tasks() if _owned(self.agent, t)]
            return ToolResult(json.dumps(
                    _compact_task_list(self.agent, self.manager, offset=params.offset,
                                       budget_chars=9000), ensure_ascii=False),
                host_observation=[{key: value for key, value in item.items()
                                   if key != "task_summary"} for item in observations])
        task = self.manager.get(params.task_id)
        if task is None or not _owned(self.agent, task):
            return ToolResult("Task not found in this conversation.", is_error=True)
        handle = self.manager._async_tasks.get(task.id)
        if handle is not None and params.wait_seconds:
            # asyncio.wait never propagates the timeout/caller cancellation to the child.
            await asyncio.wait({handle}, timeout=params.wait_seconds)
        if params.acknowledge and task.status != "running":
            self.manager.mark_notified([task.id])
        obs = _task_observation(self.agent, self.manager, task)
        return ToolResult(json.dumps(obs, ensure_ascii=False),
            host_observation={key: value for key, value in obs.items()
                              if key not in {"task", "result"}})


class RetryParams(BaseModel):
    task_id: str
    side_effects_checked: bool = Field(default=False, description="True only after checking partial changes and confirming rerunning this task will not duplicate unsafe side effects.")


class TaskRetry(Tool):
    name = "TaskRetry"
    description = "Explicitly restart a failed/cancelled named child task from its saved request, NOT an exact checkpoint continuation. Preserves original model selection and old record; refuses fork/team tasks, concurrent or duplicate retries. Check partial side effects first."
    params_model = RetryParams
    category = "harness"
    consumes_runtime_slot = False

    def __init__(self, agent, manager, agent_tool):
        self.agent, self.manager, self.agent_tool = agent, manager, agent_tool
        self._lock = asyncio.Lock()

    async def execute(self, params):
        from mewcode.tools.agent_tool import AgentToolParams, provider_route_fingerprint
        async with self._lock:
            old = self.manager.get(params.task_id)
            if old is None or not _owned(self.agent, old):
                return ToolResult("Task not found in this conversation.", is_error=True)
            if (not self.agent.registry.is_enabled("Agent")
                    or getattr(self.agent_tool, "_parent_agent", self.agent) is not self.agent):
                return ToolResult("Multi-agent is disabled. Enable it before retrying.", is_error=True)
            if old.status not in {"failed", "cancelled"} or old.retry_task_id or not old.recovery_spec:
                return ToolResult("Task is running, already retried, or has no replayable request. Inspect its result and create a new scoped task.", is_error=True)
            if old.id in self.manager._async_tasks:
                return ToolResult("Previous task cleanup has not finished.", is_error=True)
            if not params.side_effects_checked:
                return ToolResult("Inspect partial changes before explicitly retrying; no task was started.", is_error=True)
            spec = AgentToolParams.model_validate(old.recovery_spec)
            if not spec.provider_key:
                expected = old.recovery_spec.get("_route_fingerprint")
                actual = provider_route_fingerprint(getattr(self.agent_tool, "_provider_config", None))
                if not expected or expected != actual:
                    return ToolResult("Original provider route is unavailable or changed. Select it explicitly before creating a new scoped task.", is_error=True)
            if not spec.subagent_type or spec.team_name or spec.isolation:
                return ToolResult("Only named, non-team, non-worktree tasks can be retried.", is_error=True)
            result = await self.agent_tool.execute(spec)
            if not result.is_error:
                import re
                match = re.search(r"Task ID: ([a-f0-9]+)", result.output)
                if match:
                    old.retry_task_id = match.group(1)
                    self.manager._persist_state()
            return result
