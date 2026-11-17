"""Portable graph definitions with a short public delivery summary; no executor."""
from __future__ import annotations

import copy
import json
import re

from .schema import digest, normalize, walk

FORMAT = 'mewcode.workflow.blueprint.v1'
KINDS = {'work': '执行', 'judge': '判断', 'branch': '分支', 'loop': '循环'}
SECRET_FIELDS = {'api_key', 'api_token', 'access_token', 'refresh_token', 'password', 'passwd',
                 'secret', 'client_secret', 'authorization', 'cookie', 'private_key', 'credentials'}
CONCERN = re.compile(r'待复核|未解决|未完成|未确认|缺失|疑点|pending|needs_review|unresolved|失败|异常', re.I)
RECOVERY_BINDINGS = {'recovery_observation', 'local_checkpoint_resume', 'recovered_context', 'runtime_recovery'}
RULES = [
    '保留节点职责、输入输出、依赖、条件出口及循环体；新任务按实际目标替换材料、路径和业务内容。',
    'Core 按依赖推进，可运行的独立节点并发；汇合等待所有前序分支结束，未选中的分支可跳过。',
    '普通节点正常交付即可推进；判断、分支和循环由图中显式组件控制，不额外添加隐式验收或返修循环。',
    '单个工具错误交由节点修正；保留已有成果和未解决事项，外部结果未知时先核对实际状态再续接。',
    '新任务创建独立运行与输出目录；这里的执行状态和交付摘要仅供参考，不作为新任务已完成的证据。',
]


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def pretty(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def excerpt(text, limit=420):
    text = str(text or '').strip()
    return text if len(text) <= limit else text[:limit].rstrip() + '…（摘要截取）'


def public_summary(state):
    nodes = []
    for path, node in walk(state['graph']):
        row = state.get('nodes', {}).get(path, {})
        delivery = state.get('deliveries', {}).get(row.get('published_delivery_id'), {})
        result = delivery.get('result') or row.get('result') or {}
        text = str(result.get('text') or row.get('reason') or '')
        concerns = [excerpt(part, 320) for part in re.split(r'(?<=[。！？\n])', text) if CONCERN.search(part)]
        item = dict(id=path, label=node['label'], status=row.get('status', 'pending'),
                    outcome=excerpt(text), unresolved_notes=list(dict.fromkeys(concerns)),
                    artifacts=[{key: artifact[key] for key in ('name', 'path', 'sha256', 'size', 'kind') if key in artifact}
                               for artifact in result.get('artifacts', [])])
        if 'value' in result:
            item['decision'] = copy.deepcopy(result['value'])
        nodes.append(item)
    # Human/node follow-ups are summarized from public requests/replies only.
    followups = [dict(node_id=row['node_id'], kind=row['kind'], status=row['status'],
                     request=excerpt(row.get('message'), 240), reply=excerpt((row.get('reply') or {}).get('text'), 240))
                 for row in state.get('node_requests', {}).values()]
    return dict(goal=state['graph']['goal'], graph_revision=state['graph']['revision'],
                conversation_summary=state.get('conversation_summary', ''),
                execution_trace=copy.deepcopy(state.get('trace_observations', [])),
                recorded_status=state['status'], nodes=nodes, followups=followups,
                description='依据当前已发布交付和公开追问生成摘要；节点原始工具对话及隐藏推理不包含在内。')


def sanitize(value, redactions, path='$'):
    if isinstance(value, dict):
        result = {}
        sensitive_input = str(value.get('name', '')).casefold() in SECRET_FIELDS
        for key, item in value.items():
            location = path + '.' + str(key)
            if str(key).casefold() in SECRET_FIELDS or (sensitive_input and key in {'value', 'default', 'example'}):
                if item not in (None, ''):
                    result[key] = '<在新任务中绑定凭据>'
                    redactions.append(location)
                    continue
            result[key] = sanitize(item, redactions, location)
        return result
    if isinstance(value, list):
        return [sanitize(item, redactions, f'{path}[{index}]') for index, item in enumerate(value)]
    return copy.deepcopy(value)


def extract(record, selection='applied'):
    """Call on one coherent store revision; never copy checkpoint messages."""
    if selection not in {'applied', 'draft'}:
        raise ValueError('请选择已应用结构或当前草稿。')
    state = record.get('workflow_core')
    if not state:
        raise ValueError('这个运行还没有可提取的新版工作流结构。')
    from .trace import is_trace
    if is_trace(state) and state['status'] != 'completed':
        raise ValueError('任务完成后可提取探索图与对话摘要；提取不会启动执行。')
    graph = state.get('draft') if selection == 'draft' else state['graph']
    if graph is None:
        raise ValueError('当前没有未应用的结构草稿。')
    meta = (record.get('locked_flow') or {}).get('meta') or {}
    redactions = []
    context = dict(default_model=copy.deepcopy((record.get('locked_profile') or record.get('profile') or {}).get('executor')),
                   input_contract=copy.deepcopy(meta.get('run_input_contract') or {}),
                   goal_contract=copy.deepcopy(meta.get('goal_contract') or {}),
                   parameter_examples={key: value for key, value in state.get('inputs', {}).items()
                                       if key != 'runtime_recovery'},
                   input_bindings={path: {key: value for key, value in binding.items() if key not in RECOVERY_BINDINGS}
                                   for path, binding in state.get('bindings', {}).items()
                                   if any(key not in RECOVERY_BINDINGS for key in binding)})
    value = dict(format=FORMAT, source=dict(run_id=record['run_id'],
        conversation_id=record.get('conversation_id', ''), selection=selection,
        graph_revision=graph['revision'], applied_revision=state['graph']['revision'],
        has_unapplied_draft=bool(state.get('draft')), recorded_status=state['status'],
        recorded_at=record.get('updated_at'), kernel_revision=record.get('kernel_revision', 0)),
        workflow=normalize(graph), execution_context=context,
        construction_rules=RULES.copy(), summary=public_summary(state))
    value = sanitize(value, redactions)
    value['redacted_fields'] = redactions
    value['structure_digest'] = digest(value['workflow'])
    return value


def fence(text, language=''):
    # A task can itself contain code fences. Preserve it without breaking the document.
    longest = max([len(part) for part in re.findall(r'`+', text)] + [2])
    marker = '`' * max(3, longest + 1)
    return marker + language + '\n' + text + '\n' + marker


def diagram(graph):
    lines = ['flowchart TD']
    names = {path: 'n' + str(index) for index, (path, _) in enumerate(walk(graph))}
    def label(text):
        return str(text).replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;').replace('>', '&gt;').replace('\n', ' ')
    def visit(part, prefix='', depth=1):
        pad = '  ' * depth
        for node in part['nodes']:
            path = prefix + node['id']
            lines.append(f'{pad}{names[path]}["{label(path)} · {KINDS[node["kind"]]} · {label(node["label"])}"]')
            if node['kind'] == 'loop':
                lines.append(f'{pad}subgraph body_{names[path]}["{label(path)} 的循环体"]')
                visit(node['config']['graph'], path + '/', depth + 1)
                lines.append(pad + 'end')
        for edge in part['edges']:
            lines.append(f'{pad}{names[prefix + edge["source"]]} -->|"{label(edge["port"])}"| {names[prefix + edge["target"]]}')
    visit(graph)
    return '\n'.join(lines)


def summary_markdown(summary):
    lines = [f'依据已应用图 v{summary["graph_revision"]}；记录状态：{summary["recorded_status"]}。',
             summary['description'], '']
    if summary.get('conversation_summary'):
        lines.extend([summary['conversation_summary'], ''])
    for row in summary['nodes']:
        outcome = excerpt(row['outcome'], 260)
        lines.extend([f'### {row["id"]} · {row["label"]}', f'状态：{row["status"]}', outcome or '尚无交付摘要。'])
        if 'decision' in row:
            lines.append('判断/分支结果：' + compact(row['decision']))
        notes = [note for note in row['unresolved_notes'] if note.strip() not in outcome]
        for note in notes[:2]:
            lines.append('待关注：' + excerpt(note, 180))
        if len(notes) > 2:
            lines.append(f'另有 {len(notes) - 2} 条关注记录，见完整 JSON。')
        if row['artifacts']:
            names = [str(a.get('name') or a.get('path') or '').replace('\\', '/').rsplit('/', 1)[-1] for a in row['artifacts']]
            lines.append(f'产物 {len(names)} 项：' + '、'.join(names[:3]) + ('等；完整路径见 JSON。' if len(names) > 3 else '。'))
        lines.append('')
    if summary['followups']:
        lines.extend(['### 追问与修订摘要', ''])
        latest = {row['node_id']: row for row in summary['followups']}
        lines.append(f'共 {len(summary["followups"])} 次公开追问/修订，以下按节点列出最近一条摘要；完整摘要索引见 JSON。')
        for row in latest.values():
            lines.append(f'- {row["node_id"]} / {row["status"]}：{excerpt(row["request"], 100)} → {excerpt(row["reply"], 160)}')
    return '\n'.join(lines)


def pseudocode(packet):
    lines = ['# 工作流结构伪代码（说明依赖与控制流，不是可直接运行的程序）',
             '# 节点定义、完整配置与画布坐标以完整 JSON 为准。',
             '# 无前序或所有前序已终结的节点可调度；条件不匹配的路径跳过。',
             '# 汇合等待全部前序终结，至少一个有效入口即可执行；互不依赖的就绪节点可并发。',
             '# 上游真实失败仅阻止依赖它的后继。判断结果按端口路由，不运行隐式验收。',
             'WORKFLOW ' + compact(packet['workflow']['label']), 'GOAL ' + compact(packet['workflow']['goal']),
             'DEFAULT_MODEL ' + compact(packet['execution_context']['default_model'])]
    def visit(part, prefix='', depth=0):
        pad = '  ' * depth
        # Kahn layers describe eligibility, never add a barrier between unrelated tasks.
        remaining = {node['id']: node for node in part['nodes']}
        emitted = set()
        while remaining:
            ready = [key for key in remaining if all(e['source'] in emitted for e in part['edges'] if e['target'] == key)]
            for key in ready:
                node = remaining.pop(key)
                path = prefix + key
                incoming = [dict(source=prefix + edge['source'], port=edge['port']) for edge in part['edges'] if edge['target'] == key]
                lines.extend(['', pad + 'NODE ' + compact(path) + ' KIND ' + node['kind'],
                              pad + '  AFTER ' + compact(incoming), pad + '  TASK ' + compact(node['task']),
                              pad + '  INPUTS ' + compact(node['inputs']), pad + '  OUTPUTS ' + compact(node['outputs']),
                              pad + '  MODEL ' + compact(node.get('model') or packet['execution_context']['default_model']),
                              pad + '  TOOLS ' + compact(node.get('tools')) + ' DENY ' + compact(node.get('deny_tools', []))])
                config = node['config']
                lines.append(pad + '  CONFIG ' + compact({k: v for k, v in config.items() if k != 'graph'}))
                if node['kind'] == 'loop':
                    mode = config.get('mode', 'until')
                    operation = ('FOR_EACH ' + compact(config.get('items', [])) if mode == 'foreach' else
                                 'REPEAT ' + str(config['count']) if mode == 'count' else 'REPEAT_THEN_TEST ' + compact(config.get('until')))
                    lines.append(pad + '  ' + operation)
                    lines.append(pad + '  RESOURCE_COUNTERS_ONLY elapsed, tokens, rounds')
                    visit(config['graph'], path + '/', depth + 2)
                    lines.append(pad + '  END_LOOP')
                elif node['kind'] == 'branch':
                    lines.append(pad + '  SELECT_PORT_FROM ' + compact(config.get('expression', {'path': config.get('source', 'value')})))
                    lines.append(pad + '  CASES ' + compact(config.get('cases', [])) + ' DEFAULT ' + compact(config.get('default')))
                elif node['kind'] == 'judge':
                    lines.append(pad + '  ' + ('EVALUATE ' + compact(config['expression']) if 'expression' in config else
                        'WAIT_FOR_MANUAL_JUDGMENT' if config.get('manual') else 'ASK_MODEL_FOR_JUDGMENT'))
                else:
                    lines.append(pad + '  EXECUTE_AND_DELIVER')
                lines.append(pad + 'END_NODE')
            emitted.update(ready)
    visit(packet['workflow'])
    return '\n'.join(lines) + '\n'


def documents(packet):
    graph, source = packet['workflow'], packet['source']
    nodes = list(walk(graph))
    edge_count = sum(len(part['edges']) for part in [graph, *[n['config']['graph'] for _, n in nodes if n['kind'] == 'loop']])
    description = '已应用结构' if source['selection'] == 'applied' else '未应用草稿'
    parts = [f'# {graph["label"]} · 下一次任务的构造标准', '',
             f'来源：{description} v{source["graph_revision"]}；{len(nodes)} 个节点（含循环体），{edge_count} 条连线。', '',
             '## 给下一个对话', '',
             '请把以下工作流作为构造参考，结合我的新任务目标调整内容。保留需要复用的结构和控制关系；先给出新图及待替换参数，再按我的要求执行。', '',
             *['- ' + rule for rule in packet['construction_rules']], '',
             '## 结构示意', '', fence(diagram(graph), 'mermaid'), '',
             '## 完整结构 JSON', '',
             '包含所有节点、完整职责、输入输出、模型、工具、条件、嵌套循环与画布布局。坐标未显式保存的节点沿用自动布局。', '',
             fence(pretty(graph), 'json'), '', '## 运行参数与输入约定', '',
             '下面是参考配置；新任务需重新绑定材料、工作目录与可用模型。', '',
             fence(pretty(packet['execution_context']), 'json'), '', '## 本次对话与结果摘要', '',
             summary_markdown(packet['summary'])]
    if packet['redacted_fields']:
        parts.extend(['', '## 需重新绑定的凭据字段', '', *['- ' + key for key in packet['redacted_fields']]])
    stem = 'workflow-' + str(source['run_id'])[:8] + '-v' + str(source['graph_revision'])
    docs = {
        'brief': dict(label='构造说明', filename=stem + '-construction.md', content_type='text/markdown', text='\n'.join(parts)),
        'json': dict(label='完整 JSON', filename=stem + '-blueprint.json', content_type='application/json', text=pretty(packet)),
        'pseudocode': dict(label='伪代码', filename=stem + '-structure.txt', content_type='text/plain', text=pseudocode(packet)),
    }
    return dict(source=source, node_count=len(nodes), edge_count=edge_count, structure_digest=packet['structure_digest'],
                documents=docs, summary=packet['summary'])
