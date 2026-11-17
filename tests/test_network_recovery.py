"""Host-side recovery boundaries for network policy failures and SRT acceptance."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.permissions.network_policy import NetworkDenied, NetworkPolicyStore
from mewcode.sandbox import srt_runtime
from mewcode.tools.base import ToolCallComplete
from tests.test_auto_action_lifecycle import setup_agent
from tests.test_scheduler_recovery import controller, decide


async def _record_network_denial(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    agent, _, _ = setup_agent(tmp_path)
    policy_store = NetworkPolicyStore(tmp_path / "sandbox-network.json")
    agent.permission_checker.network_policy_store = policy_store
    policy = policy_store.save(
        {"enabled": True, "denied_domains": ["blocked.example"]},
        expected_revision=policy_store.read().revision,
    )
    tool = agent.registry.get("Browser")
    tool.run = AsyncMock(side_effect=NetworkDenied("NETWORK_BLACKLISTED: blocked.example"))
    call = ToolCallComplete("network-call", "Browser", {"url": "https://blocked.example/"})
    result = await agent._run_recorded_tool(tool, {}, call)
    return result, policy_store, policy


def _seed_network_denial(run_store, run_id, result):
    def seed(record):
        stage = record["node_states"]["extract"]
        stage.setdefault("tools", {})["network-call"] = {
            "tool_id": "network-call",
            "name": "Browser",
            "args": {"url": "https://blocked.example/"},
            "status": "not_started",
            "is_error": True,
            "output": result.output,
            "host_observation": result.host_observation,
        }

    run_store.mutate(run_id, seed, durable=True)


@pytest.mark.asyncio
async def test_recorded_network_denial_is_structured_and_never_effect_unknown(tmp_path):
    result, _, policy = await _record_network_denial(tmp_path)

    assert result.is_error
    assert "NETWORK_BLACKLISTED" in result.output
    assert not result.output.startswith("Tool execution error:")
    assert result.host_observation["automatic_recovery"] is False
    assert result.host_observation["network_policy"]["revision"] == policy.revision
    assert result.host_observation["command_execution"]["execution_condition"]["automatic_recovery"] is False


@pytest.mark.asyncio
async def test_same_revision_network_denial_stops_before_case_or_model_recovery(tmp_path, monkeypatch):
    denial, policy_store, _ = await _record_network_denial(tmp_path / "agent")
    handler, flow, _, run_store, primary, reviewer, _ = controller(tmp_path / "scheduler", monkeypatch)
    handler.session.agent.permission_checker = SimpleNamespace(network_policy_store=policy_store)
    _seed_network_denial(run_store, handler.run_id, denial)
    case_runner = SimpleNamespace(run=AsyncMock(return_value={"action": "case"}))
    handler.case_runner = case_runner
    account = AsyncMock()

    decision = await decide(handler, flow, account=account)

    assert decision["action"] == "stop"
    assert decision["source"] == "network_settings"
    case_runner.run.assert_not_awaited()
    account.assert_not_awaited()
    assert not primary.calls and not reviewer.calls


@pytest.mark.asyncio
async def test_changed_network_revision_allows_normal_checkpoint_recovery(tmp_path, monkeypatch):
    denial, policy_store, policy = await _record_network_denial(tmp_path / "agent")
    handler, flow, _, run_store, primary, reviewer, _ = controller(tmp_path / "scheduler", monkeypatch)
    handler.session.agent.permission_checker = SimpleNamespace(network_policy_store=policy_store)
    _seed_network_denial(run_store, handler.run_id, denial)
    case_runner = SimpleNamespace(run=AsyncMock(return_value={"action": "case"}))
    handler.case_runner = case_runner

    policy_store.save(
        {"enabled": True, "denied_domains": ["other.example"]},
        expected_revision=policy.revision,
    )
    decision = await decide(handler, flow)

    assert decision["action"] == "diagnose_path"
    assert decision["source"] == "recovery_controller"
    case_runner.run.assert_not_awaited()
    assert not primary.calls and not reviewer.calls


def test_failed_last_srt_check_for_same_identity_is_explicit_and_not_ready(tmp_path, monkeypatch):
    from mewcode.sandbox import native_gate
    monkeypatch.setattr(native_gate, 'native_gate_status', lambda: SimpleNamespace(ready=True))
    monkeypatch.setattr(srt_runtime, "cleanup_blocked", lambda: False)
    identity = "synthetic-srt-identity"
    monkeypatch.setattr(srt_runtime, "runtime_identity", lambda: identity)
    monkeypatch.setattr(srt_runtime, "user_data_root", lambda: tmp_path)
    cases = {name: True for name in srt_runtime.REQUIRED_CASES}
    cases["blacklist_domain"] = False
    old_success = {name: True for name in srt_runtime.REQUIRED_CASES}
    (tmp_path / "srt-network-acceptance.json").write_text(json.dumps({
        "schema": "mewcode-srt-acceptance-v1",
        "identity": identity,
        "check_id": "older-success",
        "cases": old_success,
    }), encoding="utf-8")
    (tmp_path / "srt-network-last-check.json").write_text(json.dumps({
        "schema": "mewcode-srt-acceptance-v1",
        "identity": identity,
        "check_id": "newer-failure",
        "cases": cases,
    }), encoding="utf-8")

    status = srt_runtime.runtime_status()

    assert status["ready"] is False
    assert status["state"] == "acceptance_failed"
    assert status["failed_checks"] == ["blacklist_domain"]
    assert "NETWORK_RUNTIME_UNAVAILABLE" in status["reason"]


def test_network_ready_cannot_hide_invalid_base_runtime_gate(monkeypatch):
    from mewcode.sandbox import native_gate
    monkeypatch.setattr(native_gate, 'native_gate_status', lambda: SimpleNamespace(
        ready=False, state=native_gate.NativeGateState.INVALID, reason='bundle changed after attestation'))
    def forbidden_identity():
        raise AssertionError('network proof must not override the base gate')
    monkeypatch.setattr(srt_runtime, 'runtime_identity', forbidden_identity)
    status = srt_runtime.runtime_status()
    assert status['ready'] is False
    assert status['native_gate_state'] == 'invalid'
    assert 'bundle changed after attestation' in status['reason']
