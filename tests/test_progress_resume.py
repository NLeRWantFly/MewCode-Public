"""Resume display and independent assistance use actual execution facts."""
import pytest

from mewcode.expert_group import ExpertRoleResult
from mewcode.orchestration.task_view import finish
from tests.test_collaboration_mode import RUN_ID, delivery, store_for
from tests.test_collaboration_integration import orchestrator, run


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['model_not_found', 'assistance_requested'])
async def test_failed_branch_keeps_independent_work_and_names_its_error(tmp_path, kind):
    from mewcode.expert_runs import ExpertRunStore
    from mewcode.experts.flow import ExpertFlowSpec
    from tests.test_collaboration_mode import PROFILE, REVIEWER, flow
    spec = flow().as_dict()
    spec['edges'] = []
    spec = ExpertFlowSpec.from_value(spec)
    store = ExpertRunStore(tmp_path / 'runs', storage='sqlite')
    store.create(run_id=RUN_ID, conversation_id='conversation', task='goal', attachments=[],
                 profile=PROFILE.as_dict(), strategy='stable', flow=spec)
    store.mutate(RUN_ID, lambda row: row.update(role_models={'roles': {'verifier': REVIEWER.as_dict()}}))
    invoked = []
    async def runner(node, *_):
        invoked.append(node.node_id)
        if node.node_id == 'work':
            if kind == 'assistance_requested':
                store.mutate(RUN_ID, lambda row: row['node_states'].setdefault('work', {}).update(
                    assistance_request={'request_id': 'unsettled-help', 'status': 'pending'}))
            return ExpertRoleResult(text=kind, status='needs_attention', error_kind=kind)
        return delivery()
    async def review(*_):
        return {'passed': True, 'evidence': ['checked']}
    async def incomplete_helper(**_):
        return {'action': 'continue_checkpoint', 'chosen': {'next_step': '仅建议，还未完成协助'}}
    host = orchestrator(store, tmp_path, runner, review)
    if kind == 'assistance_requested':
        host.recovery_handler = incomplete_helper
    result = await run(host, store, spec)
    assert result.status == 'blocked' and invoked == ['work', 'deliver']
    assert 'work：\n' + kind in result.final_summary
    assert 'deliver' in store.read_collaboration(RUN_ID)['certificates']


def test_waiting_branch_does_not_offer_resume_while_host_is_running():
    view = {'task': {'id': 't', 'run_id': 'r', 'status': '等你处理', 'execution_status': 'running',
                     'controls': ['resume', 'cancel']},
            'plan': [{'steps': [{'id': 'a', 'name': '故障分支', 'status': '等你处理'},
                               {'id': 'b', 'name': '独立分支', 'status': '进行中'}]}]}
    task = finish(view)['task']
    assert task['status'] == '进行中' and task['controls'] == ['pause', 'cancel']
    assert '独立分支' in task['current_activity'] and task['waiting_branches'] == 1


@pytest.mark.asyncio
async def test_full_checkpoint_io_stays_off_message_loop(tmp_path, monkeypatch):
    import threading
    from tests.test_collaboration_mode import CATALOG, PROFILE, flow
    store = store_for(tmp_path)
    checkpoint = store.load(RUN_ID)
    host_thread = threading.get_ident()
    seen = []
    for name in ('load', 'mutate'):
        original = getattr(store, name)
        def guarded(*args, _name=name, _original=original, **kwargs):
            assert threading.get_ident() != host_thread, _name + ' blocked the message loop'
            seen.append(_name)
            return _original(*args, **kwargs)
        monkeypatch.setattr(store, name, guarded)
    async def runner(*_):
        return delivery()
    async def review(*_):
        return {'passed': True, 'evidence': ['checked']}
    host = orchestrator(store, tmp_path, runner, review)
    result = await host.run('goal', PROFILE, CATALOG, strategy='stable', run_id=RUN_ID,
                            flow=flow(), checkpoint=checkpoint)
    assert result.status == 'completed'
    assert {'load', 'mutate'} <= set(seen)


@pytest.mark.asyncio
async def test_resume_does_not_reemit_completed_stages(tmp_path):
    store = store_for(tmp_path)
    invoked, events = [], []
    async def runner(node, *_):
        invoked.append(node.node_id)
        return delivery()
    async def review(*_):
        return {'passed': True, 'evidence': ['checked']}
    host = orchestrator(store, tmp_path, runner, review)
    assert (await run(host, store)).status == 'completed'
    host.event_sink = events.append
    assert (await run(host, store)).status == 'completed'
    assert invoked == ['work', 'deliver']
    assert not [e for e in events if e['type'] in {'expert_node_started', 'expert_node_completed'}]


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy', [False, True])
async def test_assistance_is_settled_before_business_continues(tmp_path, legacy):
    store = store_for(tmp_path)
    invoked, helped = [], []
    async def runner(node, *_):
        invoked.append(node.node_id)
        if node.node_id == 'work' and invoked.count('work') == 1:
            store.mutate(RUN_ID, lambda row: row['node_states'].setdefault('work', {}).update(
                assistance_request={'request_id': 'help-1', 'status': 'pending'}))
            return ExpertRoleResult(text='需要核对当前环境', status='needs_attention',
                                    error_kind='assistance_requested', handoff={'assistance_request_id': 'help-1'})
        return delivery()
    async def review(*_):
        return {'passed': True, 'evidence': ['checked']}
    async def help_node(**kwargs):
        assert kwargs['node'].node_id == 'work'
        assert kwargs['host_facts']['trigger'] == 'assistance_requested'
        helped.append('work')
        store.mutate(RUN_ID, lambda row: row['node_states']['work']['assistance_request'].update(status='completed'))
        return {'action': 'continue_checkpoint', 'chosen': {'next_step': '已核对，继续'}}
    host = orchestrator(store, tmp_path, runner, review)
    if legacy:
        assert (await run(host, store)).status == 'blocked'
        store.mutate_collaboration(RUN_ID, lambda value: next(iter(value['attempts'].values())).update(status='rejected'))
        store.transition(RUN_ID, 'paused')
        store.resume(RUN_ID, strategy='stable')
    host.recovery_handler = help_node
    assert (await run(host, store)).status == 'completed'
    assert invoked == ['work', 'work', 'deliver'] and helped == ['work']
    assert store.read_turn_boundary(RUN_ID, 'work')['assistance_request']['status'] == 'completed'


@pytest.mark.asyncio
@pytest.mark.parametrize('next_step', ['resume', 'rebase'])
@pytest.mark.parametrize('legacy_reservation', [False, True])
async def test_real_scheduler_runs_explicit_readonly_assistance_without_changing_contract(tmp_path, next_step, legacy_reservation):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from mewcode.kernel.escalation import KernelScheduler, decide_and_record, run_assist_count, unstarted_assistance_decision
    from mewcode.experts.assistance import AssistanceCoordinator
    from mewcode.expert_group import ExpertExecutionPolicy
    from mewcode.kernel.events import digest
    from tests.test_collaboration_mode import flow
    from mewcode.tools.request_assistance import RequestAssistance, RequestAssistanceParams
    store = store_for(tmp_path)
    store.mutate(RUN_ID, lambda row: row['node_states'].setdefault('work', {}))
    requested = await RequestAssistance(store, RUN_ID, 'work').execute(
        RequestAssistanceParams(kind='diagnosis', need='只核对已有回执'))
    assert not requested.is_error
    if legacy_reservation:
        # The old host consumed both the assist and replan rungs without ever
        # attaching a helper. Finish the existing assist; retain both records.
        def reserve_without_dispatch(row):
            first = decide_and_record(row, 'work', trigger='assistance_requested', identity='old-assist')
            second = decide_and_record(row, 'work', trigger='assistance_requested', identity='old-replan')
            assert first.action == 'assist' and second.action == 'patch_node'
        store.mutate(RUN_ID, reserve_without_dispatch)
    before = store.load(RUN_ID)
    seen = []
    async def dispatch(node, prompt, policy):
        seen.append(node.node_id)
        assert policy.assistance_session and policy.parallel_readonly
        assert '不能改文件、调用命令或改合同' in prompt
        policy.assistance_holder.update(next_step=next_step, notes='核对结束', patch={'attempted': 'change'})
        return ExpertRoleResult('已核对', status='completed')
    assistant = AssistanceCoordinator(store=store, run_id=RUN_ID, dispatch=dispatch, emit=AsyncMock(),
        before_resume=AsyncMock(), task='goal', work_dir=str(tmp_path), model={})
    scheduler = KernelScheduler(session=SimpleNamespace(work_dir=str(tmp_path)), registry=None, store=store,
        run_id=RUN_ID, emit=AsyncMock(), before_resume=AsyncMock(), task='goal', primary_repair=assistant)
    decision = await scheduler(node=flow().node_map['work'],
        result=ExpertRoleResult('请核对回执', status='needs_attention', error_kind='assistance_requested'),
        policy=ExpertExecutionPolicy('stable', False, 0), allowed=['continue_checkpoint', 'diagnose_path'],
        host_facts={'trigger':'assistance_requested', 'preserve_collaboration_contract':True, 'defer_human':True})
    assert len(seen) == 1
    assert decision['action'] == ('diagnose_path' if next_step == 'resume' else 'human')
    if next_step == 'rebase':
        assert '不能修改协作合同' in decision['chosen']['next_step']
    after = store.load(RUN_ID)
    assert after['node_states']['work']['assistance_request']['status'] == 'completed'
    assert len(after['node_states']['work']['assistance_receipts']) == 1
    assert unstarted_assistance_decision(after, 'work') is None
    assert run_assist_count(after) == 1
    if legacy_reservation:
        assert run_assist_count(before) == run_assist_count(after)
        assert all(fact in after['node_ledgers']['work']['facts'] for fact in before['node_ledgers']['work']['facts'])
    for key in ('locked_flow', 'locked_models', 'role_models'):
        assert digest(after.get(key)) == digest(before.get(key))
    assert after['collaboration']['contracts'] == before['collaboration']['contracts']
