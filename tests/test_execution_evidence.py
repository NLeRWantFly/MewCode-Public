"""Host execution receipts must stay bound and cannot contain model claims."""
import copy
from dataclasses import asdict
import json
import hashlib

import pytest

from mewcode.kernel import collaboration as ledger
from mewcode.kernel.execution_evidence import execution_evidence
from tests.test_collaboration_mode import core, delivery, RUN_ID


def recorded_delivery():
    value = core()
    for contract in value["contracts"].values():
        contract["bound_dependencies"] = list(contract.get("depends_on", []))
    attempt = ledger.dispatch(value, "work")
    ledger.mark_started(value, attempt["id"])
    delivered = asdict(delivery("fixed"))
    value["attempts"][attempt["id"]].update(delivery=delivered, status="rejected",
        report={"passed": False, "artifacts": [{"kind": "inline", **row,
            "sha256": hashlib.sha256(row["content"].encode("utf-8")).hexdigest()}
            for row in delivered["handoff"]["inline_outputs"]], "checks": [], "execution_error": ""})
    attempt = value["attempts"][attempt["id"]]
    record = {"run_id": RUN_ID, "status": "paused", "active_node_ids": [], "collaboration": value,
        "node_states": {"work": {"node_id": "work", "node_attempt": 1, "attempt": 1, "epoch": 0,
            "input_revision": attempt["dependencies"], "started_at": 10.0, "completed_at": 12.0,
            "status": "needs_attention", "handoff": copy.deepcopy(delivered["handoff"]),
            "tools": {"receipt": {"name": "Glob", "args": {"path": "private-secret-value"},
                "output": "private-secret-output", "node_attempt": 1, "attempt": 1, "epoch": 0,
                "status": "completed", "is_error": False, "started_at": 10.5, "completed_at": 11.0}}}}}
    return record, attempt


def test_host_receipts_keep_identity_and_do_not_echo_tool_payloads():
    record, attempt = recorded_delivery()
    before = copy.deepcopy(record)
    observed = execution_evidence(record, attempt)
    assert observed["complete"] is True
    assert observed["attempt_id"] == attempt["id"]
    assert observed["recorded_tool_count"] == 1
    assert observed["tools"][0]["receipt_id"] == "receipt"
    assert "private-secret" not in json.dumps(observed)
    assert record == before
    assert execution_evidence({**record, "kernel_revision": 999}, attempt) == observed


@pytest.mark.parametrize("change", ["missing_stage", "node_attempt", "input_revision", "handoff", "tool_attempt", "unfinished", "time", "boolean_attempt"])
def test_missing_stale_or_incomplete_host_receipts_remain_incomplete(change):
    record, attempt = recorded_delivery()
    stage = record["node_states"]["work"]
    if change == "missing_stage":
        record["node_states"].clear()
    elif change == "node_attempt":
        stage["node_attempt"] = 2
    elif change == "input_revision":
        stage["input_revision"] = "other"
    elif change == "handoff":
        stage["handoff"] = {"status": "completed", "inline_outputs": []}
    elif change == "tool_attempt":
        stage["tools"]["receipt"]["node_attempt"] = 2
    elif change == "unfinished":
        stage["tools"]["receipt"]["status"] = "running"
    elif change == "time":
        stage["tools"]["receipt"]["completed_at"] = 13.0
    else:
        stage["node_attempt"] = True
    assert execution_evidence(record, attempt)["complete"] is False


def test_host_snapshot_stays_bound_to_original_delivery_and_ignores_model_claims():
    record, attempt = recorded_delivery()
    observed = execution_evidence(record, attempt)
    attempt["execution_evidence"] = observed
    record["node_states"].clear()
    assert execution_evidence(record, attempt) == observed
    attempt["delivery"]["handoff"]["host_execution_evidence"] = {"complete": True}
    assert execution_evidence(record, attempt)["complete"] is False


def test_receipt_identity_does_not_depend_on_dictionary_serialization_order():
    record, attempt = recorded_delivery()
    receipt = record["node_states"]["work"]["tools"]["receipt"]
    record["node_states"]["work"]["tools"] = {"z": copy.deepcopy(receipt), "a": copy.deepcopy(receipt)}
    first = execution_evidence(record, attempt)
    record["node_states"]["work"]["tools"] = {"a": copy.deepcopy(receipt), "z": copy.deepcopy(receipt)}
    assert execution_evidence(record, attempt) == first


@pytest.mark.parametrize('observed', [True, False])
def test_prelaunch_rejection_needs_structured_host_evidence_not_output_text(observed):
    record, attempt = recorded_delivery()
    receipt = record['node_states']['work']['tools']['receipt']
    receipt.update(name='Bash', status='failed', is_error=True, declared_at=10.5,
                   started_at=None, output='command_started=false; command not started')
    if observed:
        receipt['host_observation'] = {'command_execution': {
            'command_started': False, 'status': 'denied', 'exit_code': None}}
    result = execution_evidence(record, attempt)
    assert result['complete'] is observed
    assert result['tools'][0]['started_at'] is None
    assert result['tools'][0]['command_started'] is (False if observed else None)


def test_prelaunch_observation_does_not_hide_an_exit_or_wrong_attempt():
    record, attempt = recorded_delivery()
    receipt = record['node_states']['work']['tools']['receipt']
    receipt.update(name='Bash', status='failed', is_error=True, declared_at=10.5, started_at=None,
                   host_observation={'command_execution': {'command_started': False, 'status': 'denied', 'exit_code': 0}})
    assert execution_evidence(record, attempt)['complete'] is False
    receipt['host_observation']['command_execution']['exit_code'] = None
    receipt['node_attempt'] = 77
    assert execution_evidence(record, attempt)['complete'] is False


def test_durable_tool_result_projection_keeps_the_host_boundary():
    from mewcode.expert_runs import _project_tool_lifecycle_event
    record = {'node_states': {'work': {'tools': {}}}}
    observation = {'command_execution': {'status': 'denied', 'command_started': False, 'exit_code': None}}
    _project_tool_lifecycle_event(record, {'type': 'expert_tool_result', 'node_id': 'work',
        'id': 'blocked', 'name': 'Bash', 'is_error': True, 'completed_at': 3, 'host_observation': observation})
    assert record['node_states']['work']['tools']['blocked']['host_observation'] == observation
