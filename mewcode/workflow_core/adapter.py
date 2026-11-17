"""The public expert orchestrator delegates every graph to the same Core."""
from __future__ import annotations

import json
import copy
import uuid

from .migration import graph_from_flow, migrate
from .runtime import Core, MemoryBackend
from .state import fresh, command
from .store import ensure


async def run_group(host, *, task, profile, flow, strategy, run_id=None, checkpoint=None, **kwargs):
    from mewcode.expert_group import ExpertExecutionPolicy, ExpertRunResult
    from mewcode.experts.flow import ExpertNodeSpec
    from .web import NODE_PROMPT, resolve_model
    profile = profile.as_dict() if hasattr(profile, 'as_dict') else profile
    run_id = run_id or uuid.uuid4().hex
    graph = graph_from_flow(flow, goal=task)
    backend = (ensure(host.run_store, run_id, graph) if host.run_store and host.run_store.load(run_id)
               else MemoryBackend(migrate(copy.deepcopy(checkpoint), graph) if checkpoint else fresh(graph)))
    if checkpoint and backend.read()['status'] != 'running':
        backend.change(lambda state: command(state, dict(operation='resume', request_id=uuid.uuid4().hex)))

    async def execute(ctx):
        node = ctx.node
        if node['config'].get('manual'):
            return dict(status='needs_input', reason=node['task'] or '等待人工判断', manual_judgment=True)
        if node['config'].get('legacy_pipeline'):
            if not host.pipeline_runner:
                return dict(status='needs_input', reason='流水线执行器不可用')
            old = ExpertNodeSpec.from_value(node['config']['legacy_pipeline'])
            result = await host.pipeline_runner(old, ctx.inputs, ctx.checkpoint or None, host._emit)
        else:
            model = resolve_model(node, profile)
            spec = ExpertNodeSpec.from_value(dict(node_id=ctx.node_id, role='executor', label=node['label'],
                                                 responsibility=node['task'], model=model.as_dict()))
            policy = ExpertExecutionPolicy(strategy, False, 0, stage_inputs=ctx.inputs,
                input_revision=ctx.attempt_id, node_epoch=ctx.attempt_number,
                node_tools=tuple(node['tools']) if node.get('tools') is not None else None,
                deny_tools=tuple(node.get('deny_tools') or ()))
            prompt = NODE_PROMPT + '\n背景：' + task + '\n当前节点：' + node['task'] + '\n输入：' + json.dumps(ctx.inputs, ensure_ascii=False)
            if node['kind'] == 'judge':
                prompt += '\n按条件给出 JSON 判断：{"value":true或false或命名结果,"reason":"理由"}。条件：' + json.dumps(node['config'], ensure_ascii=False)
            result = (await host.node_runner(spec, model, prompt, policy, None) if host.node_runner
                      else await host.runner('executor', model, prompt, policy))
        status = result.status
        # Segment continuations preserve the same attempt; they are not acceptance failures.
        while status in {'continuing', 'budget_exhausted'} and not ctx.cancelled.is_set():
            if node['config'].get('legacy_pipeline'):
                break
            result = (await host.node_runner(spec, model, prompt, policy, None) if host.node_runner
                      else await host.runner('executor', model, prompt, policy))
            status = result.status
        output = dict(status='completed' if status == 'completed' else 'paused' if status == 'paused' else
                      'needs_input' if status in {'blocked', 'needs_attention'} else 'failed', text=result.text,
                      tokens=result.billable_tokens or result.input_tokens + result.output_tokens,
                      artifacts=(result.handoff or {}).get('artifacts', []))
        if node['kind'] == 'judge' and status == 'completed':
            if result.verdict:
                output['value'] = result.verdict.get('verdict') == 'PASS'
            elif result.loop_decision:
                output['value'] = result.loop_decision.get('action') == 'EXIT'
            else:
                try:
                    parsed = json.loads(result.text)
                    output['value'] = parsed['value']
                    output['text'] = parsed.get('reason', result.text)
                except (ValueError, KeyError, TypeError):
                    output.update(status='needs_input', reason='判断结果缺少 value')
        return output

    result = await Core(backend, execute, concurrency=graph.get('concurrency', 4), run_id=run_id, emit=host._emit).run()
    summaries = {path: row.get('result', {}).get('text', '') for path, row in result['nodes'].items()}
    return ExpertRunResult(run_id, result['status'], strategy, '', '', '', '',
                           final_summary='\n\n'.join(text for text in summaries.values() if text), node_summaries=summaries)
