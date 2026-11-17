import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from mewcode.expert_runs import ExpertRunStore, MAX_PUBLIC_TOOLS_PER_ROLE


def source_snapshot():
    return {"run_id": "a" * 32, "conversation_id": "c", "status": "blocked",
            "node_states": {"review": {"role": "auditor", "status": "needs_attention", "tools": {}}},
            "events": [
                {"type": "expert_tool", "role": "auditor", "node_id": "review", "id": "read", "name": "ReadAuditArtifact", "args": {"page": 7}},
                {"type": "expert_tool_start", "role": "auditor", "node_id": "review", "id": "read", "name": "ReadAuditArtifact"},
                {"type": "expert_tool_result", "role": "auditor", "node_id": "review", "id": "read", "name": "ReadAuditArtifact", "output": "原页证据\ud800", "is_error": False},
            ]}


def test_failed_audit_tools_recovered_for_details_not_large_initial_summary():
    source = source_snapshot()
    original = copy.deepcopy(source)
    summary = ExpertRunStore._public_value(source, include_details=False)
    assert not any(e["type"].startswith("expert_tool") for e in summary["events"])
    detail = ExpertRunStore._public_value(source, role_filter="auditor")
    tools = [e for e in detail["events"] if e.get("id") == "read"]
    assert len(tools) == 1
    assert tools[0]["type"] == "expert_tool_result"
    assert tools[0]["args"] == {"page": 7}
    assert "原页证据" in tools[0]["output"]
    json.dumps(detail, ensure_ascii=False).encode("utf-8")
    assert source == original
    assert not any(e.get("id") == "read" for e in ExpertRunStore._public_value(source, role_filter="executor")["events"])


def test_recovered_tools_deduplicate_aggregate_and_bound_output():
    source = source_snapshot()
    source["node_states"]["review"]["tools"] = {"read": {
        "tool_id": "read", "name": "ReadAuditArtifact", "status": "completed", "output": "new aggregate"}}
    for i in range(100):
        source["events"].append({"type": "expert_tool_result", "role": "auditor", "node_id": "review",
                                 "id": f"other{i}", "name": "ReadFile", "output": "x" * 5000})
    detail = ExpertRunStore._public_value(source, role_filter="auditor")
    tools = [e for e in detail["events"] if e.get("id")]
    assert sum(e["id"] == "read" for e in tools) == 1
    assert next(e for e in tools if e["id"] == "read")["output"] == "new aggregate"
    assert len(tools) <= MAX_PUBLIC_TOOLS_PER_ROLE
    assert max(len(e.get("output", "")) for e in tools) <= 1500


def test_stopped_run_never_displays_unfinished_tool_as_still_running():
    source = source_snapshot()
    source["events"] = source["events"][:2]
    event = next(e for e in ExpertRunStore._public_value(source)["events"] if e.get("id") == "read")
    assert event["type"] == "expert_tool_state"
    assert event["status"] == "outcome_unknown"


@pytest.mark.asyncio
async def test_detail_api_isolates_node_attempt_and_member(monkeypatch):
    from frontend import server
    source = source_snapshot()
    source["node_states"]["review"].update(attempt=15, node_attempt=2)
    for event in source["events"]:
        event.update(attempt=15, node_attempt=2)
    source["events"].extend([
        {"type": "expert_tool_result", "node_id": "review", "role": "auditor", "attempt": 14,
         "node_attempt": 2, "id": "old", "output": "old"},
        {"type": "expert_tool_result", "node_id": "other-review", "role": "auditor", "attempt": 15,
         "node_attempt": 2, "id": "other", "output": "other"},
        {"type": "expert_tool_result", "node_id": "review", "role": "auditor", "attempt": 15,
         "node_attempt": 2, "member_id": "child", "id": "child", "output": "child"},
    ])
    monkeypatch.setattr(server, "EXPERT_RUNS", SimpleNamespace(
        public=lambda run_id, role: ExpertRunStore._public_value(source, role_filter=role)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.get("/api/expert-runs/" + source["run_id"],
                                    params={"node_id": "review", "attempt": 15, "node_attempt": 2})
    assert response.status_code == 200, response.text
    assert [event["id"] for event in response.json()["events"] if event.get("id")] == ["read"]
    assert set(response.json()["node_states"]) == {"review"}


def test_aggregate_text_usage_and_tools_keep_stage_identity():
    source = source_snapshot()
    source["node_states"]["review"].update(attempt=15, node_attempt=2, text="检查结果",
        usage={"input_tokens": 20}, tools={"read": {"tool_id": "read", "name": "ReadFile",
                                                  "status": "completed", "attempt": 15, "output": "result"}})
    events = ExpertRunStore._public_value(source)["events"]
    for event in events:
        if event.get("snapshot_aggregate") and event.get("node_id") == "review":
            assert event["attempt"] == 15
            assert event["node_attempt"] == 2
