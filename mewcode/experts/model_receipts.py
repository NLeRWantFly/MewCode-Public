"""Content-free model receipts and a scoped, read-only projection for audits."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit


def request_receipt(arguments, client):
    """Describe the actual SDK arguments, never credentials or prompt contents.

    This proves what the host submitted to the SDK, not a relay's backend or
    the provider's internal reasoning. Configured limits are not verified maxima.
    """
    fields = ('model', 'messages', 'input', 'system', 'instructions', 'tools', 'stream',
              'reasoning', 'reasoning_effort', 'thinking', 'output_config', 'extra_body',
              'max_tokens', 'max_output_tokens', 'max_completion_tokens')
    payload = {key: arguments[key] for key in fields if key in arguments}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    images = []

    def walk(value, position=''):
        if isinstance(value, list):
            for i, item in enumerate(value):
                walk(item, position + '/' + str(i))
        elif isinstance(value, dict):
            data, mime = None, ''
            if isinstance(value.get('type'), str) and value['type'] in {'image', 'image_url', 'input_image'}:
                source = value.get('source') or {}
                if isinstance(source, dict) and source.get('type') == 'base64':
                    data, mime = source.get('data'), source.get('media_type', '')
                url = value.get('image_url')
                if isinstance(url, dict):
                    url = url.get('url')
                if isinstance(url, str):
                    if url.startswith('data:') and ';base64,' in url:
                        header, data = url.split(';base64,', 1)
                        mime = header.removeprefix('data:')
                    else:
                        images.append({'position': position, 'url_sha256': hashlib.sha256(url.encode()).hexdigest(),
                                       'bytes_verified': False})
                if isinstance(data, str):
                    try:
                        raw = base64.b64decode(data, validate=True)
                        images.append({'position': position, 'sha256': hashlib.sha256(raw).hexdigest(),
                                       'bytes': len(raw), 'media_type': mime, 'bytes_verified': True})
                    except ValueError:
                        images.append({'position': position, 'bytes_verified': False, 'reason': 'invalid_base64'})
            for key, item in value.items():
                if isinstance(item, (dict, list)):
                    walk(item, position + '/' + key)
    # Images in tool-schema examples are definitions, not visual input.
    for field in ('messages', 'input'):
        if field in payload:
            walk(payload[field], '/' + field)
    reasoning = arguments.get('reasoning') or {}
    output_config = arguments.get('output_config') or {}
    effort = (arguments.get('reasoning_effort') or reasoning.get('effort') or output_config.get('effort'))
    extra = arguments.get('extra_body') or {}
    return {'schema': 'mewcode.model-request.v1', 'sdk_request_sha256': hashlib.sha256(encoded).hexdigest(),
            'sdk_payload_bytes': len(encoded), 'requested_model': arguments.get('model'),
            'configured_effort': getattr(client, 'effort', '') or None, 'sent_effort': effort,
            'sent_thinking': copy.deepcopy(arguments.get('thinking') or extra.get('thinking')),
            'image_inputs': images,
            'limits': {'configured_context_window': getattr(client, 'context_window', None),
                       'configured_output_tokens': getattr(client, 'response_token_limit', None)
                           or getattr(client, 'max_output_tokens', None),
                       'sent_output_tokens': next((arguments[k] for k in
                           ('max_output_tokens', 'max_completion_tokens', 'max_tokens') if k in arguments), None),
                       'basis': 'host_configuration_and_sdk_parameters', 'provider_maximum_verified': False}}


def receipt_ledger(recorder, service=None):
    return (getattr(recorder, 'receipt_ledger', None) or getattr(getattr(recorder, '__self__', None), 'ledger', None)
            or getattr(service, 'ledger', None))


def bind_recorder(recorder, record, node_id, input_revision, model):
    if recorder is None or not record.get('run_id') or not node_id:
        return recorder
    scope = {'run_id': record['run_id'], 'node_id': node_id, 'run_attempt': record.get('attempt'),
             'input_revision': input_revision, 'provider_key': model.provider_key}

    def record_scoped(usage):
        usage.metadata = {**usage.metadata, 'execution_scope': copy.deepcopy(scope)}
        recorder(usage)
    record_scoped.receipt_ledger = receipt_ledger(recorder)
    return record_scoped


def _safe_row(row, binding):
    metadata = json.loads(row['metadata_json'])
    return {'event_id': row['event_id'], 'occurred_at': row['occurred_at'], 'binding': binding,
            'provider_name': row['provider_name'], 'protocol': row['protocol'],
            'endpoint_host': urlsplit(row['base_url']).hostname,
            'configured_model': row['configured_model'], 'response_model': row['response_model'] or None,
            'response_id': row['response_id'] or None, 'request_id': row['request_id'] or None,
            'upstream_request_id': row['upstream_request_id'] or None, 'evidence': row['evidence'],
            'input_tokens': row['input_tokens'] + row['cached_input_tokens'] + row['cache_write_input_tokens'],
            'output_tokens': row['output_tokens'], 'execution_scope': metadata.get('execution_scope'),
            'model_call': metadata.get('model_call'),
            'notice': '返回字段仅按其来源证明响应声明；不证明中转后端身份、推理档位实际执行或容量极限。'}


RECORDING_CONTRACT = {
    'schema': 'mewcode.model-request.v1',
    'source': 'local_request_usage_ledger',
    'raw_transport_bodies_retained': False,
    'unrecorded_history_reconstructable': False,
    'remote_provider_records': 'not_queried',
    'notice': '本宿主只在调用发生时将 SDK 参数摘要和图像哈希写入请求账本，不保存原始传输报文。'
        '未记录的历史 SDK 参数不能从本账本事后补造；对话消息、工具回执和配置也不等于当时序列化请求。'
        '这是本宿主保存机制的边界，不断言外部服务商没有日志，也不证明模型内部实际身份或推理档位。',
}


def capture_coverage(calls, stage):
    """Describe saved observations, without upgrading time matches to evidence."""
    captured = [row for row in calls if isinstance(row.get('model_call'), dict)
                and isinstance(row['model_call'].get('request'), dict)
                and row['model_call']['request'].get('sdk_request_sha256')]
    exact = [row for row in captured if row['binding'] == 'exact_scope'
             and (row.get('execution_scope') or {}).get('input_revision') == stage.get('input_revision')]
    return {'examined_calls': len(calls), 'legacy_candidates': sum(row['binding'] == 'legacy_candidate' for row in calls),
        'saved_request_summaries': len(captured), 'current_input_bound_summaries': len(exact),
        'availability': ('available_for_review' if exact else 'candidate_requires_binding' if captured
                         else 'not_recorded_in_examined_host_records' if calls else 'no_matching_records'),
        'earliest_examined_at': min((row['occurred_at'] for row in calls), default=None),
        'latest_examined_at': max((row['occurred_at'] for row in calls), default=None)}


def audit_sources(record, node_id, ledger):
    """Freeze only this auditor's scope. Legacy time matches remain candidates."""
    value = record.get('collaboration') or {}
    contract = value.get('contracts', {}).get(node_id) or {}
    keys = [node_id, *contract.get('bound_dependencies', contract.get('depends_on', []))]
    requirements = value.get('entries') or {}
    from mewcode.permissions.decision_envelope import redact_value
    referenced = set(keys) | set(contract.get('requirement_ids', [])) | set(contract.get('constraint_ids', []))
    selected = [row for row in requirements.values()
                if row.get('state') not in {'retired', 'rejected', 'superseded'}
                and (row.get('id') in referenced or row.get('kind') in {'goal', 'constraint', 'decision'})]
    sources = {'@audit_requirements': {'kind': 'host_frozen_requirements', 'spec_version': value.get('spec_version'),
        'entries': [redact_value({**{key: row.get(key) for key in ('id', 'kind', 'body', 'scope', 'version', 'state')},
                    'data': {key: item for key, item in (row.get('data') or {}).items()
                             if key in {'acceptance', 'steps', 'value', 'category'}}}) for row in selected],
        'notice': '按当前阶段冻结要求核验；不得把后续授权、额外极限测试或已确认的方法重新加入当前验收。'}}
    path = getattr(ledger, 'path', None)
    summary = {'kind': 'host_model_receipts', 'nodes': [], 'ledger_available': False,
        'recording_contract': copy.deepcopy(RECORDING_CONTRACT), 'capture_coverage': {},
        'notice': '旧文件的 null 不证明宿主或服务商没有回执。exact_scope 是宿主执行绑定；legacy_candidate 仅为同会话同时间段候选，尚未证明页面或尝试归属。'}
    sources['@model_receipts'] = summary
    if not path or not Path(path).is_file() or not record.get('conversation_id'):
        return sources
    columns = ('event_id,occurred_at,provider_name,protocol,base_url,configured_model,response_model,response_id,'
               'request_id,upstream_request_id,evidence,input_tokens,cached_input_tokens,cache_write_input_tokens,'
               'output_tokens,metadata_json')
    try:
        with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as conn:
            conn.row_factory = sqlite3.Row
            summary['ledger_available'] = True
            summary['first_saved_request_summary_at'] = conn.execute('SELECT min(occurred_at) FROM request_usage '
                "WHERE session_id=? AND json_extract(metadata_json,'$.model_call.request.sdk_request_sha256') IS NOT NULL",
                (record['conversation_id'],)).fetchone()[0]
            for key in dict.fromkeys(keys):
                stage = record.get('node_states', {}).get(key) or {}
                spans = stage.get('model_spans') or []
                times = [(s.get('started_at'), s.get('completed_at')) for s in spans
                         if isinstance(s.get('started_at'), (int, float)) and isinstance(s.get('completed_at'), (int, float))]
                # Metadata produced by this host binds a call without using time heuristics.
                rows = conn.execute('SELECT ' + columns + ' FROM request_usage WHERE session_id=? AND '
                    "json_extract(metadata_json,'$.execution_scope.run_id')=? AND "
                    "json_extract(metadata_json,'$.execution_scope.node_id')=? ORDER BY occurred_at",
                    (record['conversation_id'], record['run_id'], key)).fetchall()
                calls = [_safe_row(row, 'exact_scope') for row in rows]
                if times:
                    lower, upper = min(t[0] for t in times), max(t[1] for t in times)
                    candidates = conn.execute('SELECT ' + columns + ' FROM request_usage WHERE session_id=? AND '
                        "json_extract(metadata_json,'$.execution_scope') IS NULL AND unixepoch(occurred_at) BETWEEN ? AND ? "
                        'ORDER BY occurred_at', (record['conversation_id'], int(lower), int(upper) + 1)).fetchall()
                    for row in candidates:
                        at = datetime.fromisoformat(row['occurred_at']).timestamp()
                        if any(start <= at <= end + 1 for start, end in times):
                            calls.append(_safe_row(row, 'legacy_candidate'))
                coverage = capture_coverage(calls, stage)
                summary['capture_coverage'][key] = coverage
                if not calls:
                    continue
                groups = {}
                for call in calls:
                    group = (call['configured_model'], call['response_model'], call['evidence'], call['binding'])
                    groups[group] = groups.get(group, 0) + 1
                source = '@model_receipts:' + key
                summary['nodes'].append({'node_id': key, 'source_id': source, 'calls': len(calls), 'groups': [
                    dict(zip(('configured_model', 'response_model', 'evidence', 'binding'), group), count=count)
                    for group, count in groups.items()]})
                sources[source] = {'node_id': key, 'kind': 'host_model_receipts', 'calls': calls,
                    'capture_coverage': coverage,
                    'current_input_revision': stage.get('input_revision'),
                    'notice': summary['notice'] + '旧候选不可自动升级为逐页身份验证；精确绑定也需核对 input_revision、请求图像哈希及返回字段。'}
    except (OSError, sqlite3.Error) as exc:
        summary.update(ledger_available=False, error=type(exc).__name__, nodes=[], capture_coverage={})
        sources = {key: item for key, item in sources.items() if not key.startswith('@model_receipts:')}
    return sources
