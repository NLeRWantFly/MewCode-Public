"""Read-only full-resume rehearsal: JSON on stdin; never writes the source store."""
import argparse
import copy
import json
import sys
from pathlib import Path

from mewcode.expert_checkpoints import ExpertCheckpointArchive
from mewcode.expert_runs import ExpertRunStore
from mewcode.experts.flow import ExpertFlowSpec
from mewcode.experts.gate_dialogue import confirmation_valid


class ReadOnlyArchive:
    def __init__(self, archive):
        self.archive = archive

    def list(self, run_id):
        return self.archive.list(run_id)

    def load(self, run_id, checkpoint_id):
        return self.archive.load(run_id, checkpoint_id)

    def save(self, *args, **kwargs):
        raise AssertionError('A rehearsal must not save an archive')


class MemoryRunReplay(ExpertRunStore):
    def __init__(self, snapshot, archive):
        # Deliberately do not initialize a writer, root directory or user cache.
        self.snapshot = copy.deepcopy(snapshot)
        self.checkpoint_archive = ReadOnlyArchive(archive)

    def mutate(self, run_id, callback, *, durable=None):
        assert run_id == self.snapshot['run_id']
        callback(self.snapshot)
        return copy.deepcopy(self.snapshot)


def rehearse(snapshot, archive):
    memory = MemoryRunReplay(snapshot, archive)
    policy = {'mode': 'team', 'authorized_by': 'user', 'change_id': 'read-only-rehearsal'}
    memory.snapshot['loop_control_policy'] = policy
    for state in memory.snapshot.get('loop_states', {}).values():
        state['loop_control_policy'] = {**policy, 'contract_hash': state.get('contract_hash')}
    resumed = memory.resume(snapshot['run_id'], strategy=snapshot.get('strategy') or 'stable')
    flow = ExpertFlowSpec.from_value(resumed['locked_flow'])
    loops = {}
    for key, state in resumed.get('loop_states', {}).items():
        before = snapshot['loop_states'][key]
        loops[key] = {'resume_from': state.get('next_node_id'), 'kept_results': list(state.get('round_results') or {}),
                      'tokens_preserved': state.get('tokens') == before.get('tokens'),
                      'time_preserved': state.get('elapsed') == before.get('elapsed'),
                      'round_preserved': state.get('round') == before.get('round')}
    return {'read_only': True, 'resume_validated': resumed['status'] == 'running',
            'next_node': resumed['stage_cursor'], 'loops': loops,
            'existing_gate_receipts_valid': {node_id: confirmation_valid(flow.node_map[node_id], flow, resumed)
                                            for node_id in resumed.get('gate_confirmations', {}) if node_id in flow.node_map},
            'provider_calls': 0, 'source_writes': 0}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', required=True)
    args = parser.parse_args()
    print(json.dumps(rehearse(json.load(sys.stdin.buffer), ExpertCheckpointArchive(Path(args.archive))), ensure_ascii=True))
