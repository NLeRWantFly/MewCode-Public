"""The UI authorization endpoint is bound to the currently focused task."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from frontend import server
from mewcode.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine


class _Request:
    def __init__(self, body, *, confirmed=True):
        self.headers = {"X-Mew-Task-Authorization": "1"} if confirmed else {}
        self._body = body

    async def json(self):
        return self._body


@pytest.mark.asyncio
async def test_task_scope_requires_explicit_ui_confirmation_and_current_task(tmp_path, monkeypatch):
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(tmp_path)), RuleEngine(), PermissionMode.AUTO)
    reviewer = SimpleNamespace(authorization_version="")
    receipts = []
    state = SimpleNamespace(
        session=SimpleNamespace(session_id="conversation-1"), web_state={},
        agent=SimpleNamespace(permission_checker=checker, safety_reviewer=reviewer,
                              audit_logger=SimpleNamespace(log_decision=lambda **kwargs: receipts.append(kwargs))),
    )
    monkeypatch.setattr(server, "SESSION", state)
    monkeypatch.setattr(server, "dialogue_focus", lambda _session: {"task_id": "task-1", "task": "Revise files"})
    monkeypatch.setattr(server, "_save_web_state", lambda _state: None)
    payload = {"conversation_id": "conversation-1", "task_id": "task-1",
               "read_directories": [str(tmp_path)], "allowed_tools": ["ReadFile"]}
    rejected = await server.api_chat_task_authorization(_Request(payload, confirmed=False))
    assert rejected.status_code == 403 and not state.web_state
    stale = await server.api_chat_task_authorization(_Request({**payload, "task_id": "other"}))
    assert stale.status_code == 400 and not state.web_state
    saved = await server.api_chat_task_authorization(_Request(payload))
    assert saved.status_code == 200
    record = json.loads(saved.body)["authorization"]
    assert checker.task_authorization.version == reviewer.authorization_version == record["version"]
    assert receipts[0]["source_layer"] == "task_scope_ui"
    assert record["version"] in receipts[0]["authorization_scope"]
    current = await server.api_chat_task_authorization_current()
    assert json.loads(current.body)["authorization"]["task_id"] == "task-1"
