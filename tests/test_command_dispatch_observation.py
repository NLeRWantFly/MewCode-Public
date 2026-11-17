import copy
from unittest.mock import AsyncMock

import pytest

from mewcode.experts.command_receipts import observe_requests, reconcile_helper_receipts, request_receipts
from mewcode.experts.command_scope import wrap_allowlisted_command
from mewcode.tools.base import ToolResult
from mewcode.tools.bash import Bash, Params as BashParams
from tests.test_command_request_receipts import command_request


def pending():
    rows = request_receipts("N01", "current", [command_request()], status="outcome_unknown")
    rows[0]["dispatch_id"] = "dispatch-current"
    return rows


@pytest.mark.asyncio
@pytest.mark.parametrize("invoked", [False, True])
async def test_closed_guard_distinguishes_no_invocation_from_unknown_execution(invoked):
    context = {"dispatch_id": "dispatch-current"}
    tool = Bash()
    tool.execute = AsyncMock(side_effect=RuntimeError("interrupted host observation"))
    guarded = wrap_allowlisted_command(tool, (command_request()["command"],), receipt_context=context)
    if invoked:
        with pytest.raises(RuntimeError):
            await guarded.execute(BashParams(command=command_request()["command"]))
    context["dispatch_observation"]["closed"] = True
    rows = observe_requests(pending(), [], dispatch_observation=context["dispatch_observation"])
    assert rows[0]["status"] == ("outcome_unknown" if invoked else "not_started")
    if not invoked:
        assert rows[0]["command_started"] is False
        assert rows[0]["evidence_source"] == "host_execution"
        assert rows[0]["exit_code"] is None
    assert tool.execute.await_count == int(invoked)


@pytest.mark.parametrize("change", [{"closed": False}, {"dispatch_id": "other"}, {"guarded": False},
                                   {"allowed_commands": []}, {"attempted_commands": [command_request()["command"]]}])
def test_absent_or_unbound_host_closure_cannot_clear_unknown(change):
    observation = {"version": 1, "guarded": True, "closed": True, "dispatch_id": "dispatch-current",
                   "allowed_commands": [command_request()["command"]], "attempted_commands": [], **change}
    assert observe_requests(pending(), [], dispatch_observation=observation)[0]["status"] == "outcome_unknown"


def closed_legacy_helper():
    return {"capability_snapshot_version": 1, "status": "needs_attention", "request_error": "",
        "started_at": 100, "completed_at": 120, "attempt": 6, "input_revision": "current",
        "command_dispatch_id": "dispatch-current", "available_tools": ["Bash", "ReadStageResult", "SubmitHandoff"],
        "tools": {"read": {"name": "ReadStageResult", "status": "completed", "attempt": 6},
                  "handoff": {"name": "SubmitHandoff", "status": "completed", "attempt": 6,
                              "output": "model incorrectly claims prior command is unknown"}}}


def test_finished_legacy_helper_with_no_command_declarations_can_be_reconciled():
    rows, helper = pending(), closed_legacy_helper()
    before = copy.deepcopy((rows, helper))
    actual = reconcile_helper_receipts(rows, helper)
    assert actual[0]["status"] == "not_started" and actual[0]["command_started"] is False
    assert actual[0]["dispatch_id"] == "dispatch-current"
    assert (rows, helper) == before


@pytest.mark.parametrize("case", ["paused", "incomplete", "provider_error", "wider_tools", "bash_declared",
                                 "older_attempt", "wrong_dispatch", "wrong_revision", "no_handoff"])
def test_legacy_absence_is_not_inferred_from_incomplete_or_unsafe_ledgers(case):
    helper = closed_legacy_helper()
    if case == "paused":
        helper["status"] = "paused"
    elif case == "incomplete":
        helper.pop("completed_at")
    elif case == "provider_error":
        helper["request_error"] = "stream interrupted"
    elif case == "wider_tools":
        helper["available_tools"].append("Agent")
    elif case == "bash_declared":
        helper["tools"]["unknown"] = {"name": "Bash", "status": "running", "attempt": 6,
                                      "args": {"command": command_request()["command"]}}
    elif case == "older_attempt":
        helper["tools"]["handoff"]["attempt"] = 5
    elif case == "wrong_dispatch":
        helper["command_dispatch_id"] = "other"
    elif case == "wrong_revision":
        helper["input_revision"] = "other"
    elif case == "no_handoff":
        helper["tools"].pop("handoff")
    assert reconcile_helper_receipts(pending(), helper)[0]["status"] == "outcome_unknown"


@pytest.mark.asyncio
async def test_rejected_non_allowlisted_command_does_not_touch_invocation_ledger():
    context = {"dispatch_id": "dispatch-current"}
    tool = Bash()
    tool.execute = AsyncMock(return_value=ToolResult(output="unexpected"))
    guarded = wrap_allowlisted_command(tool, (command_request()["command"],), receipt_context=context)
    assert (await guarded.execute(BashParams(command="other-command"))).is_error
    assert context["dispatch_observation"]["attempted_commands"] == []
    tool.execute.assert_not_awaited()
