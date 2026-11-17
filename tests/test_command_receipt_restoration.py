from __future__ import annotations

import copy

from mewcode.experts.command_receipts import (
    execution_observation,
    reconcile_helper_receipts,
    reconcile_run_command_receipts,
    request_receipts,
)


def command_request():
    return {
        "command": "python --version",
        "purpose": "读取当前运行时版本",
        "input_scope": ["当前工作区和 Python 版本"],
        "forbidden_changes": ["不写文件、不改配置、不访问网络"],
        "deliverables": ["stdout、stderr 和退出码"],
        "verification_command": "python --version",
    }


def bash_tool(command, *, host_status, command_started, attempt=4):
    return {
        "name": "Bash",
        "args": {"command": command},
        "status": "queued" if command_started is False else "failed",
        "attempt": attempt,
        "host_observation": {
            "command_execution": execution_observation(
                host_status,
                command_started=command_started,
                reason="python_runtime_missing" if command_started is False else "process outcome unknown",
            )
        },
    }


def test_empty_stage_and_terminal_event_restore_bound_host_not_started_receipt():
    node_id, revision, dispatch_id = "N01", "revision-current", "dispatch-17e40"
    request = command_request()
    in_flight = request_receipts(node_id, revision, [request], status="in_flight")[0]
    in_flight.update(dispatch_id=dispatch_id, tool_id="toolcall_2t")
    unknown = {**copy.deepcopy(in_flight), "status": "outcome_unknown", "command_started": None}
    interrupted = bash_tool(
        request["command"], host_status="spawn_failed", command_started=False,
    )
    value = {
        "events": [
            {"node_id": node_id, "phase": "capability_escalation", "status": "running",
             "attempt": 4, "seq": 1284, "server_time": 100, "command_receipts": [in_flight]},
            {"node_id": node_id, "phase": "capability_escalation", "status": "needs_attention",
             "attempt": 4, "seq": 1286, "server_time": 104, "command_receipts": [unknown]},
            # A later terminal expert-stage checkpoint has no receipt payload.
            {"node_id": node_id, "phase": "expert_stage", "status": "needs_attention",
             "attempt": 4, "seq": 1287, "server_time": 105, "command_receipts": []},
        ],
        "node_states": {
            node_id: {
                "input_revision": revision,
                "command_receipts": [],
                "handoff": {"status": "blocked", "command_requests": []},
                "tools": {"submit-handoff": {
                    "name": "SubmitHandoff",
                    "status": "completed",
                    "is_error": False,
                    "args": {"command_requests": [request]},
                }},
            },
            node_id + "__command_probe": {
                "attempt": 4,
                "input_revision": revision,
                "command_dispatch_id": dispatch_id,
                "started_at": 101,
                "completed_at": 103,
                "tools": {"toolcall_2t": interrupted},
            },
        },
    }

    result = reconcile_run_command_receipts(value)

    rows = value["node_states"][node_id]["command_receipts"]
    assert result == {"corrected": 1, "unresolved": []}
    assert len(rows) == 1
    assert rows[0]["request_id"] == in_flight["request_id"]
    assert rows[0]["request"] == request
    assert rows[0]["input_revision"] == revision
    assert rows[0]["dispatch_id"] == dispatch_id
    assert rows[0]["tool_id"] == "toolcall_2t"
    assert rows[0]["status"] == "not_started"
    assert rows[0]["command_started"] is False
    assert rows[0]["evidence_source"] == "host_execution"
    assert rows[0]["exit_code"] is None


def test_missing_or_stale_revision_does_not_reconstruct_from_handoff_or_helper_text():
    node_id, current_revision = "N01", "revision-current"
    request = command_request()
    stale = request_receipts(node_id, "revision-old", [request], status="outcome_unknown")[0]
    stale.update(dispatch_id="old-dispatch", tool_id="old-tool")
    value = {
        "events": [{"node_id": node_id, "phase": "capability_escalation",
                    "status": "needs_attention", "attempt": 3, "seq": 1,
                    "command_receipts": [stale]}],
        "node_states": {
            node_id: {
                "input_revision": current_revision,
                "command_receipts": [],
                # A successful handoff is a source of the request, not proof that
                # a helper observation belongs to this revision.
                "tools": {"submit": {"name": "SubmitHandoff", "status": "completed",
                                      "args": {"command_requests": [request]}}},
            },
            node_id + "__command_probe": {
                "input_revision": "revision-old",
                "command_dispatch_id": "old-dispatch",
                "tools": {"old-tool": bash_tool(request["command"],
                    host_status="spawn_failed", command_started=False, attempt=3)},
            },
        },
    }

    reconcile_run_command_receipts(value)

    assert value["node_states"][node_id]["command_receipts"] == []


def test_new_queued_helper_attempt_cannot_hide_older_unknown_host_observation():
    request = command_request()
    rows = request_receipts("N01", "revision-current", [request], status="outcome_unknown")
    rows[0]["dispatch_id"] = "dispatch-current"
    helper = {
        "input_revision": "revision-current",
        "command_dispatch_id": "dispatch-current",
        "tools": {
            "older-attempt": bash_tool(request["command"], host_status="timed_out",
                                       command_started=True, attempt=2),
            "new-queued-attempt": bash_tool(request["command"], host_status="spawn_failed",
                                             command_started=False, attempt=3),
        },
    }

    assert reconcile_helper_receipts(rows, helper) == rows
