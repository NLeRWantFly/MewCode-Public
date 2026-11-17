"""Conversation-scoped waiting-area and send-now behavior."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from frontend import server
from mewcode.conversation import ConversationManager
from mewcode.permissions import PermissionMode


def _owner_runtime(tmp_path, conversation_id: str = "chat-a"):
    session = SimpleNamespace(
        session_id=conversation_id,
        meta=SimpleNamespace(title="Queue test"),
        close=lambda: None,
    )
    owner = server.WebSession.__new__(server.WebSession)
    owner.session = session
    owner.chat_task = None
    owner.conversation_runs = {}
    owner.tasks_payload = lambda: []
    task = asyncio.create_task(asyncio.Event().wait())
    runtime = server.ConversationRuntime(
        owner=owner,
        conversation_id=conversation_id,
        scope="project",
        work_dir=str(tmp_path),
        session=session,
        conversation=ConversationManager(),
        agent=SimpleNamespace(permission_mode=PermissionMode.AUTO),
        client=object(),
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
    owner.conversation_runs[conversation_id] = runtime
    return owner, runtime, task


def _attachment(attachment_id: str = "a" * 32):
    metadata = {
        "id": attachment_id,
        "conversation_id": "chat-a",
        "name": "notes.md",
        "kind": "text",
        "size": 12,
        "preview_url": f"/api/chat/attachments/{attachment_id}",
    }
    return {
        "type": "attachment",
        "attachment_id": attachment_id,
        "attachment": metadata,
        **metadata,
    }


@pytest.mark.asyncio
async def test_queue_is_scoped_ordered_idempotent_and_removable(tmp_path):
    owner, runtime, task = _owner_runtime(tmp_path)
    try:
        first = owner.enqueue_chat_turn("chat-a", "queue-1", " first ", [_attachment()])
        second = owner.enqueue_chat_turn("chat-a", "queue-2", "second", [])
        replay = owner.enqueue_chat_turn("chat-a", "queue-1", "first", [_attachment()])

        assert [item["id"] for item in second["items"]] == ["queue-1", "queue-2"]
        assert second["items"][0]["attachments"][0]["name"] == "notes.md"
        assert replay["replayed"] is True
        assert owner.conversation_queues_payload()["chat-a"] == second["items"]
        assert owner.conversation_queue_revisions_payload()["chat-a"] == 2
        with pytest.raises(ValueError, match="不同内容"):
            owner.enqueue_chat_turn("chat-a", "queue-1", "changed", [])
        with pytest.raises(LookupError):
            owner.enqueue_chat_turn("chat-b", "queue-3", "foreign", [])

        payload, removed = owner.remove_queued_chat_turn("chat-a", "queue-1")
        assert removed.id == "queue-1"
        assert [item["id"] for item in payload["items"]] == ["queue-2"]
        assert runtime.running()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_send_now_prioritizes_one_item_preserves_rest_and_interrupts(tmp_path):
    owner, runtime, task = _owner_runtime(tmp_path)
    owner.enqueue_chat_turn("chat-a", "queue-1", "later", [])
    owner.enqueue_chat_turn("chat-a", "queue-2", "send this now", [])

    result = owner.prioritize_queued_chat_turn("chat-a", "queue-2")

    assert result["status"] == "cancelling"
    assert [item["id"] for item in result["items"]] == ["queue-2", "queue-1"]
    assert runtime.cancel_requested is True
    assert task.cancelled() or task.cancelling()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_manual_interrupt_clears_visible_waiting_area(tmp_path):
    owner, runtime, task = _owner_runtime(tmp_path)
    owner.enqueue_chat_turn("chat-a", "queue-1", "queued", [])

    result = owner.interrupt_conversation("chat-a")

    assert result["queue_cleared"] == 1
    assert runtime.chat_queue_payload() == []
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_dequeued_item_broadcasts_pop_before_reserved_launch(tmp_path, monkeypatch):
    owner, runtime, task = _owner_runtime(tmp_path)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    runtime.task = None
    runtime.starting = True
    item = server.QueuedChatTurn("queue-1", "next", (), ())
    runtime.queued_prompts = [server.QueuedChatTurn("queue-2", "after", (), ())]
    messages = []
    launched = []

    class Hub:
        async def send_json(self, payload):
            messages.append(payload)

    async def fake_start(target, text, **kwargs):
        launched.append((target, text, kwargs))

    monkeypatch.setattr(server, "WS_EVENTS", Hub())
    monkeypatch.setattr(server, "start_runtime_turn", fake_start)
    await server._start_dequeued_runtime_turn(runtime, item)

    assert messages == [{
        "type": "chat_queue",
        "items": [{"id": "queue-2", "text": "after", "attachments": [], "position": 1}],
        "revision": 0,
        "continuing": True,
        "conversation_id": "chat-a",
    }]
    assert launched[0][1] == "next"
    assert launched[0][2]["queued_turn"] is item
    assert launched[0][2]["_reserved"] is True


@pytest.mark.asyncio
async def test_visible_queue_auto_dispatches_every_turn_in_order(tmp_path, monkeypatch):
    owner, runtime, placeholder = _owner_runtime(tmp_path)
    runtime.expert_mode = True  # Saved mode must not send waiting-area tasks to an old graph.
    placeholder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await placeholder
    runtime.task = None
    gates = {name: asyncio.Event() for name in ("current", "first", "second")}
    started = []
    messages = []

    class Hub:
        async def send_json(self, payload):
            messages.append(payload)

    async def fake_run(_ws, text, *, content_blocks=None, runtime=None):
        started.append((text, content_blocks, runtime))
        await gates[text].wait()

    async def wait_until(predicate):
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(0)
        raise AssertionError("queue lifecycle did not advance")

    monkeypatch.setattr(server, "WS_EVENTS", Hub())
    monkeypatch.setattr(server, "run_chat", fake_run)
    await server.start_runtime_turn(runtime, "current")
    owner.enqueue_chat_turn("chat-a", "queue-1", "first", [])
    owner.enqueue_chat_turn("chat-a", "queue-2", "second", [])

    gates["current"].set()
    await wait_until(lambda: [item[0] for item in started] == ["current", "first"])
    assert runtime.chat_queue_payload() == [
        {"id": "queue-2", "text": "second", "attachments": [], "position": 1}
    ]
    gates["first"].set()
    await wait_until(
        lambda: [item[0] for item in started] == ["current", "first", "second"]
    )
    gates["second"].set()
    await wait_until(lambda: "chat-a" not in owner.conversation_runs)

    assert [item[0] for item in started] == ["current", "first", "second"]
    queued_starts = [
        item["text"] for item in messages
        if item.get("type") == "turn_started" and item.get("queued")
    ]
    assert queued_starts == ["current", "first", "second"]
    continuation_updates = [
        item for item in messages
        if item.get("type") == "chat_queue" and item.get("continuing")
    ]
    assert [len(item["items"]) for item in continuation_updates] == [1, 0]
    assert [item["revision"] for item in continuation_updates] == [3, 4]


@pytest.mark.asyncio
async def test_send_now_runs_selected_next_then_preserved_items(tmp_path, monkeypatch):
    owner, runtime, placeholder = _owner_runtime(tmp_path)
    runtime.expert_mode = True
    placeholder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await placeholder
    runtime.task = None
    started = []
    release_later = asyncio.Event()

    class Hub:
        async def send_json(self, _payload):
            return None

    async def fake_run(_ws, text, *, content_blocks=None, runtime=None):
        started.append(text)
        if text != "current":
            await release_later.wait()
        else:
            await asyncio.Event().wait()

    async def wait_until(predicate):
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(0)
        raise AssertionError("send-now lifecycle did not advance")

    monkeypatch.setattr(server, "WS_EVENTS", Hub())
    monkeypatch.setattr(server, "run_chat", fake_run)
    await server.start_runtime_turn(runtime, "current")
    await wait_until(lambda: started == ["current"])
    owner.enqueue_chat_turn("chat-a", "queue-1", "later", [])
    owner.enqueue_chat_turn("chat-a", "queue-2", "priority", [])

    result = owner.prioritize_queued_chat_turn("chat-a", "queue-2")
    assert result["status"] == "cancelling"
    await wait_until(lambda: started == ["current", "priority"])
    assert [item["text"] for item in runtime.chat_queue_payload()] == ["later"]

    release_later.set()
    await wait_until(lambda: started == ["current", "priority", "later"])
    await wait_until(lambda: "chat-a" not in owner.conversation_runs)


@pytest.mark.asyncio
async def test_dequeued_attachments_are_revalidated_and_materialized(tmp_path, monkeypatch):
    owner, runtime, placeholder = _owner_runtime(tmp_path)
    placeholder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await placeholder
    runtime.task = None
    messages = []
    calls = []

    class Hub:
        async def send_json(self, payload):
            messages.append(payload)

    class Attachments:
        def references(self, conversation_id, attachment_ids):
            calls.append(("references", conversation_id, tuple(attachment_ids)))
            return [{
                "id": "asset-1", "attachment_id": "asset-1",
                "kind": "text", "name": "note.txt",
            }]

        def materialize(self, refs):
            calls.append(("materialize", tuple(item["id"] for item in refs)))
            return [{"type": "text", "text": "attachment body"}]

        def display_refs(self, refs):
            return [{"id": item["id"], "name": item["name"]} for item in refs]

    async def fake_run(_ws, text, *, content_blocks=None, runtime=None):
        calls.append(("run", text, content_blocks, runtime.conversation_id))

    monkeypatch.setattr(server, "WS_EVENTS", Hub())
    monkeypatch.setattr(server, "ATTACHMENTS", Attachments())
    monkeypatch.setattr(server, "run_chat", fake_run)
    item = server.QueuedChatTurn(
        "queue-attachment", "inspect attachment", ("asset-1",),
        ({"id": "asset-1", "name": "note.txt"},),
    )

    await server.start_runtime_turn(runtime, item.text, queued_turn=item)
    task = runtime.task
    assert task is not None
    await task
    await asyncio.sleep(0)

    assert calls == [
        ("references", "chat-a", ("asset-1",)),
        ("materialize", ("asset-1",)),
        ("run", "inspect attachment", [{"type": "text", "text": "attachment body"}], "chat-a"),
    ]
    started = next(message for message in messages if message["type"] == "turn_started")
    assert started["attachments"] == [{"id": "asset-1", "name": "note.txt"}]


def _request_with_queue_header() -> Request:
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/api/chat/queue",
        "headers": [(b"x-mew-chat-queue", b"1")],
        "scheme": "http",
        "server": ("127.0.0.1", 80),
        "client": ("127.0.0.1", 1),
        "query_string": b"",
    })


@pytest.mark.asyncio
@pytest.mark.parametrize('main_done', [False, True])
async def test_send_now_with_only_background_work_dispatches_once(tmp_path, monkeypatch, main_done):
    from mewcode.workflow_core import goal_host

    owner, runtime, placeholder = _owner_runtime(tmp_path)
    owner.enqueue_chat_turn('chat-a', 'later', 'later', [])
    owner.enqueue_chat_turn('chat-a', 'priority', 'new instruction', [_attachment()])
    placeholder.cancel()
    await asyncio.gather(placeholder, return_exceptions=True)
    runtime.task = placeholder if main_done else None
    runtime.cancel_requested = True  # State left by the broken send-now path.
    runtime.last_send_now_id = 'priority'
    background = asyncio.create_task(asyncio.Event().wait())
    runtime._goal_run_tasks = {'graph': background}
    launched, interrupted = [], []

    def interrupt(store, cid, action):
        interrupted.append((cid, action))
        return 1  # A tool can still be saving; chat must not wait for it.

    async def launch(target, prompt):
        launched.append(prompt)

    monkeypatch.setattr(goal_host, 'interrupt', interrupt)
    monkeypatch.setattr(server, '_start_dequeued_runtime_turn', launch)
    try:
        body = server.ChatQueueItemBody(conversation_id='chat-a')
        monkeypatch.setattr(server, 'SESSION', owner)
        monkeypatch.setattr(server, '_publish_chat_queue', _noop_async)
        response = await server.api_chat_queue_send_now('priority', _request_with_queue_header(), body)
        assert response.status_code == 200
        result = json.loads(response.body)
        assert result['status'] == 'dispatching'
        assert result['revision'] == runtime.chat_queue_revision == 4
        assert owner.prioritize_queued_chat_turn('chat-a', 'priority')['status'] == 'already_requested'
        owner._continue_conversation_queue(runtime)  # Simultaneous graph callback.
        await asyncio.sleep(0)
        assert len(launched) == 1 and launched[0].id == 'priority'
        assert launched[0].attachment_ids == ('a' * 32,)
        assert interrupted == [('chat-a', 'pause')]
        assert [item.id for item in runtime.queued_prompts] == ['later']
        assert not runtime.cancel_requested and not background.cancelling()
    finally:
        background.cancel()
        await asyncio.gather(background, return_exceptions=True)


async def _noop_async(*args, **kwargs):
    pass


@pytest.mark.asyncio
async def test_send_now_repeat_during_cancellation_does_not_cancel_cleanup_again(tmp_path, monkeypatch):
    from mewcode.workflow_core import goal_host

    owner, runtime, task = _owner_runtime(tmp_path)
    interrupted = []
    monkeypatch.setattr(goal_host, 'interrupt', lambda *args: interrupted.append(args))
    owner.enqueue_chat_turn('chat-a', 'first', 'first', [])
    owner.enqueue_chat_turn('chat-a', 'priority', 'priority', [])
    try:
        assert owner.prioritize_queued_chat_turn('chat-a', 'priority')['status'] == 'cancelling'
        cancelled = task.cancelling()
        assert owner.prioritize_queued_chat_turn('chat-a', 'priority')['status'] == 'already_requested'
        assert task.cancelling() == cancelled == 1
        assert len(interrupted) == 1
        # Cancellation must not lock users out of adding/removing waiting messages.
        owner.enqueue_chat_turn('chat-a', 'third', 'third', [])
        owner.remove_queued_chat_turn('chat-a', 'third')
        assert [item.id for item in runtime.queued_prompts] == ['priority', 'first']
    finally:
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_manual_stop_background_reports_cancelling_and_preserves_no_queue(tmp_path, monkeypatch):
    from mewcode.workflow_core import goal_host

    owner, runtime, task = _owner_runtime(tmp_path)
    owner.enqueue_chat_turn('chat-a', 'queued', 'queued', [])
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    runtime.task = None
    background = asyncio.create_task(asyncio.Event().wait())
    runtime._goal_run_tasks = {'graph': background}
    monkeypatch.setattr(goal_host, 'interrupt', lambda *args: 1)
    try:
        result = owner.interrupt_conversation('chat-a')
        assert result['status'] == 'cancelling' and result['cancelled']
        assert not runtime.queued_prompts and runtime.cancel_requested
    finally:
        background.cancel()
        await asyncio.gather(background, return_exceptions=True)


@pytest.mark.asyncio
async def test_stale_main_cleanup_does_not_erase_send_now_reservation(tmp_path, monkeypatch):
    owner, runtime, task = _owner_runtime(tmp_path)
    owner.enqueue_chat_turn('chat-a', 'priority', 'priority', [])
    launched = []

    async def launch(target, prompt):
        launched.append(prompt.id)

    monkeypatch.setattr(server, '_start_dequeued_runtime_turn', launch)
    task.add_done_callback(lambda done: owner.prioritize_queued_chat_turn('chat-a', 'priority'))
    owner.attach_conversation_task(runtime, task)
    task.cancel()
    # Send-now runs after completion but before the old main cleanup callback.
    await asyncio.gather(task, return_exceptions=True)
    owner._continue_conversation_queue(runtime)
    await asyncio.sleep(0)
    assert launched == ['priority'] and runtime.starting


@pytest.mark.asyncio
async def test_queue_http_boundary_requires_header_and_active_running_chat(tmp_path, monkeypatch):
    owner, _runtime, task = _owner_runtime(tmp_path)
    messages = []

    class Hub:
        async def send_json(self, payload):
            messages.append(payload)

    monkeypatch.setattr(server, "SESSION", owner)
    monkeypatch.setattr(server, "WS_EVENTS", Hub())
    body = server.ChatQueueBody(
        conversation_id="chat-a", queue_id="http-1", text="follow up"
    )
    missing_header = Request({
        "type": "http", "method": "POST", "path": "/api/chat/queue",
        "headers": [], "scheme": "http", "server": ("127.0.0.1", 80),
        "client": ("127.0.0.1", 1), "query_string": b"",
    })
    try:
        forbidden = await server.api_chat_queue(missing_header, body)
        assert forbidden.status_code == 403
        accepted = await server.api_chat_queue(_request_with_queue_header(), body)
        assert accepted.status_code == 200
        assert json.loads(accepted.body)["accepted_id"] == "http-1"
        assert messages[-1]["type"] == "chat_queue"
        foreign = await server.api_chat_queue(
            _request_with_queue_header(), body.model_copy(update={"conversation_id": "chat-b"})
        )
        assert foreign.status_code == 409
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
