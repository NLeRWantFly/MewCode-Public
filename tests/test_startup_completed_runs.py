from unittest.mock import Mock

from mewcode.expert_runs import ExpertRunStore
from mewcode.workflow_core.state import fresh


def test_terminal_sqlite_runs_do_not_hydrate_history_during_startup(tmp_path, monkeypatch):
    store = ExpertRunStore(tmp_path, storage='sqlite')
    for rid, status in [('a' * 32, 'completed'), ('b' * 32, 'terminated'), ('d' * 32, 'paused')]:
        store.create(run_id=rid, conversation_id='chat', task='old', attachments=[],
                     profile={}, strategy='stable')
        store.mutate(rid, lambda record: record.update(status=status, last_checkpoint={'checkpoint_id': 'saved'}), durable=True)
    store.mutate('d' * 32, lambda record: record.update(
        workflow_core=dict(fresh({'nodes': [], 'edges': []}), status='paused'),
        node_ledgers={'historical': {}}), durable=True)
    disk = Mock(side_effect=AssertionError('terminal history should not be hydrated'))
    monkeypatch.setattr(store, '_read_disk', disk)
    assert store.recover_stale_runs() == 0
    assert store.backfill_latest_checkpoints() == 0
    disk.assert_not_called()


def test_pending_takeover_on_terminal_run_still_recovers(tmp_path):
    store = ExpertRunStore(tmp_path, storage='sqlite')
    rid = 'c' * 32
    store.create(run_id=rid, conversation_id='chat', task='old', attachments=[], profile={}, strategy='stable')
    store.mutate(rid, lambda record: record.update(status='terminated', interventions=[
        dict(kind='takeover', status='handling', id='interrupted')]), durable=True)
    assert store.recover_stale_runs() == 0
    assert store.load(rid)['interventions'][0]['status'] == 'interrupted'
