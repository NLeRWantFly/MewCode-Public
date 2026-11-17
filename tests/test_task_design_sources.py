"""Task design sees complete user messages and field-scoped history provenance."""
import copy

import pytest

from mewcode.orchestration.effective_history import (
    apply_history_patch, coverage_sources, effective_document, new_basis,
)
from mewcode.orchestration.workflow_draft import empty_document


def source(source_id, role, text, line):
    return {"id": source_id, "role": role, "text": text, "line": line,
            "part": 1, "parts": 1, "kind": "message"}


def fact(entry_id, src, *, body=None, kind="constraint", **data):
    return {"id": entry_id, "kind": kind, "title": entry_id,
            "body": body if body is not None else src["text"], "state": "active",
            "data": data, "sources": [{"source_id": src["id"], "quote": src["text"]}]}


def patch(*items, ignored=()):
    return {"upserts": list(items), "ignored": list(ignored)}


def test_coverage_sources_keeps_complete_user_messages_and_history_context():
    old = source("u0", "user", "处理四本书；另需保留目录。运行错误：ValueError", 1)
    assistant = source("a1", "assistant", "建议使用旧流程", 2)
    latest = source("u2", "user", "把数据库改为 SQLite，同时保留导出目录", 3)
    control = source("u3", "user", "改图：连上 SQLite", 4)
    control["purpose"] = "graph_repair_protocol"
    sources = [old, assistant, latest, control]
    basis = new_basis(empty_document(), [row["id"] for row in sources])
    basis = apply_history_patch(
        basis,
        patch(fact("storage", old, body="数据库使用 MySQL"),
              ignored=[{"source_id": old["id"], "quote": "运行错误：ValueError",
                        "reason": "运行日志", "category": "runtime"}]),
        [old], sources,
    )
    revised = fact("storage", latest, body="数据库使用 SQLite")
    revised["sources"][0]["quote"] = "把数据库改为 SQLite"
    basis = apply_history_patch(basis, patch(revised), [latest], sources)

    result = coverage_sources(basis, sources)
    assert [(row["id"], row["role"], row["text"]) for row in result] == [
        ("u0", "user", old["text"]), ("u2", "user", latest["text"]),
    ]
    context = result[0]["coverage_context"]
    assert any(row["status"] == "superseded" and row["entry_id"] == "storage"
               and row["body"] == "数据库使用 MySQL" for row in context)
    assert any(row["status"] == "excluded" and row.get("quote") == "运行错误：ValueError"
               for row in context)
    assert any(row["status"] == "current" and row["entry_id"] == "storage"
               and row["body"] == "数据库使用 SQLite" for row in result[1]["coverage_context"])
    assert sources[0]["text"] == old["text"], "coverage selection must not rewrite source history"


def test_coverage_sources_respects_frozen_boundary_and_explicit_start():
    first = source("u0", "user", "旧任务要求", 1)
    second = source("u1", "user", "当前任务要求", 2)
    basis = new_basis(empty_document(), ["u0"])
    assert [row["id"] for row in coverage_sources(basis, [first, second])] == ["u0"]
    current_basis = new_basis(empty_document(), ["u0", "u1"])
    assert [row["id"] for row in coverage_sources(
        current_basis, [first, second], start_source_id="u1")] == ["u1"]
    with pytest.raises(ValueError, match="不存在"):
        coverage_sources(current_basis, [first, second], start_source_id="missing")
    with pytest.raises(ValueError, match="冻结"):
        coverage_sources(basis, [first, second], start_source_id="u1")


def test_model_only_user_confirmation_updates_only_model_field_provenance():
    plan = source("a0", "assistant", "实施方案：收集资料并产出报告", 1)
    user = source("u1", "user", "执行节点使用 model-X", 2)
    sources = [plan, user]
    node = fact("writer", plan, body="收集资料并产出报告", kind="node",
                node_kind="implement", inputs=["全部章节"], acceptance=["报告可读"],
                model={"provider_key": "old", "model_id": "old-model", "effort": "medium"})
    basis = new_basis(empty_document(), [row["id"] for row in sources])
    basis = apply_history_patch(basis, patch(node), [plan], sources)
    previous_fields = copy.deepcopy(basis["entry_field_sources"]["writer"])
    updated = copy.deepcopy(node)
    updated["data"]["model"] = {"provider_key": "new", "model_id": "model-X", "effort": "high"}
    updated["sources"] = [{"source_id": user["id"], "quote": user["text"]}]

    basis = apply_history_patch(basis, patch(updated), [user], sources)
    current = next(row for row in effective_document(basis)[0]["entries"] if row["id"] == "writer")
    assert current["data"]["model"] == updated["data"]["model"]
    assert current["body"] == node["body"]
    assert current["data"]["acceptance"] == node["data"]["acceptance"]
    assert current["data"]["inputs"] == node["data"]["inputs"]
    assert current["evidence"] == "proposal", "a model-only choice does not confirm the whole proposal"

    fields = basis["entry_field_sources"]["writer"]
    assert fields["data.model"] == [{"source_id": user["id"], "quote": user["text"]}]
    for field in ("body", "data.acceptance", "data.inputs"):
        assert fields[field] == previous_fields[field]
    assert "entry_field_sources" not in basis["document"]
