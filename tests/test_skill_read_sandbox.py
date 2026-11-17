import pytest

from mewcode.permissions import (
    DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine,
)
from mewcode.tools.read_file import ReadFile
from mewcode.tools.write_file import WriteFile


@pytest.fixture
def scope(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    fake_home = tmp_path / "user"
    fake_home.mkdir()
    (fake_home / ".ssh").mkdir()
    skills = fake_home / ".mewcode" / "skills"
    skill = skills / "ko5-6sol" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# Test skill", encoding="utf-8")
    # Keep the test user's home outside the implicitly allowed temp directory.
    monkeypatch.setattr("mewcode.permissions.sandbox.tempfile.gettempdir", lambda: str(tmp_path / "temp"))
    monkeypatch.setattr("mewcode.skills.loader.USER_SKILLS_DIR", str(skills))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("APPDATA", str(fake_home / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(fake_home / "AppData" / "Local"))
    return PathSandbox(str(project)), skill


@pytest.mark.parametrize("mode", list(PermissionMode))
def test_user_skill_is_readable_but_not_writable(scope, mode):
    sandbox, skill = scope
    checker = PermissionChecker(DangerousCommandDetector(), sandbox, RuleEngine(), mode)
    expected = "ask" if mode == PermissionMode.CUSTOM else "allow"
    assert checker.check(ReadFile(), {"file_path": str(skill)}).effect == expected
    # Current path policy can ask for an explicit grant; skill discovery alone
    # never makes the user skill writable.
    write_decision = checker.check(
        WriteFile(), {"file_path": str(skill), "content": "changed"}
    )
    if mode == PermissionMode.STRICT_ISOLATION:
        # Strict isolation may ask before a user-authorized external write;
        # the important invariant is that the write is never auto-executed.
        assert write_decision.effect in {"ask", "deny"}
    else:
        expected_write = "ask" if mode in {
            PermissionMode.DEFAULT,
            PermissionMode.ACCEPT_EDITS,
            PermissionMode.AUTO,
            PermissionMode.CUSTOM,
        } else "deny"
        assert write_decision.effect == expected_write
    assert write_decision.effect != "allow"
    assert skill.read_text(encoding="utf-8") == "# Test skill"


def test_explicit_external_reads_and_relative_traversal(scope):
    sandbox, skill = scope
    resource = skill.parent / "references" / "guide.md"
    resource.parent.mkdir()
    resource.write_text("Guide", encoding="utf-8")
    assert sandbox.check(str(resource), read_only=True)[0]
    assert not sandbox.check(str(resource))[0]
    config = skill.parents[2] / "config.yaml"
    config.write_text("private", encoding="utf-8")
    # An explicit absolute non-sensitive path is readable even outside the
    # advertised skill root; a relative workspace escape remains blocked.
    assert sandbox.check(str(config), read_only=True)[0]
    assert sandbox.check(str(skill.parents[2] / "skills-other" / "SKILL.md"), read_only=True)[0]
    allowed, reason = sandbox.check("../user/.mewcode/config.yaml", read_only=True)
    assert not allowed
    assert "相对路径或目录联接逃逸" in reason


def test_sensitive_profile_path_is_still_blocked(scope):
    sandbox, skill = scope
    fake_ssh_dir = skill.parents[3] / ".ssh"
    assert fake_ssh_dir.is_dir()
    simulated_key_path = fake_ssh_dir / "fixture-key"
    allowed, reason = sandbox.check(str(simulated_key_path), read_only=True)
    assert not allowed
    assert "敏感位置读取被硬规则禁止" in reason


def test_workspace_symlink_cannot_escape_read_scope(scope):
    sandbox, _skill = scope
    outside_dir = sandbox.project_root.parent / "external"
    outside_dir.mkdir()
    outside = outside_dir / "ordinary.txt"
    outside.write_text("ordinary external fixture", encoding="utf-8")
    link = sandbox.project_root / "linked.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    allowed, reason = sandbox.check(str(link), read_only=True)
    assert not allowed
    assert "相对路径或目录联接逃逸" in reason
