"""A new finite plan can fund its first pass without replenishing old usage."""
import copy

from mewcode.expert_runs import ExpertRunStore
from mewcode.kernel import collaboration as ledger, lineage_budget
from mewcode.orchestration.collab_compile import compile_plan
from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.finite_work_capacity import prepare
from mewcode.orchestration.spec import OrchestrationSpec
from tests.test_collaboration_mode import PROFILE, RUN_ID, flow as collaboration_flow
from tests.test_generic_loops import spec_for


def finite_flow():
    raw = spec_for().as_dict()
    raw['nodes'].insert(2, {'node_id': 'second', 'kind': 'implement', 'label': 'second', 'goal': 'second'})
    raw['edges'] = [e for e in raw['edges'] if not (e['from_node'] == 'work' and e['to_node'] == 'check')]
    raw['edges'].extend([{'from_node': 'work', 'to_node': 'second'}, {'from_node': 'second', 'to_node': 'check'}])
    raw['meta']['loops'][0].update(mode='foreach', control_mode='team', node_ids=['work', 'second', 'check'],
        items=[{'item_id': str(i), 'goal': 'item'} for i in range(256)])
    raw['meta'].update(orchestration_mode='collaborative',
        collaboration_spec=copy.deepcopy(collaboration_flow().meta['collaboration_spec']))
    for row in raw['nodes']:
        row['plan_brief'] = {'summary': row['goal'], 'outputs': ['actual delivery'],
                             'acceptance': ['verify the current exact item'], 'known_inputs': [], 'steps': []}
    return to_expert_flow(OrchestrationSpec.from_value(raw))


def test_fresh_run_first_pass_fits_and_reload_never_replenishes_slots(tmp_path):
    flow = finite_flow()
    store = ExpertRunStore(tmp_path / 'runs', storage='sqlite')
    before = store.create(run_id=RUN_ID, conversation_id='finite', task='all items', attachments=[],
                          profile=PROFILE.as_dict(), strategy='stable', flow=flow)
    value = before['collaboration']
    assert value['initial_work_capacity']['first_pass_attempts'] == 770
    assert value['lineage_budget']['limits']['root']['attempts'] == 785
    attempt = ledger.dispatch(value, 'prepare')
    ledger.mark_started(value, attempt['id'])
    spent = copy.deepcopy(value['lineage_budget'])
    prepare(value, flow)
    assert value['lineage_budget'] == spent
    store.mutate(RUN_ID, lambda record: record.update(collaboration=copy.deepcopy(value), status='paused'))
    reloaded = ExpertRunStore(tmp_path / 'runs', storage='sqlite').load(RUN_ID)
    assert reloaded['collaboration']['lineage_budget'] == spent
    assert reloaded['collaboration']['lineage_budget']['root']['used']['attempts'] == 1
    assert not value['lineage_budget']['grants']
    assert flow.meta['loops'][0]['token_budget'] == 300000
    assert not before.get('authorizations')


def test_explicit_root_ceiling_and_existing_reservations_are_never_expanded():
    flow = finite_flow()
    plan = compile_plan(flow)
    plan['policy']['lineage_budget_limits'] = {'root': {'attempts': 10}}
    record = {'run_id': RUN_ID}
    value = ledger.initialize(record, plan)
    before = copy.deepcopy(value)
    prepare(value, flow)
    assert value == before
    assert lineage_budget.ensure(value)['limits']['root']['attempts'] == 10
