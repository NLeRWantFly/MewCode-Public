import pytest
from dataclasses import dataclass

from mewcode.experts.command_receipts import (
    command_effect_fingerprint,
    request_receipts,
    resolve_unknown_receipt,
)
from mewcode.experts.effect_verification import verify_effect_observations
from mewcode.file_fingerprints import content_digest
from mewcode.experts.node_ledger import ledger_for_run
from mewcode.experts.pending_commands import restore_pending_commands
from mewcode.sandbox.execution_conditions import classify_command_effect
from mewcode.tools.read_file import Params as ReadFileParams, ReadFile


def _record(command='python -c "import json; print(json.__name__)"', effect=None):
    node_id, revision = "node", "revision-1"
    request = {"command": command, "input_scope": [], "forbidden_changes": [],
               "verification_command": command, "declared_write_paths": []}
    row = request_receipts(node_id, revision, [request], status="outcome_unknown", epoch=0)[0]
    row["effect_class"] = effect or classify_command_effect(request)
    record = {"node_states": {node_id: {"input_revision": revision,
                                         "command_receipts": [row]}}, "node_ledgers": {}}
    ledger = ledger_for_run(record)
    ledger.record(node_id, "receipt", row, source="host", identity=["request", row["request_id"]])
    ledger.record(node_id, "blocker", {"kind": "unknown_effect", "request_id": row["request_id"],
                                        "ref": row["request_id"]}, source="host",
                  identity=["unknown", row["request_id"]])
    return record, node_id, row


async def _read_file_observation(path):
    result = await ReadFile().execute(ReadFileParams(file_path=str(path)))
    return {"name": "ReadFile", "tool_id": "read-1",
        "args": {"file_path": str(path)}, "is_error": result.is_error,
        "output": result.output, "host_observation": result.host_observation}


def _verification_fact(record, node_id, row, observation, *, assistance="assist-1"):
    return ledger_for_run(record).record(node_id, "receipt", {
        "effect_verification_observation": True,
        "request_id": row["request_id"], "effect_fingerprint": row["effect_fingerprint"],
        "assistance_request_id": assistance, "tool_id": observation["tool_id"],
        "tool_name": observation["name"], "args": observation["args"],
        "is_error": observation["is_error"], "result_hash": content_digest(observation["output"]),
        "host_observation": observation["host_observation"]}, source="host",
        identity=["observation", row["request_id"], assistance])


@pytest.mark.parametrize("command", [
    "git status --short",
    'python -c "import json; print(json.__name__)"',
    "Get-Content state.txt",
])
def test_safe_probe_commands_are_readonly(command):
    assert classify_command_effect(command) == "readonly"


@pytest.mark.parametrize("command", [
    "python -c \"import requests; print(requests.get('https://example.com'))\"",
    "python -c \"from pathlib import Path; Path('x').write_text('x')\"",
    "pip install requests",
])
def test_unknown_or_writing_commands_are_not_readonly(command):
    assert classify_command_effect(command) in {"unknown", "write"}


def test_readonly_unknown_reserves_only_one_current_epoch_retry():
    record, node_id, row = _record()
    result = resolve_unknown_receipt(record, node_id, row["request_id"])
    assert result["action"] == "retry_once"
    stage_row = record["node_states"][node_id]["command_receipts"][-1]
    assert stage_row["status"] == "not_started"
    assert stage_row["retry_count"] == 1
    assert stage_row["effect_class"] == "readonly"
    assert any(f.payload.get("readonly_retry_receipt") for f in ledger_for_run(record).view(node_id).history)
    assert resolve_unknown_receipt(record, node_id, row["request_id"])["action"] == "already_resolved"
    repeated = {**stage_row, "status": "outcome_unknown", "command_started": None}
    ledger_for_run(record).record(node_id, "receipt", repeated, source="host",
                                  identity=["retry-ended-unknown", row["request_id"]])
    exhausted = resolve_unknown_receipt(record, node_id, row["request_id"])
    assert exhausted["action"] == "retry_exhausted"
    active = ledger_for_run(record).view(node_id).active_blockers
    assert any(f.payload.get("kind") == "readonly_effect_unknown"
               and f.payload.get("unbound_request_id") == row["request_id"] for f in active)


def test_unknown_write_waits_for_bound_host_observations():
    record, node_id, row = _record("Write-Output x", effect="unknown")
    result = resolve_unknown_receipt(record, node_id, row["request_id"])
    assert result["action"] == "verify_effect"
    assert all(f.payload.get("status") != "not_started"
               for f in ledger_for_run(record).view(node_id).current_receipts)


@pytest.mark.parametrize("effect,action,replay", [
    ("applied", "completed", False),
    ("not_applied", "retry_once", True),
    ("partial", "continue_from_partial", False),
])
@pytest.mark.asyncio
async def test_write_effect_verification_controls_replay(tmp_path, effect, action, replay):
    command = "echo changed > result.txt"
    record, node_id, row = _record(command, effect="unknown")
    target = tmp_path / "result.txt"
    if effect == "applied":
        target.write_text("changed\n", encoding="utf-8")
    elif effect == "partial":
        target.write_text("different\n", encoding="utf-8")
    observation = await _read_file_observation(target)
    host_result = verify_effect_observations(row, [observation], str(tmp_path))
    assert host_result and host_result["effect"] == effect
    fact = _verification_fact(record, node_id, row, observation)
    result = resolve_unknown_receipt(record, node_id, row["request_id"], verification={
        **host_result, "evidence_refs": [fact.fact_id]}, work_dir=str(tmp_path))
    assert result["action"] == action
    assert result.get("replay", replay) is replay
    if effect == "partial":
        assert any(f.payload.get("kind") in {"unknown_effect", "effect_unknown"}
                   for f in ledger_for_run(record).view(node_id).active_blockers)


@pytest.mark.asyncio
async def test_effect_verification_rejects_stale_or_unbound_evidence(tmp_path):
    record, node_id, row = _record("echo changed > result.txt", effect="unknown")
    target = tmp_path / "result.txt"
    target.write_text("changed\n", encoding="utf-8")
    observation = await _read_file_observation(target)
    host_result = verify_effect_observations(row, [observation], str(tmp_path))
    fact = _verification_fact(record, node_id, row, observation)
    payload = dict(fact.payload)
    payload["effect_fingerprint"] = "wrong"
    forged = ledger_for_run(record).record(node_id, "receipt", payload, source="host",
                                           identity=["bad", row["request_id"]])
    with pytest.raises(ValueError, match="not bound"):
        resolve_unknown_receipt(record, node_id, row["request_id"], verification={
            **host_result, "evidence_refs": [forged.fact_id]}, work_dir=str(tmp_path))


@pytest.mark.asyncio
@pytest.mark.parametrize('initial_binding', ['request', 'current_stop'])
async def test_partial_effect_can_be_reverified_after_target_is_completed(tmp_path, initial_binding):
    from mewcode.experts.blocking_policy import refresh_clearances
    record, node_id, row = _record('echo changed > result.txt', effect='unknown')
    if initial_binding == 'current_stop':
        record['node_ledgers'] = {}
        ledger = ledger_for_run(record)
        ledger.record(node_id, 'receipt', row)
        ledger.record(node_id, 'blocker', {'kind': 'effect_unknown', 'ref': 'current_stop'})
    target = tmp_path / 'result.txt'
    for index, content in enumerate(['different\n', 'changed\n']):
        target.write_text(content, encoding='utf-8')
        observation = await _read_file_observation(target)
        proof = verify_effect_observations(row, [observation], str(tmp_path))
        fact = _verification_fact(record, node_id, row, observation, assistance=f'assist-{index}')
        result = resolve_unknown_receipt(record, node_id, row['request_id'],
            verification={**proof, 'evidence_refs': [fact.fact_id]}, work_dir=str(tmp_path))
        refresh_clearances(record, node_id)
        assert bool(ledger_for_run(record).view(node_id).active_blockers) == (index == 0)
    assert result['action'] == 'completed' and result['replay'] is False
    assert any(f.payload.get('status') == 'effect_partial' for f in ledger_for_run(record).view(node_id).history)


@pytest.mark.asyncio
async def test_resolver_rejects_unstructured_readfile_claim_and_effect_mismatch(tmp_path):
    record, node_id, row = _record("echo changed > result.txt", effect="unknown")
    target = tmp_path / "result.txt"
    target.write_text("changed\n", encoding="utf-8")
    observation = await _read_file_observation(target)
    host_result = verify_effect_observations(row, [observation], str(tmp_path))
    fact = _verification_fact(record, node_id, row, observation)
    with pytest.raises(ValueError, match="proof"):
        resolve_unknown_receipt(record, node_id, row["request_id"], verification={
            "effect": "applied", "evidence_refs": [fact.fact_id]}, work_dir=str(tmp_path))
    with pytest.raises(ValueError, match="does not establish"):
        resolve_unknown_receipt(record, node_id, row["request_id"], verification={
            **host_result, "effect": "not_applied", "evidence_refs": [fact.fact_id]},
            work_dir=str(tmp_path))


@pytest.mark.asyncio
async def test_effect_verification_clears_legacy_tool_id_blocker(tmp_path):
    record, node_id, row = _record("echo changed > result.txt", effect="unknown")
    row["tool_id"] = "legacy-tool-7"
    ledger = ledger_for_run(record)
    ledger.record(node_id, "receipt", row, source="host", identity=["legacy-tool", row["request_id"]])
    legacy = ledger.record(node_id, "blocker", {"kind": "unknown_effect", "ref": "legacy-tool-7"},
                           source="host", identity="legacy-tool-blocker")
    target = tmp_path / "result.txt"
    target.write_text("changed\n", encoding="utf-8")
    observation = await _read_file_observation(target)
    host_result = verify_effect_observations(row, [observation], str(tmp_path))
    fact = _verification_fact(record, node_id, row, observation)
    result = resolve_unknown_receipt(record, node_id, row["request_id"], verification={
        **host_result, "evidence_refs": [fact.fact_id]}, work_dir=str(tmp_path))
    assert result["action"] == "completed"
    from mewcode.experts.blocking_policy import refresh_clearances
    refresh_clearances(record, node_id)
    view = ledger_for_run(record).view(node_id)
    assert legacy.fact_id not in {f.fact_id for f in view.active_blockers}


@pytest.mark.parametrize("source", ["host_readonly_retry", "host_effect_verification"])
def test_reserved_retry_reconnects_only_to_normal_pending_dispatch(source):
    request = {"command": "python --version", "input_scope": [],
               "forbidden_changes": [], "verification_command": ""}
    row = request_receipts("node", "revision", [request], status="not_started")[0]
    row.update(command_started=False, evidence_source=source, retry_reserved=True, retry_count=1)
    @dataclass(frozen=True)
    class Result:
        status: str = "needs_attention"
        error_kind: str = "capability_missing"
        handoff: dict | None = None
    result = Result(handoff={"blocker_kind": "capability_missing", "command_requests": [],
                             "file_requests": []})
    resumed = restore_pending_commands("node", "revision", [row], result)
    assert resumed.handoff["command_requests"] == [request]


@pytest.mark.asyncio
async def test_unknown_write_cannot_cross_epoch_or_be_blindly_replayed():
    record, node_id, row = _record("echo changed > result.txt", effect="unknown")
    ledger = ledger_for_run(record)
    with pytest.raises(ValueError, match="换代际不能解除"):
        ledger.bump_epoch(node_id, reason="user_direction", source_ref="new-direction")
    result = resolve_unknown_receipt(record, node_id, row["request_id"])
    assert result["action"] == "verify_effect"
    assert all(f.payload.get("status") != "not_started"
               for f in ledger.view(node_id).current_receipts)


def test_command_fingerprint_is_independent_of_epoch():
    request = {"command": "Write-Output x", "input_scope": [], "declared_write_paths": ["a"]}
    assert command_effect_fingerprint("node", request) == command_effect_fingerprint("node", request)
