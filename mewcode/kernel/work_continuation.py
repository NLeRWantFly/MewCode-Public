"""Host-observed productive work checkpoints; never acceptance or new authority."""
from __future__ import annotations

import copy
from dataclasses import replace

from mewcode.kernel.events import digest

FORMAT = "mewcode.work-continuation.v1"


def remaining_segment_policy(policy, result, elapsed):
    """Carry the original token/time limits forward; never replenish a quota."""
    consumed = result.billable_tokens if result.billable_tokens is not None else result.input_tokens + result.output_tokens
    if type(consumed) is not int or consumed < 0:
        return None
    tokens = None if policy.token_budget is None else policy.token_budget - consumed
    seconds = None if policy.timeout_seconds is None else policy.timeout_seconds - elapsed
    if tokens is not None and tokens < 1024 or seconds is not None and seconds <= 0:
        return None
    return replace(policy, resume_messages=False, token_budget=tokens, timeout_seconds=seconds)


def checkpoint_matches(attempt):
    """Only a host-bound partial delivery can be exempt from quality retry count."""
    saved = attempt.get("work_checkpoint") or {}
    delivery = attempt.get("delivery") or {}
    if not isinstance(saved, dict) or not isinstance(delivery, dict):
        return False
    return bool(saved.get("format") == FORMAT and saved.get("host_observed_progress") is True
        and attempt.get("report") is None and delivery.get("status") == "continuing"
        and delivery.get("error_kind") == "internal_work"
        and saved.get("attempt_id") == attempt.get("id")
        and saved.get("dependencies") == attempt.get("dependencies")
        and saved.get("coordinator_epoch") == attempt.get("coordinator_epoch")
        and saved.get("delivery_hash") == digest(delivery))


def _blocks_independent_context(record, key, stage, row):
    """A still-denied historical call stays denied while other work continues."""
    from mewcode.experts.recovery_state import HARD_KINDS
    if row.get("clearance_satisfied") or row.get("kind") not in HARD_KINDS:
        return False
    if row.get("kind") != "missing_authorization" or row.get("gate_kind") != "authorization_extension":
        return True
    fact = next((fact for fact in (stage.get("ledger_view") or {}).get("active_blockers", [])
                 if fact.get("fact_id") == row.get("fact_id") and fact.get("source") == "host"), None)
    call = (fact.get("payload") or {}).get("requested_call") if fact else None
    if not isinstance(call, dict) or not isinstance(call.get("tool"), str) or not isinstance(call.get("arguments"), dict):
        return True
    from mewcode.kernel.capabilities import call_gap
    # No clearance, grant or manifest binding is created. The exact operation
    # must still fail the same host gate; a later request is reviewed normally.
    return not bool(call_gap(record.get("authorization_manifest"), key, call["tool"], call["arguments"]))


def work_checkpoint(record, attempt):
    """Derive eligibility from current host stage identity, receipts and progress."""
    from mewcode.kernel import collaboration as ledger
    from mewcode.kernel.collaboration_recovery import unresolved_calls
    from mewcode.experts.recovery_state import advance_work_cursor, blockers, observed_progress_marker

    value = ledger.state(record)
    key = attempt["contract_id"]
    delivery = attempt.get("delivery") or {}
    handoff = delivery.get("handoff") or {}
    items = handoff.get("open_items")
    if (record.get("status") == "terminated" or attempt.get("report") is not None or attempt.get("superseded")
            or delivery.get("status") != "continuing" or delivery.get("error_kind") != "internal_work"
            or handoff.get("status") != "needs_attention" or handoff.get("blocker_kind") != "internal_work"
            or not isinstance(items, list) or not items or any(not isinstance(item, str) or not item.strip() for item in items)
            or any(handoff.get(field) for field in ("blockers", "question", "file_requests", "command_requests"))
            or value["contracts"][key].get("held") or ledger.revision_pending(value, key)
            or unresolved_calls(record, [key])
            or any(row.get("status") == "waiting" and row.get("scope") == "contract:" + key
                   for row in value.get("recovery_gates", {}).values())):
        return None
    try:
        ledger.check_attempt(value, attempt["id"])
    except ValueError:
        return None
    stage = (record.get("node_states") or {}).get(key) or {}
    if (stage.get("node_id") != key or type(stage.get("node_attempt")) is not int
            or stage["node_attempt"] != attempt["number"] or stage.get("input_revision") != attempt["dependencies"]
            or stage.get("handoff_input_revision", stage.get("input_revision")) != attempt["dependencies"]
            or stage.get("status") != "continuing" or stage.get("handoff") != handoff
            or stage.get("pending_permission") or stage.get("pending_interaction")
            or any(_blocks_independent_context(record, key, stage, row) for row in blockers(stage))
            or any(row.get("status") in {"queued", "running", "waiting_permission", "outcome_unknown"}
                   for row in (stage.get("tools") or {}).values())):
        return None
    before = stage.get("slice_progress_baseline")
    after = observed_progress_marker(record, key)
    previous = value["attempts"].get(attempt.get("continued_from")) or {}
    progressed, cursor = advance_work_cursor({"before": before, "after": after},
        previous=(previous.get("work_checkpoint") or {}).get("progress_cursor"))
    if not progressed:
        return None
    return {"format": FORMAT, "host_observed_progress": True, "attempt_id": attempt["id"],
            "dependencies": attempt["dependencies"], "coordinator_epoch": attempt["coordinator_epoch"],
            "delivery_hash": digest(delivery), "progress_cursor": copy.deepcopy(cursor),
            "ungranted_call_fact_ids": [row["fact_id"] for row in blockers(stage)
                if row.get("kind") == "missing_authorization" and not row.get("clearance_satisfied")],
            "authority": "Independent context continuation only; all denied operations remain denied."}


def retain_checkpoint(record, attempt_id):
    """Annotate a settled partial result, preserving its original status and bytes."""
    from mewcode.kernel import collaboration as ledger

    value = ledger.state(record)
    attempt = value["attempts"][attempt_id]
    checkpoint = work_checkpoint(record, attempt)
    if checkpoint is not None and checkpoint != attempt.get("work_checkpoint"):
        attempt["work_checkpoint"] = checkpoint
        ledger.event(value, "productive_work_checkpoint", contract_id=attempt["contract_id"], attempt_id=attempt_id)
    return checkpoint


def segment_progress(record, key):
    """Project independent work without deleting or granting a denied call."""
    from mewcode.experts.recovery_state import HARD_KINDS, work_progress
    stage = (record.get('node_states') or {}).get(key) or {}
    progress = work_progress(record, key)
    progress['continuation_blockers'] = [row for row in progress['blockers']
        if row.get('kind') not in HARD_KINDS or _blocks_independent_context(record, key, stage, row)]
    return progress


def segment_checkpoint(record, attempt):
    """Bind a settled host slice to the same dispatch, inputs and authority."""
    from mewcode.expert_group import ExpertRoleResult
    from mewcode.kernel import collaboration as ledger
    from mewcode.kernel.collaboration_faults import scheduled_yield
    from mewcode.kernel.collaboration_recovery import gate_current, unresolved_calls
    from mewcode.experts.recovery_state import advance_work_cursor

    value = ledger.state(record)
    key = attempt['contract_id']
    delivery = attempt.get('delivery') or {}
    stage = (record.get('node_states') or {}).get(key) or {}
    if (not scheduled_yield(ExpertRoleResult(**delivery))
            or record.get('status') in {'paused', 'terminated'} or value.get('blocked')
            or attempt.get('status') != 'dispatched' or attempt.get('report') is not None
            or attempt.get('run_attempt', 1) != record.get('attempt', 1)
            or stage.get('node_id') != key or type(stage.get('node_attempt')) is not int
            or stage['node_attempt'] != attempt['number']
            or stage.get('status') != delivery['status'] or stage.get('handoff') != delivery.get('handoff')
            or stage.get('input_revision') != attempt['dependencies']
            or stage.get('handoff_input_revision', stage.get('input_revision')) != attempt['dependencies']
            or value['contracts'][key].get('held') or value.get('plan_holds', {}).get(key)
            or ledger.revision_pending(value, key) or unresolved_calls(record, [key])
            or stage.get('pending_permission') or stage.get('pending_interaction')
            or any(row.get('status') in {'queued', 'running', 'waiting_permission', 'outcome_unknown'}
                   for row in (stage.get('tools') or {}).values())
            or any(row.get('scope') == 'contract:' + key and gate_current(value, row)
                   for row in value.get('recovery_gates', {}).values())):
        return None
    try:
        ledger.check_attempt(value, attempt['id'])
    except ValueError:
        return None
    progress = segment_progress(record, key)
    if progress['continuation_blockers']:
        return None
    advanced, cursor = advance_work_cursor(progress, attempt.get('segment_progress_cursor'))
    return {'format': 'mewcode.segment-continuation.v1', 'attempt_id': attempt['id'],
            'run_attempt': record.get('attempt', 1), 'dependencies': attempt['dependencies'],
            'coordinator_epoch': attempt['coordinator_epoch'], 'delivery_hash': digest(delivery),
            'stage_epoch': stage.get('epoch', 0), 'host_observed_progress': advanced,
            'progress_cursor': copy.deepcopy(cursor)}


def retain_segment(record, attempt_id, delivery):
    """Persist partial execution, leaving acceptance and fault budgets untouched."""
    from mewcode.kernel import collaboration as ledger
    value = ledger.state(record)
    attempt = ledger.check_attempt(value, attempt_id)
    attempt.update(delivery=copy.deepcopy(delivery), status='dispatched')
    attempt.pop('execution_fault', None)
    checkpoint = segment_checkpoint(record, attempt)
    attempt['segment_checkpoint'] = checkpoint
    ledger.event(value, 'execution_segment_yielded', attempt_id=attempt_id,
                 contract_id=attempt['contract_id'], reason=delivery['error_kind'])
    return checkpoint


def restore_completed_loop_delivery(record, state, loop):
    """On validated Continue, repair only a proven historical blocker projection.

    The existing certificate authorizes reuse, never a new acceptance, permission
    or business call. Any changed input, artifact or live prerequisite falls back
    to ordinary recovery. Usage and the original delivery remain untouched.
    """
    from pathlib import Path
    from mewcode.kernel import collaboration as ledger
    from mewcode.kernel.acceptance import Acceptance
    from mewcode.kernel.collaboration_recovery import gate_current, unresolved_calls

    value = record.get('collaboration') or {}
    key = state.get('failed_node_id')
    if (not value or record.get('active_node_ids') or value.get('blocked')
            or state.get('status') != 'blocked' or state.get('error_kind') != 'awaiting_input'
            or not state.get('round_open') or state.get('contract_hash') != loop.contract_hash
            or key not in loop.node_ids or key in loop.reviewer_node_ids
            or not ledger.valid_certificate(value, key)):
        return False
    cert = value['certificates'][key]
    try:
        attempt = ledger.check_attempt(value, cert['attempt_id'])
    except (ValueError, KeyError):
        return False
    contract = value['contracts'][key]
    stage = (record.get('node_states') or {}).get(key) or {}
    delivery = attempt.get('delivery') or {}
    saved = (state.get('round_results') or {}).get(key) or {}
    handoff = delivery.get('handoff') or {}
    iteration = {'loop_id': loop.loop_id, 'contract_hash': loop.contract_hash,
                 'round': state.get('round'), 'current_item': None, 'item_round': None}
    if loop.mode == 'foreach':
        index = state.get('item_index', 0)
        if type(index) is not int or not 0 <= index < len(loop.items):
            return False
        iteration.update(current_item=loop.items[index], item_round=state.get('item_round'))
    if (attempt.get('status') != 'accepted' or attempt != ledger.attempts_for(value, key)[-1]
            or attempt.get('iteration') != iteration or attempt.get('workspace')
            or cert.get('report_hash') != digest(attempt.get('report'))
            or delivery.get('status') != 'completed' or delivery.get('error_kind')
            or handoff.get('status') != 'completed'
            or any(handoff.get(field) for field in ('blockers', 'question', 'file_requests', 'command_requests'))
            or saved.get('status') != 'needs_attention' or saved.get('error_kind') != 'awaiting_input'
            or {**saved, 'status': delivery.get('status'), 'error_kind': delivery.get('error_kind')} != delivery
            or stage.get('node_id') != key or stage.get('node_attempt') != attempt['number']
            or stage.get('input_revision') != attempt['dependencies']
            or stage.get('handoff_input_revision', stage.get('input_revision')) != attempt['dependencies']
            or stage.get('handoff') != handoff or stage.get('pending_permission') or stage.get('pending_interaction')
            or (stage.get('safety_review_state') or {}).get('pending_call_ref')
            or contract.get('held') or value.get('plan_holds', {}).get(key) or ledger.revision_pending(value, key)
            or unresolved_calls(record, [key])
            or any(gate_current(value, row) for row in value.get('recovery_gates', {}).values())
            or any(row.get('status') in {'queued', 'running', 'waiting_permission', 'outcome_unknown'}
                   for row in (stage.get('tools') or {}).values())):
        return False
    denied = (saved.get('work_progress') or {}).get('blockers') or []
    if not denied or any(row.get('kind') != 'missing_authorization'
                         or row.get('gate_kind') != 'authorization_extension'
                         or _blocks_independent_context(record, key, stage, row) for row in denied):
        return False
    progress = segment_progress(record, key)
    manifest = record.get('authorization_manifest') or {}
    workspace = manifest.get('workspace')
    if (progress['continuation_blockers'] or not manifest.get('approved') or not workspace
            or not Path(workspace).is_dir() or not Acceptance(workspace).unchanged(cert['artifacts'])):
        return False
    state['round_results'][key] = {**copy.deepcopy(delivery), 'work_progress': progress, 'restored': True}
    state.setdefault('recovery_history', []).append({'reason': 'accepted_delivery_restored',
        'node_id': key, 'attempt_id': attempt['id'], 'report_hash': cert['report_hash'],
        'previous_status': saved['status'], 'previous_error_kind': saved['error_kind']})
    state.update(status='running', reason='', error_kind='', failed_node_id='',
                 interrupted_nodes=[node for node in state.get('interrupted_nodes', []) if node != key])
    if state.get('interrupted_node') == key:
        state.pop('interrupted_node')
    for collection in ('node_states', 'stages'):
        visible = (record.get(collection) or {}).get(key)
        if visible and visible.get('handoff') == handoff:
            visible.update(status='completed', error_kind='', status_reason='')
    return True
