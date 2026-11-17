"""A fixed host validator can reopen one stopped build without changing scope."""
import copy

import pytest

from mewcode.orchestration.draft_sources import digest
from mewcode.orchestration.graph_autobuild import run_autobuild
from tests.test_graph_autobuild import FakeModel, graph, proposal
from tests.test_task_design_spec import _queued_design, _expected_build, _build_args


def stopped_task(tmp_path):
    design, queued = _queued_design(tmp_path, intent="execute")
    building = design.claim_build(queued)
    document = graph(ready=True)
    next(e for e in document["entries"] if e["id"] == "plan")["data"]["steps"] = ["使用项目 .venv，不联网安装"]
    with design.store.db() as db:
        state = design.store._get(db)
        state["task_design"]["build_request"]["base_graph_hash"] = digest(None)
        state["task_design"]["intent_authority"]["revoked"] = True
        state["candidate"] = {"id": "candidate", "sequence": 4, "status": "ready", "document": document,
            "processed_ids": ["source"], "base_snapshot": None, "base_graph_hash": digest(None),
            "readiness": {"ready": False, "diagnostics": [{"code": "authority_before_use", "entry_ids": ["plan"]}]},
            "autobuild": {"phase": "done", "status": "needs_review", "reason": "model_stopped",
                          "model_calls": 3, "autofix": {"rounds": [{"notes": "previous attempt"}], "model_calls": 3}}}
        design.store._put(db, state)
    failed = design.finish_build(_expected_build(building), "failed", "model_stopped",
        failure_origin="host_build", failure_kind="non_retryable", retryable=False,
        failure_evidence={"reason": "model_stopped", "candidate_id": "candidate", "failure": {}})
    return design, failed


@pytest.mark.asyncio
async def test_fixed_validator_restarts_same_graph_and_requires_independent_review(tmp_path):
    design, failed = stopped_task(tmp_path)
    before = design.store.get()["candidate"]
    queued = design.queue_build(task_id=failed["task_id"], revision=failed["revision"], **_build_args(failed))
    saved = design.store.get()["candidate"]
    assert saved["document"] == before["document"] and saved["processed_ids"] == before["processed_ids"]
    assert saved["readiness"]["ready"] is True and saved["sequence"] == before["sequence"] + 1
    assert saved["autobuild"]["phase"] == "autofix" and saved["autobuild"]["status"] == "working"
    assert saved["autobuild"]["previous_attempts"][-1]["autofix"] == before["autobuild"]["autofix"]
    assert queued["intent_authority"] == failed["intent_authority"]
    assert queued["build_request"]["previous_failure"]["retryable"] is False
    assert queued["build_request"]["structural_revalidation"]["candidate_sequence"] == 4
    model, reviewer = FakeModel(proposal()), FakeModel('{"gaps":[]}')
    result = await run_autobuild(saved["document"], None, [{"id": "u", "role": "user", "text": "离线构图"}],
        conversation_id="lifecycle", revision=1, state=saved["autobuild"], ask_model=model,
        ask_review=reviewer, autonomous=True)
    assert result["build"]["status"] == "ready" and len(reviewer.calls) == 1


@pytest.mark.parametrize("invalid", ["still_blocked", "wrong_candidate", "unknown_origin", "coverage_failure",
                                      "already_ready", "warnings_only", "wrong_base"])
def test_same_spec_guard_remains_for_unproven_recovery(tmp_path, invalid):
    design, failed = stopped_task(tmp_path)
    with design.store.db() as db:
        state = design.store._get(db)
        candidate, request = state["candidate"], state["task_design"]["build_request"]
        if invalid == "still_blocked":
            from mewcode.orchestration.graph_readiness import readiness
            next(e for e in candidate["document"]["entries"] if e["id"] == "plan")["data"]["outputs"] = []
            check = readiness(candidate["document"], candidate["base_snapshot"],
                conversation_id=design.store.scope[0], revision=state["revision"] + 1)
            assert check["ready"] is False
            assert any(row["code"] == "missing_outputs" for row in check["diagnostics"])
        elif invalid == "wrong_candidate":
            request["failure_evidence"]["candidate_id"] = "old"
        elif invalid == "unknown_origin":
            request.pop("failure_origin")
        elif invalid == "coverage_failure":
            request["failure_evidence"]["reason"] = "review_error"
        elif invalid == "already_ready":
            candidate["readiness"]["ready"] = True
        elif invalid == "warnings_only":
            candidate["readiness"]["diagnostics"][0]["severity"] = "warning"
        else:
            candidate["base_snapshot"] = {"changed": True}
        design.store._put(db, state)
    before = design.store.get()
    with pytest.raises(ValueError, match="同一规格"):
        design.queue_build(task_id=failed["task_id"], revision=failed["revision"], **_build_args(failed))
    assert design.store.get() == before


def test_consumed_revalidation_cannot_loop_on_a_second_failure(tmp_path):
    design, failed = stopped_task(tmp_path)
    queued = design.queue_build(task_id=failed["task_id"], revision=failed["revision"], **_build_args(failed))
    building = design.claim_build(queued)
    with design.store.db() as db:
        state = design.store._get(db)
        state["candidate"]["autobuild"].update(phase="done", status="needs_review", reason="model_stopped")
        design.store._put(db, state)
    again = design.finish_build(_expected_build(building), "failed", "model_stopped",
        failure_origin="host_build", failure_kind="non_retryable", retryable=False,
        failure_evidence=copy.deepcopy(failed["build_request"]["failure_evidence"]))
    with pytest.raises(ValueError, match="同一规格"):
        design.queue_build(task_id=again["task_id"], revision=again["revision"], **_build_args(again))
