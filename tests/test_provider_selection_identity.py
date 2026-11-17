"""Offline regression tests for stable TUI provider-slot selection identity."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from mewcode.app import MewCodeApp
from mewcode.config import ProviderConfig
from mewcode.provider_sources import ProviderSlot


def _provider(name: str = "shared", model: str = "model-a") -> ProviderConfig:
    return ProviderConfig(
        name=name,
        protocol="anthropic",
        base_url="https://api.anthropic.com",
        model=model,
        api_key="sk-offline-test",
    )


def _app_with_slots(slots: list[ProviderSlot]) -> MewCodeApp:
    """Build only the state needed by identity helpers; no TUI or runtime starts."""
    app = object.__new__(MewCodeApp)
    app._provider_slots = slots
    app._selected_provider_slot_key = None
    return app


def test_selection_key_disambiguates_same_name_slots_and_survives_refresh() -> None:
    configured = _provider()
    selected = ProviderSlot(
        provider=configured,
        source="config",
        origin=".mewcode/config.yaml",
    )
    same_label_other_source = ProviderSlot(
        provider=_provider(),
        source="codex",
        origin="C:/isolated/.codex/config.toml",
    )
    app = _app_with_slots([selected, same_label_other_source])

    app._selected_provider_slot_key = MewCodeApp._provider_slot_key_for_provider(
        app, configured
    )
    assert app._selected_provider_slot_key == "config:shared"

    # Runtime/model changes replace the selected ProviderConfig object, but not
    # the source slot identity. A refreshed discovery also creates new objects.
    app._selected_provider = deepcopy(configured)
    app._selected_provider.model = "model-b"
    refreshed_selected = ProviderSlot(
        provider=_provider(model="model-b"),
        source="config",
        origin=".mewcode/config.yaml",
    )
    refreshed_other = ProviderSlot(
        provider=_provider(model="model-b"),
        source="codex",
        origin="C:/isolated/.codex/config.toml",
    )
    refreshed = [refreshed_selected, refreshed_other]

    assert MewCodeApp._selected_provider_slot(app, refreshed) is refreshed_selected
    assert refreshed_selected.key != refreshed_other.key


def test_ambiguous_slot_key_is_not_marked_or_inferred() -> None:
    first_provider = _provider()
    first = ProviderSlot(provider=first_provider, source="config")
    duplicate = ProviderSlot(provider=_provider(), source="config")
    app = _app_with_slots([first, duplicate])

    assert MewCodeApp._provider_slot_key_for_provider(app, first_provider) is None
    app._selected_provider_slot_key = first.key
    assert MewCodeApp._selected_provider_slot(app, [first, duplicate]) is None


def test_switch_provider_updates_identity_only_after_client_configuration() -> None:
    current = ProviderSlot(provider=_provider("current"), source="config")
    target = ProviderSlot(provider=_provider("target"), source="codex")
    applied: list[tuple[ProviderConfig, object]] = []

    class Runtime:
        fail = False

        def configure_model(self, provider: ProviderConfig, *, effort: str) -> object:
            if self.fail:
                raise RuntimeError("offline configure failure")
            return object()

    runtime = Runtime()
    app = SimpleNamespace(
        _streaming=False,
        agent=object(),
        agent_runtime=runtime,
        _effort="medium",
        _selected_provider=current.provider,
        _selected_provider_slot_key=current.key,
        _apply_client=lambda provider, client: applied.append((provider, client)),
        _show_system_message=lambda _message: None,
    )

    runtime.fail = True
    error = MewCodeApp.switch_provider(app, target)
    assert error == "offline configure failure"
    assert app._selected_provider_slot_key == current.key
    assert applied == []

    runtime.fail = False
    assert MewCodeApp.switch_provider(app, target) is None
    assert app._selected_provider_slot_key == target.key
    assert len(applied) == 1


def test_apply_client_and_model_switch_keep_slot_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    slot = ProviderSlot(provider=_provider(), source="config")
    app = SimpleNamespace(
        _selected_provider_slot_key=slot.key,
        skill_executor=None,
        registry=SimpleNamespace(get=lambda _name: None),
        run_worker=lambda coroutine, **_kwargs: coroutine.close(),
        _resolve_context_window=lambda _provider: _no_op_probe(),
        query_one=lambda *_args, **_kwargs: (_ for _ in ()).throw(LookupError()),
        _update_effort_label=lambda: None,
        _update_context_label=lambda: None,
        _streaming=False,
        _selected_provider=slot.provider,
        agent=object(),
        _effort="high",
        agent_runtime=SimpleNamespace(
            configure_model=lambda provider, *, effort: object()
        ),
        _apply_client=lambda provider, _client: setattr(
            app, "_selected_provider", provider
        ),
        _show_system_message=lambda _message: None,
    )

    replacement = _provider(model="model-b")
    MewCodeApp._apply_client(app, replacement, object())
    assert app._selected_provider is replacement
    assert app._selected_provider_slot_key == slot.key

    # switch_model uses an effective copy and re-applies it without changing
    # the slot key. It performs no client/network operation in this fake runtime.
    assert MewCodeApp.switch_model(app, "model-c") is None
    assert app._selected_provider.model == "model-c"
    assert app._selected_provider_slot_key == slot.key
    assert slot.provider.model == "model-a"


async def _no_op_probe() -> None:
    return None
