import asyncio
import json
import time
from datetime import datetime, timezone

import pytest

from mewcode import client as client_module
from mewcode.orchestration.extract import collect_model_text
from mewcode.orchestration.graph_build_control import ModelStopped, SoftBuildControl
from mewcode.orchestration.graph_generation import graph_process_state
from mewcode.tools.base import TextDelta, StreamEnd


@pytest.mark.asyncio
async def test_soft_boundary_can_continue_and_model_can_stop_before_boundary():
    policy = SoftBuildControl({"active_seconds": 1801})
    seen = []
    async def model(system, user):
        seen.append(json.loads(user))
        return json.dumps({"control": {"action": "continue", "reason": "已找到新的修复方向", "next_step": "先修复返修回边，再核验交付路径"}})
    await policy.assess("{}", ask_model=model, feedback={"phase": "repair"})
    assert len(seen) == 1 and policy.state["active_seconds"] == 1801
    assert not policy.due
    with pytest.raises(ModelStopped, match="保存草案"):
        await policy.assess('```json\n{"control":{"action":"stop","reason":"保存草案","next_step":""}}\n```',
                            ask_model=model, feedback={})
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_invalid_and_repeated_continuation_cannot_spin_forever():
    policy = SoftBuildControl()
    async def invalid(system, user):
        return '{"control":{"action":"continue","reason":"继续","next_step":""}}'
    with pytest.raises(ModelStopped) as failure:
        await policy.assess("{}", ask_model=invalid, stalled=True, feedback={})
    assert failure.value.code == "invalid_control"
    decision = {"control": {"action": "continue", "reason": "再修正", "next_step": "同一修法"}}
    await policy.assess(decision, ask_model=invalid, feedback={})
    with pytest.raises(ModelStopped) as failure:
        await policy.assess(decision, ask_model=invalid, stalled=True, feedback={})
    assert failure.value.code == "repeated_control"


@pytest.mark.asyncio
async def test_graph_wait_policy_does_not_fall_back_to_scheduler_total_deadline(monkeypatch):
    monkeypatch.setattr(client_module, "LLM_REQUEST_TIMEOUT", .005)
    closed = []
    class SlowModel:
        async def stream(self, conversation, system=""):
            try:
                await asyncio.sleep(.03)
                yield TextDelta('{"upserts":[]}')
                yield StreamEnd("end_turn")
            finally:
                closed.append(True)
    result = await collect_model_text(SlowModel(), system="", user_text="", total_timeout=None,
                                      first_event_timeout=.2, soft_idle_warning=.01)
    assert result == '{"upserts":[]}' and result.active_seconds >= .03
    assert closed == [True]


def test_soft_reminder_is_visible_during_live_reasoning_but_excludes_queue_time():
    state = {"candidate": {"status": "working", "document": {"entries": []},
        "autobuild": {"phase": "extracting", "status": "working", "control": {"active_seconds": 0}}},
        "coverage": {"processed": 0, "total": 1, "pending": 1}}
    progress = {"phase": "model", "request_started_at": datetime.fromtimestamp(time.time()-1801, timezone.utc).isoformat(),
                "model": {"stage": "thinking"}}
    assert graph_process_state(state, True, progress)["soft_boundary"]
    progress["model"]["stage"] = "queued"
    assert not graph_process_state(state, True, progress)["soft_boundary"]
    progress["model"] = None
    assert not graph_process_state(state, True, progress)["soft_boundary"]
