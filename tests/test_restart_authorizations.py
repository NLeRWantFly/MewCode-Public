"""Collected stage answers do not change a host-verified restart's task scope."""
import copy
from types import SimpleNamespace

import pytest

from mewcode.experts.restart_authorizations import source_for_restart
from mewcode.orchestration.convert import from_expert_flow, to_expert_flow
from mewcode.orchestration.pipeline_bridge import from_pipeline_graph, to_pipeline_graph
from mewcode.orchestration.run_inputs import append_run_inputs
from mewcode.orchestration.spec import OrchestrationSpec
from tests.test_generic_loops import spec_for


def fixture():
    raw = spec_for().as_dict()
    raw['meta'].update(task_design={'task_id': 'new-task'})
    raw['goal_contract'] = {'objective': 'Original business scope', 'run_inputs': [
        {'name': 'upload_choice', 'label': 'Known upload scope', 'required': False, 'default': ''}]}
    flow = to_expert_flow(OrchestrationSpec.from_value(raw))
    old_flow = copy.deepcopy(flow.as_dict())
    old_flow['meta']['task_design']['task_id'] = 'source-task'
    source = {'run_id': 'a' * 32, 'conversation_id': 'conversation', 'status': 'terminated',
              'active_node_ids': [], 'locked_flow': old_flow,
              'task': append_run_inputs('Original business scope', {'upload_choice': ''}),
              'run_inputs': {'upload_choice': 'allow_known_scope'}, 'authorizations': {'entries': []}}
    design = {'task_id': 'new-task', 'restart': {'source_run_id': source['run_id'],
              'source_task_id': 'source-task', 'request_id': 'user-rebuild'}}
    return flow, source, design


def test_only_host_bound_restart_can_reuse_task_identity_after_stage_inputs_were_collected():
    flow, source, design = fixture()
    calls = []
    def read(run_id, *fields):
        calls.append((run_id, fields))
        return copy.deepcopy(source)
    store = SimpleNamespace(read_execution_fields=read)
    task = append_run_inputs('Original business scope', source['run_inputs'])
    assert task != source['task']
    reused = source_for_restart(store, design, flow, conversation_id='conversation',
                               task=task, run_inputs=source['run_inputs'])
    assert reused == source and len(calls) == 1
    assert 'node_states' not in calls[0][1] and 'events' not in calls[0][1]


@pytest.mark.parametrize('fault', ['active', 'not_terminated', 'source_task', 'target_task',
                                  'conversation', 'goal', 'inputs', 'missing_proof'])
def test_changed_scope_or_unsafe_source_never_inherits_authority(fault):
    flow, source, design = fixture()
    task = append_run_inputs('Original business scope', source['run_inputs'])
    values = dict(source['run_inputs'])
    if fault == 'active':
        source['active_node_ids'] = ['work']
    elif fault == 'not_terminated':
        source['status'] = 'paused'
    elif fault == 'source_task':
        design['restart']['source_task_id'] = 'foreign-task'
    elif fault == 'target_task':
        design['task_id'] = 'other-target'
    elif fault == 'conversation':
        source['conversation_id'] = 'foreign'
    elif fault == 'goal':
        task = append_run_inputs('Changed business scope', values)
    elif fault == 'inputs':
        values['upload_choice'] = 'broader_scope'
    else:
        design.pop('restart')
    store = SimpleNamespace(read_execution_fields=lambda *_: source)
    with pytest.raises(ValueError):
        source_for_restart(store, design, flow, conversation_id='conversation', task=task, run_inputs=values)


def test_explicit_role_models_survive_canvas_and_execution_roundtrips():
    raw = spec_for().as_dict()
    route = {'provider_key': 'authorized', 'model_id': 'gpt-6.1-sol', 'effort': 'max'}
    raw['meta']['role_models'] = {key: route for key in
        ('planner', 'executor', 'verifier', 'reviewer', 'arbiter', 'coordinator')}
    spec = OrchestrationSpec.from_value(raw)
    expected = raw['meta']['role_models']
    for roundtrip in (from_expert_flow(to_expert_flow(spec)), from_pipeline_graph(to_pipeline_graph(spec))):
        assert roundtrip.meta['role_models'] == expected
        assert to_expert_flow(roundtrip).meta['role_models'] == expected
