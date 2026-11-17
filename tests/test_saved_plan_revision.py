import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.orchestration.loops import LoopContract, LoopWatchdog
from mewcode.orchestration.saved_revision import revision_preview, adopt_revision


def example():
    loop = LoopContract.from_value(dict(loop_id="qc", entry_node_id="work", check_node_id="work",
        node_ids=["work"], exit_condition="sample", criteria=[dict(criterion_id="OLD", acceptance_test="sample")]))
    flow = dict(flow_id="same", version=3, label="test", finalizer_node_id="end", finalizer_plugin="model",
        nodes=[dict(node_id=k, role="executor", label=k, responsibility="sample") for k in ("prepare", "work", "end")],
        edges=[dict(from_node="prepare", to_node="work"), dict(from_node="work", to_node="end")],
        meta=dict(loops=[loop.as_dict()]))
    state = LoopWatchdog(loop).state
    state.update(status="blocked", round=2, tokens=500000, elapsed=2600, stalled=1,
                 reports={"work": {"decision": "EXIT"}}, round_results={"work": {"passed": True}})
    snapshot = dict(run_id="a" * 32, conversation_id="chat", status="blocked", updated_at="v1", locked_flow=flow,
        stage_cursor="work", node_states={"prepare": dict(status="completed", text="keep"),
                                          "work": dict(status="blocked", text="artifact evidence")},
        loop_states={"qc": state}, pending_dialogue_handoff=dict(action="hold", relation="plan_changed",
            summary="changed", resume_context="all pages", invalidated_completed_node_ids=[]))
    new = copy.deepcopy(flow)
    new["version"] = 4
    new["nodes"][1]["responsibility"] = "all pages"
    new["meta"]["loops"][0]["exit_condition"] = "all pages"
    new["meta"]["loops"][0]["criteria"].append(dict(criterion_id="NEW", acceptance_test="all pages"))
    return snapshot, new


def test_adoption_preserves_completed_work_and_spent_budgets_without_starting():
    snapshot, new = example()
    snapshot['review_queues'] = {'work': {'records': {'page1': {'state': 'complete'}}}}
    original = copy.deepcopy(snapshot)
    preview = revision_preview(snapshot, new)
    result = adopt_revision(snapshot, new, "v1", preview["fingerprint"])
    assert snapshot == original
    assert not result['review_queues']
    assert result['review_queue_archives'][0]['queue'] == original['review_queues']['work']
    assert result["status"] == "blocked"
    assert result["stage_cursor"] == "work"
    assert result["node_states"]["prepare"] == original["node_states"]["prepare"]
    assert result["node_states"]["work"]["text"] == "artifact evidence"
    assert result["node_states"]["work"]["status"] == "pending"
    state = result["loop_states"]["qc"]
    assert "reports" not in state and "round_results" not in state
    for key in ("round", "tokens", "elapsed", "stalled"):
        assert state[key] == original["loop_states"]["qc"][key]
    watchdog = LoopWatchdog(LoopContract.from_value(new["meta"]["loops"][0]), state)
    assert watchdog.remaining()  # Adoption cannot erase an exhausted strict budget.
    assert result["pending_dialogue_handoff"]["accepted_plan_fingerprint"] == preview["fingerprint"]


@pytest.mark.parametrize("change", ["completed", "budget", "edges", "owner", "pending_review"])
def test_incompatible_revisions_are_rejected(change):
    snapshot, new = example()
    if change == "completed":
        new["nodes"][0]["responsibility"] = "different"
    elif change == "budget":
        new["meta"]["loops"][0]["token_budget"] += 1
    elif change == "edges":
        new["edges"] = []
    elif change == "owner":
        new["meta"]["conversation_id"] = "other"
    else:
        snapshot["pending_recovery_revision"] = {"summary": "another revision"}
    with pytest.raises(ValueError):
        revision_preview(snapshot, new)


def test_stale_preview_cannot_be_accepted():
    snapshot, new = example()
    preview = revision_preview(snapshot, new)
    with pytest.raises(ValueError):
        adopt_revision(snapshot, new, "stale", preview["fingerprint"])
    new["nodes"][1]["responsibility"] += " changed again"
    with pytest.raises(ValueError):
        adopt_revision(snapshot, new, "v1", preview["fingerprint"])


def test_public_snapshot_exposes_revision_action_without_private_dialogue():
    from mewcode.expert_runs import ExpertRunStore
    snapshot, new = example()
    public = ExpertRunStore._public_value(snapshot, include_details=False)
    assert public["saved_plan_revision_pending"] is True
    assert "pending_dialogue_handoff" not in public
    preview = revision_preview(snapshot, new)
    adopted = adopt_revision(snapshot, new, "v1", preview["fingerprint"])
    assert ExpertRunStore._public_value(adopted, include_details=False)["saved_plan_revision_pending"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_again", [False, True])
async def test_accepted_handoff_reaches_resume_only_for_exact_saved_plan(monkeypatch, changed_again):
    from frontend import server
    from mewcode.orchestration import revisions
    snapshot, new = example()
    preview = revision_preview(snapshot, new)
    accepted = adopt_revision(snapshot, new, "v1", preview["fingerprint"])
    if changed_again:
        new["nodes"][1]["responsibility"] += " changed again"
    monkeypatch.setattr(revisions, "saved_execution_flow", lambda *a, **kw: new)
    monkeypatch.setattr(server, "_conversation_flow_store", lambda *a: None)
    monkeypatch.setattr(server, "_conversation_plan_store", lambda *a: None)
    monkeypatch.setattr(server, "set_dialogue_focus", lambda *a, **kw: None)
    stored = copy.deepcopy(accepted)
    def mutate(_run_id, apply, **_kwargs):
        apply(stored)
        return stored
    monkeypatch.setattr(server, "EXPERT_RUNS", SimpleNamespace(mutate=mutate,
        load=lambda _run_id: copy.deepcopy(stored)))
    resume, review = AsyncMock(), AsyncMock()
    monkeypatch.setattr(server, "run_expert_chat", resume)
    monkeypatch.setattr(server, "_run_takeover_dialogue", review)
    runtime = SimpleNamespace(session=None, work_dir="project", conversation_id="chat",
                              status_payload=lambda: {}, tasks_payload=lambda: [])
    await server._resume_expert_after_paused_dialogue(SimpleNamespace(send_json=AsyncMock()), runtime, accepted)
    review.assert_not_awaited()
    if changed_again:
        resume.assert_not_awaited()
        assert stored["pending_dialogue_handoff"]["relation"] == "plan_changed"
        assert "accepted_plan_fingerprint" not in stored["pending_dialogue_handoff"]
        # Repeated future revisions can be approved through the same endpoint.
        next_preview = revision_preview(stored, new)
        readopted = adopt_revision(stored, new, "v1", next_preview["fingerprint"])
        assert readopted["status"] == "blocked"
    else:
        resume.assert_awaited_once()
        assert resume.call_args.kwargs["resume_run_id"] == accepted["run_id"]
        assert "all pages" in resume.call_args.kwargs["supplemental"]


def test_future_plan_revision_without_a_loop_uses_same_adoption_path():
    snapshot, new = example()
    snapshot["locked_flow"]["meta"].pop("loops")
    new["meta"].pop("loops")
    snapshot.pop("loop_states")
    preview = revision_preview(snapshot, new)
    adopted = adopt_revision(snapshot, new, "v1", preview["fingerprint"])
    assert preview["reset_loop_ids"] == []
    assert adopted["node_states"]["prepare"] == snapshot["node_states"]["prepare"]
    assert adopted["node_states"]["work"]["status"] == "pending"
    assert adopted["status"] == "blocked"


def test_changed_upstream_cannot_reuse_completed_downstream():
    snapshot, new = example()
    snapshot["node_states"]["end"] = {"status": "completed", "text": "old acceptance"}
    with pytest.raises(ValueError, match="已完成节点"):
        revision_preview(snapshot, new)
