import asyncio
from types import SimpleNamespace

import pytest

from frontend import server
from mewcode.conversation import ConversationManager
from mewcode.permissions import PermissionMode


class _Socket:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.messages.append(payload)


class _SessionHandle:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.messages = []
        self.meta = SimpleNamespace(
            title=session_id,
            total_tokens=0,
            save_meta=lambda: None,
        )

    def append(self, message) -> None:
        self.messages.append(message)

    def append_record(self, _record) -> None:
        return None

    def save_meta(self) -> None:
        return None

    def close(self) -> None:
        return None


class _Agent:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cache_read_tokens = 0
        self.total_cache_creation_tokens = 0
        self.cache_usage_available = False
        self.context_window = 200_000
        self.permission_mode = SimpleNamespace(value="auto")

    async def run(self, conversation):
        yield server.StreamText(self.answer)
        conversation.add_assistant_message(self.answer)
        yield server.TurnComplete(1)


@pytest.mark.asyncio
async def test_scoped_websocket_tags_stream_events() -> None:
    socket = _Socket()
    scoped = server.ConversationWebSocket(socket, "conversation-a")

    await scoped.send_json({"type": "delta", "text": "hello"})

    assert socket.messages == [{
        "type": "delta",
        "text": "hello",
        "conversation_id": "conversation-a",
    }]


@pytest.mark.asyncio
async def test_event_hub_reconnects_without_cancelling_stream_owner() -> None:
    hub = server.WebSocketEventHub()
    first = _Socket()
    second = _Socket()
    hub.connect(first)
    await server.ConversationWebSocket(hub, "conversation-a").send_json({"type": "delta", "text": "one"})
    hub.disconnect(first)
    hub.connect(second)
    await server.ConversationWebSocket(hub, "conversation-a").send_json({"type": "delta", "text": "two"})
    assert [message["text"] for message in first.messages] == ["one"]
    assert [message["text"] for message in second.messages] == ["two"]


@pytest.mark.asyncio
async def test_event_hub_drops_blocked_socket_without_stalling_healthy_client() -> None:
    class BlockedSocket:
        async def send_json(self, _payload: dict) -> None:
            await asyncio.Event().wait()

    hub = server.WebSocketEventHub(send_timeout=0.02)
    blocked = BlockedSocket()
    healthy = _Socket()
    hub.connect(blocked)
    hub.connect(healthy)

    await asyncio.wait_for(hub.send_json({"type": "interrupt_ack"}), timeout=0.2)

    assert healthy.messages == [{"type": "interrupt_ack"}]
    assert blocked not in hub._connections


def test_reconnect_replays_pending_runtime_permission() -> None:
    future = SimpleNamespace(done=lambda: False)
    runtime = SimpleNamespace(
        conversation_id="conversation-a", permission_future=future,
        permission_request={"tool": "Bash", "description": "run tests"},
        human_oncall_request=None, running=lambda: True,
    )
    session = SimpleNamespace(conversation_runs={"conversation-a": runtime})

    assert server.pending_runtime_prompts(session) == [{
        "type": "permission", "conversation_id": "conversation-a",
        "tool": "Bash", "description": "run tests",
    }]


@pytest.mark.asyncio
async def test_running_conversation_can_be_detached_and_reattached(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    owner = server.WebSession.__new__(server.WebSession)
    owner.context = SimpleNamespace(get=lambda key: {
        server.CONVERSATION_FACTORY: SimpleNamespace(
            create=lambda: ConversationManager(),
        ),
        server.SESSION_FACTORY: SimpleNamespace(
            create=lambda directory: server.SessionManager(str(directory)),
        ),
        server.TASK_FACTORY: SimpleNamespace(
            create=lambda *_args: SimpleNamespace(),
        ),
        server.TRACE_FACTORY: SimpleNamespace(
            create=lambda: SimpleNamespace(),
        ),
    }[key])
    session_handle = SimpleNamespace(
        session_id="conversation-a",
        meta=SimpleNamespace(title="并行对话"),
        close=lambda: (_ for _ in ()).throw(AssertionError("running session was closed")),
    )
    agent = SimpleNamespace(client=object())
    conversation = ConversationManager()
    blocker = asyncio.Event()
    task = asyncio.create_task(blocker.wait())
    runtime = server.ConversationRuntime(
        owner=owner,
        conversation_id="conversation-a",
        scope="project",
        work_dir=str(tmp_path),
        session=session_handle,
        conversation=conversation,
        agent=agent,
        client=agent.client,
        slot=None,
        effort="",
        context_tier=200_000,
        multi_agent=False,
        expert_mode=False,
        goal="",
        task_manager=SimpleNamespace(),
        trace_manager=SimpleNamespace(),
        agent_tool=None,
        worktree_manager=None,
        team_manager=None,
        task=task,
    )
    owner.session = session_handle
    owner.scope = "project"
    owner.work_dir = str(tmp_path)
    owner.project_sessions = server.SessionManager(str(tmp_path))
    owner.user_sessions = owner.project_sessions
    owner.conversation = conversation
    owner.agent = agent
    owner.client = agent.client
    owner.slot = None
    owner.worktree_manager = None
    owner.team_manager = None
    owner.chat_task = task
    owner.conversation_runs = {runtime.conversation_id: runtime}
    owner.web_state = {}
    monkeypatch.setattr(server, "_save_web_state", lambda _state: None)

    owner._release_active_conversation()
    assert owner.session is None
    assert owner.agent is None
    assert owner.chat_busy("conversation-a")

    restored = owner.load_conversation("project", "conversation-a")
    assert restored is not None
    assert restored["running"] is True
    assert owner.session is session_handle
    assert owner.agent is agent

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_starting_runtime_can_be_detached_before_task_attach(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    owner = server.WebSession.__new__(server.WebSession)
    owner.context = SimpleNamespace(get=lambda key: {
        server.CONVERSATION_FACTORY: SimpleNamespace(
            create=lambda: ConversationManager(),
        ),
        server.SESSION_FACTORY: SimpleNamespace(
            create=lambda directory: server.SessionManager(str(directory)),
        ),
        server.TASK_FACTORY: SimpleNamespace(
            create=lambda *_args: SimpleNamespace(),
        ),
        server.TRACE_FACTORY: SimpleNamespace(
            create=lambda: SimpleNamespace(),
        ),
    }[key])
    session_handle = SimpleNamespace(
        session_id="conversation-starting",
        meta=SimpleNamespace(title="启动中的对话"),
        close=lambda: (_ for _ in ()).throw(
            AssertionError("starting session was closed")
        ),
    )
    agent = SimpleNamespace(client=object())
    conversation = ConversationManager()
    runtime = server.ConversationRuntime(
        owner=owner,
        conversation_id=session_handle.session_id,
        scope="project",
        work_dir=str(tmp_path),
        session=session_handle,
        conversation=conversation,
        agent=agent,
        client=agent.client,
        slot=None,
        effort="",
        context_tier=200_000,
        multi_agent=False,
        expert_mode=False,
        goal="",
        task_manager=SimpleNamespace(),
        trace_manager=SimpleNamespace(),
        agent_tool=None,
        worktree_manager=None,
        team_manager=None,
        starting=True,
    )
    owner.session = session_handle
    owner.scope = "project"
    owner.work_dir = str(tmp_path)
    owner.project_sessions = server.SessionManager(str(tmp_path))
    owner.user_sessions = owner.project_sessions
    owner.conversation = conversation
    owner.agent = agent
    owner.client = agent.client
    owner.slot = None
    owner.worktree_manager = None
    owner.team_manager = None
    owner.chat_task = None
    owner.conversation_runs = {runtime.conversation_id: runtime}
    owner.web_state = {}
    monkeypatch.setattr(server, "_save_web_state", lambda _state: None)

    owner._release_active_conversation()
    assert owner.session is None
    assert runtime.task is None
    assert runtime.running() is True

    restored = owner.load_conversation("project", runtime.conversation_id)
    assert restored is not None
    assert restored["running"] is True
    assert owner.session is session_handle

    blocker = asyncio.Event()
    task = asyncio.create_task(blocker.wait())
    owner.attach_conversation_task(runtime, task)
    assert runtime.starting is False
    assert runtime.task is task

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_parallel_runs_persist_to_their_own_conversations(tmp_path) -> None:
    socket = _Socket()
    owner = SimpleNamespace(
        session_start=0.0,
        tasks_payload=lambda: [],
    )

    def runtime(conversation_id: str, answer: str) -> server.ConversationRuntime:
        agent = _Agent(answer)
        return server.ConversationRuntime(
            owner=owner,
            conversation_id=conversation_id,
            scope="project",
            work_dir=str(tmp_path),
            session=_SessionHandle(conversation_id),
            conversation=ConversationManager(),
            agent=agent,
            client=SimpleNamespace(effort=""),
            slot=None,
            effort="",
            context_tier=200_000,
            multi_agent=False,
            expert_mode=False,
            goal="",
            task_manager=SimpleNamespace(),
            trace_manager=SimpleNamespace(),
            agent_tool=None,
            worktree_manager=None,
            team_manager=None,
        )

    first = runtime("conversation-a", "answer-a")
    second = runtime("conversation-b", "answer-b")
    await asyncio.gather(
        server.run_chat(
            server.ConversationWebSocket(socket, first.conversation_id),
            "question-a",
            runtime=first,
        ),
        server.run_chat(
            server.ConversationWebSocket(socket, second.conversation_id),
            "question-b",
            runtime=second,
        ),
    )

    assert [message.content for message in first.session.messages] == [
        "question-a", "answer-a",
    ]
    assert [message.content for message in second.session.messages] == [
        "question-b", "answer-b",
    ]
    assert {message["conversation_id"] for message in socket.messages} == {
        "conversation-a", "conversation-b",
    }


@pytest.mark.asyncio
async def test_running_conversation_command_updates_only_target(tmp_path, monkeypatch) -> None:
    class CommandAgent:
        def __init__(self) -> None:
            self.permission_mode = PermissionMode.AUTO
            self.registry = SimpleNamespace(enable=lambda _name: None, disable=lambda _name: None)

        def set_permission_mode(self, mode) -> None:
            self.permission_mode = mode

    owner = server.WebSession.__new__(server.WebSession)
    owner.session = SimpleNamespace(session_id="visible")
    owner.permission_mode = PermissionMode.DEFAULT
    owner.conversation_settings = {}
    owner.web_state = {}
    blocker = asyncio.Event()
    task = asyncio.create_task(blocker.wait())
    runtime = server.ConversationRuntime(
        owner=owner, conversation_id="background", scope="project",
        work_dir=str(tmp_path), session=_SessionHandle("background"),
        conversation=ConversationManager(), agent=CommandAgent(), client=object(),
        slot=None, effort="xhigh", context_tier=200_000, multi_agent=False,
        expert_mode=False, goal="", task_manager=SimpleNamespace(),
        trace_manager=SimpleNamespace(), agent_tool=None, worktree_manager=None,
        team_manager=None, task=task,
    )
    owner.conversation_runs = {"background": runtime}
    monkeypatch.setattr(server, "_save_web_state", lambda _state: None)
    result = owner.execute_web_command_for("background", "plan", "下一步")
    assert result["ok"] is True
    assert result["prompt"] == "下一步"
    assert runtime.agent.permission_mode == PermissionMode.PLAN
    assert owner.permission_mode == PermissionMode.DEFAULT
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_interrupt_conversation_cancels_worker_and_clears_queue() -> None:
    owner = server.WebSession.__new__(server.WebSession)
    owner.session = SimpleNamespace(session_id="conversation-a")
    blocker = asyncio.Event()
    task = asyncio.create_task(blocker.wait())
    runtime = SimpleNamespace(
        running=lambda: not task.done(),
        task=task,
        cancel_requested=False,
        queued_prompts=["queued follow-up"],
        expert_control_action="pause",
    )
    owner.conversation_runs = {"conversation-a": runtime}

    result = owner.interrupt_conversation("conversation-a")

    assert result == {
        "conversation_id": "conversation-a",
        "status": "cancelling",
        "cancelled": True,
    }
    assert runtime.cancel_requested is True
    assert runtime.queued_prompts == []
    assert runtime.expert_control_action == "pause"
    with pytest.raises(asyncio.CancelledError):
        await task


def test_interrupt_conversation_recovers_renderer_when_no_worker_exists() -> None:
    owner = server.WebSession.__new__(server.WebSession)
    owner.session = SimpleNamespace(session_id="conversation-a")
    owner.conversation_runs = {}

    assert owner.interrupt_conversation("conversation-a") == {
        "conversation_id": "conversation-a",
        "status": "not_running",
        "cancelled": False,
    }


def test_interrupt_reserved_runtime_is_remembered_until_task_attach() -> None:
    owner = server.WebSession.__new__(server.WebSession)
    owner.session = SimpleNamespace(session_id="conversation-a")
    runtime = SimpleNamespace(
        running=lambda: True,
        task=None,
        starting=True,
        cancel_requested=False,
        queued_prompts=[],
        expert_control_action="pause",
    )
    owner.conversation_runs = {"conversation-a": runtime}

    result = owner.interrupt_conversation("conversation-a", action="terminate")

    assert result["status"] == "cancelled_before_start"
    assert runtime.cancel_requested is True
    assert runtime.expert_control_action == "terminate"


@pytest.mark.asyncio
async def test_stale_renderer_interrupt_publishes_terminal_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket = _Socket()
    hub = server.WebSocketEventHub()
    hub.connect(socket)
    fake_session = SimpleNamespace(
        interrupt_conversation=lambda _conversation_id, action="pause": {
            "conversation_id": "conversation-a",
            "status": "not_running",
            "cancelled": False,
        },
        tasks_payload=lambda: [],
    )
    monkeypatch.setattr(server, "SESSION", fake_session)
    monkeypatch.setattr(server, "WS_EVENTS", hub)
    monkeypatch.setattr(server, "EXPERT_RUNS", SimpleNamespace(
        list_for_conversation=lambda _conversation_id: [],
    ))

    result = await server.request_chat_interrupt("conversation-a")

    assert result["status"] == "not_running"
    assert socket.messages == [{
        "type": "done",
        "interrupted": True,
        "already_stopped": True,
        "elapsed": 0,
        "tasks": [],
        "conversation_id": "conversation-a",
    }]


@pytest.mark.asyncio
async def test_stale_expert_interrupt_reconciles_ghost_run_to_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket = _Socket()
    hub = server.WebSocketEventHub()
    hub.connect(socket)
    fake_session = SimpleNamespace(
        interrupt_conversation=lambda _conversation_id, action="pause": {
            "conversation_id": "conversation-a",
            "status": "not_running",
            "cancelled": False,
        },
        tasks_payload=lambda: [],
    )
    transitions: list[tuple[str, str, str]] = []
    fake_runs = SimpleNamespace(
        list_for_conversation=lambda _conversation_id: [{
            "run_id": "run-a",
            "status": "running",
            "legacy": False,
        }],
        transition=lambda run_id, status, reason="": transitions.append(
            (run_id, status, reason)
        ),
    )
    monkeypatch.setattr(server, "SESSION", fake_session)
    monkeypatch.setattr(server, "WS_EVENTS", hub)
    monkeypatch.setattr(server, "EXPERT_RUNS", fake_runs)

    result = await server.request_chat_interrupt("conversation-a")

    assert result["status"] == "not_running"
    assert result["expert_status"] == "paused"
    assert result["reconciled_expert_run_ids"] == ["run-a"]
    assert transitions == [(
        "run-a",
        "paused",
        "运行协程已不存在，用户暂停时已保存为可接力状态",
    )]
    assert socket.messages == [{
        "type": "done",
        "interrupted": True,
        "already_stopped": True,
        "elapsed": 0,
        "tasks": [],
        "expert_status": "paused",
        "run_ids": ["run-a"],
        "conversation_id": "conversation-a",
    }]
