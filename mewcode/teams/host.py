"""Trusted local host facade shared by desktop, MCP and parent team tools.

Constructing this object does not open or migrate a database. The explicit
opt-in marker is persistent. Execution requires an explicitly started host
job or the separately started legacy --team-v2 worker. Reads never call models.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from .artifact_publication import ArtifactPublication
from .composition import ComponentLink, compose_team, map_team
from .service import TeamService
from .spec import PolicyLimits, RuntimeBinding, PortSpec
from .spec_compiler import CompilationContext
from .store_v2 import TeamActor, TeamStateError
from .tool_adapter_v2 import TeamToolScope


DOMAINS = ('software', 'data', 'research', 'content')


def host_context(*, models=(), resources=(), validators=()):
    policy = PolicyLimits(allowed_actions=('read', 'plan', 'write_candidate', 'verify'),
                          allowed_resources=tuple(dict.fromkeys(('inputs', *resources))), allowed_models=tuple(dict.fromkeys(('grok-build/grok-4.6', 'host/gpt-6-astra', *models))),
                          max_tasks=64, token_budget=100000)
    return CompilationContext(policy, policy, frozenset({'artifact/v1', 'request/v1', *(d + '-artifact/v1' for d in DOMAINS)}), frozenset(validators))


def template(domain='software'):
    if domain not in DOMAINS:
        raise ValueError('unknown domain template')
    schema = domain + '-artifact/v1'
    stages = {'software': ('Prepare a scoped implementation candidate', 'Inspect compatibility and prepare integration candidate'),
              'data': ('Normalize the supplied batch', 'Check data quality and prepare the final batch'),
              'research': ('Extract claims from the supplied sources without browsing or file edits', 'Synthesize a report with source/version references'),
              'content': ('Draft content from the supplied brief', 'Edit the draft for the intended audience')}
    tasks = []
    for key, objective in zip(('prepare', 'deliver'), stages[domain]):
        tasks.append(dict(task_key=key, role_id='producer', reviewer_role='reviewer', objective=objective,
                          input_scope=['inputs'], forbidden_changes=['Do not modify original sources or the workspace', 'Do not invent missing facts or verification results'],
                          inputs=[dict(port_id='request', schema_ref=schema)], outputs=[dict(port_id='result', schema_ref=schema)],
                          required_actions=['read', 'write_candidate'], verification=dict(kind='human_review', ref='reviewer'),
                          acceptance=[dict(criterion_id='correct', description='The exact artifact satisfies the task contract with traceable evidence'),
                                      dict(criterion_id='scope', description='No forbidden changes or unsupported claims')], token_budget=4000))
    return dict(spec_id=domain + '-delivery', version=1,
                inputs=[dict(port_id='request', schema_ref=schema)], outputs=[dict(port_id='delivery', schema_ref=schema)],
                roles=[dict(role_id='producer', responsibility='Produce a bounded immutable candidate', allowed_actions=['read', 'write_candidate']),
                       dict(role_id='reviewer', responsibility='Independently inspect exact versions and evidence', allowed_actions=['read', 'verify'], independent_from=['producer'])],
                tasks=tasks, connections=[dict(source=dict(port_id='request'), target=dict(task_key='prepare', port_id='request')),
                                         dict(source=dict(task_key='prepare', port_id='result'), target=dict(task_key='deliver', port_id='request')),
                                         dict(source=dict(task_key='deliver', port_id='result'), target=dict(port_id='delivery'))],
                policy=host_context().project_policy.model_copy(update={'token_budget': 10000}).model_dump(mode='json'))


class TeamHost:
    def __init__(self, root: Path, *, state_dir: Path | None = None, project_id='mewcode-local', principal='codex-verifier-01',
                 provider_configs=(), validators=None, sources=None, resources=(), model_instance_limit=4):
        self.root = Path(root).resolve()
        self.state = (state_dir or self.root / '.mewcode' / 'coordination').resolve()
        if not self.state.is_relative_to(self.root):
            raise ValueError('team state must remain within its workspace')
        self.marker = self.state / 'team-v2.enabled'
        self.actor = TeamActor(principal, project_id, frozenset({'team.read', 'team.plan', 'team.manage', 'team.review', 'team.recover'}))
        self._store = None
        self._lock = threading.RLock()
        if type(model_instance_limit) is not int or not 1 <= model_instance_limit <= 32:
            raise ValueError('model_instance_limit must be 1..32')
        self.provider_configs, self.validators = tuple(provider_configs), validators
        self.sources, self.resources = dict(sources or {}), tuple(resources)
        self.model_instance_limit = model_instance_limit
        self._responsibility = None

    @property
    def enabled(self):
        return self.marker.is_file()

    def enable(self):
        with self._lock:
            self.state.mkdir(parents=True, exist_ok=True)
            self.marker.touch(exist_ok=True)
            return self.status()

    @property
    def service(self):
        if not self.enabled:
            raise TeamStateError('FEATURE_DISABLED', 'explicitly enable Agent Team v2 first')
        with self._lock:
            if self._store is None:
                from mewcode.mcp.coord_store import CoordinationStore
                self._store = CoordinationStore(self.state / 'coordination.sqlite3', self.state / 'capabilities', enable_team_v2=True)
            context = host_context(models=[c.name + '/' + c.model for c in self.provider_configs if c.model != 'codex-auto-review'],
                                   resources=self.resources, validators=self.validators.candidates if self.validators else ())
            return TeamService(self._store, actor=self.actor, compilation_context=context)

    @property
    def runtime(self):
        return self.service.repository.runtime

    @property
    def publisher(self):
        return ArtifactPublication(self.runtime, workspace=self.root)

    @property
    def responsibility(self):
        with self._lock:
            if self._responsibility is None:
                from .orchestration_host import ResponsibilityHost
                self._responsibility = ResponsibilityHost(self)
            return self._responsibility

    def close(self):
        with self._lock:
            if self._store:
                if self._responsibility:
                    if any(not pending.done() for pending in self._responsibility.jobs.values()):
                        raise TeamStateError('HOST_EXECUTION_ACTIVE', 'await host.aclose() to drain jobs before closing their database')
                    self._responsibility = None
                self._store.close()
                self._store = None

    async def aclose(self):
        if self._responsibility:
            await self._responsibility.aclose()
        self.close()

    def status(self):
        result = dict(enabled=self.enabled, project_id=self.actor.project_id, workspace=str(self.root), execution_slots=1,
                      executor='grok-build/grok-4.6', reviewer='host / explicit human review', auto_execute=False,
                      worker_command='powershell -File scripts/start_coord_worker.ps1 -TeamV2',
                      worker_ready=False, worker_reason='Worker has not registered a fresh v2 readiness snapshot')
        if self.enabled:
            # Reading readiness never probes a provider or starts a worker.
            service = self.service
            try:
                from datetime import datetime, timezone
                agent = self._store.get_agent('grok-executor-01')
                metadata = agent.get('metadata') or {}
                seen = agent.get('last_heartbeat', '')
                fresh = bool(seen and (datetime.now(timezone.utc) - datetime.fromisoformat(seen.replace('Z', '+00:00'))).total_seconds() < 90)
                result.update(worker_ready=metadata.get('readiness') == 'ready' and metadata.get('team_v2') == 'enabled' and fresh,
                              worker_reason=metadata.get('readiness_reason') or 'Readiness does not establish per-task data-only safety; each turn checks again')
            except (ValueError, TypeError, KeyError, TeamStateError):
                pass
            except Exception:
                # Old stores may have no executor registration yet.
                pass
            result.update(model_routes=self.responsibility.pool.available(self.actor),
                          responsibility_coordination=True, explicit_execution_start=True,
                          registered_validators=sorted(self.responsibility.validators.candidates),
                          root_validators=sorted(self.responsibility.validators.goals),
                          resource_ids=list(self.service.context.project_policy.allowed_resources),
                          source_ids=sorted(self.sources))
        return result

    def templates(self):
        values = {domain: template(domain) for domain in DOMAINS}
        if self.enabled:
            policy = self.service.context.project_policy
            for value in values.values():
                value['policy']['allowed_models'] = list(policy.allowed_models)
                value['policy']['allowed_resources'] = list(policy.allowed_resources)
                value['roles'][0]['max_members'] = self.model_instance_limit
        return values

    def list_runs(self):
        repository = self.service.repository
        with repository._lock:
            rows = repository._conn.execute('SELECT r.team_run_id FROM team_v2_runs r JOIN team_v2_instances i USING(team_id) WHERE i.project_id=? ORDER BY r.created_at DESC', (self.actor.project_id,)).fetchall()
            result = []
            for row in rows:
                try:
                    view = self.runtime.snapshot(self.actor, row[0])
                    result.append(dict(team=view['team'], run=view['run'], task_count=len(view['tasks'])))
                except TeamStateError:
                    continue
            return result

    def draft(self, *, definition, name, objective, budget_ref, command_id):
        service = self.service
        compiled = service.validate_spec(definition)
        service.register_spec(definition, command_id=command_id + '-spec')
        team = service.create_team(compiled.spec.ref, name=name, command_id=command_id + '-team')
        run = service.plan_run(team['team_id'], objective=objective, budget_ref=budget_ref,
                               expected_version=team['state_version'], control_epoch=team['control_epoch'], command_id=command_id + '-run')
        return dict(team=team, run=run, execution_started=False)

    def draft_goal(self, *, objective, criteria, constraints=(), domain='software', name=None, command_id):
        from .orchestration_contracts import MissionContract
        from .responsibility import MissionService
        from .store_v2 import _digest
        contract = MissionContract(objective=objective, hard_constraints=tuple(constraints),
                                   success_criteria=tuple(dict(criterion_id=f'goal-{index + 1}', description=value) for index, value in enumerate(criteria)))
        definition = self.templates()[domain]
        definition['spec_id'] = 'mission-' + _digest(dict(command_id=command_id))[7:31]
        for task in definition['tasks']:
            task['objective'] = task['objective'] + '. Root goal: ' + contract.objective
            task['forbidden_changes'] = list(dict.fromkeys([*task['forbidden_changes'], *contract.hard_constraints]))
        created = self.draft(definition=definition, name=name or objective[:60], objective=objective,
                             budget_ref=definition['spec_id'] + '-budget', command_id=command_id)
        run_id = created['run']['team_run_id']
        MissionService(self.runtime).initialize(self.actor, run_id, contract=contract,
                    assignments=[dict(task_key=t['task_key'], owner_role=t['role_id'], goal_criteria=[c.criterion_id for c in contract.success_criteria],
                                      decision_scope=['investigate', 'propose_plan', 'request_help']) for t in definition['tasks']],
                    control_epoch=created['team']['control_epoch'], command_id=command_id + '-mission')
        return dict(created, initial_plan='host_template_requires_review', plan_generated_by_model=False)

    def snapshot(self, run_id, *, read_only=False):
        if read_only:
            from .read_views import snapshot
            return snapshot(self, run_id)
        runtime = self.runtime
        responsibility = self.responsibility
        with runtime.lock:
            result = runtime.snapshot(self.actor, run_id)
            result['definition'] = runtime._compiled(run_id).spec.model_dump(mode='json')
            result['messages'] = runtime.messages(self.actor, run_id)
            result['events'] = [dict(row) for row in runtime.db.execute('SELECT sequence,event_type,actor_ref,created_at FROM team_v2_events WHERE team_id=? ORDER BY sequence DESC LIMIT 60', (result['team']['team_id'],)).fetchall()]
            result['input_versions'] = [runtime.artifact(self.actor, row[0], include_content=False) for row in runtime.db.execute('SELECT version_id FROM team_v2_artifacts WHERE team_run_id=? AND producer_task_id IS NULL ORDER BY created_at DESC LIMIT 128', (run_id,)).fetchall()]
            result['orchestration'] = responsibility.snapshot(run_id)
        result['capabilities'] = dict(command_review=True, workspace_publication=True, append_only_plan_amendment=True, arbitrary_model_binding=False,
                                      live_parallel_execution=bool(responsibility.pool.adapters), context_limit_bytes=20000,
                                      responsibility_goals=True, checked_plan_replacement=True, scoped_tool_broker=True,
                                      configured_model_binding=True, model_generated_authority=False, explicit_host_execution=True)
        result['event_cursor'] = max((event['sequence'] for event in result['events']), default=0)
        return result

    def workbench_runs(self):
        from .read_views import runs
        return runs(self)

    def activation_plan(self, run_id):
        runtime = self.runtime
        team = runtime._owner(self.actor, run_id)
        compiled = runtime._compiled(run_id)
        from .deployment import DeploymentRegistry
        configured = DeploymentRegistry(runtime).get(run_id)
        if configured:
            return dict(bindings=configured['bindings'], minimum_budget=sum(t.token_budget for t in compiled.spec.tasks) + configured.get('coordinator_budget', 0),
                        maximum_budget=compiled.effective_policy.token_budget, selection=configured['selection'], deployment_version=configured['version'])
        producers = {task.role_id for task in compiled.spec.tasks}
        reviewers = {task.reviewer_role for task in compiled.spec.tasks}
        bindings = []
        for role in compiled.spec.roles:
            common = dict(binding_id=role.role_id, team_id=team['team_id'], team_run_id=run_id, role_id=role.role_id,
                          capabilities=role.required_capabilities, policy_version='mewcode-local-v2')
            if role.role_id in producers:
                bindings.append(RuntimeBinding(**common, principal_ref='grok-executor-01', kind='model', provider_id='grok-build', model_id='grok-4.6'))
            elif role.role_id in reviewers:
                bindings.append(RuntimeBinding(**common, principal_ref=self.actor.principal_ref, kind='human'))
        return dict(bindings=[binding.model_dump(mode='json') for binding in bindings],
                    minimum_budget=sum(t.token_budget for t in compiled.spec.tasks), maximum_budget=compiled.effective_policy.token_budget)

    def command(self, run_id, command, arguments):
        return TeamToolScope(self.service, run_id, broker=self.responsibility.broker, coordinator=self.responsibility.coordinator).command(command, arguments)

    def compose(self, *, spec_id, version, components, inputs, outputs, links, policy):
        service = self.service
        plan = compose_team(spec_id=spec_id, version=version,
                            components={key: service.validate_spec(value) for key, value in components.items()},
                            inputs=tuple(PortSpec.model_validate(p) for p in inputs), outputs=tuple(PortSpec.model_validate(p) for p in outputs),
                            links=tuple(ComponentLink(**link) for link in links), policy=PolicyLimits.model_validate(policy), context=service.context)
        return dict(definition=plan.compiled.spec.model_dump(mode='json'),
                    compiled_hash=plan.compiled.resolved_spec_hash,
                    sources=[dict(component=key, spec=ref.model_dump(), compiled_hash=digest) for key, ref, digest in plan.sources])
