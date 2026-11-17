from __future__ import annotations

import zipfile

import pytest

from mewcode.tools.read_audit_artifact import ReadAuditArtifact, ReadAuditArtifactParams


@pytest.mark.asyncio
async def test_batch_returns_each_page_pixels_with_separate_hash_and_scope(tmp_path):
    from PIL import Image
    paths=[]
    for i,color in enumerate(('red','blue','green')):
        path=tmp_path/f'p{i}.png';Image.new('RGB',(30,40),color).save(path);paths.append(str(path))
    tool=ReadAuditArtifact([str(tmp_path)])
    result=await tool.execute(ReadAuditArtifactParams(file_paths=paths,criterion_id='page-content'))
    assert not result.is_error and len(result.content_blocks)==3
    assert result.output.count('IMAGE_DELIVERED: yes')==3
    assert len(result.host_observation['artifact_batch'])==3
    again=await tool.execute(ReadAuditArtifactParams(file_path=paths[1],criterion_id='page-content'))
    assert again.is_error and not again.content_blocks
    outside=tmp_path.parent/'outside-image.png';Image.new('RGB',(30,40),'black').save(outside)
    other=ReadAuditArtifact([str(tmp_path)])
    blocked=await other.execute(ReadAuditArtifactParams(file_paths=[paths[0],str(outside)]))
    assert blocked.is_error and not blocked.content_blocks
    assert not (await other.execute(ReadAuditArtifactParams(file_path=paths[0]))).is_error


@pytest.mark.asyncio
async def test_crop_error_reports_source_bounds_then_corrected_region_succeeds(tmp_path):
    from PIL import Image
    image = tmp_path / "page.png"
    Image.new("RGB", (40, 30), "white").save(image)
    tool = ReadAuditArtifact([str(tmp_path)])
    params = ReadAuditArtifactParams(file_path=str(image), criterion_id="symbol", crop="20,20,30,20")
    invalid = await tool.execute(params)
    assert invalid.is_error and "40x30" in invalid.output and "20,20,30,20" in invalid.output
    assert not invalid.content_blocks
    result = await tool.execute(params.model_copy(update={"crop": "20,20,20,10"}))
    assert not result.is_error and "DELIVERED_IMAGE_SIZE: 20x10" in result.output
    assert "CROP: 20,20,20,10" in result.output


@pytest.mark.asyncio
async def test_pdf_visual_read_delivers_real_pixels_without_files_or_commands(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject

    pdf = tmp_path / "algorithm.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=180, height=240)
    content = DecodedStreamObject()
    content.set_data(b"0.1 0.4 0.8 rg 20 30 80 150 re f")
    page[NameObject("/Contents")] = writer._add_object(content)
    writer.write(pdf)
    before, files = pdf.read_bytes(), set(tmp_path.iterdir())
    tool = ReadAuditArtifact([str(tmp_path)])
    params = ReadAuditArtifactParams(file_path=str(pdf), page=1, render_page=True, criterion_id="layout")
    result = await tool.execute(params)
    assert not result.is_error, result.output
    assert result.content_blocks[0]["type"] == "image" and result.content_blocks[0]["data"]
    assert "IMAGE_DELIVERED: yes" in result.output and "真实页面渲染" in result.output
    assert pdf.read_bytes() == before and set(tmp_path.iterdir()) == files
    assert (await tool.execute(params)).is_error  # Same evidence is not repeated.
    invalid = await tool.execute(ReadAuditArtifactParams(file_path=str(pdf), page=2, render_page=True))
    assert invalid.is_error and "页码" in invalid.output


@pytest.mark.asyncio
async def test_pdf_pixel_request_without_vision_fails_explicitly(tmp_path):
    from pypdf import PdfWriter
    pdf = tmp_path / "page.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(pdf)
    result = await ReadAuditArtifact([str(tmp_path)], vision_ok=False).execute(
        ReadAuditArtifactParams(file_path=str(pdf), render_page=True))
    assert result.is_error and not result.content_blocks
    assert "IMAGE_DELIVERED: yes" not in result.output


@pytest.mark.asyncio
async def test_reads_docx_evidence_with_locations_without_modification(tmp_path):
    document = tmp_path / "paper.docx"
    with zipfile.ZipFile(document, "w") as archive:
        archive.writestr("word/document.xml", '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>mAP50: 42.5</w:t></w:r></w:p><w:p><w:r><w:t>图5 GT 黄色框</w:t></w:r></w:p></w:body></w:document>''')
    before = document.read_bytes()
    result = await ReadAuditArtifact([str(tmp_path)]).execute(ReadAuditArtifactParams(file_path=str(document)))
    assert not result.is_error
    assert "1\tmAP50: 42.5" in result.output and "2\t图5 GT 黄色框" in result.output
    assert "SHA256:" in result.output
    assert document.read_bytes() == before


@pytest.mark.asyncio
async def test_read_artifact_enforces_frozen_directories(tmp_path):
    frozen = tmp_path / "frozen"
    frozen.mkdir()
    outside = tmp_path / "original.txt"
    outside.write_text("must not be confused with frozen version", encoding="utf-8")
    result = await ReadAuditArtifact([str(frozen)]).execute(ReadAuditArtifactParams(file_path=str(outside)))
    assert result.is_error
    assert "本轮基线或候选目录" in result.output


@pytest.mark.asyncio
async def test_read_artifact_emits_image_and_pdf_evidence(tmp_path):
    from PIL import Image
    from pypdf import PdfWriter

    from mewcode.conversation import Message, ToolResultBlock, ToolUseBlock
    from mewcode.serialization import (
        build_anthropic_messages, build_chat_completion_messages, build_openai_input,
    )

    image = tmp_path / "figure.png"
    Image.new("RGB", (10, 10), "white").save(image)
    tool = ReadAuditArtifact([str(tmp_path)])
    result = await tool.execute(ReadAuditArtifactParams(
        file_path=str(image), criterion_id="FIG-01",
    ))
    assert not result.is_error and result.content_blocks[0]["type"] == "image"
    assert result.content_blocks[0]["mime_type"] == "image/png"
    assert result.content_blocks[0]["data"]
    assert "IMAGE_DELIVERED: yes" in result.output
    assert "PIXEL_SHA256:" in result.output
    payload = Message(
        role="user", content="",
        tool_results=[ToolResultBlock("call-1", result.output, content_blocks=result.content_blocks)],
    )
    messages = [Message(role="assistant", content="", tool_uses=[
        ToolUseBlock("call-1", "ReadAuditArtifact", {"file_path": str(image)})]), payload]
    anthropic = build_anthropic_messages(messages)[1]["content"][0]
    responses = build_openai_input(messages)
    chat = build_chat_completion_messages(messages)
    assert anthropic["content"][1]["type"] == "image"
    assert anthropic["content"][1]["source"]["data"] == result.content_blocks[0]["data"]
    assert responses[2]["content"][1]["type"] == "input_image"
    assert "base64," in responses[2]["content"][1]["image_url"]
    assert chat[2]["content"][1]["type"] == "image_url"

    pdf = tmp_path / "paper.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(pdf)
    result = await tool.execute(ReadAuditArtifactParams(file_path=str(pdf)))
    assert not result.is_error
    assert "PAGE: 1/1" in result.output and "仅文字提取" in result.output


@pytest.mark.asyncio
async def test_image_read_blocks_without_vision_and_rejects_duplicate_pixels(tmp_path):
    from PIL import Image

    image = tmp_path / "image5.png"
    Image.new("RGB", (12, 8), "green").save(image)
    blind = ReadAuditArtifact([str(tmp_path)], vision_ok=False)
    blocked = await blind.execute(ReadAuditArtifactParams(
        file_path=str(image), criterion_id="FIG-01",
    ))
    assert blocked.is_error
    assert "IMAGE_DELIVERED: yes" not in blocked.output
    assert "未进入模型请求" in blocked.output or "不具备图像能力" in blocked.output
    assert not blocked.content_blocks
    blocked_again = await blind.execute(ReadAuditArtifactParams(
        file_path=str(image), criterion_id="FIG-01",
    ))
    assert blocked_again.is_error
    assert "禁止重复读取" in blocked_again.output

    tool = ReadAuditArtifact([str(tmp_path)])
    first = await tool.execute(ReadAuditArtifactParams(
        file_path=str(image), criterion_id="FIG-01",
    ))
    assert not first.is_error and "IMAGE_DELIVERED: yes" in first.output
    repeat = await tool.execute(ReadAuditArtifactParams(
        file_path=str(image), criterion_id="FIG-01",
    ))
    assert repeat.is_error
    assert "禁止重复读取" in repeat.output
    assert "转入修改" in repeat.output or "缺什么材料" in repeat.output

    other_item = await tool.execute(ReadAuditArtifactParams(
        file_path=str(image), criterion_id="FIG-03",
    ))
    assert not other_item.is_error
    cropped = await tool.execute(ReadAuditArtifactParams(
        file_path=str(image), criterion_id="FIG-01", crop="0,0,4,4",
    ))
    assert not cropped.is_error
    assert "CROP: 0,0,4,4" in cropped.output


@pytest.mark.asyncio
async def test_review_and_repair_ledgers_are_isolated_but_resume_reuses_repair_keys(tmp_path):
    from PIL import Image

    from mewcode.experts.audit_evidence import (
        DuplicateEvidenceLedger,
        bind_evidence_ledger,
        reset_evidence_ledger,
    )

    image = tmp_path / "image5.png"
    Image.new("RGB", (8, 8), "red").save(image)
    params = ReadAuditArtifactParams(file_path=str(image), criterion_id="FIG-01")
    review_tool = ReadAuditArtifact([str(tmp_path)])
    reviewed = await review_tool.execute(params)
    assert not reviewed.is_error

    repair_tool = ReadAuditArtifact([str(tmp_path)])
    first_repair = await repair_tool.execute(params)
    assert not first_repair.is_error
    key = next(
        line.split("PIXEL_SHA256:", 1)[1].strip()
        for line in first_repair.output.splitlines() if line.startswith("PIXEL_SHA256:")
    )
    resume = DuplicateEvidenceLedger([f"{key}|FIG-01|full"])
    token = bind_evidence_ledger(resume)
    try:
        blocked = await ReadAuditArtifact([str(tmp_path)]).execute(params)
        assert blocked.is_error
        assert "禁止重复读取" in blocked.output
    finally:
        reset_evidence_ledger(token)
