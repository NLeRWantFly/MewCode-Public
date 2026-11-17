"""CodexRadar model-intelligence adapter.

The public radar feed is deliberately kept behind this small adapter.  The
rest of MewCode consumes a stable, validated snapshot and never scrapes the
website or depends on its presentation markup.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx


CODEX_RADAR_URL = (
    "https://codexradar.com/api/intelligence-efficiency-metrics"
)
CODEX_RADAR_FALLBACK_URL = (
    "https://codexradar.com/data/intelligence-efficiency.json"
)
CODEX_RADAR_SOURCE = "https://codexradar.com/"


@dataclass(frozen=True)
class IntelligencePoint:
    model: str
    effort: str
    iq: float
    runs_24h: int
    runs_total: int
    source_updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "effort": self.effort,
            "iq": self.iq,
            "runs_24h": self.runs_24h,
            "runs_total": self.runs_total,
            "source_updated_at": self.source_updated_at,
        }


@dataclass(frozen=True)
class IntelligenceSnapshot:
    points: tuple[IntelligencePoint, ...]
    fetched_at: float
    checked_at: float
    source_updated_at: str
    status: str = "live"  # live / published / stale / unavailable
    error: str = ""
    endpoint: str = CODEX_RADAR_URL

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source": CODEX_RADAR_SOURCE,
            "endpoint": self.endpoint,
            "fetched_at": self.fetched_at,
            "checked_at": self.checked_at,
            "source_updated_at": self.source_updated_at,
            "error": self.error,
            "points": [point.as_dict() for point in self.points],
        }


def canonical_radar_model(model_id: str) -> str | None:
    """Return a conservative CodexRadar key for a supported model id.

    Provider namespaces (``openai/gpt-*``, ``deepseek:deepseek-*``) and
    OpenRouter's leading ``~`` are harmless to strip.  Everything else
    requires an exact radar family id; fuzzy matching would risk assigning one
    model's IQ to another model.
    """

    value = (model_id or "").strip().lower()
    if not value:
        return None
    value = value.split("?", 1)[0]
    if ":" in value:
        left, right = value.rsplit(":", 1)
        # Accept provider namespaces, while continuing to discard
        # OpenRouter-style variant suffixes (``model:free``).
        value = (
            right
            if right.startswith(("gpt-", "deepseek-"))
            else left
        )
    value = value.rsplit("/", 1)[-1].lstrip("~")
    aliases = {
        "gpt-6-astra-latest": "gpt-6-astra",
        "gpt-5.6-sol-latest": "gpt-5.6-sol",
        "gpt-5.6-terra-latest": "gpt-5.6-terra",
        "gpt-5.6-luna-latest": "gpt-5.6-luna",
        "gpt-5.5-latest": "gpt-5.5",
        "deepseek-v4-flash-latest": "deepseek-v4-flash",
    }
    value = aliases.get(value, value)
    if not value.startswith(("gpt-", "deepseek-")):
        return None
    return value


def _number(value: Any, *, integer: bool = False) -> float | int:
    if isinstance(value, bool) or value is None:
        raise ValueError("missing numeric value")
    result = float(value)
    if result != result or result in (float("inf"), float("-inf")):
        raise ValueError("non-finite numeric value")
    if integer:
        if not result.is_integer():
            raise ValueError("expected an integer")
        return int(result)
    return result


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing source_updated_at")
    normalized = value.strip()
    datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    return normalized


def parse_intelligence_payload(
    payload: Any,
    fetched_at: float,
    *,
    status: str = "live",
    endpoint: str = CODEX_RADAR_URL,
    error: str = "",
) -> IntelligenceSnapshot:
    if not isinstance(payload, dict) or payload.get("schema") != 2:
        raise ValueError("unsupported CodexRadar schema")
    raw_points = payload.get("points")
    if not isinstance(raw_points, list):
        raise ValueError("CodexRadar points must be a list")

    source_updated_at = _timestamp(payload.get("source_updated_at"))
    points: list[IntelligencePoint] = []
    for raw in raw_points:
        if not isinstance(raw, dict):
            continue
        model = str(raw.get("model") or "").strip().lower()
        effort = str(raw.get("effort") or "").strip().lower()
        # Only families explicitly supported by the UI are accepted. Exact
        # canonical matching below prevents IQ leaking across model versions.
        if not model.startswith(("gpt-", "deepseek-")) or not effort:
            continue
        try:
            point_updated_at = (
                raw.get("source_updated_at")
                if "source_updated_at" in raw
                else raw.get("latest_graded_at") or source_updated_at
            )
            runs_24h = int(_number(raw["runs_24h"], integer=True))
            runs_total = int(_number(raw["runs_total"], integer=True))
            if runs_24h < 0 or runs_total < 0:
                raise ValueError("run counts must be non-negative")
            point = IntelligencePoint(
                model=model,
                effort=effort,
                iq=float(_number(raw.get("iq"))),
                runs_24h=runs_24h,
                runs_total=runs_total,
                source_updated_at=_timestamp(point_updated_at),
            )
        except (KeyError, TypeError, ValueError):
            continue
        points.append(point)

    if not points:
        raise ValueError("CodexRadar payload has no valid supported points")
    return IntelligenceSnapshot(
        points=tuple(points),
        fetched_at=fetched_at,
        checked_at=fetched_at,
        source_updated_at=source_updated_at,
        status=status,
        error=error,
        endpoint=endpoint,
    )


class ModelIntelligenceService:
    """Fetch, validate and cache CodexRadar intelligence data."""

    def __init__(
        self,
        *,
        url: str = CODEX_RADAR_URL,
        fallback_url: str | None = CODEX_RADAR_FALLBACK_URL,
        timeout: float = 15.0,
        cache_ttl: float = 60.0,
        stale_ttl: float = 86_400.0,
        cache_path: str | Path | None = None,
        client_factory: Any | None = None,
    ) -> None:
        self.url = url
        self.fallback_url = fallback_url
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self.stale_ttl = stale_ttl
        self.cache_path = Path(cache_path) if cache_path is not None else None
        self._client_factory = client_factory or httpx.AsyncClient
        self._snapshot: IntelligenceSnapshot | None = None
        self._last_result: IntelligenceSnapshot | None = None
        self._last_attempt_at = 0.0
        self._lock = asyncio.Lock()
        self._disk_loaded = False

    def _load_disk_snapshot(self) -> IntelligenceSnapshot | None:
        if self._disk_loaded:
            return self._snapshot
        self._disk_loaded = True
        if self.cache_path is None:
            return None
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            raw_points = payload.get("points") if isinstance(payload, dict) else None
            if not isinstance(raw_points, list):
                return None
            points = tuple(
                IntelligencePoint(
                    model=str(item["model"]),
                    effort=str(item["effort"]),
                    iq=float(item["iq"]),
                    runs_24h=int(item["runs_24h"]),
                    runs_total=int(item["runs_total"]),
                    source_updated_at=_timestamp(item["source_updated_at"]),
                )
                for item in raw_points
                if isinstance(item, dict)
            )
            if not points:
                return None
            self._snapshot = IntelligenceSnapshot(
                points=points,
                fetched_at=float(payload["fetched_at"]),
                checked_at=float(
                    payload.get("checked_at", payload["fetched_at"])
                ),
                source_updated_at=_timestamp(payload["source_updated_at"]),
                status="stale",
                endpoint=str(payload.get("endpoint") or self.url),
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return self._snapshot

    def _save_disk_snapshot(self, snapshot: IntelligenceSnapshot) -> None:
        if self.cache_path is None:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_name(
                f".{self.cache_path.name}.{os.getpid()}.tmp"
            )
            tmp.write_text(
                json.dumps(
                    snapshot.as_dict(), ensure_ascii=False, separators=(",", ":")
                ),
                encoding="utf-8",
            )
            tmp.replace(self.cache_path)
        except OSError:
            return

    async def get(self, *, force: bool = False) -> IntelligenceSnapshot:
        now = time.time()
        cached = self._snapshot or self._load_disk_snapshot()
        if (
            not force
            and self._last_result is not None
            and now - self._last_attempt_at < self.cache_ttl
        ):
            return self._last_result

        async with self._lock:
            now = time.time()
            cached = self._snapshot or self._load_disk_snapshot()
            if (
                not force
                and self._last_result is not None
                and now - self._last_attempt_at < self.cache_ttl
            ):
                return self._last_result
            try:
                errors: list[str] = []
                snapshot: IntelligenceSnapshot | None = None
                async with self._client_factory(timeout=self.timeout) as client:
                    feeds = [(self.url, "live")]
                    if self.fallback_url and self.fallback_url != self.url:
                        feeds.append((self.fallback_url, "published"))
                    for feed_url, status in feeds:
                        try:
                            headers = {
                                "Accept": "application/json",
                                "User-Agent": "MewCode/0.1 model-intelligence",
                            }
                            request_kwargs: dict[str, Any] = {"headers": headers}
                            if force:
                                headers.update({
                                    "Cache-Control": "no-cache",
                                    "Pragma": "no-cache",
                                })
                                request_kwargs["params"] = {
                                    "_mewcode_refresh": str(int(now * 1000))
                                }
                            response = await client.get(
                                feed_url,
                                **request_kwargs,
                            )
                            response.raise_for_status()
                            content_length = int(
                                getattr(response, "headers", {}).get(
                                    "content-length", "0"
                                )
                                or 0
                            )
                            if content_length > 2_000_000:
                                raise ValueError("CodexRadar payload is too large")
                            fallback_error = (
                                "; ".join(errors) if status != "live" else ""
                            )
                            snapshot = parse_intelligence_payload(
                                response.json(),
                                now,
                                status=status,
                                endpoint=feed_url,
                                error=fallback_error,
                            )
                            break
                        except Exception as exc:
                            detail = str(exc).strip()
                            errors.append(
                                f"{feed_url}: {type(exc).__name__}"
                                + (f": {detail}" if detail else "")
                            )
                if snapshot is None:
                    raise RuntimeError("; ".join(errors) or "no intelligence feed")
                self._snapshot = snapshot
                await asyncio.to_thread(self._save_disk_snapshot, snapshot)
                self._last_result = snapshot
                self._last_attempt_at = now
                return snapshot
            except Exception as exc:
                # A validated disk snapshot is preferable to a blank UI even
                # when it is old; status/source timestamps make staleness explicit.
                if cached is not None:
                    result = IntelligenceSnapshot(
                        points=cached.points,
                        fetched_at=cached.fetched_at,
                        checked_at=now,
                        source_updated_at=cached.source_updated_at,
                        status="stale",
                        error=str(exc) or type(exc).__name__,
                        endpoint=cached.endpoint,
                    )
                    self._last_result = result
                    self._last_attempt_at = now
                    return result
                result = IntelligenceSnapshot(
                    points=(),
                    fetched_at=now,
                    checked_at=now,
                    source_updated_at="",
                    status="unavailable",
                    error=str(exc) or type(exc).__name__,
                    endpoint=self.url,
                )
                self._last_result = result
                self._last_attempt_at = now
                return result

    @staticmethod
    def model_summary(
        snapshot: IntelligenceSnapshot,
        model_id: str,
        effort: str = "",
    ) -> dict[str, Any] | None:
        canonical = canonical_radar_model(model_id)
        if canonical is None:
            return None
        points = [point for point in snapshot.points if point.model == canonical]
        if not points:
            return None
        effort = (effort or "").strip().lower()
        if effort and effort != "auto":
            exact = next((point for point in points if point.effort == effort), None)
            if exact is None:
                return None
            return {
                "kind": "exact",
                "model": canonical,
                "effort": effort,
                "iq": exact.iq,
                "runs_24h": exact.runs_24h,
                "runs_total": exact.runs_total,
                "source_updated_at": exact.source_updated_at,
            }
        values = [point.iq for point in points]
        return {
            "kind": "range",
            "model": canonical,
            "min_iq": min(values),
            "max_iq": max(values),
            "runs_24h": sum(point.runs_24h for point in points),
            "runs_total": sum(point.runs_total for point in points),
            "source_updated_at": max(
                (point.source_updated_at for point in points), default=""
            ),
            "by_effort": {
                point.effort: point.iq
                for point in sorted(points, key=lambda item: item.effort)
            },
        }

    @staticmethod
    def model_by_effort(
        snapshot: IntelligenceSnapshot, model_id: str
    ) -> dict[str, dict[str, Any]]:
        """Return all radar efforts without consulting the provider UI list."""
        canonical = canonical_radar_model(model_id)
        if canonical is None:
            return {}
        result: dict[str, dict[str, Any]] = {}
        for point in snapshot.points:
            if point.model != canonical:
                continue
            summary = ModelIntelligenceService.model_summary(
                snapshot, canonical, point.effort
            )
            if summary is not None:
                result[point.effort] = summary
        return result
