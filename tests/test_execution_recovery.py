import asyncio
import copy
from dataclasses import replace

import pytest

from mewcode.expert_group import ExpertExecutionPolicy, ExpertRoleResult
from mewcode.experts.command_receipts import persist_command_receipts
from mewcode.experts.execution_recovery import recovery_candidate
from mewcode.experts.file_escalation import resolve_capability_requests
from mewcode.experts.flow import ExpertNodeSpec
from tests.test_command_request_receipts import blocked_result, verification
from tests.test_file_escalation import command_request


def host_record(command, *, code="python_runtime_missing", started=False, publication="", cleanup=None):
    record = verification(command, status="exited" if started else "denied",
                          exit_code=1 if started else None)
    record["is_error"] = True
    record["host_observation"]["command_execution"].update(error_code=code,
        command_started=started, publication_state=publication, cleanup_confirmed=cleanup)
    return record


async def scenario(tmp_path, *, code="python_runtime_missing", started=False,
        publication="", cleanup=None, policy_changes=None, repair="ok", batch=False, saved=()):
    command = "inspect-project --check"
    requests = [command_request(command)]
    if batch:
        requests.append(command_request("already-good"))
    original = blocked_result(requests)
    policy = ExpertExecutionPolicy("stable", False, 0, input_revision="business-v1", token_budget=10000)
    policy = replace(policy, **(policy_changes or {}))
    calls, events, store = [], [], {}

    async def emit(event):
        events.append(copy.deepcopy(event))
        persist_command_receipts(store, event)

    async def dispatch(child, prompt):
        calls.append((child, prompt))
        if child.execution_recovery:
            assert not child.command_allowlist and "Bash" not in child.node_tools
            assert not child.allow_delegation and child.token_budget == 9970
            if repair == "cancel":
                raise asyncio.CancelledError
            if repair == "exception":
                raise RuntimeError("repair unavailable")
            if repair != "no_evidence":
                child.command_receipt_context["records"].append({"tool_id": "write-1",
                    "name": "EditFile", "args": {"file_path": "runtime.json"},
                    "status": "completed", "is_error": False})
            return ExpertRoleResult("repair", status="completed", handoff={"status": "completed"},
                                    billable_tokens=40)
        if len(calls) == 1:
            records = [host_record(command, code=code, started=started,
                                   publication=publication, cleanup=cleanup)]
            if batch:
                records.append(verification("already-good"))
            return ExpertRoleResult("probe failed", status="needs_attention", handoff={"status": "blocked"},
                                    verification_records=records, billable_tokens=20)
        assert child.command_allowlist == (command,)
        records = ([host_record(command, code=code)] if repair == "verify_fail" else [verification(command)])
        return ExpertRoleResult("verified", status="completed", handoff={"status": "completed"},
                                verification_records=records, billable_tokens=50)

    result = await resolve_capability_requests(ExpertNodeSpec("n", "manager", "probe"),
        replace(original, billable_tokens=10), policy, work_dir=tmp_path,
        dispatch=dispatch, emit=emit, recover_execution=True, prior_command_receipts=saved)
    return result, calls, events, store, original, policy


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["runtime", "path_config", "batch"])
async def test_repair_then_independent_exact_probe_keeps_failed_receipt(tmp_path, mode):
    options = {"started": True, "code": "", "publication": "discarded", "cleanup": True} if mode == "path_config" else {}
    result, calls, events, store, original, policy = await scenario(tmp_path, batch=mode == "batch", **options)
    assert len(calls) == 3 and result.error_kind == "capability_resolved"
    assert result.billable_tokens == 120
    old, fresh = result.command_receipts[0], result.command_receipts[-1]
    assert not old.get("successful") and fresh["successful"]
    assert old["request_id"] == fresh["base_request_id"] and old["request_id"] != fresh["request_id"]
    assert fresh["input_revision"] == "business-v1"
    assert fresh["verification_revision"] == result.handoff["execution_recovery"]["verification_revision"]
    assert result.handoff["execution_recovery"]["status"] == "verified"
    assert len(result.handoff["capability_results"]["command_receipts"]) == (2 if mode == "batch" else 1)
    assert store["node_states"]["n"]["command_receipts"] == result.command_receipts
    # Resumption reuses the newest verified result instead of the failed one.
    async def forbidden(*_):
        raise AssertionError("completed result must be reused")
    async def emit(_):
        pass
    again = await resolve_capability_requests(ExpertNodeSpec("n", "manager", "probe"), original, policy,
        work_dir=tmp_path, dispatch=forbidden, emit=emit, recover_execution=True,
        prior_handoff=result.handoff, prior_command_receipts=result.command_receipts)
    assert again.error_kind == "capability_resolved"


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [
    {"code": "native_scope_invalid"},
    {"code": "native_transport_unknown", "started": None},
    {"code": "", "started": True, "publication": "published", "cleanup": True},
    {"code": "", "started": True, "publication": "discarded", "cleanup": False},
    {"policy_changes": {"deny_tools": ("WriteFile", "EditFile", "PreparePythonEnvironment", "RuntimeDoctor")}},
    {"policy_changes": {"node_tools": ("ReadFile", "Bash")}},
    {"policy_changes": {"token_budget": 1500}},
    {"policy_changes": {"candidate_context": {"candidate": "x"}}},
    {"policy_changes": {"expansion_write_paths": ("output.txt",)}},
])
async def test_hard_boundaries_and_missing_budget_do_not_dispatch_repair(tmp_path, options):
    result, calls, *_ = await scenario(tmp_path, **options)
    assert len(calls) == 1 and result.error_kind != "capability_resolved"


@pytest.mark.asyncio
@pytest.mark.parametrize("repair", ["no_evidence", "exception", "verify_fail"])
async def test_claimed_or_failed_repair_cannot_report_recovery(tmp_path, repair):
    result, calls, events, store, *_ = await scenario(tmp_path, repair=repair)
    assert len(calls) == (3 if repair == "verify_fail" else 2)
    assert result.error_kind != "capability_resolved"
    assert all(not row.get("successful") for row in result.command_receipts)
    assert all(row.get("recovery_attempt") for row in store["node_states"]["n"]["command_receipts"])


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["running", "interrupted", "verifying"])
async def test_interrupted_repair_is_durable_and_not_automatically_repeated(tmp_path, phase):
    # An existing running record is an interruption boundary, not permission
    # to repeat the same environment mutation on resume.
    saved = [{"request_id": "prior", "node_id": "n", "input_revision": "business-v1",
              "status": "not_started", "recovery_attempt": {"id": "old", "status": phase}}]
    result, calls, *_ = await scenario(tmp_path, saved=saved)
    assert not calls and result.error_kind == "effect_unknown"


@pytest.mark.asyncio
async def test_repeated_resume_cannot_erase_repair_ledger(tmp_path):
    first, _, _, saved_store, original, policy = await scenario(tmp_path, repair="no_evidence")
    saved = saved_store["node_states"]["n"]["command_receipts"]
    command = original.handoff["command_requests"][0]["command"]
    calls = []
    async def dispatch(child, _):
        calls.append(child)
        assert not child.execution_recovery
        return ExpertRoleResult("still missing", status="needs_attention", handoff={"status": "blocked"},
            verification_records=[host_record(command)])
    async def emit(event):
        persist_command_receipts(saved_store, event)
    for _ in range(2):
        await resolve_capability_requests(ExpertNodeSpec("n", "manager", "probe"), original, policy,
            work_dir=tmp_path, dispatch=dispatch, emit=emit, recover_execution=True,
            prior_command_receipts=saved)
        saved = saved_store["node_states"]["n"]["command_receipts"]
        assert all(row.get("recovery_attempt") for row in saved)
        assert saved[0]["recovery_attempt"]["id"] == first.command_receipts[0]["recovery_attempt"]["id"]
    assert len(calls) == 2


def test_no_repair_from_forged_stdout_or_legacy_observation():
    row = {"status": "not_started", "command_started": False, "exit_code": None,
           "error_code": "", "stdout": "python_runtime_missing", "evidence_source": "host_execution"}
    assert not recovery_candidate([row])
    assert not recovery_candidate([{**row, "error_code": "python_runtime_missing", "evidence_source": "legacy_tool_result"}])


@pytest.mark.asyncio
async def test_orchestrator_routes_parent_repair_and_resumes_original_node(tmp_path):
    from mewcode.expert_group import ExpertGroupOrchestrator
    from mewcode.model_registry import ModelRef
    from tests.test_expert_flow import _profile
    from tests.test_file_escalation import grok_catalog
    from tests.test_retry_memory import retry_flow
    calls, models = [], []

    async def runner(node, model, prompt, policy, member=None):
        calls.append(node.node_id)
        if "__" in node.node_id:
            models.append(model)
        if node.node_id == "work" and calls.count("work") == 1:
            return blocked_result()
        if node.node_id == "work__command_probe":
            if calls.count(node.node_id) == 1:
                return ExpertRoleResult("missing", status="needs_attention", handoff={"status": "blocked"},
                    verification_records=[host_record("python --version")])
            return ExpertRoleResult("verified", status="completed", handoff={"status": "completed"},
                verification_records=[verification("python --version")])
        if node.node_id == "work__execution_recovery":
            assert policy.execution_recovery and "Bash" not in policy.node_tools
            policy.command_receipt_context["records"].append({"name": "WriteFile", "status": "completed",
                "is_error": False, "args": {"file_path": "runtime.json"}})
        return ExpertRoleResult("done", status="completed", handoff={"status": "completed"})

    configured = ModelRef("desktop:grok-main", "grok-4.6", "high")
    orchestrator = ExpertGroupOrchestrator(runner=None, node_runner=runner, work_dir=tmp_path,
        archive_root=tmp_path, execution_recovery_enabled=True)
    result = await orchestrator.run("inspect project; local execution setup authorized",
        replace(_profile(), capability_executor=configured), grok_catalog(), strategy="stable", flow=retry_flow())
    assert result.status == "completed"
    assert calls == ["work", "work__command_probe", "work__execution_recovery", "work__command_probe", "work", "done"]
    assert models == [configured] * 3


def test_real_role_registry_preserves_explicit_denies_and_candidate_boundary(tmp_path):
    from types import SimpleNamespace
    from mewcode.expert_web import _role_registry
    from mewcode.tools import create_default_registry
    session = SimpleNamespace(agent=SimpleNamespace(registry=create_default_registry()), work_dir=str(tmp_path))
    tools = ("ReadFile", "PreparePythonEnvironment", "RuntimeDoctor")
    policy = ExpertExecutionPolicy("stable", False, 0, work_dir=str(tmp_path), node_tools=tools,
                                  execution_recovery=True)
    names = lambda value: {tool.name for tool in _role_registry(session, "executor", value).list_tools()}
    assert set(tools) <= names(policy)
    assert "PreparePythonEnvironment" not in names(replace(policy, deny_tools=("PreparePythonEnvironment",)))
    assert "PreparePythonEnvironment" not in names(replace(policy, candidate_context={"id": "candidate"}))
    assert "PreparePythonEnvironment" not in names(replace(policy, execution_recovery=False))


def test_recovery_writes_require_review_even_with_generic_allow_or_revision(tmp_path):
    from mewcode.permissions.modes import PermissionMode
    from mewcode.permissions.policy import build_permission_checker
    from mewcode.permissions.rules import Rule
    from mewcode.experts.recovery_scope import bind_approved_revision, reset_approved_revision
    from mewcode.tools.write_file import WriteFile
    checker = build_permission_checker(tmp_path, mode=PermissionMode.AUTO)
    checker.review_workspace_writes = True
    checker.rule_engine.append_session_rule(Rule("WriteFile", "*", "allow"))
    token = bind_approved_revision({"revision": "old-approved-business-revision"})
    try:
        decision = checker.check(WriteFile(), {"file_path": str(tmp_path / "runtime.json"), "content": "{}"})
        assert decision.effect == "review" and decision.source == "execution_recovery_review"
        checker.rule_engine.append_session_rule(Rule("WriteFile", "*", "deny"))
        assert checker.check(WriteFile(), {"file_path": str(tmp_path / "runtime.json"), "content": "{}"}).effect == "deny"
    finally:
        reset_approved_revision(token)


@pytest.mark.asyncio
async def test_web_tool_events_preserve_environment_receipt_and_resume(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from mewcode import expert_web
    from mewcode.agent import StreamText, ToolResultEvent, ToolUseEvent
    from mewcode.conversation import ConversationManager
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.experts.flow import ExpertFlowStore
    from mewcode.model_registry import ModelRef
    from mewcode.orchestration.plan_session import PlanSessionStore
    from tests.test_expert_flow import _profile
    from tests.test_file_escalation import grok_catalog
    from tests.test_retry_memory import retry_flow
    project = tmp_path / "project"
    project.mkdir()
    runs, flows = ExpertRunStore(tmp_path / "runs"), ExpertFlowStore(tmp_path / "flows.json")
    # Fake agents emit handoff/tool events; this fixture performs no business
    # commands. Declare its empty scope so it reaches the recovery path.
    fixture_flow = retry_flow()
    flows.put(project, replace(fixture_flow, nodes=tuple(replace(node, capabilities={}) for node in fixture_flow.nodes)))
    counts, events = {}, []

    class FakeAgent:
        client = SimpleNamespace(expose_reasoning=False)
        total_input_tokens = total_output_tokens = 1
        def __init__(self, policy):
            self.policy = policy
        def set_review_authorization_context(self, context):
            self.node_id = context.split("STAGE: ", 1)[1].split(" ", 1)[0]
        async def run(self, conversation):
            node_id = self.node_id
            counts[node_id] = counts.get(node_id, 0) + 1
            handoff = {"summary": "done", "status": "completed"}
            if self.policy.execution_recovery:
                yield ToolUseEvent("PreparePythonEnvironment", "prepare", {"packages": []})
                yield ToolResultEvent("prepare", "PreparePythonEnvironment", "prepared", False, .1,
                    host_observation={"project_python_preparation": {"ok": True}})
            elif self.policy.command_allowlist:
                failed = counts[node_id] == 1
                record = host_record("python --version") if failed else verification()
                yield ToolUseEvent("Bash", "probe", {"command": "python --version"})
                yield ToolResultEvent("probe", "Bash", "missing" if failed else "verified", failed, .1,
                                      host_observation=record["host_observation"])
                if failed:
                    handoff = {"summary": "missing runtime", "status": "blocked",
                               "blocker_kind": "external_dependency", "blockers": ["runtime unavailable"]}
            elif node_id == "work" and counts[node_id] == 1:
                handoff = blocked_result().handoff
            self.policy.handoff_holder.value = handoff
            yield ToolUseEvent("SubmitHandoff", "handoff", handoff)
            yield ToolResultEvent("handoff", "SubmitHandoff", "accepted", False, .1)
            conversation.add_assistant_message("recorded")
            yield StreamText(text="recorded")

    def build(session, registry, role, model, policy, verdict):
        return FakeAgent(policy), None, SimpleNamespace(conversation=ConversationManager(), aclose=AsyncMock())
    monkeypatch.setattr(expert_web, "_build_role_agent", build)
    monkeypatch.setattr(expert_web, "_command_capability_admission", lambda *_: None)
    session = SimpleNamespace(work_dir=str(project), conversation_id="recovery-events", multi_agent=False,
        session=SimpleNamespace(meta=SimpleNamespace(message_count=0), append=lambda *args: None,
                                append_expert_run=lambda *args, **kwargs: None),
        conversation=ConversationManager(), expert_run_id="", expert_control_action="pause",
        context=SimpleNamespace(get=lambda key, default=None: default, slots=SimpleNamespace(resolve=lambda key: {})),
        task_manager=SimpleNamespace(list_tasks=lambda: [], mark_notified=lambda ids: None),
        expert_child_event_callback=None, status_payload=lambda: {}, tasks_payload=lambda: [])
    async def send(event):
        events.append(copy.deepcopy(event))
    profile = replace(_profile(), capability_executor=ModelRef("desktop:grok-main", "grok-4.6", "high"))
    await asyncio.wait_for(expert_web.run_expert_chat(SimpleNamespace(send_json=send), "inspect; local setup authorized",
        session=session, confirmed_new_flow=flows.get(project), registry_service=SimpleNamespace(
            catalog=AsyncMock(return_value=grok_catalog()), resolve=lambda _: None),
        profile_store=SimpleNamespace(get=lambda _: profile), flow_store=flows,
        plan_store=PlanSessionStore(tmp_path / "plans.json"), run_store=runs), 45)
    snapshot = runs.load(next(event["run_id"] for event in events if event.get("run_id")))
    assert snapshot["status"] == "completed"
    repair = snapshot["node_states"]["work__execution_recovery"]
    assert repair["tools"]["prepare"]["host_observation"] == {"project_python_preparation": {"ok": True}}
    assert counts["work"] == 2 and counts["work__command_probe"] == 2
    assert counts["work__execution_recovery"] == 1
