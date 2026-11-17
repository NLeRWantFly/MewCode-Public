from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from mewcode.sandbox.execution_progress import restored as restored_native_progress

if TYPE_CHECKING:
    from mewcode.agent import Agent

log = logging.getLogger(__name__)


@dataclass
class ProgressInfo:
    tool_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    last_activity: str = ""


class TaskCleanupError(RuntimeError):
    """Raised when a task handle ends without confirmed runtime cleanup."""


@dataclass
class BackgroundTask:
    id: str
    name: str
    agent: Agent | None
    task: str
    status: str = "running"
    result: str = ""
    start_time: float = field(default_factory=time.monotonic)
    end_time: float | None = None
    execution_elapsed_seconds: float | None = None
    duration_basis: str = "legacy_wall"
    duration_quality: str = "unknown"
    cancel: Callable[[], None] | None = None
    progress: ProgressInfo = field(default_factory=ProgressInfo)
    native_execution: dict = field(default_factory=dict)
    dispatch_turn_id: str = ""
    created_at: str = ""
    # teammate 专用：TUI 进度树与看门狗共用的实时进度对象
    teammate_progress: Any = None
    owner_session_id: str = ""
    owner_scope: str = "project"
    owner_store_dir: str = ""
    owner_task_id: str = ""
    owner_task_title: str = ""
    origin: str = "chat"
    notified: bool = False
    cancellation_pending: bool = False
    cleanup_confirmed: bool = False
    cleanup_error: str = ""
    lifecycle_finalized: bool = False
    recovery_spec: dict[str, Any] = field(default_factory=dict)
    retry_task_id: str = ""
    event_callback: Callable[[dict[str, Any]], None] | None = field(
        default=None, repr=False, compare=False
    )


class TaskManager:

    # teammate 空闲等待窗口（秒）。每次被唤醒执行 follow-up 后重新计时；
    # 超时无消息才退出——退出会同步置 is_active=False，之后 SendMessage
    # 会对该成员显式报错而不是静默丢信
    teammate_idle_timeout: float = 300.0

    # 看门狗：teammate 连续无事件超过此秒数时给 lead 发一次告警
    stall_warning_seconds: float = 180.0

    def __init__(self, state_path: str | Path | None = None) -> None:
        self._tasks: dict[str, BackgroundTask] = {}
        self._notify_queue: asyncio.Queue[str] = asyncio.Queue()
        self._queued_notifications: set[str] = set()
        self._async_tasks: dict[str, asyncio.Task[None]] = {}
        self._completion_futures: dict[str, asyncio.Future[None]] = {}
        self._state_path = Path(state_path) if state_path else None
        self.runtime_scheduler = None
        self._load_state()

    def _execution_elapsed(self, task: BackgroundTask) -> float:
        if task.duration_basis == "active_runtime":
            saved = max(0.0, float(task.execution_elapsed_seconds or 0.0))
            agent_id = str(getattr(task.agent, "agent_id", "") or "")
            if agent_id:
                from mewcode.runtime_scheduler import runtime_scheduler_for_agent
                scheduler = runtime_scheduler_for_agent(task.agent, self.runtime_scheduler)
                elapsed = scheduler.agent_runtime(agent_id)["elapsed"]
                saved = max(saved, elapsed)
                task.execution_elapsed_seconds = saved
            return saved
        return max((task.end_time or time.monotonic()) - task.start_time, 0.0)


    def launch(
        self,
        agent: Agent,
        task: str,
        name: str = "",
        fork_conversation: Any = None,
        progress: Any = None,
        owner_session_id: str = "",
        owner_scope: str = "",
        owner_store_dir: str = "",
        owner_task_id: str = "",
        owner_task_title: str = "",
        origin: str = "chat",
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        recovery_spec: dict[str, Any] | None = None,
        dispatch_turn_id: str = "",
        runner: Callable[[Callable[[dict[str, Any]], None]], Awaitable[str]] | None = None,
    ) -> str:
        task_id = uuid.uuid4().hex[:8]
        bg = BackgroundTask(
            id=task_id,
            name=name or task_id,
            agent=agent,
            task=task,
            teammate_progress=progress,
            owner_session_id=(
                owner_session_id or getattr(agent, "session_id", "") or ""
            ),
            owner_scope=(
                owner_scope
                or getattr(agent, "_conversation_scope", "")
                or "project"
            ),
            owner_store_dir=(
                owner_store_dir
                or getattr(agent, "_conversation_store_dir", "")
                or getattr(agent, "work_dir", "")
            ),
            owner_task_id=owner_task_id,
            dispatch_turn_id=dispatch_turn_id if isinstance(dispatch_turn_id, str) else "",
            created_at=datetime.now(timezone.utc).isoformat(),
            owner_task_title=owner_task_title[:200],
            origin=origin or "chat",
            event_callback=event_callback,
            recovery_spec=dict(recovery_spec or {}),
            execution_elapsed_seconds=0.0,
            duration_basis="active_runtime",
            duration_quality="measured",
        )
        self._tasks[task_id] = bg
        self._persist_state()

        async_task = asyncio.create_task(
            self._run_background(task_id, fork_conversation, runner=runner)
        )
        self._track_async_task(task_id, async_task)
        bg.cancel = async_task.cancel
        return task_id

    def _track_async_task(
        self, task_id: str, async_task: asyncio.Task[None]
    ) -> None:
        self._async_tasks[task_id] = async_task
        self._completion_futures[task_id] = asyncio.get_running_loop().create_future()
        async_task.add_done_callback(
            lambda done, tracked_id=task_id: self._on_async_task_done(
                tracked_id, done
            )
        )

    def _on_async_task_done(
        self, task_id: str, done: asyncio.Task[None]
    ) -> None:
        """Finalize tasks cancelled before their coroutine first runs.

        asyncio does not enter a coroutine's ``finally`` block if its task is
        cancelled before the first step.  The done callback is the host-owned
        completion path for that case; normal workers have already finalized
        before this callback runs.
        """
        bg = self._tasks.get(task_id)
        if bg is None or bg.lifecycle_finalized:
            return
        if done.cancelled():
            outcome = "cancelled"
            error = None
        else:
            error = done.exception()
            outcome = "failed"
            if error is None:
                error = RuntimeError(
                    "Background worker exited before lifecycle finalization."
                )
        try:
            asyncio.get_running_loop().create_task(
                self._finalize_task(task_id, outcome, error=error)
            )
        except RuntimeError:
            # Event loops should still be active while their child tasks are
            # being drained. If shutdown races this callback, keep the record
            # honest and avoid claiming a successful cancellation.
            bg.status = "failed"
            bg.result = bg.result or "Background task stopped before cleanup completed."
            bg.cancellation_pending = False
            bg.cleanup_confirmed = False
            bg.cleanup_error = "Task loop ended before cleanup could be confirmed."
            bg.end_time = time.monotonic()
            bg.lifecycle_finalized = True
            self._async_tasks.pop(task_id, None)
            self._queue_notification(task_id)
            self._persist_state()
            completion = self._completion_futures.pop(task_id, None)
            if completion is not None and not completion.done():
                completion.set_result(None)


    async def _run_background(
        self, task_id: str, fork_conversation: Any = None, *, runner=None
    ) -> None:
        bg = self._tasks.get(task_id)
        if bg is None:
            return
        if bg.agent is None:
            await self._finalize_task(
                task_id, "failed",
                error=RuntimeError("Background task has no live agent."),
            )
            return

        outcome = "completed"
        error: BaseException | None = None

        progress_callback = (
            self._make_progress_callback(bg.teammate_progress)
            if bg.teammate_progress is not None
            else None
        )
        callback: Callable[[dict[str, Any]], None] | None = bg.event_callback
        if progress_callback is not None and callback is not None:
            external_callback = callback
            def combined(event: dict[str, Any]) -> None:
                progress_callback(event)
                external_callback(event)
            callback = combined
        elif progress_callback is not None:
            callback = progress_callback
        previous_callback = callback
        def observe(event: dict[str, Any]) -> None:
            event_type = event.get("type", "")
            if event_type == "tool_use":
                bg.progress.tool_call_count += 1
                bg.progress.last_activity = str(event.get("toolName", "tool"))
            elif event_type == "usage":
                bg.progress.input_tokens = bg.agent.total_input_tokens
                bg.progress.output_tokens = bg.agent.total_output_tokens
            if event_type in {"tool_use", "usage"}:
                self._persist_state()
            if previous_callback is not None:
                previous_callback(event)
        callback = observe
        from mewcode.sandbox.execution_progress import observer
        def native_progress(facts):
            bg.native_execution = dict(facts)
            self._persist_state()
        native_token = observer.set(native_progress)
        try:
            if runner is not None:
                # Interactive child sessions use Agent.run while retaining the
                # same ownership, telemetry, cancellation and cleanup lifecycle.
                result = await runner(callback)
            elif bg.teammate_progress is not None:
                result = await self._run_with_watchdog(bg, callback, fork_conversation)
            elif fork_conversation is not None:
                result = await bg.agent.run_to_completion(
                    "", fork_conversation, event_callback=callback
                )
            else:
                result = await bg.agent.run_to_completion(
                    bg.task, event_callback=callback
                )
            bg.result = result
            # 注意：status 必须等常驻空闲循环彻底结束后才能置 completed——
            # cancel()/TaskStop 都以 status=='running' 为门，提前置位会让
            # idle/follow-up 阶段的 teammate 永远停不下来

            if bg.agent.team_name and bg.agent._team_manager:
                mailbox = bg.agent._team_manager.get_mailbox(bg.agent.team_name)
                lead_id = self._resolve_lead_id(bg.agent)
                if mailbox and lead_id:
                    from mewcode.teams.mailbox import create_message
                    wt_summary = self._worktree_summary(bg.agent)
                    content = f"[idle] {bg.name}: completed initial task"
                    if wt_summary:
                        content += f"\n[worktree]\n{wt_summary}"
                    msg = create_message(
                        from_agent=bg.name,
                        to_agent=lead_id,
                        content=content,
                        summary=f"{bg.name} idle",
                    )
                    mailbox.write(lead_id, msg)
                    await self._idle_loop(bg, mailbox, lead_id, callback)

                # teammate 退出前把完整对话落盘——lead/用户事后可审计它做了什么
                try:
                    conv = getattr(bg.agent, "_current_conversation", None)
                    if conv is not None:
                        from mewcode.teams.transcript import save_transcript
                        save_transcript(bg.agent.team_name, bg.agent.agent_id, conv)
                except Exception as e:
                    log.debug("save_transcript failed: %s", e)

                # 协程退出的瞬间就同步置 idle（set_member_idle 已幂等），
                # 不等 app 的 2 秒轮询——期间发来的消息会被存活校验拦截
                try:
                    bg.agent._team_manager.on_teammate_completed(bg.agent.agent_id)
                except Exception as e:
                    log.debug("on_teammate_completed failed: %s", e)

        except asyncio.CancelledError:
            outcome = "cancelled"
            if not bg.result:
                bg.result = "Task was cancelled"
        except Exception as e:
            log.error("Background task %s failed: %s", task_id, e)
            outcome = "failed"
            error = e
            failure = f"Error: {e}"
            bg.result = f"{bg.result}\n{failure}" if bg.result else failure
        finally:
            observer.reset(native_token)
            # 被 TaskStop/delete_team 取消或异常退出的 teammate 同样要立刻
            # 标记 idle（set_member_idle 幂等），否则 -p 模式（无 app 轮询）
            # 下 is_active 永远残留 True
            if bg.agent.team_name and bg.agent._team_manager:
                try:
                    bg.agent._team_manager.on_teammate_completed(bg.agent.agent_id)
                except Exception:
                    pass
            await self._finalize_task(task_id, outcome, error=error)

    async def _finalize_task(
        self,
        task_id: str,
        outcome: str,
        *,
        error: BaseException | None = None,
    ) -> None:
        """Run async cleanup, then publish one terminal task state."""
        bg = self._tasks.get(task_id)
        if bg is None or bg.lifecycle_finalized:
            return

        terminal_status = outcome
        agent = bg.agent
        bg.cleanup_confirmed = True
        if agent is not None:
            bg.progress.input_tokens = getattr(agent, "total_input_tokens", 0)
            bg.progress.output_tokens = getattr(agent, "total_output_tokens", 0)
            runtime = getattr(agent, "_cordis_runtime", None)
            try:
                if runtime is not None:
                    await runtime.aclose()
                    agent._cordis_runtime = None
            except asyncio.CancelledError:
                terminal_status = "failed"
                bg.cleanup_confirmed = False
                bg.cleanup_error = "Runtime cleanup was cancelled before completion."
                bg.result = (
                    f"{bg.result}\n{bg.cleanup_error}"
                    if bg.result else bg.cleanup_error
                )
                log.error("Background task %s cleanup was cancelled", task_id)
            except Exception as exc:
                terminal_status = "failed"
                bg.cleanup_confirmed = False
                bg.cleanup_error = f"Runtime cleanup failed: {exc}"
                bg.result = (
                    f"{bg.result}\n{bg.cleanup_error}"
                    if bg.result else bg.cleanup_error
                )
                log.exception("Background task %s cleanup failed", task_id)

            native_cleanup_error = getattr(agent, "_native_cleanup_error", "")
            if native_cleanup_error:
                terminal_status = "failed"
                bg.cleanup_confirmed = False
                bg.cleanup_error = f"Native runtime cleanup unconfirmed: {native_cleanup_error}"
                bg.result = f"{bg.result}\n{bg.cleanup_error}" if bg.result else bg.cleanup_error

        if terminal_status == "failed" and error is not None and not bg.result:
            bg.result = f"Error: {error}"
        elif terminal_status == "cancelled" and not bg.result:
            bg.result = "Task was cancelled"

        bg.progress.input_tokens = (
            getattr(agent, "total_input_tokens", bg.progress.input_tokens)
            if agent is not None else bg.progress.input_tokens
        )
        bg.progress.output_tokens = (
            getattr(agent, "total_output_tokens", bg.progress.output_tokens)
            if agent is not None else bg.progress.output_tokens
        )
        bg.end_time = time.monotonic()
        self._execution_elapsed(bg)
        bg.status = terminal_status
        bg.cancellation_pending = False
        bg.lifecycle_finalized = True
        if bg.teammate_progress is not None:
            bg.teammate_progress.status = (
                "stopped" if terminal_status == "cancelled" else terminal_status
            )
        self._async_tasks.pop(task_id, None)
        self._queue_notification(task_id)
        self._persist_state()
        completion = self._completion_futures.pop(task_id, None)
        if completion is not None and not completion.done():
            completion.set_result(None)


    @staticmethod
    def _make_progress_callback(progress: Any) -> Callable[[dict[str, Any]], None]:
        """把 run_to_completion 的事件流接到 TeammateProgress 上，
        TUI 进度树与看门狗都靠它。"""
        def _on_event(event: dict[str, Any]) -> None:
            etype = event.get("type")
            if etype == "tool_use":
                progress.record_tool_use(
                    event.get("toolName", ""), event.get("args", {})
                )
            elif etype == "usage":
                usage = event.get("usage", {})
                progress.record_tokens(
                    usage.get("inputTokens", 0), usage.get("outputTokens", 0)
                )
            elif etype == "stream_text":
                text = event.get("text")
                if text:
                    progress.note_message(text)
        return _on_event

    async def _run_with_watchdog(
        self,
        bg: BackgroundTask,
        callback: Callable[[dict[str, Any]], None] | None,
        fork_conversation: Any,
    ) -> str:
        """在旁路监视 teammate 的事件活动，长时间无动静时告警 lead。"""
        if fork_conversation is not None:
            coro = bg.agent.run_to_completion(
                "", fork_conversation, event_callback=callback
            )
        else:
            coro = bg.agent.run_to_completion(bg.task, event_callback=callback)
        inner = asyncio.create_task(coro)
        warned = False
        try:
            while True:
                done, _ = await asyncio.wait({inner}, timeout=15)
                if done:
                    return inner.result()
                progress = bg.teammate_progress
                stale = time.monotonic() - getattr(
                    progress, "last_event_at", time.monotonic()
                )
                if stale >= self.stall_warning_seconds:
                    if not warned:
                        warned = True
                        self._notify_lead_stall(bg, stale)
                else:
                    warned = False
        except asyncio.CancelledError:
            inner.cancel()
            # The child may still be closing its provider stream or native
            # commands. Do not finalize its scope while that cleanup is active.
            try:
                await inner
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                bg.agent._native_cleanup_error = f"Cancelled child execution cleanup failed: {exc}"
                log.exception("Cancelled child execution failed during cleanup")
            raise

    def _notify_lead_stall(self, bg: BackgroundTask, stale: float) -> None:
        try:
            mailbox = bg.agent._team_manager.get_mailbox(bg.agent.team_name)
            lead_id = self._resolve_lead_id(bg.agent)
            if mailbox and lead_id:
                from mewcode.teams.mailbox import create_message
                mailbox.write(lead_id, create_message(
                    from_agent=bg.name,
                    to_agent=lead_id,
                    content=(
                        f"[watchdog] {bg.name} has shown no activity for "
                        f"{int(stale)}s — it may be stuck. Use TaskStop "
                        f"with task id '{bg.id}' to stop it if needed."
                    ),
                    summary=f"{bg.name} may be stuck",
                    message_type="progress_report",
                ))
        except Exception as e:
            log.debug("stall notify failed: %s", e)

    async def _idle_loop(
        self,
        bg: BackgroundTask,
        mailbox: Any,
        lead_id: str,
        callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """teammate 的常驻空闲循环：事件驱动、唤醒后重置计时。

        - 普通消息：复用原对话上下文执行 follow-up（不再失忆重开）
        - shutdown_request：先做完同批次普通消息，再回执 shutdown_response
          优雅退出——关停请求不能吞掉别人刚派的任务
        - 运行期间被 _consume_mailbox 抢先消费的 shutdown_request 通过
          agent._shutdown_requested_by 标志位传到这里统一握手
        - 连续 teammate_idle_timeout 秒无消息：退出
        """
        from mewcode.teams.mailbox import create_message
        from mewcode.teams.registry import AgentNameRegistry

        def _ack_shutdown(requester: str) -> None:
            target = AgentNameRegistry.instance().resolve(requester) or lead_id
            mailbox.write(target, create_message(
                from_agent=bg.name,
                to_agent=target,
                content=f"{bg.name} acknowledged shutdown_request, going idle.",
                summary=f"{bg.name} shutdown ack",
                message_type="shutdown_response",
            ))
            bg.agent._shutdown_requested_by = None

        idle_since = time.monotonic()
        while time.monotonic() - idle_since < self.teammate_idle_timeout:
            pending = getattr(bg.agent, "_shutdown_requested_by", None)
            if pending:
                _ack_shutdown(pending)
                return

            await asyncio.sleep(1)
            msgs = mailbox.consume(bg.agent.agent_id)
            if not msgs:
                continue

            shutdown_req = next(
                (m for m in msgs if m.message_type == "shutdown_request"), None
            )
            normal = [m for m in msgs if m.message_type != "shutdown_request"]

            if normal:
                prompt = "\n\n".join(
                    f"[Message from {m.from_agent}] {m.content}" for m in normal
                )
                # 复用初始任务的对话（run_to_completion 会记录
                # _current_conversation），follow-up 才带着已加载的上下文干活
                conv = getattr(bg.agent, "_current_conversation", None)
                result = await bg.agent.run_to_completion(
                    prompt, conv, event_callback=callback
                )
                bg.result = result
                idle_since = time.monotonic()
                mailbox.write(lead_id, create_message(
                    from_agent=bg.name,
                    to_agent=lead_id,
                    content=f"[idle] {bg.name}: completed follow-up",
                    summary=f"{bg.name} idle",
                ))

            if shutdown_req is not None:
                _ack_shutdown(shutdown_req.from_agent)
                return

    @staticmethod
    def _worktree_summary(agent: Agent) -> str:
        """teammate 完成时给 lead 的产出摘要：未提交变更 + 最近提交。
        lead 据此决定收编（合并分支）还是丢弃，产出不再是黑盒。"""
        import subprocess
        wd = getattr(agent, "work_dir", "")
        if not wd:
            return ""
        parts: list[str] = []
        try:
            dirty = subprocess.run(
                ["git", "-C", wd, "status", "--porcelain"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if dirty:
                parts.append(f"uncommitted changes:\n{dirty[:1500]}")
            log_out = subprocess.run(
                ["git", "-C", wd, "log", "--oneline", "-5"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if log_out:
                parts.append(f"recent commits:\n{log_out}")
        except Exception:
            return ""
        return "\n".join(parts)

    @staticmethod
    def _resolve_lead_id(agent: Agent) -> str | None:
        """idle 通知的收件目录必须是真实的 lead_agent_id——
        字面量 "lead" 目录不会被 drain_lead_mailbox 消费。"""
        try:
            team = agent._team_manager.get_team(agent.team_name)
            return team.lead_agent_id if team else None
        except Exception:
            return None

    def adopt_running(
        self,
        agent: Agent,
        task_description: str,
        partial_result: str = "",
        name: str = "",
    ) -> str:
        task_id = uuid.uuid4().hex[:8]
        bg = BackgroundTask(
            id=task_id,
            name=name or task_id,
            agent=agent,
            task=task_description,
            result=partial_result,
            owner_session_id=getattr(agent, "session_id", "") or "",
            owner_scope=getattr(agent, "_conversation_scope", "") or "project",
            owner_store_dir=(
                getattr(agent, "_conversation_store_dir", "")
                or getattr(agent, "work_dir", "")
            ),
            owner_task_id=getattr(agent, "_chat_task_id", "") or "",
            owner_task_title=(
                getattr(agent, "_chat_task_title", "") or ""
            )[:200],
            execution_elapsed_seconds=0.0,
            duration_basis="active_runtime",
            duration_quality=(
                "measured" if getattr(agent, "agent_id", "") else "unknown"
            ),
        )
        self._tasks[task_id] = bg
        self._persist_state()

        async_task = asyncio.create_task(self._continue_background(task_id))
        self._track_async_task(task_id, async_task)
        bg.cancel = async_task.cancel
        return task_id


    async def _continue_background(self, task_id: str) -> None:
        bg = self._tasks.get(task_id)
        if bg is None:
            return
        if bg.agent is None:
            await self._finalize_task(
                task_id, "failed",
                error=RuntimeError("Background task has no live agent."),
            )
            return

        outcome = "completed"
        error: BaseException | None = None
        try:
            result = await bg.agent.run_to_completion(bg.task)
            bg.result = (bg.result + "\n" + result).strip() if bg.result else result
        except asyncio.CancelledError:
            outcome = "cancelled"
        except Exception as e:
            log.error("Background task %s failed: %s", task_id, e)
            outcome = "failed"
            error = e
            failure = f"Error: {e}"
            bg.result = f"{bg.result}\n{failure}" if bg.result else failure
        finally:
            await self._finalize_task(task_id, outcome, error=error)

    def get(self, task_id: str) -> BackgroundTask | None:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[BackgroundTask]:
        return list(self._tasks.values())

    def cancel(self, task_id: str) -> bool:
        bg = self._tasks.get(task_id)
        if bg is None or bg.status != "running":
            return False
        if bg.cancellation_pending:
            return True
        async_task = self._async_tasks.get(task_id)
        if async_task is None or async_task.done():
            return False
        bg.cancellation_pending = True
        cancelling = getattr(async_task, "cancelling", None)
        if callable(cancelling) and cancelling():
            self._persist_state()
            return True
        if not async_task.cancel():
            bg.cancellation_pending = False
            return False
        self._persist_state()
        return True

    async def settle_tasks(
        self, task_ids, *, cancel: bool = False, timeout: float | None = None
    ) -> list[str]:
        """Wait for task cleanup and return unfinished ids after an optional timeout.

        A cancellation request/status flag alone is not a writer handoff.
        Capture completion futures before cancel: cleanup removes task handles.
        """
        ids = list(dict.fromkeys(task_ids))
        current = asyncio.current_task()
        registered_handles = {
            task_id: self._async_tasks[task_id]
            for task_id in ids if task_id in self._async_tasks
        }
        current_ids = {
            task_id for task_id, handle in registered_handles.items()
            if handle is current
        }
        handles_by_id = {
            task_id: handle for task_id, handle in registered_handles.items()
            if handle is not current
        }
        handles = list(handles_by_id.values())
        completions = [
            self._completion_futures[task_id]
            for task_id in ids
            if task_id in self._completion_futures
            and task_id not in current_ids
        ]
        if cancel:
            for task_id in ids:
                if task_id in current_ids:
                    continue
                if not self.cancel(task_id):
                    handle = self._async_tasks.get(task_id)
                    if handle is not None and handle is not current and not handle.done():
                        cancelling = getattr(handle, "cancelling", None)
                        if not callable(cancelling) or not cancelling():
                            handle.cancel()
        waitables = list(completions) + handles
        if waitables:
            if timeout is None:
                await asyncio.gather(*(
                    asyncio.shield(waitable) for waitable in waitables
                ), return_exceptions=True)
            else:
                await asyncio.wait(
                    waitables, timeout=max(0.0, float(timeout))
                )
        if timeout is None:
            failures = [
                task_id for task_id in ids
                if (task := self._tasks.get(task_id)) is not None
                and task.lifecycle_finalized
                and not task.cleanup_confirmed
            ]
            if failures:
                details = "; ".join(
                    f"{task_id}: {self._tasks[task_id].cleanup_error or 'cleanup unconfirmed'}"
                    for task_id in failures
                )
                raise TaskCleanupError(
                    f"Task cleanup was not confirmed ({details})."
                )
        return [
            task_id for task_id in ids
            if (
                (task := self._tasks.get(task_id)) is not None
                and task.status == "running"
            )
            or (
                task_id in handles_by_id
                and not handles_by_id[task_id].done()
            )
        ]

    def poll_completed(self) -> list[BackgroundTask]:
        completed: list[BackgroundTask] = []
        while not self._notify_queue.empty():
            try:
                task_id = self._notify_queue.get_nowait()
                self._queued_notifications.discard(task_id)
                bg = self._tasks.get(task_id)
                if bg is not None and not bg.notified:
                    completed.append(bg)
            except asyncio.QueueEmpty:
                break
        return completed

    def requeue_notification(self, task_id: str) -> None:
        bg = self._tasks.get(task_id)
        if bg is not None and not bg.notified and bg.status != "running":
            self._queue_notification(task_id)

    def mark_notified(self, task_ids: list[str]) -> None:
        changed = False
        for task_id in task_ids:
            bg = self._tasks.get(task_id)
            if bg is not None and not bg.notified:
                bg.notified = True
                self._queued_notifications.discard(task_id)
                changed = True
        if changed:
            self._persist_state()

    def task_payload(self, task_id: str, include_result: bool = True) -> dict | None:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        elapsed = self._execution_elapsed(task)
        payload = {
            "id": task.id,
            "name": task.name,
            "task": task.task,
            "status": task.status,
            "cancellation_pending": task.cancellation_pending,
            "cleanup_confirmed": (
                task.cleanup_confirmed if task.lifecycle_finalized else None
            ),
            "cleanup_failed": (
                task.lifecycle_finalized and not task.cleanup_confirmed
            ),
            "cleanup_error": task.cleanup_error,
            "elapsed": (round(elapsed, 1) if task.duration_basis == "active_runtime"
                        and task.duration_quality != "unknown" else None),
            "duration_basis": task.duration_basis,
            "duration_quality": task.duration_quality,
            "legacy_wall_elapsed": (round(elapsed, 1)
                                    if task.duration_basis != "active_runtime" else None),
            "input_tokens": task.progress.input_tokens,
            "output_tokens": task.progress.output_tokens,
            "owner_session_id": task.owner_session_id,
            "owner_scope": task.owner_scope,
            "owner_store_dir": task.owner_store_dir,
            "owner_task_id": task.owner_task_id,
            "parent_task_id": task.owner_task_id,
            "dispatch_turn_id": task.dispatch_turn_id or None,
            "created_at": task.created_at or None,
            "native_execution": dict(task.native_execution),
            "owner_task_title": task.owner_task_title,
            "origin": task.origin,
            "notified": task.notified,
            "tool_call_count": task.progress.tool_call_count,
            "last_activity": task.progress.last_activity,
            "retry_available": bool(task.recovery_spec) and task.status in {"failed", "cancelled"} and not task.retry_task_id,
            "retry_task_id": task.retry_task_id,
        }
        if include_result:
            payload["result"] = task.result
        return payload

    def _queue_notification(self, task_id: str) -> None:
        if task_id in self._queued_notifications:
            return
        self._queued_notifications.add(task_id)
        self._notify_queue.put_nowait(task_id)

    def _load_state(self) -> None:
        if self._state_path is None:
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        items = data.get("tasks", []) if isinstance(data, dict) else []
        if not isinstance(items, list):
            return

        converted_running = False
        now = time.monotonic()
        for item in items[-100:]:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            status = str(item.get("status", "failed"))
            result = str(item.get("result", ""))
            if status == "running":
                status = "failed"
                result = (
                    result + "\n" if result else ""
                ) + "Task interrupted because the web service restarted."
                converted_running = True
            try:
                elapsed = max(float(item.get("elapsed", 0) or 0), 0)
            except (TypeError, ValueError):
                elapsed = 0
            progress_data = item.get("progress", {})
            if not isinstance(progress_data, dict):
                progress_data = {}
            try:
                input_tokens = int(progress_data.get("input_tokens", 0) or 0)
                output_tokens = int(progress_data.get("output_tokens", 0) or 0)
                tool_call_count = int(
                    progress_data.get("tool_call_count", 0) or 0
                )
            except (TypeError, ValueError):
                input_tokens = output_tokens = tool_call_count = 0
            active_clock = item.get("duration_basis") == "active_runtime"
            # Old split-scheduler records claim measured zero despite real work.
            # We cannot reconstruct that time from tokens or registration age.
            clock_missing = active_clock and (
                item.get("duration_quality") == "unknown"
                or (elapsed == 0 and max(input_tokens, output_tokens, tool_call_count) > 0)
            )
            task = BackgroundTask(
                id=str(item["id"]),
                name=str(item.get("name", item["id"])),
                agent=None,
                task=str(item.get("task", "")),
                status=status,
                result=result,
                start_time=now - elapsed,
                end_time=now,
                execution_elapsed_seconds=(elapsed if item.get("duration_basis") == "active_runtime" else None),
                duration_basis=("active_runtime" if item.get("duration_basis") == "active_runtime"
                                else "legacy_wall"),
                duration_quality=("unknown" if clock_missing else
                                  "lower_bound" if item.get("duration_basis") == "active_runtime"
                                  and (item.get("status") == "running"
                                       or item.get("duration_quality") == "lower_bound") else
                                  "measured" if item.get("duration_basis") == "active_runtime" else
                                  "unknown"),
                progress=ProgressInfo(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    tool_call_count=tool_call_count,
                    last_activity=str(progress_data.get("last_activity", "")),
                ),
                native_execution=restored_native_progress(item.get("native_execution")),
                owner_session_id=str(item.get("owner_session_id", "")),
                owner_scope=str(item.get("owner_scope", "project")),
                owner_store_dir=str(item.get("owner_store_dir", "")),
                owner_task_id=str(item.get("owner_task_id", "")),
                dispatch_turn_id=str(item.get("dispatch_turn_id") or ""),
                created_at=str(item.get("created_at") or ""),
                owner_task_title=str(item.get("owner_task_title", "")),
                origin=str(item.get("origin", "chat") or "chat"),
                notified=bool(item.get("notified", False)),
                cancellation_pending=False,
                cleanup_confirmed=bool(item.get("cleanup_confirmed", False)),
                cleanup_error=str(item.get("cleanup_error", "")),
                lifecycle_finalized=bool(item.get("lifecycle_finalized", False)),
                recovery_spec=item.get("recovery_spec", {}) if isinstance(item.get("recovery_spec"), dict) else {},
                retry_task_id=str(item.get("retry_task_id", "")),
            )
            self._tasks[task.id] = task
            if not task.notified:
                self._queue_notification(task.id)
        if converted_running:
            self._persist_state()

    def _persist_state(self) -> None:
        if self._state_path is None:
            return
        tasks = list(self._tasks.values())[-100:]
        payload = {
            "version": 1,
            "tasks": [
                {
                    "id": task.id,
                    "name": task.name,
                    "task": task.task,
                    "status": task.status,
                    "result": task.result[:100_000],
                    "elapsed": round(self._execution_elapsed(task), 3),
                    "duration_basis": task.duration_basis,
                    "duration_quality": task.duration_quality,
                    "cancellation_pending": task.cancellation_pending,
                    "cleanup_confirmed": task.cleanup_confirmed,
                    "cleanup_error": task.cleanup_error,
                    "lifecycle_finalized": task.lifecycle_finalized,
                    "progress": {
                        "tool_call_count": task.progress.tool_call_count,
                        "input_tokens": task.progress.input_tokens,
                        "output_tokens": task.progress.output_tokens,
                        "last_activity": task.progress.last_activity,
                    },
                    "native_execution": dict(task.native_execution),
                    "owner_session_id": task.owner_session_id,
                    "owner_scope": task.owner_scope,
                    "owner_store_dir": task.owner_store_dir,
                    "owner_task_id": task.owner_task_id,
                    "dispatch_turn_id": task.dispatch_turn_id,
                    "created_at": task.created_at,
                    "owner_task_title": task.owner_task_title,
                    "origin": task.origin,
                    "notified": task.notified,
                    "recovery_spec": task.recovery_spec,
                    "retry_task_id": task.retry_task_id,
                }
                for task in tasks
            ],
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self._state_path.with_suffix(
                self._state_path.suffix + ".tmp"
            )
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temp_path, self._state_path)
        except OSError as exc:
            log.warning("Failed to persist background tasks: %s", exc)
