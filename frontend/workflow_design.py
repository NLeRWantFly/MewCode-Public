"""Use the same native graph for planning, the canvas and Core execution."""
from __future__ import annotations

import copy
import uuid

from mewcode.orchestration.draft_sources import digest
from mewcode.orchestration.draft_store import DraftStore, now
from mewcode.orchestration.task_design import graph_identity
from mewcode.workflow_core.planning import plan_spec, spec_for_graph, sync_materials
from mewcode.workflow_core.schema import digest as graph_digest, walk


def install(srv, check, current_spec):
    async def build(project, cid, *, expected):
        await check(project, cid, expected)
        store = DraftStore(project, cid)
        state = store.get()
        base = current_spec(project, cid)
        spec = plan_spec(state, base, cid)
        spec.meta['task_design'] = graph_identity(state['task_design'], state)
        await check(project, cid, expected)
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            state = store._get(db)
            from mewcode.orchestration.task_design import TaskDesign
            TaskDesign._assert_build(state['task_design'], expected)
            state['workflow_build'] = dict(id=uuid.uuid4().hex, expected=copy.deepcopy(expected),
                base_hash=digest(base), spec=spec.as_dict())
            store._put(db, state)

    async def preview(project, cid, *, expected):
        await check(project, cid, expected)
        candidate = (DraftStore.read_state(project, cid) or {}).get('workflow_build') or {}
        if candidate.get('expected') != expected or candidate.get('base_hash') != digest(current_spec(project, cid)):
            raise ValueError('构图依据已变化，请重新读取当前规格。')
        return candidate

    async def apply(project, cid, preview_id, *, expected):
        from frontend.task_design import LocalRequest, response_value
        candidate = await preview(project, cid, expected=expected)
        if candidate['id'] != preview_id:
            raise ValueError('预览版本已变化，未覆盖当前工作流。')
        base = current_spec(project, cid)
        return response_value(await srv.api_orchestration_open_canvas(LocalRequest(dict(
            spec=candidate['spec'], conversation_id=cid, mode='replace', task_design_expected=expected,
            expected_version=((base or {}).get('meta') or {}).get('pipeline_graph_version')))))

    srv.task_design_build_graph = build
    srv.task_design_preview_graph = preview
    srv.task_design_apply_graph = apply


def edit_plan(workbench, body, project, cid, guard):
    from frontend.workbench_editor import base_spec
    from mewcode.orchestration.consistent_save import save_consistently
    from mewcode.orchestration.pipeline_bridge import to_pipeline_graph
    from mewcode.orchestration.workflow_blocks import _baseline
    from mewcode.pipeline.store import PipelineStore
    from mewcode.workflow_core.state import fresh, edit
    srv = workbench.srv
    store = DraftStore(project, cid)
    with store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        state = store._get(db)
        design = state.get('task_design') or {}
        if (design.get('run_id') or design.get('status') in {'queued', 'building'} or
                (design.get('build_request') or {}).get('execution_state') or
                body.get('task_id', '') != design.get('task_id', '')):
            raise ValueError('任务状态已变化，请重新读取工作流。')
        new_design = not design
        if new_design:
            design = dict(task_id=uuid.uuid4().hex, revision=0, intent='plan', orchestration_mode='classic',
                goal='', status='paused', created_at=now(), build_attempts=0,
                intent_authority=dict(source='host_entry', intent='plan', selected_at=now()))
            state['task_design'] = design
        base = base_spec(workbench, project, cid, state)
        try:
            spec = plan_spec(state, base, cid)
            original = spec.as_dict()
            graph = spec.meta['workflow_core']
        except ValueError:
            from mewcode.orchestration.workflow_blocks import _baseline
            from mewcode.workflow_core.schema import normalize
            if any(e.get('kind') == 'node' and e.get('state') != 'retired'
                   for e in _baseline(state, base)['entries']):
                raise
            original = base or {}
            graph = normalize({})
        if body.get('basis') != graph_digest(graph) and not (new_design and not body.get('basis') and not graph['nodes']):
            raise ValueError('草稿版本已变化，请刷新后重试')
        runtime = fresh(graph)
        canvas = state.get('workflow_canvas') or {}
        runtime.update(draft_history=copy.deepcopy(canvas.get('history', [])),
                       draft_future=copy.deepcopy(canvas.get('future', [])))
        operation = copy.deepcopy(body.get('operation') or {})
        if operation.get('action') == 'delete' and not operation.get('kind'):
            operation['kind'] = 'node' if operation.get('id') in dict(walk(graph)) else 'edge'
        result = edit(runtime, operation, expected_revision=body.get('expected_revision'))
        changed = runtime['draft']
        before = dict(walk(graph))
        for path, node in walk(changed):
            if node.get('model') and node.get('model') != before.get(path, {}).get('model'):
                from mewcode.model_registry import ModelRef
                srv.MODEL_REGISTRY.resolve(ModelRef.from_value(node['model']))
        spec = spec_for_graph(changed, original)
        spec.meta['task_design'] = graph_identity(design, state)
        pipeline = PipelineStore(project)
        graph_id = ((base or {}).get('meta') or {}).get('pipeline_graph_id', '')
        version = ((base or {}).get('meta') or {}).get('pipeline_graph_version', 1)
        compiled = to_pipeline_graph(spec, graph_id=graph_id, version=version)
        plans, flows = srv._conversation_plan_store(cid), srv._conversation_flow_store(cid)
        guard()
        _, saved, _, _ = save_consistently(project, spec=spec, graph=compiled, pipeline_store=pipeline,
            plan_store=plans, flow_store=flows, previous=plans.get(project), note='编辑工作流')
        raw = saved.as_dict()
        changed_ids = sync_materials(state, raw, previous=_baseline(state, base), operation=body.get('operation'))
        added = [path for path, _ in walk(changed) if path not in before]
        if added:
            changed_ids = added
        state['workflow_canvas'] = dict(history=runtime['draft_history'], future=runtime['draft_future'])
        state['revision'] = state.get('revision', 0) + 1
        design['revision'] += 1
        design.update(updated_at=now(), reason='工作流已保存，可继续编辑或构图。')
        if (design.get('build_request') or {}).get('state') == 'saved':
            design['build_request'].update(saved_spec_hash=digest(raw), graph_version=saved.version)
        store._put(db, state)
    return dict(result, changed_ids=changed_ids, execution_started=False,
                view=workbench.view(dict(body, task_id=design['task_id'])))
