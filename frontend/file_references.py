"""Read local references inside the selected conversation's project."""
from __future__ import annotations

import asyncio
import mimetypes
import re
from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse

from frontend.workbench_api import Workbench

PREVIEW_BYTES = 512 * 1024


def resolve_reference(value: str, project: str) -> Path:
    value = str(value or '').strip().replace('\\', '/')
    if re.match(r'^/[A-Za-z]:/', value):
        value = value[1:]
    if not value or '\0' in value or value.startswith('//') or re.match(r'^[A-Za-z][\w+.-]*:', value) and not re.match(r'^[A-Za-z]:/', value):
        raise ValueError('这不是可打开的本地文件路径。')
    root = Path(project).resolve()
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.is_relative_to(root):
        raise PermissionError('文件位于当前项目之外，可用“打开所在文件夹”查看。')
    if not path.exists():
        raise FileNotFoundError('文件或文件夹不存在，可能已移动。')
    return path


def preview(path: Path) -> dict:
    if path.is_dir():
        return dict(kind='directory', name=path.name, path=str(path), directory=str(path))
    if not path.is_file():
        raise ValueError('这个路径不是普通文件。')
    size = path.stat().st_size
    with path.open('rb') as stream:
        data = stream.read(PREVIEW_BYTES + 1)
    truncated = len(data) > PREVIEW_BYTES
    data = data[:PREVIEW_BYTES]
    encoding = 'utf-16' if data.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
    text = None
    if encoding == 'utf-16' or b'\0' not in data:
        try:
            # Ignore an incomplete final codepoint only when the preview was capped.
            text = data.decode(encoding, errors='ignore' if truncated else 'strict')
        except UnicodeError:
            pass
    return dict(kind='file', name=path.name, path=str(path), directory=str(path.parent), size=size,
                media_type=mimetypes.guess_type(path.name)[0] or 'application/octet-stream',
                preview=text, truncated=truncated)


def install(app, srv):
    workbench = Workbench(srv)

    @app.get('/api/file-reference')
    async def reference(request: Request):
        values = dict(request.query_params)
        try:
            owner = workbench.scope(values)
            path = await asyncio.to_thread(resolve_reference, values.get('path', ''), owner[0])
            if values.get('download') == '1':
                if not path.is_file():
                    raise ValueError('请选择文件下载。')
                result = FileResponse(path, filename=path.name, media_type='application/octet-stream',
                                      content_disposition_type='attachment', headers={'Cache-Control': 'no-store'})
            else:
                result = JSONResponse(await asyncio.to_thread(preview, path), headers={'Cache-Control': 'no-store'})
            if workbench.scope(values) != owner:
                raise ValueError('项目或对话已切换，请重新打开文件。')
            return result
        except PermissionError as exc:
            return JSONResponse({'error': str(exc)}, status_code=403)
        except FileNotFoundError as exc:
            return JSONResponse({'error': str(exc)}, status_code=404)
        except (ValueError, OSError) as exc:
            return JSONResponse({'error': str(exc)}, status_code=409)
