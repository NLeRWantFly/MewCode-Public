import copy
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from frontend import server
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.recovery_case import save_case
from mewcode.experts.node_ledger import ledger_for_run
from mewcode.experts.resume_advice import RecoveryResumeBlocked, resume_advice


FIXTURE = Path(__file__).parent / "fixtures" / "recovery" / "input_discovery.json"


def saved_record():
    with FIXTURE.open(encoding="utf-8") as stream:
        return json.load(stream)


def discovery_case(record):
    return record["node_states"]["n_prep_plan"]["recovery_case"]


def install_store(tmp_path, *, mutate=None):
    record = saved_record()
    record["locked_flow"] = {
        "flow_id": "input-discovery-test", "version": 1,
        "label": "输入发现恢复测试",
        "nodes": [{"node_id": "n_prep_plan", "role": "planner", "label": "准备"}],
        "edges": [], "finalizer_node_id": "n_prep_plan", "meta": {},
    }
    record.update({
        "version": 2, "strategy": "stable", "attempt": 1, "status_reason": "",
        "flow_state": {"phase": "paused", "status": "paused", "reason": ""},
        "updated_at": "2026-10-08T00:00:00+00:00",
        "created_at": "2026-10-08T00:00:00+00:00",
        "started_at": "2026-10-08T00:00:00+00:00", "completed_at": None,
        "paused_intervals": [], "attempts": [], "stages": copy.deepcopy(record["node_states"]),
        "active_node_ids": [], "handoffs": [], "events": [], "loop_states": {},
        "children": {}, "tools": {}, "members": {}, "node_attempts": {},
        "pipeline_runs": {}, "node_expansions": {}, "display_index": 0,
        "last_seq": 0, "legacy": False,
    })
    if mutate:
        mutate(record)
    store = ExpertRunStore(tmp_path / "runs")
    with store._lock:
        store._write(record)
    return store, record["run_id"]


def test_resume_advice_has_stable_identity_and_closed_case_retry_target():
    record = saved_record()
    case = discovery_case(record)
    case.update(closed_by_user=True, pending={})
    save_case(record, case)

    advice = resume_advice(record)

    assert advice["run_id"] == record["run_id"]
    assert advice["conversation_id"] == record["conversation_id"]
    assert advice["action"] == "retry_node"
    assert advice["node_id"] == "n_prep_plan"
    assert advice["case_id"] == case["case_id"]
    assert "不会自动清除停止状态" in advice["reason"]


def test_completed_stopped_case_requires_review_instead_of_answering_missing_card():
    record = saved_record()
    case = discovery_case(record)
    case["closed_by_user"] = True
    record["node_states"]["n_prep_plan"]["status"] = "completed"
    save_case(record, case)

    advice = resume_advice(record)

    assert advice["action"] == "review_cases"
    assert advice["cases"][0]["node_id"] == "n_prep_plan"
    assert "不会自动恢复" in advice["reason"]


def test_closed_case_with_pending_item_stays_reviewable_and_plain_resume_is_read_only(tmp_path):
    def stop_without_clearing_pending(record):
        case = discovery_case(record)
        case["closed_by_user"] = True
        save_case(record, case)

    store, run_id = install_store(tmp_path, mutate=stop_without_clearing_pending)
    before = store.load(run_id)

    advice = resume_advice(before)
    assert advice["action"] == "review_cases"
    assert advice["cases"][0]["closed_by_user"]
    with pytest.raises(RecoveryResumeBlocked) as blocked:
        store.resume(run_id, strategy="stable")
    assert getattr(blocked.value, "error_code", "") == "recovery_resume_blocked"
    assert blocked.value.resume_advice["action"] == "review_cases"
    assert store.load(run_id) == before


@pytest.mark.parametrize("stage_status", ["completed", "skipped", "branch_not_taken", "terminated", "pending"])
def test_closed_case_on_nonretryable_stage_requires_review(stage_status):
    record = saved_record()
    case = discovery_case(record)
    case.update(closed_by_user=True, pending={})
    record["node_states"]["n_prep_plan"]["status"] = stage_status
    save_case(record, case)

    advice = resume_advice(record)

    assert advice["action"] == "review_cases"
    assert advice["node_id"] == "n_prep_plan"
    assert "不会自动恢复" in advice["reason"]


@pytest.mark.parametrize("root_cause,pending_kind", [
    ("authority_gap", "elevation"),
    ("business_decision", "business_decision"),
    ("unknown_effect", "incident"),
])
def test_resume_advice_does_not_shortcut_current_authority_or_business_case(
        root_cause, pending_kind):
    record = saved_record()
    case = discovery_case(record)
    case["report"]["root_cause"] = root_cause
    case["pending"] = {"kind": pending_kind, "question": "当前案件仍待决定。"}
    save_case(record, case)

    advice = resume_advice(record)

    assert advice["action"] == "answer_case"
    assert advice["case_id"] == case["case_id"]
    assert advice["node_id"] == "n_prep_plan"


def test_public_projection_exposes_advice_without_mutating_snapshot(tmp_path):
    store, run_id = install_store(tmp_path)
    before = store.load(run_id)

    public = store._public_value(store.load(run_id), include_details=False)

    assert public["resume_advice"]["run_id"] == run_id
    assert public["resume_advice"]["action"] == "continue_checkpoint"
    assert store.load(run_id) == before


def test_plain_resume_raises_typed_block_and_explicit_retry_keeps_stop_semantics(tmp_path):
    def stop(record):
        case = discovery_case(record)
        case.update(closed_by_user=True, pending={})
        save_case(record, case)

    store, run_id = install_store(tmp_path, mutate=stop)
    before = store.load(run_id)

    with pytest.raises(RecoveryResumeBlocked) as blocked:
        store.resume(run_id, strategy="stable")
    assert blocked.value.error_code == "recovery_resume_blocked"
    assert blocked.value.resume_advice["action"] == "retry_node"
    assert store.load(run_id) == before

    retried = store.resume(run_id, strategy="stable", retry_node_id="n_prep_plan")

    assert retried["status"] == "running"
    assert retried["node_states"]["n_prep_plan"]["status"] != "completed"
    assert "recovery_case" not in retried["node_states"]["n_prep_plan"]


def test_no_case_advice_is_plain_continue():
    record = saved_record()
    record["node_ledgers"] = {}
    record["node_states"]["n_prep_plan"].pop("recovery_case", None)

    advice = resume_advice(record)

    assert advice["action"] == "continue"
    assert advice["case_id"] == ""


@pytest.mark.parametrize("record", [{}, {"run_id": "r", "conversation_id": "c", "node_states": {}}])
def test_sparse_advice_and_public_projection_are_read_only(record):
    before = copy.deepcopy(record)

    advice = resume_advice(record)

    assert advice["action"] == "continue"
    assert record == before


def test_multiple_pending_cases_are_reviewed_as_explicit_targets():
    record = saved_record()
    original = copy.deepcopy(discovery_case(record))
    second = copy.deepcopy(original)
    second["case_id"] = "second-input-case"
    second["closed_by_user"] = True
    save_case(record, second)

    advice = resume_advice(record)

    assert advice["action"] == "review_cases"
    assert {item["case_id"] for item in advice["cases"]} == {
        original["case_id"], second["case_id"]}


@pytest.mark.parametrize("stale_kind", ["epoch", "revision"])
def test_stale_case_is_described_for_review_instead_of_retrying_old_node(stale_kind):
    record = saved_record()
    case = discovery_case(record)
    case["closed_by_user"] = True
    save_case(record, case)
    if stale_kind == "epoch":
        ledger_for_run(record).record(
            "n_prep_plan", "epoch_change",
            {"reason": "explicit_rebase", "source_ref": "test-stale-case"},
            epoch=1, identity="test-stale-case",
        )
    else:
        record["node_states"]["n_prep_plan"]["input_revision"] = "new-input-revision"

    advice = resume_advice(record)

    assert advice["action"] == "review_cases"
    assert advice["case_id"] == case["case_id"]
    assert advice["verification_reasons"]
    assert any("代际" in reason or "版本" in reason or "活动案件" in reason
               for reason in advice["verification_reasons"])


@pytest.mark.asyncio
async def test_ws_plain_resume_blocks_stopped_case_before_turn_or_dispatch(tmp_path, monkeypatch):
    store, run_id = install_store(tmp_path)
    conversation_id = "resume-advice-test-conversation"

    def stop(snapshot):
        snapshot["conversation_id"] = conversation_id
        case = discovery_case(snapshot)
        case.update(closed_by_user=True, pending={})
        save_case(snapshot, case)

    store.mutate(run_id, stop)
    attached = []
    session = SimpleNamespace(
        session=SimpleNamespace(session_id=conversation_id),
        conversation_runs={},
        agent=object(),
        client_state_payload=lambda: {},
        tasks_payload=lambda: [],
        collect_task_notifications=lambda: [],
        chat_busy=lambda *_: False,
        capture_conversation_runtime=lambda: pytest.fail("blocked resume started a turn"),
        begin_conversation_turn=lambda *_: pytest.fail("blocked resume began a turn"),
        attach_conversation_task=lambda *_: attached.append(True),
    )

    class Socket:
        def __init__(self):
            self.messages = []
            self._frames = iter([{
                "type": "expert_resume", "run_id": run_id,
                "conversation_id": conversation_id,
            }])

        async def accept(self):
            return None

        async def send_json(self, event):
            self.messages.append(event)

        async def receive_json(self):
            try:
                return next(self._frames)
            except StopIteration:
                raise server.WebSocketDisconnect()

    socket = Socket()
    monkeypatch.setattr(server, "SESSION", session)
    monkeypatch.setattr(server, "EXPERT_RUNS", store)
    monkeypatch.setattr(server, "WS_EVENTS", server.WebSocketEventHub())
    monkeypatch.setattr(server, "DESKTOP_TOKEN", "")

    await server.ws_endpoint(socket)

    errors = [event for event in socket.messages if event.get("type") == "error"]
    assert len(errors) == 1
    assert errors[0]["error_code"] == "recovery_resume_blocked"
    assert errors[0]["conversation_id"] == conversation_id
    assert errors[0]["resume_advice"]["action"] == "retry_node"
    assert not attached
    assert not [event for event in socket.messages if event.get("type") == "turn_started"]
