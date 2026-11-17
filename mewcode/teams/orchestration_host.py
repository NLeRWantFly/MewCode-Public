"""Explicit desktop execution entry point; no model is started by a read."""
from __future__ import annotations

import asyncio
import contextlib
import json

from mewcode.mcp.coord_protocol import canonical_json, now_iso
from .responsibility_coordinator import Coordinator, VerificationRegistry
from .deployment import ModelRoute
from .dynamic_plans import PlanService
from .execution_pool import ConfiguredModelAdapter, ExecutionPool
from .orchestration_views import OrchestrationViews
from .runtime import _id
from .runtime_lock import runtime_locked
from .store_v2 import TeamStateError, _digest
from .tool_broker import ToolBroker


class ResponsibilityHost:
    def __init__(self, host):
        self.host, self.runtime, self.actor = host, host.runtime, host.actor
        self.service = host.service
        self.broker = ToolBroker(self.runtime, host.root)
        self.pool = ExecutionPool(self.runtime, self.broker)
        self.validators = host.validators or VerificationRegistry()
        self.coordinator = Coordinator(self.service, self.pool, self.validators)
        self.views = OrchestrationViews(self.runtime)
        self.jobs = {}
        self.closing = False
        self.attestation_id = _id('host-attestation')
        for source_id, handler in host.sources.items():
            self.broker.register_source(source_id, handler)
        for config in host.provider_configs:
            if config.model == 'codex-auto-review':
                continue
            ready = bool(config.resolve_api_key())
            from mewcode.client import model_effort_levels
            descriptor = ModelRoute(provider_id=config.name, model_id=config.model, ready=ready, max_instances=host.model_instance_limit,
                                    effort_levels=tuple(model_effort_levels(config.protocol, config.model)),
                                    context_limit_bytes=min(max(20000, config.get_context_window()), 256000),
                                    reason='Host-configured API adapter; connectivity and quota are checked only during an explicitly started request'
                                    if ready else 'Configured provider has no resolved credential')
            self.pool.register(self.actor, descriptor, ConfiguredModelAdapter(config),
                               command_id=self.attestation_id + '-route-' + _digest(descriptor.model_dump())[7:])
        for previous in self.pool.registry.catalog(self.actor):
            if previous['route_id'] not in self.pool.adapters and previous['ready']:
                descriptor = {key: value for key, value in previous.items() if key not in {'route_id', 'enabled'}}
                descriptor.update(ready=False, reason='This route has no adapter installed in the current host configuration')
                self.pool.registry.register(self.actor, descriptor, command_id=self.attestation_id + '-missing-' + _digest(descriptor)[7:])

    @runtime_locked
    def snapshot(self, run_id):
        result = self.views.summary(self.actor, run_id)
        result.update(plan_proposals=PlanService(self.service).list(self.actor, run_id),
                      resources=self.broker.list_grants(self.actor, run_id), routes=self.pool.available(self.actor))
        for job in result['jobs']:
            job['attached_to_host'] = job['job_id'] in self.jobs and not self.jobs[job['job_id']].done()
        return result

    @runtime_locked
    def start(self, run_id, *, control_epoch, max_steps, command_id):
        if self.closing:
            raise TeamStateError('HOST_SHUTTING_DOWN', 'the host is draining existing execution jobs')
        if type(max_steps) is not int or not 1 <= max_steps <= 64:
            raise ValueError('max_steps must be 1..64')
        # Must be called from the authenticated asynchronous application route.
        loop = asyncio.get_running_loop()
        def action():
            self.coordinator._guard(run_id, control_epoch)
            if self.runtime.db.execute("SELECT 1 FROM team_v2_host_jobs WHERE team_run_id=? AND state IN ('queued','running')", (run_id,)).fetchone():
                raise TeamStateError('COORDINATOR_BUSY', 'an execution job already owns this run')
            job_id = _id('job')
            self.runtime.db.execute('INSERT INTO team_v2_host_jobs VALUES(?,?,?,\'queued\',?,NULL,?,NULL)',
                                    (job_id, run_id, control_epoch, max_steps, now_iso()))
            return dict(team_id=self.runtime._run(run_id)['team_id'], team_run_id=run_id, job_id=job_id, state='queued', completion_inferred=False)
        result = self.runtime.repo._write(self.actor, 'team_execution_requested', command_id,
                                         dict(run_id=run_id, epoch=control_epoch, max_steps=max_steps),
                                         lambda: self.runtime._owner(self.actor, run_id, epoch=control_epoch), action)
        row = self.runtime.db.execute('SELECT state FROM team_v2_host_jobs WHERE job_id=?', (result['job_id'],)).fetchone()
        if row['state'] == 'queued' and result['job_id'] not in self.jobs:
            self.jobs[result['job_id']] = loop.create_task(self._run(result['job_id'], run_id, control_epoch, max_steps))
        return result

    async def _run(self, job_id, run_id, epoch, max_steps):
        def start():
            self.coordinator._guard(run_id, epoch)
            changed = self.runtime.db.execute("UPDATE team_v2_host_jobs SET state='running' WHERE job_id=? AND state='queued'", (job_id,))
            if changed.rowcount != 1:
                raise TeamStateError('HOST_EXECUTION_STOPPED', 'the queued job was already stopped')
            return dict(team_id=self.runtime._run(run_id)['team_id'], job_id=job_id, state='running')
        result = dict(completed=False)
        state = 'paused'
        try:
            self.runtime.repo._write(self.actor, 'team_execution_started', job_id + '-start', dict(job_id=job_id),
                                     lambda: self.runtime._owner(self.actor, run_id, epoch=epoch), start)
            def attached():
                row = self.runtime.db.execute('SELECT state FROM team_v2_host_jobs WHERE job_id=?', (job_id,)).fetchone()
                return bool(row and row['state'] == 'running')
            coordinator = Coordinator(self.service, self.pool, self.validators, execution_guard=attached)
            result = await coordinator.run_until_idle(run_id, control_epoch=epoch, max_steps=max_steps)
            state = 'completed' if result['completed'] else 'paused'
        except asyncio.CancelledError:
            result = dict(completed=False, reason='HOST_EXECUTION_STOPPED', reconciliation_may_be_required=True)
        except Exception as exc:
            state = 'failed'
            result = dict(completed=False, reason=exc.code if isinstance(exc, TeamStateError) else type(exc).__name__)
        finally:
            def finish():
                self.runtime.db.execute("UPDATE team_v2_host_jobs SET state=?,result_json=?,finished_at=? WHERE job_id=? AND state IN ('running','queued')",
                                        (state, canonical_json(result), now_iso(), job_id))
                current = self.runtime.db.execute('SELECT state,result_json FROM team_v2_host_jobs WHERE job_id=?', (job_id,)).fetchone()
                return dict(team_id=self.runtime._run(run_id)['team_id'], job_id=job_id, state=current['state'], result=json.loads(current['result_json'] or 'null'))
            # A host takeover may remove this owner's control; it must not
            # grant the old job a state mutation of the run itself.
            self.runtime.repo._write(self.actor, 'team_execution_stopped', job_id + '-finish', dict(job_id=job_id, state=state),
                                     lambda: self.actor.require('team.manage'), finish)

    async def stop(self, run_id, *, job_id, control_epoch, command_id):
        with self.runtime.lock:
            self.runtime._owner(self.actor, run_id, epoch=control_epoch)
            row = self.runtime.db.execute('SELECT * FROM team_v2_host_jobs WHERE job_id=? AND team_run_id=?', (job_id, run_id)).fetchone()
        if not row:
            raise TeamStateError('JOB_NOT_ACCESSIBLE', job_id)
        # Revoke the durable job before yielding to cancellation. This also
        # covers cancellation before its coroutine has ever started, and a
        # job still alive in a different host process.
        def action():
            current = self.runtime.db.execute('SELECT state FROM team_v2_host_jobs WHERE job_id=?', (job_id,)).fetchone()
            if current['state'] in {'queued', 'running'}:
                for task in self.runtime.db.execute('SELECT v.task_id FROM team_v2_tasks v JOIN tasks t USING(task_id) WHERE v.team_run_id=? AND t.state=\'running\'', (run_id,)).fetchall():
                    self.runtime._invalidate_attempt(self.runtime._task(task['task_id']), 'effect_unknown')
                self.runtime._refresh_run(run_id)
                self.runtime.db.execute("UPDATE team_v2_host_jobs SET state='interrupted',result_json=?,finished_at=? WHERE job_id=?",
                                        (canonical_json(dict(reason='explicit_host_stop', reconciliation_required=True)), now_iso(), job_id))
            state = self.runtime.db.execute('SELECT state FROM team_v2_host_jobs WHERE job_id=?', (job_id,)).fetchone()[0]
            return dict(team_id=self.runtime._run(run_id)['team_id'], team_run_id=run_id, job_id=job_id, state=state, completion_inferred=False)
        result = self.runtime.repo._write(self.actor, 'team_execution_stopped_explicitly', command_id, dict(job_id=job_id, epoch=control_epoch),
                                         lambda: self.runtime._owner(self.actor, run_id, epoch=control_epoch), action)
        pending = self.jobs.get(job_id)
        if pending and not pending.done():
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending
        return result

    async def aclose(self):
        self.closing = True
        for job_id, pending in list(self.jobs.items()):
            if pending.done():
                continue
            with self.runtime.lock:
                row = self.runtime.db.execute('SELECT * FROM team_v2_host_jobs WHERE job_id=?', (job_id,)).fetchone()
            try:
                await self.stop(row['team_run_id'], job_id=job_id, control_epoch=row['control_epoch'], command_id=job_id + '-shutdown')
            except TeamStateError:
                # A new owner can revoke this host. Still drain its callbacks
                # before the database is closed; do not alter the new run.
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
