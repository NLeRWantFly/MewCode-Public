"""Chat-driven canvas reports: bounded store + run-state fallback."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mewcode.orchestration.chat_reports import ChatReportStore, store_path


def _store(tmp_path: Path) -> ChatReportStore:
    return ChatReportStore(root=tmp_path)


def test_report_produces_a_run_shaped_snapshot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.report(
        work_dir=tmp_path, conversation_id="c1", node_id="n_p2",
        label="改写 3.2 节", status="running", summary="22 处跨度",
    )
    snapshot = store.snapshot(work_dir=tmp_path, conversation_id="c1")
    assert snapshot is not None
    assert snapshot["run_id"] == "chat:c1"
    assert snapshot["source"] == "chat_report"
    assert snapshot["active"] is True
    state = snapshot["node_states"]["n_p2"]
    assert state["status"] == "running"
    assert state["label"] == "改写 3.2 节"
    assert state["summary"] == "22 处跨度"
    # The canvas badge set is the expert-run status vocabulary.
    assert state["started_at"] and state["updated_at"]


def test_reports_roll_up_to_a_conversation_status(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for node in ("a", "b"):
        store.report(work_dir=tmp_path, conversation_id="c1", node_id=node, status="completed")
    assert store.snapshot(work_dir=tmp_path, conversation_id="c1")["status"] == "completed"

    store.report(work_dir=tmp_path, conversation_id="c1", node_id="c", status="failed")
    failed = store.snapshot(work_dir=tmp_path, conversation_id="c1")
    assert failed["status"] == "failed"
    assert failed["active"] is False

    # A new step puts the conversation back in flight.
    store.report(work_dir=tmp_path, conversation_id="c1", node_id="d", status="running")
    assert store.snapshot(work_dir=tmp_path, conversation_id="c1")["status"] == "running"


def test_unknown_status_degrades_to_running(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.report(work_dir=tmp_path, conversation_id="c1", node_id="n", status="whatever")
    assert store.snapshot(work_dir=tmp_path, conversation_id="c1")["node_states"]["n"]["status"] == "running"


def test_snapshot_is_none_until_something_is_reported(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.snapshot(work_dir=tmp_path, conversation_id="c1") is None
    assert store.snapshot(work_dir=tmp_path, conversation_id="") is None


def test_process_files_expose_metadata_only_and_previews_stay_behind(tmp_path: Path) -> None:
    """The canvas poll must never carry base64 samples."""
    store = ChatReportStore()
    record = {
        "id": "f1", "name": "review.md", "path": "/w/review.md", "size": 12,
        "sha256": "abc", "node_id": "", "tool_id": "t1", "node_attempt": 1,
        "preview_type": "text", "preview": "hello", "source": "successful_write",
    }
    assert store.record_file(
        work_dir=tmp_path, conversation_id="c1", record=record, node_id="n_p2"
    )
    snapshot = store.snapshot(work_dir=tmp_path, conversation_id="c1")
    assert all("preview" not in item for item in snapshot["process_files"])
    assert snapshot["process_files"][0]["preview_type"] == "text"
    assert snapshot["process_files"][0]["node_id"] == "n_p2"
    preview = store.preview(work_dir=tmp_path, conversation_id="c1", file_id="f1")
    assert preview["preview"] == "hello"
    assert store.preview(work_dir=tmp_path, conversation_id="c1", file_id="missing") is None
    # Same receipt id is never stored twice.
    assert not store.record_file(work_dir=tmp_path, conversation_id="c1", record=record)


def test_reports_are_isolated_per_conversation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.report(work_dir=tmp_path, conversation_id="c1", node_id="n", status="running")
    assert store.snapshot(work_dir=tmp_path, conversation_id="c2") is None
    assert store.clear(work_dir=tmp_path, conversation_id="c1") is True
    assert store.snapshot(work_dir=tmp_path, conversation_id="c1") is None
    assert store.clear(work_dir=tmp_path, conversation_id="c1") is False


def test_report_store_path_lives_next_to_the_project_state(tmp_path: Path) -> None:
    assert store_path(tmp_path) == tmp_path / ".mewcode" / "chat-reports.json"


def test_corrupt_store_never_breaks_the_canvas(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    store = _store(tmp_path)
    assert store.snapshot(work_dir=tmp_path, conversation_id="c1") is None
    store.report(work_dir=tmp_path, conversation_id="c1", node_id="n", status="running")
    assert store.snapshot(work_dir=tmp_path, conversation_id="c1") is not None


# ---------- server wiring ----------


def _fake_graph(*nodes):
    return SimpleNamespace(
        nodes=[SimpleNamespace(id=node_id, label=label, title="") for node_id, label in nodes]
    )


def test_canvas_node_resolution_prefers_id_then_label_then_unique_substring(monkeypatch, tmp_path):
    from frontend import server

    monkeypatch.setattr(
        server, "_stored_orchestration_graph",
        lambda *_a, **_k: _fake_graph(("n_p2", "3.2 节方向中性化（L199-L246）")),
    )
    monkeypatch.setattr(server.SESSION, "work_dir", str(tmp_path), raising=False)

    assert server._resolve_canvas_node("n_p2") == "n_p2"
    assert server._resolve_canvas_node("3.2 节方向中性化（L199-L246）") == "n_p2"
    assert server._resolve_canvas_node("3.2 节方向中性化") == "n_p2"
    # No canvas node owns it: keep the reported name rather than dropping it.
    assert server._resolve_canvas_node("不存在的阶段") == "不存在的阶段"
    assert server._resolve_canvas_node("") == ""


def test_run_state_falls_back_to_chat_reports(monkeypatch, tmp_path):
    from frontend import server

    monkeypatch.setattr(server.SESSION, "work_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(server, "_resolve_project_run", lambda: None)
    monkeypatch.setattr(server, "_active_conversation_id", lambda: "c1")
    monkeypatch.setattr(server, "CHAT_REPORTS", ChatReportStore(root=tmp_path))

    assert server._orchestration_live_run() is None
    server.CHAT_REPORTS.report(
        work_dir=tmp_path, conversation_id="c1", node_id="n_p2",
        label="送审", status="running",
    )
    run = server._orchestration_live_run()
    assert run["run_id"] == "chat:c1"
    assert run["node_states"]["n_p2"]["status"] == "running"
    # A caller pinned to another run id must not receive the chat report.
    assert server._orchestration_live_run("expert-1") is None


def test_expert_run_still_wins_over_chat_reports(monkeypatch, tmp_path):
    from frontend import server

    monkeypatch.setattr(server.SESSION, "work_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(server, "_active_conversation_id", lambda: "c1")
    monkeypatch.setattr(server, "CHAT_REPORTS", ChatReportStore(root=tmp_path))
    server.CHAT_REPORTS.report(
        work_dir=tmp_path, conversation_id="c1", node_id="chat-node", status="running"
    )
    monkeypatch.setattr(
        server, "_resolve_project_run",
        lambda: {"run_id": "r1", "conversation_id": "c1", "status": "running",
                 "node_states": {"expert-node": {"status": "running"}}},
    )
    run = server._orchestration_live_run()
    assert run["run_id"] == "r1"
    assert "expert-node" in run["node_states"]


@pytest.mark.parametrize("tool", ["WriteFile", "EditFile", "ReportArtifact"])
def test_file_snapshot_tool_set_matches_the_orchestration_loop(tool):
    from frontend import server

    assert server.tool_writes_files(tool)
    assert not server.tool_writes_files("Bash")
    assert not server.tool_writes_files("ReadFile")


def test_mirror_report_step_records_the_canvas_node(monkeypatch, tmp_path):
    from frontend import server

    monkeypatch.setattr(server.SESSION, "work_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(server, "_active_conversation_id", lambda: "c1")
    monkeypatch.setattr(server, "CHAT_REPORTS", ChatReportStore(root=tmp_path))
    monkeypatch.setattr(server, "_resolve_canvas_node", lambda value: "n_p2" if value else "")

    node = server._mirror_chat_report_step(
        arguments={"label": "送审（review 角色）", "status": "running",
                   "node": "3.2 节方向中性化", "summary": "review.in.md 已组装"},
        output="STEP\trunning\t送审（review 角色）",
    )
    assert node == "n_p2"
    state = server.CHAT_REPORTS.snapshot(work_dir=tmp_path, conversation_id="c1")["node_states"]["n_p2"]
    assert state["status"] == "running"
    assert state["summary"] == "review.in.md 已组装"

    # A later step without a node keeps reporting on the same canvas node.
    assert server._mirror_chat_report_step(
        arguments={"label": "复核", "status": "completed"}, output=""
    ) == "n_p2"
    assert server.CHAT_REPORTS.snapshot(
        work_dir=tmp_path, conversation_id="c1"
    )["node_states"]["n_p2"]["status"] == "completed"


def test_mirror_report_step_without_a_canvas_node_is_skipped(monkeypatch, tmp_path):
    from frontend import server

    monkeypatch.setattr(server.SESSION, "work_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(server, "_active_conversation_id", lambda: "c1")
    monkeypatch.setattr(server, "CHAT_REPORTS", ChatReportStore(root=tmp_path))
    monkeypatch.setattr(server, "_resolve_canvas_node", lambda value: "")
    # Out of order: the fallback map is global, so clear any earlier entry.
    server._CHAT_REPORT_NODES.pop("c1", None)

    assert server._mirror_chat_report_step(
        arguments={"label": "无节点步骤", "status": "running"}, output=""
    ) == ""
    assert server.CHAT_REPORTS.snapshot(work_dir=tmp_path, conversation_id="c1") is None
