from __future__ import annotations

from pathlib import Path

import pytest

from mewcode.experts.node_expansion import ExpansionPathSandbox
from mewcode.permissions.checker import PermissionChecker
from mewcode.permissions.dangerous import DangerousCommandDetector
from mewcode.permissions.grants import GrantStore
from mewcode.permissions.modes import PermissionMode
from mewcode.permissions.rules import Rule, RuleEngine
from mewcode.tools.edit_file import EditFile
from mewcode.tools.read_file import ReadFile
from mewcode.tools.write_file import WriteFile


class ParentSandbox:
    """Small parent policy double: writes stay in root; reads remain broad."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()

    def check(self, path: str, *, read_only: bool = False) -> tuple[bool, str]:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.project_root / candidate
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return False, "cannot resolve path"
        if read_only or resolved.is_relative_to(self.project_root):
            return True, ""
        return False, "outside parent write scope"

    def grant_approved_path(self, path: str, *, read_only: bool = True) -> str:
        return ""


def _checker(
    root: Path,
    *,
    declared: tuple[str, ...] = ("declared",),
    mode: PermissionMode = PermissionMode.DEFAULT,
    rule_engine: RuleEngine | None = None,
    grant_store: GrantStore | None = None,
) -> tuple[PermissionChecker, ExpansionPathSandbox]:
    root.mkdir(parents=True, exist_ok=True)
    sandbox = ExpansionPathSandbox(ParentSandbox(root), declared)
    checker = PermissionChecker(
        DangerousCommandDetector(),
        sandbox,
        rule_engine or RuleEngine(),
        mode,
        grant_store=grant_store,
    )
    return checker, sandbox


def _grant_write_directory(store: GrantStore, path: Path) -> None:
    scope = store.preview_scope("write-directory", path, 3600)
    store.grant(
        "write-directory",
        path,
        ttl_seconds=3600,
        confirmation={
            "confirmed": True,
            "actor": "user",
            "confirmation_id": "confirm-node-scope-0001",
            "confirmed_at": store.clock(),
            "scope": scope,
        },
    )


def _assert_scope_denial(decision) -> None:
    assert decision.effect == "deny"
    assert decision.source == "node_write_scope"
    assert decision.subcondition == "outside_declared_write_set"
    assert decision.clearance == "revise_node_contract_with_authorization"


def test_persistent_directory_grant_cannot_expand_declared_write_set(tmp_path: Path) -> None:
    root = tmp_path / "project"
    outside = tmp_path / "persistent-authorized"
    root.mkdir()
    outside.mkdir()
    store = GrantStore(root)
    _grant_write_directory(store, outside)
    checker, _ = _checker(root, grant_store=store)

    decision = checker.check(
        WriteFile(), {"file_path": str(outside / "new.txt"), "content": "x"}
    )

    _assert_scope_denial(decision)


def test_session_grant_and_explicit_allow_cannot_expand_declared_write_set(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    target = root / "outside-declaration.txt"
    arguments = {"file_path": str(target), "content": "x"}

    checker, _ = _checker(root)
    assert checker.grant_call("WriteFile", arguments)
    _assert_scope_denial(checker.check(WriteFile(), arguments))

    rules = RuleEngine()
    rules.append_session_rule(Rule("WriteFile", str(target), "allow"))
    checker, _ = _checker(root, rule_engine=rules)
    _assert_scope_denial(checker.check(WriteFile(), arguments))


def test_bypass_and_approved_revision_cannot_expand_declared_write_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    arguments = {"file_path": str(root / "outside.txt"), "content": "x"}
    checker, _ = _checker(root, mode=PermissionMode.BYPASS)
    _assert_scope_denial(checker.check(WriteFile(), arguments))

    monkeypatch.setattr(
        "mewcode.experts.recovery_scope.current_approved_revision", lambda: object()
    )
    checker, _ = _checker(root, mode=PermissionMode.ACCEPT_EDITS)
    _assert_scope_denial(checker.check(EditFile(), {
        "file_path": str(root / "outside.txt"),
        "old_string": "old",
        "new_string": "new",
    }))


def test_relative_declarations_resolve_from_parent_project_root(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    checker, sandbox = _checker(root, declared=("declared/subdir",))

    assert sandbox.check_declared_write_scope("declared/subdir/new.txt") == (True, "")
    assert sandbox.check("declared/subdir/new.txt", read_only=False) == (True, "")
    assert checker.check(
        WriteFile(),
        {"file_path": "declared/subdir/new.txt", "content": "x"},
    ).effect == "ask"


def test_legal_child_write_and_undeclared_read_keep_existing_behavior(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    checker, sandbox = _checker(root, mode=PermissionMode.ACCEPT_EDITS)

    allowed = root / "declared" / "nested" / "new.txt"
    assert sandbox.check_declared_write_scope(str(allowed)) == (True, "")
    assert checker.check(
        WriteFile(), {"file_path": str(allowed), "content": "x"}
    ).effect == "allow"

    read_path = root / "not-declared" / "input.txt"
    read_decision = checker.check(ReadFile(), {"file_path": str(read_path)})
    assert read_decision.effect == "allow"
    assert read_decision.source != "node_write_scope"


def test_nested_scope_cannot_expand_ancestor_declaration(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    parent = ParentSandbox(root)
    outer = ExpansionPathSandbox(parent, ("declared/narrow",))
    inner = ExpansionPathSandbox(outer, ("declared",))

    assert inner.check_declared_write_scope("declared/narrow/file.txt") == (True, "")
    ok, reason = inner.check_declared_write_scope("declared/wider/file.txt")
    assert not ok
    assert "父微图" in reason or "不在微图" in reason


def test_resolve_failure_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "project"
    root.mkdir()
    sandbox = ExpansionPathSandbox(ParentSandbox(root), ("declared",))

    def fail_resolve(self, *args, **kwargs):
        raise OSError("simulated resolution failure")

    monkeypatch.setattr(Path, "resolve", fail_resolve)
    ok, reason = sandbox.check_declared_write_scope("declared/file.txt")
    assert not ok
    assert "无法解析" in reason

