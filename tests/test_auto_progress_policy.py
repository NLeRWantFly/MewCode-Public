"""Auto progress does not depend on an unused native boundary."""
import json
import os
from unittest.mock import AsyncMock

import pytest

from mewcode.permissions.modes import PermissionMode
from mewcode.permissions.network_policy import NetworkPolicy, task_network_scope
from mewcode.permissions.policy import build_permission_checker
from mewcode.sandbox.transaction_diagnostics import command_fingerprint, pending_command_context
from mewcode.tools.bash import Bash
from tests.test_transaction_diagnostics import stranded


@pytest.fixture
def checker(tmp_path, monkeypatch):
    monkeypatch.setattr("mewcode.sandbox.transaction_registry.default_root", lambda: tmp_path / "host")
    result = build_permission_checker(tmp_path)
    result.network_policy = NetworkPolicy(True)
    monkeypatch.setattr("mewcode.sandbox.srt_runtime.runtime_status",
                        lambda: {"ready": False, "state": "pending", "reason": "fixture unavailable"})
    monkeypatch.setattr("mewcode.sandbox.windows.native_runtime_ready", lambda: False)
    return result


def test_auto_host_is_selected_before_review_even_with_native_unavailable(checker):
    assert not checker.uses_native_commands()
    decision = checker.check(Bash(), {"command": "python analyze.py"})
    assert decision.effect == "review"
    assert decision.source != "network_policy"


@pytest.mark.parametrize("restriction", ["disabled", "blacklist", "discard", "readonly", "narrow", "strict"])
def test_explicit_restrictions_never_fall_back_to_host(checker, tmp_path, restriction):
    args = {"command": "python analyze.py"}
    if restriction == "disabled":
        checker.network_policy = NetworkPolicy(False)
    elif restriction == "blacklist":
        checker.network_policy = NetworkPolicy(True, ("example.test",))
    elif restriction == "discard":
        args["publication"] = "discard"
    elif restriction == "readonly":
        checker.native_publication_mode = "discard"
    elif restriction == "narrow":
        narrow = tmp_path / "narrow"
        narrow.mkdir()
        checker = build_permission_checker(tmp_path, parent_policy=checker, writable_scope=[narrow])
    else:
        checker.mode = PermissionMode.STRICT_ISOLATION
    # Windows native, strict Docker elsewhere; neither allows unisolated work.
    if restriction != "strict" or os.name == "nt":
        assert checker.uses_native_commands(args)
    assert checker.check(Bash(), args).effect in {"deny", "review"}


def test_whole_project_child_progress_and_repair_keep_existing_authority(checker, tmp_path):
    from mewcode.permissions.rules import Rule
    from mewcode.tools.write_file import WriteFile
    child = build_permission_checker(tmp_path, parent_policy=checker)
    assert not child.uses_native_commands()
    args = {"file_path": str(tmp_path / "repair.json"), "content": "{}"}
    assert child.check(WriteFile(), args).effect == "allow"
    child.rule_engine.append_session_rule(Rule("WriteFile", "*", "deny"))
    assert child.check(WriteFile(), args).effect == "deny"


def test_unknown_disposable_command_blocks_replay_but_reviews_independent_work(tmp_path, monkeypatch):
    workspace, registry, _, record = stranded(tmp_path, monkeypatch)
    registry.update(record["id"], "cleanup_unknown", command_sha256=command_fingerprint("python old.py"))
    checker = build_permission_checker(workspace)
    checker.network_policy = NetworkPolicy(True)
    before = registry.path.read_bytes()
    replay = checker.check(Bash(), {"command": "python old.py"})
    assert replay.effect == "deny" and replay.source == "unknown_command_replay"
    independent = checker.check(Bash(), {"command": "python independent.py"})
    assert independent.effect == "review"
    assert independent.risk["pending_isolated_operations"][0]["id"] == record["id"]
    assert registry.path.read_bytes() == before
    # A transaction that might have published still blocks the whole workspace.
    registry.update(record["id"], "cleanup_unknown", publication_mode="publish", metadata_broker_required=True)
    assert checker.check(Bash(), {"command": "python independent.py"}).effect == "deny"


@pytest.mark.parametrize("bound", [True, False])
def test_legacy_unknown_command_uses_bound_request_and_never_exports_script(tmp_path, monkeypatch, bound):
    import hashlib
    workspace, registry, private, record = stranded(tmp_path, monkeypatch)
    raw = json.dumps({"request_id": record["request_id"], "original_workspace": str(workspace),
                      "request": {"arguments": ["-Command", "host_bootstrap; python old.py; exit $LASTEXITCODE"]}}).encode()
    (private / "cleanup/request.bin").write_bytes(raw)
    registry.update(record["id"], "cleanup_unknown",
                    request_sha256=hashlib.sha256(raw).hexdigest() if bound else "c" * 64)
    context = pending_command_context(workspace, "python old.py")
    assert context["replays_unknown"] is bound
    assert context["items"][0]["original_command_known"] is bound
    assert "host_bootstrap" not in json.dumps(context) and "python old.py" not in json.dumps(context)


@pytest.mark.asyncio
async def test_doctor_reports_host_progress_without_claiming_native_ready(checker, tmp_path, monkeypatch):
    from mewcode.tools.runtime_doctor import RuntimeDoctor, RuntimeDoctorParams
    monkeypatch.setattr("mewcode.sandbox.native_recovery.diagnose_native_runtime",
                        lambda: {"ready": False, "reason": "fixture native unavailable"})
    with task_network_scope(checker):
        result = await RuntimeDoctor().run(RuntimeDoctorParams(), work_dir=str(tmp_path))
    report = json.loads(result.output)
    assert report["execution_backend"] == "host_reviewed" and report["commands_ready"]
    assert not report["native_ready"] and not report["ready"]


@pytest.mark.asyncio
@pytest.mark.parametrize("interactive", [True, False])
async def test_real_auto_dispatch_runs_once_after_review_without_native_fallback(checker, tmp_path, monkeypatch, interactive):
    from mewcode.agent import Agent
    from mewcode.conversation import ConversationManager
    from mewcode.permissions.reviewer import SafetyReviewer
    from mewcode.tools import create_default_registry
    from mewcode.tools.base import ToolCallComplete
    from tests.test_auto_action_lifecycle import execute
    reviewer = SafetyReviewer(object(), str(tmp_path))
    reviewer._ask_model = AsyncMock(return_value='{"outcome":"allow","rationale":"Requested local synthetic check."}')
    agent = Agent(object(), create_default_registry(), "anthropic", work_dir=str(tmp_path),
                  permission_checker=checker, safety_reviewer=reviewer)
    agent._current_conversation = ConversationManager()
    agent._current_conversation.add_user_message("Print the local synthetic check result.")
    native = AsyncMock(side_effect=AssertionError("default Auto must not try the native runner"))
    monkeypatch.setattr("mewcode.tools.command_sandbox.execute_native_command", native)
    command = "Write-Output 'mew-auto-ok'" if os.name == "nt" else "printf mew-auto-ok"
    result = await execute(agent, ToolCallComplete("auto-check", "Bash", {"command": command}), interactive)
    assert not result.is_error, result.output
    assert "mew-auto-ok" in result.output
    assert result.host_observation["command_execution"]["exit_code"] == 0
    reviewer._ask_model.assert_awaited_once()
    environment = reviewer._ask_model.await_args.kwargs["risk"]["host_execution_environment"]
    assert environment["backend_policy"] == "host_process"
    assert not environment["execution_confirmed"]
    native.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["disabled", "discard"])
async def test_selected_native_failure_does_not_retry_on_host(checker, tmp_path, monkeypatch, restriction):
    from mewcode.sandbox.receipt import NativeExecutionReceipt
    from mewcode.tools.base import ToolCallComplete
    from tests.test_auto_action_lifecycle import execute, setup_agent
    agent, reviewer, _ = setup_agent(tmp_path)
    agent.permission_checker = checker
    args = {"command": "Write-Output 'synthetic'"}
    if restriction == "disabled":
        checker.network_policy = NetworkPolicy(False)
    else:
        args["publication"] = "discard"
    monkeypatch.setattr("mewcode.sandbox.windows.native_runtime_ready", lambda: True)
    monkeypatch.setattr("mewcode.sandbox.srt_runtime.runtime_status", lambda: {"ready": True})
    native = AsyncMock(return_value=NativeExecutionReceipt("denied", command_started=False, detail="fixture unavailable"))
    host = AsyncMock(side_effect=AssertionError("native failure must not launch host process"))
    monkeypatch.setattr("mewcode.tools.command_sandbox.execute_native_command", native)
    monkeypatch.setattr("asyncio.create_subprocess_exec", host)
    result = await execute(agent, ToolCallComplete("restricted-check", "Bash", args), False)
    assert result.is_error
    native.assert_awaited_once()
    host.assert_not_awaited()


def test_network_capability_is_not_an_irreversible_business_choice(tmp_path):
    from mewcode.kernel.gates import GateKind, classify
    from tests.test_kernel_gates import setup
    store, run_id = setup(tmp_path)
    record = store.load(run_id)
    record["authorization_manifest"]["nodes"]["N03"] = {"network_hosts": ["example.test"]}
    assert classify(record, "N03") == GateKind.BUSINESS
    record["authorization_manifest"]["nodes"]["N03"]["external_targets"] = ["publish"]
    assert classify(record, "N03") == GateKind.IRREVERSIBLE


def test_review_outage_is_recoverable_but_not_manual_acceptance():
    from mewcode.kernel.blockers import policy
    from mewcode.kernel.collaboration_faults import ACCEPTANCE_BLOCKERS, TRANSIENT, REQUIRES_CLEARANCE, report_fault
    assert policy("safety_review_unavailable").category == "auto"
    assert "safety_review_unavailable" in TRANSIENT
    assert "safety_review_unavailable" not in REQUIRES_CLEARANCE
    report = {"execution_error": "provider_error", "checks": [
        {"evidence": {"execution_error": "safety_review_unavailable"}}]}
    assert report_fault(report) in ACCEPTANCE_BLOCKERS
