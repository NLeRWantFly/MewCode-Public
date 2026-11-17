"""Checked plan proposals and transactional replacement of active work.

A static plan remains a DAG. Feedback creates another plan revision, never a
back edge or a second scheduler. Superseded tasks, attempts and artifacts stay
available for audit; unresolved side effects and usage are not forgiven.
"""
from __future__ import annotations

import json

from mewcode.mcp.coord_protocol import canonical_json, now_iso
from .deployment import DeploymentRegistry, legacy_binding_check
from .orchestration_contracts import MissionContract, ModelPreference, Responsibility
from .responsibility import MissionService
from .spec import RuntimeBinding, TaskTemplate, TeamRun
from .spec_compiler import validate_compiled_snapshot, validate_runtime_bindings
from .store_v2 import TeamStateError, _digest, _text


def _edges(spec, key):
    return tuple(sorted((e.source.task_key or '', e.source.port_id, e.target.port_id)
                        for e in spec.connections if e.target.task_key == key))


class PlanService:
    def __init__(self, service):
        self.service = service
        self.runtime = service.repository.runtime
        self.db = self.runtime.db
        self.missions = MissionService(self.runtime)
        self.registry = DeploymentRegistry(self.runtime)

    def _boundary(self, old, new):
        if new.spec.spec_id != old.spec.spec_id or new.spec.version <= old.spec.version:
            raise TeamStateError('SPEC_VERSION_CONFLICT', 'a new plan needs a higher version of the same definition')
        if old.spec.inputs != new.spec.inputs:
            raise TeamStateError('INPUT_GRANT_CHANGED', 'input grants stay fixed; provide additional material through the explicit input workflow')
        for field in ('allowed_actions', 'allowed_resources', 'allowed_models'):
            if not set(getattr(new.effective_policy, field)) <= set(getattr(old.effective_policy, field)):
                raise TeamStateError('PERMISSION_REQUIRED', 'a plan cannot expand ' + field)
        for field in ('token_budget', 'max_tasks', 'max_inheritance_depth'):
            if getattr(new.effective_policy, field) > getattr(old.effective_policy, field):
                raise TeamStateError('PERMISSION_REQUIRED', 'a plan cannot expand ' + field)
        if old.effective_policy.self_acceptance_forbidden and not new.effective_policy.self_acceptance_forbidden:
            raise TeamStateError('SELF_ACCEPTANCE', 'a plan cannot disable independent verification')

    def _bindings(self, actor, run_id, compiled, preferences):
        deployment = self.registry.get(run_id)
        if deployment:
            existing = dict(deployment['preferences'])
            existing.update(preferences or {})
            needed = {task.role_id for task in compiled.spec.tasks}
            for role in needed:
                if role not in existing:
                    existing[role] = dict(mode='auto', candidates=deployment['allowed_routes'], instances=1)
            existing = {key: value for key, value in existing.items() if key in needed}
            routes = set(deployment['allowed_routes']) | {key for value in (preferences or {}).values() for key in ModelPreference.model_validate(value).candidates}
            bindings, selection = self.registry.make_bindings(actor, run_id, compiled, existing, allowed_routes=routes)
            self.registry.validate_bindings(actor, run_id, compiled, bindings, exact=False, allow_fixed_change=True, allowed_routes=routes)
            return bindings, existing, selection
        if preferences:
            raise TeamStateError('DEPLOYMENT_REQUIRED', 'configure model routes before changing bindings')
        old = [RuntimeBinding.model_validate(b) for b in json.loads(self.runtime._runtime_run(run_id)['bindings_json'])]
        team = self.runtime._owner(actor, run_id)
        producers = {t.role_id for t in compiled.spec.tasks}
        reviewers = {t.reviewer_role for t in compiled.spec.tasks}
        producer_capabilities = {cap for b in old if b.kind == 'model' for cap in b.capabilities}
        bindings = []
        for role in compiled.spec.roles:
            found = [b for b in old if b.role_id == role.role_id]
            if found:
                bindings.extend(found)
                continue
            common = dict(binding_id=role.role_id, team_id=team['team_id'], team_run_id=run_id,
                          role_id=role.role_id, capabilities=role.required_capabilities, policy_version='responsibility-v1')
            if role.role_id in producers:
                if not set(role.required_capabilities) <= producer_capabilities:
                    raise TeamStateError('CAPABILITY_MISMATCH', role.role_id)
                bindings.append(RuntimeBinding(**common, principal_ref='grok-executor-01', kind='model',
                                                 provider_id='grok-build', model_id='grok-4.6'))
            elif role.role_id in reviewers:
                human = any(t.reviewer_role == role.role_id and t.verification.kind == 'human_review' for t in compiled.spec.tasks)
                bindings.append(RuntimeBinding(**common, principal_ref=actor.principal_ref, kind='human' if human else 'host'))
        validate_runtime_bindings(compiled, bindings, team_id=team['team_id'], team_run_id=run_id)
        legacy_binding_check(actor, bindings, compiled.spec.tasks)
        return bindings, {}, []

    def _impact(self, run_id, old, new, bindings=None, preferences=None):
        rows = {r['task_key']: self.runtime._task(r['task_id']) for r in self.db.execute(
            'SELECT task_key,task_id FROM team_v2_tasks WHERE team_run_id=?', (run_id,))}
        before = {t.task_key: t for t in old.spec.tasks}
        after = {t.task_key: t for t in new.spec.tasks}
        removed, added = set(before) - set(after), set(after) - set(before)
        changed = {key for key in set(before) & set(after)
                   if TaskTemplate.model_validate_json(rows[key]['contract_json']) != after[key]
                   or _edges(old.spec, key) != _edges(new.spec, key)}
        rebound_roles = set()
        if bindings is not None:
            previous_bindings = json.loads(self.runtime._runtime_run(run_id)['bindings_json'])
            next_bindings = [b.model_dump(mode='json') for b in bindings]
            roles = {b['role_id'] for b in previous_bindings + next_bindings}
            for role in roles:
                def identity(values):
                    return _digest({'bindings': sorted((b for b in values if b['role_id'] == role), key=lambda b: b['binding_id'])})
                if identity(previous_bindings) != identity(next_bindings):
                    rebound_roles.add(role)
            changed.update(key for key in set(before) & set(after) if rows[key]['state'] != 'completed'
                           and {after[key].role_id, after[key].reviewer_role} & rebound_roles)
        affected = set(removed | added | changed)
        keep_expanding = True
        while keep_expanding:
            previous = set(affected)
            for spec in (old.spec, new.spec):
                affected.update(e.target.task_key for e in spec.connections
                                if e.source.task_key in affected and e.target.task_key is not None)
            keep_expanding = affected != previous
        approval_reasons = []
        deployment = self.registry.get(run_id)
        if deployment and preferences is not None:
            proposed_routes = {key for value in preferences.values() for key in ModelPreference.model_validate(value).candidates}
            if not proposed_routes <= set(deployment['allowed_routes']):
                approval_reasons.append('model_run_grant_changed')
            for role, value in preferences.items():
                previous = deployment['preferences'].get(role)
                current = ModelPreference.model_validate(value)
                if previous and previous['mode'] == 'fixed' and (current.mode != 'fixed' or current.candidates != tuple(previous['candidates'])):
                    approval_reasons.append('fixed_model_assignment_changed:' + role)
        if old.spec.outputs != new.spec.outputs:
            approval_reasons.append('root_output_contract_changed')
        for key in changed:
            current = TaskTemplate.model_validate_json(rows[key]['contract_json'])
            next_task = after[key]
            if not set(current.forbidden_changes) <= set(next_task.forbidden_changes):
                raise TeamStateError('FORBIDDEN_SCOPE_WEAKENED', key)
            criteria = {c.criterion_id: c for c in next_task.acceptance}
            if any(c.criterion_id not in criteria or criteria[c.criterion_id].description != c.description
                   or criteria[c.criterion_id].minimum_score < c.minimum_score for c in current.acceptance):
                approval_reasons.append('task_acceptance_changed:' + key)
            if current.verification != next_task.verification:
                approval_reasons.append('verification_changed:' + key)
            if self.db.execute("SELECT 1 FROM team_v2_interventions WHERE task_id=? AND intent='change_task' AND state='applied' LIMIT 1",
                               (rows[key]['task_id'],)).fetchone() and current.objective != next_task.objective:
                approval_reasons.append('explicit_user_direction_changed:' + key)
        existing_affected = sorted(affected & rows.keys())
        return dict(added=sorted(added), removed=sorted(removed), changed=sorted(changed), rebound_roles=sorted(rebound_roles),
                    affected=sorted(affected), reused=sorted(set(after) - affected),
                    affected_task_ids=[rows[key]['task_id'] for key in existing_affected],
                    affected_versions={key: dict(revision=rows[key]['revision'], state_version=rows[key]['state_version'],
                                                  fence=rows[key]['fence']) for key in existing_affected},
                    requires_user_decision=bool(approval_reasons), approval_reasons=approval_reasons,
                    unresolved_attempts=[rows[key]['task_id'] for key in existing_affected if rows[key]['held_tokens']],
                    execution_started=False)

    def propose(self, actor, run_id, *, definition, assignments, reason, expected_goal_version, expected_plan_revision,
                control_epoch, command_id, preferences=None):
        from .runtime import _id
        reason = _text(reason)
        compiled = self.service.validate_spec(definition)
        assignments = [Responsibility.model_validate(value) for value in assignments]
        def action():
            team = self.runtime._editable(actor, run_id, control_epoch)
            mission = self.missions._row(run_id)
            runtime = self.runtime._runtime_run(run_id)
            if (type(expected_goal_version) is not int or type(expected_plan_revision) is not int
                    or mission['version'] != expected_goal_version or runtime['task_set_revision'] != expected_plan_revision):
                raise TeamStateError('PLAN_PROPOSAL_STALE', run_id)
            old = self.runtime._compiled(run_id)
            self._boundary(old, compiled)
            checked = self.missions.validate_assignments(compiled, MissionContract.model_validate_json(mission['contract_json']), assignments)
            bindings, routing_preferences, selections = self._bindings(actor, run_id, compiled, preferences)
            impact = self._impact(run_id, old, compiled, bindings, routing_preferences)
            impact.update(preferences={key: ModelPreference.model_validate(value).model_dump() for key, value in routing_preferences.items()}, selection=selections)
            proposal_id = _id('proposal')
            self.db.execute('INSERT INTO team_v2_plan_proposals(proposal_id,team_run_id,goal_version,base_plan_revision,base_plan_hash,compiled_json,bindings_json,assignments_json,impact_json,state,reason,proposed_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            (proposal_id, run_id, mission['version'], expected_plan_revision, old.resolved_spec_hash,
                             compiled.model_dump_json(), canonical_json([b.model_dump() for b in bindings]),
                             canonical_json([a.model_dump() for a in checked]), canonical_json(impact), 'proposed', reason,
                             actor.principal_ref, now_iso()))
            return dict(team_id=team['team_id'], team_run_id=run_id, proposal_id=proposal_id, state='proposed',
                        goal_version=mission['version'], compiled_hash=compiled.resolved_spec_hash, impact=impact)
        return self.runtime.repo._write(actor, 'team_plan_proposed', command_id,
                                        dict(run_id=run_id, definition=compiled.spec.model_dump(), assignments=[a.model_dump() for a in assignments],
                                             reason=reason, goal_version=expected_goal_version, plan_revision=expected_plan_revision,
                                             epoch=control_epoch, preferences=preferences),
                                        lambda: self.runtime._owner(actor, run_id, epoch=control_epoch), action)

    def _row(self, actor, proposal_id, *, epoch=None):
        row = self.db.execute('SELECT * FROM team_v2_plan_proposals WHERE proposal_id=?', (proposal_id,)).fetchone()
        if not row:
            raise TeamStateError('PROPOSAL_NOT_ACCESSIBLE', proposal_id)
        self.runtime._owner(actor, row['team_run_id'], epoch=epoch)
        return row

    def list(self, actor, run_id):
        with self.runtime.lock:
            self.runtime._owner(actor, run_id)
            return [dict(proposal_id=row['proposal_id'], state=row['state'], reason=row['reason'], goal_version=row['goal_version'],
                         base_plan_revision=row['base_plan_revision'], impact=json.loads(row['impact_json']), created_at=row['created_at'],
                         definition=json.loads(row['compiled_json'])['spec']) for row in self.db.execute(
                'SELECT * FROM team_v2_plan_proposals WHERE team_run_id=? ORDER BY created_at,rowid', (run_id,))]

    def dismiss(self, actor, proposal_id, *, reason, control_epoch, command_id):
        reason = _text(reason)
        def action():
            row = self._row(actor, proposal_id, epoch=control_epoch)
            if row['state'] != 'proposed':
                raise TeamStateError('PROPOSAL_NOT_PENDING', proposal_id)
            self.db.execute("UPDATE team_v2_plan_proposals SET state='dismissed' WHERE proposal_id=?", (proposal_id,))
            return dict(team_id=self.runtime._run(row['team_run_id'])['team_id'], team_run_id=row['team_run_id'],
                        proposal_id=proposal_id, state='dismissed', reason=reason, execution_changed=False)
        return self.runtime.repo._write(actor, 'team_plan_dismissed', command_id, dict(proposal_id=proposal_id, reason=reason, epoch=control_epoch),
                                        lambda: self._row(actor, proposal_id, epoch=control_epoch), action)

    def _current_output(self, source_id, port):
        task = self.runtime._task(source_id)
        if task['state'] != 'completed':
            return None
        row = self.db.execute("SELECT packet_json FROM team_v2_handoffs WHERE task_id=? AND revision=? AND state='published'",
                              (source_id, task['revision'])).fetchone()
        if not row:
            return None
        value = next((a['version_id'] for a in json.loads(row[0])['artifacts'] if a['port_id'] == port), None)
        if value:
            self.runtime._artifact(value, published=True)
        return value

    def _rebuild_task(self, actor, task, contract, spec, ids, root_inputs):
        if task['state'] == 'running':
            self.runtime._invalidate_attempt(task, 'effect_unknown')
        previous = TaskTemplate.model_validate_json(task['contract_json'])
        revision = task['revision'] + 1
        contract_version = task['contract_version'] + int(previous != contract)
        self.db.execute('INSERT INTO team_v2_task_revisions VALUES(?,?,?,?,?,?,?,?)',
                        (task['task_id'], revision, contract_version, task['input_revision'] + 1,
                         contract.model_dump_json(), '', 'waiting_inputs', now_iso()))
        old_slots = {r['port_id']: r for r in self.runtime._inputs(task['task_id'], task['revision'])}
        for port in contract.inputs:
            edge = next((e for e in spec.connections if e.target.task_key == contract.task_key and e.target.port_id == port.port_id), None)
            source_id = ids[edge.source.task_key] if edge and edge.source.task_key else None
            source_port = edge.source.port_id if edge else None
            version = (self._current_output(source_id, source_port) if source_id else root_inputs.get(source_port)) if edge else None
            before = old_slots.get(port.port_id)
            compatible = bool(before and before['schema_ref'] == port.schema_ref and before['source_task_id'] == source_id
                              and before['source_port'] == source_port and before['version_id'] == version)
            # Contract or source changes require a fresh receipt. An unchanged
            # contract may carry its exact previously accepted input version.
            receipt = before['receipt'] if compatible and previous == contract else ('accepted' if version and source_id is None else 'pending')
            self.db.execute('INSERT INTO team_v2_inputs VALUES(?,?,?,?,?,?,?,?,?,?)',
                            (task['task_id'], revision, port.port_id, port.schema_ref, int(port.required), source_id,
                             source_port, version, receipt, before['reason'] if compatible and previous == contract else 'new checked plan binding'))
        self.db.execute('UPDATE team_v2_task_revisions SET input_hash=? WHERE task_id=? AND revision=?',
                        (self.runtime._input_hash(task['task_id'], revision), task['task_id'], revision))
        self.db.execute('UPDATE team_v2_tasks SET revision=?,contract_version=?,input_revision=input_revision+1,fence=fence+1,question_json=NULL,token_limit=? WHERE task_id=?',
                        (revision, contract_version, contract.token_budget, task['task_id']))
        self.db.execute("UPDATE team_v2_questions SET state='superseded' WHERE task_id=? AND state='pending'", (task['task_id'],))
        current = self.runtime._task(task['task_id'])
        state = 'blocked' if current['held_tokens'] else ('queued' if self.runtime._ready(current) else 'waiting_inputs')
        self.runtime._state(task['task_id'], state, 'effect_unknown' if current['held_tokens'] else None)
        self.db.execute('DELETE FROM team_v2_superseded_tasks WHERE task_id=?', (task['task_id'],))

    def apply(self, actor, proposal_id, *, approved, control_epoch, command_id, internal=False):
        from .runtime import _id
        from .plan_amendment import _append_task
        if type(internal) is not bool or type(approved) is not bool:
            raise ValueError('approval and internal must be booleans')
        def action():
            row = self._row(actor, proposal_id, epoch=control_epoch)
            run_id = row['team_run_id']
            team = self.runtime._editable(actor, run_id, control_epoch)
            if row['state'] != 'proposed':
                raise TeamStateError('PROPOSAL_NOT_PENDING', proposal_id)
            mission, current = self.missions._row(run_id), self.runtime._runtime_run(run_id)
            old, compiled = self.runtime._compiled(run_id), validate_compiled_snapshot(json.loads(row['compiled_json']))
            if mission['version'] != row['goal_version'] or current['task_set_revision'] != row['base_plan_revision'] or old.resolved_spec_hash != row['base_plan_hash']:
                raise TeamStateError('PLAN_PROPOSAL_STALE', 'goal or active plan changed')
            self._boundary(old, compiled)
            impact = json.loads(row['impact_json'])
            bindings = [RuntimeBinding.model_validate(value) for value in json.loads(row['bindings_json'])]
            fresh = self._impact(run_id, old, compiled, bindings, impact['preferences'])
            if fresh['affected_versions'] != impact['affected_versions']:
                raise TeamStateError('PLAN_PROPOSAL_STALE', 'affected work advanced; inspect a fresh impact preview')
            if internal:
                if not self.missions.policy(run_id).internal_replan or fresh['requires_user_decision']:
                    raise TeamStateError('APPROVAL_REQUIRED', 'this change needs an explicit user decision')
            elif approved is not True:
                raise TeamStateError('APPROVAL_REQUIRED', 'approve the exact new plan and its impact')
            validate_runtime_bindings(compiled, bindings, team_id=team['team_id'], team_run_id=run_id)
            allowed_routes = set(self.registry.get(run_id)['allowed_routes']) if self.registry.get(run_id) else set()
            if approved and not internal:
                allowed_routes.update(key for value in impact['preferences'].values() for key in value['candidates'])
            if not self.registry.validate_bindings(actor, run_id, compiled, bindings, exact=False, allow_fixed_change=approved and not internal, allowed_routes=allowed_routes):
                legacy_binding_check(actor, bindings, compiled.spec.tasks)
            records = {r['task_key']: self.runtime._task(r['task_id']) for r in self.db.execute('SELECT task_key,task_id FROM team_v2_tasks WHERE team_run_id=?', (run_id,))}
            budget = self.db.execute('SELECT * FROM team_v2_budgets WHERE project_id=? AND budget_ref=?', (actor.project_id, current['budget_ref'])).fetchone()
            needed = sum(max(0, task.token_budget - (records[task.task_key]['spent_tokens'] + records[task.task_key]['held_tokens'] if task.task_key in records else 0))
                         for task in compiled.spec.tasks if task.task_key in impact['affected'] or task.task_key not in records
                         or records[task.task_key]['state'] != 'completed')
            if any(task.token_budget < 1 for task in compiled.spec.tasks) or needed + budget['used_tokens'] + budget['reserved_tokens'] > budget['limit_tokens']:
                raise TeamStateError('BUDGET_EXCEEDED', 'new work must fit the existing grant including all historical usage')
            for key in impact['affected']:
                if key in records and records[key]['block_reason'] == 'effect_unknown' and not records[key]['held_tokens']:
                    raise TeamStateError('NEEDS_RECONCILIATION', 'plan changes cannot bypass unknown side effects')
            revision = current['task_set_revision'] + 1
            existing = self.db.execute('SELECT spec_hash FROM team_v2_specs WHERE project_id=? AND spec_id=? AND version=?',
                                       (actor.project_id, compiled.spec.spec_id, compiled.spec.version)).fetchone()
            if existing and existing[0] != _digest(compiled.spec.model_dump(mode='json')):
                raise TeamStateError('SPEC_VERSION_CONFLICT', 'another proposal registered this definition version')
            self.db.execute('INSERT OR IGNORE INTO team_v2_specs VALUES(?,?,?,?,?,?,?)',
                            (actor.project_id, compiled.spec.spec_id, compiled.spec.version, compiled.spec.model_dump_json(),
                             _digest(compiled.spec.model_dump(mode='json')), actor.principal_ref, now_iso()))
            self.db.execute('INSERT OR IGNORE INTO team_v2_plan_revisions VALUES(?,?,?,?,?)',
                            (run_id, current['task_set_revision'], old.model_dump_json(), 'Previous checked plan', now_iso()))
            self.db.execute('INSERT INTO team_v2_plan_revisions VALUES(?,?,?,?,?)',
                            (run_id, revision, compiled.model_dump_json(), row['reason'], now_iso()))
            # Cancel only formally removed work; never delete it or its evidence.
            for key in impact['removed']:
                task = records[key]
                if task['state'] == 'running':
                    self.runtime._invalidate_attempt(task, 'effect_unknown')
                if task['state'] != 'completed':
                    self.runtime._state(task['task_id'], 'cancelled', 'superseded_plan')
                self.db.execute('UPDATE team_v2_tasks SET fence=fence+1,question_json=NULL WHERE task_id=?', (task['task_id'],))
                self.db.execute("UPDATE team_v2_questions SET state='superseded' WHERE task_id=? AND state='pending'", (task['task_id'],))
                self.db.execute('INSERT INTO team_v2_superseded_tasks VALUES(?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET plan_revision=excluded.plan_revision,replacement_keys_json=excluded.replacement_keys_json,reason=excluded.reason,created_at=excluded.created_at',
                                (task['task_id'], revision, canonical_json(impact['added']), row['reason'], now_iso()))
                self.db.execute("UPDATE team_v2_disputes SET state='superseded',version=version+1,updated_at=? WHERE task_id=? AND state!='resolved'",
                                (now_iso(), task['task_id']))
            # Install role bindings before new task claims can become possible.
            self.db.execute('UPDATE team_v2_runtime_runs SET bindings_json=? WHERE team_run_id=?', (canonical_json([b.model_dump() for b in bindings]), run_id))
            active_members = {(b.role_id, b.principal_ref) for b in bindings}
            for member in self.db.execute('SELECT * FROM team_v2_memberships WHERE team_run_id=?', (run_id,)).fetchall():
                if (member['role_id'], member['principal_ref']) not in active_members:
                    self.db.execute("INSERT INTO team_v2_member_states VALUES(?,'left',?,?) ON CONFLICT(membership_id) DO UPDATE SET state='left',reason=excluded.reason,updated_at=excluded.updated_at",
                                    (member['membership_id'], 'Superseded by checked plan', now_iso()))
            for binding in bindings:
                member = self.db.execute('SELECT membership_id FROM team_v2_memberships WHERE team_run_id=? AND role_id=? AND principal_ref=?',
                                         (run_id, binding.role_id, binding.principal_ref)).fetchone()
                if member:
                    self.db.execute("INSERT INTO team_v2_member_states VALUES(?,'active',?,?) ON CONFLICT(membership_id) DO UPDATE SET state='active',reason=excluded.reason,updated_at=excluded.updated_at",
                                    (member[0], 'Retained in checked plan', now_iso()))
                else:
                    alias = binding.binding_id
                    if self.db.execute('SELECT 1 FROM team_v2_memberships WHERE team_run_id=? AND alias=?', (run_id, alias)).fetchone():
                        alias = 'member-' + _id('alias').split('-', 1)[1][:16]
                    self.db.execute('INSERT INTO team_v2_memberships VALUES(?,?,?,?,?,?)',
                                    (_id('member'), team['team_id'], run_id, alias, binding.role_id, binding.principal_ref))
            ids = {key: value['task_id'] for key, value in records.items()}
            for task in compiled.spec.tasks:
                ids.setdefault(task.task_key, _id('task'))
            roots = json.loads(current['input_json'])
            tasks = {task.task_key: task for task in compiled.spec.tasks}
            for key in compiled.task_order:
                if key not in records:
                    _append_task(self.runtime, actor, team, run_id, compiled, tasks[key], ids, roots, records)
                elif key in impact['affected']:
                    self._rebuild_task(actor, records[key], tasks[key], compiled.spec, ids, roots)
                records[key] = self.runtime._task(ids[key])
            for key in compiled.task_order:
                self.db.execute('UPDATE tasks SET target_agent_id=? WHERE task_id=?',
                                (next(b.principal_ref for b in bindings if b.role_id == tasks[key].role_id), ids[key]))
            run = TeamRun.model_validate_json(self.runtime._run(run_id)['run_json'])
            run = run.model_copy(update={'task_set_revision': revision, 'resolved_spec_hash': compiled.resolved_spec_hash})
            self.db.execute('UPDATE team_v2_runs SET run_json=? WHERE team_run_id=?', (run.model_dump_json(), run_id))
            self.db.execute('UPDATE team_v2_runtime_runs SET task_set_revision=?,export_json=NULL WHERE team_run_id=?', (revision, run_id))
            self.missions._save_assignments(run_id, mission['version'], [Responsibility.model_validate(value) for value in json.loads(row['assignments_json'])])
            deployment = self.registry.get(run_id)
            if deployment:
                version = deployment.pop('version') + 1
                deployment.update(bindings=[b.model_dump() for b in bindings], preferences=impact['preferences'], selection=impact['selection'], allowed_routes=sorted(allowed_routes))
                self.db.execute('UPDATE team_v2_run_deployments SET deployment_json=?,version=?,updated_at=? WHERE team_run_id=?',
                                (canonical_json(deployment), version, now_iso(), run_id))
            self.db.execute("UPDATE team_v2_plan_proposals SET state='applied',applied_at=? WHERE proposal_id=?", (now_iso(), proposal_id))
            self.runtime._refresh_run(run_id)
            return dict(team_id=team['team_id'], team_run_id=run_id, proposal_id=proposal_id, state='applied',
                        task_set_revision=revision, affected_task_ids=impact['affected_task_ids'],
                        added_task_ids={key: ids[key] for key in impact['added']}, reused_task_keys=impact['reused'],
                        historical_task_keys=impact['removed'], execution_started=False)
        return self.runtime.repo._write(actor, 'team_plan_replaced', command_id,
                                        dict(proposal_id=proposal_id, approved=approved, internal=internal, epoch=control_epoch),
                                        lambda: self._row(actor, proposal_id, epoch=control_epoch), action)
