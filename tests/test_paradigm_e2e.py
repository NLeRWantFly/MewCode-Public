"""Offline end-to-end checks for the orchestration paradigm integration."""
from __future__ import annotations

import re
import copy
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from mewcode.expert_group import ExpertGroupOrchestrator, ExpertRoleResult
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.flow import ExpertFlowSpec


CATALOG = {"providers": []}
FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "paradigm"


def _profile():
    from tests.test_expert_flow import _profile as profile
    return profile()


def _flow(raw):
    return ExpertFlowSpec.from_value(raw)


def _fixture(name):
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def _fixture_node_flow(fixture, node_id):
    raw = copy.deepcopy(fixture["locked_flow"])
    raw["nodes"] = [node for node in raw["nodes"] if node.get("node_id") == node_id]
    raw["nodes"][0]["model"] = None  # The fixture route is replaced by the offline node_runner.
    raw["edges"] = []
    raw["finalizer_node_id"] = node_id
    raw.setdefault("meta", {})["loops"] = []
    flow = _flow(raw)
    flow.validate()
    return flow


def _recovery_spec():
    from mewcode.orchestration.spec import OrchestrationSpec

    tools = ["ReadFile", "Bash", "WriteFile", "Glob", "Grep",
             "ReadAuditArtifact", "InspectVisualEvidence", "ReadStageResult", "InspectGitState"]
    return OrchestrationSpec.from_value({
        "spec_id": "recovery-e2e",
        "label": "Recovery e2e",
        "goal": "Plan a bounded sample, pilot, process one item, review and summarize",
        "finalizer_node_id": "reduce",
        "goal_contract": {"objective": "Process one declared item", "acceptance": ["one result reviewed"]},
        "nodes": [
            {"node_id": "plan", "kind": "plan", "label": "Plan", "goal": "Plan a bounded pilot",
             "plan_brief": {"steps": ["Run one pilot on a representative sample before batch work"],
                            "outputs": ["bounded plan"], "acceptance": ["one sample selected"]}},
            {"node_id": "pilot", "kind": "implement", "label": "Pilot", "goal": "Run one pilot",
             "tools": tools,
             "plan_brief": {"steps": ["Run a pilot on one sample"],
                            "outputs": ["pilot receipt"], "acceptance": ["pilot result recorded"]}},
            {"node_id": "batch", "kind": "implement", "label": "Batch", "goal": "Process the declared item",
             "tools": tools,
             "plan_brief": {"steps": ["Process one work item"],
                            "outputs": ["batch report"], "acceptance": ["one item processed"]}},
            {"node_id": "check", "kind": "decision", "label": "Check", "goal": "Review batch output",
             "plan_brief": {"steps": ["Check the item against its acceptance test"],
                            "outputs": ["loop decision"], "acceptance": ["item is verified"]}},
            {"node_id": "audit", "kind": "audit", "label": "Audit", "goal": "Independently verify the result",
             "tools": ["ReadFile", "ReadAuditArtifact"],
             "plan_brief": {"steps": ["Read and verify the batch report"],
                            "outputs": ["audit record"], "acceptance": ["one report verified"]}},
            {"node_id": "reduce", "kind": "reduce", "label": "Reduce", "goal": "Summarize the verified result",
             "plan_brief": {"steps": ["Summarize the verified result"],
                            "outputs": ["final summary"], "acceptance": ["one summary delivered"]}},
        ],
        "edges": [
            {"from": "plan", "to": "pilot"},
            {"from": "pilot", "to": "batch"},
            {"from": "batch", "to": "check"},
            {"from": "check", "to": "batch", "condition": "loop"},
            {"from": "check", "to": "audit", "condition": "pass"},
            {"from": "audit", "to": "reduce"},
        ],
        "meta": {"loops": [{
            "loop_id": "batch-loop", "entry_node_id": "batch", "check_node_id": "check",
            "node_ids": ["batch", "check"], "reviewer_node_ids": ["check"],
            "exit_condition": "the item is verified",
            "criteria": [{"criterion_id": "ITEM-1", "acceptance_test": "batch output is verified"}],
            "max_iterations": 4, "mode": "foreach",
            "items": [{"item_id": "one", "goal": "process one declared item"}],
        }]},
    })


@pytest.mark.asyncio
async def test_ordered_fallback_tries_next_target_after_first_fails(tmp_path):
    flow = _flow({
        "flow_id": "ordered-fallback",
        "version": 1,
        "label": "ordered-fallback",
        "nodes": [
            {"node_id": "work", "role": "executor", "label": "Work",
             "failure_policy": {"action": "fallback",
                                "fallback_node_ids": ["fallback-a", "fallback-b"]}},
            {"node_id": "fallback-a", "role": "executor", "label": "Fallback A"},
            {"node_id": "fallback-b", "role": "executor", "label": "Fallback B"},
            {"node_id": "final", "role": "manager", "label": "Final"},
        ],
        "edges": [
            {"from": "work", "to": "final"},
            {"from": "fallback-a", "to": "final"},
            {"from": "fallback-b", "to": "final"},
        ],
        "finalizer_node_id": "final",
    })
    store = ExpertRunStore(tmp_path / "runs")
    run_id = "a" * 32
    store.create(run_id=run_id, conversation_id="offline", task="verify fallback",
                 attachments=[], profile={}, strategy="stable", flow=flow)
    calls = []

    async def node_runner(node, _model, prompt, _policy, member=None):
        calls.append(node.node_id)
        if node.node_id == "work":
            return ExpertRoleResult("capability missing", status="needs_attention",
                error_kind="capability_missing", handoff={"status": "blocked", "blockers": ["missing capability"]})
        if node.node_id == "fallback-a":
            return ExpertRoleResult("first fallback failed", status="failed",
                handoff={"status": "blocked"})
        if node.node_id == "fallback-b":
            assert "回退输入与原节点事实" in prompt
        return ExpertRoleResult(node.node_id + " done", status="completed",
                                handoff={"status": "completed"})

    async def unused_runner(*_args):
        raise AssertionError("the graph node runner should execute every node")

    result = await ExpertGroupOrchestrator(runner=unused_runner, node_runner=node_runner,
        run_store=store, work_dir=tmp_path, archive_root=tmp_path / "archive").run(
            "verify ordered fallback", _profile(), CATALOG, strategy="stable", run_id=run_id, flow=flow)

    assert result.status == "completed", f"status={result.status}, calls={calls}, result={result}"
    assert calls == ["work", "fallback-a", "fallback-b", "final"]


@pytest.mark.asyncio
async def test_live_stage_prompt_resolves_four_list_paths_and_shared_self_help_protocol(tmp_path):
    from mewcode.orchestration.convert import to_expert_flow
    from mewcode.orchestration.spec import OrchestrationSpec
    from mewcode.orchestration.graph_lint import lint_graph

    paths = [r"C:\inputs\one.pdf", r"C:\inputs\two.pdf",
             r"C:\inputs\three.pdf", r"C:\inputs\four.pdf"]
    spec = OrchestrationSpec.from_value({
        "spec_id": "resolved-inputs-e2e",
        "label": "Resolved inputs e2e",
        "goal": "Read all declared files and report findings",
        "finalizer_node_id": "N01",
        "goal_contract": {"run_inputs": [{"name": "pdf_paths", "label": "PDF paths",
            "type": "list", "required": True}], "acceptance": ["4 files reviewed"]},
        "nodes": [{"node_id": "N01", "kind": "implement", "label": "Review PDFs",
            "goal": "Review the four supplied PDFs",
            "plan_brief": {"steps": ["Read each declared PDF"], "outputs": ["review notes"],
                "acceptance": ["4 PDFs checked"], "known_inputs": []}}],
        "edges": [],
    })
    assert not [finding for finding in lint_graph(spec) if finding.severity == "error"]
    flow = to_expert_flow(spec)
    # A persisted prior brief may still contain the placeholder even after the
    # graph contract has passed lint. The live runner must resolve it against
    # the actual four paths supplied at run start.
    flow.meta["node_plan_briefs"]["N01"]["known_inputs"] = ["PDF paths待提供"]
    prompts = []

    async def node_runner(node, _model, prompt, _policy, member=None):
        prompts.append(prompt)
        return ExpertRoleResult("reviewed", status="completed",
                                handoff={"status": "completed", "summary": "reviewed"})

    async def unused_runner(*_args):
        raise AssertionError("the graph node runner should execute the finalizer")

    result = await ExpertGroupOrchestrator(runner=unused_runner, node_runner=node_runner,
        work_dir=tmp_path, archive_root=tmp_path / "archive").run(
            "review PDFs", _profile(), CATALOG, strategy="stable", flow=flow,
            checkpoint={"run_inputs": {"pdf_paths": paths}})

    assert result.status == "completed"
    assert len(prompts) == 1
    prompt = prompts[0]
    assert "pdf_paths（运行参数，4 项）" in prompt
    assert all(path in prompt for path in paths)
    assert "已被运行参数 `pdf_paths` 取代" in prompt
    assert "## 遇到问题时" in prompt
    assert all(phrase in prompt for phrase in ("先自行诊断", "通过 RequestAssistance", "才通过提问交给用户"))
    assert len(re.findall(r"不得|不要|不能|不应|不可|禁止|切勿", prompt)) <= 8


@pytest.mark.asyncio
async def test_real_graph_run_recovers_four_faults_without_asking_user(tmp_path):
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.experts.assistance import AssistanceCoordinator
    from mewcode.experts.command_receipts import persist_command_receipts, request_receipts
    from mewcode.experts.node_ledger import ledger_for_run
    from mewcode.kernel.escalation import KernelScheduler as SchedulerRecovery
    from mewcode.orchestration.convert import to_expert_flow
    from mewcode.orchestration.graph_lint import lint_graph

    spec = _recovery_spec()
    errors = [finding for finding in lint_graph(spec) if finding.severity == "error"]
    assert errors == []
    flow = to_expert_flow(spec)
    flow.validate()
    store = ExpertRunStore(tmp_path / "runs")
    run_id = "c" * 32
    store.create(run_id=run_id, conversation_id="offline", task=spec.goal,
                 attachments=[], profile={}, strategy="stable", flow=flow)
    events, calls, assistance_calls = [], [], []
    session = SimpleNamespace(agent=SimpleNamespace(client=SimpleNamespace()), work_dir=str(tmp_path))

    async def emit(event):
        events.append(copy.deepcopy(event))
        if event.get("type") == "ask_user":
            request = getattr(session, "_ask_user_request", None)
            if request and not request.future.done():
                request.future.set_result({"direction": "test guard only"})

    async def before_resume():
        return None

    async def assistance_dispatch(helper, _prompt, policy):
        assistance_calls.append(helper.node_id)

        def persist_probe(snapshot):
            stage = snapshot.setdefault("node_states", {}).setdefault(helper.node_id, {})
            stage["available_tools"] = ["RuntimeDoctor"]
            stage["tools"] = {"probe-1": {"tool_id": "probe-1", "name": "RuntimeDoctor",
                "args": {"file_path": str(tmp_path / "runtime-config.json")},
                "output": "host probe complete", "status": "completed", "is_error": False}}
        store.mutate(run_id, persist_probe, durable=True)
        policy.assistance_holder.update(next_step="resume", notes="Host probe confirmed a safe continuation.")
        return ExpertRoleResult("helper probe complete", status="completed",
                                handoff={"status": "completed"}, billable_tokens=1)

    assistant = AssistanceCoordinator(store=store, run_id=run_id, dispatch=assistance_dispatch,
        emit=emit, before_resume=before_resume, task=spec.goal, work_dir=str(tmp_path), model={})
    recovery = SchedulerRecovery(session=session, registry=SimpleNamespace(), store=store, run_id=run_id,
        emit=emit, before_resume=before_resume, task=spec.goal, model_decisions=False,
        primary_repair=assistant)
    seen = {key: 0 for key in ("pilot", "batch", "audit", "reduce")}
    request_ref = {}

    async def node_runner(node, _model, _prompt, policy, member=None):
        node_id = node.node_id
        calls.append(node_id)
        if node_id == "pilot":
            seen[node_id] += 1
            if seen[node_id] == 1:
                from mewcode.tools.request_assistance import RequestAssistance, RequestAssistanceParams
                store.mutate(run_id, lambda record: record["node_states"].setdefault(node_id, {}).update(
                    input_revision=policy.input_revision), durable=True)
                requested = await RequestAssistance(store, run_id, node_id).execute(
                    RequestAssistanceParams(kind="capability", need="The node requires the RuntimeDoctor capability.",
                                            requested_tools=["RuntimeDoctor"]))
                assert not requested.is_error
                return ExpertRoleResult("capability missing", status="needs_attention",
                    error_kind="capability_missing", handoff={"status": "blocked"})
            snapshot = store.load(run_id)
            assert any(fact.payload.get("probe_success") is True
                       for fact in ledger_for_run(snapshot).view(node_id).history)
        elif node_id == "batch":
            seen[node_id] += 1
            if seen[node_id] == 1:
                command = "git status --short"
                snapshot = store.load(run_id) or {}
                epoch = ledger_for_run(snapshot).view(node_id).epoch
                receipts = request_receipts(node_id, policy.input_revision,
                    [{"command": command, "input_scope": ["current worktree"]}],
                    status="outcome_unknown", epoch=epoch)
                request_ref["id"] = receipts[0]["request_id"]
                def save_unknown(record):
                    record.setdefault("node_states", {}).setdefault(node_id, {})["input_revision"] = policy.input_revision
                    persist_command_receipts(record, {"node_id": node_id, "command_receipts": receipts})
                store.mutate(run_id, save_unknown, durable=True)
                return ExpertRoleResult("read-only probe receipt was lost", status="needs_attention",
                    error_kind="effect_unknown", handoff={"status": "blocked"}, command_receipts=receipts)
            current = store.load(run_id)["node_states"][node_id]["command_receipts"]
            assert any(row.get("request_id") == request_ref["id"] and row.get("retry_reserved")
                       and row.get("status") == "not_started" for row in current)
        elif node_id == "audit":
            seen[node_id] += 1
            if seen[node_id] == 1:
                return ExpertRoleResult("environment prerequisite missing", status="needs_attention",
                    error_kind="environment_fault", handoff={"status": "blocked"})
            return ExpertRoleResult("audit passed", status="completed", handoff={"status": "completed"},
                verdict={"verdict": "PASS", "issues": [], "evidence": [{"kind": "host", "ref": "audit"}]})
        elif node_id == "reduce":
            seen[node_id] += 1
            if seen[node_id] == 1:
                before = {"schema_version": 2, "observed_sources": [], "inline_outputs": [],
                    "review_complete": 0, "review_recorded": 0, "write_receipts": [],
                    "submitted_items": [], "submitted_revisions": []}
                after = {**before, "observed_sources": ["host-verified:one-report"]}
                return ExpertRoleResult("execution segment exhausted after verified progress",
                    status="budget_exhausted", error_kind="segment_timeout",
                    work_progress={"before": before, "after": after}, billable_tokens=1)
        if node_id == "check":
            return ExpertRoleResult("item verified", status="completed",
                loop_decision={"contract_hash": policy.loop_context["contract_hash"],
                    "decision": "EXIT", "checks": [{"criterion_id": "ITEM-1", "state": "PASS",
                        "evidence": "verified batch report", "location": "batch/report"}]})
        return ExpertRoleResult(node_id + " completed", status="completed",
                                handoff={"status": "completed", "summary": node_id + " completed"})

    async def unused_runner(*_args):
        raise AssertionError("the generated graph must use node_runner")

    result = await ExpertGroupOrchestrator(runner=unused_runner, node_runner=node_runner,
        event_sink=emit, run_store=store, recovery_handler=recovery,
        work_dir=tmp_path, archive_root=tmp_path / "archive").run(
            spec.goal, _profile(), CATALOG, strategy="stable", run_id=run_id, flow=flow)

    assert result.status == "completed", f"{result.status}: {result.final_summary}"
    assert calls[0:2] == ["plan", "pilot"]
    assert calls.count("batch") == 2
    assert calls.count("audit") == 2
    assert calls.count("reduce") == 2
    assert any(node_id.startswith("pilot__assist_") for node_id in assistance_calls)
    assert request_ref["id"]
    snapshot = store.load(run_id)
    batch_state = snapshot["node_states"]["batch"]
    pilot_facts = ledger_for_run(snapshot).view("pilot").history
    assert any(fact.payload.get("probe_success") is True for fact in pilot_facts)
    ask_events = [event for event in events if event.get("type") == "ask_user"]
    assert not ask_events, (
        f"unexpected ask_user={ask_events}; state_keys={sorted(batch_state)}; "
        f"receipts={batch_state.get('command_receipts')}; "
        f"blocker_state={[(key, value) for key, value in batch_state.items() if 'block' in key or 'fact' in key]}; "
        f"recovery={batch_state.get('scheduler_recovery')}"
    )
    batch_decisions = batch_state["scheduler_recovery"]["decisions"]
    assert any(row.get("action") in {"continue_checkpoint", "diagnose_path"}
               for row in batch_decisions)
    assert any(row.get("readonly_retry_receipt") for row in batch_state["command_receipts"])


@pytest.mark.asyncio
async def test_sanitized_n03_checkpoint_enters_assistance_and_clears_hold_without_asking(tmp_path):
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.experts.assistance import AssistanceCoordinator
    from mewcode.experts.command_receipts import persist_command_receipts, request_receipts
    from mewcode.experts.node_ledger import ledger_for_run
    from mewcode.kernel.escalation import KernelScheduler as SchedulerRecovery
    from mewcode.tools.request_assistance import RequestAssistance, RequestAssistanceParams

    fixture = _fixture("capability_missing_scheduler_hold.json")
    source_stage = fixture["node_states"]["N03"]
    assert fixture["shape_counts"]["hold_until_clearance"] == 12
    assert [row["status"] for row in source_stage["command_receipts"]] == ["outcome_unknown", "not_started"]
    flow = _fixture_node_flow(fixture, "N03")
    store = ExpertRunStore(tmp_path / "runs")
    run_id = "e" * 32
    store.create(run_id=run_id, conversation_id="offline", task="Replay sanitized N03 checkpoint",
                 attachments=[], profile={}, strategy="stable", flow=flow)
    store.mutate(run_id, lambda record: record.update(node_states={"N03": {
        "node_id": "N03", "status": source_stage["status"], "attempt": source_stage["attempt"],
        "input_revision": "", "scheduler_recovery": {"phase": "paused", "decisions": []},
    }}), durable=True)
    events, calls, helpers, request_ids = [], [], [], []
    session = SimpleNamespace(agent=SimpleNamespace(client=SimpleNamespace()), work_dir=str(tmp_path))

    async def emit(event):
        events.append(copy.deepcopy(event))

    async def before_resume():
        return None

    async def assistance_dispatch(helper, _prompt, policy):
        helpers.append(helper.node_id)
        def save_probe(snapshot):
            stage = snapshot.setdefault("node_states", {}).setdefault(helper.node_id, {})
            stage["available_tools"] = ["RuntimeDoctor"]
            stage["tools"] = {"fixture-runtime-probe": {"tool_id": "fixture-runtime-probe",
                "name": "RuntimeDoctor", "args": {"scope": "fixture runtime"},
                "output": "runtime capability available", "status": "completed", "is_error": False}}
        store.mutate(run_id, save_probe, durable=True)
        policy.assistance_holder.update(next_step="resume", notes="N03 capability confirmed by host probe.")
        return ExpertRoleResult("runtime capability confirmed", status="completed",
                                handoff={"status": "completed"}, billable_tokens=1)

    coordinator = AssistanceCoordinator(store=store, run_id=run_id, dispatch=assistance_dispatch,
        emit=emit, before_resume=before_resume, task="Replay sanitized N03 checkpoint",
        work_dir=str(tmp_path), model={})
    recovery = SchedulerRecovery(session=session, registry=SimpleNamespace(), store=store, run_id=run_id,
        emit=emit, before_resume=before_resume, task="Replay sanitized N03 checkpoint", model_decisions=False,
        primary_repair=coordinator)

    async def node_runner(node, _model, _prompt, policy, member=None):
        calls.append(node.node_id)
        if len(calls) == 1:
            def save_readonly_fixture(record):
                stage = record["node_states"]["N03"]
                stage["input_revision"] = policy.input_revision
                rows = request_receipts("N03", policy.input_revision,
                    [{"command": "git status --short", "input_scope": ["sanitized fixture worktree"]}],
                    status="outcome_unknown", epoch=ledger_for_run(record).view("N03").epoch)
                persist_command_receipts(record, {"node_id": "N03", "command_receipts": rows})
                request_ids.append(rows[0]["request_id"])
            store.mutate(run_id, save_readonly_fixture, durable=True)
            request = await RequestAssistance(store, run_id, "N03").execute(RequestAssistanceParams(
                kind="capability", need="Confirm RuntimeDoctor availability for N03.",
                requested_tools=["RuntimeDoctor"]))
            assert not request.is_error
            return ExpertRoleResult("capability missing at N03", status="needs_attention",
                error_kind="capability_missing", handoff={"status": "blocked"})
        snapshot = store.load(run_id)
        ledger = ledger_for_run(snapshot)
        assert any(fact.payload.get("probe_success") is True for fact in ledger.view("N03").history)
        readonly = next(row for row in snapshot["node_states"]["N03"]["command_receipts"]
                        if row.get("request_id") == request_ids[0])
        assert readonly.get("readonly_retry_receipt") is True and readonly["status"] == "not_started"
        return ExpertRoleResult("N03 resumed after host assistance", status="completed",
            handoff={"status": "completed", "summary": "N03 resumed"})

    async def unused_runner(*_args):
        raise AssertionError("the fixture replay must execute through node_runner")

    result = await ExpertGroupOrchestrator(runner=unused_runner, node_runner=node_runner, event_sink=emit,
        run_store=store, recovery_handler=recovery, work_dir=tmp_path, archive_root=tmp_path / "archive").run(
            "Replay sanitized N03 checkpoint", _profile(), CATALOG, strategy="stable", run_id=run_id, flow=flow)

    assert result.status == "completed"
    assert calls == ["N03", "N03"]
    assert len(helpers) == 1 and helpers[0].startswith("N03__assist_")
    assert not [event for event in events if event.get("type") == "ask_user"]
    decisions = (store.load(run_id)["node_states"]["N03"].get("scheduler_recovery") or {}).get("decisions") or []
    assert not [row for row in decisions if row.get("action") == "hold_until_clearance"]


@pytest.mark.asyncio
async def test_sanitized_n01_71_page_checkpoint_continues_on_durable_progress(tmp_path):
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.kernel.escalation import KernelScheduler as SchedulerRecovery

    fixture = _fixture("audit_tool_recovery_pause.json")
    source_stage = fixture["node_states"]["N01"]
    assert fixture["shape_counts"]["ReadAuditArtifact_calls"] == len(source_stage["ReadAuditArtifact_calls"]) == 71
    assert fixture["shape_counts"]["pdf_pages"] == 1073
    flow = _fixture_node_flow(fixture, "N01")
    store = ExpertRunStore(tmp_path / "runs")
    run_id = "f" * 32
    store.create(run_id=run_id, conversation_id="offline", task="Replay sanitized N01 progress checkpoint",
                 attachments=[], profile={}, strategy="stable", flow=flow)
    store.mutate(run_id, lambda record: record.update(node_states={"N01": {
        "node_id": "N01", "status": "paused", "attempt": source_stage["attempt"],
        "iteration_recovery": {key: value for key, value in source_stage["iteration_recovery"].items()
                               if key in {"slice_exhaustions", "automatic_continuations", "phase", "diagnostic_attempts"}},
        "ReadAuditArtifact_calls": copy.deepcopy(source_stage["ReadAuditArtifact_calls"]),
        "scheduler_recovery": {"phase": "paused", "decisions": []},
    }}), durable=True)
    events, calls = [], []
    session = SimpleNamespace(agent=SimpleNamespace(client=SimpleNamespace()), work_dir=str(tmp_path))

    async def emit(event):
        events.append(copy.deepcopy(event))

    async def before_resume():
        return None

    recovery = SchedulerRecovery(session=session, registry=SimpleNamespace(), store=store, run_id=run_id,
        emit=emit, before_resume=before_resume, task="Replay sanitized N01 progress checkpoint", model_decisions=False)
    paths = [str(tmp_path / f"input_{index:02d}.pdf") for index in range(1, 5)]
    run_inputs = {item["name"]: "sanitized fixture value" for item in fixture["run_input_contract"]}
    run_inputs["pdf_paths"] = paths
    before = {"schema_version": 2, "observed_sources": ["fixture:pages-1-1073"], "inline_outputs": [],
        "review_complete": 0, "review_recorded": 0, "write_receipts": [],
        "submitted_items": [], "submitted_revisions": []}
    after = {**before, "observed_sources": ["fixture:pages-1-1074"]}

    async def node_runner(node, _model, _prompt, _policy, member=None):
        calls.append(node.node_id)
        assert len(store.load(run_id)["node_states"]["N01"]["ReadAuditArtifact_calls"]) == 71
        assert '## 遇到问题时' in _prompt
        assert len(re.findall(r'不得|不要|不能|不应|不可|禁止|切勿', _prompt)) <= 8
        if len(calls) == 1:
            return ExpertRoleResult("1073 pages observed; one continuation remains", status="budget_exhausted",
                error_kind="segment_timeout", work_progress={"before": before, "after": after}, billable_tokens=1)
        decisions = store.load(run_id)["node_states"]["N01"]["scheduler_recovery"]["decisions"]
        assert any(row.get("action") == "continue_checkpoint" for row in decisions)
        return ExpertRoleResult("N01 finished after progress continuation", status="completed",
            handoff={"status": "completed", "summary": "N01 complete"})

    async def unused_runner(*_args):
        raise AssertionError("the fixture replay must execute through node_runner")

    result = await ExpertGroupOrchestrator(runner=unused_runner, node_runner=node_runner, event_sink=emit,
        run_store=store, recovery_handler=recovery, work_dir=tmp_path, archive_root=tmp_path / "archive").run(
            "Replay sanitized N01 progress checkpoint", _profile(), CATALOG, strategy="stable", run_id=run_id,
            flow=flow, checkpoint={"run_inputs": run_inputs})

    assert result.status == "completed"
    assert calls == ["N01", "N01"]
    assert not [event for event in events if event.get("type") == "ask_user"]
    assert not [event for event in events if event.get("tool") == "IterationRecovery"]
    assert store.load(run_id)["node_states"]["N01"]["scheduler_recovery"]["decisions"][0]["action"] == "continue_checkpoint"


@pytest.mark.asyncio
async def test_fix_rebase_applies_through_api_then_orchestrator_resumes(tmp_path, monkeypatch):
    import httpx
    from frontend import server
    from mewcode.orchestration.convert import to_expert_flow
    from mewcode.orchestration.maintenance import SCHEMA
    from mewcode.experts.node_ledger import ledger_for_run
    from tests.test_orchestration_maintenance import maintenance_env as setup_maintenance_env

    app, owner, _current, durable, spec = setup_maintenance_env.__wrapped__(tmp_path, monkeypatch)
    run_id = "d" * 32
    flow = to_expert_flow(spec)
    server.EXPERT_RUNS.create(run_id=run_id, conversation_id=owner["conversation_id"], task=spec.goal,
        attachments=[], profile={"name": "fixture profile"}, strategy="stable", flow=flow)
    initial = server.EXPERT_RUNS.mutate(run_id, lambda record: record.update(status="paused", stage_cursor="work",
        node_states={"first": {"status": "completed", "passed": True, "text": "first complete"},
                     "work": {"status": "failed", "error_kind": "capability_missing",
                              "handoff": {"status": "blocked"}, "text": "work checkpoint"},
                     "done": {"status": "pending"}}))
    durable.meta.dialogue_focus = {"kind": "orchestration", "run_id": run_id}
    maintenance = server.MAINTENANCE_STORE
    state = maintenance.open(owner["project"], owner["conversation_id"], spec.as_dict(), mode="fix", run_id=run_id)
    running = maintenance.begin(owner["project"], owner["conversation_id"], state["id"],
        revision=state["revision"], message="/fix：恢复 work 节点", base=spec.as_dict())
    ready = maintenance.complete(owner["project"], owner["conversation_id"], state["id"],
        call_id=running["call_id"], base=spec.as_dict(), run_snapshot=initial,
        response={"schema": SCHEMA, "reply": "工作节点已准备通过原接续边界重跑。",
            "proposal": {"summary": "恢复 work 节点", "run_actions": [
                {"action": "rebase_node", "node_id": "work", "reason": "保留已完成工作并恢复本节点"}]}})
    action = next(row for row in ready["proposal"]["run_actions"] if row["action"] == "rebase_node")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        applied = await client.post("/api/orchestration/maintenance/apply", json={**owner, "id": ready["id"],
            "revision": ready["revision"], "proposal_id": ready["proposal"]["id"], "confirmed_run_action_ids": []})
    assert applied.status_code == 200, applied.text
    body = applied.json()
    saved = server.EXPERT_RUNS.load(run_id)
    assert body["result"]["run_adopted"] is True
    assert action["requires_confirmation"] is False
    assert saved["node_states"]["work"]["status"] == "pending"
    assert saved["node_states"]["first"]["status"] == "completed"
    assert saved["status"] == "paused"

    resumed_flow = _flow(saved["locked_flow"])
    calls = []
    async def node_runner(node, _model, _prompt, _policy, member=None):
        calls.append(node.node_id)
        if node.node_id == "work":
            assert ledger_for_run(server.EXPERT_RUNS.load(run_id)).view("work").epoch == 1
        return ExpertRoleResult(node.node_id + " completed", status="completed",
            handoff={"status": "completed", "summary": node.node_id + " completed"})

    async def unused_runner(*_args):
        raise AssertionError("resumed /fix run must execute through node_runner")

    result = await ExpertGroupOrchestrator(runner=unused_runner, node_runner=node_runner,
        run_store=server.EXPERT_RUNS, work_dir=tmp_path, archive_root=tmp_path / "archive").run(
            spec.goal, _profile(), CATALOG, strategy="stable", run_id=run_id, flow=resumed_flow,
            checkpoint=saved)
    server.EXPERT_RUNS.transition(run_id, result.status)

    assert result.status == "completed", f"status={result.status}, calls={calls}, summary={result.final_summary}"
    assert calls == ["work", "done"]
    assert server.EXPERT_RUNS.load(run_id)["status"] == "completed"
    assert server.EXPERT_RUNS.load(run_id)["node_states"]["first"]["status"] == "completed"


def test_legacy_public_migration_is_host_only_and_exposes_epoch_and_assistance(tmp_path):
    flow = _flow({
        "flow_id": "legacy-migration",
        "version": 1,
        "label": "legacy-migration",
        "nodes": [{"node_id": "work", "role": "executor", "label": "Work"}],
        "edges": [],
        "finalizer_node_id": "work",
    })
    store = ExpertRunStore(tmp_path / "runs")
    run_id = "b" * 32
    store.create(run_id=run_id, conversation_id="offline", task="migrate legacy state",
                 attachments=[], profile={}, strategy="stable", flow=flow)
    legacy_assistance = [{"request_id": "assist-1", "next_step": "resume",
                          "actions": [{"tool": "ReadFile", "permission_decision": "allow"}]}]

    def make_legacy(snapshot):
        snapshot.pop("node_ledgers", None)
        stage = snapshot.setdefault("node_states", {}).setdefault("work", {})
        stage.update(status="needs_attention",
            handoff={"status": "blocked"}, assistance_receipts=legacy_assistance)
        stage["messages"] = [{"role": "assistant", "content": "checkpoint"}]

    store.mutate(run_id, make_legacy, durable=True)
    model_calls = []
    public = store.public(run_id)
    migrated = store.load(run_id)

    assert public["ledger_migration"]["model_calls"] == 0
    assert migrated["ledger_migration"]["requires_explicit_resume"] is True
    assert migrated["status"] == "paused"
    stage = public["node_states"]["work"]
    assert stage["epoch"] == 0
    assert stage["assistance_receipts"] == legacy_assistance
    assert model_calls == []
