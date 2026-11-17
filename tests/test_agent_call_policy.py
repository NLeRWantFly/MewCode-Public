"""Orchestration children inherit policy, not just its mode label."""
from types import SimpleNamespace

import pytest

from mewcode.agent_calls import _clone_permission_checker
from mewcode.permissions import PermissionMode
from mewcode.permissions.grants import GrantStore
from mewcode.permissions.policy import build_permission_checker
from mewcode.permissions.rules import Rule
from mewcode.tools.bash import Bash
from mewcode.tools.write_file import WriteFile


def test_clone_preserves_authorization_store_and_shared_grants(tmp_path):
    store = GrantStore(tmp_path)
    parent = build_permission_checker(tmp_path, mode=PermissionMode.AUTO, grant_store=store)
    parent.task_authorization = object()  # identity only; never used to execute
    parent.task_id = "task-parent"
    parent.plan_file_path = str(tmp_path / "plan.md")
    child = _clone_permission_checker(SimpleNamespace(permission_checker=parent), str(tmp_path))
    assert child.grant_store is store
    assert child.task_authorization is parent.task_authorization
    assert child.task_id == parent.task_id
    assert child.plan_file_path == parent.plan_file_path
    assert child.sandbox.file_grants is parent.sandbox.file_grants
    assert child.sandbox.dir_grants is parent.sandbox.dir_grants
    assert child.mode is PermissionMode.AUTO


def test_same_workspace_clone_cannot_widen_narrow_parent_scope(tmp_path):
    declared = tmp_path / "declared"
    declared.mkdir()
    parent = build_permission_checker(tmp_path, mode=PermissionMode.AUTO, writable_scope=[declared])
    parent.rule_engine.append_session_rule(Rule("WriteFile", "*", "allow"))
    child = _clone_permission_checker(SimpleNamespace(permission_checker=parent), str(tmp_path))
    assert child.check(WriteFile(), {"file_path": str(tmp_path / "outside.txt"), "content": "x"}).effect == "deny"
    assert child.check(WriteFile(), {"file_path": str(declared / "inside.txt"), "content": "x"}).effect == "allow"
    assert child.check(Bash(), {"command": "git status"}).source == "native_boundary_unverified"


def test_worktree_clone_is_bounded_and_preserves_parent_denials(tmp_path):
    workspace = tmp_path / "workspace"
    worktree = workspace / "worktree"
    worktree.mkdir(parents=True)
    parent = build_permission_checker(workspace, mode=PermissionMode.AUTO)
    parent.rule_engine.append_session_rule(Rule("WriteFile", "*blocked*", "deny"))
    child = _clone_permission_checker(SimpleNamespace(permission_checker=parent), str(worktree), profile="worktree")
    assert child.policy_writable_roots == (worktree.resolve(),)
    for path in (workspace / "outside.txt", worktree / "blocked.txt"):
        assert child.check(WriteFile(), {"file_path": str(path), "content": "x"}).effect == "deny"
    outside = tmp_path / "unapproved"
    outside.mkdir()
    with pytest.raises(ValueError, match="exceeds parent policy"):
        _clone_permission_checker(SimpleNamespace(permission_checker=parent), str(outside), profile="worktree")
