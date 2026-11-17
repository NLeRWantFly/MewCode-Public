"""Publication acceptance uses the actual fragment compiler and run store."""
import copy

import pytest

from mewcode.expert_runs import ExpertRunStore
from mewcode.kernel import collaboration as ledger, collaboration_graph as graph
from mewcode.orchestration.collab_compile import compile_plan
from tests.test_autonomous_fragment import _sample_flow, _proposal, _build
from tests.test_collaboration_mode import RUN_ID, PROFILE

REVIEW = {"passed": True, "evidence": ["按父合同逐项核对子图边界"]}


def prepared(tmp_path, *, publish=False):
    flow = _sample_flow()
    store = ExpertRunStore(tmp_path / "runs")
    store.create(run_id=RUN_ID, conversation_id="conversation", task="测试子图", attachments=[],
                 profile=PROFILE.as_dict(), strategy="stable", flow=flow)
    record = store.load(RUN_ID)
    value = ledger.initialize(record, compile_plan(flow))
    attempt = ledger.dispatch(value, "work")
    ledger.mark_started(value, attempt["id"])
    fragment = _build(value, flow, tmp_path, _proposal())
    item = graph.prepare(record, fragment, publication_id="publication-one", attempt_id=attempt["id"])
    if publish:
        publish_graph(record)
    return store, record, attempt, item


def publish_graph(record, **kwargs):
    return graph.publish(record, "publication-one", **{
        "review": REVIEW, "proposer_vendor": "brand-a", "reviewer_vendor": "brand-b", **kwargs})


def test_prepare_is_invisible_and_reserves_once(tmp_path):
    _, record, attempt, item = prepared(tmp_path)
    value = ledger.state(record)
    assert set(graph.effective_flow(record).node_map) == {"work", "deliver"}
    assert item["affected"] == ["deliver", "work"]
    assert value["plan_holds"] == {"deliver": item["id"], "work": item["id"]}
    assert value["lineage_budget"]["root"]["reserved"]["expansions"] == 1
    before = copy.deepcopy(record)
    assert graph.prepare(record, item["fragment"], publication_id=item["id"], attempt_id=attempt["id"]) == item
    assert record == before
    changed = {**item["fragment"], "expansion_id": "different"}
    with pytest.raises(ValueError):
        graph.prepare(record, changed, publication_id=item["id"], attempt_id=attempt["id"])
    assert record == before


def test_publish_preserves_locked_design_and_settles_lineage(tmp_path):
    store, record, attempt, item = prepared(tmp_path)
    original = copy.deepcopy(record["locked_flow"])
    initial_hash = ledger.state(record)["initial_plan_hash"]
    assert publish_graph(record)
    value = ledger.state(record)
    children = item["fragment"]["child_ids"]
    assert record["locked_flow"] == original
    assert value["initial_plan_hash"] == initial_hash
    assert value["plan_version"] == 2 and value["spec_version"] == 1
    assert graph.effective_flow(record).version == 2
    assert set(graph.effective_flow(record).node_map) == {"work", "deliver", *children}
    assert value["attempts"][attempt["id"]]["status"] == "expanded"
    assert not value["attempts"][attempt["id"]].get("superseded")
    assert value["plan_holds"] == {} and "work" not in value["certificates"]
    milestone = value["milestones"][value["contracts"]["work"]["milestone_id"]]
    assert milestone["contracts"] == [*children, "work", "deliver"]
    budget = value["lineage_budget"]
    assert budget["root"]["reserved"]["expansions"] == 0
    assert budget["lineages"]["work"]["used"]["expansions"] == 1
    assert [entry["plan_version"] for entry in value["flow_versions"]] == [1, 2]
    before = copy.deepcopy(record)
    assert publish_graph(record, review={"passed": False}) is False
    assert record == before
    store.mutate(RUN_ID, lambda saved: saved.update(record), durable=True)
    restarted = ExpertRunStore(tmp_path / "runs").load(RUN_ID)
    assert graph.effective_flow(restarted).as_dict() == graph.effective_flow(record).as_dict()
    assert ledger.state(restarted) == value


@pytest.mark.parametrize("mutation", ["plan", "epoch", "contract", "unknown", "in_flight", "hash", "hold"])
def test_stale_or_unreconciled_publication_is_atomic(tmp_path, mutation):
    _, record, attempt, item = prepared(tmp_path)
    value = ledger.state(record)
    if mutation == "plan":
        value["plan_version"] += 1
    elif mutation == "epoch":
        value["coordinator_epoch"] += 1
    elif mutation == "contract":
        value["contracts"]["work"]["goal"] = "另一目标"
    elif mutation == "unknown":
        record["kernel_calls"] = {"effect": {"node_id": "deliver", "state": "unknown"}}
    elif mutation == "in_flight":
        value["attempts"]["other"] = {**attempt, "id": "other", "contract_id": "deliver", "superseded": True}
    elif mutation == "hash":
        value["runtime_flow"] = copy.deepcopy(record["locked_flow"])
        value["runtime_flow_hash"] = "broken"
    else:
        value["plan_holds"]["work"] = "other-publication"
    before = copy.deepcopy(record)
    with pytest.raises(ValueError):
        publish_graph(record)
    assert record == before
    assert value["graph_publications"][item["id"]]["status"] == "prepared"


@pytest.mark.parametrize("kwargs", [{"review": {"passed": False, "evidence": ["失败"]}},
    {"review": {"passed": True, "evidence": []}}, {"reviewer_vendor": "BRAND-A"}, {"reviewer_vendor": ""}])
def test_requires_independent_review(tmp_path, kwargs):
    _, record, _, _ = prepared(tmp_path)
    before = copy.deepcopy(record)
    with pytest.raises(ValueError):
        publish_graph(record, **kwargs)
    assert record == before


def test_reject_releases_only_own_hold_and_budget(tmp_path):
    _, record, _, item = prepared(tmp_path)
    assert graph.reject(record, item["id"], reason="证据不足")
    value = ledger.state(record)
    assert value["plan_holds"] == {}
    assert graph.effective_flow(record).version == 1
    assert value["lineage_budget"]["reservations"]["expansion:" + item["id"]]["status"] == "released"
    before = copy.deepcopy(record)
    assert graph.reject(record, item["id"], reason="证据不足") is False
    with pytest.raises(ValueError):
        graph.reject(record, item["id"], reason="改写历史")
    assert record == before


def test_illegal_candidates_do_not_change_any_state(tmp_path):
    _, record, attempt, item = prepared(tmp_path)
    graph.reject(record, item["id"], reason="重新审查")
    for mutation in ("cycle", "constraints", "parent_checks", "existing_node", "wrong_lineage"):
        candidate = copy.deepcopy(item["fragment"])
        children = candidate["child_ids"]
        if mutation == "cycle":
            candidate["contracts"][children[0]]["depends_on"] = ["work"]
        elif mutation == "constraints":
            candidate["contracts"][children[0]]["requirement_ids"] = []
        elif mutation == "parent_checks":
            candidate["contracts"]["work"]["checks"] = []
        elif mutation == "existing_node":
            next(node for node in candidate["flow"]["nodes"] if node["node_id"] == "deliver")["responsibility"] = "改变"
        else:
            candidate["lineage_id"] = "new-budget"
        before = copy.deepcopy(record)
        with pytest.raises(ValueError):
            graph.prepare(record, candidate, publication_id="new", attempt_id=attempt["id"])
        assert record == before


def test_overlapping_prepare_is_rejected(tmp_path):
    _, record, attempt, item = prepared(tmp_path)
    before = copy.deepcopy(record)
    with pytest.raises(ValueError):
        graph.prepare(record, item["fragment"], publication_id="other", attempt_id=attempt["id"])
    assert record == before
