"""Explicit v2 routing for familiar team tools, with host-bound scope.

No actor/role arguments, no implicit model wake-ups, and no v1 status writes.
"""
from __future__ import annotations

import json
import uuid

from mewcode.tools.base import ToolResult
from .store_v2 import TeamStateError
from .runtime_lock import runtime_locked


class TeamToolScope:
    def __init__(self, service, run_id, *, broker=None, coordinator=None):
        self.service, self.run_id = service, run_id
        self.runtime, self.actor = service.repository.runtime, service.actor
        with self.runtime.lock:
            row = service.repository._run(self.actor, self.runtime._run(run_id)['team_id'], run_id)
        self.team_id = row['team_id']
        self.broker, self.coordinator = broker, coordinator

    def task(self, task_id):
        value = self.runtime.task(self.actor, task_id)
        if value['team_run_id'] != self.run_id:
            raise TeamStateError('TASK_NOT_ACCESSIBLE', task_id)
        return value

    @runtime_locked
    def command(self, command, arguments):
        from .deployment import DeploymentRegistry
        from .disputes import DisputeService
        from .dynamic_plans import PlanService
        from .interventions import InterventionService
        from .responsibility import MissionService
        mission, disputes, plans, interventions = MissionService(self.runtime), DisputeService(self.runtime), PlanService(self.service), InterventionService(self.runtime)
        extended = {
            'initialize_mission': (mission.initialize, True), 'configure_coordination': (mission.configure, True),
            'review_goal': (mission.review_goal, True), 'configure_deployment': (DeploymentRegistry(self.runtime).configure, True),
            'open_dispute': (disputes.open, True), 'add_dispute_evidence': (disputes.add_evidence, False),
            'verify_dispute_evidence': (disputes.verify_evidence, False), 'record_dispute_round': (disputes.record_round, False),
            'resolve_dispute': (disputes.resolve, False), 'reopen_dispute': (disputes.reopen, False),
            'submit_intervention': (interventions.submit, True), 'apply_intervention': (interventions.apply, False),
            'clarify_intervention': (interventions.clarify, False),
            'dismiss_intervention': (interventions.dismiss, False), 'propose_plan': (plans.propose, True), 'apply_plan': (plans.apply, False), 'dismiss_plan': (plans.dismiss, False),
        }
        if self.broker:
            extended.update(grant_resource=(self.broker.grant, True), revoke_resource=(self.broker.revoke, True), reconcile_tool=(self.broker.reconcile, True))
        allowed = {'register_input', 'activate', 'amend_plan', 'accept_input', 'review', 'resolve_usage', 'resume', 'revise',
                   'revoke_artifact', 'grant_export', 'suspend', 'take_over', 'cancel_run', 'finalize',
                   'close_team', 'send_message', 'ack_message', 'set_member_state'}
        if command not in allowed and command not in extended and not (self.coordinator and command in {'recover_coordinator', 'resolve_coordinator_usage'}):
            raise TeamStateError('UNSUPPORTED_COMMAND', command)
        values = dict(arguments)
        if command == 'amend_plan':
            if 'compiled' in values:
                raise TeamStateError('PERMISSION_REQUIRED', 'the host compiles amended definitions')
            values['compiled'] = self.service.validate_spec(values.pop('definition'))
        if 'actor' in values or 'principal_ref' in values:
            raise TeamStateError('PERMISSION_REQUIRED', 'identity is host-bound')
        for key, expected in (('run_id', self.run_id), ('team_id', self.team_id)):
            if key in values and values[key] != expected:
                raise TeamStateError('RUN_NOT_ACCESSIBLE', values[key])
        if 'task_id' in values:
            self.task(values['task_id'])
        for field, table, column in (('membership_id', 'team_v2_memberships', 'team_run_id'), ('handoff_id', 'team_v2_handoffs', 'task_id'), ('attempt_id', 'team_v2_attempts', 'task_id'),
                                      ('version_id', 'team_v2_artifacts', 'team_run_id'), ('message_id', 'team_v2_messages', 'team_run_id'),
                                      ('dispute_id', 'team_v2_disputes', 'team_run_id'), ('proposal_id', 'team_v2_plan_proposals', 'team_run_id'),
                                      ('intervention_id', 'team_v2_interventions', 'team_run_id')):
            if field in values:
                row = self.runtime.db.execute(f'SELECT {column} FROM {table} WHERE {field}=?', (values[field],)).fetchone()
                if row is None:
                    raise TeamStateError('OBJECT_NOT_ACCESSIBLE', field)
                if column == 'task_id':
                    self.task(row[0])
                elif row[0] != self.run_id:
                    raise TeamStateError('RUN_NOT_ACCESSIBLE', field)
        if command in extended:
            function, inject_run = extended[command]
            if inject_run:
                values['run_id'] = self.run_id
            return function(self.actor, **values)
        if command in {'recover_coordinator', 'resolve_coordinator_usage'}:
            values['run_id'] = self.run_id
            function = self.coordinator.recover_turn if command == 'recover_coordinator' else self.coordinator.resolve_usage
            return function(**values)
        if command in {'register_input', 'activate', 'amend_plan', 'cancel_run', 'finalize', 'send_message'}:
            values['run_id'] = self.run_id
        if command in {'suspend', 'take_over', 'close_team'}:
            values['team_id'] = self.team_id
        return getattr(self.runtime, command)(self.actor, **values)

    def route(self, name, params):
        if name == 'TaskGet':
            return self.task(params.task_id)
        if name == 'TaskList':
            tasks = self.runtime.snapshot(self.actor, self.run_id)['tasks']
            if params.assignee:
                bindings = self.runtime.snapshot(self.actor, self.run_id)['bindings']
                roles = {b['role_id'] for b in bindings if b['principal_ref'] == params.assignee}
                tasks = [t for t in tasks if t['contract']['role_id'] in roles]
            return [t for t in tasks if not params.status or t['state'] == params.status]
        if name == 'TeamRoster':
            snapshot = self.runtime.snapshot(self.actor, self.run_id)
            return dict(team_id=self.team_id, team_run_id=self.run_id, bindings=snapshot['bindings'], members=snapshot['members'],
                        messages=self.runtime.messages(self.actor, self.run_id), execution_slots=snapshot['execution_slots'])
        if name == 'SendMessage':
            if params.message_type in {'shutdown_request', 'shutdown_response'}:
                raise TeamStateError('EXPLICIT_COMMAND_REQUIRED', 'use the host cancel/close workflow; shutdown is not a message')
            return self.runtime.send_message(self.actor, self.run_id, recipient=params.to,
                                             body=params.message, command_id='msg-' + uuid.uuid4().hex)
        if name == 'TaskUpdate':
            self.task(params.task_id)
            raise TeamStateError('HANDOFF_REQUIRED', 'v2 state is derived from fenced execution, input receipts and independent review; use TeamCommand')
        if name == 'TaskCreate':
            raise TeamStateError('CONTRACT_REQUIRED', 'v2 tasks require typed ports, scope, forbidden changes, verification and budget in an explicitly activated TeamSpec')
        if name == 'TeamDelete':
            raise TeamStateError('EXPLICIT_COMMAND_REQUIRED', 'use TeamCommand close_team with current epoch/version; closing retains history and artifacts')
        raise TeamStateError('UNSUPPORTED_COMMAND', name)


def route_team_tool(manager, scope_name, name, params):
    scopes = getattr(manager, '_v2_scopes', {})
    if scope_name not in scopes and not scope_name.startswith('v2:'):
        return None
    try:
        if scope_name not in scopes:
            raise TeamStateError('RUN_NOT_ACCESSIBLE', 'v2 scope must be attached by the host')
        return ToolResult(output=json.dumps(scopes[scope_name].route(name, params), ensure_ascii=False))
    except (ValueError, TypeError, TeamStateError) as exc:
        return ToolResult(output=str(exc), is_error=True)
