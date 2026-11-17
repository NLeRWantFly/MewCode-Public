"""Deterministic, bounded attempt briefs and immutable full-history references."""
import copy
import json


def archive_attempt(record, node_id, *, bucket='node_states', force=False):
    from mewcode.kernel.events import digest
    stage = record.get(bucket, {}).get(node_id) or {}
    if not stage or not force and not stage.get('messages'):
        return None
    data = copy.deepcopy(stage)
    identity = digest([node_id, record.get('attempt'), data])
    archives = record.setdefault('kernel_attempt_archives', {})
    archives.setdefault(identity, {'archive_id': identity, 'node_id': node_id,
        'attempt': record.get('attempt'), 'event_revision': record.get('kernel_revision'),
        'data': data, 'data_digest': digest(data)})
    stage.update(messages=[], text='', partial_text='', last_archive_id=identity)
    return identity


def begin_stage_attempt(record, event):
    """Start a real host node call, retaining older receipts in its archive.

    Safety-review state, grants, usage and recovery counters stay on the node.
    Only the new call's transcript/receipt window is cleared on an increment.
    """
    if record.get('status') in {'paused', 'blocked', 'terminated', 'completed'}:
        return False
    node_id = str(event['node_id'])
    number = event.get('node_attempt', 1)
    if type(number) is not int or number <= 0:
        raise ValueError('节点开始事件缺少有效尝试编号')
    stage = record.setdefault('node_states', {}).setdefault(node_id, {})
    previous = stage.get('node_attempt')
    if type(previous) is int and previous > number:
        return False
    if previous != number and any(stage.get(key) for key in ('messages', 'tools', 'handoff', 'text', 'partial_text')):
        archive_attempt(record, node_id, force=True)
        stage.setdefault('handoff_input_revision', stage.get('input_revision', ''))
        for key in ('messages', 'tools', 'output_chunks', 'wait_intervals'):
            stage[key] = {} if key == 'tools' else []
        for key in ('text', 'partial_text', 'partial_reasoning', 'display_reasoning', 'request_error', 'error_kind'):
            stage[key] = ''
    # A resumed dispatch retains earlier settled tools. Its evidence window
    # spans those slices, while started_at still describes the current slice.
    if previous == number:
        stage.setdefault('attempt_started_at', stage.get('started_at', event.get('started_at')))
        stage.setdefault('execution_run_attempts', [stage.get('attempt', record.get('attempt', 1))])
    else:
        stage['attempt_started_at'] = event.get('started_at')
        stage['execution_run_attempts'] = []
    if record.get('attempt', 1) not in stage['execution_run_attempts']:
        stage['execution_run_attempts'].append(record.get('attempt', 1))
    for key in ('completed_at', 'duration_ms', 'wait_seconds', 'active_seconds', 'status_reason', 'reason'):
        stage.pop(key, None)
    stage.update(node_id=node_id, node_attempt=number, status='running', attempt=record.get('attempt', 1))
    record['stage_cursor'] = node_id
    if event.get('started_at') is not None:
        stage['started_at'] = event['started_at']
    if event.get('input_revision'):
        stage['input_revision'] = event['input_revision']
    active = record.setdefault('active_node_ids', [])
    if node_id not in active:
        active.append(node_id)
    record.setdefault('node_attempts', {})[node_id] = number
    return True


def brief(record, node_id, *, limit=8000):
    if not 1000 <= limit <= 16000:
        raise ValueError('brief limit must be 1000..16000 characters')
    from mewcode.experts.node_ledger import ledger_for_run
    from mewcode.kernel.escalation import LEVELS, used_levels
    from mewcode.expert_runs import redact_for_display
    view = ledger_for_run(copy.deepcopy(record)).view(node_id.split('::', 1)[0])
    def evidence(fact):
        return {'fact_id': fact.fact_id, 'kind': fact.kind, 'source': fact.source,
                'evidence': json.dumps(redact_for_display(fact.payload), ensure_ascii=False, sort_keys=True)[:700]}
    facts = [evidence(fact) for fact in view.history if fact.source in {'host', 'user'}
             and fact.kind in {'artifact', 'clearance', 'answer', 'progress', 'handoff'} and not fact.superseded_by]
    failed = [evidence(fact) for fact in view.current_receipts if fact.kind == 'receipt' and
              fact.payload.get('status', fact.payload.get('state')) in {'failed', 'not_started', 'blocked', 'outcome_unknown'}]
    blockers = [evidence(fact) for fact in view.active_blockers]
    used = used_levels(view)
    payload = {'schema_version': 1, 'node_id': node_id, 'epoch': view.epoch,
               'confirmed_facts': facts[-12:], 'failed_methods': failed[-12:], 'current_blockers': blockers[-12:],
               'remaining_budget': {level: int(level not in used) for level in LEVELS},
               'archives': [key for key, row in record.get('kernel_attempt_archives', {}).items()
                            if row['node_id'] == node_id][-12:],
               'rule': '执行以宿主最新 state、当前阻塞和有效回执为准。已有证据直接复用；'
                       '归档仅在当前必需证据缺失时按精确引用读取，不要重新翻查历史恢复进度。'
                       '失败记录不是当前阻塞；授权以当前清单为准。'}
    def render():
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    while len(render()) > limit:
        candidates = ['confirmed_facts', 'failed_methods', 'current_blockers']
        key = max(candidates, key=lambda name: len(payload[name]))
        if not payload[key]:
            break
        payload[key].pop(0)
        payload['truncated'] = True
    return render()
