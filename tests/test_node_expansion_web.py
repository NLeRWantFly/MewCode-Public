"""Runtime micrograph proposals: host capture, durable record, forced pause."""
import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode import expert_group, expert_web
from mewcode.agent import (
    StreamText, ToolExecutionStartEvent, ToolResultEvent, ToolUseEvent,
)
from mewcode.conversation import ConversationManager
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.flow import ExpertFlowSpec, ExpertFlowStore
from mewcode.experts.node_expansion import (
    EXPANSION_REJECTION_TOOL, NodeExpansionContext, NodeExpansionHolder,
)
from mewcode.expert_web import _role_registry
from mewcode.orchestration.plan_session import PlanSessionStore
from mewcode.permissions.sandbox import PathSandbox
from mewcode.tools.propose_node_expansion import ProposeNodeExpansionParams
from mewcode.tools.submit_handoff import SubmitHandoffParams
from tests.test_expert_children import FakeSession, make_registry
from tests.test_expert_flow import _profile


def _flow():
    return ExpertFlowSpec.from_value({
        'flow_id': 'expansion', 'label': 'Expansion run',
        'nodes': [
            {'node_id': 'implement', 'role': 'executor', 'label': 'Implement',
             'capabilities': {'write_paths': ['prep.txt', 'write.txt']}},
            {'node_id': 'done', 'role': 'manager', 'label': 'Report', 'capabilities': {}},
        ],
        'edges': [{'from': 'implement', 'to': 'done'}],
        'finalizer_node_id': 'done'})


def _proposal(parent_node_id, *, flow_version=1):
    return {
        'parent_node_id': parent_node_id,
        'expected_flow_version': flow_version,
        'reason': '独立写入边界清晰，拆为两步',
        'nodes': [
            {'node_id': 'prep', 'label': '准备', 'task': '整理输入材料',
             'input_refs': ['parent'], 'outputs': ['draft'],
             'acceptance': ['draft 可读'], 'write_paths': ['prep.txt']},
            {'node_id': 'write', 'label': '写入', 'task': '写出结果文件',
             'input_refs': ['parent'], 'outputs': ['final'],
             'acceptance': ['final 存在'], 'write_paths': ['write.txt']},
        ],
        'edges': [
            {'from_node': 'prep', 'to_node': 'write', 'condition': 'always'},
            {'from_node': 'write', 'to_node': '$join', 'condition': 'always'},
        ],
        'join': {'label': '汇合', 'acceptance': ['两个产物齐备']},
    }


def _holder(project):
    return NodeExpansionHolder(context=NodeExpansionContext(
        parent_node_id='implement',
        flow_version=1,
        input_revision='rev-1',
        node_attempt=1,
        work_dir=project.resolve(),
        allowed_tools=frozenset(),
        available_input_refs=frozenset(),
        sandbox=PathSandbox(str(project)),
    ))


def _setup(tmp_path, monkeypatch, proposal, answers=None):
    project = tmp_path / 'project'
    project.mkdir()
    holder = _holder(project)
    flows, runs = ExpertFlowStore(tmp_path / 'flows.json'), ExpertRunStore(tmp_path / 'runs')
    flows.put(project, _flow())
    events, agents = [], []

    original_derive = expert_group.derive_execution_policy
    monkeypatch.setattr(expert_group, 'derive_execution_policy',
        lambda multi_agent: replace(original_derive(multi_agent), expansion_holder=holder))

    class FakeAgent:
        client = SimpleNamespace(expose_reasoning=False)
        total_input_tokens = total_output_tokens = 1
        max_iterations = 8

        def __init__(self, role, policy):
            self.registry = _role_registry(FakeSession(make_registry(
                'ReadFile', 'WriteFile', 'EditFile', 'Agent', 'TeamCreate')), role, policy)
            self.node_id = ''
            self.executed = []
            agents.append(self)

        def set_review_authorization_context(self, context):
            self.node_id = context.split('STAGE: ', 1)[1].split(' ', 1)[0]

        async def run(self, conversation):
            tool = self.registry.get('ProposeNodeExpansion')
            if self.node_id == 'implement' and tool is not None and not self.executed:
                yield ToolUseEvent(tool_name='ProposeNodeExpansion', tool_id='exp-1',
                                   arguments={'proposal': proposal})
                yield ToolExecutionStartEvent(tool_name='ProposeNodeExpansion', tool_id='exp-1')
                persisted = ExpertRunStore(runs.root).load(session.expert_run_id)
                assert persisted['node_states']['implement']['tools']['exp-1']['status'] == 'running'
                result = await tool.execute(ProposeNodeExpansionParams(proposal=proposal))
                self.executed.append('ProposeNodeExpansion')
                yield ToolResultEvent(tool_id='exp-1', tool_name='ProposeNodeExpansion',
                                      output=result.output, is_error=result.is_error, elapsed=0.01)
                result = await tool.execute(ProposeNodeExpansionParams(proposal=proposal))
                self.executed.append('second-call')
                yield StreamText(text='unreachable')
            elif '::mx-' in self.node_id or self.node_id == 'done':
                handoff_tool = self.registry.get('SubmitHandoff')
                params = SubmitHandoffParams(
                    summary=f'{self.node_id} complete', status='completed',
                    artifacts=[], decisions=[], open_items=[], evidence=[])
                yield ToolUseEvent(tool_name='SubmitHandoff', tool_id='handoff-1',
                                   arguments=params.model_dump())
                yield ToolExecutionStartEvent(tool_name='SubmitHandoff', tool_id='handoff-1')
                persisted = ExpertRunStore(runs.root).load(session.expert_run_id)
                assert persisted['node_states'][self.node_id]['tools']['handoff-1']['status'] == 'running'
                result = await handoff_tool.execute(params)
                self.executed.append('SubmitHandoff')
                yield ToolResultEvent(tool_id='handoff-1', tool_name='SubmitHandoff',
                                      output=result.output, is_error=result.is_error, elapsed=0.01)
                yield StreamText(text=f'{self.node_id} complete')
            else:
                yield StreamText(text='plain report')

    def build(_session, _registry, role, _model, policy, _verdict):
        return FakeAgent(role, policy), None, SimpleNamespace(
            conversation=ConversationManager(), aclose=AsyncMock())

    monkeypatch.setattr(expert_web, '_build_role_agent', build)
    session = SimpleNamespace(
        work_dir=str(project), conversation_id='expansion-test', multi_agent=False,
        session=SimpleNamespace(meta=SimpleNamespace(message_count=0), append=lambda *a: None,
            append_expert_run=lambda *a, **kw: None),
        conversation=ConversationManager(), expert_run_id='',
        expert_control_action='pause',
        context=SimpleNamespace(get=lambda key, default=None: default,
            slots=SimpleNamespace(resolve=lambda key: {})),
        task_manager=SimpleNamespace(list_tasks=lambda: [], mark_notified=lambda ids: None),
        expert_child_event_callback=None, status_payload=lambda: {}, tasks_payload=lambda: [])

    async def send(event):
        events.append(copy.deepcopy(event))
        if event.get('type') == 'ask_user' and answers is not None:
            session._ask_user_request.future.set_result(answers)

    kwargs = dict(session=session,
        registry_service=SimpleNamespace(catalog=AsyncMock(return_value={'providers': []}),
            resolve=lambda model: None),
        profile_store=SimpleNamespace(get=lambda _: _profile()), flow_store=flows,
        plan_store=PlanSessionStore(tmp_path / 'plans.json'), run_store=runs)
    return project, flows, runs, events, agents, session, kwargs, SimpleNamespace(send_json=send)


@pytest.mark.asyncio
async def test_accepted_proposal_executes_micrograph_and_completes_run(tmp_path, monkeypatch):
    project, flows, runs, events, agents, session, kwargs, ws = _setup(
        tmp_path, monkeypatch, _proposal('implement'))
    await asyncio.wait_for(expert_web.run_expert_chat(
        ws, '实现节点任务', confirmed_new_flow=flows.get(project), **kwargs), 30)

    run_id = next(e['run_id'] for e in events if e.get('run_id'))
    saved = runs.load(run_id)
    assert saved['locked_flow'] == flows.get(project).as_dict()
    record = saved['node_expansions']['implement']
    assert record['status'] == 'completed' and record['expansion_id'].startswith('mx-')
    assert record['input_revision'] == saved['node_states']['implement']['input_revision']
    assert record['node_attempt'] == saved['node_attempts']['implement'] == 1
    assert saved['node_states']['implement']['status'] == 'completed'
    assert saved['status'] == 'completed', saved['node_states'].get('done')
    assert saved['node_states']['done']['status'] == 'completed'
    assert agents[0].executed == ['ProposeNodeExpansion']
    child_ids = record['child_node_ids']
    assert child_ids == [
        f"implement::{record['expansion_id']}::prep",
        f"implement::{record['expansion_id']}::write",
    ]
    assert all(saved['node_states'][node_id]['status'] == 'completed'
               for node_id in child_ids)
    assert record['join_handoff']['status'] == 'completed'
    assert len(record['join_handoff']['children']) == 2
    proposed = next(e for e in events if e['type'] == 'expert_node_expansion_proposed')
    assert proposed['expansion_id'] == record['expansion_id']
    assert proposed['proposal']['nodes'][0]['node_id'] == 'prep'


@pytest.mark.asyncio
async def test_rejected_proposal_asks_then_pauses_without_flat_fallback(tmp_path, monkeypatch):
    answers = {'action': '暂停并保留进度', 'direction': '拆成两个不重叠输出'}
    project, flows, runs, events, agents, session, kwargs, ws = _setup(
        tmp_path, monkeypatch, _proposal('other-node'), answers=answers)
    await asyncio.wait_for(expert_web.run_expert_chat(
        ws, '实现节点任务', confirmed_new_flow=flows.get(project), **kwargs), 10)

    run_id = next(e['run_id'] for e in events if e.get('run_id'))
    saved = runs.load(run_id)
    assert saved['status'] == 'paused'
    record = saved['node_expansions']['implement']
    assert record['status'] == 'rejected'
    assert record['rejection']['field'] == 'parent_node_id'
    assert record['proposal']['parent_node_id'] == 'other-node'
    assert record['user_response'] == answers
    assert saved['node_states']['implement']['status'] == 'paused'
    ask = next(e for e in events if e['type'] == 'ask_user')
    assert ask['tool'] == EXPANSION_REJECTION_TOOL
    assert 'parent_node_id' in ask['questions'][0]['message']
    assert 'done' not in saved['node_states']
    assert not saved.get('children')
    tool_events = [e for e in events if e.get('type') == 'expert_tool']
    assert [e['name'] for e in tool_events] == ['ProposeNodeExpansion']
