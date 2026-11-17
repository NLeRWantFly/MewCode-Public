"""Role creation shares the task's live policy instead of rebuilding defaults."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mewcode.expert_group import ExpertExecutionPolicy
from mewcode.expert_web import _build_role_agent, _expert_permission_checker
from mewcode.experts.node_expansion import ExpansionPathSandbox
from mewcode.model_registry import ModelRef
from mewcode.permissions.grants import GrantStore
from mewcode.permissions.modes import PermissionMode
from mewcode.permissions.policy import build_permission_checker
from mewcode.permissions.rules import Rule
from mewcode.permissions.task_authorization import TaskAuthorization
from mewcode.tools.bash import Bash
from mewcode.tools.read_file import ReadFile
from mewcode.tools.write_file import WriteFile


def session_for(parent, root):
    return SimpleNamespace(agent=SimpleNamespace(permission_checker=parent,
        permission_mode=parent.mode), work_dir=str(root))


@pytest.mark.parametrize("mode", list(PermissionMode))
def test_role_keeps_parent_mode_and_policy_state(tmp_path, mode):
    parent = build_permission_checker(tmp_path, mode=mode, grant_store=GrantStore(tmp_path))
    scope = TaskAuthorization.from_user_scope({"write_directories": [str(tmp_path)],
        "allowed_behaviors": ["isolated_command"]}, task_id="local-task")
    parent.set_task_authorization(scope, task_id="local-task")
    child = _expert_permission_checker(session_for(parent, tmp_path), str(tmp_path))
    assert child.mode is mode and parent.mode is mode
    assert child.rule_engine is parent.rule_engine
    assert child.grant_store is parent.grant_store
    assert child.task_authorization is scope and child.task_id == "local-task"
    assert child.sandbox.file_grants is parent.sandbox.file_grants
    assert child.sandbox.dir_grants is parent.sandbox.dir_grants
    assert child.policy_writable_roots == (tmp_path.resolve(),)


def test_baseline_is_readable_but_does_not_extend_write_scope(tmp_path):
    work, baseline = tmp_path / "work", tmp_path / "baseline"
    work.mkdir(); baseline.mkdir()
    parent = build_permission_checker(work, mode=PermissionMode.AUTO)
    child = _expert_permission_checker(session_for(parent, work), str(work), [str(baseline)])
    assert child.check(ReadFile(), {"file_path": str(baseline / "input.md")}).effect == "allow"
    blocked = child.check(WriteFile(), {"file_path": str(baseline / "input.md"), "content": "x"})
    assert blocked.effect == "deny" and blocked.source == "child_write_scope"


def test_mode_and_parent_rules_still_apply_after_role_creation(tmp_path):
    parent = build_permission_checker(tmp_path, mode=PermissionMode.AUTO)
    child = _expert_permission_checker(session_for(parent, tmp_path), str(tmp_path))
    parent.rule_engine.append_session_rule(Rule("WriteFile", "*", "deny"))
    assert child.check(WriteFile(), {"file_path": "draft.md", "content": "x"}).effect == "deny"
    blocked = child.check(Bash(), {"command": "python -m pytest"})
    # Native AUTO readiness can require review instead of an unconditional
    # rejection. Creating a role must preserve the parent's actual decision.
    parent_blocked = parent.check(Bash(), {"command": "python -m pytest"})
    assert blocked.effect == parent_blocked.effect
    assert blocked.effect in {'deny', 'review'}
    assert blocked.source == parent_blocked.source
    assert blocked.clearance == parent_blocked.clearance


def test_expansion_exact_file_contract_still_applies_to_factory_checker(tmp_path):
    parent = build_permission_checker(tmp_path, mode=PermissionMode.AUTO)
    child = _expert_permission_checker(session_for(parent, tmp_path), str(tmp_path))
    child.sandbox = ExpansionPathSandbox(child.sandbox, (str(tmp_path / "out.md"),))
    assert child.check(WriteFile(), {"file_path": "out.md", "content": "x"}).effect == "allow"
    assert child.check(WriteFile(), {"file_path": "other.md", "content": "x"}).effect == "deny"


def test_project_cwd_can_keep_a_narrow_inherited_output_directory(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    parent = build_permission_checker(tmp_path, mode=PermissionMode.AUTO, writable_scope=[output])
    child = _expert_permission_checker(session_for(parent, tmp_path), str(tmp_path))
    assert child.policy_writable_roots == (output.resolve(),)
    assert child.check(WriteFile(), {"file_path": "output/result.md", "content": "x"}).effect == "allow"
    assert child.check(WriteFile(), {"file_path": "elsewhere.md", "content": "x"}).effect == "deny"


def test_undeclared_working_directory_rejected_before_provider_resolution(tmp_path):
    work, unrelated = tmp_path / "work", tmp_path / "unrelated"
    work.mkdir(); unrelated.mkdir()
    parent = build_permission_checker(work, mode=PermissionMode.AUTO, writable_scope=[work])
    service = Mock()
    with pytest.raises(ValueError, match="exceeds parent policy"):
        _build_role_agent(session_for(parent, work), service, "executor", ModelRef("unit", "unit"),
                          ExpertExecutionPolicy("stable", False, 0, work_dir=str(unrelated)))
    service.resolve.assert_not_called()


def test_legacy_parent_without_checker_loads_declared_rule_files(tmp_path, monkeypatch):
    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.setattr(Path, "home", lambda: user)
    session = SimpleNamespace(agent=SimpleNamespace(permission_mode=PermissionMode.AUTO))
    child = _expert_permission_checker(session, str(tmp_path))
    assert child.mode is PermissionMode.AUTO and child.grant_store is not None
    assert child.rule_engine._user_path == user / ".mewcode" / "permissions.yaml"
    assert child.rule_engine._project_path == tmp_path / ".mewcode" / "permissions.yaml"
    assert child.rule_engine._local_path == tmp_path / ".mewcode" / "permissions.local.yaml"
