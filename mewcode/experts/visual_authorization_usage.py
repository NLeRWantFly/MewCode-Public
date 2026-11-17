"""Host-owned visual authorization accounting, independent of role budgets.

Reservations are planning estimates, not provider invoices or exact token bounds.
Missing usage is charged conservatively as an estimate and can be reconciled
later. Unknown telemetry alone never blocks the next affordable request.
"""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import json
import time
import uuid

from mewcode.experts.authorization_ledger import covers, entries
from mewcode.permissions.decision_envelope import canonical_digest

BASIS = 'provider_reported_tokens_including_cache'
JOURNAL = 'visual_authorization_usage'


class VisualQuotaError(ValueError):
    blocker_kind = 'authority'

    def __init__(self, message, account, *, required_tokens, scope, sources=()):
        super().__init__(message)
        self.code = 'visual_authorization_' + account['state']
        self.accounting = {key: value for key, value in account.items() if key not in {'calls', 'legacy_receipts'}}
        self.accounting['required_tokens'] = required_tokens
        self.accounting['request'] = {'operation_class': 'external_send', 'scope': {
            **{key: value for key, value in scope.items() if key != 'operation_classes'},
            'cost_cap_tokens': required_tokens}}
        self.accounting['sources'] = copy.deepcopy(list(sources))
        if sources:
            self.accounting['request']['scope']['paths'] = sorted({source['file_path'] for source in sources})


def usage_tokens(usage):
    value = usage.get('reported_total_tokens') if isinstance(usage, dict) else None
    return value if type(value) is int and value >= 0 else None


def _call_usage_accounting(row):
    known = usage_tokens(row.get('usage')) or 0
    reserved_tokens = row.get('reserved_tokens')
    if type(reserved_tokens) is not int or reserved_tokens < 0:
        reserved_tokens = 0
    state = row.get('state')
    reserved = reserved_tokens if state in {'reserved', 'started'} else 0
    estimated = reserved_tokens if state == 'outcome_unknown' else 0
    floor = row.get('carried_minimum_tokens')
    if type(floor) is not int or floor < 0:
        floor = 0
    estimated += max(0, floor - known - reserved - estimated)
    return {'known': known, 'estimated': estimated, 'reserved': reserved,
            'total': known + estimated + reserved,
            'has_floor_estimate': estimated > (reserved_tokens if state == 'outcome_unknown' else 0)}


def _timestamp(value):
    if isinstance(value, (int, float)):
        return value
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return None


def _legacy_rows(record, grant):
    """Account once by receipt identity, never by observation text or node totals."""
    calls = (record.get(JOURNAL) or {}).get('calls', {})
    stages = list((record.get('node_states') or {}).items())
    stages += [(owner, stage) for archive in record.get('acceptance_rechecks', [])
               for owner, stage in (archive.get('previous') or {}).items() if isinstance(stage, dict)]
    seen = set()
    for owner, stage in stages:
        for tool_id, tool in (stage.get('tools') or {}).items():
            if tool.get('name') != 'InspectVisualEvidence':
                continue
            if (owner, tool_id) in seen:
                continue
            seen.add((owner, tool_id))
            host = tool.get('host_observation') or {}
            call = host.get('visual_call') or {}
            ids = call.get('accounting_call_ids') or []
            if ids and all(identity in calls for identity in ids):
                continue  # Already settled (including unsuccessful observations).
            if not call and not (host.get('pixel_coverage') or {}).get('complete'):
                continue  # No host evidence that a legacy provider request ran.
            selected = (call.get('authorization') or {}).get('auth_id')
            if selected and selected != grant['auth_id']:
                continue
            if not selected:
                scope = grant['scope']
                if 'page_image' not in scope.get('content_classes', []):
                    continue
                model = call.get('model') or (host.get('pixel_coverage') or {}).get('model')
                if model and not any(route.rsplit('/', 1)[-1] == model for route in scope.get('model_routes', [])):
                    continue
                if 'external_send' not in scope.get('operation_classes', []):
                    continue
                ended = _timestamp(tool.get('completed_at'))
                if ended is not None and ended < grant['created_at']:
                    continue  # This grant did not authorize a previous request.
            yield {'receipt_id': owner + '/' + tool_id, 'owner_node_id': owner,
                'tool_id': tool_id, 'used_tokens': usage_tokens(call.get('usage')) if selected and not ids else None,
                'estimated_tokens': grant['scope'].get('cost_cap_tokens'),
                'estimate_basis': 'authorization_ceiling_without_historical_reservation',
                'reason': 'legacy_usage_missing' if selected else 'legacy_authorization_or_usage_missing'}


def authorization_account(record, grant):
    calls = [copy.deepcopy(row) for row in (record.get(JOURNAL) or {}).get('calls', {}).values()
             if row['auth_id'] == grant['auth_id']]
    legacy = list(_legacy_rows(record, grant))
    accounting = [_call_usage_accounting(row) for row in calls]
    known = sum(row['known'] for row in accounting)
    known += sum(row['used_tokens'] or 0 for row in legacy)
    reserved = sum(row['reserved'] for row in accounting)
    estimated = sum(row['estimated'] for row in accounting)
    estimated += sum(row['estimated_tokens'] or 0 for row in legacy if row['used_tokens'] is None)
    unknown = [row['call_id'] for row in calls if row['state'] in {'started', 'outcome_unknown'}]
    unknown += [row['call_id'] for row, usage in zip(calls, accounting) if usage['has_floor_estimate']
                and row['call_id'] not in unknown]
    unknown += [row['receipt_id'] for row in legacy if row['used_tokens'] is None]
    cap = grant['scope'].get('cost_cap_tokens')
    remaining = None if cap is None else max(0, cap - known - estimated - reserved)
    state = ('over_cap' if cap is not None and known + estimated > cap else 'estimated' if unknown
             else 'reserved' if reserved else 'reconciled')
    return {'auth_id': grant['auth_id'], 'limit_tokens': cap, 'used_tokens': known + estimated,
        'estimated_used_tokens': estimated, 'has_estimates': bool(unknown),
        'known_used_tokens': known, 'reserved_tokens': reserved, 'remaining_tokens': remaining,
        'unknown_call_ids': unknown, 'state': state,
        'usage_basis': 'reported_and_estimated' if unknown else BASIS,
        'reported_usage_basis': BASIS, 'accounting_scope': 'InspectVisualEvidence',
        'calls': calls, 'legacy_receipts': legacy,
        'rule': '缺失用量按已预留额度保守估算；旧记录无预留时按授权上限估算。估算不是供应商账单，后续实际回执可替换。'}


def authorization_accounts(record, grant_store=None):
    return [authorization_account(record, grant) for grant in entries(record, grant_store)
            if grant['enforceable'] and (('cost_cap_tokens' in grant['scope'] and
                'external_send' in grant['scope'].get('operation_classes', []) and
                'page_image' in grant['scope'].get('content_classes', [])) or
                any(row['auth_id'] == grant['auth_id'] for row in (record.get(JOURNAL) or {}).get('calls', {}).values()))]


def inherited_usage_calls(source, grant, target_auth_id, *, provenance, existing_calls=None):
    """Copy a grant's accounting into a later run without replacing local calls.

    Carried call IDs are deterministic and namespaced by their source identity. Each
    row keeps its accounting state, including reservations and unknown outcomes.
    Legacy observations are converted to journal rows so they remain charged after
    the previous run's node state is no longer present.
    """
    existing_calls = existing_calls if isinstance(existing_calls, dict) else {}
    account = authorization_account(source, grant)
    existing_origins = {}
    for call_id, row in existing_calls.items():
        origin = row.get('carried_from') if isinstance(row, dict) else None
        if not isinstance(origin, dict):
            continue
        key = (origin.get('conversation_id'), origin.get('task'), origin.get('auth_id'),
               origin.get('kind'), origin.get('source_id'))
        existing_origins.setdefault(key, []).append((call_id, row))
    used_ids = set(existing_calls)
    result = []

    def add(row, *, kind, source_id):
        origin_key = (provenance['conversation_id'], provenance['task'], grant['auth_id'], kind, source_id)
        matches = existing_origins.get(origin_key, [])
        if len(matches) > 1:
            raise ValueError('目标账本中存在重复的继承用量来源，无法安全同步。')
        marker = {'conversation_id': provenance['conversation_id'], 'task': provenance['task'],
                  'auth_id': grant['auth_id'], 'kind': kind, 'source_id': source_id}
        source_provenance = row.get('carried_from')
        if matches:
            call_id, existing = matches[0]
            if existing.get('auth_id') != target_auth_id:
                raise ValueError('继承用量已绑定到其他授权ID，拒绝错绑。')
            floor = existing.get('carried_minimum_tokens')
            if type(floor) is not int or floor < 0:
                floor = 0
            floor = max(floor, _call_usage_accounting(existing)['total'],
                        _call_usage_accounting(row)['total'])
        else:
            seed = json.dumps([*origin_key, target_auth_id], ensure_ascii=False, separators=(',', ':'))
            base_id = 'inherited-' + hashlib.sha256(seed.encode('utf-8')).hexdigest()[:32]
            call_id, suffix = base_id, 2
            while call_id in used_ids:
                call_id = f'{base_id}-{suffix}'
                suffix += 1
            floor = _call_usage_accounting(row)['total']
        carried = copy.deepcopy(row)
        carried.update(call_id=call_id, auth_id=target_auth_id, carried_from=marker,
                       carried_minimum_tokens=floor)
        if source_provenance is not None:
            carried['source_carried_from'] = copy.deepcopy(source_provenance)
        if not matches or carried != existing:
            result.append(carried)
        used_ids.add(call_id)
        existing_origins[origin_key] = [(call_id, carried)]

    for source_call in account['calls']:
        source_id = str(source_call.get('call_id') or '')
        if not source_id:
            source_id = hashlib.sha256(json.dumps(source_call, sort_keys=True, ensure_ascii=False,
                                                   default=str).encode('utf-8')).hexdigest()
        add(source_call, kind='journal_call', source_id=source_id)

    for receipt in account['legacy_receipts']:
        used = receipt.get('used_tokens')
        estimated = receipt.get('estimated_tokens')
        if type(used) is not int or used < 0:
            used = None
        if type(estimated) is not int or estimated < 0:
            estimated = 0
        reserved = 0 if used is not None else estimated
        receipt_id = str(receipt.get('receipt_id') or '')
        legacy = {'call_id': receipt_id, 'auth_id': grant['auth_id'],
            'state': 'settled' if used is not None else 'outcome_unknown',
            'usage': {'reported_total_tokens': used} if used is not None else None,
            'reserved_tokens': reserved, 'estimated_tokens': reserved,
            'estimate_basis': receipt.get('estimate_basis'), 'outcome': receipt.get('reason'),
            'legacy_receipt': copy.deepcopy(receipt), 'sources': []}
        add(legacy, kind='legacy_receipt', source_id=receipt_id)

    return result


class VisualAuthorizationMeter:
    """Mutations must be durable and serialized by ExpertRunStore.mutate."""

    def __init__(self, read_record, mutate_record, run_id, node_id, *, epoch, input_revision, grant_store=None,
                 execution_node_id=None):
        self.read_record, self.mutate_record = read_record, mutate_record
        self.run_id, self.node_id = run_id, node_id
        self.execution_node_id = execution_node_id or node_id
        self.epoch, self.input_revision, self.grant_store = epoch, input_revision, grant_store

    def _scope(self, record):
        from mewcode.experts.recovery_context import scoped_record
        scoped_record(lambda: record, self.run_id, self.node_id, self.epoch, self.input_revision)
        if self.execution_node_id != self.node_id:
            member = (record.get('members') or {}).get(self.execution_node_id)
            if not isinstance(member, dict) or member.get('node_id') != self.node_id:
                raise ValueError('视觉调用的执行成员不属于当前节点。')
        if record.get('status') in {'terminated', 'completed'}:
            raise ValueError('运行已结束，未开始新的视觉调用。')

    def reserve(self, route, payload, *, sources, input_estimate, output_allowance):
        identity, result = uuid.uuid4().hex, {}

        def apply(record):
            self._scope(record)
            request = copy.deepcopy(route['authorization']['request'])
            paths = sorted({source['file_path'] for source in sources})
            grant = next(row for row in entries(record, self.grant_store)
                         if row['auth_id'] == route['authorization']['auth_id'])
            if paths and 'paths' in grant['scope']:
                request['scope']['paths'] = paths
            elif paths and covers(record, {**request, 'scope': {**request['scope'], 'paths': paths}},
                                  grant_store=self.grant_store).status == 'denied':
                raise ValueError('实际页图来源已有拒绝授权，调用未开始。')
            selected = covers(record, request, grant_store=self.grant_store)
            if selected.status != 'covered' or selected.auth_id != route['authorization']['auth_id']:
                raise ValueError('视觉授权已变化或未覆盖实际页图来源；调用未开始。')
            account = authorization_account(record, grant)
            cap = account['limit_tokens']
            if cap is not None:
                output = min(output_allowance, account['remaining_tokens'] - input_estimate)
                if output <= 0:
                    raise VisualQuotaError('本次看图的剩余额度不足；缺失用量已按保守估算计入。请一次决定增加明确额度或停止本次调用，无需反复查日志。',
                        account, required_tokens=input_estimate + output_allowance, scope=grant['scope'], sources=sources)
            else:
                output = output_allowance
            row = {'call_id': identity, 'auth_id': selected.auth_id, 'state': 'reserved',
                'run_id': self.run_id, 'node_id': self.execution_node_id,
                'scope_node_id': self.node_id, 'epoch': self.epoch,
                'input_revision': self.input_revision, 'scope': copy.deepcopy(grant['scope']),
                'request_scope': copy.deepcopy(request['scope']),
                'route': {key: route.get(key) for key in ('provider_key', 'model', 'effort')},
                'packet': (payload.get('visual_evidence') or {}).get('packet'),
                'plan_digest': (payload.get('visual_evidence') or {}).get('plan_digest'),
                'request_digest': canonical_digest(payload), 'sources': copy.deepcopy(sources),
                'reserved_tokens': input_estimate + output, 'input_estimate_tokens': input_estimate,
                'output_allowance_tokens': output, 'estimate_basis': 'conversation_estimate_including_images',
                'usage': None, 'created_at': time.time(), 'provider_output_bound': False}
            record.setdefault(JOURNAL, {'version': 1, 'calls': {}})['calls'][identity] = row
            result.update(copy.deepcopy(row))

        if not callable(self.mutate_record):
            raise ValueError('视觉额度账本未挂载持久化写入，未开始外发调用。')
        self.mutate_record(apply)
        return result

    def start(self, identity, *, provider_output_bound=False):
        def apply(record):
            self._scope(record)
            row = record[JOURNAL]['calls'][identity]
            grant = next(entry for entry in entries(record, self.grant_store) if entry['auth_id'] == row['auth_id'])
            if grant['scope'] != row['scope']:
                raise ValueError('调用启动前授权范围已变化，未开始外发。')
            check = covers(record, {'operation_class': 'external_send', 'scope': row['request_scope']},
                           grant_store=self.grant_store)
            if check.status != 'covered' or check.auth_id != row['auth_id']:
                raise ValueError('调用启动前授权已变化，未开始外发。')
            if row['state'] != 'reserved':
                raise ValueError('本次预留已启动或结算，不得重复外发。')
            row.update(state='started', started_at=time.time(), provider_output_bound=provider_output_bound)
        self.mutate_record(apply)

    def settle(self, identity, *, usage=None, reported_model='', outcome='completed'):
        def apply(record):
            row = record[JOURNAL]['calls'][identity]
            if row['state'] in {'settled', 'released'}:
                if row.get('usage') != usage or row.get('outcome') != outcome:
                    raise ValueError('重复结算回执不一致，未覆盖原账目。')
                return
            used = usage_tokens(usage)
            state = 'released' if row['state'] == 'reserved' else 'settled' if used is not None else 'outcome_unknown'
            row.update(state=state, usage=copy.deepcopy(usage), reported_model=reported_model or None,
                       outcome=outcome, completed_at=time.time(),
                       estimated_tokens=row['reserved_tokens'] if state == 'outcome_unknown' else 0,
                       usage_source='estimate' if state == 'outcome_unknown' else 'reported' if used is not None else 'not_sent')
        self.mutate_record(apply)
        record = self.read_record()
        row = record[JOURNAL]['calls'][identity]
        grant = next(entry for entry in entries(record, self.grant_store) if entry['auth_id'] == row['auth_id'])
        return authorization_account(record, grant)
