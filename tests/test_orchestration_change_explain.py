"""Node change explanations: diff parsing, attribution, and honest gaps.

Isolated temporary repositories only; no user work tree is read or modified.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest

from mewcode.orchestration.change_explain import (
    node_change_report,
    parse_conflict_markers,
    parse_unified_diff,
    relative_path,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")


def _git(repo, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _init_repo(path) -> None:
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "tester@example.com")
    _git(path, "config", "user.name", "Tester")
    _git(path, "config", "commit.gpgsign", "false")


def test_parse_unified_diff_keeps_ranges_section_and_line_kinds() -> None:
    text = (
        "M\tsrc/app.py\n"
        "diff --git a/src/app.py b/src/app.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -10,6 +10,7 @@ def handler(request):\n"
        " context line\n"
        "-old line\n"
        "+new line\n"
        "+extra line\n"
        " trailing context\n"
    )
    parsed = parse_unified_diff(text)
    assert parsed["status"] == "M"
    assert parsed["additions"] == 2
    assert parsed["deletions"] == 1
    assert parsed["hunk_total"] == 1
    hunk = parsed["hunks"][0]
    assert hunk["old_range"] == "10–15"
    assert hunk["new_range"] == "10–16"
    assert hunk["section"] == "def handler(request):"
    assert hunk["changed"] == 3
    assert [line["kind"] for line in hunk["lines"]] == ["ctx", "del", "add", "add", "ctx"]
    assert hunk["lines"][2]["text"] == "new line"


def test_parse_unified_diff_ignores_header_noise_and_marks_added_status() -> None:
    text = (
        "A\tnew.txt\n"
        "diff --git a/new.txt b/new.txt\n"
        "new file mode 100644\n"
        "index 0000000..3333333\n"
        "@@ -0,0 +1,2 @@\n"
        "+first\n"
        "+second\n"
    )
    parsed = parse_unified_diff(text)
    assert parsed["status"] == "A"
    assert parsed["additions"] == 2
    assert parsed["deletions"] == 0
    assert parsed["hunks"][0]["old_range"] == ""
    assert parsed["hunks"][0]["new_range"] == "1–2"


def test_relative_path_rejects_paths_outside_the_repository(tmp_path) -> None:
    inside = tmp_path / "pkg" / "mod.py"
    inside.parent.mkdir()
    inside.write_text("x", encoding="utf-8")
    assert relative_path(str(tmp_path), inside) == "pkg/mod.py"
    assert relative_path(str(tmp_path), "pkg/mod.py") == "pkg/mod.py"
    assert relative_path(str(tmp_path), "../escape.py") == ""


@pytest.mark.asyncio
async def test_node_change_report_attributes_files_and_shows_real_content(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    tracked = repo / "tracked.py"
    tracked.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-qm", "initial")
    tracked.write_text("alpha\nBETA\ngamma\ndelta\n", encoding="utf-8")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-qm", "change tracked")
    fresh = repo / "fresh.py"
    fresh.write_text("print('new file')\n", encoding="utf-8")

    report = await node_change_report(
        str(repo),
        [
            {"node_id": "n1", "path": str(tracked), "node_attempt": 1, "size": tracked.stat().st_size},
            {"node_id": "n2", "path": str(fresh), "node_attempt": 1, "size": fresh.stat().st_size},
        ],
    )

    assert report["is_repo"] is True
    assert report["head"]["subject"] == "change tracked"

    first = report["nodes"]["n1"]["files"][0]
    assert first["path"] == "tracked.py"
    assert first["commit"]["subject"] == "change tracked"
    assert first["additions"] == 2 and first["deletions"] == 1
    assert first["hunks"][0]["lines"][1]["kind"] == "del"
    assert first["hunks"][0]["lines"][1]["text"] == "beta"
    added = [line["text"] for line in first["hunks"][0]["lines"] if line["kind"] == "add"]
    assert added == ["BETA", "delta"]

    second = report["nodes"]["n2"]["files"][0]
    assert second["path"] == "fresh.py"
    assert second["commit"] is None
    assert second["dirty"] == "untracked"
    assert second["hunks"][0]["lines"][0]["text"] == "print('new file')"
    assert "未提交" in second["note"]

    assert report["nodes"]["n1"]["summary"] == {
        "files": 1, "additions": 2, "deletions": 1, "committed": 1, "uncommitted": 0, "hunks": 1,
    }


@pytest.mark.asyncio
async def test_node_change_report_keeps_latest_attempt_per_path(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "one")

    report = await node_change_report(
        str(repo),
        [
            {"node_id": "n1", "path": "a.txt", "node_attempt": 1, "created_at": 1},
            {"node_id": "n1", "path": "a.txt", "node_attempt": 4, "created_at": 2},
        ],
    )
    files = report["nodes"]["n1"]["files"]
    assert len(files) == 1
    assert files[0]["attempt"] == 4


@pytest.mark.asyncio
async def test_node_change_report_handles_non_repository(tmp_path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    inside_work_tree = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=plain,
        capture_output=True,
        text=True,
    ).stdout.strip() == "true"
    if inside_work_tree:
        # Happens when this suite is run with --basetemp pointing inside a git
        # work tree; the directory then *is* inside a repository, so the
        # non-repository branch cannot be exercised from here.
        pytest.skip("临时目录落在某个 git 工作树内，无法模拟非仓库目录")
    report = await node_change_report(str(plain), [])
    assert report["is_repo"] is False
    assert report["git_available"] is False
    assert report["nodes"] == {}
    assert report["errors"] and "git" in report["errors"][0]


@pytest.mark.asyncio
async def test_node_change_report_marks_unattributed_commit_files(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "mine.txt").write_text("mine\n", encoding="utf-8")
    (repo / "other.txt").write_text("other\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "two files")

    report = await node_change_report(
        str(repo),
        [{"node_id": "n1", "path": "mine.txt", "node_attempt": 1}],
    )
    unattributed = {row["path"] for row in report["unattributed"]}
    assert "other.txt" in unattributed
    assert "mine.txt" not in unattributed
    assert "mine.txt" in report["nodes"]["n1"]["files"][0]["path"]


def test_parse_conflict_markers_keeps_both_sides_and_ancestor() -> None:
    text = (
        "# 论文草稿\n"
        "intro\n"
        "<<<<<<< HEAD\n"
        "融合模块用光照感知注意力。\n"
        "||||||| merged common ancestors\n"
        "融合模块用简单相加。\n"
        "=======\n"
        "融合模块用双流 Transformer。\n"
        ">>>>>>> feature/cross-modal\n"
        "tail\n"
    )
    sections = parse_conflict_markers(text.splitlines())
    assert len(sections) == 1
    section = sections[0]
    assert section["start_line"] == 3
    assert section["ours_label"] == "HEAD"
    assert section["theirs_label"] == "feature/cross-modal"
    assert section["base_label"] == "merged common ancestors"
    assert section["ours"] == ["融合模块用光照感知注意力。"]
    assert section["base"] == ["融合模块用简单相加。"]
    assert section["theirs"] == ["融合模块用双流 Transformer。"]
    assert section["truncated"] is False


def test_parse_conflict_markers_ignores_content_that_merely_looks_like_markers() -> None:
    """A 10-char "=" underline or an 8-char "<" run is content, not a marker."""
    text = (
        "<<<<<<< HEAD\n"
        "标题\n"
        "==========\n"
        "说明\n"
        "<<<<<<<<\n"
        "=======\n"
        "另一版\n"
        ">>>>>>> theirs\n"
    )
    sections = parse_conflict_markers(text.splitlines())
    assert len(sections) == 1
    assert sections[0]["ours"] == ["标题", "==========", "说明", "<<<<<<<<"]
    assert sections[0]["theirs"] == ["另一版"]


def test_parse_conflict_markers_needs_a_space_before_the_label() -> None:
    assert parse_conflict_markers(["<<<<<<<HEAD", "a", "=======", "b", ">>>>>>>x"]) == []
    sections = parse_conflict_markers(["<<<<<<< HEAD", "a", "=======", "b", ">>>>>>> theirs"])
    assert len(sections) == 1 and sections[0]["ours_label"] == "HEAD"


def test_parse_conflict_markers_handles_two_sections_and_unclosed_tail() -> None:
    text = (
        "<<<<<<< ours\n"
        "a\n"
        "=======\n"
        "b\n"
        ">>>>>>> theirs\n"
        "middle\n"
        "<<<<<<< ours\n"
        "c\n"
    )
    sections = parse_conflict_markers(text.splitlines())
    assert [section["start_line"] for section in sections] == [1, 7]
    assert sections[0]["ours"] == ["a"] and sections[0]["theirs"] == ["b"]
    assert sections[1]["ours"] == ["c"] and sections[1]["theirs"] == []


@pytest.mark.asyncio
async def test_node_change_report_exposes_merge_conflicts_with_both_versions(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    paper = repo / "paper.md"
    paper.write_text("intro\n融合模块用简单相加。\n", encoding="utf-8")
    _git(repo, "add", "paper.md")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    paper.write_text("intro\n融合模块用双流 Transformer。\n", encoding="utf-8")
    _git(repo, "commit", "-aqm", "theirs side")
    _git(repo, "checkout", "-q", "-")
    paper.write_text("intro\n融合模块用光照感知注意力。\n", encoding="utf-8")
    _git(repo, "commit", "-aqm", "ours side")
    merge = subprocess.run(
        ["git", "merge", "feature"], cwd=repo, capture_output=True, text=True
    )
    assert merge.returncode != 0, "the fixture must leave a real merge conflict"

    report = await node_change_report(str(repo), [{"node_id": "n1", "path": "paper.md"}])

    assert len(report["conflicts"]) == 1
    conflict = report["conflicts"][0]
    assert conflict["path"] == "paper.md"
    assert conflict["conflict_total"] == 1
    assert conflict["conflicts"][0]["ours"] == ["融合模块用光照感知注意力。"]
    assert conflict["conflicts"][0]["theirs"] == ["融合模块用双流 Transformer。"]

    assert report["conflicts_total"] == 1
    assert report["conflicts_truncated"] is False

    # The same file, attributed to a stage, carries the sections too.
    file = report["nodes"]["n1"]["files"][0]
    assert file["status"] == "U"
    assert file["status_label"] == "冲突"
    assert file["unmerged"] is True
    assert file["conflicts"][0]["theirs"] == ["融合模块用双流 Transformer。"]
    # A conflicted file has no diff of its own: the conflict is the content.
    assert file["hunks"] == []
    assert file["additions"] == 0 and file["deletions"] == 0


@pytest.mark.asyncio
async def test_node_change_report_has_no_conflicts_on_a_clean_tree(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "a.md").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "a.md")
    _git(repo, "commit", "-qm", "a")
    report = await node_change_report(str(repo), [])
    assert report["conflicts"] == []
    assert report["conflicts_total"] == 0 and report["conflicts_truncated"] is False


def test_change_report_is_awaitable_from_sync_context(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "x.txt")
    _git(repo, "commit", "-qm", "x")
    report = asyncio.run(node_change_report(str(repo), []))
    assert report["is_repo"] is True
