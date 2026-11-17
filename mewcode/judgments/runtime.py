"""Shared bounded transport, billing scope and durable typed-batch judgments."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

from mewcode.decisions.store import DecisionStore
from mewcode.judgments.spec import JudgmentRequest, JudgmentSpec, validate_answers
from mewcode.plugins.services import JUDGMENT_SPEC_SLOT, USAGE
from mewcode.tools.evaluate_evidence import judgment_choice
from mewcode.usage.service import usage_trace_scope


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def reported_usage(responses, expected):
    """Unknown usage is incomplete, never invented from characters or answers."""
    counts = {"input_tokens": 0, "output_tokens": 0}
    complete = len(responses) == expected
    for response in responses:
        usage = response.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        for key in counts:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                counts[key] += value
            else:
                complete = False
    return {"usage_complete": complete, **{"reported_" + k: v for k, v in counts.items()}}


def batch_questions(request: JudgmentRequest, spec: JudgmentSpec):
    state_size = len(encoded(request.state))
    if state_size > spec.max_state_bytes:
        raise ValueError("state_budget_exceeded")
    batches, batch = [], {}
    for key, question in request.questions.items():
        value = question.model_dump(exclude_none=True)
        trial = {**batch, key: value}
        if batch and (len(trial) > spec.max_questions_per_batch or len(encoded([request.state, trial])) > spec.max_request_bytes):
            batches.append(batch)
            batch = {}
        batch[key] = value
        if len(encoded([request.state, batch])) > spec.max_request_bytes:
            raise ValueError("question_budget_exceeded")
    if batch:
        batches.append(batch)
    if len(batches) > spec.max_batches:
        raise ValueError("batch_budget_exceeded")
    return batches


class JudgmentRuntime:
    def __init__(self, agent, providers, *, store=None, client=None, context=None):
        from mewcode.runtime_paths import user_data_root
        self.agent, self.providers, self.context = agent, providers, context
        self.store = store or DecisionStore(user_data_root() / "decisions-v1.sqlite3")
        self._client, self._route_key = client, None
        self._injected = client is not None
        self._clients, self._client_lock = [], asyncio.Lock()
        self._request_slots = asyncio.Semaphore(2)
        self._closed = False
        usage = context.get(USAGE, None) if context is not None else None
        self._scope_root = str(usage.ledger.path.resolve()) if usage is not None else str(Path(agent.work_dir).resolve())

    @property
    def scope(self):
        session_id = getattr(self.agent, "session_id", "")
        return digest([self._scope_root, session_id]) if session_id else ""

    def origin(self, suffix):
        return {"session_id": getattr(self.agent, "session_id", ""),
                "agent_id": getattr(self.agent, "agent_id", "") + ":" + suffix,
                "trace_id": getattr(self.agent, "trace_id", "")}

    def readiness(self):
        return judgment_choice(self.providers, self.agent.work_dir, config_key="decision_model")[1]

    def judgment_specs(self):
        return (self.context.slots.resolve(JUDGMENT_SPEC_SLOT) or {}) if self.context is not None else {}

    async def aclose(self):
        self._closed = True
        # Owners stop their observers before disposing this shared transport.
        for client in self._clients:
            close = getattr(client, "aclose", None)
            if close:
                await close()
        self._clients.clear()
        if not self._injected:
            self._client = None

    async def client(self):
        if self._closed:
            return None, {"status": "runtime_closed"}
        if self._injected:
            return self._client, {"model": self._client.model, "provider_key": "injected"}
        async with self._client_lock:
            if self._closed:
                return None, {"status": "runtime_closed"}
            ref, readiness = judgment_choice(self.providers, self.agent.work_dir, config_key="decision_model")
            if ref is None:
                return None, readiness
            key = (ref.provider_key, ref.model_id, getattr(self.providers, "desktop_revision", lambda: 0)())
            if self._client is None or key != self._route_key:
                # Never close a previous route's connection underneath an in-flight batch.
                if len(self._clients) >= 16:
                    return None, {"status": "route_rotation_budget_exhausted"}
                recorder = getattr(self.agent.client, "get_usage_recorder", lambda: None)()
                self._client = self.providers.resolve(ref, usage_recorder=recorder).client
                self._client.reuse_connections = True
                self._clients.append(self._client)
                self._route_key = key
            return self._client, readiness

    async def _evaluate_reserved(self, client, state, questions, origin, guard=None, on_dispatch=None):
        """Transport shared by adapters that already hold a durable reservation."""
        async with self._request_slots:
            if self._closed or guard is not None and not guard():
                raise ValueError("judgment_scope_or_permission_changed")
            if on_dispatch is not None:
                on_dispatch()
            with usage_trace_scope(**origin):
                result = await client.evaluate(state, questions)
            return {**result, "answers": validate_answers(questions, result.get("answers"))}

    async def _finish(self, scope, issue, fingerprint, result):
        try:
            await asyncio.to_thread(self.store.finish, scope, issue, fingerprint, result)
        except Exception as exc:  # noqa: BLE001 -- preserve the unknown reservation, never retry automatically.
            return {**result, "status": "fallback", "answers": {}, "reason": "receipt_persistence_failed",
                    "error_type": type(exc).__name__}
        return result

    async def judge(self, request: JudgmentRequest, *, expected_scope=None, guard=None):
        spec = self.judgment_specs().get(request.spec)
        if not isinstance(spec, JudgmentSpec):
            return {"status": "fallback", "reason": "unknown_judgment_spec", "answers": {}, "applied": False}
        base = {"spec": spec.name, "spec_version": spec.version, "mode": spec.mode,
                "failure_policy": spec.failure_policy, "advisory_only": True, "applied": False}
        scope, origin = self.scope, self.origin(spec.name)
        if not scope or expected_scope is not None and scope != expected_scope:
            return {**base, "status": "fallback", "reason": "missing_or_changed_scope", "answers": {}}
        try:
            # Detach before the first await; callers cannot change evidence after budgeting.
            request = JudgmentRequest.model_validate_json(encoded(request.model_dump()))
            batches = batch_questions(request, spec)
            size = sum(len(encoded([request.state, batch])) for batch in batches)
            client, route = await self.client()
        except Exception as exc:  # noqa: BLE001 -- boundary errors cannot trigger destructive fallback.
            return {**base, "status": "fallback", "reason": "invalid_or_oversized_request", "error_type": type(exc).__name__, "answers": {}}
        if client is None:
            return {**base, "status": "fallback", "reason": route["status"], "answers": {}}
        fingerprint = digest([spec.model_dump(), request.model_dump(), route])
        reservation_scope = digest([scope, "typed-judgments", spec.name])
        try:
            reservation = await asyncio.to_thread(self.store.reserve, reservation_scope, request.issue, fingerprint,
                size, spec.max_rounds, max_reservations=spec.max_reservations,
                max_reserved_bytes=spec.max_reserved_bytes, count_repeats=False)
        except Exception as exc:  # noqa: BLE001 -- without a durable reservation no paid call is safe.
            return {**base, "status": "fallback", "reason": "reservation_unavailable", "answers": {},
                    "error_type": type(exc).__name__}
        if not reservation.get("reserved"):
            if reservation.get("status") == "ok":
                return reservation
            # Storage is shared with the legacy selector, not its ask-user policy.
            return {**base, "status": "fallback", "answers": {},
                    "reason": reservation.get("reason", "reserved_or_unknown"),
                    "cached": reservation.get("cached", False)}
        started, responses = time.monotonic(), []
        started_requests = 0
        slots = asyncio.Semaphore(spec.concurrency)

        async def one(batch):
            async with slots:
                result = await self._evaluate_reserved(client, request.state, batch, origin, guard, dispatched)
                responses.append(result)
                return result["answers"]

        def dispatched():
            nonlocal started_requests
            started_requests += 1

        result = {**base, "fingerprint": fingerprint, "cached": False, "round": reservation["round"],
                  "answers": {}, "status": "fallback", "reason": "provider_failure"}
        try:
            async with asyncio.timeout(spec.timeout_seconds):
                groups = await asyncio.gather(*(one(batch) for batch in batches), return_exceptions=True)
            errors = [group for group in groups if isinstance(group, BaseException)]
            if errors:
                result["error_type"] = type(errors[0]).__name__
            else:
                result.update(status="ok", reason="validated_answers", answers={k: v for group in groups for k, v in group.items()})
        except asyncio.CancelledError:
            result["reason"] = "cancelled_outcome_unknown"
            await self._finish(reservation_scope, request.issue, fingerprint, result)
            raise
        except Exception as exc:  # noqa: BLE001 -- partial/unknown batches are never interpreted as negative.
            result["error_type"] = type(exc).__name__
        result.update(latency_ms=round((time.monotonic() - started) * 1000, 2),
                      request_count=started_requests, reserved_input_bytes=size,
                      **reported_usage(responses, len(batches)))
        return await self._finish(reservation_scope, request.issue, fingerprint, result)
