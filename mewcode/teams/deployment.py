"""Host-attested model routes and independently identified execution instances.

The registry contains no credentials, commands or user-controlled executable
paths. Only the trusted host registers adapters; a task selects from the
explicit run grant. Deployment and role identity are not model intelligence.
"""
from __future__ import annotations

import hashlib
import json
from typing import Annotated

from pydantic import Field, model_validator

from mewcode.mcp.coord_protocol import canonical_json, now_iso
from .orchestration_contracts import ModelPreference
from .runtime_lock import runtime_locked
from .spec import ContractModel, RuntimeBinding, Text
from .spec_compiler import validate_runtime_bindings
from .store_v2 import TeamStateError, _digest


class ModelRoute(ContractModel):
    provider_id: Text
    model_id: Text
    capabilities: tuple[Text, ...] = ('artifact.read', 'candidate.generate')
    purposes: tuple[Text, ...] = ('candidate', 'coordinator', 'verify')
    effort_levels: tuple[Text, ...] = ()
    context_limit_bytes: Annotated[int, Field(strict=True, ge=1024, le=16 * 1024 * 1024)] = 20000
    max_instances: Annotated[int, Field(strict=True, ge=1, le=32)] = 1
    cost_rank: Annotated[int, Field(strict=True, ge=0, le=10000)] = 100
    latency_rank: Annotated[int, Field(strict=True, ge=0, le=10000)] = 100
    ready: bool = False
    reason: Text = 'Adapter availability has not been established'
    data_location: Text = 'Configured provider; only scoped task inputs are sent'

    @model_validator(mode='after')
    def model_is_not_approval_route(self):
        if self.model_id == 'codex-auto-review':
            raise ValueError('approval routes are not model IDs')
        if len(set(self.capabilities)) != len(self.capabilities) or not set(self.purposes) <= {'candidate', 'coordinator', 'verify'}:
            raise ValueError('invalid model capability descriptor')
        return self

    @property
    def route_id(self):
        return self.provider_id + '/' + self.model_id


class DeploymentRegistry:
    def __init__(self, runtime):
        self.runtime, self.db = runtime, runtime.db

    def register(self, actor, descriptor, *, command_id):
        """Trusted Python host entry point; never exposed as a model command."""
        descriptor = ModelRoute.model_validate(descriptor)
        def action():
            self.db.execute('INSERT INTO team_v2_model_routes VALUES(?,?,?,1,?) ON CONFLICT(project_id,route_id) DO UPDATE SET descriptor_json=excluded.descriptor_json,enabled=1,updated_at=excluded.updated_at',
                            (actor.project_id, descriptor.route_id, descriptor.model_dump_json(), now_iso()))
            return dict(project_id=actor.project_id, route_id=descriptor.route_id, descriptor=descriptor.model_dump(), execution_started=False)
        return self.runtime.repo._write(actor, 'team_model_route_registered', command_id,
                                        dict(descriptor=descriptor.model_dump()), lambda: actor.require('team.manage'), action)

    def catalog(self, actor):
        actor.require('team.read')
        with self.runtime.lock:
            return [dict(route_id=row['route_id'], enabled=bool(row['enabled']), **json.loads(row['descriptor_json']))
                    for row in self.db.execute('SELECT * FROM team_v2_model_routes WHERE project_id=? ORDER BY route_id', (actor.project_id,))]

    @runtime_locked
    def _catalog(self, project_id):
        return {row['route_id']: ModelRoute.model_validate_json(row['descriptor_json'])
                for row in self.db.execute('SELECT * FROM team_v2_model_routes WHERE project_id=? AND enabled=1', (project_id,))}

    @runtime_locked
    def selection(self, project_id, preference, *, allowed_models, capabilities=(), purpose='candidate', used=None):
        preference = ModelPreference.model_validate(preference)
        if not set(preference.candidates) <= set(allowed_models):
            raise TeamStateError('MODEL_NOT_ALLOWED', 'all fallback candidates must be explicitly authorized')
        catalog = self._catalog(project_id)
        eligible, reasons = [], {}
        used = used or {}
        for key in preference.candidates:
            route = catalog.get(key)
            if route is None:
                reasons[key] = 'not registered by this host'
            elif not route.ready:
                reasons[key] = route.reason
            elif purpose not in route.purposes or not set(capabilities) <= set(route.capabilities):
                reasons[key] = 'required capability or purpose is unavailable'
            elif preference.effort and preference.effort not in route.effort_levels:
                reasons[key] = 'requested reasoning effort is not supported by this route'
            elif used.get(key, 0) + preference.instances > route.max_instances:
                reasons[key] = 'registered instance capacity would be exceeded'
            else:
                eligible.append(route)
        if not eligible:
            code = 'FIXED_MODEL_UNAVAILABLE' if preference.mode == 'fixed' else 'MODEL_CANDIDATES_UNAVAILABLE'
            raise TeamStateError(code, canonical_json(reasons))
        chosen = min(eligible, key=lambda r: (r.cost_rank + r.latency_rank, r.route_id)) if preference.mode == 'auto' else eligible[0]
        return chosen, dict(mode=preference.mode, requested=list(preference.candidates), selected=chosen.route_id,
                            reason='lowest declared cost/latency rank among capable routes' if preference.mode == 'auto'
                            else 'first available explicitly allowed candidate', unavailable=reasons)

    @runtime_locked
    def get(self, run_id):
        row = self.db.execute('SELECT * FROM team_v2_run_deployments WHERE team_run_id=?', (run_id,)).fetchone()
        return dict(version=row['version'], **json.loads(row['deployment_json'])) if row else None

    def make_bindings(self, actor, run_id, compiled, preferences, *, allowed_routes=None):
        team = self.runtime._owner(actor, run_id)
        producers = {t.role_id for t in compiled.spec.tasks}
        reviewers = {t.reviewer_role for t in compiled.spec.tasks}
        roles = {r.role_id: r for r in compiled.spec.roles}
        if set(preferences) - roles.keys():
            raise TeamStateError('INVALID_ROLE', 'model preference refers to an unknown role')
        bindings, decisions, used = [], [], {}
        allowed = set(compiled.effective_policy.allowed_models)
        if allowed_routes is not None:
            allowed.intersection_update(allowed_routes)
        for role_id, role in roles.items():
            common = dict(team_id=team['team_id'], team_run_id=run_id, role_id=role_id, policy_version='responsibility-v1')
            if role_id in producers:
                if role_id not in preferences:
                    raise TeamStateError('MODEL_PREFERENCE_REQUIRED', role_id)
                preference = ModelPreference.model_validate(preferences[role_id])
                if preference.instances > role.max_members:
                    raise TeamStateError('MEMBER_LIMIT', role_id)
                route, decision = self.selection(actor.project_id, preference, allowed_models=allowed,
                                                 capabilities=role.required_capabilities, used=used)
                used[route.route_id] = used.get(route.route_id, 0) + preference.instances
                decisions.append(dict(role_id=role_id, **decision))
                for index in range(preference.instances):
                    identity = hashlib.sha256(f'{run_id}:{role_id}:{route.route_id}:{index}'.encode()).hexdigest()[:24]
                    bindings.append(RuntimeBinding(**common, binding_id=f'bind-{identity}', principal_ref=f'agent-{identity}',
                                                     kind='model', provider_id=route.provider_id, model_id=route.model_id,
                                                     effort=preference.effort, capabilities=route.capabilities))
            elif role_id in reviewers:
                # Review dispatch is host-controlled and independent. Human-only
                # contracts cannot be silently replaced with a model judge.
                human = any(t.reviewer_role == role_id and t.verification.kind == 'human_review' for t in compiled.spec.tasks)
                bindings.append(RuntimeBinding(**common, binding_id=role_id, principal_ref=team['owner_ref'],
                                                 kind='human' if human else 'host', capabilities=role.required_capabilities))
        validate_runtime_bindings(compiled, bindings, team_id=team['team_id'], team_run_id=run_id)
        return bindings, decisions

    def configure(self, actor, run_id, *, preferences, max_parallel, control_epoch, command_id, expected_version=0,
                  coordinator=None, coordinator_budget=0):
        if type(max_parallel) is not int or not 1 <= max_parallel <= 32:
            raise ValueError('max_parallel must be an integer from 1 to 32')
        preferences = {key: ModelPreference.model_validate(value) for key, value in preferences.items()}
        coordinator = ModelPreference.model_validate(coordinator) if coordinator is not None else None
        if type(coordinator_budget) is not int or not 0 <= coordinator_budget <= 100000:
            raise ValueError('coordinator_budget must be 0..100000')
        if coordinator and (coordinator.instances != 1 or coordinator_budget < 1):
            raise ValueError('the logical root coordinator needs one instance and an explicit positive budget')
        def action():
            team = self.runtime._editable(actor, run_id, control_epoch)
            run = json.loads(self.runtime._run(run_id)['run_json'])
            if run['state'] != 'draft':
                raise TeamStateError('PLAN_PROPOSAL_REQUIRED', 'active roles change through a new checked plan revision')
            current = self.get(run_id)
            if type(expected_version) is not int or expected_version != (current['version'] if current else 0):
                raise TeamStateError('STATE_VERSION_CONFLICT', run_id)
            compiled = self.runtime._compiled(run_id)
            bindings, decisions = self.make_bindings(actor, run_id, compiled, preferences)
            coordination = None
            if coordinator:
                _, selected = self.selection(actor.project_id, coordinator, allowed_models=compiled.effective_policy.allowed_models, purpose='coordinator')
                coordination = dict(preference=coordinator.model_dump(), selection=selected)
                if sum(t.token_budget for t in compiled.spec.tasks) + coordinator_budget > compiled.effective_policy.token_budget:
                    raise TeamStateError('BUDGET_EXCEEDED', 'task allocations and root coordination must fit the same grant')
            allowed_routes = sorted({key for value in preferences.values() for key in value.candidates} | set(coordinator.candidates if coordinator else ()))
            deployment = dict(max_parallel=max_parallel, allowed_routes=allowed_routes,
                              preferences={key: value.model_dump() for key, value in preferences.items()},
                              bindings=[b.model_dump() for b in bindings], selection=decisions,
                              coordinator=coordination, coordinator_budget=coordinator_budget)
            self.db.execute('INSERT INTO team_v2_run_deployments VALUES(?,?,?,?) ON CONFLICT(team_run_id) DO UPDATE SET deployment_json=excluded.deployment_json,version=excluded.version,updated_at=excluded.updated_at',
                            (run_id, canonical_json(deployment), expected_version + 1, now_iso()))
            self.db.execute('DELETE FROM team_v2_model_preferences WHERE team_run_id=?', (run_id,))
            for role_id, preference in preferences.items():
                self.db.execute('INSERT INTO team_v2_model_preferences VALUES(?,?,?)', (run_id, role_id, preference.model_dump_json()))
            return dict(team_id=team['team_id'], team_run_id=run_id, version=expected_version + 1,
                        **deployment, execution_started=False)
        return self.runtime.repo._write(actor, 'team_deployment_configured', command_id,
                                        dict(run_id=run_id, preferences={k: p.model_dump() for k, p in preferences.items()},
                                             max_parallel=max_parallel, expected_version=expected_version, epoch=control_epoch,
                                             coordinator=coordinator.model_dump() if coordinator else None, coordinator_budget=coordinator_budget),
                                        lambda: self.runtime._owner(actor, run_id, epoch=control_epoch), action)

    def validate_bindings(self, actor, run_id, compiled, bindings, *, exact=True, allow_fixed_change=False, allowed_routes=None):
        deployment = self.get(run_id)
        if deployment is None:
            return False
        if exact and _digest({'bindings': [b.model_dump() for b in bindings]}) != _digest({'bindings': deployment['bindings']}):
            raise TeamStateError('EXECUTION_POLICY_MISMATCH', 'bindings differ from the host-attested run deployment')
        catalog = self._catalog(actor.project_id)
        count = {}
        producers = {task.role_id for task in compiled.spec.tasks}
        for binding in bindings:
            if binding.role_id in producers:
                key = f'{binding.provider_id}/{binding.model_id}'
                route = catalog.get(key)
                if (binding.kind != 'model' or key not in (allowed_routes if allowed_routes is not None else deployment['allowed_routes']) or route is None
                        or not route.ready or not set(binding.capabilities) <= set(route.capabilities)):
                    raise TeamStateError('EXECUTION_POLICY_MISMATCH', 'route is unavailable, ungranted or overclaims capabilities')
                count[key] = count.get(key, 0) + 1
                if count[key] > route.max_instances:
                    raise TeamStateError('MEMBER_LIMIT', key)
                preference = deployment['preferences'].get(binding.role_id)
                if not allow_fixed_change and preference and ModelPreference.model_validate(preference).mode == 'fixed' and key != preference['candidates'][0]:
                    raise TeamStateError('FIXED_MODEL_SUBSTITUTION', binding.role_id)
            elif binding.kind not in {'host', 'human'} or binding.principal_ref != actor.principal_ref:
                raise TeamStateError('EXECUTION_POLICY_MISMATCH', 'independent review remains host-dispatched')
        return True

    def capacity(self, run_id):
        deployment = self.get(run_id)
        if not deployment:
            return 1
        from .responsibility import MissionService
        mission = MissionService(self.runtime)._row(run_id, required=False)
        requested = MissionService(self.runtime).policy(run_id).max_parallel if mission else deployment['max_parallel']
        return min(deployment['max_parallel'], requested)

    def check_claim(self, actor, task):
        deployment = self.get(task['team_run_id'])
        if not deployment:
            if self.db.execute("SELECT 1 FROM team_v2_attempts a WHERE a.state='running' OR EXISTS(SELECT 1 FROM team_v2_model_turns m WHERE m.attempt_id=a.attempt_id AND m.state='running') LIMIT 1").fetchone():
                raise TeamStateError('WORKER_BUSY', 'legacy deployment retains a single execution slot')
            return
        active = self.db.execute("SELECT a.*,t.team_run_id FROM team_v2_attempts a JOIN team_v2_tasks t USING(task_id) WHERE a.state='running' OR EXISTS(SELECT 1 FROM team_v2_model_turns m WHERE m.attempt_id=a.attempt_id AND m.state='running')").fetchall()
        # Never overlap an already running legacy single-slot attempt.
        if any(self.get(row['team_run_id']) is None for row in active):
            raise TeamStateError('WORKER_BUSY', 'a legacy singleton owns the execution resource')
        if sum(row['team_run_id'] == task['team_run_id'] for row in active) >= self.capacity(task['team_run_id']):
            raise TeamStateError('WORKER_BUSY', 'run execution capacity is full')
        if any(row['principal_ref'] == actor.principal_ref for row in active):
            raise TeamStateError('WORKER_BUSY', 'this isolated model instance is already executing')
        bindings = self.runtime._binding(task, json.loads(task['contract_json'])['role_id'])
        selected = next((b for b in bindings if b.principal_ref == actor.principal_ref), None)
        if selected is None:
            raise TeamStateError('EXECUTION_POLICY_MISMATCH', actor.principal_ref)
        route = self._catalog(actor.project_id).get(f'{selected.provider_id}/{selected.model_id}')
        if not route or not route.ready:
            raise TeamStateError('MODEL_ROUTE_UNAVAILABLE', 'the bound route must become available; no silent fallback')
        route_active = 0
        for attempt in active:
            running_task = self.runtime._task(attempt['task_id'])
            role_id = json.loads(running_task['contract_json'])['role_id']
            binding = next((b for b in self.runtime._binding(running_task, role_id)
                            if b.principal_ref == attempt['principal_ref']), None)
            if binding and (binding.provider_id, binding.model_id) == (route.provider_id, route.model_id):
                route_active += 1
        route_active += self.db.execute("SELECT COUNT(*) FROM team_v2_coord_model_calls WHERE route_id=? AND state='running'", (route.route_id,)).fetchone()[0]
        if route_active >= route.max_instances:
            raise TeamStateError('WORKER_BUSY', 'the host model route capacity is occupied by active runs')


def legacy_binding_check(actor, bindings, tasks):
    for task in tasks:
        producer = [b for b in bindings if b.role_id == task.role_id]
        reviewer = [b for b in bindings if b.role_id == task.reviewer_role]
        if any(b.kind != 'model' or b.provider_id != 'grok-build' or b.model_id != 'grok-4.6'
               or b.principal_ref != 'grok-executor-01' for b in producer):
            raise TeamStateError('EXECUTION_POLICY_MISMATCH', 'configure a trusted deployment before selecting additional model instances')
        if any(b.kind not in {'host', 'human'} or b.principal_ref != actor.principal_ref for b in reviewer):
            raise TeamStateError('EXECUTION_POLICY_MISMATCH', 'independent review remains host-dispatched')
