import pytest

from mewcode.experts.segment_context import project_loop_context


def test_foreach_prompt_contains_current_item_and_all_acceptance_but_not_other_items():
    context = {"mode": "foreach", "contract_hash": "frozen", "exit_condition": "all reviewed",
        "criteria": [{"criterion_id": "C1", "acceptance_test": "independent visual PASS"}],
        "items": [{"item_id": "one", "goal": "private page one"},
                  {"item_id": "two", "goal": "private page two"}],
        "current_item": {"item_id": "one", "goal": "private page one"},
        "completed_items": [], "token_budget_authorization": "frozen authorization"}
    projected, receipt = project_loop_context(context)
    assert "items" not in projected and projected["item_count"] == 2
    assert projected["current_item"] == context["items"][0]
    assert projected["criteria"] == context["criteria"]
    assert projected["token_budget_authorization"] == context["token_budget_authorization"]
    assert receipt["criteria_preserved"] and receipt["authorization_preserved"]
    assert context["items"][1]["goal"] not in str(projected)
    assert len(context["items"]) == 2  # Host contract is untouched.


def test_required_contract_too_large_fails_instead_of_truncating():
    with pytest.raises(ValueError, match="exceeds context cap"):
        project_loop_context({"criteria": [{"acceptance_test": "x" * 5000}]}, max_chars=1024)
