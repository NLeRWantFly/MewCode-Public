import io
import json
import stat
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mewcode.skills import installer
from mewcode.skills.loader import SkillLoader
from mewcode.skills.runtime import attach_skills
from mewcode.tools import ToolRegistry
from mewcode.tools.install_skill import InstallSkill, InstallSkillParams
from mewcode.tools.load_skill import LoadSkillParams


def archive(extra=None, skill="demo"):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr(f"repo-main/skills/demo/SKILL.md", f"---\nname: {skill}\ndescription: Test\n---\nUse references/data.txt")
        for name, content in (extra or {}).items():
            info = zipfile.ZipInfo()
            info.filename = "repo-main/skills/demo/" + name
            z.writestr(info, content)
    return data.getvalue()


def install(root, data):
    return installer.install_archive(root, data, repo="owner/repo", ref="main", path="skills/demo")


def test_install_preserves_resources_and_never_overwrites(tmp_path):
    result = install(tmp_path, archive({"references/data.txt": "evidence"}))
    target = tmp_path / ".mewcode/skills/demo"
    assert result["name"] == "demo"
    assert (target / "references/data.txt").read_text() == "evidence"
    receipt = json.loads((target / installer.RECEIPT).read_text())
    assert len(receipt["archive_sha256"]) == 64 and receipt["instruction_only"]
    before = (target / "SKILL.md").read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        install(tmp_path, archive())
    assert (target / "SKILL.md").read_bytes() == before
    assert not list(target.parent.glob(".install-*"))


@pytest.mark.parametrize("path", ["../../escape", "C:/escape", "CON.txt", "a\\b", "file:stream", "a/../b", "a.", ".mewcode-install.json", "skill.md"])
def test_unsafe_archive_paths_rejected_before_publish(tmp_path, path):
    with pytest.raises(ValueError):
        install(tmp_path, archive({path: "bad"}))
    assert not (tmp_path / ".mewcode/skills/demo").exists()


def test_symlink_archive_rejected(tmp_path):
    data = io.BytesIO(archive())
    with zipfile.ZipFile(data, "a") as z:
        info = zipfile.ZipInfo("repo-main/skills/demo/link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(info, "../../outside")
    with pytest.raises(ValueError, match="link"):
        install(tmp_path, data.getvalue())


def test_size_and_identity_limits(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="name must match"):
        install(tmp_path, archive(skill="other"))
    monkeypatch.setattr(installer, "MAX_EXPANDED", 10)
    with pytest.raises(ValueError, match="limits"):
        install(tmp_path, archive())


@pytest.mark.asyncio
async def test_tool_refreshes_catalog_but_never_imports_remote_tools(tmp_path, monkeypatch):
    async def download(*args):
        return archive({"tool.json": '[{"name":"Exploit"}]',
                        "references/Exploit.py": 'raise RuntimeError("must not import")'})
    monkeypatch.setattr(installer, "download", download)
    registry = ToolRegistry()
    agent = SimpleNamespace(set_skill_catalog=Mock(), activate_skill=Mock(), registry=registry)
    loader = attach_skills(str(tmp_path), agent, registry)
    tool = registry.get("InstallSkill")
    assert tool and not tool.is_read_only
    result = await tool.execute(InstallSkillParams(repo="owner/repo", path="skills/demo"))
    assert not result.is_error and loader.get("demo")
    assert "demo" in agent.set_skill_catalog.call_args.args[0]
    loaded = await registry.get("LoadSkill").execute(LoadSkillParams(name="demo"))
    assert not loaded.is_error and registry.get("Exploit") is None
    assert agent.activate_skill.called


def test_installer_discoverable_but_not_in_restricted_node_tools(tmp_path):
    registry = ToolRegistry()
    agent = SimpleNamespace(set_skill_catalog=Mock())
    loader = attach_skills(str(tmp_path), agent, registry, instruction_only=True)
    assert loader.get("skill-installer")
    assert registry.get("InstallSkill") is None


@pytest.mark.asyncio
async def test_invalid_source_never_fetches(tmp_path, monkeypatch):
    download = Mock(side_effect=AssertionError("must not fetch"))
    monkeypatch.setattr(installer, "download", download)
    with pytest.raises(ValueError):
        await installer.install(tmp_path, repo="https://evil.invalid/repo", ref="main", path="skills/demo")
    download.assert_not_called()


def test_concurrent_lock_not_removed_or_overwritten(tmp_path):
    base = tmp_path / ".mewcode/skills"
    base.mkdir(parents=True)
    lock = base / ".install-demo.lock"
    lock.write_text("other process")
    with pytest.raises(FileExistsError):
        install(tmp_path, archive())
    assert lock.read_text() == "other process"
    assert not (base / "demo").exists()


def test_hidden_staging_is_not_discovered(tmp_path):
    path = tmp_path / ".mewcode/skills/.install-incomplete"
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text("---\nname: unfinished\ndescription: Test\n---\n")
    assert SkillLoader(str(tmp_path)).get("unfinished") is None
    assert "unfinished" not in SkillLoader(str(tmp_path)).load_all()


def test_install_requires_permission_and_plan_mode_cannot_install(tmp_path):
    from mewcode.permissions import PermissionChecker, PermissionMode, DangerousCommandDetector, PathSandbox, RuleEngine
    tool = InstallSkill(str(tmp_path), SkillLoader(str(tmp_path)), SimpleNamespace())
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(tmp_path)), RuleEngine(), PermissionMode.AUTO)
    args = {"repo": "owner/repo", "path": "skills/demo", "ref": "main"}
    decision = checker.check(tool, args)
    # AUTO requests semantic review of the new network/install scope; it does
    # not grant it outright. PLAN must still forbid the operation entirely.
    assert decision.effect == "review" and decision.risk["network"]
    checker.mode = PermissionMode.PLAN
    assert checker.check(tool, args).effect == "deny"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [(403, b"secret response"), (302, b""), (200, b"x" * 11)])
async def test_download_failure_and_size_do_not_fallback(tmp_path, monkeypatch, status, body):
    import httpx
    original = httpx.AsyncClient
    def handler(request):
        assert request.url.host == "codeload.github.com"
        return httpx.Response(status, content=body, headers={"Location": "https://other.invalid"})
    monkeypatch.setattr(installer.httpx, "AsyncClient", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(installer, "MAX_ARCHIVE", 10)
    with pytest.raises(ValueError) as exc:
        await installer.install(tmp_path, repo="owner/repo", path="skills/demo", ref="main")
    assert "secret response" not in str(exc.value)
    assert not (tmp_path / ".mewcode").exists()
