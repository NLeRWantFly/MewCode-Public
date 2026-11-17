import json
import os
from pathlib import Path
import subprocess

import pytest


def test_chat_scroll_and_live_output_in_renderer(tmp_path):
    root = Path(__file__).resolve().parents[1]
    electron = root / 'client/node_modules/electron/dist/electron.exe'
    if not electron.exists():
        pytest.skip('Optional desktop renderer is not installed')
    env = dict(os.environ)
    env.pop('ELECTRON_RUN_AS_NODE', None)
    result = subprocess.run([str(electron), str(root / 'tests/chat_scroll_renderer.cjs'), str(tmp_path / 'renderer')],
                            env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(next(line.removeprefix('SCROLL_RESULT ') for line in result.stdout.splitlines()
                             if line.startswith('SCROLL_RESULT ')))
    assert report == dict(conversationTail=True, graphTail=True, readingAnchor=True, conversationRestore=True,
                          hiddenHistory=True, liveText=True, readingPreserved=True)
