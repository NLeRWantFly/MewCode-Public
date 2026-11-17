from pathlib import Path

import pytest

from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.authorization_ledger import record_authorization
from mewcode.kernel.investigations import InvestigationGate, Investigations


def setup(tmp_path, authorize=True):
    workspace = tmp_path / 'project'
    workspace.mkdir()
    (workspace / 'file.txt').write_text('before', encoding='utf-8')
    store = ExpertRunStore(tmp_path / 'runs')
    run_id = 'a' * 32
    store.create(run_id=run_id, conversation_id='fixture', task='repair file', attachments=[], profile={}, strategy='stable')
    request = {'operation_class': 'other', 'scope': {'tools': ['WriteFile'], 'paths': [str(workspace)]}}
    def seed(record):
        record['node_states']['N03'] = {'input_revision': 'v1'}
        if authorize:
            record_authorization(record, kind='typed_grant', scope={'operation_classes': ['other'], **request['scope']},
                                 quote='fixture user permits scoped writes', source_ref='fixture-user', host_verified=True)
    store.mutate(run_id, seed, durable=True)
    return Investigations(store, run_id, workspace), workspace, request


@pytest.mark.asyncio
async def test_independent_verified_changes_merge_only_after_mainline_backup(tmp_path):
    investigation, workspace, request = setup(tmp_path)
    verified = []
    async def execute(directory):
        assert directory != str(workspace)
        (Path(directory) / 'file.txt').write_text('after', encoding='utf-8')
        assert (workspace / 'file.txt').read_text(encoding='utf-8') == 'before'
    async def verify(directory, changes, change_digest):
        verified.append(change_digest)
        return {'passed': True, 'reason': 'fixture independent check passed', 'change_digest': change_digest}
    async def merge(changes, backup):
        assert investigation.snapshots.load(backup['snapshot_id'])['reason'] == 'before_investigation_merge'
        for path, content in changes.items():
            (workspace / path).write_bytes(content)
        return {'host_verified': True, 'change_digest': verified[-1]}
    result = await investigation.run(node_id='N03', requests=[request], execute=execute,
        verify=verify, merge=merge, executor_provider='fixture-a', verifier_provider='fixture-b')
    assert result['status'] == 'merged'
    assert (workspace / 'file.txt').read_text(encoding='utf-8') == 'after'
    assert investigation.store.public(investigation.run_id).get('kernel_investigations') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['unauthorized', 'same_provider', 'rejected', 'conflict'])
async def test_gate_conditions_never_publish_snapshot_changes(tmp_path, reason):
    investigation, workspace, request = setup(tmp_path, authorize=reason != 'unauthorized')
    async def execute(directory):
        (Path(directory) / 'file.txt').write_text('after', encoding='utf-8')
    async def verify(directory, changes, change_digest):
        if reason == 'conflict':
            (workspace / 'file.txt').write_text('concurrent', encoding='utf-8')
        return {'passed': reason != 'rejected', 'reason': 'fixture judgment', 'change_digest': change_digest}
    async def merge(*args):
        raise AssertionError('must not merge through a gate')
    with pytest.raises(InvestigationGate) as error:
        await investigation.run(node_id='N03', requests=[request], execute=execute, verify=verify, merge=merge,
            executor_provider='fixture-a', verifier_provider='fixture-a' if reason == 'same_provider' else 'fixture-b')
    assert error.value.kind == ('authorization_extension' if reason == 'unauthorized' else 'business')
    assert (workspace / 'file.txt').read_text(encoding='utf-8') == ('concurrent' if reason == 'conflict' else 'before')
    if reason == 'rejected':
        record = investigation.store.load(investigation.run_id)
        receipt = next(iter(record['kernel_investigations'].values()))
        assert receipt['rejected_snapshot']['materialized'] is False
        original = investigation.snapshots.load(receipt['snapshot_id'])
        assert (Path(original['work_dir']) / 'file.txt').read_text(encoding='utf-8') == 'before'
        assert any(fact['kind'] == 'blocker' and fact['payload'].get('reason') == 'fixture judgment'
                   for fact in record['node_ledgers']['N03']['facts'])


@pytest.mark.asyncio
async def test_assistance_uses_snapshot_executor_and_readonly_independent_role(tmp_path):
    from unittest.mock import AsyncMock
    from mewcode.expert_group import ExpertExecutionPolicy, ExpertRoleResult
    from mewcode.experts.assistance import AssistanceCoordinator, AssistanceRequest, RunContext
    from mewcode.experts.flow import ExpertFlowSpec
    investigation, workspace, scope = setup(tmp_path)
    node = ExpertFlowSpec.from_value({'flow_id': 'fixture', 'label': 'fixture', 'finalizer_node_id': 'N03',
        'nodes': [{'node_id': 'N03', 'label': '调查', 'role': 'manager'}], 'edges': []}).node_map['N03']
    executed = []
    async def execute(helper, prompt, policy):
        executed.append(policy)
        assert policy.investigation_snapshot['workspace'] == str(workspace)
        assert policy.work_dir != str(workspace)
        (Path(policy.work_dir) / 'file.txt').write_text('after', encoding='utf-8')
        return ExpertRoleResult(text='fixture work finished')
    async def verify(helper, prompt, policy):
        assert helper.role == 'auditor' and policy.parallel_readonly
        assert 'WriteFile' in policy.deny_tools
        assert (Path(policy.work_dir) / 'file.txt').read_text(encoding='utf-8') == 'after'
        return ExpertRoleResult(text='fixture independent check passed', verdict={'verdict': 'PASS'})
    async def merge(changes, backup, original, policy, change_digest, scopes):
        assert scopes == (scope,)
        for relative, content in changes.items():
            (workspace / relative).write_bytes(content)
        return {'host_verified': True, 'change_digest': change_digest}
    coordinator = AssistanceCoordinator(store=investigation.store, run_id=investigation.run_id, dispatch=execute,
        emit=AsyncMock(), before_resume=AsyncMock(), task='repair fixture', work_dir=str(workspace),
        model={'provider_key': 'fixture-a'}, verification_model={'provider_key': 'fixture-b'},
        verification_dispatch=verify, merge_invoker=merge)
    request = AssistanceRequest('fixture-investigation', 'N03', 0, 'investigation', 'repair fixture', (), ('WriteFile',), 'node', (scope,))
    receipt = await coordinator.run(request, RunContext(node, ExpertExecutionPolicy('stable', False, 0)))
    assert receipt.next_step == 'resume'
    second = await coordinator.run(request, RunContext(node, ExpertExecutionPolicy('stable', False, 0)))
    assert second.next_step == 'resume' and len(executed) == 1
