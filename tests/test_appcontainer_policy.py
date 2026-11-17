from __future__ import annotations

import ntpath
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import mewcode.sandbox.appcontainer_policy as policy


SENTINEL = b"mewcode-synthetic-only-v1\n"


@pytest.fixture
def probe_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    root = tmp_path / "mewcode-lpac-probe-policy-tests"
    root.mkdir()
    (root / ".mewcode-synthetic-fixture").write_bytes(SENTINEL)

    cwd = root / "work"
    cwd.mkdir()
    runtime = cwd / "temp"
    runtime.mkdir()
    profile = root / "profile"
    app_data = profile / "AppData"
    roaming = app_data / "Roaming"
    local = app_data / "Local"
    roaming.mkdir(parents=True)
    local.mkdir()
    readable = cwd / "input.txt"
    readable.write_text("synthetic input", encoding="utf-8")
    writable = cwd / "output"
    writable.mkdir()
    fixture_executable = cwd / "probe.exe"
    fixture_executable.write_bytes(b"synthetic executable")

    system_root = tmp_path / "synthetic-windows"
    system32 = system_root / "System32"
    powershell_dir = system32 / "WindowsPowerShell" / "v1.0"
    powershell_dir.mkdir(parents=True)
    (system32 / "cmd.exe").write_bytes(b"synthetic cmd")
    powershell = powershell_dir / "powershell.exe"
    powershell.write_bytes(b"synthetic powershell")

    monkeypatch.setenv("SystemRoot", str(system_root))
    monkeypatch.delenv("WINDIR", raising=False)
    monkeypatch.setenv("PATH", "ambient-path-must-not-be-inherited")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-secret-must-not-be-inherited")
    monkeypatch.setenv("HTTPS_PROXY", "ambient-proxy-must-not-be-inherited")
    monkeypatch.setenv("USERPROFILE", "ambient-home-must-not-be-inherited")

    return {
        "root": root,
        "cwd": cwd,
        "runtime": runtime,
        "profile": profile,
        "roaming": roaming,
        "local": local,
        "readable": readable,
        "writable": writable,
        "fixture_executable": fixture_executable,
        "system_root": system_root,
        "system32": system32,
        "powershell": powershell,
    }


def _build(paths: dict[str, Path], **overrides) -> dict:
    params = {
        "fixture_root": paths["root"],
        "executable": paths["fixture_executable"],
        "arguments": ["--synthetic", "input.txt"],
        "cwd": paths["cwd"],
        "read_paths": [paths["readable"]],
        "write_paths": [paths["writable"]],
    }
    params.update(overrides)
    return policy.build_probe_request(**params)


def test_builds_exact_scoped_request_without_inheriting_ambient_environment(probe_fixture):
    paths = probe_fixture
    before = {p.relative_to(paths["root"]): p.read_bytes() if p.is_file() else None
              for p in paths["root"].rglob("*")}

    request = _build(paths, timeout_ms=12_345, lpac=False)

    assert request == {
        "fixture_root": str(paths["root"].resolve()),
        "executable": str(paths["fixture_executable"].resolve()),
        "arguments": ["--synthetic", "input.txt"],
        "cwd": str(paths["cwd"].resolve()),
        "read_paths": [str(paths["readable"].resolve())],
        "write_paths": [str(paths["writable"].resolve())],
        "execute_paths": [],
        "timeout_ms": 12_345,
        "lpac": False,
        "runtime_registry_read": False,
        "runtime_instrumentation": False,
        "runtime_com": False,
        "environment": {
            "SystemRoot": str(paths["system_root"].resolve()),
            "WINDIR": str(paths["system_root"].resolve()),
            "SystemDrive": ntpath.splitdrive(str(paths["system_root"].resolve()))[0],
            "COMSPEC": str((paths["system32"] / "cmd.exe").resolve()),
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "PATH": str(paths["system32"].resolve()),
            "TEMP": str(paths["runtime"].resolve()),
            "TMP": str(paths["runtime"].resolve()),
            "USERPROFILE": str(paths["profile"].resolve()),
            "APPDATA": str(paths["roaming"].resolve()),
            "LOCALAPPDATA": str(paths["local"].resolve()),
        },
    }
    assert set(request["environment"]) == {
        "SystemRoot", "WINDIR", "SystemDrive", "COMSPEC", "PATHEXT", "PATH", "TEMP", "TMP",
        "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    }
    assert "OPENAI_API_KEY" not in request["environment"]
    assert "HTTPS_PROXY" not in request["environment"]
    after = {p.relative_to(paths["root"]): p.read_bytes() if p.is_file() else None
             for p in paths["root"].rglob("*")}
    assert after == before


def test_environment_excludes_inherited_api_key_and_custom_proxy(probe_fixture):
    request = _build(probe_fixture)

    assert "OPENAI_API_KEY" not in request["environment"]
    assert "HTTPS_PROXY" not in request["environment"]


def test_com_candidate_is_explicit_and_lpac_only(probe_fixture):
    assert _build(probe_fixture)["runtime_com"] is False
    request = _build(probe_fixture, runtime_registry_read=True, runtime_com=True)
    assert request["runtime_com"] is True
    for options in ({}, {"lpac": False, "runtime_registry_read": False}):
        with pytest.raises(ValueError, match="runtime_com requires"):
            _build(probe_fixture, runtime_com=True, **options)


@pytest.mark.parametrize("value", [None, 1, "true", [], {}])
def test_com_candidate_cannot_accept_truthy_values(probe_fixture, value):
    with pytest.raises(ValueError, match="runtime_com must be a boolean"):
        _build(probe_fixture, runtime_com=value)


@pytest.mark.parametrize("executable_key", ["powershell", "system32"])
def test_accepts_only_the_explicit_trusted_windows_executable(probe_fixture, executable_key):
    paths = probe_fixture
    executable = paths["powershell"]
    if executable_key == "system32":
        executable = paths["system32"] / "cmd.exe"

    request = _build(paths, executable=executable)

    assert request["executable"] == str(executable.resolve())


def test_rejects_arbitrary_executable_outside_fixture(probe_fixture, tmp_path):
    outside_executable = tmp_path / "untrusted.exe"
    outside_executable.write_bytes(b"not a trusted system executable")

    with pytest.raises(ValueError, match="trusted Windows system path"):
        _build(probe_fixture, executable=outside_executable)


def _make_hardlink_or_skip(source: Path, alias: Path) -> None:
    try:
        os.link(source, alias)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"hardlinks are unavailable in this test environment: {exc}")


def test_rejects_grant_hardlink_to_synthetic_file_outside_fixture(probe_fixture, tmp_path):
    paths = probe_fixture
    outside = tmp_path / "synthetic-external.txt"
    outside.write_text("synthetic only; no secret data", encoding="utf-8")
    alias = paths["cwd"] / "external-alias.txt"
    _make_hardlink_or_skip(outside, alias)

    with pytest.raises(ValueError, match="must not be a hard link"):
        _build(paths, read_paths=[alias])


def test_execute_paths_default_empty_and_allow_only_exact_synthetic_file(probe_fixture):
    paths = probe_fixture
    default_request = _build(paths)
    request = _build(paths, execute_paths=[paths["readable"]])

    assert default_request["execute_paths"] == []
    assert request["execute_paths"] == [str(paths["readable"].resolve())]
    assert request["read_paths"] == [str(paths["readable"].resolve())]
    assert request["write_paths"] == [str(paths["writable"].resolve())]


def test_execute_paths_reject_outside_files_and_directories(probe_fixture, tmp_path):
    paths = probe_fixture
    outside_file = tmp_path / "synthetic-external.dll"
    outside_file.write_bytes(b"synthetic only")
    with pytest.raises(ValueError, match=r"execute_paths\[0\].*strictly beneath"):
        _build(paths, execute_paths=[outside_file])
    with pytest.raises(ValueError, match=r"execute_paths\[0\].*regular file"):
        _build(paths, execute_paths=[paths["writable"]])


def test_execute_paths_reject_reparse_ancestry(probe_fixture, monkeypatch):
    paths = probe_fixture
    alias = paths["cwd"] / "execute-reparse-alias"
    alias.mkdir()
    target = alias / "synthetic.dll"
    target.write_bytes(b"synthetic only")
    original_lstat = Path.lstat

    def lstat_with_reparse_marker(path: Path):
        info = original_lstat(path)
        if path == alias:
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_file_attributes=getattr(info, "st_file_attributes", 0) | 0x0400,
            )
        return info

    monkeypatch.setattr(Path, "lstat", lstat_with_reparse_marker)
    with pytest.raises(ValueError, match="execute_paths.*symlink or reparse point"):
        _build(paths, execute_paths=[target])


def test_execute_paths_reject_hardlinks(probe_fixture, tmp_path):
    paths = probe_fixture
    outside = tmp_path / "synthetic-external.dll"
    outside.write_bytes(b"synthetic only")
    alias = paths["cwd"] / "execute-hardlink.dll"
    _make_hardlink_or_skip(outside, alias)

    with pytest.raises(ValueError, match=r"execute_paths\[0\].*hard link"):
        _build(paths, execute_paths=[alias])


def test_rejects_fixture_executable_hardlink_to_synthetic_external_file(probe_fixture, tmp_path):
    paths = probe_fixture
    outside = tmp_path / "synthetic-external.exe"
    outside.write_bytes(b"synthetic executable only")
    alias = paths["cwd"] / "external-alias.exe"
    _make_hardlink_or_skip(outside, alias)

    with pytest.raises(ValueError, match="must not be a hard link"):
        _build(paths, executable=alias)


def test_rejects_hardlinked_fixture_sentinel(probe_fixture, tmp_path):
    sentinel = probe_fixture["root"] / ".mewcode-synthetic-fixture"
    outside_alias = tmp_path / "synthetic-sentinel-alias"
    _make_hardlink_or_skip(sentinel, outside_alias)

    with pytest.raises(ValueError, match="must not be a hard link"):
        _build(probe_fixture)


def test_allows_hardlinked_trusted_system_executable(probe_fixture, tmp_path):
    paths = probe_fixture
    trusted_cmd = paths["system32"] / "cmd.exe"
    trusted_cmd.unlink()
    outside = tmp_path / "synthetic-cmd.exe"
    outside.write_bytes(b"synthetic system executable only")
    _make_hardlink_or_skip(outside, trusted_cmd)

    request = _build(paths, executable=trusted_cmd)

    assert request["executable"] == str(trusted_cmd.resolve())


def test_sentinel_size_check_prevents_opening_an_oversized_marker(probe_fixture, monkeypatch):
    sentinel = probe_fixture["root"] / ".mewcode-synthetic-fixture"
    sentinel.write_bytes(b"x" * 1_000_000)
    original_open = Path.open

    def reject_sentinel_open(path: Path, *args, **kwargs):
        if path == sentinel:
            pytest.fail("oversized sentinel should be rejected from its stat size")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_sentinel_open)
    with pytest.raises(ValueError, match="sentinel content"):
        _build(probe_fixture)


def test_sentinel_content_read_is_capped_to_expected_length_plus_one(probe_fixture, monkeypatch):
    sentinel = probe_fixture["root"] / ".mewcode-synthetic-fixture"
    original_open = Path.open
    read_sizes: list[int] = []

    class ReadSpy:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return self.stream.__exit__(*exc_info)

        def read(self, size=-1):
            read_sizes.append(size)
            return self.stream.read(size)

    def record_sentinel_open(path: Path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        return ReadSpy(stream) if path == sentinel else stream

    monkeypatch.setattr(Path, "open", record_sentinel_open)

    _build(probe_fixture)

    assert read_sizes == [len(SENTINEL) + 1]


def test_rejects_non_exe_or_relative_executable(probe_fixture):
    paths = probe_fixture
    non_exe = paths["cwd"] / "probe.txt"
    non_exe.write_text("not an executable", encoding="utf-8")
    with pytest.raises(ValueError, match=".exe suffix"):
        _build(paths, executable=non_exe)

    with pytest.raises(ValueError, match="must be absolute"):
        _build(paths, executable=Path("cmd.exe"))


def test_requires_sentinel_with_exact_bytes(probe_fixture):
    sentinel = probe_fixture["root"] / ".mewcode-synthetic-fixture"
    sentinel.write_bytes(b"mewcode-synthetic-only-v1")

    with pytest.raises(ValueError, match="sentinel content"):
        _build(probe_fixture)


def test_requires_named_dedicated_fixture_root(probe_fixture, tmp_path):
    unnamed = tmp_path / "ordinary-directory"
    unnamed.mkdir()
    (unnamed / ".mewcode-synthetic-fixture").write_bytes(SENTINEL)

    with pytest.raises(ValueError, match="name must start"):
        _build(probe_fixture, fixture_root=unnamed)


def test_rejects_filesystem_root_and_user_home_as_fixture_root(probe_fixture, monkeypatch):
    with pytest.raises(ValueError, match="filesystem root"):
        _build(probe_fixture, fixture_root=Path(probe_fixture["root"].anchor))

    monkeypatch.setattr(Path, "home", lambda: probe_fixture["root"])
    with pytest.raises(ValueError, match="user home"):
        _build(probe_fixture)


def test_rejects_repository_root_as_fixture_root(probe_fixture, monkeypatch):
    # Model the module as if it were located under this otherwise valid fixture.
    fake_module_file = probe_fixture["root"] / "mewcode" / "sandbox" / "appcontainer_policy.py"
    monkeypatch.setattr(policy, "__file__", str(fake_module_file))

    with pytest.raises(ValueError, match="repository root"):
        _build(probe_fixture)


@pytest.mark.parametrize("field", ["read_paths", "write_paths"])
def test_grants_must_be_existing_strict_descendants(probe_fixture, tmp_path, field):
    paths = probe_fixture
    with pytest.raises(ValueError, match="strictly beneath"):
        _build(paths, **{field: [paths["root"]]})

    outside = tmp_path / "outside.txt"
    outside.write_text("outside fixture", encoding="utf-8")
    with pytest.raises(ValueError, match="strictly beneath"):
        _build(paths, **{field: [outside]})

    missing = paths["root"] / "work" / "missing.txt"
    with pytest.raises(ValueError, match="must exist"):
        _build(paths, **{field: [missing]})


def test_cwd_must_be_an_existing_strict_descendant(probe_fixture, tmp_path):
    with pytest.raises(ValueError, match="cwd must exist strictly beneath"):
        _build(probe_fixture, cwd=probe_fixture["root"])

    with pytest.raises(ValueError, match="cwd must exist strictly beneath"):
        _build(probe_fixture, cwd=tmp_path)


def test_runtime_directory_must_exist_and_is_never_created(probe_fixture):
    runtime = probe_fixture["runtime"]
    runtime.rmdir()

    with pytest.raises(ValueError, match="cwd runtime directory"):
        _build(probe_fixture)
    assert not runtime.exists()


@pytest.mark.parametrize("missing_directory", ["profile", "roaming", "local"])
def test_profile_environment_directories_must_already_exist(probe_fixture, missing_directory):
    paths = probe_fixture
    target = paths[missing_directory]
    if missing_directory == "profile":
        paths["local"].rmdir()
        paths["roaming"].rmdir()
        (paths["profile"] / "AppData").rmdir()
        target.rmdir()
        expected_label = "USERPROFILE"
    elif missing_directory == "roaming":
        target.rmdir()
        expected_label = "APPDATA"
    else:
        target.rmdir()
        expected_label = "LOCALAPPDATA"

    with pytest.raises(ValueError, match=expected_label):
        _build(paths)
    assert not target.exists()


def test_profile_environment_paths_reject_reparse_ancestry(probe_fixture, monkeypatch):
    profile = probe_fixture["profile"]
    original_lstat = Path.lstat

    def lstat_with_reparse_marker(path: Path):
        info = original_lstat(path)
        if path == profile:
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_file_attributes=getattr(info, "st_file_attributes", 0) | 0x0400,
            )
        return info

    monkeypatch.setattr(Path, "lstat", lstat_with_reparse_marker)
    with pytest.raises(ValueError, match="USERPROFILE.*symlink or reparse point"):
        _build(probe_fixture)


def test_rejects_reparse_point_in_grant_path_ancestry(probe_fixture, monkeypatch):
    paths = probe_fixture
    alias = paths["cwd"] / "reparse-alias"
    alias.mkdir()
    target = alias / "data.txt"
    target.write_text("synthetic", encoding="utf-8")
    original_lstat = Path.lstat

    def lstat_with_reparse_marker(path: Path):
        info = original_lstat(path)
        if path == alias:
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_file_attributes=getattr(info, "st_file_attributes", 0) | 0x0400,
            )
        return info

    monkeypatch.setattr(Path, "lstat", lstat_with_reparse_marker)
    with pytest.raises(ValueError, match="symlink or reparse point"):
        _build(paths, read_paths=[target])


@pytest.mark.parametrize("timeout_ms", [0, 999, 60_001, True, 20_000.0])
def test_timeout_is_a_bounded_integer(probe_fixture, timeout_ms):
    with pytest.raises(ValueError, match="timeout_ms"):
        _build(probe_fixture, timeout_ms=timeout_ms)


@pytest.mark.parametrize("arguments", [["ok\x00bad"], [1], "--not-a-list"])
def test_arguments_must_be_nul_free_strings(probe_fixture, arguments):
    with pytest.raises(ValueError, match="arguments"):
        _build(probe_fixture, arguments=arguments)


def test_lpac_must_be_boolean(probe_fixture):
    with pytest.raises(ValueError, match="lpac must be a boolean"):
        _build(probe_fixture, lpac=1)


def test_runtime_registry_read_defaults_to_false(probe_fixture):
    request = _build(probe_fixture)

    assert request["lpac"] is True
    assert request["runtime_registry_read"] is False
    assert request["runtime_instrumentation"] is False


def test_runtime_registry_read_can_be_explicitly_enabled_for_lpac(probe_fixture):
    request = _build(probe_fixture, runtime_registry_read=True)

    assert request["runtime_registry_read"] is True


@pytest.mark.parametrize("value", [1, "true", None])
def test_runtime_registry_read_must_be_boolean(probe_fixture, value):
    with pytest.raises(ValueError, match="runtime_registry_read must be a boolean"):
        _build(probe_fixture, runtime_registry_read=value)


def test_regular_appcontainer_rejects_runtime_registry_read(probe_fixture):
    with pytest.raises(ValueError, match="only available for LPAC"):
        _build(probe_fixture, lpac=False, runtime_registry_read=True)


@pytest.mark.parametrize("value", [1, "true", None])
def test_runtime_instrumentation_must_be_boolean(probe_fixture, value):
    with pytest.raises(ValueError, match="runtime_instrumentation must be a boolean"):
        _build(probe_fixture, runtime_instrumentation=value)


def test_runtime_instrumentation_defaults_to_false(probe_fixture):
    request = _build(probe_fixture)

    assert request["runtime_instrumentation"] is False


def test_runtime_instrumentation_requires_lpac_and_registry_read(probe_fixture):
    request = _build(
        probe_fixture,
        lpac=True,
        runtime_registry_read=True,
        runtime_instrumentation=True,
    )
    assert request["runtime_instrumentation"] is True

    with pytest.raises(ValueError, match="requires runtime_registry_read"):
        _build(probe_fixture, lpac=True, runtime_instrumentation=True)

    with pytest.raises(ValueError, match="only available for LPAC"):
        _build(
            probe_fixture,
            lpac=False,
            runtime_registry_read=True,
            runtime_instrumentation=True,
        )


def test_system_root_is_required_and_is_not_taken_from_path(probe_fixture, monkeypatch):
    monkeypatch.delenv("SystemRoot", raising=False)
    monkeypatch.delenv("WINDIR", raising=False)

    with pytest.raises(ValueError, match="Windows system root is unavailable"):
        _build(probe_fixture)


def test_system_root_environment_with_nul_is_rejected(monkeypatch):
    environment = os.environ
    environ_type = type(environment)
    original_get = environ_type.get

    def get_with_nul_system_root(self, key, default=None):
        if self is environment and key == "SystemRoot":
            return "C:\\Windows\x00unexpected"
        return original_get(self, key, default)

    monkeypatch.setattr(environ_type, "get", get_with_nul_system_root)

    with pytest.raises(ValueError, match="without NUL"):
        policy._windows_system_root()


def test_path_on_an_unusual_drive_is_not_a_fixture_descendant():
    assert not policy._is_strictly_beneath(Path("D:/outside/file.txt"), Path("C:/fixture"))
