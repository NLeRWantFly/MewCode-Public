"""Task authorization UI, in a hidden isolated renderer (never a live grant)."""
import json
import os
from pathlib import Path
import subprocess

import pytest


def test_task_authorization_layout_and_explicit_confirmation(tmp_path):
    root = Path(__file__).resolve().parents[1]
    electron = root / "client/node_modules/electron/dist/electron.exe"
    if not electron.exists():
        pytest.skip("Optional desktop renderer unavailable")
    env = dict(os.environ)
    env.pop("ELECTRON_RUN_AS_NODE", None)
    result = subprocess.run(
        [str(electron), str(root / "tests/task_authorization_dialog_dom.cjs"),
         str(tmp_path / "renderer"), str(tmp_path / "screenshots")],
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    line = next(line.removeprefix("TASK_AUTH_DOM ") for line in result.stdout.splitlines()
                if line.startswith("TASK_AUTH_DOM "))
    report = json.loads(line)
    assert all(report[key] for key in ("scope", "noImplicitGrants", "errorRecovery", "cancel", "duplicateGuard"))
    assert [item["name"] for item in report["layouts"]] == ["light", "dark", "mobile", "short"]
