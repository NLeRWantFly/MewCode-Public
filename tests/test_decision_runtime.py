from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from mewcode.config import ProviderConfig
from mewcode.cordis import Context
from mewcode.decisions.browser import (
    FastBrowse,
    FastBrowseParams,
    browser_transaction,
    navigation_candidates,
)
from mewcode.decisions.clarification import before_ask
from mewcode.decisions.runtime import DecisionRuntime
from mewcode.decisions.spec import Candidate, DecisionInput, DecisionSpec
from mewcode.decisions.store import DecisionStore
from mewcode.plugins.services import DECISION_SPEC_SLOT
from mewcode.tools import ToolRegistry
from mewcode.tools.ask_user import AskUserParams
from mewcode.tools.base import ToolResult
from mewcode.typesafe_client import TypeSafeClient
from mewcode.usage.service import UsageService


def harness(tmp_path, *, chosen="a", suitability=1, confidence=1, handler=None):
    calls = []
    def reply(request):
        body = json.loads(request.content)
        calls.append(body)
        if handler:
            return handler(body)
        answers = {}
        for key, question in body["questions"].items():
            if question["type"] == "noul":
                answers[key] = {"type": "noul", "noul": suitability}
            else:
                selected = chosen if chosen in question["criteria"] else "none"
                answers[key] = {"type": "choice", "choice": selected, "confidence": confidence,
                                "probabilities": {k: int(k == selected) for k in question["criteria"]}}
        return httpx.Response(200, json={"model": "jev-test", "answers": answers,
                                       "usage": {"input_tokens": 100, "output_tokens": 5}})
    config = ProviderConfig(name="Jev test", protocol="typesafe", model="jev-test",
                            base_url="https://unit.invalid/v1", api_key="test")
    client = TypeSafeClient(config, transport=httpx.MockTransport(reply))
    client.configure_usage_tracking(config, endpoint="/v1/systemone")
    usage = UsageService(tmp_path)
    usage.bind_client(client)
    agent = SimpleNamespace(session_id="chat-1", work_dir=str(tmp_path), agent_id="main", trace_id="trace", client=client)
    runtime = DecisionRuntime(agent, None, store=DecisionStore(tmp_path / "decisions.sqlite3"), client=client)
    agent.decision_runtime = runtime
    return runtime, calls, usage


def request(**kw):
    return DecisionInput(**{ "issue": "city", "goal": "Choose a city within known constraints",
        "candidates": [Candidate(id="a", description="City A"), Candidate(id="b", description="City B")], **kw})


@pytest.mark.asyncio
async def test_selection_batch_cache_and_durable_usage(tmp_path):
    runtime, calls, usage = harness(tmp_path)
    first = await runtime.decide(request())
    assert first["status"] == "selected" and first["choice"] == "a"
    assert set(calls[0]["questions"]) == {"selection", "fit_a", "fit_b"}
    second = await runtime.decide(request())
    assert second["cached"] and len(calls) == 1
    # A resumed runtime sees the same receipt and does not pay again.
    resumed = DecisionRuntime(runtime.agent, None, store=runtime.store, client=runtime._client)
    assert (await resumed.decide(request()))["cached"]
    summary = usage.ledger.conversation_summary("chat-1")
    assert summary["request_count"] == 1 and summary["tokens"]["total_tokens"] == 105
    assert summary["models"][0]["model"] == "jev-test"
    assert usage.ledger.conversation_summary("chat-2")["request_count"] == 0


@pytest.mark.asyncio
async def test_all_negative_requires_material_revision_then_escalates(tmp_path):
    runtime, calls, _ = harness(tmp_path, chosen="none", suitability=0)
    first = await runtime.decide(request())
    assert first["status"] == "rethink" and first["reason"] == "all_rejected"
    assert (await runtime.decide(request()))["round"] == 1
    revised = await runtime.decide(request(state={"new_evidence": "A/B exceed known budget"}))
    assert revised["status"] == "escalate" and revised["round"] == 2
    assert (await runtime.decide(request(state={"new_evidence": "third"})))["reason"] == "decision_budget_exhausted"
    assert len(calls) == 2
    # A different problem in the conversation is not disabled by this one.
    assert (await runtime.decide(request(issue="another")))["round"] == 1


@pytest.mark.asyncio
async def test_low_confidence_is_rethink_not_user_answer(tmp_path):
    runtime, _, _ = harness(tmp_path, confidence=.2)
    result = await runtime.decide(request())
    assert result["status"] == "rethink" and "choice" not in result


@pytest.mark.asyncio
async def test_hard_constraints_never_send_excluded_candidate(tmp_path):
    runtime, calls, _ = harness(tmp_path)
    result = await runtime.decide(request(candidates=[Candidate(id="a", description="no", eligible=False)]))
    assert result["status"] == "rethink" and not calls


@pytest.mark.asyncio
async def test_oversize_missing_scope_and_evidence_never_send(tmp_path):
    runtime, calls, _ = harness(tmp_path)
    assert (await runtime.decide(request(state={"x": "a" * 40000})))["reason"] == "decision_state_too_large"
    assert (await runtime.decide(request(evidence_refs=["invented"])))["reason"] == "missing_observed_evidence"
    runtime.agent.session_id = ""
    assert (await runtime.decide(request()))["reason"] == "missing_conversation_scope"
    assert not calls


@pytest.mark.asyncio
async def test_concurrent_same_state_is_reserved_once(tmp_path):
    runtime, calls, _ = harness(tmp_path)
    one, two = await asyncio.gather(runtime.decide(request()), runtime.decide(request()))
    assert len(calls) == 1 and {one["status"], two["status"]} <= {"selected", "rethink"}
    assert one.get("cached") or two.get("cached")


@pytest.mark.asyncio
async def test_provider_failure_is_bounded_no_secret_body(tmp_path):
    runtime, calls, _ = harness(tmp_path, handler=lambda _: httpx.Response(500, text="private-provider-body"))
    first = await runtime.decide(request())
    assert first["status"] == "rethink" and "private-provider-body" not in json.dumps(first)
    assert (await runtime.decide(request()))["cached"] and len(calls) == 1


@pytest.mark.asyncio
async def test_cancel_keeps_receipt_and_no_resend(tmp_path):
    runtime, _, _ = harness(tmp_path)
    started = asyncio.Event()
    calls = []
    async def evaluate(*_):
        calls.append(1)
        started.set()
        await asyncio.Event().wait()
    runtime._client.evaluate = evaluate
    task = asyncio.create_task(runtime.decide(request()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await runtime.decide(request()))["cached"] and len(calls) == 1


@pytest.mark.asyncio
async def test_typed_slot_is_live_and_versioned(tmp_path):
    runtime, calls, _ = harness(tmp_path)
    ctx = Context()
    ctx.slots.declare(DECISION_SPEC_SLOT, kind="keyed")
    runtime.context = ctx
    assert (await runtime.decide(request(spec="custom")))["reason"] == "unknown_decision_spec"
    dispose = ctx.slots.register(DECISION_SPEC_SLOT, DecisionSpec(name="custom", version="2", instructions="Choose"), item_key="custom")
    assert (await runtime.decide(request(spec="custom")))["spec_version"] == "2"
    dispose.dispose()
    assert (await runtime.decide(request(spec="custom")))["reason"] == "unknown_decision_spec"
    assert len(calls) == 1


def questions(**kw):
    return AskUserParams(**{"purpose": "decision", "decision_issue": "pick", "questions": [
        {"type": "radio", "name": "plan", "message": "Which implementation?", "options": ["A", "B"]}], **kw})


@pytest.mark.asyncio
async def test_preask_all_negative_returns_to_main_model_not_user(tmp_path):
    runtime, calls, _ = harness(tmp_path, chosen="none", suitability=0)
    first = json.loads((await before_ask(runtime.agent, questions())).output)
    assert first["status"] == "main_model" and first["selector_status"] == "rethink"
    second = json.loads((await before_ask(runtime.agent,
        questions(decision_state={"new_fact": "new strategy"}))).output)
    assert second["status"] == "main_model" and second["selector_status"] == "escalate"
    final = json.loads((await before_ask(runtime.agent, questions())).output)
    assert final["status"] == "main_model"
    assert final["reason_kind"] == "bounded_attempts_exhausted"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_real_user_facts_bypass_jev(tmp_path):
    runtime, calls, _ = harness(tmp_path)
    assert await before_ask(runtime.agent, questions(purpose="user_input")) is None
    assert not calls


@pytest.mark.asyncio
async def test_unbound_authorization_does_not_open_generic_user_question(tmp_path):
    runtime, calls, _ = harness(tmp_path)
    result = json.loads((await before_ask(runtime.agent, questions(purpose="authorization"))).output)
    assert result["status"] == "needs_authorization"
    assert "review_call_ref" in result["next_action"]
    assert not calls


@pytest.mark.asyncio
async def test_preask_selected_is_explicitly_not_a_user_answer(tmp_path):
    runtime, _, _ = harness(tmp_path, chosen="option_0")
    result = json.loads((await before_ask(runtime.agent, questions())).output)
    assert result["suggested_option"] == "A" and result["source"] == "jev_advisory_not_user_answer"


@pytest.mark.asyncio
async def test_auto_nudge_is_bounded_without_automatic_user_interrupt(tmp_path):
    runtime, calls, _ = harness(tmp_path)
    first = json.loads((await before_ask(runtime.agent, questions(purpose="auto"))).output)
    second = json.loads((await before_ask(runtime.agent, questions(purpose="auto"))).output)
    assert first["status"] == "classify_before_asking"
    assert second["status"] == "main_model"
    assert not calls


@pytest.mark.asyncio
async def test_browser_transaction_serial_and_reentrant():
    client = SimpleNamespace()
    order = []
    async def first():
        async with browser_transaction(client):
            order.append("start")
            async with browser_transaction(client):
                await asyncio.sleep(.01)
            order.append("end")
    async def second():
        async with browser_transaction(client):
            order.append("other")
    await asyncio.gather(first(), second())
    assert order == ["start", "end", "other"]


def test_browser_candidates_only_observed_scoped_urls():
    snap = '''### Page URL: https://example.test/start
- link "Good" [ref=e1]:
  - /url: /docs
- link "Bad" [ref=e2]:
  - /url: https://foreign.test/docs
- link "Mutation" [ref=e3]:
  - /url: /delete
- link "Script" [ref=e4]:
  - /url: javascript:alert(1)
'''
    url, rows = navigation_candidates(snap, {"example.test"})
    assert url == "https://example.test/start"
    assert [r.facts["observed_url"] for r in rows] == ["https://example.test/docs"]


@pytest.mark.asyncio
async def test_score_validation_and_pooled_client_cleanup(tmp_path):
    def response(_):
        return httpx.Response(200, json={"model": "jev-test", "usage": {"input_tokens": 1}, "answers": {
            "rating": {"type": "score", "score": .8, "confidence": .7,
                       "legend": {"0": "Unsuitable", "1": "Suitable"}, "probabilities": {"0": .2, "1": .8}}}})
    runtime, calls, _ = harness(tmp_path, handler=response)
    client = runtime._client
    client.reuse_connections = True
    question = {"rating": {"type": "score", "instructions": "Rate fit", "criteria": ["Unsuitable", "Suitable"]}}
    await client.evaluate({}, question)
    pooled = client._http_client
    await client.evaluate({}, question)
    assert client._http_client is pooled and len(calls) == 2
    await client.aclose()
    assert pooled.is_closed and client._http_client is None


@pytest.mark.asyncio
async def test_unchanged_negative_loop_stops_without_new_paid_calls(tmp_path):
    runtime, calls, _ = harness(tmp_path, chosen="none", suitability=0)
    assert (await runtime.decide(request()))["status"] == "rethink"
    for _ in range(3):
        result = await runtime.decide(request())
    assert result["status"] == "escalate" and result["reason"] == "unchanged_replan_loop"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_explicitly_denied_decision_tool_returns_to_main_model(tmp_path):
    runtime, calls, _ = harness(tmp_path)
    runtime.agent.registry = ToolRegistry()
    result = await before_ask(runtime.agent, questions())
    assert json.loads(result.output)["status"] == "main_model" and not calls
    repeated = json.loads((await before_ask(runtime.agent, questions())).output)
    assert repeated["status"] == "main_model"
    assert repeated["reason_kind"] == "bounded_attempts_exhausted"


@pytest.mark.asyncio
@pytest.mark.parametrize("stale,failed", [(False, False), (True, False), (False, True)])
async def test_fast_browser_uses_checked_dispatch_and_stops_on_unknown_or_stale(tmp_path, stale, failed):
    runtime, _, _ = harness(tmp_path)
    transport = SimpleNamespace()
    registry = ToolRegistry()
    for name in ("browser_snapshot", "browser_navigate"):
        registry.register(SimpleNamespace(name=name, _client=transport))
    runtime.agent.registry = registry
    snapshot = 'Page URL: https://example.test/start\n- link "Docs" [ref=e1]:\n  - /url: /docs'
    calls = []
    async def dispatch(tc):
        calls.append(tc)
        if tc.tool_name == "browser_navigate":
            return ToolResult("unknown" if failed else "ok", is_error=failed)
        return ToolResult(snapshot + ("changed" if stale and len(calls) == 2 else ""))
    runtime.agent._execute_tool_noninteractive = dispatch
    async def decide(*_, **__):
        return {"status": "selected", "choice": "link_0"}
    runtime.decide = decide
    data = json.loads((await FastBrowse(runtime.agent, runtime).execute(FastBrowseParams(
        goal="Read docs", issue="docs", allowed_hosts=["example.test"], max_steps=1))).output)
    navigation = [c for c in calls if c.tool_name == "browser_navigate"]
    assert len(navigation) == (0 if stale else 1)
    if navigation:
        assert navigation[0].arguments == {"url": "https://example.test/docs"}
    assert not data["verification_pass"]
    assert data["reason"] == ("stale_observation" if stale else "navigation_failed_or_unknown_do_not_replay" if failed else "step_budget_exhausted")


@pytest.mark.asyncio
async def test_runtime_rebinds_tools_and_discovers_builtin_skill(tmp_path):
    from mewcode.plugins import ApplicationHost
    from mewcode.plugins.runtime import AgentRuntimeRequest
    from mewcode.plugins.services import AGENT_FACTORY, USAGE
    from mewcode.skills.loader import SkillLoader
    from mewcode.tools.capabilities import ProbeParams
    runtime, _, usage = harness(tmp_path)
    host = ApplicationHost(str(tmp_path), discover_plugins=False)
    await host.astart()
    scopes = []
    try:
        factory = host.context.get(AGENT_FACTORY)
        base = {"provider": runtime._client.config, "work_dir": str(tmp_path), "client": runtime._client,
                "auxiliary_client": runtime._client, "enable_memory": False, "enable_skills": False,
                "enable_tool_search": False, "usage_service": usage}
        parent = await factory.acreate(AgentRuntimeRequest(**base))
        scopes.append(parent)
        child = await factory.acreate(AgentRuntimeRequest(**base, include_default_tools=False,
                     inherit_parent_tools=False, extra_tools=(parent.registry.get("DecideNextStep"),)))
        scopes.append(child)
        assert child.registry.get("DecideNextStep").runtime.agent is child.agent
        assert child.context.get(USAGE) is usage
        assert child.registry.get("FastBrowse") is None
        data = json.loads((await parent.registry.get("ProbeCapabilities").execute(ProbeParams(target="decision"))).output)
        assert data["decision"]["tool_enabled"] and "option-selector" in data["decision"]["specs"]
        assert "jev-decisions" in SkillLoader(str(tmp_path)).load_all()
    finally:
        for scope in reversed(scopes):
            await scope.aclose()
        await host.aclose()


@pytest.mark.asyncio
async def test_browser_waiters_never_hold_leaf_permits():
    from unittest.mock import AsyncMock

    from mcp import types

    from mewcode.mcp.tool_wrapper import MCPToolWrapper
    from mewcode.runtime_scheduler import (
        RuntimeScheduler,
        bind_runtime_scheduler,
        reset_runtime_scheduler,
    )
    client = SimpleNamespace(is_alive=True, call_tool=AsyncMock(return_value=types.CallToolResult(
        content=[types.TextContent(type="text", text="snapshot")], isError=False)))
    tool = MCPToolWrapper("playwright", types.Tool(name="browser_snapshot", inputSchema={"type": "object", "properties": {}}),
                          client, canonical_names=True)
    scheduler = RuntimeScheduler(limit=1, kind_limits={"tool": 1})
    token = bind_runtime_scheduler(scheduler)
    waiter = None
    try:
        async with asyncio.timeout(1):
            async with browser_transaction(client):
                waiter = asyncio.create_task(tool.run(tool.params_model()))
                await asyncio.sleep(0)
                assert scheduler.snapshot()["running"] == 0
                assert not (await tool.run(tool.params_model())).is_error
            assert not (await waiter).is_error
        assert client.call_tool.await_count == 2
    finally:
        if waiter is not None and not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        reset_runtime_scheduler(token)
