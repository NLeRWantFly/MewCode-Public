"""Orchestration identities must reach the native boundary without new grants."""
import json
from types import SimpleNamespace

import pytest

from mewcode.conversation import ConversationManager
from mewcode.expert_web import _expert_permission_checker
from mewcode.memory.session import SessionManager, SessionMeta
from mewcode.orchestration.chat_routing import (
    bind_task_permission_scope, dialogue_focus, set_dialogue_focus,
)
from mewcode.permissions import (
    DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine,
)
from mewcode.permissions.task_authorization import TaskAuthorization
from mewcode.sandbox.lpac_runtime import NativeScope


def runtime_for(tmp_path):
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(tmp_path)),
                                RuleEngine(), PermissionMode.AUTO)
    durable = SessionManager(str(tmp_path)).create()
    runtime = SimpleNamespace(session=durable, conversation_id=durable.session_id,
        conversation=ConversationManager(), owner=SimpleNamespace(web_state={}),
        agent=SimpleNamespace(permission_checker=checker, permission_mode=PermissionMode.AUTO,
                              safety_reviewer=SimpleNamespace(authorization_version="old")))
    return runtime, checker


def test_orchestration_identity_survives_same_run_resume_and_disk_restart(tmp_path):
    runtime, checker = runtime_for(tmp_path)
    set_dialogue_focus(runtime, "orchestration", task="original", run_id="a" * 32)
    identity = dialogue_focus(runtime)["task_id"]
    bind_task_permission_scope(runtime, task_id=identity)
    meta_path = runtime.session._sessions_dir / f"{runtime.session.session_id}.meta"
    runtime.session.meta = SessionMeta.load(meta_path)
    set_dialogue_focus(runtime, "orchestration", task="original plus retained input", run_id="a" * 32)
    assert dialogue_focus(runtime)["task_id"] == identity
    bind_task_permission_scope(runtime, task_id=identity)
    child = _expert_permission_checker(runtime, str(tmp_path))
    assert NativeScope.from_checker(child).task_id == identity
    assert child.task_authorization is None and checker.task_authorization is None
    assert child.policy_writable_roots == (tmp_path,)


@pytest.mark.parametrize("next_kind,next_run", [("orchestration", "b" * 32), ("chat", "")])
def test_replacement_run_or_chat_gets_new_identity_and_cannot_inherit_scope(tmp_path, next_kind, next_run):
    runtime, checker = runtime_for(tmp_path)
    set_dialogue_focus(runtime, "orchestration", task="same wording", run_id="a" * 32)
    old = dialogue_focus(runtime)["task_id"]
    auth = TaskAuthorization.from_user_scope({"read_directories": [str(tmp_path)],
        "allowed_tools": ["ReadFile"]}, task_id=old)
    runtime.owner.web_state["task_authorizations"] = {f"{runtime.conversation_id}:{old}": auth.as_dict()}
    bind_task_permission_scope(runtime, task_id=old)
    assert checker.task_authorization.version == auth.version
    set_dialogue_focus(runtime, next_kind, task="same wording", run_id=next_run)
    new = dialogue_focus(runtime)["task_id"]
    assert new != old
    bind_task_permission_scope(runtime, task_id=new)
    assert checker.task_authorization is None and checker.task_id == new
    assert runtime.agent.safety_reviewer.authorization_version == ""


@pytest.mark.parametrize("record_kind", ["valid", "wrong_task", "corrupt", "absent"])
def test_binding_restores_only_an_exact_existing_authorization(tmp_path, record_kind):
    runtime, checker = runtime_for(tmp_path)
    set_dialogue_focus(runtime, "orchestration", task="inspect", run_id="a" * 32)
    identity = dialogue_focus(runtime)["task_id"]
    auth = TaskAuthorization.from_user_scope({"read_directories": [str(tmp_path)],
        "allowed_tools": ["ReadFile"]}, task_id="other" if record_kind == "wrong_task" else identity)
    record = auth.as_dict()
    if record_kind == "corrupt":
        record["version"] = "invalid"
    runtime.owner.web_state["task_authorizations"] = {} if record_kind == "absent" else {
        f"{runtime.conversation_id}:{identity}": record}
    before = json.dumps(runtime.owner.web_state, sort_keys=True)
    restored = bind_task_permission_scope(runtime, task_id=identity)
    assert checker.task_id == identity
    assert (restored is not None) == (record_kind == "valid")
    assert json.dumps(runtime.owner.web_state, sort_keys=True) == before
    assert runtime.agent.safety_reviewer.authorization_version == (auth.version if restored else "")


def test_focus_persistence_failure_cannot_bind_a_new_identity(tmp_path, monkeypatch):
    runtime, checker = runtime_for(tmp_path)
    set_dialogue_focus(runtime, "chat", task="previous")
    old = dict(dialogue_focus(runtime))
    bind_task_permission_scope(runtime, task_id=old["task_id"])
    monkeypatch.setattr(runtime.session, "save_meta", lambda: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        set_dialogue_focus(runtime, "orchestration", task="new", run_id="a" * 32)
    assert dialogue_focus(runtime) == old and checker.task_id == old["task_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["", " ", None, True, "x" * 257])
async def test_missing_host_identity_is_rejected_before_dispatch_with_false_start(tmp_path, monkeypatch, identity):
    from unittest.mock import AsyncMock
    from mewcode.tools.command_sandbox import native_auto_command, run_native_command

    _, checker = runtime_for(tmp_path)
    checker.task_id = identity
    dispatch = AsyncMock()
    monkeypatch.setattr("mewcode.sandbox.windows.native_runtime_ready", lambda: True)
    monkeypatch.setattr("mewcode.sandbox.lpac_runtime.execute_lpac", dispatch)
    with native_auto_command(checker):
        result = await run_native_command("synthetic-never-launched", work_dir=str(tmp_path), timeout=1)
    assert result.is_error
    observation = result.host_observation["command_execution"]
    assert observation["command_started"] is False and observation["exit_code"] is None
    assert observation["error_code"] == "native_scope_invalid"
    dispatch.assert_not_awaited()
