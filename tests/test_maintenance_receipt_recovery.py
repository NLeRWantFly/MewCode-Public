"""Runtime repair closes command receipt blockers only from bound helper proof."""
import os
import tempfile
from types import SimpleNamespace

import httpx
import pytest

# frontend.server creates user-level stores at import time. Keep those stores
# out of the real profile if this file is run without the isolated test env.
_TEST_USER_DATA = tempfile.TemporaryDirectory(prefix="mewcode-maintenance-receipts-")
os.environ.setdefault("MEWCODE_USER_DATA_DIR", _TEST_USER_DATA.name)

from frontend import server
from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.execution_scope import prepare_orchestration_scope
from mewcode.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine
from tests.test_orchestration_maintenance import maintenance_env as maintenance_env, spec_for


@pytest.mark.asyncio
@pytest.mark.parametrize(("binding", "expected_ready"), [
    ("dispatch_id", True), ("tool_id", True), (None, False), ("python_missing", False)])
async def test_runtime_repair_reconciles_only_bound_command_receipts(
        maintenance_env, monkeypatch, tmp_path, binding, expected_ready):
    app, owner, _, durable, spec = maintenance_env

    identity = "c" * 32
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(owner["project"]),
                                RuleEngine(), PermissionMode.AUTO)
    agent = SimpleNamespace(permission_checker=checker,
                            safety_reviewer=SimpleNamespace(authorization_version=""))
    monkeypatch.setattr(server.SESSION, "agent", agent, raising=False)
    monkeypatch.setattr(server.SESSION, "web_state", {}, raising=False)
    host = SimpleNamespace(owner=server.SESSION, conversation_id=owner["conversation_id"],
        work_dir=owner["project"], session=durable, agent=agent)
    scope = prepare_orchestration_scope(host, task="原业务请求", run_id=identity)

    receipt = {"request_id": "request-1", "node_id": "work", "input_revision": "revision-1",
        "request": {"command": "python --version"}, "status": "outcome_unknown",
        "command_started": None, "dispatch_id": "dispatch-1", "tool_id": "tool-1",
        "stdout": None, "stderr": None, "exit_code": None, "evidence_source": "host_dispatch"}
    helper_tool_id = "tool-1" if binding == "tool_id" else "unrelated-tool"
    helper_dispatch_id = "dispatch-1" if binding == "dispatch_id" else "unrelated-dispatch"
    helper = {"input_revision": "revision-1", "command_dispatch_id": helper_dispatch_id,
        "tools": {helper_tool_id: {"name": "Bash", "status": "completed",
            "args": {"command": "python --version"}, "output": "Python 3.12",
            "host_observation": {"command_execution": {"status": "exited", "command_started": True,
                "stdout": "Python 3.12", "stderr": "", "exit_code": 0}}}}}
    if binding == "python_missing":
        receipt.update(status="not_started", command_started=False, error_code="python_runtime_missing")
        helper["tools"] = {}

    server.EXPERT_RUNS.create(run_id=identity, conversation_id=owner["conversation_id"],
        task="原业务请求", attachments=[], profile={}, strategy="stable", flow=to_expert_flow(spec))
    def initialize(value):
        value.update(status="paused", stage_cursor="work", host_execution_scope=scope,
            node_states={"work": {"status": "failed", "input_revision": "revision-1",
                                   "command_receipts": []},
                         "work__command_probe": helper},
            events=[{"type": "expert_flow_state", "node_id": "work", "seq": 1,
                     "command_receipts": [receipt]}])
    before = server.EXPERT_RUNS.mutate(identity, initialize)

    from mewcode.sandbox import native_recovery
    recovery_calls = []
    monkeypatch.setattr(native_recovery, "recover_native_runtime",
        lambda: recovery_calls.append(True) or {"ready": True, "action": "already_ready"})
    cas_calls = []
    mutate_stopped = server.EXPERT_RUNS.mutate_stopped
    def track_cas(run_id, callback, *, conversation_id, expected_updated_at):
        cas_calls.append((run_id, conversation_id, expected_updated_at))
        return mutate_stopped(run_id, callback, conversation_id=conversation_id,
                              expected_updated_at=expected_updated_at)
    monkeypatch.setattr(server.EXPERT_RUNS, "mutate_stopped", track_cas)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        opened = await client.post("/api/orchestration/maintenance/open",
            json={**owner, "mode": "fix"})
        assert opened.status_code == 200, opened.text
        state = opened.json()
        response = await client.post("/api/orchestration/maintenance/repair-runtime",
            json={**owner, "id": state["id"], "revision": state["revision"]})
        assert response.status_code == 200, response.text
        report = response.json()["runtime_repair"]

    after = server.EXPERT_RUNS.load(identity)
    saved = after["node_states"]["work"]["command_receipts"][0]
    assert report["native_ready"] is True
    assert report["task_identity_restored"] is True
    assert report["action"] == "already_ready"
    assert report["ready"] is expected_ready
    assert recovery_calls == [True]
    assert cas_calls == [(identity, owner["conversation_id"], before["updated_at"])]
    assert after["updated_at"] != before["updated_at"] and after["status"] == "paused"

    checkpoint_id = report["checkpoint_id"]
    meta, checkpoint = server.EXPERT_RUNS.checkpoint_archive.load(identity, checkpoint_id)
    assert meta["kind"] == "before_runtime_maintenance"
    assert checkpoint["node_states"]["work"]["command_receipts"] == []
    if binding == "python_missing":
        assert saved["status"] == "not_started" and saved["command_started"] is False
        assert report["command_receipt_blockers"] == []
        assert report["project_environment_blocked"] is True
        assert report["project_python"]["available"] is False
        assert "项目 Python 环境" in report["reason"]
    elif expected_ready:
        assert saved["status"] == "completed" and saved["exit_code"] == 0
        assert report["command_receipt_blockers"] == []
        assert report["command_receipt_reconciliation"]["corrected"] == 1
    else:
        assert saved["status"] == "outcome_unknown"
        assert report["command_receipt_blockers"] == [
            {"node_id": "work", "request_id": "request-1", "status": "outcome_unknown"}]
        assert report["command_receipt_reconciliation"]["corrected"] == 0
        assert "未解决命令回执" in report["reason"]
