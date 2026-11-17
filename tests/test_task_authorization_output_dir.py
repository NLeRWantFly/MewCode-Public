"""A task scope can name the output directory it is about to create."""
from pathlib import Path

import pytest

from mewcode.permissions.task_authorization import TaskAuthorization


def scope(read, write, **extra):
    return {'read_directories': [str(read)], 'write_directories': [str(write)],
            'network_hosts': [], 'allowed_tools': ['WriteFile'], 'allowed_behaviors': [],
            'ttl_seconds': 600, **extra}


def test_a_missing_output_directory_can_be_authorized(tmp_path):
    existing = tmp_path / 'src'
    existing.mkdir()
    target = tmp_path / 'kb' / '00_inventory'
    assert not target.exists()
    authorization = TaskAuthorization.from_user_scope(scope(existing, target), task_id='t1')
    assert str(target) in authorization.write_directories


def test_a_missing_read_directory_is_still_refused(tmp_path):
    existing = tmp_path / 'src'
    existing.mkdir()
    with pytest.raises(ValueError):
        TaskAuthorization.from_user_scope(scope(tmp_path / 'missing', existing), task_id='t1')


def test_a_write_directory_under_a_missing_parent_is_allowed(tmp_path):
    """The model may create the whole chain, so only an existing ancestor matters."""
    existing = tmp_path / 'src'
    existing.mkdir()
    target = tmp_path / 'nope' / 'kb'
    authorization = TaskAuthorization.from_user_scope(scope(existing, target), task_id='t1')
    assert str(target) in authorization.write_directories


def test_a_volume_root_stays_refused(tmp_path):
    existing = tmp_path / 'src'
    existing.mkdir()
    with pytest.raises(ValueError):
        TaskAuthorization.from_user_scope(scope(existing, Path(tmp_path.anchor)), task_id='t1')


def test_a_relative_write_directory_stays_refused(tmp_path):
    existing = tmp_path / 'src'
    existing.mkdir()
    with pytest.raises(ValueError):
        TaskAuthorization.from_user_scope(scope(existing, 'kb'), task_id='t1')


def test_the_authorized_directory_covers_writes_below_it(tmp_path):
    from types import SimpleNamespace
    existing = tmp_path / 'src'
    existing.mkdir()
    target = tmp_path / 'kb'
    authorization = TaskAuthorization.from_user_scope(scope(existing, target), task_id='t1')
    tool = SimpleNamespace(name='WriteFile', category='write')
    assert authorization.covers(tool, {'file_path': str(target / 'a' / 'b.json')}, task_id='t1')
    assert not authorization.covers(tool, {'file_path': str(tmp_path / 'other' / 'b.json')}, task_id='t1')
