import asyncio
import copy

import pytest

from mewcode.expert_group import ExpertGroupOrchestrator, ExpertRoleResult
from mewcode.workflow_core.migration import graph_from_flow, migrate
from mewcode.workflow_core.runtime import Core, MemoryBackend
from mewcode.workflow_core.state import command
from tests.test_collaboration_mode import flow, PROFILE, CATALOG
from tests.workflow_core.test_runtime import graph


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['classic', 'collaborative', 'default'])
async def test_public_orchestrator_never_adds_hidden_quality_calls(mode):
    invoked = []
    async def runner(role, model, prompt, policy):
        invoked.append(role)
        return ExpertRoleResult(text='正常交付')
    orchestrator = ExpertGroupOrchestrator(runner=runner)
    result = await orchestrator.run('OCR', PROFILE, CATALOG, strategy='stable',
                                    flow=None if mode == 'default' else flow(mode))
    assert result.status == 'completed'
    assert invoked == ['executor'] * (1 if mode == 'default' else 2)


def test_rejected_quality_delivery_is_reused_while_failed_and_unknown_are_distinct():
    record = dict(locked_flow=graph(['work', 'failed', 'unknown']), task='test', orchestration_mode='collaborative',
        node_states={'work': {'status': 'needs_attention'}, 'failed': {'status': 'failed', 'text': 'network'},
                     'unknown': {'status': 'paused', 'tools': {'t': {'status': 'outcome_unknown'}}}},
        collaboration={'attempts': {'a': {'contract_id': 'work', 'delivery': {'status': 'completed', 'text': 'usable'},
                                          'report': {'passed': False}}}})
    original = copy.deepcopy(record['collaboration'])
    state = migrate(record)
    assert state['nodes']['work']['result']['text'] == 'usable'
    assert state['nodes']['work']['status'] == 'completed'
    assert state['nodes']['failed']['status'] == 'failed'
    assert state['nodes']['unknown']['status'] == 'unknown'
    assert record['workflow_legacy']['collaboration'] == original


def test_legacy_normal_delivery_keeps_completed_status_despite_old_timeout():
    state = migrate(dict(locked_flow=graph(['work']), node_states={'work': {
        'status': 'completed', 'text': 'Recovered deployment delivered',
        'tools': {'clone': {'status': 'outcome_unknown'}}}}))
    assert state['nodes']['work']['status'] == 'completed'
    attempt = state['attempts'][state['nodes']['work']['attempt_id']]
    assert attempt['pending_tools']['clone']['status'] == 'outcome_unknown'


@pytest.mark.asyncio
async def test_legacy_input_branch_becomes_visible_component_and_only_selected_path_runs():
    raw = dict(nodes=[{'node_id': key, 'role': 'executor'} for key in ['choose', 'a', 'b', 'join']],
        edges=[dict(from_node='choose', to_node=key, input_when={'input': 'mode', 'equals': key}) for key in ('a', 'b')]
              + [dict(from_node=key, to_node='join') for key in ('a', 'b')])
    migrated = graph_from_flow(raw)
    assert any(node['kind'] == 'branch' for node in migrated['nodes'])
    state = migrate(dict(locked_flow=raw, run_inputs={'mode': 'b'}))
    calls = []
    async def execute(ctx):
        calls.append(ctx.node_id)
        return dict(text='done')
    result = await Core(MemoryBackend(state), execute).run()
    assert result['status'] == 'completed' and calls == ['choose', 'b', 'join']


@pytest.mark.asyncio
async def test_interrupted_legacy_foreach_keeps_item_and_completed_body_work():
    raw = dict(nodes=[dict(node_id=key, role='auditor' if key == 'check' else 'executor')
                      for key in ['prepare', 'ocr', 'check', 'end']],
        edges=[dict(from_node=a, to_node=b) for a,b in [('prepare','ocr'),('ocr','check'),('check','end')]],
        meta={'loops': [dict(loop_id='pages', node_ids=['prepare','ocr','check'], entry_node_id='prepare',
            check_node_id='check', mode='foreach', items=['A','B','C'], max_iterations=4, token_budget=300000,
            timeout_seconds=900)]})
    record = dict(locked_flow=raw, node_states={
        'prepare': {'status': 'completed', 'text': 'B prepared'}, 'ocr': {'status': 'paused'}},
        loop_states={'pages': {'status': 'running', 'item_index': 1, 'round': 2}})
    state = migrate(record)
    assert not state['graph']['nodes'][-1]['config'].get('limits')
    command(state, dict(operation='resume', request_id='resume'))
    calls = []
    async def execute(ctx):
        calls.append((ctx.node_id, ctx.inputs.get('item')))
        return dict(text='done', value=True)
    result = await Core(MemoryBackend(state), execute).run()
    assert result['status'] == 'completed'
    assert ('loop_pages/prepare','B') not in calls
    assert ('loop_pages/ocr','B') in calls and ('loop_pages/ocr','C') in calls
