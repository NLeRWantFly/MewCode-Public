"""Disposable browser acceptance: real reporting tool -> store -> canvas assets."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import sys

from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.tools.report_step import ReportStepParams, ReportStepTool

RUN = 'f' * 32
STATIC = Path(__file__).resolve().parents[1] / 'frontend/static'
PAGE = '''<!doctype html><html lang="zh-CN" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>书本进度画板 · 交互验收</title>
<link rel="stylesheet" href="/static/workbench/workbench.css?v=20261102-progress">
<style>body{margin:0;font-family:system-ui,sans-serif}.fixture{height:46px;display:flex;align-items:center;gap:14px;padding:0 20px;background:#f7f9fc;color:#465269;font-size:12px}.fixture button{padding:5px 9px;border:1px solid #cbd5e1;border-radius:5px;background:white;cursor:pointer}#task-workbench{top:46px}.wb-structure-content{height:100%}</style></head><body>
<header class="fixture"><strong>交互验收样例 · 非现场业务数据</strong><button data-action="fixing">开始修正</button><button data-action="resolved">修正后待复核</button><button data-action="unknown">总量未知</button><button id="theme">切换主题</button></header>
<main id="task-workbench"><section class="wb-structure-content"></section></main>
<script type="module">
import {mountStructure} from '/static/workbench/structure.js?v=20261102-progress';
const ui=mountStructure(document.querySelector('.wb-structure-content'),{});
async function refresh(){const response=await fetch('/view');ui.update(await response.json());}
for(const button of document.querySelectorAll('[data-action]'))button.addEventListener('click',async()=>{await fetch('/scenario',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:button.dataset.action})});await refresh();});
document.querySelector('#theme').onclick=()=>document.documentElement.dataset.theme=document.documentElement.dataset.theme==='light'?'dark':'light';
await refresh();setInterval(refresh,2000);
</script></body></html>'''


def main():
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=False)
    store = ExpertRunStore(root / 'runs', storage='sqlite')
    items = [('read', '逐页阅读与整理', 'executor'), ('fuse', '整理本页内容', 'executor'),
             ('review', '原图复核（grok-4.7/xhigh）', 'auditor'), ('audit', '核对文字与公式', 'auditor'),
             ('check', '检查本批结果', 'auditor'), ('human', '人工确认', 'manager'), ('done', '交付整本书', 'manager')]
    flow = ExpertFlowSpec.from_value({'flow_id': 'book-progress', 'finalizer_node_id': 'done',
        'nodes': [{'node_id': key, 'label': label, 'role': role, 'capabilities': {}} for key, label, role in items],
        'edges': [{'from_node': left[0], 'to_node': right[0]} for left, right in zip(items, items[1:])],
        'meta': {'loops': [{'loop_id': 'book-reading', 'label': '逐页识别与复核', 'entry_node_id': 'read',
            'check_node_id': 'check', 'node_ids': ['read', 'fuse', 'review', 'audit', 'check'],
            'reviewer_node_ids': ['review', 'audit', 'check'], 'exit_condition': '逐页核对全书',
            'criteria': [{'criterion_id': 'pages', 'acceptance_test': '逐页核对全书'}]}]}})
    store.create(run_id=RUN, conversation_id='book-preview', task='阅读并核对《电路基础》全书', attachments=[],
        profile={}, strategy='stable', flow=flow)
    def seed(record):
        record.update(status='running', active_node_ids=['read'], stage_cursor='read',
            node_states={key: {'node_id': key, 'label': label, 'role': role,
                'status': 'running' if key == 'read' else 'completed' if key in {'fuse', 'review', 'audit'} else 'pending',
                'node_attempt': 1, 'epoch': 0, 'input_revision': 'v1', 'tools': {}} for key, label, role in items},
            loop_states={'book-reading': {'status': 'running', 'round': 3}})
    store.mutate(RUN, seed)

    def report(action):
        progress = {'scope': '《电路基础》', 'unit': '页', 'current': 5, 'completed': 4}
        if action != 'unknown':
            progress['total'] = 110
        state = action if action in {'fixing', 'resolved'} else 'open'
        node_id = 'review' if state == 'open' else 'read'
        summary = {'open': '这本书读到第5页。后续复核发现一处公式缺少分母，正在回到原页核对。',
            'fixing': '正在修正第5页第二个公式，其他已整理的页面保留。',
            'resolved': '第5页公式已补齐，下一步重新复核这处修改。'}[state]
        params = ReportStepParams(label='核对第5页公式', summary=summary, progress=progress,
            next_step='补齐公式后重新复核，再继续阅读后面的页面。',
            issues=[{'id': 'page-5-formula-2', 'scope': '《电路基础》', 'location': '第5页 · 第二个公式',
                'message': '公式缺少分母，暂时不能确认这一页的计算结果。', 'state': state,
                'next_step': '对照原书补齐分母，再请复核确认。' if state != 'resolved' else '重新核对这处修改，确认后继续第6页。'}])
        result = asyncio.run(ReportStepTool().execute(params))
        seq = store.read_execution_fields(RUN, 'last_seq')['last_seq'] + 1
        store.append_sequenced_events(RUN, [
            dict(type='expert_tool', seq=seq, node_id=node_id, node_attempt=1, id=f'report-{seq}',
                 name='report_step', args=params.model_dump()),
            dict(type='expert_tool_result', seq=seq+1, node_id=node_id, id=f'report-{seq}',
                 is_error=False, output=result.output, host_observation=result.host_observation)], return_snapshot=False)
    report('open')

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split('?', 1)[0]
            if path == '/':
                data, mime = PAGE.encode('utf-8'), 'text/html; charset=utf-8'
            elif path == '/view':
                view = store.read_workbench(RUN)['_workbench_view']
                data, mime = json.dumps(view, ensure_ascii=False).encode('utf-8'), 'application/json; charset=utf-8'
            elif path.startswith('/static/'):
                target = (STATIC / path.removeprefix('/static/')).resolve()
                if not target.is_relative_to(STATIC.resolve()) or not target.is_file():
                    self.send_error(404)
                    return
                data, mime = target.read_bytes(), mimetypes.guess_type(target)[0] or 'application/octet-stream'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_POST(self):
            if self.path != '/scenario':
                self.send_error(404)
                return
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            if body.get('action') not in {'fixing', 'resolved', 'unknown'}:
                self.send_error(400)
                return
            report(body['action'])
            self.send_response(204)
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', int(sys.argv[2]) if len(sys.argv) > 2 else 0), Handler)
    print(f'LOOP_PROGRESS_PREVIEW http://127.0.0.1:{server.server_port}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
