import hashlib
import json
import multiprocessing
from dataclasses import replace

import pytest

from mewcode.experts.probe_scope import CandidateWriter, ProbeFiles, normalize_scope, trial_registry
from mewcode.experts.replan_ledger import ReplanLedger, LedgerConflict
from mewcode.tools import ToolRegistry
from mewcode.tools.write_file import WriteFile, Params
from mewcode.tools.read_file import ReadFile
from mewcode.tools.bash import Bash
from tests.test_adaptive_replan import setup


def _claim_in_process(root, owner, path, result):
    try:
        ProbeFiles(ReplanLedger(root)).claim('parallel', owner, [path])
        result.put('claimed')
    except LedgerConflict:
        result.put('conflict')


def claimed(tmp_path, *, run='run', episode='e', name='candidate.txt'):
    root = tmp_path / 'candidates'
    root.mkdir(exist_ok=True)
    ledger = ReplanLedger(tmp_path)
    ledger.claim(run, {'episode_id': episode, 'node_id': episode, 'status': 'consulting', 'reserved_tokens': 100, 'context': {}})
    ledger.transition(run, episode, expected_revision=1, status='proposed')
    ledger.transition(run, episode, expected_revision=2, status='probing')
    ledger.transition(run, episode, expected_revision=3, status='probing', dispatch_started=True)
    files = ProbeFiles(ledger)
    paths = normalize_scope(tmp_path, 'candidates', ['candidates/' + name])
    files.claim(run, episode, paths)
    writer = CandidateWriter(files, run, episode, 4, str(tmp_path), 'candidates', paths)
    return ledger, files, writer, paths[0]


@pytest.mark.asyncio
async def test_exclusive_create_receipt_and_idempotent_repeat(tmp_path):
    ledger, files, writer, path = claimed(tmp_path)
    params = Params(file_path='candidates/candidate.txt', content='候选内容')
    first = await writer.execute(params)
    assert not first.is_error
    receipt = json.loads(first.output)
    assert receipt['sha256'] == hashlib.sha256(params.content.encode()).hexdigest()
    assert receipt['business_acceptance'] is False
    assert (await writer.execute(params)).output == first.output
    assert (await writer.execute(Params(file_path=params.file_path, content='changed'))).is_error
    ledger.transition('run', 'e', expected_revision=4, status='rejected')
    assert (await writer.execute(params)).is_error  # Even equal bytes cannot bypass a revoked fence.


@pytest.mark.parametrize('path', ['../outside.txt', 'candidates/../outside.txt', 'candidates/x:stream',
    'candidates/NUL', 'candidates/a. ', 'C:/outside.txt', '//server/share/file', 'candidates//x',
    'candidates/COM¹', 'candidates/CONOUT$', 'candidates/con .txt', 'candidates/x*y'])
def test_path_escapes_and_windows_aliases_rejected(tmp_path, path):
    (tmp_path / 'candidates').mkdir()
    with pytest.raises(ValueError):
        normalize_scope(tmp_path, 'candidates', [path])


def test_existing_work_and_unauthorized_root_preserved(tmp_path):
    (tmp_path / 'candidates').mkdir()
    existing = tmp_path / 'candidates' / 'existing.txt'
    existing.write_text('original')
    with pytest.raises(ValueError):
        normalize_scope(tmp_path, 'candidates', ['candidates/existing.txt'])
    with pytest.raises(ValueError):
        normalize_scope(tmp_path, '', ['candidates/new.txt'])
    assert existing.read_text() == 'original'


def test_overlapping_trial_claims_block_but_independent_paths_work(tmp_path):
    ledger, files, _, path = claimed(tmp_path)
    ledger.claim('other', {'episode_id': 'other', 'node_id': 'n', 'status': 'consulting', 'reserved_tokens': 1})
    with pytest.raises(LedgerConflict, match='overlapping'):
        ProbeFiles(ReplanLedger(tmp_path)).claim('other', 'other', [path])
    files.claim('other', 'other', [str(tmp_path / 'candidates' / 'different.txt')])
    ledger.transition('run', 'e', expected_revision=4, status='rejected')
    files.claim('other', 'other', [path])  # An unused terminal claim is not a permanent lock.


def test_independent_processes_cannot_claim_same_candidate(tmp_path):
    ledger = ReplanLedger(tmp_path)
    ProbeFiles(ledger)
    for owner in ('one', 'two'):
        ledger.claim('parallel', {'episode_id': owner, 'node_id': owner, 'status': 'consulting', 'reserved_tokens': 1})
    ctx = multiprocessing.get_context('spawn')
    results = ctx.Queue()
    processes = [ctx.Process(target=_claim_in_process,
        args=(str(tmp_path), owner, str(tmp_path / 'candidate.txt'), results)) for owner in ('one', 'two')]
    for process in processes:
        process.start()
    try:
        assert sorted(results.get(timeout=25) for _ in processes) == ['claimed', 'conflict']
    finally:
        for process in processes:
            process.join(timeout=25)
            if process.is_alive():
                process.terminate()
                process.join()
        results.close()
    assert all(process.exitcode == 0 for process in processes)


def test_reparse_point_is_rejected_before_trial_dispatch(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    folder = tmp_path / 'candidates'
    folder.mkdir()
    original = Path.stat
    def stat(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if path == folder:
            return SimpleNamespace(**{key: getattr(value, key) for key in dir(value)
                if key.startswith('st_') and key != 'st_file_attributes'}, st_file_attributes=0x400)
        return value
    monkeypatch.setattr(Path, 'stat', stat)
    with pytest.raises(ValueError, match='reparse'):
        normalize_scope(tmp_path, 'candidates', ['candidates/new.txt'])


@pytest.mark.asyncio
async def test_new_external_file_between_preflight_and_write_is_not_overwritten(tmp_path):
    from pathlib import Path
    _, _, writer, path = claimed(tmp_path)
    Path(path).write_text('another writer')
    assert (await writer.execute(Params(file_path='candidates/candidate.txt', content='mine'))).is_error
    assert Path(path).read_text() == 'another writer'


@pytest.mark.asyncio
async def test_changed_binding_stops_before_write_intent(tmp_path):
    from pathlib import Path
    _, files, writer, path = claimed(tmp_path)
    writer.binding_valid = lambda: False
    result = await writer.execute(Params(file_path='candidates/candidate.txt', content='not written'))
    assert result.is_error and 'revision changed' in result.output
    assert files.receipts('run', 'e')[path] is None
    assert not Path(path).exists()


def test_registry_removes_shell_delegation_and_discovery_without_granting_writes():
    registry = ToolRegistry()
    registry.register(ReadFile())
    registry.register(Bash())
    registry.register(WriteFile())
    isolated = trial_registry(registry)
    assert isolated.is_enabled('ReadFile')
    assert isolated.get('Bash') is None and isolated.get('WriteFile') is None
    assert registry.is_enabled('Bash')


@pytest.mark.asyncio
async def test_lost_write_receipt_is_not_replayed(tmp_path, monkeypatch):
    _, files, writer, path = claimed(tmp_path)
    # Inject failure after bytes are written but before the completion receipt.
    monkeypatch.setattr('mewcode.experts.probe_scope.os.fsync', lambda fd: (_ for _ in ()).throw(OSError('synthetic crash')))
    params = Params(file_path='candidates/candidate.txt', content='retained failed candidate')
    assert (await writer.execute(params)).is_error
    assert files.receipts('run', 'e')[path]['status'] == 'writing'
    assert (await writer.execute(params)).is_error
    assert open(path, encoding='utf-8').read() == params.content


@pytest.mark.asyncio
async def test_exact_bytes_recover_receipt_without_rewriting_candidate(tmp_path, monkeypatch):
    _, files, writer, path = claimed(tmp_path)
    params = Params(file_path='candidates/candidate.txt', content='durable bytes')
    original_sync = __import__('os').fsync
    def crash_after_sync(fd):
        original_sync(fd)
        raise OSError('receipt was not saved')
    monkeypatch.setattr('mewcode.experts.probe_scope.os.fsync', crash_after_sync)
    assert (await writer.execute(params)).is_error
    monkeypatch.setattr('mewcode.experts.probe_scope.os.fsync', original_sync)
    import builtins
    original_open = builtins.open
    modes = []
    def track_open(path, mode='r', *args, **kwargs):
        modes.append(mode)
        assert mode == 'r+b', 'recovery must not create or truncate files'
        return original_open(path, mode, *args, **kwargs)
    monkeypatch.setattr('builtins.open', track_open)
    receipts = ProbeFiles(ReplanLedger(tmp_path)).reconcile_written('run', 'e', 4)
    assert receipts[path]['status'] == 'written' and receipts[path]['recovered_from_file']
    assert modes == ['r+b']


@pytest.mark.asyncio
async def test_changed_or_missing_candidate_never_gets_recovered_receipt(tmp_path, monkeypatch):
    _, files, writer, path = claimed(tmp_path)
    monkeypatch.setattr('mewcode.experts.probe_scope.os.fsync', lambda _: (_ for _ in ()).throw(OSError('crash')))
    await writer.execute(Params(file_path='candidates/candidate.txt', content='intended'))
    from pathlib import Path
    Path(path).write_text('changed externally')
    assert files.reconcile_written('run', 'e', 4)[path]['status'] == 'writing'


@pytest.mark.asyncio
async def test_coordinator_validates_candidate_bytes_not_business_acceptance(tmp_path, monkeypatch):
    handler, _, store, primary, _, coordinator, node, policy, _ = setup(tmp_path, monkeypatch)
    (tmp_path / 'candidates').mkdir()
    coordinator.project_dir = tmp_path
    store.mutate(handler.run_id, lambda s: s['locked_flow'].setdefault('meta', {}).update(
        adaptive_replan={'candidate_root': 'candidates'}))
    policy = replace(policy, node_tools=('ReadFile', 'WriteFile'))
    # Contract hash is node-local; flow generation is captured by the coordinator.
    primary.value['candidate_paths'] = ['candidates/candidate.txt']
    result = await coordinator.prepare(node=node, policy=policy, error_kind='max_iterations', charge=lambda n: None)
    from types import SimpleNamespace
    registry = ToolRegistry()
    registry.register(WriteFile())
    registry.register(Bash())
    agent = SimpleNamespace(registry=registry)
    restore = coordinator.bind_trial(agent, result['episode_id'])
    assert agent.registry.get('Bash') is None
    assert not (await agent.registry.get('WriteFile').execute(Params(
        file_path='candidates/candidate.txt', content='new candidate'))).is_error
    receipt = coordinator.finish_probe(result['episode_id'])
    assert receipt['status'] == 'candidate_ready' and receipt['verification_status'] == 'candidate_integrity_only'
    assert receipt['business_acceptance'] is False
    restore()
    assert agent.registry is registry
