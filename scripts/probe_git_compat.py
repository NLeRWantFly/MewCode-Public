"""Compare a project-local Git build with its patched copy in fresh fixtures.

Diagnostic only: no system installation, shared ACL changes, or production
authorization. Only --run creates a synthetic fixture and launches commands.
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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mewcode.sandbox.appcontainer_policy import build_probe_request
from scripts import probe_windows_path_roundtrip as rt
from scripts import probe_appcontainer_cwd as cwd_probe
from scripts import probe_windows_restricted_candidate as candidate

BUILD_NAME = "git-auto-compat-20260926"
SOURCE_COMMIT = "52ca1113d651127f89477a8763f86ab20f645e1d"
SDK_COMMIT = "4485060d9c90a94d907671e4f078bf816f9055c1"
ARCHIVE_SHA256 = "0fbcf08e8e68ec841f8135dc01bb953863a154ffc2d16b2733da503b4b357152"
SYSTEM_DLLS = {
    "advapi32.dll", "kernel32.dll", "msvcrt.dll", "ntdll.dll",
    "user32.dll", "ws2_32.dll", "bcrypt.dll", "shell32.dll",
}
VERSION = "git version 2.55.0.windows.3\n"
SCHEMA = "mewcode-git-compat-diagnostic-v1"


def sha(path: Path) -> str:
    return rt._sha256_file(path)


def read_json(path: Path) -> dict:
    result = json.loads(path.read_text(encoding="utf-8"),
                        object_pairs_hook=rt._strict_json_object,
                        parse_constant=rt._reject_nonfinite_json)
    if not isinstance(result, dict):
        raise ValueError("expected JSON object")
    return result


def checked_build(raw: Path) -> Path:
    path = rt._existing_directory(raw, "Git build directory")
    expected = ROOT / ".tmp" / BUILD_NAME
    if rt._path_key(path) != rt._path_key(expected):
        raise ValueError("build must use the authorized project-local directory")
    return path


def imported_dlls(build: Path, executable: Path) -> list[str]:
    tool = rt._existing_file(build / "sdk/mingw64/bin/objdump.exe", "objdump")
    # binutils cannot open an absolute non-ASCII argument on this host.
    relative = executable.relative_to(build).as_posix()
    result = subprocess.run([str(tool), "-p", relative], cwd=build,
                            capture_output=True, timeout=20,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode or len(result.stdout) > 4 * 1024 * 1024:
        raise ValueError("PE dependency inspection failed")
    names = re.findall(rb"DLL Name:\s*([^\r\n]+)", result.stdout)
    if not names:
        raise ValueError("no import table found")
    decoded = sorted({name.decode("ascii").strip().lower() for name in names})
    if any(not re.fullmatch(r"[a-z0-9_.-]+\.dll", name) for name in decoded):
        raise ValueError("invalid DLL name")
    return decoded


def dependency_closure(build: Path, binaries: list[Path]) -> list[Path]:
    seen: set[str] = set()
    pending = list(binaries)
    runtime: list[Path] = []
    while pending:
        for name in imported_dlls(build, pending.pop()):
            if name in seen:
                continue
            seen.add(name)
            if name in SYSTEM_DLLS:
                continue
            path = rt._existing_file(build / "sdk/mingw64/bin" / name, "runtime DLL")
            runtime.append(path)
            pending.append(path)
    return sorted(runtime)


def write_manifest(build: Path) -> Path:
    """Freeze hashes after both builds; no executions except PE inspection."""
    build = checked_build(build)
    source = read_json(build / "source-lock.json")
    if source.get("commit") != SOURCE_COMMIT or source.get("archive_sha256") != ARCHIVE_SHA256:
        raise ValueError("unexpected source lock")
    archive = build / f"git-{SOURCE_COMMIT}.tar.gz"
    if sha(archive) != ARCHIVE_SHA256:
        raise ValueError("source archive changed")
    sdk = build / "sdk"
    # The shallow checkout retains the branch ref locally; never fetch here.
    ref = (sdk / ".git/refs/heads/mingw64").read_text(encoding="ascii").strip()
    if ref != SDK_COMMIT:
        raise ValueError("SDK commit changed")
    binaries = [build / variant / name for variant in ("original", "patched")
                for name in ("git.exe", "t/helper/test-tool.exe")]
    binaries += [build / "compat-live.exe", build / "compat-unit.exe"]
    for binary in binaries:
        rt._existing_file(binary, "built executable")
    dependencies = dependency_closure(build, binaries)
    inputs = [archive, build / "source-lock.json", build / "build-git.sh",
              build / "build-probes.sh", sdk / "mingw64/bin/gcc.exe",
              sdk / "mingw64/bin/ld.exe", sdk / "mingw64/bin/objdump.exe",
              sdk / "usr/bin/make.exe", sdk / "usr/bin/bash.exe"]
    inputs += [build / v / name for v in ("original", "patched")
               for name in ("setup.c", "compat/mingw.c")]
    inputs += [build / "patched/compat/mewcode-appcontainer.h",
               build / "original-build.log", build / "patched-build.log",
               build / "probes-build.log", sdk / ".git/info/sparse-checkout"]
    patch_dir = ROOT / "native/windows/git-compat"
    sources = {str(p.relative_to(ROOT)): sha(p) for p in sorted(patch_dir.iterdir())
               if p.is_file() and p.suffix in {".c", ".h", ".patch"}}
    manifest = {
        "schema": "mewcode-git-compat-build-v1", "production_authorized": False,
        "source_commit": SOURCE_COMMIT, "sdk_commit": SDK_COMMIT,
        "files": {p.relative_to(build).as_posix(): sha(p) for p in inputs + binaries + dependencies},
        "sources": sources,
        "dlls": [p.relative_to(build).as_posix() for p in dependencies],
        "compiler": "GCC 16.2.0 / MinGW64", "make": "4.4.1",
        "same_build_script": True, "feature_define": "MEWCODE_APPCONTAINER_COMPAT",
    }
    target = build / "build-manifest.json"
    rt._write_json_new(target, manifest)
    return target


def verify_manifest(build: Path, manifest: dict) -> None:
    if (manifest.get("schema") != "mewcode-git-compat-build-v1"
            or manifest.get("production_authorized") is not False
            or manifest.get("source_commit") != SOURCE_COMMIT
            or manifest.get("sdk_commit") != SDK_COMMIT):
        raise ValueError("invalid build manifest")
    for base, field in ((build, "files"), (ROOT, "sources")):
        entries = manifest.get(field)
        if not isinstance(entries, dict) or not entries:
            raise ValueError("empty manifest")
        for relative, digest in entries.items():
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts or ":" in relative:
                raise ValueError("manifest path escapes base")
            target = rt._existing_file(base / path, "manifest artifact")
            if not rt._strictly_beneath(target, base) or sha(target) != digest:
                raise ValueError("manifest artifact drift")
    required = {f"{v}/{name}" for v in ("original", "patched")
                for name in ("git.exe", "t/helper/test-tool.exe")}
    required |= {"compat-live.exe", "compat-unit.exe", "build-git.sh"}
    if not required.issubset(manifest["files"]):
        raise ValueError("missing required artifacts")
    dlls = manifest.get("dlls")
    if not isinstance(dlls, list) or not dlls or len(dlls) != len(set(dlls)):
        raise ValueError("missing or duplicate DLL closure")
    for relative in dlls:
        if relative not in manifest["files"] or not re.fullmatch(r"sdk/mingw64/bin/[a-z0-9_.-]+\.dll", relative):
            raise ValueError("unbound DLL")


def output_passed(case: str, code: object, stdout: object, stderr: object,
                  repo: Path, *, container: bool) -> bool:
    if type(code) is not int or code != 0 or not isinstance(stdout, str) or stderr != "":
        return False
    text = stdout.replace("\r\n", "\n")
    expected = {
        "version": VERSION, "status": "?? sample.txt\n",
        "status_explicit": "?? sample.txt\n",
        "getcwd": repo.as_posix() + "\n",
        "realpath_existing": (repo / "sample.txt").as_posix() + "\n",
        "realpath_missing": (repo / "missing.txt").as_posix() + "\n",
    }
    if case == "live":
        expected_live = (
            f"appcontainer={int(container)}\n"
            f"original_dos_ok={int(not container)} error={5 if container else 0}\n"
            "MEWCODE_GIT_COMPAT_LIVE_OK same_file=1 buffer=1 invalid_fd=1\n"
        )
        return text == expected_live
    return case in expected and text == expected[case]


def helper_evidence(invocation: dict, *, lpac: bool) -> dict:
    facts = rt._receipt_facts(invocation, {"protocol_valid": True}, lpac=lpac)
    receipt = invocation.get("receipt") or {}
    # A nonzero Git result is a valid observation, never a successful command.
    complete = (facts["receipt_valid"] and facts["identity_verified"]
                and facts["capabilities_verified"] and facts["cleanup_confirmed"]
                and receipt.get("error") == ""
                and receipt.get("timed_out") is False and receipt.get("cancelled") is False
                and receipt.get("stdout_truncated") is False
                and receipt.get("stderr_truncated") is False
                and isinstance(receipt.get("stdout"), str) and isinstance(receipt.get("stderr"), str))
    return {"complete": bool(complete), "facts": facts}


async def run(build: Path) -> dict:
    if os.name != "nt":
        raise ValueError("Windows required")
    build = checked_build(build)
    manifest_path = build / "build-manifest.json"
    manifest = read_json(manifest_path)
    verify_manifest(build, manifest)
    manifest_hash = sha(manifest_path)
    paths = rt._new_fixture()
    rt._validate_new_fixture(paths)
    fixture, repo = paths["fixture"], paths["cwd"]
    report = {"schema": SCHEMA, "production_authorized": False,
              "manifest_sha256": manifest_hash, "observations": [],
              "infrastructure_complete": False, "candidate_passed": False,
              "acl_evidence": "helper_receipt_only", "error": None}
    target = fixture / "git-compat.report.json"
    report["report_path"] = str(target)
    pins: dict[Path, str] = {}
    try:
        binaries: dict[str, dict[str, Path]] = {}
        for variant in ("original", "patched"):
            dest = fixture / "runtimes" / variant
            dest.mkdir(parents=True, exist_ok=False)
            binary_set = {}
            sources = {"git.exe": build / variant / "git.exe",
                       "test-tool.exe": build / variant / "t/helper/test-tool.exe",
                       "compat-live.exe": build / "compat-live.exe"}
            sources.update({Path(p).name: build / p for p in manifest["dlls"]})
            for name, source in sources.items():
                copied = dest / name
                shutil.copyfile(source, copied)
                if sha(copied) != sha(source):
                    raise ValueError("copy drift")
                pins[copied] = sha(copied)
                binary_set[name] = copied
            binaries[variant] = binary_set
        config = paths["profile"] / "empty.gitconfig"
        rt._write_bytes_new(config, b"# synthetic empty config\n")
        template = fixture / "empty-template"
        template.mkdir()
        rt._write_bytes_new(repo / "sample.txt", b"synthetic Git compatibility input\n")
        helper, helper_build = candidate.compile_probe_helper(paths["helper_output"])
        if sha(helper) != helper_build["helper_sha256"] or sha(candidate.PROBE_SOURCE) != helper_build["source_sha256"]:
            raise ValueError("helper compile drift")
        pins[helper] = helper_build["helper_sha256"]
        report["helper_build"] = helper_build
        env = build_probe_request(fixture_root=fixture, executable=binaries["original"]["git.exe"],
                                  arguments=[], cwd=repo, read_paths=[repo], write_paths=[paths["temp"]])["environment"]
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(config),
                    "GIT_ATTR_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
                    "GIT_OPTIONAL_LOCKS": "0", "GIT_CEILING_DIRECTORIES": str(repo.parent)})
        init = await cwd_probe._run_process(
            [str(binaries["original"]["git.exe"]), "-c", "init.defaultBranch=main",
             "init", "--quiet", "--template=" + str(template)],
            cwd=repo, environment=env, timeout=15)
        if not rt._process_is_complete(init) or init["exit_code"] != 0 or not (repo / ".git").is_dir():
            raise ValueError("synthetic repository initialization failed")
        reads = [repo, *sorted(repo.rglob("*")), config]
        for path in reads:
            if path.is_file():
                pins[path] = sha(path)
        cases = [(v, name) for v in ("original", "patched") for name in
                 ("version", "status", "status_explicit", "getcwd", "realpath_existing", "realpath_missing")]
        cases.append(("patched", "live"))
        for variant, case in cases:
            binary_set = binaries[variant]
            git, test = binary_set["git.exe"], binary_set["test-tool.exe"]
            commands = {
                "version": [str(git), "--version"],
                "status": [str(git), "status", "--porcelain"],
                "status_explicit": [str(git), "-C", str(repo), "status", "--porcelain"],
                "getcwd": [str(test), "getcwd"],
                "realpath_existing": [str(test), "path-utils", "real_path", "sample.txt"],
                "realpath_missing": [str(test), "path-utils", "real_path", "missing.txt"],
                "live": [str(binary_set["compat-live.exe"])],
            }
            command = commands[case]
            for mode in ("host", "appcontainer", "lpac"):
                if sha(manifest_path) != manifest_hash or any(sha(p) != h for p, h in pins.items()):
                    raise ValueError("artifact drift before case")
                name = f"{variant}-{case}-{mode}"
                observation = {"variant": variant, "case": case, "mode": mode}
                if mode == "host":
                    result = await cwd_probe._run_process(command, cwd=repo, environment=env, timeout=15)
                    for stream in ("stdout", "stderr"):
                        rt._write_bytes_new(fixture / f"{name}.{stream}.bin", result[stream])
                    complete = rt._process_is_complete(result)
                    code = result["exit_code"]
                    stdout = result["stdout"].decode("utf-8", errors="strict")
                    stderr = result["stderr"].decode("utf-8", errors="strict")
                else:
                    lpac = mode == "lpac"
                    request = build_probe_request(
                        fixture_root=fixture, executable=Path(command[0]), arguments=command[1:], cwd=repo,
                        read_paths=reads + list(binary_set.values()), write_paths=[paths["temp"]],
                        execute_paths=list(binary_set.values()), lpac=lpac, timeout_ms=20_000,
                        runtime_registry_read=lpac, runtime_instrumentation=lpac, runtime_com=lpac)
                    request["environment"] = env.copy()
                    request_path = fixture / f"{name}.request.json"
                    rt._write_json_new(request_path, request)
                    before = sha(request_path)
                    invocation = await rt._invoke_helper(helper, request_path, cwd=repo, environment=env)
                    for stream in ("stdout", "stderr"):
                        rt._write_bytes_new(fixture / f"{name}.helper-{stream}.bin", invocation[stream])
                    evidence = helper_evidence(invocation, lpac=lpac)
                    complete = evidence["complete"] and sha(request_path) == before
                    observation["evidence"] = evidence
                    observation["request_sha256"] = before
                    receipt = invocation.get("receipt") or {}
                    code, stdout, stderr = (receipt.get(k) for k in ("exit_code", "stdout", "stderr"))
                passed = complete and output_passed(case, code, stdout, stderr, repo, container=mode != "host")
                observation.update(complete=bool(complete), passed=bool(passed), exit_code=code,
                                   stdout=stdout, stderr=stderr)
                report["observations"].append(observation)
                if not complete or (mode == "host" and not passed):
                    raise ValueError(f"incomplete observation or failed host control: {name}")
        verify_manifest(build, manifest)
        if sha(candidate.PROBE_SOURCE) != helper_build["source_sha256"]:
            raise ValueError("helper source drift")
        if sha(manifest_path) != manifest_hash or any(sha(p) != h for p, h in pins.items()):
            raise ValueError("final artifact drift")
        report["artifact_hashes_unchanged"] = True
        report["fixture_artifacts"] = {p.relative_to(fixture).as_posix(): h for p, h in pins.items()}
        report["infrastructure_complete"] = len(report["observations"]) == 39
        report["candidate_passed"] = report["infrastructure_complete"] and all(
            item["passed"] for item in report["observations"] if item["variant"] == "patched")
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        rt._write_json_new(target, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-root", type=Path, default=ROOT / ".tmp" / BUILD_NAME)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--freeze-build", action="store_true")
    group.add_argument("--run", action="store_true")
    parser.add_argument("--assert-compatible", action="store_true")
    args = parser.parse_args(argv)
    if args.assert_compatible and not args.run:
        parser.error("--assert-compatible requires --run")
    if args.freeze_build:
        print(write_manifest(args.build_root))
        return 0
    if not args.run:
        print("Not executed; use --freeze-build after builds, then --run.")
        return 0
    report = asyncio.run(run(args.build_root))
    print(json.dumps({k: report[k] for k in ("report_path", "infrastructure_complete", "candidate_passed", "error")}, ensure_ascii=False))
    return 0 if report["infrastructure_complete"] and (not args.assert_compatible or report["candidate_passed"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
