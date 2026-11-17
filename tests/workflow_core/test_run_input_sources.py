import asyncio
import json

from mewcode.tools.read_stage_result import ReadStageResult, ReadStageResultParams


def test_startup_values_and_upstream_results_remain_readable():
    sources = {
        'ssh_host': 'example.invalid',
        'port': 22,
        'enabled': True,
        'optional': None,
        'books': ['circuits', 'machines'],
        'prepare': {'source_revision': 'revision-1', 'text': 'prepared'},
    }
    tool = ReadStageResult(sources)
    assert tool.revisions['prepare'] == 'revision-1'
    for key, expected in sources.items():
        result = asyncio.run(tool.execute(ReadStageResultParams(node_id=key)))
        assert not result.is_error
        assert json.loads(result.output.split('\n', 5)[5]) == expected
        assert result.host_observation['read_cursor']['more'] is False

    changed = ReadStageResult({**sources, 'ssh_host': 'other.invalid'})
    assert changed.revisions['ssh_host'] != tool.revisions['ssh_host']
    assert changed.revisions['prepare'] == tool.revisions['prepare']
