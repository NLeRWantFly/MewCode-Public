"""Process-wide kind-aware FIFO scheduler for expensive runtime work.

The scheduler is deliberately acquired at leaf operations (one model stream or
one concrete tool invocation).  Orchestrators never hold a permit while they
wait for child agents, which keeps nested expert/workflow execution deadlock
free.
"""

from __future__ import annotations

import asyncio
import inspect
import itertools
import time
from contextvars import ContextVar, Token
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterator, Mapping


RuntimeLoadCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


class ResourceQueueTimeout(RuntimeError):
    """Admission timed out before the serialized work was invoked."""


@dataclass
class _Waiter:
    sequence: int
    priority: int
    kind: str
    label: str
    session_id: str
    agent_id: str
    future: asyncio.Future[None]
    admitted_at: float | None = None
    resource_key: str = ""


@dataclass
class _ActiveLease:
    scheduler: Any
    owner: Any
    waiter: _Waiter | None


_ACTIVE_LEASE: ContextVar[_ActiveLease | None] = ContextVar("runtime_active_lease", default=None)


class RuntimeScheduler:
    """Cancellation-safe priority/FIFO semaphore with observable state."""

    def __init__(
        self,
        limit: int = 4,
        *,
        kind_limits: Mapping[str, int] | None = None,
    ) -> None:
        if limit < 1:
            raise ValueError("runtime concurrency limit must be positive")
        self.limit = limit
        self.kind_limits = {
            str(kind): int(kind_limit)
            for kind, kind_limit in (kind_limits or {}).items()
        }
        if any(kind_limit < 1 for kind_limit in self.kind_limits.values()):
            raise ValueError("runtime kind concurrency limits must be positive")
        self._running: dict[int, _Waiter] = {}
        self._waiters: list[_Waiter] = []
        self._sequence = itertools.count(1)
        self._lock = asyncio.Lock()
        self._callback: RuntimeLoadCallback | None = None
        self._agent_completed_seconds: dict[str, float] = {}
        self._agent_first_admitted: set[str] = set()

    def set_event_callback(self, callback: RuntimeLoadCallback | None) -> None:
        self._callback = callback

    async def configure(self, *, limit: int, kind_limits: Mapping[str, int]) -> None:
        """Apply an explicit host execution profile without dropping queued work."""
        if type(limit) is not int or not 1 <= limit <= 64:
            raise ValueError('runtime concurrency must be between 1 and 64')
        if any(type(value) is not int or not 1 <= value <= limit for value in kind_limits.values()):
            raise ValueError('runtime kind limits must be positive and fit the total limit')
        async with self._lock:
            self.limit = limit
            self.kind_limits = {str(key): value for key, value in kind_limits.items()}
            self._grant_locked()
            self._notify()

    def _ordered_waiters(self) -> list[_Waiter]:
        # Strict FIFO within each eligible lane. Usage refreshes use a separate
        # low-priority lane and never jump ahead of runnable programming work.
        return sorted(self._waiters, key=lambda item: (item.priority, item.sequence))

    def snapshot(self) -> dict[str, Any]:
        ordered = self._ordered_waiters()
        running_by_kind: dict[str, int] = {}
        for waiter in self._running.values():
            running_by_kind[waiter.kind] = running_by_kind.get(waiter.kind, 0) + 1
        queued = [
            {
                "position": index,
                "kind": waiter.kind,
                "label": waiter.label,
                "session_id": waiter.session_id,
                "agent_id": waiter.agent_id,
            }
            for index, waiter in enumerate(ordered, 1)
        ]
        return {
            "limit": self.limit,
            "kind_limits": dict(self.kind_limits),
            "running": len(self._running),
            "running_by_kind": running_by_kind,
            "queued": len(ordered),
            "queue": queued,
        }

    def queue_position(self, *, session_id: str = "", agent_id: str = "") -> int:
        for index, waiter in enumerate(self._ordered_waiters(), 1):
            if agent_id and waiter.agent_id == agent_id:
                return index
            if not agent_id and session_id and waiter.session_id == session_id:
                return index
        return 0

    def agent_runtime(self, agent_id: str) -> dict[str, Any]:
        """Count actual admitted model/tool time, excluding queue wait.

        This is a monotonic process-local measurement.  The task manager
        persists the last value for completed tasks and service restarts.
        """
        if not agent_id:
            return {
                "started": False,
                "active": False,
                "active_slots": 0,
                "elapsed": 0.0,
            }
        now = time.monotonic()
        active_waiters = [
            waiter for waiter in self._running.values()
            if waiter.agent_id == agent_id and waiter.admitted_at is not None
        ]
        active = sum(
            max(0.0, now - waiter.admitted_at)
            for waiter in active_waiters
        )
        return {"started": agent_id in self._agent_first_admitted,
                "active": bool(active_waiters),
                "active_slots": len(active_waiters),
                "elapsed": max(0.0, self._agent_completed_seconds.get(agent_id, 0.0) + active)}

    def _finish_agent_lease(self, waiter: _Waiter) -> None:
        if waiter.agent_id and waiter.admitted_at is not None:
            self._agent_completed_seconds[waiter.agent_id] = (
                self._agent_completed_seconds.get(waiter.agent_id, 0.0)
                + max(0.0, time.monotonic() - waiter.admitted_at)
            )

    def _notify(self) -> None:
        callback = self._callback
        if callback is None:
            return
        try:
            result = callback({"type": "runtime_load.updated", "runtime_load": self.snapshot()})
            if inspect.isawaitable(result):
                asyncio.create_task(result)
        except (RuntimeError, TypeError):
            # Observability must never make the scheduler unavailable.
            return

    def _can_run_locked(self, waiter: _Waiter) -> bool:
        if waiter.resource_key and any(
            active.resource_key == waiter.resource_key for active in self._running.values()
        ):
            return False
        kind_limit = self.kind_limits.get(waiter.kind)
        if kind_limit is None:
            return True
        running_for_kind = sum(
            1 for active in self._running.values() if active.kind == waiter.kind
        )
        return running_for_kind < kind_limit

    def _grant_locked(self) -> None:
        while len(self._running) < self.limit and self._waiters:
            self._waiters = [
                waiter for waiter in self._waiters if not waiter.future.cancelled()
            ]
            waiter = next(
                (
                    candidate
                    for candidate in self._ordered_waiters()
                    if self._can_run_locked(candidate)
                ),
                None,
            )
            if waiter is None:
                break
            self._waiters.remove(waiter)
            self._running[waiter.sequence] = waiter
            waiter.admitted_at = time.monotonic()
            if waiter.agent_id:
                self._agent_first_admitted.add(waiter.agent_id)
            if not waiter.future.done():
                waiter.future.set_result(None)

    async def acquire(
        self,
        *,
        kind: str,
        label: str = "",
        session_id: str = "",
        agent_id: str = "",
        priority: int = 0,
        resource_key: str = "",
    ) -> _Waiter:
        loop = asyncio.get_running_loop()
        waiter = _Waiter(
            sequence=next(self._sequence),
            priority=max(0, int(priority)),
            kind=str(kind),
            label=str(label)[:160],
            session_id=str(session_id),
            agent_id=str(agent_id),
            future=loop.create_future(),
            resource_key=resource_key,
        )
        async with self._lock:
            self._waiters.append(waiter)
            self._grant_locked()
            self._notify()
        try:
            await waiter.future
            return waiter
        except asyncio.CancelledError:
            async with self._lock:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
                elif waiter.sequence in self._running:
                    self._running.pop(waiter.sequence, None)
                    self._finish_agent_lease(waiter)
                    self._grant_locked()
                self._notify()
            raise

    async def release(self, waiter: _Waiter) -> None:
        async with self._lock:
            if self._running.pop(waiter.sequence, None) is not None:
                self._finish_agent_lease(waiter)
            self._grant_locked()
            self._notify()

    @asynccontextmanager
    async def slot(
        self,
        *,
        kind: str,
        label: str = "",
        session_id: str = "",
        agent_id: str = "",
        priority: int = 0,
        resource_key: str = "",
    ) -> Iterator[None]:
        waiter = await self.acquire(
            kind=kind,
            label=label,
            session_id=session_id,
            agent_id=agent_id,
            priority=priority,
            resource_key=resource_key,
        )
        lease = _ActiveLease(self, asyncio.current_task(), waiter)
        token = _ACTIVE_LEASE.set(lease)
        try:
            yield
        finally:
            try:
                if lease.waiter is not None:
                    await self.release(lease.waiter)
            finally:
                _ACTIVE_LEASE.reset(token)

    @asynccontextmanager
    async def serialized(
        self, resource_key: str, *, label: str = "", session_id: str = "",
        agent_id: str = "", wait_timeout: float = 300,
    ) -> Iterator[None]:
        """Upgrade a leaf tool permit to an exclusive resource permit.

        Return the ordinary permit before waiting: queue time is not execution
        time, and waiters cannot exhaust the tool lane needed by the owner.
        The replacement permit remains owned by the enclosing slot through
        tool completion/cleanup. Child tasks cannot borrow their parent's slot.
        """
        if not resource_key:
            raise ValueError("A serialized runtime resource needs an identity")
        lease = _ACTIVE_LEASE.get()
        borrowed = (lease is not None and lease.scheduler is self
                    and lease.owner is asyncio.current_task() and lease.waiter is not None)
        if borrowed:
            prior = lease.waiter
            if prior.resource_key == resource_key:
                yield
                return
            if prior.resource_key:
                raise RuntimeError("Cannot switch resources while holding a serialized permit")
            await self.release(prior)
            lease.waiter = None
            try:
                async with asyncio.timeout(max(0.001, wait_timeout)):
                    lease.waiter = await self.acquire(
                        kind=prior.kind, label=prior.label, session_id=prior.session_id,
                        agent_id=prior.agent_id, priority=prior.priority, resource_key=resource_key,
                    )
            except TimeoutError as error:
                raise ResourceQueueTimeout("Serialized runtime admission timed out") from error
            yield
        else:
            try:
                async with asyncio.timeout(max(0.001, wait_timeout)):
                    waiter = await self.acquire(kind="tool", label=label, session_id=session_id,
                                                agent_id=agent_id, resource_key=resource_key)
            except TimeoutError as error:
                raise ResourceQueueTimeout("Serialized runtime admission timed out") from error
            owned = _ActiveLease(self, asyncio.current_task(), waiter)
            token = _ACTIVE_LEASE.set(owned)
            try:
                yield
            finally:
                try:
                    if owned.waiter is not None:
                        await self.release(owned.waiter)
                finally:
                    _ACTIVE_LEASE.reset(token)


# Model inference and local/tool I/O have different bottlenecks.  Keep the
# existing four-model ceiling, but reserve an independent lane so a batch of
# long model streams cannot starve tool progress (and vice versa).
GLOBAL_RUNTIME_SCHEDULER = RuntimeScheduler(
    limit=8,
    kind_limits={"model": 4, "tool": 4, "usage": 1},
)

_CURRENT_RUNTIME_SCHEDULER: ContextVar[RuntimeScheduler | None] = ContextVar(
    "mewcode_runtime_scheduler",
    default=None,
)


def current_runtime_scheduler(
    fallback: RuntimeScheduler | None = None,
) -> RuntimeScheduler:
    """Return the scheduler owned by the active Cordis runtime scope."""

    return _CURRENT_RUNTIME_SCHEDULER.get() or fallback or GLOBAL_RUNTIME_SCHEDULER


def bind_runtime_scheduler(scheduler: RuntimeScheduler) -> Token[RuntimeScheduler | None]:
    return _CURRENT_RUNTIME_SCHEDULER.set(scheduler)


def runtime_scheduler_for_agent(
    agent: Any, fallback: RuntimeScheduler | None = None,
) -> RuntimeScheduler:
    """Read clocks/queues from the same scheduler bound by Agent.run().

    Observers run outside the child's ContextVar scope. Prefer its explicit
    owner, then the caller's pool; the observer's current scope is last resort.
    """
    owned = getattr(agent, "runtime_scheduler", None)
    if owned is not None:
        return owned
    return fallback if fallback is not None else current_runtime_scheduler()


def reset_runtime_scheduler(token: Token[RuntimeScheduler | None]) -> None:
    _CURRENT_RUNTIME_SCHEDULER.reset(token)
