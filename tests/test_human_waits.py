import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.agent import AskUserRequest, HumanOnCallRequest, PermissionRequest, PermissionResponse, StreamText, ToolUseEvent
from mewcode.experts.waits import HumanWaitTimeout, wait_for_human
from mewcode.experts.waits import wait_for_interaction
from mewcode.llm_watchdog import watch_provider_stream
from mewcode.client import LLMError
from tests.test_acceptance_completion import old_false_completion


@pytest.mark.asyncio
async def test_permission_heartbeat_preserves_future_past_timeout_until_user_reply(tmp_path):
    runs, run_id = old_false_completion(tmp_path)
    runs.mutate(run_id, lambda s: s["node_states"]["work"].update(pending_permission={"tool_id": "original"}))
    future = asyncio.get_running_loop().create_future()
    events = []

    async def emit(event):
        events.append(event)
        if event["active"]:
            assert not future.cancelled()

    waiting = asyncio.create_task(wait_for_human(future, kind="permission", emit=emit, store=runs, run_id=run_id,
                             stage_key="work", wait_id="original", heartbeat=.01, soft_timeout=.02))
    await asyncio.sleep(.06)
    assert not waiting.done()
    future.set_result(PermissionResponse.DENY)
    assert await waiting == PermissionResponse.DENY
    assert runs.load(run_id)["node_states"]["work"]["pending_permission"]["tool_id"] == "original"
    assert sum(e["active"] for e in events) >= 2 and not events[-1]["active"]
    assert not future.cancelled()


@pytest.mark.asyncio
async def test_provider_watchdog_closes_silent_stream():
    closed = []

    async def stream():
        try:
            await asyncio.sleep(10)
            yield None
        finally:
            closed.append(True)

    with pytest.raises(LLMError, match="provider stalled"):
        async for _ in watch_provider_stream(stream(), first_timeout=.02):
            pass
    assert closed


@pytest.mark.asyncio
async def test_parallel_human_questions_queue_without_overwriting(tmp_path):
    runs, run_id = old_false_completion(tmp_path)
    session = SimpleNamespace()
    requests = [AskUserRequest(request_id=name, tool_name="AskUserQuestion", questions=[{"question": name}],
        future=asyncio.get_running_loop().create_future()) for name in ("one", "two")]
    visible = []
    async def emit(event):
        if event["type"] == "ask_user":
            visible.append(event["request_id"])
    tasks = [asyncio.create_task(wait_for_interaction(request, kind="ask_user", session=session, emit=emit,
        store=runs, run_id=run_id, stage_key=request.request_id)) for request in requests]
    try:
        for _ in range(10):
            await asyncio.sleep(.001)
            if visible:
                break
        assert visible == ["one"]
        assert runs.load(run_id)["node_states"]["two"]["pending_interaction"]["request_id"] == "two"
        requests[0].future.set_result("first")
        assert await tasks[0] == "first"
        for _ in range(10):
            await asyncio.sleep(.001)
            if len(visible) == 2:
                break
        assert visible == ["one", "two"]
        requests[1].future.set_result("second")
        assert await tasks[1] == "second"
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["permission", "human_oncall", "ask_user"])
@pytest.mark.parametrize("context_change", ["none", "plan", "legacy"])
async def test_web_timeout_and_replay_original_request(tmp_path, monkeypatch, kind, context_change):
    from mewcode import expert_web
    from mewcode.agent import Agent, ToolResultEvent, TurnComplete
    from mewcode.conversation import ConversationManager
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.experts.flow import ExpertFlowSpec, ExpertFlowStore
    from mewcode.orchestration.plan_session import PlanSessionStore
    from mewcode.tools import ToolRegistry
    from tests.test_expert_flow import _profile

    # This fake registry has no visual observer. Ambient desktop settings
    # must not resolve a real observer or alter a saved-question replay test.
    monkeypatch.setattr('mewcode.permissions.review_client.load_review_model_settings', lambda: {})

    monkeypatch.setenv("MEWCODE_EXPERT_HITL_SOFT_TIMEOUT", ".05")
    project = tmp_path / "project"
    project.mkdir()
    runs = ExpertRunStore(tmp_path / "runs", storage="sqlite")
    flows = ExpertFlowStore(tmp_path / "flows.json")
    flows.put(project, ExpertFlowSpec.from_value({"flow_id": "wait", "label": "wait", "finalizer_node_id": "done",
        "nodes": [{"node_id": "done", "role": "manager", "label": "确认",
                   "tools": ["ReadFile"], "capabilities": {}}], "edges": []}))
    resumed, calls, events = False, [], []

    class FakeAgent(Agent):
        client = SimpleNamespace(expose_reasoning=False)
        total_input_tokens = total_output_tokens = 0

        def __init__(self, policy):
            from tests.test_agent import MockLLMClient
            from mewcode.tools.submit_handoff import SubmitHandoff
            super().__init__(MockLLMClient([]), ToolRegistry(), 'anthropic', str(project))
            self.registry.register(SubmitHandoff(policy.handoff_holder))

        def set_review_authorization_context(self, context):
            pass

        async def run(self, conversation):
            calls.append(list(conversation.history))
            if resumed:
                assert self.workflow_context.snapshot['waiting_for'] == 'agent'
                assert '处理当前宿主保存的待答问题' not in self.workflow_context.snapshot['next_action']
            if not resumed:
                future = asyncio.get_running_loop().create_future()
                tool = {"permission": "Bash", "human_oncall": "human_oncall", "ask_user": "AskUserQuestion"}[kind]
                yield ToolUseEvent(tool, "same-id", {})
                if kind == "permission":
                    yield PermissionRequest(tool_name=tool, description="confirm", future=future, tool_id="same-id")
                else:
                    cls = HumanOnCallRequest if kind == "human_oncall" else AskUserRequest
                    yield cls("same-id", tool, [{"name": "choice", "question": "confirm"}], future)
                await future
            from mewcode.tools.submit_handoff import SubmitHandoffParams
            params = SubmitHandoffParams(summary='done', inline_outputs=[{'name': 'result', 'content': 'done'}])
            yield ToolUseEvent('SubmitHandoff', 'delivery', params.model_dump())
            result = await self.registry.get('SubmitHandoff').execute(params)
            assert not result.is_error
            yield ToolResultEvent('delivery', 'SubmitHandoff', result.output, False, .001)
            yield TurnComplete(1)
            conversation.add_assistant_message("done")
            yield StreamText("done")

    monkeypatch.setattr(expert_web, "_build_role_agent", lambda *args: (
        FakeAgent(args[4]), None, SimpleNamespace(conversation=ConversationManager(), aclose=AsyncMock())))
    durable = SimpleNamespace(meta=SimpleNamespace(message_count=0), append=lambda *args: None,
                              append_expert_run=lambda *args, **kwargs: None)
    session = SimpleNamespace(work_dir=str(project), conversation_id="c", multi_agent=False,
        session=durable, conversation=ConversationManager(), expert_run_id="", expert_control_action="pause",
        context=SimpleNamespace(get=lambda key, default=None: default, slots=SimpleNamespace(resolve=lambda key: {})),
        task_manager=SimpleNamespace(list_tasks=lambda: [], mark_notified=lambda ids: None),
        expert_child_event_callback=None, status_payload=lambda: {}, tasks_payload=lambda: [])

    async def send(event):
        events.append(event)
        if kind == 'permission' and not resumed and event['type'] == 'expert_role_wait' and event.get('active'):
            session._perm_future.cancel()
        if event["type"] == kind and resumed:
            # Permission cards have their own request epoch/identity; the
            # original operation is bound by tool_id across restoration.
            identity = event.get('tool_id') if kind == 'permission' else event.get('request_id')
            assert event["resumed"] and identity == "same-id"
            future = session._perm_future if kind == "permission" else getattr(session, "_" + kind + "_request").future
            future.set_result(PermissionResponse.ALLOW if kind == "permission" else {"choice": "continue"})

    kwargs = dict(session=session, registry_service=SimpleNamespace(catalog=AsyncMock(return_value={"providers": []}), resolve=lambda model: None),
        profile_store=SimpleNamespace(get=lambda p: _profile()), flow_store=flows,
        plan_store=PlanSessionStore(tmp_path / "plans.json"), run_store=runs)
    await asyncio.wait_for(expert_web.run_expert_chat(SimpleNamespace(send_json=send), "confirm", confirmed_new_flow=flows.get(project), **kwargs), 180)
    snapshot = runs.latest_resumable("c")
    assert snapshot["status"] in {"paused", "needs_attention"}
    pending_key = "pending_permission" if kind == "permission" else "pending_interaction"
    assert snapshot["node_states"]["done"][pending_key]
    assert snapshot["node_states"]["done"]["tools"]["same-id"]["status"] in {
        "queued", "interrupted_before_execution",
    }
    if context_change == "plan":
        flow_value = flows.get(project).as_dict()
        flow_value["version"] = int(flow_value.get("version") or 1) + 1
        flow_value["nodes"][0]["responsibility"] = "当前输入已补齐，仍不得推定用户授权"
        flows.put(project, ExpertFlowSpec.from_value(flow_value))
    elif context_change == "legacy":
        runs.mutate(snapshot["run_id"], lambda s: s["node_states"]["done"].pop("input_revision", None))
    resumed = True
    await asyncio.wait_for(expert_web.run_expert_chat(SimpleNamespace(send_json=send), "继续",
        resume_run_id=snapshot["run_id"], **kwargs), 180)
    # Finish with the current structured protocol so recovery of the original
    # request is checked independently of missing-delivery escalation.
    assert runs.load(snapshot["run_id"])["status"] == "completed"
    assert len(calls) == 2
    assert any("same-id" in str(m.content) for m in calls[-1])
    latest = runs.load(snapshot["run_id"])["node_states"]["done"]
    if latest.get("superseded_questions"):
        assert latest["superseded_questions"][-1]["answered"] is False
        assert latest["superseded_questions"][-1]["pending"]["request_id"] == "same-id"
        assert any("这不是同意或授权" in str(m.content) for m in calls[-1])
        assert not any(event.get("resumed") and event["type"] == kind for event in events)
    else:
        assert any(event.get("resumed") and event["type"] == kind for event in events)
