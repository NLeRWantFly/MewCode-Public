"""Read-only, bounded artifact access for a frozen audit workspace."""
from __future__ import annotations

import base64
import asyncio
import io
import json
import threading
import time
import zipfile
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree

from pydantic import BaseModel, Field

from mewcode.experts.audit_evidence import (
    DUPLICATE_EVIDENCE,
    IMAGE_BLOCKED_NO_VISION,
    IMAGE_DELIVERED,
    DuplicateEvidenceLedger,
    apply_crop,
    current_evidence_ledger,
    evidence_key,
    normalize_crop,
    pixel_sha256,
)
from mewcode.tools.base import MAX_OUTPUT_CHARS, Tool, ToolResult, resolve_tool_path
from mewcode.tools.artifact_read_cache import (
    ArtifactPayload, ArtifactReadCache, CountedReader, ReadUsage,
    SHARED_ARTIFACT_CACHE, SourceVersion, source_fingerprint,
)
from mewcode.unicode_text import utf8_safe_text


class ReadAuditArtifactParams(BaseModel):
    file_path: str = Field(default="", description="read 时必填；缓存操作可留空以查看/清理本工具允许目录的缓存")
    file_paths: list[str] = Field(default_factory=list, max_length=4,
        description="可替代 file_path，一次读取最多4张 PNG/JPEG 页图（合计≤8MiB）；逐图保留哈希与像素回执")
    action: Literal["read", "cache_info", "clear_cache"] = "read"
    cache_mode: Literal["use", "refresh", "bypass"] = Field(
        default="use", description="复用当前文件版本缓存、强制重读并刷新，或不读写缓存；不清除视觉验收账本")
    offset: int = Field(default=0, ge=0)
    char_offset: int = Field(default=0, ge=0, description="首行规范化显示文字的字符偏移；使用返回的 NEXT_CHAR_OFFSET 接续长行")
    limit: int = Field(default=100, ge=1, le=500)
    page: int = Field(default=1, ge=1, description="PDF 页码，从 1 开始")
    render_page: bool = Field(default=False, description="PDF 为 true 时读取真实页面像素；视觉/排版比较必须开启，文字提取不代表看过页面")
    image_member: str = Field(default="", description="DOCX 的 word/media/ 图片成员名；留空读取正文")
    criterion_id: str = Field(default="", description="本次核验的冻结验收项编号")
    crop: str = Field(default="", description="可选裁剪 x,y,w,h（像素）；空表示整图")


class ReadAuditArtifact(Tool):
    name = "ReadAuditArtifact"
    description = (
        "Read frozen audit artifacts without running commands: UTF-8 text, DOCX paragraphs "
        "and embedded images, PDF page text (render_page=true for actual page pixels), PNG/JPEG images. Successful image reads include "
        "IMAGE_DELIVERED: yes when pixels enter the model request. Re-reading the same pixel "
        "hash + acceptance item + crop is blocked. Text extraction alone is not visual proof."
        " Large PDFs are read by page. File hashes and page results use a bounded memory cache, "
        "invalidated when source versions change. Use returned NEXT_OFFSET/NEXT_CHAR_OFFSET to "
        "continue text, cache_mode=refresh/bypass for explicit rereads, action=cache_info/clear_cache "
        "for scoped cache controls and measured read usage. These never clear the visual ledger."
        " Use file_paths for up to four page images in one model turn; read full pages first, then only uncertain crops."
    )
    params_model = ReadAuditArtifactParams
    category = "read"
    is_concurrency_safe = True

    def __init__(
        self,
        roots: list[str],
        *,
        vision_ok: bool = True,
        ledger: DuplicateEvidenceLedger | None = None,
        cache: ArtifactReadCache | None = None,
    ):
        self.roots = [Path(root).resolve() for root in roots if root]
        self.vision_ok = vision_ok
        self.ledger = ledger if ledger is not None else DuplicateEvidenceLedger()
        self.cache = cache if cache is not None else SHARED_ARTIFACT_CACHE
        self._usage_lock = threading.Lock()
        self._io_totals = {key: 0 for key, value in ReadUsage().as_dict().items() if type(value) is int}
        self._io_totals.update(read_calls=0, failed_calls=0, cancelled_calls=0, active_reads=0)
        self.image_observer = None

    def _active_ledger(self) -> DuplicateEvidenceLedger:
        return current_evidence_ledger() or self.ledger

    def _deliver_image(self, data: bytes, label: str, params: ReadAuditArtifactParams, source_digest: str = "") -> ToolResult:
        pixels = apply_crop(data, params.crop)
        digest = pixel_sha256(pixels)
        key = evidence_key(digest, params.criterion_id, params.crop)
        ledger = self._active_ledger()
        if ledger.contains(key):
            raise ValueError(
                f"{DUPLICATE_EVIDENCE} PIXEL_SHA256={digest} 验收项="
                f"{(params.criterion_id or '-').strip() or '-'} "
                f"裁剪={normalize_crop(params.crop) or 'full'}"
            )
        if not self.vision_ok:
            ledger.record(key)
            raise ValueError(IMAGE_BLOCKED_NO_VISION)
        result = self.image(
            pixels, label, criterion_id=params.criterion_id, crop=params.crop, digest=digest,
        )
        if not result.content_blocks or result.content_blocks[0].get("type") != "image":
            ledger.record(key)
            raise ValueError("图像块未能放入工具结果，像素未进入模型请求")
        if not result.content_blocks[0].get("data"):
            ledger.record(key)
            raise ValueError("图像数据为空，像素未进入模型请求")
        ledger.record(key)
        if self.image_observer is not None:
            self.image_observer(params, source_digest, digest)
        return result

    @staticmethod
    def image(
        data: bytes,
        label: str,
        *,
        criterion_id: str = "",
        crop: str = "",
        digest: str = "",
    ) -> ToolResult:
        from PIL import Image

        if len(data) > 8 * 1024 * 1024:
            raise ValueError("图片超过 8 MiB，请提供较小的审稿图片")
        with Image.open(io.BytesIO(data)) as picture:
            if picture.width * picture.height > 30_000_000 or picture.format not in {"PNG", "JPEG"}:
                raise ValueError("图片需为不超过 3000 万像素的 PNG/JPEG")
            mime = "image/png" if picture.format == "PNG" else "image/jpeg"
            dimensions = f"{picture.width}x{picture.height}"
            picture.verify()
        pixel_digest = digest or pixel_sha256(data)
        marker = (
            f"\n{IMAGE_DELIVERED}\nPIXEL_SHA256: {pixel_digest}\n"
            f"CRITERION: {(criterion_id or '-').strip() or '-'}\n"
            f"CROP: {normalize_crop(crop) or 'full'}\n"
            f"DELIVERED_IMAGE_SIZE: {dimensions}\nIMAGE_BYTES: {len(data)}"
        )
        return ToolResult(output=label + marker, content_blocks=[{
            "type": "image", "mime_type": mime, "data": base64.b64encode(data).decode("ascii"),
            '_mewcode_source_tool': 'ReadAuditArtifact',
        }])

    @staticmethod
    def _text_payload(text, *, pages=0, note=""):
        safe = utf8_safe_text(text)
        return ArtifactPayload(safe, pages=pages, note=note, unicode_repaired=safe != text)

    def _load_payload(self, path, params, usage):
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            from mewcode.tools.pdf_pixels import render_page, extract_page_text
            if params.render_page:
                pixels, pages = render_page(path, params.page, read_meter=usage)
                return ArtifactPayload(pixels, pages=pages, note=f"PAGE: {params.page}/{pages}（真实页面渲染）")
            text, pages = extract_page_text(path, params.page, read_meter=usage)
            return self._text_payload(text, pages=pages,
                note=f"PAGE: {params.page}/{pages}（仅文字提取，不验证图像及版式）")
        with CountedReader(path, usage) as stream:
            if suffix in {".png", ".jpg", ".jpeg"}:
                return ArtifactPayload(stream.read(64 * 1024 * 1024 + 1))
            if suffix == ".docx":
                with zipfile.ZipFile(stream) as document:
                    if params.image_member:
                        member = params.image_member
                        if not member.startswith("word/media/") or document.getinfo(member).file_size > 8 * 1024 * 1024:
                            raise ValueError("只能读取 word/media/ 内不超过 8 MiB 的嵌入图片")
                        return ArtifactPayload(document.read(member), note="MEMBER: " + member)
                    if document.getinfo("word/document.xml").file_size > 16 * 1024 * 1024:
                        raise ValueError("DOCX 正文 XML 超过读取上限")
                    root = ElementTree.fromstring(document.read("word/document.xml"))
                    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                    text = "\n".join("".join(paragraph.itertext()) for paragraph in root.findall(".//w:p", ns))
                    note = "正文段落（含表格段落，不代表排版验证）。图片成员：" + ", ".join(
                        name for name in document.namelist() if name.startswith("word/media/"))[:4000]
                    return self._text_payload(text, note=note)
            data = stream.read(64 * 1024 * 1024 + 1)
            if len(data) > 64 * 1024 * 1024:
                raise ValueError("文件超过 64 MiB 的单次审稿读取上限")
            return self._text_payload(data.decode("utf-8"))

    def _read_cached(self, source, path, params, usage):
        # A cancelled async caller cannot stop native parsing mid-call. Count
        # its eventual I/O here, while never delivering its pixels or text.
        with self._usage_lock:
            self._io_totals["active_reads"] += 1
        try:
            return self._read_cached_impl(source, path, params, usage)
        finally:
            with self._usage_lock:
                self._io_totals["active_reads"] -= 1
                for key, value in usage.as_dict().items():
                    if type(value) is int and not key.startswith("output_"):
                        self._io_totals[key] += value

    def _read_cached_impl(self, source, path, params, usage):
        digest = source_fingerprint(source, usage, cache=self.cache, mode=params.cache_mode)
        suffix = path.suffix.lower()
        variant = ("pdf-pixels-v1" if params.render_page else "pdf-text-v1", params.page) if suffix == ".pdf" else (
            suffix, params.image_member if suffix == ".docx" else "")
        payload, usage.data_cache_hit = self.cache.get_or_load(
            source, variant, lambda: self._load_payload(path, params, usage), usage, mode=params.cache_mode)
        source.check()
        return digest, payload

    @staticmethod
    def _text_result(label, payload, params):
        lines = str(payload.data).splitlines()
        index, char_offset = params.offset, params.char_offset
        if char_offset and (index >= len(lines) or char_offset > len(lines[index])):
            raise ValueError("字符偏移超出当前行，请使用返回的接续游标")
        # Leave room for cursor, encoding notes and usage within the Agent's
        # own output cap, so a returned cursor never skips host-truncated text.
        parts, budget = [], max(0, MAX_OUTPUT_CHARS - len(label) - 1000)
        end = min(len(lines), index + params.limit)
        while index < end:
            prefix = f"{index + 1}\t"
            available = budget - len(prefix) - bool(parts)
            if available <= 0:
                break
            fragment = lines[index][char_offset:]
            selected = fragment[:available]
            parts.append(prefix + selected)
            budget -= len(prefix) + len(selected) + (len(parts) > 1)
            if len(fragment) > available:
                char_offset += len(selected)
                break
            index, char_offset = index + 1, 0
        cursor = {"has_more": index < len(lines), "next_offset": min(index, len(lines)),
                  "next_char_offset": char_offset}
        output = (label + f"\nTOTAL: {len(lines)}\nNEXT_OFFSET: {cursor['next_offset']}"
                  f"\nNEXT_CHAR_OFFSET: {char_offset}\nHAS_MORE: {str(cursor['has_more']).lower()}\n" + "\n".join(parts))
        if cursor["has_more"]:
            output += "\n[使用 NEXT_OFFSET 与 NEXT_CHAR_OFFSET 接续；同一页的内容缓存可复用]"
        if payload.unicode_repaired:
            output += ("\n[文字提取编码提示：异常 Unicode 已安全处理，无法还原的字符以 \\uXXXX 标记；"
                       "标记不代表原始字形。涉及这些字符的判断须核对原页，不应据此判定文稿有误。原文件未修改。]")
        return ToolResult(output=output, host_observation={"read_cursor": cursor})

    def _finish(self, result, usage, started, cache_mode):
        cache_status = ("bypassed" if cache_mode == "bypass" else "hit" if usage.data_cache_hit
                        else "partial" if usage.hash_cache_hit else "miss")
        result.output = utf8_safe_text(result.output) + (
            f"\nREAD_USAGE: source_read_bytes={usage.source_read_bytes}; cache={cache_status}; "
            f"cache_read_bytes={usage.cache_read_bytes}; cache_write_bytes={usage.cache_write_bytes}; source_write_bytes=0")
        usage.output_text_bytes = len(result.output.encode("utf-8"))
        for block in result.content_blocks:
            data = block.get("data") if block.get("type") == "image" else None
            if isinstance(data, str):
                usage.output_image_bytes += len(data) * 3 // 4 - (len(data) - len(data.rstrip("=")))
        metrics = {**usage.as_dict(), "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                   "cache_mode": cache_mode, "cache_status": cache_status,
                   "read_bytes_kind": "host_returned_bytes_not_physical_disk_io"}
        result.host_observation = {**(result.host_observation or {}), "artifact_io": metrics}
        with self._usage_lock:
            self._io_totals["read_calls"] += 1
            self._io_totals["failed_calls"] += bool(result.is_error)
            for key, value in usage.as_dict().items():
                if type(value) is int and key.startswith("output_"):
                    self._io_totals[key] += value
        return result

    async def execute(self, params: ReadAuditArtifactParams) -> ToolResult:
        started, usage = time.perf_counter(), ReadUsage()
        try:
            if params.file_paths:
                return await self._image_batch(params)
            path = None
            if params.file_path:
                path = resolve_tool_path(params.file_path).resolve(strict=params.action == "read")
                if (not any(path.is_relative_to(root) for root in self.roots)
                        or (path.exists() and not path.is_file())):
                    raise ValueError("文件必须位于本轮基线或候选目录")
            if params.action != "read":
                removed = self.cache.clear(self.roots, path=path) if params.action == "clear_cache" else 0
                with self._usage_lock:
                    totals = dict(self._io_totals)
                return ToolResult(output=json.dumps({"action": params.action, "removed_entries": removed,
                    "cache": self.cache.info(self.roots, path=path), "io_totals": totals,
                    "visual_ledger_unchanged": True}, ensure_ascii=False))
            if path is None:
                raise ValueError("读取时必须提供 file_path")
            source = SourceVersion.read(path)
            if path.suffix.lower() == ".pdf":
                if source.size > 2 * 1024 * 1024 * 1024:
                    raise ValueError("PDF 超过 2 GiB 的源文件上限，请分成带原页码映射的分册")
                if params.render_page and not self.vision_ok:
                    raise ValueError(IMAGE_BLOCKED_NO_VISION)
            elif source.size > 64 * 1024 * 1024:
                raise ValueError("文件超过 64 MiB 的单次审稿读取上限（大 PDF 支持按页读取）")
            if params.cache_mode == "refresh":
                self.cache.clear(self.roots, path=path)
            digest, payload = await asyncio.to_thread(self._read_cached, source, path, params, usage)
            source.check()
            label = f"FILE: {path.name}\nSHA256: {digest}"
            if payload.note:
                label += "\n" + payload.note
            if isinstance(payload.data, bytes):
                result = self._deliver_image(payload.data, label, params, digest)
            else:
                result = self._text_result(label, payload, params)
            return self._finish(result, usage, started, params.cache_mode)
        except asyncio.CancelledError:
            with self._usage_lock:
                self._io_totals["read_calls"] += 1
                self._io_totals["cancelled_calls"] += 1
            raise
        except Exception as exc:
            result = ToolResult(output=utf8_safe_text(f"无法读取审稿证据：{exc}"), is_error=True)
            return self._finish(result, usage, started, params.cache_mode)

    async def _image_batch(self, params):
        if params.file_path or params.action != 'read' or params.crop or params.image_member:
            raise ValueError('批量页图读取不能混用单文件、缓存管理或局部裁剪')
        if not self.vision_ok:
            raise ValueError(IMAGE_BLOCKED_NO_VISION)
        paths = [resolve_tool_path(name).resolve(strict=True) for name in params.file_paths]
        if len(set(paths)) != len(paths) or any(not path.is_file()
                or path.suffix.lower() not in {'.png','.jpg','.jpeg'}
                or not any(path.is_relative_to(root) for root in self.roots) for path in paths):
            raise ValueError('批量页图必须是允许目录内不重复的 PNG/JPEG 文件')
        sources = [SourceVersion.read(path) for path in paths]
        if sum(source.size for source in sources) > 8 * 1024 * 1024:
            raise ValueError('批量页图合计超过8MiB；请减少本次页数，原图不缩减')
        meters = [ReadUsage() for _ in paths]
        child = [params.model_copy(update={'file_path':str(path),'file_paths':[]}) for path in paths]
        started = time.perf_counter()
        # Load first. Cancellation cannot record pixels that were never returned.
        payloads = await asyncio.gather(*(asyncio.to_thread(self._read_cached, source, path, arg, meter)
            for source,path,arg,meter in zip(sources,paths,child,meters)),return_exceptions=True)
        results, observations = [], []
        for source,path,arg,meter,payload in zip(sources,paths,child,meters,payloads):
            try:
                if isinstance(payload, BaseException):
                    raise payload
                source.check()
                digest, data = payload
                result = self._deliver_image(data.data, f'FILE: {path}\nSHA256: {digest}', arg, digest)
            except Exception as exc:
                result=ToolResult(output=f'FILE: {path}\n无法读取审稿证据：{exc}',is_error=True)
            results.append(self._finish(result,meter,started,params.cache_mode))
            observations.append({'file_path':str(path),'is_error':result.is_error,
                                 **(result.host_observation or {})})
        return ToolResult(output='\n\n'.join(f'PAGE_IMAGE {i+1}/{len(results)}\n'+r.output for i,r in enumerate(results)),
            content_blocks=[block for r in results for block in r.content_blocks],
            is_error=all(r.is_error for r in results),host_observation={'artifact_batch':observations})
