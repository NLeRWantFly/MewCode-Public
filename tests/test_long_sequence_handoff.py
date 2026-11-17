"""Long local scheduling with real host decisions; no model/provider calls."""
import copy
import hashlib
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.conversation import Message
from mewcode.expert_group import ExpertGroupOrchestrator, ExpertRoleResult
from mewcode.expert_runs import ExpertRunStore, serialize_messages
from mewcode.experts.continuation import checkpoint_prompt
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.experts.recovery_state import advanced, advance_work_cursor, progress_marker, work_progress
from mewcode.kernel.escalation import KernelScheduler as SchedulerRecovery
from mewcode.experts.stage_context import collect_stage_inputs, input_revision, stage_inputs_prompt
from mewcode.tools.read_stage_result import ReadStageResult, ReadStageResultParams
from tests.test_expert_flow import _profile


def simple_flow():
    return ExpertFlowSpec.from_value({"flow_id": "long-sequence", "label": "Long analysis",
        "nodes": [{"node_id": "work", "role": "planner", "label": "Source analysis"},
                  {"node_id": "done", "role": "manager", "label": "Result"}],
        "edges": [{"from": "work", "to": "done"}], "finalizer_node_id": "done"})


def source_tool(page, *, call_id="read"):
    return {"tool_id": call_id, "name": "ReadFile", "status": "completed",
        "args": {"file_path": "source.pdf", "page_idx": page},
        "result_sha256": hashlib.sha256(f"page {page}".encode()).hexdigest()}


def test_helper_receipts_and_summary_changes_cannot_buy_progress():
    snapshot = {"node_states": {"work": {"tools": {}}}}
    before = progress_marker(snapshot, "work")
    stage = snapshot["node_states"]["work"]
    for i, name in enumerate(["SubmitHandoff", "LoadSkill", "ProbeCapabilities", "BackgroundTasks", "Bash"]):
        stage["tools"][str(i)] = {"name": name, "status": "completed", "args": {"summary": "new claim"}}
    stage["handoff"] = {"summary": "More claims", "open_items": ["continue"]}
    after = progress_marker(snapshot, "work")
    assert not advanced(before, after) and after["write_receipts"] == []


def test_content_progress_deduplicates_new_call_ids_and_prevents_oscillation():
    snapshot = {"node_states": {"work": {"tools": {}}}}
    stage = snapshot["node_states"]["work"]
    baseline = progress_marker(snapshot, "work")
    stage["tools"]["first"] = source_tool(1)
    stage["handoff"] = {"inline_outputs": [{"name": "atoms", "content": "atom-1"}]}
    after = progress_marker(snapshot, "work")
    changed, cursor = advance_work_cursor({"before": baseline, "after": after})
    assert changed and len(cursor["observed_sources"]) == len(cursor["inline_outputs"]) == 1
    stage["tools"]["duplicate"] = source_tool(1, call_id="new-id")
    assert not advanced(after, progress_marker(snapshot, "work"))
    stage["tools"]["second"] = source_tool(2)
    changed, cursor = advance_work_cursor({"after": progress_marker(snapshot, "work")}, cursor)
    assert changed
    stage["tools"] = {"old": source_tool(1)}
    assert not advance_work_cursor({"after": progress_marker(snapshot, "work")}, cursor)[0]


def test_same_file_version_is_deduplicated_and_unknown_calls_do_not_count():
    stage = {"tools": {"a": {"name": "WriteFile", "status": "completed",
        "args": {"file_path": "a.txt", "content": "a"}, "effect_sha256": "version-a"}}}
    snapshot = {"node_states": {"work": stage}}
    before = progress_marker(snapshot, "work")
    stage["tools"]["b"] = copy.deepcopy(stage["tools"]["a"])
    stage["tools"]["unknown"] = {"name": "EditFile", "status": "outcome_unknown"}
    assert not advanced(before, progress_marker(snapshot, "work"))
    stage["tools"]["b"]["effect_sha256"] = "version-b"
    assert advanced(before, progress_marker(snapshot, "work"))


@pytest.mark.asyncio
async def test_auto_consumed_content_cursor_survives_reopen_and_rejects_old_versions(tmp_path, monkeypatch):
    from tests.test_scheduler_recovery import controller, decide
    handler, flow, _, store, _, _, _ = controller(tmp_path, monkeypatch)
    handler.model_decisions = False
    for index, content in enumerate(["version-a", "version-b", "version-a"], 1):
        def save(value):
            stage = value["node_states"]["extract"]
            stage["slice_progress_baseline"] = progress_marker(value, "extract")
            stage.update(started_at=float(index), completed_at=index + .5,
                handoff={"inline_outputs": [{"name": "analysis", "content": content}], "open_items": ["next"]})
        store.mutate(handler.run_id, save, durable=True)
        # Reconstruct the controller: all evidence consumption is checkpointed.
        handler = SchedulerRecovery(session=handler.session, registry=handler.registry, store=store,
            run_id=handler.run_id, emit=handler.emit, before_resume=AsyncMock(), task="analysis")
        decision = await decide(handler, flow)
        if index < 3:
            assert decision["controller"]["action"] == "progress_continue"
            assert decision["controller"]["level"] is None
            assert decision["action"] == "continue_checkpoint"
        else:
            # No new evidence: only the kernel's one bounded retry is legal.
            assert decision["controller"]["level"] == "retry_same_method"
    from mewcode.experts.node_ledger import ledger_for_run
    record = store.load(handler.run_id)
    continuations = [fact for fact in ledger_for_run(record).view("extract").history
        if fact.payload.get("recovery_action") == "progress_continue"
        and fact.payload['decision']['level'] is None]
    assert len(continuations) == 2 and len(record['kernel_progress']['extract']['inline_outputs']) == 2


@pytest.mark.asyncio
async def test_plain_node_can_continue_72_productive_segments_without_recursion(tmp_path):
    flow, run_id = simple_flow(), "c" * 32
    store = ExpertRunStore(tmp_path / "runs")
    store.create(run_id=run_id, conversation_id="long-test", task="Read all pages", attachments=[],
        profile={}, strategy="stable", flow=flow)
    session = SimpleNamespace(agent=SimpleNamespace(client=SimpleNamespace()))
    events, calls, depths, suggestions = [], [], [], []
    async def emit(event):
        events.append(copy.deepcopy(event))
    handler = SchedulerRecovery(session=session, registry=SimpleNamespace(), store=store,
        run_id=run_id, emit=emit, before_resume=AsyncMock(), task="Read all pages")
    async def runner(node, model, prompt, policy, member=None):
        calls.append(node.node_id)
        if node.node_id == "done":
            return ExpertRoleResult(text="Final analysis", handoff={"summary": "Delivered", "status": "completed"})
        page = calls.count("work")
        depths.append(sum(frame.function == "execute_node" for frame in inspect.stack()))
        suggestions.append(prompt.count("宿主接续建议"))
        def save(value):
            stage = value["node_states"].setdefault("work", {"tools": {}})
            stage["slice_progress_baseline"] = progress_marker(value, "work")
            stage.update(started_at=float(page), completed_at=page + .5,
                input_revision=policy.input_revision,
                messages=serialize_messages([Message("assistant", f"page {page} saved")]),
                handoff={"status": "needs_attention", "blocker_kind": "internal_work",
                    "summary": f"Saved page {page}", "open_items": [f"Read page {page + 1}"]})
            stage["tools"][str(page)] = source_tool(page, call_id=str(page))
        store.mutate(run_id, save, durable=True)
        if page == 73:
            return ExpertRoleResult(text="All pages analyzed", handoff={"status": "completed", "summary": "Delivered"})
        return ExpertRoleResult(text=f"Saved page {page}", status="continuing", error_kind="internal_work",
            handoff=store.load(run_id)["node_states"]["work"]["handoff"],
            work_progress=work_progress(store.load(run_id), "work"), billable_tokens=10)
    result = await ExpertGroupOrchestrator(runner=lambda *_: None, node_runner=runner,
        recovery_handler=handler, archive_root=tmp_path / "archives", event_sink=emit).run(
            "Read all pages", _profile(), {}, strategy="stable", flow=flow, run_id=run_id)
    assert result.status == "completed" and calls.count("work") == 73 and calls[-1] == "done"
    assert max(depths) == min(depths) == 1 and max(suggestions) == 1
    from mewcode.experts.node_ledger import ledger_for_run
    decisions = [fact.payload['decision'] for fact in ledger_for_run(store.load(run_id)).view("work").history
        if fact.payload.get('recovery_action') == 'progress_continue']
    assert len(decisions) == 72
    assert all(d['action'] == 'progress_continue' and d['level'] is None for d in decisions)
    assert not any(e["type"] == "ask_user" for e in events)


@pytest.mark.asyncio
async def test_multi_agent_handoff_pages_stay_pinned_and_unrelated_sources_are_private():
    handoff = {"summary": "index", "inline_outputs": [{"name": "full", "content": "a" * 30000}]}
    sources = collect_stage_inputs([{"from_node": "worker", "handoff": handoff, "status": "completed"}], {}, {})
    revision = sources["worker"]["source_revision"]
    reader = ReadStageResult(sources)
    handoff["inline_outputs"][0]["content"] = "changed-after-dispatch"
    first = await reader.execute(ReadStageResultParams(node_id="worker", offset=0, limit=100))
    second = await reader.execute(ReadStageResultParams(node_id="worker", offset=100, limit=100))
    assert f"SOURCE_REVISION: {revision}" in first.output and f"SOURCE_REVISION: {revision}" in second.output
    assert "changed-after-dispatch" not in reader.sources["worker"]
    assert (await reader.execute(ReadStageResultParams(node_id="unrelated"))).is_error
    assert revision in stage_inputs_prompt(sources)


def test_resume_prompt_keeps_work_index_and_omits_full_content():
    stage = {"input_revision": "v1", "handoff": {"summary": "saved", "open_items": ["next page"],
        "inline_outputs": [{"name": "atoms", "content": "large-body" * 10000}]}}
    prompt = checkpoint_prompt(stage, node_id="work")
    assert "next page" in prompt and "atoms" in prompt and "v1" in prompt
    assert "large-body" not in prompt and len(prompt) < 1000


@pytest.mark.asyncio
async def test_large_agent_chain_is_bounded_but_its_full_index_remains_readable():
    sources = {f"worker-{i}": {"source_revision": "a" * 64,
        "handoff": {"summary": "details" * 1000}, "status": "completed"} for i in range(1000)}
    prompt = stage_inputs_prompt(sources)
    assert len(prompt) < 19000 and "1000" in prompt and "node_id=''" in prompt
    reader = ReadStageResult(sources)
    assert "worker-999" in reader.index
    page = await reader.execute(ReadStageResultParams(node_id="", offset=0, limit=300))
    assert "NODE: (index)" in page.output and "NEXT_OFFSET: 300" in page.output


def test_derived_handoff_metadata_does_not_invalidate_legacy_work():
    flow = simple_flow()
    old = {"source": {"node_id": "source", "text": "original", "status": "completed", "handoff": {}}}
    new = copy.deepcopy(old)
    new["source"]["source_revision"] = "a" * 64
    args = dict(flow=flow, node=flow.node_map["work"], model=_profile().planner)
    assert input_revision(sources=old, **args) == input_revision(sources=new, **args)


@pytest.mark.asyncio
async def test_productive_continuation_deducts_original_budget_each_segment(tmp_path, monkeypatch):
    from mewcode import expert_group
    from mewcode.expert_group import ExpertExecutionPolicy
    monkeypatch.setattr(expert_group, "derive_execution_policy", lambda *args:
        ExpertExecutionPolicy("stable", False, 0, token_budget=6000, timeout_seconds=60))
    calls, limits = [], []
    async def runner(node, model, prompt, policy, member=None):
        calls.append(node.node_id)
        limits.append(policy.token_budget)
        number = len(calls)
        marker = lambda count: {"review_complete": count, "review_recorded": count, "submitted_items": []}
        return ExpertRoleResult(text="saved", status="continuing", error_kind="internal_work",
            billable_tokens=2000, work_progress={"before": marker(number-1), "after": marker(number)})
    async def recover(**kwargs):
        return {"action": "continue_checkpoint", "source": "host_progress_checkpoint", "decision_tokens": 100}
    result = await ExpertGroupOrchestrator(runner=lambda *_: None, node_runner=runner,
        recovery_handler=recover, archive_root=tmp_path).run("Finite budget", _profile(), {},
        strategy="stable", flow=simple_flow())
    assert calls == ["work"] * 3 and limits == [6000, 3900, 1800]
    assert result.status != "completed"


def test_usage_keeps_segment_totals_and_latest_context_separate():
    from mewcode.experts.continuation import cumulative_segment_usage
    before = {"input_tokens": 50, "output_tokens": 10, "billable_tokens": 60, "total_cost_usd": .1}
    current = {"input_tokens": 20, "output_tokens": 5, "billable_tokens": 25,
        "request_input_tokens": 1000, "total_cost_usd": .05}
    combined = cumulative_segment_usage(before, current)
    assert combined["input_tokens"] == 70 and combined["billable_tokens"] == 85
    assert combined["request_input_tokens"] == 1000
    assert cumulative_segment_usage(before, current) == combined  # Streaming replacements cannot double count.
    assert before["input_tokens"] == 50 and current["input_tokens"] == 20


@pytest.mark.asyncio
@pytest.mark.parametrize("token_budget,configured_cap", [(None, 2048), (6000, 2048), (1200, 8192), (6000, None)])
async def test_web_segments_resume_latest_conversation_and_delivery_without_provider_calls(tmp_path, monkeypatch, token_budget, configured_cap):
    import asyncio
    from dataclasses import replace
    from mewcode import expert_group
    from mewcode import expert_web
    from mewcode.agent import StreamText, UsageEvent
    from mewcode.conversation import ConversationManager
    from mewcode.expert_web import _role_registry
    from mewcode.experts.flow import ExpertFlowStore
    from mewcode.orchestration.plan_session import PlanSessionStore
    from mewcode.permissions import PermissionMode
    from mewcode.permissions.policy import build_permission_checker
    from mewcode.tools.submit_handoff import SubmitHandoffParams
    from tests.test_expert_children import FakeSession, make_registry

    project = tmp_path / "project"
    project.mkdir()
    flows = ExpertFlowStore(tmp_path / "flows.json")
    runs = ExpertRunStore(tmp_path / "web-runs", storage='sqlite')
    # This fake runner only submits inline handoffs. Declare that scope so
    # capability preflight does not replace the continuation test.
    fixture_flow = simple_flow()
    flows.put(project, replace(fixture_flow, nodes=tuple(replace(node, capabilities={}) for node in fixture_flow.nodes)))
    calls, events = [], []
    output_limits = []
    original_policy = expert_group.derive_execution_policy
    monkeypatch.setattr(expert_group, "derive_execution_policy", lambda multi_agent:
        replace(original_policy(multi_agent), token_budget=token_budget))
    class FakeAgent:
        total_input_tokens, total_output_tokens, context_window = 10, 2, 1000
        def __init__(self, role, policy):
            self.policy = policy
            self.permission_checker = build_permission_checker(project, mode=PermissionMode.AUTO)
            self.client = SimpleNamespace(expose_reasoning=False, max_output_tokens=configured_cap or 2048)
            if configured_cap is not None:
                self.expert_output_configured_cap = configured_cap
            def set_max_output_tokens(limit):
                output_limits.append((self.node_id, policy.token_budget, limit))
                self.client.max_output_tokens = limit
            self.client.set_max_output_tokens = set_max_output_tokens
            self.registry = _role_registry(FakeSession(make_registry("ReadFile", "WriteFile")), role, policy)
        def set_review_authorization_context(self, context):
            self.node_id = context.split("STAGE: ", 1)[1].split(" ", 1)[0]
        async def run(self, conversation):
            calls.append(self.node_id)
            page = calls.count("work")
            if self.node_id == "work" and page > 1:
                # Restarted nodes receive the bounded host handoff context,
                # rather than replaying every prior assistant message.
                assert f"atom-{page - 1}" in str(conversation.get_messages())
                record = await self.registry.get("ReadStageResult").execute(ReadStageResultParams(node_id="work"))
                assert f"atom-{page - 1}" in record.output and not record.is_error
            unfinished = self.node_id == "work" and page < 8
            params = SubmitHandoffParams(summary=f"Saved batch {page}",
                status="needs_attention" if unfinished else "completed",
                blocker_kind="internal_work" if unfinished else "",
                open_items=[f"Read page {page + 1}"] if unfinished else [],
                inline_outputs=[{"name": f"page-{page}", "content": f"atom-{page}"}])
            assert not (await self.registry.get("SubmitHandoff").execute(params)).is_error
            conversation.add_assistant_message(f"PAGE-{page}-SAVED")
            yield UsageEvent(input_tokens=10, output_tokens=2, request_input_tokens=100,
                billable_tokens=12, total_cost_usd=.05)
            yield StreamText(text=f"PAGE-{page}-SAVED")
    def build(_session, _registry, role, _model, policy, _verdict):
        return FakeAgent(role, policy), None, SimpleNamespace(conversation=ConversationManager(), aclose=AsyncMock())
    monkeypatch.setattr(expert_web, "_build_role_agent", build)
    session = SimpleNamespace(work_dir=str(project), conversation_id="long-web-test", multi_agent=False,
        agent=SimpleNamespace(client=SimpleNamespace(), permission_checker=build_permission_checker(project, mode=PermissionMode.AUTO)),
        session=SimpleNamespace(meta=SimpleNamespace(message_count=0), append=lambda *a: None,
            append_expert_run=lambda *a, **kw: None), conversation=ConversationManager(), expert_run_id="",
        expert_control_action="pause", context=SimpleNamespace(get=lambda key, default=None: default,
            slots=SimpleNamespace(resolve=lambda key: {})),
        task_manager=SimpleNamespace(list_tasks=lambda: [], mark_notified=lambda ids: None),
        expert_child_event_callback=None, status_payload=lambda: {}, tasks_payload=lambda: [])
    async def send(event):
        events.append(copy.deepcopy(event))
    await asyncio.wait_for(expert_web.run_expert_chat(SimpleNamespace(send_json=send), "Read 8 pages",
        session=session, registry_service=SimpleNamespace(catalog=AsyncMock(return_value={"providers": []}), resolve=lambda model: None),
        profile_store=SimpleNamespace(get=lambda _: _profile()), flow_store=flows,
        # Nine real host dispatches persist checkpoints even with a fake model.
        # This is a cancellation guard, not an execution-budget assertion.
        plan_store=PlanSessionStore(tmp_path / "plans.json"), run_store=runs, confirmed_new_flow=flows.get(project)), 240)
    run_id = next(event["run_id"] for event in events if event.get("run_id"))
    saved = runs.load(run_id)
    assert calls == ["work"] * 8 + ["done"] and saved["status"] == "completed"
    from mewcode.experts.node_ledger import ledger_for_run
    from mewcode.kernel.escalation import action_count
    decisions = [fact.payload['decision'] for fact in ledger_for_run(saved).view('work').history
                 if fact.source == 'host' and fact.payload.get('recovery_action')]
    assert len(decisions) == 7
    assert all(d['action'] == 'progress_continue' and d['level'] is None for d in decisions)
    assert action_count(ledger_for_run(saved).view('work'), 'assist') == 0
    assert saved["usage"]["total"]["input_tokens"] == 90
    assert saved["node_states"]["work"]["usage"]["billable_tokens"] == 96
    if token_budget is None:
        assert output_limits == []
    else:
        assert len(output_limits) == 9
        assert all(limit == min(configured_cap or 2048, remaining - 12, 8192)
                   for _node, remaining, limit in output_limits)
        work_budgets = [remaining for node, remaining, _limit in output_limits if node == "work"]
        assert work_budgets == [token_budget - 12 * number for number in range(8)]
    assert not any(event["type"] == "ask_user" for event in events)
    # Host snapshots and versioned workflow packages are not business output.
    assert all((path.relative_to(project).parts[:2] == ('.mewcode', 'review_context')
                and path.suffix == '.json')
               or (path.relative_to(project).parts[:2] == ('.mewcode', 'workflow-skills')
                   and (path.name == 'SKILL.md' or path.suffix == '.json'))
               for path in project.rglob('*') if path.is_file())
