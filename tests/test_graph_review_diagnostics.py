"""Coverage-review failures reach the host and main agent without losing cause."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from frontend import orch_drafts, server
from mewcode.client import LLMError
from mewcode.orchestration.graph_review import review_coverage
from mewcode.orchestration.task_design import TaskBuildError
from mewcode.orchestration.workflow_blocks import compact_task_design
from tests.test_graph_review import FakeReview, source, document
from tests.test_graph_autobuild import run, graph, FakeModel, proposal, new_build_state
from tests.test_task_design_entry import task_api_env
from tests.test_task_design_e2e import _prepare_queued_task


@pytest.mark.parametrize("response,stage,category,retryable", [
    (TimeoutError(), "model_call", "generation", True),
    ("not json", "response_parse", "response_format", False),
    (ValueError("retryable=true RemoteProtocolError"), "model_call", "generation", False),
])
@pytest.mark.asyncio
async def test_review_failure_is_structured_and_cannot_claim_retryability(response, stage, category, retryable):
    result = await review_coverage([source("u", "保留所有要求")], document(), ask_review=FakeReview(response))
    failure = result["failure"]
    assert failure["stage"] == stage and failure["batch"] == 1
    assert failure["category"] == category and failure["retryable"] is retryable
    assert failure["summary"] and result["error"]
    assert result["batches"][0]["status"] == "failed"


@pytest.mark.parametrize("phase,retryable", [
    ("first_event", True), ("queue", False), ("reasoning_idle", False), ("stream_idle", False),
])
@pytest.mark.asyncio
async def test_first_event_timeout_classification_is_narrow(phase, retryable):
    from mewcode.orchestration.extract import OrchestrationStreamTimeout

    error = OrchestrationStreamTimeout(phase=phase, timeout_seconds=120)
    reviewer = FakeReview(error)
    result = await review_coverage([source("u", "保留完整要求")], document(), ask_review=reviewer)
    assert len(reviewer.calls) == 1
    assert result["failure"]["exception_type"] == "OrchestrationStreamTimeout"
    assert result["failure"]["stage"] == "model_call"
    assert result["failure"]["retryable"] is retryable
    assert result["error"] and result["batches"][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_first_event_timeout_does_not_override_authentication_failure():
    from mewcode.orchestration.extract import OrchestrationStreamTimeout

    error = OrchestrationStreamTimeout(phase="first_event", timeout_seconds=120)
    error.status_code = 401
    result = await review_coverage([source("u", "保留要求")], document(), ask_review=FakeReview(error))
    assert result["failure"]["category"] == "authentication"
    assert result["failure"]["retryable"] is False


@pytest.mark.asyncio
async def test_first_event_attribute_on_plain_value_error_is_not_retryable():
    error = ValueError("network timeout")
    error.phase = "first_event"
    result = await review_coverage([source("u", "保留要求")], document(), ask_review=FakeReview(error))
    assert result["failure"]["retryable"] is False


@pytest.mark.asyncio
async def test_five_mixed_connection_failures_remain_failed_but_retryable(monkeypatch):
    from mewcode.client import NetworkError
    from mewcode.orchestration import graph_review
    from mewcode.orchestration.extract import OrchestrationStreamTimeout

    sources = [source("u" + str(index), "完整用户要求 " + str(index)) for index in range(5)]
    monkeypatch.setattr(graph_review, "_review_batches", lambda rows, graph, limit: [[row] for row in rows])
    errors = [NetworkError("Connection error."),
        OrchestrationStreamTimeout(phase="first_event", timeout_seconds=120),
        NetworkError("Request timed out."),
        OrchestrationStreamTimeout(phase="first_event", timeout_seconds=120),
        NetworkError("Connection error.")]
    calls = []

    async def review(system, prompt):
        calls.append(prompt)
        raise errors[len(calls) - 1]

    result = await review_coverage(sources, document(), ask_review=review)
    assert len(calls) == result["model_calls"] == 5
    assert result["failure"]["retryable"] is True
    assert result["error"] and result["gaps"] == []
    assert all(row["status"] == "failed" and row["failure"]["retryable"] is True
        for row in result["batches"])


@pytest.mark.asyncio
async def test_review_failure_preserves_actual_route_and_redacts_provider_secrets():
    provider = SimpleNamespace(api_key="opaque-secret-key", default_headers={"Authorization": "private-header"})
    error = LLMError("denied opaque-secret-key private-header")
    error.status_code = 401
    result = await review_coverage([source("u", "保留要求")], document(), ask_review=FakeReview(error),
        failure_context={"provider": provider, "provider_key": "audit", "model": "selected-reviewer"})
    failure = result["failure"]
    assert failure["http_status"] == 401 and failure["retryable"] is False
    assert failure["route"]["model"] == "selected-reviewer"
    assert "opaque-secret-key" not in json.dumps(result) and "private-header" not in json.dumps(result)


@pytest.mark.parametrize("phase", ["review", "final_review"])
@pytest.mark.asyncio
async def test_review_diagnostics_survive_build_checkpoint(phase):
    doc = graph(ready=True)
    state = new_build_state(doc)
    state["phase"] = phase
    state["review"] = {"gaps": [], "model_calls": 0}
    snapshots = []
    result = await run(doc, FakeModel(proposal()), FakeModel(TimeoutError()), state=state,
        on_checkpoint=lambda build, current: snapshots.append((build, current)))
    assert result["build"]["status"] == "needs_review"
    assert result["build"]["failure"]["phase"] == phase
    assert result["build"]["failure"]["retryable"] is True
    assert snapshots[-1][0]["failure"] == result["build"]["failure"]
    assert result["document"] == doc


@pytest.mark.parametrize("kind,retryable", [
    ("timeout", True), ("first_event", True), ("authentication", False), ("format", False),
])
@pytest.mark.asyncio
async def test_real_task_builder_exposes_review_failure_to_workbench_and_agent(task_api_env, monkeypatch, kind, retryable):
    _, _, history_path, owner, _ = task_api_env
    monkeypatch.setattr(server, "_load_review_model", lambda: {})
    monkeypatch.setattr(server.WS_EVENTS, "send_json", AsyncMock())

    async def model(_client, **kwargs):
        payload = json.loads(kwargs["user_text"])
        if isinstance(payload.get("sources"), list):
            return json.dumps({"upserts": [], "ignored": [
                {"source_id": item["id"], "reason": "要求已在规格中"} for item in payload["sources"]]})
        if isinstance(payload.get("requirements"), list):
            if kind == "format":
                return "invalid JSON"
            if kind == "authentication":
                error = LLMError("review provider denied")
                error.status_code = 401
                raise error
            if kind == "first_event":
                from mewcode.orchestration.extract import OrchestrationStreamTimeout
                raise OrchestrationStreamTimeout(phase="first_event", timeout_seconds=120)
            raise TimeoutError()
        return json.dumps({"upserts": [], "notes": "保持已有结构"})

    monkeypatch.setattr(orch_drafts, "collect_model_text", model)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        design, queued = await _prepare_queued_task(client, owner, history_path, "plan")
        building = design.claim_build(queued)
        request = building["build_request"]
        expected = {**request, "task_id": building["task_id"], "task_revision": building["revision"],
                    "build_id": request["id"], "build_epoch": request["epoch"]}
        with pytest.raises(TaskBuildError) as caught:
            await server.task_design_build_graph(owner["project"], owner["conversation_id"], expected=expected)
        error = caught.value
        candidate = design.store.get()["candidate"]
        assert candidate["autobuild"]["reason"] == "review_error"
        assert error.retryable is retryable
        failure = error.diagnostic["failure"]
        assert failure == candidate["autobuild"]["failure"]
        assert failure["summary"] in str(error) and failure["exception_type"] in str(error)
        stopped = design.finish_build(expected, "failed", str(error), failure_evidence=error.diagnostic)
        read = compact_task_design(stopped)
        assert read["failure"]["stage"] == failure["stage"]
        assert read["failure"]["retryable"] is retryable
        assert read["failure"]["summary"] == failure["summary"]
        assert design.store.get()["candidate"]["document"] == candidate["document"]


async def _capacity_failed_task(task_api_env, monkeypatch, phase="review"):
    import copy
    from mewcode.orchestration import graph_review
    from tests.test_task_design_spec import _expected_build

    _, _, history_path, owner, _ = task_api_env
    monkeypatch.setattr(server, "_load_review_model", lambda: {})
    monkeypatch.setattr(server.WS_EVENTS, "send_json", AsyncMock())
    calls = []

    async def model(_client, **kwargs):
        payload = json.loads(kwargs["user_text"])
        calls.append(payload)
        if isinstance(payload.get("sources"), list):
            return json.dumps({"upserts": [], "ignored": [
                {"source_id": item["id"], "reason": "要求已在规格中"} for item in payload["sources"]]})
        if isinstance(payload.get("requirements"), list):
            return '{"gaps":[],"notes":"synthetic review"}'
        return json.dumps({"upserts": [], "notes": "保持已有结构"})

    monkeypatch.setattr(orch_drafts, "collect_model_text", model)
    original_batches = graph_review._review_batches

    def oversized(*args, **kwargs):
        raise graph_review.ReviewRequestTooLarge("synthetic capacity failure")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        design, queued = await _prepare_queued_task(client, owner, history_path, "plan")
        building = design.claim_build(queued)
        expected = _expected_build(building)
        monkeypatch.setattr(graph_review, "_review_batches", oversized)
        with pytest.raises(TaskBuildError) as caught:
            await server.task_design_build_graph(owner["project"], owner["conversation_id"], expected=expected)
        monkeypatch.setattr(graph_review, "_review_batches", original_batches)
    error = caught.value
    assert error.diagnostic["failure"]["exception_type"] == "ReviewRequestTooLarge"
    assert error.diagnostic["failure"]["stage"] == "request_preparation"
    assert error.retryable is False
    diagnostic = copy.deepcopy(error.diagnostic)
    if phase == "final_review":
        with design.store.db() as db:
            state = design.store._get(db)
            build = state["candidate"]["autobuild"]
            build["failure"]["phase"] = phase
            build["final_review"] = copy.deepcopy(build["review"])
            build["review"] = {"gaps": [], "model_calls": 1, "notes": "retained first review"}
            build["model_calls"] += 1
            build["call_attempts"] += 1
            design.store._put(db, state)
        diagnostic["failure"]["phase"] = phase
    failed = design.finish_build(expected, "failed", str(error), failure_origin="host_build",
        failure_kind="non_retryable", retryable=False, failure_evidence=diagnostic)
    return design, failed, calls, history_path, owner


def _queue_capacity_repair(design, failed, history_path, owner, **kwargs):
    from mewcode.orchestration.workflow_blocks import WorkflowBlocks
    from mewcode.tools.task_design import review_request_preflight
    from tests.test_task_design_spec import _build_args

    blocks = WorkflowBlocks(owner["project"], owner["conversation_id"], source_path=history_path,
        base_spec=lambda: None, guard=lambda: True, model_routes=lambda: [], tool_services=lambda: [])
    validator = kwargs.pop("capacity_validator", lambda state: review_request_preflight(blocks, state))
    return design.queue_build(task_id=failed["task_id"], revision=failed["revision"],
        **_build_args(failed), capacity_validator=validator, **kwargs)


@pytest.mark.parametrize("phase", ["review", "final_review"])
@pytest.mark.asyncio
async def test_capacity_repair_resumes_real_frontend_with_frozen_sources_and_existing_costs(task_api_env, monkeypatch, phase):
    import copy
    from mewcode.orchestration.draft_store import DraftStore
    from tests.test_task_design_spec import _expected_build
    from tests.test_workflow_drafts import record, write_history

    design, failed, calls, history_path, owner = await _capacity_failed_task(task_api_env, monkeypatch, phase)
    before = design.store.get()["candidate"]
    write_history(history_path, [record("assistant", "new assistant context must not be extracted")], append=True)
    queued = _queue_capacity_repair(design, failed, history_path, owner)
    restored = design.store.get()["candidate"]
    assert restored["sequence"] == before["sequence"] + 1
    assert restored["autobuild"]["phase"] == phase
    assert restored["autobuild"][phase] is None
    assert queued["build_request"]["previous_failure"]["retryable"] is False
    for key in ("call_attempts", "model_calls", "started_at", "deadline_at", "control", "autofix", "gap_fill", "locked_baseline"):
        assert restored["autobuild"].get(key) == before["autobuild"].get(key)
    building = design.claim_build(queued)
    expected = _expected_build(building)
    expected_before = copy.deepcopy(expected)
    old_calls = len(calls)
    original_begin = DraftStore.begin
    original_freeze = orch_drafts.freeze_basis

    def forbid_begin(*args, **kwargs):
        pytest.fail("capacity repair must not extend sources through begin")

    def forbid_freeze(*args, **kwargs):
        pytest.fail("capacity repair must not recompute the frozen history basis")

    monkeypatch.setattr(DraftStore, "begin", forbid_begin)
    monkeypatch.setattr(orch_drafts, "freeze_basis", forbid_freeze)
    await server.task_design_build_graph(owner["project"], owner["conversation_id"], expected=expected)
    monkeypatch.setattr(DraftStore, "begin", original_begin)
    monkeypatch.setattr(orch_drafts, "freeze_basis", original_freeze)
    saved = design.store.get()["candidate"]
    fresh_calls = calls[old_calls:]
    assert fresh_calls and all(isinstance(payload.get("requirements"), list) for payload in fresh_calls)
    assert all("new assistant context" not in json.dumps(payload) for payload in fresh_calls)
    assert saved["document"] == before["document"]
    assert saved["history_basis"] == before["history_basis"]
    assert saved["history_source_ids"] == before["history_source_ids"]
    assert saved["materials_snapshot"] == before["materials_snapshot"]
    assert len(design.store.get()["source_ids"]) > len(saved["history_source_ids"])
    assert saved["autobuild"]["status"] == "ready"
    assert saved["autobuild"]["call_attempts"] == before["autobuild"]["call_attempts"] + len(fresh_calls)
    assert saved["autobuild"]["model_calls"] == before["autobuild"]["model_calls"] + len(fresh_calls)
    for key in ("started_at", "deadline_at", "autofix", "gap_fill", "locked_baseline", "capacity_recovery_history"):
        assert saved["autobuild"].get(key) == restored["autobuild"].get(key)
    if phase == "final_review":
        assert saved["autobuild"]["review"] == before["autobuild"]["review"]
    assert design.read()["status"] == "building"
    assert design.store.get()["revision"] == 0
    assert not design.read().get("run_id")
    assert expected == expected_before


@pytest.mark.parametrize("kind", ["candidate_sequence", "document", "history_basis", "source_ids", "receipt", "pending", "preflight_sources"])
@pytest.mark.asyncio
async def test_capacity_frontend_rejects_changed_frozen_inputs_before_model_dispatch(task_api_env, monkeypatch, kind):
    from tests.test_task_design_spec import _expected_build

    design, failed, calls, history_path, owner = await _capacity_failed_task(task_api_env, monkeypatch)
    queued = _queue_capacity_repair(design, failed, history_path, owner)
    building = design.claim_build(queued)
    expected = _expected_build(building)
    with design.store.db() as db:
        state = design.store._get(db)
        candidate = state["candidate"]
        if kind == "candidate_sequence":
            candidate["sequence"] += 1
        elif kind == "document":
            candidate["document"]["entries"][0]["body"] += " changed"
        elif kind == "history_basis":
            candidate["history_basis"]["dirty"] = True
        elif kind == "source_ids":
            candidate["history_source_ids"] = []
        elif kind == "receipt":
            state["task_design"]["build_request"]["capacity_revalidation"]["document_hash"] = "stale"
        elif kind == "pending":
            candidate["processed_ids"] = []
            candidate["reused_ids"] = []
        else:
            receipt = state["task_design"]["build_request"]["capacity_revalidation"]
            receipt["preflight"]["review_sources_hash"] = "mismatched"
            candidate["autobuild"]["capacity_recovery_history"][-1]["capacity_revalidation"] = receipt
            expected["capacity_revalidation"] = receipt
        design.store._put(db, state)
    count = len(calls)
    with pytest.raises((ValueError, TaskBuildError)):
        await server.task_design_build_graph(owner["project"], owner["conversation_id"], expected=expected)
    assert len(calls) == count
    assert design.store.get()["candidate"]["autobuild"]["call_attempts"] == candidate["autobuild"]["call_attempts"]
    assert design.store.get()["revision"] == 0


@pytest.mark.asyncio
async def test_superseding_message_fences_capacity_resume_before_dispatch(task_api_env, monkeypatch):
    from tests.test_task_design_spec import _expected_build

    design, failed, calls, history_path, owner = await _capacity_failed_task(task_api_env, monkeypatch)
    queued = _queue_capacity_repair(design, failed, history_path, owner)
    building = design.claim_build(queued)
    expected = _expected_build(building)
    changed = design.record_turn("补充当前任务要求，旧构图回调不得继续。")
    assert changed["status"] == "designing"
    assert changed["build_request"]["state"] == "superseded"
    before = design.store.get()
    count = len(calls)
    with pytest.raises(ValueError, match="任务已暂停|规格版本变化|旧构图"):
        await server.task_design_build_graph(owner["project"], owner["conversation_id"], expected=expected)
    assert design.store.get() == before
    assert len(calls) == count
    assert changed["intent"] == building["intent"]
    assert changed["intent_authority"] == building["intent_authority"]


@pytest.mark.asyncio
async def test_capacity_preflight_exception_rolls_back_real_queue_transaction(task_api_env, monkeypatch):
    design, failed, _, history_path, owner = await _capacity_failed_task(task_api_env, monkeypatch)
    before = design.store.get()

    def changed(_state):
        raise ValueError("preflight source changed")

    with pytest.raises(ValueError, match="preflight source changed"):
        _queue_capacity_repair(design, failed, history_path, owner, capacity_validator=changed)
    assert design.store.get() == before


@pytest.mark.asyncio
async def test_capacity_repair_receipt_cannot_be_consumed_after_second_failure(task_api_env, monkeypatch):
    import copy
    from tests.test_task_design_spec import _expected_build

    design, failed, calls, history_path, owner = await _capacity_failed_task(task_api_env, monkeypatch)
    queued = _queue_capacity_repair(design, failed, history_path, owner)
    building = design.claim_build(queued)
    with design.store.db() as db:
        state = design.store._get(db)
        state["candidate"]["autobuild"].update(phase="done", status="needs_review", reason="review_error",
            failure=copy.deepcopy(failed["build_request"]["failure_evidence"]["failure"]))
        design.store._put(db, state)
    again = design.finish_build(_expected_build(building), "failed", "same capacity failure",
        failure_origin="host_build", failure_kind="non_retryable", retryable=False,
        failure_evidence=copy.deepcopy(failed["build_request"]["failure_evidence"]))
    before = design.store.get()
    count = len(calls)
    with pytest.raises(ValueError, match="同一规格"):
        _queue_capacity_repair(design, again, history_path, owner)
    assert design.store.get() == before
    assert len(calls) == count
