"""Keep canvas pointer regressions in the concentrated workflow suite."""
from pathlib import Path
import subprocess


def test_canvas_pointer_interactions():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ['node', str(Path(__file__).with_name('canvas_interactions.cjs'))],
        cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '12 cases passed' in result.stdout
