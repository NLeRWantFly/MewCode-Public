"""Build the x64 host network-fence candidate; this never installs WFP rules."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "native/windows/MewCode.NetworkFence.cs"
BINARY_NAME = "MewCode.NetworkFence.exe"
EXPECTED_LAYOUT = {
    "FWPM_DISPLAY_DATA0": 16,
    "FWP_BYTE_BLOB": 16,
    "FWPM_PROVIDER0": 64,
    "FWPM_SUBLAYER0": 72,
    "FWP_VALUE0": 16,
    "FWP_CONDITION_VALUE0": 16,
    "FWPM_FILTER_CONDITION0": 40,
    "FWPM_ACTION0": 20,
    "FWPM_FILTER0": 200,
    "FWP_V6_ADDR_AND_MASK": 17,
}


def _workspace_output(value: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    if path.is_symlink():
        raise ValueError("The candidate output cannot be a symbolic link")
    path = path.resolve()
    try:
        path.relative_to(ROOT.resolve())
    except ValueError as error:
        raise ValueError("The candidate output must stay inside the workspace") from error
    return path


def build(output: Path) -> dict:
    if os.name != "nt":
        raise ValueError("The host network-fence candidate can only be compiled on Windows")
    output = _workspace_output(output)
    if not SOURCE.is_file():
        raise FileNotFoundError("The native network-fence source is missing")
    output.mkdir(parents=True, exist_ok=True)
    if output.is_symlink() or any(output.iterdir()):
        raise ValueError("Refusing to overwrite a linked or nonempty candidate directory")

    system_root = Path(os.environ.get("SystemRoot", ""))
    compiler = system_root / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not compiler.is_file():
        raise FileNotFoundError("The x64 .NET Framework C# compiler is unavailable")
    binary = output / BINARY_NAME
    compile_args = [
        str(compiler), "/nologo", "/platform:x64", "/r:System.Web.Extensions.dll",
        "/main:MewCode.Native.Windows.MewCodeNetworkFence", "/out:" + str(binary), str(SOURCE),
    ]
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    compiled = subprocess.run(compile_args, capture_output=True, timeout=60,
                              creationflags=no_window)
    if compiled.returncode != 0:
        stdout = compiled.stdout.decode(errors="replace")
        stderr = compiled.stderr.decode(errors="replace")
        raise RuntimeError("Network-fence C# compilation failed: " + stdout + stderr)
    if not binary.is_file():
        raise RuntimeError("The C# compiler reported success without producing the candidate executable")

    # This execution only reports CLR struct sizes and offsets. It does not load
    # Fwpuclnt, open the WFP engine, or touch loopback policy.
    layout_run = subprocess.run([str(binary), "--validate-layouts"], capture_output=True,
                                timeout=20, creationflags=no_window)
    if layout_run.returncode != 0:
        detail = layout_run.stdout.decode(errors="replace") + layout_run.stderr.decode(errors="replace")
        raise RuntimeError("Native WFP layout validation failed: " + detail)
    try:
        layout_report = json.loads(layout_run.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Native WFP layout validation did not return valid JSON") from error
    if (not isinstance(layout_report, dict)
            or layout_report.get("schema") != "mewcode-network-fence-layout-v1"
            or layout_report.get("ok") is not True
            or layout_report.get("layout") != EXPECTED_LAYOUT):
        raise RuntimeError("Native WFP layout report does not match Windows SDK 10.0.26100.0 x64")

    manifest = {
        "schema": "mewcode-network-fence-candidate-v1",
        "binary": BINARY_NAME,
        "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "source_sha256": {SOURCE.name: hashlib.sha256(SOURCE.read_bytes()).hexdigest()},
        "layout_validation": layout_report,
        "wfp_install_performed": False,
        "loopback_exemption_changed": False,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(build(args.output), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
