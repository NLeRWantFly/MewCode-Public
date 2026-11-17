from collections import OrderedDict
import copy
import json
from pathlib import Path
import threading

import pytest

from mewcode.kernel import events


def cold_journal(tmp_path, monkeypatch):
    journal = events.EventJournal(tmp_path, 'e' * 32)
    value = {'status': 'paused', 'nodes': {'start': {'accepted': True}}, 'history': []}
    for number in range(1, 13):
        value['history'].append({'number': number, 'nested': [{'text': str(number)}]})
        journal.commit(value)
    monkeypatch.setattr(events, '_SCAN_CACHE', OrderedDict())
    return journal, value


def test_cold_replay_overlaps_file_reads_and_applies_in_sequence(tmp_path, monkeypatch):
    journal, expected = cold_journal(tmp_path, monkeypatch)
    barrier = threading.Barrier(4)
    fourth_done = threading.Event()
    completed = []
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path.parent == journal.directory and path.stem.isdigit():
            number = int(path.stem)
            barrier.wait(timeout=3)
            if number == 1:
                assert fourth_done.wait(timeout=3)
            text = original(path, *args, **kwargs)
            completed.append(number)
            if number == 4:
                fourth_done.set()
            return text
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read)
    actual = journal.load()
    assert completed.index(4) < completed.index(1)
    assert sorted(completed) == list(range(1, 13))
    assert actual == expected
    actual['history'][0]['nested'][0]['text'] = 'caller mutation'
    assert journal.load() == expected


@pytest.mark.parametrize('corruption', ['content', 'link', 'sequence', 'missing'])
def test_cold_replay_rejects_invalid_middle_event_without_caching_it(tmp_path, monkeypatch, corruption):
    journal, expected = cold_journal(tmp_path, monkeypatch)
    path = journal.directory / '0000000000000006.json'
    original = path.read_bytes()
    document = json.loads(original)
    if corruption == 'missing':
        path.unlink()
    else:
        if corruption == 'content':
            document['kind'] = 'tampered'
        elif corruption == 'link':
            document['previous_hash'] = 'f' * 64
            document['sha256'] = events.digest({key: item for key, item in document.items() if key != 'sha256'})
        else:
            document['sequence'] = 7
            document['sha256'] = events.digest({key: item for key, item in document.items() if key != 'sha256'})
        path.write_text(json.dumps(document), encoding='utf-8')
    with pytest.raises(ValueError, match='integrity'):
        journal.load()
    assert not events._SCAN_CACHE
    path.write_bytes(original)
    assert journal.load() == expected


def test_public_reducer_keeps_operation_values_isolated():
    before = {'nested': {'old': True}}
    operations = [{'op': 'set', 'path': ['nested'], 'value': {'items': [{'text': 'original'}]}}]
    saved_before, saved_operations = copy.deepcopy(before), copy.deepcopy(operations)
    after = events.reduce_changes(before, operations)
    after['nested']['items'][0]['text'] = 'caller mutation'
    assert before == saved_before
    assert operations == saved_operations
