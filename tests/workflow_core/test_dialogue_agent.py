from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.agent import Agent
from mewcode.conversation import ConversationManager
from mewcode.permissions import PermissionMode
from mewcode.permissions.policy import build_permission_checker
from mewcode.permissions.reviewer import SafetyReviewResult
from mewcode.tools import ToolRegistry
from mewcode.tools.base import TextDelta, ToolCallComplete, StreamEnd
from mewcode.tools.write_file import WriteFile
from mewcode.workflow_core.runtime import Core, MemoryBackend
from mewcode.workflow_core.state import fresh
from mewcode.workflow_core.web import AgentExecutor
from tests.test_agent import MockLLMClient
from tests.workflow_core.test_runtime import graph


@pytest.mark.asyncio
async def test_migrated_loop_accepts_legacy_continue_and_exit_without_an_extra_round(tmp_path, monkeypatch):
    from mewcode import expert_web
    from mewcode.workflow_core.migration import graph_from_flow
    raw = dict(nodes=[dict(node_id=key, role='auditor' if key == 'check' else 'executor')
                      for key in ['make', 'check', 'end']],
        edges=[dict(from_node=a, to_node=b) for a, b in [('make', 'check'), ('check', 'end')]],
        meta={'loops': [dict(loop_id='pilot', node_ids=['make', 'check'], entry_node_id='make', check_node_id='check')]})
    backend = MemoryBackend(fresh(graph_from_flow(raw)))
    calls = []
    def build(session, registry, role, model, policy):
        attempt = backend.read()['attempts'][policy.input_revision]
        calls.append((policy.execution_node_id, attempt['iteration']))
        value = 'CONTINUE' if attempt['iteration'] == [0] else 'EXIT'
        text = '{"value":"' + value + '","reason":"checked"}' if policy.execution_node_id.endswith('/check') else 'done'
        return make_agent(tmp_path, [response(text)])
    monkeypatch.setattr(expert_web, '_build_role_agent', build)
    executor = AgentExecutor(SimpleNamespace(work_dir=str(tmp_path)), None,
        SimpleNamespace(root=tmp_path / 'runs', load=lambda _: {}), 'legacy-loop',
        {'executor': dict(provider_key='offline', model_id='offline', effort='max')}, AsyncMock())
    result = await Core(backend, executor).run()
    assert result['status'] == 'completed'
    assert calls == [('loop_pilot/make', [0]), ('loop_pilot/check', [0]),
                     ('loop_pilot/make', [1]), ('loop_pilot/check', [1]), ('end', [])]
    assert [row['check']['value'] for row in result['nodes']['loop_pilot']['loop']['results']] == [False, True]


def response(text):
    return [TextDelta(text), StreamEnd(stop_reason='end_turn', input_tokens=1, output_tokens=1)]


def call(identity, tool, args):
    return [ToolCallComplete(identity, tool, args), StreamEnd(stop_reason='tool_use', input_tokens=1, output_tokens=1)]


def make_agent(tmp_path, responses):
    registry = ToolRegistry()
    registry.register(WriteFile())
    agent = Agent(client=MockLLMClient(responses), registry=registry, protocol='anthropic',
        permission_checker=build_permission_checker(str(tmp_path), mode=PermissionMode.ACCEPT_EDITS), work_dir=str(tmp_path))
    agent.safety_reviewer.review = AsyncMock(return_value=SafetyReviewResult(decision='allow', risk='low', reason='Test output'))
    runtime = SimpleNamespace(conversation=ConversationManager(), aclose=AsyncMock())
    return agent, None, runtime


@pytest.mark.asyncio
async def test_real_agent_peer_tools_yield_and_resume_with_the_reply(tmp_path, monkeypatch):
    from mewcode import expert_web
    from mewcode.expert_runs import serialize_messages
    backend = MemoryBackend(fresh(graph(['source', 'consumer'], [('source', 'consumer', 'always')])))
    invocations, histories = [], []
    def build(session, registry, role, model, policy):
        attempt = backend.read()['attempts'][policy.input_revision]
        invocations.append((policy.execution_node_id, bool(attempt.get('request_id'))))
        if attempt.get('request_id'):
            responses = [response('The missing fact is 42.')]
        elif policy.execution_node_id == 'source':
            responses = [response('Original source delivery.')]
        elif attempt.get('messages'):
            responses = [response('Used the reply to finish the consumer.')]
        else:
            responses = [call('send', 'SendNodeMessage', {'node_id': 'source', 'message': 'Explain missing fact', 'request_id': 'ask-source'}),
                         call('wait', 'WaitForNodeReply', {'request_id': 'ask-source'}),
                         response('MUST NOT RUN BEFORE THE REPLY')]
        built = make_agent(Path(policy.work_dir or tmp_path), responses)
        histories.append(built[2].conversation)
        return built
    monkeypatch.setattr(expert_web, '_build_role_agent', build)
    store = SimpleNamespace(root=tmp_path / 'runs', load=lambda _: {})
    executor = AgentExecutor(SimpleNamespace(work_dir=str(tmp_path)), None, store, 'offline-dialogue',
        {'executor': dict(provider_key='offline', model_id='offline', effort='max')}, AsyncMock())
    result = await Core(backend, executor, concurrency=1).run()
    assert result['status'] == 'completed', result['nodes']
    assert invocations == [('source', False), ('consumer', False), ('source', True), ('consumer', False)]
    assert result['node_requests']['ask-source']['status'] == 'completed'
    source_history = str(serialize_messages(histories[2].history))
    resumed_history = str(serialize_messages(histories[3].history))
    assert 'Original source delivery.' in source_history and 'Explain missing fact' in source_history
    assert 'The missing fact is 42.' in resumed_history
    assert 'MUST NOT RUN BEFORE THE REPLY' not in resumed_history


@pytest.mark.asyncio
async def test_real_agent_revision_writes_copy_and_publishes_without_changing_old_file(tmp_path, monkeypatch):
    from mewcode import expert_web
    backend = MemoryBackend(fresh(graph(['producer'])))
    agents = []
    def build(session, registry, role, model, policy):
        attempt = backend.read()['attempts'][policy.input_revision]
        if policy.parallel_readonly:
            responses = [response('Explanation only.')]
        else:
            content = 'fixed' if attempt.get('request_id') else 'original'
            responses = [call('write', 'WriteFile', {'file_path': 'page.txt', 'content': content}), response(content)]
        built = make_agent(Path(policy.work_dir or tmp_path), responses)
        if policy.expansion_write_paths:
            from mewcode.experts.node_expansion import ExpansionPathSandbox
            built[0].permission_checker.sandbox = ExpansionPathSandbox(
                built[0].permission_checker.sandbox, policy.expansion_write_paths)
        agents.append(built[0])
        return built
    monkeypatch.setattr(expert_web, '_build_role_agent', build)
    store = SimpleNamespace(root=tmp_path / 'runs', load=lambda _: {})
    executor = AgentExecutor(SimpleNamespace(work_dir=str(tmp_path)), None, store, 'offline-revision',
        {'executor': dict(provider_key='offline', model_id='offline', effort='max')}, AsyncMock())
    await Core(backend, executor).run()
    core = Core(backend, executor)
    request = dict(node_id='producer', kind='revise', message='Correct this page', request_id='revision')
    core.send_message(request)
    result = await core.run()
    assert result['node_requests']['revision']['status'] == 'completed', result['node_requests']['revision']
    assert (tmp_path / 'page.txt').read_text() == 'original'
    versions = list(result['deliveries'].values())
    assert len(versions) == 2
    assert len(versions[-1]['result']['artifacts']) == 1
    assert Path(versions[-1]['result']['artifacts'][0]['stored_path']).read_text() == 'fixed'
    assert Path(agents[1].work_dir).is_relative_to(tmp_path / '.mewcode' / 'workflow-revisions')
    assert core.send_message(request)['id'] == 'revision'  # idempotent after the base version advances
    core.send_message(dict(node_id='producer', message='Explain the fix', request_id='explain'))
    result = await core.run()
    assert result['node_requests']['explain']['status'] == 'completed'
    assert not agents[-1].registry.is_enabled('WriteFile')
    assert len(result['deliveries']) == 2


def test_new_reply_snapshot_is_readable_without_replacing_reader_configuration(tmp_path):
    import os
    from mewcode.tools.read_audit_artifact import ReadAuditArtifact
    from mewcode.workflow_core.revisions import grant_reads
    project = tmp_path / 'project'
    project.mkdir()
    allowed = tmp_path / 'runs' / 'run'
    allowed.mkdir(parents=True)
    snapshot = allowed / 'new.txt'
    snapshot.write_text('new revision', encoding='utf-8')
    agent, _, _ = make_agent(project, [])
    agent.permission_checker = build_permission_checker(str(project), writable_scope=[project])
    reader = ReadAuditArtifact([str(project)], vision_ok=False)
    agent.registry.register(reader)
    assert os.path.normcase(str(snapshot)) not in agent.permission_checker.sandbox.file_grants
    grant_reads(agent, {'delivery': {'artifacts': [{'stored_path': str(snapshot)}]}}, allowed)
    assert agent.permission_checker.sandbox.check(str(snapshot), read_only=True)[0]
    assert agent.permission_checker.sandbox.file_grants[os.path.normcase(str(snapshot))] is False
    assert not agent.permission_checker.sandbox.check(str(snapshot), read_only=False)[0]
    updated = agent.registry.get('ReadAuditArtifact')
    assert not updated.vision_ok and updated.ledger is reader.ledger
    assert snapshot in updated.roots and snapshot not in reader.roots
