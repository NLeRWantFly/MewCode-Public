"""Read-only workflow blueprints bound to the selected conversation/run."""
import asyncio

from fastapi import Request
from fastapi.responses import JSONResponse

from mewcode.workflow_core.blueprint import documents


def install(app, workbench):
    @app.get('/api/workbench/export')
    async def export(request: Request):
        values = dict(request.query_params)
        try:
            owner = workbench.scope(values)
            def read():
                _, _, _, _, record = workbench.select(values, read_only=True)
                if not record:
                    raise ValueError('请先选择已有工作流运行。')
                packet = workbench.srv.EXPERT_RUNS.read_workflow_blueprint(record['run_id'], values.get('selection', 'applied'))
                if packet['source']['conversation_id'] != owner[1]:
                    raise ValueError('项目或对话已切换，请重新选择工作流。')
                return documents(packet)
            result = await asyncio.to_thread(read)
            if workbench.scope(values) != owner:
                raise ValueError('项目或对话已切换，请重新提取。')
            return JSONResponse(result, headers={'Cache-Control': 'no-store'})
        except LookupError as exc:
            return JSONResponse({'error': str(exc)}, status_code=404)
        except (ValueError, TypeError, OSError) as exc:
            return JSONResponse({'error': str(exc)}, status_code=409)
