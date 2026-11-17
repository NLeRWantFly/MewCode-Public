"""Prepare the user's confirmed 11-stage cleanup without touching live stores.

The explicit --apply option backs up and synchronizes the exact graph, plan and
flow via the real canvas API. Never starts a run or edits transcript/checkpoints.
"""
import argparse
import asyncio
import json
import shutil
from pathlib import Path

import httpx

from mewcode.orchestration.pipeline_bridge import from_pipeline_graph
from mewcode.pipeline.models import PipelineGraph

KEEP = ['inspect_handoff', 'collect_evidence', 'check_interfaces', 'confirm_scope',
        'revise_methods', 'revise_experiments', 'repair_figure_five', 'assemble_package',
        'audit_package', 'human_handoff', 'summarize']


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--layout', type=Path)
    args = parser.parse_args()
    project, data, output = args.project.resolve(), args.data.resolve(), args.output.resolve()
    target = project / '.mewcode/pipelines/g_acaaff32.json'
    original = json.loads(target.read_text(encoding='utf-8'))
    graph = json.loads(json.dumps(original))
    live = set(KEEP) | {'__orch_start__', '__orch_end__'}
    assert live <= {n['id'] for n in graph['nodes']}, 'Confirmed stages missing; do not guess'
    graph['nodes'] = [n for n in graph['nodes'] if n['id'] in live]
    graph['edges'] = [e for e in graph['edges'] if (e.get('from_node') or e.get('from')) in live
                      and (e.get('to_node') or e.get('to')) in live]
    assert len(graph['edges']) == 15, 'Confirmed 15-edge DAG changed; inspect before applying'
    payload = json.loads(graph['variables']['__orch.payload__'])
    payload['nodes'] = {nid:meta for nid,meta in payload['nodes'].items() if nid in KEEP}
    for meta in payload['nodes'].values():
        for key in ('refine_group','parent_group','group_expanded','canvas_group_size','checkpoint_order'):
            meta.pop(key,None)
        if meta.get('audit_target') not in KEEP:
            meta['audit_target'] = ''
    payload['edges'] = [{'from_node':e.get('from_node') or e.get('from'),
                         'to_node':e.get('to_node') or e.get('to'),
                         'condition':e.get('label') or 'always'} for e in graph['edges']
                        if (e.get('from_node') or e.get('from')) in KEEP and (e.get('to_node') or e.get('to')) in KEEP]
    payload['finalizer_node_id'] = 'summarize'
    payload['meta'].update(canvas_groups={},loops=[],canvas_loop_rewrites=[],canvas_auto_layout_pending=True)
    if args.layout:
        layout = json.loads(args.layout.read_text(encoding='utf-8'))
        assert {n['id'] for n in layout['nodes']} == live, 'Layout scope differs from confirmed stages'
        positions = {n['id']:n['position'] for n in layout['nodes']}
        for node in graph['nodes']:
            node['position'] = positions[node['id']]
        payload['meta'].update(canvas_auto_layout_pending=False,canvas_layout_version=2,canvas_layout_density='compact')
    if isinstance(payload['meta'].get('inventory'),dict):
        inventory = payload['meta']['inventory']
        inventory['stages'] = [s for s in inventory.get('stages',[]) if s.get('stage_id') in KEEP]
    graph['variables']['__orch.payload__'] = json.dumps(payload,ensure_ascii=False)
    cleaned = PipelineGraph.model_validate(graph)
    spec = from_pipeline_graph(cleaned)
    assert {n.node_id for n in spec.nodes} == set(KEEP)
    output.mkdir(parents=True,exist_ok=True)
    (output/'prepared.json').write_text(json.dumps(graph,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'before_tasks':len(original['nodes'])-2,'after_tasks':len(KEEP),
                      'version':original['version'],'edges':len(graph['edges']),'applied':args.apply}))
    if not args.apply:
        return
    from frontend import server
    from mewcode.experts.flow import ExpertFlowStore
    from mewcode.orchestration.plan_session import PlanSessionStore
    plans = PlanSessionStore(data/'orchestration-sessions.json')
    server.get_plan_session_store = lambda:plans
    server.EXPERT_FLOWS_STORE = ExpertFlowStore(data/'expert-flows.json')
    server.SESSION.work_dir = str(project)
    # Refuse reusing a backup directory; all three exact sources must be recoverable.
    backup = output/'backup'
    backup.mkdir(exist_ok=False)
    for source in (target,plans.path,server.EXPERT_FLOWS_STORE.path):
        shutil.copy2(source,backup/source.name)
    # A fresh API read honors monotonic plan revision; retain it in the payload.
    previous = plans.get(project)
    payload['version'] = max(payload.get('version',1),previous.spec.version,original['version'])+1
    graph['variables']['__orch.payload__'] = json.dumps(payload,ensure_ascii=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url='http://test') as client:
        saved=await client.post('/api/orchestration/canvas-sync',json={'graph':graph,'project':str(project)})
        assert saved.status_code==200,saved.text
        for _ in range(2):
            opened=await client.post('/api/orchestration/open-canvas',json={'graph_id':original['graph_id']})
            assert opened.status_code==200,opened.text
            assert {n['id'] for n in opened.json()['graph']['nodes']} == live
        assert {n.node_id for n in plans.get(project).spec.nodes} == set(KEEP)
        (output/'saved.json').write_text(json.dumps(opened.json(),ensure_ascii=False,indent=2),encoding='utf-8')
        print('Verified: saved graph, plan and two reopens contain only the confirmed stages; no execution started.')


if __name__ == '__main__':
    asyncio.run(main())
