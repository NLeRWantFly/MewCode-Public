from __future__ import annotations

import json
from pathlib import Path

from mewcode.model_registry import ModelRef
from mewcode.permissions.review_client import (
    load_review_model_settings,
    resolve_safety_review_client,
)


class _FakeResolved:
    def __init__(self, client: object) -> None:
        self.client = client


class _FakeRegistry:
    def __init__(self, client: object) -> None:
        self.client = client
        self.calls: list[ModelRef] = []

    def resolve(self, ref, *, usage_recorder=None):  # noqa: ANN001
        self.calls.append(ModelRef.from_value(ref) if not isinstance(ref, ModelRef) else ref)
        return _FakeResolved(self.client)


def test_load_review_model_settings(tmp_path: Path) -> None:
    path = tmp_path / "review-model.json"
    path.write_text(
        json.dumps({
            "model_id": "gpt-5.6-luna",
            "provider_key": "codex:codex-custom",
            "source": "manual",
        }),
        encoding="utf-8",
    )
    settings = load_review_model_settings(path)
    assert settings["model_id"] == "gpt-5.6-luna"
    assert settings["provider_key"] == "codex:codex-custom"


def test_resolve_safety_review_client_uses_settings(tmp_path: Path) -> None:
    path = tmp_path / "review-model.json"
    path.write_text(
        json.dumps({
            "model_id": "gpt-5.6-luna",
            "provider_key": "codex:codex-custom",
        }),
        encoding="utf-8",
    )
    review_client = object()
    fallback = object()
    registry = _FakeRegistry(review_client)
    client = resolve_safety_review_client(
        registry,
        fallback_client=fallback,
        settings_path=path,
    )
    assert client is review_client
    assert registry.calls[0].model_id == "gpt-5.6-luna"
    assert registry.calls[0].provider_key == "codex:codex-custom"


def test_resolve_safety_review_client_falls_back_when_missing(tmp_path: Path) -> None:
    fallback = object()
    client = resolve_safety_review_client(
        _FakeRegistry(object()),
        fallback_client=fallback,
        settings_path=tmp_path / "missing.json",
    )
    assert client is fallback


def test_set_review_model_rebinds_running_reviewer(tmp_path: Path, monkeypatch) -> None:
    """POST /api/settings/review-model must rebind the live reviewer's client."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    import frontend.server as server
    import mewcode.permissions.review_client as review_client

    monkeypatch.setattr(server, "REVIEW_MODEL_FILE", tmp_path / "review-model.json")
    resolved = object()
    captured = {}

    def fake_resolve(registry, *, fallback_client=None, usage_recorder=None, settings_path=None):
        captured["fallback"] = fallback_client
        return resolved

    monkeypatch.setattr(review_client, "resolve_safety_review_client", fake_resolve)
    monkeypatch.setattr(server, "MODEL_REGISTRY", _FakeRegistry(resolved))
    reviewer = SimpleNamespace(client=object())
    agent = SimpleNamespace(safety_reviewer=reviewer, auxiliary_client="aux")
    monkeypatch.setattr(server.SESSION, "agent", agent)
    monkeypatch.setattr(
        server.SESSION, "client",
        SimpleNamespace(get_usage_recorder=MagicMock(return_value=None)),
    )

    response = TestClient(server.app).post(
        "/api/settings/review-model",
        json={"model_id": "gpt-5.6-luna", "provider_key": "codex:x"},
    )
    assert response.status_code == 200
    assert reviewer.client is resolved
    assert captured["fallback"] == "aux"
    saved = json.loads((tmp_path / "review-model.json").read_text(encoding="utf-8"))
    assert saved["model_id"] == "gpt-5.6-luna"


def test_set_review_model_requires_a_complete_owner_selection(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    import frontend.server as server
    import mewcode.permissions.review_client as review_client

    monkeypatch.setattr(server, "REVIEW_MODEL_FILE", tmp_path / "review-model.json")
    monkeypatch.setattr(
        review_client, "resolve_safety_review_client",
        lambda *a, **kw: kw.get("fallback_client"),
    )
    fallback = object()
    existing_client = object()
    reviewer = SimpleNamespace(client=existing_client)
    agent = SimpleNamespace(safety_reviewer=reviewer, auxiliary_client=fallback)
    monkeypatch.setattr(server.SESSION, "agent", agent)
    monkeypatch.setattr(
        server.SESSION, "client",
        SimpleNamespace(get_usage_recorder=MagicMock(return_value=None)),
    )

    response = TestClient(server.app).post(
        "/api/settings/review-model", json={"model_id": ""},
    )
    assert response.status_code == 400
    assert response.json()['error'] == '审核模型需要同时选择 Provider 和模型'
    assert reviewer.client is existing_client
    assert not (tmp_path / 'review-model.json').exists()
