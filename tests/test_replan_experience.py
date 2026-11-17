from types import SimpleNamespace

from mewcode.experts.recovery_state import digest
from mewcode.harness.evolution.replan_experience import ReplanExperienceStore


METHOD = {'contract': {'node': {'role': 'executor'}}, 'tools': ['ReadFile'], 'deny_tools': [],
          'method_steps': ['Check raw and text digests independently']}


def ingest(experiences, root, run, source, *, held_out=False, failed=False, candidate_revision='current'):
    episode = {'episode_id': 'episode', 'node_id': 'repair', 'status': 'consulting', 'reserved_tokens': 500,
        'context': {'contract': METHOD['contract'], 'allowed_tools': METHOD['tools'], 'deny_tools': [],
                    'evidence': [{'digest': digest(run)}]}}
    experiences.ledger.claim(run, episode)
    experiences.ledger.transition(run, 'episode', expected_revision=1, status='proposed',
        proposal={'method_steps': METHOD['method_steps']}, used_tokens=100)
    experiences.ledger.transition(run, 'episode', expected_revision=2, status='probing', baseline={'submitted_items': []})
    experiences.ledger.transition(run, 'episode', expected_revision=3, status='applied',
        progress_after={'submitted_items': ['item']}, verification_status='host_progress_only')
    snapshot = {'locked_flow': {'meta': {'adaptive_experience_evaluation':
        {'candidate_id': digest(METHOD), 'split': 'held_out'} if held_out else {}}},
        'review_queues': {'auditor': {'contract_hash': 'contract',
            'repair_batches': {'b': {'status': 'submitted', 'owner': 'repair', 'item_ids': ['item']}},
            'repair_candidates': {'item': {'candidate_revision': 'current', 'sources': [{'sha256': digest(source)}]}},
            'records': {'item': {'state': 'needs_repair' if failed else 'complete',
                                'candidate_revision': candidate_revision}}}}}
    return experiences.ingest(SimpleNamespace(load=lambda _: snapshot), run, root)


def test_promotion_requires_two_tasks_and_disjoint_heldout_and_revokes(tmp_path):
    experiences = ReplanExperienceStore(tmp_path)
    assert ingest(experiences, tmp_path, 'one', 'a')[0]['status'] == 'candidate'
    assert ingest(experiences, tmp_path, 'two', 'b')[0]['status'] == 'candidate'
    assert ingest(experiences, tmp_path, 'copied', 'a', held_out=True)[0]['status'] == 'candidate'
    active = ingest(experiences, tmp_path, 'heldout', 'c', held_out=True)[0]
    assert active['status'] == 'active' and active['source'] == 'runtime_verified_candidate'
    assert experiences.matching(tmp_path, contract=METHOD['contract'], tools=METHOD['tools'], deny_tools=[])
    assert not experiences.matching(tmp_path, contract=METHOD['contract'], tools=[], deny_tools=[])
    assert not experiences.list(tmp_path / 'other-project')
    assert ingest(experiences, tmp_path, 'counterexample', 'd', failed=True)[0]['status'] == 'revoked'
    assert not experiences.matching(tmp_path, contract=METHOD['contract'], tools=METHOD['tools'], deny_tools=[])
    assert ingest(experiences, tmp_path, 'later-success', 'e')[0]['status'] == 'revoked'


def test_old_candidate_review_never_counts(tmp_path):
    experiences = ReplanExperienceStore(tmp_path)
    result = ingest(experiences, tmp_path, 'one', 'a', candidate_revision='old')[0]
    assert result['status'] == 'candidate'
    assert not next(iter(result['observations'].values()))['passed']
