from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from mewcode.context.shadow_store import evaluate_labeled_report
from mewcode.context.tool_history_shadow import (
    SHADOW_SPEC,
    ShadowPolicy,
    ToolHistoryShadow,
    collect_snapshot,
    load_policy,
    plan_from_answers,
)
from mewcode.conversation import (
    ConversationManager,
    Message,
    ToolResultBlock,
    ToolUseBlock,
)
from mewcode.tools import ToolRegistry
from mewcode.tools.base import Tool, ToolResult
from mewcode.tools.inspect_tool_history import InspectToolHistory, Params
from tests.test_judgment_runtime import harness as judgment_harness


class ReadTool(Tool):
    name = "ReadFile"
    description = "Synthetic read-only fixture"
    params_model = BaseModel
    async def execute(self, params):
        return ToolResult("fixture")


def conversation(count=3, size=1000):
    view = ConversationManager()
    view.history = [Message("user", "Compare the results and implement the best design.")]
    for i in range(count):
        view.history += [Message("assistant", "", tool_uses=[ToolUseBlock(f"call-{i}", "ReadFile", {"path": f"notes-{i}.md"})]),
                         Message("user", "", tool_results=[ToolResultBlock(f"call-{i}", f"Result {i}: " + "x" * size)])]
    view.history += [Message("assistant", "Comparison complete."), Message("user", "Continue with the implementation.")]
    return view


def harness(tmp_path, **policy):
    runtime, calls, usage = judgment_harness(tmp_path, spec=SHADOW_SPEC)
    registry = ToolRegistry()
    registry.register(ReadTool())
    runtime.agent.registry = registry
    shadow = ToolHistoryShadow(runtime.agent, runtime, policy=ShadowPolicy(**{
        "min_candidate_chars": 0, "preserve_recent_messages": 2, "sample_interval_seconds": 0, **policy}))
    registry.register(InspectToolHistory(shadow))
    return shadow, calls, usage


@pytest.mark.asyncio
async def test_shadow_immutability_metadata_only_and_real_receipts(tmp_path):
    shadow, calls, usage = harness(tmp_path)
    view = conversation()
    original = copy.deepcopy(view.__dict__)
    state = {"replacements": {"previous": "preserve"}, "cache_anchor": 7}
    payload = {"agent": shadow.agent, "messages": view, "conversation": view, "system": "unchanged", "state": state}
    assert await shadow.before_model(payload) is payload
    assert view.__dict__ == original and payload["state"] is state
    await shadow._task
    report = await shadow.report()
    assert view.__dict__ == original and payload["system"] == "unchanged"
    assert report["samples"] == 1 and report["latest"]["action_counts"] == {"drop_call": 3}
    assert report["actual_saved_tokens"] == 0 and not report["applied"]
    assert report["latest"]["hypothetical_saved_tokens_estimate"] > 0
    assert report["promotion"]["misdeletion_rate"] is None and not report["promotion"]["allowed"]
    assert report["latest"]["observation_latency_ms"] >= 0
    assert usage.ledger.conversation_summary("chat-1")["request_count"] == len(calls) == 1
    persisted = json.dumps(shadow.store.reports(shadow.runtime.scope))
    assert "notes-0.md" not in persisted and "Result 0:" not in persisted and "Compare the results" not in persisted
    tool = shadow.agent.registry.get("InspectToolHistory")
    await tool.execute(Params())
    assert len(calls) == 1  # Report-only requests are not paid evaluations.
    assert not shadow.offer(view)
    # New observer, same durable receipt: no payment again and no double-counted sample.
    resumed = ToolHistoryShadow(shadow.agent, shadow.runtime, policy=shadow.policy()[0], store=shadow.store)
    assert resumed.offer(view)
    await resumed._task
    assert len(calls) == 1 and (await resumed.report())["samples"] == 1


def test_pin_sensitive_errors_unknown_writes_orphans_recent_and_multimodal(tmp_path):
    shadow, _, _ = harness(tmp_path)
    view = conversation(11)
    view.history[2].tool_results[0].is_error = True
    view.history[4].tool_results[0].content = "execution interrupted; outcome unknown"
    view.history[5].tool_uses[0].arguments = {"path": ".env"}
    view.history[8].tool_results[0].content = "Bearer unit-test-not-a-real-token"
    view.history[9].tool_uses[0].tool_name = "WriteFile"
    view.history[12].tool_results[0].content_blocks = [{"type": "image", "data": "fixture"}]
    view.history[14].tool_results[0].tool_use_id = "orphan"
    view.history[16].tool_results[0].content = "<persisted-output>already budgeted</persisted-output>"
    view.history[18].tool_results.append(view.history[18].tool_results[0])
    view.history[19].tool_uses[0].arguments = {"path": "x" * 1300}
    snapshot = collect_snapshot(view, shadow.agent.registry, shadow.agent.work_dir, shadow.policy()[0])
    reasons = {reason for row in snapshot["rows"] for reason in row["pinned"]}
    assert {"unpaired_or_ambiguous", "error_or_unknown_outcome", "security_or_sensitive",
            "mutating_or_unknown_tool", "multimodal_or_already_replaced", "input_too_large"} <= reasons
    answers = {prefix + row["ref"]: {"noul": 0} for row in snapshot["rows"] for prefix in ("call_", "result_")}
    plan, _ = plan_from_answers(snapshot, {"status": "ok", "answers": answers})
    assert all(row["action"] == "keep" for row in plan if row["pinned"])
    state = json.dumps(snapshot["state"])
    assert "unit-test-not-a-real-token" not in state and ".env" not in state and "outcome unknown" not in state
    # Move a pair into the protected recent suffix.
    recent = conversation(1)
    recent.history = recent.history[:-2]
    assert collect_snapshot(recent, shadow.agent.registry, shadow.agent.work_dir, shadow.policy()[0])["candidates"] == []


@pytest.mark.parametrize("call,result,action", [
    (0, 0, "drop_call"), (1, 0, "drop_result"), (0, 1, "keep"), (.4, .3, "keep"),
    (None, 0, "keep"), (True, 0, "keep"), (0, float("nan"), "keep"), (-1, 0, "keep"), (0, 2, "keep"),
])
def test_plan_conservative_independent_probabilities(tmp_path, call, result, action):
    shadow, _, _ = harness(tmp_path)
    snapshot = collect_snapshot(conversation(1), shadow.agent.registry, shadow.agent.work_dir, shadow.policy()[0])
    ref = snapshot["candidates"][0]
    original = copy.deepcopy(snapshot)
    plan, removed = plan_from_answers(snapshot, {"status": "ok", "answers": {
        "call_" + ref: {"noul": call}, "result_" + ref: {"noul": result}}})
    assert plan[0]["action"] == action and (removed > 0) == (action != "keep")
    assert snapshot == original
    json.dumps(plan, allow_nan=False)
    fallback, removed = plan_from_answers(snapshot, {"status": "fallback", "answers": {"call_" + ref: {"noul": 0}}})
    assert fallback[0]["action"] == "keep" and removed == 0


@pytest.mark.asyncio
async def test_budgeted_view_and_manual_sample_never_reread_raw_history(tmp_path):
    from mewcode.context.manager import (
        ContentReplacementState,
        apply_tool_result_budget,
    )
    shadow, calls, _ = harness(tmp_path)
    raw = conversation(2)
    raw.history[2].tool_results[0].content = "raw-output-not-in-provider-view" * 1000
    replacements = ContentReplacementState()
    (tmp_path / "transcript").mkdir()
    view, _ = apply_tool_result_budget(raw, tmp_path / "transcript", replacements, single_result_char_limit=4000)
    original = copy.deepcopy(raw.__dict__)
    saved_replacements = copy.deepcopy(replacements)
    shadow.agent._current_conversation = raw
    # No provider view has been observed: a manual request must not analyze raw history.
    await shadow.agent.registry.get("InspectToolHistory").execute(Params(action="sample"))
    assert not calls and shadow._task is None
    assert await shadow.before_model({"agent": shadow.agent, "messages": view})
    await shadow._task
    assert raw.__dict__ == original and replacements == saved_replacements
    assert "raw-output-not-in-provider-view" not in json.dumps(calls)
    assert (await shadow.report())["latest"]["pinned_reasons"]["multimodal_or_already_replaced"] >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("effect", ["deny", "ask", "review"])
async def test_no_permission_prompts_or_automatic_bypass(tmp_path, effect):
    shadow, calls, _ = harness(tmp_path)
    shadow.agent.permission_checker = SimpleNamespace(check=lambda *_: SimpleNamespace(effect=effect))
    payload = {"agent": shadow.agent, "messages": conversation()}
    assert await shadow.before_model(payload) is payload and shadow._task is None and not calls
    assert not shadow.sample_latest()


@pytest.mark.asyncio
async def test_unloaded_plugin_disabled_tool_off_missing_scope_or_route_all_skip(tmp_path):
    shadow, calls, _ = harness(tmp_path)
    shadow.agent.registry.disable("InspectToolHistory")
    assert not shadow.offer(conversation())
    shadow.agent.registry.enable("InspectToolHistory")
    shadow.agent.session_id = ""
    assert not shadow.offer(conversation())
    shadow.agent.session_id = "chat-1"
    shadow.runtime._injected = False
    assert not shadow.offer(conversation()) and shadow._last_status == "judgment_route_unavailable"
    shadow.runtime._injected = True
    shadow.runtime.context.close()
    assert not shadow.offer(conversation()) and not calls


@pytest.mark.asyncio
async def test_nonblocking_cancellation_and_scope_switch_preserve(tmp_path):
    shadow, calls, _ = harness(tmp_path)
    entered = asyncio.Event()
    async def blocked(*_):
        entered.set()
        await asyncio.Event().wait()
    shadow.runtime._client.evaluate = blocked
    view = conversation()
    original = copy.deepcopy(view.__dict__)
    # The observer returns before a provider response can finish.
    await asyncio.wait_for(shadow.before_model({"agent": shadow.agent, "messages": view}), 1)
    await asyncio.wait_for(entered.wait(), 2)
    assert not shadow._task.done()
    assert not shadow.offer(view)
    await shadow.aclose()
    assert shadow._task.cancelled() and view.__dict__ == original and not calls
    assert (await shadow.report())["actual_saved_tokens"] == 0
    resumed = ToolHistoryShadow(shadow.agent, shadow.runtime, policy=shadow.policy()[0])
    assert resumed.offer(view)
    await resumed._task  # Unknown outcome is cached; no resend/hang.
    shadow.agent.session_id = "new-chat"
    assert (await resumed.report())["samples"] == 0
    assert not resumed.sample_latest()


@pytest.mark.asyncio
async def test_cooldown_minimum_size_and_durable_four_sample_cap(tmp_path):
    shadow, calls, _ = harness(tmp_path, min_candidate_chars=99999)
    assert not shadow.offer(conversation()) and not calls
    shadow._fixed_policy = ShadowPolicy(min_candidate_chars=0, preserve_recent_messages=2, sample_interval_seconds=3600)
    assert shadow.offer(conversation())
    await shadow._task
    other = conversation()
    other.history[-1].content = "New facts"
    assert not shadow.offer(other) and len(calls) == 1
    shadow._fixed_policy = shadow.policy()[0].model_copy(update={"sample_interval_seconds": 0})
    for i in range(1, 6):
        other.history[-1].content = f"Next state {i}"
        assert shadow.offer(other)
        await shadow._task
    assert len(calls) == 4 and (await shadow.report())["samples"] == 4
    assert shadow._last_status == "decision_budget_exhausted"


@pytest.mark.asyncio
async def test_observer_secrets_and_oversize_constraints_not_truncated(tmp_path):
    shadow, calls, _ = harness(tmp_path)
    view = conversation()
    fake_secret = "tvly-dev-fixture-never-real"
    view.history[0].content += " key: " + fake_secret
    view.history[2].tool_results[0].content += fake_secret
    assert shadow.offer(view)
    await shadow._task
    assert fake_secret not in json.dumps(calls)
    assert fake_secret not in json.dumps(await shadow.report())
    view.history[0].content = "constraints must not be silently omitted" * 1000
    assert not shadow.offer(view) and shadow._last_status == "state_budget_exceeded_preserve"
    assert len(calls) == 1


def test_enforce_invalid_config_disables_instead_of_mutating(tmp_path, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "isolated-home"))
    assert load_policy(tmp_path)[0].mode == "shadow"
    folder = tmp_path / ".mewcode"
    folder.mkdir()
    config = folder / "config.yaml"
    config.write_text("tool_history_pruning:\n  mode: enforce\n", encoding="utf-8")
    policy, status = load_policy(tmp_path)
    assert policy.mode == "off" and status == "invalid_config_preserve"
    config.write_text("tool_history_pruning:\n  mode: off\n", encoding="utf-8")
    # YAML 1.1 parses bare 'off' as False: configuration must be explicitly valid.
    assert load_policy(tmp_path)[0].mode == "off"


@pytest.mark.asyncio
async def test_revisit_proxy_and_independent_labels_never_auto_promote(tmp_path):
    shadow, _, _ = harness(tmp_path)
    view = conversation()
    assert shadow.offer(view)
    await shadow._task
    report = (await shadow.report())["latest"]
    ref = report["decisions"][0]["ref"]
    blank = evaluate_labeled_report(report, {})
    assert blank["misdeletion_rate"] is None
    labeled = evaluate_labeled_report(report, {ref: "retain"})
    assert labeled["misdeletion_rate"] == 1 and labeled["unreviewed_removals"] == 2
    assert not labeled["promotion_allowed"]
    previous = report["decisions"][0]
    shadow.store.observe_revisits(shadow.runtime.scope, [{"ref": "later-call", "resource": previous["resource"]}])
    shadow.store.observe_revisits(shadow.runtime.scope, [{"ref": "later-call", "resource": previous["resource"]}])
    assert (await shadow.report())["revisit_signals"] == 1
    assert (await shadow.report())["promotion"]["misdeletion_rate"] is None
    assert shadow.store.summary("different-scope")["samples"] == 0


def test_offline_scorer_only_reads_and_never_enables(tmp_path, capsys):
    from mewcode.context.shadow_store import main
    report_file, label_file = tmp_path / "report.json", tmp_path / "labels.json"
    report_file.write_text(json.dumps({"mode": "shadow", "applied": False, "decisions": [
        {"ref": "must-keep", "action": "drop_result"}, {"ref": "obsolete", "action": "drop_call"}]}), encoding="utf-8")
    label_file.write_text(json.dumps({"must-keep": "retain", "obsolete": "dispensable"}), encoding="utf-8")
    before = (report_file.read_bytes(), label_file.read_bytes())
    result = main([str(report_file), str(label_file)])
    assert result["misdeletion_rate"] == .5 and not result["promotion_allowed"]
    assert json.loads(capsys.readouterr().out) == result
    assert before == (report_file.read_bytes(), label_file.read_bytes())


@pytest.mark.asyncio
@pytest.mark.parametrize("denied", [False, True])
async def test_actual_graph_builder_obeys_node_tools_and_rebinds(tmp_path, denied):
    from mewcode.config import ProviderConfig
    from mewcode.expert_group import ExpertExecutionPolicy
    from mewcode.expert_web import _build_role_agent
    from mewcode.model_registry import ModelRef
    from mewcode.plugins import ApplicationHost
    from mewcode.plugins.runtime import AgentRuntimeRequest
    from mewcode.plugins.services import AGENT_FACTORY
    from mewcode.tools.capabilities import ProbeParams
    host = ApplicationHost(str(tmp_path), discover_plugins=False).start()
    config = ProviderConfig(name="test", protocol="openai-compat", model="test", base_url="https://unit.invalid")
    client = SimpleNamespace(model="test", base_url="https://unit.invalid", set_usage_recorder=lambda _: None,
                             get_usage_recorder=lambda: None)
    root = child = None
    try:
        root = host.context.get(AGENT_FACTORY).create(AgentRuntimeRequest(provider=config, client=client,
            auxiliary_client=client, work_dir=str(tmp_path), enable_memory=False, enable_skills=False))
        session = SimpleNamespace(agent=root.agent, context=root.context, work_dir=str(tmp_path),
                                  session=SimpleNamespace(session_id="graph-chat"), task_manager=None)
        service = SimpleNamespace(resolve=lambda *a, **k: SimpleNamespace(config=config, client=client, context_window=64000))
        policy = ExpertExecutionPolicy("stable", False, 0, node_tools=("ReadFile", "InspectToolHistory", "ProbeCapabilities"),
                                       deny_tools=("InspectToolHistory",) if denied else ())
        agent, _, child = _build_role_agent(session, service, "executor", ModelRef("local", "test"), policy)
        data = json.loads((await agent.registry.get("ProbeCapabilities").execute(ProbeParams(target="context"))).output)
        assert data["tool_history_shadow"]["tool_enabled"] is not denied
        if denied:
            assert not agent.tool_history_shadow.allowed()
        else:
            assert agent.registry.get("InspectToolHistory").observer.agent is agent
            assert agent.registry.get("InspectToolHistory").observer is not root.agent.tool_history_shadow
            assert agent.tool_history_shadow.allowed()
    finally:
        if child:
            await child.aclose()
        if root:
            await root.aclose()
        await host.aclose()


@pytest.mark.asyncio
async def test_registered_plugin_runtime_rebinding_discovery_and_lifecycle(tmp_path):
    from mewcode.plugins import ApplicationHost
    from mewcode.plugins.runtime import AgentRuntimeRequest
    from mewcode.plugins.services import AGENT_FACTORY, JUDGMENTS, TOOL_HISTORY_SHADOW
    from mewcode.tools.capabilities import ProbeParams
    shadow, _, usage = harness(tmp_path)
    host = ApplicationHost(str(tmp_path), discover_plugins=False)
    await host.astart()
    runtimes = []
    try:
        factory = host.context.get(AGENT_FACTORY)
        base = {"provider": shadow.runtime._client.config, "work_dir": str(tmp_path), "client": shadow.runtime._client,
                "auxiliary_client": shadow.runtime._client, "enable_memory": False, "enable_skills": False,
                "enable_tool_search": False, "usage_service": usage}
        parent = await factory.acreate(AgentRuntimeRequest(**base))
        runtimes.append(parent)
        child = await factory.acreate(AgentRuntimeRequest(**base, include_default_tools=False,
            inherit_parent_tools=False, extra_tools=(parent.registry.get("InspectToolHistory"),)))
        runtimes.append(child)
        omitted = await factory.acreate(AgentRuntimeRequest(**base, include_default_tools=False, inherit_parent_tools=False))
        runtimes.append(omitted)
        assert child.registry.get("InspectToolHistory").observer is child.agent.tool_history_shadow
        assert child.context.get(JUDGMENTS) is child.agent.decision_runtime
        assert child.context.get(TOOL_HISTORY_SHADOW) is not parent.context.get(TOOL_HISTORY_SHADOW)
        assert not omitted.agent.tool_history_shadow.allowed()
        data = json.loads((await parent.registry.get("ProbeCapabilities").execute(ProbeParams(target="context", live=True))).output)
        assert data["tool_history_shadow"]["plugin_loaded"] and data["typed_judgments"]["types"] == ["choice", "noul", "score"]
        assert data["live_probe"]["status"] == "not_run"
        parent.agent.session_id = "chat-integration"
        observer = parent.agent.tool_history_shadow
        observer._fixed_policy = ShadowPolicy(min_candidate_chars=0, preserve_recent_messages=2, sample_interval_seconds=0)
        observer.runtime._injected = True
        observer.runtime._client = shadow.runtime._client
        view = conversation()
        payload = {"agent": parent.agent, "messages": view}
        before = [asdict(m) for m in view.history]
        assert await parent.context.events.waterfall("agent/before_model", payload) is payload
        await observer._task
        assert [asdict(m) for m in view.history] == before
        await host.context.aunload("core.tool_history_shadow")
        assert not observer.allowed() and not observer.offer(conversation())
        await host.context.aload("core.tool_history_shadow")
        assert observer.allowed()
    finally:
        for runtime in reversed(runtimes):
            await runtime.aclose()
        await host.aclose()
    assert observer._closed and observer.runtime._closed
