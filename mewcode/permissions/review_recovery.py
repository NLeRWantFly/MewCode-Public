"""Return review denials to the executor before asking a person for authority."""
from __future__ import annotations

import copy
from collections import Counter, deque
from dataclasses import dataclass, field
import hashlib
import time
from pathlib import Path
import uuid

from mewcode.permissions.action_review import ActionReviewBinding
from mewcode.permissions.decision_envelope import canonical_digest
from mewcode.permissions.effects import (EffectSignature, effect_signature,
    legacy_workflow_fallback, workflow_design_contract)


def obsolete_text_capacity_stop(checkpoint, conversation):
    """Recognize only removed application byte guards with a real failed call.

    This permits rebuilding a review after loading fixed code, never tool
    execution or retirement of a policy denial. Other stops remain blocked.
    """
    reason = checkpoint.get('blocked_reason') if isinstance(checkpoint, dict) else None
    if not isinstance(reason, str) or not reason.startswith((
        'AUTO_REVIEW_UNAVAILABLE: 完整审查请求超过文本容量；授权和原操作未截断。\n',
        'AUTO_REVIEW_UNAVAILABLE: 看图取证请求中的文本超出容量；授权和原操作未截断。\n',
    )):
        return False
    call_ids = {call.tool_use_id for message in conversation.history for call in message.tool_uses}
    return any(result.tool_use_id in call_ids and result.is_error and result.content == reason
               for message in conversation.history for result in message.tool_results)


def legacy_review_outage_call(checkpoint, conversation):
    """Scope a host-stamped old service hold to its actual failed operation."""
    reason = checkpoint.get("blocked_reason") if isinstance(checkpoint, dict) else None
    if not isinstance(reason, str) or not reason.startswith(("AUTO_REVIEW_UNAVAILABLE:", "REVIEW_EVIDENCE_UNAVAILABLE:")):
        return None
    calls = {call.tool_use_id: call for message in conversation.history for call in message.tool_uses}
    candidate = None
    for message in reversed(conversation.history):
        for result in reversed(message.tool_results):
            if result.is_error and result.content == reason and result.tool_use_id in calls:
                candidate = calls[result.tool_use_id]
            elif candidate is not None:
                # Within the latest contiguous outage, later errors were copies
                # of the old hold. An earlier recovered outage is a different
                # episode, even if its diagnostic text is identical.
                return candidate
    return candidate


@dataclass
class DeniedAction:
    reference: str
    tool_name: str
    arguments: dict
    reason: str
    alternative: str
    kind: str
    binding: ActionReviewBinding
    user_response: str = ""
    effect: EffectSignature | None = None
    abandoned: bool = False
    abandonment_reason: str = ""
    evidence_digest: str = ""
    attempts: int = 0
    arguments_digest: str = ""
    review_route: dict = field(default_factory=dict)
    arguments_verified: bool = True
    effect_reassessments: list[dict] = field(default_factory=list)

    def current(self, agent):
        return self.binding.is_current(agent, self.tool_name, self.arguments)


@dataclass
class ReviewRecovery:
    requests: dict[str, DeniedAction] = field(default_factory=dict)
    recent: deque = field(default_factory=lambda: deque(maxlen=50))
    consecutive: int = 0
    stalls: dict[str, tuple[str, int]] = field(default_factory=dict)
    total_denials: int = 0
    strict: bool = False  # audit context, never a session-wide permission gate
    ledger: list[dict] = field(default_factory=list)
    ledger_totals: Counter = field(default_factory=Counter)
    ledger_omitted: int = 0
    untrusted_read_seen: bool = False
    history_digest: str = ""

    @staticmethod
    def user_context(conversation):
        return canonical_digest([m.content for m in conversation.history if m.role == "user"
                                 and not m.tool_results and not getattr(m, "transient", False)
                                 and not m.content.startswith(("<system-reminder>", "[摘要]", "本次会话延续自之前的对话，因上下文空间不足"))])

    def checkpoint(self, conversation):
        # Host metadata stores constraints, never reusable authority. Arguments
        # are recovered from the actual call history instead of duplicated here.
        return {"schema": "mewcode.auto-recovery.v1", "user_context": self.user_context(conversation),
                "requests": [{"reference": i.reference, "tool_name": i.tool_name,
                    "arguments_sha256": i.arguments_digest or canonical_digest(i.arguments), "reason": i.reason[:2000],
                    "pending_arguments": copy.deepcopy(i.arguments) if not i.user_response and not i.abandoned
                        and i.kind in {"inconclusive", "unavailable", "missing_authorization"} else None,
                    "alternative": i.alternative[:2000], "kind": i.kind,
                    "effect": i.effect.as_dict(), "abandoned": i.abandoned,
                    "abandonment_reason": i.abandonment_reason, "user_response": i.user_response,
                    "attempts": i.attempts, "evidence_digest": i.evidence_digest,
                    "review_route": copy.deepcopy(i.review_route),
                    "effect_reassessments": copy.deepcopy(i.effect_reassessments)}
                    for i in self.requests.values() if i.effect is not None],
                "strict": self.strict, "stalls": {key: list(value) for key, value in self.stalls.items()}, "total_denials": self.total_denials,
                "recent": list(self.recent), "consecutive": self.consecutive,
                "ledger": copy.deepcopy(self.ledger), "ledger_totals": dict(self.ledger_totals),
                "ledger_omitted": self.ledger_omitted, "history_digest": self.history_digest,
                "untrusted_read_seen": self.untrusted_read_seen}

    def restore(self, agent, conversation, checkpoint):
        if checkpoint.get("schema") != "mewcode.auto-recovery.v1":
            raise ValueError("Unknown recovery checkpoint")
        calls = {(c.tool_name, canonical_digest(c.arguments)): c for m in conversation.history for c in m.tool_uses}
        same_user = checkpoint["user_context"] == self.user_context(conversation)
        for row in checkpoint["requests"]:
            effect = EffectSignature(**row["effect"])
            call = calls.get((row["tool_name"], row["arguments_sha256"]))
            pending_arguments = row.get("pending_arguments")
            if pending_arguments is not None and (not isinstance(pending_arguments, dict)
                    or canonical_digest(pending_arguments) != row["arguments_sha256"]):
                raise ValueError("Pending arguments do not match checkpoint")
            response = row["user_response"]
            if response == "approved_once":
                response = "expired"
            if response == "rejected" and not same_user:
                response = "prior_rejection"
            if call is None:
                # A compacted call still constrains its effect, but cannot be
                # presented as an exact authorization request.
                if pending_arguments is None and not row["abandoned"] and response == "" and row["kind"] in {"inconclusive", "unavailable", "missing_authorization"}:
                    raise ValueError("Pending operation missing from checkpoint history")
                if pending_arguments is None:
                    response = response or "original_call_unavailable"
            arguments = copy.deepcopy(call.arguments if call else pending_arguments or {})
            ref = row["reference"]
            self.requests[ref] = DeniedAction(ref, row["tool_name"], arguments,
                row["reason"], row["alternative"], row["kind"],
                ActionReviewBinding.capture(agent, row["tool_name"], arguments),
                user_response=response, effect=effect, abandoned=row["abandoned"],
                abandonment_reason=row["abandonment_reason"], attempts=int(row["attempts"]),
                evidence_digest=row["evidence_digest"], arguments_digest=row["arguments_sha256"],
                review_route=copy.deepcopy(row.get("review_route", {})),
                arguments_verified=call is not None,
                effect_reassessments=copy.deepcopy(row.get("effect_reassessments", [])))
        self.strict = bool(checkpoint["strict"])
        self.stalls = {k: (v[0], int(v[1])) for k, v in checkpoint["stalls"].items()}
        self.total_denials = int(checkpoint["total_denials"])
        self.recent = deque(checkpoint["recent"], maxlen=50)
        self.consecutive = int(checkpoint["consecutive"])
        self.ledger = copy.deepcopy(checkpoint["ledger"])
        self.ledger_totals = Counter(checkpoint["ledger_totals"])
        self.ledger_omitted = int(checkpoint["ledger_omitted"])
        self.history_digest = checkpoint["history_digest"]
        self.untrusted_read_seen = bool(checkpoint["untrusted_read_seen"])

    @staticmethod
    def signature(agent, tool_name, arguments):
        return effect_signature(tool_name, arguments, work_dir=agent.work_dir,
                                tool=agent.registry.get(tool_name))

    def related(self, agent, tool_name, arguments):
        effect = self.signature(agent, tool_name, arguments)
        items = []
        for item in self.requests.values():
            if item.effect is None:
                continue
            prior_effect = self._related_effect(agent, item)
            same = ((item.tool_name, item.arguments_digest or canonical_digest(item.arguments))
                    == (tool_name, canonical_digest(arguments)))
            if item.kind == "unavailable":
                # A transport outage is not evidence that every opaque sibling
                # effect is unsafe. Only its exact call or known shared target
                # retains this pending review requirement.
                known = (not prior_effect.uncertain and not effect.uncertain
                         and prior_effect.kind != "authority_change" and effect.kind != "authority_change"
                         and prior_effect.overlaps(effect))
                if same or known:
                    items.append(item)
            elif same or prior_effect.overlaps(effect):
                items.append(item)
        return items

    def _related_effect(self, agent, item):
        """Reclassify an exact legacy fallback, never its denial or authority.

        Restore requires the real historical call and digest; absent/compacted
        calls, changed arguments, plugins and invalid scopes remain opaque.
        Exact retries still enter review independently of target overlap.
        """
        original = item.effect
        digest = canonical_digest(item.arguments)
        if (not item.arguments_verified or original is None
                or (item.arguments_digest and digest != item.arguments_digest)
                or original != legacy_workflow_fallback(item.tool_name, item.arguments, work_dir=agent.work_dir)):
            return original
        contract = workflow_design_contract(item.tool_name, item.arguments, work_dir=agent.work_dir,
            tool=agent.registry.get(item.tool_name))
        if (contract is None or not getattr(agent, "session_id", "")
                or contract["conversation_id"] != agent.session_id):
            return original
        reassessed = EffectSignature(**contract["effect"])
        record = {"schema": "mewcode.effect-reassessment.v1", "original_effect": original.as_dict(),
            "current_effect": reassessed.as_dict(), "arguments_sha256": digest,
            "contract": contract,
            "meaning": "Recomputed relevance only; original denial and user response remain in force."}
        if record not in item.effect_reassessments:
            item.effect_reassessments.append(record)
        return reassessed

    def needs_review(self, agent, tool_name, arguments):
        effect = self.signature(agent, tool_name, arguments)
        return effect.kind != "read" and bool(self.related(agent, tool_name, arguments))

    def allow(self, agent=None, tool_name="", arguments=None):
        self.recent.append(False)
        self.consecutive = 0
        if agent is not None:
            for item in self.related(agent, tool_name, arguments or {}):
                if item.effect is not None:
                    self.stalls.pop(item.effect.key, None)
                if item.kind in {"inconclusive", "unavailable"} and (item.tool_name, item.arguments) == (tool_name, arguments):
                    item.user_response = "review_allowed"

    def _progress_key(self, agent, effect, tool_name, arguments):
        # A revised proposal deserves review even when it touches the same
        # target. Its arguments distinguish attempts, not evidence or authority;
        # related denials still reach the reviewer before anything executes.
        targets = []
        for target in effect.targets:
            if not target.startswith("path:"):
                continue
            try:
                stat = Path(target[5:]).stat()
                targets.append((target, stat.st_size, stat.st_mtime_ns))
            except OSError:
                targets.append((target, "unavailable"))
        evidence = getattr(agent, "_safety_progress_digest", None) or getattr(agent, "_safety_evidence_digest", None)
        from mewcode.tools.ask_user import recorded_user_answers
        conversation = getattr(agent, '_current_conversation', None)
        return canonical_digest([tool_name, arguments, targets, evidence() if callable(evidence) else "",
            self.user_context(conversation) if conversation is not None else '',
            recorded_user_answers(conversation)])

    def _record_stall(self, agent, effect, call):
        progress = self._progress_key(agent, effect, call.tool_name, call.arguments)
        old_progress, count = self.stalls.get(effect.key, ("", 0))
        self.stalls[effect.key] = (progress, count + 1 if old_progress == progress else 1)

    def stalled_action(self, agent, tool_name, arguments):
        """Bound unchanged retries without locking a target against repairs."""
        effect = self.signature(agent, tool_name, arguments)
        progress, count = self.stalls.get(effect.key, ("", 0))
        latest_navigation = next((item for item in reversed(list(self.requests.values()))
            if tool_name == "browser_navigate" and item.tool_name == tool_name
            and (item.arguments_digest or canonical_digest(item.arguments)) == canonical_digest(arguments)), None)
        # A conclusive navigation denial needs new evidence, not another call ID.
        # Service outages and inconclusive reviews retain their bounded retry path.
        limit = 1 if latest_navigation is not None and latest_navigation.kind in {
            "semantic_risk", "policy_violation"} else 3
        if count < limit or progress != self._progress_key(agent, effect, tool_name, arguments):
            return None
        if any(item.user_response == "approved_once" and not item.abandoned
               and (item.tool_name, item.arguments) == (tool_name, arguments) and item.current(agent)
               for item in self.requests.values()):
            return None
        for item in reversed(list(self.requests.values())):
            if item.effect is not None and item.effect.key == effect.key:
                return item
        return None

    def cooling_review(self, tool_name, arguments):
        from mewcode.permissions.review_retry import retry_remaining
        for item in self.pending_reviews():
            if item.kind == "unavailable" and (item.tool_name, item.arguments) == (tool_name, arguments):
                remaining = retry_remaining(item.review_route, time.time())
                if remaining is not None and remaining > 0:
                    return item, remaining
        return None

    def deny(self, agent, call, review):
        self.recent.append(True)
        self.consecutive += 1
        self.total_denials += 1
        effect = self.signature(agent, call.tool_name, call.arguments)
        route = getattr(review, "review_route", {})
        if effect.kind == "authority_change" or route.get("strict_review") is True:
            self.strict = True
        # Possible overlap informs review, but is not proof of repetition.
        # In particular, unknown tools/commands overlap everything. Counting
        # against the first old denial merged distinct pages into one stall.
        self._record_stall(agent, effect, call)
        binding = getattr(review, "execution_binding", None) or ActionReviewBinding.capture(
            agent, call.tool_name, call.arguments)
        for item in self.requests.values():
            if ((item.tool_name, item.arguments) == (call.tool_name, call.arguments)
                    and (item.binding != binding or item.kind != review.denial_kind)):
                item.user_response = "superseded"
        existing = next((item for item in self.requests.values()
                         if item.tool_name == call.tool_name and item.arguments == call.arguments
                         and item.binding == binding and item.kind == review.denial_kind
                         and not item.abandoned and not item.user_response), None)
        if existing is None:
            existing = DeniedAction(uuid.uuid4().hex, call.tool_name, copy.deepcopy(call.arguments),
                                    review.reason, review.alternative, review.denial_kind, binding,
                                    effect=effect)
            self.requests[existing.reference] = existing
        else:
            existing.reason, existing.alternative = review.reason, review.alternative
        return existing

    def context(self, agent=None, tool_name="", arguments=None):
        items = (self.related(agent, tool_name, arguments or {}) if agent is not None
                 else list(self.requests.values()))
        return [{"tool_name": item.tool_name, "arguments": item.arguments,
                 "reason": item.reason, "blocker_kind": item.kind,
                 "alternative": item.alternative, "user_response": item.user_response,
                 "effect": item.effect.as_dict() if item.effect else None,
                 "effect_reassessments": copy.deepcopy(item.effect_reassessments),
                 "abandoned": item.abandoned, "abandonment_reason": item.abandonment_reason}
                for item in items[-12:]]

    def abandon(self, references, reason):
        items = [self.requests.get(ref) for ref in references]
        if not references or any(item is None for item in items) or not reason.strip():
            return False
        if any(item.user_response in {"consumed", "review_allowed", "execution_started"} or any(
                row["tool_name"] == item.tool_name
                and row["arguments_sha256"] == canonical_digest(item.arguments)
                and row["status"] != "not_started" for row in self.ledger) for item in items):
            return False
        for item in items:
            item.abandoned = True
            item.abandonment_reason = reason.strip()[:2000]
            if item.user_response == "approved_once":
                item.user_response = "abandoned"
        # Keep tombstones, cumulative effects and stall counters. Abandoning
        # an operation is neither permission nor a clean safety history.
        return True

    def pending_authorizations(self):
        return [item for item in self.requests.values() if item.kind == "missing_authorization"
                and not item.user_response and not item.abandoned]

    def pending_reviews(self):
        return [item for item in self.requests.values() if item.kind in {"inconclusive", "unavailable"}
                and not item.user_response and not item.abandoned]

    def unavailable(self, agent, call, review):
        # Service failure is not a policy denial or permission to execute. Keep
        # the exact operation pending, with its transport diagnostics, while
        # other actions continue through their own permission checks.
        for pending in self.pending_reviews():
            if pending.kind == "inconclusive" and (pending.tool_name, pending.arguments) == (call.tool_name, call.arguments):
                pending.user_response = "superseded"
        item = next((item for item in self.pending_reviews() if item.kind == "unavailable"
                     and (item.tool_name, item.arguments) == (call.tool_name, call.arguments)), None)
        if item is None:
            item = DeniedAction(uuid.uuid4().hex, call.tool_name, copy.deepcopy(call.arguments),
                review.reason, review.alternative, "unavailable",
                ActionReviewBinding.capture(agent, call.tool_name, call.arguments),
                effect=self.signature(agent, call.tool_name, call.arguments))
            self.requests[item.reference] = item
        else:
            item.reason, item.alternative = review.reason, review.alternative
            item.binding = ActionReviewBinding.capture(agent, call.tool_name, call.arguments)
        item.review_route = copy.deepcopy(getattr(review, "review_route", {}))
        item.attempts += 1
        self._record_stall(agent, item.effect, call)
        return item

    def inconclusive(self, agent, call, review):
        for pending in self.pending_reviews():
            if pending.kind == "unavailable" and (pending.tool_name, pending.arguments) == (call.tool_name, call.arguments):
                pending.user_response = "superseded"
        item = next((item for item in self.pending_reviews() if item.kind == "inconclusive"
                     and (item.tool_name, item.arguments) == (call.tool_name, call.arguments)), None)
        if item is None:
            item = DeniedAction(uuid.uuid4().hex, call.tool_name, copy.deepcopy(call.arguments),
                review.reason, review.alternative, "inconclusive",
                ActionReviewBinding.capture(agent, call.tool_name, call.arguments),
                effect=self.signature(agent, call.tool_name, call.arguments))
            self.requests[item.reference] = item
        item.reason, item.alternative = review.reason, review.alternative
        item.review_route = copy.deepcopy(getattr(review, "review_route", {}))
        if not getattr(review, "cache_hit", False):
            item.attempts += 1
        item.evidence_digest = agent._safety_evidence_digest()
        return item

    def begin_effect(self, agent, call):
        effect = self.signature(agent, call.tool_name, call.arguments)
        for item in self.requests.values():
            if ((item.tool_name, item.arguments) == (call.tool_name, call.arguments)
                    and item.user_response not in {"consumed", "review_allowed"}):
                item.user_response = "execution_started"
        row = {"step": sum(self.ledger_totals.values()) + 1, "tool_name": call.tool_name,
               "effect": effect.as_dict(), "arguments_sha256": canonical_digest(call.arguments),
               "status": "execution_started_effect_unknown"}
        self.ledger_totals[effect.kind] += 1
        self.ledger.append(row)
        if call.tool_name in {"ReadFile", "Browser", "browser", "WebFetch", "web_fetch", "WebSearch"}:
            self.untrusted_read_seen = True
        if len(self.ledger) > 64:
            self.history_digest = canonical_digest([self.history_digest, self.ledger.pop(0)])
            self.ledger_omitted += 1
        return row

    @staticmethod
    def finish_effect(row, result):
        observation = getattr(result, "host_observation", None)
        receipt = observation.get("command_execution", {}) if isinstance(observation, dict) else {}
        if receipt.get("execution_started") is False or receipt.get("command_started") is False:
            row["status"] = "not_started"
        else:
            row["status"] = "tool_error_effect_unknown" if result.is_error else "tool_reported_completed"
        row["result_sha256"] = hashlib.sha256(result.output.encode("utf-8")).hexdigest()

    def effect_context(self):
        return {"schema": "mewcode.cumulative-effects.v1", "rows": copy.deepcopy(self.ledger),
                "counts": dict(self.ledger_totals), "omitted_rows": self.ledger_omitted,
                "strict_review": self.strict,
                "history_digest": self.history_digest, "untrusted_read_seen": self.untrusted_read_seen,
                "meaning": "Host-observed dispatch/results; requested effects are not independent proof of every side effect. No row grants authority."}

    def review_effect_context(self, effect):
        context = self.effect_context()
        related = [row for row in self.ledger[:-8] if EffectSignature(**row["effect"]).overlaps(effect)][-8:]
        context["rows"] = copy.deepcopy(related + self.ledger[-8:])
        context["omitted_rows"] += len(self.ledger) - len(context["rows"])
        context["full_context_sha256"] = canonical_digest(self.effect_context())
        return context

    def injection_signals(self, effect):
        signals = []
        if effect.kind == "authority_change":
            signals.append("permission_authority_mutation")
        if effect.kind in {"net_egress", "exec_unbounded"} and self.untrusted_read_seen:
            signals.append("untrusted_content_read_before_external_or_opaque_effect")
        return signals

    def consume_consent(self, agent, tool_name, arguments):
        for item in self.requests.values():
            if not item.abandoned and item.user_response == "approved_once" and (item.tool_name, item.arguments) == (tool_name, arguments):
                item.user_response = "consumed"
                if item.current(agent):
                    return {"review_call_ref": item.reference, "decision": "approved_once",
                            "action_binding": item.binding.state_digest,
                            "tool_name": item.tool_name,
                            "arguments_sha256": canonical_digest(item.arguments),
                            "consent_kind": "exact_operation_effects_once",
                            "scope": "User approved the displayed exact operation and its described effects for one exact retry, "
                                     "including the missing authority described below. Execution still requires independent "
                                     "safety review and all host restrictions; no additional targets or effects are authorized.",
                            "resolved_gap": item.reason}
        return None
