"""Opt-in local PowerShell/Git wrapper contract; no LPAC acceptance claim."""
import os
from pathlib import Path
import subprocess

import pytest

from mewcode.sandbox.lpac_runtime import _git_capture_wrapper

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not os.environ.get("MEWCODE_TEST_CAPTURE_GIT"),
    reason="explicit local Git wrapper witness required")


@pytest.mark.parametrize("trailing_separator", [False, True])
@pytest.mark.parametrize("constrained", [False, True])
def test_wrapper_follows_powershell_location_and_preserves_arguments(tmp_path, trailing_separator, constrained):
    git = Path(os.environ["MEWCODE_TEST_CAPTURE_GIT"]).resolve(strict=True)
    repo = tmp_path / "中文 repository 空格"
    repo.mkdir()
    templates = tmp_path / "empty-template"
    templates.mkdir()
    config = tmp_path / "isolated.gitconfig"
    config.write_text("", encoding="utf-8")
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(config),
           "GIT_TERMINAL_PROMPT": "0", "TEMP": str(tmp_path), "TMP": str(tmp_path),
           "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path)}
    initialized = subprocess.run([str(git), "init", "--quiet", "--template=" + str(templates), str(repo)],
                                 env=env, capture_output=True, timeout=20)
    assert initialized.returncode == 0, initialized.stderr
    (repo / "中文 file.txt").write_text("fixture", encoding="utf-8")
    quoted_repo = str(repo).replace("'", "''") + ("\\" if trailing_separator else "")
    # The OS cwd and PowerShell provider cwd deliberately differ.
    bootstrap = "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
    if constrained:
        bootstrap += "$ExecutionContext.SessionState.LanguageMode = 'ConstrainedLanguage'; "
    command = bootstrap + _git_capture_wrapper(git) + (
        f"Set-Location -LiteralPath '{quoted_repo}'; "
        "git -c core.quotepath=false status --porcelain; "
        r'''git -c 'test.value=中文 "quoted" value\' config --get test.value; '''
        f"git -C '{quoted_repo}' rev-parse --is-inside-work-tree; exit $LASTEXITCODE")
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-Command", command],
                            cwd=tmp_path, env=env, capture_output=True, timeout=30)
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")
    assert result.returncode == 0, stderr
    assert "中文 file.txt" in stdout, (stdout, stderr)
    assert '中文 "quoted" value\\' in stdout, (stdout, stderr)
    assert "true" in stdout.splitlines(), (stdout, stderr)
    failed = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-Command",
                             bootstrap + _git_capture_wrapper(git) +
                             f"git -C '{quoted_repo}' rev-parse --verify missing-ref; exit $LASTEXITCODE"],
                            cwd=tmp_path, env=env, capture_output=True, timeout=30)
    assert failed.returncode == 128, failed.stderr
    assert b"Needed a single revision" in failed.stderr
    assert b"MethodInvocationNotSupportedInConstrainedLanguage" not in failed.stderr
