import copy

import pytest

from mewcode.kernel import collaboration as ledger
from mewcode.orchestration import collab_coordinator
from mewcode.orchestration.collab_coordinator import CollaborationCoordinator
from tests.test_collaboration_mode import CATALOG, RUN_ID, store_for


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["retry", "propose_patch"])
async def test_instruction_list_is_preserved_for_fresh_and_legacy_cached_decisions(tmp_path, action):
    if action == "propose_patch":
        from tests.test_autonomous_context_runtime import retry_store
        from tests.test_autonomous_patch import patch_flow
        store = retry_store(tmp_path, patch_flow())
    else:
        store = store_for(tmp_path)
    requests = []
    raw = {"action": action, "reason": "仅补齐宿主回执", "instructions": ["保留原交付", "复核执行记录"]}
    if action == "propose_patch":
        value = ledger.state(store.load(RUN_ID))
        contract = value["contracts"]["work"]
        raw.update(basis_hash=ledger.dependencies(value, "work"),
                   target_refs=[f"work@{contract['version']}"],
                   expected_improvement="补齐当前检查的宿主证据",
                   evidence_refs=["host receipts"],
                   verification_refs=[row["id"] for row in contract["checks"]])
    async def ask(*args):
        requests.append(args)
        return copy.deepcopy(raw)
    coordinator = CollaborationCoordinator(store, RUN_ID, CATALOG, ask=ask)
    first = await coordinator.decide(event_id="failure", node_id="work", evidence={"missing": "host receipts"})
    assert first["instructions"] == "保留原交付\n复核执行记录"
    record = store.load(RUN_ID)
    call = next(call for call in record["collaboration"]["calls"].values() if call["kind"] == "coordinator")
    assert call["result"] == raw
    store.mutate(RUN_ID, lambda saved: next(iter(saved["collaboration"]["diagnostics"].values()))["result"].update(
        instructions=copy.deepcopy(raw["instructions"])), durable=True)
    cached = await coordinator.decide(event_id="failure", node_id="work", evidence={"missing": "host receipts"})
    assert cached == first
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["retry", "propose_patch"])
@pytest.mark.parametrize("field,value", [("reason", ["not text"]), ("instructions", {"step": "not a list"}),
                                        ("instructions", ["valid", {"invalid": "step"}])])
async def test_invalid_coordinator_fields_stop_with_a_validation_error(tmp_path, action, field, value):
    store = store_for(tmp_path)
    async def ask(*args):
        return {"action": action, "reason": "retry", "instructions": "keep scope", field: value}
    with pytest.raises(ValueError):
        await CollaborationCoordinator(store, RUN_ID, CATALOG, ask=ask).decide(
            event_id="failure", node_id="work", evidence={"failure": True})


@pytest.mark.asyncio
async def test_patch_format_recovery_reuses_the_finished_call_without_new_model_request(tmp_path, monkeypatch):
    from tests.test_autonomous_context_runtime import retry_store
    from tests.test_autonomous_patch import decision, patch_flow

    store = retry_store(tmp_path, patch_flow())
    requests = []
    async def ask(model, payload, system):
        raw = decision(payload)
        raw["instructions"] = ["只修正合计", "保留行数"]
        requests.append(copy.deepcopy(raw))
        return raw

    coordinator = CollaborationCoordinator(store, RUN_ID, CATALOG, ask=ask)
    kwargs = dict(event_id="failed-check", node_id="work", evidence={"total": "failed"})
    with monkeypatch.context() as legacy:
        # The previous adapter passed patch instruction lists through unchanged.
        legacy.setattr(collab_coordinator, "_decision_fields", lambda result: result)
        with pytest.raises(ValueError, match="绑定当前合同"):
            await coordinator.decide(**kwargs)
    before = ledger.state(store.load(RUN_ID))
    assert next(iter(before["diagnostics"].values()))["status"] == "pending"
    assert next(iter(before["calls"].values()))["status"] == "finished"

    result = await coordinator.decide(**kwargs)
    assert result == {**requests[0], "instructions": "只修正合计\n保留行数"}
    after = ledger.state(store.load(RUN_ID))
    assert after["calls"] == before["calls"]
    assert after["contracts"] == before["contracts"]
    assert after["lineage_budget"] == before["lineage_budget"]
    assert len(requests) == 1
    assert await coordinator.decide(**kwargs) == result
    assert len(requests) == 1
