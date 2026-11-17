"""Compare fixed Windows PowerShell probes under the synthetic child environment.

This is a host-only control. It never creates an AppContainer and cannot turn on
AUTO. The only compiled program is the repository's fixed synthetic payload.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_SOURCE = ROOT / "native/windows/MewCode.SyntheticPayload.cs"
PATHEXT_CONTROL = ".COM;.EXE;.BAT;.CMD"
ENVIRONMENT_KEYS = frozenset(
    {
        "SystemRoot", "WINDIR", "SystemDrive", "COMSPEC", "PATH",
        "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    }
)
HOST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetPathRoot([Environment]::CurrentDirectory)
Write-Output ('PATHEXT_PRESENT_' + [bool]($env:PATHEXT))
Write-Output ('PATHEXT_EQUALS_CONTROL_' + ($env:PATHEXT -eq '.COM;.EXE;.BAT;.CMD'))
Write-Output ('CURRENT_ROOT_LOGICAL_' + ([Environment]::GetLogicalDrives() -contains $root))
Write-Output ('CURRENT_ROOT_EXISTS_' + [IO.Directory]::Exists($root))
$drives = @(Get-PSDrive -PSProvider FileSystem)
Write-Output ('CURRENT_ROOT_PSDRIVE_' + [bool]($drives | Where-Object { $_.Name -eq $root.Substring(0, 1) }))
$command = Get-Command $env:ComSpec -ErrorAction Stop
Write-Output ('COMSPEC_COMMAND_APPLICATION_' + ($command.CommandType -eq 'Application'))
& $env:ComSpec /d /c echo PS_NATIVE_CHILD_OK
""".strip()

EXPECTED_MARKERS = (
    "PS_DIRECT_CHILD_EXIT_CODE_0",
    "PS_DIRECT_CHILD_STDOUT_MARKER_PRESENT_TRUE",
    "PS_DIRECT_CHILD_STDERR_EMPTY_TRUE",
    "PS_COMSPEC_PRESENT_TRUE",
    "PS_INIT_PIPELINE_INVOKE_RETURNED",
    "PS_NATIVE_ENGINE_OK",
    "PS_NATIVE_CHILD_OK",
    "PS_PIPELINE_STATE_Completed",
    "PS_PIPELINE_FAILURE_REASON_NONE",
    "PS_INIT_PIPELINE_DONE",
    "PS_INIT_RUNSPACE_DISPOSE_AFTER",
    "PS_INIT_DONE",
    "PATHEXT_PRESENT_True",
    "PATHEXT_PRESENT_False",
    "PATHEXT_EQUALS_CONTROL_True",
    "PATHEXT_EQUALS_CONTROL_False",
    "CURRENT_ROOT_LOGICAL_True",
    "CURRENT_ROOT_EXISTS_True",
    "CURRENT_ROOT_PSDRIVE_True",
    "COMSPEC_COMMAND_APPLICATION_True",
)


def build_environment(system_root: Path, fixture: Path, *, pathext: bool) -> dict[str, str]:
    """Build only the ten existing probe keys plus optional fixed PATHEXT."""
    system32 = system_root / "System32"
    profile = fixture / "profile"
    environment = {
        "SystemRoot": str(system_root),
        "WINDIR": str(system_root),
        "SystemDrive": system_root.drive,
        "COMSPEC": str(system32 / "cmd.exe"),
        "PATH": str(system32),
        "TEMP": str(fixture / "runtime"),
        "TMP": str(fixture / "runtime"),
        "USERPROFILE": str(profile),
        "APPDATA": str(profile / "AppData/Roaming"),
        "LOCALAPPDATA": str(profile / "AppData/Local"),
    }
    assert set(environment) == ENVIRONMENT_KEYS
    if pathext:
        environment["PATHEXT"] = PATHEXT_CONTROL
    return environment


def summarize_process(result: subprocess.CompletedProcess[str] | subprocess.TimeoutExpired) -> dict[str, object]:
    """Keep only fixed markers; process output cannot become a credential report."""
    if isinstance(result, subprocess.TimeoutExpired):
        return {"timed_out": True, "exit_code": None, "markers": {name: False for name in EXPECTED_MARKERS}}
    lines = set(result.stdout.splitlines())
    markers = {name: name in lines for name in EXPECTED_MARKERS}
    return {"timed_out": False, "exit_code": result.returncode, "markers": markers}


def run_fixed(command: list[str], *, cwd: Path, environment: dict[str, str]) -> dict[str, object]:
    try:
        result = subprocess.run(
            command, cwd=cwd, env=environment, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return summarize_process(exc)
    return summarize_process(result)


def main() -> int:
    if sys.platform != "win32":
        raise SystemExit("Windows-only synthetic host control")
    system_root = Path(os.environ["SystemRoot"])
    compiler = system_root / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    powershell = system_root / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if not (compiler.is_file() and powershell.is_file() and PAYLOAD_SOURCE.is_file()):
        raise SystemExit("Fixed local compiler, Windows PowerShell, or synthetic source is unavailable")

    (ROOT / ".tmp").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mewcode-ps-runtime-", dir=ROOT / ".tmp") as directory:
        fixture = Path(directory)
        for relative in ("runtime", "profile/AppData/Roaming", "profile/AppData/Local", "workspace"):
            (fixture / relative).mkdir(parents=True)
        workspace = fixture / "workspace"
        payload = workspace / "synthetic-payload.exe"
        clean_environment = build_environment(system_root, fixture, pathext=False)
        compiled = subprocess.run(
            [str(compiler), "/nologo", "/out:" + str(payload), str(PAYLOAD_SOURCE)],
            cwd=workspace, env=clean_environment, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
        )
        if compiled.returncode != 0 or not payload.is_file():
            detail = (compiled.stdout + compiled.stderr).strip().replace(str(fixture), "<synthetic fixture>")
            raise SystemExit(
                "Fixed synthetic payload compilation failed "
                f"(exit {compiled.returncode}): {detail[:500]}"
            )

        encoded = base64.b64encode(HOST_SCRIPT.encode("utf-16le")).decode("ascii")
        cases: dict[str, object] = {}
        for name, pathext in (("ten_keys_no_pathext", False), ("eleven_keys_with_pathext", True)):
            environment = build_environment(system_root, fixture, pathext=pathext)
            cases[name] = {
                "synthetic_runspace": run_fixed([str(payload), "powershell-init"], cwd=workspace, environment=environment),
                "powershell_cli": run_fixed(
                    [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                    cwd=workspace, environment=environment,
                ),
            }
        print(json.dumps({"host_control_only": True, "cases": cases}, indent=2))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
