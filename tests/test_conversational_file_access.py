"""Human file grants must stay exact, live-bound, and independent of execution."""
import os

import pytest
from pydantic import ValidationError

from mewcode.agent import AskUserRequest
from mewcode.permissions.file_access import request_file_access
from mewcode.permissions import PermissionMode
from mewcode.tools.ask_user import AskUserParams
from mewcode.tools.base import ToolCallComplete, ToolResult
from tests.test_auto_denial_recovery import _make_agent, _write, _deny


def setup_request(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    target = tmp_path / "outside" / "target.txt"
    target.parent.mkdir()
    target.write_text("original", encoding="utf-8")
    agent, checker, reviewer, _, conversation = _make_agent(work, [], [])
    checker.sandbox._allowed_roots = [work.resolve()]
    agent._current_conversation = conversation
    args = {"purpose": "authorization", "file_access": {
        "file_path": str(target), "access": "read_write", "reason": "应用用户要求的源码修复"}}
    call = ToolCallComplete("file-consent", "AskUserQuestion", args)
    return agent, checker, target, call


async def collect(agent, call, answer="grant_file", mutate=None):
    params = AskUserParams.model_validate(call.arguments)
    events = []
    async for item in request_file_access(agent, call, params.file_access):
        events.append(item)
        if isinstance(item, AskUserRequest):
            if mutate:
                mutate(item)
            item.future.set_result({"file_access": answer} if answer is not None else None)
    return events


@pytest.mark.asyncio
async def test_explicit_consent_grants_only_file_and_does_not_execute(tmp_path):
    agent, checker, target, call = setup_request(tmp_path)
    events = await collect(agent, call)
    assert isinstance(events[0], AskUserRequest)
    assert str(target.resolve()) in events[0].questions[0]["message"]
    assert "重启后失效" in events[0].questions[0]["context"]
    assert events[-1].host_observation["file_access_grant"]["execution_authorized"] is False
    assert checker.sandbox.check(str(target))[0]
    assert not checker.sandbox.check(str(target.parent / "sibling.txt"))[0]
    assert not checker.sandbox.dir_grants
    assert not checker._approved_calls
    assert target.read_text(encoding="utf-8") == "original"


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["deny_file", "已配置", "授权此文件", "yes", "", None])
async def test_only_exact_human_choice_grants(tmp_path, answer):
    agent, checker, target, call = setup_request(tmp_path)
    events = await collect(agent, call, answer)
    assert events[-1].is_error
    assert not checker.sandbox.file_grants


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["mode", "task", "root", "directory", "sandbox", "user_context"])
async def test_waiting_request_is_invalidated_by_context_change(tmp_path, change):
    agent, checker, target, call = setup_request(tmp_path)
    original_sandbox = checker.sandbox
    def mutate(request):
        if change == "mode":
            checker.mode = PermissionMode.PLAN
        elif change == "task":
            checker.task_id = "different-task"
        elif change == "root":
            agent.work_dir = str(tmp_path)
        elif change == "directory":
            target.unlink()
            target.mkdir()
        elif change == "sandbox":
            from mewcode.permissions import PathSandbox
            checker.sandbox = PathSandbox(str(tmp_path))
        elif change == "user_context":
            agent._current_conversation.add_user_message("停止修改，保留原文件")
    events = await collect(agent, call, mutate=mutate)
    assert "FILE_ACCESS_CONTEXT_CHANGED" in events[-1].output
    assert not original_sandbox.file_grants
    assert not checker.sandbox.file_grants


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["relative", "directory", "sensitive", "policy", "denied_rule"])
async def test_hard_boundaries_never_offer_confirmation(tmp_path, kind):
    agent, checker, target, call = setup_request(tmp_path)
    if kind == "relative":
        path = "../outside/target.txt"
    elif kind == "directory":
        path = str(target.parent)
    elif kind == "sensitive":
        path = str(tmp_path / ".ssh" / "id_rsa")
    elif kind == "policy":
        path = str(checker.sandbox.project_root / ".mewcode" / "permissions.grants.json")
    else:
        path = str(target)
        from mewcode.permissions.checker import Decision
        checker.check = lambda *args: Decision("deny", "Explicit deny rule", "rule_engine")
    call.arguments["file_access"]["file_path"] = path
    events = await collect(agent, call)
    assert len(events) == 1 and isinstance(events[0], ToolResult)
    assert events[0].is_error
    assert not checker.sandbox.file_grants


@pytest.mark.asyncio
async def test_new_file_grant_does_not_create_target_or_grant_parent(tmp_path):
    agent, checker, target, call = setup_request(tmp_path)
    target.unlink()
    events = await collect(agent, call)
    assert not events[-1].is_error
    assert not target.exists()
    assert checker.sandbox.check(str(target))[0]
    assert not checker.sandbox.check(str(target.parent))[0]


@pytest.mark.asyncio
async def test_cancelled_question_does_not_grant(tmp_path):
    import asyncio
    agent, checker, target, call = setup_request(tmp_path)
    params = AskUserParams.model_validate(call.arguments)
    generator = request_file_access(agent, call, params.file_access)
    request = await anext(generator)
    request.future.cancel()
    with pytest.raises(asyncio.CancelledError):
        await anext(generator)
    assert not checker.sandbox.file_grants


@pytest.mark.asyncio
async def test_host_resolved_choice_wins_over_raw_answer(tmp_path):
    agent, checker, target, call = setup_request(tmp_path)
    def deny(request):
        request.resolved_answers = {"file_access": "deny_file"}
        request.answer_notes = {"file_access": "grant_file"}
    events = await collect(agent, call, mutate=deny)
    assert events[-1].is_error
    assert not checker.sandbox.file_grants


@pytest.mark.asyncio
async def test_existing_access_does_not_interrupt(tmp_path):
    agent, checker, target, call = setup_request(tmp_path)
    checker.sandbox.grant_file(str(target), writable=True)
    events = await collect(agent, call)
    assert len(events) == 1 and "ALREADY_AVAILABLE" in events[0].output


@pytest.mark.parametrize("extra", [{"purpose": "user_input"}, {"review_call_ref": "stale"},
    {"review_call_refs": ["stale"]}, {"questions": [{"type": "radio", "name": "q", "message": "approve?"}]}])
def test_schema_disallows_mixed_or_fake_authority(extra):
    args = {"purpose": "authorization", "file_access": {"file_path": "absolute.txt", "reason": "edit"}}
    args.update(extra)
    with pytest.raises(ValidationError):
        AskUserParams.model_validate(args)


@pytest.mark.asyncio
async def test_agent_dispatch_grant_does_not_bypass_review(tmp_path):
    agent, checker, target, call = setup_request(tmp_path)
    results = []
    async for event in agent._execute_tool(call):
        if isinstance(event, AskUserRequest):
            event.future.set_result({"file_access": "grant_file"})
        elif isinstance(event, tuple):
            results.append(event[0])
    assert any("FILE_ACCESS_GRANTED" in result.output for result in results)
    assert checker.sandbox.file_grants[os.path.normcase(str(target.resolve()))]
    # Even after the file boundary is opened, the exact write can be denied.
    agent.safety_reviewer._ask_model.side_effect = [_deny("semantic_risk")]
    results = []
    async for event in agent._execute_tool(_write(target, "must not write")):
        if isinstance(event, tuple):
            results.append(event[0])
    assert any(result.is_error and "AUTO_DENIED" in result.output for result in results)
    agent.safety_reviewer._ask_model.assert_awaited_once()
    assert target.read_text(encoding="utf-8") == "original"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [PermissionMode.PLAN, PermissionMode.BYPASS, PermissionMode.DONT_ASK])
async def test_noninteractive_or_readonly_mode_cannot_grant(tmp_path, mode):
    agent, checker, target, call = setup_request(tmp_path)
    checker.mode = mode
    events = await collect(agent, call)
    assert len(events) == 1 and events[0].is_error
    assert not checker.sandbox.file_grants


@pytest.mark.asyncio
async def test_frontend_preserves_host_question_without_model_preparation(tmp_path):
    import copy
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from frontend.server import _prepare_chat_choice_event
    agent, checker, target, call = setup_request(tmp_path)
    generator = request_file_access(agent, call, AskUserParams.model_validate(call.arguments).file_access)
    request = await anext(generator)
    original = copy.deepcopy(request.questions)
    socket = SimpleNamespace(send_json=AsyncMock())
    await _prepare_chat_choice_event(SimpleNamespace(orchestration_planning=True), request, socket)
    assert request.questions == original and not request.choice_bundle
    socket.send_json.assert_not_awaited()
    await generator.aclose()
    assert request.future.cancelled()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["desktop", "remote"])
async def test_real_response_dispatch_binds_conversation_and_request(tmp_path, transport):
    from types import SimpleNamespace
    from frontend import server
    from frontend.prompt_dispatch import dispatch, PromptError
    agent, checker, target, call = setup_request(tmp_path)
    generator = request_file_access(agent, call, AskUserParams.model_validate(call.arguments).file_access)
    request = await anext(generator)
    runtime = SimpleNamespace(conversation_id="file-chat", ask_user_request=request)
    message = {"conversation_id": "file-chat", "request_id": request.request_id,
               "answers": {"file_access": "grant_file"}}
    for invalid in [{**message, "conversation_id": "other-chat"},
                    {**message, "request_id": "old-request"}]:
        with pytest.raises(PromptError):
            await dispatch(server, "ask_user", invalid, runtime=runtime, transport=transport)
        assert not request.future.done() and not checker.sandbox.file_grants
    result = await dispatch(server, "ask_user", message, runtime=runtime, transport=transport)
    assert result["ok"]
    result = await anext(generator)
    assert "FILE_ACCESS_GRANTED" in result.output
    with pytest.raises(PromptError):
        await dispatch(server, "ask_user", message, runtime=runtime, transport=transport)
    await generator.aclose()


@pytest.mark.asyncio
async def test_orchestration_grant_waits_for_human_without_choice_rewrite_or_timeout(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from tests.test_kernel_gates import setup
    from mewcode.experts.waits import wait_for_interaction
    from mewcode.kernel.gates import GateKind, scope_identity
    monkeypatch.setenv('MEWCODE_EXPERT_HITL_SOFT_TIMEOUT', '.01')
    agent, checker, target, call = setup_request(tmp_path)
    generator = request_file_access(agent, call, AskUserParams.model_validate(call.arguments).file_access)
    request = await anext(generator)
    store, run_id = setup(tmp_path)
    preparer = AsyncMock(side_effect=AssertionError("Must not rewrite authorization"))
    session = SimpleNamespace(interaction_choice_preparer=preparer)
    emitted = []
    async def emit(event):
        emitted.append(event)
    task = asyncio.create_task(wait_for_interaction(request, kind='ask_user', session=session,
        emit=emit, store=store, run_id=run_id, stage_key='N03'))
    try:
        for _ in range(100):
            if any(event.get('type') == 'ask_user' for event in emitted):
                break
            await asyncio.sleep(.01)
        await asyncio.sleep(.04)
        assert not task.done()
        record = store.load(run_id)
        gate = record['kernel_gates'][request.request_id]
        assert gate['kind'] == GateKind.AUTHORIZATION
        assert gate['deadline'] is None and gate['recommended'] is None
        pending = record['node_states']['N03']['pending_interaction']
        assert pending['host_file_access'] == request.host_file_access
        old_identity = scope_identity(record, 'N03', request)
        request.host_file_access = {**request.host_file_access, 'nonce': 'fresh-request'}
        assert scope_identity(record, 'N03', request) != old_identity
        request.host_file_access = pending['host_file_access']
        request.future.set_result({'file_access': 'grant_file'})
        assert await task == {'file_access': 'grant_file'}
        result = await anext(generator)
        assert "FILE_ACCESS_GRANTED" in result.output
        preparer.assert_not_awaited()
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await generator.aclose()
