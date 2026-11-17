"""Observe a regular Windows AppContainer candidate using synthetic data only.

This diagnostic is independent of AUTO routing. It always requests
``lpac=False`` and never invokes a command on the host as a fallback. The
existing synthetic AppContainer probe supplies the PowerShell, EXE, path,
loopback, and cancellation observations; this module adds a Git child-process
probe and reports raw facts without making a production-readiness decision.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCHEMA = "mewcode-regular-appcontainer-candidate-v1"
PROBE_SOURCE = ROOT / "native" / "windows" / "MewCode.AppContainerProbe.cs"
GIT_RUNTIME_DLLS = (
    "libiconv-2.dll",
    "libintl-8.dll",
    "libpcre2-8-0.dll",
    "zlib1.dll",
)


class CandidateUnavailable(RuntimeError):
    """The synthetic candidate cannot be run with the current local inputs."""


def _has_reparse_ancestor(path: Path) -> bool:
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except OSError:
            return True
        if candidate.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x0400:
            return True
    return False


def checked_executable(raw: str | Path, label: str) -> Path:
    """Resolve an explicit executable without PATH search or reparse paths."""
    path = Path(raw)
    if not path.is_absolute():
        raise CandidateUnavailable(f"{label} must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CandidateUnavailable(f"{label} is unavailable") from exc
    if _has_reparse_ancestor(path) or not resolved.is_file() or resolved.suffix.casefold() != ".exe":
        raise CandidateUnavailable(f"{label} must be a regular, non-link .exe file")
    return resolved


def _powershell_quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _launch_failure_catch(prefix: str, name: str) -> str:
    """Emit only the exception type and an actual Win32Exception number."""
    return (
        "catch { $failure=$_.Exception; $type='unavailable'; $win32='none'; "
        "try { $type=$failure.GetType().FullName } catch { $type='metadata-unavailable' }; "
        "try { while ($failure) { "
        "if ($failure -is [System.ComponentModel.Win32Exception]) { "
        "$win32=[string]$failure.NativeErrorCode; break }; $failure=$failure.InnerException "
        "} } catch { }; "
        f"Write-Output '{prefix}{name}_LAUNCH_ERROR'; "
        f"Write-Output ('{prefix}{name}_LAUNCH_ERROR_TYPE=' + $type); "
        f"Write-Output ('{prefix}{name}_LAUNCH_ERROR_WIN32=' + $win32) "
        "}"
    )


def powershell_arguments(script: str) -> list[str]:
    import base64

    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return ["-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def build_git_script(
    git_exe: Path,
    repository: Path,
    nonce: str,
    global_config: Path | None = None,
) -> str:
    """Create a fixed Git smoke script confined to the synthetic repository."""
    if not re.fullmatch(r"[0-9a-f]{32}", nonce):
        raise ValueError("nonce must be 32 lowercase hexadecimal characters")
    git_path = checked_executable(git_exe, "Git executable")
    if not repository.is_absolute():
        raise CandidateUnavailable("synthetic Git repository must be absolute")
    config = global_config or repository.parent / "synthetic-gitconfig"
    git = _powershell_quote(git_path)
    repo = _powershell_quote(repository)
    prefix = "GIT_" + nonce + "_"
    return (
        "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
        "$env:HOME=$env:USERPROFILE; $env:GIT_CONFIG_NOSYSTEM='1'; "
        "$env:GIT_TERMINAL_PROMPT='0'; "
        f"$env:GIT_CONFIG_GLOBAL={_powershell_quote(config)}; "
        f"$git={git}; $repo={repo}; "
        f"Write-Output '{prefix}START'; "
        "try { & $env:COMSPEC /d /c 'echo SYNTHETIC_NUL_DEVICE_CHECK>NUL'; "
        "$nulCode=$LASTEXITCODE; "
        f"Write-Output ('{prefix}NUL_EXIT=' + $nulCode) }} " + _launch_failure_catch(prefix, "NUL") + "; "
        "try { & $git --version; $versionCode=$LASTEXITCODE; "
        f"Write-Output ('{prefix}VERSION_EXIT=' + $versionCode) }} " + _launch_failure_catch(prefix, "VERSION") + "; "
        "try { & $git -C $repo init --quiet; $initCode=$LASTEXITCODE; "
        f"Write-Output ('{prefix}INIT_EXIT=' + $initCode) }} " + _launch_failure_catch(prefix, "INIT") + "; "
        "try { & $git -C $repo status --porcelain --untracked-files=no; $statusCode=$LASTEXITCODE; "
        f"Write-Output ('{prefix}STATUS_EXIT=' + $statusCode) }} " + _launch_failure_catch(prefix, "STATUS") + "; "
        f"Write-Output '{prefix}END'; exit 0"
    )


def git_observation(receipt: dict, nonce: str) -> dict:
    """Return literal process evidence; do not collapse it into a pass verdict."""
    prefix = "GIT_" + nonce + "_"
    output = receipt.get("stdout", "") if isinstance(receipt, dict) else ""
    lines = output.splitlines() if isinstance(output, str) else []
    selected = [line for line in lines if line.startswith(prefix)]
    counts = {name: sum(line == prefix + name for line in selected)
              for name in ("START", "END", "NUL_LAUNCH_ERROR", "VERSION_LAUNCH_ERROR",
                           "INIT_LAUNCH_ERROR", "STATUS_LAUNCH_ERROR")}
    for name in ("NUL", "VERSION", "INIT", "STATUS"):
        matches = [line for line in selected if line.startswith(prefix + name + "_EXIT=")]
        counts[name + "_EXIT_MARKERS"] = len(matches)
        counts[name + "_EXIT_VALUES"] = [line.rsplit("=", 1)[-1] for line in matches]
        for suffix in ("LAUNCH_ERROR_TYPE", "LAUNCH_ERROR_WIN32"):
            error_markers = [line for line in selected if line.startswith(prefix + name + "_" + suffix + "=")]
            counts[name + "_" + suffix + "_VALUES"] = [line.split("=", 1)[1] for line in error_markers]
    launch_names = ("NUL", "VERSION", "INIT", "STATUS")
    stderr = receipt.get("stderr", "") if isinstance(receipt, dict) else ""
    exception_types = re.findall(r"(?:[A-Za-z_][A-Za-z0-9_]*\.)+[A-Za-z_][A-Za-z0-9_]*Exception", stderr)
    win32_values = re.findall(r"(?i)win32(?:exception| error)?[^0-9-]*(-?\d+)", stderr)
    return {
        "receipt_exit_code": receipt.get("exit_code") if isinstance(receipt, dict) else None,
        "appcontainer_verified": receipt.get("appcontainer_verified") if isinstance(receipt, dict) else None,
        "lpac_verified": receipt.get("lpac_verified") if isinstance(receipt, dict) else None,
        "timed_out": receipt.get("timed_out") if isinstance(receipt, dict) else None,
        "cancelled": receipt.get("cancelled") if isinstance(receipt, dict) else None,
        "helper_error_present": bool(receipt.get("error")) if isinstance(receipt, dict) else True,
        "stderr_exception_types": list(dict.fromkeys(exception_types)),
        "stderr_win32_values": list(dict.fromkeys(win32_values)),
        "markers": selected,
        "marker_counts": counts,
        "launch_observations": {
            name: {
                "error_types": counts[name + "_LAUNCH_ERROR_TYPE_VALUES"],
                "win32_codes": counts[name + "_LAUNCH_ERROR_WIN32_VALUES"],
                "exit_codes": counts[name + "_EXIT_VALUES"],
            }
            for name in launch_names
        },
        "stderr": _nul_diagnostic_lines(receipt.get("stderr", "")) if isinstance(receipt, dict) else [],
    }


def _nul_diagnostic_lines(value: str, redacted_paths: tuple[Path | str, ...] = ()) -> list[str]:
    """Retain NUL-specific stderr evidence while redacting synthetic paths."""
    lines = []
    for line in value.splitlines():
        if "nul" not in line.casefold():
            continue
        sanitized = line
        for path in sorted((str(item) for item in redacted_paths), key=len, reverse=True):
            sanitized = re.sub(re.escape(path), "<synthetic-path>", sanitized, flags=re.IGNORECASE)
        sanitized = re.sub(r"(?i)[a-z]:\\[^\s\"']+", "<path>", sanitized)
        lines.append(sanitized[:500])
    return lines


def direct_git_case_arguments(case_name: str, repository: Path) -> list[str]:
    """Arguments for one direct Git candidate invocation in the synthetic repo."""
    if case_name == "version":
        return ["--version"]
    if case_name == "status":
        return ["-C", str(repository), "status", "--porcelain", "--untracked-files=no"]
    raise ValueError("unknown direct Git case")


def nul_write_command_arguments() -> list[str]:
    """Open NUL for a zero-byte write using cmd.exe built-ins only."""
    return ["/d", "/c", "rem > NUL"]


def build_direct_git_request(
    *,
    fixture: Path,
    executable: Path,
    arguments: list[str],
    repository: Path,
    read_paths: list[Path],
    write_paths: list[Path],
    execute_paths: list[Path] | None = None,
) -> dict:
    """Build an exact-grant, non-LPAC request for a fixture or trusted child."""
    from mewcode.sandbox.appcontainer_policy import build_probe_request

    return build_probe_request(
        fixture_root=fixture,
        executable=executable,
        arguments=arguments,
        cwd=repository,
        read_paths=read_paths,
        write_paths=write_paths,
        execute_paths=execute_paths,
        timeout_ms=20_000,
        lpac=False,
    )


def copied_git_dll_execute_paths(repository: Path) -> list[Path]:
    """Return only the four copied Git runtime DLL paths for execute grants."""
    return [repository / "git-runtime" / "mingw64" / "bin" / name for name in GIT_RUNTIME_DLLS]


def direct_git_case_facts(
    *,
    case_name: str,
    host_control_exit_code: int | None,
    helper_exit_code: int | None,
    receipt: dict | None,
    request_write_error_type: str | None = None,
    helper_start_error_type: str | None = None,
    outer_timeout: bool = False,
    redacted_paths: tuple[Path | str, ...] = (),
) -> dict:
    """Separate helper startup, child launch, and child exit observations."""
    if request_write_error_type:
        phase = "request_write_failure"
    elif helper_start_error_type:
        phase = "helper_start_failure"
    elif outer_timeout:
        phase = "helper_timeout"
    elif not isinstance(receipt, dict):
        phase = "receipt_unavailable"
    elif receipt.get("exit_code") is not None:
        phase = "child_exit_observed"
    elif receipt.get("error"):
        phase = "receipt_error_without_child_exit"
    else:
        phase = "child_exit_unobserved"
    valid_receipt = receipt if isinstance(receipt, dict) else {}
    cleanup_keys = ("job_cleanup_ok", "profile_cleanup_ok", "acl_cleanup_ok")
    cleanup = {
        key: valid_receipt.get(key) if isinstance(valid_receipt.get(key), bool) else None
        for key in cleanup_keys
    }
    return {
        "name": case_name,
        "host_control_exit_code": host_control_exit_code,
        "helper_exit_code": helper_exit_code,
        "request_write_error_type": request_write_error_type,
        "helper_start_error_type": helper_start_error_type,
        "outer_timeout": outer_timeout,
        "phase": phase,
        "appcontainer_identity_verified": valid_receipt.get("appcontainer_verified"),
        "lpac_verified": valid_receipt.get("lpac_verified"),
        "child_exit_code": valid_receipt.get("exit_code"),
        "candidate_error_present": bool(valid_receipt.get("error")),
        "nul_stderr_lines": _nul_diagnostic_lines(str(valid_receipt.get("stderr", "")), redacted_paths),
        "cleanup_receipt": cleanup,
    }


def archive_direct_git_cases(cases: list[dict]) -> dict[str, dict]:
    """Keep direct Git runs as separate named observations."""
    archived = {}
    for case in cases:
        name = case.get("name") if isinstance(case, dict) else None
        if name in {"version", "status"}:
            archived[name] = case
    return archived


async def _invoke_probe_request(helper: Path, request: dict, request_path: Path) -> dict:
    """Run the native helper and retain only safe, structured diagnostics."""
    try:
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        return {"request_write_error_type": type(exc).__name__}
    try:
        process = await asyncio.create_subprocess_exec(
            str(helper), str(request_path), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        return {"helper_start_error_type": type(exc).__name__}
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 35)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return {"helper_exit_code": process.returncode, "helper_stderr_bytes": 0, "outer_timeout": True}
    try:
        receipt = json.loads(stdout.decode("utf-8-sig"))
        if not isinstance(receipt, dict):
            receipt = None
    except (UnicodeError, json.JSONDecodeError):
        receipt = None
    return {
        "helper_exit_code": process.returncode,
        "helper_stderr_bytes": len(stderr),
        "receipt": receipt,
    }


def _run_host_nul_write_control(
    cmd_exe: Path,
    arguments: list[str],
    repository: Path,
    environment: dict[str, str],
    redacted_paths: tuple[Path | str, ...],
) -> dict:
    """Run the exact cmd/NUL command on the host, with synthetic cwd/profile only."""
    try:
        result = subprocess.run(
            [str(cmd_exe), *arguments],
            cwd=repository,
            env=environment,
            capture_output=True,
            timeout=15,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        stderr = getattr(exc, "stderr", b"") or b""
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        return {
            "exit_code": None,
            "exception_type": type(exc).__name__,
            "nul_stderr_lines": _nul_diagnostic_lines(str(stderr), redacted_paths),
        }
    return {
        "exit_code": result.returncode,
        "stdout_bytes": len(result.stdout),
        "stderr_bytes": len(result.stderr),
        "nul_stderr_lines": _nul_diagnostic_lines(result.stderr.decode(errors="replace"), redacted_paths),
    }


def cmd_nul_case_facts(
    host_control: dict,
    invocation: dict,
    receipt: dict | None,
    redacted_paths: tuple[Path | str, ...],
) -> dict:
    """Combine host and AppContainer facts without surfacing raw helper errors."""
    facts = direct_git_case_facts(
        case_name="cmd_nul_write",
        host_control_exit_code=host_control.get("exit_code"),
        helper_exit_code=invocation.get("helper_exit_code"),
        receipt=receipt,
        request_write_error_type=invocation.get("request_write_error_type"),
        helper_start_error_type=invocation.get("helper_start_error_type"),
        outer_timeout=invocation.get("outer_timeout", False),
        redacted_paths=redacted_paths,
    )
    facts.update({
        "host_control_exception_type": host_control.get("exception_type"),
        "host_nul_stderr_lines": host_control.get("nul_stderr_lines", []),
        "host_stdout_bytes": host_control.get("stdout_bytes"),
        "host_stderr_bytes": host_control.get("stderr_bytes"),
        "helper_stderr_bytes": invocation.get("helper_stderr_bytes"),
    })
    return facts


def _copy_git_runtime(git_exe: Path, repository: Path) -> tuple[Path, list[dict]]:
    """Copy the two Git launchers and the current Git for Windows DLL closure."""
    cmd_git = checked_executable(git_exe, "Git executable")
    install_root = cmd_git.parent.parent.resolve(strict=True)
    if cmd_git.parent.name.casefold() != "cmd":
        raise CandidateUnavailable("Git executable must be the Git for Windows cmd/git.exe")
    mingw_git = checked_executable(install_root / "mingw64" / "bin" / "git.exe", "Git core executable")
    source_files = [
        (cmd_git, Path("cmd") / "git.exe", "git_cmd_exe"),
        (mingw_git, Path("mingw64") / "bin" / "git.exe", "git_core_exe"),
    ]
    for name in GIT_RUNTIME_DLLS:
        source = install_root / "mingw64" / "bin" / name
        try:
            source = source.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise CandidateUnavailable(f"Required Git runtime DLL is unavailable: {name}") from exc
        if not source.is_file() or not source.is_relative_to(install_root):
            raise CandidateUnavailable(f"Required Git runtime DLL is unavailable: {name}")
        source_files.append((source, Path("mingw64") / "bin" / name, "git_runtime_dll"))

    runtime_root = repository / "git-runtime"
    manifest = []
    for source, relative_target, role in source_files:
        target = runtime_root / relative_target
        if not target.resolve(strict=False).is_relative_to(repository.resolve(strict=True)):
            raise CandidateUnavailable("Git runtime copy target escaped the synthetic repository")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        manifest.append({
            "source": str(source),
            "path": str(target),
            "relative_path": target.relative_to(repository).as_posix(),
            "role": role,
            "size": target.stat().st_size,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        })
    return runtime_root / "cmd" / "git.exe", manifest


def _synthetic_path_manifest(scope_root: Path, paths: list[Path]) -> list[dict]:
    entries = []
    for path in sorted(set(paths), key=lambda item: str(item).casefold()):
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(scope_root.resolve(strict=True)):
            raise CandidateUnavailable("A Git probe grant escaped its synthetic scope")
        is_directory = resolved.is_dir()
        item = {"path": str(resolved), "relative_path": resolved.relative_to(scope_root).as_posix(),
                "kind": "directory" if is_directory else "file"}
        if not is_directory:
            item["sha256"] = hashlib.sha256(resolved.read_bytes()).hexdigest()
        entries.append(item)
    return entries


def _enumerate_repository_paths(repository: Path) -> tuple[list[Path], list[Path]]:
    directories = [repository]
    files = []
    for path in repository.rglob("*"):
        if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x0400:
            raise CandidateUnavailable("Synthetic Git fixture unexpectedly contains a reparse point")
        (directories if path.is_dir() else files).append(path)
    return directories, files


def _host_git_environment(git_root: Path, profile: Path, runtime_temp: Path, global_config: Path) -> dict[str, str]:
    system_root = Path(os.environ.get("SystemRoot") or os.environ.get("WINDIR", ""))
    system32 = system_root / "System32"
    return {
        "SystemRoot": str(system_root),
        "WINDIR": str(system_root),
        "SystemDrive": system_root.drive,
        "COMSPEC": str(system32 / "cmd.exe"),
        "PATH": os.pathsep.join((str(git_root / "mingw64" / "bin"), str(git_root / "cmd"), str(system32))),
        "TEMP": str(runtime_temp),
        "TMP": str(runtime_temp),
        "USERPROFILE": str(profile),
        "HOME": str(profile),
        "APPDATA": str(profile / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(profile / "AppData" / "Local"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(global_config),
        "GIT_TERMINAL_PROMPT": "0",
    }


def _host_copied_git_controls(
    git_exe: Path,
    runtime_root: Path,
    repository: Path,
    profile: Path,
    runtime_temp: Path,
    global_config: Path,
) -> dict:
    """Exercise the copied Git runtime on the host, only in the synthetic repo."""
    env = _host_git_environment(runtime_root, profile, runtime_temp, global_config)
    controls = {}
    commands = {
        "version": [str(git_exe), "--version"],
        "status": [str(git_exe), "-C", str(repository), "status", "--porcelain", "--untracked-files=no"],
    }
    controls["executable_role"] = "copied mingw64/bin/git.exe"
    for name, command in commands.items():
        try:
            result = subprocess.run(
                command,
                cwd=repository,
                env=env,
                capture_output=True,
                timeout=20,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            controls[name] = {"exception_type": type(exc).__name__}
            continue
        controls[name] = {
            "exit_code": result.returncode,
            "stdout_bytes": len(result.stdout),
            "stderr_bytes": len(result.stderr),
        }
    return controls


def compile_probe_helper(output_directory: Path) -> tuple[Path, dict]:
    """Compile the existing probe helper into a fresh scratch directory."""
    if os.name != "nt":
        raise CandidateUnavailable("regular AppContainer probe requires Windows")
    system_root = Path(os.environ.get("SystemRoot") or os.environ.get("WINDIR", ""))
    compiler = system_root / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    compiler = checked_executable(compiler, ".NET Framework C# compiler")
    if not PROBE_SOURCE.is_file() or PROBE_SOURCE.is_symlink():
        raise CandidateUnavailable("regular AppContainer probe source is unavailable")
    output_directory.mkdir(parents=True, exist_ok=False)
    target = output_directory / "MewCode.AppContainerProbe.exe"
    command = [
        str(compiler), "/nologo", "/platform:x64", "/r:System.Web.Extensions.dll",
        "/main:MewCode.Native.Windows.MewCodeAppContainerProbe", "/out:" + str(target),
        str(PROBE_SOURCE),
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=60,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CandidateUnavailable("temporary probe-helper compilation could not complete") from exc
    if result.returncode != 0 or not target.is_file():
        raise CandidateUnavailable("temporary probe-helper compilation failed")
    return target, {
        "compiler_path": str(compiler),
        "compiler_exit_code": result.returncode,
        "source_sha256": hashlib.sha256(PROBE_SOURCE.read_bytes()).hexdigest(),
        "helper_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    }


async def _run_git_case(helper: Path, git_exe: Path, base_report: dict) -> dict:
    from mewcode.sandbox.appcontainer_policy import build_probe_request

    fixture = Path(base_report["fixture"]).resolve(strict=True)
    workspace = fixture / "workspace"
    profile = fixture / "profile"
    repo = workspace / "git-synthetic-repository"
    runtime_temp = repo / "temp"
    repo.mkdir()
    runtime_temp.mkdir()
    (repo / "synthetic-input.txt").write_text("SYNTHETIC_GIT_INPUT\n", encoding="utf-8")
    global_config = profile / "synthetic-gitconfig"
    global_config.write_text("", encoding="utf-8")
    git_exe = checked_executable(git_exe, "Git executable")
    install_root = git_exe.parent.parent.resolve(strict=True)
    host_init = subprocess.run(
        [str(git_exe), "-C", str(repo), "init", "--quiet"],
        cwd=repo,
        env=_host_git_environment(install_root, profile, runtime_temp, global_config),
        capture_output=True,
        timeout=30,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    host_control = {
        "operation": "host git init in synthetic repository",
        "exit_code": host_init.returncode,
        "git_directory_created": (repo / ".git").is_dir(),
        "stdout_bytes": len(host_init.stdout),
        "stderr_bytes": len(host_init.stderr),
    }
    if host_init.returncode != 0 or not host_control["git_directory_created"]:
        return {"host_positive_control": host_control, "candidate_started": False,
                "reason": "host_git_init_control_unavailable", "lpac_requested": False}

    copied_git, copied_files = _copy_git_runtime(git_exe, repo)
    copied_core = repo / "git-runtime" / "mingw64" / "bin" / "git.exe"
    if not copied_git.is_file() or not copied_core.is_file():
        raise CandidateUnavailable("Copied Git launchers are missing from the synthetic repository")
    copied_runtime_root = repo / "git-runtime"
    copied_git_control = _host_copied_git_controls(
        copied_core, copied_runtime_root, repo, profile, runtime_temp, global_config,
    )
    copied_git_control_ok = all(
        isinstance(copied_git_control.get(name), dict)
        and copied_git_control[name].get("exit_code") == 0
        for name in ("version", "status")
    )
    if not copied_git_control_ok:
        return {
            "host_positive_control": host_control,
            "host_copied_git_controls": copied_git_control,
            "candidate_started": False,
            "reason": "copied_git_runtime_host_control_unavailable",
            "lpac_requested": False,
            "copied_git_files": copied_files,
        }
    nonce = uuid.uuid4().hex
    command = build_git_script(copied_git, repo, nonce, global_config)
    system_root = Path(os.environ.get("SystemRoot") or os.environ.get("WINDIR", ""))
    powershell = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    repo_directories, repo_files = _enumerate_repository_paths(repo)
    profile_paths = [profile, profile / "AppData", profile / "AppData" / "Local",
                     profile / "AppData" / "Roaming", global_config]
    read_paths = sorted(set(repo_directories + repo_files + profile_paths), key=lambda item: str(item).casefold())
    git_metadata = [repo / ".git", *(repo / ".git").rglob("*")]
    write_paths = sorted(set([repo, runtime_temp, *(path for path in git_metadata
                                                    if path.is_dir() or path.is_file())]),
                         key=lambda item: str(item).casefold())
    git_dll_execute_paths = copied_git_dll_execute_paths(repo)
    cmd_exe = checked_executable(system_root / "System32" / "cmd.exe", "Windows command processor")
    nul_arguments = nul_write_command_arguments()
    nul_request = build_direct_git_request(
        fixture=fixture,
        executable=cmd_exe,
        arguments=nul_arguments,
        repository=repo,
        read_paths=read_paths,
        write_paths=write_paths,
    )
    nul_request_path = fixture / "direct-cmd-nul-write.request.json"
    nul_host_control = _run_host_nul_write_control(
        cmd_exe,
        nul_arguments,
        repo,
        nul_request["environment"],
        (fixture, repo, nul_request_path, cmd_exe),
    )
    nul_invocation = await _invoke_probe_request(helper, nul_request, nul_request_path)
    nul_receipt = nul_invocation.get("receipt")
    nul_case = cmd_nul_case_facts(
        nul_host_control,
        nul_invocation,
        nul_receipt if isinstance(nul_receipt, dict) else None,
        (fixture, repo, nul_request_path, cmd_exe),
    )
    nul_case.update({
        "request_executable": str(cmd_exe),
        "request_arguments": nul_arguments,
        "request_path": str(nul_request_path),
        "cwd": str(repo),
        "lpac_requested": False,
        "read_grants": _synthetic_path_manifest(fixture, read_paths),
        "write_grants": _synthetic_path_manifest(repo, write_paths),
    })
    powershell_request = build_probe_request(
        fixture_root=fixture,
        executable=powershell,
        arguments=powershell_arguments(command),
        cwd=repo,
        read_paths=read_paths,
        write_paths=write_paths,
        timeout_ms=20_000,
        lpac=False,
    )
    powershell_request_path = fixture / "git-case.request.json"
    powershell_invocation = await _invoke_probe_request(helper, powershell_request, powershell_request_path)
    powershell_receipt = powershell_invocation.get("receipt")
    if not isinstance(powershell_receipt, dict):
        powershell_receipt = {}

    direct_case_results = []
    for case_name in ("version", "status"):
        arguments = direct_git_case_arguments(case_name, repo)
        request = build_direct_git_request(
            fixture=fixture,
            executable=copied_core,
            arguments=arguments,
            repository=repo,
            read_paths=read_paths,
            write_paths=write_paths,
            execute_paths=git_dll_execute_paths,
        )
        request_path = fixture / f"direct-git-{case_name}.request.json"
        invocation = await _invoke_probe_request(helper, request, request_path)
        host_case_control = copied_git_control.get(case_name, {})
        facts = direct_git_case_facts(
            case_name=case_name,
            host_control_exit_code=host_case_control.get("exit_code"),
            helper_exit_code=invocation.get("helper_exit_code"),
            receipt=invocation.get("receipt"),
            request_write_error_type=invocation.get("request_write_error_type"),
            helper_start_error_type=invocation.get("helper_start_error_type"),
            outer_timeout=invocation.get("outer_timeout", False),
            redacted_paths=(fixture, repo, copied_git, request_path),
        )
        facts.update({
            "request_executable": str(copied_core),
            "request_arguments": arguments,
            "request_path": str(request_path),
            "helper_stderr_bytes": invocation.get("helper_stderr_bytes"),
            "read_grants": _synthetic_path_manifest(fixture, read_paths),
            "write_grants": _synthetic_path_manifest(repo, write_paths),
            "execute_grants": _synthetic_path_manifest(repo, git_dll_execute_paths),
        })
        direct_case_results.append(facts)
    direct_cases = archive_direct_git_cases(direct_case_results)

    powershell_summary = {
        "helper_exit_code": powershell_invocation.get("helper_exit_code"),
        "helper_start_error_type": powershell_invocation.get("helper_start_error_type"),
        "outer_timeout": powershell_invocation.get("outer_timeout", False),
        "helper_stderr_bytes": powershell_invocation.get("helper_stderr_bytes"),
        "receipt_summary": {
            "exit_code": powershell_receipt.get("exit_code"),
            "appcontainer_verified": powershell_receipt.get("appcontainer_verified"),
            "lpac_verified": powershell_receipt.get("lpac_verified"),
            "timed_out": powershell_receipt.get("timed_out"),
            "cancelled": powershell_receipt.get("cancelled"),
            "helper_error_present": bool(powershell_receipt.get("error")),
        },
        "observation": git_observation(powershell_receipt, nonce),
        "limitation": (
            "A PowerShell START/END marker only establishes wrapper execution. "
            "ConstrainedLanguage may hide exception metadata or prevent native child launch, "
            "so wrapper LAUNCH_ERROR markers are not direct Git execution evidence."
        ),
    }
    return {
        "host_positive_control": host_control,
        "host_copied_git_controls": copied_git_control,
        "git_source_executable_path": str(git_exe),
        "candidate_git_executable_path": str(copied_git),
        "candidate_git_core_executable_path": str(copied_core),
        "repository_path": str(repo),
        "request_path": str(powershell_request_path),
        "nonce": nonce,
        "lpac_requested": False,
        "helper_exit_code": powershell_summary["helper_exit_code"],
        "helper_stderr_bytes": powershell_summary["helper_stderr_bytes"],
        "copied_git_files": copied_files,
        "read_grants": _synthetic_path_manifest(fixture, read_paths),
        "write_grants": _synthetic_path_manifest(repo, write_paths),
        "receipt_summary": powershell_summary["receipt_summary"],
        "observation": powershell_summary["observation"],
        "powershell_wrapper": powershell_summary,
        "direct_cases": direct_cases,
        "nul_write_case": nul_case,
    }


async def run_probe(helper: Path, git_exe: Path, helper_build: dict | None = None) -> dict:
    """Run the regular-AppContainer synthetic suite and add the Git case."""
    from scripts.probe_windows_appcontainer import experiment

    base = await experiment(helper, lpac=False, diagnose_cancellation=True)
    git_case = await _run_git_case(helper, git_exe, base)
    cases = {item.get("name"): item for item in base.get("cases", []) if isinstance(item, dict)}
    raw_cases = {
        name: {
            "helper_exit_code": item.get("helper_exit_code"),
            "receipt": item.get("receipt", {}),
            "descendant_exit_observed": item.get("descendant_exit_observed"),
            "cancel_signal_sent": item.get("cancel_signal_sent"),
        }
        for name, item in cases.items()
    }
    powershell_receipt = cases.get("powershell_boundary", {}).get("receipt", {})
    exe_receipt = cases.get("exe_boundary", {}).get("receipt", {})
    ps_lines = str(powershell_receipt.get("stdout", "")).splitlines()
    exe_lines = str(exe_receipt.get("stdout", "")).splitlines()
    boundary_identity = {
        name: cases.get(name, {}).get("receipt", {}).get("appcontainer_verified")
        for name in ("cmd_smoke", "powershell_boundary", "exe_boundary")
    }
    process_starts = {
        name: {
            "helper_exit_code": cases.get(name, {}).get("helper_exit_code"),
            "pid": cases.get(name, {}).get("receipt", {}).get("pid"),
            "appcontainer_verified": cases.get(name, {}).get("receipt", {}).get("appcontainer_verified"),
            "error": cases.get(name, {}).get("receipt", {}).get("error"),
        }
        for name in ("cmd_smoke", "powershell_boundary", "exe_boundary")
    }
    return {
        "schema": SCHEMA,
        "candidate": "windows-regular-appcontainer",
        "lpac_requested": False,
        "production_authorized": False,
        "fixture": base.get("fixture"),
        "helper_build": helper_build,
        "helper_sha256": base.get("helper_sha256"),
        "host_control": base.get("host_control", {}),
        "observed": base.get("observed", {}),
        "fixture_acl_cleanup_ok": base.get("fixture_acl_cleanup_ok"),
        "drive_alias_cleanup_ok": base.get("drive_alias_cleanup_ok"),
        "cases": raw_cases,
        "candidate_process_start_facts": process_starts,
        "candidate_child_identity_observed": boundary_identity,
        "path_observations": {
            "host_same_acl_read_write_control": base.get("host_control", {}).get("outside_read_write"),
            "same_acl_synthetic_targets": ["outside/ordinary.txt", "outside/.env"],
            "candidate_powershell_identity_verified": boundary_identity.get("powershell_boundary"),
            "ordinary_file_read_markers": [line for line in ps_lines if line.startswith("OUTSIDE_READ_")],
            "secret_style_file_read_markers": [line for line in ps_lines if line.startswith("SECRET_READ_")],
            "host_observed_outside_write_exists": base.get("observed", {}).get("outside_write_exists"),
            "candidate_write_markers": [line for line in ps_lines if line.startswith("OUTSIDE_WRITE_")],
            "candidate_junction_markers": [line for line in ps_lines if line.startswith("JUNCTION_")],
            "host_can_read_appcontainer_output": base.get("observed", {}).get("output_readable_by_host"),
            "host_can_read_exe_output": base.get("observed", {}).get("exe_output_readable_by_host"),
        },
        "network_observations": {
            "host_positive_control_reachable": base.get("host_control", {}).get("same_payload_network_reachable"),
            "candidate_exe_identity_verified": boundary_identity.get("exe_boundary"),
            "candidate_payload_received_by_host": base.get("observed", {}).get("network_child_payload_received"),
            "candidate_network_markers": [line for line in exe_lines if line.startswith("NETWORK_")],
        },
        "cancellation_observation": raw_cases.get("cancellation_descendants"),
        "git_case": git_case,
        "network_scope_observed": "synthetic IPv4 loopback only; external egress is not exercised here",
        "path_targets_observed": ["synthetic ordinary.txt", "synthetic .env", "synthetic outside write", "synthetic junction"],
    }


def _candidate_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--git-exe", required=True, type=Path,
                        help="absolute Git for Windows git.exe; it is launched only as a sandbox child")
    parser.add_argument("--helper", type=Path,
                        help="existing absolute AppContainer probe helper executable")
    parser.add_argument("--compile-helper", action="store_true",
                        help="compile the reviewed probe source to a fresh temporary directory; does not install it")
    parser.add_argument("--execute-synthetic-probes", action="store_true", required=True,
                        help="required confirmation that only disposable synthetic targets will be exercised")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _candidate_parser().parse_args(argv)
    if os.name != "nt":
        raise SystemExit("Windows is required")
    if bool(args.helper) == bool(args.compile_helper):
        raise SystemExit("supply exactly one of --helper or --compile-helper")
    git_exe = checked_executable(args.git_exe, "Git executable")
    build_meta = None
    if args.helper:
        helper = checked_executable(args.helper, "AppContainer probe helper")
    else:
        scratch = (ROOT / ".tmp").resolve(strict=False)
        scratch.mkdir(exist_ok=True)
        if scratch.is_symlink() or not scratch.is_dir() or not scratch.is_relative_to(ROOT.resolve()):
            raise SystemExit("project .tmp scratch directory is not a safe local directory")
        with tempfile.TemporaryDirectory(prefix="mewcode-regular-appcontainer-helper-", dir=scratch) as temp:
            helper, build_meta = compile_probe_helper(Path(temp) / "compiled")
            report = asyncio.run(run_probe(helper, git_exe, build_meta))
    if args.helper:
        report = asyncio.run(run_probe(helper, git_exe, build_meta))
    fixture = Path(report["fixture"])
    evidence = fixture / "regular-appcontainer-candidate-report.json"
    evidence.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["evidence_path"] = str(evidence)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
