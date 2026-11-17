"""Stable, evidence-driven disputes; feedback is not an implicit task transfer."""
from __future__ import annotations

import json

from mewcode.mcp.coord_protocol import canonical_json, now_iso
from .orchestration_contracts import DisputeEvidence, DisputeSubmission
from .spec import TaskTemplate
from .store_v2 import TeamStateError, _digest, _text


class DisputeService:
    def __init__(self, runtime):
        self.runtime, self.db = runtime, runtime.db

    def _row(self, actor, dispute_id, *, owner=False, epoch=None):
        row = self.db.execute('SELECT * FROM team_v2_disputes WHERE dispute_id=?', (dispute_id,)).fetchone()
        if not row:
            raise TeamStateError('DISPUTE_NOT_ACCESSIBLE', dispute_id)
        self.runtime._task_access(actor, row['task_id'])
        if owner:
            self.runtime._owner(actor, row['team_run_id'], epoch=epoch)
        return row

    @staticmethod
    def _expected(row, expected_version):
        if type(expected_version) is not int or expected_version != row['version']:
            raise TeamStateError('STATE_VERSION_CONFLICT', row['dispute_id'])

    def _touch(self, dispute_id, **changes):
        changes.update(updated_at=now_iso())
        self.db.execute('UPDATE team_v2_disputes SET version=version+1,' + ','.join(key + '=?' for key in changes) + ' WHERE dispute_id=?',
                        (*changes.values(), dispute_id))

    def _view(self, row):
        result = dict(row)
        result['submission'] = json.loads(result.pop('submission_json'))
        result['outcome'] = json.loads(result.pop('outcome_json') or 'null')
        result['evidence'] = []
        for entry in self.db.execute('SELECT * FROM team_v2_dispute_evidence WHERE dispute_id=? ORDER BY created_at,rowid', (row['dispute_id'],)):
            evidence = dict(entry)
            evidence['evidence'] = json.loads(evidence.pop('evidence_json'))
            evidence['attestations'] = [dict(r) for r in self.db.execute('SELECT task_revision,verified_by,verification,created_at FROM team_v2_evidence_attestations WHERE evidence_id=? ORDER BY task_revision', (evidence['evidence_id'],))]
            result['evidence'].append(evidence)
        result['round_history'] = [dict(entry) for entry in self.db.execute(
            'SELECT * FROM team_v2_dispute_rounds WHERE dispute_id=? ORDER BY round', (row['dispute_id'],))]
        return result

    def get(self, actor, dispute_id):
        with self.runtime.lock:
            return self._view(self._row(actor, dispute_id))

    def inbox(self, actor, run_id):
        with self.runtime.lock:
            self.runtime.repo._run(actor, self.runtime._run(run_id)['team_id'], run_id)
            result = []
            for row in self.db.execute('SELECT * FROM team_v2_disputes WHERE team_run_id=? ORDER BY created_at', (run_id,)):
                try:
                    self._row(actor, row['dispute_id'])
                    result.append(self._view(row))
                except TeamStateError:
                    continue
            return result

    def open(self, actor, run_id, *, submission, command_id):
        from .runtime import _id
        submission = DisputeSubmission.model_validate(submission)
        def authorize():
            task = self.runtime._task_access(actor, submission.task_id)
            if task['team_run_id'] != run_id:
                raise TeamStateError('RUN_NOT_ACCESSIBLE', run_id)
        def action():
            task = self.runtime._task(submission.task_id)
            contract = TaskTemplate.model_validate_json(task['contract_json'])
            if task['revision'] != submission.revision:
                raise TeamStateError('INPUT_REVISION_MISMATCH', submission.task_id)
            if submission.criterion_id not in {c.criterion_id for c in contract.acceptance}:
                raise TeamStateError('REVIEW_CRITERIA_MISMATCH', submission.criterion_id)
            if submission.owner_role != contract.role_id:
                raise TeamStateError('RESPONSIBILITY_TRANSFER_DENIED', 'a dispute does not transfer the task')
            if submission.artifact_version:
                artifact = self.runtime._artifact(submission.artifact_version)
                if artifact['producer_task_id'] != task['task_id']:
                    raise TeamStateError('ARTIFACT_NOT_ACCESSIBLE', submission.artifact_version)
                self.runtime.artifact(actor, submission.artifact_version, include_content=False)
            existing = self.db.execute('SELECT * FROM team_v2_disputes WHERE team_run_id=? AND task_id=? AND criterion_id=? AND issue_key=?',
                                       (run_id, submission.task_id, submission.criterion_id, submission.issue_key)).fetchone()
            if existing:
                return dict(team_id=task['team_id'], dispute_id=existing['dispute_id'], version=existing['version'],
                            state=existing['state'], duplicate=True, responsibility_transferred=False)
            dispute_id, now = _id('dispute'), now_iso()
            self.db.execute('INSERT INTO team_v2_disputes(dispute_id,team_run_id,task_id,issue_key,criterion_id,owner_role,state,submission_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                            (dispute_id, run_id, submission.task_id, submission.issue_key, submission.criterion_id,
                             submission.owner_role, 'investigating', submission.model_dump_json(), now, now))
            return dict(team_id=task['team_id'], dispute_id=dispute_id, version=1, state='investigating',
                        duplicate=False, responsibility_transferred=False)
        return self.runtime.repo._write(actor, 'team_dispute_opened', command_id,
                                        dict(run_id=run_id, submission=submission.model_dump()), authorize, action)

    def add_evidence(self, actor, dispute_id, *, evidence, expected_version, command_id):
        from .runtime import _id
        evidence = DisputeEvidence.model_validate(evidence)
        # Rephrasing, or attaching the same bytes to a new revision, is not new evidence.
        identity = _digest(dict(source_ref=evidence.source_ref, source_hash=evidence.source_hash, kind=evidence.kind))
        def action():
            row = self._row(actor, dispute_id)
            self._expected(row, expected_version)
            if not self.db.execute('SELECT 1 FROM team_v2_task_revisions WHERE task_id=? AND revision=?',
                                   (row['task_id'], evidence.task_revision)).fetchone():
                raise TeamStateError('INPUT_REVISION_MISMATCH', row['task_id'])
            existing = self.db.execute('SELECT evidence_id FROM team_v2_dispute_evidence WHERE dispute_id=? AND identity_hash=?',
                                       (dispute_id, identity)).fetchone()
            if existing:
                return dict(team_id=self.runtime._run(row['team_run_id'])['team_id'], dispute_id=dispute_id,
                            evidence_id=existing[0], duplicate=True, verified=False)
            evidence_id = _id('de')
            self.db.execute('INSERT INTO team_v2_dispute_evidence VALUES(?,?,?,?,?,NULL,NULL,?)',
                            (evidence_id, dispute_id, identity, actor.principal_ref, evidence.model_dump_json(), now_iso()))
            self._touch(dispute_id)
            return dict(team_id=self.runtime._run(row['team_run_id'])['team_id'], dispute_id=dispute_id,
                        evidence_id=evidence_id, duplicate=False, verified=False, version=row['version'] + 1)
        return self.runtime.repo._write(actor, 'team_dispute_evidence_added', command_id,
                                        dict(dispute_id=dispute_id, evidence=evidence.model_dump(), expected_version=expected_version),
                                        lambda: self._row(actor, dispute_id), action)

    def verify_evidence(self, actor, dispute_id, *, evidence_id, expected_version, control_epoch, command_id, observed_revision=None, verification=None):
        if observed_revision is not None and (type(observed_revision) is not int or observed_revision < 1 or not verification):
            raise ValueError('re-attestation needs an exact positive revision and independent verification explanation')
        if verification is not None:
            verification = _text(verification)
        def authorize():
            actor.require('team.review')
            row = self._row(actor, dispute_id, owner=True, epoch=control_epoch)
            task = self.runtime._task(row['task_id'])
            role = TaskTemplate.model_validate_json(task['contract_json']).role_id
            if actor.principal_ref in {b.principal_ref for b in self.runtime._binding(task, role)}:
                raise TeamStateError('SELF_ACCEPTANCE', dispute_id)
        def action():
            row = self._row(actor, dispute_id)
            self._expected(row, expected_version)
            evidence = self.db.execute('SELECT * FROM team_v2_dispute_evidence WHERE evidence_id=? AND dispute_id=?',
                                       (evidence_id, dispute_id)).fetchone()
            if not evidence:
                raise TeamStateError('EVIDENCE_NOT_ACCESSIBLE', evidence_id)
            original = json.loads(evidence['evidence_json'])
            revision = original['task_revision'] if observed_revision is None else observed_revision
            if observed_revision is not None and revision != self.runtime._task(row['task_id'])['revision']:
                raise TeamStateError('CURRENT_EVIDENCE_REQUIRED', 're-attest only the exact current task revision')
            prior = self.db.execute('SELECT 1 FROM team_v2_evidence_attestations WHERE evidence_id=? AND task_revision=?', (evidence_id, revision)).fetchone()
            if evidence['verified_by'] and (prior or observed_revision is None):
                return dict(team_id=self.runtime._run(row['team_run_id'])['team_id'], dispute_id=dispute_id,
                            evidence_id=evidence_id, verified=True, version=row['version'])
            self.db.execute('UPDATE team_v2_dispute_evidence SET verified_by=COALESCE(verified_by,?),verified_at=COALESCE(verified_at,?) WHERE evidence_id=?',
                            (actor.principal_ref, now_iso(), evidence_id))
            self.db.execute('INSERT OR IGNORE INTO team_v2_evidence_attestations VALUES(?,?,?,?,?)',
                            (evidence_id, revision, actor.principal_ref, verification or original.get('verification') or 'Independent host verification of the exact evidence', now_iso()))
            self._touch(dispute_id)
            return dict(team_id=self.runtime._run(row['team_run_id'])['team_id'], dispute_id=dispute_id,
                        evidence_id=evidence_id, verified=True, version=row['version'] + 1)
        return self.runtime.repo._write(actor, 'team_dispute_evidence_verified', command_id,
                                        dict(dispute_id=dispute_id, evidence_id=evidence_id, expected_version=expected_version, epoch=control_epoch,
                                             observed_revision=observed_revision, verification=verification), authorize, action)

    def _verified_for_revision(self, evidence, revision):
        return bool(evidence and evidence['verified_by'] and (
            json.loads(evidence['evidence_json'])['task_revision'] == revision or self.db.execute(
                'SELECT 1 FROM team_v2_evidence_attestations WHERE evidence_id=? AND task_revision=?', (evidence['evidence_id'], revision)).fetchone()))

    def record_round(self, actor, dispute_id, *, evidence_ids, action_taken, result, expected_version, control_epoch, command_id):
        action_taken, result = _text(action_taken), _text(result)
        if not isinstance(evidence_ids, (list, tuple)) or len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError('evidence_ids must be a list of unique IDs')
        def action():
            row = self._row(actor, dispute_id)
            self._expected(row, expected_version)
            if row['state'] in {'resolved', 'superseded'}:
                raise TeamStateError('DISPUTE_RESOLVED', dispute_id)
            previous_ids = set()
            for previous in self.db.execute('SELECT evidence_ids_json FROM team_v2_dispute_rounds WHERE dispute_id=?', (dispute_id,)):
                previous_ids.update(json.loads(previous[0]))
            verified = set()
            for evidence_id in evidence_ids:
                evidence = self.db.execute('SELECT * FROM team_v2_dispute_evidence WHERE dispute_id=? AND evidence_id=?',
                                           (dispute_id, evidence_id)).fetchone()
                if not evidence:
                    raise TeamStateError('EVIDENCE_NOT_ACCESSIBLE', evidence_id)
                if evidence['verified_by']:
                    verified.add(evidence_id)
            progressed = bool(verified - previous_ids)
            rounds = row['rounds'] + 1
            stalled = 0 if progressed else row['no_progress_rounds'] + 1
            from .responsibility import MissionService
            threshold = MissionService(self.runtime).policy(row['team_run_id']).stagnation_rounds
            state = 'replanning' if stalled >= threshold else 'investigating'
            self.db.execute('INSERT INTO team_v2_dispute_rounds VALUES(?,?,?,?,?,?)',
                            (dispute_id, rounds, canonical_json(sorted(verified)), action_taken, result, now_iso()))
            self._touch(dispute_id, rounds=rounds, no_progress_rounds=stalled, state=state)
            return dict(team_id=self.runtime._run(row['team_run_id'])['team_id'], dispute_id=dispute_id,
                        version=row['version'] + 1, progress=progressed, state=state, ask_user=False, task_completed=False)
        return self.runtime.repo._write(actor, 'team_dispute_round_recorded', command_id,
                                        dict(dispute_id=dispute_id, evidence_ids=list(evidence_ids), action_taken=action_taken,
                                             result=result, expected_version=expected_version, epoch=control_epoch),
                                        lambda: self._row(actor, dispute_id, owner=True, epoch=control_epoch), action)

    def reopen(self, actor, dispute_id, *, evidence_id, reason, expected_version, control_epoch, command_id):
        reason = _text(reason)
        def action():
            row = self._row(actor, dispute_id, owner=True, epoch=control_epoch)
            self._expected(row, expected_version)
            task = self.runtime._task(row['task_id'])
            evidence = self.db.execute('SELECT * FROM team_v2_dispute_evidence WHERE dispute_id=? AND evidence_id=? AND verified_by IS NOT NULL', (dispute_id, evidence_id)).fetchone()
            if row['state'] != 'resolved' or not self._verified_for_revision(evidence, task['revision']):
                raise TeamStateError('CURRENT_EVIDENCE_REQUIRED', 'reopening needs a verified current recurrence')
            previous = json.loads(row['outcome_json'] or '{}')
            if evidence_id in previous.get('evidence_ids', []):
                raise TeamStateError('NEW_EVIDENCE_REQUIRED', 'the closing evidence is not a new recurrence')
            self._touch(dispute_id, state='investigating', outcome_json=canonical_json(dict(reopened=True, reason=reason, evidence_id=evidence_id)))
            return dict(team_id=task['team_id'], dispute_id=dispute_id, state='investigating', version=row['version'] + 1)
        def authorize():
            actor.require('team.review')
            self._row(actor, dispute_id, owner=True, epoch=control_epoch)
        return self.runtime.repo._write(actor, 'team_dispute_reopened', command_id,
                                        dict(dispute_id=dispute_id, evidence_id=evidence_id, reason=reason, expected_version=expected_version, epoch=control_epoch), authorize, action)

    def resolve(self, actor, dispute_id, *, outcome, evidence_ids, explanation, expected_version,
                control_epoch, command_id, conflicting_constraints=None, choices=None):
        explanation = _text(explanation)
        if outcome not in {'resolved', 'unknown', 'decision_required'}:
            raise ValueError('outcome must be resolved, unknown or decision_required')
        if not isinstance(evidence_ids, (list, tuple)) or len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError('evidence_ids must contain unique IDs')
        def action():
            row = self._row(actor, dispute_id)
            self._expected(row, expected_version)
            task = self.runtime._task(row['task_id'])
            records = []
            for evidence_id in evidence_ids:
                evidence = self.db.execute('SELECT * FROM team_v2_dispute_evidence WHERE dispute_id=? AND evidence_id=? AND verified_by IS NOT NULL',
                                           (dispute_id, evidence_id)).fetchone()
                if not evidence:
                    raise TeamStateError('VERIFIED_EVIDENCE_REQUIRED', evidence_id)
                records.append(evidence)
            if outcome == 'resolved' and not any(self._verified_for_revision(e, task['revision']) for e in records):
                raise TeamStateError('CURRENT_EVIDENCE_REQUIRED', 'closing a loop does not verify the current version')
            if outcome == 'decision_required':
                from .responsibility import MissionService
                mission = MissionService(self.runtime)._row(row['team_run_id'])
                contract = json.loads(mission['contract_json'])
                allowed = set(contract['hard_constraints']) | {c['criterion_id'] for c in contract['success_criteria']}
                conflicts = conflicting_constraints or []
                if (len(set(conflicts)) < 2 or not set(conflicts) <= allowed or not records
                        or not isinstance(choices, list) or len(choices) < 2 or any(not isinstance(c, str) or not c.strip() for c in choices)):
                    raise TeamStateError('CONCRETE_DECISION_REQUIRED', 'provide conflicting goal clauses, verified evidence and actionable choices')
            state = 'investigating' if outcome == 'unknown' else outcome
            decision = dict(outcome=outcome, evidence_ids=list(evidence_ids), explanation=explanation,
                            conflicting_constraints=conflicting_constraints or [], choices=choices or [])
            self._touch(dispute_id, state=state, outcome_json=canonical_json(decision))
            return dict(team_id=self.runtime._run(row['team_run_id'])['team_id'], dispute_id=dispute_id,
                        state=state, version=row['version'] + 1, task_completed=False,
                        user_action_required=state == 'decision_required')
        return self.runtime.repo._write(actor, 'team_dispute_decided', command_id,
                                        dict(dispute_id=dispute_id, outcome=outcome, evidence_ids=list(evidence_ids), explanation=explanation,
                                             expected_version=expected_version, epoch=control_epoch,
                                             conflicting_constraints=conflicting_constraints, choices=choices),
                                        lambda: self._row(actor, dispute_id, owner=True, epoch=control_epoch), action)
