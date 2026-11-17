"""A reviewed criteria revision must reach dispatch, not just a handoff note."""
import pytest

from mewcode.expert_group import ExpertRoleResult
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.kernel import collaboration as ledger
from mewcode.orchestration.collab_service import user_revision
from mewcode.orchestration.task_view import _requirements, _plan_versions, collaboration_projection
from tests.test_collaboration_integration import orchestrator, run
from tests.test_collaboration_mode import PROFILE, REVIEWER, RUN_ID, delivery, flow, store_for


def revision_store(tmp_path):
    raw = flow().as_dict()
    old = 'Historical validation must precede first parse'
    raw['meta']['node_plan_briefs']['work']['acceptance'] = [old]
    raw['nodes'][0]['responsibility'] = 'Prepare environment\n步骤：Keep originals\n验收：' + old
    raw['meta']['collaboration_spec']['document']['entries'].append(dict(
        id='work', kind='node', body='Prepare environment', state='active', sources=[],
        data={'acceptance': [old], 'contract': {'method': {'id': 'original', 'description': 'Offline only'},
                                              'forbidden_changes': ['Keep originals']}}))
    spec = ExpertFlowSpec.from_value(raw)
    store = ExpertRunStore(tmp_path / 'runs')
    store.create(run_id=RUN_ID, conversation_id='conversation', task='goal', attachments=[],
                 profile=PROFILE.as_dict(), strategy='stable', flow=spec)
    store.mutate(RUN_ID, lambda record: record.update(role_models={'roles': {
        'verifier': REVIEWER.as_dict(), 'reviewer': REVIEWER.as_dict()}}), durable=True)
    return store, spec, old


@pytest.mark.asyncio
async def test_revision_reaches_review_dispatch_and_current_view(tmp_path):
    store, spec, old = revision_store(tmp_path)
    before = store.load(RUN_ID)
    payload = dict(entry_id='work', field='acceptance', body='Record historical ordering gap\nRequire validation before future parses',
                   expected_version=1, request_id='acceptance-v2')
    saved = user_revision(store, before, **payload)
    assert saved['collaboration']['contracts']['work']['checks'][0]['criteria'] == [old]
    assert saved['collaboration']['contracts']['work']['held']
    reviews, invoked = [], []

    async def coordinate(model, proposed, system):
        reviews.append(proposed)
        assert proposed['proposal']['payload']['actor'] == 'user'
        assert proposed['user_sources']['user_revision:acceptance-v2']['text'] == payload['body']
        return {'passed': True, 'evidence': ['explicit revision and retained restrictions']}

    async def runner(node, model, prompt, *args):
        invoked.append(node.node_id)
        if node.node_id == 'work':
            assert old not in node.responsibility
            assert old not in prompt
            assert 'Require validation before future parses' in prompt
            assert 'Keep originals' in node.responsibility
        result = delivery()
        if node.node_id == 'work':
            contract = store.load(RUN_ID)['collaboration']['contracts']['work']
            result.handoff.update(method_id='original', method_decision=contract.get('method_decision'))
        return result

    async def review(model, data, *args):
        if data['contract']['id'] == 'work':
            assert data['contract']['checks'][0]['version'] == 2
        return {'passed': True, 'evidence': ['current criteria checked']}

    result = await run(orchestrator(store, tmp_path, runner, review, coordinate), store, spec)
    assert result.status == 'completed', result.final_summary
    assert invoked == ['work', 'deliver'] and len(reviews) == 1
    record = store.load(RUN_ID)
    value = record['collaboration']
    assert value['contracts']['work']['version'] == 2
    assert value['contracts']['work']['method_decision'] == before['collaboration']['contracts']['work']['method_decision']
    assert value['contracts']['work']['forbidden_changes'] == before['collaboration']['contracts']['work']['forbidden_changes']
    assert value['entries']['work']['data']['acceptance'] == payload['body'].splitlines()
    assert value['entries']['work']['field_sources']['data.acceptance'][0]['source_id'] == 'user_revision:acceptance-v2'
    assert ledger.valid_certificate(value, 'work')
    assert len(value['proposals']) == 1
    assert user_revision(store, record, **payload)['status'] == record['status']
    rows = [r for r in _requirements(value) if r['id'] == 'work']
    assert {r['field'] for r in rows} == {'acceptance', 'method'}
    assert next(r for r in rows if r['field'] == 'acceptance')['body'] == payload['body']
    assert any(r['field'] == 'acceptance' for r in collaboration_projection(record)['entries'])
    versions = _plan_versions(record, value, [])
    assert old in versions[0]['nodes'][0]['goal']
    assert old not in versions[-1]['nodes'][0]['goal']


@pytest.mark.parametrize('case', ['host', 'command', 'multiple', 'empty', 'stale'])
def test_revision_preserves_authority_and_check_types(tmp_path, case):
    store, spec, old = revision_store(tmp_path)
    def prepare(record):
        value = record['collaboration']
        if case == 'host':
            value['entries']['work']['origin'] = 'host'
        elif case == 'command':
            value['contracts']['work']['checks'] = [dict(id='test', kind='command', version=1, command='python verify.py')]
        elif case == 'multiple':
            value['contracts']['work']['checks'].append(dict(id='file', kind='file_exists', version=1, path='result.json'))
    before = store.mutate(RUN_ID, prepare, durable=True)
    with pytest.raises(ValueError):
        user_revision(store, before, entry_id='work', field='acceptance', body=' ' if case == 'empty' else 'New criteria',
                      expected_version=0 if case == 'stale' else 1, request_id='rejected')
    assert store.load(RUN_ID)['collaboration'] == before['collaboration']


@pytest.mark.asyncio
@pytest.mark.parametrize('blocker', ['plan_revision_required', 'contract_conflict'])
@pytest.mark.parametrize('action', ['retry', 'propose_patch', None])
async def test_revision_blocker_never_becomes_same_contract_retry_or_file_patch(tmp_path, blocker, action):
    store = store_for(tmp_path)
    invoked = []
    async def runner(node, *args):
        invoked.append(node.node_id)
        return ExpertRoleResult(text='Current criteria cannot be satisfied retrospectively', status='needs_attention',
            handoff={'status': 'needs_attention', 'blocker_kind': blocker,
                     'blockers': ['Review proposed contract revision']})
    async def coordinate(*args):
        return {'action': action, 'reason': 'Needs a contract revision', 'instructions': 'retry unchanged'}
    async def review(*args):
        raise AssertionError('An incomplete handoff must not be accepted')
    result = await run(orchestrator(store, tmp_path, runner, review, coordinate if action else None), store)
    assert result.status == 'blocked', result.final_summary
    assert invoked == ['work']
    value = store.load(RUN_ID)['collaboration']
    assert any(g['status'] == 'waiting' and g['kind'] == 'coordination_wait' for g in value['recovery_gates'].values())
    assert not value['certificates']
