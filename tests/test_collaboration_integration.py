"""Offline collaboration boundaries through journal, scheduler, waits and API."""
import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from mewcode.agent import AskUserRequest
from mewcode.expert_group import ExpertGroupOrchestrator
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.experts.waits import wait_for_interaction
from mewcode.kernel import collaboration as ledger
from mewcode.kernel.acceptance import Acceptance
from mewcode.kernel.constraints import annotate, record_challenge
from mewcode.orchestration import ask_gateway
from mewcode.orchestration.collab_coordinator import CollaborationCoordinator
from mewcode.orchestration.collab_service import bound_record, user_revision
from mewcode.orchestration.task_design import TaskDesign
from tests.test_collaboration_mode import (CATALOG, EXECUTOR, PROFILE, REVIEWER, RUN_ID,
    accept, core, delivery, flow, passing_report, store_for)
from tests.test_task_design_entry import task_api_env


def orchestrator(store, tmp_path, runner, review, coordinate=None):
    return ExpertGroupOrchestrator(runner=None, node_runner=runner, run_store=store, work_dir=tmp_path,
        archive_root=tmp_path / "archive", collaboration_review=review, collaboration_coordinate=coordinate,
        event_sink=lambda event: store.append_event(RUN_ID, event))


async def run(host, store, spec=None):
    return await host.run("完成原始目标", PROFILE, CATALOG, strategy="stable", run_id=RUN_ID,
        flow=spec or flow(), checkpoint=store.load(RUN_ID))


def test_a_report_cannot_omit_replace_or_forge_contract_checks():
    value = core()
    attempt = ledger.dispatch(value, "work")
    for bad in ({"passed": True, "checks": []}, {"passed": True, "checks": [{"id": "invented", "version": 1, "passed": True}]}):
        with pytest.raises(ValueError, match="逐项覆盖"):
            ledger.complete(value, attempt["id"], bad)
    assert not ledger.valid_certificate(value, "work")


def test_snapshot_has_fixed_bytes_and_detects_corruption(tmp_path):
    original = tmp_path / "result.json"
    original.write_text('{"value": 1}', encoding="utf-8")
    acceptance = Acceptance(tmp_path)
    artifacts = acceptance.freeze({"artifacts": [{"path": "result.json"}]})
    frozen = tmp_path / artifacts[0]["snapshot_path"]
    original.write_text('{"value": 2}', encoding="utf-8")
    assert frozen.read_text(encoding="utf-8") == '{"value": 1}'
    assert not acceptance.unchanged(artifacts)
    original.write_text('{"value": 1}', encoding="utf-8")
    frozen.write_text("damaged", encoding="utf-8")
    with pytest.raises(ValueError, match="存储损坏"):
        acceptance.freeze({"artifacts": [{"path": "result.json"}]})


def test_semantic_changes_do_not_reset_no_progress_budget():
    value = core()
    for version in range(1, 4):
        value["contracts"]["work"]["version"] = version
        attempt = ledger.dispatch(value, "work")
        ledger.complete(value, attempt["id"], {"passed": False, "checks": [], "artifacts": []})
    value["contracts"]["work"]["version"] += 1
    with pytest.raises(ValueError, match="尝试上限"):
        ledger.dispatch(value, "work")


@pytest.mark.parametrize("kind,role,origin", [("message", "user", "user"), ("message", "assistant", "model"),
    ("template", "assistant", "template"), ("host_policy", "tool", "host"), ("message", "tool", "observation")])
def test_constraint_origin_comes_from_host_sources(kind, role, origin):
    entry = {"id": "constraint", "kind": "constraint", "body": "Keep", "data": {"scope": {"milestone_id": "phase"}},
             "origin": "model", "sources": [{"source_id": "source", "quote": "Keep"}]}
    source = {"sources": [{"id": "source", "kind": kind, "role": role}]}
    actual = annotate(entry, source)
    assert actual["origin"] == origin
    assert actual["scope"] == {"milestone_id": "phase"}


def test_challenges_deduplicate_per_constraint_version_and_evidence():
    value = core()
    for key in ("c1", "c2"):
        value["entries"][key] = {"id": key, "kind": "constraint", "version": 1}
    first = record_challenge(value, "c1", ["e1"], reason="method failed")
    value["challenges"][first["id"]]["status"] = "rejected"
    assert record_challenge(value, "c1", ["e1"], reason="try again")["cached"]
    assert "cached" not in record_challenge(value, "c2", ["e1"], reason="different constraint")
    assert "cached" not in record_challenge(value, "c1", ["e2"], reason="new evidence")


@pytest.mark.asyncio
async def test_coordinator_caches_events_and_stops_at_persistent_budget(tmp_path):
    store = store_for(tmp_path)
    invoked = []
    async def ask(model, payload, system):
        invoked.append(model)
        return {"action": "wait", "reason": "need original evidence"}
    coordinator = CollaborationCoordinator(store, RUN_ID, CATALOG, ask=ask)
    store.mutate(RUN_ID, lambda row: row["collaboration"]["policy"].update(max_model_calls=1), durable=True)
    first = await coordinator.decide(event_id="event", node_id="work", evidence=["evidence"])
    assert await coordinator.decide(event_id="event", node_id="work", evidence=["evidence"]) == first
    with pytest.raises(ValueError, match="预算"):
        await coordinator.decide(event_id="next", node_id="work", evidence=["new evidence"])
    assert invoked == [EXECUTOR]


@pytest.mark.asyncio
async def test_takeover_releases_lease_and_ignores_late_coordinator(tmp_path):
    store = store_for(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    async def ask(*args):
        started.set()
        await release.wait()
        return {"action": "retry", "reason": "old coordinator"}
    coordinator = CollaborationCoordinator(store, RUN_ID, CATALOG, ask=ask)
    old = asyncio.create_task(coordinator.decide(event_id="event", node_id="work", evidence=["e"]))
    await asyncio.wait_for(started.wait(), 3)
    with pytest.raises(ValueError, match="另一项"):
        await coordinator.decide(event_id="other", node_id="work", evidence=["a distinct diagnostic"])
    store.mutate(RUN_ID, lambda row: ledger.takeover(row["collaboration"], REVIEWER.as_dict(), source_ref="user-choice"), durable=True)
    release.set()
    with pytest.raises(ValueError, match="历史记录"):
        await old
    assert store.load(RUN_ID)["collaboration"]["coordinator_lease"]["status"] != "active"
    assert any(call["status"] == "stale" for call in store.load(RUN_ID)["collaboration"]["calls"].values())


@pytest.mark.asyncio
async def test_final_quality_failure_completes_as_draft_with_findings(tmp_path):
    store = store_for(tmp_path)
    invoked = []
    async def runner(node, *args):
        invoked.append(node.node_id)
        return delivery()
    async def review(model, payload, context):
        return {"passed": payload["contract"]["id"] != "final", "evidence": ["whole result misses original goal"]}
    host = orchestrator(store, tmp_path, runner, review)
    result = await run(host, store)
    assert result.status == "completed" and '初稿已完成' in result.final_summary and invoked == ["work", "deliver"]
    assert store.load(RUN_ID)["collaboration"]["integrations"]["final"]["report"]["passed"] is False
    await run(host, store)
    assert invoked == ["work", "deliver"]


def milestone_flow():
    raw = flow().as_dict()
    entries = raw["meta"]["collaboration_spec"]["document"]["entries"]
    for node, phase in (("work", "phase_one"), ("deliver", "phase_two")):
        entries.extend([{"id": phase, "kind": "milestone", "body": phase, "state": "active", "sources": [], "data": {}},
                        {"id": node, "kind": "node", "body": node, "state": "active", "sources": [],
                         "data": {"contract": {"milestone_id": phase}}}])
    return ExpertFlowSpec.from_value(raw)


@pytest.mark.asyncio
async def test_next_milestone_waits_for_integrated_acceptance(tmp_path):
    spec = milestone_flow()
    rows = spec.meta['collaboration_spec']['document']['entries']
    next(row for row in rows if row['id'] == 'phase_one')['data']['checks'] = [
        {'id': 'phase_input', 'kind': 'review', 'version': 1, 'blocking': True, 'criteria': ['下一阶段的必需输入可用']}]
    store = ExpertRunStore(tmp_path / "runs", storage="sqlite")
    store.create(run_id=RUN_ID, conversation_id="conversation", task="goal", attachments=[], profile=PROFILE.as_dict(), strategy="stable", flow=spec)
    store.mutate(RUN_ID, lambda row: row.update(role_models={"roles": {"verifier": REVIEWER.as_dict()}}), durable=True)
    invoked = []
    async def runner(node, *args):
        invoked.append(node.node_id)
        return delivery()
    async def review(model, payload, context):
        return {"passed": payload["contract"]["id"] != "milestone:phase_one", "evidence": ["integration evidence"]}
    result = await run(orchestrator(store, tmp_path, runner, review), store, spec)
    assert result.status == "blocked" and invoked == ["work"]


@pytest.mark.asyncio
async def test_user_revision_is_independently_reviewed_before_resume(tmp_path):
    store = store_for(tmp_path)
    store.mutate(RUN_ID, lambda row: accept(row["collaboration"]), durable=True)
    before = store.load(RUN_ID)
    user_revision(store, before, entry_id="goal", body="新的完成定义", expected_version=1, request_id="choice")
    seen = []
    async def ask(model, payload, system):
        seen.append(payload)
        return {"passed": True, "evidence": ["user revision matches proposed requirement"]}
    async def runner(node, model, prompt, *args):
        assert "新的完成定义" in prompt
        return delivery()
    async def review(*args):
        return {"passed": True, "evidence": ["matches new goal"]}
    result = await run(orchestrator(store, tmp_path, runner, review, ask), store)
    assert result.status == "completed", result.final_summary
    assert len(seen) == 1
    value = store.load(RUN_ID)["collaboration"]
    assert value["spec_version"] == 2 and value["goal"] == "新的完成定义"
    assert list(value["proposals"].values())[0]["status"] == "adopted"


def question(index=0):
    return [{"name": "choice", "question": "Business choice " + str(index),
             "options": [{"value": "a", "label": "A"}, {"value": "b", "label": "B"}]}]


def test_optional_question_limit_does_not_hide_required_or_missing_input():
    record = {"run_id": RUN_ID, "collaboration": core()}
    proof = {"reviewed": True, "reversible": True, "changes_requirements": False, "changes_authority": False,
             "missing_required_input": False, "default": {"choice": "a"}}
    first = ask_gateway.prepare(record, "work", question(1), category="optional", optional_proof=proof)
    second = ask_gateway.prepare(record, "work", question(2), category="optional", optional_proof=proof)
    defaulted = ask_gateway.prepare(record, "work", question(3), category="optional", optional_proof=proof)
    assert first["status"] == second["status"] == "waiting"
    assert defaulted["answer_source"] == "assumption"
    mandatory = ask_gateway.prepare(record, "work", question(4), category="authorization", optional_proof=proof)
    missing = ask_gateway.prepare(record, "work", question(5), category="optional", optional_proof={**proof, "missing_required_input": True})
    assert mandatory["status"] == missing["status"] == "waiting"
    with pytest.raises(ValueError, match="必要问题"):
        ask_gateway.answer(record, mandatory["id"], {"choice": "a"}, source="auto_selected_recommended")


def test_old_answer_cannot_apply_to_changed_requirement():
    record = {"run_id": RUN_ID, "collaboration": core()}
    row = ask_gateway.prepare(record, "work", question())
    record["collaboration"]["entries"]["goal"]["version"] += 1
    with pytest.raises(ValueError, match="旧答案"):
        ask_gateway.answer(record, row["id"], {"choice": "a"})


@pytest.mark.asyncio
async def test_real_question_wait_has_no_timeout_default_and_reuses_user_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("MEWCODE_EXPERT_HITL_SOFT_TIMEOUT", ".01")
    store, session, events = store_for(tmp_path), SimpleNamespace(), []
    shown = asyncio.Event()
    request = AskUserRequest("first", "AskUserQuestion", question(), asyncio.get_running_loop().create_future())
    async def emit(event):
        events.append(event)
        if event["type"] == "ask_user":
            shown.set()
    task = asyncio.create_task(wait_for_interaction(request, kind="ask_user", session=session, emit=emit,
        store=store, run_id=RUN_ID, stage_key="work"))
    await asyncio.wait_for(shown.wait(), 3)
    await asyncio.sleep(.06)
    assert not task.done()
    assert store.load(RUN_ID)["kernel_gates"]["first"]["deadline"] is None
    session._ask_user_request.future.set_result({"choice": "b"})
    assert await asyncio.wait_for(task, 3) == {"choice": "b"}
    request = AskUserRequest("again", "AskUserQuestion", question(), asyncio.get_running_loop().create_future())
    assert await wait_for_interaction(request, kind="ask_user", session=session, emit=emit,
        store=store, run_id=RUN_ID, stage_key="work") == {"choice": "b"}
    assert sum(event["type"] == "ask_user" for event in events) == 1


@pytest.mark.asyncio
async def test_real_mode_and_revision_api_preserves_task_binding(task_api_env, tmp_path, monkeypatch):
    from frontend import server
    _, _, _, owner, _ = task_api_env
    store = store_for(tmp_path)
    monkeypatch.setattr(server, "EXPERT_RUNS", store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.post("/api/orchestration/task-design", json={**owner, "action": "start", "intent": "execute", "orchestration_mode": "collaborative"})
        assert response.status_code == 200, response.text
        task = response.json()["task_design"]
        design = TaskDesign(owner["project"], owner["conversation_id"])
        design.transition(task, "completed", run_id=RUN_ID)
        store.mutate(RUN_ID, lambda row: (row.update(conversation_id=owner["conversation_id"]),
            row["collaboration"].update(task_id=task["task_id"])), durable=True)
        data = (await client.get("/api/orchestration/task-design", params=owner)).json()
        assert data["task_design"]["collaboration"]["spec_version"] == 1
        payload = {**owner, "expected_task_id": task["task_id"], "expected_version": 1,
                   "entry_id": "goal", "body": "完整新目标", "request_id": "edit-one", "actor": "coordinator"}
        response = await client.post("/api/orchestration/task-design/revision", json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["collaboration"]["pending_revisions"] == 1
        assert design.read()["status"] == "paused"
        assert store.load(RUN_ID)["status"] == "paused"
        assert list(store.load(RUN_ID)["collaboration"]["proposals"].values())[0]["payload"]["actor"] == "user"
        repeat = await client.post("/api/orchestration/task-design/revision", json=payload)
        assert repeat.status_code == 200 and len(store.load(RUN_ID)["collaboration"]["proposals"]) == 1
        stale = await client.post("/api/orchestration/task-design/revision", json={**payload, "request_id": "other", "expected_version": 0})
        assert stale.status_code == 409
        wrong = await client.post("/api/orchestration/task-design/revision", json={**payload, "conversation_id": "other"})
        assert wrong.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_bounded_loop_and_restart_use_collaboration_acceptance(tmp_path, batch):
    from tests.test_generic_loops import spec_for, report
    from mewcode.orchestration.convert import to_expert_flow
    raw = to_expert_flow(spec_for()).as_dict()
    if batch:
        raw["meta"]["loops"][0].update(mode="foreach", items=[
            {"item_id": "first", "goal": "First full result"}, {"item_id": "second", "goal": "Second full result"}])
    raw["meta"].update(orchestration_mode="collaborative", collaboration_spec=flow().meta["collaboration_spec"],
        node_plan_briefs={node["node_id"]: {"acceptance": ["fulfills original goal"], "outputs": ["result"]} for node in raw["nodes"]})
    for node in raw["nodes"]:
        node["role"] = "executor"
    spec = ExpertFlowSpec.from_value(raw)
    store = ExpertRunStore(tmp_path / "runs", storage="sqlite")
    store.create(run_id=RUN_ID, conversation_id="conversation", task="goal", attachments=[], profile=PROFILE.as_dict(), strategy="stable", flow=spec)
    store.mutate(RUN_ID, lambda row: row.update(role_models={"roles": {"verifier": REVIEWER.as_dict()}}), durable=True)
    invoked = []
    async def runner(node, model, prompt, policy, member=None):
        invoked.append(node.node_id)
        item = (policy.loop_context or {}).get("current_item") or {}
        result = delivery(node.node_id + item.get("item_id", ""))
        if batch and node.node_id == "work":
            (tmp_path / "shared.txt").write_text(item["item_id"], encoding="utf-8")
            result.handoff["artifacts"] = [{"path": "shared.txt"}]
        return replace(result, loop_decision=report(policy.loop_context,
            "FAIL" if not batch and invoked.count("check") == 1 else "PASS") if node.node_id == "check" else None)
    async def review(model, payload, context):
        if batch and context.get("integration_basis"):
            content = " ".join(row.get("content", "") for row in payload["artifacts"])
            assert "workfirst" in content and "worksecond" in content
            fixed = {row["path"] for row in payload["artifacts"] if row["kind"] == "file"}
            assert len(fixed) == 3, "Both historical bytes and the current file must reach integration"
        return {"passed": True, "evidence": ["inspected result and loop check"]}
    host = orchestrator(store, tmp_path, runner, review)
    result = await run(host, store, spec)
    assert result.status == "completed", result.final_summary
    assert invoked == ["prepare", "work", "check", "work", "check", "done"]
    again = await run(host, store, spec)
    assert again.status == "completed", again.final_summary
    assert invoked == ["prepare", "work", "check", "work", "check", "done"]
    spent_rounds = store.load(RUN_ID)["loop_states"]["improve"]["round"]
    revised = user_revision(store, store.load(RUN_ID), entry_id="goal", body="核对新的完整目标", expected_version=1, request_id="loop-revision")
    proposal_id = next(iter(revised["collaboration"]["proposals"]))
    store.mutate(RUN_ID, lambda record: ledger.adopt(record["collaboration"], proposal_id,
        review={"passed": True, "evidence": ["Host fixture independent review"]}, proposer_vendor="a", reviewer_vendor="b"), durable=True)
    after_revision = await run(host, store, spec)
    assert after_revision.status == "completed", after_revision.final_summary
    assert invoked.count("work") == (4 if batch else 3)
    assert store.load(RUN_ID)["loop_states"]["improve"]["round"] >= spent_rounds


def test_bound_collaboration_ledger_is_the_only_writable_requirements_source(tmp_path):
    from mewcode.orchestration.workflow_blocks import WorkflowBlocks
    store = store_for(tmp_path)
    design = TaskDesign(tmp_path, "conversation")
    task = design.start("execute", orchestration_mode="collaborative")
    design.transition(task, "running", run_id=RUN_ID)
    store.mutate(RUN_ID, lambda record: record["collaboration"].update(task_id=task["task_id"]), durable=True)
    blocks = WorkflowBlocks(tmp_path, "conversation", source_path=tmp_path / "empty.jsonl", run_store=store)
    with pytest.raises(ValueError, match="修订"):
        blocks.submit(expected_sequence=0, source_hash="", base_graph_hash="", upserts=[], ignored=[])


@pytest.mark.asyncio
async def test_required_branch_wait_does_not_cancel_independent_work(tmp_path):
    from mewcode.experts.waits import BranchWaitPaused
    raw = flow().as_dict()
    raw["edges"] = []
    spec = ExpertFlowSpec.from_value(raw)
    store = ExpertRunStore(tmp_path / "runs", storage="sqlite")
    store.create(run_id=RUN_ID, conversation_id="conversation", task="goal", attachments=[], profile=PROFILE.as_dict(), strategy="stable", flow=spec)
    store.mutate(RUN_ID, lambda row: row.update(role_models={"roles": {"verifier": REVIEWER.as_dict()}}), durable=True)
    invoked = []
    async def runner(node, *args):
        invoked.append(node.node_id)
        if node.node_id == "work":
            raise BranchWaitPaused("work", reason="需要选择范围")
        return delivery()
    async def review(*args):
        return {"passed": True, "evidence": ["verified"]}
    result = await run(orchestrator(store, tmp_path, runner, review), store, spec)
    assert result.status == "blocked" and invoked == ["work", "deliver"]
    attempts = list(store.load(RUN_ID)["collaboration"]["attempts"].values())
    assert attempts[0]["status"] == "dispatched" and attempts[1]["status"] == "accepted"


@pytest.mark.parametrize("problem", ["empty", "unknown", "cycle"])
def test_milestone_completion_cannot_ignore_declared_work_or_dependencies(problem):
    from mewcode.orchestration.collab_compile import compile_plan
    spec = milestone_flow()
    rows = spec.meta["collaboration_spec"]["document"]["entries"]
    if problem == "empty":
        rows.append({"id": "missing", "kind": "milestone", "body": "Unplanned goal", "data": {}})
    elif problem == "unknown":
        next(row for row in rows if row["id"] == "phase_one")["data"] = {"depends_on": ["absent"]}
    else:
        next(row for row in rows if row["id"] == "phase_one")["data"] = {"depends_on": ["phase_two"]}
    with pytest.raises(ValueError, match="里程碑"):
        compile_plan(spec)


def test_copy_keeps_classic_history_and_unknown_effects_without_forged_acceptance(tmp_path):
    from mewcode.orchestration.collab_migration import copy_run
    store = store_for(tmp_path, "classic")
    store.mutate(RUN_ID, lambda row: row["node_states"].setdefault("work", {}).update(status="completed", text="legacy result",
        command_receipts=[{"status": "outcome_unknown", "command_started": True, "request": {"command": "publish result"}}]), durable=True)
    store.mutate(RUN_ID, lambda row: row.update(authorizations={'version': 1, 'entries': [
        {'id': 'old-active-grant', 'enforceable': True, 'revoked_at': None}]}), durable=True)
    original = store.load(RUN_ID)
    copied = copy_run(store, original, flow=flow(), workspace=tmp_path, request_id="copy-one")
    assert copied["run_id"] != RUN_ID and copied["status"] == "paused"
    assert store.load(RUN_ID) == original
    assert copied["orchestration_mode"] == "collaborative"
    assert not copied["collaboration"]["certificates"] and copied["collaboration"]["blocked"]
    assert copied["collaboration"]["imported_deliveries"]["work"]["text"] == "legacy result"
    assert copied["collaboration_import"]["model_calls"] == 0
    assert not (copied.get('authorizations') or {}).get('entries')
    assert copied['authorization_manifest']['approved'] is False
    assert copy_run(store, original, flow=flow(), workspace=tmp_path, request_id="copy-one")["run_id"] == copied["run_id"]


def test_resume_known_effects_requires_explicit_new_host_attempt():
    value = core()
    first = ledger.dispatch(value, "work")
    record = {"run_id": RUN_ID, "attempt": 1, "collaboration": value}
    assert ledger.resume_dispatch(record, "work") is None
    record["attempt"] = 2
    resumed = ledger.resume_dispatch(record, "work")
    assert resumed["id"] == first["id"] and resumed["run_attempt"] == 2
    assert ledger.resume_dispatch(record, "work") is None
    record["attempt"] = 3
    record["kernel_calls"] = {"unknown": {"node_id": "work", "state": "unknown"}}
    with pytest.raises(ValueError, match="结果不明"):
        ledger.resume_dispatch(record, "work")


def test_start_identity_binding_is_stable_and_recovers_without_another_run(tmp_path):
    design = TaskDesign(tmp_path, "conversation")
    created = design.start("execute", orchestration_mode="collaborative")
    ready = design.transition(created, "ready", build_request={"state": "saved", "id": "build", "epoch": 1})
    claimed = design.claim_execution(ready)
    run_id = design.execution_run_id(claimed)
    bound = design.bind_run(task_id=claimed["task_id"], claim_id=claimed["build_request"]["execution_claim_id"], run_id=run_id)
    assert bound["run_id"] == run_id and design.execution_run_id(design.read()) == run_id
    with pytest.raises(ValueError, match="不一致"):
        design.bind_run(task_id=claimed["task_id"], claim_id="other", run_id=run_id)
    assert design.read()["run_id"] == run_id


def test_user_can_replace_designated_method_only_through_reviewed_contract(tmp_path):
    raw = flow().as_dict()
    raw["meta"]["collaboration_spec"]["document"]["entries"].append({"id": "work", "kind": "node", "body": "work",
        "state": "active", "sources": [], "data": {"contract": {"method": {"id": "original", "description": "Original method"}}}})
    store = ExpertRunStore(tmp_path / "runs", storage="sqlite")
    record = store.create(run_id=RUN_ID, conversation_id="conversation", task="goal", attachments=[], profile=PROFILE.as_dict(),
        strategy="stable", flow=ExpertFlowSpec.from_value(raw))
    revised = user_revision(store, record, entry_id="work", body="Approved new method", field="method", expected_version=1, request_id="method-choice")
    value = revised["collaboration"]
    assert value["contracts"]["work"]["method"]["id"] == "original" and value["contracts"]["work"]["held"]
    proposal_id = next(iter(value["proposals"]))
    store.mutate(RUN_ID, lambda row: ledger.adopt(row["collaboration"], proposal_id, review={"passed": True, "evidence": ["checked impact"]},
        proposer_vendor="a", reviewer_vendor="b"), durable=True)
    contract = store.load(RUN_ID)["collaboration"]["contracts"]["work"]
    assert contract["method"]["description"] == "Approved new method" and contract["method_decision"] == proposal_id


def test_user_revision_adopts_only_changed_fields_and_retry_after_acceptance_is_idempotent(tmp_path):
    store = store_for(tmp_path)
    store.mutate(RUN_ID, lambda record: record["collaboration"]["entries"]["goal"].update(origin="model"), durable=True)
    record = store.load(RUN_ID)
    payload = dict(entry_id="goal", body="用户明确指定的新目标", expected_version=1, request_id="adopt-model-field")
    revised = user_revision(store, record, **payload)
    identity = next(iter(revised["collaboration"]["proposals"]))
    store.mutate(RUN_ID, lambda row: ledger.adopt(row["collaboration"], identity,
        review={"passed": True, "evidence": ["matches user source"]}, proposer_vendor="a", reviewer_vendor="b"), durable=True)
    store.mutate(RUN_ID, lambda record: record.update(status="completed"), durable=True)
    again = user_revision(store, record, **payload)
    value = again["collaboration"]
    assert again["status"] == "completed" and value["spec_version"] == 2 and len(value["proposals"]) == 1
    entry = value["entries"]["goal"]
    assert entry["origin"] == "model" and entry["adopted_fields"] == ["body"]
    assert entry["field_sources"]["body"][0]["source_id"] == "user_revision:adopt-model-field"
    with pytest.raises(ValueError, match="用户明确决定"):
        ledger.proposal(value, proposal_id="silently-change-back", changes={"goal": {**entry, "body": "old model guess"}},
            evidence=["model"], actor="coordinator", base_version=2, epoch=1)
    forged = {**entry, "field_sources": {}}
    with pytest.raises(ValueError, match="来源"):
        ledger.proposal(value, proposal_id="forge-source", changes={"goal": forged}, evidence=["model"], actor="coordinator", base_version=2, epoch=1)


@pytest.mark.asyncio
async def test_late_completion_cannot_overwrite_user_revision(tmp_path):
    store = store_for(tmp_path)
    async def runner(*args):
        return delivery()
    async def review(*args):
        return {"passed": True, "evidence": ["verified complete result"]}
    result = await run(orchestrator(store, tmp_path, runner, review), store)
    assert result.status == "completed"
    store.transition(RUN_ID, "completed")
    user_revision(store, store.load(RUN_ID), entry_id="goal", body="新的目标", expected_version=1, request_id="late-revision")
    with pytest.raises(ValueError, match="最终验收"):
        store.transition(RUN_ID, result.status)
    assert store.load(RUN_ID)["status"] == "paused"


@pytest.mark.asyncio
async def test_arbitration_identity_includes_proposal_and_related_versions(tmp_path):
    from mewcode.kernel.arbitration import Arbitration
    store = store_for(tmp_path)
    seen = []
    async def ask(payload, system):
        seen.append(payload["proposal_id"])
        return {"decision": "accept_verifier", "confidence": .95, "reason": "supported by evidence", "evidence_refs": ["e"]}
    arbiter = Arbitration(store, RUN_ID, ask)
    versions = {"coordinator_epoch": 1, "entries": {"goal": 1}, "contracts": {"work": 1}}
    args = ("work", {"claim": "done"}, {"claim": "incomplete"}, [{"id": "e", "content": "observed result"}])
    for identity in ("first", "first", "second"):
        answer = await arbiter.resolve(*args, proposal_id=identity, related_versions=versions)
        assert answer["decision"] == "accept_verifier"
    assert seen == ["first", "second"]
    answer = await arbiter.resolve(*args, proposal_id="permission", related_versions=versions, authority=True)
    assert answer["decision"] == "human" and len(seen) == 2
    store.mutate(RUN_ID, lambda record: record["collaboration"]["entries"]["goal"].update(version=2), durable=True)
    with pytest.raises(ValueError, match="过期"):
        await arbiter.resolve(*args, proposal_id="first", related_versions=versions)


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef", "$recursiveRef"])
def test_schema_checks_cannot_resolve_external_resources(keyword):
    from mewcode.kernel.contracts import checks
    with pytest.raises(ValueError, match="本地引用"):
        checks([{"id": "shape", "kind": "schema", "path": "result.json",
                 "schema": {"properties": {"answer": {keyword: "https://external.invalid/private"}}}}])


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,passed", [(42, True), ("unverified", False)])
async def test_schema_acceptance_checks_fixed_content_not_just_file_existence(tmp_path, answer, passed):
    import json
    from mewcode.kernel.contracts import checks
    (tmp_path / "result.json").write_text(json.dumps({"answer": answer}), encoding="utf-8")
    contract = {"checks": checks([{"id": "shape", "kind": "schema", "path": "result.json", "schema": {
        "type": "object", "required": ["answer"], "properties": {"answer": {"type": "number"}}}}])}
    result = delivery()
    result.handoff["artifacts"] = [{"path": "result.json"}]
    report = await Acceptance(tmp_path).check(contract, result, context={})
    assert report["passed"] is passed and report["artifacts"][0]["snapshot_path"]


@pytest.mark.parametrize("same_entry", [True, False])
def test_multiple_user_revisions_preserve_latest_intent_and_all_pending_holds(tmp_path, same_entry):
    from mewcode.orchestration.collab_service import projection
    raw = flow().as_dict()
    raw["meta"]["collaboration_spec"]["document"]["entries"].append({
        "id": "assumption", "kind": "assumption", "body": "Initial assumption", "data": {}})
    store = ExpertRunStore(tmp_path / "runs", storage="sqlite")
    store.create(run_id=RUN_ID, conversation_id="conversation", task="goal", attachments=[], profile=PROFILE.as_dict(),
                 strategy="stable", flow=ExpertFlowSpec.from_value(raw))
    first = user_revision(store, store.load(RUN_ID), entry_id="goal", body="First user goal", expected_version=1, request_id="first")
    first_id = next(iter(first["collaboration"]["proposals"]))
    second = user_revision(store, first, entry_id="goal" if same_entry else "assumption", body="Latest user decision",
                           expected_version=1, request_id="second")
    second_id = next(key for key in second["collaboration"]["proposals"] if key != first_id)
    def adopt(identity):
        return store.mutate(RUN_ID, lambda record: ledger.adopt(record["collaboration"], identity,
            review={"passed": True, "evidence": ["current user source"]}, proposer_vendor="a", reviewer_vendor="b"), durable=True)
    if same_entry:
        with pytest.raises(ValueError, match="取代"):
            adopt(first_id)
    else:
        partially_adopted = adopt(first_id)
        assert partially_adopted["collaboration"]["contracts"]["work"]["held"]
        with pytest.raises(ValueError, match="待处理修订"):
            ledger.dispatch(partially_adopted["collaboration"], "work")
    adopted = adopt(second_id)
    assert not adopted["collaboration"]["contracts"]["work"].get("held")
    assert projection(adopted)["pending_revisions"] == 0
    assert adopted["collaboration"]["entries"]["goal" if same_entry else "assumption"]["body"] == "Latest user decision"


@pytest.mark.asyncio
async def test_live_revision_resumes_known_effects_under_new_contract(tmp_path):
    store = store_for(tmp_path)
    entered = asyncio.Event()
    calls, effects = [], []
    async def runner(node, model, prompt, policy, member=None):
        calls.append(node.node_id)
        if node.node_id == "work" and calls.count("work") == 1:
            effects.append("already performed")
            store.mutate(RUN_ID, lambda row: row.setdefault("kernel_calls", {}).update({
                "known": {"node_id": "work", "state": "succeeded"}}), durable=True)
            entered.set()
            await asyncio.Future()
        if node.node_id == "work":
            assert policy.resume_messages and "Updated live requirement" in prompt
        return delivery()
    async def review(*args):
        return {"passed": True, "evidence": ["verified updated requirement"]}
    host = orchestrator(store, tmp_path, runner, review)
    running = asyncio.create_task(run(host, store))
    await asyncio.wait_for(entered.wait(), 20)
    revised = user_revision(store, store.load(RUN_ID), entry_id="goal", body="Updated live requirement", expected_version=1, request_id="live-edit")
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    identity = next(iter(revised["collaboration"]["proposals"]))
    def resume(record):
        ledger.adopt(record["collaboration"], identity, review={"passed": True, "evidence": ["verified user decision"]},
                     proposer_vendor="a", reviewer_vendor="b")
        record["attempt"] = 2  # The existing host Resume action advances this.
    store.mutate(RUN_ID, resume, durable=True)
    result = await run(host, store)
    assert result.status == "completed", result.final_summary
    assert effects == ["already performed"] and calls == ["work", "work", "deliver"]
    attempts = list(store.load(RUN_ID)["collaboration"]["attempts"].values())
    assert attempts[0]["status"] == "superseded_closed" and attempts[1]["resumed_from"] == attempts[0]["id"]
