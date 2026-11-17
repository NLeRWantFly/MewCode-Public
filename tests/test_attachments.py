from __future__ import annotations

import io

import pytest
from docx import Document
from openpyxl import Workbook
from PIL import Image
from pypdf import PdfWriter

from mewcode.attachments import AttachmentError, AttachmentStore
from mewcode.conversation import Message
from mewcode.memory.session import SessionRecord, records_to_messages


def _image_bytes() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (12, 8), (210, 80, 50)).save(stream, format="PNG")
    return stream.getvalue()


def test_image_reference_materializes_without_persisting_base64(tmp_path):
    store = AttachmentStore(tmp_path)
    ref = store.save("session-1", "../screen.png", _image_bytes(), "image/png")
    assert ref.name == "screen.png"
    blocks = store.references("session-1", [ref.id])
    materialized = store.materialize(blocks)
    assert materialized[0]["type"] == "image"
    assert materialized[0]["data"]

    records = SessionRecord.from_message(
        Message(role="user", content="inspect", content_blocks=materialized)
    )
    serialized = records[0].to_jsonl()
    assert materialized[0]["data"] not in serialized
    restored = records_to_messages(records)[0]
    assert restored.content == "inspect"
    assert restored.content_blocks[0]["type"] == "attachment"


def test_text_docx_xlsx_and_pdf_are_accepted(tmp_path):
    store = AttachmentStore(tmp_path)
    text = store.save("s", "notes.md", "你好\nworld".encode(), "text/markdown")
    assert "你好" in store.materialize(store.references("s", [text.id]))[0]["text"]

    doc_stream = io.BytesIO()
    document = Document()
    document.add_paragraph("DOCX content")
    document.save(doc_stream)
    doc = store.save("s", "spec.docx", doc_stream.getvalue())
    assert "DOCX content" in store.materialize(store.references("s", [doc.id]))[0]["text"]

    sheet_stream = io.BytesIO()
    workbook = Workbook()
    workbook.active["A1"] = "cell value"
    workbook.save(sheet_stream)
    sheet = store.save("s", "data.xlsx", sheet_stream.getvalue())
    assert "cell value" in store.materialize(store.references("s", [sheet.id]))[0]["text"]

    pdf_stream = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(pdf_stream)
    pdf = store.save("s", "blank.pdf", pdf_stream.getvalue())
    assert pdf.kind == "pdf"


def test_rejects_unsupported_binary_and_cross_conversation_reference(tmp_path):
    store = AttachmentStore(tmp_path)
    with pytest.raises(AttachmentError, match="不支持"):
        store.save("s", "archive.zip", b"PK\x03\x04junk")
    ref = store.save("s", "a.txt", b"hello")
    with pytest.raises(AttachmentError, match="不存在|不属于"):
        store.references("other", [ref.id])


def test_delete_conversation_removes_all_assets(tmp_path):
    store = AttachmentStore(tmp_path)
    ref = store.save("s", "a.txt", b"hello")
    assert store.asset_path(ref).exists()
    store.delete_conversation("s")
    assert not (tmp_path / "s").exists()
