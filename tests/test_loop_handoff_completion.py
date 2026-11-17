"""A real Agent/web/loop run must collect both reviewer submissions."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.agent import Agent
from mewcode.conversation import ConversationManager
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.flow import ExpertFlowSpec, ExpertFlowStore
from mewcode.orchestration.plan_session import PlanSessionStore
from mewcode.orchestration.task_design import TaskDesign
from mewcode.tools.base import TextDelta, ToolCallComplete, StreamEnd
from mewcode.tools import ToolRegistry
from mewcode.tools.read_file import ReadFile
from tests.test_agent import MockLLMClient
from tests.test_collaboration_mode import CATALOG, PROFILE
from tests.test_expert_children import FakeSession


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['PASS', 'FAIL', 'PASS_RATE_LIMIT', 'PASS_SAVED_WAIT'])
async def test_web_loop_advances_only_after_both_submissions(tmp_path, monkeypatch, state):
    from mewcode import expert_web
    from mewcode.client import RateLimitError
    from mewcode import provider_cooldown

    cooldown_case = state if state.startswith('PASS_') else ''
    state = 'PASS' if cooldown_case else state

    monkeypatch.setattr('mewcode.permissions.review_client.load_review_model_settings', lambda: {})
    project = tmp_path / 'project'
    project.mkdir()
    design = TaskDesign(project, 'fixture')
    created = design.start('execute', orchestration_mode='classic')
    claimed = design.claim_execution(design.transition(created, 'ready',
        build_request={'id': 'build', 'state': 'saved', 'epoch': 1}))
    flow = ExpertFlowSpec.from_value({'flow_id': 'dual', 'finalizer_node_id': 'done',
        'nodes': [{'node_id': key, 'role': 'executor', 'tools': ['ReadFile'], 'capabilities': {},
                   'failure_policy': {'action': 'stop', 'max_attempts': 1}}
                  for key in ('work', 'review', 'next_review', 'done')],
        'edges': [{'from': a, 'to': b} for a, b in
                  [('work', 'review'), ('review', 'next_review'), ('next_review', 'done')]],
        'meta': {'task_design': {'task_id': claimed['task_id'], 'orchestration_mode': 'classic'},
            'loops': [{'loop_id': 'review-loop', 'entry_node_id': 'work', 'check_node_id': 'next_review',
                'node_ids': ['work', 'review', 'next_review'], 'reviewer_node_ids': ['review', 'next_review'],
                'max_iterations': 1, 'exit_condition': 'All checks pass',
                'criteria': [{'criterion_id': 'R1', 'acceptance_test': 'Verify actual result'}]}]}})
    runs, flows = ExpertRunStore(tmp_path / 'runs', storage='sqlite'), ExpertFlowStore(tmp_path / 'flows.json')
    flows.put(project, flow)
    calls, agents, events, waits = [], {}, [], []
    clock_offset = 0
    original_remaining = provider_cooldown.cooldown_remaining
    original_record = provider_cooldown.record_cooldown
    real_sleep = asyncio.sleep

    def remaining(record, model, *, now=None):
        import time
        clock = 1000 if cooldown_case == 'PASS_SAVED_WAIT' else time.time()
        return original_remaining(record, model, now=(clock if now is None else now) + clock_offset)

    def record_cooldown(record, model, seconds, *, now=None):
        # Disk commits must not consume the synthetic provider wait before
        # the restored role reaches it; advance this clock only at sleep.
        if cooldown_case == 'PASS_SAVED_WAIT' and now is None:
            now = 1000 + clock_offset
        return original_record(record, model, seconds, now=now)

    async def sleep(delay, *args, **kwargs):
        nonlocal clock_offset
        if cooldown_case and 40 < delay <= 56:
            run_id = next(event['run_id'] for event in events if event.get('run_id'))
            saved = runs.load(run_id)
            assert remaining(saved, PROFILE.executor) > 0
            if cooldown_case == 'PASS_RATE_LIMIT':
                stage = saved['node_states']['review']
                assert len(stage['tools']) == 1
                receipt = next(iter(stage['tools'].values()))
                assert receipt['name'] == 'SubmitHandoff' and not receipt['is_error']
                assert stage['messages']
            else:
                assert 'review' not in agents  # Wait before constructing another model client.
            waits.append(delay)
            clock_offset += 60
            return
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(provider_cooldown, 'cooldown_remaining', remaining)
    monkeypatch.setattr(provider_cooldown, 'record_cooldown', record_cooldown)
    monkeypatch.setattr(asyncio, 'sleep', sleep)

    def build(_session, _registry, role, _model, policy, verdict):
        node = policy.execution_node_id
        calls.append(node)
        handoff = {'summary': 'Review delivered; findings preserved',
            'inline_outputs': [{'name': 'Actual findings', 'content': 'An unresolved defect' if state == 'FAIL' else 'Verified result'}]}
        submissions = {'SubmitHandoff': handoff}
        reviewer = bool(policy.loop_context and policy.loop_context.get('reviewer'))
        if reviewer:
            submissions['SubmitLoopDecision'] = {'contract_hash': policy.loop_context['contract_hash'],
                'decision': 'EXIT' if state == 'PASS' else 'CONTINUE',
                'checks': [{'criterion_id': 'R1', 'state': state, 'evidence': 'Actual report evidence',
                            'location': 'inline:Actual findings'}]}
        end = StreamEnd('end_turn', input_tokens=1, output_tokens=1)
        first = 'SubmitHandoff' if node == 'review' else 'SubmitLoopDecision'
        order = [first, next(name for name in submissions if name != first)] if reviewer else ['SubmitHandoff']
        responses = []
        for i, name in enumerate(order):
            responses += [[ToolCallComplete(f'{node}-{i}', name, submissions[name]), end],
                          [TextDelta('Finished.'), end]]
        class Client(MockLLMClient):
            injected = False

            async def stream(self, *args, **kwargs):
                if not self.injected and self._call_index == 1:
                    if cooldown_case == 'PASS_RATE_LIMIT' and node == 'review':
                        self.injected = True
                        raise RateLimitError('Rate limited. Retry after 56s.', retry_after=56)
                    if cooldown_case == 'PASS_SAVED_WAIT' and node == 'work':
                        self.injected = True
                        run_id = next(event['run_id'] for event in events if event.get('run_id'))
                        runs.mutate(run_id, lambda record: provider_cooldown.record_cooldown(
                            record, PROFILE.executor, 56), durable=True)
                async for event in super().stream(*args, **kwargs):
                    yield event

        client = Client(responses)
        client.expose_reasoning = False
        client.max_output_tokens = 4096
        parent = ToolRegistry()
        parent.register(ReadFile())
        parent_session = FakeSession(parent)
        parent_session.work_dir = str(project)
        registry = expert_web._role_registry(parent_session, role, policy, verdict)
        agent = Agent(client, registry, 'anthropic', work_dir=str(project), max_iterations=10)
        agent.expert_role = role
        agents[node] = agent
        return agent, None, SimpleNamespace(conversation=ConversationManager(), aclose=AsyncMock())

    session = SimpleNamespace(work_dir=str(project), conversation_id='fixture', multi_agent=False,
        session=SimpleNamespace(meta=SimpleNamespace(message_count=0), append=lambda *a: None,
            append_expert_run=lambda *a, **kw: None), conversation=ConversationManager(), expert_run_id='',
        expert_control_action='pause', context=SimpleNamespace(get=lambda key, default=None: default,
            slots=SimpleNamespace(resolve=lambda key: {})),
        task_manager=SimpleNamespace(list_tasks=lambda: [], mark_notified=lambda ids: None),
        expert_child_event_callback=None, status_payload=lambda: {}, tasks_payload=lambda: [])
    monkeypatch.setattr(expert_web, '_build_role_agent', build)
    async def send(event):
        events.append(event)
        if event.get('type') == 'expert_ask_user':
            raise AssertionError(event)
    await asyncio.wait_for(expert_web.run_expert_chat(SimpleNamespace(send_json=send),
        'Review the result without changing findings', session=session, confirmed_new_flow=flow,
        task_design_dispatch=design.execution_token(claimed),
        registry_service=SimpleNamespace(catalog=AsyncMock(return_value=CATALOG), resolve=lambda ref, **kw: SimpleNamespace(ref=ref)),
        profile_store=SimpleNamespace(get=lambda _: PROFILE), flow_store=flows,
        plan_store=PlanSessionStore(tmp_path / 'plans.json'), run_store=runs), 480)
    run_id = next(event['run_id'] for event in events if event.get('run_id'))
    saved = runs.load(run_id)
    assert calls == ['work', 'review', 'next_review'] + (['done'] if state == 'PASS' else []), events[-5:]
    for node in ('review', 'next_review'):
        stage = saved['node_states'][node]
        assert saved['loop_states']['review-loop']['round_results'][node]['status'] == 'completed'
        assert stage['handoff']['status'] == 'completed'
        assert stage['loop_decision']['checks'][0]['state'] == state
        assert agents[node].client._call_index == 3
        assert stage['iteration_recovery']['handoff_correction']['attempts'] == 1
        assert all(not row['is_error'] for row in stage['tools'].values())
    assert (saved['status'] == 'completed') is (state == 'PASS')
    if cooldown_case:
        assert len(waits) == 1
        assert saved['provider_cooldowns']


def test_submission_correction_preserves_blocks_and_is_bounded():
    from mewcode.experts.completion import rejected_handoff_correction
    kwargs = {'input_revision': 'v1', 'node_attempt': 1, 'require_loop_decision': True}
    state = {}
    for _ in range(2):
        assert 'SubmitLoopDecision' in rejected_handoff_correction({'status': 'completed'}, {}, state, **kwargs)
    assert not rejected_handoff_correction({'status': 'completed'}, {}, state, **kwargs)
    for handoff in ({'status': 'blocked'}, {'status': 'needs_attention', 'open_items': ['review more']},
                    {'status': 'completed', 'blockers': ['missing input']}):
        assert not rejected_handoff_correction(handoff, {}, {}, **kwargs)
