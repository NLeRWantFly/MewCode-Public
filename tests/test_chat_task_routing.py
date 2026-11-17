"""Actual chat ingress must not treat a saved workflow as new-task authority."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from frontend import server
from mewcode.conversation import ConversationManager
from mewcode.expert_runs import ExpertRunStore
from mewcode.expert_web import run_expert_chat
from mewcode.memory.session import SessionManager, SessionMeta
from mewcode.orchestration.chat_routing import (
    CHAT_TASK_MARKER, dialogue_focus, prepare_chat_focus,
    requests_orchestration_resume, set_dialogue_focus,
)
from tests.test_parallel_conversations import _Agent


INSTALL = "帮我安装 https://github.com/crabin/paper-humanizer-skill"
FLOW_FOCUS = {"kind": "orchestration", "run_id": "a" * 32}


@pytest.mark.parametrize("focus", [{}, {"kind": "chat", "task": INSTALL}, FLOW_FOCUS])
@pytest.mark.parametrize("text", [INSTALL, "新建任务：" + INSTALL, "继续安装这个技能", "列出全部技能",
                                  "能否恢复编排？", "不要继续编排", "解释恢复检查点按钮",
                                  "继续编排讨论", "继续流程设计", "从失败截图继续分析", "continue workflow documentation"])
def test_unrelated_tasks_never_become_workflow_commands(focus, text):
    assert not requests_orchestration_resume(text, focus)


@pytest.mark.parametrize("text", ["继续", "请继续", "继续任务", "continue", "resume"])
def test_bare_continue_is_scoped_to_durable_focus(text):
    assert requests_orchestration_resume(text, FLOW_FOCUS)
    assert not requests_orchestration_resume(text, {})
    assert not requests_orchestration_resume(text, {"kind": "chat", "task": INSTALL})


@pytest.mark.parametrize("text", ["恢复编排", "请继续原来的流程", "继续，补跑测试", "resume workflow",
                                  "从上次失败的节点继续", "继续流程执行",
                                  "继续当前编排任务，期间不要让我接管",
                                  "继续当前编排图任务至完整流程结束，期间不要让我接管",
                                  "继续编排任务；不要重复已完成的工具调用"])
def test_explicit_workflow_control_remains_available(text):
    assert requests_orchestration_resume(text, FLOW_FOCUS)


@pytest.mark.parametrize("text", ["继续编排，但不要执行任何节点", "继续编排，先不要恢复任务",
                                  "继续编排任务是否会重试？", "继续编排任务按钮在哪里？"])
def test_questions_and_negated_execution_are_not_resume_commands(text):
    assert not requests_orchestration_resume(text, FLOW_FOCUS)


def test_focus_survives_restart_and_old_sessions_default_to_unknown(tmp_path):
    manager = SessionManager(str(tmp_path))
    durable = manager.create()
    runtime = SimpleNamespace(session=durable, conversation=ConversationManager())
    runtime.conversation.add_assistant_message("历史论文节点只允许读取 main.tex")
    original = list(runtime.conversation.history)
    set_dialogue_focus(runtime, "orchestration", task="修改论文", run_id=FLOW_FOCUS["run_id"])
    prepare_chat_focus(runtime, INSTALL)
    prepare_chat_focus(runtime, "继续")
    path = durable._sessions_dir / f"{durable.session_id}.meta"
    durable.close()
    restored = SessionMeta.load(path)
    assert restored.dialogue_focus["kind"] == "chat"
    assert restored.dialogue_focus["task"] == INSTALL
    assert restored.dialogue_focus["run_id"] == ""
    assert runtime.conversation.history[0] is original[0]
    reminders = [m for m in runtime.conversation.history if CHAT_TASK_MARKER in m.content]
    assert len(reminders) == 1 and reminders[0].transient
    assert INSTALL in reminders[0].content
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("dialogue_focus")
    path.write_text(json.dumps(data), encoding="utf-8")
    assert SessionMeta.load(path).dialogue_focus == {}


def test_latest_continuation_request_survives_metadata_reload(tmp_path):
    durable = SessionManager(str(tmp_path)).create()
    runtime = SimpleNamespace(session=durable, conversation=ConversationManager())
    prepare_chat_focus(runtime, "旧的 A3 验收")
    task_id = dialogue_focus(runtime)["task_id"]
    prepare_chat_focus(runtime, "最新 A4 验收", continuation_task_id=task_id)
    before = dict(dialogue_focus(runtime))
    path = durable._sessions_dir / f"{durable.session_id}.meta"
    durable.close()
    durable.meta = SessionMeta.load(path)
    prepare_chat_focus(runtime, "继续", continuation_task_id=task_id)
    focus = dialogue_focus(runtime)
    assert focus["task_id"] == task_id and focus["task"] == "旧的 A3 验收"
    assert focus["latest_request"] == "最新 A4 验收"
    assert focus["turn_id"] != before["turn_id"]
    assert SessionMeta.load(path).dialogue_focus == focus


@pytest.fixture
def ingress(tmp_path, monkeypatch):
    """Real WS dispatcher, real run_chat and durable history; no provider calls."""
    durable = SessionManager(str(tmp_path)).create()
    prompts = []

    class Agent(_Agent):
        async def run(self, conversation):
            prompts.append(copy.deepcopy(conversation.history))
            async for event in super().run(conversation):
                yield event

    agent = Agent("主对话处理当前任务")
    owner = SimpleNamespace(session=durable, agent=agent, expert_mode=True,
                            orchestration_planning=False, session_start=0,
                            tasks_payload=lambda: [], scope="project", slot=None)
    runtime = server.ConversationRuntime(
        owner=owner, conversation_id=durable.session_id, scope="project", work_dir=str(tmp_path),
        session=durable, conversation=ConversationManager(), agent=agent,
        client=SimpleNamespace(effort=""), slot=None, effort="", context_tier=200000,
        multi_agent=False, expert_mode=True, goal="旧目标：完成论文写作",
        task_manager=SimpleNamespace(), trace_manager=SimpleNamespace(),
        agent_tool=None, worktree_manager=None, team_manager=None,
    )
    tasks = []

    def attach(target, task):
        target.task, target.starting = task, False
        tasks.append(task)

    owner.conversation_runs = {runtime.conversation_id: runtime}
    owner.client_state_payload = lambda: {"status": runtime.status_payload()}
    owner.status_payload = runtime.status_payload
    owner.capture_conversation_runtime = lambda: runtime
    owner.runtime_for = lambda *_: runtime
    owner.chat_busy = lambda *_: runtime.running()
    owner.begin_conversation_turn = lambda target: setattr(target, "starting", True)
    owner.attach_conversation_task = attach
    owner.collect_task_notifications = lambda: []
    runs = ExpertRunStore(tmp_path / "runs")
    monkeypatch.setattr(server, "SESSION", owner)
    monkeypatch.setattr(server, "EXPERT_RUNS", runs)
    monkeypatch.setattr(server, "WS_EVENTS", server.WebSocketEventHub())
    monkeypatch.setattr(server, "DESKTOP_TOKEN", "")
    monkeypatch.setattr(server, "pending_runtime_prompts", lambda _: [])
    monkeypatch.setattr(server, "get_plan_session_store", lambda: None)
    monkeypatch.setattr(server, "ATTACHMENTS", SimpleNamespace(
        references=lambda cid, ids: [{"attachment_id": identity, "kind": "text", "name": "notes.txt"} for identity in ids],
        materialize=lambda refs: [{"type": "text", "text": "attachment content", **item} for item in refs],
        display_refs=lambda refs: refs,
    ))

    class Socket:
        def __init__(self, frames):
            self.frames = iter(frames)
            self.messages = []

        async def accept(self):
            pass

        async def send_json(self, event):
            self.messages.append(event)

        async def receive_json(self):
            if tasks:
                await asyncio.gather(*tasks)
            try:
                frame = next(self.frames)
            except StopIteration:
                raise server.WebSocketDisconnect()
            return {"type": "chat", "conversation_id": runtime.conversation_id, **frame}

    yield SimpleNamespace(runtime=runtime, owner=owner, runs=runs, prompts=prompts, Socket=Socket)
    durable.close()


def old_run(env, status="paused", identity="a" * 32):
    env.runs.create(run_id=identity, conversation_id=env.runtime.conversation_id, task="融论文写作",
                    attachments=[], profile={}, strategy="stable")
    env.runs.transition(identity, status)
    return env.runs.root / (identity + ".json")


@pytest.mark.asyncio
async def test_canvas_restore_failure_does_not_block_chat_or_start_design(ingress, monkeypatch):
    from mewcode.tools import ToolRegistry
    from mewcode.workflow_core.chat_tools import ChatWorkflow, DESIGN_TOOLS
    ingress.runtime.agent.registry = ToolRegistry()
    for name in DESIGN_TOOLS:
        ingress.runtime.agent.registry.register(SimpleNamespace(name=name))
    def broken_snapshot(*args):
        raise OSError('canvas storage unavailable')
    monkeypatch.setattr(ChatWorkflow, 'restore', broken_snapshot)
    monkeypatch.setattr('frontend.workbench_revision.sync_tool',
                        lambda *args, **kwargs: pytest.fail('ordinary chat entered revision pipeline'))
    monkeypatch.setattr(server, '_canvas_nodes_text',
                        lambda *args: pytest.fail('ordinary chat waited for legacy canvas'))
    socket = ingress.Socket([{'text': '整理行测章节的数学材料'}])
    await server.ws_endpoint(socket)
    assert ingress.prompts
    assert any(m.content == '主对话处理当前任务' for m in ingress.runtime.conversation.history)
    assert all(ingress.runtime.agent.registry.get(name) is None for name in DESIGN_TOOLS)
    assert ingress.runtime.agent.registry.get('UpdateWorkflowTasks') is not None
    assert not any(m.get('type') == 'error' for m in socket.messages)


def bind_git_authorization(env):
    from mewcode.permissions import (DangerousCommandDetector, PathSandbox,
                                    PermissionChecker, PermissionMode, RuleEngine)
    from mewcode.permissions.task_authorization import TaskAuthorization
    set_dialogue_focus(env.runtime, "chat", task="验收：测试并提交两份文件")
    task_id = dialogue_focus(env.runtime)["task_id"]
    authorization = TaskAuthorization.from_user_scope(
        {"allowed_behaviors": ["git_write"]}, task_id=task_id)
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(env.runtime.work_dir),
                                RuleEngine(), PermissionMode.AUTO)
    env.runtime.agent.permission_checker = checker
    env.owner.web_state = {"task_authorizations": {
        f"{env.runtime.conversation_id}:{task_id}": authorization.as_dict()}}
    return task_id, authorization, checker


@pytest.mark.asyncio
async def test_explicit_continuation_keeps_task_and_exact_authorization_through_ws(ingress):
    task_id, authorization, checker = bind_git_authorization(ingress)
    records = copy.deepcopy(ingress.owner.web_state)
    text = "Git 写入授权已确认。解除此前不重试限制，仅暂存并本地提交两份文件；若拒绝立即停止。"
    socket = ingress.Socket([{"text": text, "continuation_task_id": task_id}])
    await server.ws_endpoint(socket)
    assert len(ingress.prompts) == 1
    assert dialogue_focus(ingress.runtime)["task_id"] == task_id
    assert checker.task_authorization == authorization
    # The same production scope now permits Git payload, not control files.
    from pathlib import Path
    from mewcode.sandbox.lpac_runtime import NativeScope
    git = Path(ingress.runtime.work_dir) / "repository" / ".git"
    git.mkdir(parents=True)
    (git / "index").write_bytes(b"synthetic")
    scope = NativeScope.from_checker(checker)
    assert scope.allows(git / "index", write=True)
    assert not scope.allows(git / "config", write=True)
    assert not scope.allows(git / "hooks" / "pre-commit", write=True)
    assert ingress.owner.web_state == records  # no copied, extended or renewed grant
    assert any(m.content == text for m in ingress.prompts[0])
    focus = dialogue_focus(ingress.runtime)
    assert focus["latest_request"] == text
    assert focus["turn_id"] == ingress.runtime.agent._chat_turn_id
    reminder = next(m.content for m in ingress.prompts[0] if CHAT_TASK_MARKER in m.content)
    assert reminder.endswith(text + "\n</system-reminder>")
    assert not any(m.get("rejected") for m in socket.messages)


@pytest.mark.asyncio
async def test_new_acceptance_turn_does_not_claim_historical_launch(ingress):
    from mewcode.agents.task_manager import BackgroundTask, TaskManager
    from mewcode.tools.agent_status import BackgroundTasks
    set_dialogue_focus(ingress.runtime, "chat", task="旧验收 A3：只执行一次")
    task_id = dialogue_focus(ingress.runtime)["task_id"]
    manager = TaskManager()
    old = BackgroundTask(id="old-a3", name="acceptance-cancel-A3", agent=None,
        task="历史 A3 原指令", status="completed", result="旧 STARTED FINISHED",
        owner_session_id=ingress.runtime.conversation_id,
        owner_store_dir=ingress.runtime.work_dir, owner_task_id=task_id)
    manager._tasks[old.id] = old
    ingress.runtime.task_manager = manager
    ingress.runtime.agent.session_id = ingress.runtime.conversation_id
    ingress.runtime.agent.work_dir = ingress.runtime.work_dir
    text = "本轮只创建 acceptance-cancel-A4，不重跑 A3。"
    await server.ws_endpoint(ingress.Socket([{"text": text, "continuation_task_id": task_id}]))
    first_turn = ingress.runtime.agent._chat_turn_id
    query = BackgroundTasks(ingress.runtime.agent, manager)
    old_observation = json.loads((await query.execute(query.params_model(task_id=old.id))).output)
    assert old_observation["dispatch_relation"] == "existing_before_turn"
    assert old_observation["dispatch_turn_id"] is None
    assert dialogue_focus(ingress.runtime)["task_id"] == task_id
    assert dialogue_focus(ingress.runtime)["latest_request"] == text
    assert set(manager._tasks) == {old.id}  # no automatic launches or retries
    await server.ws_endpoint(ingress.Socket([{"text": "继续", "continuation_task_id": task_id}]))
    assert ingress.runtime.agent._chat_turn_id != first_turn
    assert dialogue_focus(ingress.runtime)["latest_request"] == text
    reminder = next(m.content for m in ingress.prompts[-1] if CHAT_TASK_MARKER in m.content)
    assert reminder.endswith(text + "\n</system-reminder>")
    assert "本轮用户原文：\n继续" in reminder
    assert set(manager._tasks) == {old.id}


@pytest.mark.asyncio
async def test_ordinary_new_message_does_not_inherit_previous_git_authorization(ingress):
    task_id, _, checker = bind_git_authorization(ingress)
    await server.ws_endpoint(ingress.Socket([{"text": "新任务：处理另外一个仓库"}]))
    assert len(ingress.prompts) == 1
    assert dialogue_focus(ingress.runtime)["task_id"] != task_id
    assert checker.task_authorization is None


@pytest.mark.asyncio
async def test_plain_chat_installs_lazy_workflow_tools_with_current_identity(ingress):
    from mewcode.tools import ToolRegistry
    ingress.runtime.agent.registry = ToolRegistry()
    await server.ws_endpoint(ingress.Socket([{'text': '只讨论行测章节的处理方式'}]))
    assert len(ingress.prompts) == 1
    tool = ingress.runtime.agent.registry.get('UpdateWorkflowTasks')
    assert tool is not None
    assert tool.bridge.task_id == dialogue_focus(ingress.runtime)['task_id']
    assert tool.bridge.runtime is ingress.runtime
    assert not getattr(ingress.runtime, '_goal_run_tasks', {})
    assert 'SaveWorkflow' in {tool.name for tool in ingress.runtime.agent.registry.list_tools()}


@pytest.mark.asyncio
@pytest.mark.parametrize("task_id", [None, "", 7, {}, "old-task"])
async def test_invalid_explicit_continuation_rejects_before_history_or_model(ingress, task_id):
    bind_git_authorization(ingress)
    before = copy.deepcopy(dialogue_focus(ingress.runtime))
    socket = ingress.Socket([{"text": "继续暂存", "continuation_task_id": task_id}])
    await server.ws_endpoint(socket)
    assert not ingress.prompts and not ingress.runtime.conversation.history
    assert dialogue_focus(ingress.runtime) == before
    assert socket.messages[-1]["type"] == "done" and socket.messages[-1]["rejected"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["conversation", "missing_conversation", "orchestration", "busy"])
async def test_continuation_rejects_wrong_context(ingress, case):
    task_id, _, _ = bind_git_authorization(ingress)
    frame = {"text": "继续暂存", "continuation_task_id": task_id}
    if case == "conversation":
        frame["conversation_id"] = "other-conversation"
    elif case == "missing_conversation":
        frame["conversation_id"] = ""
    elif case == "orchestration":
        ingress.runtime.session.meta.dialogue_focus["kind"] = "orchestration"
    else:
        ingress.owner.chat_busy = lambda *_: True
    socket = ingress.Socket([frame])
    await server.ws_endpoint(socket)
    assert not ingress.prompts
    assert socket.messages[-1]["type"] == "done" and socket.messages[-1]["rejected"]


@pytest.mark.asyncio
async def test_continuation_rechecks_identity_after_startup_await(ingress):
    task_id, _, _ = bind_git_authorization(ingress)
    async def startup():
        await asyncio.sleep(0)
        set_dialogue_focus(ingress.runtime, "chat", task="另一个任务")
    ingress.owner._mcp_init_task = asyncio.create_task(startup())
    socket = ingress.Socket([{"text": "补充说明", "continuation_task_id": task_id}])
    await server.ws_endpoint(socket)
    assert not ingress.prompts and not ingress.runtime.conversation.history
    assert socket.messages[-1]["type"] == "done" and socket.messages[-1]["rejected"]


@pytest.mark.asyncio
async def test_continuation_does_not_revive_expired_grant(ingress, monkeypatch):
    task_id, authorization, checker = bind_git_authorization(ingress)
    monkeypatch.setattr("mewcode.permissions.task_authorization.time.time",
                        lambda: authorization.expires_at + 1)
    await server.ws_endpoint(ingress.Socket([{"text": "继续暂存", "continuation_task_id": task_id}]))
    assert len(ingress.prompts) == 1
    assert dialogue_focus(ingress.runtime)["task_id"] == task_id
    assert checker.task_authorization is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["paused", "blocked", "terminated", "completed"])
async def test_install_then_continue_through_real_ws_never_runs_or_mutates_old_workflow(ingress, monkeypatch, status):
    env = ingress
    path = old_run(env, status)
    before = path.read_bytes()
    set_dialogue_focus(env.runtime, "orchestration", run_id=FLOW_FOCUS["run_id"], task="融论文")
    expert = AsyncMock(side_effect=AssertionError("ordinary chat entered expert runner"))
    monkeypatch.setattr(server, "run_expert_chat", expert)
    socket = env.Socket([{"text": INSTALL}, {"text": "继续"}])
    await server.ws_endpoint(socket)
    assert expert.await_count == 0 and len(env.prompts) == 2
    assert path.read_bytes() == before
    assert len(env.runs.list_for_conversation(env.runtime.conversation_id)) == 1
    starts = [m for m in socket.messages if m["type"] == "turn_started"]
    assert len(starts) == 2 and all(m["expert_mode"] is False for m in starts)
    done = [m for m in socket.messages if m["type"] == "done"]
    assert len(done) == 2 and all(not m.get("error") and not m.get("checkpoint") for m in done)
    assert all(m["status"]["execution_kind"] == "chat" for m in done)
    assert dialogue_focus(env.runtime)["task"] == INSTALL
    for history in env.prompts:
        reminder = next(m for m in history if CHAT_TASK_MARKER in m.content)
        assert INSTALL in reminder.content and "以本轮实际工具和权限为准" in reminder.content
        # An unrelated turn no longer receives an old goal reminder at all.
        # Its absence is stronger than merely placing current focus after it.
        assert not any(env.runtime.goal in m.content for m in history)
        assert not any("[MEWCODE_CONTINUOUS_CHAT]" in m.content for m in history)
    assert env.runtime.agent is env.owner.agent
    assert env.runtime.agent.permission_mode.value == "auto"


@pytest.mark.asyncio
@pytest.mark.parametrize("focus,text", [({}, "继续"), ({"kind": "chat", "task": INSTALL}, "继续"),
                                      (FLOW_FOCUS, "继续安装这个技能"), (FLOW_FOCUS, "新建任务：" + INSTALL)])
async def test_saved_mode_and_checkpoint_do_not_claim_ordinary_frames(ingress, monkeypatch, focus, text):
    old_run(ingress)
    ingress.runtime.session.meta.dialogue_focus = dict(focus)
    expert = AsyncMock()
    monkeypatch.setattr(server, "run_expert_chat", expert)
    await server.ws_endpoint(ingress.Socket([{"text": text}]))
    expert.assert_not_awaited()
    assert len(ingress.prompts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("frame,focus", [({"text": "继续"}, FLOW_FOCUS),
    ({"text": "继续当前编排任务，期间不要让我接管"}, FLOW_FOCUS),
    ({"text": "继续当前编排图任务至完整流程结束，期间不要让我接管"}, FLOW_FOCUS),
    ({"text": "恢复编排"}, {"kind": "chat", "task": INSTALL}),
    ({"type": "expert_resume", "run_id": "a" * 32}, {"kind": "chat", "task": INSTALL})])
async def test_explicit_resume_dispatches_exact_run_even_with_mode_off(ingress, monkeypatch, frame, focus):
    old_run(ingress)
    ingress.runtime.session.meta.dialogue_focus = dict(focus)
    ingress.owner.expert_mode = ingress.runtime.expert_mode = False
    expert = AsyncMock()
    monkeypatch.setattr(server, "run_expert_chat", expert)
    native = AsyncMock()
    monkeypatch.setattr('frontend.workflow_core_api.run_action', native)
    socket = ingress.Socket([frame])
    await server.ws_endpoint(socket)
    if frame.get('type') == 'expert_resume':
        native.assert_awaited_once()
        assert native.await_args.args[1]['run_id'] == 'a' * 32
        assert native.await_args.args[1]['operation'] == 'resume'
        assert not ingress.prompts
        expert.assert_not_awaited()
        return
    assert expert.await_count == 1 and not ingress.prompts
    assert expert.await_args.kwargs["resume_run_id"] == "a" * 32
    starts = [event for event in socket.messages if event.get("type") == "turn_started"]
    if frame.get("type", "chat") == "chat":
        assert len(starts) == 1 and starts[0]["expert_mode"] is True
        assert starts[0]["resume_run_id"] == "a" * 32
    assert ingress.runtime.execution_kind == "orchestration"


@pytest.mark.asyncio
async def test_multiple_old_runs_require_selection_and_do_not_silently_choose_latest(ingress, monkeypatch):
    old_run(ingress)
    old_run(ingress, identity="b" * 32)
    expert = AsyncMock()
    monkeypatch.setattr(server, "run_expert_chat", expert)
    socket = ingress.Socket([{"text": "恢复编排"}])
    await server.ws_endpoint(socket)
    expert.assert_not_awaited()
    assert any("多个可恢复编排" in m.get("text", "") for m in socket.messages)
    assert socket.messages[-1]["type"] == "done"
    assert server._focused_resume_checkpoint(ingress.runtime.conversation_id, FLOW_FOCUS)["run_id"] == "a" * 32
    assert server._focused_resume_checkpoint("another-conversation", FLOW_FOCUS) is None
    # A focused completed run must not fall back to either unrelated checkpoint.
    old_run(ingress, status="completed", identity="c" * 32)
    assert server._focused_resume_checkpoint(ingress.runtime.conversation_id,
        {"kind": "orchestration", "run_id": "c" * 32}) is None


@pytest.mark.asyncio
async def test_continue_with_attachment_stays_in_main_chat_and_preserves_attachment(ingress, monkeypatch):
    old_run(ingress)
    ingress.runtime.session.meta.dialogue_focus = dict(FLOW_FOCUS)
    expert = AsyncMock()
    monkeypatch.setattr(server, "run_expert_chat", expert)
    await server.ws_endpoint(ingress.Socket([{"text": "继续", "attachments": ["notes"]}]))
    expert.assert_not_awaited()
    latest = ingress.prompts[-1][-1]
    assert latest.content_blocks[0]["attachment_id"] == "notes"


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [INSTALL, "新建任务：" + INSTALL, "开始编排"])
async def test_low_level_new_run_cannot_bypass_confirmation_even_without_old_run(tmp_path, text):
    runs = ExpertRunStore(tmp_path / "runs")
    socket = SimpleNamespace(send_json=AsyncMock())
    await run_expert_chat(socket, text, session=SimpleNamespace(status_payload=lambda: {}, tasks_payload=lambda: []),
                          registry_service=None, profile_store=None, run_store=runs)
    assert socket.send_json.await_args.args[0]["error_code"] == "unconfirmed_flow_start"
    assert runs.list_recent() == []


@pytest.mark.asyncio
async def test_sticky_expert_flag_cannot_bypass_main_model_image_validation(ingress):
    from tests.test_chat_queue import _request_with_queue_header
    ingress.owner._active_queue_runtime = lambda _, **kwargs: ingress.runtime
    server.ATTACHMENTS.references = lambda *_: [{"kind": "image", "attachment_id": "picture"}]
    response = await server.api_chat_queue(_request_with_queue_header(), server.ChatQueueBody(
        conversation_id=ingress.runtime.conversation_id, queue_id="picture-queue",
        text="解释图片", attachments=["picture"],
    ))
    assert response.status_code == 422 and "不支持图片输入" in response.body.decode()
    socket = ingress.Socket([{"text": "解释图片", "attachments": ["picture"]}])
    await server.ws_endpoint(socket)
    assert not ingress.prompts
    assert any("不支持图片输入" in m.get("text", "") for m in socket.messages)
