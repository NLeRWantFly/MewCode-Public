"""Real graph publication/dispatch must fund a complete split, without new quota."""
import copy
import json

import pytest

from mewcode.kernel import collaboration as ledger, collaboration_graph as graph, lineage_budget
from mewcode.kernel.collaboration_work_budget import expansion_capacity
from mewcode.orchestration.collab_compile import compile_plan
from mewcode.orchestration.task_context import build_task_context
from tests.test_autonomous_fragment import _sample_flow, _proposal, _build
from tests.test_autonomous_publication import prepared, publish_graph
from tests.test_collaboration_mode import RUN_ID, passing_report


def fresh(tmp_path, limit):
    flow = _sample_flow()
    record = {'run_id': RUN_ID, 'locked_flow': flow.as_dict()}
    value = ledger.initialize(record, compile_plan(flow))
    value['lineage_budget']['limits']['lineage']['attempts'] = limit
    attempt = ledger.dispatch(value, 'work')
    ledger.mark_started(value, attempt['id'])
    fragment = _build(value, flow, tmp_path, _proposal())
    return record, attempt, fragment


def test_prepare_requires_whole_split_and_failure_is_atomic(tmp_path):
    record, attempt, fragment = fresh(tmp_path, 3)
    before = copy.deepcopy(record)
    with pytest.raises(lineage_budget.BudgetExhausted) as error:
        graph.prepare(record, fragment, publication_id='no-room', attempt_id=attempt['id'])
    assert error.value.requested == 3 and error.value.available == 2
    assert record == before
    capacity = expansion_capacity(ledger.state(record), 'work')
    assert capacity['minimum_shortfall'] == 1 and capacity['max_children'] == 1


def test_exact_budget_reserves_children_and_join_once_and_survives_reload(tmp_path):
    record, attempt, fragment = fresh(tmp_path, 4)
    item = graph.prepare(record, fragment, publication_id='publication-one', attempt_id=attempt['id'])
    value = ledger.state(record)
    assert value['lineage_budget']['root']['reserved']['attempts'] == 3
    before = copy.deepcopy(record)
    assert graph.prepare(record, fragment, publication_id=item['id'], attempt_id=attempt['id']) == item
    assert record == before
    publish_graph(record)
    # JSON round trip uses no process-local allocation state.
    record = json.loads(json.dumps(record))
    value = ledger.state(record)
    for key in [*fragment['child_ids'], 'work']:
        reserved = value['lineage_budget']['root']['reserved']['attempts']
        child = ledger.dispatch(value, key)
        assert child['budget_reservation_id'] == item['attempt_reservations'][key]
        assert value['lineage_budget']['root']['reserved']['attempts'] == reserved
        ledger.mark_started(value, child['id'])
        ledger.complete(value, child['id'], passing_report(value, key))
        ledger.mark_started(value, child['id'])
        assert value['lineage_budget']['root']['reserved']['attempts'] == reserved - 1
    budget = lineage_budget.ensure(value)
    assert budget['lineages']['work']['used']['attempts'] == 4
    assert budget['lineages']['work']['reserved']['attempts'] == 0
    assert not budget['grants']
    assert ledger.valid_certificate(value, 'work')


def test_rejection_and_coordinator_takeover_release_unstarted_permits(tmp_path):
    for action in ('reject', 'takeover'):
        _, record, _, item = prepared(tmp_path / action)
        if action == 'reject':
            graph.reject(record, item['id'], reason='需求覆盖不全')
        else:
            ledger.takeover(ledger.state(record), {'provider_key': 'coord', 'model_id': 'same'}, source_ref='user')
        value = ledger.state(record)
        assert value['lineage_budget']['root']['reserved']['attempts'] == 0
        assert value['lineage_budget']['root']['used']['attempts'] == 1
        assert all(value['lineage_budget']['reservations'][key]['status'] == 'released'
                   for key in item['attempt_reservations'].values())


def test_retry_cannot_take_a_siblings_or_parent_join_reservation(tmp_path):
    record, attempt, fragment = fresh(tmp_path, 4)
    graph.prepare(record, fragment, publication_id='publication-one', attempt_id=attempt['id'])
    publish_graph(record)
    value = ledger.state(record)
    key = fragment['child_ids'][0]
    child = ledger.dispatch(value, key)
    ledger.mark_started(value, child['id'])
    ledger.complete(value, child['id'], {'passed': False})
    value.setdefault('retry_generations', {})[key] = 1
    before = copy.deepcopy(value)
    with pytest.raises(lineage_budget.BudgetExhausted):
        ledger.dispatch(value, key)
    assert value == before
    assert value['lineage_budget']['root']['reserved']['attempts'] == 2


def test_published_contract_revision_releases_only_unclaimed_permits(tmp_path):
    _, record, _, item = prepared(tmp_path, publish=True)
    value = ledger.state(record)
    first = item['fragment']['child_ids'][0]
    attempt = ledger.dispatch(value, first)
    ledger.mark_started(value, attempt['id'])
    replacement = {**value['entries']['goal'], 'body': '新交付要求'}
    ledger.proposal(value, proposal_id='change', changes={'goal': replacement}, evidence=['user'],
                   actor='user', base_version=1, epoch=1)
    ledger.adopt(value, 'change', review={'passed': True, 'evidence': ['范围核对']},
                 proposer_vendor='a', reviewer_vendor='b')
    budget = lineage_budget.ensure(value)
    assert budget['root']['used']['attempts'] == 2
    assert budget['root']['reserved']['attempts'] == 0
    assert budget['reservations'][attempt['budget_reservation_id']]['status'] == 'settled'


def test_model_context_reports_real_shortfall_and_does_not_mutate_budget(tmp_path):
    record, _, _ = fresh(tmp_path, 4)
    value = ledger.state(record)
    lineage_budget.reserve(value, 'other-pending', lineage_id='work', kind='attempts')
    before = copy.deepcopy(value)
    context = build_task_context(value, 'work')
    capacity = context['budget']['execution']['decomposition']
    assert capacity['available_attempts'] == 2 and capacity['minimum_shortfall'] == 1
    assert capacity['current_attempt_already_accounted'] is True
    assert value == before


def test_capacity_counts_replacement_after_coordinator_takeover(tmp_path):
    record, attempt, _ = fresh(tmp_path, 4)
    value = ledger.state(record)
    ledger.takeover(value, {'provider_key': 'coord', 'model_id': 'same'}, source_ref='user')
    before = copy.deepcopy(record)
    capacity = expansion_capacity(value, 'work')
    assert capacity['current_attempt_additional_cost'] == 1
    assert capacity['max_children'] == 1 and capacity['minimum_shortfall'] == 1
    assert record == before
    record['attempt'] = 2
    replacement = ledger.resume_dispatch(record, 'work')
    assert replacement['id'] != attempt['id']
    after = expansion_capacity(value, 'work')
    assert after['current_attempt_additional_cost'] == 0
    assert after['max_children'] == 1 and after['minimum_shortfall'] == 1


@pytest.mark.asyncio
async def test_tool_returns_smaller_split_hint_without_ending_current_attempt(tmp_path):
    from tests.test_node_expansion import _holder, _fanout_proposal
    from mewcode.tools.propose_node_expansion import ProposeNodeExpansion, ProposeNodeExpansionParams
    holder = _holder(tmp_path)
    holder.budget_capacity = lambda: {'max_children': 2, 'minimum_shortfall': 0}
    tool = ProposeNodeExpansion(holder)
    proposal = _fanout_proposal()
    blocked = await tool.execute(ProposeNodeExpansionParams(proposal=proposal))
    assert blocked.is_error and json.loads(blocked.output)['status'] == 'budget_limited'
    assert holder.value is None and holder.rejection is None
    proposal['nodes'].pop()
    proposal['edges'].pop()
    accepted = await tool.execute(ProposeNodeExpansionParams(proposal=proposal))
    assert not accepted.is_error and holder.value


def test_publish_rejects_missing_or_consumed_allocation_atomically(tmp_path):
    for mutation in ('missing', 'released'):
        _, record, _, item = prepared(tmp_path / mutation)
        value = ledger.state(record)
        identity = next(iter(item['attempt_reservations'].values()))
        if mutation == 'missing':
            value['graph_publications'][item['id']]['attempt_reservations'] = {}
        else:
            lineage_budget.release(value, identity)
        before = copy.deepcopy(record)
        with pytest.raises(ValueError):
            publish_graph(record)
        assert record == before


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['readonly', 'write', 'command', 'dependent'])
async def test_real_scheduler_parallelizes_only_independent_readonly_children(tmp_path, mode):
    import asyncio
    from mewcode.expert_group import ExpertRoleResult
    from tests.test_autonomous_context_runtime import retry_store, orchestrate, run_flow
    from tests.test_autonomous_fragment import ALLOWED_TOOLS
    from tests.test_collaboration_mode import delivery
    flow = _sample_flow()
    store = retry_store(tmp_path, flow)
    active = set()
    seen, peaks = [], []
    both = asyncio.Event()

    async def runner(node, model, prompt, policy, member=None):
        if node.node_id == 'work':
            proposal = _proposal()
            for child in proposal['nodes']:
                child['tools'] = ['Bash'] if mode == 'command' else ['ReadFile']
                if mode != 'write':
                    child['write_paths'] = []
            if mode == 'dependent':
                proposal['edges'] = [{'from_node': 'first', 'to_node': 'second'},
                                     {'from_node': 'second', 'to_node': '$join'}]
            policy.expansion_holder.set_allowed_tools(set(ALLOWED_TOOLS))
            validated = policy.expansion_holder.submit(proposal)
            return ExpertRoleResult(text='划分', status='node_expansion', node_expansion=validated.as_dict())
        if node.node_id.startswith('work::'):
            seen.append(node.node_id)
            assert policy.parallel_readonly is (mode == 'readonly')
            if mode == 'readonly':
                assert policy.expansion_holder is None
            active.add(node.node_id)
            peaks.append(len(active))
            if len(active) == 2:
                both.set()
            if mode == 'readonly':
                await asyncio.wait_for(both.wait(), timeout=5)
            else:
                await asyncio.sleep(0)
            active.remove(node.node_id)
        return delivery(node.node_id)

    async def review(*args):
        return {'passed': True, 'evidence': ['按原合同验收']}

    result = await run_flow(orchestrate(store, tmp_path, runner, review, review), flow, store)
    assert result.status == 'completed', result.final_summary
    assert len(seen) == len(set(seen)) == 2
    assert max(peaks) == (2 if mode == 'readonly' else 1)
    value = ledger.state(store.load(RUN_ID))
    assert value['lineage_budget']['lineages']['work']['used']['attempts'] == 4
    assert value['lineage_budget']['lineages']['work']['reserved']['attempts'] == 0


@pytest.mark.asyncio
async def test_two_parallel_context_overflows_reuse_permits_and_finish_nested_joins(tmp_path, monkeypatch):
    from mewcode.expert_group import ExpertRoleResult
    from mewcode.orchestration import collab_runtime
    from mewcode.orchestration.task_context import ContextBudgetExceeded
    from tests.test_autonomous_context_runtime import retry_store, orchestrate, run_flow
    from tests.test_collaboration_mode import delivery
    flow = _sample_flow()
    store = retry_store(tmp_path, flow)
    store.mutate(RUN_ID, lambda record: ledger.initialize(record, compile_plan(flow)), durable=True)
    store.mutate(RUN_ID, lambda record: ledger.state(record)['lineage_budget']['limits']['lineage'].update(attempts=10), durable=True)
    normal = collab_runtime.build_task_context
    first_attempts, invoked = {}, []

    def large_context(value, contract_id, **kwargs):
        if value['contracts'][contract_id].get('expansion_depth') == 1:
            attempt = ledger.attempts_for(value, contract_id)[-1]
            first_attempts.setdefault(contract_id, []).append(attempt['id'])
            raise ContextBudgetExceeded('模拟完整 wrapper 后该孩子需要分页')
        return normal(value, contract_id, **kwargs)

    monkeypatch.setattr(collab_runtime, 'build_task_context', large_context)

    def readonly_proposal(parent='work', version=1):
        proposal = _proposal(parent, version)
        for child in proposal['nodes']:
            child.update(tools=['ReadFile'], write_paths=[])
        return proposal

    async def runner(node, model, prompt, policy, member=None):
        invoked.append(node.node_id)
        if node.node_id == 'work':
            policy.expansion_holder.set_allowed_tools({'ReadFile'})
            result = policy.expansion_holder.submit(readonly_proposal())
            return ExpertRoleResult(text='划分', status='node_expansion', node_expansion=result.as_dict())
        return delivery(node.node_id)

    async def coordinate(model, payload, system):
        if payload.get('purpose') == 'context_overflow_partition':
            return {'notes': '拆成两个只读核查，父级完整验收'}
        if payload.get('purpose') == 'context_overflow_plan':
            return readonly_proposal(payload['parent_node_id'], payload['expected_flow_version'])
        return {'passed': True, 'evidence': ['原要求与范围保留']}

    async def review(*args):
        return {'passed': True, 'evidence': ['真实调度后完整验收']}

    result = await run_flow(orchestrate(store, tmp_path, runner, review, coordinate), flow, store)
    assert result.status == 'completed', result.final_summary
    value = ledger.state(store.load(RUN_ID))
    assert len(first_attempts) == 2
    assert all(len(ids) == 2 and len(set(ids)) == 1 for ids in first_attempts.values())
    for key in first_attempts:
        assert ledger.valid_certificate(value, key)
        assert key not in invoked  # Only grandchildren execute business work.
    assert value['lineage_budget']['lineages']['work']['used']['attempts'] == 10
    assert value['lineage_budget']['root']['reserved']['attempts'] == 0
    assert not value['lineage_budget']['grants']
    assert len(value['graph_publications']) == 3


@pytest.mark.parametrize('started', [False, True])
def test_revised_unstarted_attempt_returns_its_permit_but_used_work_is_not_refunded(tmp_path, started):
    record, _, _ = fresh(tmp_path, 5)
    value = ledger.state(record)
    # Start from a fresh independent contract, leaving the original spent work
    # untouched. A simulated authorized revision fences this pending dispatch.
    value['contracts']['other'] = {**copy.deepcopy(value['contracts']['work']), 'id': 'other', 'node_id': 'other'}
    attempt = ledger.dispatch(value, 'other', run_attempt=1)
    if started:
        ledger.mark_started(value, attempt['id'])
    value['contracts']['other']['version'] += 1
    record['attempt'] = 2
    resumed = ledger.resume_dispatch(record, 'other')
    budget = lineage_budget.ensure(value)
    permit = budget['reservations'][attempt['budget_reservation_id']]
    assert permit['status'] == ('settled' if started else 'released')
    assert resumed['resumed_from'] == attempt['id']
    assert budget['root']['used']['attempts'] == (2 if started else 1)
    assert budget['root']['reserved']['attempts'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('second_state', ['completed', 'deferred'])
async def test_serial_revision_after_batch_redispatches_only_obsolete_sibling_work(tmp_path, monkeypatch, second_state):
    from mewcode.expert_group import ExpertRoleResult
    from mewcode.orchestration import collab_runtime
    from mewcode.orchestration.task_context import ContextBudgetExceeded
    from tests.test_autonomous_context_runtime import retry_store, orchestrate, run_flow
    from tests.test_collaboration_mode import delivery
    flow = _sample_flow()
    store = retry_store(tmp_path, flow)
    def setup(record):
        value = ledger.initialize(record, compile_plan(flow))
        value['policy']['max_attempts'] = 3
        value['entries']['method'] = dict(id='method', kind='requirement', title='方法', body='first method',
            version=1, origin='model', strength='soft', sources=[], data={}, state='active')
        value['contracts']['work']['requirement_ids'].append('method')
    store.mutate(RUN_ID, setup, durable=True)
    calls = {}
    normal = collab_runtime.build_task_context

    def context(value, contract_id, **kwargs):
        if second_state == 'deferred' and contract_id.endswith('::second') and value['spec_version'] == 1:
            raise ContextBudgetExceeded('并行占位后，由兄弟的已复核修订改变上下文')
        return normal(value, contract_id, **kwargs)
    monkeypatch.setattr(collab_runtime, 'build_task_context', context)

    async def runner(node, model, prompt, policy, member=None):
        calls[node.node_id] = calls.get(node.node_id, 0) + 1
        if node.node_id == 'work':
            proposal = _proposal()
            for child in proposal['nodes']:
                child.update(tools=['ReadFile'], write_paths=[])
            policy.expansion_holder.set_allowed_tools({'ReadFile'})
            validated = policy.expansion_holder.submit(proposal)
            return ExpertRoleResult(text='划分', status='node_expansion', node_expansion=validated.as_dict())
        return delivery(node.node_id)

    async def review(model, payload, context):
        key = payload['contract']['id']
        failed = key.endswith('::first') and ledger.state(store.load(RUN_ID))['spec_version'] == 1
        return {'passed': not failed, 'evidence': ['方法未满足' if failed else '按当前要求核对'], 'reason': '方法需修订' if failed else ''}

    async def coordinate(model, payload, system):
        if 'event_id' in payload:
            entry = ledger.state(store.load(RUN_ID))['entries']['method']
            return {'action': 'revise', 'entry_changes': {'method': {**entry, 'body': 'revised method'}},
                    'reason': 'method failed', 'evidence_refs': ['fixture'], 'instructions': '按修订后方法执行'}
        return {'passed': True, 'evidence': ['方法修订保留用户目标和边界']}

    result = await run_flow(orchestrate(store, tmp_path, runner, review, coordinate), flow, store)
    assert result.status == 'completed', result.final_summary
    value = ledger.state(store.load(RUN_ID))
    assert value['spec_version'] == 2
    children = value['contracts']['work']['join_children']
    first, second = children
    assert calls[first] == 2 and calls[second] == (2 if second_state == 'completed' else 1)
    assert all(ledger.valid_certificate(value, key) for key in children)
    assert value['lineage_budget']['root']['reserved']['attempts'] == 0
    assert value['lineage_budget']['lineages']['work']['used']['attempts'] == (6 if second_state == 'completed' else 5)
    if second_state == 'deferred':
        prior, replacement = ledger.attempts_for(value, second)
        assert prior['status'] == 'superseded_closed' and not prior.get('budget_started')
        assert replacement['resumed_from'] == prior['id']
        assert value['lineage_budget']['reservations'][prior['budget_reservation_id']]['status'] == 'released'
