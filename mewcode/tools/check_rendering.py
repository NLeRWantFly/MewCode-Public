"""Read-only Markdown image-link checks for page candidate artifacts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult
from mewcode.tools.verify_artifacts import canonical_digest

MAX_MARKDOWN_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 30_000_000
MAX_IMAGE_LINKS = 100
MAX_TOTAL_IMAGE_BYTES = 64 * 1024 * 1024
ALLOWED_BITMAP_FORMATS = frozenset({"PNG", "JPEG", "WEBP", "GIF", "BMP", "TIFF"})
_INLINE_IMAGE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<target><[^>]+>|[^\s)]+)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\)")
_REFERENCE_IMAGE = re.compile(r"!\[(?P<alt>[^\]]*)\](?:\s*\[(?P<ref>[^\]]*)\])?(?!\s*\()")
_REFERENCE_DEF = re.compile(r"^[ \t]{0,3}\[(?P<ref>[^\]]+)\]:[ \t]*(?P<target><[^>]+>|\S+)")
_BRACKET_PLACEHOLDER = re.compile(r"^\s*\[[^\[\]\r\n]{1,300}\]\s*$")


def _root(root: str | Path) -> Path:
    path = Path(root).absolute()
    resolved = path.resolve(strict=True)
    if resolved != path or not resolved.is_dir():
        raise ValueError("工作区根目录必须是普通目录，不能是链接")
    return resolved


def _relative(value: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("路径为空或格式无效")
    normalized = value.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or normalized.startswith("//") or ":" in pure.parts[0] \
            or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError("路径必须是工作区内的相对路径")
    return Path(*pure.parts)


def _resolve_file(root: Path, relative: str, *, maximum: int) -> Path:
    rel = _relative(relative)
    current = root
    for part in rel.parts:
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"候选路径不允许符号链接或重解析点：{part}")
    resolved = current.resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError("候选文件必须位于工作区根目录内")
    info = resolved.stat()
    if info.st_size > maximum:
        raise ValueError("候选文件超过读取大小上限")
    if getattr(info, "st_nlink", 1) > 1:
        raise ValueError("候选文件不能是硬链接")
    return resolved


def _doc_page(relative: str, document: str, page: int) -> tuple[str, int]:
    parts = PurePosixPath(relative.replace("\\", "/")).parts
    page_part = next((part for part in reversed(parts) if re.fullmatch(r"page_(\d{1,6})", part)), None)
    if page_part:
        page = int(page_part[5:])
        if not document:
            index = parts.index(page_part)
            if index:
                document = parts[index - 1]
    return document or "unknown", page


def _image_target(raw: str) -> str:
    target = raw[1:-1] if raw.startswith("<") and raw.endswith(">") else raw
    target = unquote(target)
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("只允许工作区内的本地相对图片链接")
    if not target.strip():
        raise ValueError("图片链接为空")
    return target


def check_markdown_rendering(root: str | Path, markdown_path: str,
                             *, document: str = "", page: int = 0) -> dict[str, Any]:
    """Validate inline Markdown bitmap links without launching a renderer."""
    workspace = _root(root)
    markdown = _resolve_file(workspace, markdown_path, maximum=MAX_MARKDOWN_BYTES)
    markdown_bytes = markdown.read_bytes()
    markdown_sha = hashlib.sha256(markdown_bytes).hexdigest()
    try:
        content = markdown_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("candidate.md 必须是 UTF-8 文本") from exc
    document, page = _doc_page(markdown_path, document, page)
    errors: list[dict[str, Any]] = []
    lines = content.splitlines()
    image_links: list[tuple[int, str]] = []
    definitions: dict[str, str] = {}
    fenced = False
    fence_marker = ""
    for line_number, line in enumerate(lines, 1):
        opening = re.match(r"^\s{0,3}(```+|~~~+)", line)
        if opening:
            marker = opening.group(1)[0]
            if not fenced:
                fenced, fence_marker = True, marker
            elif marker == fence_marker:
                fenced = False
            continue
        if fenced:
            continue
        definition = _REFERENCE_DEF.match(line)
        if definition:
            key = " ".join(definition.group("ref").split()).casefold()
            if key in definitions and definitions[key] != definition.group("target"):
                errors.append({"document": document, "page": page,
                               "line": line_number, "code": "duplicate_reference",
                               "message": f"图片引用定义冲突：{key}"})
            definitions[key] = definition.group("target")
    fenced = False
    fence_marker = ""
    for line_number, line in enumerate(lines, 1):
        opening = re.match(r"^\s{0,3}(```+|~~~+)", line)
        if opening:
            marker = opening.group(1)[0]
            if not fenced:
                fenced, fence_marker = True, marker
            elif marker == fence_marker:
                fenced = False
            continue
        if fenced:
            continue
        if _REFERENCE_DEF.match(line):
            continue
        if _BRACKET_PLACEHOLDER.fullmatch(line):
            errors.append({"document": document, "page": page, "line": line_number,
                           "code": "bracket_placeholder",
                           "message": "仅有方括号说明，不能渲染为图片"})
        image_links.extend((line_number, match.group("target")) for match in _INLINE_IMAGE.finditer(line))
        for match in _REFERENCE_IMAGE.finditer(line):
            ref = (match.group("ref") or match.group("alt")).strip()
            key = " ".join(ref.split()).casefold()
            target = definitions.get(key)
            if target is None:
                errors.append({"document": document, "page": page, "line": line_number,
                               "code": "image_reference_missing", "target": ref,
                               "message": "Markdown 图片引用没有对应定义"})
            else:
                image_links.append((line_number, target))
    if len(image_links) > MAX_IMAGE_LINKS:
        errors.append({"document": document, "page": page, "line": 1,
                       "code": "image_link_limit", "message": "图片链接超过 100 项核验上限"})
        image_links = image_links[:MAX_IMAGE_LINKS]
    if not image_links:
        errors.append({"document": document, "page": page, "line": 1,
                       "code": "no_image_links", "message": "candidate.md 没有可渲染的 Markdown 图片链接"})

    linked: list[dict[str, Any]] = []
    total_image_bytes = 0
    for line_number, raw_target in image_links:
        try:
            target = _image_target(raw_target)
            relative = (PurePosixPath(markdown_path.replace("\\", "/")).parent
                        / PurePosixPath(target.replace("\\", "/"))).as_posix()
            image_path = _resolve_file(workspace, relative, maximum=MAX_IMAGE_BYTES)
            total_image_bytes += image_path.stat().st_size
            if total_image_bytes > MAX_TOTAL_IMAGE_BYTES:
                raise ValueError("候选图片总量超过 64 MiB 核验上限")
            from PIL import Image
            with Image.open(image_path) as picture:
                image_format = str(picture.format or "").upper()
                width, height = picture.size
                if image_format not in ALLOWED_BITMAP_FORMATS:
                    raise ValueError("图片必须是受支持的位图格式")
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    raise ValueError("图片尺寸为空或超过像素上限")
                picture.load()
            image_sha = hashlib.sha256(image_path.read_bytes()).hexdigest()
            linked.append({"path": relative, "line": line_number, "format": image_format,
                           "width": width, "height": height, "sha256": image_sha})
        except Exception as exc:
            errors.append({"document": document, "page": page, "line": line_number,
                           "code": "image_unrenderable", "target": raw_target,
                           "message": str(exc)})

    return {"status": "PASS" if not errors else "FAIL", "document": document,
            "page": page, "markdown_sha256": markdown_sha, "image_count": len(linked),
            "images": linked, "errors": errors}


class CheckRenderingParams(BaseModel):
    markdown_path: str = Field(description="candidate.md 相对当前工作区根目录的路径")
    document: str = Field(default="", max_length=240)
    page: int = Field(default=0, ge=0, le=1_000_000)


class CheckRendering(Tool):
    """Check Markdown image links resolve to contained, decodable bitmaps."""

    name = "CheckRendering"
    description = ("复核前运行：只读解析候选 Markdown 图片链接，检查相对路径包含、文件存在、"
                   "位图格式与解码；把仅有的 [方括号说明] 判失败并报告文档、页、行号。")
    params_model = CheckRenderingParams
    category = "read"
    is_concurrency_safe = True

    def __init__(self, root: str | Path):
        self.root = Path(root)

    async def execute(self, params: CheckRenderingParams) -> ToolResult:
        import asyncio

        document, page = _doc_page(params.markdown_path, params.document, params.page)
        try:
            checked = await asyncio.to_thread(check_markdown_rendering, self.root,
                params.markdown_path, document=document, page=page)
            identity = {"object": f"{checked['document']}/page_{checked['page']:04d}",
                        "version": checked["markdown_sha256"],
                        "sha256": canonical_digest(checked)}
            result = {"identity": identity, **checked}
            return ToolResult(json.dumps(result, ensure_ascii=False, sort_keys=True),
                              is_error=checked["status"] != "PASS")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            error = {"status": "FAIL", "document": document, "page": page,
                     "errors": [{"document": document, "page": page, "line": 0,
                                 "code": "input_invalid", "message": str(exc)}]}
            error["identity"] = {"object": f"{document}/page_{page:04d}",
                                 "version": "unverified", "sha256": canonical_digest(error)}
            return ToolResult(json.dumps(error, ensure_ascii=False, sort_keys=True), is_error=True)
