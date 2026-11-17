"""One-shot CLI activation of an already saved graph, through normal confirm.

This is not an HTTP authentication alternative or a new permission grant. The
operator must explicitly pass a request file at process launch. It cannot carry
model overrides, tool permissions, arbitrary prompts or replacement graphs.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class SavedGraphStart(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    project: str
    conversation_id: str
    graph_id: str = Field(pattern=r'^[A-Za-z0-9_-]+$')
    graph_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class PendingGateResume(SavedGraphStart):
    run_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    node_id: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_-]*$')
    pending_request_id: str
    inputs_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    locked_flow_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class GraphGenerationRetry(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    project: str
    conversation_id: str
    draft_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,80}$')
    candidate_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    sequence: int = Field(ge=0)
    sources_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    provider_key: str
    model: str
    effort: str


async def retry_graph_generation(path, *, project, conversation_id, read_state, selected_route, update):
    """Retry an exact failed graph using the user's current route and sources."""
    request_path = Path(path).resolve(strict=True)
    result = {'started': False, 'source': 'explicit_cli_graph_retry'}
    try:
        if request_path.stat().st_size > 16384 or Path(path).is_symlink():
            raise ValueError('构图接续请求必须是小型普通 JSON 文件')
        request = GraphGenerationRetry.model_validate_json(request_path.read_text(encoding='utf-8'))
        if Path(request.project).resolve() != Path(project).resolve() or request.conversation_id != conversation_id:
            raise ValueError('构图接续请求不属于当前项目和对话')
        state = read_state(project, conversation_id, request.draft_id)
        candidate = (state or {}).get('candidate') or {}
        if (candidate.get('id') != request.candidate_id or candidate.get('sequence') != request.sequence
                or candidate.get('status') != 'failed'):
            raise ValueError('候选图已变化或不是失败状态；未重新发起模型请求')
        if value_sha256((state or {}).get('source_ids') or []) != request.sources_sha256:
            raise ValueError('构图的原始来源已变化；未重读或扩大范围')
        actual = selected_route()
        route = {key: str(actual.get(key) or '') for key in ('provider_key', 'model', 'requested_effort')}
        result['route'] = route
        if route != {'provider_key': request.provider_key, 'model': request.model, 'requested_effort': request.effort}:
            raise ValueError('构图模型未匹配用户当前选择；未切换或降档')
        with request_path.with_suffix('.claimed').open('x', encoding='utf-8') as claim:
            claim.write(request.operation_id)
        result.update(operation_id=request.operation_id, conversation_id=conversation_id, candidate_id=request.candidate_id)
        result.update(await update({'project': str(project), 'conversation_id': conversation_id,
                                   'draft_id': request.draft_id}))
        result['started'] = result.get('http_status') == 202
    except (OSError, ValueError, TypeError) as exc:
        result['error'] = str(exc)
    request_path.with_suffix('.result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


def value_sha256(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


async def resume_pending_gate(path, *, project, conversation_id, load_run, resume):
    """Explicit restart continuation to an unanswered gate, never its approval."""
    request_path = Path(path).resolve(strict=True)
    result = {'started': False, 'source': 'explicit_cli_pending_gate'}
    try:
        if request_path.stat().st_size > 16384 or Path(path).is_symlink():
            raise ValueError('门禁恢复请求必须是小型普通 JSON 文件')
        request = PendingGateResume.model_validate_json(request_path.read_text(encoding='utf-8'))
        if Path(request.project).resolve() != Path(project).resolve() or request.conversation_id != conversation_id:
            raise ValueError('门禁恢复请求不属于当前项目和对话')
        graph_path = Path(project).resolve() / '.mewcode/pipelines' / (request.graph_id + '.json')
        if graph_path.is_symlink() or not graph_path.resolve().is_relative_to(Path(project).resolve()):
            raise ValueError('图路径超出项目')
        if hashlib.sha256(graph_path.read_bytes()).hexdigest() != request.graph_sha256:
            raise ValueError('门禁恢复请求的原图已变化')
        record = load_run(request.run_id)
        if not record or record.get('conversation_id') != conversation_id:
            raise ValueError('门禁运行不属于当前对话')
        flow = record.get('locked_flow') or {}
        if (flow.get('meta') or {}).get('pipeline_graph_id') != request.graph_id:
            raise ValueError('原运行不属于请求的图')
        if ((flow.get('meta') or {}).get('node_kinds') or {}).get(request.node_id) != 'gate':
            raise ValueError('只能恢复明确声明的人工门禁')
        stage = (record.get('node_states') or {}).get(request.node_id) or {}
        if stage.get('status') not in {'paused', 'blocked', 'needs_attention'}:
            raise ValueError('门禁已完成或不是当前阻塞步骤')
        pending = stage.get('pending_interaction') or {}
        draft = (record.get('gate_dialogues') or {}).get(request.node_id) or {}
        waiting_gate = (record.get('status') == 'running'
            and record.get('active_node_ids') == [request.node_id]
            and pending.get('kind') == 'human_oncall' and pending.get('tool') == 'human_oncall'
            and bool(draft.get('proposal'))
            and pending.get('scope_hash') == str(draft.get('scope_hash') or '') + ':' + value_sha256(draft['proposal'])
            and not (record.get('gate_confirmations') or {}).get(request.node_id))
        stopped = record.get('status') in {'paused', 'blocked', 'needs_attention'} and not record.get('active_node_ids')
        if not stopped and not waiting_gate:
            raise ValueError('原运行仍在处理业务或已结束，不能另行恢复')
        if str(pending.get('request_id') or '') != request.pending_request_id:
            raise ValueError('待答问题已变化，不能重放旧恢复请求')
        if (value_sha256(record.get('run_inputs') or {}) != request.inputs_sha256
                or value_sha256(flow) != request.locked_flow_sha256):
            raise ValueError('当前参数或原运行模型与恢复请求不一致')
        with request_path.with_suffix('.claimed').open('x', encoding='utf-8') as claim:
            claim.write(request.operation_id)
        result.update(operation_id=request.operation_id, run_id=request.run_id, node_id=request.node_id,
                      restoring_waiting_gate=waiting_gate)
        result.update(await resume(record))
        result['started'] = True
    except (OSError, ValueError, TypeError) as exc:
        result['error'] = str(exc)
    request_path.with_suffix('.result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


async def activate_saved_graph(path, *, project, conversation_id, saved_spec, list_runs, confirm, permission_mode):
    request_path = Path(path).resolve(strict=True)
    result_path = request_path.with_suffix('.result.json')
    claim_path = request_path.with_suffix('.claimed')
    result = {'started':False, 'source':'explicit_cli_saved_graph'}
    try:
        if request_path.stat().st_size > 16384 or Path(path).is_symlink():
            raise ValueError('启动请求必须是小型普通 JSON 文件')
        request = SavedGraphStart.model_validate_json(request_path.read_text(encoding='utf-8'))
        if Path(request.project).resolve() != Path(project).resolve() or request.conversation_id != conversation_id:
            raise ValueError('启动请求不属于当前项目和对话')
        graph_path = Path(project).resolve() / '.mewcode/pipelines' / (request.graph_id + '.json')
        if graph_path.is_symlink() or not graph_path.resolve().is_relative_to(Path(project).resolve()):
            raise ValueError('图路径超出项目')
        if hashlib.sha256(graph_path.read_bytes()).hexdigest() != request.graph_sha256:
            raise ValueError('已保存图与启动请求不一致')
        if saved_spec is None or str(saved_spec.meta.get('pipeline_graph_id') or '') != request.graph_id:
            raise ValueError('当前已保存方案不属于请求的图')
        if str(saved_spec.meta.get('conversation_id') or '') != conversation_id:
            raise ValueError('已保存图不属于当前对话')
        if list_runs(conversation_id):
            raise ValueError('当前对话仍有运行记录，不能把重启当作从头执行')
        # Claim before dispatch. A crash or app relaunch never silently repeats
        # a paid operation; the normal run checkpoint is its continuation path.
        with claim_path.open('x', encoding='utf-8') as claim:
            claim.write(request.operation_id)
        result.update(operation_id=request.operation_id, graph_id=request.graph_id)
        payload = {'conversation_id':conversation_id, 'spec':saved_spec.as_dict(),
            'activate':True, 'permission_mode':permission_mode}
        result.update(await confirm(payload))
    except (OSError, ValueError, TypeError) as exc:
        result['error'] = str(exc)
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result
