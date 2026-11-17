import copy

import pytest

from mewcode.mcp.coord_store import CoordinationStore
from mewcode.teams.deployment import DeploymentRegistry, ModelRoute
from mewcode.teams.runtime_protocol import ArtifactContent
from mewcode.teams.service import TeamService
from mewcode.teams.spec import PolicyLimits, SpecRef
from mewcode.teams.spec_compiler import CompilationContext
from mewcode.teams.store_v2 import TeamActor, TeamStateError
from test_team_runtime_v2 import runtime_actor
from test_team_spec_v2 import graph_definition


@pytest.fixture
def deployment_case(tmp_path):
    with CoordinationStore(tmp_path / 'coord.sqlite', enable_team_v2=True) as store:
        raw = graph_definition()
        raw['policy']['allowed_models'] = ['fixture/model-a', 'fixture/model-b']
        raw['roles'][0]['max_members'] = 4
        for task in raw['tasks']:
            task['token_budget'] = 300
        # Independent jobs allow testing actual concurrent leases.
        for edge in raw['connections']:
            if edge['target'].get('task_key') == 'second':
                edge['source'] = dict(port_id='request')
        policy = PolicyLimits.model_validate(raw['policy'])
        context = CompilationContext(policy, policy, frozenset({'software-artifact/v1'}), frozenset({'software-check/v1'}))
        owner = runtime_actor()
        service = TeamService(store, actor=owner, compilation_context=context)
        service.register_spec(raw, command_id='spec')
        team = service.create_team(SpecRef(spec_id=raw['spec_id'], version=1), name='Concurrent team', command_id='team')
        run = service.plan_run(team['team_id'], objective='Produce independent artifacts', budget_ref='pool-budget',
                               expected_version=1, control_epoch=1, command_id='run')
        runtime, run_id = store.teams_v2.runtime, run['team_run_id']
        registry = DeploymentRegistry(runtime)
        for model in ('model-a', 'model-b'):
            registry.register(owner, dict(provider_id='fixture', model_id=model, ready=True, reason='Synthetic test adapter',
                                           max_instances=4, cost_rank=20 if model == 'model-a' else 10), command_id=model)
        seed = runtime.register_input(owner, run_id, ArtifactContent.from_bytes(port_id='request', schema_ref='software-artifact/v1',
                                       media_type='text/plain', content=b'Test input'), control_epoch=1, command_id='input')
        yield runtime, owner, registry, service, run_id, seed


def deploy(case, preference=None, max_parallel=2):
    runtime, owner, registry, _, run_id, seed = case
    configured = registry.configure(owner, run_id, preferences={'producer': preference or dict(mode='fixed', candidates=['fixture/model-a'], instances=2)},
                                     max_parallel=max_parallel, control_epoch=1, command_id='configure')
    active = runtime.activate(owner, run_id, bindings=configured['bindings'], inputs={'request': seed['version_id']},
                               budget_limit=1000, expected_version=1, control_epoch=1, command_id='activate')
    actors = [TeamActor(b['principal_ref'], owner.project_id, frozenset({'team.read', 'team.execute'}))
              for b in configured['bindings'] if b['kind'] == 'model']
    return configured, active, actors


def test_same_model_independent_instances_can_hold_parallel_tasks(deployment_case):
    runtime, owner, _, _, run_id, _ = deployment_case
    configured, active, actors = deploy(deployment_case)
    assert len(actors) == 2 and actors[0].principal_ref != actors[1].principal_ref
    tokens = []
    for index, key in enumerate(('produce', 'second')):
        task = runtime.task(owner, active['task_ids'][key])
        tokens.append(runtime.claim(actors[index], task['task_id'], worker_session_id=f'instance-{index}',
                                    expected_version=task['state_version'], command_id=f'claim-{index}')['token'])
    assert runtime.snapshot(owner, run_id)['execution_slots'] == 2
    assert runtime.db.execute("SELECT COUNT(*) FROM team_v2_attempts WHERE state='running'").fetchone()[0] == 2
    assert all(b['model_id'] == 'model-a' for b in configured['bindings'] if b['kind'] == 'model')


def test_one_instance_cannot_be_used_as_two_concurrent_workers(deployment_case):
    runtime, owner = deployment_case[:2]
    _, active, actors = deploy(deployment_case)
    for index, key in enumerate(('produce', 'second')):
        task = runtime.task(owner, active['task_ids'][key])
        if index:
            with pytest.raises(TeamStateError, match='WORKER_BUSY'):
                runtime.claim(actors[0], task['task_id'], worker_session_id='second-process', expected_version=task['state_version'], command_id='claim-again')
        else:
            runtime.claim(actors[0], task['task_id'], worker_session_id='first-process', expected_version=task['state_version'], command_id='claim')


def test_fixed_unavailable_does_not_fall_back(deployment_case):
    _, owner, registry, _, run_id, _ = deployment_case
    registry.register(owner, dict(provider_id='fixture', model_id='model-a', ready=False, reason='Not connected'), command_id='unavailable')
    with pytest.raises(TeamStateError, match='FIXED_MODEL_UNAVAILABLE'):
        registry.configure(owner, run_id, preferences={'producer': dict(mode='fixed', candidates=['fixture/model-a'])},
                             max_parallel=1, control_epoch=1, command_id='configure')
    assert registry.get(run_id) is None


def test_preferred_uses_only_the_allowed_fallback_and_records_why(deployment_case):
    _, owner, registry, _, _, _ = deployment_case
    registry.register(owner, dict(provider_id='fixture', model_id='model-a', ready=False, reason='Disconnected'), command_id='offline')
    configured, _, _ = deploy(deployment_case, dict(mode='preferred', candidates=['fixture/model-a', 'fixture/model-b']))
    assert configured['selection'][0]['selected'] == 'fixture/model-b'
    assert 'fixture/model-a' in configured['selection'][0]['unavailable']


def test_auto_uses_capabilities_then_declared_cost_latency(deployment_case):
    configured, _, _ = deploy(deployment_case, dict(mode='auto', candidates=['fixture/model-a', 'fixture/model-b']))
    assert configured['selection'][0]['selected'] == 'fixture/model-b'


def test_unregistered_or_ungranted_candidate_is_rejected(deployment_case):
    _, owner, registry, _, run_id, _ = deployment_case
    with pytest.raises(TeamStateError, match='MODEL_NOT_ALLOWED'):
        registry.configure(owner, run_id, preferences={'producer': dict(mode='preferred', candidates=['fixture/model-a', 'unknown/model'])},
                             max_parallel=1, control_epoch=1, command_id='forged-candidate')


def test_binding_cannot_forge_capabilities_or_identity(deployment_case):
    runtime, owner, registry, _, run_id, seed = deployment_case
    configured = registry.configure(owner, run_id, preferences={'producer': dict(mode='fixed', candidates=['fixture/model-a'])},
                                     max_parallel=1, control_epoch=1, command_id='configure')
    bindings = copy.deepcopy(configured['bindings'])
    bindings[0]['capabilities'] = list(bindings[0]['capabilities']) + ['unlimited-shell']
    with pytest.raises(TeamStateError, match='EXECUTION_POLICY_MISMATCH'):
        runtime.activate(owner, run_id, bindings=bindings, inputs={'request': seed['version_id']}, budget_limit=1000,
                           expected_version=1, control_epoch=1, command_id='activate-forged')


def test_route_becoming_unavailable_blocks_claim_without_substitution(deployment_case):
    runtime, owner, registry = deployment_case[:3]
    _, active, actors = deploy(deployment_case)
    registry.register(owner, dict(provider_id='fixture', model_id='model-a', ready=False, reason='Adapter disconnected'), command_id='disconnected')
    task = runtime.task(owner, active['task_ids']['produce'])
    with pytest.raises(TeamStateError, match='MODEL_ROUTE_UNAVAILABLE'):
        runtime.claim(actors[0], task['task_id'], worker_session_id='attempt', expected_version=task['state_version'], command_id='claim')
    assert task['held_tokens'] == 0


def test_approval_label_is_never_a_provider_model():
    with pytest.raises(ValueError, match='approval routes'):
        ModelRoute(provider_id='fixture', model_id='codex-auto-review')
