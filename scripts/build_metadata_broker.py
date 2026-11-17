"""Compile the standalone Windows metadata broker into a new empty directory."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "native/windows/MewCode.MetadataBroker.cs"


def build(output: Path) -> dict:
    if os.name != "nt" or not output.is_absolute():
        raise ValueError("Windows and an absolute output directory are required")
    output.mkdir(parents=True, exist_ok=False)
    compiler = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    target = output / "MewCode.MetadataBroker.exe"
    result = subprocess.run(
        [str(compiler), "/nologo", "/platform:x64", "/target:exe",
         "/r:System.Web.Extensions.dll", "/main:MewCode.Native.Windows.MewCodeMetadataBroker",
         "/out:" + str(target), str(SOURCE)],
        capture_output=True,
        timeout=60,
    )
    if result.returncode:
        raise RuntimeError(result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace"))
    manifest = {
        "schema": "mewcode-metadata-broker-v1",
        "protocol": "length-prefixed-utf8-json-v1",
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        "production_enabled": False,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.output), sort_keys=True))
