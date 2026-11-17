"""Host-owned recovery clearance policy.

An error label is not a permission grant.  Each currently reported blocker has
an explicit, machine-readable condition for clearing it; callers must obtain
the corresponding host evidence before retrying or asking for a decision.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable, Literal, Mapping

from mewcode.experts.recovery_diagnosis import KINDS


# These are the twelve *diagnostic* categories.  A category describes what to
# try, never whether a particular write, approval, or business result is safe.
DIAGNOSTIC_POLICY = {
    "capacity_shortfall": ("retry_same_checkpoint", "durable_progress_or_smaller_batch", "automatic_recovery_exhausted"),
    "idle_stall": ("replan_execution_method", "new_durable_progress", "automatic_recovery_exhausted"),
    "environment_fault": ("replan_execution_method", "corrected_typed_path_or_tool", "automatic_recovery_exhausted"),
    "review_infrastructure": ("retry_review_after_recheck", "current_review_service_receipt", "automatic_recovery_exhausted"),
    "protocol_conflict": ("reconcile_protocol", "current_item_revision_receipt", "automatic_recovery_exhausted"),
    "model_service_fault": ("retry_same_checkpoint", "completed_model_response", "automatic_recovery_exhausted"),
    "state_divergence": ("reconcile_state", "matching_current_file_and_ledger_receipts", "automatic_recovery_exhausted"),
    "business_ambiguity": ("human_decision", "explicit_contract_decision", "acceptance_or_scope_change"),
    "insufficient_evidence": ("gather_read_only_facts", "independent_source_evidence", "automatic_recovery_exhausted"),
    "context_window_limit": ("replan_execution_method", "bounded_context_fits_model", "automatic_recovery_exhausted"),
    "reasoning_limit": ("request_deeper_reasoning", "capable_route_judgment_receipt", "new_authority_or_automatic_recovery_exhausted"),
    "output_window_limit": ("replan_execution_method", "complete_output_chunk_chain", "automatic_recovery_exhausted"),
}

# Compatibility imports are derived from the kernel's single policy registry.
from mewcode.kernel.blockers import BLOCKER_CLEARANCE, BLOCKER_CLASS

BlockerClass = Literal["auto", "authority", "business"]


@dataclass(frozen=True)
class ClearanceReceipt:
    clearance_kind: str
    blocker_fact_id: str
    evidence_ref: str
    produced_by: str


@dataclass(frozen=True)
class ClearanceContext:
    blocker: object
    node_view: object
    observations: Mapping | None = None


ClearanceProducer = Callable[[ClearanceContext], ClearanceReceipt | None]


def _evidence(ctx, name, predicate):
    blocker = ctx.blocker
    ref = blocker.payload.get("ref") or blocker.payload.get("request_id")
    for fact in reversed(ctx.node_view.history):
        if (fact.epoch != ctx.node_view.epoch or fact.superseded_by
                or fact.source not in {"host", "user", "maintenance"}
                or fact.kind not in {"receipt", "answer", "progress", "maintenance", "artifact"}):
            continue
        payload = fact.payload
        bound = payload.get("blocker_fact_id") == blocker.fact_id or (
            bool(ref) and ref == (payload.get("request_id") or payload.get("ref")))
        if predicate(payload, bound, fact):
            return ClearanceReceipt(name, blocker.fact_id, fact.fact_id, "produce_" + name)
    return None


def produce_capability_binding_receipt(ctx):
    return _evidence(ctx, "capability_binding_receipt", lambda p, bound, f:
        bound and f.source == "host" and p.get("probe_success") is True
        and p.get("required_tools_available") is True)


def produce_completed_model_response(ctx):
    return _evidence(ctx, "completed_model_response", lambda p, bound, f:
        f.source == "host" and p.get("model_response_completed") is True and f.created_at >= ctx.blocker.created_at)


def produce_independent_effect_receipt(ctx):
    return _evidence(ctx, "independent_effect_receipt", lambda p, bound, f:
        bound and f.source == "host" and p.get("host_verified") is True
        and p.get("effect") in {"applied", "not_applied"})


def produce_answer_to_original_request_id(ctx):
    return _evidence(ctx, "answer_to_original_request_id", lambda p, bound, f:
        bound and f.kind == "answer" and (f.source == "user" or (
            p.get("mode") == "auto_selected_recommended" and p.get("host_continuation") is True
            and p.get("expands_authority") is False)))


def produce_environment_ready_receipt(ctx):
    return _evidence(ctx, "environment_ready_receipt", lambda p, bound, f:
        f.source == "host" and ((p.get("runtime_ready") is True and p.get("python_probe_success") is True
        and (bound or f.created_at >= ctx.blocker.created_at)) or (
            bound and p.get('node_preflight_passed') is True and bool(p.get('manifest_sha256'))
            and f.created_at >= ctx.blocker.created_at)))


def record_node_preflight(record, node_id, findings, manifest):
    """Close dispatch prerequisite faults with a fresh, bound host probe.

    Tool/acceptance failures and permission/business gates require their own
    exact receipts. Legacy faults require a matching host preflight event;
    their generic environment label alone never proves the failed component.
    """
    from mewcode.experts.node_ledger import ledger_for_run
    from mewcode.experts.recovery_state import digest
    record.setdefault('stage_preflight', {})[node_id] = findings
    if (any(item.get('severity') == 'error' for item in findings)
            or manifest != record.get('authorization_manifest')
            or not manifest.get('approved') or node_id not in (manifest.get('nodes') or {})):
        return
    facts = ledger_for_run(record)
    for blocker in facts.view(node_id).active_blockers:
        if (blocker.payload.get('kind') != 'environment_fault'
                or not str(blocker.payload.get('ref', '')).startswith('collaboration-fault:')):
            continue
        origin = blocker.payload.get('fault_origin')
        legacy_event = None
        if not origin:
            boundaries = [event for event in record.get('events') or []
                if event.get('node_id') == node_id and event.get('type') in {
                    'expert_constraint_blocked', 'expert_node_started', 'expert_node_completed',
                    'expert_tool', 'expert_tool_start', 'expert_tool_result'}
                and type(event.get('server_time')) in {int, float}
                and event['server_time'] <= blocker.created_at]
            legacy_event = max(boundaries, key=lambda row: row['server_time'], default={})
            if (legacy_event.get('type') != 'expert_constraint_blocked'
                    or not str(legacy_event.get('text') or '').startswith('当前节点环境预检未通过：')):
                continue
        elif origin != 'node_preflight':
            continue
        facts.record(node_id, 'receipt', {'blocker_fact_id': blocker.fact_id,
            'node_preflight_passed': True, 'manifest_sha256': digest(manifest),
            'findings_sha256': digest(findings), 'legacy_preflight_event': legacy_event},
            identity=['node_preflight', blocker.fact_id, digest(manifest)])
    refresh_clearances(record, node_id)
    from mewcode.kernel.escalation import retire_resolved_recovery
    retire_resolved_recovery(record, node_id)


def produce_explicit_permission_receipt(ctx):
    return _evidence(ctx, "explicit_permission_receipt", lambda p, bound, f:
        bound and f.source == "user" and p.get("approved") is True and p.get("exact_operation") is True)


def record_permission_response(record, node_id, tool_id, response):
    """Persist only an actual UI response to this exact pending operation."""
    from mewcode.agent import PermissionResponse
    from mewcode.experts.node_ledger import ledger_for_run, sync_node_facts
    sync_node_facts(record, node_id, include_legacy_blockers=True)
    from mewcode.experts.authorization_ledger import record_authorization
    from mewcode.experts.recovery_case import digest
    pending = record.get('node_states', {}).get(node_id, {}).get('pending_permission') or {}
    allowed_response = response in {PermissionResponse.ALLOW, PermissionResponse.ALLOW_ALWAYS, PermissionResponse.ALLOW_DIRECTORY}
    if pending.get('tool_id') == tool_id:
        record_authorization(record, kind='gate_answer' if allowed_response else 'denial',
            scope={'operation_classes': ['other'], 'tools': [str(pending.get('name') or pending.get('tool') or 'unknown')],
                'operation_digest': digest(
                    [pending.get('name') or pending.get('tool'), pending.get('arguments') or {}]),
                'node_ids': [node_id], 'epoch': ledger_for_run(record).view(node_id).epoch},
            quote=response.value, source_ref='permission:' + tool_id, host_verified=allowed_response)
    if response not in {PermissionResponse.ALLOW, PermissionResponse.ALLOW_ALWAYS, PermissionResponse.ALLOW_DIRECTORY}:
        return
    ledger = ledger_for_run(record)
    for blocker in ledger.view(node_id).active_blockers:
        if blocker.payload.get('kind') in {'permission', 'permission_required', 'permission_denied'} and blocker.payload.get('ref') == tool_id:
            ledger.record(node_id, 'answer', {'blocker_fact_id': blocker.fact_id,
                'tool_id': tool_id, 'approved': True, 'exact_operation': True,
                'response': response.value}, source='user', identity=['permission_response', blocker.fact_id, response.value])
    refresh_clearances(record, node_id)


def record_reviewed_tool_start(record, node_id, tool_id, tool_name, arguments):
    """ToolExecutionStart is emitted after the ordinary permission gateway.

    It can retire a failed review of the same operation, never an unrelated
    review, human permission, or unknown execution effect.
    """
    from mewcode.experts.node_ledger import ledger_for_run
    from mewcode.permissions.review_retry import action_key
    ledger = ledger_for_run(record)
    stage = record.get('node_states', {}).get(node_id) or {}
    key = action_key(tool_name, arguments)
    matching = {str(row.get('tool_id') or identity) for identity, row in (stage.get('tools') or {}).items()
        if row.get('name') == tool_name and action_key(tool_name, row.get('args')) == key}
    route_matches = (stage.get('safety_review') or {}).get('action_key') == key
    for blocker in ledger.view(node_id).active_blockers:
        if blocker.payload.get('kind') not in {'safety_review', 'legacy_safety_review', 'safety_review_unavailable', 'safety_review_inconclusive'}:
            continue
        ref = blocker.payload.get('ref')
        if ref in matching or (ref == 'current_stop' and route_matches):
            ledger.record(node_id, 'receipt', {'blocker_fact_id': blocker.fact_id,
                'tool_id': tool_id, 'action_key': key, 'decision': 'allow', 'binding_current': True},
                identity=['reviewed_start', blocker.fact_id, tool_id])
    refresh_clearances(record, node_id)


def produce_current_safety_review_receipt(ctx):
    return _evidence(ctx, "current_safety_review_receipt", lambda p, bound, f:
        bound and f.source == "host" and p.get("decision") == "allow" and p.get("binding_current") is True)


def produce_approved_plan_revision(ctx):
    return _evidence(ctx, "approved_plan_revision", lambda p, bound, f:
        bound and f.source == "maintenance" and p.get("adopted") is True and p.get("user_confirmed") is True)


def produce_approved_contract_revision(ctx):
    return _evidence(ctx, "approved_contract_revision", lambda p, bound, f:
        bound and f.source == "host" and p.get("contract_adopted") is True
        and p.get("independent_review") is True and bool(p.get("proposal_id"))
        and type(p.get("contract_version")) is int and p["contract_version"] > 1)


def produce_verified_dependency_receipt(ctx):
    return _evidence(ctx, "verified_dependency_receipt", lambda p, bound, f:
        bound and f.source == "host" and p.get("dependency_verified") is True)


def produce_new_authorized_deadline(ctx):
    return _evidence(ctx, "new_authorized_deadline", lambda p, bound, f:
        bound and f.source == "user" and p.get("deadline_authorized") is True)


def produce_readonly_retry_receipt(ctx):
    return _evidence(ctx, "readonly_retry_receipt", lambda p, bound, f:
        bound and f.source == "host" and p.get("effect_class") == "readonly"
        and p.get("retry_reserved") is True and p.get("retry_count") == 1)


def produce_explicit_contract_decision(ctx):
    return _evidence(ctx, "explicit_contract_decision", lambda p, bound, f:
        bound and f.source == "user" and p.get("contract_decision") is not None)


CLEARANCE_PRODUCERS: dict[str, ClearanceProducer] = {
    name: globals()["produce_" + name] for name in set(BLOCKER_CLEARANCE.values())
}


def refresh_clearances(record, node_id):
    from mewcode.experts.node_ledger import ledger_for_run, project_node_view
    ledger = ledger_for_run(record)
    view = ledger.view(node_id)
    produced = []
    for blocker in view.active_blockers:
        name = clearance_for_blocker(str(blocker.payload.get("kind") or "business_ambiguity"))
        receipt = CLEARANCE_PRODUCERS[name](ClearanceContext(blocker, view))
        if receipt is not None:
            ledger.record(node_id, "clearance", asdict(receipt), identity=asdict(receipt))
            produced.append(receipt)
    project_node_view(record, node_id)
    return produced


def policy_for(kind: str) -> dict[str, str]:
    """Return a complete policy row or raise for an unreviewed category."""
    action, clearance, human_when = DIAGNOSTIC_POLICY[kind]
    return {"kind": kind, "automatic_action": action,
            "clearance": clearance, "human_when": human_when}


def clearance_for_blocker(kind: str) -> str:
    return BLOCKER_CLEARANCE[kind]


if set(DIAGNOSTIC_POLICY) != set(KINDS):
    raise RuntimeError("recovery diagnostic policy is incomplete")
