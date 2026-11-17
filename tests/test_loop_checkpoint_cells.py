import copy
import pytest

from mewcode.expert_checkpoints import ExpertCheckpointArchive
from mewcode.kernel.events import EventJournal
from mewcode.kernel.sqlite_state import RunDatabase,pointer


def setup(tmp_path):
    run='f'*32
    state={'run_id':run,'conversation_id':'c','status':'paused','active_node_ids':[], 'attempt':2,'last_seq':7,
        'loop_states':{'pages':{'tokens':735,'round':1,'contract_hash':'fixed','segment_budget_receipts':{'a':[{'time':1},{'time':2}]}}}}
    EventJournal(tmp_path,run,storage='sqlite').commit(state)
    archive=ExpertCheckpointArchive(tmp_path/'archives')
    point=archive.save(state,turn_key='paused:2')
    db=RunDatabase(tmp_path,run)
    with db.connection(write=True) as conn:
        for path in (('loop_states','pages','segment_budget_receipts','a'),
                     ('loop_states','pages','segment_budget_receipts','a',0)):
            conn.execute('DELETE FROM cells WHERE run_id=? AND space=? AND path=?',(run,'state',pointer(path)))
    return state,db,archive,point


def test_exact_pause_archive_repairs_missing_cells_without_changing_values(tmp_path):
    state,db,archive,point=setup(tmp_path)
    with pytest.raises(ValueError,match='parent'):db.load()
    repaired=db.repair_loop_checkpoint(archive,point['checkpoint_id'],expected_revision=state['kernel_revision'])
    assert repaired['missing_cells']==2
    actual=db.load();actual.pop('kernel_revision');state.pop('kernel_revision')
    assert actual==state
    assert not db.repair_loop_checkpoint(archive,point['checkpoint_id'],expected_revision=repaired['revision'])['repaired']


def test_repair_rejects_newer_or_different_existing_progress(tmp_path):
    state,db,archive,point=setup(tmp_path)
    with db.connection(write=True) as conn:
        db._set(conn,'state',('loop_states','pages','tokens'),999)
    with pytest.raises(ValueError,match='不一致'):
        db.repair_loop_checkpoint(archive,point['checkpoint_id'],expected_revision=state['kernel_revision'])


@pytest.mark.parametrize('mismatch', [False, True])
def test_mirror_receipts_repaired_without_overwriting_clock_or_metadata(tmp_path, mismatch):
    state, db, archive, point = setup(tmp_path)
    loop = copy.deepcopy(state['loop_states']['pages'])
    loop.update(elapsed=99, active_at=123)
    with db.connection(write=True) as conn:
        db._set(conn, 'state', ('flow_state',), {'loop_state_key':'pages', 'node_id':'b', 'loop_state':loop})
        prefix = ('flow_state', 'loop_state', 'segment_budget_receipts', 'a')
        conn.execute('DELETE FROM cells WHERE run_id=? AND space=? AND path=?',
                     (db.run_id, 'state', pointer(prefix)))
        if mismatch:
            db._set(conn, 'state', prefix + (1, 'time'), 999)
    if mismatch:
        with pytest.raises(ValueError, match='不一致'):
            db.repair_loop_checkpoint(archive, point['checkpoint_id'], expected_revision=state['kernel_revision'])
        with pytest.raises(ValueError, match='parent'): db.load()
    else:
        result = db.repair_loop_checkpoint(archive, point['checkpoint_id'], expected_revision=state['kernel_revision'])
        assert result['missing_cells'] == 3
        assert db.load()['flow_state'] == {'loop_state_key':'pages', 'node_id':'b', 'loop_state':loop}
