"""The spoken-authorization path: detect the intent, offer it, never grant it.

The contract under test: a message that sounds like permission only records an
*offer* plus a short explanation; nothing becomes effective until the user
clicks through the grant endpoint (which requires the explicit UI header).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from frontend import server


def runtime_with(offers=None):
    return SimpleNamespace(pending_file_grants=list(offers or []))


class TestIntentRecorder:
    def test_permission_records_an_offer_and_explains_the_gap(self):
        runtime = runtime_with()
        guide = server._record_authorization_intent(runtime, "这个文件你可以改：docs/03.md")
        assert runtime.pending_file_grants == [{"path": "docs/03.md", "access": "read_write"}]
        assert "docs/03.md" in guide
        assert "读写授权" in guide
        assert "不会自动生效" in guide

    def test_read_only_intent_records_read(self):
        runtime = runtime_with()
        guide = server._record_authorization_intent(runtime, "你可以看一下 docs/02.md")
        assert runtime.pending_file_grants == [{"path": "docs/02.md", "access": "read"}]
        assert "只读授权" in guide

    def test_intent_without_a_path_asks_for_one(self):
        runtime = runtime_with()
        guide = server._record_authorization_intent(runtime, "这个文件你改一下")
        assert runtime.pending_file_grants == []
        assert "没有说出具体路径" in guide
        assert "文件授权" in guide

    @pytest.mark.parametrize("text", [
        "帮我下载这篇论文",
        "这个文件不要改",
        "do not edit docs/x.md",
        "",
    ])
    def test_no_intent_records_nothing(self, text):
        runtime = runtime_with()
        assert server._record_authorization_intent(runtime, text) == ""
        assert runtime.pending_file_grants == []

    def test_repeat_upgrades_read_to_read_write_and_never_downgrades(self):
        runtime = runtime_with([{"path": "docs/a.md", "access": "read"}])
        server._record_authorization_intent(runtime, "允许你修改 docs/a.md")
        assert runtime.pending_file_grants == [{"path": "docs/a.md", "access": "read_write"}]
        server._record_authorization_intent(runtime, "你可以看一下 docs/a.md")
        assert runtime.pending_file_grants == [{"path": "docs/a.md", "access": "read_write"}]


class TestPendingGrantsEndpoint:
    def test_no_active_runtime_returns_an_empty_list(self, monkeypatch):
        monkeypatch.setattr(server, "SESSION", SimpleNamespace(conversation_runs={}))
        assert server._pending_file_grants() == []

    def test_offers_are_copied_not_handed_out_live(self, monkeypatch):
        runtime = runtime_with([{"path": "docs/a.md", "access": "read_write"}])
        monkeypatch.setattr(
            server, "SESSION",
            SimpleNamespace(conversation_runs={"conv": runtime}, session=None, conversation_id="conv"),
        )
        rows = server._pending_file_grants()
        assert rows == [{"path": "docs/a.md", "access": "read_write"}]
        rows[0]["path"] = "tampered"
        assert runtime.pending_file_grants[0]["path"] == "docs/a.md"

    def test_garbage_entries_are_dropped(self, monkeypatch):
        runtime = runtime_with(["nope", {"path": "docs/a.md"}, None, 3])
        monkeypatch.setattr(
            server, "SESSION",
            SimpleNamespace(conversation_runs={"conv": runtime}, session=None, conversation_id="conv"),
        )
        assert server._pending_file_grants() == [{"path": "docs/a.md"}]

    def test_runtime_without_the_attribute_is_tolerated(self, monkeypatch):
        monkeypatch.setattr(
            server, "SESSION",
            SimpleNamespace(conversation_runs={"conv": SimpleNamespace()}, session=None, conversation_id="conv"),
        )
        assert server._pending_file_grants() == []
