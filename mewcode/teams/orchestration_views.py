"""Bounded read models for goals, task histories and resumable causal events."""
from __future__ import annotations

import json

from .deployment import DeploymentRegistry
from .disputes import DisputeService
from .interventions import InterventionService
from .responsibility import MissionService


class OrchestrationViews:
    def __init__(self, runtime):
        self.runtime, self.db = runtime, runtime.db

    def summary(self, actor, run_id):
        with self.runtime.lock:
            self.runtime._owner(actor, run_id)
            reviews = [dict(r) for r in self.db.execute('SELECT * FROM team_v2_goal_reviews WHERE team_run_id=? ORDER BY rowid DESC LIMIT 10', (run_id,))]
            for review in reviews:
                review['scores'] = json.loads(review.pop('scores_json'))
                review['accepted'] = bool(review['accepted'])
            jobs = [dict(r) for r in self.db.execute('SELECT * FROM team_v2_host_jobs WHERE team_run_id=? ORDER BY rowid DESC LIMIT 10', (run_id,))]
            for job in jobs:
                job['result'] = json.loads(job.pop('result_json') or 'null')
            turns = [dict(r) for r in self.db.execute('SELECT * FROM team_v2_coordinator_turns WHERE team_run_id=? ORDER BY rowid DESC LIMIT 10', (run_id,))]
            for turn in turns:
                turn['result'] = json.loads(turn.pop('result_json') or 'null')
                turn.pop('decision_json', None)
            calls = [dict(r) for r in self.db.execute('SELECT call_id,route_id,state,reserved_tokens,usage_tokens,goal_version,plan_revision,created_at,finished_at FROM team_v2_coord_model_calls WHERE team_run_id=? ORDER BY rowid DESC LIMIT 30', (run_id,))]
            tools = []
            for row in self.db.execute('SELECT c.* FROM team_v2_tool_calls c JOIN team_v2_tasks t USING(task_id) WHERE t.team_run_id=? ORDER BY c.rowid DESC LIMIT 60', (run_id,)):
                result = json.loads(row['result_json'] or '{}')
                content = result.get('result') or {}
                provenance = {key: content[key] for key in ('source_ref', 'source_version', 'source_hash', 'version_id', 'content_hash', 'truncated') if isinstance(content, dict) and key in content}
                tools.append(dict(attempt_id=row['attempt_id'], call_id=row['call_id'], task_id=row['task_id'], revision=row['revision'],
                                  name=row['name'], resource_id=row['resource_id'], state=row['state'], provenance=provenance,
                                  started_at=row['started_at'], finished_at=row['finished_at']))
            return dict(mission=MissionService(self.runtime).view(actor, run_id), deployment=DeploymentRegistry(self.runtime).get(run_id),
                        disputes=DisputeService(self.runtime).inbox(actor, run_id), interventions=InterventionService(self.runtime).list(actor, run_id),
                        goal_reviews=reviews, coordinator_turns=turns, coordinator_calls=calls, jobs=jobs, tool_calls=tools)

    def events(self, actor, run_id, *, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('after >= 0 and 1 <= limit <= 200 are required')
        with self.runtime.lock:
            team = self.runtime._owner(actor, run_id)
            rows = self.db.execute('SELECT sequence,event_type,actor_ref,payload_json,created_at FROM team_v2_events WHERE project_id=? AND team_id=? AND sequence>? ORDER BY sequence LIMIT ?',
                                   (actor.project_id, team['team_id'], after, limit + 1)).fetchall()
            events = [dict(sequence=r['sequence'], event_type=r['event_type'], actor_ref=r['actor_ref'], payload=json.loads(r['payload_json']), created_at=r['created_at']) for r in rows[:limit]]
            return dict(team_run_id=run_id, events=events, next_cursor=events[-1]['sequence'] if events else after, has_more=len(rows) > limit)

    def task_history(self, actor, task_id, *, before_revision=None, limit=10):
        if type(limit) is not int or not 1 <= limit <= 30 or (before_revision is not None and (type(before_revision) is not int or before_revision < 1)):
            raise ValueError('invalid revision cursor')
        with self.runtime.lock:
            task = self.runtime._task_access(actor, task_id)
            revisions = [dict(r) for r in self.db.execute('SELECT * FROM team_v2_task_revisions WHERE task_id=? AND revision<? ORDER BY revision DESC LIMIT ?', (task_id, before_revision or task['revision'] + 1, limit))]
            for revision in revisions:
                number = revision['revision']
                revision['contract'] = json.loads(revision.pop('contract_json'))
                revision['inputs'] = [dict(r) for r in self.runtime._inputs(task_id, number)]
                revision['attempts'] = []
                for attempt in self.db.execute('SELECT attempt_id,revision,fence,control_epoch,principal_ref,worker_session_id,state,reserved_tokens,usage_tokens,input_hash,created_at FROM team_v2_attempts WHERE task_id=? AND revision=? ORDER BY created_at', (task_id, number)):
                    value = dict(attempt)
                    value['public_turns'] = [dict(turn=r['turn'], state=r['state'], request_hash=r['request_hash'], result=json.loads(r['response_json'] or 'null'),
                                                    usage_tokens=r['usage_tokens'], created_at=r['created_at'], finished_at=r['finished_at'])
                                             for r in self.db.execute('SELECT * FROM team_v2_model_turns WHERE attempt_id=? ORDER BY turn LIMIT 64', (attempt['attempt_id'],))]
                    value['tools'] = [dict(call_id=r['call_id'], name=r['name'], resource_id=r['resource_id'], state=r['state'],
                                                request=json.loads(r['request_json']), result=json.loads(r['result_json'] or 'null'),
                                                started_at=r['started_at'], finished_at=r['finished_at'])
                                     for r in self.db.execute('SELECT * FROM team_v2_tool_calls WHERE attempt_id=? ORDER BY rowid LIMIT 128', (attempt['attempt_id'],))]
                    frozen = self.db.execute('SELECT goal_version,context_hash,context_json FROM team_v2_attempt_contexts WHERE attempt_id=?', (attempt['attempt_id'],)).fetchone()
                    value['context'] = dict(goal_version=frozen['goal_version'], context_hash=frozen['context_hash'],
                                            information=json.loads(frozen['context_json']).get('user_information', [])) if frozen else None
                    revision['attempts'].append(value)
                revision['reviews'] = [dict(review_id=r['review_id'], actor_ref=r['actor_ref'], accepted=bool(r['accepted']), review=json.loads(r['review_json']), created_at=r['created_at'])
                                       for r in self.db.execute('SELECT r.* FROM team_v2_reviews r JOIN team_v2_handoffs h USING(handoff_id) WHERE h.task_id=? AND h.revision=? ORDER BY r.created_at', (task_id, number))]
            return dict(task_id=task_id, current_revision=task['revision'], revisions=revisions,
                        next_before_revision=revisions[-1]['revision'] if len(revisions) == limit and revisions[-1]['revision'] > 1 else None,
                        private_reasoning_exposed=False)
