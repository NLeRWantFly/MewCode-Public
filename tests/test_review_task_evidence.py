"""Regression for a 1,838-char old receipt displacing a 2,347-char live receipt."""
import copy
import hashlib
import json

import pytest

from mewcode.conversation import ConversationManager, Message, ToolUseBlock, ToolResultBlock
from mewcode.permissions.review_evidence import evidence_cache_material, MAX_TOTAL_BYTES
from mewcode.permissions.reviewer import REVIEWER_SYSTEM_PROMPT, SafetyReviewer


def observe(conv, call_id, payload, *, tool="BackgroundTasks", task_id="A", error=False, host=None):
    args = {"task_id": task_id} if task_id is not None else {}
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    conv.history.extend([
        Message(role="assistant", content="", tool_uses=[ToolUseBlock(call_id, tool, args)]),
        Message(role="user", content="", tool_results=[
            ToolResultBlock(call_id, text, error, host_observation=host)]),
    ])
    return text


def snapshot(*, phase="running", identity="A", size=2400):
    native = {} if phase is None else {
        "phase": phase, "process_started": phase in {"running", "cleaning", "finished"},
        "child_pid": 123, "helper_pid": 456, "request_id": "request-A",
        "request_sha256": "a" * 64, "transaction_id": "transaction-A",
        "execution_id": "execution-A", "cleanup_confirmed": None,
        "start_observed_at": "2026-09-28T17:05:14+08:00",
    }
    item = {"id": identity, "name": "acceptance-" + identity, "status": "running",
        "parent_task_id": "parent", "dispatch_turn_id": "turn",
        "native_execution": native, "task": "", "result": ""}
    item["task"] = "x" * max(0, size - len(json.dumps(item, ensure_ascii=False)))
    return item


def states(conv):
    return [json.loads(row["text"]) for row in SafetyReviewer._evidence_context(conv)]


def settled(identity):
    item = dict(snapshot(identity=identity, phase="finished", size=900),
        status="completed", cleanup_confirmed=True, cleanup_failed=False,
        cancellation_pending=False, dispatch_relation="existing_before_turn")
    item["native_execution"]["cleanup_confirmed"] = True
    return item


def inventory_of(rows):
    return next((json.loads(row["text"]) for row in rows
                 if json.loads(row["text"]).get("observation_kind") == "task_inventory_at_query_time"),
                None)


def test_preflight_inventory_is_complete_without_ambiguous_omission():
    conv = ConversationManager()
    conv.add_user_message("只有旧任务均结束且清理确认、A5/B5 不存在时才派发 A5")
    ids = ["A1", "A2", "A3", "A4", "B1", "B2", "B3", "B4"]
    tasks = [settled(identity) for identity in ids]
    observe(conv, "status", {"tasks": tasks, "permission_mode": "auto",
        "current_turn": {"turn_id": "turn", "dispatched_task_ids": []}},
        tool="AgentStatus", task_id=None)
    observe(conv, "list", tasks, task_id=None)
    observe(conv, "git-1", json.dumps({"branch": "main", "detail": "g" * 650}),
        tool="InspectGitState", task_id=None)
    observe(conv, "git-2", json.dumps({"branch": "main", "detail": "h" * 650}),
        tool="InspectGitState", task_id=None)
    rows = SafetyReviewer._evidence_context(conv)
    inventory = inventory_of(rows)
    assert inventory is not None and inventory["inventory_complete"] is True
    assert inventory["task_count"] == 8
    columns = inventory["columns"]
    assert [entry[columns.index("id")] for entry in inventory["tasks"]] == ids
    assert all(entry[columns.index("status")] == "completed" for entry in inventory["tasks"])
    assert all(entry[columns.index("cleanup_confirmed")] is True for entry in inventory["tasks"])
    assert not any("task_id" in row for row in rows)
    assert {"git-1", "git-2"} <= {row["tool_use_id"] for row in rows}
    assert not any(row.get("omission_reason") == "bounded_evidence_selection" for row in rows)


def test_newer_detail_supersedes_stale_inventory_columns():
    conv = ConversationManager()
    stale = dict(snapshot(identity="A", phase="queued"), dispatch_relation="current_turn")
    observe(conv, "inventory", [stale, settled("B1")], task_id=None)
    observe(conv, "A-started", snapshot())
    rows = SafetyReviewer._evidence_context(conv)
    inventory = inventory_of(rows)
    assert inventory["superseded_by_newer_observation_ids"] == ["A"]
    entry = next(item for item in inventory["tasks"] if item[0] == "A")
    assert entry[2:] == [None] * 8
    task_rows = [row for row in rows if row.get("task_id") == "A"]
    assert len(task_rows) == 1
    assert json.loads(task_rows[0]["text"])["native_execution"]["process_started"] is True


def test_inventory_outranks_newer_generic_rows():
    conv = ConversationManager()
    observe(conv, "list", [settled("B1"), settled("B2"), settled("B3")], task_id=None)
    for n in range(5):
        observe(conv, f"read-{n}", "file body " + str(n) + " " + "r" * 1200,
            tool="ReadFile", task_id=None)
    rows = SafetyReviewer._evidence_context(conv)
    assert inventory_of(rows) is not None
    notice = next((json.loads(row["text"]) for row in rows
                   if row.get("omission_reason") == "bounded_evidence_selection"), None)
    assert notice is not None
    assert notice["omitted_generic_observation_count"] >= 1
    assert notice["omitted_task_state_ids"] == []
    assert notice["omitted_task_inventory"] is False
    assert notice["absence_is_not_evidence"] is False


def test_oversize_inventory_is_explicit_with_count():
    conv = ConversationManager()
    items = [dict(settled(str(n)), name="acceptance-" + "n" * 40 + str(n)) for n in range(60)]
    observe(conv, "list", items, task_id=None)
    rows = SafetyReviewer._evidence_context(conv)
    parsed = [json.loads(row["text"]) for row in rows]
    assert any(row.get("observation") == "unavailable"
               and row["reason"] == "task_inventory_exceeds_view_limit"
               and row["task_count"] == 60 for row in parsed)
    assert not any("task_id" in row for row in rows)
    notice = next((row for row in parsed if row.get("reason") == "bounded_evidence_selection"), None)
    if notice is not None:
        assert notice["absence_is_not_evidence"] is True


def test_truncated_background_list_uses_host_observation():
    conv = ConversationManager()
    items = [dict(settled(str(n)), task_summary="s" * 240) for n in range(12)]
    text = json.dumps(items, ensure_ascii=False)
    assert len(text) > 10000
    host = [{k: v for k, v in item.items() if k != "task_summary"} for item in items]
    observe(conv, "list", text[:10000] + "\n… (output truncated)", task_id=None, host=host)
    rows = SafetyReviewer._evidence_context(conv)
    inventory = inventory_of(rows)
    assert inventory["inventory_complete"] is True and inventory["task_count"] == 12
    columns = inventory["columns"]
    assert [e[columns.index("id")] for e in inventory["tasks"]] == [str(n) for n in range(12)]
    row = next(r for r in rows if json.loads(r["text"]).get("observation_kind")
               == "task_inventory_at_query_time")
    assert row["observation_source"] == "host_structured_result"


def test_truncated_agent_status_uses_host_observation():
    conv = ConversationManager()
    tasks = [settled(identity) for identity in ("A1", "A2", "A3", "A4", "B1", "B2", "B3", "B4")]
    filler = [{"name": f"tool-{n:03d}", "registered": True, "enabled": True,
               "schema_loaded": True, "category": "read",
               "permission": "checked_per_call", "connection": "not_probed"}
              for n in range(120)]
    payload = {"host_work_dir": "W", "permission_mode": "auto",
        "runtime_route": {"provider_name": "p", "configured_model": "m"},
        "capabilities": {"tools": filler},
        "current_turn": {"turn_id": "turn", "dispatched_task_ids": []},
        "tasks": tasks}
    text = json.dumps(payload, ensure_ascii=False)
    assert len(text) > 10000
    host = {key: payload[key] for key in
            ("host_work_dir", "permission_mode", "runtime_route", "current_turn", "tasks")}
    observe(conv, "status", text[:10000] + "\n… (output truncated)",
        tool="AgentStatus", task_id=None, host=host)
    rows = SafetyReviewer._evidence_context(conv)
    inventory = inventory_of(rows)
    assert inventory["inventory_complete"] is True and inventory["task_count"] == 8
    assert inventory["permission_mode"] == "auto"
    assert inventory["current_turn"] == {"turn_id": "turn", "dispatched_task_ids": []}
    row = next(r for r in rows if json.loads(r["text"]).get("observation_kind")
               == "task_inventory_at_query_time")
    assert row["observation_source"] == "host_structured_result"


def agent_status_of(rows):
    return next((json.loads(row["text"]) for row in rows
                 if json.loads(row["text"]).get("observation_kind") == "agent_status_at_query_time"),
                None)


def test_agent_status_config_survives_newer_inventory():
    conv = ConversationManager()
    conv.add_user_message("预检含 AgentStatus；旧任务均结束且清理确认、A6/B6 不存在时才派发 A6")
    ids = [f"A{n}" for n in range(6)] + [f"B{n}" for n in range(6)]
    tasks = [settled(identity) for identity in ids]
    filler = [{"name": f"tool-{n:03d}", "registered": True, "enabled": True,
               "schema_loaded": True, "category": "read",
               "permission": "checked_per_call", "connection": "not_probed"}
              for n in range(120)]
    payload = {"host_work_dir": "W", "permission_mode": "auto",
        "runtime_route": {"configured_model": "gpt-6-luna"},
        "capabilities": {"tools": filler},
        "current_turn": {"turn_id": "turn", "dispatched_task_ids": []},
        "tasks": tasks}
    text = json.dumps(payload, ensure_ascii=False)
    assert len(text) > 10000
    host = {key: payload[key] for key in
            ("host_work_dir", "permission_mode", "runtime_route", "current_turn", "tasks")}
    observe(conv, "status", text[:10000] + "\n… (output truncated)",
        tool="AgentStatus", task_id=None, host=host)
    list_text = json.dumps(tasks, ensure_ascii=False)
    assert len(list_text) > 10000
    observe(conv, "list", list_text[:10000] + "\n… (output truncated)",
        task_id=None, host=tasks)
    observe(conv, "git-1", json.dumps({"branch": "main", "detail": "g" * 1450}),
        tool="InspectGitState", task_id=None)
    observe(conv, "git-2", json.dumps({"branch": "main", "detail": "h" * 1450}),
        tool="InspectGitState", task_id=None)
    rows = SafetyReviewer._evidence_context(conv)
    inventory = inventory_of(rows)
    assert inventory is not None and inventory["task_count"] == 12
    inv_row = next(r for r in rows if json.loads(r["text"]).get("observation_kind")
                   == "task_inventory_at_query_time")
    assert inv_row["tool"] == "BackgroundTasks"
    status = agent_status_of(rows)
    assert status is not None
    assert status["tasks_superseded_by_newer_inventory"] is True
    assert status["permission_mode"] == "auto"
    assert status["current_turn"]["dispatched_task_ids"] == []
    assert "tasks" not in status
    assert {"git-1", "git-2"} <= {row["tool_use_id"] for row in rows}
    assert not any(row.get("omission_reason") == "bounded_evidence_selection" for row in rows)


def test_only_latest_agent_status_config_is_kept():
    conv = ConversationManager()
    observe(conv, "status-old", {"permission_mode": "default", "tasks": []},
        tool="AgentStatus", task_id=None)
    observe(conv, "status-new", {"permission_mode": "auto", "tasks": []},
        tool="AgentStatus", task_id=None)
    observe(conv, "list", [settled("A1")], task_id=None)
    rows = SafetyReviewer._evidence_context(conv)
    statuses = [json.loads(row["text"]) for row in rows
                if json.loads(row["text"]).get("observation_kind") == "agent_status_at_query_time"]
    assert len(statuses) == 1 and statuses[0]["permission_mode"] == "auto"


def test_latest_agent_status_inventory_needs_no_separate_config_row():
    conv = ConversationManager()
    observe(conv, "list", [settled("A1")], task_id=None)
    observe(conv, "status",
        {"permission_mode": "auto", "tasks": [settled("A1")]},
        tool="AgentStatus", task_id=None)
    rows = SafetyReviewer._evidence_context(conv)
    inv_row = next(r for r in rows if json.loads(r["text"]).get("observation_kind")
                   == "task_inventory_at_query_time")
    assert inv_row["tool"] == "AgentStatus"
    inventory = inventory_of(rows)
    assert inventory["permission_mode"] == "auto" and inventory["task_count"] == 1
    assert agent_status_of(rows) is None


def test_unreadable_agent_status_config_is_explicit():
    conv = ConversationManager()
    filler = [{"name": f"tool-{n:03d}", "registered": True, "enabled": True,
               "schema_loaded": True, "category": "read",
               "permission": "checked_per_call", "connection": "not_probed"}
              for n in range(120)]
    payload = {"permission_mode": "auto",
               "capabilities": {"tools": filler},
               "tasks": [settled("A1")]}
    text = json.dumps(payload, ensure_ascii=False)
    assert len(text) > 10000
    observe(conv, "status", text[:10000] + "\n… (output truncated)",
        tool="AgentStatus", task_id=None)
    observe(conv, "list", [settled("A1")], task_id=None)
    rows = SafetyReviewer._evidence_context(conv)
    unavailable = [r for r in rows
                   if json.loads(r["text"]).get("observation") == "unavailable"]
    assert len(unavailable) == 1
    parsed = json.loads(unavailable[0]["text"])
    assert parsed["reason"] == "latest_agent_status_failed_or_invalid"
    assert unavailable[0]["observation_source"] == "tool_output_text"
    assert agent_status_of(rows) is None


def test_agent_status_omission_is_named():
    conv = ConversationManager()
    observe(conv, "status",
        {"permission_mode": "auto",
         "current_turn": {"turn_id": "turn", "dispatched_task_ids": []}, "tasks": []},
        tool="AgentStatus", task_id=None)
    observe(conv, "list", [settled("A1")], task_id=None)
    for n in range(8):
        observe(conv, f"detail-{n}",
            snapshot(identity=f"d{n}", phase="running"), task_id=f"d{n}")
    rows = SafetyReviewer._evidence_context(conv)
    notice = next((json.loads(row["text"]) for row in rows
                   if json.loads(row["text"]).get("reason") == "bounded_evidence_selection"),
                  None)
    assert notice is not None and notice["omitted_agent_status"] is True


@pytest.mark.asyncio
async def test_review_payload_carries_agent_status_and_inventory(tmp_path):
    payloads = []

    class Client:
        async def review_safety(self, payload, system, **kwargs):
            payloads.append(payload)
            return {"outcome": "deny", "risk_level": "low",
                    "blocker_kind": "insufficient_evidence",
                    "rationale": "Synthetic refusal: do not execute or bypass review"}

    reviewer = SafetyReviewer(Client(), str(tmp_path))
    conv = ConversationManager()
    conv.add_user_message("预检含 AgentStatus；旧任务均结束且清理确认、A6/B6 不存在时才派发 A6")
    tasks = [settled(identity) for identity in
             [f"A{n}" for n in range(6)] + [f"B{n}" for n in range(6)]]
    filler = [{"name": f"tool-{n:03d}"} for n in range(120)]
    payload = {"host_work_dir": "W", "permission_mode": "auto",
        "runtime_route": {"configured_model": "gpt-6-luna"},
        "capabilities": {"tools": filler},
        "current_turn": {"turn_id": "turn", "dispatched_task_ids": []},
        "tasks": tasks}
    text = json.dumps(payload, ensure_ascii=False)
    assert len(text) > 10000
    host = {key: payload[key] for key in
            ("host_work_dir", "permission_mode", "runtime_route", "current_turn", "tasks")}
    observe(conv, "status", text[:10000] + "\n… (output truncated)",
        tool="AgentStatus", task_id=None, host=host)
    list_text = json.dumps(tasks, ensure_ascii=False)
    observe(conv, "list", list_text[:10000] + "\n… (output truncated)",
        task_id=None, host=tasks)
    await reviewer.review(tool_name="Agent", arguments={"name": "A6"},
                          risk={}, conversation=conv)
    parsed = [json.loads(row["text"]) for row in payloads[-1]["untrusted_tool_evidence"]]
    assert any(row.get("observation_kind") == "task_inventory_at_query_time" for row in parsed)
    assert any(row.get("observation_kind") == "agent_status_at_query_time" for row in parsed)
    assert "tasks_superseded_by_newer_inventory=true" in REVIEWER_SYSTEM_PROMPT


def test_truncated_output_without_host_observation_fails_closed():
    conv = ConversationManager()
    items = [dict(settled(str(n)), task_summary="s" * 240) for n in range(12)]
    text = json.dumps(items, ensure_ascii=False)
    assert len(text) > 10000
    observe(conv, "list", text[:10000] + "\n… (output truncated)", task_id=None)
    rows = SafetyReviewer._evidence_context(conv)
    assert inventory_of(rows) is None
    assert len(rows) == 1
    parsed = json.loads(rows[0]["text"])
    assert parsed["observation"] == "unavailable"
    assert parsed["reason"] == "latest_task_inventory_failed_or_invalid"
    assert rows[0]["observation_source"] == "tool_output_text"


def test_host_observation_ignored_for_other_tools():
    conv = ConversationManager()
    observe(conv, "read", "plain file text", tool="ReadFile", task_id=None,
        host={"id": "A", "status": "running", "native_execution": {"process_started": True}})
    rows = SafetyReviewer._evidence_context(conv)
    assert len(rows) == 1 and "task_id" not in rows[0]
    assert rows[0]["tool"] == "ReadFile" and rows[0]["text"] == "plain file text"
    assert "observation_source" not in rows[0]


def test_error_result_with_host_observation_stays_unavailable():
    conv = ConversationManager()
    observe(conv, "list", "query failed", task_id=None, error=True,
        host=[{"id": "A", "status": "completed"}])
    rows = SafetyReviewer._evidence_context(conv)
    assert len(rows) == 1
    parsed = json.loads(rows[0]["text"])
    assert parsed["observation"] == "unavailable"
    assert parsed["reason"] == "latest_task_inventory_failed_or_invalid"
    assert rows[0]["observation_source"] == "tool_output_text"


def test_real_sized_startup_receipt_replaces_old_empty_state_without_mutating_history():
    conv = ConversationManager()
    old = observe(conv, "before", snapshot(phase=None, size=1838))
    latest = observe(conv, "started", snapshot(size=2347))
    before = copy.deepcopy(conv.history)
    assert len(old) == 1838 and len(latest) == 2347
    rows = SafetyReviewer._evidence_context(conv)
    assert len(rows) == 1 and rows[0]["tool_use_id"] == "started"
    state = json.loads(rows[0]["text"])
    assert state["native_execution"] == snapshot()["native_execution"]
    assert state["parent_task_id"] == "parent" and state["dispatch_turn_id"] == "turn"
    assert "task" not in state and "result" not in state
    assert rows[0]["source_sha256"] == hashlib.sha256(latest.encode()).hexdigest()
    assert not rows[0]["complete"] and rows[0]["omission_reason"]
    assert rows[0]["trusted_authorization"] is False
    assert conv.history == before


@pytest.mark.parametrize("tool", ["BackgroundTasks", "AgentStatus"])
def test_latest_full_inventory_supersedes_old_detail_and_keeps_all_native_fields(tool):
    conv = ConversationManager()
    observe(conv, "started", snapshot())
    cancelled = snapshot(phase="cleaning")
    cancelled.update(cancellation_pending=True, cleanup_confirmed=False, cleanup_failed=True)
    cancelled["native_execution"].update(cleanup_confirmed=False, outcome="cancelled")
    payload = [cancelled] if tool == "BackgroundTasks" else {"tasks": [cancelled], "permission_mode": "auto"}
    observe(conv, "new-list", payload, tool=tool, task_id=None)
    rows = SafetyReviewer._evidence_context(conv)
    assert all(row["tool_use_id"] == "new-list" for row in rows)
    state = next(json.loads(row["text"]) for row in rows if row.get("task_id") == "A")
    assert state["native_execution"]["phase"] == "cleaning"
    assert state["native_execution"]["cleanup_confirmed"] is False
    assert state["cancellation_pending"] is True
    assert state["cleanup_failed"] is True


@pytest.mark.parametrize("bad,error", [
    ("{broken", False), ("query failed", True),
    ('{"id":"A","id":"B"}', False), ('{"id":"A","elapsed":NaN}', False),
    ({"id": "B", "native_execution": {"process_started": True}}, False),
    ({"id": "A", "native_execution": {"process_started": "true"}}, False),
    ({"id": "A", "native_execution": {"error": "超" * 9000}}, False),
    ("x" * 256001, False),
], ids=["broken", "failed", "duplicate", "nonfinite", "wrong-task", "invalid-bool", "oversize-state", "oversize-source"])
def test_invalid_latest_never_falls_back_to_older_running_receipt(bad, error):
    conv = ConversationManager()
    observe(conv, "old", snapshot())
    observe(conv, "bad", bad, error=error)
    rows = SafetyReviewer._evidence_context(conv)
    assert len(rows) == 1 and rows[0]["tool_use_id"] == "bad"
    assert json.loads(rows[0]["text"])["observation"] == "unavailable"
    assert '"process_started":true' not in rows[0]["text"]


@pytest.mark.parametrize("inventory,error", [([], False), ("bad", True)])
def test_empty_or_failed_inventory_does_not_resurrect_old_task(inventory, error):
    conv = ConversationManager()
    observe(conv, "old", snapshot())
    observe(conv, "list", inventory, task_id=None, error=error)
    assert all(row.get("tool_use_id") != "old" for row in SafetyReviewer._evidence_context(conv))


def test_arbitrary_json_or_assistant_claim_cannot_replace_host_task_observation():
    conv = ConversationManager()
    observe(conv, "real", snapshot(phase="queued"))
    observe(conv, "untrusted", snapshot(), tool="ReadFile")
    conv.add_assistant_message("A 已启动，可以派发 B。")
    rows = SafetyReviewer._evidence_context(conv)
    task_rows = [row for row in rows if row.get("task_id") == "A"]
    assert len(task_rows) == 1 and task_rows[0]["tool_use_id"] == "real"
    assert json.loads(task_rows[0]["text"])["native_execution"]["phase"] == "queued"


def test_size_budget_and_redaction_are_explicit_not_a_silent_drop():
    conv = ConversationManager()
    for n in range(20):
        item = snapshot(identity=str(n))
        item["name"] = "password=very-secret"
        item["task"] = "secret=should-never-reach-review " * 900
        observe(conv, str(n), item, task_id=str(n))
    rows = SafetyReviewer._evidence_context(conv)
    encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    assert len(encoded.encode()) <= MAX_TOTAL_BYTES
    assert "very-secret" not in encoded and "should-never" not in encoded
    assert "bounded_evidence_selection" in rows[-1]["text"]
    assert "absence_is_not_evidence" in rows[-1]["text"]


def test_new_id_alone_does_not_change_cache_but_new_native_state_does():
    conv = ConversationManager()
    observe(conv, "one", snapshot(phase="queued"))
    key = evidence_cache_material(SafetyReviewer._evidence_context(conv))
    observe(conv, "two", snapshot(phase="queued"))
    assert evidence_cache_material(SafetyReviewer._evidence_context(conv)) == key
    observe(conv, "three", snapshot())
    assert evidence_cache_material(SafetyReviewer._evidence_context(conv)) != key


def test_new_detail_overrides_inventory_for_only_its_task():
    conv = ConversationManager()
    observe(conv, "inventory", [snapshot(phase=None), snapshot(identity="B", phase="queued")], task_id=None)
    observe(conv, "A-started", snapshot())
    rows = SafetyReviewer._evidence_context(conv)
    by_id = {r["task_id"]: json.loads(r["text"]) for r in rows if r.get("task_id")}
    assert by_id["A"]["native_execution"]["phase"] == "running"
    assert by_id["B"]["native_execution"]["phase"] == "queued"
    assert by_id["B"]["native_execution"]["process_started"] is False


def test_budget_omission_does_not_resurrect_old_short_state():
    conv = ConversationManager()
    observe(conv, "old-empty", snapshot(phase=None, size=300))
    observe(conv, "new-started", snapshot())
    for n in range(6):
        observe(conv, f"newer-{n}", snapshot(identity=str(n)), task_id=str(n))
    rows = SafetyReviewer._evidence_context(conv)
    assert not any(r.get("tool_use_id") == "old-empty" for r in rows)
    assert rows[-1]["omission_reason"] == "bounded_evidence_selection"


def test_active_task_not_crowded_out_by_completed_history_in_inventory():
    conv = ConversationManager()
    items = [settled(str(n)) for n in range(12)]
    items.append(dict(snapshot(), dispatch_relation="current_turn"))
    observe(conv, "list", items, task_id=None)
    rows = SafetyReviewer._evidence_context(conv)
    details = [row for row in rows if row.get("task_id")]
    assert len(details) == 1 and details[0]["task_id"] == "A"
    assert json.loads(details[0]["text"])["native_execution"]["process_started"] is True
    inventory = inventory_of(rows)
    assert inventory["task_count"] == 13
    assert inventory["tasks"][-1][1] == "acceptance-A"
    assert inventory["current_turn_task_ids"] == ["A"]
    assert not any(row.get("omission_reason") == "bounded_evidence_selection" for row in rows)


def test_unbound_json_is_not_promoted_to_a_host_snapshot():
    conv = ConversationManager()
    conv.add_tool_results_message([ToolResultBlock("unknown", json.dumps(snapshot(size=700)))])
    row = SafetyReviewer._evidence_context(conv)[0]
    assert row["tool"] == "unbound_tool_result" and "task_id" not in row
    assert not row["trusted_authorization"]


def test_invalid_query_identity_and_long_generic_output_stay_explicit():
    conv = ConversationManager()
    observe(conv, "bad-args", snapshot(), task_id=["A"])
    observe(conv, "long", "large " * 2000, tool="ReadFile")
    rows = SafetyReviewer._evidence_context(conv)
    assert len(rows) == 2
    assert all(json.loads(r["text"])["observation"] == "unavailable" for r in rows)
    assert all(not r["complete"] for r in rows)


@pytest.mark.asyncio
async def test_actual_review_payload_contains_latest_startup_and_deny_cache_invalidates(tmp_path):
    payloads = []

    class Client:
        async def review_safety(self, payload, system, **kwargs):
            payloads.append(payload)
            return {"outcome": "deny", "risk_level": "low", "blocker_kind": "insufficient_evidence",
                    "rationale": "Synthetic refusal: do not execute or bypass review"}

    reviewer = SafetyReviewer(Client(), str(tmp_path))
    conv = ConversationManager()
    conv.add_user_message("B only after A starts; no change to permissions")
    args = dict(tool_name="Agent", arguments={"name": "B"}, risk={}, conversation=conv)
    observe(conv, "old", snapshot(phase=None, size=1838))
    assert not (await reviewer.review(**args)).allowed
    assert (await reviewer.review(**args)).cache_hit
    observe(conv, "new", snapshot(size=2347))
    latest = await reviewer.review(**args)
    assert not latest.allowed and not latest.cache_hit and len(payloads) == 2
    payload = payloads[-1]
    state = json.loads(payload["untrusted_tool_evidence"][0]["text"])
    assert state["native_execution"]["process_started"] is True
    assert payload["envelope"]["omissions"] and not payload["envelope"]["complete"]
    assert payload["envelope"]["planned_action_complete"] is True
    assert payload["source_bindings"][-1]["trusted_authorization"] is False
    assert not payload["source_bindings"][-1]["complete"]
    assert payload["authorized_task"] == payloads[0]["authorized_task"]


@pytest.mark.asyncio
async def test_review_payload_carries_complete_preflight_inventory(tmp_path):
    payloads = []

    class Client:
        async def review_safety(self, payload, system, **kwargs):
            payloads.append(payload)
            return {"outcome": "deny", "risk_level": "low", "blocker_kind": "insufficient_evidence",
                    "rationale": "Synthetic refusal: do not execute or bypass review"}

    reviewer = SafetyReviewer(Client(), str(tmp_path))
    conv = ConversationManager()
    conv.add_user_message("只有旧任务均结束且清理确认、A5/B5 不存在时才派发 A5")
    ids = ["A1", "A2", "A3", "A4", "B1", "B2", "B3", "B4"]
    tasks = [settled(identity) for identity in ids]
    observe(conv, "status", {"tasks": tasks, "permission_mode": "auto",
        "current_turn": {"turn_id": "turn", "dispatched_task_ids": []}},
        tool="AgentStatus", task_id=None)
    observe(conv, "list", tasks, task_id=None)
    observe(conv, "git-1", json.dumps({"branch": "main", "detail": "g" * 650}),
        tool="InspectGitState", task_id=None)
    observe(conv, "git-2", json.dumps({"branch": "main", "detail": "h" * 650}),
        tool="InspectGitState", task_id=None)
    await reviewer.review(tool_name="Agent", arguments={"name": "A5"}, risk={}, conversation=conv)
    parsed = [json.loads(row["text"]) for row in payloads[-1]["untrusted_tool_evidence"]]
    inventory = next((row for row in parsed
                      if row.get("observation_kind") == "task_inventory_at_query_time"), None)
    assert inventory is not None
    assert inventory["task_count"] == 8 and inventory["inventory_complete"] is True
    assert "inventory_complete=true" in REVIEWER_SYSTEM_PROMPT
