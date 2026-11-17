"""Current listener information flows from host launch to executor and review."""
import json
import sys

import pytest

from mewcode.conversation import ConversationManager
from mewcode.permissions.reviewer import SafetyReviewer
from mewcode.tools.agent_status import AgentStatus, StatusParams, _ui_endpoint
from tests.test_agent_capabilities import parent
from tests.test_review_task_evidence import observe


@pytest.mark.parametrize("host,port,url", [
    ("127.0.0.1", "64219", "http://127.0.0.1:64219/"),
    ("0.0.0.0", "43210", "http://127.0.0.1:43210/"),
    ("localhost", "8123", "http://localhost:8123/"),
    ("::", "8123", "http://[::1]:8123/"),
    ("::1", "8123", "http://[::1]:8123/"),
    ("192.168.1.5", "9000", "http://192.168.1.5:9000/"),
    ("", "8765", None), ("127.0.0.1", "", None),
    ("127.0.0.1", "0", None), ("127.0.0.1", "65536", None),
    ("127.0.0.1", "not-a-port", None), ("127.0.0.1", "１２３", None),
    ("user:secret@127.0.0.1", "8765", None),
    ("https://127.0.0.1/path?secret=1", "8765", None),
    ("fe80::1%12", "8765", None),
])
def test_endpoint_never_guesses_or_embeds_credentials(monkeypatch, host, port, url):
    monkeypatch.setenv("MEWCODE_BACKEND_HOST", host)
    monkeypatch.setenv("MEWCODE_BACKEND_PORT", port)
    monkeypatch.setenv("MEWCODE_DESKTOP_TOKEN", "never-expose-this-token")
    result = _ui_endpoint()
    assert result["url"] == url
    assert result["source"] == "backend_listener_configuration"
    assert result["live_verified"] is False
    assert "secret" not in json.dumps(result) and "never-expose" not in json.dumps(result)


@pytest.mark.parametrize("newer_inventory", [False, True])
@pytest.mark.asyncio
async def test_current_address_reaches_status_and_review_after_output_truncation(tmp_path, monkeypatch, newer_inventory):
    monkeypatch.setenv("MEWCODE_BACKEND_HOST", "127.0.0.1")
    monkeypatch.setenv("MEWCODE_BACKEND_PORT", "61029")
    tool = AgentStatus(parent(tmp_path))
    previous = await tool.execute(StatusParams())
    monkeypatch.setenv("MEWCODE_BACKEND_PORT", "64219")
    current = await tool.execute(StatusParams())
    assert json.loads(previous.output)["ui_endpoint"]["url"] == "http://127.0.0.1:61029/"
    assert json.loads(current.output)["ui_endpoint"] == current.host_observation["ui_endpoint"]
    assert current.host_observation["ui_endpoint"]["url"] == "http://127.0.0.1:64219/"
    conv = ConversationManager()
    observe(conv, "old", "truncated", tool="AgentStatus", task_id=None, host=previous.host_observation)
    observe(conv, "current", "truncated", tool="AgentStatus", task_id=None, host=current.host_observation)
    if newer_inventory:
        observe(conv, "tasks", [], task_id=None, host=[])
    rows = SafetyReviewer._evidence_context(conv)
    endpoint_rows = [row for row in rows if "ui_endpoint" in json.loads(row["text"])]
    assert len(endpoint_rows) == 1
    assert json.loads(endpoint_rows[0]["text"])["ui_endpoint"] == current.host_observation["ui_endpoint"]
    assert endpoint_rows[0]["trusted_authorization"] is False
    assert "61029" not in json.dumps(rows)


def test_backend_launch_records_actual_host_and_port_before_serving(monkeypatch):
    from frontend import server
    monkeypatch.setenv("MEWCODE_BACKEND_HOST", "stale.invalid")
    monkeypatch.setenv("MEWCODE_BACKEND_PORT", "8765")
    monkeypatch.setattr(sys, "argv", ["server.py", "--host", "::1", "--port", "64219"])
    observed = []

    def serve(app, *, host, port, log_level):
        observed.append((host, port, _ui_endpoint()["url"]))

    monkeypatch.setattr(server.uvicorn, "run", serve)
    server.main()
    assert observed == [("::1", 64219, "http://[::1]:64219/")]
