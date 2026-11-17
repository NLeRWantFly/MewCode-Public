"""Real host handoff/assistance boundaries with deterministic model events."""
import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from mewcode.agent import StreamText, ToolResultEvent, ToolUseEvent, TurnComplete
from mewcode.conversation import ConversationManager
from mewcode.expert_group import ExpertExecutionPolicy, ExpertGroupOrchestrator
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.assistance import AssistanceCoordinator, AssistanceRequest, RunContext
from mewcode.experts.flow import ExpertFlowSpec, ExpertFlowStore
from mewcode.experts.waits import BranchWaitPaused
from mewcode.orchestration.plan_session import PlanSessionStore
from mewcode.tools.request_assistance import RequestAssistance, RequestAssistanceParams, SubmitAssistanceParams
from mewcode.tools.submit_handoff import SubmitHandoffParams
from tests.test_expert_children import FakeSession, make_registry
from tests.test_expert_flow import _profile


def test_scenario_guidance_matches_enabled_tools_and_helper_lifecycle():
    from mewcode.experts.delivery import tool_scenario_prompt
    readonly = tool_scenario_prompt('executor', ['ReadStageResult', 'SubmitHandoff'])
    assert 'ReadStageResult' in readonly and 'SubmitHandoff' in readonly
    assert all(name not in readonly for name in ['LoadSkill', 'FindSkills', 'ToolSearch', 'RequestAssistance'])
    helper = tool_scenario_prompt('manager', ['SubmitAssistanceReceipt', 'SubmitHandoff'], assistance_session=True)
    assert '协助回执' in helper and '先交回执再交接' in helper
    assert 'RequestAssistance' not in helper
    audit = tool_scenario_prompt('auditor', ['SubmitLoopDecision', 'ReviewWorkQueue'])
    assert '循环审查' in audit and 'ReviewWorkQueue' in audit
    assert 'SubmitHandoff' not in audit and 'SubmitVerdict' not in audit


def test_public_assistance_schema_explains_the_required_authorization_request():
    from mewcode.experts.authorization_ledger import OPERATIONS
    schema = RequestAssistance().get_schema()['input_schema']
    item = schema['properties']['scope_requests']['items']
    request = schema['$defs'][item['$ref'].rsplit('/', 1)[-1]]
    assert {'operation_class', 'scope'} <= set(request['required'])
    assert set(request['properties']['operation_class']['enum']) == OPERATIONS
    assert 'command_prefix' in request['properties']['scope']['description']
    row = {'operation_class': 'other', 'scope': {'tools': ['ReadFile'], 'paths': ['config/settings.toml']},
           'reason': 'Only read a redacted configuration summary'}
    params = RequestAssistanceParams(kind='investigation', need='Inspect configuration', scope_requests=[row])
    assert params.model_dump()['scope_requests'] == [row]
    receipt = SubmitAssistanceParams(next_step='ask_human', notes='Specific scope needed', authority_needed=[row])
    assert receipt.model_dump()['authority_needed'] == [row]


@pytest.mark.parametrize('row', [
    {'scope': {'paths': ['config']}},
    {'operation_class': 'read_config', 'scope': {'paths': ['config']}},
    {'operation_class': 'other', 'scope': {'paths': ['*']}},
    {'operation_class': 'other', 'scope': {'unsupported': ['config']}},
    {'operation_class': 'network', 'scope': {'tools': ['WebFetch']}},
    {'operation_class': 'other', 'scope': {'epoch': True}},
])
def test_exposed_authorization_schema_keeps_existing_scope_checks(row):
    with pytest.raises(ValidationError):
        RequestAssistanceParams(kind='investigation', need='Inspect configuration', scope_requests=[row])
    with pytest.raises(ValidationError):
        SubmitAssistanceParams(next_step='ask_human', notes='Scope needed', authority_needed=[row])


@pytest.mark.asyncio
@pytest.mark.parametrize('request_assistance,permission_mode', [
    (True, 'auto'), (True, 'bypassPermissions'), (True, 'default'), (False, 'auto')])
async def test_web_yield_and_helper_preflight_use_actual_parent_scope(tmp_path, monkeypatch, request_assistance, permission_mode):
    from mewcode import expert_web
    # This text-only fixture has no external visual-review provider. Keep the
    # developer machine's saved model settings out of its fake registry.
    monkeypatch.setattr('mewcode.permissions.review_client.load_review_model_settings', lambda: {})

    project = tmp_path / 'project'
    project.mkdir()
    flow = ExpertFlowSpec.from_value({'flow_id': 'handoff', 'finalizer_node_id': 'work', 'edges': [],
        'nodes': [{'node_id': 'work', 'role': 'planner', 'tools': ['ReadFile'], 'capabilities': {}}],
        'meta': {'task_design': {'task_id': 'fixture'}}})
    runs, flows = ExpertRunStore(tmp_path / 'runs'), ExpertFlowStore(tmp_path / 'flows.json')
    calls, events, verified = [], [], []

    class FakeAgent:
        client = SimpleNamespace(expose_reasoning=False)
        total_input_tokens = total_output_tokens = 1
        max_iterations = 40

        def __init__(self, role, policy):
            self.policy = policy
            self.registry = expert_web._role_registry(FakeSession(make_registry('ReadFile')), role, policy)
            self.permission_checker = expert_web._expert_permission_checker(runtime, str(project))

        def set_review_authorization_context(self, context):
            pass

        async def run(self, conversation):
            calls.append(self.policy.execution_node_id)
            if self.policy.assistance_session:
                assert self.policy.authorization_node_id == 'work'
                # Preflight does not create a helper grant or allow new effects.
                assert self.manifest_guard.node_id == 'work'
                from mewcode.kernel.capabilities import call_gap
                manifest = runs.load(runtime.expert_run_id)['authorization_manifest']
                assert self.policy.execution_node_id not in manifest['nodes']
                assert call_gap(manifest, 'work', 'WriteFile', {'file_path': str(project / 'extra.txt')})
                params = SubmitAssistanceParams(next_step='resume', notes='Read-only diagnosis finished')
                result = await self.registry.get('SubmitAssistanceReceipt').execute(params)
                assert not result.is_error
                assert not (await self.registry.get('SubmitHandoff').execute(
                    SubmitHandoffParams(summary='Diagnosis delivered', inline_outputs=[
                        {'name': 'Findings', 'content': 'Service must be tested at its prepared execution stage.'}]))).is_error
            elif request_assistance:
                params = RequestAssistanceParams(kind='capability', need='Read-only service diagnosis', requested_tools=['ReadFile'])
                yield ToolUseEvent('RequestAssistance', 'request-1', params.model_dump())
                result = await self.registry.get('RequestAssistance').execute(params)
                assert not result.is_error
                yield ToolResultEvent('request-1', 'RequestAssistance', result.output, False, 0.01)
                yield TurnComplete(1)
                pytest.fail('pending assistance must yield before another model response')
            conversation.add_assistant_message('Retained findings')
            yield StreamText('Retained findings')

    def build(_session, _registry, role, _model, policy, _verdict):
        return FakeAgent(role, policy), None, SimpleNamespace(conversation=ConversationManager(), aclose=AsyncMock())

    class Harness:
        def __init__(self, **callbacks):
            self.callbacks = callbacks

        async def run(self, *args, **kwargs):
            run_id = kwargs['run_id']
            node = kwargs['flow'].node_map['work']
            policy = ExpertExecutionPolicy('stable', False, 0, node_tools=('ReadFile',), input_revision='same')
            result = await self.callbacks['node_runner'](node, _profile().planner, 'Prepare findings', policy)
            if not request_assistance:
                assert result.status == 'needs_attention' and result.error_kind == 'handoff_format'
                assert result.handoff['blocker_kind'] == 'delivery_format'
                assert 'SubmitHandoff' in result.text
            else:
                assert result.status == 'needs_attention' and result.error_kind == 'assistance_requested'
                assert not result.handoff.get('blockers') and '尚未提交 SubmitHandoff' not in result.text
                saved = runs.load(run_id)['node_states']['work']
                assert saved['handoff']['status'] == 'needs_attention'
                assert not saved['handoff'].get('blockers')
                request = saved['assistance_request']
                scheduler = self.callbacks['recovery_handler']
                assert scheduler.primary_repair is not None
                decision = await scheduler(node=node, result=result, policy=policy,
                    allowed=['continue_checkpoint', 'diagnose_path'], host_facts={
                        'trigger': 'assistance_requested', 'preserve_collaboration_contract': True, 'defer_human': True})
                assert decision['action'] == 'diagnose_path', decision
                assert decision['assistance_receipt']['next_step'] == 'resume'
                helper_id = 'work__assist_' + request['request_id'][:12]
                current = runs.load(run_id)
                assert current['node_states']['work']['assistance_request']['status'] == 'completed'
                assert len(current['node_states']['work']['assistance_receipts']) == 1
                assert expert_web._inherited_permission_mode(runtime).value == permission_mode
                assert current['stage_preflight'][helper_id] == []
                assert current['node_states'][helper_id]['handoff']['status'] == 'completed'
                assert current['node_states']['work']['handoff']['status'] != 'completed'
                assert calls == ['work', helper_id]
                # A transient helper without a bound parent remains blocked.
                unknown = replace(node, node_id='unbound-helper')
                blocked = await self.callbacks['node_runner'](unknown, _profile().manager, '',
                    replace(policy, assistance_session=True, stage_inputs={}))
                assert blocked.error_kind == 'environment_fault'
                assert len(calls) == 2
            verified.append(True)
            raise RuntimeError('fixture stopped after verified boundaries')

    runtime = SimpleNamespace(work_dir=str(project), conversation_id='fixture', multi_agent=False,
        session=SimpleNamespace(meta=SimpleNamespace(message_count=0), append=lambda *a: None,
                                append_expert_run=lambda *a, **kw: None),
        conversation=ConversationManager(), expert_run_id='', expert_control_action='pause',
        context=SimpleNamespace(get=lambda key, default=None: default, slots=SimpleNamespace(resolve=lambda key: {})),
        task_manager=SimpleNamespace(list_tasks=lambda: [], mark_notified=lambda ids: None),
        expert_child_event_callback=None, status_payload=lambda: {}, tasks_payload=lambda: [])
    from mewcode.permissions.policy import build_permission_checker
    from mewcode.permissions import PermissionMode
    primary = _profile().manager
    runtime.agent = SimpleNamespace(permission_checker=build_permission_checker(project, mode=PermissionMode(permission_mode)),
                                    client=SimpleNamespace(effort=primary.effort))
    runtime.slot = SimpleNamespace(key=primary.provider_key, provider=SimpleNamespace(model=primary.model_id))
    monkeypatch.setattr(expert_web, '_build_role_agent', build)
    monkeypatch.setattr(expert_web, 'ExpertGroupOrchestrator', Harness)
    await asyncio.wait_for(expert_web.run_expert_chat(SimpleNamespace(send_json=AsyncMock(side_effect=events.append)),
        'Prepare findings', session=runtime, confirmed_new_flow=flow,
        registry_service=SimpleNamespace(catalog=AsyncMock(return_value={'providers': []}), resolve=lambda model: None),
        profile_store=SimpleNamespace(get=lambda _: _profile()), flow_store=flows,
        plan_store=PlanSessionStore(tmp_path / 'plans.json'), run_store=runs), 120)
    assert verified == [True], events[-5:]


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,reason', [
    ('system_recovery_blocked', '协助预检失败，尚未启动模型。'),
    ('human_gate_waiting', '当前方案需要用户确认范围。'),
])
async def test_paused_graph_preserves_the_actual_node_reason(tmp_path, kind, reason):
    flow = ExpertFlowSpec.from_value({'flow_id': 'paused', 'finalizer_node_id': 'done',
        'nodes': [{'node_id': 'work', 'role': 'planner', 'label': '核对来源'},
                  {'node_id': 'done', 'role': 'manager'}],
        'edges': [{'from': 'work', 'to': 'done'}]})
    calls, events = [], []
    async def runner(node, *args):
        calls.append(node.node_id)
        raise BranchWaitPaused(node.node_id, reason=reason, error_kind=kind)
    result = await ExpertGroupOrchestrator(runner=lambda *_: None, node_runner=runner, archive_root=tmp_path,
        event_sink=lambda event: events.append(copy.deepcopy(event))).run('Task', _profile(), {}, strategy='stable', flow=flow)
    assert calls == ['work'] and result.status == 'paused'
    assert reason in result.final_summary and '核对来源' in result.final_summary
    assert '独立分支已执行' not in result.final_summary and '其余分支等待用户决定' not in result.final_summary
    assert next(e for e in events if e.get('phase') == 'branch_paused')['reason'] == kind
