from __future__ import annotations

import pytest

from mewcode.experts.audit_evidence import (
    build_repair_progress,
    charge_repair_budget,
    compact_repair_activity,
    compact_repair_handoff,
    evidence_key,
    infer_repair_step,
    issue_contracts,
    remaining_repair_budget,
    repair_instructions,
    split_delivery_and_scratch,
    stage_brief_for_safety,
    stall_without_progress,
)


def test_evidence_key_distinguishes_item_and_crop():
    digest = "abc"
    assert evidence_key(digest, "FIG-01", "") != evidence_key(digest, "FIG-03", "")
    assert evidence_key(digest, "FIG-01", "") != evidence_key(digest, "FIG-01", "0,0,4,4")


def test_repair_instructions_are_stepped_and_only_list_open_items():
    accepted = {
        "checks": [
            {"criterion_id": "FIG-01", "state": "FAIL",
             "acceptance_test": "图5场景表达与图像内容一致", "evidence": "场景标签与画面不符",
             "location": "paper.docx:图5"},
            {"criterion_id": "FIG-02", "state": "PASS",
             "acceptance_test": "already ok", "evidence": "ok", "location": "paper.docx"},
            {"criterion_id": "FIG-03", "state": "PARTIAL",
             "acceptance_test": "绿色 GT 含义说明清楚", "evidence": "图例未解释绿色框",
             "location": "paper.docx:图注"},
        ]
    }
    text = repair_instructions(
        candidate_dir="/tmp/candidate",
        original_dir="/tmp/src",
        rubric={"criteria": accepted["checks"]},
        accepted=accepted,
        issues=[{"issue_id": "fig5-scene", "criterion_id": "FIG-01",
                 "acceptance_test": "图5场景表达与图像内容一致"}],
        step="locate",
        delivered_evidence=["deadbeef|FIG-01|full"],
        previous_attempt={},
        max_iterations=40,
        delivery_files=["paper.docx", "main.tex"],
    )
    assert "定位未通过项" in text
    assert "修改对应图注" in text or "修改对应" in text
    assert "验证修改" in text or "验证本次修改" in text
    assert "交付复审" in text or "配对复审" in text
    assert "不重新全面盘点" in text
    assert "FIG-01" in text and "FIG-03" in text
    assert "FIG-02" not in text.split("未通过项", 1)[-1]
    assert "当前步骤：locate" in text
    assert "deadbeef" in text
    assert "不编造" in text
    assert "paper.docx" in text and "main.tex" in text
    assert "ReadAuditArtifact" in text
    assert "执行契约" in text
    assert "fig5-scene" in text
    assert "issue_contracts" in text
    assert "灰度" in text


def test_empty_ledger_handoff_allows_directed_reads():
    handoff = compact_repair_handoff(
        step="locate",
        open_items=[{"criterion_id": "FIG-01", "state": "FAIL",
                     "acceptance_test": "行/区域与正文对应"}],
        delivered_evidence=[],
        changed_files=["crops/image5_crop.png"],
        delivery_files=["paper.docx", "main.tex"],
        scratch_files=["crops/image5_crop.png"],
        located_files=["paper.docx", "main.tex"],
        remaining="修改 paper.docx 图5 图注并同步 main.tex",
        notes="Word 第 12 段对应图5，绿色框为 GT",
    )
    assert "Word 第 12 段" in handoff["text"]
    assert "paper.docx" in handoff["text"]
    assert "仍允许" in handoff["text"] or "读段落" in handoff["text"] or "计算哈希" in handoff["text"]
    assert "已交付证据禁止重复读取" not in handoff["text"]
    assert "不要重新看图" not in handoff["text"]
    assert "临时" in handoff["text"] or "不算" in handoff["text"]


def test_ledger_handoff_only_blocks_read_audit_artifact_keys():
    handoff = compact_repair_handoff(
        step="modify",
        open_items=[{"criterion_id": "FIG-01", "state": "FAIL"}],
        delivered_evidence=["pix|FIG-01|full"],
        changed_files=["main.tex"],
        delivery_files=["main.tex"],
        notes="已改 main.tex 图注",
    )
    assert "pix|FIG-01|full" in handoff["text"]
    assert "ReadAuditArtifact" in handoff["text"]
    assert "读 Word" in handoff["text"] or "仍允许" in handoff["text"] or "计算哈希" in handoff["text"]


def test_scratch_pngs_do_not_count_as_modify():
    accepted = {"checks": [{"criterion_id": "FIG-01", "state": "FAIL",
                            "location": "paper.docx:图5", "acceptance_test": "场景"}]}
    delivery, scratch = split_delivery_and_scratch(
        ["crops/image5_crop.png", "labels/gt_preview.png", "paper.docx"],
        accepted=accepted,
        issues=[],
    )
    assert "paper.docx" in delivery
    assert any("crop" in path for path in scratch)
    assert infer_repair_step({"repair_step": "locate"}, changed=True, delivery_changed=False) == "locate"
    assert infer_repair_step({"repair_step": "locate"}, changed=True, delivery_changed=True) == "modify"
    assert infer_repair_step(
        {"repair_step": "locate", "delivered_evidence": ["pix|FIG-01|full"]},
        changed=False,
        delivery_changed=False,
    ) == "locate"
    assert infer_repair_step({"repair_step": "verify"}, changed=True, delivery_changed=False) == "verify"


def test_incomplete_progress_keeps_locations_and_remaining_actions():
    class Result:
        text = "已确认绿色框为 GT，尚未同步 Word"
        status = "budget_exhausted"
        activity_records = [
            {"kind": "read", "path": "candidate/paper.docx"},
            {"kind": "step", "label": "核对 Word 段落编号", "status": "completed"},
            {"kind": "step", "label": "开始修改", "status": "running"},
            {"kind": "edit", "path": "candidate/main.tex"},
        ]

    progress = build_repair_progress(
        repaired=Result(),
        changed_files=["main.tex", "crops/tmp.png"],
        accepted={"checks": [{"state": "FAIL", "location": "paper.docx:图5"}]},
        issues=[],
    )
    assert "绿色框为 GT" in progress["text"]
    assert any("paper.docx" in path for path in progress["located_files"])
    assert any("main.tex" in path for path in progress["edited_delivery"])
    assert progress["remaining"]
    assert "crops/tmp.png" in progress["scratch_files"] or any("tmp.png" in path for path in progress["scratch_files"])


def test_compact_repair_activity_extracts_reads_edits_and_steps():
    records = compact_repair_activity({
        "1": {"name": "ReadFile", "args": {"file_path": "main.tex"}, "is_error": False},
        "2": {"name": "report_step", "args": {"label": "开始修改", "status": "running"}},
        "3": {"name": "EditFile", "args": {"file_path": "main.tex"}},
        "4": {"name": "Bash", "args": {"command": "python -c \"from PIL import Image; print(Image.open('fig.png').size)\""}},
    })
    kinds = [item["kind"] for item in records]
    assert "read" in kinds and "edit" in kinds and "step" in kinds
    assert "probe" in kinds


def test_safety_stage_brief_strips_reread_bans():
    prompt = repair_instructions(
        candidate_dir="/tmp/c",
        original_dir="/tmp/s",
        rubric={},
        accepted={"checks": [{"criterion_id": "FIG-01", "state": "FAIL",
                              "acceptance_test": "x", "location": "paper.docx"}]},
        issues=[],
        step="modify",
        delivered_evidence=[],
        previous_attempt={},
        max_iterations=40,
        resume=True,
        notes="已定位 paper.docx 第12段",
    )
    brief = stage_brief_for_safety(prompt)
    assert "禁止重复读取" not in brief
    assert "不要重新看图" not in brief
    assert "候选" in brief or "修复" in brief or "delivery" in brief.lower() or "交付" in brief


def test_issue_contracts_split_open_items_by_stable_id():
    accepted = {
        "checks": [
            {"criterion_id": "FIG-01", "state": "FAIL",
             "acceptance_test": "场景标签与图像一致", "location": "paper.docx:图5",
             "evidence": "标签不清"},
            {"criterion_id": "FIG-02", "state": "PARTIAL",
             "acceptance_test": "GT 与预测框颜色可区分", "location": "figure5.png",
             "evidence": "图例未解释绿色框"},
        ]
    }
    contracts = issue_contracts(accepted, [
        {"issue_id": "fig5-scene", "criterion_id": "FIG-01",
         "acceptance_test": "场景标签与图像一致"},
        {"issue_id": "fig5-gt-color", "criterion_id": "FIG-02",
         "acceptance_test": "GT 与预测框颜色可区分"},
    ])
    ids = [item["issue_id"] for item in contracts]
    assert ids == ["fig5-scene", "fig5-gt-color"]
    assert any("paper.docx" in path for path in contracts[0]["files"])
    assert "灰度" in contracts[0]["if_unknown"]


def test_ordinary_continue_consumes_remaining_repair_budget():
    state = {"round": 1}
    charge_repair_budget(state, elapsed_seconds=100, iterations_used=40, exhausted=True)
    left_iter, left_time = remaining_repair_budget(
        state, max_iterations=40, timeout_seconds=900, resume=True,
    )
    assert left_iter == 0 and left_time == 0
    fresh = remaining_repair_budget(
        {"round": 1}, max_iterations=40, timeout_seconds=900, resume=False,
    )
    assert fresh == (40, 900.0)


def test_scratch_and_progress_text_do_not_count_as_repair_progress():
    state = {
        "changed_files": ["crops/tmp.png"],
        "repair_progress": {"edited_delivery": [], "text": "still looking"},
        "delivered_evidence": [],
    }
    assert stall_without_progress(state) is True
    state["delivered_evidence"] = ["pix|FIG-01|full"]
    assert stall_without_progress(state) is False
    assert stall_without_progress(state) is True
    state["repair_progress"]["edited_delivery"] = ["paper.docx"]
    assert stall_without_progress(state) is False


@pytest.mark.asyncio
async def test_repair_command_autocorrects_missing_path_once(tmp_path):
    from types import SimpleNamespace

    from mewcode.experts.audit_evidence import COMMAND_RETRY_EXHAUSTED, wrap_repair_command
    from mewcode.tools.base import Tool, ToolResult

    target = tmp_path / "missing.png"
    target.write_bytes(b"img")
    calls = []

    class FakeBash(Tool):
        name = "Bash"
        description = "fake"
        params_model = SimpleNamespace
        category = "command"

        async def execute(self, params):
            command = str(getattr(params, "command", "") or "")
            calls.append(command)
            if command.strip() == "python missing.png":
                return ToolResult(output="No such file or directory: missing.png", is_error=True)
            return ToolResult(output="ok", is_error=False)

    wrapped = wrap_repair_command(FakeBash(), work_dir=str(tmp_path))
    result = await wrapped.execute(SimpleNamespace(command="python missing.png"))
    assert result.is_error is False
    assert any("missing.png" in item and item != "python missing.png" for item in calls)
    await wrapped.execute(SimpleNamespace(command="python missing.png"))
    third = await wrapped.execute(SimpleNamespace(command="python missing.png"))
    assert third.is_error is True
    assert COMMAND_RETRY_EXHAUSTED in third.output


