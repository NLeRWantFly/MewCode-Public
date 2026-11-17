import json
import os
import subprocess
from pathlib import Path

import pytest


def test_chat_fold_renderer(tmp_path):
    """对话折叠：真实渲染验收（Electron 隐藏窗口 + 回环静态服务）。"""
    root = Path(__file__).resolve().parents[1]
    electron = root / "client/node_modules/electron/dist/electron.exe"
    if not electron.exists():
        pytest.skip("Optional desktop renderer unavailable")
    env = dict(os.environ)
    env.pop("ELECTRON_RUN_AS_NODE", None)
    result = subprocess.run(
        [str(electron), str(root / "tests/chat_fold_renderer.cjs"), str(tmp_path)],
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / "chat-fold-report.json").read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert report["live"] is True
