import asyncio

import pytest

from mewcode.expert_runs import ExpertRunStore
from mewcode.workflow_core.web import envelope
from mewcode.workflow_core.store import ensure
from mewcode.workflow_core.runtime import Core
from mewcode.workflow_core.state import edit
from mewcode.workflow_core.projection import node_detail
from tests.workflow_core.test_runtime import graph


@pytest.mark.asyncio
async def test_durable_run_publishes_current_graph_and_public_progress(tmp_path):
    store = ExpertRunStore(tmp_path / 'runs')
    flow = graph(['environment', 'ocr'], [('environment', 'ocr', 'always')])
    store.create(run_id='a' * 32, conversation_id='chat', task='OCR', attachments=[], profile={},
                 strategy='stable', flow=envelope(flow))
    backend = ensure(store, 'a' * 32, flow)
    async def execute(ctx):
        for index in range(7):
            await ctx.publish('progress', text=f'发现 {index}')
        await ctx.publish('tool_result', name='ReadFile', output='raw diagnostic')
        return dict(text='done')
    result = await Core(backend, execute).run()
    assert result['status'] == 'completed'
    public = store.read_workbench('a' * 32)['_workbench_view']
    assert public['task']['mode'] == 'workflow' and public['editor']['editable']
    assert [row['text'] for row in public['structure']['nodes'][0]['progress']] == [f'发现 {i}' for i in range(2, 7)]
    reopened = ExpertRunStore(tmp_path / 'runs')
    recovered = ensure(reopened, 'a' * 32).read()
    assert recovered['nodes'] == result['nodes']
    detail = node_detail(reopened.load('a' * 32), 'ocr')
    assert len(detail['attempts'][0]['progress']) == 7
    assert detail['attempts'][0]['diagnostics'][0]['output'] == 'raw diagnostic'


def test_legacy_migration_keeps_completed_work_and_original_archive(tmp_path):
    store = ExpertRunStore(tmp_path / 'runs')
    old = envelope(graph(['a', 'b'], [('a', 'b', 'always')])).as_dict()
    old['meta'].pop('workflow_core')
    store.create(run_id='b' * 32, conversation_id='chat', task='OCR', attachments=[], profile={}, strategy='stable', flow=old)
    store.mutate('b' * 32, lambda record: record.update(status='paused', node_states={
        'a': dict(status='completed', text='already delivered'), 'b': dict(status='failed', text='connection lost')}))
    backend = ensure(store, 'b' * 32)
    assert backend.read()['nodes']['a']['status'] == 'completed'
    assert backend.read()['nodes']['b']['status'] == 'failed'
    assert store.load('b' * 32)['workflow_legacy']['node_states']['a']['text'] == 'already delivered'
    backend.change(lambda state: edit(state, dict(action='upsert', kind='node', id='check', fields={'kind': 'judge', 'label': '核对'})))
    assert backend.read()['draft']['nodes'][-1]['id'] == 'check'
