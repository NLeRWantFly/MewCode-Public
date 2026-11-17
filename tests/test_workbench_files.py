import copy
import hashlib

import pytest

from frontend.workbench_files import collaborative_file
from mewcode.kernel.acceptance import Acceptance
from mewcode.kernel.events import digest


def fixture_file(tmp_path):
    (tmp_path / 'delivery.txt').write_text('original delivery', encoding='utf-8')
    artifacts = Acceptance(tmp_path).freeze({'artifacts': [{'path': 'delivery.txt'}]})
    record = {'collaboration': {'attempts': {'one': {'id': 'one', 'report': {'artifacts': artifacts}}}}}
    return record, 'collab:' + digest(['one', 0, artifacts[0]['sha256']])


def test_delivery_reads_frozen_bytes_not_changed_source(tmp_path):
    record, identity = fixture_file(tmp_path)
    before = copy.deepcopy(record)
    (tmp_path / 'delivery.txt').write_text('changed source', encoding='utf-8')
    result = collaborative_file(record, identity, tmp_path)
    assert result['preview'] == 'original delivery'
    assert result['sha256'] == hashlib.sha256(b'original delivery').hexdigest()
    assert result['content_b64'] and record == before
    assert collaborative_file(record, 'collab:unreferenced', tmp_path) is None


def test_corruption_and_path_escape_never_return_other_bytes(tmp_path):
    record, identity = fixture_file(tmp_path)
    artifact = record['collaboration']['attempts']['one']['report']['artifacts'][0]
    frozen = tmp_path / artifact['snapshot_path']
    frozen.write_text('corrupt')
    with pytest.raises(ValueError, match='已损坏'):
        collaborative_file(record, identity, tmp_path)
    artifact['snapshot_path'] = '../secret.txt'
    with pytest.raises(ValueError, match='不属于'):
        collaborative_file(record, identity, tmp_path)


def test_large_file_is_bounded_and_unreferenced_hash_is_not_read(tmp_path, monkeypatch):
    record, identity = fixture_file(tmp_path)
    monkeypatch.setattr('frontend.workbench_files.MAX_FILE_BYTES', 3)
    with pytest.raises(ValueError, match='超过预览上限'):
        collaborative_file(record, identity, tmp_path)
    assert collaborative_file({'collaboration': {'attempts': {}}}, identity, tmp_path) is None
