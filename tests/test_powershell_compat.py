"""Real Windows runspace tests; not a substitute for native LPAC acceptance."""
import os
from pathlib import Path
import subprocess

import pytest

from mewcode.sandbox.powershell_compat import remove_item_wrapper

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell required")


def run_script(root, script, constrained):
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    boot = "$ErrorActionPreference='Stop'; "
    if constrained:
        boot += "$ExecutionContext.SessionState.LanguageMode='ConstrainedLanguage'; "
    result = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-Command",
                             boot + remove_item_wrapper() + script], cwd=root, capture_output=True, timeout=20,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return result


@pytest.mark.parametrize("constrained", [False, True])
@pytest.mark.parametrize("command", [
    "Remove-Item -LiteralPath '中文 [literal].txt'",
    "'中文 [literal].txt' | Remove-Item -Force",
    "Get-Item -LiteralPath '中文 [literal].txt' | Remove-Item",
    "Remove-Item -Path *.txt -Exclude keep.txt",
    "rm -LiteralPath '中文 [literal].txt' -Confirm:$false",
])
def test_remove_literal_wildcard_pipeline_and_restore_location(tmp_path, constrained, command):
    (tmp_path / "中文 [literal].txt").write_text("synthetic")
    (tmp_path / "keep.txt").write_text("retained")
    # String pipeline to -Path uses wildcard semantics just like the cmdlet.
    if command.startswith("'中文"):
        command = "'remove-me.txt' | Remove-Item -Force"
        (tmp_path / "remove-me.txt").write_text("synthetic")
        removed = tmp_path / "remove-me.txt"
    else:
        removed = tmp_path / "中文 [literal].txt"
    result = run_script(tmp_path, "$before=(Get-Location).Path; " + command +
                        "; if ((Get-Location).Path -ne $before) { throw 'Location changed' }; 'DONE'", constrained)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert not removed.exists()
    assert (tmp_path / "keep.txt").read_text() == "retained"
    assert b"DONE" in result.stdout


@pytest.mark.parametrize("constrained", [False, True])
@pytest.mark.parametrize("scenario", ["whatif", "current", "ancestor", "missing", "readonly", "other_provider"])
def test_remove_guards_and_failure_restore(tmp_path, constrained, scenario):
    path = tmp_path / "file.txt"
    path.write_text("keep")
    nested = tmp_path / "nested"
    nested.mkdir()
    scripts = {
        "whatif": "Remove-Item -LiteralPath file.txt -WhatIf; if (!(Test-Path file.txt)) { throw 'Removed' }",
        "current": "try { Remove-Item -LiteralPath . -Recurse; throw 'UNEXPECTED' } catch { if ($_.Exception.Message -eq 'UNEXPECTED') { throw } }",
        "ancestor": "Push-Location nested; try { Remove-Item -LiteralPath .. -Recurse; throw 'UNEXPECTED' } catch { if ($_.Exception.Message -eq 'UNEXPECTED') { throw } }; Pop-Location",
        "missing": "try { Remove-Item -LiteralPath missing -ErrorAction Stop; throw 'UNEXPECTED' } catch { if ($_.Exception.Message -eq 'UNEXPECTED') { throw } }",
        "readonly": "Set-ItemProperty file.txt -Name IsReadOnly -Value $true; try { Remove-Item file.txt -ErrorAction Stop; throw 'UNEXPECTED' } catch { if ($_.Exception.Message -eq 'UNEXPECTED') { throw } }; Set-ItemProperty file.txt -Name IsReadOnly -Value $false",
        "other_provider": "$global:MewSyntheticVariable='test'; Remove-Item Variable:\\MewSyntheticVariable; if (Test-Path Variable:\\MewSyntheticVariable) { throw 'Not removed' }",
    }
    result = run_script(tmp_path, "$before=(Get-Location).Path; " + scripts[scenario] +
                        "; if ((Get-Location).Path -ne $before) { throw 'Location changed' }; 'GUARD_OK'", constrained)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert path.read_text() == "keep"
    assert b"GUARD_OK" in result.stdout


@pytest.mark.parametrize("constrained", [False, True])
def test_recursive_remove_and_stream_parameter(tmp_path, constrained):
    root = tmp_path / "remove-tree"
    (root / "child").mkdir(parents=True)
    (root / "child/file.txt").write_bytes(b"synthetic")
    keep = tmp_path / "keep.txt"
    keep.write_bytes(b"keep")
    stream = Path(str(keep) + ":test-stream")
    stream.write_bytes(b"stream")
    result = run_script(tmp_path, "Remove-Item -LiteralPath remove-tree -Recurse -Confirm:$false; "
                        "Remove-Item -LiteralPath keep.txt -Stream test-stream; 'STREAM_OK'", constrained)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert not root.exists()
    assert keep.read_bytes() == b"keep"
    assert not stream.exists()
    assert b"STREAM_OK" in result.stdout
