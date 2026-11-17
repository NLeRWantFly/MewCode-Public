"""Approval telemetry must stay correlated, session scoped and read-only."""
import ast
import hashlib
import inspect
import json
from types import SimpleNamespace

import pytest

from frontend import server
from mewcode.agent import Agent
from mewcode.permissions import PermissionMode
from mewcode.permissions.audit import ARGS_SCHEMA
from mewcode.tools.base import ToolCallComplete


def _audit_checker(mode):
    return SimpleNamespace(
        mode=mode,
        uses_native_commands=lambda: mode == PermissionMode.AUTO,
    )


@pytest.mark.parametrize("mode,tool,backend", [
    (PermissionMode.AUTO, "Bash", "windows_native_required"),
    (PermissionMode.AUTO, "RunVerification", "windows_native_required"),
    (PermissionMode.STRICT_ISOLATION, "Bash", "strict_container_required"),
    (PermissionMode.DEFAULT, "Bash", "host_requested"),
    (PermissionMode.AUTO, "ReadFile", "structured_tool"),
])
def test_audit_metadata_is_not_execution_evidence(mode, tool, backend):
    receipts = []
    agent = object.__new__(Agent)
    agent.audit_logger = SimpleNamespace(log_decision=lambda **fields: receipts.append(fields))
    agent.permission_checker = _audit_checker(mode)
    agent.permission_mode = PermissionMode.DEFAULT
    agent.session_id = "session-current"
    call = ToolCallComplete("operation-1", tool, {})
    agent._audit_permission(call, tool_name=tool, decision="deny", source_layer="native_boundary_unverified")
    assert receipts == [{"tool_name": tool, "decision": "deny", "source_layer": "native_boundary_unverified",
                         "operation_id": "operation-1", "permission_mode": mode.value,
                         "execution_backend": backend, "session_id": "session-current",
                         "args_sha256": hashlib.sha256(b"{}").hexdigest(),
                         "args_schema": ARGS_SCHEMA, "params_summary_truncated": False}]


def test_agent_audit_fingerprint_uses_full_arguments_and_is_order_stable():
    receipts = []
    agent = object.__new__(Agent)
    agent.audit_logger = SimpleNamespace(log_decision=lambda **fields: receipts.append(fields))
    agent.permission_checker = _audit_checker(PermissionMode.DEFAULT)
    agent.permission_mode = PermissionMode.DEFAULT
    agent.session_id = "session-current"

    first = {"nested": {"b": 2, "a": 1}, "tail": "same"}
    reordered = {"tail": "same", "nested": {"a": 1, "b": 2}}
    long_first = {"prefix": "p" * 240, "secret_field": "private-marker-alpha"}
    long_changed = {"prefix": "p" * 240, "secret_field": "private-marker-beta"}

    for arguments in (first, reordered, long_first, long_changed):
        agent._audit_permission(
            ToolCallComplete("operation-1", "Bash", arguments),
            tool_name="Bash",
            params_summary=str(arguments)[:200],
            decision="deny",
            source_layer="mode",
        )

    rows = receipts
    assert rows[0]["args_sha256"] == rows[1]["args_sha256"]
    assert rows[0]["args_schema"] == rows[1]["args_schema"] == ARGS_SCHEMA
    assert rows[2]["params_summary"] == rows[3]["params_summary"]
    assert rows[2]["params_summary_truncated"] is True
    assert rows[3]["params_summary_truncated"] is True
    assert rows[2]["args_sha256"] != rows[3]["args_sha256"]
    expected_full_digest = hashlib.sha256(json.dumps(
        long_first, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()
    assert rows[2]["args_sha256"] == expected_full_digest
    assert "private-marker-alpha" not in json.dumps(rows, ensure_ascii=False)
    assert "private-marker-beta" not in json.dumps(rows, ensure_ascii=False)


def test_agent_execution_paths_use_correlated_logger():
    tree = ast.parse(inspect.getsource(Agent))
    direct = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute) and node.func.attr == "log_decision"]
    assert len(direct) == 1  # the metadata helper, never an uncorrelated call site


@pytest.mark.asyncio
async def test_metrics_api_cannot_mix_sessions(monkeypatch):
    calls = []
    audit = SimpleNamespace(summary=lambda **fields: calls.append(fields) or {"count": 3})
    monkeypatch.setattr(server, "SESSION", SimpleNamespace(agent=SimpleNamespace(audit_logger=audit, session_id="current")))
    response = await server.api_chat_approval_metrics()
    assert json.loads(response.body) == {"count": 3}
    assert calls == [{"session_id": "current"}]


@pytest.mark.asyncio
async def test_metrics_api_without_current_session_does_not_read_all_audit(monkeypatch):
    def forbidden(**_fields):
        pytest.fail("must not read shared audit without a session filter")
    monkeypatch.setattr(server, "SESSION", SimpleNamespace(agent=SimpleNamespace(audit_logger=SimpleNamespace(summary=forbidden))))
    response = await server.api_chat_approval_metrics()
    assert json.loads(response.body)["unavailable"] is True
