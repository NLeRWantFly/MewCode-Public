"""The one-shot NUL diagnostic must self-check without writing the device ACL."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows kernel object security only")
def test_null_acl_trial_selftest_preserves_device_dacl(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    compiler = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    source = root / "native/windows/MewCode.NullAclTrial.cs"
    executable = tmp_path / "MewCode.NullAclTrial.exe"
    compile_result = subprocess.run(
        [str(compiler), "/nologo", "/platform:x64", "/r:System.Web.Extensions.dll",
         "/out:" + str(executable), str(source)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert compile_result.returncode == 0, compile_result.stdout + compile_result.stderr

    def inspect() -> str:
        result = subprocess.run([str(executable), "inspect"], capture_output=True,
                                text=True, timeout=10, check=False)
        assert result.returncode == 0, result.stderr
        return next(line for line in result.stdout.splitlines()
                    if line.startswith("nul_dacl_sha256="))

    before = inspect()
    selftest = subprocess.run([str(executable), "selftest"], capture_output=True,
                              text=True, timeout=10, check=False)
    assert selftest.returncode == 0, selftest.stderr
    assert "selftest_ok=true; no ACL write performed" in selftest.stdout
    assert inspect() == before
