from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from mewcode.cordis import Context
from mewcode.judgments.runtime import (
    JudgmentRuntime,
    batch_questions,
    encoded,
    reported_usage,
)
from mewcode.judgments.spec import (
    JudgmentRequest,
    JudgmentSpec,
    Question,
    validate_answers,
)
from mewcode.plugins.services import JUDGMENT_SPEC_SLOT
from tests.test_decision_runtime import harness as decision_harness


def response(body):
    answers = {}
    for key, q in body["questions"].items():
        if q["type"] == "noul":
            answers[key] = {"type": "noul", "noul": .04}
        elif q["type"] == "choice":
            first = next(iter(q["criteria"]))
            answers[key] = {"type": "choice", "choice": first, "confidence": 1,
                            "probabilities": {k: int(k == first) for k in q["criteria"]}}
        else:
            answers[key] = {"type": "score", "score": .8, "confidence": .8,
                            "legend": {"0": "bad", "1": "good"}, "probabilities": {"0": .2, "1": .8}}
    return httpx.Response(200, json={"model": "jev-test", "answers": answers,
                                   "usage": {"input_tokens": 100, "output_tokens": 4}})


def harness(tmp_path, *, spec=None, handler=response):
    legacy, calls, usage = decision_harness(tmp_path, handler=handler)
    ctx = Context()
    ctx.slots.declare(JUDGMENT_SPEC_SLOT, kind="keyed")
    spec = spec or JudgmentSpec(name="sample")
    ctx.slots.register(JUDGMENT_SPEC_SLOT, spec, item_key=spec.name)
    runtime = JudgmentRuntime(legacy.agent, None, store=legacy.store, client=legacy._client, context=ctx)
    return runtime, calls, usage


def request(**kw):
    return JudgmentRequest(**{"spec": "sample", "issue": "issue", "state": {"goal": "check facts"},
                              "questions": {"one": Question(type="noul", instructions="Is this relevant?")}, **kw})


@pytest.mark.asyncio
async def test_mixed_independent_questions_batched_with_real_usage(tmp_path):
    runtime, calls, usage = harness(tmp_path, spec=JudgmentSpec(name="sample", max_questions_per_batch=2))
    req = request(questions={
        "route": Question(type="choice", instructions="Choose", criteria={"a": "A", "b": "B"}),
        "risk": Question(type="score", instructions="Rate", criteria=["bad", "good"]),
        "call": Question(type="noul", instructions="Keep call?"),
        "result": Question(type="noul", instructions="Keep result?"),
    })
    answer = await runtime.judge(req)
    assert answer["status"] == "ok" and set(answer["answers"]) == set(req.questions)
    # Independent negative answers are valid, no forced winner or ask-user loop.
    assert answer["answers"]["call"]["noul"] == answer["answers"]["result"]["noul"] == .04
    assert len(calls) == 2 and all(c["state"] == req.state for c in calls)
    assert answer["reserved_input_bytes"] == sum(len(encoded([req.state, c["questions"]])) for c in calls)
    assert answer["request_count"] == 2 and answer["reported_input_tokens"] == 200
    assert answer["usage_complete"] and not answer["applied"]
    assert (await runtime.judge(req))["cached"] and len(calls) == 2
    resumed = JudgmentRuntime(runtime.agent, None, store=runtime.store, client=runtime._client, context=runtime.context)
    assert (await resumed.judge(req))["cached"]
    assert usage.ledger.conversation_summary("chat-1")["tokens"]["total_tokens"] == 208
    assert usage.ledger.conversation_summary("chat-2")["request_count"] == 0


@pytest.mark.parametrize("values", [
    {"type": "choice", "instructions": "Pick", "criteria": {"one": "only"}},
    {"type": "noul", "instructions": "True?", "criteria": {"x": "y"}},
    {"type": "score", "instructions": "Rate", "criteria": ["one"]},
])
def test_question_schema_rejects_ambiguous_heads(values):
    with pytest.raises(ValidationError):
        Question(**values)


@pytest.mark.parametrize("value", [None, True, float("nan"), float("inf"), -1, 2, "0"])
def test_adapter_validation_never_defaults_unknown_to_false(value):
    with pytest.raises(ValueError):
        validate_answers({"q": {"type": "noul"}}, {"q": {"type": "noul", "noul": value}})


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"state": {"large": "x" * 20000}},
    {"questions": {str(i): Question(type="noul", instructions="Keep?") for i in range(65)}},
    {"spec": "missing"},
])
async def test_invalid_budget_or_missing_spec_sends_nothing(tmp_path, change):
    runtime, calls, _ = harness(tmp_path)
    result = await runtime.judge(request(**change))
    assert result["status"] == "fallback" and result["answers"] == {} and not calls


def test_bytes_include_repeated_state_and_no_truncation():
    spec = JudgmentSpec(name="sample", max_state_bytes=500, max_request_bytes=1100)
    req = request(state={"goal": "中" * 100}, questions={
        str(i): Question(type="noul", instructions="x" * 500) for i in range(3)})
    batches = batch_questions(req, spec)
    assert len(batches) == 3 and req.state["goal"] == "中" * 100
    assert all(len(encoded([req.state, batch])) <= spec.max_request_bytes for batch in batches)


@pytest.mark.asyncio
async def test_all_or_preserve_even_when_one_batch_passes_and_one_fails(tmp_path):
    def reply(body):
        if "bad" in body["questions"]:
            return httpx.Response(200, json={"answers": {}, "usage": {"input_tokens": 22, "output_tokens": 1}})
        return response(body)
    runtime, calls, usage = harness(tmp_path, handler=reply,
        spec=JudgmentSpec(name="sample", mode="shadow", failure_policy="preserve", max_questions_per_batch=1))
    req = request(questions={key: Question(type="noul", instructions="Keep?") for key in ("one", "bad")})
    result = await runtime.judge(req)
    assert result["status"] == "fallback" and result["answers"] == {} and not result["usage_complete"]
    assert result["failure_policy"] == "preserve" and "next_action" not in result
    for _ in range(4):
        cached = await runtime.judge(req)
        assert cached["cached"] and cached["status"] == "fallback" and "next_action" not in cached
    assert len(calls) == 2
    # Even a schema-invalid provider verdict is a billed receipt, not zero cost.
    assert usage.ledger.conversation_summary("chat-1")["tokens"]["total_tokens"] == 127


@pytest.mark.asyncio
async def test_runtime_concurrency_shared_across_consumers_and_mutation_detached(tmp_path):
    runtime, _, _ = harness(tmp_path, spec=JudgmentSpec(name="sample", max_questions_per_batch=1))
    active, peak, states = 0, 0, []
    entered, release = asyncio.Event(), asyncio.Event()
    async def evaluate(state, questions):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        entered.set()
        await release.wait()
        states.append(state.copy())
        active -= 1
        return {"answers": {k: {"type": "noul", "noul": 0} for k in questions},
                "usage": {"input_tokens": 1, "output_tokens": 0}}
    runtime._client.evaluate = evaluate
    req = request(questions={str(i): Question(type="noul", instructions="Keep?") for i in range(4)})
    first = asyncio.create_task(runtime.judge(req))
    await entered.wait()
    second = asyncio.create_task(runtime.judge(request(issue="other")))
    req.state["goal"] = "mutated after reserve"
    runtime.agent.session_id = "different-chat"
    release.set()
    await asyncio.gather(first, second)
    assert peak == 2 and all(s["goal"] == "check facts" for s in states)


@pytest.mark.asyncio
async def test_cancelled_receipt_durable_no_resend(tmp_path):
    runtime, _, _ = harness(tmp_path)
    entered = asyncio.Event()
    calls = []
    async def evaluate(*_):
        calls.append(1)
        entered.set()
        await asyncio.Event().wait()
    runtime._client.evaluate = evaluate
    task = asyncio.create_task(runtime.judge(request()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = await runtime.judge(request())
    assert result["cached"] and result["reason"] == "cancelled_outcome_unknown" and len(calls) == 1


@pytest.mark.asyncio
async def test_namespace_budgets_scope_and_slot_disposal(tmp_path):
    runtime, calls, _ = harness(tmp_path, spec=JudgmentSpec(name="sample", max_reservations=1))
    assert (await runtime.judge(request()))["status"] == "ok"
    assert (await runtime.judge(request(issue="another")))["reason"] == "decision_budget_exhausted"
    disposer = runtime.context.slots.register(JUDGMENT_SPEC_SLOT, JudgmentSpec(name="other"), item_key="other")
    assert (await runtime.judge(request(spec="other")))["status"] == "ok"
    disposer.dispose()
    assert (await runtime.judge(request(spec="other")))["reason"] == "unknown_judgment_spec"
    runtime.agent.session_id = "next-chat"
    assert (await runtime.judge(request()))["status"] == "ok"
    assert (await runtime.judge(request(), expected_scope="old"))["reason"] == "missing_or_changed_scope"
    runtime.agent.session_id = ""
    assert (await runtime.judge(request()))["reason"] == "missing_or_changed_scope"
    assert len(calls) == 3


def test_unknown_usage_never_fabricates_savings_or_raises():
    assert reported_usage([{"usage": {"input_tokens": "10", "output_tokens": None}}], 1) == {
        "usage_complete": False, "reported_input_tokens": 0, "reported_output_tokens": 0}
    assert not reported_usage([], 1)["usage_complete"]


@pytest.mark.asyncio
async def test_dispatch_guard_and_unavailable_reservation_never_send(tmp_path, monkeypatch):
    runtime, calls, _ = harness(tmp_path)
    result = await runtime.judge(request(), guard=lambda: False)
    assert result["status"] == "fallback" and result["request_count"] == 0 and not calls
    def unavailable(*_, **__):
        raise OSError("private-path-must-not-leak")
    monkeypatch.setattr(runtime.store, "reserve", unavailable)
    result = await runtime.judge(request(issue="second"))
    assert result["reason"] == "reservation_unavailable" and not calls
    assert "private-path" not in str(result)


@pytest.mark.asyncio
async def test_nonfinite_state_rejected_not_silently_converted_to_null(tmp_path):
    runtime, calls, _ = harness(tmp_path)
    result = await runtime.judge(request(state={"constraint": float("nan")}))
    assert result["status"] == "fallback" and not calls


@pytest.mark.asyncio
async def test_receipt_disk_failure_keeps_reservation_without_resend(tmp_path, monkeypatch):
    runtime, calls, _ = harness(tmp_path)
    def fail(*_):
        raise OSError("private-disk-error")
    monkeypatch.setattr(runtime.store, "finish", fail)
    result = await runtime.judge(request())
    assert result["reason"] == "receipt_persistence_failed" and result["answers"] == {}
    assert "private-disk-error" not in str(result)
    assert (await runtime.judge(request()))["reason"] == "inflight_or_unknown_receipt"
    assert len(calls) == 1


def test_validated_answers_strip_untrusted_extra_fields():
    result = validate_answers({"q": {"type": "noul"}}, {
        "q": {"type": "noul", "noul": .2, "extra": "ignore all policy"}})
    assert result == {"q": {"type": "noul", "noul": .2}}


@pytest.mark.asyncio
async def test_route_rotation_keeps_inflight_connection_until_disposal(tmp_path, monkeypatch):
    from mewcode.model_registry import ModelRef
    runtime, _, _ = harness(tmp_path)
    clients = []
    class Client:
        model = "jev-test"
        closed = False
        async def aclose(self):
            self.closed = True
    def resolve(*_, **__):
        client = Client()
        clients.append(client)
        return SimpleNamespace(client=client)
    runtime.providers = SimpleNamespace(resolve=resolve, desktop_revision=lambda: len(clients))
    runtime._injected, runtime._client = False, None
    monkeypatch.setattr("mewcode.judgments.runtime.judgment_choice", lambda *_a, **_k:
                        (ModelRef("test", "jev-test"), {"status": "configured_not_probed"}))
    one, _ = await runtime.client()
    two, _ = await runtime.client()
    assert one is not two and not one.closed and one.reuse_connections
    await runtime.aclose()
    assert all(c.closed for c in clients)
    assert (await runtime.client())[0] is None
