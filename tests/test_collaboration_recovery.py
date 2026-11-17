"""Regression coverage for acceptance deadlocks and infrastructure recovery."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

import pytest

from mewcode.expert_group import ExpertRoleResult
from mewcode.kernel import collaboration as ledger
from mewcode.kernel.collaboration_recovery import resolve_gate, latest_attempt
from mewcode.orchestration.collab_service import user_recovery
from tests.test_collaboration_mode import CATALOG, RUN_ID, core, delivery, store_for as base_store_for, require_reviews
from tests.test_collaboration_integration import orchestrator, run as run_host
from tests.test_task_design_entry import task_api_env  # noqa: F401


def store_for(tmp_path):
    store = base_store_for(tmp_path)
    require_reviews(store, 'work')
    return store


def explicit_recovery(store, *, scope='contract:work', kind='infrastructure'):
    """A user-requested recovery entry, separate from automatic quality gates."""
    from mewcode.kernel.collaboration_recovery import open_gate
    def create(record):
        value = ledger.state(record)
        if any(row['status'] == 'waiting' for row in value.get('recovery_gates', {}).values()):
            return
        keys = ['work'] if scope.startswith('contract:') else list(value['contracts'])
        open_gate(record, scope=scope, keys=keys, label='用户主动恢复', reason='用户选择处理当前已保存的检查点', kind=kind)
    store.mutate(RUN_ID, create, durable=True)


async def run(host, store):
    # Match the web host's explicit resume and terminal state persistence.
    if store.load(RUN_ID)["status"] in {"blocked", "paused", "needs_attention"}:
        store.resume(RUN_ID, strategy="stable")
    result = await run_host(host, store)
    store.transition(RUN_ID, result.status, reason=result.final_summary)
    return result


def decide(store, tmp_path, *, action="retry", reason="用户核对了当前交付并决定继续", request_id="decision", model=None):
    explicit_recovery(store)
    record = store.load(RUN_ID)
    gate = next(row for row in record["collaboration"]["recovery_gates"].values() if row["status"] == "waiting")
    return user_recovery(store, record, gate_id=gate["id"], expected_version=gate["spec_version"],
        request_id=request_id, action=action, reason=reason, model=model, work_dir=tmp_path, catalog=CATALOG)


def test_adopted_user_revision_reopens_exhausted_specification():
    value = core()
    for _ in range(3):
        attempt = ledger.dispatch(value, "work")
        ledger.complete(value, attempt["id"], {"passed": False, "checks": [], "artifacts": []})
    with pytest.raises(ledger.AttemptBudgetExhausted):
        ledger.dispatch(value, "work")
    revised = {**value["entries"]["goal"], "body": "用户修订后的具体要求"}
    ledger.proposal(value, proposal_id="user-new", changes={"goal": revised}, evidence=["user-message"],
        actor="user", base_version=value["spec_version"], epoch=value["coordinator_epoch"])
    ledger.adopt(value, "user-new", review={"passed": True, "evidence": ["用户原始修订"]}, proposer_vendor="a", reviewer_vendor="b")
    assert ledger.dispatch(value, "work")["number"] == 4


@pytest.mark.asyncio
async def test_exhaustion_has_durable_user_retry_and_does_not_erase_history(tmp_path):
    store = store_for(tmp_path)
    store.mutate_collaboration(RUN_ID, lambda value: value['policy'].update(lineage_budget_mode='fixed'))
    invocations = []
    async def execute(node, *args):
        invocations.append(node.node_id)
        return delivery(str(len(invocations)))
    async def review(model, payload, context):
        return {"passed": invocations.count("work") > 3, "evidence": ["逐项核对"]}
    host = orchestrator(store, tmp_path, execute, review)
    assert (await run(host, store)).status == "blocked"
    assert invocations == ["work"] * 3
    assert not store.read_collaboration(RUN_ID).get("recovery_gates")
    explicit_recovery(store, kind="attempts_exhausted")
    before = store.load(RUN_ID)["collaboration"]["attempts"]
    gate = next(iter(store.load(RUN_ID)["collaboration"]["recovery_gates"].values()))
    decide(store, tmp_path)
    user_recovery(store, store.load(RUN_ID), gate_id=gate["id"], expected_version=gate["spec_version"],
        request_id="decision", action="retry", reason="用户核对了当前交付并决定继续", work_dir=tmp_path, catalog=CATALOG)
    assert (await run(host, store)).status == "completed"
    current = store.load(RUN_ID)["collaboration"]
    assert set(before) <= set(current["attempts"])
    assert invocations.count("work") == 4 and len(current["human_decisions"]) == 1


@pytest.mark.asyncio
async def test_manual_acceptance_is_user_only_and_keeps_failed_report(tmp_path):
    store = store_for(tmp_path)
    async def review(model, payload, context):
        return {"passed": payload["contract"]["id"] != "work", "evidence": ["自动检查不通过"]}
    host = orchestrator(store, tmp_path, AsyncMock(side_effect=lambda *a: delivery()), review)
    await run(host, store)
    explicit_recovery(store)
    record = store.load(RUN_ID)
    gate = next(iter(record["collaboration"]["recovery_gates"].values()))
    with pytest.raises(ValueError, match="只有用户"):
        resolve_gate(record, gate_id=gate["id"], expected_version=1, request_id="fake", action="accept", reason="模型自述", actor="coordinator")
    decide(store, tmp_path, action="accept")
    assert (await run(host, store)).status == "completed"
    current = store.load(RUN_ID)["collaboration"]
    cert = current["certificates"]["work"]
    assert cert["accepted_by"] == "user" and ledger.valid_certificate(current, "work")
    assert current["attempts"][cert["attempt_id"]]["report"]["passed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["milestone", "final"])
async def test_integration_feedback_repairs_named_units_without_coordinator(tmp_path, scope):
    store = store_for(tmp_path)
    if scope == 'milestone':
        store.mutate_collaboration(RUN_ID, lambda value: next(iter(value['milestones'].values())).update(
            checks=[{'id': 'required-integration', 'version': 1, 'kind': 'review', 'blocking': True}]))
    invocations, rejected = [], []
    async def execute(node, model, prompt, *args):
        invocations.append(node.node_id)
        if node.node_id == 'work' and invocations.count(node.node_id) > 1:
            assert '修正组合结果' in prompt
        return delivery()
    async def review(model, payload, context):
        key = payload['contract']['id']
        reject = key.startswith(scope) and not rejected
        if reject:
            rejected.append(key)
        return {'passed': not reject, 'evidence': ['组合核对'],
                'issues': [{'node_id': 'work', 'description': '修正组合结果', 'resolution': 'producer_fix',
                            'acceptance_test': '组合结果一致'}] if reject else []}
    coordinate = AsyncMock(side_effect=AssertionError('no extra planner for concrete repair'))
    host = orchestrator(store, tmp_path, execute, review, coordinate)
    assert (await run(host, store)).status == 'completed'
    assert invocations == ['work', 'deliver', 'work', 'deliver']
    assert not coordinate.called


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["provider_interrupted", "environment_fault", "safety_review_unavailable"])
async def test_transient_execution_faults_use_host_recovery_without_acceptance_retries(tmp_path, kind):
    store = store_for(tmp_path)
    calls = []
    async def execute(node, model, prompt, policy, member):
        calls.append(node.node_id)
        if node.node_id == "work" and calls.count("work") <= 3:
            return ExpertRoleResult(text="临时服务错误", status="failed", error_kind=kind)
        return delivery()
    host = orchestrator(store, tmp_path, execute, AsyncMock(return_value={"passed": True, "evidence": ["核对完成"]}))
    host.recovery_handler = AsyncMock(return_value={"action": "continue_checkpoint"})
    assert (await run(host, store)).status == "completed"
    assert host.recovery_handler.await_count == 3
    current = store.load(RUN_ID)["collaboration"]
    assert len([row for row in current["attempts"].values() if row["contract_id"] == "work"]) == 1
    assert not ledger.failed_attempts(current, "work")


@pytest.mark.asyncio
async def test_transient_verifier_fault_retries_verification_without_reexecuting_work(tmp_path):
    store = store_for(tmp_path)
    attempts = []
    async def review(model, payload, context):
        if payload["contract"]["id"] == "work":
            attempts.append(context["acceptance_round"])
            if len(attempts) <= 3:
                raise ConnectionError("temporary provider outage")
        return {"passed": True, "evidence": ["核对完成"]}
    execute = AsyncMock(side_effect=lambda *a: delivery())
    host = orchestrator(store, tmp_path, execute, review)
    host.recovery_handler = AsyncMock(return_value={"action": "continue_checkpoint"})
    assert (await run(host, store)).status == "completed"
    assert attempts == [0, 1, 2, 3] and execute.await_count == 2


def exhausted_gate(store, tmp_path, *, file=False):
    from mewcode.kernel.acceptance import Acceptance
    from mewcode.kernel.collaboration_recovery import open_gate
    def create(record):
        value = ledger.state(record)
        result = delivery()
        if file:
            (tmp_path / "result.txt").write_text("original", encoding="utf-8")
            result.handoff = {"status": "completed", "artifacts": [{"path": "result.txt"}]}
        for _ in range(3):
            attempt = ledger.dispatch(value, "work")
            ledger.complete(value, attempt["id"], {"passed": False, "checks": [],
                "artifacts": Acceptance(tmp_path).freeze(result.handoff)})
            value["attempts"][attempt["id"]]["delivery"] = {"status": "completed", "handoff": result.handoff}
        record.update(status="blocked", active_node_ids=[])
        open_gate(record, scope="contract:work", keys=["work"], label="work", reason="未通过", kind="attempts_exhausted")
    store.mutate(RUN_ID, create, durable=True)
    return next(iter(store.load(RUN_ID)["collaboration"]["recovery_gates"].values()))


def test_manual_acceptance_cannot_replace_unavailable_safety_review(tmp_path):
    store = store_for(tmp_path)
    exhausted_gate(store, tmp_path, file=True)
    def outage(record):
        from mewcode.kernel.collaboration_recovery import open_gate
        value = ledger.state(record)
        attempt = latest_attempt(value, "work")
        attempt["report"]["execution_error"] = "provider_error"
        attempt["report"]["checks"] = [{"evidence": {"execution_error": "safety_review_unavailable"}}]
        for gate in value["recovery_gates"].values():
            gate["status"] = "superseded"
        open_gate(record, scope="contract:work", keys=["work"], label="work",
                  reason="审查服务暂不可用", kind="infrastructure")
    store.mutate(RUN_ID, outage, durable=True)
    with pytest.raises(ValueError, match="人工验收不能替代"):
        decide(store, tmp_path, action="accept")
    assert not store.load(RUN_ID)["collaboration"].get("human_decisions")


@pytest.mark.parametrize("problem", ["stale", "unknown", "tampered", "running"])
def test_manual_acceptance_rejects_changed_or_unsettled_delivery(tmp_path, problem):
    store = store_for(tmp_path)
    exhausted_gate(store, tmp_path, file=True)
    if problem == "tampered":
        (tmp_path / "result.txt").write_text("changed", encoding="utf-8")
    else:
        def change(record):
            if problem == "stale":
                record["collaboration"]["spec_version"] += 1
            elif problem == "unknown":
                record["kernel_calls"] = {"unsettled": {"node_id": "work__acceptance", "state": "unknown"}}
            else:
                record.update(status="running")
        store.mutate(RUN_ID, change, durable=True)
    with pytest.raises(ValueError):
        decide(store, tmp_path, action="accept")
    assert not store.load(RUN_ID)["collaboration"].get("human_decisions")


@pytest.mark.asyncio
async def test_repeated_final_feedback_keeps_draft_and_stops_automatic_repair(tmp_path):
    store = store_for(tmp_path)
    execute = AsyncMock(side_effect=lambda *a: delivery())
    async def review(model, payload, context):
        reject = payload['contract']['id'] == 'final'
        return {'passed': not reject, 'evidence': ['组合结果不一致'],
                'issues': [{'node_id': 'work', 'description': '修复组合一致性', 'resolution': 'producer_fix',
                            'acceptance_test': '组合结果一致'}] if reject else []}
    coordinate = AsyncMock(side_effect=AssertionError('no extra planner'))
    host = orchestrator(store, tmp_path, execute, review, coordinate)
    result = await run(host, store)
    assert result.status == 'completed' and '初稿已完成' in result.final_summary
    assert execute.await_count == 4 and not coordinate.called
    assert not store.read_collaboration(RUN_ID).get('recovery_gates')
    assert (await run(host, store)).status == 'completed'
    assert execute.await_count == 4 and not coordinate.called
    assert store.read_collaboration(RUN_ID)['integrations']['final']['report']['passed'] is False


@pytest.mark.asyncio
async def test_saved_final_findings_survive_restart_without_repeating_work(tmp_path):
    store = store_for(tmp_path)
    execute = AsyncMock(side_effect=lambda *a: delivery())
    async def review(model, payload, context):
        return {'passed': payload['contract']['id'] != 'final', 'evidence': ['集成核对仍有待修订项']}
    host = orchestrator(store, tmp_path, execute, review)
    assert (await run(host, store)).status == 'completed'
    original = copy.deepcopy(store.read_collaboration(RUN_ID)['integrations']['final']['report'])
    assert (await run(host, store)).status == 'completed'
    assert execute.await_count == 2
    assert store.read_collaboration(RUN_ID)['integrations']['final']['report'] == original


@pytest.mark.asyncio
async def test_unavailable_verifier_requires_repair_instead_of_manual_acceptance(tmp_path):
    store = store_for(tmp_path)
    unavailable = True
    async def review(model, payload, context):
        if payload["contract"]["id"] == "work" and unavailable:
            raise ConnectionError("unavailable")
        return {"passed": True, "evidence": ["checked"]}
    execute = AsyncMock(side_effect=lambda *a: delivery())
    host = orchestrator(store, tmp_path, execute, review)
    assert (await run(host, store)).status == "blocked"
    from mewcode.orchestration.collab_service import projection
    assert not projection(store.load(RUN_ID))["recovery_gates"][0]["manual_available"]
    with pytest.raises(ValueError, match="人工验收不能替代模型服务恢复"):
        decide(store, tmp_path, action="accept")
    unavailable = False
    decide(store, tmp_path, action="retry")
    assert (await run(host, store)).status == "completed" and execute.await_count == 2


@pytest.mark.asyncio
async def test_recovery_cannot_reinvoke_a_superseded_contract(tmp_path):
    store = store_for(tmp_path)
    execute = AsyncMock(return_value=ExpertRoleResult(text="provider outage", status="failed", error_kind="provider_error"))
    host = orchestrator(store, tmp_path, execute, AsyncMock())
    async def recover(**kwargs):
        store.mutate(RUN_ID, lambda record: ledger.invalidate(record["collaboration"], ["work"], "用户修订"), durable=True)
        return {"action": "continue_checkpoint"}
    host.recovery_handler = recover
    assert (await run(host, store)).status == "blocked" and execute.await_count == 1


@pytest.mark.asyncio
async def test_real_kernel_ladder_preserves_authorized_model_and_contract(tmp_path):
    from mewcode.kernel.escalation import KernelScheduler
    store = store_for(tmp_path)
    execute = AsyncMock(return_value=ExpertRoleResult(text="temporary outage", status="failed", error_kind="provider_error"))
    host = orchestrator(store, tmp_path, execute, AsyncMock())
    registry = SimpleNamespace(catalog=AsyncMock(return_value=CATALOG))
    repair = AsyncMock()
    host.recovery_handler = KernelScheduler(session=SimpleNamespace(work_dir=str(tmp_path)), registry=registry,
        store=store, run_id=RUN_ID, emit=AsyncMock(), before_resume=AsyncMock(), task="goal", primary_repair=repair)
    before = copy.deepcopy(store.load(RUN_ID)["collaboration"]["contracts"])
    assert (await run(host, store)).status == "blocked"
    current = store.load(RUN_ID)
    assert execute.await_count == 3 and not registry.catalog.called and not repair.called
    assert not ledger.failed_attempts(current["collaboration"], "work")
    assert current["collaboration"]["contracts"]["work"]["version"] == before["work"]["version"]
    gates = [row for row in current["collaboration"]["recovery_gates"].values() if row['status'] == 'waiting']
    assert len(gates) == 1 and gates[0]['kind'] == 'infrastructure'


@pytest.mark.parametrize("receipt,expected", [
    ({"status": "spawn_failed"}, "environment_fault"), ({"status": "timed_out"}, "effect_unknown"),
    ({"status": "outcome_unknown"}, "effect_unknown"), ({"status": "denied"}, "permission_denied"),
    ({"status": "denied", "execution_condition": {"kind": "environment_unavailable"}}, "environment_fault"),
    ({"status": "completed", "exit_code": 1}, ""),
])
def test_command_faults_follow_host_receipts(receipt, expected):
    from mewcode.kernel.collaboration_faults import command_fault
    assert command_fault({"command_execution": receipt}) == expected


@pytest.mark.asyncio
async def test_recovery_api_binds_user_gate_and_rechecks_after_catalog_wait(task_api_env, tmp_path, monkeypatch):
    from frontend import server
    from mewcode.orchestration.task_design import TaskDesign
    _, _, _, owner, _ = task_api_env
    store = store_for(tmp_path)
    gate = exhausted_gate(store, tmp_path)
    monkeypatch.setattr(server, "EXPERT_RUNS", store)
    design = TaskDesign(owner["project"], owner["conversation_id"])
    task = design.start("execute", orchestration_mode="collaborative")
    design.transition(task, "blocked", run_id=RUN_ID)
    store.mutate(RUN_ID, lambda record: (record.update(conversation_id=owner["conversation_id"]),
        record["collaboration"].update(task_id=task["task_id"])), durable=True)
    catalog_started, catalog_release = asyncio.Event(), asyncio.Event()
    async def catalog(**kw):
        catalog_started.set()
        await catalog_release.wait()
        return CATALOG
    monkeypatch.setattr(server.MODEL_REGISTRY, "catalog", catalog)
    payload = {**owner, "expected_task_id": task["task_id"], "expected_version": 1, "gate_id": gate["id"],
        "request_id": "user-decision", "action": "retry", "reason": "用户明确要求再试"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        for invalid in ({"expected_task_id": "foreign"}, {"conversation_id": "foreign"}, {"expected_version": 0}):
            assert (await client.post("/api/orchestration/task-design/recovery", json={**payload, **invalid})).status_code == 409
        pending = asyncio.create_task(client.post("/api/orchestration/task-design/recovery", json={**payload,
            "action": "switch_model", "model": CATALOG["models"][0]}))
        await asyncio.wait_for(catalog_started.wait(), 3)
        store.mutate(RUN_ID, lambda record: record.update(status="running"), durable=True)
        catalog_release.set()
        assert (await pending).status_code == 409
        store.mutate(RUN_ID, lambda record: record.update(status="blocked"), durable=True)
        response = await client.post("/api/orchestration/task-design/recovery", json=payload)
        assert response.status_code == 200, response.text
        assert (await client.post("/api/orchestration/task-design/recovery", json=payload)).status_code == 200
        assert (await client.post("/api/orchestration/task-design/recovery", json={**payload, "action": "accept"})).status_code == 409
    current = store.load(RUN_ID)["collaboration"]
    assert len(current["human_decisions"]) == 1 and not ledger.retry_exhausted(current, "work")


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["work", "final"])
async def test_acceptance_user_wait_resumes_same_call_only_on_explicit_host_resume(tmp_path, scope):
    from mewcode.experts.waits import BranchWaitPaused
    store = store_for(tmp_path)
    waits = []
    async def review(model, payload, context):
        if payload["contract"]["id"] == scope:
            waits.append((context["acceptance_round"], context["resume_acceptance"]))
            if len(waits) == 1:
                raise BranchWaitPaused(scope + "__acceptance", "original-question", reason="等待原问题回答")
        return {"passed": True, "evidence": ["复用原问题答案后核对"]}
    execute = AsyncMock(side_effect=lambda *a: delivery())
    host = orchestrator(store, tmp_path, execute, review)
    host.recovery_handler = AsyncMock(return_value={"action": "continue_checkpoint"})
    assert (await run(host, store)).status == "blocked"
    assert host.recovery_handler.await_count == 0 and waits == [(0, False)]
    # Merely evaluating the same host attempt cannot ask again.
    assert (await run_host(host, store)).status == "blocked" and waits == [(0, False)]
    store.transition(RUN_ID, "blocked")
    decide(store, tmp_path, action="retry")
    assert (await run(host, store)).status == "completed"
    assert waits == [(0, False), (0, True)] and execute.await_count == 2
    assert not ledger.failed_attempts(store.load(RUN_ID)["collaboration"], "work")


@pytest.mark.asyncio
async def test_acceptance_wait_stops_remaining_commands_and_cannot_be_manually_bypassed(tmp_path):
    from mewcode.experts.waits import BranchWaitPaused
    from mewcode.kernel.acceptance import Acceptance
    command = AsyncMock(side_effect=BranchWaitPaused("work", "permission-one", reason="等待原授权"))
    check = {"id": "permission-check", "version": 1, "kind": "command", "command": "write-output"}
    report = await Acceptance(tmp_path, command=command).check({"checks": [check, {**check, "id": "later-command"}]}, delivery(), context={})
    assert report["execution_error"] == "user_input" and command.await_count == 1


@pytest.mark.asyncio
async def test_unknown_acceptance_helper_call_is_not_replayed(tmp_path):
    store = store_for(tmp_path)
    async def review(model, payload, context):
        store.mutate(RUN_ID, lambda record: record.setdefault("kernel_calls", {}).__setitem__("unknown", {
            "node_id": "work__acceptance_helper", "state": "unknown"}), durable=True)
        return {"passed": False, "evidence": [], "execution_error": "effect_unknown"}
    execute = AsyncMock(side_effect=lambda *a: delivery())
    reviewer = AsyncMock(side_effect=review)
    host = orchestrator(store, tmp_path, execute, reviewer)
    host.recovery_handler = AsyncMock(return_value={"action": "continue_checkpoint"})
    assert (await run(host, store)).status == "blocked"
    assert (await run(host, store)).status == "blocked"
    assert execute.await_count == 1 and reviewer.await_count == 1 and not host.recovery_handler.called
    with pytest.raises(ValueError, match="结果不明"):
        decide(store, tmp_path, action="retry")


def test_user_model_change_is_explicit_available_and_independent(tmp_path):
    from tests.test_collaboration_mode import EXECUTOR, REVIEWER
    store = store_for(tmp_path)
    gate = exhausted_gate(store, tmp_path)
    before = copy.deepcopy(store.load(RUN_ID).get("locked_models"))
    with pytest.raises(ValueError, match="不同厂商"):
        decide(store, tmp_path, action="switch_model", model=REVIEWER.as_dict())
    assert store.load(RUN_ID).get("locked_models") == before
    with pytest.raises(ValueError):
        decide(store, tmp_path, action="switch_model", model={"provider_key": "missing", "model_id": "invented"})
    chosen = {**EXECUTOR.as_dict(), "model_id": "alternate-authorized"}
    catalog = {"models": CATALOG["models"] + [{**chosen, "available": True, "tools": True, "provider_brand": "brand-a"}]}
    updated = user_recovery(store, store.load(RUN_ID), gate_id=gate["id"], expected_version=1,
        request_id="model-choice", action="switch_model", reason="用户选择另一执行模型", model=chosen, catalog=catalog, work_dir=tmp_path)
    assert updated["locked_models"]["work"] == chosen
    assert updated["collaboration_model_changes"][-1]["source"] == "user"
    assert not ledger.retry_exhausted(updated["collaboration"], "work")


@pytest.mark.asyncio
async def test_required_clearance_dominates_transient_check_and_manual_gate(tmp_path):
    from mewcode.kernel.acceptance import Acceptance
    from mewcode.kernel.collaboration_recovery import open_gate
    from mewcode.orchestration.collab_service import projection
    review = AsyncMock(side_effect=[{"passed": False, "evidence": [], "execution_error": "provider_error"},
        {"passed": False, "evidence": [], "execution_error": "permission_denied"}])
    check = {"id": "service", "version": 1, "kind": "review"}
    report = await Acceptance(tmp_path, review=review).check({"checks": [check, {**check, "id": "permission"}]}, delivery(), context={})
    assert report["execution_error"] == "permission_denied"
    # A report written by an older version may still have the wrong top-level classification.
    report["execution_error"] = "provider_error"
    store = store_for(tmp_path)
    def seed(record):
        value = ledger.state(record)
        attempt = ledger.dispatch(value, "work")
        value["attempts"][attempt["id"]].update(status="checking", delivery={"status": "completed"}, acceptance_fault=report)
        record.update(status="blocked")
        open_gate(record, scope="contract:work", keys=["work"], label="work", reason="needs clearance", kind="infrastructure")
    store.mutate(RUN_ID, seed, durable=True)
    assert projection(store.load(RUN_ID))["recovery_gates"][0]["manual_available"] is False
    with pytest.raises(ValueError, match="人工验收不能"):
        decide(store, tmp_path, action="accept")


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["work", "milestone"])
async def test_command_permission_wait_is_not_reissued_in_same_host_attempt(tmp_path, scope):
    from mewcode.experts.waits import BranchWaitPaused
    store = store_for(tmp_path)
    check = {"id": "command-check", "version": 1, "kind": "command", "command": "synthetic-check"}
    def configure(record):
        value = ledger.state(record)
        target = value["contracts"]["work"] if scope == "work" else next(iter(value["milestones"].values()))
        target["checks"] = [check]
    store.mutate(RUN_ID, configure, durable=True)
    calls = []
    async def command(check, context):
        calls.append(context["acceptance_round"])
        if len(calls) == 1:
            raise BranchWaitPaused("work", "original-permission", reason="等待原授权")
        return {"exit_code": 0, "receipt_id": "host-test-receipt", "command_started": True}
    execute = AsyncMock(side_effect=lambda *a: delivery())
    host = orchestrator(store, tmp_path, execute, AsyncMock(return_value={"passed": True, "evidence": ["checked"]}))
    host.collaboration_command = command
    assert (await run(host, store)).status == "blocked"
    assert (await run_host(host, store)).status == "blocked" and calls == [0]
    store.transition(RUN_ID, "blocked")
    decide(store, tmp_path, action="retry")
    assert (await run(host, store)).status == "completed"
    assert calls == [0, 0] and execute.await_count == 2


@pytest.mark.asyncio
async def test_switch_model_reexecutes_work_instead_of_reviewing_old_delivery(tmp_path):
    from tests.test_collaboration_mode import EXECUTOR, PROFILE, flow
    store = store_for(tmp_path)
    invocations = []
    async def execute(node, model, *args):
        invocations.append((node.node_id, model.model_id))
        return delivery()
    async def review(model, payload, context):
        if payload["contract"]["id"] == "work" and len(invocations) == 1:
            raise ConnectionError("verifier unavailable after the old executor finished")
        return {"passed": True, "evidence": ["checked"]}
    host = orchestrator(store, tmp_path, execute, review)
    assert (await run(host, store)).status == "blocked"
    explicit_recovery(store)
    gate = next(iter(store.load(RUN_ID)["collaboration"]["recovery_gates"].values()))
    chosen = {**EXECUTOR.as_dict(), "model_id": "alternate-authorized"}
    catalog = {"models": CATALOG["models"] + [{**chosen, "available": True, "tools": True, "provider_brand": "brand-a"}]}
    user_recovery(store, store.load(RUN_ID), gate_id=gate["id"], expected_version=1, request_id="model-retry",
        action="switch_model", reason="用户要求换执行模型重新完成", model=chosen, catalog=catalog, work_dir=tmp_path)
    store.resume(RUN_ID, strategy="stable")
    result = await host.run("goal", PROFILE, catalog, strategy="stable", run_id=RUN_ID,
        flow=flow(), checkpoint=store.load(RUN_ID))
    assert result.status == "completed"
    assert invocations == [("work", EXECUTOR.model_id), ("work", chosen["model_id"]), ("deliver", EXECUTOR.model_id)]


@pytest.mark.asyncio
async def test_switch_verifier_resumes_saved_acceptance_without_reexecuting_work(tmp_path):
    from tests.test_collaboration_mode import PROFILE, flow
    from mewcode.orchestration import inbox, task_view
    store = store_for(tmp_path)
    invocations, reviews = [], []
    chosen = {"provider_key": "other-verification", "model_id": "independent-verifier", "effort": "high"}
    catalog = {"models": CATALOG["models"] + [{**chosen, "available": True, "tools": True,
                "provider_brand": "brand-c", "efforts": ["high"]}]}
    async def execute(node, model, *args):
        invocations.append(node.node_id)
        return delivery()
    async def review(model, payload, context):
        reviews.append((payload["contract"]["id"], model.as_dict()))
        if payload["contract"]["id"] == "work" and model.provider_key != chosen["provider_key"]:
            raise ConnectionError("old verifier service unavailable")
        return {"passed": True, "evidence": ["independent review"]}
    host = orchestrator(store, tmp_path, execute, review)
    assert (await run(host, store)).status == "blocked"
    explicit_recovery(store)
    before = store.load(RUN_ID)
    gate = next(g for g in before["collaboration"]["recovery_gates"].values() if g["status"] == "waiting")
    attempt = latest_attempt(before["collaboration"], "work")
    assert attempt["status"] == "checking"
    failed_work_reviews = sum(key == "work" for key, model in reviews)
    projection = task_view.collaboration_projection(before)
    assert projection["recovery_gates"][0]["verifier_switch_available"] is True
    card = next(row for row in inbox.collect(task_view.from_record(before)["task"], record=before)
                if row["type"] == "collaboration_recovery")
    assert "switch_verifier" in {row["value"] for row in card["input_schema"][0]["options"]}
    def change():
        return user_recovery(store, store.load(RUN_ID), gate_id=gate["id"], expected_version=1,
            request_id="new-verifier", action="switch_verifier", reason="用户选择新的独立验收通道",
            model=chosen, catalog=catalog, work_dir=tmp_path)
    change()
    saved = store.load(RUN_ID)
    assert latest_attempt(saved["collaboration"], "work")["id"] == attempt["id"]
    assert latest_attempt(saved["collaboration"], "work")["delivery"] == attempt["delivery"]
    assert latest_attempt(saved["collaboration"], "work")["status"] == "checking"
    assert saved["collaboration"]["acceptance_model_overrides"] == {"work": chosen}
    assert saved["collaboration"]["lineage_budget"] == before["collaboration"]["lineage_budget"]
    assert saved["role_models"] == before["role_models"]
    assert saved.get("locked_models") == before.get("locked_models")
    change()  # Stable user request identity is idempotent.
    assert store.load(RUN_ID)["collaboration"] == saved["collaboration"]
    store.resume(RUN_ID, strategy="stable")
    result = await host.run("goal", PROFILE, catalog, strategy="stable", run_id=RUN_ID,
        flow=flow(), checkpoint=store.load(RUN_ID))
    assert result.status == "completed"
    assert invocations == ["work", "deliver"]
    work_reviews = [model for key, model in reviews if key == "work"]
    assert work_reviews[-1] == chosen
    assert len(work_reviews) == failed_work_reviews + 1
    assert all(model != chosen for key, model in reviews if key != "work")


@pytest.mark.asyncio
async def test_switch_verifier_rejects_same_vendor_or_unavailable_model_without_mutation(tmp_path):
    from tests.test_collaboration_mode import EXECUTOR
    store = store_for(tmp_path)
    async def review(*args):
        raise ConnectionError("verifier service unavailable")
    await run(orchestrator(store, tmp_path, AsyncMock(side_effect=lambda *args: delivery()), review), store)
    explicit_recovery(store)
    before = store.load(RUN_ID)
    gate = next(g for g in before["collaboration"]["recovery_gates"].values() if g["status"] == "waiting")
    same_brand = {"provider_key": "same-vendor-route", "model_id": "other-model", "effort": ""}
    catalog = {"models": CATALOG["models"] + [{**same_brand, "available": True, "tools": True, "provider_brand": "brand-a"}]}
    for model in (EXECUTOR.as_dict(), same_brand, {**same_brand, "model_id": "missing"}):
        with pytest.raises(ValueError):
            user_recovery(store, store.load(RUN_ID), gate_id=gate["id"], expected_version=1,
                request_id="bad-verifier", action="switch_verifier", reason="明确选择", model=model,
                catalog=catalog, work_dir=tmp_path)
        assert store.load(RUN_ID) == before


@pytest.mark.parametrize("fault", ["permission_required", "effect_unknown", "safety_review_unavailable", ""])
def test_switch_verifier_cannot_clear_host_blocker_or_substantive_failure(fault):
    from mewcode.kernel.collaboration_recovery import open_gate, verifier_switch_attempt
    from mewcode.orchestration.task_view import collaboration_projection
    value = core()
    record = {"run_id": RUN_ID, "status": "blocked", "collaboration": value}
    attempt = ledger.dispatch(value, "work")
    value["attempts"][attempt["id"]].update(status="checking", delivery={"status": "completed"},
        acceptance_fault={"execution_error": fault})
    gate = open_gate(record, scope="contract:work", keys=["work"], label="work", reason="待核对", kind="infrastructure")
    assert verifier_switch_attempt(value, gate) is None
    assert collaboration_projection(record)["recovery_gates"][0]["verifier_switch_available"] is False
    before = copy.deepcopy(record)
    with pytest.raises(ValueError, match="已完成交付"):
        resolve_gate(record, gate_id=gate["id"], expected_version=1, request_id="no-bypass",
            action="switch_verifier", reason="明确选择", actor="user",
            model={"provider_key": "other", "model_id": "verifier", "effort": "high"})
    assert record == before


@pytest.mark.asyncio
async def test_switch_verifier_workbench_api_changes_only_current_acceptance(task_api_env, tmp_path, monkeypatch):
    from frontend import server
    from mewcode.orchestration.task_design import TaskDesign
    _, _, _, owner, _ = task_api_env
    store = store_for(tmp_path / "run")
    async def review(*args):
        raise ConnectionError("verifier service unavailable")
    await run(orchestrator(store, tmp_path, AsyncMock(side_effect=lambda *args: delivery()), review), store)
    explicit_recovery(store)
    task = TaskDesign(owner["project"], owner["conversation_id"]).start("execute", orchestration_mode="collaborative")
    TaskDesign(owner["project"], owner["conversation_id"]).transition(task, "blocked", run_id=RUN_ID)
    store.mutate(RUN_ID, lambda row: (row.update(conversation_id=owner["conversation_id"]),
        row["collaboration"].update(task_id=task["task_id"])), durable=True)
    chosen = {"provider_key": "other-verification", "model_id": "independent-verifier", "effort": "high"}
    catalog = {"models": CATALOG["models"] + [{**chosen, "available": True, "tools": True,
        "provider_brand": "brand-c", "efforts": ["high"]}]}
    monkeypatch.setattr(server, "EXPERT_RUNS", store)
    monkeypatch.setattr(server.MODEL_REGISTRY, "catalog", AsyncMock(return_value=catalog))
    before = store.load(RUN_ID)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        params = {**owner, "run_id": RUN_ID}
        card = next(row for row in (await client.get("/api/workbench/view", params=params)).json()["inbox"]
                    if row["type"] == "collaboration_recovery")
        assert "switch_verifier" in {row["value"] for row in card["input_schema"][0]["options"]}
        response = await client.post("/api/workbench/inbox/" + card["id"] + "/answer",
            headers={"X-Mew-Workbench": "1"}, json={**params, "action": "switch_verifier", "model": chosen,
            "reason": "用户指定新的独立验收通道"})
        assert response.status_code == 200, response.text
    after = store.load(RUN_ID)
    assert after["role_models"] == before["role_models"]
    assert after.get("locked_models") == before.get("locked_models")
    assert after["collaboration"]["acceptance_model_overrides"] == {"work": chosen}
    assert latest_attempt(after["collaboration"], "work")["delivery"] == latest_attempt(before["collaboration"], "work")["delivery"]


@pytest.mark.asyncio
async def test_new_host_receipts_recheck_original_delivery_without_executing_it(tmp_path):
    from tests.test_execution_evidence import recorded_delivery
    from tests.test_collaboration_mode import PROFILE, flow
    from mewcode.kernel.collaboration_recovery import execution_recheck_gates
    record, old_attempt = recorded_delivery()
    store = store_for(tmp_path)
    store.mutate(RUN_ID, lambda saved: saved.update(collaboration=record["collaboration"],
        node_states=record["node_states"], status="paused", active_node_ids=[]), durable=True)
    before = store.load(RUN_ID)
    gates = execution_recheck_gates(before)
    assert len(gates) == 1
    user_recovery(store, before, gate_id=gates[0]["id"], expected_version=1, request_id="host-receipts",
        action="recheck_execution_evidence", reason="补齐宿主回执，仅复核原交付", catalog={}, work_dir=tmp_path)
    saved = store.load(RUN_ID)
    pending = saved["collaboration"]["attempts"][old_attempt["id"]]
    assert pending["status"] == "checking" and pending["delivery"] == old_attempt["delivery"]
    assert pending["report"] == old_attempt["report"]
    assert pending["acceptance_history"] == [old_attempt["report"]]
    assert saved["collaboration"]["lineage_budget"] == before["collaboration"]["lineage_budget"]
    assert saved["role_models"] == before["role_models"]
    invocations, observed = [], []
    async def execute(node, *args):
        invocations.append(node.node_id)
        return delivery()
    async def review(model, payload, context):
        if payload["contract"]["id"] == "work":
            observed.append(payload["host_execution_evidence"])
        return {"passed": True, "evidence": ["checked real host receipts"]}
    store.resume(RUN_ID, strategy="stable")
    result = await orchestrator(store, tmp_path, execute, review).run("goal", PROFILE, CATALOG,
        strategy="stable", run_id=RUN_ID, flow=flow(), checkpoint=store.load(RUN_ID))
    assert result.status == "completed"
    assert invocations == ["deliver"]
    assert observed[0]["complete"] is True
    assert observed[0]["tools"][0]["name"] == "Glob"
    assert execution_recheck_gates(store.load(RUN_ID)) == []


def test_host_receipt_recheck_refuses_stale_partial_or_already_reviewed_evidence(tmp_path):
    from tests.test_execution_evidence import recorded_delivery
    from mewcode.kernel.execution_evidence import execution_evidence
    from mewcode.kernel.collaboration_recovery import execution_recheck_gates
    record, attempt = recorded_delivery()
    assert execution_recheck_gates(record)
    record["node_states"]["work"]["tools"]["receipt"]["status"] = "running"
    assert execution_recheck_gates(record) == []
    record["node_states"]["work"]["tools"]["receipt"]["status"] = "completed"
    attempt["acceptance_input_evidence_hash"] = execution_evidence(record, attempt)["fingerprint"]
    assert execution_recheck_gates(record) == []


def test_host_receipt_recheck_rejects_stale_offer_and_host_clearance_without_mutation(tmp_path):
    from tests.test_execution_evidence import recorded_delivery
    from mewcode.kernel.collaboration_recovery import execution_recheck_gates
    record, attempt = recorded_delivery()
    store = store_for(tmp_path)
    store.mutate(RUN_ID, lambda saved: saved.update(collaboration=record["collaboration"],
        node_states=record["node_states"], status="paused", active_node_ids=[]), durable=True)
    gate = execution_recheck_gates(store.load(RUN_ID))[0]
    store.mutate(RUN_ID, lambda saved: saved["node_states"]["work"]["tools"]["receipt"].update(output="new observation"), durable=True)
    before = store.load(RUN_ID)
    with pytest.raises(ValueError):
        user_recovery(store, before, gate_id=gate["id"], expected_version=1, request_id="stale-evidence",
            action="recheck_execution_evidence", reason="复核", catalog={}, work_dir=tmp_path)
    assert store.load(RUN_ID) == before
    attempt["report"]["execution_error"] = "permission_required"
    assert execution_recheck_gates(record) == []


def test_host_receipt_recheck_rejects_changed_fixed_artifact_without_mutation(tmp_path):
    from tests.test_execution_evidence import recorded_delivery
    from mewcode.kernel.acceptance import Acceptance
    from mewcode.kernel.collaboration_recovery import execution_recheck_gates
    record, attempt = recorded_delivery()
    artifact = tmp_path / "result.txt"
    artifact.write_text("fixed original", encoding="utf-8")
    handoff = {"status": "completed", "artifacts": [{"path": artifact.name}]}
    attempt["delivery"]["handoff"] = copy.deepcopy(handoff)
    record["node_states"]["work"]["handoff"] = copy.deepcopy(handoff)
    attempt["report"]["artifacts"] = Acceptance(tmp_path).freeze(handoff)
    store = store_for(tmp_path)
    store.mutate(RUN_ID, lambda saved: saved.update(collaboration=record["collaboration"],
        node_states=record["node_states"], status="paused", active_node_ids=[]), durable=True)
    gate = execution_recheck_gates(store.load(RUN_ID))[0]
    artifact.write_text("changed bytes", encoding="utf-8")
    before = store.load(RUN_ID)
    with pytest.raises(ValueError, match="原交付产物已经变化"):
        user_recovery(store, before, gate_id=gate["id"], expected_version=1, request_id="changed-artifact",
            action="recheck_execution_evidence", reason="复核", catalog={}, work_dir=tmp_path)
    assert store.load(RUN_ID) == before


@pytest.mark.asyncio
async def test_host_receipt_recheck_workbench_api_preserves_failed_report(task_api_env, tmp_path, monkeypatch):
    from frontend import server
    from mewcode.orchestration.task_design import TaskDesign
    from tests.test_execution_evidence import recorded_delivery
    _, _, _, owner, _ = task_api_env
    record, attempt = recorded_delivery()
    store = store_for(tmp_path / "run")
    task = TaskDesign(owner["project"], owner["conversation_id"]).start("execute", orchestration_mode="collaborative")
    TaskDesign(owner["project"], owner["conversation_id"]).transition(task, "paused", run_id=RUN_ID)
    record["collaboration"]["task_id"] = task["task_id"]
    store.mutate(RUN_ID, lambda saved: saved.update(collaboration=record["collaboration"], node_states=record["node_states"],
        status="paused", active_node_ids=[], conversation_id=owner["conversation_id"]), durable=True)
    monkeypatch.setattr(server, "EXPERT_RUNS", store)
    monkeypatch.setattr(server.MODEL_REGISTRY, "catalog", AsyncMock(return_value=CATALOG))
    before = store.load(RUN_ID)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        params = {**owner, "run_id": RUN_ID}
        card = next(row for row in (await client.get("/api/workbench/view", params=params)).json()["inbox"]
                    if row["type"] == "collaboration_recovery")
        assert [row["value"] for row in card["input_schema"][0]["options"]] == ["recheck_execution_evidence", "revise"]
        response = await client.post("/api/workbench/inbox/" + card["id"] + "/answer",
            headers={"X-Mew-Workbench": "1"}, json={**params, "action": "recheck_execution_evidence",
            "reason": "补齐宿主回执，复用原交付重新验收"})
        assert response.status_code == 200, response.text
        stale = await client.post("/api/workbench/inbox/" + card["id"] + "/answer",
            headers={"X-Mew-Workbench": "1"}, json={**params, "action": "recheck_execution_evidence", "reason": "再验"})
        assert stale.status_code == 409
    saved = store.load(RUN_ID)
    current = saved["collaboration"]["attempts"][attempt["id"]]
    assert current["status"] == "checking" and current["delivery"] == attempt["delivery"]
    assert current["report"] == attempt["report"] and current["acceptance_history"] == [attempt["report"]]
    assert saved["collaboration"]["lineage_budget"] == before["collaboration"]["lineage_budget"]
