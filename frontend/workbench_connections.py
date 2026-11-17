"""Authenticated client views bound to one already running workflow owner."""
import html
import json
import os

import httpx
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from mewcode.workflow_core.connections import descriptors, inspect_connection, listing

READS = {'api/workbench/view', 'api/workbench/tasks', 'api/workbench/node', 'api/workbench/artifact', 'api/workbench/export'}
WRITES = {'api/workbench/graph/edit', 'api/workbench/graph/apply', 'api/workbench/run-action',
          'api/workbench/node-message', 'api/workbench/node-message/cancel'}


def descriptor(connection_id):
    return next((item for item in descriptors() if item['id'] == connection_id), None)


def bound_values(item, values):
    result = dict(values)
    for key in ('run_id', 'conversation_id', 'project'):
        value = result.get(key)
        if value and (os.path.normcase(os.path.realpath(value)) != os.path.normcase(os.path.realpath(item[key]))
                      if key == 'project' else value != item[key]):
            raise ValueError('请求与已连接的工作流不一致，请刷新后重试。')
        result[key] = item[key]
    return result


def install(app):
    @app.get('/api/workbench/connections')
    async def connections():
        return await listing()

    @app.get('/workbench/connections/{connection_id}', response_class=HTMLResponse)
    async def page(connection_id: str):
        item = descriptor(connection_id)
        if not item:
            return HTMLResponse('找不到这个工作流连接。', status_code=404)
        bootstrap = json.dumps(dict(conversationId=item['conversation_id'], project=item['project'],
            run_id=item['run_id'], apiBase='/api/workbench/connections/' + connection_id), ensure_ascii=False).replace('<', '\\u003c')
        return HTMLResponse('''<!doctype html><html lang="zh-CN" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>''' + html.escape(item['title']) + '''</title>
<link rel="stylesheet" href="/static/workbench/workbench.css?v=20261109-blueprint"></head><body>
<button id="workbench-toggle">工作台</button><div id="workbench-resize"></div><div id="workbench-backdrop"></div>
<aside id="task-workbench"></aside><script id="workflow-connection" type="application/json">''' + bootstrap + '''</script>
<script type="module" src="/static/workbench/connection-page.js?v=20261109-blueprint"></script></body></html>''')

    @app.api_route('/api/workbench/connections/{connection_id}/{path:path}', methods=['GET', 'POST'])
    async def relay(connection_id: str, path: str, request: Request):
        if path not in (READS if request.method == 'GET' else WRITES):
            return JSONResponse({'error': '这个接口不属于已连接的工作流。'}, status_code=404)
        item = descriptor(connection_id)
        if not item:
            return JSONResponse({'error': '找不到这个工作流连接。'}, status_code=404)
        if request.method == 'POST':
            origin = request.headers.get('origin')
            if (request.headers.get('x-mew-workbench') != '1' or request.headers.get('sec-fetch-site') == 'cross-site'
                    or origin and origin != str(request.base_url).rstrip('/')):
                return JSONResponse({'error': '请在当前客户端工作台操作。'}, status_code=403)
        try:
            values = await request.json() if request.method == 'POST' else dict(request.query_params)
            values = bound_values(item, values)
        except (ValueError, TypeError):
            return JSONResponse({'error': '请求与已连接的工作流不一致，请刷新后重试。'}, status_code=400)
        client = httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False)
        response = None
        async def cleanup():
            if response is not None:
                await response.aclose()
            await client.aclose()
        try:
            # Other read paths validate this binding in the owner too. Check its
            # identity before a mutation or file read in case a local port was reused.
            if request.method == 'POST' or path == 'api/workbench/artifact':
                current = await inspect_connection(client, item)
                if not current['available']:
                    await cleanup()
                    return JSONResponse({'error': current['status_label']}, status_code=409)
            target = item['url'] + '/' + path
            outgoing = client.build_request(request.method, target,
                **({'json': values, 'headers': {'X-Mew-Workbench': '1', 'Origin': item['url']}}
                   if request.method == 'POST' else {'params': values}))
            # Desktop cookies and credentials stay in this host. The run owner
            # receives only the bound workflow request, and is never started here.
            response = await client.send(outgoing, stream=True)
            if response.is_redirect:
                await cleanup()
                return JSONResponse({'error': '工作流连接已变化，请重新连接。'}, status_code=409)
            headers = {key: response.headers[key] for key in ('content-type', 'content-disposition', 'etag')
                       if key in response.headers}
            headers['Cache-Control'] = 'no-store'
            return StreamingResponse(response.aiter_bytes(), status_code=response.status_code,
                                     headers=headers, background=BackgroundTask(cleanup))
        except httpx.HTTPError:
            await cleanup()
            return JSONResponse({'error': '连接暂不可用，请刷新核对当前状态及操作结果。'}, status_code=503)
