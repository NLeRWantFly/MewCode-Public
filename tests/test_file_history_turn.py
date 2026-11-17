from __future__ import annotations

from pathlib import Path

from mewcode.filehistory import FileHistory


def test_turn_changes_classifies_add_modify_delete(tmp_path: Path) -> None:
    work = tmp_path / "proj"
    work.mkdir()
    history = FileHistory(str(work), "sess-1")
    history.begin_turn()

    new_file = work / "added.txt"
    history.track_edit(str(new_file))
    new_file.write_text("hello", encoding="utf-8")

    existing = work / "existing.txt"
    existing.write_text("old", encoding="utf-8")
    history.track_edit(str(existing))
    existing.write_text("new", encoding="utf-8")

    doomed = work / "doomed.txt"
    doomed.write_text("bye", encoding="utf-8")
    history.track_edit(str(doomed))
    doomed.unlink()

    changes = {item["path"]: item["kind"] for item in history.describe_turn_changes()}
    assert changes["added.txt"] == "add"
    assert changes["existing.txt"] == "modify"
    assert changes["doomed.txt"] == "delete"


def test_make_snapshot_persists_and_clears_turn(tmp_path: Path) -> None:
    work = tmp_path / "proj"
    work.mkdir()
    history = FileHistory(str(work), "sess-2")
    history.begin_turn()
    target = work / "note.md"
    history.track_edit(str(target))
    target.write_text("# hi", encoding="utf-8")

    changes = history.make_snapshot(3, "summary")
    assert changes == [{"path": "note.md", "kind": "add"}]
    assert history.last_turn_changes == changes
    assert history.describe_turn_changes() == []

    loaded = history.load_turn_changes()
    assert loaded[3] == [{"path": "note.md", "kind": "add"}]
