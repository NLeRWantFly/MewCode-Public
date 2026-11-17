import copy
import json
from dataclasses import replace

import pytest

from mewcode.expert_group import ExpertGroupOrchestrator, ExpertRoleResult
from mewcode.experts.attempt_memory import summarize_attempt, compact_tool_output, collect_verification_records
from mewcode.experts.audit_evidence import repair_instructions
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.experts.repair_domain import repair_domain
from tests.test_audit_protocol import flow_spec, checks
from tests.test_expert_flow import _profile


def retry_flow(action="retry"):
    return ExpertFlowSpec.from_value({"flow_id": "retry-memory", "label": "retry", "finalizer_node_id": "done",
        "nodes": [{"node_id": "work", "role": "executor", "label": "work",
                   "failure_policy": {"action": action, "max_attempts": 3}},
                  {"node_id": "done", "role": "manager", "label": "done"}],
        "edges": [{"from": "work", "to": "done"}]})


def failed_result():
    return ExpertRoleResult("saved partial work", status="failed",
        verification_records=[{"name": "Bash", "args": {"command": "pytest tests/test_api.py"},
                               "status": "failed", "output": "ModuleNotFoundError: unavailable_dependency"}],
        activity_records=[{"kind": "edit", "path": "src/api.py"}])


def test_attempt_memory_keeps_three_failures_actual_edits_and_log_tail():
    tools = {str(i): {"name": "Bash", "args": {"command": f"cmd-{i}"}, "status": "failed",
                     "is_error": True, "output": "header " + "x" * 9000 + f" failure-{i}"} for i in range(5)}
    tools["edit"] = {"name": "WriteFile", "status": "completed", "args": {"file_path": "src/api.py"}}
    tools["not-edited"] = {"name": "WriteFile", "status": "failed", "args": {"file_path": "not-written.py"}}
    result = summarize_attempt({"tools": tools, "status": "failed"})
    assert [item["command"] for item in result["failed_commands"]] == ["cmd-2", "cmd-3", "cmd-4"]
    assert result["failed_commands"][-1]["output"].endswith("failure-4")
    assert result["changed_files"] == ["src/api.py"]
    records = collect_verification_records(tools)
    assert records[-1]["is_error"] and records[-1]["output"].endswith("failure-4")
    assert len(records[-1]["output"]) <= 4000
    proof = compact_tool_output("VerifyCandidate", json.dumps({"exit_code": 1, "cwd": "candidate",
        "output": "first " + "x" * 12000 + " LAST_FAILURE", "diff": "d" * 8000, "changed_files": ["src/api.py"]}))
    parsed = json.loads(proof)
    assert parsed["output"].endswith("LAST_FAILURE") and len(parsed["diff"]) == 2000
    memory = summarize_attempt({"verification_records": [{"name": "VerifyCandidate", "output": proof}]})
    assert memory["failed_commands"][0]["output"].endswith("LAST_FAILURE")
    assert memory["changed_files"] == ["src/api.py"]


@pytest.mark.asyncio
async def test_retry_prompt_has_failure_command_status_edits_and_persists_memory(tmp_path):
    calls, events = [], []
    async def runner(node, model, prompt, policy, member=None):
        calls.append(node.node_id)
        if node.node_id == "work" and calls.count("work") == 1:
            return failed_result()
        if node.node_id == "work":
            for value in ("第 2 次尝试", "failed", "pytest tests/test_api.py", "unavailable_dependency", "src/api.py"):
                assert value in prompt
        return ExpertRoleResult("done")
    orchestrator = ExpertGroupOrchestrator(runner=lambda *_: None, node_runner=runner, archive_root=tmp_path,
        event_sink=lambda event: events.append(copy.deepcopy(event)))
    result = await orchestrator.run("task", _profile(), {}, strategy="stable", flow=retry_flow())
    assert result.status == "completed" and calls == ["work", "work", "done"]
    event = next(e for e in events if e["type"] == "expert_node_completed" and e["node_id"] == "work")
    assert event["attempt_history"][0]["changed_files"] == ["src/api.py"]
    assert event["verification_records"] == failed_result().verification_records


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [True, False])
async def test_resume_injects_saved_failure_without_supplemental_input(tmp_path, legacy):
    saved = {"status": "failed", "text": "earlier failure", "node_attempt": 1,
             "tools": {"bash": {"name": "Bash", "args": {"command": "old-pytest"}, "status": "failed", "output": "prior-stack"},
                       "edit": {"name": "EditFile", "args": {"file_path": "src/old.py"}, "status": "completed"}}}
    if not legacy:
        saved["attempt_history"] = [summarize_attempt(saved)]
        saved.pop("tools")
    async def runner(node, model, prompt, policy, member=None):
        if node.node_id == "work":
            assert policy.resume_messages
            assert "old-pytest" in prompt and "prior-stack" in prompt and "src/old.py" in prompt
        return ExpertRoleResult("done")
    result = await ExpertGroupOrchestrator(runner=lambda *_: None, node_runner=runner, archive_root=tmp_path).run(
        "task", _profile(), {}, strategy="stable", flow=retry_flow(),
        checkpoint={"node_states": {"work": saved}, "node_attempts": {"work": 1}})
    assert result.status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [None, 1500, 90000])
@pytest.mark.parametrize("quantified", [False, True])
@pytest.mark.parametrize("second", ["missing", "valid", "provider_failure"])
async def test_missing_verdict_retries_only_auditor_once_then_stops(tmp_path, monkeypatch, budget, quantified, second):
    from mewcode import expert_group
    original_policy = expert_group.derive_execution_policy
    monkeypatch.setattr(expert_group, "derive_execution_policy",
        lambda multi_agent: replace(original_policy(multi_agent), token_budget=budget))
    source = tmp_path / "project"
    source.mkdir()
    (source / "paper.txt").write_text("unchanged", encoding="utf-8")
    calls, contracts, events = [], [], []
    async def runner(node, model, prompt, policy, member=None):
        calls.append(node.node_id)
        assert node.node_id != "repair", "missing verdict is not an executor defect"
        if node.node_id == "done":
            return ExpertRoleResult("done")
        contracts.append(copy.deepcopy(policy.audit_contract))
        if calls.count("audit") == 1:
            return ExpertRoleResult("audit prose but no verdict", status="incomplete_audit")
        assert "审计协议补交" in prompt and "你必须调用 SubmitVerdict" in prompt
        assert policy.max_iterations <= 4 and policy.timeout_seconds <= 120
        assert policy.token_budget == budget  # Retry inherits explicit limits; no implicit 12,000 cap.
        if second == "missing":
            return ExpertRoleResult("still no verdict", status="incomplete_audit")
        if second == "provider_failure":
            return ExpertRoleResult("provider failed", status="failed")
        verdict = {"verdict": "PASS", "issues": []}
        if quantified:
            contract = policy.audit_contract
            verdict["assessment"] = {"rubric_hash": contract["rubric"]["rubric_hash"],
                                     "artifact_hash": contract["artifact_hash"], "checks": checks("PASS")}
        return ExpertRoleResult("audit pass", verdict=verdict)
    result = await ExpertGroupOrchestrator(runner=lambda *_: None, node_runner=runner,
        archive_root=tmp_path / "archive", work_dir=source if quantified else None,
        event_sink=lambda e: events.append(e)).run("task", _profile(), {}, strategy="stable", flow=flow_spec())
    assert calls.count("audit") == 2 and calls.count("repair") == 0
    assert (result.status == "completed") == (second == "valid")
    assert contracts[0] == contracts[1]
    assert sum(e.get("phase") == "audit_protocol_retry" for e in events) == 1


@pytest.mark.asyncio
async def test_routed_repair_gets_previous_executor_failure_evidence_and_plan(tmp_path):
    flow = ExpertFlowSpec.from_value({"flow_id": "route-memory", "label": "route", "finalizer_node_id": "done",
        "nodes": [{"node_id": "plan", "role": "planner", "label": "plan"},
                  {"node_id": "work", "role": "executor", "label": "work"},
                  {"node_id": "audit", "role": "auditor", "label": "audit", "failure_policy": {
                      "action": "route", "target_node_id": "work", "max_attempts": 2}},
                  {"node_id": "done", "role": "manager", "label": "done"}],
        "edges": [{"from": "plan", "to": "work"}, {"from": "work", "to": "audit"}, {"from": "audit", "to": "done"}]})
    calls = []
    async def runner(node, model, prompt, policy, member=None):
        calls.append(node.node_id)
        if node.node_id == "plan":
            return ExpertRoleResult("ORIGINAL_PLAN")
        if node.node_id == "work":
            if calls.count("work") == 1:
                result = failed_result()
                result.status = "completed"  # Implementation finished, but one verification is failing.
                return result
            assert "ORIGINAL_PLAN" in prompt and "src/api.py" in prompt and "unavailable_dependency" in prompt
            assert not policy.resume_messages
        if node.node_id == "audit":
            passed = calls.count("audit") > 1
            return ExpertRoleResult("audit", verdict={"verdict": "PASS" if passed else "NEEDS_ATTENTION",
                "issues": [] if passed else [{"issue_id": "A", "description": "test fails"}]})
        return ExpertRoleResult("done")
    result = await ExpertGroupOrchestrator(runner=lambda *_: None, node_runner=runner, archive_root=tmp_path).run(
        "task", _profile(), {}, strategy="stable", flow=flow)
    assert result.status == "completed" and calls.count("work") == 2


@pytest.mark.parametrize("task,files,domain", [("修复 API 失败测试", ["readme.pdf", "app.py"], "code"),
    ("更新论文图注", ["generate.py", "paper.docx"], "document"), ("", ["src/app.ts"], "code"),
    ("修复编排循环", ["docs/reference.pdf", "pyproject.toml", "loop.py"], "code"),
    ("", ["image.png"], "visual"), ("整理材料", ["notes.txt"], "general")])
def test_repair_prompts_select_domain_without_visual_bias(task, files, domain):
    assert repair_domain(task, files) == domain
    prompt = repair_instructions(candidate_dir="candidate", original_dir="original", rubric={}, accepted={},
        issues=[], step="modify", delivered_evidence=[], previous_attempt={}, max_iterations=20, domain=domain)
    if domain == "code":
        assert "失败测试" in prompt and "最小修改" in prompt and "VerifyCandidate" in prompt and "diff" in prompt
    if domain in {"code", "general"}:
        assert all(value not in prompt for value in ("Word", "LaTeX", "PIXEL_SHA256", "图注"))
    else:
        assert "ReadAuditArtifact" in prompt and "Word/LaTeX" in prompt


@pytest.mark.asyncio
async def test_web_provider_error_preserves_verification_memory_in_next_retry_and_disk(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from mewcode import expert_web
    from mewcode.agent import StreamText, ToolUseEvent, ToolResultEvent
    from mewcode.client import LLMError
    from mewcode.conversation import ConversationManager
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.experts.flow import ExpertFlowStore
    from mewcode.orchestration.plan_session import PlanSessionStore

    source = tmp_path / "project"
    source.mkdir()
    runs, flows = ExpertRunStore(tmp_path / "runs"), ExpertFlowStore(tmp_path / "flows.json")
    flows.put(source, retry_flow())
    calls, events = [], []

    class FakeAgent:
        client = SimpleNamespace(expose_reasoning=False)
        total_input_tokens = total_output_tokens = 1

        def set_review_authorization_context(self, context):
            self.node_id = context.split("STAGE: ", 1)[1].split(" ", 1)[0]

        async def run(self, conversation):
            calls.append(self.node_id)
            if self.node_id == "work" and calls.count("work") == 1:
                yield ToolUseEvent("EditFile", "edit", {"file_path": "src/api.py"})
                yield ToolResultEvent("edit", "EditFile", "saved", False, .1)
                yield ToolUseEvent("Bash", "check", {"command": "pytest tests/test_api.py"})
                yield ToolResultEvent("check", "Bash", "header " + "x" * 8000 + " LAST_STACK_FAILURE", True, .1)
                raise LLMError("provider connection lost")
            if self.node_id == "work":
                prompt = str(conversation.get_messages())
                assert "LAST_STACK_FAILURE" in prompt and "src/api.py" in prompt and "pytest tests/test_api.py" in prompt
            conversation.add_assistant_message("done")
            yield StreamText(text="done")

    def build(*args):
        return FakeAgent(), None, SimpleNamespace(conversation=ConversationManager(), aclose=AsyncMock())
    monkeypatch.setattr(expert_web, "_build_role_agent", build)
    session = SimpleNamespace(work_dir=str(source), conversation_id="memory", multi_agent=False,
        session=SimpleNamespace(meta=SimpleNamespace(message_count=0), append=lambda *args: None,
                                append_expert_run=lambda *args, **kwargs: None),
        conversation=ConversationManager(), expert_run_id="", expert_control_action="pause",
        context=SimpleNamespace(get=lambda key, default=None: default, slots=SimpleNamespace(resolve=lambda key: {})),
        task_manager=SimpleNamespace(list_tasks=lambda: [], mark_notified=lambda ids: None),
        expert_child_event_callback=None, status_payload=lambda: {}, tasks_payload=lambda: [])
    async def send(event):
        events.append(copy.deepcopy(event))
    await asyncio.wait_for(expert_web.run_expert_chat(SimpleNamespace(send_json=send), "fix code", session=session,
        confirmed_new_flow=flows.get(source),
        registry_service=SimpleNamespace(catalog=AsyncMock(return_value={"providers": []}), resolve=lambda model: None),
        profile_store=SimpleNamespace(get=lambda _: _profile()), flow_store=flows,
        plan_store=PlanSessionStore(tmp_path / "plans.json"), run_store=runs), 10)
    snapshot = runs.load(next(event["run_id"] for event in events if event.get("run_id")))
    assert snapshot["status"] == "completed" and calls == ["work", "work", "done"]
    memory = snapshot["node_states"]["work"]["attempt_history"][0]
    assert memory["status"] == "failed" and memory["changed_files"] == ["src/api.py"]
    assert memory["failed_commands"][0]["output"].endswith("LAST_STACK_FAILURE")
    assert snapshot["test_records"][0]["output"].endswith("LAST_STACK_FAILURE")
