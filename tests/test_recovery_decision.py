import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.conversation import ConversationManager, Message
from mewcode.experts.iteration_recovery import CONTINUE, PAUSE, recover_iteration_stream
from mewcode.experts.recovery_decision import RecoveryDecider, RecoveryDecisionError, validate_candidates, validate_selection
from mewcode.tools.base import StreamEnd, TextDelta
from tests.test_iteration_recovery import SlicedAgent


def candidate(action="diagnose_path"):
    return {"action": action, "reason": "工具返回路径不存在", "next_step": "核对实际目录与已有产物路径",
            "expected_progress": "成功读取已有文件，不重复生成"}


class Chat:
    model = "chosen-primary"
    max_output_tokens = 8192
    def __init__(self, value=None):
        self.calls, self.ends = [], []
        self.value = value or {"candidates": [candidate()]}
    def get_usage_recorder(self):
        return None
    def set_max_output_tokens(self, count):
        self.max_output_tokens = count
    def _record_stream_end(self, event, headers=None):
        self.ends.append(event)
        return event
    async def stream(self, conversation, system="", tools=None):
        self.calls.append((copy.deepcopy(conversation.history), system, tools, self.max_output_tokens))
        value = copy.deepcopy(self.value)
        if 'observations' in value:
            payload = json.loads(conversation.history[0].content)
            value.update(covered_evidence_ids=payload['visual_evidence']['required_evidence_ids'],
                         uncertain=[], requires_joint_pixels=False)
        yield TextDelta(json.dumps(value))
        yield self._record_stream_end(StreamEnd("end_turn", input_tokens=100, output_tokens=30, model=self.model))


class Jev(Chat):
    async def evaluate(self, state, questions):
        self.calls.append((state, questions))
        self._record_stream_end(StreamEnd("end_turn", input_tokens=80, output_tokens=0))
        choices = questions["path"]["criteria"]
        return {"model": "jev-version-receipt", "answers": {"path": {"choice": "diagnose_path", "confidence": .99,
                "probabilities": {key: .99 if key == "diagnose_path" else .01 for key in choices}}}}


def setup_decider(tmp_path, *, reviewer=None, visual=None, budget=lambda: None):
    settings = tmp_path / "review.json"
    settings.write_text(json.dumps({"provider_key": "my-jev", "model_id": "jev-latest",
                                  "vision_provider_key": "my-vision", "vision_model_id": "vision-model"}), encoding="utf-8")
    primary, reviewer, calls, charged = Chat(), reviewer or Jev(), [], []
    def resolve(ref, **kwargs):
        calls.append(ref)
        return SimpleNamespace(client=visual if ref.provider_key == "my-vision" else reviewer,
                               capabilities=("vision",) if ref.provider_key == "my-vision" else ())
    decider = RecoveryDecider(primary=primary, registry=SimpleNamespace(resolve=resolve), task="审核现有产物",
        tools=["ReadAuditArtifact"], remaining_tokens=budget, charge=charged.append, settings_path=settings)
    return decider, primary, reviewer, calls, charged, settings


@pytest.mark.asyncio
async def test_main_proposes_selected_jev_chooses_without_tools_or_mutating_main(tmp_path):
    decider, primary, jev, refs, charged, settings = setup_decider(tmp_path)
    result = await decider.decide(ConversationManager(), allowed=["diagnose_path", "human"], state={}, progress={})
    assert result["action"] == "diagnose_path" and result["confidence"] == .99
    assert result["review_route"] == {"model": "jev-version-receipt", "configured_model": "jev-latest",
                                     "provider_key": "my-jev", "channels": ["text"], "pixel_access": "none"}
    assert primary.max_output_tokens == 8192 and primary.calls[0][2:] == ([], 2048)
    assert sum(charged) == 210
    assert set(jev.calls[0][1]["path"]["criteria"]) == {"diagnose_path", "human"}
    settings.write_text(json.dumps({"provider_key": "new-selection", "model_id": "jev-preview"}), encoding="utf-8")
    await decider.decide(ConversationManager(), allowed=["diagnose_path", "human"], state={}, progress={})
    assert refs[-1].provider_key == "new-selection" and refs[-1].model_id == "jev-preview"


@pytest.mark.asyncio
async def test_non_jev_selection_is_respected(tmp_path):
    decider, primary, reviewer, _, _, _ = setup_decider(tmp_path, reviewer=Chat({"choice": "diagnose_path", "confidence": .98}))
    result = await decider.decide(ConversationManager(), allowed=["diagnose_path", "human"], state={}, progress={})
    assert result["action"] == "diagnose_path" and len(reviewer.calls) == 1
    assert reviewer.calls[0][2] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("confidence", [.99, .6])
async def test_native_jev_choice_receipt_tagged_and_counted_even_when_rejected(tmp_path, confidence):
    import httpx
    from tests.test_typesafe_review import make_client, response_for
    records, requests = [], []
    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response_for(request, overrides={"path": "diagnose_path"}, confidence=confidence))
    native = make_client(handle)
    native.set_usage_recorder(records.append)
    original_recorder = native.get_usage_recorder()
    decider, _, _, _, charged, _ = setup_decider(tmp_path, reviewer=native)
    state = {}
    if confidence < .9:
        with pytest.raises(RecoveryDecisionError, match="置信度"):
            await decider.decide(ConversationManager(), allowed=["diagnose_path", "human"], state=state, progress={})
        assert state["decision_draft"]["review_choice"] == "diagnose_path"
        assert state["decision_draft"]["confidence"] == confidence
        assert "probabilities" in state["decision_draft"]
    else:
        result = await decider.decide(ConversationManager(), allowed=["diagnose_path", "human"], state=state, progress={})
        assert result["review_route"]["model"] == "jev-1.13.0"
    assert set(requests[0]["questions"]) == {"path"}  # Not safety allow/deny questions.
    assert records[0].metadata["purpose"] == "orchestration_recovery_decision"
    assert native.get_usage_recorder() is original_recorder
    assert charged == [130, 130]


@pytest.mark.asyncio
async def test_visual_evidence_routed_before_text_planning_and_jev(tmp_path):
    visual = Chat({"observations": "图像路径提示错误，其他内容无法确认"})
    visual.model = "selected-vision"
    decider, primary, jev, _, charged, _ = setup_decider(tmp_path, visual=visual)
    conversation = ConversationManager()
    conversation.history = [Message("user", "核对图片", content_blocks=[{"type": "image", "data": "AAAA", "media_type": "image/png"}])]
    result = await decider.decide(conversation, allowed=["diagnose_path", "human"], state={}, progress={})
    assert visual.calls[0][0][0].content_blocks[0]["data"] == "AAAA"
    assert not primary.calls[0][0][0].content_blocks
    assert "visual_evidence" in jev.calls[0][0]
    assert "AAAA" not in json.dumps(jev.calls)
    assert result["vision_route"]["model"] == "selected-vision"
    assert sum(charged) == 340


@pytest.mark.asyncio
async def test_dispatch_only_does_not_require_reinterpreting_old_page_images(tmp_path):
    decider, primary, jev, refs, charged, _ = setup_decider(tmp_path)
    conversation = ConversationManager()
    conversation.history = [Message("user", "已读过的业务页", content_blocks=[
        {"type": "image", "data": "AAAA", "media_type": "image/png"}])]
    result = await decider.decide(conversation, allowed=["diagnose_path", "human"], state={}, progress={},
        needs_visual_evidence=False, host_facts={"trigger": "segment_timeout", "checkpoint_present": True})
    assert result["vision_route"] is None
    assert len(refs) == 1 and refs[0].provider_key == "my-jev"
    assert sum(charged) == 210
    assert "AAAA" not in json.dumps(jev.calls)


@pytest.mark.asyncio
async def test_budget_blocks_calls_and_missing_visual_model_never_strips_images(tmp_path):
    decider, primary, jev, _, charged, settings = setup_decider(tmp_path, budget=lambda: 1024)
    with pytest.raises(RecoveryDecisionError, match="预算"):
        await decider.decide(ConversationManager(), allowed=["diagnose_path", "human"], state={}, progress={})
    assert not primary.calls and not jev.calls and not charged
    settings.write_text(json.dumps({"provider_key": "my-jev", "model_id": "jev-latest"}), encoding="utf-8")
    conversation = ConversationManager()
    conversation.history = [Message("user", "image", content_blocks=[{"type": "image", "data": "AAAA"}])]
    with pytest.raises(RecoveryDecisionError, match="看图能力"):
        await decider.decide(conversation, allowed=["diagnose_path", "human"], state={}, progress={})
    assert not primary.calls and not jev.calls


@pytest.mark.parametrize("action", ["increase_budget", "skip_acceptance", "rerun_graph", "grant_tool"])
def test_host_rejects_new_capabilities_and_contract_changes(action):
    with pytest.raises(RecoveryDecisionError):
        validate_candidates({"candidates": [candidate(action)]}, ["diagnose_path", "human"])


@pytest.mark.parametrize("answer", [{"choice": "missing", "confidence": 1}, {"choice": "human", "confidence": .5},
                                     {"choice": "human", "confidence": float("nan")}, {"choice": "human", "confidence": True}])
def test_invalid_choice_and_uncertainty_fail_closed(answer):
    with pytest.raises(RecoveryDecisionError):
        validate_selection(answer, [candidate("human")])


@pytest.mark.asyncio
async def test_malformed_or_interrupted_decision_still_charges_usage(tmp_path):
    decider, primary, jev, _, charged, _ = setup_decider(tmp_path)
    primary.value = {"candidates": [candidate("increase_budget")]}
    with pytest.raises(RecoveryDecisionError):
        await decider.decide(ConversationManager(), allowed=["human"], state={}, progress={})
    assert charged == [130] and not jev.calls
    class Interrupted(Chat):
        async def stream(self, *args, **kwargs):
            yield TextDelta('{')
    decider.primary = Interrupted()
    with pytest.raises(RecoveryDecisionError):
        await decider.decide(ConversationManager(), allowed=["human"], state={}, progress={})
    assert charged[-1] > 2048


async def run_recovery(agent, state, decider, *, ask=None, before_resume=None, remaining=lambda: True):
    events, saves = [], []
    async def emit(event):
        events.append(copy.deepcopy(event))
    result = [event async for event in recover_iteration_stream(agent, ConversationManager(),
        state=state, save=lambda: saves.append(copy.deepcopy(state)), emit=emit,
        ask=ask or AsyncMock(return_value={"action": CONTINUE}), remaining=remaining,
        progress=lambda: {}, node_id="audit", role="auditor", decide=decider, before_resume=before_resume)]
    return result, events, saves


@pytest.mark.asyncio
async def test_selected_diagnosis_runs_same_node_and_persists_visible_result():
    state, agent = {}, SlicedAgent(failures=1)
    decide = AsyncMock(return_value={"action": "diagnose_path", "chosen": candidate()})
    ask = AsyncMock()
    _, events, saves = await run_recovery(agent, state, decide, ask=ask)
    assert [limit for limit, _ in agent.calls] == [12, 12]
    assert any(s.get("phase") == "diagnosing" for s in saves)
    assert state["controller_decision"]["action"] == "self_diagnose"
    from mewcode.experts.node_ledger import ledger_for_run
    from mewcode.kernel.escalation import action_count
    view = ledger_for_run(state["controller_record"]).view("audit")
    assert action_count(view, "self_diagnose") == 1
    assert action_count(view, "assist") == 0
    assert events[-1]["controller"]["action"] == "self_diagnose"
    decide.assert_not_awaited()
    ask.assert_not_called()


@pytest.mark.asyncio
async def test_same_path_cannot_run_twice_and_escalates_in_place():
    state, agent = {}, SlicedAgent(failures=2)
    decide = AsyncMock(return_value={"action": "diagnose_path", "chosen": candidate()})
    ask = AsyncMock(return_value={"action": PAUSE})
    events, _, _ = await run_recovery(agent, state, decide, ask=ask)
    assert len(agent.calls) == 2
    assert events[-1].kind == "max_iterations"
    assert state["decisions"][-1]["controller"]["action"] == "assist"
    assert state["phase"] == "yielding"
    from mewcode.experts.node_ledger import ledger_for_run
    from mewcode.kernel.escalation import action_count
    view = ledger_for_run(state["controller_record"]).view("audit")
    assert action_count(view, "self_diagnose") == 1
    assert action_count(view, "assist") == 1
    decide.assert_not_awaited()
    ask.assert_not_awaited()


@pytest.mark.asyncio
async def test_plan_revision_or_user_cancellation_does_not_execute_selected_action():
    agent, state = SlicedAgent(failures=1), {}
    checks = 0
    async def check():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise asyncio.CancelledError("orchestration_updated")
    with pytest.raises(asyncio.CancelledError):
        await run_recovery(agent, state, AsyncMock(return_value={"action": "diagnose_path"}), before_resume=check)
    assert len(agent.calls) == 1 and state["phase"] == "diagnosing"
    assert state["controller_decision"]["action"] == "self_diagnose"


@pytest.mark.asyncio
async def test_restart_uses_ledger_budget_instead_of_reissuing_interrupted_diagnosis():
    from mewcode.experts.node_ledger import ledger_for_run

    record = {"node_states": {"audit": {}}, "node_ledgers": {}}
    ledger_for_run(record).record("audit", "maintenance", {"recovery_action": "self_diagnose"},
        source="host", identity="prior-self-diagnosis")
    decide, ask, agent = AsyncMock(), AsyncMock(return_value={"action": PAUSE}), SlicedAgent(failures=5)
    state = {"phase": "deciding", "decision_attempts": 1, "controller_record": record}
    events, _, _ = await run_recovery(agent, state, decide, ask=ask)
    assert len(agent.calls) == 1 and events[-1].kind == "max_iterations"
    assert state["controller_decision"]["action"] == "assist"
    assert state["phase"] == "yielding"
    decide.assert_not_awaited()
    ask.assert_not_awaited()


@pytest.mark.asyncio
async def test_web_auto_uses_host_diagnosis_and_keeps_missing_handoff_attention(tmp_path, monkeypatch):
    from mewcode import expert_web
    from mewcode.agent import UsageEvent
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.experts import recovery_decision
    from mewcode.experts.flow import ExpertFlowSpec, ExpertFlowStore
    from mewcode.orchestration.plan_session import PlanSessionStore
    from tests.test_expert_flow import _profile

    project = tmp_path / "project"
    project.mkdir()
    runs, flows = ExpertRunStore(tmp_path / "runs"), ExpertFlowStore(tmp_path / "flows.json")
    flow = ExpertFlowSpec.from_value({"flow_id": "decision", "label": "decision", "finalizer_node_id": "done",
        "nodes": [{"node_id": "done", "role": "manager", "label": "成果整理"}], "edges": []})
    flows.put(project, flow)
    class WebAgent(SlicedAgent):
        client = SimpleNamespace(expose_reasoning=False, set_max_output_tokens=lambda n: None)
        context_window = 100000
        total_input_tokens = total_output_tokens = 0
        registry = SimpleNamespace(get=lambda name: None, register=lambda *args, **kwargs: None,
            unregister=lambda *args, **kwargs: None, is_enabled=lambda name: False,
            list_tools=lambda: [])
        def set_review_authorization_context(self, context):
            pass
        async def run(self, conversation):
            # This cumulative executor event must not overwrite planner/reviewer cost.
            yield UsageEvent(input_tokens=50, output_tokens=0, billable_tokens=50)
            async for event in super().run(conversation):
                yield event
    agent, primary, jev = WebAgent(failures=1), Chat(), Jev()
    monkeypatch.setattr(recovery_decision, "load_review_model_settings", lambda path: {"provider_key": "selected", "model_id": "jev-latest"})
    monkeypatch.setattr(expert_web, "_build_role_agent", lambda *args: (
        agent, None, SimpleNamespace(conversation=ConversationManager(), aclose=AsyncMock())))
    session = SimpleNamespace(work_dir=str(project), conversation_id="c", multi_agent=False,
        agent=SimpleNamespace(client=primary, permission_mode="auto"),
        session=SimpleNamespace(meta=SimpleNamespace(message_count=0), append=lambda *a: None, append_expert_run=lambda *a, **kw: None),
        conversation=ConversationManager(), expert_run_id="", expert_control_action="pause",
        context=SimpleNamespace(get=lambda key, default=None: default, slots=SimpleNamespace(resolve=lambda key: {})),
        task_manager=SimpleNamespace(list_tasks=lambda: [], mark_notified=lambda ids: None),
        expert_child_event_callback=None, status_payload=lambda: {}, tasks_payload=lambda: [])
    events = []
    async def send(event):
        events.append(event)
        if event["type"] == "ask_user":
            session._ask_user_request.future.set_result({"action": PAUSE})
    registry = SimpleNamespace(catalog=AsyncMock(return_value={"providers": []}),
        resolve=lambda ref, **kwargs: SimpleNamespace(client=jev, capabilities=()))
    await asyncio.wait_for(expert_web.run_expert_chat(SimpleNamespace(send_json=send), "finish task",
        session=session, registry_service=registry, profile_store=SimpleNamespace(get=lambda p: _profile()),
        flow_store=flows, plan_store=PlanSessionStore(tmp_path / "plans.json"), run_store=runs, confirmed_new_flow=flow), 15)
    final = next(e for e in reversed(events) if e["type"] == "done")
    loaded = runs.load(final["run_id"])
    assert final["expert_status"] in {'needs_attention', 'paused'}
    if final['expert_status'] == 'paused':
        assert loaded['node_states']['done']['recovery_case']['status'] == 'reported'
        assert loaded['node_states']['done']['recovery_case']['pending']['options']
    assert not primary.calls
    # The sole JEV call is the post-run experience reflection, not a recovery decision.
    assert len(jev.calls) == 1 and isinstance(jev.calls[0][0][0], Message)
    stage = runs.load(final["run_id"])["node_states"]["done"]
    assert stage["iteration_recovery"]["decisions"][-1]["controller"]["action"] == "self_diagnose"
    assert "SubmitHandoff" in stage["handoff"]["blockers"][0]
    assert stage["iteration_recovery"].get("decision_tokens", 0) == 0
    assert stage["billable_tokens"] == 50
    assert stage["usage"]["billable_tokens"] == 50
    assert not any(e["type"] == "ask_user" for e in events)
    assert not any(e.get("recovery_decision", {}).get("review_route") for e in events)
