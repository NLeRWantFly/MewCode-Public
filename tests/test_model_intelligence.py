from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from mewcode.model_intelligence import (
    ModelIntelligenceService,
    canonical_radar_model,
    parse_intelligence_payload,
)


def _payload() -> dict:
    return {
        "schema": 2,
        "source_updated_at": "2026-08-09T04:00:00Z",
        "points": [
            {
                "model": "gpt-5.6-sol",
                "effort": "low",
                "iq": 77.5,
                "runs_24h": 4,
                "runs_total": 10,
                "source_updated_at": "2026-08-09T03:59:00Z",
            },
            {
                "model": "gpt-5.6-sol",
                "effort": "xhigh",
                "iq": 107.14,
                "runs_24h": 3,
                "runs_total": 12,
                "source_updated_at": "2026-08-09T04:00:00Z",
            },
            {
                "model": "deepseek-v4-flash",
                "effort": "high",
                "iq": 60,
                "runs_24h": 1,
                "runs_total": 2,
            },
        ],
    }


def test_parse_accepts_supported_families_and_builds_effort_summary():
    snapshot = parse_intelligence_payload(_payload(), 100.0)
    assert len(snapshot.points) == 3
    assert snapshot.checked_at == 100.0
    exact = ModelIntelligenceService.model_summary(
        snapshot, "openai/gpt-5.6-sol", "xhigh"
    )
    assert exact and exact["iq"] == 107.14
    ranged = ModelIntelligenceService.model_summary(
        snapshot, "~openai/gpt-5.6-sol", "auto"
    )
    assert ranged and ranged["min_iq"] == 77.5
    assert ranged["max_iq"] == 107.14
    by_effort = ModelIntelligenceService.model_by_effort(
        snapshot, "gpt-5.6-sol"
    )
    assert set(by_effort) == {"low", "xhigh"}
    assert by_effort["xhigh"]["iq"] == 107.14
    assert ModelIntelligenceService.model_summary(
        snapshot, "gpt-5.6-sol", "ultra"
    ) is None
    deepseek = ModelIntelligenceService.model_summary(
        snapshot, "deepseek/deepseek-v4-flash", "high"
    )
    assert deepseek and deepseek["iq"] == 60


def test_canonical_mapping_is_conservative():
    assert canonical_radar_model("openai/gpt-5.6-sol") == "gpt-5.6-sol"
    assert canonical_radar_model("gpt-5.6-sol-latest") == "gpt-5.6-sol"
    assert canonical_radar_model("openai:gpt-5.6-sol") == "gpt-5.6-sol"
    assert canonical_radar_model("openai/gpt-5.6-sol:free") == "gpt-5.6-sol"
    assert (
        canonical_radar_model("deepseek:deepseek-v4-flash")
        == "deepseek-v4-flash"
    )
    assert (
        canonical_radar_model("~deepseek/deepseek-v4-flash:free")
        == "deepseek-v4-flash"
    )
    assert (
        canonical_radar_model("deepseek-v4-flash-latest")
        == "deepseek-v4-flash"
    )
    assert canonical_radar_model("claude-opus-5") is None
    assert canonical_radar_model("my-gpt-wrapper") is None
    snapshot = parse_intelligence_payload(_payload(), 100.0)
    assert ModelIntelligenceService.model_summary(
        snapshot, "deepseek-chat", "high"
    ) is None


class _Response:
    def __init__(self, payload: dict):
        self._payload = payload
        self.headers = {}

    def raise_for_status(self):
        return None

    def json(self):
        return json.loads(json.dumps(self._payload))


class _Client:
    def __init__(self, payload: dict, fail: bool = False, **_kwargs):
        self.payload = payload
        self.fail = fail

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, *_args, **_kwargs):
        if self.fail:
            raise RuntimeError("offline")
        return _Response(self.payload)


@pytest.mark.asyncio
async def test_service_uses_last_good_snapshot_when_refresh_fails():
    state = {"fail": False}

    def factory(**kwargs):
        return _Client(_payload(), fail=state["fail"], **kwargs)

    service = ModelIntelligenceService(
        client_factory=factory, cache_ttl=0, stale_ttl=3600, fallback_url=None
    )
    live = await service.get()
    assert live.status == "live"
    state["fail"] = True
    stale = await service.get(force=True)
    assert stale.status == "stale"
    assert stale.points == live.points


@pytest.mark.asyncio
async def test_unavailable_result_is_cached_for_sixty_second_window():
    calls = 0

    def factory(**kwargs):
        nonlocal calls
        calls += 1
        return _Client(_payload(), fail=True, **kwargs)

    service = ModelIntelligenceService(
        client_factory=factory, cache_ttl=60, fallback_url=None
    )
    first = await service.get()
    second = await service.get()
    assert first.status == second.status == "unavailable"
    assert calls == 1


def test_unknown_schema_is_rejected():
    with pytest.raises(ValueError, match="schema"):
        parse_intelligence_payload({"schema": 3, "points": []}, 1.0)


def test_invalid_runs_and_timestamp_are_not_accepted():
    payload = _payload()
    payload["points"] = [
        {**payload["points"][0], "runs_total": 1.5},
        {**payload["points"][1], "source_updated_at": ""},
    ]
    with pytest.raises(ValueError, match="no valid supported points"):
        parse_intelligence_payload(payload, 1.0)


def test_published_snapshot_uses_payload_timestamp_for_points():
    payload = _payload()
    for point in payload["points"]:
        point.pop("source_updated_at", None)
        point["latest_graded_at"] = "2026-08-09T03:00:00Z"
    snapshot = parse_intelligence_payload(
        payload,
        100.0,
        status="published",
        endpoint="https://example.test/published.json",
    )
    assert snapshot.status == "published"
    assert snapshot.endpoint.endswith("published.json")
    assert snapshot.points[0].source_updated_at == "2026-08-09T03:00:00Z"


@pytest.mark.asyncio
async def test_service_falls_back_to_published_snapshot():
    class FallbackClient(_Client):
        async def get(self, url, **_kwargs):
            if url.endswith("/live"):
                raise TimeoutError("live feed timed out")
            return _Response(_payload())

    service = ModelIntelligenceService(
        url="https://example.test/live",
        fallback_url="https://example.test/published.json",
        client_factory=lambda **kwargs: FallbackClient(_payload(), **kwargs),
    )
    snapshot = await service.get(force=True)
    assert snapshot.status == "published"
    assert snapshot.points
    assert "TimeoutError" in snapshot.error


@pytest.mark.asyncio
async def test_valid_snapshot_is_atomically_persisted_and_reused_offline():
    cache_path = Path(tempfile.mkdtemp()) / "model-intelligence.json"
    live_service = ModelIntelligenceService(
        client_factory=lambda **kwargs: _Client(_payload(), **kwargs),
        fallback_url=None,
        cache_path=cache_path,
    )
    live = await live_service.get(force=True)
    assert live.status == "live"
    saved = json.loads(cache_path.read_text(encoding="utf-8"))
    assert saved["points"][1]["effort"] == "xhigh"
    assert saved["checked_at"] == live.checked_at
    assert not list(cache_path.parent.glob("*.tmp"))

    offline_service = ModelIntelligenceService(
        client_factory=lambda **kwargs: _Client(_payload(), fail=True, **kwargs),
        fallback_url=None,
        cache_path=cache_path,
        stale_ttl=0,
    )
    cached = await offline_service.get(force=True)
    assert cached.status == "stale"
    assert cached.source_updated_at == live.source_updated_at
    assert cached.points == live.points
    assert cached.fetched_at == live.fetched_at
    assert cached.checked_at >= cached.fetched_at


@pytest.mark.asyncio
async def test_force_refresh_bypasses_upstream_caches_and_updates_check_time(
    monkeypatch,
):
    requests = []

    class CapturingClient(_Client):
        async def get(self, url, **kwargs):
            requests.append((url, kwargs))
            return await super().get(url, **kwargs)

    times = iter([100.0, 100.0, 200.0, 200.0])
    monkeypatch.setattr("mewcode.model_intelligence.time.time", lambda: next(times))
    service = ModelIntelligenceService(
        client_factory=lambda **kwargs: CapturingClient(_payload(), **kwargs),
        fallback_url=None,
    )

    first = await service.get(force=True)
    second = await service.get(force=True)

    assert first.checked_at == 100.0
    assert second.checked_at == 200.0
    assert requests[1][1]["headers"]["Cache-Control"] == "no-cache"
    assert requests[1][1]["headers"]["Pragma"] == "no-cache"
    assert requests[1][1]["params"]["_mewcode_refresh"] == "200000"


def test_legacy_disk_snapshot_without_checked_at_is_compatible():
    cache_path = Path(tempfile.mkdtemp()) / "model-intelligence.json"
    snapshot = parse_intelligence_payload(_payload(), 123.0)
    payload = snapshot.as_dict()
    payload.pop("checked_at")
    cache_path.write_text(json.dumps(payload), encoding="utf-8")

    service = ModelIntelligenceService(cache_path=cache_path)
    loaded = service._load_disk_snapshot()

    assert loaded is not None
    assert loaded.checked_at == loaded.fetched_at == 123.0
