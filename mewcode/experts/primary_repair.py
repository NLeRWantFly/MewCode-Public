"""Compatibility entrypoint; coordination lives in assistance."""
from mewcode.experts.assistance import AssistanceCoordinator
PrimaryRepairCoordinator = AssistanceCoordinator


def record_primary_verification(store, run_id, event):
    # Provisional graph events are not final verification of the repair.
    if event.get("restored") or event.get("status") not in {
            "completed", "failed", "blocked", "needs_attention", "budget_exhausted", "stalled", "incomplete_audit"}:
        return
    current = store.read_stage_fields(run_id, event.get('node_id'), 'primary_repairs')
    if not any(entry.get("status") == "ready_to_verify" for entry in current.get("primary_repairs") or []):
        return
    def update(value):
        stage = (value.get("node_states") or {}).get(event.get("node_id")) or {}
        for entry in stage.get("primary_repairs") or []:
            if entry.get("status") == "ready_to_verify" and entry.get("input_revision") == stage.get("input_revision"):
                entry.update(status="verified" if event.get("status") == "completed" else "verification_failed",
                             verification_status=event.get("status"), verification_error=event.get("error_kind", ""))
    store.mutate(run_id, update, return_snapshot=False)
