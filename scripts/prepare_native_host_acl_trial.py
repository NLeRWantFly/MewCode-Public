"""Prepare a hashed synthetic fixture for the one-shot native host ACL trial.

This script creates payloads and requests only. It never invokes the compiled
AppContainer helper and never changes any ACL. The separate host trial tool
consumes the manifest hash after the user reviews the prepared fixture.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import probe_windows_restricted_candidate as candidate  # noqa: E402
from mewcode.sandbox import appcontainer_policy  # noqa: E402


SCHEMA = "mewcode-native-host-acl-trial-v1"
FIXTURE_PREFIX = "mewcode-lpac-probe-"
SENTINEL_NAME = ".mewcode-synthetic-fixture"
SENTINEL_CONTENT = b"mewcode-synthetic-only-v1\n"
INPUT_CONTENT = "SYNTHETIC_GIT_INPUT\n"
POWERSHELL_HOST_MARKER = "MEWCODE_POWERSHELL_HOST_CONTROL_OK"
SYSTEM_GIT = Path(r"C:\Program Files\Git\cmd\git.exe")
CASE_NAMES = ("nul", "cwd", "git_version", "git_status", "powershell")
LPAC_CAPABILITIES = {
    "runtime_registry_read": True,
    "runtime_instrumentation": True,
    "runtime_com": True,
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return True
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x0400)


def _require_plain_path(path: Path, label: str, *, directory: bool | None = None) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise RuntimeError(f"{label} must be an absolute path without traversal")
    for item in (path, *path.parents):
        if _is_reparse(item):
            raise RuntimeError(f"{label} uses a missing or reparse path")
    resolved = path.resolve(strict=True)
    if directory is True and not resolved.is_dir():
        raise RuntimeError(f"{label} must be a directory")
    if directory is False and not resolved.is_file():
        raise RuntimeError(f"{label} must be a file")
    return resolved


def _system_drive_root() -> tuple[Path, str]:
    system_root = appcontainer_policy._windows_system_root()
    system_root = _require_plain_path(system_root, "Windows system root", directory=True)
    drive, _ = ntpath.splitdrive(str(system_root))
    if not re.fullmatch(r"[A-Za-z]:", drive):
        raise RuntimeError("Windows system root must be on a local drive")
    return system_root, drive.upper() + "\\"


@contextmanager
def _fixture_temp_environment(runtime_temp: Path):
    """Keep compiler scratch output inside the synthetic fixture."""
    old_values = {key: os.environ.get(key) for key in ("TEMP", "TMP")}
    os.environ["TEMP"] = str(runtime_temp)
    os.environ["TMP"] = str(runtime_temp)
    try:
        yield
    finally:
        for key, value in old_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _compile_native_probe(source: Path, target: Path, compiler: Path, runtime_temp: Path) -> Path:
    source = _require_plain_path(source, "native probe source", directory=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [str(compiler), "/nologo", "/platform:x64", "/out:" + str(target), str(source)]
    environment = os.environ.copy()
    environment["TEMP"] = str(runtime_temp)
    environment["TMP"] = str(runtime_temp)
    try:
        result = subprocess.run(
            command,
            cwd=target.parent,
            env=environment,
            capture_output=True,
            timeout=45,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("native probe compilation could not complete") from exc
    if result.returncode != 0 or not target.is_file() or _is_reparse(target):
        raise RuntimeError("native probe compilation failed")
    return target.resolve(strict=True)


def _powershell_quote(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _powershell_smoke_script(repository: Path, payload: Path) -> str:
    """Return the fixed, non-interactive smoke script used by host and trials."""
    return (
        "$ErrorActionPreference = 'Stop'\n"
        # Match lpac_runtime.build_request's existing runspace-only bootstrap.
        # This neither maps a Windows drive nor changes any filesystem ACL.
        f"New-PSDrive -Name MewWork -PSProvider FileSystem -Root {_powershell_quote(repository)} | Out-Null\n"
        "Set-Location -LiteralPath 'MewWork:\\'\n"
        f"if ((Get-Location).ProviderPath.TrimEnd('\\') -ne {_powershell_quote(repository)}) {{ exit 71 }}\n"
        f"$content = Get-Content -LiteralPath {_powershell_quote(payload)} -Raw\n"
        "if ($content.Trim() -ne 'SYNTHETIC_GIT_INPUT') { exit 72 }\n"
        f"Write-Output '{POWERSHELL_HOST_MARKER}'\n"
        "exit 0\n"
    )


def _powershell_arguments(repository: Path) -> list[str]:
    # Match the production command-entry API. Do not alter execution policy or
    # load a local script through Invoke-Expression; these are fixed expressions.
    return ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
            _powershell_smoke_script(repository, repository / "synthetic-input.txt")]


def _run_control(
    *,
    name: str,
    command: list[str],
    cwd: Path,
    environment: dict[str, str],
    expected_lines: tuple[str, ...] = (),
    expected_stdout: str | None = None,
) -> dict:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            capture_output=True,
            timeout=25,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"name": name, "exception_type": type(exc).__name__, "ok": False}

    stdout = result.stdout.decode("utf-8", errors="replace")
    lines = set(stdout.splitlines())
    markers_ok = all(line in lines for line in expected_lines)
    if expected_stdout is not None:
        markers_ok = stdout.strip() == expected_stdout
    return {
        "name": name,
        "exit_code": result.returncode,
        "stdout_bytes": len(result.stdout),
        "stderr_bytes": len(result.stderr),
        "markers_ok": markers_ok,
        "ok": result.returncode == 0 and markers_ok,
    }


def _run_host_controls(
    *,
    system_root: Path,
    git_core: Path,
    git_runtime: Path,
    nul_probe: Path,
    cwd_probe: Path,
    powershell_script: Path,
    repository: Path,
    workspace: Path,
    profile: Path,
    runtime_temp: Path,
    global_config: Path,
) -> dict:
    git_environment = candidate._host_git_environment(
        git_runtime, profile, runtime_temp, global_config,
    )
    git_environment["GIT_OPTIONAL_LOCKS"] = "0"
    system32 = system_root / "System32"
    powershell = candidate.checked_executable(
        system32 / "WindowsPowerShell" / "v1.0" / "powershell.exe", "Windows PowerShell",
    )
    nul = _run_control(
        name="nul",
        command=[str(nul_probe)],
        cwd=repository,
        environment=git_environment,
        expected_lines=("nul-read:open=ok", "nul-write:open=ok", "nul-readwrite:open=ok"),
    )
    cwd = _run_control(
        name="cwd",
        command=[str(cwd_probe), str(workspace)],
        cwd=repository,
        environment=git_environment,
        expected_lines=(
            "getcwd=ok",
            "dot:attributes=ok",
            "parent:attributes=ok",
            "dot_read_attributes:open=ok",
            "dot_list_directory:open=ok",
            "parent_list_directory:open=ok",
            "dot_star:find=ok",
            "parent_star:find=ok",
            "git_mingw_getcwd=ok",
        ),
    )
    version = _run_control(
        name="git_version",
        command=[str(git_core), "--version"],
        cwd=repository,
        environment=git_environment,
    )
    status = _run_control(
        name="git_status",
        command=[str(git_core), "-c", "core.fsmonitor=false", "status", "--porcelain"],
        cwd=repository,
        environment=git_environment,
    )
    powershell_env = dict(git_environment)
    powershell_env["PATH"] = os.pathsep.join((str(system32), str(system32 / "WindowsPowerShell" / "v1.0")))
    powershell = _run_control(
        name="powershell",
        command=[str(powershell), *_powershell_arguments(repository)],
        cwd=repository,
        environment=powershell_env,
        expected_stdout=POWERSHELL_HOST_MARKER,
    )
    return {
        "schema": "mewcode-native-host-acl-host-controls-v1",
        "controls": [nul, cwd, version, status, powershell],
    }


def _require_host_controls(controls: dict) -> None:
    observed = controls.get("controls") if isinstance(controls, dict) else None
    if not isinstance(observed, list) or [entry.get("name") for entry in observed] != list(CASE_NAMES):
        raise RuntimeError("Host positive controls are incomplete")
    failed = [{key: entry.get(key) for key in ("name", "exit_code", "markers_ok", "exception_type")}
              for entry in observed if entry.get("ok") is not True]
    if failed:
        raise RuntimeError("Host positive controls did not all succeed: " + json.dumps(failed))


def _existing_git_metadata(repository: Path) -> list[Path]:
    metadata = [repository / ".git", *(repository / ".git").rglob("*")]
    return [path for path in metadata if path.is_dir() or path.is_file()]


def _request_for_case(
    *,
    name: str,
    lpac: bool,
    fixture: Path,
    repository: Path,
    workspace: Path,
    profile: Path,
    global_config: Path,
    runtime_temp: Path,
    nul_probe: Path,
    cwd_probe: Path,
    git_core: Path,
    powershell: Path,
    powershell_script: Path,
) -> dict:
    directories, files = candidate._enumerate_repository_paths(repository)
    profile_paths = [
        profile,
        profile / "AppData",
        profile / "AppData" / "Local",
        profile / "AppData" / "Roaming",
        global_config,
    ]
    git_read = sorted(set(directories + files + profile_paths), key=lambda path: str(path).casefold())
    common_write = [runtime_temp]

    if name == "nul":
        executable, arguments, read_paths, write_paths, execute_paths = (
            nul_probe, [], [repository], common_write, [],
        )
    elif name == "cwd":
        executable, arguments, read_paths, write_paths, execute_paths = (
            cwd_probe, [str(workspace)], [repository, workspace], common_write, [],
        )
    elif name == "git_version":
        executable, arguments, read_paths, write_paths, execute_paths = (
            git_core,
            candidate.direct_git_case_arguments("version", repository),
            git_read,
            [repository, runtime_temp, *_existing_git_metadata(repository)],
            candidate.copied_git_dll_execute_paths(repository),
        )
    elif name == "git_status":
        executable = git_core
        arguments = ["-c", "core.fsmonitor=false", "status", "--porcelain"]
        read_paths = git_read
        write_paths = [repository, runtime_temp, *_existing_git_metadata(repository)]
        execute_paths = candidate.copied_git_dll_execute_paths(repository)
    elif name == "powershell":
        executable, arguments = powershell, _powershell_arguments(repository)
        read_paths = [repository, repository / "synthetic-input.txt", powershell_script]
        write_paths, execute_paths = common_write, []
    else:
        raise ValueError("unknown native host ACL trial case")

    runtime_flags = LPAC_CAPABILITIES if lpac else {
        "runtime_registry_read": False,
        "runtime_instrumentation": False,
        "runtime_com": False,
    }
    return appcontainer_policy.build_probe_request(
        fixture_root=fixture,
        executable=executable,
        arguments=arguments,
        cwd=repository,
        read_paths=read_paths,
        write_paths=write_paths,
        execute_paths=execute_paths,
        timeout_ms=20_000,
        lpac=lpac,
        **runtime_flags,
    )


def _artifact(path: Path) -> dict:
    resolved = _require_plain_path(path, "fixture artifact", directory=False)
    return {"path": str(resolved), "sha256": _sha256(resolved)}


def prepare() -> dict:
    if os.name != "nt":
        raise RuntimeError("Windows only")
    scratch = _require_plain_path(ROOT / ".tmp", "project .tmp directory", directory=True)
    project_root = _require_plain_path(ROOT, "project root", directory=True)
    if not scratch.is_relative_to(project_root):
        raise RuntimeError("fixture parent must be inside the project")
    fixture = Path(tempfile.mkdtemp(prefix=FIXTURE_PREFIX, dir=scratch)).resolve(strict=True)
    if not fixture.is_relative_to(scratch) or not fixture.name.startswith(FIXTURE_PREFIX):
        raise RuntimeError("created fixture escaped the project .tmp directory")
    _require_plain_path(fixture, "fixture root", directory=True)
    (fixture / SENTINEL_NAME).write_bytes(SENTINEL_CONTENT)

    workspace = fixture / "workspace"
    profile = fixture / "profile"
    repository = workspace / "git-synthetic-repository"
    for directory in (
        repository,
        repository / "temp",
        profile / "AppData" / "Local",
        profile / "AppData" / "Roaming",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    runtime_temp = repository / "temp"
    global_config = profile / "synthetic-gitconfig"
    global_config.write_text("", encoding="utf-8")
    payload = repository / "synthetic-input.txt"
    payload.write_text(INPUT_CONTENT, encoding="utf-8", newline="")

    system_root, drive_root = _system_drive_root()
    compiler = candidate.checked_executable(
        system_root / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe",
        ".NET Framework C# compiler",
    )
    powershell = candidate.checked_executable(
        system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe",
        "Windows PowerShell",
    )
    git_exe = candidate.checked_executable(SYSTEM_GIT, "Git executable")
    git_root = git_exe.parent.parent.resolve(strict=True)

    nul_probe = repository / "native-probes" / "MewCode.NulOpenProbe.exe"
    cwd_probe = repository / "native-probes" / "MewCode.CwdAccessProbe.exe"
    nul_source = ROOT / "native" / "windows" / "MewCode.NulOpenProbe.cs"
    cwd_source = ROOT / "native" / "windows" / "MewCode.CwdAccessProbe.cs"
    with _fixture_temp_environment(runtime_temp):
        nul_probe = _compile_native_probe(nul_source, nul_probe, compiler, runtime_temp)
        cwd_probe = _compile_native_probe(cwd_source, cwd_probe, compiler, runtime_temp)
        helper, helper_build = candidate.compile_probe_helper(fixture / "compiled-helper")

    init_environment = candidate._host_git_environment(git_root, profile, runtime_temp, global_config)
    init = subprocess.run(
        [str(git_exe), "-C", str(repository), "init", "--quiet"],
        cwd=repository,
        env=init_environment,
        capture_output=True,
        timeout=25,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if init.returncode != 0 or not (repository / ".git").is_dir():
        raise RuntimeError("Synthetic host Git initialization failed")

    copied_git, copied_files = candidate._copy_git_runtime(git_exe, repository)
    git_core = repository / "git-runtime" / "mingw64" / "bin" / "git.exe"
    git_runtime = repository / "git-runtime"
    copy_hashes = []
    for item in copied_files:
        source = Path(item["source"])
        target = Path(item["path"])
        source_hash = _sha256(_require_plain_path(source, "Git runtime source", directory=False))
        target_hash = _sha256(_require_plain_path(target, "Git runtime copy", directory=False))
        if source_hash != target_hash:
            raise RuntimeError("Copied Git runtime hash changed during fixture preparation")
        copy_hashes.append({"source": str(source), "sha256": source_hash})

    script_path = fixture / "powershell-native-host-smoke.ps1"
    # Windows PowerShell 5.1 needs a BOM to recognize UTF-8 paths containing CJK.
    script_path.write_text(_powershell_smoke_script(repository, payload), encoding="utf-8-sig", newline="")
    powershell_script = _require_plain_path(script_path, "fixed PowerShell smoke script", directory=False)

    controls = _run_host_controls(
        system_root=system_root,
        git_core=git_core,
        git_runtime=git_runtime,
        nul_probe=nul_probe,
        cwd_probe=cwd_probe,
        powershell_script=powershell_script,
        repository=repository,
        workspace=workspace,
        profile=profile,
        runtime_temp=runtime_temp,
        global_config=global_config,
    )
    host_controls_path = fixture / "native-host-acl-host-controls.json"
    host_controls_path.write_text(json.dumps(controls, ensure_ascii=False, indent=2), encoding="utf-8")
    _require_host_controls(controls)

    requests = []
    for lpac, prefix in ((False, "ac_"), (True, "lpac_")):
        for name in CASE_NAMES:
            case_name = prefix + name
            request = _request_for_case(
                name=name,
                lpac=lpac,
                fixture=fixture,
                repository=repository,
                workspace=workspace,
                profile=profile,
                global_config=global_config,
                runtime_temp=runtime_temp,
                nul_probe=nul_probe,
                cwd_probe=cwd_probe,
                git_core=git_core,
                powershell=powershell,
                powershell_script=powershell_script,
            )
            request_path = fixture / f"native-host-acl-trial-{case_name}.request.json"
            request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
            requests.append({
                "name": case_name,
                "request": str(request_path.resolve(strict=True)),
                "request_sha256": _sha256(request_path),
            })

    git_artifacts = [Path(item["path"]) for item in copied_files]
    artifacts = [
        fixture / SENTINEL_NAME,
        payload,
        global_config,
        nul_probe,
        cwd_probe,
        script_path,
        helper,
        *git_artifacts,
        host_controls_path,
    ]
    artifact_manifest = [_artifact(path) for path in artifacts]
    source_paths = [nul_source, cwd_source, candidate.PROBE_SOURCE]
    source_hashes = [
        {"path": str(path.resolve(strict=True)), "sha256": _sha256(path)}
        for path in source_paths
    ]
    source_hashes.extend(copy_hashes)

    # Preserve the ordinary host's file ownership so the elevated trial can
    # export diagnostics without requiring another UAC just to read receipts.
    (fixture / "host-acl-recovery-inspection.json").touch(exist_ok=False)

    manifest_path = fixture / "native-host-acl-trial-manifest.json"
    manifest = {
        "schema": SCHEMA,
        "fixture_root": str(fixture),
        "system_drive_root": drive_root,
        "helper": str(helper.resolve(strict=True)),
        "helper_sha256": _sha256(helper),
        "cases": requests,
        "artifacts": artifact_manifest,
        "source_hashes": source_hashes,
        "host_controls": str(host_controls_path.resolve(strict=True)),
        "host_controls_sha256": _sha256(host_controls_path),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "fixture_root": str(fixture),
        "manifest": str(manifest_path.resolve(strict=True)),
        "manifest_sha256": _sha256(manifest_path),
        "case_count": len(requests),
        "host_controls_ok": True,
        "helper_sha256": _sha256(helper),
    }


if __name__ == "__main__":
    try:
        print(json.dumps(prepare(), ensure_ascii=False, separators=(",", ":")))
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False))
        raise
