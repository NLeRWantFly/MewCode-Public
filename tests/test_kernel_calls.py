import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from mewcode.agent import Agent
from mewcode.expert_runs import ExpertRunStore
from mewcode.kernel.calls import CallGateway, invocation_id, propose, transition
from mewcode.kernel.blockers import Blocker, policy
from mewcode.tools.base import ToolResult
from mewcode.tools.submit_handoff import SubmitHandoffParams


def test_lifecycle_rejects_missing_dispatch_and_impossible_transitions():
    row = propose({}, 'N03', 'input-1', {'command': 'fixture'})
    with pytest.raises(ValueError, match='illegal'):
        transition(row, 'unknown')
    transition(row, 'reviewed')
    with pytest.raises(ValueError, match='acceptance'):
        transition(row, 'dispatched')
    transition(row, 'dispatched', evidence={'executor_accepted': True})
    transition(row, 'unknown')
    with pytest.raises(ValueError, match='evidence'):
        transition(row, 'not_started')
    transition(row, 'succeeded', evidence={'host_verified': True, 'probe': 'independent receipt'})
    with pytest.raises(ValueError, match='illegal'):
        transition(row, 'dispatched')


def test_identity_is_canonical_and_unknown_effect_survives_changed_input():
    assert invocation_id('N03', 'v1', {'a': 1, 'b': 2}) == invocation_id('N03', 'v1', {'b': 2, 'a': 1})
    record = {}
    row = propose(record, 'N03', 'v1', {'command': 'fixture'})
    transition(row, 'reviewed')
    transition(row, 'dispatched', evidence={'executor_accepted': True})
    assert propose(record, 'N03', 'v2', {'command': 'fixture'}) is row


def test_one_closed_registry_covers_protocols_and_legacy_aliases():
    assert policy('effect_unknown') == policy('unknown_effect')
    assert policy('permission_required') == policy('permission_denied') == policy('permission')
    assert policy('safety_review_format').owner == 'reviewer'
    assert SubmitHandoffParams(summary='need permission', status='blocked', blocker_kind='permission').blocker_kind is Blocker.PERMISSION
    with pytest.raises(ValidationError):
        SubmitHandoffParams(summary='invalid', blocker_kind='invented_kind')


def test_actual_tool_gateway_reuses_durable_result_after_restart(tmp_path):
    run_id = 'a' * 32
    store = ExpertRunStore(tmp_path / 'runs')
    store.create(run_id=run_id, conversation_id='fixture', task='task', attachments=[], profile={}, strategy='stable')
    calls = []
    async def run(params, **kwargs):
        calls.append(params)
        return ToolResult('saved once')
    tool = SimpleNamespace(category='write', run=run)
    tc = SimpleNamespace(tool_name='WriteFile', arguments={'file_path': 'fixture.txt'}, tool_id='tool-1')
    def agent(active_store):
        value = Agent.__new__(Agent)
        value._safety_call_ref = lambda *args: 'test'
        value._prepare_rollback_evidence = lambda *args: None
        value._review_recovery = SimpleNamespace(begin_effect=lambda *args: {}, finish_effect=lambda *args: None)
        value.work_dir = str(tmp_path)
        value.permission_checker = None
        value.kernel_gateway = CallGateway(active_store, run_id, 'N03', 'v1')
        return value
    first = asyncio.run(agent(store)._run_recorded_tool(tool, {}, tc))
    second = asyncio.run(agent(ExpertRunStore(tmp_path / 'runs'))._run_recorded_tool(tool, {}, tc))
    assert first.output == 'saved once'
    assert second.output.startswith('KERNEL_RECEIPT_REUSED:') and second.output.endswith('saved once')
    assert second.host_observation['reused'] is True
    assert len(calls) == 1
    row = next(iter(store.load(run_id)['kernel_calls'].values()))
    assert row['history'] == ['proposed', 'reviewed', 'dispatched', 'succeeded']
