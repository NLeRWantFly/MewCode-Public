"""Both public start/resume routes bind host identity before capability preflight."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import threading

import pytest

from mewcode.conversation import ConversationManager
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.flow import ExpertFlowSpec, ExpertFlowStore
from mewcode.orchestration.plan_session import PlanSessionStore
from tests.test_expert_flow import _profile


@pytest.mark.asyncio
async def test_start_and_resume_use_the_same_preflight_after_host_binding(tmp_path, monkeypatch):
    from mewcode import expert_web
    from mewcode.kernel import preflight
    from mewcode.experts.authorization_ledger import record_authorization
    monkeypatch.setattr('mewcode.permissions.review_client.load_review_model_settings', lambda: {})
    project = tmp_path / 'project'
    project.mkdir()
    runs = ExpertRunStore(tmp_path / 'runs')
    event_loop_thread = threading.get_ident()
    for method in ('prepare_authorization_manifest', 'resume'):
        original = getattr(runs, method)
        def checked(*args, _original=original, **kwargs):
            assert threading.get_ident() != event_loop_thread, 'recovery blocked the event loop'
            return _original(*args, **kwargs)
        monkeypatch.setattr(runs, method, checked)
    flows = ExpertFlowStore(tmp_path / 'flows.json')
    flow = ExpertFlowSpec.from_value({'flow_id': 'inventory', 'label': 'Inventory',
        'finalizer_node_id': 'work', 'edges': [], 'nodes': [{'node_id': 'work', 'role': 'executor'}]})
    flows.put(project, flow)
    durable = SimpleNamespace(meta=SimpleNamespace(message_count=0), append=lambda *a: None,
                              append_expert_run=lambda *a, **kw: None)
    session = SimpleNamespace(work_dir=str(project), conversation_id='fixture', multi_agent=False,
        session=durable, conversation=ConversationManager(), expert_run_id='', expert_control_action='pause',
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
    builds = []
    monkeypatch.setattr(expert_web, '_build_role_agent', lambda *a: builds.append(a))
    await expert_web.run_expert_chat(SimpleNamespace(send_json=send), '清点书目', confirmed_new_flow=flow, **kwargs)
    done = events[-1]
    assert done.get('error_code') == 'capability_preflight', events
    run_id = done['run_id']
    first = runs.load(run_id)
    assert first['host_execution_scope']['task_id']
    assert not builds
    # Restarting without a grant must not bypass the new-run preflight.
    events.clear()
    await expert_web.run_expert_chat(SimpleNamespace(send_json=send), '继续', resume_run_id=run_id, **kwargs)
    assert events[-1].get('error_code') == 'capability_preflight', events
    assert not builds
    runs.mutate(run_id, lambda record: record_authorization(record, kind='gate_answer', scope={
        'operation_classes': ['write_outside_workspace'], 'paths': [(project / 'kb').as_posix()]},
        quote='允许输出目录', source_ref='fixture:directory', host_verified=True), durable=True)
    seen = []
    class PreflightPassed(Exception):
        pass
    inspect = preflight.inspect
    def verify(manifest, **kw):
        seen.append(manifest)
        assert not any(f['severity'] == 'error' for f in inspect(manifest))
        raise PreflightPassed()
    monkeypatch.setattr(preflight, 'inspect', verify)
    with pytest.raises(PreflightPassed):
        await expert_web.run_expert_chat(SimpleNamespace(send_json=send), '继续', resume_run_id=run_id, **kwargs)
    assert seen[0]['nodes']['work'] is not None
    assert runs.load(run_id)['host_execution_scope']['task_id'] == first['host_execution_scope']['task_id']
    assert not builds  # No model or provider call was needed to repair the binding.
