from __future__ import annotations

from pathlib import Path
import os

# Opt-in delivery gate: isolate imports as well as fixtures, and fail any
# accidental external connection instead of consulting a real provider.
if os.environ.get("MEWCODE_OFFLINE_TEST") == "1":
    import socket
    _offline_home = Path(__file__).resolve().parents[1] / ".tmp" / ("paradigm-offline-home-" + str(os.getpid()))
    _offline_home.mkdir(parents=True, exist_ok=True)
    Path.home = classmethod(lambda cls: _offline_home)
    os.environ["MEWCODE_USER_DATA_DIR"] = str(_offline_home / "userdata")
    os.environ["MEWCODE_DESKTOP_PROFILE_ISOLATED"] = "1"
    os.environ.pop("MEWCODE_DESKTOP_TOKEN", None)
    _original_connect = socket.socket.connect
    _original_connect_ex = socket.socket.connect_ex
    def _offline_connect(sock, address):
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise OSError("Offline test blocked an external network connection")
        return _original_connect(sock, address)
    def _offline_connect_ex(sock, address):
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise OSError("Offline test blocked an external network connection")
        return _original_connect_ex(sock, address)
    socket.socket.connect = _offline_connect
    socket.socket.connect_ex = _offline_connect_ex

import pytest

from mewcode.teams.registry import AgentNameRegistry


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """把 Path.home() 指到 tmp_path，让 ~/.mewcode/teams 落进临时目录。"""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def reset_name_registry():
    AgentNameRegistry.reset()
    yield
    AgentNameRegistry.reset()
