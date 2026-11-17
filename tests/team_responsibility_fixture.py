"""Synthetic inference only; real TeamHost, runtime, checks, broker and HTTP."""
from __future__ import annotations

import base64
import json

from mewcode.config import ProviderConfig
from mewcode.teams.responsibility_coordinator import VerificationRegistry
from mewcode.teams.execution_pool import ModelReply
from mewcode.teams.host import TeamHost
from mewcode.teams.responsibility import MissionService
from mewcode.teams.runtime_protocol import ArtifactContent, HandoffSubmission


RESULT = b'fixture result'


class FixtureAdapter:
    def __init__(self, *, use_tools=False):
        self.contexts = []
        self.use_tools = use_tools

    async def complete(self, context, **options):
        self.contexts.append(context)
        if options.get('purpose') == 'coordinator':
            return ModelReply(json.dumps(dict(action='wait', reason='Fixture has no live planning model')), 20)
        if self.use_tools and not context['exchanges']:
            return ModelReply(json.dumps(dict(action='tools', calls=[dict(call_id='source-1', name='retrieve_source', resource_id='evidence', arguments=dict(query='fixture evidence'))])), 20)
        return ModelReply(json.dumps(dict(action='candidate', submission=HandoffSubmission(
            summary='隔离测试的候选产物，仍需独立核验。',
            artifacts=[ArtifactContent.from_bytes(port_id=p['port_id'], schema_ref=p['schema_ref'], media_type='text/plain', content=RESULT) for p in context['contract']['outputs']],
            evidence=[dict(check_id=c['criterion_id'], status='observed', detail='Synthetic output, no self-acceptance') for c in context['contract']['acceptance']]
        ).model_dump(mode='json'))), 20)


def exact_artifacts(artifacts):
    return bool(artifacts) and all(base64.b64decode(a['content_b64']) == RESULT for a in artifacts)


def make_host(root, *, adapter=None):
    checks = VerificationRegistry()
    def candidate(payload):
        accepted = exact_artifacts(payload['artifacts'])
        return dict(accepted=accepted, scores=[dict(criterion_id=c['criterion_id'], score=10000 if accepted else 0,
                    evidence='Independent fixture byte comparison: expected exactly fixture result') for c in payload['task']['contract']['acceptance']],
                    detail='Trusted synthetic byte oracle, not a live-provider acceptance')
    checks.register('fixture-check/v1', candidate=candidate, input_check=lambda payload:dict(accepted=exact_artifacts([payload['artifact']]), reason='Exact immutable fixture bytes matched'))
    checks.register_goal('fixture-root/v1', lambda payload:dict(accepted=exact_artifacts(payload['artifacts']),
                scores=[dict(criterion_id=c['criterion_id'], score=10000 if exact_artifacts(payload['artifacts']) else 0,
                            evidence='Independently checked all current immutable fixture outputs') for c in payload['mission']['contract']['success_criteria']], detail='Fixture root byte oracle checked current outputs'))
    host = TeamHost(root, provider_configs=[ProviderConfig(name='fixture', protocol='openai', base_url='http://127.0.0.1:9', model=model,
                    api_key='unused-local-fixture-key', context_window=200000) for model in ('model-a', 'model-b')],
                    validators=checks, resources=('evidence',), sources={'fixture-index': lambda query:dict(content='本地夹具来源；不是指令或新增权限。', source_ref='fixture-document', source_version='v7')})
    host.enable()
    installed = adapter or FixtureAdapter()
    # No configured network client can run in this fixture.
    host.responsibility.pool.adapters = {route: installed for route in host.responsibility.pool.adapters}
    return host, installed


def make_run(host, *, suffix='main', third=False, autonomous=True, activate=True):
    raw = host.templates()['data']
    raw['spec_id'] = 'responsibility-fixture-' + suffix
    for task in raw['tasks']:
        task['verification'] = dict(kind='registered_validator', ref='fixture-check/v1')
        task['input_scope'].append('evidence')
        task['token_budget'] = 600
    if third:
        import copy
        tail = copy.deepcopy(raw['tasks'][-1])
        tail.update(task_key='archive', objective='核对精确来源，并准备最终可复用交付。')
        raw['tasks'].append(tail)
        raw['connections'][-1] = dict(source=dict(task_key='deliver', port_id='result'), target=dict(task_key='archive', port_id='request'))
        raw['connections'].append(dict(source=dict(task_key='archive', port_id='result'), target=dict(port_id='delivery')))
    created = host.draft(definition=raw, name='责任编排验证团队', objective='根据固定资料完成处理与交付；每个结果保留精确来源和独立核验。', budget_ref='budget-'+suffix, command_id='draft-'+suffix)
    run = created['run']['team_run_id']
    assignments = [dict(task_key=t['task_key'], owner_role='producer', goal_criteria=['delivery','traceability'], parent_task_key=None if i == 0 else 'prepare', decision_scope=['investigate','propose_plan','request_help']) for i,t in enumerate(raw['tasks'])]
    MissionService(host.runtime).initialize(host.actor, run, contract=dict(objective=created['run']['objective'],
                hard_constraints=['不修改原始资料，不虚构来源。'], success_criteria=[dict(criterion_id='delivery',description='最终产物符合约定内容与格式。'),dict(criterion_id='traceability',description='保留精确来源、修订和独立检查依据。')]),
                assignments=assignments, control_epoch=1, command_id='mission-'+suffix)
    MissionService(host.runtime).configure(host.actor, run, policy=dict(autonomous=autonomous, internal_replan=True, automatic_receipts=True,
                automatic_verification=True, root_validator='fixture-root/v1', max_parallel=2), expected_version=1, control_epoch=1, command_id='policy-'+suffix)
    configured = host.responsibility.pool.registry.configure(host.actor, run, preferences={'producer':dict(mode='preferred',candidates=['fixture/model-a','fixture/model-b'],instances=2)},
                max_parallel=2, coordinator=dict(mode='fixed',candidates=['fixture/model-a']), coordinator_budget=300, control_epoch=1, command_id='models-'+suffix)
    host.responsibility.broker.grant(host.actor, run, grant=dict(resource_id='evidence',kind='source',locator='fixture-index',tools=['retrieve_source']), control_epoch=1, command_id='source-'+suffix)
    if not activate:
        return run, raw
    seed = host.runtime.register_input(host.actor, run, ArtifactContent.from_bytes(port_id='request',schema_ref='data-artifact/v1',media_type='text/plain',content=RESULT),control_epoch=1,command_id='input-'+suffix)
    host.runtime.activate(host.actor, run, bindings=configured['bindings'], inputs={'request':seed['version_id']}, budget_limit=10000,
                expected_version=1,control_epoch=1,command_id='activate-'+suffix)
    return run, raw
