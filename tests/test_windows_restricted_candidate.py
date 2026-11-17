from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace

import pytest

from scripts import probe_windows_restricted_candidate as candidate
from mewcode.sandbox import appcontainer_policy


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def synthetic_root():
    scratch = REPO_ROOT / ".tmp"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="restricted-candidate-test-", dir=scratch) as directory:
        yield Path(directory)


def test_git_script_invokes_fixed_git_only_inside_synthetic_repo(synthetic_root: Path) -> None:
    install = synthetic_root / "fake-git"
    (install / "cmd").mkdir(parents=True)
    (install / "mingw64" / "bin").mkdir(parents=True)
    (install / "cmd" / "git.exe").write_bytes(b"synthetic cmd launcher")
    (install / "mingw64" / "bin" / "git.exe").write_bytes(b"synthetic git core")
    for dll in candidate.GIT_RUNTIME_DLLS:
        (install / "mingw64" / "bin" / dll).write_bytes((dll + " synthetic").encode())
    repo = synthetic_root / "synthetic repository"
    repo.mkdir()
    (repo / ".git" / "refs").mkdir(parents=True)
    (repo / ".git" / "config").write_text("synthetic config", encoding="utf-8")

    copied_git, copied_files = candidate._copy_git_runtime(install / "cmd" / "git.exe", repo)
    directories, files = candidate._enumerate_repository_paths(repo)
    grants = candidate._synthetic_path_manifest(repo, directories + files)
    granted_paths = {item["relative_path"] for item in grants}

    script = candidate.build_git_script(copied_git, repo, "a" * 32)

    assert str(copied_git) in script
    assert str(repo) in script
    assert "$env:COMSPEC /d /c 'echo SYNTHETIC_NUL_DEVICE_CHECK>NUL'" in script
    assert "& $git --version" in script
    assert "& $git -C $repo init --quiet" in script
    assert "& $git -C $repo status --porcelain --untracked-files=no" in script
    assert "_LAUNCH_ERROR_TYPE=" in script
    assert "_LAUNCH_ERROR_WIN32=" in script
    assert "$_.Exception.Message" not in script
    assert "GIT_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa_START" in script
    assert "exit 0" in script
    assert copied_git == repo / "git-runtime" / "cmd" / "git.exe"
    assert len(copied_files) == 2 + len(candidate.GIT_RUNTIME_DLLS)
    assert "git-runtime/cmd/git.exe" in granted_paths
    assert "git-runtime/mingw64/bin/git.exe" in granted_paths
    assert "git-runtime/mingw64/bin/libiconv-2.dll" in granted_paths
    assert ".git/config" in granted_paths
    execute_paths = candidate.copied_git_dll_execute_paths(repo)
    assert execute_paths == [repo / "git-runtime" / "mingw64" / "bin" / name
                             for name in candidate.GIT_RUNTIME_DLLS]
    assert all(path.is_file() and path in execute_paths for path in execute_paths)


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell parser is required")
def test_git_script_is_valid_powershell_without_executing_it(synthetic_root: Path) -> None:
    git_exe = synthetic_root / "git.exe"
    git_exe.write_bytes(b"synthetic executable placeholder")
    repo = synthetic_root / "repo"
    repo.mkdir()
    script = candidate.build_git_script(git_exe, repo, "d" * 32)
    encoded = candidate.powershell_arguments(script)[-1]
    parser_command = (
        f"$source=[Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('{encoded}')); "
        "$tokens=$null; $parseErrors=$null; "
        "[System.Management.Automation.Language.Parser]::ParseInput($source,[ref]$tokens,[ref]$parseErrors) "
        "| Out-Null; if ($parseErrors.Count) { $parseErrors | ForEach-Object ErrorId; exit 1 }"
    )
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run(
        [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", parser_command],
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_git_observation_preserves_raw_markers_without_verdict() -> None:
    nonce = "b" * 32
    prefix = "GIT_" + nonce + "_"
    result = candidate.git_observation({
        "exit_code": 0,
        "appcontainer_verified": True,
        "lpac_verified": False,
        "timed_out": False,
        "cancelled": False,
        "error": "",
        "stdout": "\n".join((prefix + "START", prefix + "NUL_EXIT=5",
                              prefix + "VERSION_EXIT=0",
                              prefix + "INIT_EXIT=1", prefix + "STATUS_EXIT=128", prefix + "END")),
    }, nonce)

    assert result["marker_counts"]["START"] == 1
    assert result["marker_counts"]["END"] == 1
    assert result["marker_counts"]["NUL_EXIT_VALUES"] == ["5"]
    assert result["marker_counts"]["VERSION_EXIT_VALUES"] == ["0"]
    assert result["marker_counts"]["INIT_EXIT_VALUES"] == ["1"]
    assert result["marker_counts"]["STATUS_EXIT_VALUES"] == ["128"]
    assert result["launch_observations"]["NUL"]["exit_codes"] == ["5"]
    assert "passed" not in result and "production_ready" not in result


def test_git_observation_counts_duplicate_or_missing_markers() -> None:
    nonce = "c" * 32
    prefix = "GIT_" + nonce + "_"
    result = candidate.git_observation({"stdout": f"{prefix}START\n{prefix}START\n{prefix}VERSION_EXIT=0"}, nonce)

    assert result["marker_counts"]["START"] == 2
    assert result["marker_counts"]["END"] == 0
    assert result["marker_counts"]["VERSION_EXIT_MARKERS"] == 1
    assert result["marker_counts"]["NUL_EXIT_MARKERS"] == 0
    assert result["marker_counts"]["INIT_EXIT_MARKERS"] == 0


def test_copied_git_host_controls_use_only_synthetic_repo(synthetic_root: Path, monkeypatch) -> None:
    runtime = synthetic_root / "repo" / "git-runtime"
    repo = synthetic_root / "repo"
    copied_git = runtime / "mingw64" / "bin" / "git.exe"
    profile = synthetic_root / "profile"
    temp = repo / "temp"
    global_config = profile / "synthetic-gitconfig"
    for path in (copied_git.parent, repo, profile, temp):
        path.mkdir(parents=True, exist_ok=True)
    global_config.write_text("", encoding="utf-8")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"synthetic", stderr=b"")

    monkeypatch.setattr(candidate.subprocess, "run", fake_run)
    facts = candidate._host_copied_git_controls(copied_git, runtime, repo, profile, temp, global_config)

    assert facts["version"]["exit_code"] == 0
    assert facts["status"]["exit_code"] == 0
    assert facts["executable_role"] == "copied mingw64/bin/git.exe"
    assert len(calls) == 2
    assert all(call[0][0] == str(copied_git) for call in calls)
    assert all(call[1]["cwd"] == repo for call in calls)
    assert all(call[1]["env"]["PATH"].split(candidate.os.pathsep)[0] == str(runtime / "mingw64" / "bin")
               for call in calls)
    assert calls[1][0][2] == str(repo)


@pytest.mark.parametrize(
    ("name", "expected_arguments"),
    [
        ("version", ["--version"]),
        ("status", ["-C", "<repo>", "status", "--porcelain", "--untracked-files=no"]),
    ],
)
def test_direct_git_requests_use_core_executable_and_exact_arguments(
    synthetic_root: Path, monkeypatch, name: str, expected_arguments: list[str]
) -> None:
    fixture = synthetic_root / "fixture"
    repo = fixture / "workspace" / "repo"
    temp = repo / "temp"
    core_git = repo / "git-runtime" / "mingw64" / "bin" / "git.exe"
    for path in (repo, temp, core_git.parent, fixture / "profile" / "AppData" / "Local",
                 fixture / "profile" / "AppData" / "Roaming"):
        path.mkdir(parents=True, exist_ok=True)
    read_paths = [repo, temp, core_git]
    write_paths = [repo, temp]
    execute_paths = candidate.copied_git_dll_execute_paths(repo)
    observed = {}

    def fake_build_probe_request(**kwargs):
        observed.update(kwargs)
        return kwargs

    monkeypatch.setattr(appcontainer_policy, "build_probe_request", fake_build_probe_request)
    arguments = candidate.direct_git_case_arguments(name, repo)
    request = candidate.build_direct_git_request(
        fixture=fixture,
        executable=core_git,
        arguments=arguments,
        repository=repo,
        read_paths=read_paths,
        write_paths=write_paths,
        execute_paths=execute_paths,
    )

    expected = ["--version"] if name == "version" else ["-C", str(repo), *expected_arguments[2:]]
    assert observed["executable"] == core_git
    assert request["arguments"] == expected
    assert observed["cwd"] == repo
    assert observed["read_paths"] is read_paths
    assert observed["write_paths"] is write_paths
    assert observed["execute_paths"] is execute_paths
    assert request["execute_paths"] == execute_paths
    assert execute_paths == [core_git.parent / dll for dll in candidate.GIT_RUNTIME_DLLS]
    assert all(path.suffix.casefold() == ".dll" for path in request["execute_paths"])
    assert observed["lpac"] is False


def test_cmd_nul_request_uses_system_cmd_and_same_synthetic_grants(
    synthetic_root: Path, monkeypatch
) -> None:
    fixture = synthetic_root / "fixture"
    repo = fixture / "workspace" / "repo"
    temp = repo / "temp"
    cmd_exe = Path("C:/Windows/System32/cmd.exe")
    for path in (repo, temp, fixture / "profile" / "AppData" / "Local",
                 fixture / "profile" / "AppData" / "Roaming"):
        path.mkdir(parents=True, exist_ok=True)
    read_paths = [repo, temp, repo / "synthetic-input.txt"]
    write_paths = [repo, temp]
    observed = {}

    def fake_build_probe_request(**kwargs):
        observed.update(kwargs)
        return kwargs

    monkeypatch.setattr(appcontainer_policy, "build_probe_request", fake_build_probe_request)
    arguments = candidate.nul_write_command_arguments()
    request = candidate.build_direct_git_request(
        fixture=fixture,
        executable=cmd_exe,
        arguments=arguments,
        repository=repo,
        read_paths=read_paths,
        write_paths=write_paths,
    )

    assert arguments == ["/d", "/c", "rem > NUL"]
    assert "echo" not in arguments[2].casefold()
    assert observed["executable"] == cmd_exe
    assert request["arguments"] == ["/d", "/c", "rem > NUL"]
    assert observed["cwd"] == repo
    assert observed["read_paths"] is read_paths
    assert observed["write_paths"] is write_paths
    assert observed["lpac"] is False


def test_direct_git_cases_are_archived_independently_and_failures_are_distinct(
    synthetic_root: Path,
) -> None:
    repo = synthetic_root / "synthetic repo"
    version = candidate.direct_git_case_facts(
        case_name="version",
        host_control_exit_code=0,
        helper_exit_code=0,
        receipt={"exit_code": 128, "appcontainer_verified": True, "stderr": f"NUL error in {repo}\\file"},
        redacted_paths=(repo,),
    )
    status_start_failure = candidate.direct_git_case_facts(
        case_name="status",
        host_control_exit_code=0,
        helper_exit_code=None,
        receipt=None,
        helper_start_error_type="FileNotFoundError",
    )
    status_launch_failure = candidate.direct_git_case_facts(
        case_name="status",
        host_control_exit_code=0,
        helper_exit_code=0,
        receipt={"error": "must not expose", "exit_code": None},
    )

    archived = candidate.archive_direct_git_cases([version, status_start_failure])

    assert list(archived) == ["version", "status"]
    assert archived["version"]["phase"] == "child_exit_observed"
    assert archived["version"]["child_exit_code"] == 128
    assert archived["version"]["nul_stderr_lines"] == ["NUL error in <synthetic-path>\\file"]
    assert archived["status"]["phase"] == "helper_start_failure"
    assert status_launch_failure["phase"] == "receipt_error_without_child_exit"
    assert "must not expose" not in str(status_launch_failure)


def test_cmd_nul_facts_separate_host_helper_and_child_failures(synthetic_root: Path) -> None:
    repo = synthetic_root / "synthetic repo"
    successful_host = {"exit_code": 0, "nul_stderr_lines": [], "stdout_bytes": 0, "stderr_bytes": 0}
    child_failure = candidate.cmd_nul_case_facts(
        successful_host,
        {"helper_exit_code": 0, "helper_stderr_bytes": 18},
        {
            "exit_code": 5,
            "appcontainer_verified": True,
            "lpac_verified": False,
            "stderr": f"NUL write denied at {repo}\\NUL",
            "job_cleanup_ok": True,
            "profile_cleanup_ok": True,
            "acl_cleanup_ok": True,
        },
        (repo,),
    )
    helper_start_failure = candidate.cmd_nul_case_facts(
        successful_host,
        {"helper_start_error_type": "PermissionError"},
        None,
        (repo,),
    )

    assert child_failure["host_control_exit_code"] == 0
    assert child_failure["helper_exit_code"] == 0
    assert child_failure["child_exit_code"] == 5
    assert child_failure["phase"] == "child_exit_observed"
    assert child_failure["appcontainer_identity_verified"] is True
    assert child_failure["nul_stderr_lines"] == ["NUL write denied at <synthetic-path>\\NUL"]
    assert child_failure["cleanup_receipt"] == {
        "job_cleanup_ok": True, "profile_cleanup_ok": True, "acl_cleanup_ok": True,
    }
    assert helper_start_failure["phase"] == "helper_start_failure"
    assert helper_start_failure["child_exit_code"] is None


@pytest.mark.parametrize("relative", [Path("git.exe"), Path("nested") / "git.exe"])
def test_checked_executable_rejects_relative_paths(relative: Path) -> None:
    with pytest.raises(candidate.CandidateUnavailable, match="absolute"):
        candidate.checked_executable(relative, "Git executable")


def test_git_script_rejects_unstructured_nonce(synthetic_root: Path) -> None:
    tmp_path = synthetic_root
    git_exe = tmp_path / "git.exe"
    git_exe.write_bytes(b"placeholder")
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(ValueError, match="nonce"):
        candidate.build_git_script(git_exe, repo, "not-a-nonce")
