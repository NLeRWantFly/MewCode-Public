import copy

import pytest

from mewcode.workflow_core.runtime import Core, MemoryBackend
from mewcode.workflow_core.schema import normalize
from mewcode.workflow_core.state import current, edit, finish_attempt, fresh, start_attempt
from tests.workflow_core.test_runtime import graph


@pytest.mark.parametrize('port', ['pass', 'fail', 'named-choice'])
def test_work_cannot_silently_drop_downstream_on_a_condition_port(port):
    with pytest.raises(ValueError, match='执行节点.*条件出口'):
        graph(['work', 'next'], [('work', 'next', port)])


def test_changing_judge_to_work_requires_updating_its_outgoing_condition():
    state = fresh(graph([dict(id='check', kind='judge'), 'next'], [('check', 'next', 'pass')]))
    before = copy.deepcopy(state)
    with pytest.raises(ValueError, match='执行节点.*条件出口'):
        edit(state, dict(action='upsert', kind='node', id='check', fields={'kind': 'work'}))
    assert state == before
    edge_id = state['graph']['edges'][0]['id']
    edit(state, dict(action='batch', operations=[
        dict(action='upsert', kind='node', id='check', fields={'kind': 'work'}),
        dict(action='upsert', kind='edge', id=edge_id, fields={'port': 'always'})]))
    assert state['draft']['edges'][0]['port'] == 'always'


def test_nested_work_rejects_condition_ports():
    with pytest.raises(ValueError, match='执行节点.*条件出口'):
        normalize(dict(nodes=[dict(id='loop', kind='loop', config={'graph': {
            'nodes': [dict(id='work'), dict(id='next')],
            'edges': [dict(source='work', target='next', port='pass')]}})]))


@pytest.mark.asyncio
async def test_repair_old_condition_resumes_skipped_nodes_and_keeps_deliveries():
    state = fresh(graph(['scope', 'authorization', 'prepare', 'deliver'], [
        ('scope', 'authorization', 'always'), ('authorization', 'prepare', 'always'),
        ('prepare', 'deliver', 'always')]))
    for path in ['scope', 'authorization']:
        attempt = start_attempt(state, path, {}, [])
        finish_attempt(state, attempt['id'], dict(status='completed', text=path + ' delivered'))
    kept = copy.deepcopy({path: state['nodes'][path] for path in ['scope', 'authorization']})
    for path in ['prepare', 'deliver']:
        current(state, path).update(status='skipped', skip_reason='branch')
    # A persisted graph from before the routing check, matching the real defect.
    state['graph']['edges'][1]['port'] = 'pass'
    state['status'] = 'completed'
    backend, calls = MemoryBackend(state), []
    changed = backend.change(lambda value: edit(value, dict(action='upsert', kind='edge',
        id=value['graph']['edges'][1]['id'], fields={'port': 'always'})))

    async def execute(ctx):
        calls.append(ctx.node_id)
        return dict(text='continued')

    core = Core(backend, execute)
    core.control(dict(operation='apply', request_id='repair', expected_revision=changed['revision']))
    result = await core.run()
    assert result['status'] == 'completed'
    assert calls == ['prepare', 'deliver']
    assert {path: result['nodes'][path] for path in kept} == kept
    assert all('skip_reason' not in result['nodes'][path] for path in calls)
