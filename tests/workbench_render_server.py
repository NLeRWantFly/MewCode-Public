"""Offline visual fixture using unmodified production HTML, CSS and app.js.

T81 screenshots show seeded running records, not live execution evidence.
Run as a module with --mode classic or collaborative. All state is disposable.
"""
import argparse
import asyncio
import os
from pathlib import Path
import socket
import uuid
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["classic", "collaborative"], required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--inbox", action="store_true")
    parser.add_argument("--revision", action="store_true")
    parser.add_argument("--structure", action="store_true")
    parser.add_argument("--design", action="store_true")
    parser.add_argument("--canvas", action="store_true", help="Seed a branched classic graph for canvas interaction checks")
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    root = repository / (".tmp-chat-queue-integration-workbench-" + uuid.uuid4().hex[:10])
    runtime_root = repository / ".tmp" / ("workbench-runtime-" + uuid.uuid4().hex[:10])
    Path.home = classmethod(lambda cls: runtime_root / "home")
    os.environ["MEWCODE_OFFLINE_TEST"] = "1"
    os.environ["MEWCODE_USER_DATA_DIR"] = str(runtime_root / "userdata")
    os.environ["MEWCODE_DESKTOP_PROFILE_ISOLATED"] = "1"
    os.environ.pop("MEWCODE_DESKTOP_TOKEN", None)
    original_connect = socket.socket.connect

    def local_connect(sock, address):
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "localhost", "::1"}:
            raise OSError("Offline visual fixture forbids external connections")
        return original_connect(sock, address)

    socket.socket.connect = local_connect
    import uvicorn
    from fastapi import WebSocket, WebSocketDisconnect
    from frontend import server
    from mewcode.kernel import collaboration as ledger
    from mewcode.orchestration.collab_service import projection
    from tests.chat_queue_render_server import fixture_app, CONVERSATION_ID, FIXTURE_TOKEN
    from tests.test_collaboration_mode import flow, PROFILE, RUN_ID, accept
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.experts.flow import ExpertFlowSpec
    from frontend.workbench_api import install as install_workbench
    from mewcode.orchestration.task_design import TaskDesign

    goal = "整理两份研究资料，核对差异并生成可追溯的交付包"
    label = "协作" if args.mode == "collaborative" else "经典"
    app = fixture_app(root, initial_history=[
        {"role": "user", "text": goal},
        {"role": "assistant", "text": "已将要求整理为编排草稿：读取资料后并行整理，再核对差异和来源，最后生成交付包。可以继续补充要求。" if args.design else "已完成资料盘点，正在核对差异，并保留每条结论的来源。"},
    ])
    design_service = TaskDesign(root / 'project', CONVERSATION_ID)
    real_design = design_service.start('execute', orchestration_mode=args.mode) if args.revision else None
    if args.design:
        from mewcode.orchestration.draft_store import DraftStore
        design_service.start('execute', orchestration_mode=args.mode)
        design_service.record_turn(goal)
        draft = DraftStore(root / 'project', CONVERSATION_ID)
        with draft.db() as db:
            value = draft._get(db)
            value['materials'] = {'sequence':1, 'document': {'entries': [
                {'id':'goal', 'kind':'goal', 'title':'研究资料比对与交付', 'body':goal, 'evidence':'user_source', 'sources':[{'source_id':'fixture-source','quote':goal}]},
                {'id':'milestone', 'kind':'milestone', 'title':'核对差异', 'body':'逐条比较两份资料，标记共同结论与差异。', 'sources':[]},
                {'id':'limit', 'kind':'constraint', 'title':'保留资料原文', 'body':'记录原始出处，不改写引用内容。', 'sources':[]},
                {'id':'check', 'kind':'acceptance', 'title':'每条结论有据可查', 'body':'交付包中的每条结论都可以追溯到原始资料。', 'sources':[]},
            ]}}
            for entry in value['materials']['document']['entries']:
                entry.update(state='active', data={})
            draft._put(db,value)
    raw = flow(args.mode).as_dict()
    if real_design:
        raw['meta']['task_design'] = {'task_id': real_design['task_id'], 'orchestration_mode': args.mode}
        if args.mode == 'collaborative':
            raw['meta']['collaboration_spec']['task_id'] = real_design['task_id']
    raw['label'] = '研究资料比对与交付'
    names = {'work': '整理资料与来源', 'deliver': '核对差异并交付'}
    for row in raw['nodes']:
        row.update(label=names[row['node_id']], responsibility='保留来源，逐项核对两份资料的差异。')
    if args.canvas:
        if args.mode != 'classic':
            parser.error('--canvas requires --mode classic')
        canvas_nodes = [
            ('sources', '读取资料', '读取两份研究资料，提取标题、章节与来源。'),
            ('extract_a', '整理资料 A', '保留原文和出处，提取资料 A 的结论与关键数据。'),
            ('extract_b', '整理资料 B', '保留原文和出处，提取资料 B 的结论与关键数据。'),
            ('compare', '比较差异', '逐项比较共同结论、数据差异和缺失内容。'),
            ('verify', '核验来源', '根据原始资料核验差异，标记证据不足的结论。'),
            ('report', '生成对比报告', '按主题组织差异说明，附上对应来源。'),
            ('deliver', '交付文件', '汇总报告与引用清单，生成可追溯的交付包。'),
        ]
        raw['nodes'] = [{**raw['nodes'][0], 'node_id':key, 'label':name, 'responsibility':detail}
                        for key, name, detail in canvas_nodes]
        raw['edges'] = [{'from_node':a, 'to_node':b} for a, b in [
            ('sources','extract_a'), ('sources','extract_b'), ('extract_a','compare'),
            ('extract_b','compare'), ('extract_a','verify'), ('extract_b','verify'),
            ('compare','report'), ('verify','report'), ('report','deliver')]]
        raw['meta']['node_plan_briefs'] = {key: {'known_inputs':['来源资料'], 'outputs':[name],
            'acceptance':['保留可追溯的来源']} for key, name, _ in canvas_nodes}
        if args.design:
            with draft.db() as db:
                value=draft._get(db)
                value['materials']['document']['entries'].extend([
                    {'id':key, 'kind':'node', 'state':'active', 'title':name, 'body':detail,
                     'sources':[{'source_id':'fixture-source','quote':goal}],
                     'data':{'inputs':['两份研究资料'], 'outputs':[name], 'acceptance':['保留可追溯的来源']}}
                    for key, name, detail in canvas_nodes])
                value['materials']['document']['entries'].extend([
                    {'id':f'edge-{index}', 'kind':'edge', 'state':'active', 'title':'', 'body':'', 'sources':[],
                     'data':edge} for index, edge in enumerate(raw['edges'])])
                draft._put(db,value)
    if args.mode == 'collaborative':
        raw['meta']['workflow_draft'] = {'parents': {'work': 'research', 'deliver': 'delivery'}}
        spec = raw['meta']['collaboration_spec']
        spec['goal'] = goal
        spec['document']['entries'][0]['body'] = goal
        spec['document']['entries'].extend([
            {'id': 'research', 'kind': 'milestone', 'body': '整理研究资料', 'data': {}},
            {'id': 'delivery', 'kind': 'milestone', 'body': '差异核验与交付', 'data': {}},
        ])
    store = ExpertRunStore(root / 'runs')
    store.create(run_id=RUN_ID, conversation_id=CONVERSATION_ID, task=goal, attachments=[],
                 profile=PROFILE.as_dict(), strategy='stable', flow=ExpertFlowSpec.from_value(raw))

    def seed(record):
        record.update(conversation_id=CONVERSATION_ID, status="running", task=goal)
        record["node_states"] = {
            "work": {"status": "completed", "label": "整理资料与来源", "attempt": 1,
                     "text": "已整理资料清单与可追溯来源。"},
            "deliver": {"status": "running", "label": "核对差异并交付", "attempt": 1,
                "tools": {"source-check": {"tool_id": "source-check", "name": "核对来源", "args": {"source": "研究资料 A/B"},
                    "status": "completed", "output": "来源已核对，发现 3 处需要逐项比较的差异。"}},
                "text": "正在对照原始来源核对差异，并整理可追溯的交付说明。"},
        }
        if args.canvas:
            record['node_states'] = {key: {'status': 'completed' if index<3 else 'running' if index==3 else 'pending',
                'label':name, 'attempt':1 if index<=3 else 0} for index, (key,name,_) in enumerate(canvas_nodes)}
        if args.mode == "collaborative":
            if args.revision:
                from mewcode.kernel.acceptance import Acceptance
                from tests.test_collaboration_mode import passing_report
                (root / 'project' / 'sources.txt').write_text('资料 A：原始来源已核对。\n资料 B：三处差异已记录。', encoding='utf-8')
                attempt = ledger.dispatch(record['collaboration'], 'work')
                report = passing_report(record['collaboration'])
                report['artifacts'] = Acceptance(root / 'project').freeze({'artifacts': [{'path': 'sources.txt'}]})
                ledger.complete(record['collaboration'], attempt['id'], report)
            else:
                accept(record["collaboration"])
            record['collaboration']['milestones']['research']['status'] = 'accepted'  # Offline display seed.
            ledger.dispatch(record["collaboration"], "deliver")

    store.mutate(RUN_ID, seed, durable=True)
    if real_design:
        real_design = design_service.transition(real_design, 'running', run_id=RUN_ID)
    runtime = SimpleNamespace(expert_run_id=RUN_ID, work_dir=str(root / 'project'), conversation_id=CONVERSATION_ID)
    if args.inbox:
        from mewcode.agent import AskUserRequest
        runtime.ask_user_request = AskUserRequest('offline-choice', 'AskUserQuestion', [dict(name='scope', type='choice',
            question='交付清单需要覆盖哪些资料？', context='目前已核对两份文档，下一步将整理可追溯的差异清单。',
            options=[dict(value='all', label='覆盖全部资料', description='保留全部来源，形成完整清单。', recommended=True),
                     dict(value='changes', label='只列新增差异', description='输出更精简，只保留这次发生变化的部分。')])],
            asyncio.new_event_loop().create_future())
    srv = SimpleNamespace(app=app, EXPERT_RUNS=store, EXPERT_LESSONS=SimpleNamespace(list=lambda _: []),
        SESSION=SimpleNamespace(work_dir=str(root / 'project'), runtime_for=lambda _: runtime, chat_busy=lambda _: False,
            permission_mode=server.SESSION.permission_mode),
        WS_EVENTS=SimpleNamespace(send_json=app.state.fixture_broadcast), _active_conversation_id=lambda: CONVERSATION_ID,
        _conversation_plan_store=server._conversation_plan_store,
        validate_answer_notes=server.validate_answer_notes, _resolve_prepared_response=server._resolve_prepared_response)
    install_workbench(app, srv)
    if args.design:
        srv.EXPERT_RUNS = ExpertRunStore(root / 'empty-runs')
    if args.revision:
        from fastapi import FastAPI
        from frontend.task_design import install as install_design
        design_app = FastAPI()
        install_design(design_app, srv)
        for route in design_app.routes:
            if getattr(route, 'path', '') == '/api/orchestration/task-design/revision':
                app.add_api_route(route.path, route.endpoint, methods=['POST'])
        from tests.test_workflow_drafts import write_history, record as history_record
        history_path = root / 'project' / '.mewcode' / 'sessions' / (CONVERSATION_ID + '.jsonl')
        write_history(history_path, [history_record('user', goal)])
        srv.SESSION.session = SimpleNamespace(session_id=CONVERSATION_ID, _sessions_dir=history_path.parent)
        from mewcode.orchestration.collab_service import user_revision
        user_revision(store, store.load(RUN_ID), entry_id='goal', body=goal + '，补充逐条来源', expected_version=1, request_id='fixture-revision')
        if args.structure:
            def adopt(saved):
                value = saved['collaboration']
                ledger.adopt(value, next(iter(value['proposals'])), review={'passed': True, 'evidence': ['offline display fixture']},
                             proposer_vendor='synthetic-a', reviewer_vendor='synthetic-b')
            store.mutate(RUN_ID, adopt, durable=True)
    record = store.load(RUN_ID)
    task = {"task_id": "task-one", "run_id": RUN_ID, "revision": 1, "intent": "execute",
            "status": "running", "goal": goal, "summary": "资料盘点已完成，正在核对差异。",
            "reason": "离线运行状态样例，用于重构前的界面对照。", "orchestration_mode": args.mode}
    if args.mode == "collaborative":
        task["collaboration"] = projection(record)
    snapshot = store.public(RUN_ID)
    endpoints = {route.path: route.endpoint for route in app.routes if hasattr(route, "endpoint")}
    replaced = {"/api/state", "/api/conversations/{scope}/{conversation_id}", "/ws"}
    app.router.routes[:] = [route for route in app.routes if getattr(route, "path", "") not in replaced]

    def client_state():
        result = endpoints["/api/state"]()
        result["status"].update(conversation_title=label + "任务基线", expert_mode=True,
                                execution_kind="orchestration")
        result["running_conversations"] = [CONVERSATION_ID]
        if args.design:
            result['status'].update(expert_mode=False, orchestration_planning=True)
            result['running_conversations'] = []
        return result

    @app.get("/api/state")
    def state():
        return client_state()

    @app.get("/api/conversations/{scope}/{conversation_id}")
    def conversation(scope: str, conversation_id: str):
        result = endpoints["/api/conversations/{scope}/{conversation_id}"](scope, conversation_id)
        result.update(expert_runs=[] if args.design else [snapshot], running=not args.design)
        return result

    @app.get("/api/orchestration/task-design")
    def design():
        if args.design:
            return {'project':str(root/'project'), 'conversation_id':CONVERSATION_ID, 'task_design':design_service.read()}
        if args.revision:
            return {'project': str(root / 'project'), 'conversation_id': CONVERSATION_ID,
                    'task_design': {**design_service.read(), 'collaboration': projection(store.load(RUN_ID))}}
        return {"project": str(root / "project"), "conversation_id": CONVERSATION_ID, "task_design": task}

    @app.get('/api/orchestration/graph-autobuild/progress')
    def design_progress():
        return {'project':str(root/'project'), 'conversation_id':CONVERSATION_ID, 'running':True,
                'candidate':{'log':[{'status':'ok','duration_ms':8300}]},
                'process':{'headline':'正在核对要求与资料来源','stages':['整理历史','补全结构','独立审查','交付'],
                           'stage_index':0,'last_result':'已整理目标、约束与验收标准。','next_step':'根据要求生成可执行的步骤。'}}

    @app.get('/api/orchestration/draft/source/{source_id}')
    def design_source(source_id:str):
        return {'text':goal,'role':'user','at':'离线夹具'}

    @app.get('/api/expert-runs/{run_id}/files/{file_id}')
    def file_preview(run_id: str, file_id: str, conversation_id: str):
        from frontend.workbench_files import collaborative_file
        if run_id != RUN_ID or conversation_id != CONVERSATION_ID:
            raise ValueError('Fixture scope mismatch')
        return {'file': collaborative_file(store.load(run_id), file_id, root / 'project')}

    @app.get("/api/expert-runs/{run_id}")
    def run(run_id: str):
        return {"snapshot": snapshot}

    @app.get("/api/orchestration/run-state")
    def run_state():
        return {"run": snapshot, "conversation_id": CONVERSATION_ID}

    @app.websocket("/ws")
    async def websocket(websocket: WebSocket):
        if websocket.cookies.get(server.DESKTOP_COOKIE) != FIXTURE_TOKEN:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        await websocket.send_json({"type": "state", **client_state()})
        await asyncio.sleep(0.2)
        if not args.design:
            await websocket.send_json({"type": "expert_snapshot", "snapshot": snapshot,
                                       "conversation_id": CONVERSATION_ID})
        if args.inbox and not runtime.ask_user_request.future.done():
            await websocket.send_json(dict(type='ask_user', request_id='offline-choice', run_id=RUN_ID,
                conversation_id=CONVERSATION_ID, questions=runtime.ask_user_request.questions))
        try:
            while True:
                await websocket.receive_json()
        except WebSocketDisconnect:
            pass

    sock = socket.socket()
    sock.bind(("127.0.0.1", args.port))
    print(f"WORKBENCH_FIXTURE mode={args.mode} port={sock.getsockname()[1]} root={root}", flush=True)
    uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[sock])


if __name__ == "__main__":
    main()
