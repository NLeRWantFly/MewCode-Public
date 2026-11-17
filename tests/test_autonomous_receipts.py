"""Lost business receipts recover through the real gateway and host probe."""
from types import SimpleNamespace

import pytest

from mewcode.agent import Agent
from mewcode.expert_runs import ExpertRunStore
from mewcode.kernel.calls import CallGateway
from mewcode.tools.write_file import Params, WriteFile


@pytest.mark.asyncio
@pytest.mark.parametrize('queryable', [True, False])
async def test_effect_before_lost_receipt_is_never_dispatched_twice(tmp_path, monkeypatch, queryable):
    run_id = 'a' * 32
    store = ExpertRunStore(tmp_path / 'runs')
    store.create(run_id=run_id, conversation_id='synthetic', task='one effect', attachments=[],
                 profile={}, strategy='stable')
    calls = []
    target = tmp_path / 'effect.txt'
    async def business(params, **kwargs):
        calls.append(True)
        return await WriteFile().run(Params(file_path=str(target), content='applied'), **kwargs)
    tool = SimpleNamespace(category='write', run=business)
    tc = SimpleNamespace(tool_name='WriteFile' if queryable else 'Bash', tool_id='dispatch-1',
        arguments={'file_path': str(target), 'content': 'applied'} if queryable else {'command': 'synthetic-external-effect'})
    def agent(active):
        value = Agent.__new__(Agent)
        value._safety_call_ref = lambda *args: 'fixture'
        value._prepare_rollback_evidence = lambda *args: None
        value._review_recovery = SimpleNamespace(begin_effect=lambda *args: {}, finish_effect=lambda *args: None)
        value.work_dir, value.permission_checker = str(tmp_path), None
        value.kernel_gateway = CallGateway(active, run_id, 'work', 'fixed-input')
        return value
    first = agent(store)
    update = first.kernel_gateway.update
    def lose_receipt(call_id, state, **kwargs):
        if state == 'succeeded':
            raise OSError('connection lost after business effect before receipt commit')
        return update(call_id, state, **kwargs)
    monkeypatch.setattr(first.kernel_gateway, 'update', lose_receipt)
    with pytest.raises(OSError, match='before receipt commit'):
        await first._run_recorded_tool(tool, {}, tc)
    assert target.read_text() == 'applied'
    recovered = ExpertRunStore(tmp_path / 'runs')
    assert next(iter(recovered.load(run_id)['kernel_calls'].values()))['state'] == 'dispatched'
    result = await agent(recovered)._run_recorded_tool(tool, {}, tc)
    row = next(iter(recovered.load(run_id)['kernel_calls'].values()))
    assert len(calls) == 1
    if queryable:
        assert row['state'] == 'succeeded' and not result.is_error
        assert row['reconciliation_checks'][0]['readonly'] is True
        assert row['history'] == ['proposed', 'reviewed', 'dispatched', 'unknown', 'succeeded']
    else:
        assert row['state'] == 'dispatched' and result.is_error
        assert 'UNKNOWN_EFFECT_RECONCILIATION_REQUIRED' in result.output
    await agent(ExpertRunStore(tmp_path / 'runs'))._run_recorded_tool(tool, {}, tc)
    assert len(calls) == 1
