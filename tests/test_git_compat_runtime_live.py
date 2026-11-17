"""Opt-in functional witness through the native runtime's PowerShell entry.

No network, real repository, system installation, or production gate changes.
Both explicit environment variables are required; all data lives in basetemp.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from mewcode.permissions.checker import PermissionChecker
from mewcode.permissions.dangerous import DangerousCommandDetector
from mewcode.permissions.modes import PermissionMode
from mewcode.permissions.rules import RuleEngine
from mewcode.permissions.sandbox import PathSandbox
from mewcode.sandbox import lpac_runtime as lpac
from scripts import probe_git_compat as probe

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not os.environ.get("MEWCODE_TEST_GIT_COMPAT_BUILD")
    or not os.environ.get("MEWCODE_TEST_LPAC_RUNTIME_EXE"),
    reason="explicit isolated Git build and native runtime opt-in required",
)


@pytest.mark.asyncio
async def test_git_compat_via_native_powershell(tmp_path, monkeypatch):
    build = probe.checked_build(Path(os.environ["MEWCODE_TEST_GIT_COMPAT_BUILD"]))
    manifest = probe.read_json(build / "build-manifest.json")
    probe.verify_manifest(build, manifest)
    helper = lpac.checked_path(Path(os.environ["MEWCODE_TEST_LPAC_RUNTIME_EXE"]), file=True)
    helper_manifest = probe.read_json(helper.parent / "manifest.json")
    assert helper_manifest["schema"] == "mewcode-lpac-bundle-v1"
    assert helper_manifest["production_enabled"] is False
    assert probe.sha(helper) == helper_manifest["sha256"]
    assert tmp_path.resolve().is_relative_to(probe.ROOT / ".tmp")
    workspace = tmp_path / "中文 workspace"
    workspace.mkdir()
    runtime = tmp_path / "private-host"
    for relative in ("temp", "profile/AppData/Local", "profile/AppData/Roaming"):
        (runtime / relative).mkdir(parents=True)
    binary_dir = workspace / "git-compat-bin"
    binary_dir.mkdir()
    sources = [build / "patched/git.exe", *(build / name for name in manifest["dlls"])]
    copied = []
    for source in sources:
        target = binary_dir / source.name
        shutil.copyfile(source, target)
        assert probe.sha(source) == probe.sha(target)
        copied.append(target)
    pins = {target: probe.sha(target) for target in copied}
    repo = workspace / "repository"
    repo.mkdir()
    template = workspace / "empty-template"
    template.mkdir()
    config = workspace / "empty.gitconfig"
    config.write_text("# synthetic config\n", encoding="utf-8")
    (repo / "sample.txt").write_text("SYNTHETIC_GIT_RUNTIME_INPUT\n", encoding="utf-8")
    system = Path(os.environ["SystemRoot"])
    host_env = {
        "SystemRoot": str(system), "WINDIR": str(system),
        # Without SystemDrive, the Windows thumbnail cache writes a literal
        # "%SystemDrive%" directory into the repository (reproduced 2026-09-26).
        "SystemDrive": system.drive,
        "PATH": str(system / "System32"), "TEMP": str(runtime / "temp"),
        "TMP": str(runtime / "temp"), "USERPROFILE": str(runtime / "profile"),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(config),
        "GIT_TERMINAL_PROMPT": "0",
    }
    git = binary_dir / "git.exe"
    initialized = subprocess.run(
        [str(git), "-C", str(repo), "-c", "init.defaultBranch=main",
         "init", "--quiet", "--template=" + str(template)],
        env=host_env, capture_output=True, timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert initialized.returncode == 0, initialized.stderr
    checker = PermissionChecker(DangerousCommandDetector(), PathSandbox(str(workspace)),
                                RuleEngine(), PermissionMode.AUTO)
    scope = lpac.NativeScope.from_checker(checker)
    commands = [
        ("version", r".\git-compat-bin\git.exe --version; exit $LASTEXITCODE", probe.VERSION),
        ("status", r".\git-compat-bin\git.exe -C .\repository status --porcelain; exit $LASTEXITCODE", "?? sample.txt\n"),
        # NativeScope explicitly makes .git read-only. This is a policy
        # negative, not evidence that add/commit work in the production path.
        ("add_denied", r".\git-compat-bin\git.exe -C .\repository add sample.txt; exit $LASTEXITCODE", ""),
    ]
    metadata_before = {p.relative_to(repo).as_posix(): probe.sha(p)
                       for p in (repo / ".git").rglob("*") if p.is_file()}
    receipts = {}
    decode = lpac.decode_receipt

    def capture_receipt(raw, request_id, helper_exit):
        # Preserve raw evidence without changing any production validation.
        (runtime / (request_id + ".helper.stdout.bin")).write_bytes(raw)
        return decode(raw, request_id, helper_exit)

    monkeypatch.setattr(lpac, "decode_receipt", capture_receipt)
    for name, command, expected in commands:
        cancel = lpac.HostCancelEvent()
        try:
            request = lpac.build_runtime_request(command, scope, runtime,
                                                 timeout=20, cancel_name=cancel.name)
            # Keep the existing host allowlist unchanged. Its isolated profile
            # has no user config; only the host init control uses config above.
            raw_request = json.dumps(request, ensure_ascii=False, sort_keys=True)
            (runtime / (request["request_id"] + ".request.json")).write_text(
                raw_request, encoding="utf-8")
            result = await lpac.invoke_helper(helper, request, cancel)
            receipts[name] = {"request_sha256": hashlib.sha256(raw_request.encode()).hexdigest(),
                              "result": result.__dict__}
            (runtime / "git-compat-powershell.receipts.json").write_text(
                json.dumps(receipts, ensure_ascii=False, indent=2), encoding="utf-8")
            assert result.cleanup_confirmed is True, result
            assert result.output_truncated is False, result
            if name == "add_denied":
                assert not scope.allows(repo / ".git", write=True)
                assert result.exit_code == 128, result
                assert "/.git/index.lock" in result.stderr and "Permission denied" in result.stderr
                assert result.stdout == ""
                assert not (repo / ".git/index.lock").exists()
            else:
                assert result.exit_code == 0, result
                assert result.stdout.replace("\r\n", "\n") == expected, result
                assert result.stderr == "", result
        finally:
            cancel.close()
        assert all(probe.sha(path) == digest for path, digest in pins.items())
    assert probe.sha(helper) == helper_manifest["sha256"]
    assert {p.relative_to(repo).as_posix(): probe.sha(p)
            for p in (repo / ".git").rglob("*") if p.is_file()} == metadata_before
    probe.verify_manifest(build, manifest)
