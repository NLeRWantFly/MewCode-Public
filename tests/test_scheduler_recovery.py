import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.expert_group import ExpertExecutionPolicy, ExpertRoleResult
from mewcode.expert_runs import ExpertRunStore, serialize_messages
from mewcode.expert_runs import expert_flow_fingerprint
from mewcode.model_registry import ModelRef
from mewcode.conversation import Message
from mewcode.experts import recovery_decision
from mewcode.experts.iteration_recovery import PAUSE
from mewcode.experts.waits import BranchWaitPaused
from mewcode.kernel.escalation import KernelScheduler as SchedulerRecovery
from tests.test_recovery_decision import Chat, Jev
from tests.test_team_loop_control import chain_flow, execute, report


@pytest.mark.asyncio
async def test_model_stream_fault_switches_only_declared_backup_after_bounded_retry(tmp_path, monkeypatch):
    handler, flow, _, store, _, _, _ = controller(tmp_path, monkeypatch)
    handler.model_decisions = False
    primary, backup = ModelRef("p", "primary"), ModelRef("p", "backup")
    handler.registry = SimpleNamespace(resolve=lambda ref, **kwargs: SimpleNamespace(
        capabilities=("chat", "tools", "vision")))
    node = replace(flow.node_map["extract"], backup_models=(backup,))

    def seed(value):
        frozen = value["locked_flow"]
        for row in frozen["nodes"]:
            if row["node_id"] == "extract":
                row["backup_models"] = [backup.as_dict()]
        value["flow_fingerprint"] = expert_flow_fingerprint(frozen)
        value["locked_models"] = {"extract": primary.as_dict()}
        value["node_states"]["extract"].update(input_revision="input-v1",
            started_at=1.0, completed_at=2.0)
    store.mutate(handler.run_id, seed, durable=True)
    request = dict(node=node, result=ExpertRoleResult(text="stream stalled",
        status="budget_exhausted", error_kind="provider_interrupted"),
        policy=ExpertExecutionPolicy("stable", False, 0),
        allowed=["continue_checkpoint", "diagnose_path"],
        host_facts={"trigger": "provider_interrupted"}, account=AsyncMock())
    _reserve_same_method_retry(store, handler.run_id)
    first = await handler(**request)
    assert first["action"] == "switch_declared_route"
    saved = store.load(handler.run_id)
    assert saved["locked_models"]["extract"] == backup.as_dict()
    assert saved["model_route_recoveries"][0]["input_revision"] == "input-v1"


@pytest.mark.asyncio
async def test_stream_fault_uses_bounded_smaller_slice_after_retry_without_backup(tmp_path, monkeypatch):
    handler, flow, _, store, _, _, _ = controller(tmp_path, monkeypatch)
    handler.model_decisions = False
    store.mutate(handler.run_id, lambda value: value["node_states"]["extract"].update(
        input_revision="input-v1", started_at=1.0, completed_at=2.0), durable=True)
    request = dict(node=flow.node_map["extract"],
        result=ExpertRoleResult(text="stream stalled", status="budget_exhausted",
                                error_kind="provider_interrupted"),
        policy=ExpertExecutionPolicy("stable", False, 0),
        allowed=["continue_checkpoint", "diagnose_path", "reduce_execution_profile"],
        host_facts={"trigger": "provider_interrupted"}, account=AsyncMock())
    _reserve_same_method_retry(store, handler.run_id)
    first = await handler(**request)
    assert first["action"] == "diagnose_path"
    second = await handler(**request)
    assert second["controller"] == first["controller"]
    from mewcode.experts.node_ledger import ledger_for_run
    from mewcode.kernel.escalation import action_count
    assert action_count(ledger_for_run(store.load(handler.run_id)).view("extract"), "self_diagnose") == 1


@pytest.mark.asyncio
async def test_reasoning_uncertainty_uses_single_self_diagnosis_before_assistance(tmp_path, monkeypatch):
    handler, flow, _, store, primary, jev, events = controller(tmp_path, monkeypatch)
    _reserve_same_method_retry(store, handler.run_id)
    result = await decide(handler, flow, result=ExpertRoleResult(text="typed judgment uncertain",
        status="needs_attention", error_kind="semantic_uncertain"))
    assert result["action"] == "diagnose_path"
    assert result["controller"]["action"] == "self_diagnose"
    assert not primary.calls and not jev.calls
    assert not any(e["type"] == "ask_user" for e in events)


@pytest.mark.asyncio
async def test_reasoning_uncertainty_switches_only_frozen_higher_effort(tmp_path, monkeypatch):
    handler, flow, _, store, _, _, _ = controller(tmp_path, monkeypatch)
    handler.model_decisions = False
    primary, stronger = ModelRef("p", "same", "low"), ModelRef("p", "same", "high")
    handler.registry = SimpleNamespace(
        resolve=lambda ref: SimpleNamespace(protocol="openai", provider=object(),
            effort=ref.effort, capabilities=("chat", "tools", "vision")),
        _effort_levels=lambda protocol, model, provider: ["low", "medium", "high"])
    node = replace(flow.node_map["extract"], backup_models=(stronger,))
    def seed(value):
        frozen = value["locked_flow"]
        for row in frozen["nodes"]:
            if row["node_id"] == "extract":
                row["backup_models"] = [stronger.as_dict()]
        value["flow_fingerprint"] = expert_flow_fingerprint(frozen)
        value["locked_models"] = {"extract": primary.as_dict()}
        value["node_states"]["extract"].update(input_revision="input-v1",
            started_at=1.0, completed_at=2.0)
    store.mutate(handler.run_id, seed, durable=True)
    _reserve_same_method_retry(store, handler.run_id)
    decision = await handler(node=node,
        result=ExpertRoleResult(text="typed judgment uncertain", status="needs_attention",
                                error_kind="semantic_uncertain"),
        policy=ExpertExecutionPolicy("stable", False, 0),
        allowed=["continue_checkpoint", "reduce_execution_profile", "diagnose_path",
                 "switch_declared_route"],
        host_facts={"trigger": "semantic_uncertain"}, account=AsyncMock())
    assert decision["action"] == "switch_declared_route"
    assert decision["model_switch"]["reason"] == "declared_reasoning_upgrade"
    assert store.load(handler.run_id)["locked_models"]["extract"] == stronger.as_dict()


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["semantic_uncertain", "idle_stall", "capability_missing"])
async def test_user_authorized_difficult_route_switches_without_paid_advice_but_preserves_capability_block(tmp_path, monkeypatch, trigger):
    handler, flow, _, store, primary_client, reviewer, events = controller(tmp_path, monkeypatch)
    current, target = ModelRef("original", "other-model", "high"), ModelRef("chosen", "gpt-6.1-sol", "xhigh")
    handler.registry = SimpleNamespace(resolve=lambda ref: SimpleNamespace(
        ref=ref, effort=ref.effort, capabilities=("chat", "tools", "vision")))
    def seed(value):
        value["locked_models"] = {"extract": current.as_dict()}
        value["node_states"]["extract"].update(status="needs_attention", input_revision="input-v1",
            started_at=1.0, completed_at=2.0, handoff={"open_items": ["preserve this work"]})
        if trigger == "idle_stall":
            value["node_states"]["extract"].update(segment_health={"started_at": 1.0,
                "sampling": "tool_complete", "repeated_reads": 3, "segment_id": "stalled-segment",
                "work_delta": {"known": True, "total_units": 0}}, scheduler_recovery={"host_attempts": 1})
    store.mutate(handler.run_id, seed, durable=True)
    store.transition(handler.run_id, "paused", reason="pending recovery")
    store.set_difficult_node_model(handler.run_id, conversation_id="recovery-test", model=target.as_dict(),
        node_ids=["extract"], apply_node_ids=[], expected_updated_at=store.load(handler.run_id)["updated_at"],
        command_id="user-difficulty", reason="user selected GPT-6.1 SOL xhigh")
    request = dict(node=flow.node_map["extract"],
        result=ExpertRoleResult(text="current stop", status="needs_attention",
            error_kind="budget_exhausted" if trigger == "idle_stall" else trigger),
        policy=ExpertExecutionPolicy("stable", False, 0),
        allowed=["continue_checkpoint", "diagnose_path", "reduce_execution_profile"],
        host_facts={}, account=AsyncMock())
    if trigger == "capability_missing":
        decision = await handler(**request)
        assert decision["action"] == "continue_checkpoint"
        assert store.load(handler.run_id)["locked_models"]["extract"] == current.as_dict()
    else:
        _reserve_same_method_retry(store, handler.run_id)
        decision = await handler(**request)
        assert decision["action"] == "switch_declared_route"
        assert decision["model_switch"]["reason"] == "user_authorized_difficult_route"
        assert "gpt-6.1-sol" in decision["presentation"]["reason"]
        assert "极高" in decision["presentation"]["reason"]
        assert store.load(handler.run_id)["locked_models"]["extract"] == target.as_dict()
        assert store.load(handler.run_id)["node_states"]["extract"]["handoff"]["open_items"] == ["preserve this work"]
    assert not primary_client.calls and not reviewer.calls


def controller(tmp_path, monkeypatch, reviewer=None):
    flow, contract = chain_flow()
    store = ExpertRunStore(tmp_path / "runs")
    run_id = "a" * 32
    store.create(run_id=run_id, conversation_id="recovery-test", task="verify saved artifacts", attachments=[],
                 profile={}, strategy="stable", flow=flow)
    store.mutate(run_id, lambda v: v["node_states"].setdefault("extract", {}).update(
        messages=serialize_messages([Message("assistant", "已有产物已落盘，继续下一项")]),
        tools={"read": {"name": "Read", "status": "interrupted"}}, available_tools=["Read"]))
    primary, jev, events = Chat(), reviewer or Jev(), []
    session = SimpleNamespace(agent=SimpleNamespace(client=primary))
    async def emit(event):
        events.append(event)
        if event["type"] == "ask_user":
            # This is an actual UI answer, not synthesized by the controller.
            session._ask_user_request.future.set_result({"action": PAUSE})
    registry = SimpleNamespace(resolve=lambda ref, **kwargs: SimpleNamespace(client=jev, capabilities=()))
    monkeypatch.setattr(recovery_decision, "load_review_model_settings", lambda p: {
        "provider_key": "user-selected-jev", "model_id": "jev-latest"})
    handler = SchedulerRecovery(session=session, registry=registry, store=store, run_id=run_id,
        emit=emit, before_resume=AsyncMock(), task="verify saved artifacts", model_decisions=True)
    return handler, flow, contract, store, primary, jev, events


async def decide(handler, flow, **kwargs):
    values = dict(node=flow.node_map["extract"], result=ExpertRoleResult(text="900 秒执行段超时",
        status="budget_exhausted", error_kind="segment_timeout"),
        policy=ExpertExecutionPolicy("stable", False, 0), allowed=["continue_checkpoint", "diagnose_path"],
        host_facts={"trigger": "segment_timeout", "progress": {"complete": 12}}, account=AsyncMock())
    values.update(kwargs)
    return await handler(**values)


@pytest.mark.asyncio
async def test_scheduler_host_decision_persists_without_paid_advice(tmp_path, monkeypatch):
    handler, flow, _, store, primary, jev, events = controller(tmp_path, monkeypatch)
    account = AsyncMock()
    chosen = await decide(handler, flow, account=account)
    assert chosen["action"] == "continue_checkpoint"
    assert not primary.calls and not jev.calls
    account.assert_not_awaited()
    state = handler.stage("extract")["scheduler_recovery"]
    assert state["controller"]["action"] == "progress_continue"
    await handler.record_result({"node_id": "extract", "status": "completed"})
    assert "completed" in handler.stage("extract")["scheduler_recovery"]["decisions"][0]["execution_status"]
    assert not any(e["type"] == "ask_user" for e in events)


@pytest.mark.asyncio
async def test_old_host_prompt_revalidated_without_answering_for_user(tmp_path, monkeypatch):
    from mewcode.experts.recovery_state import binding
    handler, flow, _, store, primary, jev, events = controller(tmp_path, monkeypatch)
    def seed(value):
        stage = value["node_states"]["extract"]
        stage["pending_interaction"] = {"request_id": "obsolete", "tool": "SchedulerRecovery", "binding": binding(stage)}
        stage["scheduler_recovery"] = {"phase": "awaiting_input", "decision_attempts": 0,
            "decisions": [{"action": "human", "decision_tokens": 0}]}
        stage["tools"]["new"] = {"name": "ReadReviewQueue", "tool_id": "new", "status": "completed", "output": "revision241"}
    store.mutate(handler.run_id, seed)
    result = await decide(handler, flow)
    assert result["action"] == "continue_checkpoint"
    saved = store.load(handler.run_id)["node_states"]["extract"]
    assert saved["interaction_history"][-1]["answered"] is False
    assert "pending_interaction" not in saved
    assert not primary.calls and not jev.calls
    assert not any(e["type"] == "ask_user" for e in events)


@pytest.mark.asyncio
async def test_interrupted_local_read_does_not_force_human(tmp_path, monkeypatch):
    handler, flow, _, store, _, jev, _ = controller(tmp_path, monkeypatch)
    store.mutate(handler.run_id, lambda value: value["node_states"]["extract"].update(
        tools={"read": {"name": "ReadFile", "status": "running"}}))
    assert (await decide(handler, flow))["action"] == "continue_checkpoint"
    assert not jev.calls
    assert store.load(handler.run_id)["node_states"]["extract"]["tools"]["read"]["status"] == "interrupted_read_only"


@pytest.mark.asyncio
async def test_proven_progress_resumes_without_paid_decision_and_is_consumed(tmp_path, monkeypatch):
    handler, flow, _, store, primary, jev, _ = controller(tmp_path, monkeypatch)
    def seed(value):
        value["review_queues"] = {"extract": {"records": {"item": {"state": "complete"}}}}
        value["node_states"]["extract"]["slice_progress_baseline"] = {
            "review_complete": 0, "review_recorded": 0, "submitted_items": []}
    store.mutate(handler.run_id, seed)
    result = await decide(handler, flow)
    assert result["action"] == "continue_checkpoint"
    assert result["source"] == "host_progress_checkpoint"
    assert not primary.calls and not jev.calls
    await decide(handler, flow)
    assert not primary.calls and not jev.calls


@pytest.mark.asyncio
async def test_repair_executor_progress_is_read_from_the_reviewers_queue(tmp_path, monkeypatch):
    """Submitting into the auditor's queue is the executor's real progress."""
    from mewcode.experts.recovery_state import advanced, progress_marker
    handler, flow, _, store, _, _, _ = controller(tmp_path, monkeypatch)
    node = store.load(handler.run_id)["node_states"]["extract"]
    assert progress_marker(store.load(handler.run_id), "extract")["submitted_items"] == []
    def seed(value):
        value["review_queues"] = {"check": {"records": {"item": {"state": "complete"}},
            "repair_batches": {"b1": {"owner": "extract", "status": "submitted", "item_ids": ["item"],
                "candidates": {"item": {"candidate_revision": "rev1"}}}}}}
    store.mutate(handler.run_id, seed)
    marker = progress_marker(store.load(handler.run_id), "extract")
    assert marker["submitted_items"] == ["item"] and marker["review_complete"] == 1
    assert advanced(node.get("slice_progress_baseline") or
                    {"review_complete": 0, "review_recorded": 0, "submitted_items": []}, marker)


@pytest.mark.asyncio
async def test_progress_does_not_override_pending_approval(tmp_path, monkeypatch):
    handler, flow, _, store, primary, jev, _ = controller(tmp_path, monkeypatch)
    def seed(value):
        value["review_queues"] = {"extract": {"records": {"item": {"state": "complete"}}}}
        value["node_states"]["extract"].update(slice_progress_baseline={}, pending_permission={"tool_id": "approval"})
    store.mutate(handler.run_id, seed)
    with pytest.raises(BranchWaitPaused):
        await decide(handler, flow)
    assert not primary.calls and not jev.calls


@pytest.mark.asyncio
async def test_timeout_preserves_saved_progress_and_billing_without_paid_advice(tmp_path, monkeypatch):
    handler, flow, _, store, primary, jev, _ = controller(tmp_path, monkeypatch)
    progress = {'total': 50, 'complete': 8, 'pending': 40, 'needs_repair': 2, 'advanced': True}
    store.mutate(handler.run_id, lambda v: v['node_states']['extract'].update(
        review_progress=progress, billable_tokens=1234, iterations_used=9,
        available_tools=['ReviewWorkQueue', 'ReadAuditArtifact']))
    result = handler.checkpoint_result('extract', ExpertRoleResult(text='timeout', status='budget_exhausted'))
    assert result.review_progress == progress and result.billable_tokens == 1234 and result.iterations_used == 9
    await decide(handler, flow)
    assert not primary.calls and not jev.calls
    assert handler.stage('extract')['available_tools'] == ['ReviewWorkQueue', 'ReadAuditArtifact']


@pytest.mark.asyncio
async def test_missing_repair_capability_enters_automatic_ladder(tmp_path, monkeypatch):
    handler, flow, _, _, primary, jev, events = controller(tmp_path, monkeypatch)
    result = await decide(handler, flow, result=ExpertRoleResult(text='missing worker', status='needs_attention',
        error_kind='capability_missing'))
    assert result['controller']['action'] == 'progress_continue'
    assert not primary.calls and not jev.calls
    assert not any(e['type'] == 'ask_user' for e in events)


@pytest.mark.asyncio
async def test_exhausted_uncertain_review_preserves_scores_without_network_advice(tmp_path, monkeypatch):
    handler, flow, _, store, primary, jev, events = controller(tmp_path, monkeypatch)
    route = {"status": "needs_evidence", "unresolved": ["scope"], "judgments": {"scope": {"confidence": .62}}}
    store.mutate(handler.run_id, lambda v: v["node_states"]["extract"].update(safety_review=route))
    with pytest.raises(BranchWaitPaused):
        await decide(handler, flow, result=ExpertRoleResult(text="补证复审仍不确定", status="blocked",
            error_kind="safety_review_inconclusive"))
    assert not primary.calls and not jev.calls
    assert handler.stage('extract')['safety_review'] == route
    case = next(e['case'] for e in reversed(events) if e.get('type') == 'recovery_case')
    assert case['status'] == 'reported' and case['report']['kind'] == 'incident'


@pytest.mark.asyncio
async def test_recovery_choice_does_not_call_a_paid_confidence_reviewer(tmp_path, monkeypatch):
    reviewer = Chat({"choice": "diagnose_path", "confidence": .62})
    handler, flow, _, _, primary, _, events = controller(tmp_path, monkeypatch, reviewer)
    result = await decide(handler, flow)
    assert result['controller']['action'] == 'progress_continue'
    assert not primary.calls and not reviewer.calls
    assert not any(e['type'] == 'ask_user' for e in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["unknown_write", "permission", "budget", "contract", "decision_limit"])
async def test_host_boundaries_cannot_be_overridden(tmp_path, monkeypatch, boundary):
    handler, flow, _, store, primary, jev, events = controller(tmp_path, monkeypatch)
    args = {}
    if boundary == "unknown_write":
        store.mutate(handler.run_id, lambda v: v["node_states"]["extract"].update(
            tools={"write": {"name": "Write", "status": "outcome_unknown"}}))
    elif boundary == "permission":
        store.mutate(handler.run_id, lambda v: v["node_states"]["extract"].update(pending_permission={"request_id": "original"}))
    elif boundary == "budget":
        args["policy"] = ExpertExecutionPolicy("stable", False, 0, token_budget=100)
    elif boundary == "contract":
        args.update(allowed=[], host_facts={"trigger": "plan_revision_required", "hard_boundary": "冻结合同已变化"})
    else:
        _seed_recovery(store, handler.run_id)
    if boundary in {"budget", "contract"}:
        assert (await decide(handler, flow, **args))["action"] == "stop"
    else:
        with pytest.raises(BranchWaitPaused):
            await decide(handler, flow, **args)
    assert not primary.calls and not jev.calls
    if boundary == "permission":
        assert not any(e["type"] == "ask_user" for e in events)
        assert store.load(handler.run_id)["node_states"]["extract"]["pending_permission"]["request_id"] == "original"


@pytest.mark.asyncio
async def test_actual_loop_scheduler_timeout_enters_review_and_resumes_only_current_node(tmp_path, monkeypatch):
    handler, flow, contract, store, primary, jev, _ = controller(tmp_path, monkeypatch)
    calls, states = [], {}
    async def runner(node, incoming, policy, prompt):
        calls.append(node.node_id)
        if node.node_id == "extract" and calls.count("extract") == 1:
            return ExpertRoleResult(text="节点执行段超过时限 900 秒", status="budget_exhausted", error_kind="segment_timeout", billable_tokens=17)
        if node.node_id == "extract":
            assert policy.resume_messages and policy.max_iterations == contract.node_max_iterations
            assert "continue_checkpoint" in prompt
        return ExpertRoleResult(text="fresh", billable_tokens=11,
            loop_decision=report(policy.loop_context) if node.node_id == "check" else None)
    _, result = await execute(flow, contract, states, runner, recover_node=handler)
    assert result.status == "completed"
    assert calls == ["retrieve", "extract", "extract", "analyze", "rewrite", "check"]
    assert not primary.calls and not jev.calls
    assert states[contract.loop_id]["tokens"] == 17 + 55


# Drive past the bounded host/adaptive branches (host_attempts >= 2,
# decision_attempts >= 2) so the human-prompt path is the one under test.
EXHAUSTED_RECOVERY = {"host_attempts": 2, "decision_attempts": 2}
NEVER_EXECUTED_TOOL = {"call": {"tool_id": "call", "name": "WriteFile",
                                "status": "interrupted_before_execution"}}


def _seed_recovery(store, run_id, **extra):
    from mewcode.kernel.escalation import record_decision, RecoveryDecision
    def seed(value):
        stage = value['node_states']['extract']
        stage.update(tools=dict(NEVER_EXECUTED_TOOL))
        stage['scheduler_recovery'] = {**EXHAUSTED_RECOVERY, **extra}
        for index, action in enumerate(['self_diagnose', 'assist', 'assist', 'assist', 'patch_node']):
            record_decision(value, 'extract', RecoveryDecision(action, 'prior verified attempt'),
                trigger='test_prior_segment', identity=f'prior-{index}')
    store.mutate(run_id, seed)


async def _asked_options(handler, flow, events):
    with pytest.raises(BranchWaitPaused):
        await decide(handler, flow)
    prompt = next(e for e in events if e["type"] == "ask_user")
    return prompt["questions"][0]["options"]


@pytest.mark.asyncio
async def test_recovery_stops_without_a_concrete_question_after_common_budget(tmp_path, monkeypatch):
    handler, flow, _, store, primary, jev, events = controller(tmp_path, monkeypatch)
    _seed_recovery(store, handler.run_id)
    with pytest.raises(BranchWaitPaused) as caught:
        await decide(handler, flow)
    assert caught.value.error_kind == 'system_recovery_blocked'
    assert not any(e['type'] == 'ask_user' for e in events)
    assert not primary.calls and not jev.calls


@pytest.mark.asyncio
async def test_manual_continue_does_not_reset_common_budget(tmp_path, monkeypatch):
    from mewcode.experts.node_ledger import ledger_for_run
    from mewcode.kernel.escalation import action_count
    handler, flow, _, store, _, _, events = controller(tmp_path, monkeypatch)
    _seed_recovery(store, handler.run_id)
    before = ledger_for_run(store.load(handler.run_id)).view('extract')
    with pytest.raises(BranchWaitPaused):
        await decide(handler, flow)
    after = ledger_for_run(store.load(handler.run_id)).view('extract')
    assert action_count(before, 'assist') == action_count(after, 'assist') == 3
    assert handler.stage('extract')['scheduler_recovery']['controller']['action'] == 'ask_human'


@pytest.mark.asyncio
async def test_new_durable_progress_continues_even_after_assistance_budget(tmp_path, monkeypatch):
    handler, flow, _, store, _, _, events = controller(tmp_path, monkeypatch)
    _seed_recovery(store, handler.run_id)
    store.mutate(handler.run_id, lambda v: v['node_states']['extract'].update(slice_progress_baseline={}))
    store.mutate(handler.run_id, lambda v: v.update(review_queues={
        'extract': {'records': {'new': {'state': 'complete'}}}}))
    result = await decide(handler, flow)
    assert result['action'] == 'continue_checkpoint'
    assert not any(e['type'] == 'ask_user' for e in events)


def test_answered_recovery_prompt_is_not_silently_superseded():
    """Revalidation retires derived prompts, never a decision the user made."""
    from mewcode.experts.recovery_state import answered_recovery_prompt, save_interaction
    stage = {"attempt": 1, "tools": {}, "scheduler_recovery": {"decisions": []}}
    pending = {"request_id": "r1", "tool": "SchedulerRecovery",
               "questions": [{"type": "radio", "name": "action"}]}
    saved = save_interaction({"node_states": {"work": stage}}, "node_states", "work", pending)
    assert answered_recovery_prompt(stage) is None  # Still open.
    stage["interaction_history"] = [{"pending": saved, "status": "answered",
                                     "answered": True, "at": 1.0}]
    assert answered_recovery_prompt(stage) is not None
    stage["interaction_history"] = [{"pending": saved, "status": "superseded",
                                     "answered": False, "at": 1.0}]
    assert answered_recovery_prompt(stage) is None


@pytest.mark.asyncio
async def test_strict_budget_calls_no_auto_extension():
    flow, contract = chain_flow(team=False)
    handler = AsyncMock(return_value=None)
    async def runner(node, incoming, policy, prompt):
        return ExpertRoleResult(text="budget", status="budget_exhausted", billable_tokens=contract.token_budget + 1)
    _, result = await execute(flow, contract, {}, runner, recover_node=handler)
    assert result.status == "needs_attention"
    assert handler.await_args.kwargs["allowed"] == []


@pytest.mark.asyncio
async def test_cancel_never_triggers_review():
    flow, contract = chain_flow()
    handler = AsyncMock()
    async def runner(*args):
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await execute(flow, contract, {}, runner, recover_node=handler)
    handler.assert_not_called()


@pytest.mark.asyncio
async def test_real_orchestrator_wait_for_timeout_reaches_review_outside_cancelled_role(tmp_path, monkeypatch):
    from mewcode.expert_group import ExpertGroupOrchestrator
    from tests.test_expert_flow import _profile
    handler, flow, _, _, primary, jev, _ = controller(tmp_path, monkeypatch)
    calls, cleanup = [], []
    original_wait = asyncio.wait_for
    # Scale the role-segment timeout so the test is fast; the value must track
    # the configured recovery budget rather than a hard-coded one.
    # Keep the loop's initial 900-second segment on the same test budget too.
    monkeypatch.setenv("MEWCODE_EXPERT_RECOVERY_TIMEOUT_SECONDS", "900")
    from mewcode.expert_group import recovery_executor_timeout_seconds
    segment_timeout = recovery_executor_timeout_seconds()
    async def scaled_wait(future, timeout):
        return await original_wait(future, .02 if timeout == segment_timeout else timeout)
    monkeypatch.setattr(asyncio, "wait_for", scaled_wait)
    async def node_runner(node, ref, prompt, policy, member=None):
        calls.append(node.node_id)
        if node.node_id == "extract" and calls.count("extract") == 1:
            try:
                await asyncio.Future()
            finally:
                cleanup.append("role_closed")
        if node.node_id == "extract":
            assert cleanup == ["role_closed"]
            assert policy.resume_messages
        return ExpertRoleResult(text="saved", handoff={"summary": "saved", "artifacts": [], "open_items": []},
            loop_decision=report(policy.loop_context) if node.node_id == "check" else None)
    orchestrator = ExpertGroupOrchestrator(runner=AsyncMock(), node_runner=node_runner,
        recovery_handler=handler, archive_root=tmp_path / "archives")
    result = await orchestrator.run("verify", _profile(), {"providers": []}, strategy="stable", flow=flow)
    assert result.status == "completed"
    assert calls == ["prepare", "retrieve", "extract", "extract", "analyze", "rewrite", "check", "done"]
    assert not primary.calls and not jev.calls
    assert handler.stage("extract")["scheduler_recovery"]["controller"]["action"] == "progress_continue"


@pytest.mark.asyncio
async def test_no_progress_team_loop_gets_reviewed_change_of_path():
    flow, contract = chain_flow()
    recovery = AsyncMock(return_value={"action": "diagnose_path", "chosen": {"next_step": "核对输入路径"}})
    checks, states = [], {}
    async def runner(node, incoming, policy, prompt):
        decision = None
        if node.node_id == "check":
            checks.append(node.node_id)
            decision = report(policy.loop_context, "PASS" if recovery.await_count else "FAIL")
        return ExpertRoleResult(text="saved", loop_decision=decision)
    _, result = await execute(flow, contract, states, runner, recover_node=recovery)
    assert result.status == "completed"
    assert len(checks) == contract.max_stalled_rounds + 2
    assert recovery.await_count == 1
    assert recovery.await_args.kwargs["allowed"] == ["diagnose_path"]
    assert states[contract.loop_id]["recovery_history"][-1]["reason"] == "reviewed_no_progress_diagnosis"


@pytest.mark.asyncio
async def test_retired_legacy_paid_recovery_does_not_reissue_model_request(tmp_path, monkeypatch):
    handler, flow, _, store, primary, jev, events = controller(tmp_path, monkeypatch)
    store.mutate(handler.run_id, lambda v: v['node_states']['extract'].update(
        scheduler_recovery={'phase': 'deciding', 'decision_attempts': 1}))
    result = await decide(handler, flow)
    assert result['action'] == 'continue_checkpoint'
    assert not primary.calls and not jev.calls
    assert handler.stage('extract')['retired_model_gates'][0]['status'] == 'retired_not_approved'
    assert not any(e['type'] == 'ask_user' for e in events)


def test_recovery_renderer_shows_actual_rejected_scores_and_trigger():
    import subprocess
    from pathlib import Path
    source = Path("frontend/static/app.js").read_text(encoding="utf-8")
    start = source.index("  function recoveryDecisionText(")
    end = source.index("\n  function ", start + 1)
    script = source[start:end] + '''
const text = recoveryDecisionText({trigger:'segment_timeout', review_choice:'diagnose_path', action:'human',
  confidence:0.62, review_route:{model:'jev-1.13',provider_key:'selected'}, host_check:'未通过',
  candidates:[{action:'diagnose_path',reason:'path evidence',next_step:'inspect',expected_progress:'read'}],
  probabilities:{diagnose_path:.62}});
for(const expected of ['segment_timeout','jev-1.13','62%','请求人工介入','诊断并修复路径']) {
  if(!text.includes(expected)) throw new Error(expected);
}
'''
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr


def _reserve_same_method_retry(store, run_id):
    from mewcode.kernel.escalation import decide_and_record
    store.mutate(run_id, lambda value: decide_and_record(value, 'extract', trigger='failure',
        identity='fixture-prior-same-method-retry'), durable=True)
