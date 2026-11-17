"""Shared chat/graph decision runtime with durable de-duplication and budgets."""
from __future__ import annotations

import asyncio
import json
import time

from mewcode.decisions.spec import BUILTIN_SPECS, DecisionInput, DecisionSpec
from mewcode.judgments.runtime import JudgmentRuntime, digest
from mewcode.plugins.services import DECISION_SPEC_SLOT


class DecisionRuntime(JudgmentRuntime):
    """Backward-compatible option-selector adapter over the general judgment layer."""
    def specs(self):
        custom = self.context.slots.resolve(DECISION_SPEC_SLOT) if self.context is not None else {}
        return {**BUILTIN_SPECS, **(custom or {})}

    async def decide(self, request: DecisionInput, *, observed_evidence=None):
        scope, origin = self.scope, self.origin("decision")
        if not scope:
            return {"status": "main_model", "reason": "missing_conversation_scope"}
        spec = self.specs().get(request.spec)
        if not isinstance(spec, DecisionSpec):
            return {"status": "main_model", "reason": "unknown_decision_spec"}
        candidates = [c for c in request.candidates if c.eligible]
        evidence = observed_evidence or []
        if observed_evidence is None and request.evidence_refs:
            conversation = getattr(self.agent, "_current_conversation", None)
            evidence = [{"ref": r.tool_use_id, "output": r.content[:4000], "is_error": r.is_error}
                        for m in getattr(conversation, "history", []) for r in m.tool_results
                        if r.tool_use_id in request.evidence_refs]
            if not set(request.evidence_refs) <= {r["ref"] for r in evidence}:
                return {"status": "rethink", "reason": "missing_observed_evidence"}
        state = {"goal": request.goal, "facts": request.state, "evidence": evidence}
        questions = spec.questions(candidates)
        size = len(json.dumps([state, questions], ensure_ascii=False, allow_nan=False).encode())
        if size > 28000:
            return {"status": "rethink", "reason": "decision_state_too_large", "next_action": "Narrow evidence; never truncate constraints."}
        try:
            client, route = await self.client()
        except Exception as exc:  # noqa: BLE001 -- plugin route failures fall back without exposing secrets.
            return {"status": "main_model", "reason": "route_unavailable", "error_type": type(exc).__name__}
        if client is None:
            return {"status": "main_model", "reason": route["status"],
                    "next_action": "Reason locally; no auxiliary call was made. Do not ask user just because Jev is unavailable."}
        fingerprint = digest([spec.model_dump(), state, questions, route])
        reservation = self.store.reserve(scope, request.issue, fingerprint, size, spec.max_rounds)
        if not reservation.get("reserved"):
            return reservation
        round_number = reservation["round"]
        started = time.monotonic()
        result = {"status": "rethink" if round_number < spec.max_rounds else "escalate",
                  "round": round_number, "spec": spec.name, "spec_version": spec.version,
                  "fingerprint": fingerprint, "cached": False, "advisory_only": True}
        try:
            if not candidates:
                result["reason"] = "all_candidates_fail_hard_constraints"
            else:
                async with asyncio.timeout(spec.timeout_seconds):
                    response = await self._evaluate_reserved(client, state, questions, origin)
                answers = response["answers"]
                selection = answers["selection"]
                suitable = {c.id: answers["fit_" + c.id]["noul"] for c in candidates}
                choice = selection["choice"]
                result.update(model=response.get("model", client.model), suitability=suitable,
                              confidence=selection["confidence"], probabilities=selection["probabilities"])
                if (choice in suitable and suitable[choice] >= spec.min_suitability
                        and selection["confidence"] >= spec.min_confidence
                        and selection["probabilities"][choice] >= spec.min_probability):
                    result.update(status="selected", choice=choice, reason="supported_candidate")
                else:
                    result["reason"] = "all_rejected" if not any(v >= spec.min_suitability for v in suitable.values()) else "no_supported_selection"
        except asyncio.CancelledError:
            result["reason"] = "cancelled_outcome_unknown"
            self.store.finish(scope, request.issue, fingerprint, result)
            raise
        except Exception as exc:  # noqa: BLE001 -- isolate provider failures; cancellation is handled above.
            result.update(reason="provider_failure", error_type=type(exc).__name__)
        result["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
        result["next_action"] = {
            "selected": "Continue main-model planning; execution still needs host permissions and outcome verification.",
            "rethink": "Main model: gather missing evidence, materially revise candidates/state, then re-evaluate once with SAME issue. Do not ask user yet.",
            "escalate": "Two bounded attempts exhausted. Summarize rejected options and ask only for the concrete remaining blocker; never invent user facts or permission.",
        }[result["status"]]
        self.store.finish(scope, request.issue, fingerprint, result)
        return result
