import pytest

from mewcode.teams.artifact_publication import ArtifactPublication
from mewcode.teams.store_v2 import TeamStateError
from test_team_runtime_v2 import runtime_case, submit_candidate, review_candidate


def prepared(case, root):
    _, runtime, owner, worker, active, schema = case
    _, _, result = submit_candidate(runtime, worker, active['task_ids']['produce'], schema)
    review_candidate(runtime, owner, result)
    publisher = ArtifactPublication(runtime, workspace=root)
    entries = publisher.preview(owner, active['team_run_id'], [dict(relative_path='result.json', version_id=result['artifacts'][0]['version_id'])])
    entries = [{k: v for k, v in item.items() if k != 'content_hash'} for item in entries]
    journal = publisher.prepare(owner, active['team_run_id'], entries, control_epoch=1, command_id='prepare')
    return publisher, owner, journal['publication_id']


def test_explicit_reviewed_publish(runtime_case, tmp_path):
    publisher, owner, journal = prepared(runtime_case, tmp_path)
    assert not (tmp_path / 'result.json').exists()
    assert publisher.commit(owner, journal, control_epoch=1)['state'] == 'committed'
    assert (tmp_path / 'result.json').read_bytes() == b'{"result":2}'


def test_third_party_edit_is_never_overwritten(runtime_case, tmp_path):
    publisher, owner, journal = prepared(runtime_case, tmp_path)
    (tmp_path / 'result.json').write_text('user edit')
    with pytest.raises(TeamStateError, match='VERSION_CONFLICT'):
        publisher.commit(owner, journal, control_epoch=1)
    with pytest.raises(TeamStateError, match='VERSION_CONFLICT'):
        publisher.reconcile(owner, journal, control_epoch=1, action='rollback')
    assert (tmp_path / 'result.json').read_text() == 'user edit'


@pytest.mark.parametrize('action', ['finish', 'rollback'])
def test_crash_after_replace_is_recoverable(runtime_case, tmp_path, action):
    publisher, owner, journal = prepared(runtime_case, tmp_path)
    def crash(phase, index):
        if phase == 'after_replace':
            raise KeyboardInterrupt('simulated process loss')
    publisher.fault_hook = crash
    with pytest.raises(KeyboardInterrupt):
        publisher.commit(owner, journal, control_epoch=1)
    assert publisher.reconcile(owner, journal, control_epoch=1, action=action)['state'] == ('committed' if action == 'finish' else 'rolled_back')
    assert (tmp_path / 'result.json').exists() == (action == 'finish')
    if action == 'rollback':
        assert len(list(tmp_path.glob('*.team-unmerged-*'))) == 1


@pytest.mark.parametrize('path', ['../escape', '/absolute', 'NUL.txt', '.git/config', 'file:stream', 'dir/../x', 'dir\\x', 'x.'])
def test_unsafe_paths_rejected(runtime_case, tmp_path, path):
    publisher = ArtifactPublication(runtime_case[1], workspace=tmp_path)
    with pytest.raises(TeamStateError, match='UNSAFE_PATH'):
        publisher._target(path)


def test_producer_revision_change_between_intent_and_replace(runtime_case, tmp_path):
    publisher, owner, journal = prepared(runtime_case, tmp_path)
    runtime = runtime_case[1]
    def change(phase, index):
        if phase == 'before_replace':
            task = runtime.task(owner, runtime_case[4]['task_ids']['produce'])
            runtime.revise(owner, task['task_id'], inputs={}, reason='Explicit new revision', approved=True,
                           expected_version=task['state_version'], control_epoch=1, command_id='revise')
    publisher.fault_hook = change
    with pytest.raises(TeamStateError, match='VERSION_CONFLICT'):
        publisher.commit(owner, journal, control_epoch=1)
    assert not (tmp_path / 'result.json').exists()


def test_cancellation_between_intent_and_replace_blocks_publish(runtime_case, tmp_path):
    publisher, owner, journal = prepared(runtime_case, tmp_path)
    def cancel(phase, index):
        if phase == 'before_replace':
            runtime_case[1].cancel_run(owner, runtime_case[4]['team_run_id'], control_epoch=1, reason='Explicit cancellation', command_id='cancel')
    publisher.fault_hook = cancel
    with pytest.raises(TeamStateError, match='RUN_NOT_ACTIVE'):
        publisher.commit(owner, journal, control_epoch=1)
    assert not (tmp_path / 'result.json').exists()
    assert publisher.reconcile(owner, journal, control_epoch=1, action='rollback')['state'] == 'rolled_back'


def test_same_bytes_do_not_bypass_head_generation(runtime_case, tmp_path):
    publisher, owner, first = prepared(runtime_case, tmp_path)
    publisher.commit(owner, first, control_epoch=1)
    _, journal = publisher._journal(owner, first)
    seed = [dict(relative_path='result.json', version_id=journal['entries'][0]['version_id'])]
    preview = publisher.preview(owner, runtime_case[4]['team_run_id'], seed)
    entries = [{k: v for k, v in item.items() if k != 'content_hash'} for item in preview]
    second = publisher.prepare(owner, runtime_case[4]['team_run_id'], entries, control_epoch=1, command_id='same-bytes')
    publisher.commit(owner, second['publication_id'], control_epoch=1)
    with pytest.raises(TeamStateError, match='HEAD_CONFLICT'):
        publisher.prepare(owner, runtime_case[4]['team_run_id'], entries, control_epoch=1, command_id='stale-preview')


@pytest.mark.parametrize('phase,index', [('prepared', -1), ('before_replace', 0), ('after_replace', 0), ('before_replace', 1), ('after_replace', 1)])
def test_each_journal_step_preserves_before_bytes(runtime_case, tmp_path, phase, index):
    publisher, owner, first = prepared(runtime_case, tmp_path)
    publisher.reconcile(owner, first, control_epoch=1, action='rollback')
    _, original = publisher._journal(owner, first)
    version = original['entries'][0]['version_id']
    (tmp_path / 'a.txt').write_bytes(b'original a')
    (tmp_path / 'b.txt').write_bytes(b'original b')
    entries = publisher.preview(owner, runtime_case[4]['team_run_id'], [dict(relative_path=name, version_id=version) for name in ('a.txt', 'b.txt')])
    entries = [{k: v for k, v in item.items() if k != 'content_hash'} for item in entries]
    prepared_result = publisher.prepare(owner, runtime_case[4]['team_run_id'], entries, control_epoch=1, command_id='two-files')
    def crash(at_phase, at_index):
        if (at_phase, at_index) == (phase, index):
            raise KeyboardInterrupt('crash fixture')
    publisher.fault_hook = crash
    with pytest.raises(KeyboardInterrupt):
        publisher.commit(owner, prepared_result['publication_id'], control_epoch=1)
    restarted = ArtifactPublication(runtime_case[1], workspace=tmp_path)
    assert restarted.reconcile(owner, prepared_result['publication_id'], control_epoch=1, action='rollback')['state'] == 'rolled_back'
    assert (tmp_path / 'a.txt').read_bytes() == b'original a' and (tmp_path / 'b.txt').read_bytes() == b'original b'


def test_case_alias_and_hardlink_rejected(runtime_case, tmp_path):
    publisher = ArtifactPublication(runtime_case[1], workspace=tmp_path)
    (tmp_path / 'Config.txt').write_bytes(b'original')
    with pytest.raises(TeamStateError, match='PATH_ALIAS_CONFLICT'):
        publisher._target('config.txt')
    (tmp_path / 'alias.txt').hardlink_to(tmp_path / 'Config.txt')
    with pytest.raises(TeamStateError, match='UNSAFE_PATH'):
        publisher._target('alias.txt')


def test_windows_junction_cannot_escape(runtime_case, tmp_path):
    import os
    import subprocess
    publisher = ArtifactPublication(runtime_case[1], workspace=tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = tmp_path / 'linked'
    if os.name == 'nt':
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], capture_output=True, check=False)
        assert result.returncode == 0
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(TeamStateError, match='UNSAFE_PATH'):
        publisher._target('linked/result.txt')
