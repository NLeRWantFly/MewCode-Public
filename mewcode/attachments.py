"""Secure local chat attachment storage and document extraction."""

from __future__ import annotations

import base64
import io
import json
import mimetypes
import re
import shutil
import uuid
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_MESSAGE_BYTES = 50 * 1024 * 1024
MAX_ATTACHMENTS = 10
MAX_EXTRACTED_CHARS = 80_000
MAX_MESSAGE_EXTRACTED_CHARS = 200_000
MAX_IMAGE_PIXELS = 40_000_000

_SAFE_ID = re.compile(r"^[a-f0-9]{32}$")
_TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".py", ".js", ".jsx", ".ts",
    ".tsx", ".css", ".scss", ".html", ".htm", ".xml", ".json", ".jsonl",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".csv", ".tsv",
    ".log", ".sql", ".sh", ".ps1", ".bat", ".c", ".h", ".cpp", ".hpp",
    ".java", ".go", ".rs", ".php", ".rb", ".swift", ".kt", ".vue", ".svelte",
}
_IMAGE_MIMES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
}


class AttachmentError(ValueError):
    """User-facing validation error for an attachment."""


@dataclass(frozen=True)
class AttachmentRef:
    id: str
    conversation_id: str
    name: str
    mime_type: str
    size: int
    kind: str
    extracted_chars: int = 0
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["preview_url"] = f"/api/chat/attachments/{self.id}"
        return value


def _clean_name(filename: str) -> str:
    name = Path(str(filename or "attachment").replace("\\", "/")).name
    name = "".join(ch for ch in name if ch >= " " and ch not in '<>:"/\\|?*')
    return (name.strip(" .") or "attachment")[:240]


def _decode_text(data: bytes) -> str:
    if b"\x00" in data[:8192]:
        raise AttachmentError("文件看起来是二进制内容，无法作为文本读取")
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise AttachmentError("无法识别文本编码，请转换为 UTF-8 后重试")


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_EXTRACTED_CHARS:
        return text, False
    return text[:MAX_EXTRACTED_CHARS], True


def _extract_pdf(data: bytes) -> str:
    if not data.startswith(b"%PDF-"):
        raise AttachmentError("PDF 文件签名无效")
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise AttachmentError("当前安装缺少 PDF 解析组件") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join(
            f"--- PDF 第 {index} 页 ---\n{page.extract_text() or ''}"
            for index, page in enumerate(reader.pages, 1)
        )
    except Exception as exc:
        raise AttachmentError(f"PDF 解析失败：{exc}") from exc


def _extract_docx(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if "word/document.xml" not in archive.namelist():
                raise AttachmentError("DOCX 文件结构无效")
    except zipfile.BadZipFile as exc:
        raise AttachmentError("DOCX 文件结构无效") from exc
    try:
        from docx import Document
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise AttachmentError("当前安装缺少 DOCX 解析组件") from exc
    try:
        document = Document(io.BytesIO(data))
        parts = [paragraph.text for paragraph in document.paragraphs if paragraph.text]
        for table_index, table in enumerate(document.tables, 1):
            rows = ["\t".join(cell.text for cell in row.cells) for row in table.rows]
            parts.append(f"--- 表格 {table_index} ---\n" + "\n".join(rows))
        return "\n\n".join(parts)
    except Exception as exc:
        raise AttachmentError(f"DOCX 解析失败：{exc}") from exc


def _extract_xlsx(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if "xl/workbook.xml" not in archive.namelist():
                raise AttachmentError("XLSX 文件结构无效")
    except zipfile.BadZipFile as exc:
        raise AttachmentError("XLSX 文件结构无效") from exc
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise AttachmentError("当前安装缺少 XLSX 解析组件") from exc
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        parts: list[str] = []
        total = 0
        for sheet in workbook.worksheets:
            rows: list[str] = []
            for values in sheet.iter_rows(values_only=True):
                line = "\t".join("" if value is None else str(value) for value in values)
                if line.strip("\t"):
                    rows.append(line)
                    total += len(line) + 1
                if total >= MAX_EXTRACTED_CHARS:
                    break
            parts.append(f"--- 工作表：{sheet.title} ---\n" + "\n".join(rows))
            if total >= MAX_EXTRACTED_CHARS:
                break
        workbook.close()
        return "\n\n".join(parts)
    except AttachmentError:
        raise
    except Exception as exc:
        raise AttachmentError(f"XLSX 解析失败：{exc}") from exc


def _inspect_image(data: bytes) -> tuple[str, int, int]:
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise AttachmentError("当前安装缺少图片校验组件") from exc
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            image_format = str(image.format or "").upper()
        mime = _IMAGE_MIMES.get(image_format)
        if not mime:
            raise AttachmentError("仅支持 PNG、JPEG 和 WebP 图片")
        if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
            raise AttachmentError("图片尺寸超过 4,000 万像素限制")
        return mime, width, height
    except AttachmentError:
        raise
    except Exception as exc:
        raise AttachmentError("图片内容损坏或格式不受支持") from exc


class AttachmentStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _conversation_dir(self, conversation_id: str) -> Path:
        if not conversation_id or any(ch in conversation_id for ch in "/\\"):
            raise AttachmentError("无效的会话 ID")
        return self.root / conversation_id

    def _paths(self, conversation_id: str, attachment_id: str) -> tuple[Path, Path, Path]:
        if not _SAFE_ID.fullmatch(attachment_id):
            raise AttachmentError("无效的附件 ID")
        directory = self._conversation_dir(conversation_id)
        return (
            directory / f"{attachment_id}.bin",
            directory / f"{attachment_id}.json",
            directory / f"{attachment_id}.txt",
        )

    def save(
        self,
        conversation_id: str,
        filename: str,
        data: bytes,
        declared_mime: str = "",
    ) -> AttachmentRef:
        if not data:
            raise AttachmentError("附件为空")
        if len(data) > MAX_FILE_BYTES:
            raise AttachmentError("单个附件不能超过 20 MiB")
        name = _clean_name(filename)
        suffix = Path(name).suffix.casefold()
        extracted = ""
        truncated = False

        if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
            mime_type, _, _ = _inspect_image(data)
            kind = "image"
        elif suffix == ".pdf":
            mime_type, kind = "application/pdf", "pdf"
            extracted, truncated = _truncate(_extract_pdf(data))
        elif suffix == ".docx":
            mime_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            kind = "docx"
            extracted, truncated = _truncate(_extract_docx(data))
        elif suffix == ".xlsx":
            mime_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            kind = "spreadsheet"
            extracted, truncated = _truncate(_extract_xlsx(data))
        elif suffix in _TEXT_EXTENSIONS or str(declared_mime).startswith("text/"):
            mime_type = mimetypes.guess_type(name)[0] or declared_mime or "text/plain"
            kind = "text"
            extracted, truncated = _truncate(_decode_text(data))
        else:
            raise AttachmentError(
                "不支持该文件类型；可上传图片、文本/代码、PDF、DOCX、CSV 或 XLSX"
            )

        attachment_id = uuid.uuid4().hex
        asset_path, meta_path, text_path = self._paths(conversation_id, attachment_id)
        asset_path.parent.mkdir(parents=True, exist_ok=True)
        ref = AttachmentRef(
            id=attachment_id,
            conversation_id=conversation_id,
            name=name,
            mime_type=mime_type,
            size=len(data),
            kind=kind,
            extracted_chars=len(extracted),
            truncated=truncated,
        )
        asset_path.write_bytes(data)
        if extracted:
            text_path.write_text(extracted, encoding="utf-8")
        meta_path.write_text(json.dumps(asdict(ref), ensure_ascii=False), encoding="utf-8")
        return ref

    def get(self, conversation_id: str, attachment_id: str) -> AttachmentRef:
        _, meta_path, _ = self._paths(conversation_id, attachment_id)
        try:
            value = json.loads(meta_path.read_text(encoding="utf-8"))
            ref = AttachmentRef(**value)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise AttachmentError("附件不存在或已被删除") from exc
        if ref.conversation_id != conversation_id or ref.id != attachment_id:
            raise AttachmentError("附件不属于当前会话")
        return ref

    def find(self, attachment_id: str) -> AttachmentRef:
        if not _SAFE_ID.fullmatch(attachment_id):
            raise AttachmentError("无效的附件 ID")
        for meta_path in self.root.glob(f"*/{attachment_id}.json"):
            try:
                value = json.loads(meta_path.read_text(encoding="utf-8"))
                return AttachmentRef(**value)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        raise AttachmentError("附件不存在或已被删除")

    def asset_path(self, ref: AttachmentRef) -> Path:
        asset_path, _, _ = self._paths(ref.conversation_id, ref.id)
        if not asset_path.is_file():
            raise AttachmentError("附件内容不存在")
        return asset_path

    def references(
        self, conversation_id: str, attachment_ids: Iterable[str]
    ) -> list[dict[str, Any]]:
        ids = list(dict.fromkeys(str(value) for value in attachment_ids))
        if len(ids) > MAX_ATTACHMENTS:
            raise AttachmentError("每条消息最多添加 10 个附件")
        refs = [self.get(conversation_id, attachment_id) for attachment_id in ids]
        if sum(ref.size for ref in refs) > MAX_MESSAGE_BYTES:
            raise AttachmentError("单条消息的附件总大小不能超过 50 MiB")
        return [
            {"type": "attachment", "attachment_id": ref.id, **ref.as_dict()}
            for ref in refs
        ]

    def materialize(self, blocks: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        extracted_total = 0
        for block in blocks:
            if block.get("type") != "attachment":
                output.append(dict(block))
                continue
            ref = self.find(str(block.get("attachment_id") or block.get("id") or ""))
            common = {
                "attachment_id": ref.id,
                "attachment": ref.as_dict(),
                "name": ref.name,
            }
            if ref.kind == "image":
                output.append({
                    "type": "image",
                    "mime_type": ref.mime_type,
                    "data": base64.b64encode(self.asset_path(ref).read_bytes()).decode("ascii"),
                    **common,
                })
                continue
            _, _, text_path = self._paths(ref.conversation_id, ref.id)
            text = text_path.read_text(encoding="utf-8") if text_path.exists() else ""
            remaining = max(MAX_MESSAGE_EXTRACTED_CHARS - extracted_total, 0)
            visible = text[:remaining]
            extracted_total += len(visible)
            suffix = "\n[内容已截断]" if len(visible) < len(text) or ref.truncated else ""
            output.append({
                "type": "text",
                "text": f"[附件：{ref.name}，类型：{ref.kind}]\n{visible}{suffix}",
                **common,
            })
        return output

    def display_refs(self, blocks: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for block in blocks:
            attachment_id = str(block.get("attachment_id") or "")
            if not attachment_id or attachment_id in seen:
                continue
            seen.add(attachment_id)
            metadata = block.get("attachment")
            if isinstance(metadata, dict):
                refs.append(dict(metadata))
                continue
            try:
                refs.append(self.find(attachment_id).as_dict())
            except AttachmentError:
                refs.append({
                    "id": attachment_id,
                    "name": str(block.get("name") or "已删除的附件"),
                    "kind": "missing",
                    "size": 0,
                    "preview_url": "",
                })
        return refs

    def delete(self, conversation_id: str, attachment_id: str) -> None:
        for path in self._paths(conversation_id, attachment_id):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def delete_conversation(self, conversation_id: str) -> None:
        directory = self._conversation_dir(conversation_id)
        if directory.is_dir():
            shutil.rmtree(directory)
