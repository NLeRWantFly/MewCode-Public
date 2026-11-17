"""Synthetic native writable-copy feasibility probe, never a production route.

Only fresh project .tmp directories are accepted. No original business data,
system Git, global ACL, gate record or installation is modified. --run is
explicit; the prototype result schema is not accepted by native_evidence.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mewcode.sandbox import lpac_runtime as lpac  # noqa: E402
from mewcode.sandbox import staged_workspace as staging  # noqa: E402
from mewcode.sandbox import staged_publication as publication  # noqa: E402
from mewcode.orchestration.write_lease import WriteLeaseStore  # noqa: E402
from scripts.native_acceptance_executor import bounded_directory  # noqa: E402
from scripts.probe_lpac_production import build_checker  # noqa: E402
from scripts.manage_native_runtime import verify_bundle  # noqa: E402

SCHEMA = "mewcode-staged-workspace-feasibility-v1"
SOURCES = tuple(ROOT / "native/windows" / name for name in (
    "MewCode.AppContainerProbe.cs", "MewCode.AppContainerRuntime.cs", "MewCode.StagedWorkspacePrototype.cs"))


class MappedScope:
    """Private-copy paths map back to the host's original policy, not a new grant."""
    def __init__(self, scope, workspace):
        self.original = scope
        self.workspace = workspace

    def current(self):
        return self.original.current()

    def allows(self, path, *, write):
        if path.is_relative_to(self.workspace):
            path = self.original.workspace / path.relative_to(self.workspace)
        return self.original.allows(path, write=write)


def protections(plan: staging.StagedPlan) -> tuple[list[str], list[str]]:
    readonly, containers = [], []
    entries = plan.baseline
    writable_ancestors = {parent.as_posix() for entry in entries if entry.writable
                          for parent in Path(entry.relative).parents}
    for entry in entries:
        if entry.relative == "." or entry.writable:
            continue
        path = plan.workspace / entry.relative
        if entry.kind == "directory":
            # Pin containers whose descendants include authorized writes;
            # do not deny the whole parent and prevent those writes.
            nested_writes = entry.relative in writable_ancestors
            if path.name.casefold() == ".git" or nested_writes:
                containers.append(str(path))
                continue
        readonly.append(str(path))
    return readonly, containers


def build_prototype(output: Path) -> tuple[Path, dict]:
    compiler = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    helper = output / "MewCode.StagedWorkspacePrototype.exe"
    args = [str(compiler), "/nologo", "/platform:x64", "/r:System.Web.Extensions.dll",
            "/define:MEWCODE_STAGED_PROTOTYPE", "/main:MewCode.Native.Windows.MewCodeStagedWorkspacePrototype",
            "/out:" + str(helper), *(str(path) for path in SOURCES)]
    result = subprocess.run(args, capture_output=True, timeout=60,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    (output / "compile.stdout.log").write_bytes(result.stdout)
    (output / "compile.stderr.log").write_bytes(result.stderr)
    if result.returncode:
        raise RuntimeError("Prototype compilation failed; inspect retained compile logs")
    provenance = {"helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
                  "source_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in SOURCES},
                  "host_source_sha256": {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                                         for path in (Path(__file__).resolve(), Path(staging.__file__).resolve(),
                                                      Path(lpac.__file__).resolve(), Path(publication.__file__).resolve(),
                                                      ROOT / "mewcode/sandbox/publication_io.py",
                                                      ROOT / "mewcode/sandbox/powershell_compat.py",
                                                      ROOT / "mewcode/orchestration/write_lease.py")}}
    return helper, provenance


def seed_source(output: Path, git: Path) -> Path:
    source = output / "synthetic-source"
    source.mkdir()
    (source / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    (source / "outputs/prior/nested").mkdir(parents=True)
    (source / "outputs/prior/nested/data.txt").write_text("directory baseline\n", encoding="utf-8")
    config = output / "empty-host.gitconfig"
    config.write_text("", encoding="ascii")
    template = output / "empty-template"
    template.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "Synthetic", "GIT_AUTHOR_EMAIL": "synthetic@localhost",
           "GIT_COMMITTER_NAME": "Synthetic", "GIT_COMMITTER_EMAIL": "synthetic@localhost"}
    for args in (["-c", "init.defaultBranch=main", "init", "--quiet", "--template=" + str(template)],
                 ["add", "--", "tracked.txt", "outputs"], ["commit", "--quiet", "-m", "synthetic baseline"]):
        result = subprocess.run([str(git), "-C", str(source), *args], env=env, capture_output=True,
                                timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise RuntimeError("Cannot create the isolated synthetic Git input")
    return source


async def run_case(output: Path, name: str, command: str, expected: tuple[str, ...],
                   scope, revision: str, helper: Path, git_dir: Path, *, publish: bool = False) -> dict:
    parent = output / name
    parent.mkdir()
    host_stage = parent / "transactions"
    host_stage.mkdir()
    plan = staging.prepare_workspace(scope, host_stage, policy_revision=revision)
    runtime = parent / "runtime"
    for part in ("temp", "profile/AppData/Local", "profile/AppData/Roaming"):
        (runtime / part).mkdir(parents=True, exist_ok=True)
    cancel = lpac.HostCancelEvent()
    record = {"case": name, "ok": False, "transaction": str(plan.transaction), "failures": []}
    try:
        if name == "host_manifest":
            # The PSDrive has its root at workspace: ../ is not proof of an
            # actual parent access. Use the host-known absolute path instead.
            command = command.replace("__HOST_MANIFEST__", str(plan.transaction / "manifest.json").replace("'", "''"))
        request = lpac.build_runtime_request(command, MappedScope(scope, plan.workspace), runtime,
                                             timeout=60, cancel_name=cancel.name, git_dir=git_dir)
        readonly, containers = protections(plan)
        wrapper = {"schema": "mewcode-lpac-staged-prototype-v1", "request": request,
                   "readonly_paths": readonly, "pinned_containers": containers}
        # Transport metadata is needed by invoke_helper, but the prototype
        # validates the nested host request. Not consumed as child authority.
        wrapper.update(request_id=request["request_id"], timeout_ms=request["timeout_ms"])
        (parent / "request.json").write_text(json.dumps(wrapper, ensure_ascii=False), encoding="utf-8")
        receipt = await lpac.invoke_helper(helper, wrapper, cancel)
        receipt = replace(receipt, diagnostic_only=True)
        record["receipt"] = asdict(receipt)
        if any(marker not in receipt.stdout for marker in expected):
            record["failures"].append("expected child output missing")
        verified = staging.verify_workspace(plan, scope, receipt, policy_revision=revision)
        record["state"] = verified.state
        record["changes"] = [asdict(change) for change in verified.changes]
        record["original_unchanged"] = True
        if name == "file_roundtrip":
            record["second_version_confirmed"] = (plan.workspace / "created.txt").read_bytes().strip() == b"second"
            if not record["second_version_confirmed"]:
                record["failures"].append("host did not observe the second file version")
        if name == "git_two_commits":
            changed = {change.relative for change in verified.changes}
            if ".git/refs/heads/main" not in changed or ".git/index" not in changed:
                record["failures"].append("host-observed Git ref/index did not change")
        if name in {"protected_config", "protected_config_delete_rename", "original_write", "original_delete", "host_manifest"} and verified.changes:
            record["failures"].append("protected-write test changed the candidate")
        if publish and verified.changes and not record["failures"]:
            # Explicit synthetic-only roundtrip. The command-line output/source
            # guards above prevent this harness from touching business files.
            exact, directories = set(), set()
            for change in verified.changes:
                target = plan.source / change.relative
                if target.is_dir() or (plan.workspace / change.relative).is_dir() or not target.parent.exists():
                    ancestor = target.parent
                    while not ancestor.exists():
                        ancestor = ancestor.parent
                    if ancestor == plan.source:
                        raise ValueError("Synthetic publication needs a declared existing subdirectory lease")
                    directories.add(ancestor)
                else:
                    exact.add(target)
            # Keep the already requested outer scopes, without submitting
            # duplicate descendant scopes the lease store correctly rejects.
            directories = {path for path in directories if not any(
                path != parent and path.is_relative_to(parent) for parent in directories)}
            exact = {path for path in exact if not any(path.is_relative_to(parent) for parent in directories)}
            who = {"owner_id": "synthetic-publisher", "run_id": name, "conversation_id": "synthetic-staged-probe"}
            leases = WriteLeaseStore(parent / "leases.json", sandbox=scope.original.checker.sandbox if isinstance(scope, MappedScope) else scope.checker.sandbox)
            held = leases.acquire(**who, exact_paths=sorted(exact), directory_paths=sorted(directories), ttl_seconds=300)
            if not held["acquired"]:
                raise ValueError("Synthetic publication lease conflict")
            published = publication.publish_workspace(plan, scope, receipt, leases=leases, lease_id=held["lease"]["lease_id"],
                                                      identity=who, policy_revision=lambda: revision)
            record["publication"] = asdict(published)
            record["original_unchanged_before_publication"] = record.pop("original_unchanged")
            observed = staging._walk(plan.source, scope)
            candidate = staging._walk(plan.workspace, scope, staged=True)
            record["published_content_matches"] = [(e.relative, e.content) for e in observed] == [(e.relative, e.content) for e in candidate]
            restored = publication.recover_publication(plan, scope, leases=leases, lease_id=held["lease"]["lease_id"],
                                                      identity=who, policy_revision=lambda: revision, rollback_published=True)
            record["recovery"] = asdict(restored)
            observed_restored = staging._walk(plan.source, scope)
            # Kernel empty-directory deletion cannot retain the original dir
            # ID. Its host-owned replacement ID is separately recorded in the
            # durable recovery plan; file identities must still match exactly.
            record["original_files_identity_restored"] = [e for e in observed_restored if e.kind == "file"] == [
                e for e in plan.baseline if e.kind == "file"]
            record["original_content_restored"] = [(e.relative, e.content) for e in observed_restored] == [
                (e.relative, e.content) for e in plan.baseline]
            old_ids = {e.relative: e.inode for e in plan.baseline}
            record["reconstructed_directories"] = [e.relative for e in observed_restored
                                                    if e.kind == "directory" and e.inode != old_ids[e.relative]]
            record["original_restored"] = record["original_files_identity_restored"] and record["original_content_restored"]
            if not record["published_content_matches"] or not record["original_restored"] or restored.state != "rolled_back":
                record["failures"].append("Synthetic publish/restore roundtrip failed")
            leases.release(held["lease"]["lease_id"], **who)
        record["ok"] = not record["failures"]
    except (OSError, ValueError, RuntimeError) as error:
        record["failures"].append(str(error))
    finally:
        cancel.close()
        (parent / "result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


async def probe(output: Path, bundle: Path, run: bool, *, publish: bool = False) -> dict:
    if os.name != "nt":
        raise ValueError("Windows-only prototype")
    output = bounded_directory(output, must_exist=False)
    bundle = bounded_directory(bundle)
    if os.path.lexists(output):
        raise ValueError("Use a fresh output directory; prior evidence is never overwritten")
    if not verify_bundle(bundle)["ok"]:
        raise ValueError("Candidate Git bundle verification failed")
    output.mkdir(parents=True)
    helper, provenance = build_prototype(output)
    report = {"schema": SCHEMA, "complete": False, "production_eligible": False,
              "provenance": provenance, "cases": []}
    path = output / "report.json"
    def flush():
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    flush()
    if not run:
        report["state"] = "compiled_not_run"
        flush()
        return report
    git_dir = bundle / "git"
    source = seed_source(output, git_dir / "git.exe")
    scope = lpac.NativeScope.from_checker(build_checker(source))
    revision = hashlib.sha256(json.dumps(scope.checker.task_authorization.as_dict(), sort_keys=True).encode()).hexdigest()
    # This is a freshly generated synthetic source, not an arbitrary path
    # supplied by a model. Quote literally for the negative boundary check.
    original_file = str(source / "tracked.txt").replace("'", "''")
    cases = (
        ("file_roundtrip", "Set-Content created.txt 'first'; Get-Content created.txt; "
         "Set-Content created.txt 'second'; Get-Content created.txt", ("first", "second")),
        ("git_two_commits", "Set-Content tracked.txt 'first edit'; git add -A; if ($LASTEXITCODE) { exit $LASTEXITCODE }; "
         "git commit -m 'staged-first'; if ($LASTEXITCODE) { exit $LASTEXITCODE }; "
         "Set-Content tracked.txt 'second edit'; git add -A; if ($LASTEXITCODE) { exit $LASTEXITCODE }; "
         "git commit -m 'staged-second'; if ($LASTEXITCODE) { exit $LASTEXITCODE }; "
         "git log -2 --format=%s; git status --porcelain; exit $LASTEXITCODE", ("staged-first", "staged-second")),
        ("directory_roundtrip", "$ErrorActionPreference='Stop'; "
         "Get-Content -LiteralPath outputs/prior/nested/data.txt; "
         "Get-Item -LiteralPath outputs/prior/nested/data.txt | Select-Object -ExpandProperty Attributes; "
         "try { Remove-Item -LiteralPath outputs/prior/nested/data.txt } catch { "
         "Write-Output $_.Exception.StackTrace; throw }; "
         "Remove-Item -LiteralPath outputs/prior/nested; Remove-Item -LiteralPath outputs/prior; "
         "New-Item -ItemType Directory -Path outputs/new | Out-Null; "
         "Set-Content outputs/new/result.txt 'directory result'; Get-Content outputs/new/result.txt",
         ("directory result",)),
        # Independent native EXE control: do not replace or excuse the
        # PowerShell Remove-Item case above when its provider rejects a path.
        ("directory_git_roundtrip", "$ErrorActionPreference='Stop'; "
         "git rm -r -- outputs/prior; if ($LASTEXITCODE) { exit $LASTEXITCODE }; "
         "New-Item -ItemType Directory -Path outputs/new | Out-Null; "
         "Set-Content outputs/new/result.txt 'git directory result'; Get-Content outputs/new/result.txt",
         ("git directory result",)),
        ("protected_config", "try { Set-Content .git/config 'changed' -ErrorAction Stop; Write-Output 'CONFIG_UNEXPECTED' } "
         "catch { Write-Output 'CONFIG_REFUSED' }", ("CONFIG_REFUSED",)),
        ("protected_config_delete_rename",
         "try { Remove-Item .git/config -ErrorAction Stop; Write-Output 'DELETE_UNEXPECTED' } "
         "catch { Write-Output 'DELETE_REFUSED' }; "
         "try { Rename-Item .git/config config-moved -ErrorAction Stop; Write-Output 'RENAME_UNEXPECTED' } "
         "catch { Write-Output 'RENAME_REFUSED' }", ("DELETE_REFUSED", "RENAME_REFUSED")),
        ("original_write",
         "try { Set-Content -LiteralPath '" + original_file + "' 'changed' -ErrorAction Stop; "
         "Write-Output 'ORIGINAL_UNEXPECTED' } catch { Write-Output 'ORIGINAL_REFUSED' }",
         ("ORIGINAL_REFUSED",)),
        ("original_delete",
         "try { Remove-Item -LiteralPath '" + original_file + "' -ErrorAction Stop; "
         "Write-Output 'ORIGINAL_DELETE_UNEXPECTED' } catch { Write-Output 'ORIGINAL_DELETE_REFUSED' }",
         ("ORIGINAL_DELETE_REFUSED",)),
        ("host_manifest",
         "try { Get-Content -LiteralPath '__HOST_MANIFEST__' -ErrorAction Stop | Out-Null; "
         "Write-Output 'MANIFEST_READ_UNEXPECTED' } catch { Write-Output 'MANIFEST_READ_REFUSED' }; "
         "try { Set-Content -LiteralPath '__HOST_MANIFEST__' 'changed' -ErrorAction Stop; "
         "Write-Output 'MANIFEST_WRITE_UNEXPECTED' } catch { Write-Output 'MANIFEST_WRITE_REFUSED' }",
         ("MANIFEST_READ_REFUSED", "MANIFEST_WRITE_REFUSED")),
    )
    for name, command, expected in cases:
        report["cases"].append(await run_case(output, name, command, expected, scope, revision, helper, git_dir, publish=publish))
        flush()
    report["complete"] = True
    report["ok"] = all(case["ok"] for case in report["cases"])
    flush()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--publish", action="store_true", help="Also publish and restore synthetic changes; never enables production")
    args = parser.parse_args()
    try:
        report = asyncio.run(probe(args.output, args.bundle, args.run, publish=args.publish))
        print(json.dumps({"report": str(args.output / "report.json"), "state": report.get("state"),
                          "passed": [c["case"] for c in report["cases"] if c["ok"]],
                          "failed": [c["case"] for c in report["cases"] if not c["ok"]],
                          "production_eligible": False}, ensure_ascii=False))
        return 0 if report.get("ok") or not args.run else 1
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
