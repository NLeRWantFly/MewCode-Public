import json

import pytest

from mewcode.expert_group import ExpertExecutionPolicy, ExpertRoleResult
from mewcode.experts.artifact_inventory import artifact_inventory
from tests.test_scheduler_recovery import controller, decide


def test_inventory_uses_magic_type_and_only_declared_read_tools(tmp_path):
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "page.txt").write_bytes(b"\x89PNG\r\n\x1a\n" + b"opaque")
    (tmp_path / "private.bin").write_bytes(b"\x00\x01\x02")
    inventory = artifact_inventory(tmp_path, available_tools=["ReadFile", "ReadAuditArtifact"],
                                   failed_paths=["missing/page.txt"])
    rows = {row["path"]: row for row in inventory["files"]}
    assert rows["page.txt"]["kind"] == "image/png"
    assert rows["page.txt"]["read_tool"] == "ReadAuditArtifact"
    assert rows["notes.txt"]["read_tool"] == "ReadFile"
    assert rows["private.bin"]["read_tool"] is None
    assert inventory["files"][0]["path"] == "page.txt"
    assert inventory["authority_change"] is False
    assert "hello" not in json.dumps(inventory)


def test_inventory_is_bounded_and_never_crosses_workspace(tmp_path):
    for index in range(8):
        (tmp_path / f"file-{index}.txt").write_text(str(index), encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=redacted", encoding="utf-8")
    inventory = artifact_inventory(tmp_path, available_tools=[], max_files=3,
                                   max_entries=8)
    assert len(inventory["files"]) == 3
    assert inventory["truncated"] is True
    assert inventory["inspected_entries"] <= 8
    assert all(row["read_tool"] is None for row in inventory["files"])
    assert all(row["path"] != ".env" for row in inventory["files"])


@pytest.mark.asyncio
async def test_host_diagnosis_keeps_inventory_typed_and_non_authoritative(tmp_path, monkeypatch):
    handler, flow, _, store, _, _, _ = controller(tmp_path, monkeypatch)
    handler.model_decisions = False
    (tmp_path / "page.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"opaque")

    def seed(snapshot):
        stage = snapshot["node_states"]["extract"]
        stage.update(started_at=100.0, completed_at=110.0, input_revision="r1",
                     node_attempt=1, available_tools=["ReadFile", "ReadAuditArtifact"],
                     tools={"bad": {"tool_id": "bad", "name": "ReadFile", "status": "failed",
                         "error_code": "file_not_found", "args": {"path": "wrong/page.png"},
                         "declared_at": 101.0, "started_at": 101.0,
                         "completed_at": 102.0}})

    store.mutate(handler.run_id, seed)
    result = await decide(handler, flow,
        result=ExpertRoleResult(text="path error", status="budget_exhausted",
                                error_kind="segment_timeout"),
        policy=ExpertExecutionPolicy("stable", False, 0, work_dir=str(tmp_path)))
    assert result["controller"]["action"] == "self_diagnose"
    assert result["action"] == "diagnose_path"
    manifest = artifact_inventory(tmp_path, available_tools=store.load(handler.run_id)["node_states"]["extract"]["available_tools"],
                                  failed_paths=["wrong/page.png"])
    assert manifest["files"][0]["path"] == "page.png"
    assert manifest["files"][0]["read_tool"] == "ReadAuditArtifact"
    assert manifest["authority_change"] is False and manifest["business_evidence"] is False
