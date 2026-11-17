from __future__ import annotations

from mewcode.teams.mailbox import Mailbox, create_message


def _msg(content="hello", to="bob", frm="alice", **kw):
    return create_message(from_agent=frm, to_agent=to, content=content, summary="s", **kw)


def test_write_then_read_preserves_order(tmp_path):
    mb = Mailbox(tmp_path)
    for i in range(5):
        mb.write("bob", _msg(content=f"m{i}"))
    got = mb.read("bob")
    assert [m.content for m in got] == [f"m{i}" for i in range(5)]
    # read 不消费
    assert len(mb.read("bob")) == 5


def test_consume_removes_messages(tmp_path):
    mb = Mailbox(tmp_path)
    mb.write("bob", _msg())
    first = mb.consume("bob")
    assert len(first) == 1
    assert mb.consume("bob") == []


def test_consume_empty_dir(tmp_path):
    mb = Mailbox(tmp_path)
    assert mb.consume("nobody") == []


def test_broadcast_excludes_sender(tmp_path):
    mb = Mailbox(tmp_path)
    msg = _msg(to="*")
    mb.broadcast(["a", "b", "c"], msg, exclude="b")
    assert len(mb.read("a")) == 1
    assert mb.read("b") == []
    assert len(mb.read("c")) == 1


def test_corrupt_message_file_is_skipped(tmp_path):
    mb = Mailbox(tmp_path)
    mb.write("bob", _msg())
    d = tmp_path / "bob"
    (d / "0000000000.000000_bad.json").write_text("{not json", encoding="utf-8")
    got = mb.consume("bob")
    assert len(got) == 1


def test_cleanup_all(tmp_path):
    mb = Mailbox(tmp_path)
    mb.write("a", _msg())
    mb.write("b", _msg())
    mb.cleanup_all()
    assert mb.read("a") == []
    assert mb.read("b") == []
