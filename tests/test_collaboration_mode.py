from __future__ import annotations

import asyncio
import copy
from dataclasses import replace

import pytest

from mewcode.expert_group import ExpertGroupOrchestrator, ExpertProfile, ExpertRoleResult
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.kernel import collaboration as ledger
from mewcode.kernel.acceptance import Acceptance
from mewcode.kernel.events import EventJournal
from mewcode.model_registry import ModelRef
from mewcode.orchestration.collab_compile import compile_plan
from mewcode.orchestration.collab_runtime import CollaborationRuntime
from mewcode.orchestration.task_design import TaskDesign

RUN_ID = "a" * 32
EXECUTOR = ModelRef("execution", "model-a", "")
REVIEWER = ModelRef("verification", "model-b", "")
PROFILE = ExpertProfile(manager=EXECUTOR, planner=EXECUTOR, executor=EXECUTOR, auditor=REVIEWER)
CATALOG = {"models": [{**ref.as_dict(), "available": True, "tools": True, "provider_brand": brand}
                       for ref, brand in [(EXECUTOR, "brand-a"), (REVIEWER, "brand-b")]]}


def flow(mode="collaborative"):
    meta = {"orchestration_mode": mode, "node_plan_briefs": {
        "work": {"known_inputs": ["目标"], "outputs": ["候选"], "acceptance": ["候选符合原始目标"]},
        "deliver": {"known_inputs": ["候选"], "outputs": ["结果"], "acceptance": ["全量结果符合原始目标"]}}}
    if mode == "collaborative":
        meta["collaboration_spec"] = {"format": ledger.FORMAT, "task_id": "task-one", "goal": "交付测试结果",
            "document": {"entries": [{"id": "goal", "kind": "goal", "title": "目标", "body": "交付测试结果",
                                       "state": "active", "data": {}, "sources": [{"source_id": "user_1", "quote": "交付测试结果"}]}]}}
    return ExpertFlowSpec.from_value({"flow_id": "sample", "version": 1, "label": "样例", "meta": meta,
        "nodes": [{"node_id": key, "role": "executor", "label": key, "responsibility": "交付测试结果",
                   "failure_policy": {"action": "stop", "max_attempts": 1}} for key in ("work", "deliver")],
        "edges": [{"from_node": "work", "to_node": "deliver"}], "finalizer": "deliver"})


def store_for(tmp_path, selected="collaborative"):
    store = ExpertRunStore(tmp_path / "runs", storage='sqlite')
    store.create(run_id=RUN_ID, conversation_id="conversation", task="交付测试结果", attachments=[],
                 profile=PROFILE.as_dict(), strategy="stable", flow=flow(selected))
    store.mutate(RUN_ID, lambda record: record.update(role_models={"roles": {"verifier": REVIEWER.as_dict(),
        "reviewer": REVIEWER.as_dict(), "executor": EXECUTOR.as_dict(), "planner": EXECUTOR.as_dict()}}), durable=True)
    return store


def delivery(text="内容"):
    return ExpertRoleResult(text=text, handoff={"status": "completed", "inline_outputs": [{"name": "结果", "content": text}]})


def require_reviews(store, *keys):
    """Boundary fixtures explicitly declare a prerequisite semantic review."""
    def apply(record):
        for key in keys:
            for check in record['collaboration']['contracts'][key]['checks']:
                if check['kind'] == 'review':
                    check['blocking'] = True
    store.mutate(RUN_ID, apply, durable=True)


def core():
    record = {"run_id": RUN_ID}
    return ledger.initialize(record, compile_plan(flow()))


def accept(value, node="work"):
    attempt = ledger.dispatch(value, node)
    report = passing_report(value, node)
    ledger.complete(value, attempt["id"], report)
    return attempt, report


def passing_report(value, node="work"):
    return {"passed": True, "checks": [{"id": check["id"], "version": check["version"], "passed": True,
            "evidence": {"host": "observed"}} for check in value["contracts"][node]["checks"]], "artifacts": []}


def test_mode_is_chosen_at_creation_and_cannot_change_existing_task(tmp_path):
    design = TaskDesign(tmp_path, "conversation")
    initial = design.start("plan", orchestration_mode="collaborative")
    assert initial["orchestration_mode"] == "collaborative"
    assert design.start("plan")["orchestration_mode"] == "collaborative"
    with pytest.raises(ValueError, match="固定编排模式"):
        design.start("plan", orchestration_mode="classic")
    assert TaskDesign(tmp_path, "other").start("execute")["orchestration_mode"] == "classic"
    with pytest.raises(ValueError, match="模式无效"):
        TaskDesign(tmp_path, "invalid").start("plan", orchestration_mode="unknown")


def test_plan_cannot_start_without_provenance_or_acceptance():
    raw = flow().as_dict()
    del raw["meta"]["collaboration_spec"]
    with pytest.raises(ValueError, match="来源快照"):
        compile_plan(ExpertFlowSpec.from_value(raw))
    raw = flow().as_dict()
    raw["meta"]["node_plan_briefs"]["work"]["acceptance"] = []
    with pytest.raises(ValueError, match="缺少验收标准"):
        compile_plan(ExpertFlowSpec.from_value(raw))


def test_dispatch_and_acceptance_are_idempotent_and_no_unknown_replay():
    value = core()
    attempt = ledger.dispatch(value, "work")
    with pytest.raises(ValueError, match="不能重复"):
        ledger.dispatch(value, "work")
    with pytest.raises(ValueError, match="上游"):
        ledger.dispatch(value, "deliver")
    report = passing_report(value)
    certificate = ledger.complete(value, attempt["id"], report)
    assert ledger.complete(value, attempt["id"], report) == certificate
    assert ledger.dispatch(value, "work") is None
    assert ledger.dispatch(value, "deliver") is not None


def test_user_revision_fences_late_delivery_and_invalidates_downstream():
    value = core()
    accept(value)
    attempt = ledger.dispatch(value, "deliver")
    revised = {**value["entries"]["goal"], "body": "新的完成定义"}
    ledger.proposal(value, proposal_id="user-edit", changes={"goal": revised}, evidence=["source_2"],
                    actor="user", base_version=1, epoch=1)
    assert not ledger.valid_certificate(value, "work")
    with pytest.raises(ValueError, match="旧回执"):
        ledger.complete(value, attempt["id"], {"passed": True, "checks": []})
    with pytest.raises(ValueError, match="待处理修订"):
        ledger.dispatch(value, "work")


def test_model_cannot_relax_user_or_unknown_constraint_and_source_cannot_be_forged():
    value = core()
    revised = {**value["entries"]["goal"], "body": "少做一点"}
    with pytest.raises(ValueError, match="用户明确决定"):
        ledger.proposal(value, proposal_id="model-edit", changes={"goal": revised}, evidence=["e"],
                        actor="coordinator", base_version=1, epoch=1)
    revised["origin"] = "model"
    with pytest.raises(ValueError, match="来源"):
        ledger.proposal(value, proposal_id="model-edit", changes={"goal": revised}, evidence=["e"],
                        actor="coordinator", base_version=1, epoch=1)


def test_independent_semantic_review_and_related_version_fence():
    value = core()
    revised = {**value["entries"]["goal"], "body": "新的完成定义"}
    ledger.proposal(value, proposal_id="edit", changes={"goal": revised}, evidence=["source_2"],
                    actor="user", base_version=1, epoch=1)
    review = {"passed": True, "evidence": ["covered"]}
    with pytest.raises(ValueError, match="独立复核"):
        ledger.adopt(value, "edit", review=review, proposer_vendor="same", reviewer_vendor="same")
    assert ledger.adopt(value, "edit", review=review, proposer_vendor="a", reviewer_vendor="b")
    assert not ledger.adopt(value, "edit", review=review, proposer_vendor="a", reviewer_vendor="b")
    assert value["spec_version"] == 2
    assert value["entries"]["goal"]["body"] == "新的完成定义"


def test_takeover_fences_old_response_without_resetting_calls():
    value = core()
    attempt = ledger.dispatch(value, "work")
    value["calls"]["used"] = {"status": "dispatched"}
    ledger.takeover(value, REVIEWER.as_dict(), source_ref="host-choice")
    assert value["coordinator_epoch"] == 2 and "used" in value["calls"]
    with pytest.raises(ValueError, match="旧回执"):
        ledger.complete(value, attempt["id"], {"passed": True, "checks": []})
    ledger.takeover(value, REVIEWER.as_dict(), source_ref="host-choice")
    assert value["coordinator_epoch"] == 2


@pytest.mark.asyncio
async def test_mutated_file_during_review_cannot_be_delivered(tmp_path):
    path = tmp_path / "result.txt"
    path.write_text("original", encoding="utf-8")
    async def review(payload, context):
        path.write_text("changed", encoding="utf-8")
        return {"passed": True, "evidence": ["reviewed original"]}
    result = delivery()
    result.handoff["artifacts"] = [{"path": "result.txt"}]
    report = await Acceptance(tmp_path, review=review).check(core()["contracts"]["work"], result, context={})
    assert not report["passed"] and "变化" in report["reason"]


@pytest.mark.asyncio
async def test_file_existence_alone_and_unreceipted_commands_never_certify(tmp_path):
    (tmp_path / "result.txt").write_text("x", encoding="utf-8")
    result = delivery()
    result.handoff["artifacts"] = [{"path": "result.txt"}]
    contract = core()["contracts"]["work"]
    contract["checks"] = [{"id": "exists", "kind": "file_exists", "path": "result.txt", "version": 1}]
    assert not (await Acceptance(tmp_path).check(contract, result, context={}))["passed"]
    contract["checks"] = [{"id": "test", "kind": "test", "command": "test", "version": 1}]
    async def unreceipted(*args):
        return {"exit_code": 0}
    assert not (await Acceptance(tmp_path, command=unreceipted).check(contract, result, context={}))["passed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", ["classic", "collaborative"])
async def test_real_scheduler_both_modes_and_no_duplicate_resume(tmp_path, selected):
    store = store_for(tmp_path, selected)
    invoked, reviewed = [], []
    async def runner(node, model, prompt, policy, member=None):
        invoked.append(node.node_id)
        if selected == "collaborative":
            assert "协作工作合同" in prompt
            assert not policy.allow_delegation
        return delivery(node.node_id)
    async def reviewer(model, payload, context):
        reviewed.append(context["node_id"])
        return {"passed": True, "evidence": ["inline content matches goal"]}
    orchestrator = ExpertGroupOrchestrator(runner=None, node_runner=runner, run_store=store,
        work_dir=tmp_path, archive_root=tmp_path / "archive", collaboration_review=reviewer,
        event_sink=lambda event: store.append_event(RUN_ID, event))
    result = await orchestrator.run("交付测试结果", PROFILE, CATALOG, strategy="stable", run_id=RUN_ID,
        flow=flow(selected), checkpoint=store.load(RUN_ID))
    assert result.status == "completed", result.final_summary
    assert invoked == ["work", "deliver"]
    assert reviewed == (["deliver"] if selected == "collaborative" else [])
    if selected == "collaborative":
        expected = store.load(RUN_ID)["collaboration"]
        assert all(ledger.valid_certificate(expected, key) for key in ("work", "deliver"))
        assert EventJournal(store.root, RUN_ID).load()["collaboration"] == expected
        reloaded = ExpertRunStore(store.root)
        orchestrator.run_store = reloaded
        orchestrator.event_sink = lambda event: reloaded.append_event(RUN_ID, event)
        resumed = await orchestrator.run("交付测试结果", PROFILE, CATALOG, strategy="stable", run_id=RUN_ID,
            flow=flow(selected), checkpoint=reloaded.load(RUN_ID))
        assert resumed.status == "completed"
        assert invoked == ["work", "deliver"]


@pytest.mark.asyncio
async def test_failed_acceptance_never_dispatches_downstream(tmp_path):
    store = store_for(tmp_path)
    require_reviews(store, 'work')
    invoked = []
    async def runner(node, *args):
        invoked.append(node.node_id)
        return delivery()
    async def reviewer(*args):
        return {"passed": False, "evidence": ["missing requirement"]}
    orchestrator = ExpertGroupOrchestrator(runner=None, node_runner=runner, run_store=store, work_dir=tmp_path,
        archive_root=tmp_path / "archive", collaboration_review=reviewer,
        event_sink=lambda event: store.append_event(RUN_ID, event))
    result = await orchestrator.run("task", PROFILE, CATALOG, strategy="stable", run_id=RUN_ID,
        flow=flow(), checkpoint=store.load(RUN_ID))
    assert result.status != "completed"
    assert "deliver" not in invoked
    assert not ledger.valid_certificate(store.load(RUN_ID)["collaboration"], "work")


@pytest.mark.asyncio
async def test_interrupted_delivery_is_not_reexecuted(tmp_path):
    store = store_for(tmp_path)
    runtime = CollaborationRuntime(store=store, run_id=RUN_ID, flow=flow(), catalog=CATALOG, work_dir=tmp_path)
    runtime.mutate(lambda value: ledger.dispatch(value, "work"))
    from mewcode.expert_group import derive_execution_policy
    invoked = []
    async def runner(*args):
        invoked.append(True)
        return delivery()
    result = await runtime.execute(flow().node_map["work"], EXECUTOR, "", derive_execution_policy(False), None, runner)
    assert result.status == "blocked" and not invoked


@pytest.mark.asyncio
async def test_same_vendor_review_is_not_called(tmp_path):
    store = store_for(tmp_path)
    require_reviews(store, 'work')
    store.mutate(RUN_ID, lambda record: record["role_models"]["roles"].update(verifier=EXECUTOR.as_dict()), durable=True)
    invoked = []
    async def reviewer(*args):
        invoked.append(True)
        return {"passed": True, "evidence": ["claimed"]}
    runtime = CollaborationRuntime(store=store, run_id=RUN_ID, flow=flow(), catalog=CATALOG,
                                   work_dir=tmp_path, review=reviewer)
    from mewcode.expert_group import derive_execution_policy
    async def runner(*args):
        return delivery()
    result = await runtime.execute(flow().node_map["work"], EXECUTOR, "", derive_execution_policy(False), None, runner)
    assert result.status != "completed" and not invoked


@pytest.mark.parametrize("mode", ["classic", "collaborative"])
@pytest.mark.parametrize("kind,data", [
    ("milestone", {}), ("acceptance", {}), ("assumption", {"value": "有输入"}),
    ("node", {"contract": {}}), ("constraint", {"strength": "hard"}),
])
def test_workflow_contract_and_submission_follow_fixed_mode(tmp_path, mode, kind, data):
    from tests.test_workflow_blocks import make_blocks, record, user_sources
    from mewcode.orchestration.workflow_draft import entry_contract

    TaskDesign(tmp_path, "chat").start("plan", orchestration_mode=mode)
    blocks = make_blocks(tmp_path, rows=[record("user", "交付报告")])
    snapshot = blocks.read()
    assert snapshot["contract"] == entry_contract(mode)
    assert ("milestone" in snapshot["contract"]["entry_kinds"]) == (mode == "collaborative")
    source = user_sources(blocks, snapshot)[0]
    entry = {"id": "planned", "kind": kind, "title": "交付报告", "body": "交付报告",
             "data": data, "sources": [{"source_id": source["id"], "quote": source["text"]}]}
    receipt = blocks.submit(expected_sequence=snapshot["sequence"], source_hash=snapshot["source_hash"],
                            base_graph_hash=snapshot["base_graph_hash"], upserts=[entry], ignored=[])
    assert receipt["saved_entry_ids"] == (["planned"] if mode == "collaborative" else [])
    if mode == "classic":
        assert receipt["pending"]
        assert receipt["contract"] == entry_contract()
