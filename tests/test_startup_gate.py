"""The packaging gate must stop before building when verification fails."""
from pathlib import Path
import os
import runpy
import shutil
import subprocess
import sys
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_gate_catches_an_undefined_name_in_an_unvisited_ui_branch(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(ROOT / "scripts/check_startup.py", scripts / "check_startup.py")
    (tmp_path / "frontend").mkdir()
    (tmp_path / "mewcode").mkdir()
    (tmp_path / "frontend/server.py").write_text(
        "def confirm(bindings):\n    if bindings:\n        return ModelRef.from_value(bindings)\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(scripts / "check_startup.py")],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode != 0
    assert "F821" in result.stdout and "ModelRef" in result.stdout
    assert "Checking confirm/start" not in result.stdout


def test_gate_propagates_test_failure_and_isolates_runtime_data(monkeypatch):
    from scripts import check_startup

    monkeypatch.setenv("MEWCODE_USER_DATA_DIR", "real-profile-must-not-be-used")
    monkeypatch.setenv("MEWCODE_DESKTOP_TOKEN", "inherited-token-must-not-be-used")
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0 if len(calls) == 1 else 1)
    monkeypatch.setattr(subprocess, "run", run)
    assert check_startup.main() == 1
    assert len(calls) == 2
    env = calls[1][1]["env"]
    assert env["MEWCODE_USER_DATA_DIR"] != os.environ["MEWCODE_USER_DATA_DIR"]
    assert env["MEWCODE_DESKTOP_PROFILE_ISOLATED"] == "1"
    assert "MEWCODE_DESKTOP_TOKEN" not in env
    assert os.environ["MEWCODE_DESKTOP_TOKEN"] == "inherited-token-must-not-be-used"
    assert not Path(env["MEWCODE_USER_DATA_DIR"]).parent.exists()


def test_sidecar_spec_stops_before_collecting_or_building_on_a_failed_gate(monkeypatch):
    hooks = ModuleType("PyInstaller.utils.hooks")
    hooks.collect_all = lambda _name: pytest.fail("Packaging ran after the gate failed")
    monkeypatch.setitem(sys.modules, "PyInstaller.utils.hooks", hooks)
    calls = []
    def failed_gate(command, **kwargs):
        calls.append((command, kwargs))
        raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(subprocess, "run", failed_gate)
    with pytest.raises(subprocess.CalledProcessError):
        runpy.run_path(str(ROOT / "client/mewcode-backend.spec"),
                       init_globals={"SPECPATH": str(ROOT / "client")})
    assert len(calls) == 1
    assert calls[0][0] == [sys.executable, str(ROOT / "scripts/check_startup.py")]
    assert calls[0][1] == {"cwd": ROOT, "check": True}
