"""Real web adapter, stage preflight and handoff/verdict tools; offline actors."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from mewcode.agent import Agent, StreamText, ToolResultEvent, ToolUseEvent
from mewcode.conversation import ConversationManager
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.flow import ExpertFlowSpec, ExpertFlowStore
from mewcode.model_registry import ModelRef
from mewcode.orchestration.plan_session import PlanSessionStore
from mewcode.orchestration.task_design import TaskDesign
from mewcode.tools.submit_handoff import SubmitHandoffParams
from mewcode.tools.submit_verdict import SubmitVerdictParams
from tests.test_collaboration_mode import CATALOG, PROFILE, flow
from tests.test_expert_children import FakeSession, make_registry


@pytest.mark.asyncio
@pytest.mark.parametrize("command_policy", [None, "allow", "deny", "expansion", "rolling", "patch", "stream", "scheduling", "turn_finish", "final_audit", "final_draft", "worktree", "node_binding", "provider_cooldown", "decision_evidence", "review_intervention", "soft_deadline"])
async def test_web_execution_uses_independent_readonly_acceptance_and_one_start(tmp_path, monkeypatch, command_policy):
    from mewcode import expert_web
    expansion = command_policy == "expansion"
    rolling = command_policy == "rolling"
    patch = command_policy == 'patch'
    isolated = command_policy == 'worktree'
    scheduling = command_policy == 'scheduling'
    stream = command_policy in {'stream', 'scheduling'}
    turn_finish = command_policy == 'turn_finish'
    decision_evidence = command_policy == 'decision_evidence'
    intervention = command_policy == 'review_intervention'
    final_audit = command_policy in {'final_audit', 'final_draft', 'decision_evidence'}
    final_draft = command_policy == 'final_draft'
    node_binding = command_policy == 'node_binding'
    provider_cooldown = command_policy == 'provider_cooldown'
    soft_deadline = command_policy == 'soft_deadline'
    # Offline role fixtures must not inherit a machine's configured observer.
    monkeypatch.setattr('mewcode.permissions.review_client.load_review_model_settings', lambda: {})
    if expansion or rolling or patch or stream or turn_finish or final_audit or isolated or node_binding or provider_cooldown or intervention or soft_deadline:
        command_policy = None
    if command_policy:
        # The external command environment is synthetic; policy, manifest,
        # execution gateway and receipt recording remain real.
        from mewcode.sandbox import native_gate
        monkeypatch.setattr(native_gate, "native_gate_status", lambda: SimpleNamespace(ready=True, reason="Synthetic command environment"))
        from mewcode.sandbox import srt_runtime
        monkeypatch.setattr(srt_runtime, "runtime_status", lambda: {"ready": True, "reason": "Synthetic command environment"})
        from mewcode.kernel import preflight
        monkeypatch.setattr(preflight, "powershell_quotes", lambda: (True, "Synthetic quote probe"))
    project = tmp_path / "project"
    project.mkdir()
    design = TaskDesign(project, "fixture")
    created = design.start("execute", orchestration_mode="collaborative")
    ready = design.transition(created, "ready", build_request={"id": "build", "state": "saved", "epoch": 1})
    claimed = design.claim_execution(ready)
    token = design.execution_token(claimed)
    expected_run = design.execution_run_id(claimed)
    raw = flow().as_dict()
    if intervention:
        from tests.test_automatic_review_repair import review_flow
        raw = review_flow().as_dict()
    if rolling:
        from tests.test_autonomous_rolling_runtime import rolling_flow
        raw = rolling_flow().as_dict()
    if patch:
        from tests.test_autonomous_patch import patch_flow
        raw = patch_flow().as_dict()
    raw["meta"]["task_design"] = {"task_id": claimed["task_id"], "orchestration_mode": "collaborative"}
    raw["meta"]["collaboration_spec"]["task_id"] = claimed["task_id"]
    if final_audit:
        raw['nodes'][-1]['role'] = 'auditor'
    for node in raw["nodes"]:
        node.update(tools=["ReadFile"], capabilities={})
        if intervention and node['node_id'] == 'audit':
            from mewcode.orchestration.review_intervention import READ_TOOLS
            node['tools'] = sorted(READ_TOOLS)
    if expansion:
        raw["nodes"][0].update(tools=["ReadFile", "ProposeNodeExpansion"], capabilities={"write_paths": ["work"]})
    if patch:
        raw['nodes'][0].update(tools=['ReadFile', 'WriteFile'], capabilities={'write_paths': ['report.json']})
    if isolated:
        for node in raw['nodes']:
            node.update(tools=['ReadFile', 'WriteFile'], capabilities={'write_paths': ['report.json']})
            raw['meta']['collaboration_spec']['document']['entries'].append({
                'id': node['node_id'], 'kind': 'node', 'body': '交付隔离文件', 'data': {
                    'contract': {'workspace_mode': 'worktree', 'checks': [{'id': node['node_id'] + '_file',
                        'kind': 'file_exists', 'version': 1, 'path': 'report.json'}]}}})
    if command_policy:
        raw["nodes"][0].update(tools=["ReadFile", "Bash"], capabilities={
            "command_execution": True, "command_prefixes": [["python", "check.py"]]})
        raw["meta"]["collaboration_spec"]["document"]["entries"].append({
            "id": "work", "kind": "node", "body": "Run explicit check", "data": {"contract": {"checks": [
                {"id": "command_check", "version": 1, "kind": "command", "command": "python check.py"}]}}})
    selected = ExpertFlowSpec.from_value(raw)
    # Exercise the same transactional store as the desktop web adapter.
    store, flows = ExpertRunStore(tmp_path / "runs", storage="sqlite"), ExpertFlowStore(tmp_path / "flows.json")
    chosen = ModelRef(PROFILE.executor.provider_key, PROFILE.executor.model_id, 'high')
    if node_binding:
        create = store.create
        def create_with_binding(**kwargs):
            create(**kwargs)
            return store.mutate(kwargs['run_id'], lambda r: r.setdefault('locked_models', {}).update(work=chosen.as_dict()), durable=True)
        monkeypatch.setattr(store, 'create', create_with_binding)
    flows.put(project, selected)
    invoked, events, command_calls, agent_errors = [], [], [], []

    class OfflineAgent(Agent):
        client = SimpleNamespace(expose_reasoning=False)
        total_input_tokens = total_output_tokens = 1
        max_iterations = 40

        def __init__(self, role, model, policy, verdict):
            from mewcode.permissions import PermissionMode
            from mewcode.permissions.policy import build_permission_checker
            from mewcode.permissions.rules import Rule
            from mewcode.tools.bash import Bash
            from mewcode.tools.base import ToolResult
            from tests.test_agent import MockLLMClient
            base_registry = make_registry('ReadFile')
            if patch or isolated:
                from mewcode.tools import ToolRegistry
                from mewcode.tools.read_file import ReadFile
                from mewcode.tools.write_file import WriteFile
                from mewcode.tools.file_state_cache import FileStateCache
                cache = FileStateCache()
                base_registry = ToolRegistry()
                base_registry.register(ReadFile(file_state_cache=cache))
                base_registry.register(WriteFile(file_state_cache=cache))
            registry = expert_web._role_registry(FakeSession(base_registry), role, policy, verdict)
            if intervention:
                from mewcode.tools.read_authorizations import ReadAuthorizations
                registry.register(ReadAuthorizations(policy.authorization_reader))
            if role == "executor" and command_policy:
                tool = Bash()
                async def observed_command(params):
                    command_calls.append(params.command)
                    return ToolResult("Synthetic external check passed", host_observation={"command_execution": {
                        "status": "completed", "command_started": True, "exit_code": 0}})
                monkeypatch.setattr(tool, "execute", observed_command)
                registry.register(tool)
            execution_root = Path(policy.work_dir) if patch or isolated else project
            checker = build_permission_checker(execution_root, mode=PermissionMode.AUTO)
            if patch and (policy.candidate_context or {}).get('collaboration_patch'):
                from mewcode.kernel.shadow_policy import bind_collaboration_candidate
                bind_collaboration_candidate(checker, session.agent.permission_checker, policy.candidate_context)
            if isolated:
                from mewcode.kernel.shadow_policy import bind_collaboration_worktree
                bind_collaboration_worktree(checker, session.agent.permission_checker, policy.collaboration_workspace)
            if policy.expansion_holder:
                policy.expansion_holder.context.sandbox = checker.sandbox
                policy.expansion_holder.context.work_dir = project
            checker.rule_engine.append_session_rule(Rule("Bash", "*", command_policy or "deny"))
            super().__init__(MockLLMClient([]), registry, "anthropic", work_dir=str(execution_root), permission_checker=checker)
            from mewcode.permissions.reviewer import SafetyReviewResult
            monkeypatch.setattr(self.safety_reviewer, "review", AsyncMock(return_value=SafetyReviewResult(
                decision="allow", risk="low", reason="Offline reviewer approved this exact synthetic check")))
            self.policy, self.role, self.model = policy, role, model

        def set_review_authorization_context(self, context):
            pass

        async def run(self, conversation):
            invoked.append((self.policy.execution_node_id, self.role, self.model))
            if (decision_evidence or intervention) and self.role == 'executor' and self.policy.execution_node_id == 'work':
                from mewcode.orchestration import ask_gateway
                def answer_pilot(record):
                    row = ask_gateway.prepare(record, 'work', [{'name': 'evidence', 'question': 'Accept pilot evidence?',
                        'options': ['accept_pilot_only', 'decline']}])
                    ask_gateway.answer(record, row['id'], {'evidence': 'accept_pilot_only'})
                store.mutate(expected_run, answer_pilot, durable=True)
            if provider_cooldown:
                from mewcode.client import RateLimitError
                raise RateLimitError('Rate limited. Retry after 1440902s.', retry_after=1440902)
            active_stage = store.load(expected_run)['node_states'][self.policy.execution_node_id]
            assert active_stage['status'] == 'running'
            assert 'completed_at' not in active_stage
            if (soft_deadline and self.policy.execution_node_id == 'work'
                    and sum(node == 'work' for node, _, _ in invoked) == 1):
                from mewcode.agent import ErrorEvent, ToolExecutionStartEvent, TurnComplete
                from mewcode.conversation import ToolUseBlock, ToolResultBlock
                from pydantic import ValidationError
                conversation.add_assistant_message('Reading only the necessary input.',
                    tool_uses=[ToolUseBlock('slice-read', 'ReadFile', {'file_path': 'fixture.txt'})])
                yield ToolUseEvent('ReadFile', 'slice-read', {'file_path': 'fixture.txt'})
                yield ToolExecutionStartEvent('ReadFile', 'slice-read')
                yield ToolResultEvent('slice-read', 'ReadFile', 'saved source bytes', False, .001)
                conversation.add_tool_results_message([ToolResultBlock('slice-read', 'saved source bytes')])
                conversation.add_assistant_message('Source bytes saved; continue the remaining work.')
                invalid = {'summary': 'Source checked; result not delivered yet', 'status': 'needs_attention',
                           'blocker_kind': 'internal_work', 'blockers': ['slice ended']}
                with pytest.raises(ValidationError) as rejected:
                    SubmitHandoffParams.model_validate(invalid)
                feedback = str(rejected.value)
                conversation.add_assistant_message('', tool_uses=[ToolUseBlock('slice-handoff', 'SubmitHandoff', invalid)])
                yield ToolUseEvent('SubmitHandoff', 'slice-handoff', invalid)
                yield ToolResultEvent('slice-handoff', 'SubmitHandoff', feedback, True, .001)
                conversation.add_tool_results_message([ToolResultBlock('slice-handoff', feedback, True)])
                yield TurnComplete(turn=1)
                yield ErrorEvent('slice ended', kind='soft_deadline')
                return
            if soft_deadline and self.policy.execution_node_id == 'work':
                restored = '\n'.join(str(message.content) for message in conversation.history)
                assert 'Source bytes saved; continue the remaining work.' in restored
                assert '从原检查点继续' in restored
                results = {result.tool_use_id: result for message in conversation.history for result in message.tool_results}
                assert results['slice-read'].content == 'saved source bytes'
                assert results['slice-handoff'].is_error and 'open_items' in results['slice-handoff'].content
                assert '最近一次 SubmitHandoff 未被接受' in restored
            if stream:
                from mewcode.agent import ThinkingText
                offset = len(events)
                for _ in range(5):
                    yield ThinkingText('PRIVATE_REASONING_SENTINEL')
                activity = [e for e in events[offset:] if e['type'] == 'expert_activity']
                assert len(activity) == 1, 'Hidden stream must be visible and coalesced'
                assert activity[0]['stream_activity']['phase'] == 'processing'
                assert activity[0]['node_id'] == self.policy.execution_node_id
                assert 'PRIVATE_REASONING_SENTINEL' not in json.dumps(events[offset:])
                yield StreamText('First public chunk. ')
                assert any(e['type'] == 'expert_delta' and e.get('text') == 'First public chunk. ' for e in events[offset:])
                yield StreamText('Second public chunk.')
                if scheduling:
                    import time
                    from mewcode.agent import ToolExecutionStartEvent, UsageEvent
                    from mewcode.kernel.async_commit import durable_call
                    from mewcode.kernel.events import EventJournal
                    from mewcode.kernel.sqlite_state import RunDatabase
                    from mewcode.orchestration.interventions import enqueue_intervention, deliver_interventions
                    def seed_delivered_input(row):
                        enqueue_intervention(row, 'already-delivered', 'Keep checking the original task')
                        deliver_interventions(row, conversation, node_id=self.policy.execution_node_id, node_attempt=1)
                    store.mutate(expected_run, seed_delivered_input)
                    store.mutate(expected_run, lambda row: enqueue_intervention(
                        row, 'new-boundary-message', 'Check the next source'))
                    assert await self.pending_user_input_callback(), 'new mailbox input was not delivered'
                    assert not await self.pending_user_input_callback(), 'mailbox input was delivered twice'
                    mailbox_state = store.read_turn_boundary(expected_run, self.policy.execution_node_id)
                    assert len(mailbox_state['interventions'][-1]['delivered_to']) == 1
                    assert all(not row['enforceable'] for row in mailbox_state['authorizations']['entries'])
                    store.mutate(expected_run, lambda row: enqueue_intervention(
                        row, 'failed-boundary-message', 'Keep this message pending after a failed save'))
                    history_size = len(conversation.history)
                    with monkeypatch.context() as failing_commit:
                        def fail(*args, **kwargs):
                            raise OSError('injected mailbox commit failure')
                        failing_commit.setattr(store, '_commit', fail)
                        with pytest.raises(OSError, match='injected mailbox'):
                            await self.pending_user_input_callback()
                    assert len(conversation.history) == history_size
                    assert await self.pending_user_input_callback(), 'failed mailbox transaction lost its input'
                    class HistoricalContext(list):
                        def __deepcopy__(self, memo):
                            raise AssertionError('tool callback copied historical context')
                    # A real stream checkpoint, mixed usage/start batch and
                    # the web adapter's permission/receipt callbacks all run.
                    private = EventJournal(store.root, expected_run)._sql_load(detached=False)
                    live = store._live[expected_run]
                    private['kernel_attempt_archives'] = HistoricalContext()
                    live['kernel_attempt_archives'] = HistoricalContext()
                    try:
                        # Old mailbox receipts must not trigger any full load
                        # or no-op save on every subsequent model boundary.
                        # A background stream checkpoint may legitimately
                        # advance the run revision while this read awaits I/O.
                        # Assert that the mailbox itself does not redeliver.
                        with monkeypatch.context() as idle_mailbox:
                            delivery_spy = Mock(wraps=deliver_interventions)
                            idle_mailbox.setattr('mewcode.orchestration.interventions.deliver_interventions', delivery_spy)
                            assert not await self.pending_user_input_callback()
                            delivery_spy.assert_not_called()
                        await durable_call(store.flush_stream, expected_run, self.policy.execution_node_id,
                            text='First public chunk. Second public chunk.', reasoning='private',
                            messages=[], stream_activity={'phase': 'generating', 'updated_at': time.time()},
                            return_snapshot=False)
                        yield ToolUseEvent('ReadFile', 'scheduling-read', {'file_path': 'fixture.txt'})
                        yield UsageEvent(1, 1)
                        started = time.monotonic()
                        yield ToolExecutionStartEvent('ReadFile', 'scheduling-read')
                        dispatch = time.monotonic() - started
                        durable = RunDatabase(store.root, expected_run).load()
                        assert durable['node_states'][self.policy.execution_node_id]['tools']['scheduling-read']['status'] == 'running'
                        yield ToolResultEvent('scheduling-read', 'ReadFile', 'observed', False, .001,
                                              dispatch_seconds=dispatch)
                        durable = RunDatabase(store.root, expected_run).load()
                        assert durable['node_states'][self.policy.execution_node_id]['tools']['scheduling-read']['status'] == 'completed'
                    finally:
                        EventJournal(store.root, expected_run)._sql_load(detached=False).pop('kernel_attempt_archives', None)
                        store._live[expected_run].pop('kernel_attempt_archives', None)
                raise asyncio.CancelledError()  # exercise the real pause/checkpoint path
            assert self.manifest_guard is not None
            if not (patch or isolated) or self.role == 'auditor':
                assert self.registry.get("WriteFile") is None
            if self.role == "auditor":
                if not final_audit and not (intervention and self.policy.execution_node_id == 'audit'):
                    assert self.policy.assistance_session and self.policy.parallel_readonly
                from mewcode.kernel.collaboration_graph import effective_flow
                assert self.policy.authorization_node_id in effective_flow(store.load(session.expert_run_id)).node_map
                assert self.registry.get("Agent") is None
                name = "SubmitVerdict"
                params = SubmitVerdictParams(verdict="NEEDS_ATTENTION" if final_draft else "PASS",
                    evidence=[{"kind": "read", "summary": "Checked fixed inline result against the original goal"}],
                    issues=[{'description': '说明措辞可继续完善', 'resolution': 'producer_fix',
                             'acceptance_test': '说明涵盖原要求中的所有步骤'}] if final_draft else [])
                params = params.model_copy(update={'reviewed_sources': dict(self.registry.get(name).required_sources)})
                if intervention:
                    owner = self.policy.authorization_node_id
                    if '::review-' in owner:
                        # Exercise the final registry used by the nested verifier,
                        # not just the investigator's initial tool list.
                        from mewcode.tools.read_authorizations import ReadAuthorizationsParams
                        from mewcode.tools.read_run_archive import ReadRunArchiveParams
                        from mewcode.tools.read_stage_result import ReadStageResultParams
                        for required_tool in ('ReadAuthorizations', 'ReadRunArchive', 'SubmitVerdict'):
                            assert self.registry.is_enabled(required_tool), required_tool
                        for forbidden_tool in ('Bash', 'WriteFile', 'EditFile', 'Agent', 'AskUserQuestion', 'SubmitHandoff'):
                            assert self.registry.get(forbidden_tool) is None, forbidden_tool
                        answer = await self.registry.get('ReadAuthorizations').execute(ReadAuthorizationsParams())
                        assert not answer.is_error and 'accept_pilot_only' in answer.output
                        archive = await self.registry.get('ReadRunArchive').execute(ReadRunArchiveParams(tool_id=owner + '-tool'))
                        assert not archive.is_error and 'SubmitHandoff' in archive.output
                        reader = self.registry.get('ReadStageResult')
                        receipts = await reader.execute(ReadStageResultParams(node_id='@model_receipts:work'))
                        assert not receipts.is_error and 'original-work-receipt' in receipts.output
                        assert '@model_receipts:unrelated' not in reader.sources
                        assert self.registry.get(name).required_sources['@model_receipts'] == reader.revisions['@model_receipts']
                    if self.policy.execution_node_id == 'audit' and sum(n == 'audit' for n, _, _ in invoked) == 1:
                        params = params.model_copy(update={'verdict': 'NEEDS_ATTENTION', 'issues': SubmitVerdictParams(
                            verdict='NEEDS_ATTENTION', issues=[{'issue_id': 'R-1', 'description': 'Inspect the original receipt.',
                                'resolution': 'capability_gap', 'acceptance_test': 'Check the saved receipt.'}]).issues})
                if decision_evidence or intervention and self.registry.get(name).required_decisions:
                    rejected = await self.registry.get(name).execute(params)
                    assert rejected.is_error and 'accept_pilot_only' in rejected.output
                    required = self.registry.get(name).required_decisions
                    assert len(required) == 1
                    params = params.model_copy(update={'reviewed_decisions': SubmitVerdictParams(verdict='PASS',
                        reviewed_decisions={key: {'applicability': 'original_scope_only',
                            'reason': 'The pilot answer exists and remains limited to the pilot, not full upload.'}
                            for key in required}).reviewed_decisions})
            elif expansion and self.policy.execution_node_id == "work":
                from tests.test_autonomous_fragment import _proposal
                from mewcode.tools.propose_node_expansion import ProposeNodeExpansionParams
                name = "ProposeNodeExpansion"
                params = ProposeNodeExpansionParams(proposal=_proposal())
                assert self.policy.collaborative_expansion
            else:
                name = "SubmitHandoff"
                if intervention and '::review-' in self.policy.execution_node_id:
                    assert self.registry.is_enabled('ReadAuthorizations')
                    assert self.registry.get('SubmitVerdict') is None
                    assert self.registry.get('WriteFile') is None and self.registry.get('AskUserQuestion') is None
                params = SubmitHandoffParams(summary="Delivered result", inline_outputs=[{"name": "result", "content": "Complete synthetic result"}])
                if isolated and self.policy.execution_node_id == 'deliver':
                    assert json.loads((Path(self.work_dir) / 'report.json').read_text()) == {'total': 3, 'rows': 2}
                    params = SubmitHandoffParams(summary='已核对隔离交付', artifacts=[{'path': 'report.json'}])
                if (patch or isolated) and self.policy.execution_node_id == 'work':
                    from mewcode.experts.reviewed_tools import invoke_reviewed_tool
                    target = Path(self.work_dir) / 'report.json'
                    async def patch_emit(event):
                        events.append(event)
                    if target.exists():
                        inspected = await invoke_reviewed_tool(self, 'ReadFile', {'file_path': str(target)},
                            session=session, store=store, run_id=expected_run, node_id='work', emit=patch_emit)
                        assert not inspected.is_error, inspected.output
                    work_attempts = sum(node == 'work' and role == 'executor' for node, role, model in invoked)
                    content = json.dumps({'total': 3 if isolated or work_attempts > 1 else 0, 'rows': 2})
                    written = await invoke_reviewed_tool(self, 'WriteFile', {'file_path': str(target), 'content': content},
                        session=session, store=store, run_id=expected_run, node_id='work', emit=patch_emit)
                    assert not written.is_error, written.output
                    params = SubmitHandoffParams(summary='已交付待核对的表格', artifacts=[{'path': 'report.json'}])
            yield ToolUseEvent(name, self.policy.execution_node_id + "-tool", params.model_dump())
            result = await self.registry.get(name).execute(params)
            assert not result.is_error, result.output
            yield ToolResultEvent(self.policy.execution_node_id + "-tool", name, result.output, False, .01)
            conversation.add_assistant_message("Complete synthetic result")
            yield StreamText("Complete synthetic result")
            if turn_finish:
                from mewcode.agent import TurnComplete
                from mewcode.experts.node_ledger import ledger_for_run
                yield TurnComplete(turn=1)
                view = ledger_for_run(store.load(expected_run)).view(self.policy.execution_node_id)
                assert any(fact.payload.get('model_response_completed') for fact in view.history)

    def build(_session, _registry, role, model, policy, verdict=None):
        agent = OfflineAgent(role, model, policy, verdict)
        run = agent.run
        async def observed_run(conversation):
            try:
                async for event in run(conversation):
                    yield event
            except Exception:
                import traceback
                agent_errors.append(traceback.format_exc())
                raise
        agent.run = observed_run
        return agent, None, SimpleNamespace(conversation=ConversationManager(), aclose=AsyncMock())

    session = SimpleNamespace(work_dir=str(project), conversation_id="fixture", multi_agent=False,
        session=SimpleNamespace(meta=SimpleNamespace(message_count=0), append=lambda *a: None, append_expert_run=lambda *a, **kw: None),
        conversation=ConversationManager(), expert_run_id="", expert_control_action="pause",
        context=SimpleNamespace(get=lambda key, default=None: default, slots=SimpleNamespace(resolve=lambda key: {})),
        task_manager=SimpleNamespace(list_tasks=lambda: [], mark_notified=lambda ids: None),
        expert_child_event_callback=None, status_payload=lambda: {}, tasks_payload=lambda: [])
    registry = SimpleNamespace(catalog=AsyncMock(return_value=CATALOG), resolve=lambda ref, **kw: SimpleNamespace(ref=ModelRef.from_value(ref)))
    if isolated:
        from mewcode.permissions.policy import build_permission_checker
        from mewcode.permissions import PermissionMode
        session.agent = SimpleNamespace(permission_checker=build_permission_checker(project, mode=PermissionMode.AUTO))
    if expansion or rolling or patch or intervention:
        from mewcode.kernel import arbitration
        async def independent_graph_review(client, payload, system):
            if intervention:
                if 'intervention_plan' in payload:
                    return {'passed': True, 'reason': 'Read-only investigation remains within the original scope.', 'evidence': ['issue:R-1']}
                from tests.test_review_intervention import investigation_plan
                return investigation_plan()
            if patch:
                from tests.test_autonomous_patch import decision
                return decision(payload)
            if rolling and "fragment" not in payload:
                from tests.test_autonomous_rolling_fragment import _proposal
                return _proposal()
            assert "fragment" in payload
            return {"passed": True, "reason": "目标、权限、验收均保留", "evidence": ["parent_contract", "fragment"]}
        monkeypatch.setattr(arbitration, "ask_client", independent_graph_review)
        session.agent = SimpleNamespace(client=SimpleNamespace(get_usage_recorder=lambda: None), permission_checker=None)
        if patch:
            from mewcode.permissions.policy import build_permission_checker
            from mewcode.permissions import PermissionMode
            session.agent.permission_checker = build_permission_checker(project, mode=PermissionMode.AUTO)
        registry.resolve = lambda ref, **kw: SimpleNamespace(ref=ModelRef.from_value(ref), client=object())
    if intervention:
        from mewcode.usage.ledger import UsageLedger
        from mewcode.usage.models import RequestUsage
        usage = UsageLedger(tmp_path / 'usage')
        for key in ('work', 'unrelated'):
            usage.append(RequestUsage(session_id='fixture', configured_model='route', response_model='original-' + key + '-receipt',
                metadata={'execution_scope': {'run_id': expected_run, 'node_id': key}}))
        session.usage_service = SimpleNamespace(ledger=usage)
    monkeypatch.setattr(expert_web, "_build_role_agent", build)
    ws = SimpleNamespace(send_json=AsyncMock(side_effect=events.append))
    kwargs = dict(session=session, registry_service=registry, profile_store=SimpleNamespace(get=lambda _: PROFILE),
        flow_store=flows, plan_store=PlanSessionStore(tmp_path / "plans.json"), run_store=store,
        confirmed_new_flow=selected, task_design_dispatch=token)
    # This watchdog covers a complete multi-stage workflow and its durable
    # commits; dispatch latency has a separate benchmark and regression case.
    timeout = 300 if expansion or rolling or patch or isolated or intervention or soft_deadline else 120
    await asyncio.wait_for(expert_web.run_expert_chat(ws, "Complete synthetic task", **kwargs), timeout)
    saved = store.load(expected_run)
    assert saved is not None
    actual_status = saved["status"]
    if provider_cooldown:
        from mewcode.provider_cooldown import cooldown_remaining
        assert actual_status == 'blocked' and len(invoked) == 1
        assert cooldown_remaining(saved, PROFILE.executor) > 1440000
        assert not saved['collaboration'].get('fault_recoveries')
        assert any('1440902' in gate['reason'] for gate in saved['collaboration']['recovery_gates'].values())
        assert '等待' in expert_web.expert_resume_model_error(registry, saved)
        return
    if node_binding:
        assert next(model for node, role, model in invoked if node == 'work') == chosen
        assert saved['locked_models']['work'] == chosen.as_dict()
    if stream:
        assert actual_status == 'paused', '\n'.join(agent_errors)
        public = store.public(expected_run)
        assert 'PRIVATE_REASONING_SENTINEL' not in json.dumps(public)
        assert any(e['type'] == 'expert_delta' and e.get('text') == 'First public chunk. Second public chunk.'
                   for e in public['events'])
        assert any(e['type'] == 'expert_stage' and e.get('status') == 'paused' for e in events)
        return
    assert not agent_errors, '\n'.join(agent_errors)
    assert actual_status == ("blocked" if command_policy == "deny" else "completed"), next(
        (row.get("final_summary") for row in reversed(events) if row.get("final_summary")), "No summary")
    assert design.read()["run_id"] == expected_run
    if command_policy == "deny":
        assert command_calls == []
        assert any(node == "work" for node, role, model in invoked)
        assert any(row.get("name") == "Bash" and row.get("is_error") for row in saved["node_states"]["work"].get("tools", {}).values())
        assert not saved["collaboration"]["certificates"]
        assert not any(node == "deliver" for node, role, model in invoked)
        return
    assert command_calls == (["python check.py"] if command_policy else [])
    executed = [node for node, role, model in invoked if role == "executor"]
    if soft_deadline:
        assert executed == ['work', 'work', 'deliver']
        attempts = [row for row in saved['collaboration']['attempts'].values() if row['contract_id'] == 'work']
        assert len(attempts) == 1 and attempts[0]['status'] == 'accepted'
        assert not saved['collaboration'].get('recovery_gates')
        assert any(row['kind'] == 'execution_segment_yielded' and row['reason'] == 'soft_deadline'
                   for row in saved['collaboration']['timeline'])
        assert not any(event['type'] == 'ask_user' for event in events)
    elif patch:
        assert executed == ['work', 'work', 'deliver']
        assert json.loads((project / 'report.json').read_text()) == {'total': 3, 'rows': 2}
        patches = [row['patch'] for row in saved['collaboration']['attempts'].values() if row.get('patch')]
        assert patches == []
        assert sum(row['node_id'] == 'work' and row['invocation']['tool'] == 'WriteFile' and row['state'] == 'succeeded'
                   for row in saved['kernel_calls'].values()) == 2
    elif rolling:
        publication = next(iter(saved["collaboration"]["graph_publications"].values()))
        children = publication["fragment"]["child_ids"]
        assert executed == ["work", "deliver", *children]
        assert saved["locked_flow"] == selected.as_dict()
        assert saved["collaboration"]["milestones"]["m2"]["status"] == "accepted"
        assert all(key in saved["authorization_manifest"]["nodes"] for key in children)
    elif expansion:
        publication = next(iter(saved["collaboration"]["graph_publications"].values()))
        children = publication["fragment"]["child_ids"]
        assert executed == ["work", *children, "deliver"]
        assert saved["locked_flow"] == selected.as_dict()
        assert saved["collaboration"]["runtime_flow"]["version"] == 2
        assert all(key in saved["authorization_manifest"]["nodes"] for key in children)
        assert len([role for node, role, model in invoked if role == "auditor"]) == 1
    elif intervention:
        publication = next(iter(saved['collaboration']['graph_publications'].values()))
        children = publication['fragment']['child_ids']
        assert executed == ['work', *children, 'deliver']
        assert sum(n == 'audit' for n, _, _ in invoked) == 2
        reviews = [n for n, role, _ in invoked if role == 'auditor' and n.startswith(children[0] + '__acceptance_')]
        assert reviews and all(saved['node_states'][key]['verdict']['verdict'] == 'PASS' for key in reviews)
        assert all(len(saved['node_states'][key]['verdict']['reviewed_decisions']) == 1 for key in reviews)
    elif final_audit:
        assert executed == ['work']
        assert [node for node, role, model in invoked if role == 'auditor'] == ['deliver']
        assert saved['node_states']['deliver']['verdict']['verdict'] == ('NEEDS_ATTENTION' if final_draft else 'PASS')
        if decision_evidence:
            reviewed = saved['node_states']['deliver']['verdict']['reviewed_decisions']
            assert len(reviewed) == 1
            assert 'accept_pilot_only' in next(iter(reviewed.values()))['host_record']['answer_excerpt']
    else:
        assert executed == ["work", "deliver"]
        assert len([role for node, role, model in invoked if role == "auditor"]) == 1
    assert saved["collaboration"]["integrations"]["final"]["report"]["passed"] is (not final_draft)
    if isolated:
        assert not (project / 'report.json').exists()
        assert all(row.get('workspace', {}).get('status') == 'sealed'
                   for row in saved['collaboration']['attempts'].values())
        assert sum(row['invocation']['tool'] == 'WriteFile' and row['state'] == 'succeeded'
                   for row in saved['kernel_calls'].values()) == 1
    before = list(invoked)
    await expert_web.run_expert_chat(ws, "Same startup request", **kwargs)
    assert invoked == before and events[-1].get("rejected") is True
    assert len(store.list_for_conversation("fixture")) == 1
