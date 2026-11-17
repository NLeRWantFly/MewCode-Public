"""Guards against persisting a collaboration protocol the kernel cannot read.

The store is the last boundary before durable state.  These tests exercise the
real ``ExpertRunStore`` produced by ``tests.test_collaboration_mode.store_for``
rather than a private stub.
"""
from __future__ import annotations

import pytest

from tests.test_collaboration_mode import RUN_ID, store_for


def _paused_store(tmp_path):
    store = store_for(tmp_path)
    store.mutate(RUN_ID, lambda record: record.update(status="paused"), durable=True)
    return store


def test_valid_collaborative_mutate_and_maintenance_pass(tmp_path):
    store = store_for(tmp_path)
    record = store.mutate(RUN_ID, lambda r: r.update(task="更新后的目标"), durable=True)
    assert record["task"] == "更新后的目标"
    assert record["collaboration"] is not None

    store.mutate(RUN_ID, lambda r: r.update(status="paused"), durable=True)
    stored = store.load(RUN_ID)
    assert stored["status"] == "paused"
    result = store.mutate_stopped(
        RUN_ID, lambda r: r.update(task="维护后的目标"),
        conversation_id="conversation", expected_updated_at=stored["updated_at"])
    assert result["task"] == "维护后的目标"
    assert result["collaboration"] is not None


def test_callback_cannot_persist_unknown_format_or_capabilities(tmp_path):
    store = store_for(tmp_path)
    before = store.load(RUN_ID)

    def change_format(record):
        record["collaboration"]["format"] = "unknown-protocol"

    with pytest.raises(ValueError):
        store.mutate(RUN_ID, change_format, durable=True)
    assert store.load(RUN_ID) == before

    def add_capability(record):
        record["collaboration"].setdefault("required_capabilities", []).append(
            "unknown-capability")

    with pytest.raises(ValueError):
        store.mutate(RUN_ID, add_capability, durable=True)
    assert store.load(RUN_ID) == before


def test_mutate_stopped_cannot_switch_mode_to_classic(tmp_path):
    store = _paused_store(tmp_path)
    stored = store.load(RUN_ID)
    with pytest.raises(ValueError):
        store.mutate_stopped(
            RUN_ID, lambda r: r.update(orchestration_mode="classic"),
            conversation_id="conversation", expected_updated_at=stored["updated_at"])
    assert store.load(RUN_ID)["orchestration_mode"] == "collaborative"


def test_unknown_existing_collaboration_is_readable_but_not_writable(tmp_path):
    store = _paused_store(tmp_path)
    # Simulate a record already loaded in memory by a newer program.
    store._live[RUN_ID]["collaboration"]["format"] = "unknown-from-newer-program"
    stored = store.load(RUN_ID)
    assert stored["collaboration"]["format"] == "unknown-from-newer-program"

    calls: list[str] = []
    with pytest.raises(ValueError):
        store.mutate(RUN_ID, lambda r: calls.append("mutate"), durable=True)
    with pytest.raises(ValueError):
        store.mutate_stopped(
            RUN_ID, lambda r: calls.append("maintenance"),
            conversation_id="conversation", expected_updated_at=stored["updated_at"])
    assert calls == []
    assert store.load(RUN_ID) == stored
