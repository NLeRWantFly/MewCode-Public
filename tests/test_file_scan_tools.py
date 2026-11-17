from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

import mewcode.tools.grep as grep_module
from mewcode.tools.glob import Glob, Params as GlobParams
from mewcode.tools.grep import Grep, Params as GrepParams


@pytest.mark.asyncio
async def test_file_searches_are_recursive_and_prune_generated_trees(tmp_path) -> None:
    (tmp_path / "root.py").write_text("needle at root\n", encoding="utf-8")
    source = tmp_path / "src"
    source.mkdir()
    (source / "nested.py").write_text("needle nested\n", encoding="utf-8")
    ignored = tmp_path / ".tmp-large"
    ignored.mkdir()
    (ignored / "ignored.py").write_text("needle ignored\n", encoding="utf-8")
    state = tmp_path / ".mewcode"
    state.mkdir()
    (state / "state.py").write_text("needle state\n", encoding="utf-8")
    hidden = tmp_path / ".custom-cache"
    hidden.mkdir()
    (hidden / "hidden.py").write_text("needle hidden\n", encoding="utf-8")

    globbed = await Glob().execute(GlobParams(pattern="**/*.py", path=str(tmp_path)))
    searched = await Grep().execute(GrepParams(
        pattern="needle",
        path=str(tmp_path),
        include="*.py",
    ))

    assert globbed.is_error is False
    nested_path = str(Path("src") / "nested.py")
    assert globbed.output.splitlines() == ["root.py", nested_path]
    assert "root.py:1:needle at root" in searched.output
    assert f"{nested_path}:1:needle nested" in searched.output
    assert "ignored.py" not in searched.output
    assert "state.py" not in searched.output
    assert "hidden.py" not in searched.output


@pytest.mark.asyncio
async def test_grep_scan_does_not_block_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    started = threading.Event()
    release = threading.Event()

    def slow_scan(_base, _regex, _pattern, _cancel_requested) -> str:
        started.set()
        release.wait(timeout=1)
        return "No matches found."

    monkeypatch.setattr(grep_module, "_grep_files", slow_scan)
    task = asyncio.create_task(Grep().execute(GrepParams(
        pattern="anything",
        path=str(tmp_path),
    )))
    for _ in range(50):
        if started.is_set():
            break
        await asyncio.sleep(0.01)
    assert started.is_set()

    # A synchronous scan inside execute() would prevent this timer from firing.
    await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.1)
    assert not task.done()
    release.set()
    result = await asyncio.wait_for(task, timeout=0.5)
    assert result.output == "No matches found."


@pytest.mark.asyncio
async def test_cancelling_grep_signals_the_scan_thread(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    started = threading.Event()
    exited = threading.Event()

    def cancellable_scan(_base, _regex, _pattern, cancel_requested) -> str:
        started.set()
        cancel_requested.wait(timeout=1)
        exited.set()
        return "No matches found."

    monkeypatch.setattr(grep_module, "_grep_files", cancellable_scan)
    task = asyncio.create_task(Grep().execute(GrepParams(
        pattern="anything",
        path=str(tmp_path),
    )))
    for _ in range(50):
        if started.is_set():
            break
        await asyncio.sleep(0.01)
    assert started.is_set()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.2)

    assert await asyncio.to_thread(exited.wait, 0.5)
