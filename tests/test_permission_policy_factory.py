from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from mewcode.permissions.modes import PermissionMode
from mewcode.permissions.policy import PermissionRulePaths, build_permission_checker
from mewcode.permissions.rules import Rule, RuleEngine
from mewcode.tools.bash import Bash
from mewcode.tools.read_file import ReadFile
from mewcode.tools.write_file import WriteFile


@pytest.fixture(autouse=True)
def distinct_runtime_temp(tmp_path, monkeypatch):
    # pytest's workspace is itself under the OS temp root. Give auditor/temp
    # policy a separate root so these scope assertions exercise that boundary.
    runtime_temp = tmp_path / "runtime-temp"
    runtime_temp.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(runtime_temp))


def test_child_checker_inherits_parent_mode_rules_and_shared_grants_but_not_write_scope(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    child_root = workspace / "child"
    outside = workspace / "outside"
    workspace.mkdir()
    child_root.mkdir()
    outside.mkdir()
    grants: dict[str, bool] = {}
    parent = build_permission_checker(
        workspace,
        mode=PermissionMode.AUTO,
        shared_file_grants=grants,
        shared_directory_grants={},
    )
    parent.rule_engine.append_session_rule(Rule("WriteFile", "*", "allow"))

    child = build_permission_checker(
        workspace,
        parent_policy=parent,
        writable_scope=[child_root],
    )

    assert child.mode is PermissionMode.AUTO
    assert child.rule_engine is parent.rule_engine
    assert child.sandbox.file_grants is grants
    assert child.sandbox.dir_grants is parent.sandbox.dir_grants
    assert child.policy_writable_roots == (child_root.resolve(),)

    tool = WriteFile()
    allowed = child.check(tool, {"file_path": str(child_root / "candidate.md"), "content": "ok"})
    denied = child.check(tool, {"file_path": str(outside / "candidate.md"), "content": "no"})
    assert allowed.effect == "allow"
    assert denied.effect == "deny"
    assert denied.source == "child_write_scope"


def test_child_scope_cannot_escape_legacy_parent_roots_or_inherited_directory_grants(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    parent = build_permission_checker(workspace, mode=PermissionMode.AUTO)
    parent.sandbox.dir_grants[str(outside.resolve())] = True

    # Existing broad grants remain visible for read compatibility, but cannot
    # authorize a write outside the child's frozen scope.
    child = build_permission_checker(
        workspace,
        parent_policy=parent,
        writable_scope=[workspace],
    )
    decision = child.check(
        WriteFile(),
        {"file_path": str(outside / "candidate.md"), "content": "blocked"},
    )
    assert child.sandbox.dir_grants is parent.sandbox.dir_grants
    assert decision.effect == "deny"
    assert decision.subcondition == "write_target_outside_child_scope"

    with pytest.raises(ValueError, match="exceeds parent policy"):
        build_permission_checker(outside, parent_policy=parent)


def test_auditor_profile_is_read_only_for_workspace_and_can_write_only_to_temp(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent = build_permission_checker(workspace, mode=PermissionMode.AUTO)
    auditor = build_permission_checker(
        workspace,
        parent_policy=parent,
        profile="auditor",
    )

    assert auditor.policy_profile == "auditor"
    assert auditor.policy_writable_roots == (Path(tempfile.gettempdir()).resolve(),)
    denied = auditor.check(
        WriteFile(), {"file_path": str(workspace / "review.md"), "content": "x"}
    )
    allowed = auditor.check(
        WriteFile(),
        {"file_path": str(auditor.policy_writable_roots[0] / "review-output.txt"), "content": "x"},
    )
    assert denied.effect == "deny"
    assert denied.source == "child_write_scope"
    assert allowed.effect == "allow"

    with pytest.raises(ValueError, match="auditor profile is writable only"):
        build_permission_checker(
            workspace,
            parent_policy=parent,
            profile="auditor",
            writable_scope=[workspace],
        )


def test_worktree_profile_is_exactly_the_worktree_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    worktree = workspace / "worktree"
    workspace.mkdir()
    worktree.mkdir()
    parent = build_permission_checker(workspace, mode=PermissionMode.AUTO)

    child = build_permission_checker(
        worktree,
        parent_policy=parent,
        profile="worktree",
    )
    assert child.policy_writable_roots == (worktree.resolve(),)

    with pytest.raises(ValueError, match="only at its project/worktree root"):
        build_permission_checker(
            worktree,
            parent_policy=parent,
            profile="worktree",
            writable_scope=[workspace],
        )


def test_child_cannot_raise_parent_mode_but_may_choose_plan_mode(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent = build_permission_checker(workspace, mode=PermissionMode.PLAN)

    with pytest.raises(ValueError, match="cannot be broader"):
        build_permission_checker(
            workspace,
            parent_policy=parent,
            mode=PermissionMode.AUTO,
        )

    child = build_permission_checker(
        workspace,
        parent_policy=parent,
        mode=PermissionMode.PLAN,
    )
    assert child.mode is PermissionMode.PLAN


@pytest.mark.parametrize(
    "child_mode",
    [PermissionMode.DEFAULT, PermissionMode.ACCEPT_EDITS, PermissionMode.STRICT_ISOLATION],
)
def test_auto_parent_rejects_modes_that_drop_its_native_command_boundary(
    tmp_path: Path,
    child_mode: PermissionMode,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent = build_permission_checker(workspace, mode=PermissionMode.AUTO)

    with pytest.raises(ValueError, match="cannot be broader"):
        build_permission_checker(workspace, parent_policy=parent, mode=child_mode)

    plan_child = build_permission_checker(
        workspace, parent_policy=parent, mode=PermissionMode.PLAN,
    )
    auto_child = build_permission_checker(
        workspace, parent_policy=parent, mode=PermissionMode.AUTO,
    )
    assert plan_child.mode is PermissionMode.PLAN
    assert auto_child.mode is PermissionMode.AUTO


@pytest.mark.parametrize(
    "child_mode",
    [PermissionMode.DEFAULT, PermissionMode.ACCEPT_EDITS, PermissionMode.AUTO],
)
def test_strict_parent_allows_only_strict_or_plan_children(
    tmp_path: Path,
    child_mode: PermissionMode,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent = build_permission_checker(workspace, mode=PermissionMode.STRICT_ISOLATION)

    with pytest.raises(ValueError, match="cannot be broader"):
        build_permission_checker(workspace, parent_policy=parent, mode=child_mode)

    strict_child = build_permission_checker(
        workspace, parent_policy=parent, mode=PermissionMode.STRICT_ISOLATION,
    )
    plan_child = build_permission_checker(
        workspace, parent_policy=parent, mode=PermissionMode.PLAN,
    )
    assert strict_child.mode is PermissionMode.STRICT_ISOLATION
    assert plan_child.mode is PermissionMode.PLAN


def test_explicit_child_rule_paths_cannot_replace_parent_read_or_write_denies(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent_user = tmp_path / "parent-user.rules.yml"
    child_user = tmp_path / "child-user.rules.yml"
    parent_user.write_text("", encoding="utf-8")
    child_user.write_text(
        '- rule: "ReadFile(*)"\n  effect: allow\n'
        '- rule: "WriteFile(*)"\n  effect: allow\n',
        encoding="utf-8",
    )
    parent = build_permission_checker(
        workspace,
        mode=PermissionMode.DEFAULT,
        rule_paths=PermissionRulePaths(user=parent_user),
    )
    child = build_permission_checker(
        workspace,
        parent_policy=parent,
        rule_paths=PermissionRulePaths(user=child_user),
    )
    parent_user.write_text(
        '- rule: "ReadFile(*)"\n  effect: deny\n'
        '- rule: "WriteFile(*)"\n  effect: deny\n',
        encoding="utf-8",
    )

    read = child.check(ReadFile(), {"file_path": str(workspace / "note.txt")})
    write = child.check(
        WriteFile(), {"file_path": str(workspace / "note.txt"), "content": "x"},
    )
    assert (read.effect, read.source) == ("deny", "rule_engine")
    assert (write.effect, write.source) == ("deny", "rule_engine")


def test_parent_ask_rule_survives_child_rule_path_override(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent_project = tmp_path / "parent" / "permissions.yaml"
    parent_rules = parent_project.parent / "rules"
    parent_rules.mkdir(parents=True)
    shell = "powershell" if os.name == "nt" else "bash"
    (parent_rules / "default.rules").write_text(
        f'prefix_rule(pattern=["git", "status"], decision="prompt", '
        f'shell="{shell}", match=["git status --short"], '
        f'not_match=["git statusx"])\n',
        encoding="utf-8",
    )

    child_project = tmp_path / "child" / "permissions.yaml"
    child_project.parent.mkdir(parents=True)
    child_project.write_text(
        '- rule: "Bash(git status*)"\n  effect: allow\n', encoding="utf-8",
    )
    parent = build_permission_checker(
        workspace,
        mode=PermissionMode.DEFAULT,
        rule_paths=PermissionRulePaths(project=parent_project),
    )
    child = build_permission_checker(
        workspace,
        parent_policy=parent,
        rule_paths=PermissionRulePaths(project=child_project),
    )

    decision = child.check(Bash(), {"command": "git status --short"})
    assert (decision.effect, decision.source) == ("ask", "command_prefix_rule")


def test_inherited_parent_allow_remains_bounded_by_child_write_scope(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent_user = tmp_path / "parent-user.rules.yml"
    parent_user.write_text('- rule: "WriteFile(*)"\n  effect: allow\n', encoding="utf-8")
    parent = build_permission_checker(
        workspace,
        mode=PermissionMode.DEFAULT,
        rule_paths=PermissionRulePaths(user=parent_user),
    )
    child = build_permission_checker(workspace, parent_policy=parent)

    inside = child.check(
        WriteFile(), {"file_path": str(workspace / "note.txt"), "content": "x"},
    )
    outside = child.check(
        WriteFile(), {"file_path": str(tmp_path / "outside.txt"), "content": "x"},
    )
    assert (inside.effect, inside.source) == ("allow", "rule_engine")
    assert (outside.effect, outside.source) == ("deny", "child_write_scope")


def test_parent_allow_does_not_override_a_child_ask_rule(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent_user = tmp_path / "parent-user.rules.yml"
    parent_user.write_text('- rule: "Bash(git status*)"\n  effect: allow\n', encoding="utf-8")
    child_project = tmp_path / "child" / "permissions.yaml"
    child_rules = child_project.parent / "rules"
    child_rules.mkdir(parents=True)
    shell = "powershell" if os.name == "nt" else "bash"
    (child_rules / "default.rules").write_text(
        f'prefix_rule(pattern=["git", "status"], decision="prompt", '
        f'shell="{shell}", match=["git status --short"], '
        f'not_match=["git statusx"])\n',
        encoding="utf-8",
    )
    parent = build_permission_checker(
        workspace,
        mode=PermissionMode.DEFAULT,
        rule_paths=PermissionRulePaths(user=parent_user),
    )
    child = build_permission_checker(
        workspace,
        parent_policy=parent,
        rule_paths=PermissionRulePaths(project=child_project),
    )

    decision = child.check(Bash(), {"command": "git status --short"})
    assert (decision.effect, decision.source) == ("ask", "command_prefix_rule")


def test_child_session_approval_remains_available_after_policy_composition(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "note.txt"
    parent = build_permission_checker(workspace, mode=PermissionMode.DEFAULT)
    child = build_permission_checker(workspace, parent_policy=parent)
    child.rule_engine.append_session_rule(Rule("WriteFile", str(target), "allow"))

    decision = child.check(WriteFile(), {"file_path": str(target), "content": "x"})
    assert (decision.effect, decision.source) == ("allow", "rule_engine")


@pytest.mark.parametrize("failure_stage", ["construct", "expanduser", "resolve"])
def test_policy_write_scope_fails_closed_on_path_processing_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    import mewcode.permissions.policy as policy_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    checker = build_permission_checker(workspace, mode=PermissionMode.AUTO)
    candidate = workspace / "candidate.txt"

    if failure_stage == "construct":
        class BrokenPathLike:
            def __fspath__(self) -> str:
                raise TypeError("invalid path-like object")

        target: object = BrokenPathLike()
    else:
        target = str(candidate)

        def fail_path_processing(self) -> Path:
            if failure_stage == "expanduser":
                raise ValueError("home expansion failed")
            raise TypeError("path resolution failed")

        monkeypatch.setattr(policy_module.Path, failure_stage, fail_path_processing)

    assert checker.sandbox.allows_policy_write(target) is False

    monkeypatch.undo()
    assert checker.sandbox.allows_policy_write(str(candidate)) is True


def test_explicit_child_rule_paths_keep_parent_tiers_and_session_rules(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent_user = tmp_path / "user.rules.yml"
    child_project = tmp_path / "child.rules.yml"
    parent_user.write_text('- rule: "ReadFile(*)"\n  effect: deny\n', encoding="utf-8")
    child_project.write_text('- rule: "WriteFile(*)"\n  effect: deny\n', encoding="utf-8")
    parent = build_permission_checker(
        workspace,
        rule_paths=PermissionRulePaths(user=parent_user),
    )
    parent.rule_engine.append_session_rule(Rule("Bash", "git push*", "deny"))

    child = build_permission_checker(
        workspace,
        parent_policy=parent,
        rule_paths=PermissionRulePaths(project=child_project),
    )
    assert child.rule_engine.matches_deny("ReadFile", "anything")
    assert child.rule_engine.matches_deny("WriteFile", "anything")
    assert child.rule_engine.matches_deny("Bash", "git push origin main")
