"""A model boundary reads control facts without copying its run history."""
import copy

from mewcode.conversation import ConversationManager
from mewcode.orchestration.interventions import (
    deliver_interventions, enqueue_intervention, has_pending_interventions,
)
from tests.test_tool_performance import RUN, running_store


def test_turn_boundary_is_small_detached_and_read_only(tmp_path):
    store = running_store(tmp_path)
    def seed(row):
        enqueue_intervention(row, 'message', 'Check the evidence')
        row['node_states']['executor']['assistance_request'] = {'status': 'pending'}
        row['node_states']['executor']['adaptive_request'] = {'status': 'pending'}
    store.mutate(RUN, seed)
    class HistoricalContext(list):
        def __deepcopy__(self, memo):
            raise AssertionError('copied unrelated execution history')
    live = store._live[RUN]
    live['kernel_attempt_archives'] = HistoricalContext()
    revision = live['kernel_revision']
    for _ in range(3):
        boundary = store.read_turn_boundary(RUN, 'executor')
        assert boundary['assistance_request'] == {'status': 'pending'}
        assert boundary['adaptive_request'] == {'status': 'pending'}
        assert boundary['interventions'][0]['id'] == 'message'
        assert not boundary['authorizations']['entries'][0]['enforceable']
        boundary['interventions'][0]['text'] = 'mutated copy'
        boundary['assistance_request']['status'] = 'changed'
        boundary['adaptive_request']['status'] = 'changed'
    assert live['kernel_revision'] == revision
    assert live['interventions'][0]['text'] == 'Check the evidence'
    assert live['node_states']['executor']['assistance_request']['status'] == 'pending'
    assert live['node_states']['executor']['adaptive_request']['status'] == 'pending'


def test_mailbox_check_preserves_conversation_delivery_and_new_messages():
    state = {'status': 'running'}
    first, second = ConversationManager(), ConversationManager()
    assert not has_pending_interventions(state, first)
    enqueue_intervention(state, 'one', 'Check the first source')
    before = copy.deepcopy(state)
    assert has_pending_interventions(state, first)
    assert state == before
    assert deliver_interventions(state, first, node_id='a', node_attempt=1) == ['one']
    assert not has_pending_interventions(state, first)
    # A receipt alone cannot hide input from a fresh/restored conversation.
    assert has_pending_interventions(state, second)
    assert deliver_interventions(state, second, node_id='a', node_attempt=1) == ['one']
    assert len(state['interventions'][0]['delivered_to']) == 1
    enqueue_intervention(state, 'two', 'Check the next source')
    assert has_pending_interventions(state, first)
    assert deliver_interventions(state, first, node_id='a', node_attempt=1) == ['two']
    assert not has_pending_interventions(state, first)


def test_mailbox_check_ignores_takeover_but_keeps_legacy_statement_migration():
    state = {'status': 'running'}
    conversation = ConversationManager()
    enqueue_intervention(state, 'side', 'Separate task', kind='takeover')
    assert not has_pending_interventions(state, conversation)
    enqueue_intervention(state, 'legacy', 'Existing statement')
    deliver_interventions(state, conversation, node_id='a', node_attempt=1)
    state['interventions'][1].pop('statement_id')
    assert has_pending_interventions(state, conversation)
    assert deliver_interventions(state, conversation, node_id='a', node_attempt=1) == []
    assert not has_pending_interventions(state, conversation)
    assert all(not row['enforceable'] for row in state['authorizations']['entries'])
