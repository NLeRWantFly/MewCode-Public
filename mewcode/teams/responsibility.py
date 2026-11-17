"""Goals, responsibility and root acceptance on the existing host authority."""
from __future__ import annotations

import json

from mewcode.mcp.coord_protocol import canonical_json, now_iso
from .orchestration_contracts import CoordinationPolicy, MissionContract, Responsibility
from .runtime_protocol import ReviewScore
from .runtime_lock import runtime_locked
from .store_v2 import TeamStateError, _digest, _text


class MissionService:
    def __init__(self, runtime):
        self.runtime, self.db = runtime, runtime.db

    @runtime_locked
    def _row(self, run_id, *, required=True):
        row = self.db.execute('SELECT * FROM team_v2_missions WHERE team_run_id=?', (run_id,)).fetchone()
        if row is None and required:
            raise TeamStateError('MISSION_REQUIRED', run_id)
        return row

    def policy(self, run_id):
        row = self._row(run_id, required=False)
        return CoordinationPolicy.model_validate_json(row['policy_json']) if row else CoordinationPolicy()

    def validate_assignments(self, compiled, mission, assignments):
        values = tuple(Responsibility.model_validate(value) for value in assignments)
        tasks = {task.task_key: task for task in compiled.spec.tasks}
        assigned = {value.task_key: value for value in values}
        criteria = {criterion.criterion_id for criterion in mission.success_criteria}
        if len(assigned) != len(values) or set(assigned) != set(tasks):
            raise TeamStateError('RESPONSIBILITY_INCOMPLETE', 'every active task requires exactly one responsibility')
        covered = set()
        for value in values:
            if value.owner_role != tasks[value.task_key].role_id:
                raise TeamStateError('RESPONSIBILITY_ROLE_MISMATCH', value.task_key)
            if not set(value.goal_criteria) <= criteria or len(set(value.goal_criteria)) != len(value.goal_criteria):
                raise TeamStateError('GOAL_CRITERION_MISMATCH', value.task_key)
            covered.update(value.goal_criteria)
            visited, current = {value.task_key}, value.parent_task_key
            while current is not None:
                if current not in assigned or current in visited:
                    raise TeamStateError('RESPONSIBILITY_CYCLE', value.task_key)
                visited.add(current)
                current = assigned[current].parent_task_key
        if covered != criteria:
            raise TeamStateError('GOAL_COVERAGE_INCOMPLETE', 'every goal criterion needs an accountable work item')
        return values

    def _save_assignments(self, run_id, version, assignments):
        self.db.execute('DELETE FROM team_v2_responsibilities WHERE team_run_id=?', (run_id,))
        for value in assignments:
            self.db.execute('INSERT INTO team_v2_responsibilities VALUES(?,?,?,?)',
                            (run_id, value.task_key, version, value.model_dump_json()))

    def initialize(self, actor, run_id, *, contract, assignments, policy=None, control_epoch, command_id):
        contract = MissionContract.model_validate(contract)
        policy = CoordinationPolicy.model_validate(policy or {})
        values = [Responsibility.model_validate(value) for value in assignments]
        payload = dict(run_id=run_id, contract=contract.model_dump(), assignments=[v.model_dump() for v in values],
                       policy=policy.model_dump(), epoch=control_epoch)
        def action():
            team = self.runtime._editable(actor, run_id, control_epoch)
            if self._row(run_id, required=False):
                raise TeamStateError('MISSION_EXISTS', 'use a versioned intervention to change the goal')
            checked = self.validate_assignments(self.runtime._compiled(run_id), contract, values)
            now = now_iso()
            self.db.execute('INSERT INTO team_v2_missions VALUES(?,1,?,?,?,1,?,?)',
                            (run_id, team['owner_ref'], contract.model_dump_json(), policy.model_dump_json(), now, now))
            self.db.execute('INSERT INTO team_v2_mission_revisions VALUES(?,1,?,?,?,?)',
                            (run_id, contract.model_dump_json(), actor.principal_ref, 'Initial goal contract', now))
            self._save_assignments(run_id, 1, checked)
            # The existing TeamRun remains the objective and execution state authority.
            run = json.loads(self.runtime._run(run_id)['run_json'])
            run['objective'] = contract.objective
            self.db.execute('UPDATE team_v2_runs SET run_json=? WHERE team_run_id=?', (canonical_json(run), run_id))
            return dict(team_id=team['team_id'], team_run_id=run_id, goal_version=1,
                        owner_ref=team['owner_ref'], execution_started=False)
        return self.runtime.repo._write(actor, 'team_mission_initialized', command_id, payload,
                                        lambda: self.runtime._owner(actor, run_id, epoch=control_epoch), action)

    def configure(self, actor, run_id, *, policy, expected_version, control_epoch, command_id):
        policy = CoordinationPolicy.model_validate(policy)
        def action():
            team = self.runtime._editable(actor, run_id, control_epoch)
            row = self._row(run_id)
            if type(expected_version) is not int or row['state_version'] != expected_version:
                raise TeamStateError('STATE_VERSION_CONFLICT', run_id)
            # Capacity is only an upper bound. A registered deployment must also authorize it.
            self.db.execute('UPDATE team_v2_missions SET policy_json=?,state_version=state_version+1,updated_at=? WHERE team_run_id=?',
                            (policy.model_dump_json(), now_iso(), run_id))
            return dict(team_id=team['team_id'], team_run_id=run_id, policy=policy.model_dump(),
                        state_version=expected_version + 1, execution_started=False)
        return self.runtime.repo._write(actor, 'team_coordination_policy_changed', command_id,
                                        dict(run_id=run_id, policy=policy.model_dump(), expected_version=expected_version, epoch=control_epoch),
                                        lambda: self.runtime._owner(actor, run_id, epoch=control_epoch), action)

    def view(self, actor, run_id):
        with self.runtime.lock:
            team = self.runtime._owner(actor, run_id)
            row = self._row(run_id, required=False)
            if row is None:
                return None
            value = dict(row)
            value['owner_ref'] = team['owner_ref']  # takeover changes host authority, not history
            value['contract'] = json.loads(value.pop('contract_json'))
            value['policy'] = json.loads(value.pop('policy_json'))
            value['assignments'] = [json.loads(r[0]) for r in self.db.execute(
                'SELECT assignment_json FROM team_v2_responsibilities WHERE team_run_id=? ORDER BY task_key', (run_id,))]
            value['history'] = [dict(r) for r in self.db.execute(
                'SELECT version,reason,actor_ref,created_at FROM team_v2_mission_revisions WHERE team_run_id=? ORDER BY version', (run_id,))]
            return value

    def context(self, actor, task_id):
        with self.runtime.lock:
            task = self.runtime._task_access(actor, task_id)
            row = self._row(task['team_run_id'], required=False)
            if not row:
                return None
            assignment = self.db.execute('SELECT assignment_json FROM team_v2_responsibilities WHERE team_run_id=? AND task_key=?',
                                         (task['team_run_id'], task['task_key'])).fetchone()
            if assignment is None:
                raise TeamStateError('RESPONSIBILITY_INCOMPLETE', task['task_key'])
            assignment = json.loads(assignment[0])
            contract = json.loads(row['contract_json'])
            return dict(goal_version=row['version'], objective=contract['objective'],
                        hard_constraints=contract['hard_constraints'], responsibility=assignment,
                        success_criteria=[c for c in contract['success_criteria'] if c['criterion_id'] in assignment['goal_criteria']])

    @runtime_locked
    def state_hash(self, run_id):
        tasks = []
        for item in self.db.execute('SELECT task_id,task_key FROM team_v2_tasks WHERE team_run_id=? ORDER BY task_key', (run_id,)):
            task = self.runtime._task(item['task_id'])
            tasks.append(dict(task_key=task['task_key'], revision=task['revision'], state=task['state'],
                              input_hash=task['input_hash'], fence=task['fence']))
        return _digest(dict(tasks=tasks, goal_version=self._row(run_id)['version'],
                            plan_hash=self.runtime._compiled(run_id).resolved_spec_hash))

    def review_goal(self, actor, run_id, *, scores, accepted, expected_goal_version, expected_plan_revision,
                    control_epoch, command_id):
        from .runtime import _id
        scores = tuple(ReviewScore.model_validate(score) for score in scores)
        if type(accepted) is not bool:
            raise ValueError('accepted must be boolean')
        def authorize():
            actor.require('team.review')
            self.runtime._owner(actor, run_id, epoch=control_epoch)
        def action():
            self.runtime._editable(actor, run_id, control_epoch)
            row, run = self._row(run_id), self.runtime._runtime_run(run_id)
            if row['version'] != expected_goal_version or run['task_set_revision'] != expected_plan_revision:
                raise TeamStateError('GOAL_REVISION_MISMATCH', run_id)
            if run['state'] != 'verifying':
                raise TeamStateError('GOAL_NOT_READY', 'all required current tasks must pass first')
            contract = MissionContract.model_validate_json(row['contract_json'])
            criteria = {c.criterion_id: c for c in contract.success_criteria}
            if len(scores) != len(criteria) or {s.criterion_id for s in scores} != set(criteria):
                raise TeamStateError('GOAL_CRITERION_MISMATCH', run_id)
            if accepted and any(s.score < criteria[s.criterion_id].minimum_score for s in scores):
                raise TeamStateError('ACCEPTANCE_FAILED', 'root goal criteria did not pass')
            if accepted and self.db.execute("SELECT 1 FROM team_v2_disputes WHERE team_run_id=? AND state NOT IN ('resolved','superseded') LIMIT 1", (run_id,)).fetchone():
                raise TeamStateError('DISPUTES_UNRESOLVED', run_id)
            review_id = _id('goal-review')
            self.db.execute('INSERT INTO team_v2_goal_reviews VALUES(?,?,?,?,?,?,?,?,?)',
                            (review_id, run_id, row['version'], expected_plan_revision, self.state_hash(run_id),
                             int(accepted), canonical_json([s.model_dump() for s in scores]), actor.principal_ref, now_iso()))
            return dict(team_id=self.runtime._run(run_id)['team_id'], team_run_id=run_id, review_id=review_id,
                        goal_version=row['version'], accepted=accepted, task_completed=False)
        return self.runtime.repo._write(actor, 'team_goal_reviewed', command_id,
                                        dict(run_id=run_id, scores=[s.model_dump() for s in scores], accepted=accepted,
                                             goal_version=expected_goal_version, plan_revision=expected_plan_revision, epoch=control_epoch), authorize, action)

    def require_acceptance(self, run_id):
        row = self._row(run_id, required=False)
        if not row:
            return  # existing runs retain their original acceptance protocol
        if self.db.execute("SELECT 1 FROM team_v2_disputes WHERE team_run_id=? AND state NOT IN ('resolved','superseded') LIMIT 1", (run_id,)).fetchone():
            raise TeamStateError('DISPUTES_UNRESOLVED', run_id)
        if self.db.execute("SELECT 1 FROM team_v2_interventions WHERE team_run_id=? AND state IN ('received','pending','needs_clarification','interpreting') AND intent NOT IN ('question','information') LIMIT 1", (run_id,)).fetchone():
            raise TeamStateError('INTERVENTION_PENDING', 'a requested change has not been decided')
        review = self.db.execute('SELECT * FROM team_v2_goal_reviews WHERE team_run_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (run_id,)).fetchone()
        if not review or not review['accepted'] or review['state_hash'] != self.state_hash(run_id):
            raise TeamStateError('GOAL_REVIEW_REQUIRED', 'accept the exact current root goal and task versions')
