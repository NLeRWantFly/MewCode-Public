import copy
import json

import pytest

from mewcode.experts.node_ledger import ledger_for_run
from mewcode.kernel.briefs import archive_attempt, brief
from mewcode.tools.read_run_archive import ReadRunArchive, ReadRunArchiveParams


@pytest.mark.asyncio
async def test_full_history_archived_and_pageable_while_new_context_is_bounded():
    original = [{'role': 'assistant', 'content': 'old narrative' * 3000}]
    record = {'node_states': {'N03': {'messages': copy.deepcopy(original)}}, 'attempt': 2, 'kernel_revision': 5}
    archive = archive_attempt(record, 'N03')
    assert record['node_states']['N03']['messages'] == []
    assert record['kernel_attempt_archives'][archive]['data']['messages'] == original
    assert archive_attempt(record, 'N03') is None
    tool = ReadRunArchive(lambda: record, 'N03')
    chunks, offset = [], 0
    while True:
        result = await tool.execute(ReadRunArchiveParams(archive_id=archive, offset=offset))
        page = json.loads(result.output)
        chunks.append(page['content'])
        offset = page['next_offset']
        if page['complete']:
            break
    assert json.loads(''.join(chunks))['messages'] == original
    ledger = ledger_for_run(record)
    for number in range(40):
        ledger.record('N03', 'receipt', {'status': 'not_started', 'method': str(number), 'reason': 'x' * 1000},
                      source='host', identity=number)
    result = brief(record, 'N03', limit=2000)
    assert len(result) <= 2000 and result == brief(record, 'N03', limit=2000)
    assert 'old narrative' not in result
    assert json.loads(result)['failed_methods']
