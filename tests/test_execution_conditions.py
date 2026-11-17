from types import SimpleNamespace

import pytest

from mewcode.sandbox.execution_conditions import classify_execution_condition
from mewcode.sandbox.receipt import NativeExecutionReceipt
from mewcode.tools.command_sandbox import native_receipt_observation


@pytest.mark.parametrize("code", ["python_runtime_missing", "native_helper_start_failed", "native_queue_timeout"])
def test_explicit_environment_codes_are_retryable_only_when_host_proves_no_start(code):
    receipt = NativeExecutionReceipt("denied", detail="host resource unavailable", command_started=False,
                                     error_code=code, cleanup_confirmed=True)
    result = classify_execution_condition(receipt)
    assert result["kind"] == "environment_unavailable"
    assert result["safe_to_retry"] is True
    assert result["command_started"] is False
    assert result["code"] == code

    uncertain = NativeExecutionReceipt("denied", command_started=None, error_code=code)
    result = classify_execution_condition(uncertain)
    assert result["kind"] == "environment_unavailable"
    assert result["safe_to_retry"] is False


@pytest.mark.parametrize("code", ["python_runtime_missing", "native_helper_start_failed", "native_queue_timeout"])
def test_environment_rejected_transaction_is_retryable_only_with_confirmed_no_start_and_cleanup(code):
    receipt = {
        "status": "denied",
        "error_code": code,
        "command_started": False,
        "cleanup_confirmed": True,
        "transaction_id": "tx-123",
        "publication_state": "rejected",
    }
    result = classify_execution_condition(receipt)
    assert result["kind"] == "environment_unavailable"
    assert result["safe_to_retry"] is True

    unsafe_receipts = [
        {**receipt, "cleanup_confirmed": None},
        {**receipt, "cleanup_confirmed": False},
        {**receipt, "command_started": None},
        {**receipt, "command_started": True},
        {**receipt, "transaction_id": ""},
        {**receipt, "error_code": "native_scope_invalid"},
        {**receipt, "error_code": "unknown_error"},
    ]
    for unsafe in unsafe_receipts:
        assert classify_execution_condition(unsafe)["safe_to_retry"] is False


@pytest.mark.parametrize("code", ["native_gate_pending", "native_scope_invalid", "native_workspace_missing",
                                   "native_admission_failed", "native_preparation_failed", "unknown_error"])
def test_unknown_and_boundary_denials_do_not_request_environment_repair(code):
    result = classify_execution_condition(NativeExecutionReceipt(
        "denied", detail="No module named pymupdf", stdout="No module named pymupdf",
        command_started=False, error_code=code))
    assert result["kind"] == "permission_denied"
    assert result["safe_to_retry"] is False


def test_stdout_and_rendered_tool_text_never_create_environment_classification():
    result = classify_execution_condition({
        "output": "AUTO_ENVIRONMENT_UNAVAILABLE: No module named pymupdf",
        "host_observation": {"command_execution": {"status": "denied", "command_started": False,
            "stdout": "ModuleNotFoundError: No module named pymupdf", "stderr": "",
            "exit_code": None, "reason": "boundary denied", "error_code": "unknown_native_denial"}},
    })
    assert result["kind"] == "permission_denied"
    assert result["safe_to_retry"] is False
    assert result["reason"] == "boundary denied"

    output_only = classify_execution_condition("AUTO_ENVIRONMENT_UNAVAILABLE: No module named pymupdf")
    assert output_only["kind"] == "permission_denied"


@pytest.mark.parametrize("code", ["native_receipt_unverified", "native_transport_unknown"])
def test_unverified_transport_and_cleanup_keep_effect_unknown(code):
    receipt = NativeExecutionReceipt("spawn_failed", detail="receipt missing", command_started=None,
        error_code=code, cleanup_confirmed=False)
    result = classify_execution_condition(receipt)
    assert result["kind"] == "effect_unknown"
    assert result["safe_to_retry"] is False


@pytest.mark.parametrize("status", ["timed_out", "cancelled"])
def test_started_or_unobserved_interrupted_command_has_unknown_effect(status):
    result = classify_execution_condition(NativeExecutionReceipt(
        status, detail="host stopped waiting", command_started=None, cleanup_confirmed=True))
    assert result["kind"] == "effect_unknown"
    assert result["safe_to_retry"] is False


@pytest.mark.parametrize("started", [None, True])
def test_spawn_failure_requires_reliable_not_started_fact_for_safe_retry(started):
    result = classify_execution_condition(NativeExecutionReceipt(
        "spawn_failed", detail="helper did not start", command_started=started))
    assert result["kind"] == ("environment_unavailable" if started is False else "effect_unknown")
    assert result["safe_to_retry"] is False


def test_completed_observation_status_is_classified_from_structured_exit_code():
    successful = {"status": "completed", "command_started": True, "exit_code": 0,
                  "cleanup_confirmed": True, "publication_state": "discarded",
                  "transaction_id": "txn-1"}
    assert classify_execution_condition(successful)["kind"] == "ready"

    failed = {**successful, "exit_code": 7}
    assert classify_execution_condition(failed)["kind"] == "execution_failed"

    inconsistent = {**successful, "command_started": False}
    assert classify_execution_condition(inconsistent)["kind"] == "effect_unknown"


@pytest.mark.parametrize("publication", ["cleanup_unknown", "recovery_required", "publishing", "unexpected"])
def test_unsettled_or_unknown_publication_never_becomes_ready(publication):
    result = classify_execution_condition(NativeExecutionReceipt(
        "exited", exit_code=0, command_started=True, cleanup_confirmed=True,
        transaction_id="txn-2", publication_state=publication))
    assert result["kind"] == "effect_unknown"


def test_known_native_result_rejection_after_start_is_execution_failure():
    result = classify_execution_condition(NativeExecutionReceipt(
        "denied", exit_code=1, command_started=True, cleanup_confirmed=True,
        transaction_id="txn-3", publication_state="rejected", error_code="native_result_rejected"))
    assert result["kind"] == "execution_failed"
    assert result["safe_to_retry"] is False


@pytest.mark.parametrize("receipt,kind,prefix", [
    (NativeExecutionReceipt("denied", detail="missing project runtime", command_started=False,
                            error_code="python_runtime_missing"),
     "environment_unavailable", "AUTO_ENVIRONMENT_UNAVAILABLE:"),
    (NativeExecutionReceipt("denied", detail="scope mismatch", command_started=False,
                            error_code="native_scope_invalid"),
     "permission_denied", "AUTO_BOUNDARY_DENIED:"),
])
def test_native_receipt_render_uses_only_typed_environment_classification(receipt, kind, prefix):
    assert classify_execution_condition(receipt)["kind"] == kind
    assert receipt.render().startswith(prefix)


def test_native_receipt_host_observation_includes_same_execution_condition():
    receipt = NativeExecutionReceipt("denied", detail="Python runtime not available",
        command_started=False, error_code="python_runtime_missing", cleanup_confirmed=True)
    observation = native_receipt_observation(receipt)["command_execution"]
    assert observation["execution_condition"] == classify_execution_condition(receipt)
    assert observation["error_code"] == "python_runtime_missing"


def test_tool_result_observation_not_output_is_used_as_classification_input():
    result = SimpleNamespace(
        output="ModuleNotFoundError: No module named pymupdf",
        host_observation=native_receipt_observation(NativeExecutionReceipt(
            "denied", command_started=False, error_code="native_scope_invalid")))
    assert classify_execution_condition(result)["kind"] == "permission_denied"
