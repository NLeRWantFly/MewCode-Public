from __future__ import annotations

import asyncio
import json

import pytest

from mewcode.orchestration.draft_sources import digest
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.task_design import TaskBuildError, TaskDesign, execution_scope_error, host_task_control
from mewcode.orchestration.workflow_blocks import WorkflowBlocks
from mewcode.tools.workflow_blocks import SubmitWorkflowBlocks, SubmitWorkflowBlocksParams
from tests.test_workflow_drafts import cited, entry, record, write_history


def test_task_identity_and_original_intent_survive_service_restart(tmp_path):
    first = TaskDesign(tmp_path, "paper")
    created = first.start("plan")
    reopened = TaskDesign(tmp_path, "paper")

    again = reopened.start("execute")
    assert again["task_id"] == created["task_id"]
    assert again["intent"] == "plan"
    assert again["status"] == "designing"
    assert again["revision"] == created["revision"]

    reopened.record_turn("本次目标：整理一个离线报告流程。")
    after_turn = TaskDesign(tmp_path, "paper").read()
    assert after_turn["task_id"] == created["task_id"]
    assert after_turn["intent"] == "plan"
    assert after_turn["goal"] == "本次目标：整理一个离线报告流程。"


def test_stale_task_revision_cannot_pause_or_queue_build(tmp_path):
    design = TaskDesign(tmp_path, "paper")
    created = design.start("execute")
    design.record_turn("处理这份本地样例。")

    with pytest.raises(ValueError, match="任务规格已变化"):
        design.control("pause", task_id=created["task_id"], revision=created["revision"])
    current = design.read()
    assert current["status"] == "designing"

    with pytest.raises(ValueError, match="任务规格已变化"):
        design.queue_build(task_id=created["task_id"], revision=created["revision"], sequence=0,
                           source_hash=digest([]), base_graph_hash=digest(None))


def test_build_requires_a_saved_node_and_rejects_pending_workflow_materials(tmp_path):
    project = tmp_path / "project"
    history = project / ".mewcode" / "sessions" / "paper.jsonl"
    write_history(history, [record("user", "离线目标：整理本地示例文件。")])
    design = TaskDesign(project, "paper")
    task = design.start("plan")
    design.record_turn("离线目标：整理本地示例文件。")
    current = design.read()

    with pytest.raises(ValueError, match="尚无任务步骤"):
        design.queue_build(task_id=current["task_id"], revision=current["revision"], sequence=0,
                           source_hash=digest([]), base_graph_hash=digest(None))

    blocks = WorkflowBlocks(project, "paper", source_path=history)
    read = blocks.read()
    source_id = read["source_index"][0]["id"]
    source = blocks.read(source_ids=[source_id])["sources"][0]
    # This intentionally invalid material is stored as a pending repair, proving
    # the build gate reads the real WorkflowBlocks ledger rather than a mock.
    invalid = cited(entry("prepare", body="整理样例", data={
        "node_kind": "implement", "outputs": ["已整理文件"],
        "acceptance": ["输出存在"], "unsupported_field": "拒绝"}), source)
    tool = SubmitWorkflowBlocks(blocks, turn_id=lambda: "synthetic-turn")
    result = asyncio.run(tool.execute(SubmitWorkflowBlocksParams(
        expected_sequence=read["sequence"], source_hash=read["source_hash"],
        base_graph_hash=read["base_graph_hash"], upserts=[invalid], ignored=[], use_jev=False)))
    assert not result.is_error, result.output
    receipt = json.loads(result.output)
    assert receipt["pending"], "the schema error should remain visible for repair"

    current = TaskDesign(project, "paper").read()
    materials = (DraftStore(project, "paper").get().get("materials") or {})
    assert materials["pending"]
    with pytest.raises(ValueError, match="仍有待修正字段"):
        design.queue_build(task_id=task["task_id"], revision=current["revision"],
                           sequence=materials["sequence"], source_hash=read["source_hash"],
                           base_graph_hash=read["base_graph_hash"])


def _queued_design(tmp_path, *, intent="plan"):
    design = TaskDesign(tmp_path, "lifecycle")
    design.start(intent)
    current = design.record_turn("整理合成资料并生成编排。")
    with design.store.db() as db:
        state = design.store._get(db)
        state["materials"] = {"sequence": 1, "pending": [], "document": {"entries": [
            {"id": "sample", "kind": "node", "state": "active"}]}}
        design.store._put(db, state)
    queued = design.queue_build(task_id=current["task_id"], revision=current["revision"], sequence=1,
                                source_hash="full-source", user_source_hash="users", base_graph_hash="base",
                                requirement_fingerprint="requirements")
    return design, queued


def _expected_build(value):
    request = value["build_request"]
    return {**request, "task_id": value["task_id"], "task_revision": value["revision"],
            "build_id": request["id"], "build_epoch": request["epoch"]}


@pytest.mark.parametrize("claimed", [False, True])
def test_pause_and_resume_retain_same_build_and_fence_old_callbacks(tmp_path, claimed):
    design, queued = _queued_design(tmp_path)
    current = design.claim_build(queued) if claimed else queued
    expected = _expected_build(current)
    paused = design.control("pause", task_id=current["task_id"], revision=current["revision"])
    assert paused["build_request"]["id"] == queued["build_request"]["id"]
    assert paused["build_request"]["state"] == "paused"
    resumed = design.control("resume", task_id=paused["task_id"], revision=paused["revision"])
    assert resumed["status"] == "queued"
    assert resumed["build_request"]["id"] == queued["build_request"]["id"]
    assert resumed["build_request"]["sequence"] == 1
    with pytest.raises(ValueError):
        design.finish_build(expected, "ready", "迟到保存")
    newly_claimed = design.claim_build(resumed)
    assert newly_claimed["build_request"]["epoch"] > expected["build_epoch"]
    with pytest.raises(ValueError):
        design.claim_build(resumed)
    design.check_build(_expected_build(newly_claimed))


def test_trusted_whole_message_control_keeps_queue_but_new_requirements_supersede_it(tmp_path):
    design, queued = _queued_design(tmp_path)
    control = host_task_control(" 继续任务 ")
    assert control == "continue"
    assert host_task_control("继续任务，但把交付改为PDF") == ""
    assert host_task_control("继续", has_attachments=True) == ""
    assert design.record_turn("继续任务", control=control) == queued
    changed = design.record_turn("继续任务，但把交付改为PDF")
    assert changed["status"] == "designing"
    assert changed["build_request"]["state"] == "superseded"
    assert changed["build_request"]["epoch"] > queued["build_request"]["epoch"]
    with pytest.raises(ValueError):
        design.claim_build(queued)


def test_execution_authority_is_revoked_by_restriction_and_requires_explicit_versioned_plan_correction(tmp_path):
    design = TaskDesign(tmp_path, "intent")
    execute = design.start("execute")
    restricted = design.record_turn("本次只设计并保存编排图，不要执行业务。")
    assert restricted["intent"] == "execute"
    assert "用户目标限定" in execution_scope_error(restricted)
    with pytest.raises(ValueError, match="当前任务版本"):
        design.start("plan")
    with pytest.raises(ValueError, match="任务规格已变化"):
        design.set_intent("plan", task_id=execute["task_id"], revision=execute["revision"])
    plan = design.set_intent("plan", task_id=restricted["task_id"], revision=restricted["revision"])
    assert plan["intent"] == "plan" and execution_scope_error(plan) == ""
    assert design.start("execute")["intent"] == "plan"
    with pytest.raises(ValueError, match="不能增加执行授权"):
        design.set_intent("execute", task_id=plan["task_id"], revision=plan["revision"])
    legacy = {"intent": "execute", "goal": "仅保存设计"}
    assert "受信用户入口" in execution_scope_error(legacy)


def test_canvas_commit_receipt_and_execution_claim_are_at_most_once(tmp_path):
    design, queued = _queued_design(tmp_path, intent="execute")
    building = design.claim_build(queued)
    expected = _expected_build(building)
    with design.commit_build(expected) as receipt:
        receipt["spec"] = {"meta": {"pipeline_graph_version": 2}}
    saved = design.saved_build(expected)
    assert saved["status"] == "ready" and saved["build_request"]["state"] == "saved"
    with pytest.raises(ValueError):
        with design.commit_build(expected):
            pytest.fail("an old callback reached canvas commit twice")
    dispatching = design.claim_execution(saved)
    with pytest.raises(ValueError):
        design.claim_execution(saved)
    paused = design.control("pause", task_id=dispatching["task_id"], revision=dispatching["revision"])
    with pytest.raises(ValueError, match="启动结果尚未确认"):
        design.control("resume", task_id=paused["task_id"], revision=paused["revision"])


def test_failed_basis_cannot_be_requeued_but_new_basis_keeps_lifetime_attempt_evidence(tmp_path):
    design, queued = _queued_design(tmp_path)
    building = design.claim_build(queued)
    failed = design.finish_build(_expected_build(building), "failed", "同一需求缺少输出绑定",
                                 failure_fingerprint="binding-error", repair_attempts=2)
    args = {k: queued["build_request"][k] for k in
            ("sequence", "source_hash", "base_graph_hash", "user_source_hash", "requirement_fingerprint")}
    with pytest.raises(ValueError, match="同一规格的构图已失败"):
        design.queue_build(task_id=failed["task_id"], revision=failed["revision"], **args)
    with design.store.db() as db:
        state = design.store._get(db)
        state["materials"]["sequence"] = 2
        design.store._put(db, state)
    args.update(sequence=2, requirement_fingerprint="corrected requirements")
    changed = design.queue_build(task_id=failed["task_id"], revision=failed["revision"], **args)
    assert changed["build_request"]["id"] != queued["build_request"]["id"]
    claimed = design.claim_build(changed)
    assert claimed["build_attempts"] == 2
    assert claimed["build_request"]["attempts"] == 1


def test_known_nonstart_releases_dispatch_claim_and_plan_correction_can_resume_saved_graph(tmp_path):
    design, queued = _queued_design(tmp_path, intent="execute")
    building = design.claim_build(queued)
    ready = design.finish_build(_expected_build(building), "ready", "编排图已保存。")
    dispatching = design.claim_execution(ready)
    rejected = design.reject_execution(dispatching, "当前环境条件尚未满足。")
    assert rejected["status"] == "blocked"
    assert "execution_state" not in rejected["build_request"]
    resumed = design.control("resume", task_id=rejected["task_id"], revision=rejected["revision"])
    retry = design.claim_execution(resumed)
    paused = design.control("pause", task_id=retry["task_id"], revision=retry["revision"])
    plan = design.set_intent("plan", task_id=paused["task_id"], revision=paused["revision"])
    safe = design.control("resume", task_id=plan["task_id"], revision=plan["revision"])
    assert safe["status"] == "ready" and safe["intent"] == "plan"
    assert safe["build_request"]["execution_state"] == "dispatching"


def test_new_requirements_supersede_saved_result_before_pause_and_resume(tmp_path):
    design, queued = _queued_design(tmp_path)
    building = design.claim_build(queued)
    design.finish_build(_expected_build(building), "ready", "编排图已保存。")
    updated = design.record_turn("把交付内容改为PDF并补充验收步骤。")
    assert updated["build_request"]["state"] == "superseded"
    paused = design.control("pause", task_id=updated["task_id"], revision=updated["revision"])
    # A control must not turn a superseded saved result into an active build.
    assert paused["build_request"]["state"] == "superseded"
    resumed = design.control("resume", task_id=paused["task_id"], revision=paused["revision"])
    assert resumed["status"] == "designing"


@pytest.mark.parametrize("retryable", [False, None, 1, "true"])
def test_typed_build_failure_requires_a_strict_boolean(retryable):
    assert TaskBuildError("retryable=true RemoteProtocolError", retryable=retryable).retryable is False
    assert TaskBuildError("host classification", retryable=True).retryable is True


def _build_args(value):
    return {key: value["build_request"][key] for key in
            ("sequence", "source_hash", "base_graph_hash", "user_source_hash", "requirement_fingerprint")}


@pytest.mark.parametrize("count", [1, 2])
def test_host_transport_failure_can_requeue_same_basis_without_discarding_candidate(tmp_path, count):
    design, queued = _queued_design(tmp_path)
    building = design.claim_build(queued)
    candidate = {"document": {"entries": [entry("saved-seven-chunks", body="retained material")]},
                 "processed_ids": ["already-processed"], "log": [{"status": "ok", "chunk": 7}]}
    with design.store.db() as db:
        state = design.store._get(db)
        state["candidate"] = candidate
        design.store._put(db, state)
    evidence = {"reason": "model_error", "failure": {"retryable": True, "category": "upstream"}}
    failed = design.finish_build(_expected_build(building), "needs_input", "transport cut",
        failure_origin="host_build", failure_kind="transport", retryable=True,
        transport_failure_count=count, failure_fingerprint="same-cut", failure_evidence=evidence, repair_attempts=0)
    fresh = design.queue_build(task_id=failed["task_id"], revision=failed["revision"], **_build_args(failed))
    previous = fresh["build_request"]["previous_failure"]
    assert previous["transport_failure_count"] == count and previous["retryable"] is True
    assert previous["failure_origin"] == "host_build" and previous["failure_kind"] == "transport"
    assert previous["evidence"] == evidence and previous["fingerprint"] == "same-cut"
    assert design.store.get()["candidate"] == candidate


@pytest.mark.parametrize("untrusted", [
    {"retryable": True, "failure_kind": "transport", "transport_failure_count": 1},
    {"failure_origin": "host_build", "retryable": True, "failure_kind": "non_retryable", "transport_failure_count": 1},
    {"failure_origin": "host_build", "retryable": "true", "failure_kind": "transport", "transport_failure_count": 1},
    {"failure_origin": "host_build", "retryable": True, "failure_kind": "transport", "transport_failure_count": True},
])
def test_old_unknown_or_nonretryable_failure_keeps_same_basis_guard(tmp_path, untrusted):
    design, queued = _queued_design(tmp_path)
    failed = design.finish_build(_expected_build(design.claim_build(queued)), "failed",
                                 "RemoteProtocolError retryable=true", **untrusted)
    with pytest.raises(ValueError, match="同一规格的构图已失败"):
        design.queue_build(task_id=failed["task_id"], revision=failed["revision"], **_build_args(failed))
    assert design.read() == failed


@pytest.mark.parametrize("superseded", [False, True])
def test_transport_budget_stops_third_same_basis_failure_and_new_basis_can_progress(tmp_path, superseded):
    design, queued = _queued_design(tmp_path)
    failed = design.finish_build(_expected_build(design.claim_build(queued)), "failed", "transport cut",
        failure_origin="host_build", failure_kind="transport", retryable=True, transport_failure_count=3)
    if superseded:
        failed = design.record_turn("继续核对当前规格。")
    with pytest.raises(ValueError, match="瞬时连接失败上限"):
        design.queue_build(task_id=failed["task_id"], revision=failed["revision"], **_build_args(failed))
    with design.store.db() as db:
        state = design.store._get(db)
        state["materials"]["sequence"] = 2
        design.store._put(db, state)
    args = {**_build_args(failed), "sequence": 2, "requirement_fingerprint": "new requirements"}
    assert design.queue_build(task_id=failed["task_id"], revision=failed["revision"], **args)["status"] == "queued"


def test_transport_retry_cannot_bypass_revision_material_or_cancel_fences(tmp_path):
    design, queued = _queued_design(tmp_path)
    building = design.claim_build(queued)
    expected = _expected_build(building)
    failed = design.finish_build(expected, "needs_input", "transport cut", failure_origin="host_build",
                                failure_kind="transport", retryable=True, transport_failure_count=1)
    with pytest.raises(ValueError, match="任务规格已变化"):
        design.queue_build(task_id=failed["task_id"], revision=building["revision"], **_build_args(failed))
    def stale_snapshot():
        raise ValueError("SNAPSHOT_STALE")
    with pytest.raises(ValueError, match="SNAPSHOT_STALE"):
        design.queue_build(task_id=failed["task_id"], revision=failed["revision"],
                           basis_validator=stale_snapshot, **_build_args(failed))
    assert design.read() == failed
    fresh = design.queue_build(task_id=failed["task_id"], revision=failed["revision"], **_build_args(failed))
    active = design.claim_build(fresh)
    paused = design.control("pause", task_id=active["task_id"], revision=active["revision"])
    with pytest.raises(ValueError):
        design.finish_build(_expected_build(active), "needs_input", "late transport error", retryable=True,
                            failure_origin="host_build", failure_kind="transport", transport_failure_count=2)
    assert design.read() == paused
