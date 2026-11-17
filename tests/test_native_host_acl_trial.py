"""Compile and exercise joint-trial checks without writing any system ACL."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


@pytest.fixture(scope="module")
def trial_exe(tmp_path_factory):
    if os.name != "nt":
        pytest.skip("Windows security descriptors only")
    root = Path(__file__).resolve().parents[1]
    compiler = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    directory = tmp_path_factory.mktemp("joint-acl-trial")
    output = directory / "MewCode.NativeHostAclTrial.exe"
    result = subprocess.run([
        str(compiler), "/nologo", "/platform:x64", "/r:System.Web.Extensions.dll",
        "/out:" + str(output), str(root / "native/windows/MewCode.NativeHostAclTrial.cs"),
    ], capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return output


def run(executable: Path, *arguments: str):
    return subprocess.run([str(executable), *arguments], capture_output=True,
                          text=True, timeout=10, check=False)


def test_pure_delta_selftest_preserves_both_system_descriptors(trial_exe):
    before = run(trial_exe, "inspect")
    assert before.returncode == 0, before.stderr
    assert len(before.stdout.splitlines()) == 2
    assert "nul_dacl_sha256=" in before.stdout
    assert "drive_root_dacl_sha256=" in before.stdout
    check = run(trial_exe, "selftest")
    assert check.returncode == 0, check.stderr
    assert check.stdout.strip() == "selftest_ok=true; system_acl_writes=0"
    after = run(trial_exe, "inspect")
    assert after.returncode == 0, after.stderr
    assert after.stdout == before.stdout


def test_invalid_arguments_and_manifest_validation_do_not_mutate(trial_exe, tmp_path):
    before = run(trial_exe, "inspect")
    invalid = run(trial_exe, "no-such-command")
    assert invalid.returncode == 2
    assert "Usage:" in invalid.stderr
    untrusted = run(trial_exe, "validate", str(tmp_path / "missing.json"), "0" * 64)
    assert untrusted.returncode == 1
    assert run(trial_exe, "inspect").stdout == before.stdout


def test_system_trial_is_not_a_production_activation(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    source = (root / "native/windows/MewCode.NativeHostAclTrial.cs").read_text(encoding="utf-8")
    assert 'private const int RootMask = 0x00120088;' in source
    assert 'SetKernelObjectSecurity(handle, DaclOnly, replacement)' in source
    assert 'AceFlags.None, AceQualifier.AccessAllowed' in source
    assert '"production_authorized", false' in source
    assert 'RestoreAll(plan)' in source
    assert 'FileMode.CreateNew' in source
    # Pin the gate to an empty data root so this never depends on host state.
    monkeypatch.setenv("MEWCODE_USER_DATA_DIR", str(tmp_path))
    from mewcode.sandbox.windows import native_fastpath_ready, native_runtime_ready
    assert native_fastpath_ready() is False
    assert native_runtime_ready() is False


def test_directory_writer_preserves_controls_and_never_propagates_to_witness(trial_exe, tmp_path):
    # The executable refuses non-synthetic inputs. All ACL writes here are on
    # disposable children; the two real system descriptors remain unchanged.
    fixture = tmp_path / ".tmp" / "mewcode-lpac-probe-directory-selftest"
    fixture.mkdir(parents=True)
    (fixture / ".mewcode-synthetic-fixture").write_bytes(b"mewcode-synthetic-only-v1\n")
    before = run(trial_exe, "inspect")
    tested = run(trial_exe, "directory-selftest", str(fixture))
    assert tested.returncode == 0, tested.stderr
    assert "child_unchanged=true; restore_verified=true; system_acl_writes=0" in tested.stdout
    assert run(trial_exe, "inspect").stdout == before.stdout


def test_trial_rehearses_restore_before_first_system_backup():
    root = Path(__file__).resolve().parents[1]
    source = (root / "native/windows/MewCode.NativeHostAclTrial.cs").read_text(encoding="utf-8")
    trial = source.split("private static int Trial(", 1)[1].split("private static int SelfTest(", 1)[0]
    assert trial.index("DirectorySelfTest(root)") < trial.index("var backups")
    assert "NtSetSecurityObject(handle, DaclOnly | protection, Bytes(sd))" in source
