import multiprocessing
from concurrent.futures import ThreadPoolExecutor

import pytest

from mewcode.experts.replan_ledger import LedgerConflict, ReplanLedger


def record(key='a', node='n'):
    return {'episode_id': key, 'node_id': node, 'status': 'consulting',
            'reserved_tokens': 3000, 'context': {'input_revision': 'r1'}}


def claim_from_process(root, result):
    try:
        ReplanLedger(root).claim('run', record())
        result.put('claimed')
    except LedgerConflict:
        result.put('conflict')


def test_processes_cannot_claim_same_request(tmp_path):
    ReplanLedger(tmp_path)
    ctx = multiprocessing.get_context('spawn')
    result = ctx.Queue()
    processes = [ctx.Process(target=claim_from_process, args=(str(tmp_path), result)) for _ in range(2)]
    for process in processes:
        process.start()
    try:
        assert sorted(result.get(timeout=25) for _ in processes) == ['claimed', 'conflict']
    finally:
        for process in processes:
            process.join(timeout=25)
            if process.is_alive():
                process.terminate()
                process.join()
        result.close()
    assert all(p.exitcode == 0 for p in processes)


def test_parallel_independent_nodes_and_run_limit(tmp_path):
    ledger = ReplanLedger(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda i: ReplanLedger(tmp_path).claim('run', record(str(i), str(i))), range(6)))
    assert len(results) == 6
    with pytest.raises(LedgerConflict, match='limit'):
        ledger.claim('run', record('next', 'next'))


def test_fencing_unknown_reservations_and_checkpoint_rewind(tmp_path):
    ledger = ReplanLedger(tmp_path)
    ledger.claim('run', record())
    ledger.transition('run', 'a', expected_revision=1, status='outcome_unknown', used_tokens=3000)
    reopened = ReplanLedger(tmp_path)
    reopened.import_legacy('run', {'a': record()})
    assert reopened.records('run')['a']['status'] == 'outcome_unknown'
    with pytest.raises(LedgerConflict, match='fencing'):
        reopened.transition('run', 'a', expected_revision=1, status='proposed')
    with pytest.raises(LedgerConflict, match='owns'):
        reopened.claim('run', record('b'))
    assert reopened.records('run')['a']['reserved_tokens'] == 3000


def test_trial_dispatch_is_single_use(tmp_path):
    ledger = ReplanLedger(tmp_path)
    ledger.claim('run', record())
    ledger.transition('run', 'a', expected_revision=1, status='proposed')
    ledger.transition('run', 'a', expected_revision=2, status='probing')
    ledger.transition('run', 'a', expected_revision=3, status='probing', dispatch_started=True)
    with pytest.raises(LedgerConflict, match='consumed'):
        ledger.transition('run', 'a', expected_revision=4, status='probing', dispatch_started=True)
    assert ledger.records('run')['a']['revision'] == 4


def test_cannot_rewrite_frozen_identity(tmp_path):
    ledger = ReplanLedger(tmp_path)
    ledger.claim('run', record())
    with pytest.raises(LedgerConflict):
        ledger.transition('run', 'a', expected_revision=1, status='proposed', reserved_tokens=0)
    assert ledger.records('run')['a']['status'] == 'consulting'
