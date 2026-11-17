r"""Loopback fixture for the real background-agent task UI and lifecycle APIs.

Run with::

    .\.venv\Scripts\python.exe tests\ui_task_lifecycle_fixture.py --host 127.0.0.1 --port 50741

All mutable application state is rooted under ``.tmp/ui-task-lifecycle-fixture``
by default. The real FastAPI routes and renderer are served unchanged; only the
application lifespan is replaced so this fixture does not scan providers,
probe models, or start external-agent watchers.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / ".tmp" / "ui-task-lifecycle-fixture"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mewcode.runtime_scheduler import RuntimeScheduler


_TASK_PREFIX = "UI lifecycle fixture · "
_server: Any | None = None
_current_generation: dict[str, Any] | None = None


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _prepare_isolation(data_dir: str | Path | None = None) -> Path:
    """Set up process-wide paths before importing frontend.server."""
    root = Path(data_dir).expanduser().resolve() if data_dir else FIXTURE_ROOT.resolve()
    allowed_root = (ROOT / ".tmp").resolve()
    if not _inside(root, allowed_root):
        raise ValueError(f"fixture data directory must be inside {allowed_root}")

    workspace = root / "workspace"
    private_home = root / "home"
    roaming = private_home / "AppData" / "Roaming"
    local = private_home / "AppData" / "Local"
    (root / "user-data").mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    private_home.mkdir(parents=True, exist_ok=True)
    roaming.mkdir(parents=True, exist_ok=True)
    local.mkdir(parents=True, exist_ok=True)
    fixture_config = workspace / ".mewcode" / "config.yaml"
    fixture_config.parent.mkdir(parents=True, exist_ok=True)
    if not fixture_config.exists():
        fixture_config.write_text(
            "providers:\n"
            "  - name: UI lifecycle fixture (unused)\n"
            "    protocol: openai\n"
            "    base_url: http://127.0.0.1:9/v1\n"
            "    model: fixture-unused\n"
            "    api_key: fixture-unused\n"
            "    usage:\n"
            "      enabled: false\n"
            "      account_tracking: false\n",
            encoding="utf-8",
        )
    os.environ["MEWCODE_UI_TASK_LIFECYCLE_DATA_DIR"] = str(root)
    os.environ["MEWCODE_USER_DATA_DIR"] = str(root / "user-data")
    os.environ["HOME"] = str(private_home)
    os.environ["USERPROFILE"] = str(private_home)
    os.environ["APPDATA"] = str(roaming)
    os.environ["LOCALAPPDATA"] = str(local)
    if private_home.drive:
        os.environ["HOMEDRIVE"] = private_home.drive
        os.environ["HOMEPATH"] = str(private_home)[len(private_home.drive):]
    # Keep the real inherited desktop token intact for desktop-boot/auth flows.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    os.chdir(workspace)
    return root


def _load_server(data_dir: str | Path | None = None):
    global _server
    root = _prepare_isolation(data_dir)
    if _server is None:
        _server = importlib.import_module("frontend.server")
    else:
        expected = str(root / "user-data")
        if Path(_server.USER_DATA_ROOT).resolve() != Path(expected).resolve():
            raise RuntimeError(
                "frontend.server is already loaded with a different fixture data directory"
            )
    return _server


class _FixtureAgent:
    """Tiny cancellable agent that uses a real scheduler lease, never a model."""

    def __init__(
        self,
        *,
        scheduler: RuntimeScheduler,
        name: str,
        session_id: str,
        store_dir: str,
    ) -> None:
        self.agent_id = f"ui-fixture-{uuid.uuid4().hex[:10]}"
        self.name = name
        self.scheduler = scheduler
        self.started = asyncio.Event()
        self._release = asyncio.Event()
        self.session_id = session_id
        self._conversation_scope = "project"
        self._conversation_store_dir = store_dir
        self.work_dir = store_dir
        self.team_name = ""
        self._team_manager = None
        self._cordis_runtime = None
        self.total_input_tokens = 17
        self.total_output_tokens = 9
        self.total_tool_calls = 1

    async def run_to_completion(self, *_args: Any, **_kwargs: Any) -> str:
        async with self.scheduler.slot(
            kind="model",
            label=self.name,
            session_id=self.session_id,
            agent_id=self.agent_id,
        ):
            self.started.set()
            await self._release.wait()
        return "Fixture task completed without invoking a model."


def _history_state(session_id: str, store_dir: str) -> dict[str, Any]:
    return {
        "version": 1,
        "tasks": [{
            "id": "ui-fixture-history-59m",
            "name": _TASK_PREFIX + "History sample · 59 min old",
            "task": "Legacy completed task used to verify stable historical duration rendering.",
            "status": "completed",
            "result": "Historical fixture result.",
            "elapsed": 3540.0,
            "duration_basis": "legacy_wall",
            "duration_quality": "unknown",
            "owner_session_id": session_id,
            "owner_scope": "project",
            "owner_store_dir": store_dir,
            "owner_task_id": "ui-fixture-parent-running",
            "owner_task_title": "Fixture parent: running sample",
            "origin": "fixture",
            "notified": True,
            "progress": {"input_tokens": 0, "output_tokens": 0, "tool_call_count": 0},
        }],
    }


async def initialize_fixture(data_dir: str | Path | None = None) -> dict[str, Any]:
    """Initialize two parent groups, with two Agents in the running group."""
    global _current_generation
    server = _load_server(data_dir)
    root = Path(os.environ["MEWCODE_UI_TASK_LIFECYCLE_DATA_DIR"])
    workspace = root / "workspace"
    session = server.SESSION

    # Reinitialization owns only fixture-named tasks in this isolated session.
    if _current_generation is not None:
        old_manager = _current_generation["manager"]
        old_ids = _current_generation["task_ids"]
        await old_manager.settle_tasks(old_ids, cancel=True, timeout=2.0)
    else:
        existing = session.task_manager
        fixture_ids = [
            task.id for task in existing.list_tasks()
            if task.name.startswith(_TASK_PREFIX) and task.status == "running"
        ]
        if fixture_ids:
            await existing.settle_tasks(fixture_ids, cancel=True, timeout=2.0)

    # Route ownership checks resolve against this fixture-only project session.
    session.work_dir = str(workspace)
    session.project_sessions = session.new_session_store(workspace)
    session.slots = []
    session.slot = None
    session.client = None
    session.agent = None
    session.new_conversation("project")
    session_id = session.session.session_id
    store_dir = session._conversation_store_dir()

    state_path = Path(server.BACKGROUND_TASKS_FILE)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(_history_state(session_id, store_dir), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    scheduler = RuntimeScheduler(limit=1, kind_limits={"model": 1})
    # The renderer's telemetry reads this same scheduler; TaskManager lifecycle
    # and queue state are still produced by their real implementations.
    server.APP_RUNTIME_SCHEDULER = scheduler
    manager = server.TaskManager(state_path)
    manager.runtime_scheduler = scheduler
    session.task_manager = manager

    active_agent = _FixtureAgent(
        scheduler=scheduler,
        name=_TASK_PREFIX + "Running sample · cancel me",
        session_id=session_id,
        store_dir=store_dir,
    )
    running_id = manager.launch(
        active_agent,
        "Hold one real local scheduler slot until cancelled from the UI.",
        name=active_agent.name,
        owner_session_id=session_id,
        owner_scope="project",
        owner_store_dir=store_dir,
        owner_task_id="ui-fixture-parent-running",
        owner_task_title="Fixture parent: running sample",
        origin="fixture",
    )
    await asyncio.wait_for(active_agent.started.wait(), timeout=3.0)

    queued_agent = _FixtureAgent(
        scheduler=scheduler,
        name=_TASK_PREFIX + "Same-group queued sample · cancel with group",
        session_id=session_id,
        store_dir=store_dir,
    )
    group_sibling_id = manager.launch(
        queued_agent,
        "Wait behind the running sibling in the same fixture parent group.",
        name=queued_agent.name,
        owner_session_id=session_id,
        owner_scope="project",
        owner_store_dir=store_dir,
        owner_task_id="ui-fixture-parent-running",
        owner_task_title="Fixture parent: running sample",
        origin="fixture",
    )

    async def queued(agent: _FixtureAgent) -> None:
        while scheduler.queue_position(agent_id=agent.agent_id) == 0:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(queued(queued_agent), timeout=3.0)

    other_group_agent = _FixtureAgent(
        scheduler=scheduler,
        name=_TASK_PREFIX + "Other-group queued sample · leave untouched",
        session_id=session_id,
        store_dir=store_dir,
    )
    queued_id = manager.launch(
        other_group_agent,
        "Wait for a scheduler slot under a separate fixture parent group.",
        name=other_group_agent.name,
        owner_session_id=session_id,
        owner_scope="project",
        owner_store_dir=store_dir,
        owner_task_id="ui-fixture-parent-queued",
        owner_task_title="Fixture parent: queued sample",
        origin="fixture",
    )
    await asyncio.wait_for(queued(other_group_agent), timeout=3.0)

    task_ids = [running_id, group_sibling_id, queued_id]
    task_id_roles = {
        "running_agent": running_id,
        "same_parent_queued_agent": group_sibling_id,
        "other_parent_queued_agent": queued_id,
        "legacy_history_task": "ui-fixture-history-59m",
    }
    host = os.environ.get("MEWCODE_UI_TASK_LIFECYCLE_HOST", "127.0.0.1")
    port = int(os.environ.get("MEWCODE_UI_TASK_LIFECYCLE_PORT", "50741"))
    display_host = f"[{host}]" if ":" in host else host
    manifest = {
        "base_url": f"http://{display_host}:{port}",
        "data_dir": str(root),
        "session_id": session_id,
        "running_task_id": running_id,
        "group_sibling_task_id": group_sibling_id,
        "queued_task_id": queued_id,
        "history_task_id": "ui-fixture-history-59m",
        "task_ids": task_id_roles,
        "task_id_list": list(task_id_roles.values()),
        "task_groups": [
            {
                "parent_task_id": "ui-fixture-parent-running",
                "title": "Fixture parent: running sample",
                "task_ids": [running_id, group_sibling_id, "ui-fixture-history-59m"],
                "cancellable_task_ids": [running_id, group_sibling_id],
            },
            {
                "parent_task_id": "ui-fixture-parent-queued",
                "title": "Fixture parent: queued sample",
                "task_ids": [queued_id],
                "cancellable_task_ids": [queued_id],
            },
        ],
        "parent_groups": [
            "Fixture parent: running sample",
            "Fixture parent: queued sample",
        ],
        "read_endpoints": ["/api/tasks", "/api/agent_tasks",
                            f"/api/tasks/{running_id}"],
        "cancel_endpoint": f"POST /api/tasks/{running_id}/cancel",
        "group_cancel_endpoint_template": "POST /api/tasks/{task_id}/cancel",
    }
    (root / "fixture-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _current_generation = {
        "manager": manager,
        "task_ids": task_ids,
        "scheduler": scheduler,
        "session_id": session_id,
    }
    return manifest


async def _stop_fixture() -> None:
    global _current_generation
    if _current_generation is None:
        return
    await _current_generation["manager"].settle_tasks(
        _current_generation["task_ids"], cancel=True, timeout=2.0
    )
    _current_generation = None


def _install_fixture_lifespan(server, data_dir: str | Path | None) -> None:
    @asynccontextmanager
    async def fixture_lifespan(_app):
        manifest = await initialize_fixture(data_dir)
        print(
            "UI task lifecycle fixture ready: "
            + json.dumps(manifest, ensure_ascii=False),
            flush=True,
        )
        try:
            yield
        finally:
            await _stop_fixture()

    # Preserve the production app, routes, middleware, and static JS. Replacing
    # only the lifespan prevents provider scans/probes and background watchers.
    server.app.router.lifespan_context = fixture_lifespan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=50741)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument(
        "--exit-on-stdin-eof",
        action="store_true",
        help="shut down this fixture cleanly when its parent closes stdin",
    )
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("the UI lifecycle fixture may bind only to a loopback host")
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")

    os.environ["MEWCODE_UI_TASK_LIFECYCLE_HOST"] = args.host
    os.environ["MEWCODE_UI_TASK_LIFECYCLE_PORT"] = str(args.port)
    os.environ["MEWCODE_BACKEND_PORT"] = str(args.port)
    server = _load_server(args.data_dir)
    _install_fixture_lifespan(server, args.data_dir)
    import uvicorn

    print(
        "MEWCODE_BACKEND_READY "
        + json.dumps({
            "url": f"http://{args.host}:{args.port}",
            "host": args.host,
            "port": args.port,
        }),
        flush=True,
    )

    config = uvicorn.Config(
        server.app,
        host=args.host,
        port=args.port,
        lifespan="on",
        log_level="info",
    )
    uvicorn_server = uvicorn.Server(config)

    async def serve() -> None:
        if args.exit_on_stdin_eof:
            loop = asyncio.get_running_loop()

            def watch_stdin() -> None:
                try:
                    while sys.stdin.read(1):
                        pass
                except (OSError, ValueError):
                    pass
                loop.call_soon_threadsafe(
                    setattr, uvicorn_server, "should_exit", True
                )

            threading.Thread(
                target=watch_stdin,
                name="ui-task-fixture-stdin-eof",
                daemon=True,
            ).start()
        await uvicorn_server.serve()

    asyncio.run(serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
