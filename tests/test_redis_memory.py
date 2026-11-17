from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from mewcode.config import RedisConfig, load_config
from mewcode.conversation import Message, ThinkingBlock, ToolResultBlock, ToolUseBlock
from mewcode.memory.redis_cache import RedisContextCache
from mewcode.memory import redis_cache as redis_cache_module
from mewcode.memory.session import SessionManager
from mewcode.validator import ConfigError


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def ping(self) -> bool:
        return True

    def set(self, key: str, value: str, ex: int) -> None:
        self.values[key] = value
        self.ttls[key] = ex

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def _cache(tmp_path: Path) -> tuple[RedisContextCache, FakeRedis]:
    config = RedisConfig(
        enabled=True,
        context_ttl_seconds=600,
        recall_ttl_seconds=30,
        max_messages=20,
    )
    cache = RedisContextCache(config, str(tmp_path))
    fake = FakeRedis()
    cache._client = fake
    return cache, fake


def test_redis_config_defaults_and_override(tmp_path: Path) -> None:
    minimal = tmp_path / "minimal.yaml"
    minimal.write_text(
        "providers:\n"
        "  - name: test\n"
        "    protocol: openai\n"
        "    base_url: http://localhost\n"
        "    model: test\n",
        encoding="utf-8",
    )
    assert load_config(minimal).redis.enabled is False

    enabled = tmp_path / "enabled.yaml"
    enabled.write_text(
        minimal.read_text(encoding="utf-8")
        + "redis:\n"
        + "  enabled: true\n"
        + "  context_ttl_seconds: 3600\n"
        + "  recall_ttl_seconds: 60\n"
        + "  max_messages: 100\n",
        encoding="utf-8",
    )
    config = load_config(enabled).redis
    assert config.enabled is True
    assert config.context_ttl_seconds == 3600
    assert config.recall_ttl_seconds == 60


def test_invalid_redis_config_is_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "invalid.yaml"
    config_file.write_text(
        "providers:\n"
        "  - name: test\n"
        "    protocol: openai\n"
        "    base_url: http://localhost\n"
        "    model: test\n"
        "redis:\n"
        "  enabled: true\n"
        "  context_ttl_seconds: 0\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="context_ttl_seconds"):
        load_config(config_file)


def test_context_roundtrip_preserves_structured_messages(tmp_path: Path) -> None:
    cache, fake = _cache(tmp_path)
    messages = [
        Message(role="user", content="read file"),
        Message(
            role="assistant",
            content="reading",
            tool_uses=[ToolUseBlock("t1", "ReadFile", {"file_path": "a.txt"})],
            thinking_blocks=[ThinkingBlock("check", "sig")],
        ),
        Message(
            role="user",
            content="",
            tool_results=[ToolResultBlock("t1", "contents")],
        ),
    ]
    assert cache.store_context("session-1", messages) is True
    restored = cache.load_context("session-1")
    assert restored is not None
    loaded, saved_at = restored
    assert saved_at > 0
    assert loaded[1].tool_uses[0].arguments == {"file_path": "a.txt"}
    assert loaded[1].thinking_blocks[0].signature == "sig"
    assert loaded[2].tool_results[0].content == "contents"
    assert 600 in fake.ttls.values()


def test_recall_cache_and_delete(tmp_path: Path) -> None:
    cache, fake = _cache(tmp_path)
    assert cache.set_recall("database", "SQLite memory", ["project:1"])
    assert cache.get_recall("database") == ("SQLite memory", ["project:1"])
    assert 30 in fake.ttls.values()

    cache.store_context("session-1", [Message(role="user", content="hello")])
    cache.delete_context("session-1")
    assert cache.load_context("session-1") is None


def test_oversized_context_skips_cache(
    tmp_path: Path, monkeypatch
) -> None:
    cache, fake = _cache(tmp_path)
    monkeypatch.setattr(redis_cache_module, "MAX_CONTEXT_PAYLOAD_BYTES", 32)
    stored = cache.store_context(
        "large-session", [Message(role="user", content="x" * 100)]
    )
    assert stored is False
    assert fake.values == {}


def test_session_resume_uses_only_fresh_cached_context(tmp_path: Path) -> None:
    manager = SessionManager(str(tmp_path))
    session = manager.create()
    session.append(Message(role="user", content="durable message"))
    session_id = session.session_id
    last_active = session.meta.last_active.timestamp()
    session.close()

    stale = manager.resume(
        session_id,
        cached_context=([Message(role="user", content="stale cache")], 0.0),
    )
    assert stale is not None
    assert stale.messages[0].content == "durable message"
    stale.session.close()

    fresh = manager.resume(
        session_id,
        cached_context=(
            [Message(role="user", content="fresh cache")],
            max(last_active, datetime.now(timezone.utc).timestamp()) + 1,
        ),
    )
    assert fresh is not None
    assert fresh.messages[0].content == "fresh cache"
    fresh.session.close()
