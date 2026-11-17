"""Directory grants keep external writes explicit and narrowly scoped.

A file grant covers exactly one file, so Glob/Grep/ReadFile over a directory
outside the workspace could not be authorised at all. Explicit non-sensitive
absolute reads are now allowed; these tests pin the narrower write contract:
directory grants cover that directory and its descendants, never siblings, and
are read-only unless writes were explicitly requested.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from mewcode.permissions.sandbox import PathSandbox

@pytest.fixture
def fixture_paths(tmp_path, monkeypatch):
    project = tmp_path / "project"
    external = tmp_path / "external"
    sibling = tmp_path / "sibling"
    fake_home = tmp_path / "fake-home"
    for directory in (project, external, sibling, fake_home):
        directory.mkdir()
    project_file = project / "existing.txt"
    project_file.write_text("project fixture", encoding="utf-8")
    external_file = external / "ordinary.txt"
    external_file.write_text("external fixture", encoding="utf-8")
    (fake_home / ".ssh").mkdir()
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("APPDATA", str(fake_home / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(fake_home / "AppData" / "Local"))
    return SimpleNamespace(
        project=project,
        external=external,
        sibling=sibling,
        fake_home=fake_home,
        project_file=project_file,
        external_file=external_file,
    )


def _sandbox(paths, **kwargs):
    sandbox = PathSandbox(str(paths.project), **kwargs)
    # Keep all fixture paths outside the host's default temp allowance so
    # write checks exercise grants rather than pytest's temp location.
    sandbox._allowed_roots = [paths.project.resolve()]
    return sandbox


class TestDirectoryGrants:
    def test_explicit_external_read_is_allowed_but_write_requires_scope(self, fixture_paths):
        sandbox = _sandbox(fixture_paths)
        assert sandbox.check(str(fixture_paths.external_file), read_only=True)[0] is True
        allowed, reason = sandbox.check(str(fixture_paths.external_file), read_only=False)
        assert allowed is False
        assert "超出沙箱范围" in reason
        assert "目录授权" in reason, "拦截提示必须告诉用户怎么办"

    def test_sensitive_profile_path_is_still_blocked(self, fixture_paths):
        sandbox = _sandbox(fixture_paths)
        simulated_secret = fixture_paths.fake_home / ".ssh" / "fixture-key"
        allowed, reason = sandbox.check(str(simulated_secret), read_only=True)
        assert allowed is False
        assert "敏感位置读取被硬规则禁止" in reason

    def test_relative_path_escape_is_still_blocked(self, fixture_paths):
        sandbox = _sandbox(fixture_paths)
        allowed, reason = sandbox.check("../external/ordinary.txt", read_only=True)
        assert allowed is False
        assert "相对路径或目录联接逃逸" in reason

    def test_grant_covers_directory_but_not_sibling_writes(self, fixture_paths):
        sandbox = _sandbox(fixture_paths)
        granted = sandbox.grant_directory(str(fixture_paths.external), writable=True)
        assert Path(granted) == fixture_paths.external.resolve()
        nested = fixture_paths.external / "nested" / "file.md"
        sibling_file = fixture_paths.sibling / "some-other-project.md"
        assert sandbox.check(str(fixture_paths.external_file), read_only=False)[0] is True
        assert sandbox.check(str(nested), read_only=False)[0] is True
        assert sandbox.check(str(fixture_paths.sibling), read_only=True)[0] is True
        allowed, reason = sandbox.check(str(sibling_file), read_only=False)
        assert allowed is False
        assert "超出沙箱范围" in reason

    def test_read_only_grant_does_not_allow_writes(self, fixture_paths):
        sandbox = _sandbox(fixture_paths)
        sandbox.grant_directory(str(fixture_paths.external))
        target = fixture_paths.external / "x.md"
        assert sandbox.check(str(target), read_only=True)[0] is True
        assert sandbox.check(str(target), read_only=False)[0] is False

    def test_writable_grant_allows_writes(self, fixture_paths):
        sandbox = _sandbox(fixture_paths)
        sandbox.grant_directory(str(fixture_paths.external), writable=True)
        assert sandbox.check(str(fixture_paths.external / "x.md"), read_only=False)[0] is True

    def test_grants_are_shared_by_reference_with_already_created_sandboxes(self, fixture_paths):
        grants: dict[str, bool] = {}
        first = _sandbox(fixture_paths, dir_grants=grants)
        second = _sandbox(fixture_paths, dir_grants=grants)
        target = fixture_paths.external / "a.md"
        assert second.check(str(target), read_only=False)[0] is False
        first.grant_directory(str(fixture_paths.external), writable=True)
        assert second.check(str(target), read_only=False)[0] is True, "已存在的节点必须立刻看到新授权"

    @pytest.mark.parametrize("bad", ["relative/dir", ""])
    def test_relative_path_is_rejected(self, bad, fixture_paths):
        sandbox = _sandbox(fixture_paths)
        with pytest.raises(ValueError, match="绝对路径"):
            sandbox.grant_directory(bad)

    def test_a_file_is_not_a_directory_grant(self, fixture_paths):
        sandbox = _sandbox(fixture_paths)
        with pytest.raises(ValueError, match="现有目录"):
            sandbox.grant_directory(str(fixture_paths.project_file))

    def test_missing_directory_is_rejected(self, fixture_paths):
        sandbox = _sandbox(fixture_paths)
        with pytest.raises((ValueError, OSError)):
            sandbox.grant_directory(str(fixture_paths.external / "definitely-missing-dir-xyz"))

    def test_a_whole_drive_is_rejected(self, fixture_paths):
        sandbox = _sandbox(fixture_paths)
        with pytest.raises(ValueError, match="整个磁盘"):
            sandbox.grant_directory(str(Path(fixture_paths.project.anchor)))

    def test_file_grants_and_dir_grants_are_independent(self, fixture_paths):
        """An exact writable file grant must not open its folder or sibling."""
        grants: dict[str, bool] = {}
        sandbox = _sandbox(fixture_paths, file_grants=grants)
        sandbox.grant_file(str(fixture_paths.external_file), writable=True)
        assert sandbox.check(str(fixture_paths.external_file), read_only=False)[0] is True
        assert sandbox.check(str(fixture_paths.external_file.parent), read_only=True)[0] is True
        assert sandbox.check(str(fixture_paths.external_file.parent), read_only=False)[0] is False
        assert sandbox.check(str(fixture_paths.sibling / "other.txt"), read_only=True)[0] is True
        assert sandbox.check(str(fixture_paths.sibling / "other.txt"), read_only=False)[0] is False
        assert sandbox.dir_grants == {}


class TestDirGrantEndpoint:
    """The endpoint is the only place a directory can enter the sandbox."""

    @pytest.fixture()
    def client(self, monkeypatch, fixture_paths):
        import httpx
        from frontend import server

        runtime = SimpleNamespace(
            execution_kind="orchestration",
            expert_run_id="run-1",
            work_dir=str(fixture_paths.project),
            dir_access_grants={},
            file_access_grants={},
        )
        monkeypatch.setattr(
            server, "SESSION",
            SimpleNamespace(_active_queue_runtime=lambda conversation_id: runtime),
        )
        transport = httpx.ASGITransport(app=server.app)
        return httpx.AsyncClient(transport=transport, base_url="http://test"), runtime

    @pytest.mark.asyncio
    async def test_without_the_explicit_header_it_is_refused(self, client, fixture_paths):
        http, runtime = client
        async with http as session:
            response = await session.post(
                "/api/orchestration/dir-grants",
                json={"conversation_id": "c", "run_id": "run-1", "project": str(fixture_paths.project),
                      "path": str(fixture_paths.external), "access": "read"},
            )
        assert response.status_code == 403
        assert "明确确认" in response.json()["error"]
        assert runtime.dir_access_grants == {}

    @pytest.mark.asyncio
    async def test_read_only_directory_grant_is_recorded(self, client, fixture_paths):
        http, runtime = client
        async with http as session:
            response = await session.post(
                "/api/orchestration/dir-grants",
                headers={"X-Mew-File-Grant": "1"},
                json={"conversation_id": "c", "run_id": "run-1", "project": str(fixture_paths.project),
                      "path": str(fixture_paths.external), "access": "read"},
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["access"] == "read"
        assert "只读" in body["message"]
        assert str(fixture_paths.external.resolve()) in body["path"]
        assert set(runtime.dir_access_grants.values()) == {False}

    @pytest.mark.asyncio
    async def test_write_access_is_recorded_as_writable(self, client, fixture_paths):
        http, runtime = client
        async with http as session:
            response = await session.post(
                "/api/orchestration/dir-grants",
                headers={"X-Mew-File-Grant": "1"},
                json={"conversation_id": "c", "run_id": "run-1", "project": str(fixture_paths.project),
                      "path": str(fixture_paths.external), "access": "read_write"},
            )
        assert response.status_code == 200, response.text
        assert set(runtime.dir_access_grants.values()) == {True}

    @pytest.mark.asyncio
    async def test_a_file_or_missing_path_is_rejected(self, client, fixture_paths):
        http, runtime = client
        async with http as session:
            for path in (str(fixture_paths.project_file), str(fixture_paths.external / "no-such-dir-xyz")):
                response = await session.post(
                    "/api/orchestration/dir-grants",
                    headers={"X-Mew-File-Grant": "1"},
                    json={"conversation_id": "c", "run_id": "run-1", "project": str(fixture_paths.project),
                          "path": path, "access": "read"},
                )
                assert response.status_code == 400, path
        assert runtime.dir_access_grants == {}

    @pytest.mark.asyncio
    async def test_bad_access_and_stale_run_are_rejected(self, client, fixture_paths):
        http, runtime = client
        async with http as session:
            bad_access = await session.post(
                "/api/orchestration/dir-grants",
                headers={"X-Mew-File-Grant": "1"},
                json={"conversation_id": "c", "run_id": "run-1", "project": str(fixture_paths.project),
                      "path": str(fixture_paths.external), "access": "everything"},
            )
            stale = await session.post(
                "/api/orchestration/dir-grants",
                headers={"X-Mew-File-Grant": "1"},
                json={"conversation_id": "c", "run_id": "other-run", "project": str(fixture_paths.project),
                      "path": str(fixture_paths.external), "access": "read"},
            )
        assert bad_access.status_code == 400
        assert stale.status_code == 400
        assert runtime.dir_access_grants == {}
