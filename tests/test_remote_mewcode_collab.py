from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

import frontend.server as server
from mewcode.memory import SessionManager


def _client(monkeypatch) -> TestClient:
    monkeypatch.setattr(server, "DESKTOP_TOKEN", "desktop-test-token")
    monkeypatch.setattr(server, "_request_has_remote_token", lambda _request: True)
    return TestClient(server.app)


def test_remote_info_hides_external_agents(monkeypatch) -> None:
    client = _client(monkeypatch)
    payload = client.get("/api/remote/info").json()
    assert payload["capabilities"]["history"] is True
    assert payload["capabilities"]["send_prompt"] is True
    assert payload["capabilities"]["live_sync"] is True
    assert payload["capabilities"]["orchestration_live"] is True
    assert payload["capabilities"]["interrupt"] is True
    assert payload["capabilities"]["resume"] is True
    assert payload["capabilities"]["prompts"] is True
    assert payload["capabilities"]["external_agents"] is False


def test_remote_history_rejects_non_mewcode_source(monkeypatch) -> None:
    client = _client(monkeypatch)
    response = client.get("/api/remote/history", params={"source": "codex"})
    assert response.status_code == 400
    assert "mewcode" in response.json()["error"]


def test_remote_history_item_rejects_non_mewcode_source(monkeypatch) -> None:
    client = _client(monkeypatch)
    response = client.get(
        "/api/remote/history/item",
        params={"source": "claude-code", "external_id": "x"},
    )
    assert response.status_code == 400
    assert "mewcode" in response.json()["error"]


def test_remote_history_only_packs_mewcode_conversations(monkeypatch, tmp_path) -> None:
    dir_a = os.path.abspath(str(tmp_path / "alpha" / "proj"))
    dir_b = os.path.abspath(str(tmp_path / "beta" / "proj"))
    dir_empty = os.path.abspath(str(tmp_path / "empty"))
    sidebar = {
        "projects": [
            {
                "dir": dir_a,
                "label": "proj",
                "active": True,
                "conversations": [
                    {
                        "id": "project-1",
                        "title": "项目A 对话",
                        "last_active": "2026-09-15",
                        "message_count": 3,
                        "active": True,
                        "running": False,
                    }
                ],
            },
            {
                "dir": dir_b,
                "label": "proj",
                "active": False,
                "conversations": [
                    {
                        "id": "project-2",
                        "title": "同名项目B 对话",
                        "last_active": "2026-09-14",
                        "message_count": 2,
                        "active": False,
                        "running": False,
                    }
                ],
            },
            {"dir": dir_empty, "label": "empty", "active": False, "conversations": []},
        ],
        "normal": [
            {
                "id": "user-1",
                "title": "user 对话",
                "last_active": "2026-09-13",
                "message_count": 1,
                "active": False,
                "running": False,
            }
        ],
        "work_dir": dir_a,
    }
    monkeypatch.setattr(server.SESSION, "sidebar_payload", lambda: sidebar)
    monkeypatch.setattr(server.SESSION, "running_conversation_ids", lambda: set())
    monkeypatch.setattr(server, "_active_conversation_id", lambda: "project-1")
    client = _client(monkeypatch)
    payload = client.get("/api/remote/history").json()
    assert payload["items"]
    assert {item["source"] for item in payload["items"]} == {"mewcode"}
    assert payload["active_conversation_id"] == "project-1"
    assert all(item["controllable"] for item in payload["items"])
    projects = payload["projects"]
    assert [project["dir"] for project in projects] == [dir_a, dir_b, dir_empty]
    assert {project["id"] for project in projects} == {
        server._remote_project_id(dir_a),
        server._remote_project_id(dir_b),
        server._remote_project_id(dir_empty),
    }
    by_id = {item["external_id"]: item for item in payload["items"]}
    assert by_id["project-1"]["project_id"] == server._remote_project_id(dir_a)
    assert by_id["project-2"]["project_id"] == server._remote_project_id(dir_b)
    assert by_id["project-1"]["project_id"] != by_id["project-2"]["project_id"]
    assert by_id["project-2"]["project_path"] == dir_b
    assert by_id["project-2"]["parent_folder"] == dir_b
    assert by_id["user-1"]["project_id"] == ""
    assert by_id["user-1"]["parent_folder"] == "普通对话"


def test_remote_send_requires_conversation_id(monkeypatch) -> None:
    client = _client(monkeypatch)
    response = client.post("/api/remote/conversations/message", json={"text": "hello"})
    assert response.status_code == 400
    assert "conversation_id" in response.json()["error"]


def test_remote_send_queues_when_conversation_busy(monkeypatch) -> None:
    queued: dict = {}

    monkeypatch.setattr(
        server,
        "_activate_remote_mewcode_conversation",
        AsyncMock(side_effect=lambda conversation_id, scope_hint="", project_id="": {"id": conversation_id, "scope": "project"}),
    )
    monkeypatch.setattr(server.SESSION, "chat_busy", lambda _conversation_id: True)

    def fake_enqueue(conversation_id, queue_id, text, refs):
        queued.update({
            "conversation_id": conversation_id,
            "queue_id": queue_id,
            "text": text,
            "refs": refs,
        })
        return {
            "conversation_id": conversation_id,
            "accepted_id": queue_id,
            "items": [{"id": queue_id, "text": text}],
            "revision": 1,
        }

    async def fake_publish(_payload):
        return None

    monkeypatch.setattr(server.SESSION, "enqueue_chat_turn", fake_enqueue)
    monkeypatch.setattr(server, "_publish_chat_queue", fake_publish)
    client = _client(monkeypatch)
    response = client.post(
        "/api/remote/conversations/message",
        params={"conversation_id": "conv-1"},
        json={"text": "hello"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["queued"] is True
    assert payload["status"] == "queued"
    assert queued["text"] == "hello"
    assert queued["conversation_id"] == "conv-1"


def test_remote_activate_missing_conversation(monkeypatch) -> None:
    monkeypatch.setattr(server, "_activate_remote_mewcode_conversation", AsyncMock(return_value=None))
    client = _client(monkeypatch)
    response = client.post(
        "/api/remote/conversations/activate",
        json={"conversation_id": "missing"},
    )
    assert response.status_code == 404


def test_fanout_json_sink_copies_events_to_every_target() -> None:
    seen: list[tuple[str, dict]] = []

    class Target:
        def __init__(self, name: str) -> None:
            self.name = name

        async def send_json(self, payload: dict) -> None:
            seen.append((self.name, payload))

    sink = server.FanoutJsonSink(Target("phone"), Target("desktop"))
    asyncio.run(sink.send_json({"type": "delta", "text": "hi"}))
    assert [name for name, _payload in seen] == ["phone", "desktop"]
    assert seen[0][1]["text"] == "hi"


class _FakeRuns:
    def __init__(self, items: list[dict]) -> None:
        self.items = items

    def list_for_conversation(self, conversation_id: str) -> list[dict]:
        return [item for item in self.items if item.get("conversation_id") == conversation_id]

    def latest_resumable(self, conversation_id: str):
        from mewcode.expert_runs import is_resumable_snapshot
        return next(
            (item for item in self.list_for_conversation(conversation_id) if is_resumable_snapshot(item)),
            None,
        )

    def load(self, run_id: str):
        return next((item for item in self.items if item.get("run_id") == run_id), None)

    def public(self, run_id: str, *, role: str = ""):
        item = self.load(run_id)
        if item is None:
            return None
        return {"run_id": run_id, "events": list(item.get("events") or [])}

    def reject_recovery_revision(self, run_id: str) -> dict:
        item = self.load(run_id)
        if item is not None:
            item.pop("pending_recovery_revision", None)
        return item or {}


def _orch_snapshot(**overrides) -> dict:
    item = {
        "run_id": "run-1",
        "conversation_id": "conv-1",
        "status": "running",
        "task": "写完这一章",
        "locked_flow": {"nodes": [{"node_id": "work", "label": "执行"}]},
        "node_states": {
            "work": {"status": "running", "label": "执行", "summary": "正在写"},
        },
    }
    item.update(overrides)
    return item


def test_remote_live_event_ring_keeps_last_500() -> None:
    server.REMOTE_LIVES.pop("conv-cap", None)
    for index in range(501):
        server.record_remote_live_event("conv-cap", {"type": "expert_node_delta", "text": str(index)})
    events = server.REMOTE_LIVES["conv-cap"]
    assert len(events) == 500
    assert events[0]["text"] == "1"
    assert events[-1]["text"] == "500"


def test_remote_live_follows_desktop_started_orchestration(monkeypatch) -> None:
    server.REMOTE_LIVES.pop("conv-1", None)
    monkeypatch.setattr(server, "EXPERT_RUNS", _FakeRuns([_orch_snapshot()]))
    monkeypatch.setattr(server, "_live_mewcode_history_item", lambda cid: {"external_id": cid, "messages": []} if cid == "conv-1" else None)
    monkeypatch.setattr(server, "_disk_mewcode_history_item", lambda cid: None)
    monkeypatch.setattr(server.SESSION, "chat_busy", lambda cid: cid == "conv-1")
    monkeypatch.setattr(server.SESSION, "runtime_for", lambda cid: None)
    monkeypatch.setattr(server, "pending_runtime_prompts", lambda _session: [])
    server.record_remote_live_event("conv-1", {
        "type": "expert_node_delta",
        "node_id": "work",
        "label": "执行",
        "text": "正在写",
        "run_id": "run-1",
    })
    client = _client(monkeypatch)
    payload = client.get("/api/remote/live", params={"conversation_id": "conv-1"}).json()
    assert payload["conversation_id"] == "conv-1"
    assert payload["run"]["run_id"] == "run-1"
    assert payload["execution_kind"] == "orchestration"
    assert payload["steps"]
    assert payload["steps"][0]["label"] == "执行"
    assert any("正在写" in str(event.get("text") or event.get("summary") or "") for event in payload["events"])


def test_remote_live_shows_parallel_members_and_tools(monkeypatch) -> None:
    server.REMOTE_LIVES.pop("conv-1", None)
    snapshot = _orch_snapshot(
        locked_flow={"nodes": [
            {"node_id": "research", "label": "调研"},
            {"node_id": "write", "label": "执笔"},
        ]},
        node_states={
            "research": {
                "status": "running",
                "label": "调研",
                "summary": "查资料",
                "tools": {
                    "t1": {
                        "tool_id": "t1",
                        "name": "web_search",
                        "args": {"query": "ssh"},
                        "status": "completed",
                        "output": "ok",
                    }
                },
            },
            "write": {"status": "running", "label": "执笔", "summary": "在写"},
        },
        members={
            "research::alice": {
                "member_id": "alice",
                "node_id": "research",
                "name": "Alice",
                "status": "running",
                "text": "我在搜端口",
            },
            "write::bob": {
                "member_id": "bob",
                "node_id": "write",
                "name": "Bob",
                "status": "running",
                "text": "我在写报告",
                "tools": {
                    "t2": {
                        "tool_id": "t2",
                        "name": "Read",
                        "args": {"path": "/tmp/a"},
                        "status": "completed",
                        "output": "file",
                    }
                },
            },
        },
        active_node_ids=["research", "write"],
    )
    monkeypatch.setattr(server, "EXPERT_RUNS", _FakeRuns([snapshot]))
    monkeypatch.setattr(server, "_live_mewcode_history_item", lambda cid: {"external_id": cid, "messages": []} if cid == "conv-1" else None)
    monkeypatch.setattr(server, "_disk_mewcode_history_item", lambda cid: None)
    monkeypatch.setattr(server.SESSION, "chat_busy", lambda cid: cid == "conv-1")
    monkeypatch.setattr(server.SESSION, "runtime_for", lambda cid: None)
    monkeypatch.setattr(server, "pending_runtime_prompts", lambda _session: [])
    client = _client(monkeypatch)
    payload = client.get("/api/remote/live", params={"conversation_id": "conv-1"}).json()
    running = [step for step in payload["steps"] if step.get("status") == "running"]
    assert len(running) >= 2
    labels = {step["label"] for step in payload["steps"]}
    assert "Alice" in labels
    assert "Bob" in labels
    texts = " ".join(str(event.get("text") or "") for event in payload["events"])
    assert "我在搜端口" in texts
    assert "我在写报告" in texts
    names = {event.get("name") for event in payload["events"]}
    assert "web_search" in names
    assert "Read" in names
    assert payload["run"]["active_node_ids"] == ["research", "write"]


def test_remote_live_includes_chat_tool_events(monkeypatch) -> None:
    server.REMOTE_LIVES.pop("conv-tool", None)
    monkeypatch.setattr(server, "EXPERT_RUNS", _FakeRuns([]))
    monkeypatch.setattr(server, "_live_mewcode_history_item", lambda cid: None)
    monkeypatch.setattr(server, "_disk_mewcode_history_item", lambda cid: None)
    monkeypatch.setattr(server.SESSION, "chat_busy", lambda cid: False)
    monkeypatch.setattr(server.SESSION, "runtime_for", lambda cid: None)
    monkeypatch.setattr(server, "pending_runtime_prompts", lambda _session: [])
    server.record_remote_live_event("conv-tool", {
        "type": "tool",
        "id": "tool-1",
        "name": "Bash",
        "args": {"command": "ping 10.108.17.54"},
    })
    server.record_remote_live_event("conv-tool", {
        "type": "tool_result",
        "id": "tool-1",
        "output": "ttl=64",
        "is_error": False,
    })
    client = _client(monkeypatch)
    payload = client.get("/api/remote/live", params={"conversation_id": "conv-tool"}).json()
    assert any(event.get("type") == "tool" and event.get("name") == "Bash" for event in payload["events"])
    assert any(event.get("type") == "tool_result" and "ttl=64" in str(event.get("output") or "") for event in payload["events"])


def test_remote_history_item_keeps_speech_only(monkeypatch) -> None:
    monkeypatch.setattr(
        server,
        "_live_mewcode_history_item",
        lambda cid: {
            "external_id": cid,
            "messages": [
                {"role": "user", "text": "hi", "kind": "message"},
                {"role": "assistant", "text": "stage", "kind": "expert_node"},
            ],
        } if cid == "conv-1" else None,
    )
    monkeypatch.setattr(server, "_disk_mewcode_history_item", lambda cid: None)
    client = _client(monkeypatch)
    payload = client.get(
        "/api/remote/history/item",
        params={"source": "mewcode", "external_id": "conv-1", "speech_only": True},
    ).json()
    assert payload["messages"]
    assert all(str(item.get("kind") or "message") == "message" for item in payload["messages"])


def test_remote_interrupt_activates_and_pauses(monkeypatch) -> None:
    active = {"id": "other"}
    runs = _FakeRuns([_orch_snapshot(status="running")])
    monkeypatch.setattr(server, "EXPERT_RUNS", runs)
    monkeypatch.setattr(server, "_active_conversation_id", lambda: active["id"])
    monkeypatch.setattr(
        server,
        "_activate_remote_mewcode_conversation",
        AsyncMock(side_effect=lambda cid, scope="", project_id="": active.__setitem__("id", cid) or {"id": cid, "scope": "project"}),
    )

    async def fake_interrupt(cid, *, action="pause"):
        assert action == "pause"
        runs.items[0]["status"] = "paused"
        return {"conversation_id": cid, "status": "cancelling", "cancelled": True, "expert_status": "paused"}

    monkeypatch.setattr(server, "request_chat_interrupt", fake_interrupt)
    client = _client(monkeypatch)
    response = client.post("/api/remote/conversations/interrupt", json={"conversation_id": "conv-1"})
    assert response.status_code == 200
    assert response.json()["status"] == "cancelling"
    assert runs.items[0]["status"] == "paused"
    assert client.get("/api/remote/info").json()["active_conversation_id"] == "conv-1"


def test_remote_resume_rejects_busy_and_missing_checkpoint(monkeypatch) -> None:
    monkeypatch.setattr(
        server,
        "_activate_remote_mewcode_conversation",
        AsyncMock(side_effect=lambda cid, scope="", project_id="": {"id": cid, "scope": "project"}),
    )
    monkeypatch.setattr(server.SESSION, "chat_busy", lambda cid: True)
    client = _client(monkeypatch)
    busy = client.post("/api/remote/conversations/resume", json={"conversation_id": "conv-1"})
    assert busy.status_code == 409

    monkeypatch.setattr(server.SESSION, "chat_busy", lambda cid: False)
    monkeypatch.setattr(server, "EXPERT_RUNS", _FakeRuns([]))
    missing = client.post("/api/remote/conversations/resume", json={"conversation_id": "conv-1"})
    assert missing.status_code == 409
    assert any(token in missing.json()["error"] for token in ("接力", "检查点", "暂停"))


def test_remote_resume_same_run_emits_resume_id(monkeypatch) -> None:
    server.REMOTE_LIVES.pop("conv-1", None)
    snapshot = _orch_snapshot(status="paused")
    monkeypatch.setattr(server, "EXPERT_RUNS", _FakeRuns([snapshot]))
    monkeypatch.setattr(
        server,
        "_activate_remote_mewcode_conversation",
        AsyncMock(side_effect=lambda cid, scope="", project_id="": {"id": cid, "scope": "project"}),
    )
    monkeypatch.setattr(server.SESSION, "chat_busy", lambda cid: False)

    async def fake_launch(item, **_kwargs):
        await server.WS_EVENTS.send_json({
            "type": "turn_started",
            "conversation_id": item["conversation_id"],
            "resume_run_id": item["run_id"],
            "expert_mode": True,
        })
        return {
            "conversation_id": item["conversation_id"],
            "run_id": item["run_id"],
            "status": "running",
            "resume_run_id": item["run_id"],
        }

    monkeypatch.setattr(server, "launch_expert_resume", fake_launch)
    client = _client(monkeypatch)
    payload = client.post("/api/remote/conversations/resume", json={"conversation_id": "conv-1"}).json()
    assert payload["run_id"] == "run-1"
    assert payload["resume_run_id"] == "run-1"
    assert any(event.get("resume_run_id") == "run-1" for event in server.REMOTE_LIVES.get("conv-1") or [])


def test_remote_resume_blocks_pending_revision(monkeypatch) -> None:
    monkeypatch.setattr(
        server,
        "_activate_remote_mewcode_conversation",
        AsyncMock(side_effect=lambda cid, scope="", project_id="": {"id": cid, "scope": "project"}),
    )
    monkeypatch.setattr(server.SESSION, "chat_busy", lambda cid: False)
    monkeypatch.setattr(
        server,
        "EXPERT_RUNS",
        _FakeRuns([_orch_snapshot(status="blocked", pending_recovery_revision={"summary": "改核验"})]),
    )
    client = _client(monkeypatch)
    response = client.post("/api/remote/conversations/resume", json={"conversation_id": "conv-1"})
    assert response.status_code == 409
    assert "修订" in response.json()["error"]


def test_remote_prompts_human_oncall_and_permission(monkeypatch) -> None:
    class FakeFuture:
        def __init__(self) -> None:
            self._done = False
            self.value = None

        def done(self) -> bool:
            return self._done

        def set_result(self, value) -> None:
            self._done = True
            self.value = value

    oncall_future = FakeFuture()
    perm_future = FakeFuture()
    runtime = type("Runtime", (), {})()
    runtime.human_oncall_request = type("Req", (), {
        "request_id": "req-1",
        "questions": [{"name": "choice", "options": [{"value": "go", "label": "继续"}], "allowOther": True}],
        "future": oncall_future,
        "tool_name": "human_oncall",
    })()
    runtime.permission_future = perm_future
    runtime.permission_request = {"tool": "Bash", "description": "run tests"}
    runtime.ask_user_request = None
    monkeypatch.setattr(
        server,
        "_activate_remote_mewcode_conversation",
        AsyncMock(side_effect=lambda cid, scope="", project_id="": {"id": cid, "scope": "project"}),
    )
    monkeypatch.setattr(server.SESSION, "runtime_for", lambda cid: runtime if cid == "conv-1" else None)
    monkeypatch.setattr(
        server,
        "pending_runtime_prompts",
        lambda _session: [{
            "type": "human_oncall",
            "conversation_id": "conv-1",
            "request_id": "req-1",
            "questions": runtime.human_oncall_request.questions,
        }],
    )
    monkeypatch.setattr(server, "EXPERT_RUNS", _FakeRuns([_orch_snapshot()]))
    monkeypatch.setattr(server, "_live_mewcode_history_item", lambda cid: {"external_id": cid, "messages": []} if cid == "conv-1" else None)
    monkeypatch.setattr(server, "_disk_mewcode_history_item", lambda cid: None)
    monkeypatch.setattr(server.SESSION, "chat_busy", lambda cid: True)
    client = _client(monkeypatch)
    live = client.get("/api/remote/live", params={"conversation_id": "conv-1"}).json()
    assert live["pending"][0]["request_id"] == "req-1"
    assert live["pending"][0]["questions"]

    stale = client.post("/api/remote/prompts", json={
        "conversation_id": "conv-1",
        "kind": "human_oncall",
        "request_id": "old",
        "answers": {"choice": "go"},
    })
    assert stale.status_code == 409
    assert any(token in stale.json()["error"] for token in ("过期", "当前"))

    ok = client.post("/api/remote/prompts", json={
        "conversation_id": "conv-1",
        "kind": "human_oncall",
        "request_id": "req-1",
        "answers": {"choice": "go"},
    })
    assert ok.status_code == 200
    assert oncall_future.done()
    monkeypatch.setattr(server, "pending_runtime_prompts", lambda _session: [])
    live_after = client.get("/api/remote/live", params={"conversation_id": "conv-1"}).json()
    assert not any(item.get("type") == "human_oncall" for item in live_after["pending"])

    denied = client.post("/api/remote/prompts", json={
        "conversation_id": "conv-1",
        "kind": "permission",
        "decision": "not-a-choice",
    })
    assert denied.status_code == 200
    from mewcode.agent import PermissionResponse
    assert perm_future.value == PermissionResponse.DENY


def test_remote_prompts_plan_revision_reject(monkeypatch) -> None:
    runs = _FakeRuns([_orch_snapshot(status="blocked", pending_recovery_revision={"summary": "改核验"})])
    monkeypatch.setattr(server, "EXPERT_RUNS", runs)
    monkeypatch.setattr(
        server,
        "_activate_remote_mewcode_conversation",
        AsyncMock(side_effect=lambda cid, scope="", project_id="": {"id": cid, "scope": "project"}),
    )
    monkeypatch.setattr(server.SESSION, "runtime_for", lambda cid: None)
    client = _client(monkeypatch)
    response = client.post("/api/remote/prompts", json={
        "conversation_id": "conv-1",
        "kind": "plan_revision",
        "run_id": "run-1",
        "recovery_action": "reject",
    })
    assert response.status_code == 200
    assert "pending_recovery_revision" not in (runs.load("run-1") or {})


def _stub_empty_session_managers(monkeypatch) -> None:
    class _Empty:
        def resume(self, _session_id):
            return None

        def list(self):
            return []

    monkeypatch.setattr(server.SESSION, "session_manager", lambda _scope: _Empty())


def _write_session(root, message_text: str, title: str = ""):
    from mewcode.conversation import Message
    from mewcode.memory import SessionManager

    manager = SessionManager(str(root))
    session = manager.create()
    session.append(Message(role="user", content=message_text))
    if title:
        session.meta.title = title
        session.save_meta()
    session.close()
    return session.session_id


def test_remote_send_starts_turn_and_returns_operation(monkeypatch) -> None:
    focus_events: list[tuple] = []
    tasks: list = []
    runtime = SimpleNamespace(conversation_id="conv-1", scope="project", starting=False)
    monkeypatch.setattr(server, "REMOTE_OPERATIONS", {})
    monkeypatch.setattr(server, "REMOTE_LIVES", {})
    monkeypatch.setattr(
        server,
        "_activate_remote_mewcode_conversation",
        AsyncMock(side_effect=lambda cid, scope="", project_id="": {"id": "conv-1", "scope": "project"}),
    )
    monkeypatch.setattr(server.SESSION, "chat_busy", lambda _cid: False)
    monkeypatch.setattr(server.SESSION, "agent", object(), raising=False)
    monkeypatch.setattr(server.SESSION, "capture_conversation_runtime", lambda: runtime)
    monkeypatch.setattr(
        server.SESSION, "begin_conversation_turn",
        lambda rt: setattr(rt, "starting", True),
    )
    monkeypatch.setattr(
        server.SESSION, "attach_conversation_task",
        lambda rt, task: tasks.append(task),
    )

    async def fake_broadcast(cid, scope, *, mode):
        focus_events.append((cid, scope, mode))

    async def fake_run_chat(sink, text, *, runtime=None, **_kwargs):
        await sink.send_json({"type": "done"})
        return {}

    monkeypatch.setattr(server, "_broadcast_remote_conversation_focus", fake_broadcast)
    monkeypatch.setattr(server, "run_chat", fake_run_chat)
    client = _client(monkeypatch)
    response = client.post(
        "/api/remote/conversations/message",
        params={"conversation_id": "conv-1"},
        json={"text": "hello"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["conversation_id"] == "conv-1"
    assert payload["operation_id"]
    operation = server.REMOTE_OPERATIONS.get(payload["operation_id"])
    assert operation is not None
    assert any(
        event.get("type") == "turn_started" and event.get("text") == "hello"
        for event in operation.events
    )
    assert ("conv-1", "project", "send") in focus_events
    assert tasks, "chat task must be attached to the captured runtime"
    assert runtime.starting is True


def test_remote_send_preflight_errors(monkeypatch) -> None:
    monkeypatch.setattr(server, "REMOTE_OPERATIONS", {})
    monkeypatch.setattr(server, "REMOTE_LIVES", {})
    monkeypatch.setattr(
        server,
        "_activate_remote_mewcode_conversation",
        AsyncMock(side_effect=lambda cid, scope="", project_id="": {"id": "conv-1", "scope": "project"}),
    )
    monkeypatch.setattr(server.SESSION, "chat_busy", lambda _cid: False)
    client = _client(monkeypatch)

    monkeypatch.setattr(server.SESSION, "agent", None, raising=False)
    missing_agent = client.post(
        "/api/remote/conversations/message",
        params={"conversation_id": "conv-1"},
        json={"text": "hello"},
    )
    assert missing_agent.status_code == 409
    assert "模型" in missing_agent.json()["error"]

    monkeypatch.setattr(server.SESSION, "agent", object(), raising=False)

    def broken_capture():
        raise RuntimeError("当前对话尚未准备好")

    monkeypatch.setattr(server.SESSION, "capture_conversation_runtime", broken_capture)
    missing_runtime = client.post(
        "/api/remote/conversations/message",
        params={"conversation_id": "conv-1"},
        json={"text": "hello"},
    )
    assert missing_runtime.status_code == 409
    assert "尚未准备好" in missing_runtime.json()["error"]

    switched = SimpleNamespace(conversation_id="conv-other", scope="project", starting=False)
    monkeypatch.setattr(server.SESSION, "capture_conversation_runtime", lambda: switched)
    mismatch = client.post(
        "/api/remote/conversations/message",
        params={"conversation_id": "conv-1"},
        json={"text": "hello"},
    )
    assert mismatch.status_code == 409
    assert "已切换" in mismatch.json()["error"]


def test_remote_send_captures_runtime_before_broadcast(monkeypatch) -> None:
    monkeypatch.setattr(server, "REMOTE_OPERATIONS", {})
    monkeypatch.setattr(server, "REMOTE_LIVES", {})
    current = {"conversation_id": "conv-1"}
    seen_runtime_ids: list[str] = []

    def fake_capture():
        return SimpleNamespace(
            conversation_id=current["conversation_id"], scope="project", starting=False,
        )

    async def switching_broadcast(_cid, _scope, *, mode):
        current["conversation_id"] = "conv-2"

    async def fake_run_chat(sink, text, *, runtime=None, **_kwargs):
        seen_runtime_ids.append(runtime.conversation_id)
        await sink.send_json({"type": "done"})
        return {}

    monkeypatch.setattr(
        server,
        "_activate_remote_mewcode_conversation",
        AsyncMock(side_effect=lambda cid, scope="", project_id="": {"id": "conv-1", "scope": "project"}),
    )
    monkeypatch.setattr(server.SESSION, "chat_busy", lambda _cid: False)
    monkeypatch.setattr(server.SESSION, "agent", object(), raising=False)
    monkeypatch.setattr(server.SESSION, "capture_conversation_runtime", fake_capture)
    monkeypatch.setattr(server.SESSION, "begin_conversation_turn", lambda rt: setattr(rt, "starting", True))
    monkeypatch.setattr(server.SESSION, "attach_conversation_task", lambda rt, task: None)
    monkeypatch.setattr(server, "_broadcast_remote_conversation_focus", switching_broadcast)
    monkeypatch.setattr(server, "run_chat", fake_run_chat)
    client = _client(monkeypatch)
    response = client.post(
        "/api/remote/conversations/message",
        params={"conversation_id": "conv-1"},
        json={"text": "hello"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["conversation_id"] == "conv-1"
    assert seen_runtime_ids == ["conv-1"], (
        "runtime must be captured before the broadcast await so a desktop-side "
        "switch during the await cannot redirect the turn"
    )


def test_remote_history_item_reads_other_known_project(monkeypatch, tmp_path) -> None:
    dir_a = tmp_path / "current"
    dir_b = tmp_path / "other"
    dir_a.mkdir()
    session_b = _write_session(dir_b, "来自项目B的消息", title="B 对话")
    project_b = server._remote_project_id(os.path.abspath(str(dir_b)))

    monkeypatch.setattr(server, "_load_projects", lambda: [str(dir_b)])
    monkeypatch.setattr(server.SESSION, "work_dir", str(dir_a))
    monkeypatch.setattr(server.SESSION, "session", None)
    monkeypatch.setattr(server.SESSION, "runtime_for", lambda _cid="": None)
    monkeypatch.setattr(server.SESSION, "chat_busy", lambda _cid="": False)
    monkeypatch.setattr(server.SESSION, "new_session_store", lambda d: SessionManager(str(d)))
    _stub_empty_session_managers(monkeypatch)
    monkeypatch.setattr(server, "EXPERT_RUNS", _FakeRuns([]))

    client = _client(monkeypatch)
    payload = client.get(
        "/api/remote/history/item",
        params={"external_id": session_b, "project_id": project_b},
    ).json()
    assert payload["project_id"] == project_b
    assert payload["project_path"] == os.path.abspath(str(dir_b))
    assert payload["title"] == "B 对话"
    assert any("来自项目B" in str(m.get("text") or "") for m in payload["messages"])
    assert server.SESSION.work_dir == str(dir_a)

    unknown = client.get(
        "/api/remote/history/item",
        params={"external_id": session_b, "project_id": "f" * 24},
    )
    assert unknown.status_code == 404
    traversal = client.get(
        "/api/remote/history/item",
        params={"external_id": "../../etc/passwd", "project_id": project_b},
    )
    assert traversal.status_code == 404
    assert server.SESSION.work_dir == str(dir_a)


def test_remote_history_item_rejects_active_duplicate_from_other_project(monkeypatch, tmp_path) -> None:
    dir_a = tmp_path / "current"
    dir_b = tmp_path / "copy"
    dir_a.mkdir()
    session_b = _write_session(dir_b, "B项目内容", title="B 副本")
    project_b = server._remote_project_id(os.path.abspath(str(dir_b)))

    active_session = SimpleNamespace(session_id=session_b, title="A 当前对话")
    monkeypatch.setattr(server, "_load_projects", lambda: [str(dir_b)])
    monkeypatch.setattr(server.SESSION, "work_dir", str(dir_a))
    monkeypatch.setattr(server.SESSION, "scope", "project")
    monkeypatch.setattr(server.SESSION, "session", active_session)
    monkeypatch.setattr(server.SESSION, "conversation", SimpleNamespace(history=[]))
    monkeypatch.setattr(server.SESSION, "runtime_for", lambda _cid="": None)
    monkeypatch.setattr(server.SESSION, "chat_busy", lambda _cid="": False)
    monkeypatch.setattr(server.SESSION, "new_session_store", lambda d: SessionManager(str(d)))
    _stub_empty_session_managers(monkeypatch)
    monkeypatch.setattr(server, "EXPERT_RUNS", _FakeRuns([]))

    client = _client(monkeypatch)
    payload = client.get(
        "/api/remote/history/item",
        params={"external_id": session_b, "project_id": project_b},
    ).json()
    assert payload["title"] == "B 副本"
    assert payload["project_id"] == project_b
    assert payload["active"] is False
    assert server.SESSION.session is active_session
    assert server.SESSION.work_dir == str(dir_a)


def test_remote_activate_cross_project_switches_registered_root(monkeypatch, tmp_path) -> None:
    dir_a = tmp_path / "current"
    dir_b = tmp_path / "other"
    dir_a.mkdir()
    session_b = _write_session(dir_b, "激活我", title="B 激活")
    project_b = server._remote_project_id(os.path.abspath(str(dir_b)))

    activations: list[str] = []

    async def fake_aactivate(dir_path):
        activations.append(os.path.abspath(dir_path))
        return None

    monkeypatch.setattr(server, "_load_projects", lambda: [str(dir_b)])
    monkeypatch.setattr(server.SESSION, "work_dir", str(dir_a))
    monkeypatch.setattr(server.SESSION, "scope", "project")
    monkeypatch.setattr(server.SESSION, "session", None)
    monkeypatch.setattr(server.SESSION, "runtime_for", lambda _cid="": None)
    monkeypatch.setattr(server.SESSION, "aactivate_project", fake_aactivate)
    monkeypatch.setattr(
        server.SESSION, "load_conversation",
        lambda scope, sid: {"id": sid, "scope": scope} if scope == "project" else None,
    )

    async def fake_broadcast(*_args, **_kwargs):
        return None
    monkeypatch.setattr(server, "_broadcast_remote_conversation_focus", fake_broadcast)

    client = _client(monkeypatch)
    response = client.post(
        "/api/remote/conversations/activate",
        json={"conversation_id": session_b, "project_id": project_b},
    )
    assert response.status_code == 200, response.text
    assert response.json()["id"] == session_b
    assert activations == [os.path.abspath(str(dir_b))]

    missing = client.post(
        "/api/remote/conversations/activate",
        json={"conversation_id": session_b, "project_id": "e" * 24},
    )
    assert missing.status_code == 404
    assert activations == [os.path.abspath(str(dir_b))]

    wrong_id = client.post(
        "/api/remote/conversations/activate",
        json={"conversation_id": "../" + session_b, "project_id": project_b},
    )
    assert wrong_id.status_code == 404


def test_remote_activate_refuses_runtime_owned_by_other_project(monkeypatch, tmp_path) -> None:
    dir_a = tmp_path / "current"
    dir_b = tmp_path / "other"
    dir_a.mkdir()
    session_a = _write_session(dir_a, "项目A对话", title="A 对话")
    project_a = server._remote_project_id(os.path.abspath(str(dir_a)))

    foreign_runtime = SimpleNamespace(
        conversation_id=session_a,
        scope="project",
        work_dir=os.path.abspath(str(dir_b)),
        running=lambda: True,
    )
    active_other = SimpleNamespace(session_id="other-conv", title="别的对话")
    monkeypatch.setattr(server, "_load_projects", lambda: [str(dir_b)])
    monkeypatch.setattr(server.SESSION, "work_dir", str(dir_a))
    monkeypatch.setattr(server.SESSION, "scope", "project")
    monkeypatch.setattr(server.SESSION, "session", active_other)
    monkeypatch.setattr(server.SESSION, "runtime_for", lambda _cid="": foreign_runtime)
    monkeypatch.setattr(server.SESSION, "chat_busy", lambda _cid="": False)
    activations = []

    async def fake_aactivate(dir_path):
        activations.append(dir_path)
        return None
    monkeypatch.setattr(server.SESSION, "aactivate_project", fake_aactivate)
    loaded_calls = []
    monkeypatch.setattr(
        server.SESSION, "load_conversation",
        lambda scope, sid: loaded_calls.append((scope, sid)) or {"id": sid, "scope": scope},
    )

    async def fake_broadcast(*_args, **_kwargs):
        return None
    monkeypatch.setattr(server, "_broadcast_remote_conversation_focus", fake_broadcast)
    client = _client(monkeypatch)
    response = client.post(
        "/api/remote/conversations/activate",
        json={"conversation_id": session_a, "project_id": project_a},
    )
    assert response.status_code == 404
    assert activations == []
    assert loaded_calls == []


def test_remote_activate_refuses_active_duplicate_when_requesting_other_project(
    monkeypatch, tmp_path
) -> None:
    dir_a = tmp_path / "current"
    dir_b = tmp_path / "copy"
    dir_a.mkdir()
    session_b = _write_session(dir_b, "B副本", title="B 副本")
    project_b = server._remote_project_id(os.path.abspath(str(dir_b)))

    active_session = SimpleNamespace(session_id=session_b, title="A 同名对话")
    monkeypatch.setattr(server, "_load_projects", lambda: [str(dir_b)])
    monkeypatch.setattr(server.SESSION, "work_dir", str(dir_a))
    monkeypatch.setattr(server.SESSION, "scope", "project")
    monkeypatch.setattr(server.SESSION, "session", active_session)
    monkeypatch.setattr(server.SESSION, "runtime_for", lambda _cid="": None)
    monkeypatch.setattr(server.SESSION, "chat_busy", lambda _cid="": False)
    activations = []

    async def fake_aactivate(dir_path):
        activations.append(dir_path)
        return None
    monkeypatch.setattr(server.SESSION, "aactivate_project", fake_aactivate)

    async def fake_broadcast(*_args, **_kwargs):
        return None
    monkeypatch.setattr(server, "_broadcast_remote_conversation_focus", fake_broadcast)
    client = _client(monkeypatch)
    response = client.post(
        "/api/remote/conversations/activate",
        json={"conversation_id": session_b, "project_id": project_b},
    )
    assert response.status_code == 404
    assert activations == []


def test_remote_send_passes_project_id_to_requested_root(monkeypatch, tmp_path) -> None:
    dir_a = tmp_path / "current"
    dir_b = tmp_path / "other"
    dir_a.mkdir()
    session_b = _write_session(dir_b, "发给B", title="B 目标")
    project_b = server._remote_project_id(os.path.abspath(str(dir_b)))

    monkeypatch.setattr(server, "_load_projects", lambda: [str(dir_b)])
    monkeypatch.setattr(server.SESSION, "work_dir", str(dir_a))
    monkeypatch.setattr(server.SESSION, "scope", "project")
    monkeypatch.setattr(server.SESSION, "session", None)
    monkeypatch.setattr(server.SESSION, "runtime_for", lambda _cid="": None)
    monkeypatch.setattr(server.SESSION, "chat_busy", lambda _cid: False)
    monkeypatch.setattr(server, "REMOTE_OPERATIONS", {})
    monkeypatch.setattr(server, "REMOTE_LIVES", {})
    activations = []

    async def fake_aactivate(dir_path):
        activations.append(os.path.abspath(dir_path))
        return None
    monkeypatch.setattr(server.SESSION, "aactivate_project", fake_aactivate)
    monkeypatch.setattr(
        server.SESSION, "load_conversation",
        lambda scope, sid: {"id": sid, "scope": scope} if scope == "project" else None,
    )
    runtime = SimpleNamespace(
        conversation_id=session_b,
        scope="project",
        work_dir=os.path.abspath(str(dir_b)),
        starting=False,
    )
    monkeypatch.setattr(server.SESSION, "agent", object(), raising=False)
    monkeypatch.setattr(server.SESSION, "capture_conversation_runtime", lambda: runtime)
    monkeypatch.setattr(server.SESSION, "begin_conversation_turn", lambda rt: setattr(rt, "starting", True))
    monkeypatch.setattr(server.SESSION, "attach_conversation_task", lambda rt, task: None)

    async def fake_broadcast(*_args, **_kwargs):
        return None

    async def fake_run_chat(sink, text, *, runtime=None, **_kwargs):
        await sink.send_json({"type": "done"})
        return {}
    monkeypatch.setattr(server, "_broadcast_remote_conversation_focus", fake_broadcast)
    monkeypatch.setattr(server, "run_chat", fake_run_chat)
    client = _client(monkeypatch)
    response = client.post(
        "/api/remote/conversations/message",
        params={"conversation_id": session_b},
        json={"text": "hello B", "project_id": project_b},
    )
    assert response.status_code == 200, response.text
    assert response.json()["conversation_id"] == session_b
    assert activations == [os.path.abspath(str(dir_b))]


def test_remote_history_list_neutralizes_conflicting_duplicate(monkeypatch, tmp_path) -> None:
    dir_a = tmp_path / "current"
    dir_b = tmp_path / "copy"
    dir_a.mkdir()
    dir_b.mkdir()
    shared_id = "dup-1"
    sidebar = {
        "projects": [
            {
                "dir": os.path.abspath(str(dir_a)),
                "label": "current",
                "active": True,
                "conversations": [
                    {"id": shared_id, "title": "A 对话", "last_active": "2026-09-15",
                     "message_count": 1, "active": True, "running": True},
                ],
            },
            {
                "dir": os.path.abspath(str(dir_b)),
                "label": "copy",
                "active": False,
                "conversations": [
                    {"id": shared_id, "title": "B 副本", "last_active": "2026-09-14",
                     "message_count": 1, "active": True, "running": True},
                ],
            },
        ],
        "normal": [],
        "work_dir": os.path.abspath(str(dir_a)),
    }
    active_session = SimpleNamespace(session_id=shared_id, title="A 对话")
    runtime_a = SimpleNamespace(
        conversation_id=shared_id,
        scope="project",
        work_dir=os.path.abspath(str(dir_a)),
        running=lambda: True,
    )
    monkeypatch.setattr(server.SESSION, "sidebar_payload", lambda: sidebar)
    monkeypatch.setattr(server.SESSION, "running_conversation_ids", lambda: {shared_id})
    monkeypatch.setattr(server, "_active_conversation_id", lambda: shared_id)
    monkeypatch.setattr(server.SESSION, "work_dir", str(dir_a))
    monkeypatch.setattr(server.SESSION, "scope", "project")
    monkeypatch.setattr(server.SESSION, "session", active_session)
    monkeypatch.setattr(server.SESSION, "runtime_for", lambda _cid="": runtime_a)
    monkeypatch.setattr(server.SESSION, "chat_busy", lambda _cid="": True)
    monkeypatch.setattr(server, "EXPERT_RUNS", _FakeRuns([_orch_snapshot()]))
    client = _client(monkeypatch)
    payload = client.get("/api/remote/history").json()
    by_project = {
        item["project_id"]: item for item in payload["items"]
    }
    item_a = by_project[server._remote_project_id(os.path.abspath(str(dir_a)))]
    item_b = by_project[server._remote_project_id(os.path.abspath(str(dir_b)))]
    assert item_a["active"] is True
    assert item_a["running"] is True
    assert item_b["active"] is False
    assert item_b["running"] is False
    assert item_b["execution_kind"] == "chat"
    assert item_b["expert_status"] == ""
    assert item_b["resumable"] is False
    assert item_b["run_id"] == ""


def test_remote_live_rejects_user_scoped_active_duplicate(monkeypatch, tmp_path) -> None:
    dir_a = tmp_path / "current"
    dir_b = tmp_path / "other"
    dir_a.mkdir()
    session_b = _write_session(dir_b, "B内容", title="B")
    project_b = server._remote_project_id(os.path.abspath(str(dir_b)))

    active_session = SimpleNamespace(session_id=session_b, title="同名普通对话")
    monkeypatch.setattr(server, "_load_projects", lambda: [str(dir_b)])
    monkeypatch.setattr(server.SESSION, "work_dir", str(dir_a))
    monkeypatch.setattr(server.SESSION, "scope", "user")
    monkeypatch.setattr(server.SESSION, "session", active_session)
    monkeypatch.setattr(server.SESSION, "runtime_for", lambda _cid="": None)
    monkeypatch.setattr(server.SESSION, "chat_busy", lambda _cid="": False)
    monkeypatch.setattr(server.SESSION, "new_session_store", lambda d: SessionManager(str(d)))
    _stub_empty_session_managers(monkeypatch)
    monkeypatch.setattr(server, "EXPERT_RUNS", _FakeRuns([]))
    client = _client(monkeypatch)
    response = client.get(
        "/api/remote/live",
        params={"conversation_id": session_b, "project_id": project_b},
    )
    assert response.status_code == 404
