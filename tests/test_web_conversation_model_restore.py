from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from frontend import server
from mewcode.config import ProviderConfig


class _FakeClient:
    def __init__(self, model: str) -> None:
        self.model = model
        self.effort = ""

    def set_effort(self, value: str) -> None:
        self.effort = value


def _slot(key: str = "synthetic:primary", model: str = "configured-model"):
    return server.ProviderSlot(
        provider=ProviderConfig(
            name="Synthetic provider",
            protocol="openai-compat",
            base_url="https://example.invalid/v1",
            model=model,
            api_key="synthetic-test-key",
        ),
        source="config",
        registry_key=key,
        effort_levels=["low", "high"],
        default_effort="low",
    )


def _patch_state_files(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(server, "STATE_FILE", tmp_path / "web_state.json")
    monkeypatch.setattr(server, "PROJECTS_FILE", tmp_path / "projects.json")
    monkeypatch.setattr(
        server, "BACKGROUND_TASKS_FILE", tmp_path / "background_tasks.json"
    )


def test_saved_model_route_survives_restart_and_startup_does_not_reselect(
    monkeypatch, tmp_path: Path
) -> None:
    project = tmp_path / "synthetic-project"
    project.mkdir()
    _patch_state_files(monkeypatch, tmp_path)
    monkeypatch.chdir(project)

    original_slot = _slot()
    source = server.WebSession()
    source._ensure_active_agent = lambda: None
    created = source.new_conversation("project")
    source.slot = original_slot
    source.client = _FakeClient("selected-model")
    source.permission_mode = server.PermissionMode.AUTO
    source.multi_agent = False
    source.effort = "high"
    source.web_state["permission_mode"] = "auto"
    source.web_state["multi_agent"] = False
    source._save_active_conversation_settings()
    source._close_session()

    saved = json.loads(server.STATE_FILE.read_text(encoding="utf-8"))
    assert saved["conversation_settings"][created["id"]]["provider_key"] == original_slot.key
    assert saved["conversation_settings"][created["id"]]["model"] == "selected-model"

    restored = server.WebSession()
    restored.slots = [original_slot]

    def build_synthetic_agent(provider: ProviderConfig) -> None:
        restored.agent = SimpleNamespace(permission_mode=restored.permission_mode)
        restored.client = _FakeClient(provider.model)

    restored._build_agent = build_synthetic_agent
    restored._apply_context_tier = lambda: None
    restored._sync_context_faces = lambda: None
    restored._sync_agent_conversation_owner = lambda: None
    selected: list[str] = []

    async def unexpected_default_selection(key: str) -> str | None:
        selected.append(key)
        return None

    restored.use_slot = unexpected_default_selection
    loaded = restored.load_conversation("project", created["id"])
    startup_error = asyncio.run(server._select_startup_provider(restored, True))

    assert loaded is not None
    assert startup_error is None
    assert selected == []
    assert restored.slot is not original_slot
    assert restored.slot.provider is not original_slot.provider
    assert original_slot.provider.model == "configured-model"
    assert restored.slot.provider.model == "selected-model"
    assert restored.effort == "high"
    assert restored.client.effort == "high"
    assert restored.permission_mode is server.PermissionMode.AUTO
    assert restored.multi_agent is False
    source._close_session()
    restored._close_session()


def test_legacy_route_without_model_uses_provider_default(monkeypatch, tmp_path: Path) -> None:
    _patch_state_files(monkeypatch, tmp_path)
    session = server.WebSession()
    configured = _slot(model="legacy-default")
    session.slots = [configured]
    session.conversation_settings["legacy"] = {
        "provider_key": configured.key,
        "permission_mode": "auto",
        "multi_agent": False,
    }

    session._restore_conversation_settings("legacy")

    assert session.slot is not configured
    assert session.slot.provider.model == "legacy-default"
    assert session.provider_restore_error == ""
    assert "model" not in session.conversation_settings["legacy"]
    assert session.multi_agent is False


@pytest.mark.parametrize(
    ("provider_key", "include_key"),
    [
        (None, False),
        ("", True),
        (None, True),
        (7, True),
        ("removed:provider", True),
    ],
    ids=["missing-key", "empty-key", "null-key", "non-string-key", "unavailable-key"],
)
def test_invalid_saved_route_fails_closed_and_is_visible(
    monkeypatch, tmp_path: Path, provider_key, include_key: bool
) -> None:
    _patch_state_files(monkeypatch, tmp_path)
    session = server.WebSession()
    fallback = _slot("other:available")
    session.slots = [fallback]
    session.slot = fallback
    settings = {"model": "saved-model"}
    if include_key:
        settings["provider_key"] = provider_key
    session.conversation_settings["broken"] = settings

    session._restore_conversation_settings("broken")
    session.session = SimpleNamespace(session_id="broken")
    selected: list[str] = []

    async def unexpected_fallback(key: str) -> str | None:
        selected.append(key)
        return None

    session.use_slot = unexpected_fallback
    startup_error = asyncio.run(server._select_startup_provider(session, True))
    assert session.slot is None
    assert session.provider_restore_error
    assert startup_error == session.provider_restore_error
    assert selected == []

    saved_route = {
        key: settings[key] for key in ("provider_key", "model") if key in settings
    }
    session._save_active_conversation_settings()
    assert {
        key: session.conversation_settings["broken"][key]
        for key in ("provider_key", "model")
        if key in session.conversation_settings["broken"]
    } == saved_route

    session.session = None
    assert session.status_payload()["provider_error"] == session.provider_restore_error
    session._close_session()


@pytest.mark.parametrize("bad_model", [None, "", "   ", False, 7, {}])
def test_present_but_invalid_model_is_not_treated_as_legacy(
    monkeypatch, tmp_path: Path, bad_model
) -> None:
    _patch_state_files(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    session = server.WebSession()
    configured = _slot()
    session.slots = [configured]
    session.slot = configured
    session.conversation_settings["broken-model"] = {
        "provider_key": configured.key, "model": bad_model,
    }
    session._restore_conversation_settings("broken-model")
    assert session.slot is None
    assert session.provider_restore_error
    session.session = SimpleNamespace(session_id="broken-model")

    async def no_fallback(_key):
        raise AssertionError("Invalid saved model must not select a default route")

    session.use_slot = no_fallback
    assert asyncio.run(server._select_startup_provider(session, True))
    session._save_active_conversation_settings()
    assert session.conversation_settings["broken-model"]["model"] == bad_model
    session.session = None


def test_model_switch_and_runtime_save_persist_the_actual_route(
    monkeypatch, tmp_path: Path
) -> None:
    _patch_state_files(monkeypatch, tmp_path)
    session = server.WebSession()
    session.session = SimpleNamespace(session_id="switched")
    session.slot = _slot(model="before-switch")
    session.agent = SimpleNamespace(permission_mode=server.PermissionMode.AUTO)
    session.client = _FakeClient("before-switch")
    session.effort = "high"

    def build_synthetic_agent(provider: ProviderConfig) -> None:
        session.agent = SimpleNamespace(permission_mode=server.PermissionMode.AUTO)
        session.client = _FakeClient(provider.model)

    async def resolve_window(_provider: ProviderConfig) -> None:
        return None

    session._build_agent = build_synthetic_agent
    session._apply_context_tier = lambda: None
    session._resolve_window = resolve_window

    async def switch() -> str | None:
        error = session.switch_model("after-switch")
        await asyncio.sleep(0)
        return error

    assert asyncio.run(switch()) is None
    assert session.conversation_settings["switched"]["model"] == "after-switch"
    assert session.conversation_settings["switched"]["provider_key"] == session.slot.key

    runtime_slot = _slot("runtime:route", "runtime-model")
    runtime = SimpleNamespace(
        conversation_id="runtime-save",
        slot=runtime_slot,
        client=_FakeClient("runtime-client-model"),
        effort="high",
        context_tier=0,
        agent=SimpleNamespace(permission_mode=server.PermissionMode.AUTO),
        multi_agent=False,
        expert_mode=False,
        orchestration_planning=False,
        permission_before_plan="auto",
    )
    session._save_runtime_settings(runtime)
    saved = session.conversation_settings["runtime-save"]
    assert saved["provider_key"] == runtime_slot.key
    assert saved["model"] == "runtime-client-model"
