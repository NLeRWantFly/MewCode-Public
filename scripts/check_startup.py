"""Offline backend gate shared by local development, CI and PyInstaller."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
STARTUP_TESTS = (
    "tests/test_orchestration_start_run.py",
    "tests/test_long_sequence_handoff.py::test_web_segments_resume_latest_conversation_and_delivery_without_provider_calls",
    "tests/test_startup_gate.py",
)


def main() -> int:
    print("Checking backend undefined names...", flush=True)
    lint = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--no-cache",
         "--select", "F821,F822,F823", "frontend", "mewcode", "scripts/check_startup.py"],
        cwd=ROOT,
    )
    if lint.returncode:
        print("Backend check failed; fix the diagnostics before building. "
              "Install development tools with: uv sync --group dev", file=sys.stderr)
        return lint.returncode

    print("Checking confirm/start and budgeted execution paths (offline)...", flush=True)
    # Never import the server against a developer's real conversations or
    # provider credentials. Each invocation gets independent stores and cache.
    with tempfile.TemporaryDirectory(prefix="mewcode-startup-") as scratch:
        isolated = Path(scratch)
        env = os.environ.copy()
        env["MEWCODE_USER_DATA_DIR"] = str(isolated / "userdata")
        env["MEWCODE_DESKTOP_PROFILE_ISOLATED"] = "1"
        env.pop("MEWCODE_DESKTOP_TOKEN", None)
        tests = subprocess.run(
            [sys.executable, "-m", "pytest", *STARTUP_TESTS, "-q",
             "--basetemp", str(isolated / "pytest"),
             "-o", f"cache_dir={isolated / 'cache'}"],
            cwd=ROOT, env=env,
        )
    if tests.returncode:
        print("Backend startup regression failed; build stopped.", file=sys.stderr)
    return tests.returncode


if __name__ == "__main__":
    raise SystemExit(main())
