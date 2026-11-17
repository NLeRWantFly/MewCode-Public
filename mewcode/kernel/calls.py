"""Strict invocation lifecycle, shared identities and durable execution gateway."""
import copy
from enum import StrEnum

from mewcode.kernel.events import digest


class CallState(StrEnum):
    PROPOSED = 'proposed'
    REVIEWED = 'reviewed'
    DISPATCHED = 'dispatched'
    SUCCEEDED = 'succeeded'
    FAILED = 'failed'
    UNKNOWN = 'unknown'
    REJECTED = 'rejected'
    NOT_STARTED = 'not_started'


TRANSITIONS = {
    'proposed': {'reviewed', 'rejected', 'not_started'},
    'reviewed': {'dispatched', 'rejected', 'not_started'},
    'dispatched': {'succeeded', 'failed', 'unknown', 'not_started'},
    'unknown': {'succeeded', 'failed', 'not_started'},
    'rejected': {'proposed'}, 'not_started': {'proposed'},
    'succeeded': set(), 'failed': set(),
}


def invocation_id(node_id, input_revision, invocation):
    return digest([node_id, input_revision, invocation])


def canonical_invocation(tool_name, arguments):
    arguments = copy.deepcopy(arguments)
    if tool_name == 'WriteFile':
        arguments.setdefault('encoding', 'utf-8')
        arguments.setdefault('mode', None)
    return {'tool': tool_name, 'arguments': arguments}


def definite_receipt(payload):
    return (payload.get('state', payload.get('status')) in {'succeeded', 'failed', 'completed', 'exited'}
            and payload.get('command_started') is not False)


def successful_receipt(payload):
    return (payload.get('state') == 'succeeded'
            or (payload.get('status') in {'completed', 'exited'} and payload.get('exit_code') == 0)
            or payload.get('effect_verified') is True or payload.get('verified_effect') == 'applied')


def failed_before_effect(row):
    """Only the host's explicit pre-effect observation proves non-execution."""
    result = row.get('result') or {}
    if not isinstance(result, dict):
        return False
    host = result.get('host_observation') or {}
    if not isinstance(host, dict):
        return False
    command = host.get('command_execution') or {}
    if not isinstance(command, dict):
        return False
    return (row.get('state') == 'failed' and result.get('is_error') is True
            and host.get('operation_blocked') is True and host.get('phase') == 'before_effect'
            and command.get('command_started') is not True
            and command.get('status') not in {'timed_out', 'unknown', 'outcome_unknown'})


def unstarted_retry_calls(record, node_id):
    """Read-only candidates with canonical identities and proven non-execution."""
    calls = record.get('kernel_calls') or {}
    grants = record.get('kernel_call_retry_authorizations') or {}
    authorized = []
    for key, call in calls.items():
        if (call.get('node_id') != node_id or call.get('retry_of') or call.get('origin') == 'legacy'
                or call.get('user_adjudication') or not failed_before_effect(call)
                or key != call.get('call_id')
                or key != invocation_id(node_id, call.get('input_revision'), call.get('invocation'))):
            continue
        prior = grants.get(key)
        previous = calls.get(digest([key, 'user_retry', prior['decision_id']])) if prior else None
        if previous and not failed_before_effect(previous):
            continue  # Success, uncertainty and an already active retry are not replayable.
        authorized.append(key)
    return authorized


def authorize_unstarted_retries(record, node_id, decision_id):
    """Called only by a validated user retry gate; old receipts stay immutable."""
    authorized = unstarted_retry_calls(record, node_id)
    for key in authorized:
        call = record['kernel_calls'][key]
        record.setdefault('kernel_call_retry_authorizations', {})[key] = {
            'source': 'user', 'decision_id': decision_id, 'node_id': node_id,
            'input_revision': call['input_revision'], 'source_call_hash': digest(call)}
    return authorized


def reused_tool_result(call):
    """Return old terminal evidence without attributing its effects to this call."""
    from mewcode.tools.base import ToolResult
    from mewcode.experts.command_receipts import execution_observation
    if call['state'] not in {'succeeded', 'failed'}:
        raise ValueError('only a terminal receipt can be reused')
    saved = call['result']
    old_host = saved.get('host_observation') or {}
    old_command = old_host.get('command_execution') if isinstance(old_host, dict) else None
    source = {key: call.get(key) for key in ('call_id', 'node_id', 'input_revision', 'state')}
    source['result_sha256'] = digest(saved)
    source['source_tool_id'] = next((item['tool_id'] for item in reversed(call.get('evidence') or [])
        if item.get('executor_accepted') and item.get('tool_id')), None)
    if isinstance(old_command, dict):
        source['command_execution'] = {key: old_command.get(key)
            for key in ('status', 'command_started', 'exit_code')}
    observation = {'reused': True, 'kernel_receipt_reuse': source}
    reason = ('复用既有内核回执 ' + str(call['call_id']) +
              '；本次未重新执行，原结果不证明当前文件或环境已验证。')
    if isinstance(old_command, dict) or (call.get('invocation') or {}).get('tool') in {
            'Bash', 'RunVerification', 'TerminalStart', 'TerminalInput'}:
        observation['command_execution'] = execution_observation(
            'not_started', reason=reason, command_started=False)
    return ToolResult('KERNEL_RECEIPT_REUSED: ' + reason + '\n原始结果：\n' + saved['output'],
        is_error=saved['is_error'], content_blocks=copy.deepcopy(saved.get('content_blocks') or []),
        host_observation=observation)


def propose(record, node_id, input_revision, invocation):
    identity = invocation_id(node_id, input_revision, invocation)
    calls = record.setdefault('kernel_calls', {})
    effect = digest([node_id, invocation])
    unresolved = next((row for row in calls.values() if row.get('effect_id') == effect
                       and row['state'] in {'dispatched', 'unknown'} and not row.get('user_adjudication')), None)
    if unresolved:
        return unresolved
    imported = next((row for row in calls.values() if row.get('effect_id') == effect and row.get('origin') == 'legacy'
                     and row['state'] in {'succeeded', 'failed'} and row.get('import_scope') == digest([
                         record.get('task'), record.get('run_inputs')])), None)
    if imported:
        return imported
    assumed = next((row for row in reversed(list(calls.values())) if row.get('effect_id') == effect
                    and row.get('user_adjudication')), None)
    if assumed:
        if assumed['user_adjudication']['disposition'] == 'assume_applied':
            return assumed
        identity = digest([node_id, input_revision, invocation, assumed['user_adjudication']])
    retry = None
    original = calls.get(identity)
    grant = (record.get('kernel_call_retry_authorizations') or {}).get(identity) or {}
    if (original and failed_before_effect(original) and grant.get('source') == 'user'
            and grant.get('decision_id') and grant.get('node_id') == node_id
            and grant.get('input_revision') == input_revision
            and grant.get('source_call_hash') == digest(original)):
        retry = {'retry_of': identity, 'retry_decision_id': grant['decision_id']}
        identity = digest([identity, 'user_retry', grant['decision_id']])
    if identity not in calls:
        calls[identity] = {'call_id': identity, 'node_id': node_id, 'input_revision': input_revision,
                           'invocation': copy.deepcopy(invocation), 'effect_id': effect,
                           'state': 'proposed', 'history': ['proposed'], **(retry or {})}
    row = calls[identity]
    if row['state'] in {'rejected', 'not_started'}:
        transition(row, 'proposed')
    return row


def transition(row, state, *, evidence=None):
    state = CallState(state).value
    if state not in TRANSITIONS[row['state']]:
        raise ValueError(f"illegal call transition: {row['state']} -> {state}")
    if state == 'dispatched' and not (evidence or {}).get('executor_accepted'):
        raise ValueError('dispatch requires host executor acceptance')
    if row['state'] in {'dispatched', 'unknown'} and state == 'not_started' and not (
            (evidence or {}).get('command_started') is False and (evidence or {}).get('host_verified') is True):
        raise ValueError('non-execution requires bound host evidence')
    if row['state'] == 'unknown' and not (evidence or {}).get('host_verified'):
        raise ValueError('unknown effects require reconciliation evidence')
    row['state'] = state
    row['history'].append(state)
    if evidence is not None:
        row.setdefault('evidence', []).append(copy.deepcopy(evidence))
    return row


class CallGateway:
    """The existing run-store writer owns every dispatch and terminal receipt."""
    def __init__(self, store, run_id, node_id, input_revision):
        self.store, self.run_id, self.node_id, self.input_revision = store, run_id, node_id, input_revision
        self.prepared_ids = {}

    def _mutate(self, callback):
        scoped = getattr(self.store, 'mutate_tool_boundary', None)
        if callable(scoped):
            return scoped(self.run_id, self.node_id, callback, fields=('kernel_calls',))
        result = []
        self.store.mutate(self.run_id, lambda record: result.append(callback(record)),
                          durable=True, return_snapshot=False)
        return copy.deepcopy(result[0])

    def read(self, call_id):
        scoped = getattr(self.store, 'read_kernel_call', None)
        if callable(scoped):
            return scoped(self.run_id, call_id)
        return (self.store.load(self.run_id).get('kernel_calls') or {}).get(call_id)

    def prepare(self, tc):
        invocation = canonical_invocation(tc.tool_name, tc.arguments)
        def apply(record):
            return propose(record, self.node_id, self.input_revision, invocation)
        row = self._mutate(apply)
        self.prepared_ids[invocation_id(self.node_id, self.input_revision, invocation)] = row['call_id']
        return row

    def unstarted(self, tc, result):
        identity = invocation_id(self.node_id, self.input_revision, canonical_invocation(tc.tool_name, tc.arguments))
        identity = self.prepared_ids.get(identity, identity)
        row = self.read(identity)
        if row and row['state'] in {'proposed', 'reviewed'}:
            self.update(identity, 'rejected' if 'AUTO_DENIED' in result.output else 'not_started',
                        evidence={'host_verified': True, 'command_started': False}, result=result)

    def interrupted(self, call_id, *, tool_id, effect_started=True):
        # Cancellation may arrive while the durable dispatch worker is finishing.
        # Inspect its committed state before recording the interrupted effect.
        row = self.read(call_id)
        if row and row['state'] == 'dispatched':
            evidence = {'executor_accepted': True, 'tool_id': tool_id}
            if not effect_started:
                evidence.update(host_verified=True, command_started=False)
            self.update(call_id, 'unknown' if effect_started else 'not_started', evidence=evidence)

    def update(self, call_id, state, *, evidence=None, result=None):
        def apply(record):
            row = record['kernel_calls'][call_id]
            transition(row, state, evidence=evidence)
            if result is not None:
                row['result'] = {'output': result.output, 'is_error': result.is_error,
                                 'content_blocks': copy.deepcopy(result.content_blocks),
                                 'host_observation': copy.deepcopy(result.host_observation)}
            from mewcode.experts.node_ledger import ledger_for_run
            ledger = ledger_for_run(record)
            statuses = {'succeeded': 'completed', 'failed': 'completed', 'unknown': 'outcome_unknown',
                        'not_started': 'not_started', 'rejected': 'not_started', 'dispatched': 'in_flight'}
            if state in statuses:
                ledger.record(self.node_id, 'receipt', {'request_id': call_id, 'state': state,
                    'status': statuses[state], 'effect_class': 'side_effecting',
                    'executor_accepted': state in {'dispatched', 'succeeded', 'failed', 'unknown'},
                    'command_started': (evidence or {}).get('command_started'),
                    'exit_code': (evidence or {}).get('exit_code')},
                    identity=['kernel_call', call_id, len(row['history'])])
            if state == 'unknown':
                from mewcode.kernel.blockers import policy
                contract = policy('unknown_effect')
                ledger.record(self.node_id, 'blocker', {'kind': 'unknown_effect', 'ref': call_id,
                              'owner': contract.owner, 'clearance': contract.clearance}, identity=['unknown_call', call_id])
            from mewcode.experts.node_ledger import project_node_view
            project_node_view(record, self.node_id)
            return row
        return self._mutate(apply)
