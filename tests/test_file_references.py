from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from frontend.file_references import PREVIEW_BYTES, install


@pytest.fixture
def reference_app(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    srv = SimpleNamespace(SESSION=SimpleNamespace(work_dir=str(root)), _active_conversation_id=lambda: 'chat')
    app = FastAPI()
    install(app, srv)
    return app, root, dict(project=str(root), conversation_id='chat')


@pytest.mark.asyncio
async def test_referenced_text_directory_download_and_html_are_read_only(reference_app):
    app, root, scope = reference_app
    path = root / '中文 结构.json'
    original = '{"结构": "完整", "nodes": [1, 2]}'
    path.write_text(original, encoding='utf-8')
    html = root / 'index.html'
    html.write_text('<script>window.mustNotExecute=true</script>', encoding='utf-8')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/api/file-reference', params={**scope, 'path': path.name})
        assert response.status_code == 200 and response.json()['preview'] == original
        download = await client.get('/api/file-reference', params={**scope, 'path': str(path), 'download': '1'})
        assert download.content == path.read_bytes()
        assert download.headers['content-disposition'].startswith('attachment;')
        folder = await client.get('/api/file-reference', params={**scope, 'path': str(root)})
        assert folder.json()['kind'] == 'directory'
        page = await client.get('/api/file-reference', params={**scope, 'path': str(html)})
        assert page.headers['content-type'].startswith('application/json')
        assert page.json()['preview'] == html.read_text(encoding='utf-8')
    assert path.read_text(encoding='utf-8') == original


@pytest.mark.asyncio
async def test_reference_scope_missing_file_and_traversal_have_clear_errors(reference_app, tmp_path):
    app, root, scope = reference_app
    outside = tmp_path / 'outside.txt'
    outside.write_text('outside', encoding='utf-8')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        for values, expected in [({**scope, 'path': '../outside.txt'}, 403),
                                 ({**scope, 'path': str(outside)}, 403),
                                 ({**scope, 'path': 'missing.md'}, 404),
                                 ({**scope, 'path': '.', 'conversation_id': 'other'}, 409),
                                 ({**scope, 'path': '.', 'project': str(tmp_path)}, 409),
                                 ({**scope, 'path': '//server/share'}, 409)]:
            response = await client.get('/api/file-reference', params=values)
            assert response.status_code == expected, response.text
            assert response.json()['error']


@pytest.mark.asyncio
async def test_binary_and_large_file_previews_are_bounded(reference_app):
    app, root, scope = reference_app
    (root / 'data.bin').write_bytes(b'\x00\xff\xfe')
    text = root / 'large.txt'
    text.write_bytes(b'a' * (PREVIEW_BYTES - 1) + 'sample-project'.encode())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        binary = (await client.get('/api/file-reference', params={**scope, 'path': 'data.bin'})).json()
        assert binary['preview'] is None
        large = (await client.get('/api/file-reference', params={**scope, 'path': text.name})).json()
        assert large['truncated'] and len(large['preview']) <= PREVIEW_BYTES


@pytest.mark.asyncio
async def test_symlink_cannot_escape_project(reference_app, tmp_path):
    app, root, scope = reference_app
    outside = tmp_path / 'outside.txt'
    outside.write_text('outside', encoding='utf-8')
    try:
        (root / 'linked.txt').symlink_to(outside)
    except OSError:
        pytest.skip('symlink creation unavailable on this host')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/api/file-reference', params={**scope, 'path': 'linked.txt'})
        assert response.status_code == 403
