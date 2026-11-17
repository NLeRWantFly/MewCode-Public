"""Compile a host LPAC bundle, without installing or enabling AUTO.

The caller explicitly chooses an output directory. Building is not acceptance
or authorization; both production readiness gates remain false.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SOURCES = (ROOT / "native/windows/MewCode.AppContainerProbe.cs",
           ROOT / "native/windows/MewCode.AppContainerRuntime.cs",
           ROOT / "native/windows/MewCode.StagedWorkspacePrototype.cs")


def build(output: Path) -> dict:
    if os.name != "nt" or not output.is_absolute():
        raise ValueError("Windows and an absolute output directory are required")
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Refusing to overwrite a nonempty bundle directory")
    compiler = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    target = output / "MewCode.AppContainerRuntime.exe"
    result = subprocess.run([str(compiler), "/nologo", "/platform:x64", "/r:System.Web.Extensions.dll",
        "/main:MewCode.Native.Windows.MewCodeAppContainerRuntime", "/out:" + str(target),
        *(str(path) for path in SOURCES)], capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError(result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace"))
    manifest = {"schema": "mewcode-lpac-bundle-v1",
                "protocols": ["mewcode-lpac-request-v1", "mewcode-lpac-staged-request-v1",
                              "mewcode-native-cleanup-v1"],
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "source_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in SOURCES},
                "production_enabled": False}
    (output / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.output), sort_keys=True))
