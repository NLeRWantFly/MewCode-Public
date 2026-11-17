from __future__ import annotations

import pytest

from mewcode.teams.backend_detect import BackendDetectionError, detect_backend
from mewcode.teams.fsio import atomic_write_text, quarantine_corrupt_file
from mewcode.teams.models import AgentTeam, BackendType, TeamConfigError


def test_atomic_write_creates_parents_and_replaces(tmp_path):
    target = tmp_path / "deep" / "nested" / "file.json"
    atomic_write_text(target, "v1")
    assert target.read_text(encoding="utf-8") == "v1"
    atomic_write_text(target, "v2")
    assert target.read_text(encoding="utf-8") == "v2"
    # 不残留临时文件
    leftovers = [p for p in target.parent.iterdir() if p.name != "file.json"]
    assert leftovers == []


def test_quarantine_corrupt_file(tmp_path):
    bad = tmp_path / "config.json"
    bad.write_text("{broken", encoding="utf-8")
    backup = quarantine_corrupt_file(bad)
    assert backup is not None
    assert not bad.exists()
    assert backup.exists()
    assert ".corrupt-" in backup.name


def test_team_load_quarantines_corrupt_config(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(TeamConfigError):
        AgentTeam.load(str(cfg))
    # 原文件被隔离，不会让团队永久卡死
    assert not cfg.exists()
    assert any(".corrupt-" in p.name for p in tmp_path.iterdir())


def test_detect_backend_defaults_in_process():
    assert detect_backend() is BackendType.IN_PROCESS
    assert detect_backend("in-process") is BackendType.IN_PROCESS


def test_detect_backend_rejects_unwired_pane_modes():
    with pytest.raises(BackendDetectionError):
        detect_backend("tmux")
    with pytest.raises(BackendDetectionError):
        detect_backend("iterm2")
