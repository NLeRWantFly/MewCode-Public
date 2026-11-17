"""Adopting a semantic revision is transactional and billed once per lineage."""
from __future__ import annotations

import copy

import pytest

from mewcode.kernel import collaboration as ledger
from mewcode.kernel import lineage_budget
from tests.test_collaboration_mode import core


def _user_proposal(value, proposal_id, body):
    revised = copy.deepcopy(value["entries"]["goal"])
    revised["body"] = body
    source = "u_" + proposal_id
    value.setdefault("user_sources", {})[source] = {"text": body}
    return ledger.proposal(value, proposal_id=proposal_id, changes={"goal": revised}, evidence=[source],
                           actor="user", base_version=value["spec_version"], epoch=value["coordinator_epoch"])


def _review(proposal_id):
    return {"passed": True, "evidence": ["u_" + proposal_id]}


def _bind(value, keys, lineage):
    for key in keys:
        value["contracts"][key]["lineage_id"] = lineage
        value["contracts"][key]["requirement_ids"] = ["goal"]
        value["contracts"][key]["constraint_ids"] = []


def test_same_lineage_charges_one_revision_and_duplicate_adopt_is_free():
    value = core()
    keys = sorted(value["contracts"])
    assert len(keys) >= 2
    _bind(value, keys[:2], "shared-lineage")
    value["lineage_budget"]["limits"]["lineage"]["revisions"] = 2

    _user_proposal(value, "rev-1", "第一次修订")
    assert ledger.adopt(value, "rev-1", review=_review("rev-1"),
                        proposer_vendor="A", reviewer_vendor="B") is True
    assert value["spec_version"] == 2
    assert value["lineage_budget"]["root"]["used"]["revisions"] == 1
    assert value["proposals"]["rev-1"]["affected_lineages"] == ["shared-lineage"]

    _user_proposal(value, "rev-2", "第二次修订")
    assert ledger.adopt(value, "rev-2", review=_review("rev-2"),
                        proposer_vendor="A", reviewer_vendor="B") is True
    assert value["lineage_budget"]["root"]["used"]["revisions"] == 2
    assert value["lineage_budget"]["lineages"]["shared-lineage"]["used"]["revisions"] == 2

    adopted = copy.deepcopy(value)
    assert ledger.adopt(value, "rev-2", review=_review("rev-2"),
                        proposer_vendor="A", reviewer_vendor="B") is False
    assert value == adopted


def test_second_lineage_without_budget_aborts_adoption_atomically():
    value = core()
    keys = sorted(value["contracts"])
    assert len(keys) >= 2
    _bind(value, [keys[0]], "lineage-a")
    _bind(value, [keys[1]], "lineage-b")
    value["lineage_budget"]["limits"]["lineage"]["revisions"] = 1

    seed = "seed:lineage-b"
    lineage_budget.reserve(value, seed, lineage_id="lineage-b", kind="revisions")
    lineage_budget.settle(value, seed)

    _user_proposal(value, "rev-x", "跨越两个谱系")
    before = copy.deepcopy(value)
    with pytest.raises(ValueError):
        ledger.adopt(value, "rev-x", review=_review("rev-x"), proposer_vendor="A", reviewer_vendor="B")
    assert value == before


def test_same_vendor_review_is_rejected_without_touching_the_ledger():
    value = core()
    _bind(value, sorted(value["contracts"])[:1], "lineage-solo")
    _user_proposal(value, "rev-vendor", "同厂商复核")
    before = copy.deepcopy(value)
    with pytest.raises(ValueError, match="独立复核"):
        ledger.adopt(value, "rev-vendor", review=_review("rev-vendor"),
                     proposer_vendor="V", reviewer_vendor="V")
    assert value == before


def test_illegal_contract_topology_is_rejected_atomically():
    value = core()
    node = "work"
    value["entries"][node] = {**copy.deepcopy(value["entries"]["goal"]),
                              "id": node, "kind": "node", "data": {}}
    replacement = copy.deepcopy(value["entries"][node])
    contract = {**replacement.get("data", {}).get("contract", {}),
                "requirement_ids": ["other_requirement"]}
    replacement["data"] = {**replacement.get("data", {}), "contract": contract}
    ledger.proposal(value, proposal_id="rev-topology", changes={node: replacement}, evidence=["e"],
                    actor="user", base_version=value["spec_version"], epoch=value["coordinator_epoch"])

    before = copy.deepcopy(value)
    with pytest.raises(ValueError, match="拓扑变更"):
        ledger.adopt(value, "rev-topology", review={"passed": True, "evidence": ["e"]},
                     proposer_vendor="A", reviewer_vendor="B")
    assert value == before
