"""Opt-in real-model contract probe; all tool effects stay in synthetic fixtures.

Example (uses the user's existing OpenCode Go profile, never saves credentials):
  python scripts/probe_orchestration_tools.py --live --output .tmp/orchestration-probe
Without --live, only the scenario inventory is printed. No business run is resumed.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mewcode.agent import Agent, ErrorEvent, ToolResultEvent, ToolUseEvent, TurnComplete
from mewcode.conversation import ConversationManager
from mewcode.desktop_providers import read_snapshot
from mewcode.expert_group import ExpertExecutionPolicy, ExpertRoleResult
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.assistance import AssistanceCoordinator, AssistanceRequest, RunContext
from mewcode.experts.delivery import delivery_prompt
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.model_registry import ModelRef, ProviderRegistry
from mewcode.skills.loader import SkillLoader
from mewcode.skills.runtime import attach_skills
from mewcode.tools import ToolRegistry
from mewcode.tools.impl.tool_search import ToolSearchTool
from mewcode.tools.impl.invoke_deferred import InvokeDeferredTool
from mewcode.tools.read_stage_result import ReadStageResult
from mewcode.tools.request_assistance import RequestAssistance, SubmitAssistanceReceipt
from mewcode.tools.submit_handoff import HandoffHolder, SubmitHandoff
from mewcode.tools.submit_loop_decision import SubmitLoopDecisionTool
from mewcode.tools.submit_verdict import SubmitVerdictTool, VerdictHolder

SCENARIOS = ['skill_and_paged_delivery', 'request_assistance', 'assistance_receipt',
             'resume_delivery', 'independent_verdict', 'loop_decision']
BOUNDARY = (
    '这是有明确范围的工具连调任务。输入仅为本轮提供的合成记录及测试 Skill。'
    '禁止操作真实项目、业务 PDF、原始会话、外部服务或配置；不安装、不改代码、不自行调用其他模型。'
    '交付为本轮要求的结构化工具回执；宿主验证实际工具调用、分页末尾校验值和持久化状态。'
    '验收命令由宿主运行：python scripts/probe_orchestration_tools.py --live；你只完成当前测试节点。'
    '可以通过工具读取测试输入；不能用口头声称调用成功代替工具调用。'
)


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


class Probe:
    def __init__(self, output, resolved):
        self.output, self.resolved = output, resolved
        self.cases = []

    async def run_agent(self, name, registry, prompt, *, role='executor', helper=False,
                        skills=False, stop=None):
        work = self.output / name
        work.mkdir(parents=True, exist_ok=False)
        agent = Agent(self.resolved.client, registry, self.resolved.protocol, str(work),
                      max_iterations=14, context_window=self.resolved.context_window,
                      instructions_content=BOUNDARY)
        if skills:
            skill_dir = work / '.mewcode/skills/capsule-report'
            skill_dir.mkdir(parents=True)
            (skill_dir / 'SKILL.md').write_text(
                '---\nname: capsule-report\ndescription: 合成数据胶囊报告与尾部校验\n'
                'allowedTools: [WriteFile]\nmode: inline\n---\n'
                '读取完整输入的开头与末尾。交付一张 Markdown 表，列名必须为“项目”和“观测值”。'
                '至少包含 source_token 与 end_seal 的原值。交付正文写上 CAPSULE_SOP_APPLIED。'
                '这是内容交付，不要求文件。', encoding='utf-8')
            loader = SkillLoader(str(work))
            loader._user_dir = work / 'empty-user-skills'  # Never transmit personal skills.
            attach_skills(str(work), agent, registry, loader=loader, instruction_only=True)
        agent.instructions_content += delivery_prompt(role,
            [t.name for t in registry.list_tools() if registry.is_enabled(t.name)],
            assistance_session=helper)
        conversation = ConversationManager()
        conversation.add_user_message(prompt)
        case = {'name': name, 'calls': [], 'results': [], 'errors': []}
        self.cases.append(case)
        emit({'case_started': name})
        stream = agent.run(conversation)
        try:
            async with asyncio.timeout(240):
                async for event in stream:
                    if isinstance(event, ToolUseEvent):
                        case['calls'].append({'tool': event.tool_name, 'id': event.tool_id, 'arguments': event.arguments})
                        emit({'case': name, 'tool': event.tool_name})
                    elif isinstance(event, ToolResultEvent):
                        case['results'].append({'tool': event.tool_name, 'is_error': event.is_error,
                                                'output': event.output, 'observation': event.host_observation})
                    elif isinstance(event, ErrorEvent):
                        # Tool schemas and synthetic results are persisted; never dump client/config objects.
                        case['errors'].append({'kind': event.kind, 'message': event.message[:1200]})
                    elif isinstance(event, TurnComplete) and stop and stop():
                        case['stopped_at_settled_turn'] = True
                        break
        finally:
            await stream.aclose()
            case['active_skills'] = list(agent.active_skills)
            case['enabled_tools'] = [t.name for t in registry.list_tools() if registry.is_enabled(t.name)]
            case['input_tokens'] = agent.total_input_tokens
            case['output_tokens'] = agent.total_output_tokens
            self.save()
        assert not case['errors'], case['errors']
        recovered = []
        for index, row in enumerate(case['results']):
            if not row['is_error']:
                continue
            # Validation fails before dispatch. A corrected later submission is
            # a successful recovery, but must remain visible in the report.
            recoverable = row['output'].startswith('Parameter validation error:') or (
                row['tool'] == 'ReadStageResult' and row['output'].startswith('该节点不在本次已激活上游输入中。'))
            assert recoverable, row['output']
            assert any(later['tool'] == row['tool'] and not later['is_error']
                       for later in case['results'][index + 1:]), 'Unresolved parameter error'
            recovered.append({'tool': row['tool'], 'error': row['output']})
        case['recovered_call_errors'] = recovered
        return case

    def save(self):
        (self.output / 'report.json').write_text(json.dumps({
            'route': {'provider': self.resolved.ref.provider_key, 'model': self.resolved.config.model,
                      'protocol': self.resolved.protocol, 'base_url': self.resolved.config.base_url},
            'synthetic_inputs_only': True, 'cases': self.cases,
        }, ensure_ascii=False, indent=2), encoding='utf-8')

    async def run(self):
        seal, token = uuid.uuid4().hex[:14], uuid.uuid4().hex[:14]
        payload = f'source_token={token}\n' + '合成填充段，无须复述。' * 1450 + f'\nend_seal={seal}'
        sources = {'fixture_source': {'status': 'completed', 'handoff': {
            'inline_outputs': [{'name': 'Synthetic capsule', 'content': payload}]}}}
        holder = HandoffHolder()
        registry = ToolRegistry()
        reader = ReadStageResult(sources)
        reader.should_defer = True  # Exercise the production discovery/gateway path.
        registry.register(reader)
        registry.register(ToolSearchTool(registry, self.resolved.protocol))
        registry.register(InvokeDeferredTool(registry))
        registry.register(SubmitHandoff(holder))
        case = await self.run_agent('skill_and_paged_delivery', registry,
            '先搜索已安装的胶囊报告 Skill，再加载匹配项，按其 SOP 整理本节点合成上游数据，完整读取并交付报告。', skills=True,
            stop=lambda: holder.read() is not None)
        handoff = holder.read() or {}
        content = json.dumps(handoff.get('inline_outputs'), ensure_ascii=False)
        assert handoff.get('status') == 'completed' and seal in content and token in content
        assert 'CAPSULE_SOP_APPLIED' in content and '项目' in content and '观测值' in content, 'Loaded SOP was not followed'
        assert any(r['tool'] == 'FindSkills' for r in case['calls']), 'Skill discovery was not exercised'
        assert 'capsule-report' in case['active_skills'] and 'WriteFile' not in case['enabled_tools']
        assert registry.is_discovered('ReadStageResult')
        pages = [r['observation']['read_cursor'] for r in case['results']
                 if isinstance(r.get('observation'), dict) and r['observation'].get('read_cursor')]
        assert any(p['node_id'] == 'fixture_source' and p['next_offset'] >= p['total_chars'] for p in pages)
        case['passed'] = True

        flow = ExpertFlowSpec.from_value({'flow_id': 'synthetic-probe', 'finalizer_node_id': 'work',
            'nodes': [{'node_id': 'work', 'role': 'executor', 'tools': ['ReadStageResult']}], 'edges': []})
        store = ExpertRunStore(self.output / 'runs')
        run_id = uuid.uuid4().hex
        store.create(run_id=run_id, conversation_id='synthetic-probe', task='合成能力诊断',
                     attachments=[], profile={}, strategy='stable', flow=flow)
        store.mutate(run_id, lambda r: r['node_states'].setdefault('work', {}).update(
            input_revision='fixture-revision', status='running', messages=[]))
        registry, pending_handoff = ToolRegistry(), HandoffHolder()
        registry.register(RequestAssistance(store, run_id, 'work'))
        registry.register(SubmitHandoff(pending_handoff))
        def pending():
            return (store.load(run_id)['node_states']['work'].get('assistance_request') or {}).get('status') == 'pending'
        case = await self.run_agent('request_assistance', registry,
            '当前合成节点缺少读取服务状态的工具，请向主模型请求能力诊断。已完成输入核对，'
            '尚未核验服务。不需要新增权限或隔离调查；请说明这个障碍及已有进展。', stop=pending)
        assert pending() and pending_handoff.read() is None
        request = store.load(run_id)['node_states']['work']['assistance_request']
        assert request['kind'] in {'capability', 'diagnosis'} and not request['scope_requests']
        case['passed'] = True

        async def noop(*args, **kwargs):
            pass

        async def dispatch(helper_node, prompt, policy):
            registry, handoff = ToolRegistry(), HandoffHolder()
            registry.register(ReadStageResult({**policy.stage_inputs, 'service_probe': {
                'status': 'completed', 'handoff': {'inline_outputs': [
                    {'name': 'Synthetic observation', 'content': 'service_status=ready; observation_token=' + seal}]}}}))
            registry.register(SubmitAssistanceReceipt(policy.assistance_holder))
            registry.register(SubmitHandoff(handoff))
            case = await self.run_agent('assistance_receipt', registry,
                prompt + '\n宿主合成输入 service_probe 包含本次服务状态观测，仅本协助节点可见；读取并回交下一步。'
                '原节点将读取你的协助回执，不会自动获得这个新来源。仅检查合成记录，无实际外部服务调用。', helper=True,
                stop=lambda: bool(policy.assistance_holder) and handoff.read() is not None)
            assert policy.assistance_holder.get('next_step') == 'resume'
            assert not policy.assistance_holder.get('clearances')
            assert seal in json.dumps(policy.assistance_holder, ensure_ascii=False)
            assert handoff.read() and handoff.read()['status'] == 'completed'
            calls = [r['tool'] for r in case['calls']]
            assert calls.index('SubmitAssistanceReceipt') < calls.index('SubmitHandoff')
            case['passed'] = True
            return ExpertRoleResult(text='Synthetic diagnosis delivered', status='completed', handoff=handoff.read())

        coordinator = AssistanceCoordinator(store=store, run_id=run_id, dispatch=dispatch, emit=noop,
            before_resume=noop, task='仅核验合成服务记录', work_dir=str(self.output), model=asdict(self.resolved.ref))
        receipt = await coordinator.run(AssistanceRequest(**{k: v for k, v in request.items() if k != 'status'}),
            RunContext(flow.node_map['work'], ExpertExecutionPolicy('stable', False, 0, input_revision='fixture-revision')))
        assert receipt.next_step == 'resume', asdict(receipt)
        stage = store.load(run_id)['node_states']['work']
        assert stage['assistance_receipts'] and stage.get('assistance_resume_message')
        registry, holder = ToolRegistry(), HandoffHolder()
        registry.register(ReadStageResult({'@assistance': asdict(receipt)}))
        registry.register(SubmitHandoff(holder))
        case = await self.run_agent('resume_delivery', registry,
            '你是原节点，收到宿主协助回执后接续。读取当前来源中协助者的真实回执，报告合成服务状态与观测 token。'
            '本节点仅要求内容交付，不要求实际连接服务或写文件。', stop=lambda: holder.read() is not None)
        assert holder.read() and holder.read()['status'] == 'completed'
        assert seal in json.dumps(holder.read(), ensure_ascii=False)
        case['passed'] = True

        registry, verdict = ToolRegistry(), VerdictHolder()
        registry.register(ReadStageResult({'work': {'status': 'completed', 'handoff': holder.read()}}))
        registry.register(SubmitVerdictTool(verdict))
        case = await self.run_agent('independent_verdict', registry,
            '独立审核合成 work 节点的内容交付。验收：包含合成服务 ready 状态、非空观测 token，'
            '没有声称访问真实服务。读取完整交付后给结构化审计结论。', role='auditor', stop=lambda: verdict.read() is not None)
        assert verdict.read() and verdict.read()['verdict'] == 'PASS'
        assert any(r['tool'] == 'ReadStageResult' for r in case['calls'])
        case['passed'] = True

        loop = {'contract_hash': uuid.uuid4().hex, 'criteria': [{'criterion_id': 'delivery'}],
                'effective_control_mode': 'team', 'node_ids': ['work', 'review'], 'reviewer_node_ids': ['review']}
        registry, verdict = ToolRegistry(), VerdictHolder()
        registry.register(ReadStageResult({'work': {'status': 'completed', 'handoff': holder.read()}}))
        registry.register(SubmitLoopDecisionTool(verdict, loop))
        case = await self.run_agent('loop_decision', registry,
            '审核当前循环的唯一冻结条件 delivery：合成 work 交付包含 ready 状态与非空观测 token。'
            '读取实际交付后提交循环判定。冻结上下文：' + json.dumps(loop, ensure_ascii=False),
            role='auditor', stop=lambda: verdict.read() is not None)
        assert verdict.read() and verdict.read()['decision'] == 'EXIT'
        case['passed'] = True
        self.save()
        emit({'passed': len(self.cases), 'expected': len(SCENARIOS), 'report': str(self.output / 'report.json')})


async def main(args):
    if not args.live:
        emit({'scenarios': SCENARIOS, 'model': 'deepseek-flash', 'provider': 'OpenCode Go', 'live': False})
        return
    output = Path(args.output).resolve()
    repo = Path(__file__).resolve().parents[1]
    if not output.is_relative_to(repo / '.tmp'):
        raise ValueError('Probe outputs must stay under the repository .tmp directory')
    output.mkdir(parents=True, exist_ok=False)
    rows = read_snapshot(Path(os.environ['APPDATA']) / 'com.mewcode.desktop/providers.json')
    rows = [r for r in rows if r.get('base_url', '').rstrip('/') == 'https://opencode.ai/zen/go/v1'
            and (not args.provider_key or r.get('provider_key') == args.provider_key)]
    if len(rows) != 1:
        raise ValueError('Select exactly one configured OpenCode Go route with --provider-key; no fallback')
    registry = ProviderRegistry()
    registry.replace_desktop_profiles(rows)
    resolved = registry.resolve(ModelRef(rows[0]['provider_key'], 'deepseek-flash'))
    if resolved.config.model != 'deepseek-flash':
        raise ValueError('Unexpected model substitution')
    resolved.client.max_output_tokens = 4096  # Local probe budget; persisted model settings are untouched.
    probe = Probe(output, resolved)
    try:
        await probe.run()
    except Exception as exc:
        if probe.cases:
            probe.cases[-1]['passed'] = False
            probe.cases[-1]['verification_failure'] = type(exc).__name__ + ': ' + str(exc)[:1200]
        raise
    finally:
        probe.save()
        await resolved.client._client.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--provider-key', default='', help='Exact existing profile if more than one OpenCode Go route exists')
    parser.add_argument('--output', default='.tmp/orchestration-probe-' + uuid.uuid4().hex[:8])
    asyncio.run(main(parser.parse_args()))
