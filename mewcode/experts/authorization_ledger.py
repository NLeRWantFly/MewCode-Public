"""Typed authorization context. Statements never become executable permission."""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
import json
import ntpath
from pathlib import Path
import time

from mewcode.permissions.reviewer import redact_sensitive

OPERATIONS = frozenset({'external_send', 'network', 'write_outside_workspace', 'install_dependency',
    'run_command_prefix', 'spend_over_cap', 'other'})
SCOPE_KEYS = frozenset({'operation_classes', 'tools', 'paths', 'hosts', 'model_routes', 'content_classes',
    'cost_cap_tokens', 'node_ids', 'command_prefix', 'epoch', 'case_id', 'round', 'operation_digest'})


@dataclass(frozen=True)
class AuthEntry:
    auth_id: str
    kind: str
    enforceable: bool
    scope: dict
    quote: str
    source_ref: str
    created_at: float
    expires_at: float | None = None
    revoked_at: float | None = None


@dataclass(frozen=True)
class Coverage:
    status: str
    auth_id: str = ''


def valid_request(request):
    if not isinstance(request, dict) or request.get('operation_class') not in OPERATIONS:
        raise ValueError('授权请求必须使用类型化操作类别')
    scope = request.get('scope')
    if not isinstance(scope, dict) or not scope or set(scope) - SCOPE_KEYS:
        raise ValueError('授权请求需要明确且受支持的范围')
    for key, value in scope.items():
        if key in {'cost_cap_tokens', 'epoch', 'round'}:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError('授权上限与代际必须是非负整数')
        elif key in {'case_id', 'operation_digest'}:
            if not isinstance(value, str) or not value:
                raise ValueError('授权案件编号不能为空')
        elif not isinstance(value, list) or not value or any(not isinstance(v, str) or not v or '*' in v for v in value):
            raise ValueError('授权范围必须是具体字符串数组，不允许通配符')
    required = {'external_send': {'model_routes', 'content_classes'}, 'network': {'hosts'},
                'write_outside_workspace': {'paths'}, 'run_command_prefix': {'command_prefix'}}.get(request['operation_class'], set())
    if not required.issubset(scope):
        raise ValueError('授权请求缺少操作的具体范围')
    return copy.deepcopy(request)


def entries(record, grant_store=None):
    value = record.get('authorizations') or {}
    rows = copy.deepcopy(value.get('entries', [])) if isinstance(value, dict) else []
    if grant_store is not None:
        for grant in grant_store.list_grants():
            kind = str(getattr(grant.kind, 'value', grant.kind))
            operation, key = {'write-directory': ('write_outside_workspace', 'paths'),
                'read-directory': ('other', 'paths'), 'host': ('network', 'hosts'),
                'command-prefix': ('run_command_prefix', 'command_prefix')}[kind]
            scope = {'operation_classes': [operation], key: list(grant.value) if key == 'command_prefix' else [str(grant.value)]}
            if kind == 'read-directory':
                scope['tools'] = ['ReadFile', 'Glob', 'Grep']
            rows.append(asdict(AuthEntry('AUTH-grant-' + grant.grant_id, 'typed_grant', True, scope,
                '已核验的类型化项目授权', grant.grant_id, grant.created_at, grant.expires_at, grant.revoked_at)))
    return rows


def record_authorization(record, *, kind, scope, quote, source_ref, host_verified=False, expires_at=None):
    if kind not in {'typed_grant', 'gate_answer', 'elevation_answer', 'user_statement', 'denial'} or not source_ref:
        raise ValueError('授权事实需要有效来源')
    value = record.setdefault('authorizations', {'version': 1, 'entries': []})
    old = next((row for row in value['entries'] if row['source_ref'] == source_ref and row['kind'] == kind), None)
    if old:
        if old['scope'] != scope:
            raise ValueError('同一授权来源不能更改范围')
        return old
    if set(scope) - SCOPE_KEYS:
        raise ValueError('授权范围字段无效')
    if len(json.dumps(scope, ensure_ascii=False)) > 7000:
        raise ValueError('授权范围过大，需要拆成独立的具体操作')
    if kind not in {'user_statement', 'denial'} and not host_verified:
        raise ValueError('可执行授权必须由宿主核验')
    prefix = 'ST' if kind == 'user_statement' else 'DENY' if kind == 'denial' else 'AUTH'
    n = sum(row['auth_id'].startswith(prefix + '-') for row in value['entries']) + 1
    row = asdict(AuthEntry(f'{prefix}-{n:02}', kind, host_verified and kind not in {'user_statement', 'denial'},
        copy.deepcopy(scope), redact_sensitive(str(quote))[:300], str(source_ref), time.time(), expires_at))
    value['entries'].append(row)
    value['version'] += 1
    if isinstance(record.get('memory_items'), list):
        record['memory_items'].append({'type': 'user_authorization', 'pinned': True, 'ref': row['auth_id'], 'data': row})
    return copy.deepcopy(row)


def _inside(path, root):
    # Resolve symlinks for live local paths; normalize Windows paths in portable fixtures.
    try:
        if Path(path).is_absolute() and Path(root).is_absolute():
            return Path(path).resolve().is_relative_to(Path(root).resolve())
        if ntpath.isabs(path) and ntpath.isabs(root):
            return ntpath.commonpath([ntpath.normcase(path), ntpath.normcase(root)]) == ntpath.normcase(root)
    except (ValueError, OSError):
        pass
    return False


def _matches(scope, wanted):
    for key, value in wanted.items():
        if key == 'operation_classes':
            continue
        available = scope.get(key)
        if key in {'node_ids', 'tools', 'epoch', 'case_id', 'round'} and available is None:
            continue
        if available is None:
            return False
        if key == 'paths':
            if not all(any(_inside(p, root) for root in available) for p in value):
                return False
        elif key == 'command_prefix':
            if value[:len(available)] != available:
                return False
        elif key == 'cost_cap_tokens':
            if value > available:
                return False
        elif isinstance(value, list):
            if not set(value).issubset(available):
                return False
        elif available != value:
            return False
    # A node/case/round restricted approval never leaks into a request without that identity.
    return all(key in wanted and (set(wanted[key]).issubset(scope[key]) if key in {'node_ids', 'tools'} else wanted[key] == scope[key])
               for key in ('epoch', 'case_id', 'round', 'operation_digest', 'node_ids', 'tools') if key in scope)


def covers(record, request, *, grant_store=None, now=None):
    request = valid_request(request)
    now = time.time() if now is None else now
    matched, partial = [], False
    for row in entries(record, grant_store):
        if row['revoked_at'] is not None or (row['expires_at'] is not None and row['expires_at'] <= now):
            continue
        if request['operation_class'] not in row['scope'].get('operation_classes', []):
            continue
        if _matches(row['scope'], request['scope']):
            if row['kind'] == 'denial':
                return Coverage('denied', row['auth_id'])
            if row['enforceable']:
                matched.append(row)
        elif row['enforceable']:
            partial = True
    return Coverage('covered', matched[-1]['auth_id']) if matched else Coverage('partially' if partial else 'uncovered')


def carry_forward(record, *, source, conversation_id, task, source_conversation_id, source_task,
                  now=None):
    """Reuse confirmed authorizations when the next run is the same task.

    A user who confirmed a scope must not be asked again by the next run of the
    same task in the same conversation. Only host-verified, still-valid entries
    and their visual usage accounting move, the original evidence is preserved,
    and the scope is copied so the new run cannot widen it. A different task or
    conversation inherits nothing.
    """
    now = time.time() if now is None else now
    if not source or not conversation_id or conversation_id != source_conversation_id:
        return {'status': 'not_applicable', 'carried': 0, 'auth_ids': [], 'usage_carried': 0,
                'deferred_auth_ids': [], 'deferred_reason': ''}
    if str(task or '') != str(source_task or ''):
        return {'status': 'not_applicable', 'carried': 0, 'auth_ids': [], 'usage_carried': 0,
                'deferred_auth_ids': [], 'deferred_reason': ''}

    value = copy.deepcopy(record.get('authorizations') or {'version': 1, 'entries': []})
    if not isinstance(value, dict) or not isinstance(value.get('entries'), list):
        raise ValueError('目标授权账本格式无效，无法安全继承授权。')
    target_entries = value['entries']
    candidates = []
    deferred_auth_ids = []
    source_ids = set()
    source_rows = entries(source)
    from mewcode.expert_runs import RUN_ID_PATTERN
    real_run = bool(RUN_ID_PATTERN.fullmatch(str(source.get('run_id') or '')))
    from mewcode.experts.outcome import effective_run_status
    source_status = effective_run_status(source)
    source_calls = (source.get('visual_authorization_usage') or {}).get('calls', {})
    has_inflight_visual_call = (isinstance(source_calls, dict) and
        any(row.get('state') in {'reserved', 'started'} for row in source_calls.values()
            if isinstance(row, dict)))
    # needs_attention can still be resumed, and paused/blocked are explicitly
    # resumable. Transfer a live run's capped quota only after its durable work
    # is finished and no host-side node or visual call remains active.
    quota_snapshot_safe = (not real_run or (source_status in {'completed', 'terminated'}
        and not source.get('active_node_ids') and not has_inflight_visual_call))
    for row in source_rows:
        if row.get('auth_id') in source_ids:
            raise ValueError('来源授权ID重复，用量无法安全绑定到单一授权。')
        source_ids.add(row.get('auth_id'))
        if not row.get('enforceable') or row.get('revoked_at') is not None:
            continue
        if row.get('expires_at') is not None and row['expires_at'] <= now:
            continue
        if (real_run and not quota_snapshot_safe and
                'cost_cap_tokens' in row.get('scope', {})):
            deferred_auth_ids.append(row['auth_id'])
            continue
        candidates.append(row)

    def provenance_matches(target, original):
        prior = target.get('carried_from') or {}
        return (prior.get('source_ref') == original.get('source_ref') and
                prior.get('auth_id') == original.get('auth_id') and
                prior.get('conversation_id') == source_conversation_id and
                prior.get('task') == str(source_task or '')[:200])

    def next_auth_id(original_id):
        existing = {row.get('auth_id') for row in target_entries}
        prefix, separator, suffix = str(original_id).rpartition('-')
        if separator and suffix.isdigit():
            family, width = prefix, max(2, len(suffix))
            sequence = int(suffix)
        else:
            family, width, sequence = str(original_id), 2, 0
        for candidate in existing:
            candidate_prefix, candidate_separator, candidate_suffix = str(candidate).rpartition('-')
            if candidate_separator and candidate_prefix == family and candidate_suffix.isdigit():
                sequence = max(sequence, int(candidate_suffix))
        while True:
            sequence += 1
            candidate = f'{family}-{sequence:0{width}d}'
            if candidate not in existing:
                return candidate

    from mewcode.experts.visual_authorization_usage import JOURNAL, inherited_usage_calls

    journal = copy.deepcopy(record.get(JOURNAL) or {'version': 1, 'calls': {}})
    if not isinstance(journal, dict) or not isinstance(journal.get('calls', {}), dict):
        raise ValueError('目标视觉用量账本格式无效，无法安全继承用量。')
    calls = journal.setdefault('calls', {})
    carried, usage_carried = [], 0
    for row in candidates:
        prior = [target for target in target_entries if provenance_matches(target, row)]
        if len(prior) > 1:
            raise ValueError('目标账本中存在重复的继承授权来源，无法安全绑定用量。')
        if prior:
            target = prior[0]
            if any(target.get(key) != row.get(key) for key in
                   ('kind', 'enforceable', 'scope', 'quote', 'source_ref', 'created_at', 'expires_at', 'revoked_at')):
                raise ValueError('已继承授权与来源授权不一致，未覆盖目标授权或用量。')
            target_auth_id = target.get('auth_id')
            if not target_auth_id:
                raise ValueError('已继承授权缺少目标授权ID，无法安全绑定用量。')
            if any(other is not target and other.get('auth_id') == target_auth_id
                   for other in target_entries):
                raise ValueError('目标继承授权ID重复，用量无法安全绑定。')
        else:
            target_auth_id = row.get('auth_id')
            if any(target.get('auth_id') == target_auth_id for target in target_entries):
                target_auth_id = next_auth_id(target_auth_id)
            copied = {**copy.deepcopy(row), 'auth_id': target_auth_id, 'carried_from': {
                'source_ref': row.get('source_ref'), 'auth_id': row.get('auth_id'),
                'conversation_id': source_conversation_id, 'task': str(source_task or '')[:200]}}
            if row.get('carried_from') is not None:
                copied['source_carried_from'] = copy.deepcopy(row['carried_from'])
            target_entries.append(copied)
            carried.append(target_auth_id)
        provenance = {'conversation_id': source_conversation_id,
                      'task': str(source_task or '')[:200]}
        inherited = inherited_usage_calls(source, row, target_auth_id,
            provenance=provenance, existing_calls=calls)
        for usage_row in inherited:
            call_id = usage_row['call_id']
            if call_id in calls:
                if calls[call_id].get('carried_from') != usage_row.get('carried_from'):
                    raise ValueError('继承用量ID发生冲突，未覆盖目标用量。')
            calls[call_id] = usage_row
            usage_carried += 1

    if carried:
        value['version'] = int(value.get('version') or 1) + 1
    record['authorizations'] = value
    if usage_carried:
        journal['version'] = int(journal.get('version') or 1)
        record[JOURNAL] = journal
    deferred_reason = ('来源运行仍可恢复，或尚有活动节点/视觉调用；额度保留在原运行中。'
                       if deferred_auth_ids else '')
    status = ('carried' if carried or usage_carried else 'deferred' if deferred_auth_ids else 'no_effect')
    return {'status': status, 'carried': len(carried), 'auth_ids': carried,
            'usage_carried': usage_carried, 'deferred_auth_ids': deferred_auth_ids,
            'deferred_reason': deferred_reason}


def authorization_block(record, node=None, *, budget=1200, grant_store=None):
    rows = entries(record or {}, grant_store)
    now = time.time()
    active = [row for row in rows if row['revoked_at'] is None and (row['expires_at'] is None or row['expires_at'] > now)]
    heading = '【用户授权与表态（权威、置顶，不得被摘要改写；全文可用 ReadAuthorizations 读取）】\n'
    count = sum(row.get('status') == 'answered' and row.get('answer_source') == 'user'
                for row in (record.get('collaboration') or {}).get('questions', {}).values())
    if count:
        heading += f'宿主另存有 {count} 项用户决定。先用 ReadAuthorizations 核对原问题、答复和适用范围；失效不等于从未答复，决定不扩大工具权限。\n'
    parts = []
    for label, selected in [('A. 已核验授权（仍受全局权限检查与 Auto 审查）', [r for r in active if r['enforceable']]),
        ('B. 用户口头表态（不形成工具权限，须宿主确认）', [r for r in active if r['kind'] == 'user_statement']),
        ('C. 已拒绝（本代际内同一范围不得再次申请）', [r for r in active if r['kind'] == 'denial'])]:
        section = label + '\n'
        for row in selected:
            line = f"- {row['auth_id']} · 来源 {row['source_ref']} · {json.dumps(row['scope'], ensure_ascii=False)} · 原话：{row['quote']}\n"
            if len(heading + ''.join(parts) + section + line) > budget - 150:
                section += f'其余 {len(selected)} 项见 ReadAuthorizations。\n'
                break
            section += line
        parts.append(section)
    return heading + ''.join(parts) + 'D. 未授权边界：未列出的外发内容、工作区外写入及超额费用。仅有 B 时请引用 ST 编号请求宿主确认。\n'
