"""Recovery display reads must not copy unrelated execution histories."""
import copy

import pytest

from mewcode.experts.resume_advice import resume_advice
from mewcode.experts.recovery_case import save_case
from mewcode.orchestration.inbox import collect
from tests.test_resume_advice_backend import saved_record, discovery_case


class ExecutionHistory(list):
    def __deepcopy__(self, memo):
        raise AssertionError('display attempted to copy unrelated execution history')


@pytest.mark.parametrize('size', [10, 5000])
def test_no_case_advice_skips_history_and_does_not_initialize_source_ledger(size):
    history = ExecutionHistory({'sequence': i, 'messages': ['private']} for i in range(size))
    record = dict(run_id='run', conversation_id='chat', kernel_attempt_archives=history)
    assert resume_advice(record) == dict(run_id='run', conversation_id='chat', action='continue',
        reason='没有未决恢复案件，可以继续任务。', node_id='', node_label='', case_id='')
    assert set(record) == {'run_id', 'conversation_id', 'kernel_attempt_archives'}
    assert record['kernel_attempt_archives'] is history


@pytest.mark.parametrize('size', [10, 5000])
def test_empty_inbox_skips_history_and_does_not_initialize_source_ledger(size):
    record = dict(run_id='run', kernel_attempt_archives=ExecutionHistory(range(size)))
    assert collect(dict(id='task', run_id='run'), record=record) == []
    assert set(record) == {'run_id', 'kernel_attempt_archives'}


def test_active_case_inbox_keeps_binding_and_detaches_question_without_copying_messages():
    record = saved_record()
    case = discovery_case(record)
    case.update(status='awaiting_user', pending=dict(kind='business_decision',
        question='请选择', questions=[dict(name='decision', message='继续吗', options=['保留', '停止'])]))
    save_case(record, case)
    ledger_before = copy.deepcopy(record['node_ledgers'])
    record['node_states']['n_prep_plan']['messages'] = ExecutionHistory(range(5000))
    items = collect(dict(id='task', run_id=record['run_id']), record=record)
    selected = next(item for item in items if item['type'] == 'recovery_case')
    assert selected['source_id'] == case['case_id']
    assert selected['context']['epoch'] == case['epoch']
    assert selected['context']['input_revision'] == case['input_revision']
    selected['input_schema'][0]['options'].append('not stored')
    selected['context']['pending']['questions'][0]['message'] = 'not stored'
    assert record['node_ledgers'] == ledger_before
