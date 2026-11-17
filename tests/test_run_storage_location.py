import json
import os
import subprocess

import pytest

from mewcode.expert_runs import ExpertRunStore
from mewcode.kernel.events import EventJournal
from mewcode.orchestration.write_lease import WriteLeaseError
from mewcode.runtime_paths import expert_runs_root
from mewcode.sandbox.windows import NativeBoundaryUnavailable


def configure(profile, destination):
    profile.mkdir(exist_ok=True)
    (profile / 'storage-paths.json').write_text(json.dumps({
        'schema_version': 1, 'expert_runs': str(destination),
    }), encoding='utf-8')


def test_default_storage_remains_in_selected_profile(tmp_path, monkeypatch):
    monkeypatch.setenv('MEWCODE_USER_DATA_DIR', str(tmp_path))
    assert expert_runs_root() == tmp_path / 'expert-runs-v1'
    assert ExpertRunStore(root='').root == tmp_path / 'expert-runs-v1'


def test_explicit_storage_reopens_existing_journal_without_changing_identity(tmp_path, monkeypatch):
    profile, target = tmp_path / 'profile', tmp_path / 'other-volume'
    target.mkdir()
    identity = 'a' * 32
    EventJournal(target, identity).commit({'run_id': identity, 'status': 'paused', 'attempt': 51})
    configure(profile, target)
    monkeypatch.setenv('MEWCODE_USER_DATA_DIR', str(profile))
    store = ExpertRunStore()
    assert store.root == target
    assert EventJournal(store.root, identity).load()['attempt'] == 51
    assert not (profile / 'expert-runs-v1').exists()


@pytest.mark.parametrize('raw', ['{broken', '{}', '{"schema_version":true,"expert_runs":"x"}',
                                '{"schema_version":1,"expert_runs":"relative/path"}'])
def test_invalid_storage_does_not_silently_start_an_empty_store(tmp_path, raw):
    (tmp_path / 'storage-paths.json').write_text(raw, encoding='utf-8')
    with pytest.raises(ValueError):
        expert_runs_root(tmp_path)
    assert not (tmp_path / 'expert-runs-v1').exists()


def test_missing_destination_does_not_recreate_migrated_history(tmp_path):
    configure(tmp_path, tmp_path / 'missing')
    with pytest.raises(FileNotFoundError):
        expert_runs_root(tmp_path)
    assert not (tmp_path / 'missing').exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows directory junction contract')
def test_compatibility_junction_is_not_used_for_writer_leases(tmp_path):
    profile, target = tmp_path / 'profile', tmp_path / 'other-volume'
    profile.mkdir()
    target.mkdir()
    alias = profile / 'expert-runs-v1'
    def quote(path):
        return "'" + str(path).replace("'", "''") + "'"
    subprocess.run(['powershell.exe', '-NoProfile', '-Command',
        'New-Item -ItemType Junction -Path ' + quote(alias) + ' -Target ' + quote(target) + ' | Out-Null'],
        check=True, capture_output=True)
    try:
        with pytest.raises(WriteLeaseError, match='reparse'):
            EventJournal(alias, 'a' * 32).commit({'status': 'paused'})
        configure(profile, target)
        journal = EventJournal(expert_runs_root(profile), 'a' * 32)
        journal.commit({'status': 'paused', 'attempt': 51})
        assert journal.load()['attempt'] == 51
        configure(profile, alias)
        with pytest.raises(NativeBoundaryUnavailable, match='reparse'):
            expert_runs_root(profile)
    finally:
        alias.rmdir()
