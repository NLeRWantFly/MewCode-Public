import json
import os
from pathlib import Path
import subprocess

import pytest


def test_chat_bubbles_and_read_only_help_in_renderer(tmp_path):
    root = Path(__file__).resolve().parents[1]
    electron = root / "client/node_modules/electron/dist/electron.exe"
    if not electron.exists():
        pytest.skip("Optional desktop renderer unavailable")
    env = dict(os.environ)
    env.pop("ELECTRON_RUN_AS_NODE", None)
    result = subprocess.run([str(electron), str(root / "tests/chat_layout_renderer.cjs"), str(tmp_path)],
                            env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / "chat-report.json").read_text(encoding="utf-8"))
    assert len(report["cases"]) == 6
    assert all(case["nodeModels"] for case in report["cases"])
    assert report["readOnlyHelp"]
    assert report["progressDashboard"]
    assert report["autoCards"]["kinds"] == 3 and report["autoCards"]["coalesced"]
    assert len(report["autoLayouts"]) == 12
    assert all(report['askUserChoices'].values())
    assert len(report['questionLayouts']) == 4
