from __future__ import annotations

from pathlib import Path

import pytest

from mewcode.mcp.coord_capabilities import codex_manifest, grok_inspect_manifest, run_grok_inspect
from mewcode.mcp.coord_protocol import APPROVAL_MODEL_ID, CoordinationError


def test_grok_inspect_uses_dynamic_fields() -> None:
    manifest = grok_inspect_manifest({
        "capabilities": {
            "tools": [{"name": "runtime-test", "schema_hash": "abc", "access": "read"}],
            "skills": [{"name": "audit", "version": "1"}],
            "mcpServers": [{"name": "coord"}],
        }
    })
    assert [tool["name"] for tool in manifest["tools"]] == ["runtime-test", "run_terminal_cmd"]
    assert manifest["skills"][0]["name"] == "audit"
    assert manifest["source"] == "grok-inspect"


def test_grok_inspect_adds_terminal_adapter_to_empty_tools() -> None:
    manifest = grok_inspect_manifest({"tools": []})

    assert manifest["tools"] == [{"name": "run_terminal_cmd"}]


def test_grok_inspect_does_not_duplicate_existing_terminal_adapter() -> None:
    manifest = grok_inspect_manifest({
        "tools": ["dynamic-first", {"name": "run_terminal_cmd"}, "dynamic-last"],
    })

    assert [tool["name"] for tool in manifest["tools"]] == [
        "dynamic-first",
        "run_terminal_cmd",
        "dynamic-last",
    ]


def test_codex_manifest_is_normalized() -> None:
    result = codex_manifest(agent_id="codex-planner-01", model_id=APPROVAL_MODEL_ID, role="planner", tools=["coord_protocol"], skills=[], mcp_servers=[])
    assert result["tools"] == [{"name": "coord_protocol"}]


def test_invalid_grok_json_is_rejected() -> None:
    with pytest.raises(CoordinationError, match="valid JSON"):
        grok_inspect_manifest("not-json")


def test_grok_inspect_uses_project_grok_home_without_user_home_detection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_home = tmp_path / ".mewcode" / "grok-home"
    project_home.mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "user-home-that-must-not-be-used"))
    monkeypatch.setenv("HOME", str(tmp_path / "user-home-that-must-not-be-used"))
    monkeypatch.delenv("GROK_HOME", raising=False)
    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object):
        captured.update(kwargs)
        class Completed:
            stdout = '{"mcpServers": []}'
        return Completed()

    monkeypatch.setattr("mewcode.mcp.coord_capabilities.subprocess.run", fake_run)
    run_grok_inspect(executable="fake-grok", cwd=tmp_path)
    assert captured["env"]["GROK_HOME"] == str(project_home)


def test_grok_inspect_explicit_home_wins_over_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit_home = tmp_path / "explicit-home"
    monkeypatch.setenv("MEWCODE_GROK_HOME", str(tmp_path / "environment-home"))
    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object):
        captured.update(kwargs)
        class Completed:
            stdout = '{"mcpServers": []}'
        return Completed()

    monkeypatch.setattr("mewcode.mcp.coord_capabilities.subprocess.run", fake_run)
    run_grok_inspect(executable="fake-grok", cwd=tmp_path, grok_home=explicit_home)
    assert captured["env"]["GROK_HOME"] == str(explicit_home)
