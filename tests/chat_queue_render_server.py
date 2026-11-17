"""Full production chat UI with a synthetic, controllable queue lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

import frontend.server as desktop


FIXTURE_TOKEN = "chat-queue-fixture-only"
CONVERSATION_ID = "queue-chat-a"


def fixture_app(root: str | Path, *, pipeline_events=None, agent_groups=None, initial_history=None) -> FastAPI:
    repository = Path(__file__).resolve().parents[1]
    root = Path(root).resolve()
    if not root.is_relative_to(repository) or not root.name.startswith(".tmp-chat-queue-integration"):
        raise ValueError("A fresh isolated .tmp-chat-queue-integration directory is required")
    root.mkdir(exist_ok=False)
    project = root / "project"
    project.mkdir()

    state = {
        "running": False,
        "turn_gate": None,
        "interrupt_requested": False,
        "queue": [],
        "queue_revision": 0,
        "history": [dict(entry) for entry in initial_history or []],
        "submissions": [],
        "interrupts": 0,
        "clients": set(),
        "tasks": set(),
        "opened": False,
    }
    app = FastAPI()
    desktop.DESKTOP_TOKEN = FIXTURE_TOKEN
    app.middleware("http")(desktop.desktop_security)
    app.add_api_route("/desktop-boot", desktop.desktop_boot, methods=["GET"])
    app.mount("/static", StaticFiles(directory=repository / "frontend/static"), name="static")

    def status() -> dict:
        return {
            "conversation_id": CONVERSATION_ID,
            "conversation_title": "等待区浏览器验证",
            "scope": "project",
            "work_dir": str(project),
            "project_name": "chat-queue-fixture",
            "model": "synthetic-fixture",
            "provider": "Fixture",
            "provider_key": "fixture",
            "permission_mode": "auto",
            "multi_agent": False,
            "expert_mode": False,
            "orchestration_planning": False,
            "goal": "",
            "goal_mode": False,
            "effort": "auto",
            "window": 200_000,
            "ctx": 0,
            "context_tier": 200_000,
            "total_in": 0,
            "total_out": 0,
            "session": "0s",
        }

    def queue_payload() -> list[dict]:
        return [
            {**item, "position": index}
            for index, item in enumerate(state["queue"], 1)
        ]

    def client_state() -> dict:
        queues = {CONVERSATION_ID: queue_payload()} if state["queue"] else {}
        return {
            "status": status(),
            "ready": True,
            "slots": [],
            "tasks": [],
            "commands": [],
            "running_conversations": [CONVERSATION_ID] if state["running"] else [],
            "conversation_queues": queues,
            "conversation_queue_revisions": {CONVERSATION_ID: state["queue_revision"]},
            "efforts": {"levels": ["auto"], "current": "auto"},
        }

    async def broadcast(payload: dict) -> None:
        message = {**payload, "conversation_id": CONVERSATION_ID}
        stale = []
        for websocket in list(state["clients"]):
            try:
                await websocket.send_json(message)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            state["clients"].discard(websocket)

    app.state.fixture_broadcast = broadcast

    async def drain_queue() -> None:
        while state["queue"]:
            item = state["queue"].pop(0)
            state["queue_revision"] += 1
            await broadcast({
                "type": "chat_queue", "items": queue_payload(),
                "revision": state["queue_revision"], "continuing": True,
            })
            state["submissions"].append(item["text"])
            state["history"].append({"role": "user", "text": item["text"]})
            await broadcast({
                "type": "turn_started",
                "text": item["text"],
                "attachments": [],
                "queued": True,
                "expert_mode": False,
                "client_echoed": False,
            })
            answer = "已自动处理等待消息：" + item["text"]
            state["history"].append({"role": "assistant", "text": answer})
            await broadcast({"type": "delta", "text": answer})
            await asyncio.sleep(0.2)
            await broadcast({
                "type": "done", "interrupted": False, "elapsed": 0.1,
                "status": status(), "tasks": [],
            })
            await asyncio.sleep(0.03)
        state["running"] = False

    async def run_direct(text: str) -> None:
        state["running"] = True
        state["interrupt_requested"] = False
        gate = asyncio.Event()
        state["turn_gate"] = gate
        state["submissions"].append(text)
        state["history"].append({"role": "user", "text": text})
        await broadcast({
            "type": "turn_started", "text": text, "attachments": [],
            "queued": False, "expert_mode": False, "client_echoed": True,
        })
        await broadcast({"type": "delta", "text": "正在处理：" + text})
        await gate.wait()
        interrupted = bool(state["interrupt_requested"])
        if not interrupted:
            answer = "已完成当前消息：" + text
            state["history"].append({"role": "assistant", "text": answer})
            await broadcast({"type": "delta", "text": answer})
        await broadcast({
            "type": "done", "interrupted": interrupted, "elapsed": 0.2,
            "status": status(), "tasks": [],
        })
        state["turn_gate"] = None
        await drain_queue()

    @app.get("/")
    def chat_html():
        return FileResponse(repository / "frontend/static/index.html")

    @app.get("/api/state")
    def api_state():
        return client_state()

    @app.get("/api/workspace-tree")
    def workspace_tree():
        return {
            "work_dir": str(project),
            "unread": [],
            "machines": [{
                "id": "local", "node_id": "local", "label": "隔离浏览器验证", "kind": "local",
                "sources": [{
                    "id": "mewcode", "label": "MewCode", "conversations": [],
                    "projects": [{
                        "id": "fixture", "dir": str(project), "label": "chat-queue-fixture",
                        "active": True,
                        "conversations": [{
                            "id": CONVERSATION_ID, "source": "mewcode", "node_id": "local",
                            "title": "等待区浏览器验证", "summary": "", "active": True,
                            "writable": True, "running": state["running"],
                            "last_active": "2026-09-09T00:00:00Z",
                            "message_count": len(state["history"]),
                        }],
                    }],
                }],
            }],
        }

    @app.get("/api/conversations/{scope}/{conversation_id}")
    def conversation(scope: str, conversation_id: str):
        if conversation_id != CONVERSATION_ID:
            raise HTTPException(404)
        state["opened"] = True
        return {
            "id": CONVERSATION_ID,
            "scope": scope,
            "entries": state["history"],
            "pipeline_events": list(pipeline_events or []),
            "expert_runs": [],
            "running": state["running"],
        }

    @app.get("/api/models/catalog")
    def model_catalog():
        return {"models": [], "providers": [], "cc_switch": {"available": False}}

    @app.get("/api/orchestration/current")
    def no_orchestration():
        return {"project": str(project), "custom": False, "spec": None, "flow": None}

    @app.get("/api/plugins/ui")
    def no_plugins():
        return {"plugins": []}

    @app.get("/api/agent_tasks")
    def no_background_tasks():
        return {"tasks": [], "running": 0, "queued": 0, "groups": list(agent_groups or [])}

    @app.get("/__test__/status")
    def fixture_status():
        return {
            "running": state["running"],
            "queue": queue_payload(),
            "queue_revision": state["queue_revision"],
            "submissions": list(state["submissions"]),
            "interrupts": state["interrupts"],
            "history": list(state["history"]),
            "opened": state["opened"],
        }

    @app.post("/__test__/release")
    async def release_current():
        gate = state["turn_gate"]
        if gate is None:
            raise HTTPException(409, "no held turn")
        gate.set()
        return {"ok": True}

    @app.post("/api/chat/queue")
    async def enqueue(request: Request):
        if request.headers.get("x-mew-chat-queue") != "1":
            raise HTTPException(403)
        body = await request.json()
        if body.get("conversation_id") != CONVERSATION_ID or not state["running"]:
            return desktop.JSONResponse({"error": "当前回复已经结束", "code": "not_running"}, status_code=409)
        item = {
            "id": str(body.get("queue_id") or ""),
            "text": str(body.get("text") or "").strip(),
            "attachments": [],
        }
        state["queue"].append(item)
        state["queue_revision"] += 1
        payload = {
            "conversation_id": CONVERSATION_ID, "items": queue_payload(),
            "revision": state["queue_revision"], "accepted_id": item["id"],
        }
        await broadcast({
            "type": "chat_queue", "items": payload["items"],
            "revision": payload["revision"],
        })
        return payload

    @app.post("/api/chat/queue/{queue_id}/remove")
    async def remove(queue_id: str, request: Request):
        body = await request.json()
        if request.headers.get("x-mew-chat-queue") != "1" or body.get("conversation_id") != CONVERSATION_ID:
            raise HTTPException(403)
        state["queue"] = [item for item in state["queue"] if item["id"] != queue_id]
        state["queue_revision"] += 1
        payload = {
            "conversation_id": CONVERSATION_ID, "items": queue_payload(),
            "revision": state["queue_revision"],
        }
        await broadcast({
            "type": "chat_queue", "items": payload["items"],
            "revision": payload["revision"],
        })
        return payload

    @app.post("/api/chat/queue/{queue_id}/send-now")
    async def send_now(queue_id: str, request: Request):
        body = await request.json()
        if request.headers.get("x-mew-chat-queue") != "1" or body.get("conversation_id") != CONVERSATION_ID:
            raise HTTPException(403)
        index = next((i for i, item in enumerate(state["queue"]) if item["id"] == queue_id), -1)
        if index < 0 or not state["running"]:
            raise HTTPException(409)
        selected = state["queue"].pop(index)
        state["queue"].insert(0, selected)
        state["queue_revision"] += 1
        state["interrupts"] += 1
        state["interrupt_requested"] = True
        payload = {
            "conversation_id": CONVERSATION_ID, "items": queue_payload(),
            "revision": state["queue_revision"], "send_now_id": queue_id,
            "status": "cancelling",
        }
        await broadcast({
            "type": "chat_queue", "items": payload["items"],
            "revision": payload["revision"],
        })
        await broadcast({"type": "interrupt_ack", "status": "cancelling", "send_now_id": queue_id})
        state["turn_gate"].set()
        return payload

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        if websocket.cookies.get(desktop.DESKTOP_COOKIE) != FIXTURE_TOKEN:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        state["clients"].add(websocket)
        await websocket.send_json({"type": "state", **client_state()})
        try:
            while True:
                message = await websocket.receive_json()
                if message.get("type") != "chat":
                    continue
                if state["running"]:
                    await websocket.send_json({
                        "type": "error", "text": "fixture busy",
                        "conversation_id": CONVERSATION_ID,
                    })
                    continue
                task = asyncio.create_task(run_direct(str(message.get("text") or "")))
                state["tasks"].add(task)
                task.add_done_callback(state["tasks"].discard)
        except WebSocketDisconnect:
            pass
        finally:
            state["clients"].discard(websocket)

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    app = fixture_app(parser.parse_args().root)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    print("CHAT_QUEUE_FIXTURE_PORT=" + str(sock.getsockname()[1]), flush=True)
    uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[sock])
