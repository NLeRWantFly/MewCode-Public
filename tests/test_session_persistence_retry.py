from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from mewcode.conversation import ConversationManager, Message
from mewcode.memory.session import (
    RecordType, SessionAppendMetadataError, SessionManager, SessionMeta, SessionRecord,
)
from mewcode.orchestration.chat_routing import prepare_chat_focus
from mewcode.teams import fsio


def denied(code=5):
    exc = PermissionError("synthetic replace denied")
    exc.winerror = code
    return exc


@pytest.mark.parametrize("code", [5, 32, 33])
def test_retry_only_replace_with_same_complete_temp(tmp_path, monkeypatch, code):
    target = tmp_path / "中文 空格" / "session.meta"
    fsio.atomic_write_text(target, "old")
    real_replace = fsio.os.replace
    attempts, sleeps = [], []

    def replace(source, destination):
        attempts.append(source)
        assert source.read_text(encoding="utf-8") == "new"
        assert target.read_text(encoding="utf-8") == "old"
        if len(attempts) < 3:
            raise denied(code)
        return real_replace(source, destination)

    monkeypatch.setattr(fsio.os, "replace", replace)
    monkeypatch.setattr(fsio.time, "sleep", sleeps.append)
    fsio.atomic_write_text(target, "new")
    assert len(set(attempts)) == 1
    assert sleeps == [0.05, 0.1]
    assert target.read_text(encoding="utf-8") == "new"
    assert not list(target.parent.glob("*.tmp-*"))


@pytest.mark.parametrize("code,expected_attempts", [(5, 6), (32, 6), (33, 6), (2, 1), (None, 1)])
def test_persistent_failure_preserves_original_and_is_bounded(tmp_path, monkeypatch, code, expected_attempts):
    target = tmp_path / "session.meta"
    fsio.atomic_write_text(target, "original")
    attempts, sleeps = [], []
    error = denied(code)

    def replace(*args):
        attempts.append(args)
        raise error

    monkeypatch.setattr(fsio.os, "replace", replace)
    monkeypatch.setattr(fsio.time, "sleep", sleeps.append)
    with pytest.raises(PermissionError) as caught:
        fsio.atomic_write_text(target, "replacement")
    assert caught.value is error
    assert len(attempts) == expected_attempts
    assert sum(sleeps) <= 1.55
    assert target.read_text(encoding="utf-8") == "original"
    assert not list(tmp_path.glob("*.tmp-*"))


@pytest.mark.parametrize("raw_record", [False, True])
def test_append_failure_retry_meta_only_does_not_duplicate(tmp_path, monkeypatch, raw_record):
    manager = SessionManager(str(tmp_path))
    session = manager.create()
    path = manager._sessions_dir / f"{session.session_id}.meta"
    log = path.with_suffix(".jsonl")
    original = path.read_bytes()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(fsio.os, "replace", lambda *args: (_ for _ in ()).throw(denied()))
            patch.setattr(fsio.time, "sleep", lambda _: None)
            with pytest.raises(SessionAppendMetadataError) as caught:
                if raw_record:
                    session.append_record(SessionRecord(
                        type=RecordType.USER, content="hello", timestamp=datetime.now(timezone.utc),
                    ))
                else:
                    session.append(Message(role="user", content="hello"))
            assert caught.value.log_appended is True
            assert caught.value.__cause__.winerror == 5
            assert "请勿重复发送" in str(caught.value)
            assert path.read_bytes() == original
            assert len(log.read_text(encoding="utf-8").splitlines()) == 1
        session.save_meta()  # Deliberate metadata-only recovery, never append().
        assert len(log.read_text(encoding="utf-8").splitlines()) == 1
        assert SessionMeta.load(path).message_count == (0 if raw_record else 1)
    finally:
        session.close()
    restored = manager.resume(session.session_id)
    assert restored is not None
    try:
        assert [m.content for m in restored.messages] == ["hello"]
    finally:
        restored.session.close()


def test_transient_append_failure_survives_restart_without_duplicates(tmp_path, monkeypatch):
    manager = SessionManager(str(tmp_path))
    session = manager.create()
    real_replace = fsio.os.replace
    attempts = []

    def replace(*args):
        attempts.append(args)
        if len(attempts) < 3:
            raise denied(32)
        return real_replace(*args)

    monkeypatch.setattr(fsio.os, "replace", replace)
    monkeypatch.setattr(fsio.time, "sleep", lambda _: None)
    try:
        session.append(Message(role="user", content="exactly once"))
    finally:
        session.close()
    restored = SessionManager(str(tmp_path)).resume(session.session_id)
    try:
        assert restored.session.meta.message_count == 1
        assert [m.content for m in restored.messages] == ["exactly once"]
    finally:
        restored.session.close()


def test_failed_focus_save_does_not_install_task_identity(tmp_path, monkeypatch):
    session = SessionManager(str(tmp_path)).create()
    runtime = SimpleNamespace(session=session, conversation=ConversationManager())
    previous = session.meta.dialogue_focus
    monkeypatch.setattr(fsio.os, "replace", lambda *args: (_ for _ in ()).throw(denied()))
    monkeypatch.setattr(fsio.time, "sleep", lambda _: None)
    try:
        with pytest.raises(PermissionError):
            prepare_chat_focus(runtime, "new task")
        assert session.meta.dialogue_focus is previous
        assert not runtime.conversation.history
    finally:
        session.close()


def test_concurrent_appends_and_readers_share_session_lock(tmp_path):
    manager = SessionManager(str(tmp_path))
    session = manager.create()
    path = manager._sessions_dir / f"{session.session_id}.meta"

    def operation(i):
        session.append(Message(role="user", content=str(i)))
        assert SessionMeta.load(path) is not None

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(operation, range(40)))
        assert SessionMeta.load(path).message_count == 40
        assert len(path.with_suffix(".jsonl").read_text(encoding="utf-8").splitlines()) == 40
    finally:
        session.close()


def test_meta_reader_waits_for_atomic_write(tmp_path, monkeypatch):
    path = tmp_path / "session.meta"
    meta = SessionMeta(id="test")
    meta.save(path)
    real_replace = fsio.os.replace
    entered, release, reader_started = threading.Event(), threading.Event(), threading.Event()

    def replace(*args):
        entered.set()
        assert release.wait(5)
        return real_replace(*args)

    def read():
        reader_started.set()
        return SessionMeta.load(path)

    monkeypatch.setattr(fsio.os, "replace", replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        meta.title = "new"
        writer = pool.submit(meta.save, path)
        try:
            assert entered.wait(5)
            reader = pool.submit(read)
            assert reader_started.wait(5)
            assert not reader.done()
        finally:
            release.set()
        writer.result(timeout=5)
        assert reader.result(timeout=5).title == "new"


def test_newer_log_invalidates_stale_context_cache(tmp_path):
    manager = SessionManager(str(tmp_path))
    session = manager.create()
    session.append(Message(role="user", content="durable log"))
    session.close()
    path = manager._sessions_dir / f"{session.session_id}.meta"
    stamp = path.stat().st_mtime_ns
    os.utime(path.with_suffix(".jsonl"), ns=(stamp + 10_000_000, stamp + 10_000_000))
    restored = manager.resume(session.session_id, cached_context=(
        [Message(role="user", content="stale cache")], stamp / 1e9 + 100,
    ))
    try:
        assert [m.content for m in restored.messages] == ["durable log"]
    finally:
        restored.session.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
def test_real_windows_reader_handle_released_during_retry(tmp_path):
    import ctypes
    from ctypes import wintypes

    target = tmp_path / "中文 空格.meta"
    fsio.atomic_write_text(target, "old")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(str(target), 0x80000000, 1, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value, ctypes.get_last_error()
    results = []
    timer = threading.Timer(0.15, lambda: results.append(bool(close(handle))))
    timer.start()
    try:
        fsio.atomic_write_text(target, "new")
    finally:
        timer.join(timeout=5)
    assert results == [True]
    assert target.read_text(encoding="utf-8") == "new"
