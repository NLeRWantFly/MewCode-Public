"""Old review failures remain visible without becoming retry or execution grants."""
import copy

import pytest

from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.graph_diagnostics import build_diagnostics
from mewcode.orchestration.task_design import design_view
from mewcode.orchestration.workflow_blocks import WorkflowBlocks, compact_task_design
from tests.test_graph_discussion import discussion_graph


def legacy_state(*, adopted=False):
    error = {"error": "RemoteProtocolError", "gaps": [], "model_calls": 1}
    return {"task_design": {"task_id": "task", "revision": 53, "status": "failed", "intent": "execute",
                "intent_authority": {"source": "host_entry", "intent": "execute", "revoked": True},
                "reason": '构图尚未通过：{"error":"","reason":"review_error"}',
                "build_request": {"state": "failed", "retryable": False, "failure_kind": "non_retryable",
                    "failure_evidence": {"reason": "review_error", "candidate_id": "candidate", "failure": {}}}},
            "candidate": {"id": "candidate", "sequence": 241, "status": "ready",
                "document": discussion_graph(), "readiness": {"ready": True},
                "autobuild": {"phase": "autofix" if adopted else "done",
                    "status": "working" if adopted else "needs_review",
                    "reason": "decision_adopted" if adopted else "review_error",
                    "review_stale": adopted, "review": None if adopted else error,
                    "final_review": None, "review_history": [error] if adopted else []}}}


@pytest.mark.parametrize("adopted", [False, True])
def test_legacy_failure_visible_in_view_and_first_agent_page_without_mutation(tmp_path, monkeypatch, adopted):
    state = legacy_state(adopted=adopted)
    before = copy.deepcopy(state)
    monkeypatch.setattr(DraftStore, "read_state", staticmethod(lambda *args, **kwargs: copy.deepcopy(state)))
    view = design_view(state)
    assert "RemoteProtocolError" in view["reason"]
    assert "旧记录未保存具体异常文本" in view["reason"]
    assert view["status"] == "failed" and view["intent_authority"]["revoked"]
    assert view["build_request"] == state["task_design"]["build_request"]
    header, _ = WorkflowBlocks(tmp_path, "chat").read_page(limit=500)
    compact = header["task_design"]
    assert compact == compact_task_design(view)
    diagnostics = compact["build_diagnostics"]
    failure = diagnostics["previous_review_failure" if adopted else "failure"]
    assert failure["exception_type"] == "RemoteProtocolError"
    assert failure["retryable"] is None and failure["legacy"] is True
    if adopted:
        assert "failure" not in diagnostics
        assert "待重新校验" in compact["reason"]
        assert diagnostics["phase"] == "autofix" and diagnostics["review_stale"]
    assert state == before


def test_current_failure_wins_over_history_and_wrong_candidate_never_relabels_task():
    state = legacy_state()
    state["candidate"]["autobuild"]["final_review"] = {"error": "JSONDecodeError"}
    state["candidate"]["autobuild"]["review_history"] = [{"error": "TimeoutError"}]
    assert "JSONDecodeError" in design_view(state)["reason"]
    state["task_design"]["build_request"]["failure_evidence"]["candidate_id"] = "older-candidate"
    assert design_view(state)["reason"] == state["task_design"]["reason"]


def test_new_structured_failure_retains_phase_and_retry_classification():
    state = legacy_state()
    failure = {"phase": "final_review", "stage": "model_call", "exception_type": "TimeoutError",
               "retryable": True, "summary": "覆盖审查请求超时", "batch": 1}
    state["candidate"]["autobuild"]["failure"] = failure
    result = build_diagnostics(state)["failure"]
    assert all(result[key] == value for key, value in failure.items())
    assert result["origin"] == "build" and result["legacy"] is False


def test_success_or_resumed_build_never_reports_old_failure_as_current():
    state = legacy_state(adopted=True)
    state["candidate"]["autobuild"]["failure"] = {"summary": "old failure", "retryable": True}
    assert "failure" not in build_diagnostics(state)
    state["task_design"].update(status="ready", reason="编排图已保存")
    assert design_view(state)["reason"] == "编排图已保存"


def test_arbitrary_legacy_error_text_is_not_exposed_or_classified():
    state = legacy_state()
    state["candidate"]["autobuild"]["review"]["error"] = "secret-value retryable=true"
    result = build_diagnostics(state)["failure"]
    assert "secret-value" not in str(result)
    assert result["exception_type"] == "UnknownError" and result["retryable"] is None
