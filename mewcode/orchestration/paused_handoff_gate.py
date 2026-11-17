"""Host-owned gate for returning from a paused-dialogue handoff.

This module cannot approve business work.  It can only establish that a typed
handoff decision was based on a complete, current, source-bound review and
that the original execution/approval boundaries still hold.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mewcode.orchestration.paused_handoff_classify import CLASSIFIER_VERSION, host_retention
from mewcode.orchestration.paused_handoff_compression import supporting_source_references
from mewcode.orchestration.paused_handoff_index import PausedHandoffIndex
from mewcode.orchestration.paused_handoff_omission import OMISSION_VERSION
from mewcode.orchestration.paused_handoff_review import ReviewLedger
from mewcode.orchestration.paused_handoff_reconciliation import unresolved_hard_holds
from mewcode.orchestration.paused_handoff_semantic import SEMANTIC_REVIEW_VERSION


REQUIRED_SNAPSHOT_FIELDS = (
    "run_id", "conversation_id", "contract_fingerprint", "plan_revision",
    "node_id", "permission_revision", "ledger_fingerprint",
)
SEMANTIC_AUDIT_VERSION = "paused-handoff-audit-v1"


def audit_semantic_risk(index: PausedHandoffIndex,
                        ledger: ReviewLedger) -> dict[str, Any]:
    """Audit observable hard signals and typed risk preservation.

    This does not claim to prove every possible business interpretation.  The
    independent final decision and original acceptance gates remain separate.
    """
    checked = 0
    missed = 0
    unclassified = 0
    suspicious_low_attention = 0
    unverified_low_attention = 0
    for record in index.manifest["records"]:
        mandatory, _flags = host_retention(record)
        if mandatory in {"core", "uncertain"}:
            checked += 1
        for fragment in record["fragments"]:
            classification = ledger.completed_classification(fragment)
            if classification is None:
                unclassified += 1
                continue
            level = classification.get("level")
            if mandatory in {"core", "uncertain"} and level not in {"core", "uncertain"}:
                missed += 1
            if (level in {"routine", "unrelated"}
                    and any(flag in {"contract_change", "state_change", "cross_fragment"}
                            for flag in classification.get("risk_flags") or [])):
                suspicious_low_attention += 1
            if level in {"routine", "unrelated"}:
                receipt = ledger.latest.get(fragment["id"]) or {}
                judgment = receipt.get("judgment") or {}
                try:
                    confidence = float(judgment.get("confidence"))
                    risks = {key: float(value) for key, value in
                             (judgment.get("probabilities") or {}).items()}
                except (TypeError, ValueError):
                    confidence, risks = 0.0, {}
                if not (
                    receipt.get("phase") == "omission_reviewed"
                    and receipt.get("safe") is True
                    and receipt.get("fragment_sha256") == fragment["sha256"]
                    and receipt.get("classification") == classification
                    and judgment.get("policy_version") == OMISSION_VERSION
                    and judgment.get("source_sha256") == record["sha256"]
                    and judgment.get("choice") == "safe"
                    and confidence >= 0.95
                    and set(risks) == {"contract_risk", "state_risk", "context_risk"}
                    and all(0 <= value < 0.1 for value in risks.values())
                ):
                    unverified_low_attention += 1
    return {
        "policy_version": SEMANTIC_AUDIT_VERSION,
        "source_id": index.manifest["id"],
        "scope": "host_critical_and_typed_risk_preservation",
        "critical_events_checked": checked,
        "critical_events_missed": missed,
        "unclassified_fragments": unclassified,
        "suspicious_low_attention_fragments": suspicious_low_attention,
        "unverified_low_attention_fragments": unverified_low_attention,
        "passed": checked > 0 and missed == 0 and unclassified == 0
                  and suspicious_low_attention == 0
                  and unverified_low_attention == 0,
        "semantic_completeness_proven": False,
    }


def _valid_semantic_review(record: dict, fragment: dict, receipt: dict,
                           ledger: ReviewLedger) -> bool:
    original = receipt
    inherited = str(receipt.get("inherited_from") or "")
    if inherited:
        if inherited not in {item["id"] for item in record["fragments"]}:
            return False
        original = ledger.latest.get(inherited) or {}
        if original.get("phase") != "reviewed" or original.get("decision") != "retain":
            return False
    judgment = original.get("judgment") or {}
    try:
        confidence = float(judgment.get("confidence") or 0)
        probabilities = {key: float(value) for key, value in
                         (judgment.get("probabilities") or {}).items()}
    except (TypeError, ValueError):
        return False
    return (
        judgment.get("policy_version") == SEMANTIC_REVIEW_VERSION
        and judgment.get("source_sha256") == record["sha256"]
        and judgment.get("choice") == "unchanged"
        and confidence >= 0.9
        and all(0 <= value < 0.2 for value in probabilities.values())
        and set(probabilities)
        == {"cross_record", "unknown_side_effect", "signoff_or_review"}
    )


def evaluate_handoff_gate(
    index: PausedHandoffIndex,
    ledger: ReviewLedger,
    *,
    frozen_snapshot: dict[str, Any],
    current_snapshot: dict[str, Any],
    final_decision: str,
    citation_receipts: list[dict[str, Any]] | None = None,
    supporting_references: list[dict[str, Any]] | None = None,
    semantic_audit: dict[str, Any] | None = None,
    reconciled_records: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return a host-verifiable ``resume`` or a reasoned ``hold``.

    Missing state is unsafe, not an implicit success.  The final model decision
    is necessary but cannot override any of the mechanical or business gates.
    """
    reasons: list[str] = []
    try:
        coverage = index.verify(original=True)
    except (ValueError, OSError, KeyError):
        coverage = {"complete": False}
        reasons.append("source_changed_or_incomplete")
    source = index.manifest["source"]
    try:
        if Path(source["log_path"]).stat().st_size != source["end"]:
            reasons.append("new_dialogue_requires_incremental_generation")
    except OSError:
        if "source_changed_or_incomplete" not in reasons:
            reasons.append("source_unavailable")
    if ledger.index.manifest["id"] != index.manifest["id"]:
        reasons.append("review_source_mismatch")
    if unresolved_hard_holds(index, reconciled_records or {}):
        reasons.append("hard_reconciliation_incomplete")

    levels: dict[str, int] = {}
    supporting_fragments: set[str] = set()
    for record in index.manifest["records"]:
        for fragment in record["fragments"]:
            receipt = ledger.latest.get(fragment["id"])
            classification = ledger.completed_classification(fragment)
            if (classification is None
                    or classification.get("policy_version") != CLASSIFIER_VERSION
                    or classification.get("source_sha256") != fragment["sha256"]
                    or classification.get("start") != fragment["start"]
                    or classification.get("end") != fragment["end"]):
                reasons.append("classification_incomplete")
                continue
            level = classification.get("level")
            if level not in {"core", "supporting", "routine", "unrelated", "uncertain"}:
                reasons.append("classification_invalid")
                continue
            levels[level] = levels.get(level, 0) + 1
            if level == "supporting":
                supporting_fragments.add(fragment["id"])
            if level in {"core", "uncertain"}:
                if not receipt or receipt.get("phase") != "reviewed":
                    reasons.append("core_review_incomplete")
                elif receipt.get("decision") != "retain":
                    reasons.append("review_hold")
                elif fragment["id"] not in receipt.get("evidence_refs", []):
                    reasons.append("review_evidence_missing")
                elif not _valid_semantic_review(record, fragment, receipt, ledger):
                    reasons.append("semantic_review_unverified")
            if receipt and receipt.get("phase") in {"started", "review_started", "omission_started"}:
                reasons.append("review_outcome_unknown")

    for citation in citation_receipts or []:
        if not citation.get("verified") or citation.get("verdict") != "verified":
            reasons.append("citation_unverified")
        elif not any(record["id"] == citation.get("record_id")
                     and record["sha256"] == citation.get("source_sha256")
                     for record in index.manifest["records"]):
            reasons.append("citation_source_mismatch")
    expected_refs = {item["fragment_id"]: item for item in supporting_source_references(
        index, ledger.classifications,
    )}
    represented_fragments: set[str] = set()
    for reference in supporting_references or []:
        if not isinstance(reference, dict) or expected_refs.get(reference.get("fragment_id")) != reference:
            reasons.append("supporting_reference_invalid")
        else:
            represented_fragments.add(reference["fragment_id"])
    cited_records = {citation.get("record_id") for citation in citation_receipts or []
                     if citation.get("verified") and citation.get("verdict") == "verified"}
    represented_fragments.update(
        fragment["id"] for record in index.manifest["records"]
        if record["id"] in cited_records for fragment in record["fragments"]
    )
    if supporting_fragments - represented_fragments:
        reasons.append("supporting_evidence_unrepresented")
    # A caller-supplied audit is a display receipt, never the authority for a
    # resume.  Recompute from the source-bound ledger immediately before the
    # gate so a stale or fabricated ``passed`` cannot clear low-attention risk.
    current_audit = audit_semantic_risk(index, ledger)
    if semantic_audit is None or not semantic_audit.get("passed"):
        reasons.append("semantic_risk_not_cleared")
    elif semantic_audit.get("source_id") != index.manifest["id"]:
        reasons.append("semantic_audit_source_mismatch")
    elif (semantic_audit.get("policy_version") != SEMANTIC_AUDIT_VERSION
          or semantic_audit.get("critical_events_checked", 0) <= 0
          or semantic_audit.get("critical_events_missed") != 0):
        reasons.append("semantic_audit_incomplete")
    elif any(semantic_audit.get(key) != current_audit.get(key) for key in (
            "critical_events_checked", "critical_events_missed",
            "unclassified_fragments", "suspicious_low_attention_fragments",
            "unverified_low_attention_fragments", "passed")):
        reasons.append("semantic_audit_stale_or_forged")
    if not current_audit["passed"]:
        reasons.append("semantic_risk_not_cleared")

    for field in REQUIRED_SNAPSHOT_FIELDS:
        frozen = frozen_snapshot.get(field)
        current = current_snapshot.get(field)
        if not frozen or not current:
            reasons.append(f"{field}_unknown")
        elif frozen != current:
            reasons.append(f"{field}_changed")
    if frozen_snapshot.get("run_id") != source["run_id"]:
        reasons.append("run_source_mismatch")
    if frozen_snapshot.get("conversation_id") != source["conversation_id"]:
        reasons.append("conversation_source_mismatch")
    if frozen_snapshot.get("contract_fingerprint") != source["contract_fingerprint"]:
        reasons.append("contract_source_mismatch")
    if current_snapshot.get("unknown_side_effects") is not False:
        reasons.append("unknown_side_effects")
    if current_snapshot.get("independent_review_intact") is not True:
        reasons.append("independent_review_unverified")
    if current_snapshot.get("acceptance_intact") is not True:
        reasons.append("acceptance_unverified")
    if current_snapshot.get("user_signoff_required") is True and current_snapshot.get("user_signoff_granted") is not True:
        reasons.append("user_signoff_required")
    if final_decision != "resume":
        reasons.append("no_valid_resume_decision")

    reasons = list(dict.fromkeys(reasons))
    return {
        "decision": "hold" if reasons else "resume",
        "reasons": reasons,
        "source_id": index.manifest["id"],
        "coverage": coverage,
        "levels": levels,
        "classified": ledger.progress()["classified"],
        "reviewed": ledger.progress()["reviewed"],
    }


def build_final_handoff_view(index: PausedHandoffIndex, ledger: ReviewLedger,
                             *, citation_receipts: list[dict[str, Any]] | None = None,
                             supporting_references: list[dict[str, Any]] | None = None,
                             max_raw_bytes: int = 250_000) -> dict[str, Any]:
    """Assemble only reviewed core original and verified summary references.

    A too-large core cannot be silently truncated.  The caller must arrange a
    separate bounded semantic review before asking for the final decision.
    """
    core: list[dict[str, Any]] = []
    used = 0
    for record in index.manifest["records"]:
        critical = [fragment for fragment in record["fragments"]
                    if (ledger.completed_classification(fragment) or {}).get("level")
                    in {"core", "uncertain"}]
        if not critical:
            continue
        for fragment in critical:
            receipt = ledger.latest.get(fragment["id"])
            if not receipt or receipt.get("phase") != "reviewed" or receipt.get("decision") != "retain":
                return {"status": "review_incomplete", "fragment_id": fragment["id"]}
            if not _valid_semantic_review(record, fragment, receipt, ledger):
                return {"status": "semantic_review_unverified", "fragment_id": fragment["id"]}
        if record.get("semantic_input_safe") is not True:
            return {"status": "sensitive_core_requires_local_review",
                    "record_id": record["id"]}
        # The source remains byte-exact in the index.  The model-facing packet
        # excludes private thinking/signature blocks while keeping exact
        # visible fields and a byte-exact source retrieval reference.
        raw = index.read(record["id"])
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"status": "invalid_core_source", "record_id": record["id"]}
        content = value.get("content")
        if isinstance(content, list):
            content = [block for block in content if isinstance(block, dict)
                       and block.get("type") in {"text", "tool_use"}]
        visible = json.dumps({"type": value.get("type"), "content": content},
                             ensure_ascii=False, sort_keys=True)
        used += len(visible.encode("utf-8"))
        if used > max_raw_bytes:
            return {"status": "core_exceeds_final_context", "required_bytes": used,
                    "max_raw_bytes": max_raw_bytes, "record_id": record["id"]}
        core.append({"record_id": record["id"],
                     "start": record["start"], "end": record["end"],
                     "sha256": record["sha256"],
                     "fragment_refs": [item["id"] for item in critical],
                     "visible_json": visible})
    citations = [item for item in citation_receipts or []
                 if item.get("verified") and item.get("verdict") == "verified"]
    expected_refs = {item["fragment_id"]: item for item in supporting_source_references(
        index, ledger.classifications,
    )}
    references = [item for item in supporting_references or []
                  if isinstance(item, dict)
                  and expected_refs.get(item.get("fragment_id")) == item]
    return {"status": "ready", "source_id": index.manifest["id"],
            "core": core, "verified_citations": citations,
            "supporting_references": references,
            "exceptions": [fragment["id"] for record in index.manifest["records"]
                           for fragment in record["fragments"]
                           if ledger.completed_classification(fragment) is None]}
