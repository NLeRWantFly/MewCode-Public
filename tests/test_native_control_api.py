"""Native lifecycle controls are host-confirmed and current-task scoped."""
from __future__ import annotations

import json
import asyncio
import threading
from types import SimpleNamespace

import pytest

from frontend import server
from mewcode.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine
from mewcode.sandbox import native_gate, staged_execution, transaction_registry


class Request:
    def __init__(self, body=None, confirmed=True):
        self.headers = {"X-Mew-Native-Control": "1"} if confirmed else {}
        self.body = body or {"conversation_id": "conversation", "task_id": "task"}

    async def json(self):
        return self.body


@pytest.fixture
def context(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = transaction_registry.TransactionRegistry(tmp_path / "host")
    registry.root.mkdir()
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(workspace)), RuleEngine(), PermissionMode.AUTO)
    checker.task_id = "task"
    session = SimpleNamespace(session=SimpleNamespace(session_id="conversation"), work_dir=str(workspace),
        agent=SimpleNamespace(permission_checker=checker), any_chat_busy=lambda: False)
    monkeypatch.setattr(server, "SESSION", session)
    monkeypatch.setattr(server, "dialogue_focus", lambda _: {"task_id": "task"})
    monkeypatch.setattr(transaction_registry, "TransactionRegistry", lambda: registry)
    monkeypatch.setattr(native_gate, "native_gate_status", lambda: SimpleNamespace(
        state=native_gate.NativeGateState.PENDING, reason="synthetic pending", missing_evidence=("metadata_preserved",)))
    return workspace, registry, session, checker


@pytest.mark.asyncio
async def test_state_is_readonly_and_does_not_export_raw_transaction(context):
    workspace, registry, *_ = context
    registry.begin(workspace, task_id="other", policy_revision="a" * 64)
    result = await server.api_chat_native_execution()
    assert result.status_code == 200
    data = json.loads(result.body)
    assert data["gate"]["state"] == "pending"
    assert data["transactions"][0]["current_task"] is False
    assert set(data["transactions"][0]) == {"id", "task_id", "state", "cleanup_confirmed", "current_task", "private_only",
        "execution_receipt_present", "cleanup_evidence", "command_started", "diagnostic_code", "message", "next_step", "failure_type"}
    assert data["transactions"][0]["diagnostic_code"] == "preparation_interrupted"
    assert not (registry.root / "volume-stores.json").exists()
    assert registry.pending(workspace)[0]["state"] == "preparing"


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", ["api_chat_native_storage", "api_chat_native_reconcile"])
async def test_mutations_require_confirmation_and_current_task(context, handler):
    call = getattr(server, handler)
    assert (await call(Request(confirmed=False))).status_code == 403
    assert (await call(Request({"conversation_id": "other", "task_id": "task"}))).status_code == 400
    context[2].any_chat_busy = lambda: True
    assert (await call(Request())).status_code == 400


@pytest.mark.asyncio
async def test_storage_confirm_uses_revision_and_preserves_authority(context, tmp_path):
    from mewcode.sandbox.volume_stores import describe
    workspace, registry, _, checker = context
    directory = tmp_path / "chosen-store"
    directory.mkdir()
    revision = describe(registry, workspace)["revision"]
    original = checker.task_authorization
    payload = {"conversation_id": "conversation", "task_id": "task", "directory": str(directory), "revision": revision}
    result = await server.api_chat_native_storage(Request(payload))
    assert result.status_code == 200
    assert json.loads(result.body)["storage"]["directory"] == str(directory)
    assert checker.task_authorization is original
    stale = await server.api_chat_native_storage(Request(payload))
    assert stale.status_code == 400


@pytest.mark.asyncio
async def test_recovery_uses_current_host_scope_and_keeps_incomplete_visible(context, monkeypatch):
    workspace, _, _, checker = context
    observed = []
    def reconcile(scope):
        observed.append(scope)
        return [{"id": "synthetic", "state": "recovery_required", "reason": "cleanup unknown"}]
    monkeypatch.setattr(staged_execution, "reconcile", reconcile)
    result = await server.api_chat_native_reconcile(Request())
    assert result.status_code == 200
    assert json.loads(result.body)["complete"] is False
    assert observed[0].checker is checker and observed[0].workspace == workspace
    checker.task_id = "other"
    assert (await server.api_chat_native_reconcile(Request())).status_code == 400
    assert len(observed) == 1


@pytest.mark.asyncio
async def test_recovery_preview_forwards_exact_transaction_and_host_scope(context, monkeypatch):
    from mewcode.sandbox import recovery_handoff
    workspace, registry, _, checker = context
    transaction_id = "a" * 32
    observed = []

    def preview(scope, supplied_registry, supplied_id):
        observed.append((scope, supplied_registry, supplied_id))
        return {"transaction_id": supplied_id, "token": "one-shot-token", "targets": ["a.txt"]}

    monkeypatch.setattr(recovery_handoff, "preview", preview)
    result = await server.api_chat_native_recovery_preview(Request({
        "conversation_id": "conversation", "task_id": "task", "transaction_id": transaction_id,
    }))

    assert result.status_code == 200
    assert result.headers["Cache-Control"] == "no-store"
    assert json.loads(result.body)["preview"] == {
        "transaction_id": transaction_id, "token": "one-shot-token", "targets": ["a.txt"],
    }
    assert len(observed) == 1
    scope, supplied_registry, supplied_id = observed[0]
    assert scope.checker is checker and scope.workspace == workspace and scope.task_id == "task"
    assert supplied_registry is registry
    assert supplied_id == transaction_id


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"conversation_id": "stale-conversation", "task_id": "task", "transaction_id": "a" * 32},
    {"conversation_id": "conversation", "task_id": "stale-task", "transaction_id": "a" * 32},
])
async def test_recovery_preview_rejects_stale_conversation_or_task_before_preview(context, monkeypatch, body):
    from mewcode.sandbox import recovery_handoff
    called = []
    monkeypatch.setattr(recovery_handoff, "preview", lambda *_args: called.append(True))

    result = await server.api_chat_native_recovery_preview(Request(body))

    assert result.status_code == 400
    assert called == []


@pytest.mark.asyncio
async def test_recovery_preview_requires_control_header_and_is_excluded_while_busy(context, monkeypatch):
    from mewcode.sandbox import recovery_handoff
    body = {"conversation_id": "conversation", "task_id": "task", "transaction_id": "a" * 32}
    called = []
    monkeypatch.setattr(recovery_handoff, "preview", lambda *_args: called.append(True))

    missing_header = await server.api_chat_native_recovery_preview(Request(body, confirmed=False))
    assert missing_header.status_code == 403
    assert called == []

    context[2].any_chat_busy = lambda: True
    busy = await server.api_chat_native_recovery_preview(Request(body))
    assert busy.status_code == 400
    assert called == []


@pytest.mark.asyncio
async def test_recovery_confirmation_forwards_only_bound_preview_identity(context, monkeypatch):
    workspace, _, _, checker = context
    confirmation = {"transaction_id": "b" * 32, "token": "exact-preview-token"}
    payload = {"conversation_id": "conversation", "task_id": "task", "confirmation": confirmation}
    observed = []

    def reconcile(scope, *, confirmation=None):
        observed.append((scope, confirmation))
        return [{"id": "b" * 32, "state": "rolled_back"}]

    monkeypatch.setattr(staged_execution, "reconcile", reconcile)
    missing_header = await server.api_chat_native_reconcile(Request(payload, confirmed=False))
    assert missing_header.status_code == 403
    assert observed == []

    result = await server.api_chat_native_reconcile(Request(payload))

    assert result.status_code == 200
    assert json.loads(result.body)["complete"] is True
    assert len(observed) == 1
    scope, supplied_confirmation = observed[0]
    assert scope.checker is checker and scope.workspace == workspace and scope.task_id == "task"
    assert supplied_confirmation == confirmation

    assert (await server.api_chat_native_reconcile(Request({
        "conversation_id": "stale-conversation", "task_id": "task", "confirmation": confirmation,
    }))).status_code == 400
    assert (await server.api_chat_native_reconcile(Request({
        "conversation_id": "conversation", "task_id": "stale-task", "confirmation": confirmation,
    }))).status_code == 400
    assert len(observed) == 1

    context[2].any_chat_busy = lambda: True
    busy = await server.api_chat_native_reconcile(Request(payload))
    assert busy.status_code == 400
    assert len(observed) == 1


@pytest.mark.asyncio
async def test_control_reservation_outlives_http_cancellation_and_blocks_new_turn(context):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def operation():
        entered.set()
        try:
            assert release.wait(5)
        finally:
            finished.set()
    request = asyncio.create_task(server._run_native_control(operation))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert server._NATIVE_CONTROL_BUSY.is_set()
        with pytest.raises(ValueError, match="宿主正在"):
            server.WebSession.begin_conversation_turn(SimpleNamespace(starting=False))
        with pytest.raises(ValueError, match="宿主恢复"):
            await server._run_native_control(lambda: None)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert server._NATIVE_CONTROL_BUSY.is_set()
    finally:
        release.set()
        await asyncio.to_thread(finished.wait, 5)
        # Let the executor completion callback run; the worker clears first.
        await asyncio.sleep(0)
    assert not server._NATIVE_CONTROL_BUSY.is_set()
    await server._run_native_control(lambda: None)
