"""Project-scoped method candidates, never executable skills or user lessons.

Only host queue receipts for the exact submitted candidate revision count as
independent evaluation. A held-out sample must be designated in the locked flow
before execution; it cannot be selected after observing its result.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from mewcode.experts.recovery_state import digest
from mewcode.experts.replan_ledger import ReplanLedger


class ReplanExperienceStore:
    def __init__(self, root):
        self.ledger = ReplanLedger(root)
        with self.ledger.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS method_experiences ('
                       'project TEXT, id TEXT, body TEXT NOT NULL, PRIMARY KEY(project,id))')

    @staticmethod
    def project_key(project):
        return os.path.normcase(str(Path(project).resolve()))

    def list(self, project):
        with self.ledger.transaction() as db:
            return [json.loads(row[0]) for row in db.execute(
                'SELECT body FROM method_experiences WHERE project=?', (self.project_key(project),))]

    def ingest(self, store, run_id, project):
        snapshot = store.load(run_id) or {}
        episodes = self.ledger.records(run_id)
        project = self.project_key(project)
        flow = snapshot.get('locked_flow') or {}
        evaluation = (flow.get('meta') or {}).get('adaptive_experience_evaluation') or {}
        for episode in episodes.values():
            if episode['status'] != 'applied' or episode.get('verification_status') != 'host_progress_only':
                continue
            context, proposal = episode['context'], episode.get('proposal') or {}
            method = {'contract': context['contract'], 'tools': context['allowed_tools'],
                      'deny_tools': context['deny_tools'], 'method_steps': proposal.get('method_steps')}
            key = digest(method)
            new_items = set(episode['progress_after']['submitted_items']) - set(episode['baseline']['submitted_items'])
            receipts, failures, source_fingerprints = [], [], set()
            # Review state is produced by the existing independent queue tool;
            # the executor's own status/handoff/text never counts here.
            for reviewer, queue in (snapshot.get('review_queues') or {}).items():
                if reviewer == episode['node_id']:
                    continue
                for batch in (queue.get('repair_batches') or {}).values():
                    if batch.get('owner') != episode['node_id'] or batch.get('status') != 'submitted':
                        continue
                    for item_id in set(batch.get('item_ids') or []) & new_items:
                        candidate = (queue.get('repair_candidates') or {}).get(item_id) or {}
                        review = (queue.get('records') or {}).get(item_id) or {}
                        if not candidate.get('candidate_revision') or review.get('candidate_revision') != candidate['candidate_revision']:
                            continue
                        sources = candidate.get('sources') or []
                        if not sources or any(len(str(s.get('sha256') or '')) != 64 for s in sources):
                            continue
                        source_fingerprints.update(s['sha256'] for s in sources)
                        proof = {'reviewer': reviewer, 'item_id': item_id, 'candidate': candidate,
                                 'review': review, 'contract_hash': queue.get('contract_hash')}
                        if review.get('state') == 'complete' and not review.get('issue_ids'):
                            receipts.append(proof)
                        elif review.get('state') == 'needs_repair':
                            failures.append(proof)
            # Pending review remains a candidate. Evidence identities prevent a
            # copied training example from masquerading as independent holdout.
            sample = digest(sorted(source_fingerprints))
            observation = {'run_id': run_id, 'episode_id': episode['episode_id'], 'sample': sample,
                'passed': len(receipts) == len(new_items) and bool(new_items) and not failures,
                'counterexample': bool(failures), 'receipt_digest': digest([receipts, failures]),
                'source_fingerprints': sorted(source_fingerprints),
                'held_out': evaluation.get('candidate_id') == key and evaluation.get('split') == 'held_out',
                'used_tokens': episode.get('used_tokens')}
            with self.ledger.transaction() as db:
                row = db.execute('SELECT body FROM method_experiences WHERE project=? AND id=?', (project, key)).fetchone()
                value = json.loads(row[0]) if row else {'id': key, 'version': 1, 'source': 'runtime_verified_candidate',
                    'status': 'candidate', 'method': method, 'observations': {}, 'history': []}
                value['observations'][run_id + ':' + episode['episode_id']] = observation
                observations = list(value['observations'].values())
                training = [o for o in observations if o['passed'] and not o['held_out']]
                runs, samples = {o['run_id'] for o in training}, {o['sample'] for o in training}
                trained_sources = {s for o in training for s in o['source_fingerprints']}
                independent_training = any(a['run_id'] != b['run_id'] and
                    not set(a['source_fingerprints']) & set(b['source_fingerprints']) for a in training for b in training)
                holdout = any(o['passed'] and o['held_out'] and o['run_id'] not in runs
                              and not set(o['source_fingerprints']) & trained_sources
                              for o in observations)
                status = ('revoked' if value['status'] == 'revoked' or any(o['counterexample'] for o in observations) else
                          'active' if len(runs) >= 2 and len(samples) >= 2 and independent_training and holdout else 'candidate')
                if value['status'] != status:
                    value['history'].append({'from': value['status'], 'to': status, 'run_id': run_id})
                    value['version'] += 1
                value['status'] = status
                db.execute('INSERT OR REPLACE INTO method_experiences VALUES (?,?,?)',
                           (project, key, json.dumps(value, ensure_ascii=False)))
        return self.list(project)

    def matching(self, project, *, contract, tools, deny_tools):
        return [{'id': v['id'], 'version': v['version'], 'method_steps': v['method']['method_steps'],
                 'source': v['source'], 'authorization': False}
                for v in self.list(project) if v['status'] == 'active'
                and v['method']['contract'] == contract and set(v['method']['tools']) <= set(tools)
                and set(v['method']['deny_tools']) == set(deny_tools)][:3]
