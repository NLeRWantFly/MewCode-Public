import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.agent import ErrorEvent, StreamText, TurnComplete
from mewcode.conversation import ConversationManager
from mewcode.experts.iteration_recovery import CONTINUE, recover_iteration_stream


class SlicedAgent:
    max_iterations = 12

    def __init__(self, failures=3, kind='max_iterations'):
        self.failures = failures
        self.kind = kind
        self.calls = []
        self.closed = 0

    async def run(self, conversation):
        self.calls.append((self.max_iterations, copy.deepcopy(conversation.history)))
        try:
            yield TurnComplete(turn=1)
            if len(self.calls) <= self.failures:
                yield ErrorEvent('slice limit', kind=self.kind)
            else:
                yield StreamText('done')
        finally:
            self.closed += 1


async def collect(agent, *, state=None, ask=None, remaining=lambda: True,
                  progress=lambda: None, batch_handoff=False):
    state = state if state is not None else {}
    saved, events, conversation = [], [], ConversationManager()
    async def emit(event):
        events.append(event)
    result = [event async for event in recover_iteration_stream(agent, conversation,
        state=state, save=lambda: saved.append(copy.deepcopy(state)), emit=emit,
        ask=ask or AsyncMock(return_value={'action': CONTINUE}), remaining=remaining,
        progress=progress, node_id='review', role='auditor', batch_handoff=batch_handoff)]
    return result, saved, events


@pytest.mark.asyncio
@pytest.mark.parametrize('has_remaining', [False, True])
async def test_soft_deadline_yields_to_scheduler_without_restarting_or_human_prompt(has_remaining):
    agent = SlicedAgent(failures=1, kind="soft_deadline")
    events, _, _ = await collect(agent, remaining=lambda: has_remaining,
                                 ask=AsyncMock(side_effect=AssertionError("no human gate")))
    assert len(agent.calls) == 1 and agent.closed == 1
    assert any(isinstance(event, ErrorEvent) and event.kind == "soft_deadline" for event in events)


@pytest.mark.asyncio
async def test_limit_uses_one_self_diagnosis_then_yields_without_human_question():
    agent, state = SlicedAgent(), {}
    ask = AsyncMock(side_effect=AssertionError("iteration recovery must not ask for continuation"))
    events, saved, updates = await collect(agent, state=state, ask=ask)
    assert [limit for limit, _ in agent.calls] == [12, 12, 12]
    assert agent.max_iterations == 12 and agent.closed == 3
    assert [event.turn for event in events if isinstance(event, TurnComplete)] == [1, 2, 3]
    assert [item['controller']['action'] for item in state['decisions']] == ['progress_continue', 'self_diagnose', 'patch_node']
    assert state['controller_decision']['action'] == 'patch_node'
    assert state['phase'] == 'yielding' and saved[-1]['phase'] == 'yielding'
    assert events[-1].kind == 'max_iterations'
    assert [e['recovery_phase'] for e in updates] == ['continuing', 'diagnosing']
    ask.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['provider_interrupted', 'safety_review_unavailable', 'stalled'])
async def test_other_failures_do_not_get_replayed(kind):
    agent = SlicedAgent(kind=kind)
    ask = AsyncMock()
    events, _, updates = await collect(agent, ask=ask)
    assert len(agent.calls) == 1 and events[-1].kind == kind
    assert not updates and not ask.called


@pytest.mark.asyncio
async def test_explicit_method_request_goes_to_recovery_controller_without_local_trial():
    from mewcode.kernel.escalation import RecoveryDecision

    agent, state, events = SlicedAgent(failures=0), {}, []
    request = [True]
    controller_calls = []
    async def controller(**kwargs):
        controller_calls.append(kwargs)
        return RecoveryDecision(action='assist', reason='主模型协助', assistance_kind='diagnosis')
    async def emit(event):
        events.append(event)
    ask = AsyncMock(side_effect=AssertionError("the iteration helper does not own the human gate"))
    adapt = AsyncMock(side_effect=AssertionError("adaptive planning belongs to the host controller"))
    result = [e async for e in recover_iteration_stream(agent, ConversationManager(),
        state=state, save=lambda: None, emit=emit, ask=ask, remaining=lambda: True,
        progress=lambda: None, node_id='n', role='executor', adapt=adapt,
        requested=lambda: request[0], controller=controller)]
    assert [limit for limit, _ in agent.calls] == [12]
    assert agent.max_iterations == 12 and agent.closed == 1
    assert controller_calls == [{'trigger': 'assistance_requested', 'made_progress': False, 'identity': ['iteration', 1]}]
    assert state['controller_decision']['action'] == 'assist'
    assert result[-1].kind == 'max_iterations'
    ask.assert_not_awaited()
    adapt.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_probe_cannot_restart_itself_in_iteration_recovery():
    agent, ask = SlicedAgent(failures=10), AsyncMock()
    async def emit(event):
        pass
    events = [e async for e in recover_iteration_stream(agent, ConversationManager(),
        state={}, save=lambda: None, emit=emit, ask=ask, remaining=lambda: True,
        progress=lambda: None, node_id='n', role='executor', probe_only=True)]
    assert len(agent.calls) == 1 and events[-1].kind == 'max_iterations'
    ask.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('restored', [False, True])
async def test_recovery_controller_uses_epoch_ledger_for_bounded_self_diagnosis(restored):
    from mewcode.experts.node_ledger import ledger_for_run

    record = {'node_states': {'n': {}}, 'node_ledgers': {}}
    if restored:
        ledger_for_run(record).record('n', 'maintenance', {'recovery_action': 'self_diagnose'},
            source='host', identity='already-used-self-diagnosis')
    state = {'controller_record': record}
    agent, ask, finish = SlicedAgent(failures=10), AsyncMock(), AsyncMock()
    adapt = AsyncMock(side_effect=AssertionError("local advisory fallback is no longer used"))
    saved = []
    events = [e async for e in recover_iteration_stream(agent, ConversationManager(),
        state=state, save=lambda: saved.append(copy.deepcopy(state)), emit=AsyncMock(), ask=ask,
        remaining=lambda: True, progress=lambda: None, node_id='n', role='executor',
        adapt=adapt, finish_probe=finish)]
    assert [limit for limit, _ in agent.calls] == ([12] if restored else [12, 12, 12])
    assert events[-1].kind == 'max_iterations'
    assert state['controller_decision']['action'] == 'patch_node'
    assert state['phase'] == 'yielding'
    assert agent.closed == len(agent.calls) and agent.max_iterations == 12
    assert saved[-1]['phase'] == 'yielding'
    ask.assert_not_awaited()
    adapt.assert_not_awaited()
    finish.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['budget', 'revision', 'unknown_effect'])
async def test_recovery_controller_keeps_real_host_boundaries(boundary):
    from mewcode.experts.node_ledger import ledger_for_run

    agent, ask, resources = SlicedAgent(failures=10), AsyncMock(), [True]
    checks = 0
    record = {'node_states': {'n': {}}, 'node_ledgers': {}}
    state = {'controller_record': record}
    if boundary == 'unknown_effect':
        ledger_for_run(record).record('n', 'blocker',
            {'kind': 'effect_unknown', 'reason': 'unresolved write effect'}, identity='effect-unknown')
    async def check():
        nonlocal checks
        checks += 1
        if boundary == 'revision' and checks == 2:
            raise asyncio.CancelledError('orchestration_updated')
    def progress():
        if boundary == 'budget':
            resources[0] = False
        return None
    async def run():
        return [e async for e in recover_iteration_stream(agent, ConversationManager(),
            state=state, save=lambda: None, emit=AsyncMock(), ask=ask,
            remaining=lambda: resources[0], progress=progress,
            node_id='n', role='executor', before_resume=check)]
    if boundary == 'budget':
        events = await run()
        assert events[-1].kind == 'max_iterations'
    else:
        if boundary == 'revision':
            with pytest.raises(asyncio.CancelledError):
                await run()
        else:
            events = await run()
            assert events[-1].kind == 'max_iterations'
    assert len(agent.calls) == agent.closed == 1 and agent.max_iterations == 12
    ask.assert_not_awaited()
    if boundary == 'unknown_effect':
        assert state['controller_decision']['action'] == 'assist'
        assert state['controller_decision']['assistance_kind'] == 'effect_verification'


@pytest.mark.asyncio
async def test_hard_resource_budget_and_saved_batch_remain_scheduler_owned():
    for options in ({'remaining': lambda: False},
                    {'batch_handoff': True, 'progress': lambda: {'advanced': True}}):
        agent, ask = SlicedAgent(), AsyncMock()
        events, _, _ = await collect(agent, ask=ask, **options)
        assert len(agent.calls) == 1 and events[-1].kind == 'max_iterations'
        ask.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_recovery_counters_do_not_replace_controller_ledger():
    agent = SlicedAgent(failures=10)
    state = {'automatic_continuations': 1, 'diagnostic_attempts': 1, 'phase': 'diagnosing'}
    ask = AsyncMock(side_effect=AssertionError("legacy counters must not trigger a human prompt"))
    await collect(agent, state=state, ask=ask)
    assert [limit for limit, _ in agent.calls] == [12, 12, 12]
    assert [item['controller']['action'] for item in state['decisions']] == ['progress_continue', 'self_diagnose', 'patch_node']
    assert agent.max_iterations == 12 and agent.closed == 3
    ask.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_plan_cancels_before_another_model_segment():
    agent, conversation = SlicedAgent(), ConversationManager()
    checks = 0
    async def before_resume():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise asyncio.CancelledError('orchestration_updated')
    with pytest.raises(asyncio.CancelledError):
        async for _ in recover_iteration_stream(agent, conversation, state={}, save=lambda: None,
            emit=AsyncMock(), ask=AsyncMock(), remaining=lambda: True, progress=lambda: None,
            node_id='work', role='executor', before_resume=before_resume):
            pass
    assert len(agent.calls) == agent.closed == 1
    assert agent.max_iterations == 12


@pytest.mark.asyncio
async def test_host_owned_human_decision_does_not_restart_iteration_helper():
    from mewcode.kernel.escalation import RecoveryDecision

    agent = SlicedAgent(failures=10)
    ask = AsyncMock(return_value={'action': CONTINUE})
    async def controller(**_kwargs):
        return RecoveryDecision(action='ask_human', reason='需人工决定', assistance_kind=None)
    events = [event async for event in recover_iteration_stream(agent, ConversationManager(),
        state={}, save=lambda: None, emit=AsyncMock(), ask=ask, remaining=lambda: True,
        progress=lambda: None, node_id='n', role='executor', controller=controller)]
    assert len(agent.calls) == agent.closed == 1
    assert events[-1].kind == 'max_iterations'
    ask.assert_not_awaited()


@pytest.mark.asyncio
async def test_web_flow_reserves_node_replan_after_two_bounded_trials(tmp_path, monkeypatch):
    from mewcode import expert_web
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.experts.flow import ExpertFlowSpec, ExpertFlowStore
    from mewcode.orchestration.plan_session import PlanSessionStore
    from tests.test_expert_flow import _profile

    project = tmp_path / 'project'
    project.mkdir()
    runs, flows = ExpertRunStore(tmp_path / 'runs'), ExpertFlowStore(tmp_path / 'flows.json')
    flow = ExpertFlowSpec.from_value({'flow_id': 'slice', 'label': 'slice', 'finalizer_node_id': 'done',
        'nodes': [{'node_id': 'done', 'role': 'manager', 'label': '整理成果'}], 'edges': []})
    flows.put(project, flow)
    class WebAgent(SlicedAgent):
        client = SimpleNamespace(expose_reasoning=False)
        total_input_tokens = total_output_tokens = 0
        registry = SimpleNamespace(get=lambda name: None, register=lambda *args, **kwargs: None)
        def set_review_authorization_context(self, context):
            pass
    agent = WebAgent()
    monkeypatch.setattr(expert_web, '_build_role_agent', lambda *args: (
        agent, None, SimpleNamespace(conversation=ConversationManager(), aclose=AsyncMock())))
    session = SimpleNamespace(work_dir=str(project), conversation_id='c', multi_agent=False,
        session=SimpleNamespace(meta=SimpleNamespace(message_count=0), append=lambda *a: None,
                                append_expert_run=lambda *a, **kw: None),
        conversation=ConversationManager(), expert_run_id='', expert_control_action='pause',
        context=SimpleNamespace(get=lambda key, default=None: default, slots=SimpleNamespace(resolve=lambda key: {})),
        task_manager=SimpleNamespace(list_tasks=lambda: [], mark_notified=lambda ids: None),
        expert_child_event_callback=None, status_payload=lambda: {}, tasks_payload=lambda: [])
    events = []
    async def send(event):
        events.append(event)
    kwargs = dict(session=session,
        registry_service=SimpleNamespace(catalog=AsyncMock(return_value={'providers': []}), resolve=lambda model: None),
        profile_store=SimpleNamespace(get=lambda p: _profile()), flow_store=flows,
        plan_store=PlanSessionStore(tmp_path / 'plans.json'), run_store=runs)
    ws = SimpleNamespace(send_json=send)
    await asyncio.wait_for(expert_web.run_expert_chat(ws, 'finish task', confirmed_new_flow=flow, **kwargs), 15)
    final = next(e for e in reversed(events) if e['type'] == 'done')
    assert final['expert_status'] == 'needs_attention'
    snapshot = runs.load(final['run_id'])
    stage = snapshot['node_states']['done']
    recovery = stage['iteration_recovery']
    assert stage['status'] == 'needs_attention'
    assert stage['pending_recovery_decision']['decision']['owner'] == 'planner'
    assert [item['controller']['action'] for item in recovery['decisions']] == ['progress_continue', 'self_diagnose', 'patch_node']
    assert recovery['controller_decision']['assistance_kind'] is None
    assert not stage.get('pending_interaction')
    assert not any(event['type'] == 'ask_user' for event in events)
    assert len([e for e in events if e['type'] == 'done']) == 1
    assert len(agent.calls) == agent.closed == 3
