from __future__ import annotations

import json

from mewcode.conversation import Message
from mewcode.expert_runs import (
    ExpertRunStore, aggregate_snapshot_usage, interval_union_seconds,
    parse_resume_message, redact_for_display, strategy_for_multi_agent,
)
from mewcode.expert_web import (
    _blocked_run_reason,
    _human_input_cancelled,
    _restore_pending_permission,
    _unresolved_hitl_reason,
    visible_chat_index,
)
from mewcode.expert_group import ExpertRunResult


class _PendingFuture:
    def __init__(self, done: bool = False) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


def _blocked_result(final_summary: str) -> ExpertRunResult:
    return ExpertRunResult(
        run_id="r", status="blocked", strategy="stable", manager_summary="",
        planner_summary="", executor_summary="", audit_summary="",
        final_summary=final_summary,
    )


def test_blocked_run_reason_prefers_structured_user_question_over_full_report():
    reason = _blocked_run_reason(_blocked_result("progress\n\n## 结论\n" + "报告" * 500), {
        "stage_cursor": "probe",
        "node_states": {"probe": {
            "status": "needs_attention",
            "handoff": {"blocker_kind": "user_input", "question": "请贴回只读探测命令的输出。"},
        }},
    })

    assert reason == "等待用户输入：请贴回只读探测命令的输出。"


def test_blocked_run_reason_surfaces_executor_failure_and_never_full_report():
    reason = _blocked_run_reason(_blocked_result("I'll scan first.\n" + "完整报告" * 500), {
        "stage_cursor": "probe",
        "node_states": {"probe": {
            "status": "needs_attention",
            "handoff": {"escalation_receipt": {
                "status": "needs_attention",
                "text": "API error (403): INSUFFICIENT_BALANCE\n请求路由：Claude",
            }},
        }},
    })

    assert reason == "执行器转派失败：API error (403): INSUFFICIENT_BALANCE 请求路由：Claude"
    assert "完整报告" not in reason


def test_blocked_run_reason_uses_safe_generic_fallback_for_multiline_report():
    assert _blocked_run_reason(_blocked_result("progress\n## report"), {}) == (
        "存在未完成节点；请打开执行详情查看阻塞项。"
    )


def test_productive_current_handoff_does_not_surface_an_old_service_failure():
    stage = {"status": "continuing", "handoff": {"blocker_kind": "internal_work", "open_items": ["finish"]}}
    snapshot = {"stage_cursor": "work", "node_states": {"old-review": {
        "status": "needs_attention", "request_error": "old balance failure"}, "work": stage}}
    assert _blocked_run_reason(_blocked_result("progress\nreport"), snapshot) == "已有执行进展，仍有 1 项内部工作；可从保存点继续。"
    stage["request_error"] = "current real failure"
    assert _blocked_run_reason(_blocked_result("progress\nreport"), snapshot) == "current real failure"
    stage["request_error"] = ""
    stage["handoff"]["blockers"] = ["actual permission boundary"]
    assert _blocked_run_reason(_blocked_result("progress\nreport"), snapshot) == "actual permission boundary"


def test_collaboration_current_gate_precedes_historical_acceptance_failure():
    from mewcode.kernel import collaboration as ledger
    from mewcode.kernel.collaboration_recovery import open_gate
    from mewcode.orchestration.collab_compile import compile_plan
    from tests.test_autonomous_fragment import _sample_flow

    snapshot = {"run_id": "r"}
    value = ledger.initialize(snapshot, compile_plan(_sample_flow()))
    ledger.dispatch(value, "work")
    open_gate(snapshot, scope="contract:work", keys=["work"], label="Work",
              reason="nodes.0.acceptance: Input should be a valid list", kind="collaboration_checkpoint")
    snapshot.update(stage_cursor="work", node_states={
        "work": {"status": "blocked", "request_error": ""},
        "work__acceptance_old": {"status": "failed", "request_error": "old Claude 403 balance failure"},
    })
    result = _blocked_result("任务尚未通过完整验收。\nnodes.0.acceptance: Input should be a valid list")
    assert _blocked_run_reason(result, snapshot) == "nodes.0.acceptance: Input should be a valid list"
    gate = next(iter(value["recovery_gates"].values()))
    gate["status"] = "resolved"
    assert "Claude" not in _blocked_run_reason(result, snapshot)
    gate["status"] = "waiting"
    gate["basis"] = "stale"
    snapshot["node_states"]["work"]["request_error"] = "current real worker failure"
    assert _blocked_run_reason(result, snapshot) == "current real worker failure"
    assert snapshot["node_states"]["work__acceptance_old"]["request_error"] == "old Claude 403 balance failure"


def test_resume_clears_old_stop_banner_without_resetting_progress_or_watchdogs(tmp_path):
    store = ExpertRunStore(tmp_path)
    run_id = "c" * 32
    store.create(run_id=run_id, conversation_id="c", task="t", attachments=[], profile={}, strategy="stable")
    store.mutate(run_id, lambda snapshot: snapshot.update(
        node_states={"executor": {"status": "completed", "text": "already delivered"}},
        flow_state={"phase": "done", "status": "blocked", "reason": "old error", "rework_rounds": 2},
        audit_protocols={"audit": {"round": 2, "no_progress": 1}},
        loop_states={"loop": {"round": 2, "tokens": 123}},
        pause_dialogue_anchor={"record_offset": 120, "attempt": 1},
        pending_dialogue_handoff={"action": "resume", "resume_context": "debugged"},
        dialogue_handoff_reviews=[{"decision": {"action": "resume"}}],
    ))
    store.transition(run_id, "blocked", reason="old error")
    resumed = store.resume(run_id, strategy="stable")
    assert resumed["status"] == "running" and resumed["status_reason"] == ""
    assert resumed["flow_state"]["phase"] == "resuming"
    assert resumed["flow_state"]["rework_rounds"] == 2
    assert resumed["audit_protocols"]["audit"] == {"round": 2, "no_progress": 1}
    assert resumed["loop_states"]["loop"] == {"round": 2, "tokens": 123}
    assert resumed["node_states"]["executor"]["status"] == "completed"
    assert "pause_dialogue_anchor" not in resumed
    assert "pending_dialogue_handoff" not in resumed
    assert resumed["dialogue_handoff_reviews"][-1]["decision"]["action"] == "resume"


def test_durable_snapshot_stores_legacy_stages_only_once(tmp_path):
    store = ExpertRunStore(tmp_path)
    run_id = "a" * 32
    store.create(run_id=run_id, conversation_id="c", task="t", attachments=[],
                 profile={}, strategy="stable")
    store.mutate(run_id, lambda snapshot: snapshot["node_states"].update({
        "executor": {"status": "completed", "text": "persisted"},
    }), durable=True)
    disk = json.loads((store.root / f"{run_id}.json").read_text(encoding="utf-8"))
    assert "stages" not in disk
    restored = ExpertRunStore(tmp_path).load(run_id)
    assert restored["node_states"] == restored["stages"]
    assert restored["node_states"] is restored["stages"]
    assert restored["node_states"]["executor"]["text"] == "persisted"


def test_process_file_preview_is_stored_outside_main_snapshot(tmp_path):
    store = ExpertRunStore(tmp_path)
    run_id = "b" * 32
    store.create(run_id=run_id, conversation_id="c", task="t", attachments=[],
                 profile={}, strategy="stable")
    store.mutate(run_id, lambda snapshot: snapshot.update(process_files=[{
        "id": "file-1", "name": "candidate.md", "preview_type": "text",
        "preview": "small but private preview",
    }]), durable=True)
    disk = json.loads((store.root / f"{run_id}.json").read_text(encoding="utf-8"))
    assert "preview" not in disk["process_files"][0]
    assert disk["process_file_preview_refs"]["file-1"]["sha256"]
    restarted = ExpertRunStore(tmp_path)
    assert "small but private preview" not in json.dumps(restarted.public(run_id))
    assert restarted.process_file_preview(run_id, "file-1")["preview"] == "small but private preview"


def test_review_queue_shard_is_content_bound_and_readable_on_demand(tmp_path):
    store = ExpertRunStore(tmp_path)
    run_id = "c" * 32
    store.create(run_id=run_id, conversation_id="c", task="t", attachments=[],
                 profile={}, strategy="stable")
    queue = {"revision": 7, "items": [{"item_id": "page-1"}],
             "records": {"page-1": {"state": "needs_repair", "summary": "x" * 2000}}}
    store.mutate(run_id, lambda snapshot: snapshot.update(review_queues={"audit": queue}),
                 durable=True)
    disk = json.loads((store.root / f"{run_id}.json").read_text(encoding="utf-8"))
    assert "review_queues" not in disk
    reference = disk["review_queue_refs"]["audit"]
    assert reference["revision"] == 7
    assert "page-1" not in json.dumps(disk)
    restarted = ExpertRunStore(tmp_path)
    assert restarted.load_review_queue(run_id, "audit") == queue
    assert restarted.load(run_id)["review_queues"]["audit"] == queue


def test_missing_review_queue_shard_fails_closed(tmp_path):
    store = ExpertRunStore(tmp_path)
    run_id = "e" * 32
    store.create(run_id=run_id, conversation_id="c", task="t", attachments=[],
                 profile={}, strategy="stable")
    store.mutate(run_id, lambda snapshot: snapshot.update(
        review_queues={"audit": {"revision": 1, "items": [], "records": {}}}),
        durable=True)
    disk = json.loads((store.root / f"{run_id}.json").read_text(encoding="utf-8"))
    digest = disk["review_queue_refs"]["audit"]["sha256"]
    (store.root / "blobs" / run_id / f"{digest}.queue.json").unlink()
    restarted = ExpertRunStore(tmp_path)
    assert restarted.load(run_id)['review_queues']['audit']['revision'] == 1


def test_legacy_inline_review_queue_still_loads(tmp_path):
    store = ExpertRunStore(tmp_path)
    run_id = "f" * 32
    store.create(run_id=run_id, conversation_id="c", task="t", attachments=[],
                 profile={}, strategy="stable")
    path = store.root / f"{run_id}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("review_queue_refs", None)
    raw["review_queues"] = {"audit": {"revision": 3, "items": [], "records": {}}}
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    restarted = ExpertRunStore(tmp_path)
    assert restarted.load_review_queue(run_id, "audit") is None
    assert not restarted.load(run_id).get('review_queues')


def test_needs_attention_done_run_remains_resumable(tmp_path):
    """A finished scheduling pass may still contain retryable failed nodes."""
    store = ExpertRunStore(tmp_path)
    run_id = "d" * 32
    store.create(
        run_id=run_id, conversation_id="conversation", task="task",
        attachments=[], profile={}, strategy="stable",
    )
    store.mutate(run_id, lambda snapshot: snapshot.update(
        flow_state={"phase": "done", "status": "needs_attention"},
        node_states={"manager.finalize": {"status": "failed", "passed": False}},
    ))
    store.transition(run_id, "needs_attention")

    assert store.latest_resumable("conversation")["run_id"] == run_id
    resumed = store.resume(
        run_id, strategy="stable", retry_node_id="manager.finalize"
    )
    assert resumed["status"] == "running"
    assert resumed["retry_node_id"] == "manager.finalize"


class _HitlRequest:
    def __init__(self, done: bool = False) -> None:
        self.future = _PendingFuture(done)


def test_unresolved_hitl_reason_detects_pending_permission() -> None:
    class Session:
        permission_future = _PendingFuture(done=False)

    assert _unresolved_hitl_reason(Session(), {}) == "pending_permission"
    assert _unresolved_hitl_reason(Session(), {"t": {"status": "waiting_permission"}}) == (
        "pending_permission"
    )


def test_unresolved_hitl_reason_detects_waiting_tool_and_human_oncall() -> None:
    class Session:
        permission_future = None
        human_oncall_request = _HitlRequest(done=False)

    assert _unresolved_hitl_reason(Session(), {}) == "pending_human_oncall"
    assert _unresolved_hitl_reason(
        type("S", (), {"permission_future": None, "human_oncall_request": None})(),
        {"t": {"status": "waiting_permission"}},
    ) == "waiting_permission"


def test_human_input_cancelled_detection() -> None:
    assert _human_input_cancelled(
        "human_oncall", '{"status": "cancelled", "reason": "user_cancelled"}'
    )
    assert not _human_input_cancelled(
        "human_oncall", '{"status": "answered", "choice": {"id": "a"}}'
    )
    assert not _human_input_cancelled("Bash", "cancelled")


def test_pending_permission_restore_is_optional():
    tools = {}
    waits = []

    assert _restore_pending_permission({}, tools, waits) is None
    assert tools == {}
    assert waits == []


def test_pending_permission_restore_initializes_resume_state():
    tools = {}
    waits = []

    restored = _restore_pending_permission(
        {"pending_permission": {"tool_id": "tool-1", "name": "Bash"}},
        tools,
        waits,
    )

    assert restored is not None
    pending, pending_id, pending_item, resumed_wait = restored
    assert pending["name"] == "Bash"
    assert pending_id == "tool-1"
    assert pending_item["status"] == "waiting_permission"
    assert resumed_wait == waits[0]


def test_snapshot_attempts_and_strategy_transition(tmp_path):
    store = ExpertRunStore(tmp_path)
    run_id = "b" * 32
    created = store.create(
        run_id=run_id, conversation_id="conversation", task="task",
        attachments=[], profile={"manager": {}, "executor": {}, "auditor": {}},
        strategy=strategy_for_multi_agent(False),
    )
    assert created["status"] == "running"
    assert created["strategy"] == "stable"
    store.transition(run_id, "paused", reason="test")
    resumed = store.resume(run_id, strategy="fast", supplemental="also do this")
    assert resumed["attempt"] == 2
    assert resumed["resume_transition"] == "stable->fast"
    assert resumed["status"] == "running"
    assert resumed["supplemental_requirements"][-1]["text"] == "also do this"


def test_running_snapshot_is_recovered_as_paused(tmp_path):
    store = ExpertRunStore(tmp_path)
    run_id = "c" * 32
    store.create(
        run_id=run_id, conversation_id="conversation", task="task",
        attachments=[], profile={}, strategy="stable",
    )
    store.mutate(run_id, lambda value: value.update({
        "stage_cursor": "manager.intake",
        "active_node_ids": ["manager.intake"],
        "node_states": {
            "manager.intake": {"status": "running", "attempt": 1},
        },
    }))
    assert store.recover_stale_runs() == 1
    recovered = store.load(run_id)
    assert recovered["status"] == "paused"
    assert recovered["stage_cursor"] == "manager.intake"
    assert recovered["node_states"]["manager.intake"]["status"] == "paused"
    assert recovered["active_node_ids"] == []


def test_public_snapshot_recursively_redacts_credentials():
    value = redact_for_display({
        "api_key": "secret", "nested": [{"access_token": "token"}],
        "signature": "hidden", "ordinary": "visible",
    })
    assert value["api_key"] == "[已脱敏]"
    assert value["nested"][0]["access_token"] == "[已脱敏]"
    assert "signature" not in value
    assert value["ordinary"] == "visible"


def test_public_token_metrics_remain_visible_without_exposing_credentials():
    value = redact_for_display({"tokens": 2500, "token_budget": 5000,
        "token_window": {"issued_until": 3000, "token_bucket": {
            "burst_tokens": 2000, "api_token": "secret"}},
        "access_token": "secret", "token_window_receipts": []})
    assert value["tokens"] == 2500 and value["token_budget"] == 5000
    assert value["token_window"]["token_bucket"]["burst_tokens"] == 2000
    assert value["token_window"]["token_bucket"]["api_token"] == "[已脱敏]"
    assert value["access_token"] == "[已脱敏]"


def test_latest_paused_and_conversation_cleanup(tmp_path):
    store = ExpertRunStore(tmp_path)
    for char in ("d", "e"):
        store.create(
            run_id=char * 32, conversation_id="conversation", task=char,
            attachments=[], profile={}, strategy="stable",
        )
        store.transition(char * 32, "paused")
    assert store.latest_paused("conversation")["run_id"] == "e" * 32
    assert store.delete_conversation("conversation") == 2
    assert store.list_for_conversation("conversation") == []


def test_wait_intervals_use_union_instead_of_double_counting():
    assert interval_union_seconds([
        {"start": 10, "end": 20}, {"start": 15, "end": 30},
        {"start": 40, "end": 45},
    ]) == 25


def test_list_recent_returns_newest_across_conversations(tmp_path):
    store = ExpertRunStore(tmp_path)
    for index, run_id in enumerate(("a" * 32, "b" * 32, "c" * 32)):
        store.create(
            run_id=run_id, conversation_id=f"conv-{index % 2}", task=f"t{index}",
            attachments=[], profile={}, strategy="stable",
        )
        store.transition(run_id, "completed")
    recent = store.list_recent(limit=10)
    assert [run["run_id"] for run in recent] == ["c" * 32, "b" * 32, "a" * 32]
    assert len(store.list_recent(limit=2)) == 2


def test_resume_message_requires_a_real_prefix_boundary():
    assert parse_resume_message("继续，并补跑测试") == (True, "并补跑测试")
    assert parse_resume_message("continue with the audit") == (True, "with the audit")
    assert parse_resume_message("resume") == (True, "")
    assert parse_resume_message("continued deployment") == (False, "")
    assert parse_resume_message(
        "帮我修改这些内容，然后进入下一阶段，继续任务"
    ) == (True, "帮我修改这些内容，然后进入下一阶段")
    assert parse_resume_message("研究如何继续任务的设计") == (False, "")


def test_resume_strategy_transitions_preserve_completed_children(tmp_path):
    store = ExpertRunStore(tmp_path)
    run_id = "f" * 32
    store.create(
        run_id=run_id, conversation_id="conversation", task="task",
        attachments=[], profile={}, strategy="fast",
    )
    store.mutate(run_id, lambda value: value.update({"children": {
        "done": {"status": "completed", "messages": ["kept"]},
        "live": {"status": "running", "worktree": "wt"},
    }}))
    store.transition(run_id, "paused")
    stable = store.resume(run_id, strategy="stable")
    assert stable["children"]["done"]["status"] == "completed"
    assert stable["children"]["live"]["status"] == "stopped_for_stable_takeover"
    assert stable["children"]["live"]["worktree"] == "wt"


def test_fast_to_fast_resumes_only_unfinished_children(tmp_path):
    store = ExpertRunStore(tmp_path)
    run_id = "2" * 32
    store.create(
        run_id=run_id, conversation_id="conversation", task="task",
        attachments=[], profile={}, strategy="fast",
    )
    store.mutate(run_id, lambda value: value.update({"children": {
        "done": {"status": "completed", "worktree": "done-wt"},
        "live": {"status": "running", "worktree": "live-wt", "messages": ["kept"]},
    }}))
    store.transition(run_id, "paused")
    resumed = store.resume(run_id, strategy="fast")
    assert resumed["children"]["done"]["resume_action"] == "skip_completed"
    assert resumed["children"]["live"]["status"] == "resume_pending"
    assert resumed["children"]["live"]["attempt"] == 2
    assert resumed["children"]["live"]["messages"] == ["kept"]


def test_usage_total_deduplicates_latest_role_and_child_counters():
    total = aggregate_snapshot_usage({
        "stages": {
            "executor": {"usage": {
                "input_tokens": 80, "output_tokens": 20,
                "cache_read_tokens": 40, "cache_creation_tokens": 5,
                "cache_usage_available": True,
            }},
        },
        "children": {
            "child": {"usage": {
                "input_tokens": 20, "output_tokens": 10,
                "cache_read_tokens": 10, "cache_creation_tokens": 2,
                "cache_usage_available": True,
            }},
        },
    })
    assert total["input_tokens"] == 100
    assert total["output_tokens"] == 30
    assert total["cache_read_tokens"] == 50
    assert total["cache_miss_tokens"] == 100
    assert total["cache_usage_available"] is True


def test_event_batch_omits_stream_and_child_detail_chunks(tmp_path):
    store = ExpertRunStore(tmp_path)
    run_id = "3" * 32
    store.create(
        run_id=run_id, conversation_id="conversation", task="task",
        attachments=[], profile={}, strategy="stable",
    )
    stored = store.append_sequenced_events(run_id, [
        {"type": "expert_delta", "seq": 1, "text": "a"},
        {"type": "expert_child_event", "child_event_type": "tool_result",
         "seq": 2, "output": "x" * 10_000},
    ])
    assert stored["last_seq"] == 2
    assert stored["events"] == []
    replayed = store.append_sequenced_events(run_id, [
        {"type": "expert_delta", "seq": 2, "text": "duplicate"},
        {"type": "expert_usage", "seq": 3, "input_tokens": 1},
    ])
    assert replayed["last_seq"] == 3
    assert [event["type"] for event in replayed["events"]] == ["expert_usage"]


def test_public_snapshot_compacts_legacy_stream_events(tmp_path):
    store = ExpertRunStore(tmp_path)
    run_id = "4" * 32
    store.create(
        run_id=run_id, conversation_id="conversation", task="task",
        attachments=[], profile={}, strategy="stable",
    )
    def add_history(value):
        value["events"].extend([
            {"type": "expert_delta", "text": "redundant"},
            {"type": "expert_child_event", "output": "redundant"},
            {"type": "expert_stage", "role": "manager", "status": "completed"},
        ])
        value["stages"] = {"manager": {"partial_text": "aggregate"}}
        value["children"] = {"agent": {
            "agent_id": "agent", "name": "helper", "status": "completed",
            "messages": ["private"], "result": "private", "usage": {},
        }}
    store.mutate(run_id, add_history)

    public = store.public(run_id)

    assert [event["type"] for event in public["events"]] == [
        "expert_stage", "expert_delta", "expert_child",
    ]
    assert public["events"][1]["text"] == "aggregate"
    assert public["events"][2]["agent_id"] == "agent"
    assert "partial_text" not in public["stages"]["manager"]


def test_public_snapshot_keeps_reported_steps_for_reconnect(tmp_path):
    store = ExpertRunStore(tmp_path)
    run_id = "3" * 32
    store.create(
        run_id=run_id,
        conversation_id="conversation",
        task="task",
        attachments=[],
        profile={},
        strategy="stable",
    )
    store.append_event(run_id, {
        "type": "expert_step",
        "node_id": "executor",
        "label": "正在修复",
        "status": "running",
    })

    public = store.public(run_id)

    assert any(
        event.get("type") == "expert_step"
        and event.get("label") == "正在修复"
        for event in public["events"]
    )


def test_conversation_summary_keeps_text_but_defers_tool_details(tmp_path):
    store = ExpertRunStore(tmp_path)
    run_id = "7" * 32
    store.create(
        run_id=run_id, conversation_id="conversation", task="task",
        attachments=[], profile={}, strategy="stable",
    )
    store.mutate(run_id, lambda value: value.update({
        "status": "paused",
        "display_index": 99,
        "stages": {"manager": {
            "status": "paused", "attempt": 1,
            "partial_text": "saved body",
            "messages": [{"role": "assistant", "content": "private"}],
            "tools": {"tool-1": {
                "tool_id": "tool-1", "name": "Read", "status": "completed",
                "output": "detail output",
            }},
        }},
    }))

    summary = store.public_for_conversation("conversation")[0]
    detail = store.public(run_id, role="manager")

    assert [(event["type"], event.get("text")) for event in summary["events"]] == [
        ("expert_stage", None), ("expert_delta", "saved body"),
    ]
    assert summary["display_index"] == 99
    assert not any("tool" in event["type"] for event in summary["events"])
    assert any(event["type"] == "expert_tool_result" for event in detail["events"])
    assert "messages" not in summary["stages"]["manager"]


def test_terminated_snapshot_does_not_present_stale_stage_as_running(tmp_path):
    store = ExpertRunStore(tmp_path)
    run_id = "8" * 32
    store.create(
        run_id=run_id, conversation_id="conversation", task="task",
        attachments=[], profile={}, strategy="stable",
    )
    store.mutate(run_id, lambda value: value.update({
        "status": "terminated",
        "stages": {"auditor": {"status": "running", "attempt": 4}},
    }))

    summary = store.public_for_conversation("conversation")[0]

    stage = next(event for event in summary["events"] if event["type"] == "expert_stage")
    assert stage["status"] == "terminated"


def test_public_summary_disk_cache_avoids_reloading_large_snapshot(tmp_path, monkeypatch):
    store = ExpertRunStore(tmp_path)
    run_id = "9" * 32
    store.create(
        run_id=run_id, conversation_id="conversation", task="task",
        attachments=[], profile={}, strategy="stable",
    )
    store.mutate(run_id, lambda value: value.update({
        "stages": {"manager": {"partial_text": "body"}},
    }))
    fresh = ExpertRunStore(tmp_path)
    original_read_text = type(tmp_path).read_text

    def guarded_read(path, *args, **kwargs):
        if path.name == f"{run_id}.json":
            raise AssertionError("large private snapshot should use its public sidecar")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(type(tmp_path), "read_text", guarded_read)

    summary = fresh.public_for_conversation("conversation")

    assert summary[0]["run_id"] == run_id


def test_public_summaries_can_be_warmed_before_conversation_open(tmp_path):
    store = ExpertRunStore(tmp_path)
    run_id = "0" * 32
    store.create(
        run_id=run_id, conversation_id="conversation", task="task",
        attachments=[], profile={}, strategy="stable",
    )
    store._summary_path(run_id).unlink()
    fresh = ExpertRunStore(tmp_path)

    assert fresh.refresh_public_summaries() == 1
    assert fresh._summary_path(run_id).is_file()
    assert fresh.refresh_public_summaries() == 0


def test_expert_child_task_lookup_supports_legacy_snapshots(tmp_path):
    store = ExpertRunStore(tmp_path)
    run_id = "a" * 32
    store.create(
        run_id=run_id, conversation_id="conversation", task="task",
        attachments=[], profile={}, strategy="stable",
    )
    store.mutate(run_id, lambda value: value.update({
        "children": {"child": {"task_id": "legacy-task"}},
    }))

    assert store.is_expert_child_task("legacy-task", "conversation") is True
    assert store.is_expert_child_task("ordinary-task", "conversation") is False


def test_visible_chat_index_counts_user_and_assistant_only() -> None:
    history = [
        Message(role="user", content="q"),
        Message(role="assistant", content="a"),
        Message(role="user", content="follow"),
    ]
    assert visible_chat_index(history) == 3
    assert visible_chat_index([]) == 0
    assert visible_chat_index(None) == 0


def test_public_conversation_snapshots_do_not_reload_each_run(tmp_path, monkeypatch):
    store = ExpertRunStore(tmp_path)
    for char in ("5", "6"):
        store.create(
            run_id=char * 32, conversation_id="conversation", task=char,
            attachments=[], profile={}, strategy="stable",
        )
    monkeypatch.setattr(
        store, "public",
        lambda _run_id: (_ for _ in ()).throw(AssertionError("duplicate reload")),
    )

    public = store.public_for_conversation("conversation")

    assert {item["run_id"] for item in public} == {"5" * 32, "6" * 32}


def test_blocked_run_is_resumable_after_provider_is_fixed(tmp_path):
    store = ExpertRunStore(tmp_path)
    run_id = "1" * 32
    store.create(
        run_id=run_id, conversation_id="conversation", task="task",
        attachments=[], profile={}, strategy="stable",
    )
    store.transition(run_id, "blocked", reason="provider unavailable")
    assert store.latest_resumable("conversation")["run_id"] == run_id
    assert store.resume(run_id, strategy="stable")["status"] == "running"


def test_resume_rebinds_latest_flow_and_preserves_common_checkpoints(tmp_path):
    store = ExpertRunStore(tmp_path)
    run_id = "8" * 32
    old_flow = {
        "flow_id": "project-flow",
        "version": 1,
        "label": "old",
        "nodes": [
            {"node_id": "a", "role": "executor", "label": "A"},
            {"node_id": "audit", "role": "auditor", "label": "Audit"},
            {"node_id": "old", "role": "executor", "label": "Old"},
            {"node_id": "done", "role": "manager", "label": "Done"},
        ],
        "edges": [
            {"from": "a", "to": "audit"},
            {"from": "audit", "to": "old"},
            {"from": "old", "to": "done"},
        ],
        "finalizer_node_id": "done",
    }
    new_flow = {
        "flow_id": "project-flow",
        "version": 1,
        "label": "edited",
        "nodes": [
            {"node_id": "a", "role": "executor", "label": "A"},
            {
                "node_id": "audit",
                "role": "auditor",
                "label": "Audit",
                "failure_policy": {
                    "action": "route",
                    "max_attempts": 2,
                    "target_node_id": "repair",
                },
            },
            {"node_id": "repair", "role": "executor", "label": "Repair"},
            {"node_id": "done", "role": "manager", "label": "Done"},
        ],
        "edges": [
            {"from": "a", "to": "audit"},
            {"from": "audit", "to": "done", "condition": "pass"},
            {"from": "audit", "to": "repair", "condition": "fail"},
            {"from": "repair", "to": "done"},
        ],
        "finalizer_node_id": "done",
    }
    store.create(
        run_id=run_id,
        conversation_id="conversation",
        task="task",
        attachments=[],
        profile={},
        strategy="stable",
        flow=old_flow,
    )
    store.mutate(run_id, lambda snapshot: snapshot.update({
        "status": "blocked",
        "flow_state": {"phase": "batch_halted"},
        "node_states": {
            "a": {"status": "completed", "text": "kept"},
            "audit": {
                "status": "completed",
                "verdict": {"verdict": "NEEDS_ATTENTION"},
            },
            "old": {"status": "failed"},
        },
        "locked_models": {"audit": {"provider_key": "old", "model_id": "old"}},
    }))

    resumed = store.resume(run_id, strategy="stable", flow=new_flow)

    ids = {node["node_id"] for node in resumed["locked_flow"]["nodes"]}
    assert ids == {"a", "audit", "repair", "done"}
    assert resumed["node_states"]["a"]["text"] == "kept"
    assert resumed["node_states"]["audit"]["status"] == "completed"
    assert "old" not in resumed["node_states"]
    assert resumed["locked_models"] == {}
    assert resumed["flow_revisions"][-1]["to_fingerprint"] == resumed[
        "flow_fingerprint"
    ]


def test_successors_after_node_returns_reachable_nodes():
    from mewcode.expert_runs import successors_after_node
    flow = {
        "nodes": [
            {"node_id": "a"}, {"node_id": "b"}, {"node_id": "c"}, {"node_id": "d"},
        ],
        "edges": [
            {"from_node": "a", "to_node": "b"},
            {"from_node": "b", "to_node": "c"},
            {"from_node": "a", "to_node": "d"},
        ],
    }
    assert successors_after_node(flow, "a") == ["b", "c", "d"] or set(
        successors_after_node(flow, "a")
    ) == {"b", "c", "d"}
    assert successors_after_node(flow, "b") == ["c"] or set(
        successors_after_node(flow, "b")
    ) == {"c"}
    assert successors_after_node(flow, "c") == []


def test_resume_after_node_keeps_anchor_and_clears_successors(tmp_path):
    import pytest
    from mewcode.expert_runs import ExpertRunStore, strategy_for_multi_agent

    store = ExpertRunStore(tmp_path)
    run_id = "d" * 32
    flow = {
        "flow_id": "test.resume-after",
        "version": 1,
        "label": "resume-after",
        "finalizer_node_id": "c",
        "finalizer_plugin": "model",
        "nodes": [
            {"node_id": "a", "role": "executor", "label": "A"},
            {"node_id": "b", "role": "executor", "label": "B"},
            {"node_id": "c", "role": "manager", "label": "C"},
        ],
        "edges": [
            {"from_node": "a", "to_node": "b"},
            {"from_node": "b", "to_node": "c"},
        ],
    }
    store.create(
        run_id=run_id,
        conversation_id="conversation",
        task="task",
        attachments=[],
        profile={"manager": {}, "executor": {}, "auditor": {}},
        strategy=strategy_for_multi_agent(False),
        flow=flow,
    )

    def mark_blocked(snapshot):
        snapshot["status"] = "needs_attention"
        snapshot["flow_state"] = {"phase": "finalizer_skipped"}
        snapshot["node_states"] = {
            "a": {"status": "completed", "role": "executor", "text": "done-a"},
            "b": {"status": "completed", "role": "executor", "text": "done-b"},
            "c": {"status": "failed", "role": "manager", "text": "fail-c"},
        }
        snapshot["members"] = {
            "m-b": {"member_id": "m-b", "node_id": "b", "status": "completed"},
        }

    store.mutate(run_id, mark_blocked)
    store.mutate(
        run_id,
        lambda snapshot: (
            snapshot.__setitem__(
                "pipeline_runs",
                {"b": {"status": "completed"}, "c": {"status": "failed"}},
            ),
            snapshot.__setitem__("node_attempts", {"a": 1, "b": 2, "c": 1}),
        ),
    )
    resumed = store.resume(run_id, strategy="stable", resume_after_node="a")
    assert resumed["status"] == "running"
    assert resumed.get("resume_after_node") == "a"
    assert resumed["node_states"]["a"]["status"] == "completed"
    assert "b" not in resumed["node_states"]
    assert "c" not in resumed["node_states"]
    assert "m-b" not in (resumed.get("members") or {})
    assert "b" not in (resumed.get("pipeline_runs") or {})
    assert "c" not in (resumed.get("pipeline_runs") or {})
    assert (resumed.get("node_attempts") or {}).get("a") == 1
    assert "b" not in (resumed.get("node_attempts") or {})
    assert "c" not in (resumed.get("node_attempts") or {})

    store.mutate(run_id, mark_blocked)
    with pytest.raises(ValueError, match="completed anchor"):
        store.resume(run_id, strategy="stable", resume_after_node="c")
