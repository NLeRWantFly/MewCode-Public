"""Prepare three hashed synthetic AppContainer requests; never changes host ACLs.

The separate, elevated MewCode.NullAclTrial.exe consumes the printed manifest
hash, backs up the original NUL DACL, runs these requests, and restores it.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.probe_windows_restricted_candidate import (  # noqa: E402
    _copy_git_runtime,
    _enumerate_repository_paths,
    _host_copied_git_controls,
    _host_git_environment,
    build_direct_git_request,
    checked_executable,
    compile_probe_helper,
    copied_git_dll_execute_paths,
    direct_git_case_arguments,
)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare() -> dict:
    if os.name != "nt":
        raise RuntimeError("Windows only")
    scratch = ROOT / ".tmp"
    if not scratch.is_dir() or scratch.is_symlink():
        raise RuntimeError("Expected an ordinary project .tmp directory")
    fixture = Path(tempfile.mkdtemp(prefix="mewcode-lpac-probe-", dir=scratch)).resolve(strict=True)
    (fixture / ".mewcode-synthetic-fixture").write_bytes(b"mewcode-synthetic-only-v1\n")
    workspace = fixture / "workspace"
    profile = fixture / "profile"
    repo = workspace / "git-synthetic-repository"
    repo.mkdir(parents=True)
    (profile / "AppData" / "Local").mkdir(parents=True)
    (profile / "AppData" / "Roaming").mkdir(parents=True)
    runtime_temp = repo / "temp"
    runtime_temp.mkdir()
    config = profile / "synthetic-gitconfig"
    config.write_text("", encoding="utf-8")
    (repo / "synthetic-input.txt").write_text("SYNTHETIC_GIT_INPUT\n", encoding="utf-8")

    system_root = Path(os.environ["SystemRoot"])
    csc = checked_executable(system_root / "Microsoft.NET/Framework64/v4.0.30319/csc.exe", "csc")
    nul_probe = repo / "MewCode.NulOpenProbe.exe"
    source = ROOT / "native/windows/MewCode.NulOpenProbe.cs"
    compiled = subprocess.run(
        [str(csc), "/nologo", "/platform:x64", "/out:" + str(nul_probe), str(source)],
        capture_output=True, timeout=30, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if compiled.returncode or not nul_probe.is_file():
        raise RuntimeError("NUL probe compilation failed")
    host_nul = subprocess.run(
        [str(nul_probe)], cwd=repo, capture_output=True, timeout=10, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    git_exe = checked_executable(Path(r"C:\Program Files\Git\cmd\git.exe"), "Git")
    git_root = git_exe.parent.parent
    host_init = subprocess.run(
        [str(git_exe), "-C", str(repo), "init", "--quiet"], cwd=repo,
        env=_host_git_environment(git_root, profile, runtime_temp, config),
        capture_output=True, timeout=20, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if host_init.returncode or not (repo / ".git").is_dir():
        raise RuntimeError("Synthetic host Git initialization failed")
    _copy_git_runtime(git_exe, repo)
    copied_core = repo / "git-runtime/mingw64/bin/git.exe"
    controls = _host_copied_git_controls(
        copied_core, repo / "git-runtime", repo, profile, runtime_temp, config,
    )
    if any(controls[name].get("exit_code") != 0 for name in ("version", "status")):
        raise RuntimeError("Copied Git host controls failed")

    helper, build = compile_probe_helper(fixture / "compiled")
    dirs, files = _enumerate_repository_paths(repo)
    read_paths = sorted(set(dirs + files + [
        profile, profile / "AppData", profile / "AppData/Local",
        profile / "AppData/Roaming", config,
    ]), key=lambda path: str(path).casefold())
    metadata = [repo / ".git", *(repo / ".git").rglob("*")]
    write_paths = sorted(set([repo, runtime_temp, *metadata]), key=lambda path: str(path).casefold())
    cases = []
    commands = (
        ("nul", nul_probe, [], []),
        ("git_version", copied_core, direct_git_case_arguments("version", repo), copied_git_dll_execute_paths(repo)),
        ("git_status", copied_core, direct_git_case_arguments("status", repo), copied_git_dll_execute_paths(repo)),
    )
    for name, executable, arguments, execute_paths in commands:
        request = build_direct_git_request(
            fixture=fixture, executable=executable, arguments=arguments, repository=repo,
            read_paths=read_paths, write_paths=write_paths, execute_paths=execute_paths,
        )
        path = fixture / ("nul-acl-" + name + ".request.json")
        path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
        cases.append({"name": name, "request": str(path), "request_sha256": _hash(path)})
    manifest = fixture / "nul-acl-trial-manifest.json"
    manifest.write_text(json.dumps({
        "fixture_root": str(fixture),
        "helper": str(helper),
        "helper_sha256": _hash(helper),
        "cases": cases,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "fixture": str(fixture), "manifest": str(manifest), "manifest_sha256": _hash(manifest),
        "helper_sha256": build["helper_sha256"],
        "host_nul_exit": host_nul.returncode,
        "host_nul_stdout": host_nul.stdout.decode(errors="replace").splitlines(),
        "host_copied_git_controls": controls,
    }


if __name__ == "__main__":
    print(json.dumps(prepare(), ensure_ascii=False, indent=2))
