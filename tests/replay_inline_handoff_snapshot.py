"""Read-only replay of an already authorized team checkpoint, from JSON stdin."""
import copy
import asyncio
import json
import sys
from pathlib import Path

from mewcode.expert_group import ExpertGroupOrchestrator, ExpertRoleResult
from mewcode.experts.delivery import prior_handoff_source
from tests.replay_team_control_snapshot import MemoryRunReplay


class ReadOnlyOrchestrator(ExpertGroupOrchestrator):
    def _archive(self, *args, **kwargs):
        return Path('read-only-no-archive')  # No directory or file is created.


async def inspect_dispatch(resumed, expected_node):
    observed, verified = [], []
    async def inspect(node, model, prompt, policy, member=None):
        observed.append(node.node_id)
        assert node.node_id == expected_node
        stage = resumed['node_states'][expected_node]
        prior = prior_handoff_source(expected_node, stage, input_revision=policy.input_revision)
        assert prior is not None, 'Current dispatch would discard the prior delivery'
        verified.append(True)
        return ExpertRoleResult(text='Read-only diagnostic stop before any provider call', status='needs_attention')
    orchestrator = ReadOnlyOrchestrator(runner=inspect, node_runner=inspect)
    result = await orchestrator.run(resumed['task'], resumed['locked_profile'], {},
        strategy=resumed.get('strategy') or 'stable', run_id=resumed['run_id'], attempt=resumed['attempt'],
        checkpoint=copy.deepcopy(resumed), flow=resumed['locked_flow'])
    assert observed == [expected_node], f'Unexpected dispatch {observed}: {result.final_summary[:500]}'
    assert verified == [True], f'Current dispatch did not expose the prior delivery: {result.final_summary[:500]}'


def rehearse(snapshot):
    before = copy.deepcopy(snapshot)
    # No file store, writer, archive creation or provider. Contract must already
    # match; unlike the older migration rehearsal this does not grant a policy.
    memory = MemoryRunReplay(snapshot, None)
    resumed = memory.resume(snapshot['run_id'], strategy=snapshot.get('strategy') or 'stable')
    node_id = resumed['stage_cursor']
    old_stage = (snapshot.get('node_states') or {}).get(node_id) or {}
    new_stage = (resumed.get('node_states') or {}).get(node_id) or {}
    history = prior_handoff_source(node_id, new_stage)
    assert history and history['handoff'] == old_stage['handoff']
    assert new_stage['status'] != 'completed'  # No acceptance fabricated.
    assert snapshot == before
    asyncio.run(inspect_dispatch(resumed, node_id))
    return {'read_only': True, 'next_node': node_id, 'complete_prior_handoff_preserved': True,
            'current_dispatch_prior_delivery_accessible': True,
            'prior_decision_count': len(history['handoff'].get('decisions') or []),
            'kept_results': {key: list(state['round_results']) for key, state in resumed['loop_states'].items()},
            'usage_preserved': all(state['tokens'] == snapshot['loop_states'][key]['tokens']
                                   and state['elapsed'] == snapshot['loop_states'][key]['elapsed']
                                   for key, state in resumed['loop_states'].items()),
            'gate_receipts_unchanged': resumed.get('gate_confirmations') == snapshot.get('gate_confirmations'),
            'source_writes': 0, 'provider_calls': 0}


if __name__ == '__main__':
    print(json.dumps(rehearse(json.load(sys.stdin.buffer)), ensure_ascii=True))
