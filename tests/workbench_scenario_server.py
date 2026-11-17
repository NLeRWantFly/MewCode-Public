"""Disposable T95 browser host: production routes, stores, scheduler and acceptance.

The deterministic design compiler and node/review callbacks are synthetic. They
operate only on fixture files. No front-end state or task projection is mocked.
External sockets are rejected before importing the application.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['classic', 'collaborative', 'autonomous', 'queue'], required=True)
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    orchestration_mode = 'collaborative' if args.mode == 'autonomous' else args.mode
    repository = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repository / 'tests'))
    root = repository / ('.tmp-chat-queue-integration-t95-' + uuid.uuid4().hex[:10])
    runtime_root = repository / '.tmp' / ('t95-runtime-' + uuid.uuid4().hex[:10])
    Path.home = classmethod(lambda cls: runtime_root / 'home')
    os.environ.update(MEWCODE_OFFLINE_TEST='1', MEWCODE_USER_DATA_DIR=str(runtime_root / 'userdata'),
                      MEWCODE_DESKTOP_PROFILE_ISOLATED='1')
    os.environ.pop('MEWCODE_DESKTOP_TOKEN', None)
    connect = socket.socket.connect
    def local_connect(sock, address):
        if isinstance(address, tuple) and address[0] not in {'127.0.0.1', 'localhost', '::1'}:
            raise OSError('T95 fixture forbids external connections')
        return connect(sock, address)
    socket.socket.connect = local_connect

    import uvicorn
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
    from frontend import server
    from frontend.task_design import install as install_design
    from frontend.workbench_api import install as install_workbench
    from frontend.workbench_revision import sources
    from mewcode.agent import AskUserRequest
    from mewcode.expert_group import ExpertGroupOrchestrator
    from mewcode.expert_runs import ExpertRunStore, apply_recovery_decision
    from mewcode.experts.flow import ExpertFlowSpec
    from mewcode.experts.waits import BranchWaitPaused
    from mewcode.kernel.events import digest
    from mewcode.kernel import collaboration as ledger
    from mewcode.orchestration.collab_coordinator import CollaborationCoordinator
    from mewcode.orchestration.draft_store import DraftStore
    from mewcode.orchestration.plan_session import PlanSessionStore
    from mewcode.orchestration.task_design import TaskDesign
    from mewcode.permissions import PermissionMode
    from tests.chat_queue_render_server import fixture_app, CONVERSATION_ID as cid, FIXTURE_TOKEN
    from tests.test_collaboration_mode import flow, delivery, PROFILE, CATALOG, EXECUTOR, REVIEWER
    from tests.test_workflow_drafts import write_history, record as history_record

    app = fixture_app(root)
    project = root / 'project'
    history_path = project / '.mewcode' / 'sessions' / (cid + '.jsonl')
    write_history(history_path, [])
    store = ExpertRunStore(root / 'runs')
    plans = PlanSessionStore(root / 'plans.json')
    state = dict(history=[], clients=set(), tasks=set(), run_id='', flow=None, error='',
                 invoked=[], reviews=[], decisions=[], answered=None, hold=None, execution=None, revision_ready=False)
    runtime = SimpleNamespace(conversation_id=cid, work_dir=str(project), expert_run_id='', ask_user_request=None)
    session = SimpleNamespace(work_dir=str(project), session=SimpleNamespace(session_id=cid, _sessions_dir=history_path.parent),
        runtime_for=lambda _: runtime, chat_busy=lambda *_: False, expert_mode=False, orchestration_planning=False,
        permission_mode=PermissionMode.DEFAULT)
    def setting(name):
        def apply(value): setattr(session, name, value)
        return apply
    session.set_expert_mode = setting('expert_mode')
    session.set_orchestration_planning = setting('orchestration_planning')
    original_state = next(r.endpoint for r in app.routes if getattr(r, 'path', '') == '/api/state')
    def client_state():
        value = original_state()
        value['status'].update(orchestration_planning=session.orchestration_planning, expert_mode=False,
                               conversation_title='任务工作台端到端验收')
        return value
    async def broadcast(payload):
        for client in list(state['clients']):
            try: await client.send_json(dict(payload, conversation_id=cid))
            except Exception: state['clients'].discard(client)
    srv = SimpleNamespace(app=app, SESSION=session, EXPERT_RUNS=store, _active_conversation_id=lambda: cid,
        _conversation_plan_store=lambda _: plans,
        EXPERT_LESSONS=SimpleNamespace(list=lambda _: []), WS_EVENTS=SimpleNamespace(send_json=broadcast),
        validate_answer_notes=server.validate_answer_notes, _resolve_prepared_response=server._resolve_prepared_response)
    install_design(app, srv)
    install_workbench(app, srv)
    # The confirmation preview also reads generation progress. Mount the real
    # read route against this fixture's DraftStore, without exposing build IO.
    from frontend.orch_drafts import install as install_drafts
    from mewcode.cordis import Context
    srv.APP_CONTEXT = Context('workbench-browser-fixture')
    progress_app = FastAPI()
    install_drafts(progress_app, srv)
    progress_route = next(r for r in progress_app.routes
                          if getattr(r, 'path', '') == '/api/orchestration/graph-autobuild/progress')
    app.add_api_route(progress_route.path, progress_route.endpoint, methods=['GET'])
    if args.mode == 'queue':
        sock = socket.socket(); sock.bind(('127.0.0.1', args.port))
        print('WORKBENCH_SCENARIO ' + json.dumps(dict(port=sock.getsockname()[1], root=str(root))), flush=True)
        uvicorn.Server(uvicorn.Config(app, log_level='warning')).run(sockets=[sock])
        return

    async def coordinate(model, payload, system):
        if args.mode == 'autonomous':
            state['decisions'].append('review' if 'fragment' in payload else 'plan')
            if 'fragment' not in payload:
                from tests.test_autonomous_rolling_fragment import _proposal
                return _proposal()
        if 'message' in payload:
            return dict(action='revise', summary='按用户原话补充逐条来源', changes=[
                dict(entry_id='goal', field='body', body=payload['message']['text'])])
        return dict(passed=True, evidence=['固定夹具复核：两步和来源要求完整保留'])
    async def coordinator(record, _cid):
        return CollaborationCoordinator(store, record['run_id'], CATALOG, ask=coordinate)
    srv.WORKBENCH_REVISIONS.coordinator = coordinator

    def background(coro):
        task = asyncio.create_task(coro)
        state['tasks'].add(task)
        def finished(value):
            state['tasks'].discard(value)
            if not value.cancelled() and value.exception():
                import traceback
                state['error'] = ''.join(traceback.format_exception(value.exception()))
                print(state['error'], flush=True)
        task.add_done_callback(finished)
        return task

    async def reviewed(model, payload, context):
        state['reviews'].append(context.get('node_id'))
        return dict(passed=True, evidence=['离线固定内容已逐字核对'])

    async def runner(node, model, prompt, policy, member=None):
        state['invoked'].append(node.node_id)
        if args.mode == 'autonomous':
            if node.node_id == 'work':
                state['hold'] = asyncio.Event()
                await state['hold'].wait()
            return delivery('固定的当前合同成果：' + node.label)
        if node.node_id == 'deliver' and not state['revision_ready']:
            if args.mode == 'classic':
                from mewcode.experts.recovery_planner import resolve_stall
                from tests.test_recovery_planner import _node
                proposal = resolve_stall({'blocker':'missing_evidence', 'rubric':{'criteria':[
                    {'criterion_id':'REQ-01','severity':'major','acceptance_test':'结果可核验'}]}}, node=_node())['proposal']
                store.mutate(state['run_id'], lambda row: row.update(pending_recovery_revision=proposal), durable=True)
                raise BranchWaitPaused('deliver', reason='等待用户核对计划修订')
            question = dict(name='scope', type='choice', question='交付清单需要覆盖哪些资料？',
                context='两份资料的清单已经整理完成，下一步核对差异并交付。', options=[
                    dict(value='all', label='覆盖全部资料', description='保留全部来源并逐条核对', recommended=True),
                    dict(value='changes', label='只列新增差异', description='仅核对新增内容')])
            request = AskUserRequest('t95-scope', 'AskUserQuestion', [question], asyncio.get_running_loop().create_future())
            runtime.ask_user_request = request
            await broadcast(dict(type='ask_user', request_id=request.request_id, run_id=state['run_id'], questions=[question]))
            from mewcode.orchestration.workbench_events import changed
            changed('runtime', conversation_id=cid, run_id=state['run_id'])
            state['answered'] = await request.future
            runtime.ask_user_request = None
            await broadcast(dict(type='done', status=client_state()['status'], tasks=[], elapsed=0))
            state['hold'] = asyncio.Event()
            await state['hold'].wait()
        name = 'sources.txt' if node.node_id == 'work' else 'delivery.txt'
        text = ('原始资料 A/B，逐条来源已核对。' if node.node_id == 'work' else '差异清单和全部来源。')
        (project / name).write_text(text, encoding='utf-8')
        result = delivery(text)
        result.handoff['artifacts'] = [{'path':name}]
        return result

    async def execute():
        run_id = state['run_id']
        def event(payload):
            # The real web host persists these scheduler events in its nested
            # event_sink. This fixture adapter persists the actual classic
            # events too; append_event alone only saves the event journal.
            if args.mode == 'classic' and payload.get('node_id') and payload.get('type') in {
                    'expert_stage', 'expert_node_completed'} and not payload.get('restored'):
                def stage(row):
                    node = row.setdefault('node_states', {}).setdefault(payload['node_id'], {})
                    if payload.get('status') == 'skipped' and payload.get('reason') == 'checkpoint_completed':
                        return
                    node.update({key: value for key, value in payload.items() if key not in {'type', 'run_id'}})
                store.mutate(run_id, stage, durable=True)
            store.append_event(run_id, payload)
            background(broadcast(payload))
        orchestrator = ExpertGroupOrchestrator(runner=None, node_runner=runner, run_store=store,
            work_dir=project, archive_root=root / 'archive', collaboration_review=reviewed,
            collaboration_coordinate=coordinate, event_sink=event)
        result = await orchestrator.run('整理资料并交付', PROFILE, CATALOG, strategy='stable', run_id=run_id,
                                       flow=state['flow'], checkpoint=store.load(run_id))
        design = TaskDesign(project, cid)
        if result.status == 'completed':
            store.transition(run_id, 'completed')
            design.transition(design.read(), 'completed', '已交付并完成验收。')
        await broadcast(dict(type='done', status=client_state()['status'], tasks=[], elapsed=0))
        return result.status

    async def resume(snapshot, recovery_action='', **kw):
        run_id = snapshot['run_id']
        if recovery_action:
            store.mutate(run_id, lambda row: apply_recovery_decision(row, action=recovery_action), durable=True)
        state['revision_ready'] = True
        state['execution'] = background(execute())
        return dict(started=True, run_id=run_id)
    srv.launch_expert_resume = resume

    async def compile_design(text):
        """Fixed offline compiler output, committed with the real CAS lifecycle."""
        design = TaskDesign(project, cid)
        task = design.record_turn(text)
        if args.mode == 'autonomous':
            from tests.test_autonomous_rolling_runtime import rolling_flow
            raw = rolling_flow().as_dict()
        else:
            raw = flow(args.mode).as_dict()
        raw['label'] = '研究资料比对与交付'
        raw['meta']['task_design'] = {'task_id':task['task_id'], 'orchestration_mode':orchestration_mode}
        for row in raw['nodes']:
            row['label'] = {'work':'整理资料与来源', 'deliver':'核对差异并交付'}[row['node_id']]
        if orchestration_mode == 'collaborative':
            spec = raw['meta']['collaboration_spec']
            spec.update(task_id=task['task_id'], goal=text)
            spec['document']['entries'][0]['body'] = text
        draft = DraftStore(project, cid)
        with draft.db() as db:
            saved = draft._get(db)
            saved['materials'] = {'sequence':1, 'document':{'entries':[
                {'id':n['node_id'], 'kind':'node', 'body':n['label'], 'state':'active'} for n in raw['nodes']]}}
            draft._put(db, saved)
        value = design.queue_build(task_id=task['task_id'], revision=task['revision'], sequence=1,
                                   source_hash=digest(text), base_graph_hash='')
        value = design.claim_build(value)
        expected = dict(value['build_request'], task_id=value['task_id'], task_revision=value['revision'],
                        build_id=value['build_request']['id'], build_epoch=value['build_request']['epoch'])
        with design.commit_build(expected) as receipt: receipt['spec'] = raw
        value = design.claim_execution(design.saved_build(expected))
        run_id = design.execution_run_id(value)
        state.update(run_id=run_id, flow=ExpertFlowSpec.from_value(raw))
        runtime.expert_run_id = run_id
        store.create(run_id=run_id, conversation_id=cid, task=text, attachments=[], profile=PROFILE.as_dict(),
                     strategy='stable', flow=state['flow'])
        store.mutate(run_id, lambda row: row.update(role_models={'roles':{'coordinator':EXECUTOR.as_dict(),
            'executor':EXECUTOR.as_dict(), 'verifier':REVIEWER.as_dict(), 'reviewer':REVIEWER.as_dict()}}), durable=True)
        if orchestration_mode == 'collaborative':
            value = design.bind_run(task_id=value['task_id'], claim_id=value['build_request']['execution_claim_id'], run_id=run_id)
        design.transition(value, 'running', run_id=run_id)
        state['execution'] = background(execute())

    # Use the production redirect endpoints, including the team route.
    for path in ['/orch-canvas', '/teams', '/expert-flow', '/pipeline']:
        original = next(r for r in server.app.routes if getattr(r, 'path', '') == path)
        app.add_api_route(path, original.endpoint, methods=['GET'])
    app.router.routes = [r for r in app.routes if getattr(r, 'path', '') not in
                        {'/ws', '/api/conversations/{scope}/{conversation_id}', '/api/orchestration/current'}]
    @app.get('/api/conversations/{scope}/{conversation_id}')
    def conversation(scope: str, conversation_id: str):
        return dict(id=cid, scope='project', entries=state['history'], pipeline_events=[], expert_runs=[], running=False)
    @app.get('/api/orchestration/current')
    def current(): return dict(project=str(project), custom=False, spec=None, flow=None)
    @app.get('/api/expert-runs/{run_id}')
    def get_run(run_id: str): return dict(snapshot=store.load(run_id))
    @app.get('/api/orchestration/run-state')
    def run_state(): return dict(run=store.load(state['run_id']) if state['run_id'] else None, conversation_id=cid)
    @app.get('/__test__/workbench')
    def report():
        return dict(mode=args.mode, run_id=state['run_id'], error=state['error'], invoked=state['invoked'],
                    reviews=state['reviews'], decisions=state['decisions'], answered=state['answered'], source_count=len(sources(srv,str(project),cid)),
                    design=TaskDesign(project,cid).read())
    @app.post('/__test__/release-current')
    async def release_current():
        if args.mode != 'autonomous' or state['hold'] is None:
            raise ValueError('No current synthetic-provider wait')
        state['hold'].set()
        return dict(ok=True)
    @app.post('/__test__/review-and-resume')
    async def review_resume():
        state['revision_ready'] = True
        state['hold'].set()
        await state['execution']  # Late old result must settle before a new attempt.
        coord = await coordinator(store.load(state['run_id']), cid)
        for identity in store.load(state['run_id'])['collaboration']['proposals']:
            await coord.review_proposal(identity)
        state['execution'] = background(execute())
        return dict(ok=True)
    @app.websocket('/ws')
    async def websocket(ws: WebSocket):
        if ws.cookies.get(server.DESKTOP_COOKIE) != FIXTURE_TOKEN:
            await ws.close(code=1008); return
        await ws.accept(); state['clients'].add(ws)
        await ws.send_json(dict(type='state', **client_state()))
        try:
            while True:
                msg = await ws.receive_json()
                if msg.get('type') == 'chat':
                    text = msg.get('text', '')
                    state['history'].append(dict(role='user', text=text))
                    write_history(history_path, [history_record('user', text)], append=True)
                    await broadcast(dict(type='turn_started', text=text, client_echoed=True, expert_mode=False))
                    answer = '已整理目标，开始按固定离线方案执行。' if not state['run_id'] else '请点击这条消息的“作为要求修改”，再核对修订草稿。'
                    state['history'].append(dict(role='assistant', text=answer))
                    await broadcast(dict(type='delta', text=answer))
                    await broadcast(dict(type='done', status=client_state()['status'], tasks=[], elapsed=0))
                    if not state['run_id']: background(compile_design(text))
        except WebSocketDisconnect: pass
        finally: state['clients'].discard(ws)

    # Team data uses the same real host and deterministic executor as T83.
    if args.mode == 'collaborative':
        from tests.test_workbench_team_view import create_team
        from mewcode.teams.interventions import InterventionService
        host, team_run, _, _ = create_team(project)
        InterventionService(host.runtime).submit(host.actor, team_run, request=dict(intent='pause',
            message='先暂停检查交付', expected_goal_version=1, expected_plan_revision=1),
            control_epoch=1, command_id='t95-intervention')
        app.state.team_v2_host = host
        app.state.team_v2_authorize = lambda request: request.cookies.get(server.DESKTOP_COOKIE) == FIXTURE_TOKEN

    sock = socket.socket(); sock.bind(('127.0.0.1', args.port))
    print('WORKBENCH_SCENARIO ' + json.dumps(dict(port=sock.getsockname()[1], root=str(root))), flush=True)
    uvicorn.Server(uvicorn.Config(app, log_level='warning')).run(sockets=[sock])


if __name__ == '__main__': main()
