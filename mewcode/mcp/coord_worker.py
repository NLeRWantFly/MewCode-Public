"""Always-on local worker that turns queued Broker tasks into Grok calls.

This is intentionally separate from the stdio MCP server.  The server remains
coordination-only; one worker process is responsible for starting external
Grok sessions and returning bounded evidence to the Broker.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
import uuid
from pathlib import Path
from typing import Any

from .coord_capabilities import grok_inspect_manifest, run_grok_inspect
from .coord_protocol import CoordinationError, EXTERNAL_EXECUTOR_MODEL_ID, TaskState
from .coord_server import project_root, state_directory, token_file
from .coord_store import CoordinationStore
from .external_model import (
    ExternalModelError,
    GrokCLIInvoker,
    ModelCallStatus,
    ModelInvocationRequest,
    ModelInvocationResult,
)

EXECUTOR_ID = "grok-executor-01"
EXECUTOR_MODEL = EXTERNAL_EXECUTOR_MODEL_ID
EXECUTOR_ROLE = "executor"


class _WorkerLock:
    """Non-blocking process lock for the fixed project worker."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            handle.close()
            raise CoordinationError(
                f"another MewCode coordinator worker is already running (lock: {self.path})"
            ) from exc
        self._file = handle

    def release(self) -> None:
        handle = self._file
        if handle is None:
            return
        self._file = None
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _acquire_worker_lock(state: Path) -> _WorkerLock:
    lock = _WorkerLock(state / ".worker.lock")
    lock.acquire()
    return lock


def task_prompt(task: dict[str, Any]) -> str:
    metadata = task.get("metadata") or {}
    explicit = metadata.get("prompt") if isinstance(metadata, dict) else None
    if isinstance(explicit, str) and explicit.strip():
        return explicit
    acceptance = task.get("acceptance_criteria") or []
    return (
        f"Task: {task.get('title', '')}\n"
        f"Description:\n{task.get('description', '')}\n"
        f"Acceptance criteria:\n{acceptance}\n\n"
        "Complete only this bounded task. Return a concise result and evidence."
    )


class GrokCoordWorker:
    """Poll, claim, execute, heartbeat, and submit Grok executor tasks."""

    def __init__(
        self,
        *,
        root: str | Path,
        store: CoordinationStore,
        invoker: GrokCLIInvoker,
        capabilities: dict[str, Any],
        poll_seconds: float = 2.0,
        heartbeat_seconds: float = 15.0,
        auth_check_seconds: float = 30.0,
        session_id: str | None = None,
        worker_lock: _WorkerLock | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.store = store
        self.invoker = invoker
        self.capabilities = capabilities
        self.poll_seconds = max(0.2, poll_seconds)
        self.heartbeat_seconds = max(1.0, heartbeat_seconds)
        self.auth_check_seconds = max(1.0, auth_check_seconds)
        self.session_id = session_id or f"grok-worker-{uuid.uuid4().hex[:12]}"
        self._registered_hash: str | None = None
        self._readiness: ModelInvocationResult | None = None
        self._last_auth_check = 0.0
        self._last_registration = 0.0
        self._worker_lock = worker_lock

    def close(self) -> None:
        """Release worker resources, including the process-level lock."""
        self.store.close()
        if self._worker_lock is not None:
            self._worker_lock.release()
            self._worker_lock = None

    @property
    def is_ready(self) -> bool:
        return self._readiness is not None and self._readiness.status is ModelCallStatus.COMPLETED

    def _refresh_readiness(self) -> bool:
        """Refresh the authentication state and report whether it changed."""
        now = time.monotonic()
        if self._readiness is not None and now - self._last_auth_check < self.auth_check_seconds:
            return False
        previous = self._readiness.status if self._readiness is not None else None
        # ``grok models`` refreshes the authenticated catalog and settings
        # before returning. On a cold start that can exceed the CLI's short
        # default while still being a healthy login, so give the preflight a
        # bounded but practical window.
        self._readiness = self.invoker.probe_auth(timeout_seconds=45)
        self._last_auth_check = now
        return previous is not self._readiness.status

    def _worker_metadata(self) -> dict[str, str]:
        if self._readiness is None:
            return {
                "runner": "mewcode-coord-worker",
                "provider_id": self.invoker.provider_id,
                "readiness": "unknown",
                "readiness_reason": "Grok authentication preflight has not completed",
            }
        status = self._readiness.status
        reason = {
            ModelCallStatus.AUTH_REQUIRED: "Grok authentication is required",
            ModelCallStatus.TIMEOUT: "Grok authentication preflight timed out",
            ModelCallStatus.FAILED: "Grok authentication preflight failed",
            ModelCallStatus.REJECTED: "Grok authentication preflight was rejected",
        }.get(status, "")
        return {
            "runner": "mewcode-coord-worker",
            "team_v2": "enabled" if self.store._teams_v2 is not None else "disabled",
            "provider_id": self.invoker.provider_id,
            "readiness": "ready" if status is ModelCallStatus.COMPLETED else status.value,
            "readiness_reason": reason,
        }

    def ensure_executor(self) -> dict[str, Any]:
        # The worker polls frequently.  Reusing a healthy registration avoids
        # rewriting the same capability snapshot and audit event on every poll.
        readiness_changed = self._refresh_readiness()
        if self._registered_hash:
            try:
                agent = self.store.get_agent(EXECUTOR_ID)
                snapshot = self.store.get_capabilities(EXECUTOR_ID)
                if (
                    not bool(agent.get("capabilities_stale"))
                    and agent.get("capabilities_hash") == self._registered_hash
                    and snapshot.get("capabilities_hash") == self._registered_hash
                ):
                    if readiness_changed or time.monotonic() - self._last_registration >= self.auth_check_seconds:
                        self.store.register_agent(
                            agent_id=EXECUTOR_ID,
                            model_id=EXECUTOR_MODEL,
                            role=EXECUTOR_ROLE,
                            session_id=self.session_id,
                            metadata=self._worker_metadata(),
                        )
                        self._last_registration = time.monotonic()
                    return snapshot
            except CoordinationError:
                self._registered_hash = None
        self.store.register_agent(
            agent_id=EXECUTOR_ID,
            model_id=EXECUTOR_MODEL,
            role=EXECUTOR_ROLE,
            session_id=self.session_id,
            metadata=self._worker_metadata(),
        )
        self._last_registration = time.monotonic()
        snapshot = self.store.sync_capabilities(agent_id=EXECUTOR_ID, manifest=self.capabilities, source=self.capabilities.get("source", "grok-worker"))
        self._registered_hash = str(snapshot["capabilities_hash"])
        return snapshot

    async def run_once(self) -> list[dict[str, Any]]:
        self.ensure_executor()
        tasks = self.store.list_tasks(
            states={TaskState.QUEUED.value},
            target_agent_id=EXECUTOR_ID,
            limit=10,
            before_deadline=True,
        )
        if not self.is_ready:
            # Keep queued work retryable after the user completes device login.
            # New tasks are rejected by the Store while this readiness state is set.
            return []
        results: list[dict[str, Any]] = []
        for queued in tasks:
            try:
                claimed = self.store.claim_task(task_id=str(queued["task_id"]), agent_id=EXECUTOR_ID)
            except CoordinationError:
                continue
            results.append(await self.execute_claimed(claimed))
        if self.store._teams_v2 is not None:
            from mewcode.teams.runtime_binding import GrokTeamExecutor
            executor = GrokTeamExecutor(self.store.teams_v2.runtime, self.invoker, root=self.root,
                                        session_id=self.session_id, heartbeat_seconds=self.heartbeat_seconds)
            results.extend(await executor.run_once())
        return results

    async def execute_claimed(self, task: dict[str, Any]) -> dict[str, Any]:
        task_id = str(task["task_id"])
        heartbeat_task: asyncio.Task[None] | None = None
        try:
            self.store.heartbeat(agent_id=EXECUTOR_ID, task_id=task_id, progress="Grok invocation started", progress_made=True)
            heartbeat_task = asyncio.create_task(self._heartbeat_loop(task_id))
            request = ModelInvocationRequest.from_task(
                task,
                prompt=task_prompt(task),
                cwd=self.root,
                reasoning_effort=str((task.get("metadata") or {}).get("reasoning_effort", "high")),
                timeout_seconds=min(120, int((task.get("metadata") or {}).get("timeout_seconds", 120))),
            )
            result = await self.invoker.invoke_async(request)
        except (ExternalModelError, CoordinationError) as exc:
            return self._mark_blocked(task, str(exc), status="rejected")
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

        if result.status is ModelCallStatus.COMPLETED:
            output = result.parsed_output
            if not isinstance(output, dict):
                output = {"text": result.stdout}
            completed = self.store.submit_result(
                task_id=task_id,
                agent_id=EXECUTOR_ID,
                result=output,
                evidence=result.as_evidence(),
            )
            self.store.post_message(
                task_id=task_id,
                sender_id=EXECUTOR_ID,
                recipient_id="codex-verifier-01",
                kind="result",
                payload={"task": completed, "evidence": result.as_evidence()},
            )
            return completed

        if result.status is ModelCallStatus.AUTH_REQUIRED:
            return self._mark_blocked(task, "Grok authentication is required", status=result.status.value, evidence=result.as_evidence())
        return self._mark_failed(task, result)

    async def _heartbeat_loop(self, task_id: str) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            try:
                self.store.heartbeat(agent_id=EXECUTOR_ID, task_id=task_id, progress="Grok invocation in progress", progress_made=True)
            except CoordinationError:
                return

    def _mark_blocked(self, task: dict[str, Any], reason: str, *, status: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        task_id = str(task["task_id"])
        payload = {"status": status, "reason": reason, "evidence": evidence or {}}
        updated = self.store.update_task(task_id=task_id, actor_id=EXECUTOR_ID, state=TaskState.BLOCKED.value, evidence=payload)
        self.store.post_message(task_id=task_id, sender_id=EXECUTOR_ID, recipient_id="codex-verifier-01", kind="blocked", payload=payload)
        return updated

    def _mark_failed(self, task: dict[str, Any], result: ModelInvocationResult) -> dict[str, Any]:
        payload = result.as_evidence()
        updated = self.store.update_task(task_id=str(task["task_id"]), actor_id=EXECUTOR_ID, state=TaskState.FAILED.value, evidence=payload)
        self.store.post_message(task_id=str(task["task_id"]), sender_id=EXECUTOR_ID, recipient_id="codex-verifier-01", kind="result", payload={"status": result.status.value, "evidence": payload})
        return updated

    async def run_forever(self) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(self.poll_seconds)


def _load_capabilities(executable: str, root: Path, grok_home: Path | None = None) -> dict[str, Any]:
    return run_grok_inspect(executable=executable, cwd=root, grok_home=grok_home)


def build_worker(*, root: Path | None = None, state_dir: Path | None = None, executable: str = "grok", grok_home: Path | None = None, poll_seconds: float = 2.0, heartbeat_seconds: float = 15.0, enable_team_v2: bool = False) -> GrokCoordWorker:
    fixed_root = (root or project_root()).resolve()
    if fixed_root != project_root():
        raise CoordinationError("worker root must be the fixed MewCode project")
    token_file(fixed_root)
    state = (state_dir or state_directory(fixed_root)).resolve()
    allowed_state = (fixed_root / ".mewcode" / "coordination").resolve()
    if state != allowed_state and allowed_state not in state.parents:
        raise CoordinationError("worker state directory must stay inside .mewcode/coordination")
    executable_path = executable
    selected_home = (
        Path(grok_home).expanduser().resolve()
        if grok_home is not None
        else Path(os.environ.get("MEWCODE_GROK_HOME", fixed_root / ".mewcode" / "grok-home")).expanduser().resolve()
    )
    worker_lock = _acquire_worker_lock(state)
    store: CoordinationStore | None = None
    try:
        capabilities = _load_capabilities(executable_path, fixed_root, selected_home)
        store = CoordinationStore(state / "coordination.sqlite3", state / "capabilities", enable_team_v2=enable_team_v2)
        invoker = GrokCLIInvoker(executable=executable_path, project_root=fixed_root, grok_home=selected_home)
        return GrokCoordWorker(root=fixed_root, store=store, invoker=invoker, capabilities=capabilities, poll_seconds=poll_seconds, heartbeat_seconds=heartbeat_seconds, worker_lock=worker_lock)
    except Exception:
        if store is not None:
            store.close()
        worker_lock.release()
        raise


async def _main_async(args: argparse.Namespace) -> None:
    worker = build_worker(root=args.root, state_dir=args.state_dir, executable=args.grok_executable, grok_home=args.grok_home, poll_seconds=args.poll_seconds, heartbeat_seconds=args.heartbeat_seconds, enable_team_v2=args.team_v2)
    try:
        if args.once:
            print(await worker.run_once())
        else:
            await worker.run_forever()
    finally:
        worker.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the always-on Grok executor for MewCode coordination tasks.")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--grok-executable", default=os.environ.get("MEWCODE_GROK_EXECUTABLE", "grok"))
    parser.add_argument("--grok-home", type=Path, default=None)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--heartbeat-seconds", type=float, default=15.0)
    parser.add_argument("--once", action="store_true", help="process currently queued tasks once and exit")
    parser.add_argument("--team-v2", action="store_true", help="opt in to the typed team runtime in the same single worker slot")
    args = parser.parse_args()
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
