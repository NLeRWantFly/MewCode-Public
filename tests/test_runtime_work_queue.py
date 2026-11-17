from mewcode.orchestration.loops import LoopContract


def test_finite_declared_items_select_serial_host_queue_without_changing_acceptance():
    contract = LoopContract.from_value({
        "loop_id": "review-items", "entry_node_id": "work", "check_node_id": "check",
        "node_ids": ["work", "check"], "exit_condition": "all items independently reviewed",
        "criteria": [{"criterion_id": "C1", "acceptance_test": "source evidence and independent PASS"}],
        "items": [{"item_id": "page-1", "goal": "verify page one"},
                  {"item_id": "page-2", "goal": "verify page two"}],
    })
    assert contract.mode == "foreach"
    assert [item["item_id"] for item in contract.items] == ["page-1", "page-2"]
    assert contract.criteria[0]["acceptance_test"] == "source evidence and independent PASS"
    assert contract.as_dict()["mode"] == "foreach"
