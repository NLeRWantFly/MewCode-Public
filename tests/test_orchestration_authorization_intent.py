"""Conservatism tests for file-authorization intent detection.

The rule these cases lock: explicit permission wording may offer a grant on its
own; a bare instruction only counts when it names a file. A false offer is
worse than a missed one, because it looks like the system angling for more
permissions.
"""
from __future__ import annotations

import pytest

from mewcode.orchestration.authorization_intent import (
    AuthorizationIntent,
    detect_authorization_intent,
    extract_paths,
    merge_offer,
)


class TestDetection:
    @pytest.mark.parametrize("text, access, paths", [
        ("这个文件你可以改：docs/03-元素对比分析.md", "read_write", ["docs/03-元素对比分析.md"]),
        ("我允许你修改 docs/01-论文清单.md 和 figures/a.png", "read_write",
         ["docs/01-论文清单.md", "figures/a.png"]),
        ("不用问我，直接改 `scripts/build.py`", "read_write", ["scripts/build.py"]),
        ("论文的这段可以重写一下：docs/04.md", "read_write", ["docs/04.md"]),
        ("改一下 docs/03.md", "read_write", ["docs/03.md"]),
        ("你可以看一下 docs/02.md", "read", ["docs/02.md"]),
        ("授权你读取 data/raw.csv", "read", ["data/raw.csv"]),
        ("you may edit docs/summary.md", "read_write", ["docs/summary.md"]),
        ("feel free to update the changelog.md", "read_write", ["changelog.md"]),
        ("please update the abstract in docs/00.md", "read_write", ["docs/00.md"]),
    ])
    def test_permission_and_instruction_with_paths(self, text, access, paths):
        intent = detect_authorization_intent(text)
        assert isinstance(intent, AuthorizationIntent)
        assert intent.access == access
        assert intent.paths == paths
        assert intent.needs_path is False
        assert intent.phrase

    @pytest.mark.parametrize("text", [
        "这个文件不要改，先别动 paper.tex",
        "别改，我说的是不允许修改 config.yaml",
        "禁止编辑 docs/secret.md",
        "先别动 docs/wip.tex，等我看完",
        "不允许你改 docs/x.md",
        "我没有授权你改 docs/y.md",
        "do not edit docs/x.md",
        "without my permission, never touch docs/z.md",
    ])
    def test_negation_cancels_the_offer(self, text):
        assert detect_authorization_intent(text) is None

    @pytest.mark.parametrize("text", [
        "帮我下载这篇论文",
        "请修改论文的结论段",
        "你把这部分重写成 200 字",
        "现在不用管 docs/old.md",
        "",
        "   ",
        "先总结一下这篇论文的方法",
        "看看 docs/ 下面有什么",
    ])
    def test_plain_work_is_not_an_authorization(self, text):
        assert detect_authorization_intent(text) is None

    def test_permission_without_a_path_asks_which_file(self):
        intent = detect_authorization_intent("这个文件你改一下")
        assert intent is not None
        assert intent.needs_path is True
        assert intent.paths == []

    def test_a_protected_path_is_never_adopted_from_another_clause(self):
        intent = detect_authorization_intent("这段你可以改，但不要动 docs/locked.md")
        assert intent is not None
        assert "docs/locked.md" not in intent.paths

    def test_single_known_path_resolves_a_bare_reference(self):
        intent = detect_authorization_intent(
            "这个文件你可以改", known_paths=["docs/only.md"],
        )
        assert intent is not None and intent.paths == ["docs/only.md"]
        intent = detect_authorization_intent(
            "这个文件你可以改", known_paths=["docs/a.md", "docs/b.md"],
        )
        assert intent is not None and intent.needs_path is True

    def test_long_messages_are_scanned_within_bounds(self):
        text = "背景：" + "说明" * 5000 + " 你可以改 docs/big.md"
        intent = detect_authorization_intent(text)
        assert intent is None or intent.paths == [] or intent.paths == ["docs/big.md"]


class TestPathExtraction:
    @pytest.mark.parametrize("text, expected", [
        ("改 `docs/a.md`", ["docs/a.md"]),
        ('改 "docs/a.md"', ["docs/a.md"]),
        ("改 docs/sub/dir/", ["docs/sub/dir/"]),
        ("改 " + chr(92).join(["C:", "work", "paper.md"]), ["C:/work/paper.md"]),
        ("改 docs/a.md 和 docs/a.md", ["docs/a.md"]),
        ("看看 https://example.com/docs/a.md", []),
        ("改 -", []),
        ("改 ../", []),
    ])
    def test_path_shapes(self, text, expected):
        assert extract_paths(text) == expected


class TestOfferMerge:
    def test_newest_first_and_read_upgrades_to_read_write(self):
        offers = merge_offer([], AuthorizationIntent(access="read", paths=["a.md"]))
        assert offers == [{"path": "a.md", "access": "read"}]
        offers = merge_offer(offers, AuthorizationIntent(access="read_write", paths=["a.md"]))
        assert offers == [{"path": "a.md", "access": "read_write"}]
        offers = merge_offer(offers, AuthorizationIntent(access="read", paths=["b.md"]))
        assert [item["path"] for item in offers] == ["a.md", "b.md"]

    def test_read_write_never_downgrades_and_case_is_ignored(self):
        offers = merge_offer([{"path": "Docs/A.md", "access": "read_write"}],
                            AuthorizationIntent(access="read", paths=["docs/a.md"]))
        assert offers == [{"path": "Docs/A.md", "access": "read_write"}]

    def test_needs_path_adds_nothing(self):
        assert merge_offer([], AuthorizationIntent(access="read", needs_path=True)) == []

    def test_offer_list_is_bounded(self):
        offers: list[dict] = []
        for index in range(10):
            offers = merge_offer(offers, AuthorizationIntent(access="read", paths=[f"docs/{index}.md"]))
        assert len(offers) == 5
        assert offers[-1]["path"] == "docs/9.md"
