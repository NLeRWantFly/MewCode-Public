"""Settings and execution buttons stay visible at narrow composer widths."""
import json
import os
from pathlib import Path
import subprocess

import pytest


def test_composer_controls_in_real_renderer(tmp_path):
    root = Path(__file__).resolve().parents[1]
    electron = root / 'client/node_modules/electron/dist/electron.exe'
    if not electron.exists():
        pytest.skip('Optional desktop renderer unavailable')
    env = dict(os.environ)
    env.pop('ELECTRON_RUN_AS_NODE', None)
    result = subprocess.run([str(electron), str(root / 'tests/composer_layout_renderer.cjs'), str(tmp_path)],
                            env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / 'report.json').read_text(encoding='utf-8'))
    assert report == [{'zoom':1,'cases':14}, {'zoom':1.25,'cases':14}, {'zoom':1.5,'cases':14}]
