import json
import os
from pathlib import Path
import subprocess

import pytest


def test_expert_calls_in_real_hidden_renderer(tmp_path):
    root = Path(__file__).resolve().parents[1]
    electron = root / "client/node_modules/electron/dist/electron.exe"
    if not electron.exists():
        pytest.skip("Optional desktop renderer is not installed")
    env = dict(os.environ)
    env.pop("ELECTRON_RUN_AS_NODE", None)
    result = subprocess.run([str(electron), str(root / "tests/expert_calls_dom.cjs"), str(tmp_path / "renderer")],
                            env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    output = next(line.removeprefix("DOM_RESULT ") for line in result.stdout.splitlines() if line.startswith("DOM_RESULT "))
    report = json.loads(output)
    assert report == {"legacyCardRemoved": True, "liveWithoutMode": True, "historyVisible": True,
                      "blockedClearly": True, "toolRows": 2, "nodeScoped": True, "attemptScoped": True,
                      "publicProgress": True}
