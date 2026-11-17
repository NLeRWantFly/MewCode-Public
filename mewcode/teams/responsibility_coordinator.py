"""Team v2 responsibility coordination; the runtime is the only scheduler.

The loop performs mechanical host actions, invokes registered independent
validators, and asks an explicitly selected planning model only when needed.
It never treats a model report or a fixed number of rounds as goal completion.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from mewcode.mcp.coord_protocol import canonical_json, now_iso
from .disputes import DisputeService
from .dynamic_plans import PlanService
from .execution_pool import ModelNotStarted, ModelReply
from .orchestration_contracts import Responsibility
from .responsibility import MissionService
from .runtime import _id
from .runtime_lock import runtime_locked
from .runtime_protocol import ReviewScore, byte_hash
from .spec import ContractModel, Text
from .store_v2 import TeamStateError, _digest, _text


class VerificationResult(ContractModel):
    accepted: bool
    scores: Annotated[tuple[ReviewScore, ...], Field(min_length=1, max_length=64)]
    detail: Text


class InputResult(ContractModel):
    accepted: bool
    reason: Text


class CoordinationDecision(ContractModel):
    action: Literal['propose_plan', 'answer_internal', 'decision_required', 'wait']
    reason: Text
    definition: dict | None = None
    assignments: tuple[Responsibility, ...] = ()
    preferences: dict | None = None
    task_id: Text | None = None
    answer: Text | None = None
    evidence_refs: tuple[Text, ...] = ()
    dispute_id: Text | None = None
    conflicting_constraints: tuple[Text, ...] = ()
    choices: tuple[Text, ...] = ()

    @model_validator(mode='after')
    def required_fields(self):
        if self.action == 'propose_plan' and (not self.definition or not self.assignments):
            raise ValueError('a plan proposal needs a complete typed definition and responsibility mapping')
        if self.action == 'answer_internal' and (not self.task_id or not self.answer or not self.evidence_refs):
            raise ValueError('internal answers need an exact task and supplied evidence references')
        if self.action == 'decision_required' and (not self.dispute_id or not self.evidence_refs or len(self.choices) < 2):
            raise ValueError('user decisions need an existing dispute, verified evidence and concrete alternatives')
        return self


class VerificationRegistry:
    """Only host-registered functions can attest checks; refs are not commands."""
    def __init__(self):
        self.candidates, self.inputs, self.goals = {}, {}, {}

    def register(self, ref, *, candidate, input_check=None):
        if not callable(candidate) or (input_check is not None and not callable(input_check)):
            raise ValueError('validators must be trusted callables')
        self.candidates[ref] = candidate
        if input_check:
            self.inputs[ref] = input_check

    def register_goal(self, ref, handler):
        if not callable(handler):
            raise ValueError('root validator must be a trusted callable')
        self.goals[ref] = handler

    @staticmethod
    async def call(handler, payload):
        async with asyncio.timeout(30):
            return await handler(payload) if inspect.iscoroutinefunction(handler) else await asyncio.to_thread(handler, payload)


class Coordinator:
    max_step_seconds = 600

    def __init__(self, service, pool, validators=None, *, execution_guard=None):
        self.service, self.pool = service, pool
        self.runtime, self.actor = service.repository.runtime, service.actor
        self.db = self.runtime.db
        self.missions = MissionService(self.runtime)
        self.disputes = DisputeService(self.runtime)
        self.plans = PlanService(service)
        self.validators = validators or VerificationRegistry()
        self.execution_guard = execution_guard

    @runtime_locked
    def _guard(self, run_id, epoch, *, goal=None, plan=None, ignore_intervention=None, manual=False):
        if self.execution_guard is not None and not self.execution_guard():
            raise TeamStateError('HOST_EXECUTION_STOPPED', 'this exact host job no longer owns execution')
        self.runtime._editable(self.actor, run_id, epoch)
        mission = self.missions._row(run_id)
        run = self.runtime._runtime_run(run_id)
        if not manual and not self.missions.policy(run_id).autonomous:
            raise TeamStateError('AUTONOMY_NOT_GRANTED', 'enable responsibility coordination explicitly')
        if goal is not None and (mission['version'] != goal or run['task_set_revision'] != plan):
            raise TeamStateError('COORDINATOR_CONTEXT_STALE', 'goal or active plan changed during the turn')
        if self.db.execute("SELECT 1 FROM team_v2_interventions WHERE team_run_id=? AND state IN ('pending','received','needs_clarification','interpreting') AND intent IN ('change_goal','change_task','pause','cancel','auto') AND (? IS NULL OR intervention_id!=?) LIMIT 1", (run_id, ignore_intervention, ignore_intervention)).fetchone():
            raise TeamStateError('INTERVENTION_PENDING', 'resolve the requested control change before new autonomous work')
        return mission, run

    def _claim_turn(self, run_id, epoch, command_id):
        def action():
            mission, run = self._guard(run_id, epoch)
            if self.db.execute("SELECT 1 FROM team_v2_coordinator_turns WHERE team_run_id=? AND state='running'", (run_id,)).fetchone():
                raise TeamStateError('COORDINATOR_BUSY', 'another host loop owns this run; an interrupted turn must be recovered explicitly')
            turn_id = _id('ct')
            self.db.execute('INSERT INTO team_v2_coordinator_turns VALUES(?,?,?,?,?,?,NULL,NULL,?,NULL)',
                            (turn_id, run_id, mission['version'], run['task_set_revision'], 'running', self.missions.state_hash(run_id), now_iso()))
            return dict(team_id=self.runtime._run(run_id)['team_id'], team_run_id=run_id, turn_id=turn_id,
                        goal_version=mission['version'], plan_revision=run['task_set_revision'])
        return self.runtime.repo._write(self.actor, 'team_coordinator_started', command_id,
                                        dict(run_id=run_id, epoch=epoch), lambda: self.runtime._owner(self.actor, run_id, epoch=epoch), action)

    def _finish_turn(self, claim, result):
        def action():
            self.db.execute("UPDATE team_v2_coordinator_turns SET state='finished',result_json=?,finished_at=? WHERE turn_id=? AND state='running'",
                            (canonical_json(result), now_iso(), claim['turn_id']))
            return dict(team_id=claim['team_id'], team_run_id=claim['team_run_id'], turn_id=claim['turn_id'], **result)
        return self.runtime.repo._write(self.actor, 'team_coordinator_finished', claim['turn_id'] + '-finish', result,
                                        lambda: self.runtime._owner(self.actor, claim['team_run_id']), action)

    async def _receipts(self, run_id, epoch):
        if not self.missions.policy(run_id).automatic_receipts:
            return []
        results = []
        for task in self.runtime.snapshot(self.actor, run_id)['tasks']:
            ref = task['contract']['verification']['ref']
            handler = self.validators.inputs.get(ref)
            if not handler or task['state'] not in {'waiting_inputs', 'queued'}:
                continue
            for slot in task['inputs']:
                if not slot['version_id'] or slot['receipt'] != 'pending':
                    continue
                artifact = self.runtime.artifact(self.actor, slot['version_id'])
                if not artifact['valid_for_consumption']:
                    continue
                verdict = InputResult.model_validate(await self.validators.call(handler, dict(stage='input', task=task,
                                                       port_id=slot['port_id'], artifact=artifact)))
                self._guard(run_id, epoch)
                results.append(self.runtime.accept_input(self.actor, task['task_id'], slot['port_id'], slot['version_id'],
                              revision=task['revision'], accepted=verdict.accepted, reason=verdict.reason,
                              control_epoch=epoch, command_id=f"auto-receipt-{task['task_id']}-{task['revision']}-{slot['port_id']}-{slot['version_id']}"))
        return results

    @runtime_locked
    def _verification_evidence(self, run_id, task, handoff, verdict, epoch):
        criteria = {c['criterion_id']: c for c in task['contract']['acceptance']}
        current_disputes = {d['criterion_id']: d for d in self.disputes.inbox(self.actor, run_id)
                            if d['task_id'] == task['task_id'] and d['issue_key'].startswith('acceptance-') and d['state'] != 'superseded'}
        for score in verdict.scores:
            passed = verdict.accepted and score.score >= criteria[score.criterion_id]['minimum_score']
            previous = current_disputes.get(score.criterion_id)
            if passed and not previous:
                continue
            opened = self.disputes.open(self.actor, run_id, submission=dict(task_id=task['task_id'], revision=task['revision'],
                    criterion_id=score.criterion_id, artifact_version=handoff['packet']['artifacts'][0]['version_id'],
                    owner_role=task['contract']['role_id'], issue_key=('acceptance-' + score.criterion_id)[:96],
                    problem=score.evidence, reproduction='Run the registered independent validator: ' + task['contract']['verification']['ref']),
                    command_id=handoff['handoff_id'] + '-dispute-' + score.criterion_id)
            dispute_id = opened['dispute_id']
            evidence = dict(kind='test', source_ref=f"validator:{task['contract']['verification']['ref']}:{task['task_id']}:{score.criterion_id}",
                            source_hash=_digest(dict(validator=task['contract']['verification']['ref'], criterion=score.criterion_id,
                                score=score.score, accepted=passed, input_hash=task['input_hash'],
                                artifact_bytes=[dict(port=a['port_id'], schema=a['schema_ref'], content_hash=a['content_hash']) for a in handoff['packet']['artifacts']])), detail=score.evidence,
                            task_revision=task['revision'], verification=verdict.detail)
            current = self.disputes.get(self.actor, dispute_id)
            added = self.disputes.add_evidence(self.actor, dispute_id, evidence=evidence, expected_version=current['version'],
                                             command_id=handoff['handoff_id'] + '-evidence-' + score.criterion_id)
            current = self.disputes.get(self.actor, dispute_id)
            self.disputes.verify_evidence(self.actor, dispute_id, evidence_id=added['evidence_id'], expected_version=current['version'],
                                         control_epoch=epoch, command_id=handoff['handoff_id'] + '-attest-' + score.criterion_id,
                                         observed_revision=task['revision'], verification=verdict.detail)
            current = self.disputes.get(self.actor, dispute_id)
            if passed:
                if current['state'] != 'resolved':
                    self.disputes.resolve(self.actor, dispute_id, outcome='resolved', evidence_ids=[added['evidence_id']],
                                          explanation=verdict.detail, expected_version=current['version'], control_epoch=epoch,
                                          command_id=handoff['handoff_id'] + '-close-' + score.criterion_id)
            else:
                if current['state'] == 'resolved':
                    self.disputes.reopen(self.actor, dispute_id, evidence_id=added['evidence_id'], reason=verdict.detail,
                                         expected_version=current['version'], control_epoch=epoch,
                                         command_id=handoff['handoff_id'] + '-reopen-' + score.criterion_id)
                    current = self.disputes.get(self.actor, dispute_id)
                self.disputes.record_round(self.actor, dispute_id, evidence_ids=[added['evidence_id']],
                                            action_taken='Independent candidate validation', result=verdict.detail,
                                            expected_version=current['version'], control_epoch=epoch,
                                            command_id=handoff['handoff_id'] + '-round-' + score.criterion_id)

    async def _reviews(self, run_id, epoch):
        if not self.missions.policy(run_id).automatic_verification:
            return []
        results = []
        for task in self.runtime.snapshot(self.actor, run_id)['tasks']:
            verification = task['contract']['verification']
            # A human contract or arbitrary command ref is never silently
            # converted to automatic acceptance.
            handler = self.validators.candidates.get(verification['ref']) if verification['kind'] == 'registered_validator' else None
            if task['state'] != 'verifying' or handler is None or task['held_tokens']:
                continue
            handoff = next((h for h in reversed(task['handoffs']) if h['revision'] == task['revision'] and h['state'] == 'submitted'), None)
            if not handoff:
                continue
            artifacts = [self.runtime.artifact(self.actor, a['version_id']) for a in handoff['packet']['artifacts']]
            inputs = [self.runtime.artifact(self.actor, i['version_id']) for i in task['inputs'] if i['version_id']]
            verdict = VerificationResult.model_validate(await self.validators.call(handler, dict(stage='candidate', task=task,
                                                    mission=self.missions.context(self.actor, task['task_id']), handoff=handoff,
                                                    artifacts=artifacts, inputs=inputs)))
            self._guard(run_id, epoch)
            results.append(self.runtime.review(self.actor, handoff['handoff_id'], accepted=verdict.accepted,
                           scores=[s.model_dump() for s in verdict.scores], expected_revision=task['revision'],
                           control_epoch=epoch, command_id=handoff['handoff_id'] + '-automatic-review'))
            self._verification_evidence(run_id, task, handoff, verdict, epoch)
        return results

    @runtime_locked
    def _recover_mechanical(self, run_id, epoch):
        recovered = []
        for task in self.runtime.snapshot(self.actor, run_id)['tasks']:
            if task['state'] != 'blocked' or task['held_tokens'] or task['question'] or task['block_reason'] not in {'execution_error', 'review_rejected'}:
                continue
            disputes = [d for d in self.disputes.inbox(self.actor, run_id) if d['task_id'] == task['task_id'] and d['state'] not in {'resolved', 'superseded'}]
            if any(d['state'] in {'decision_required', 'replanning'} for d in disputes):
                continue
            row = self.runtime._task(task['task_id'])
            if self.runtime._attempt_limit_reached(row) or self.db.execute("SELECT 1 FROM team_v2_tool_calls WHERE task_id=? AND state IN ('running','effect_unknown')", (task['task_id'],)).fetchone():
                continue
            self._guard(run_id, epoch)
            recovered.append(self.runtime.resume(self.actor, task['task_id'], expected_version=task['state_version'], control_epoch=epoch,
                                                 command_id=f"auto-repair-{task['task_id']}-{task['state_version']}"))
        return recovered

    @runtime_locked
    def _recover_routes(self, run_id, epoch):
        deployment = self.pool.registry.get(run_id)
        if not deployment or not self.missions.policy(run_id).internal_replan:
            return None
        snapshot = self.runtime.snapshot(self.actor, run_id)
        catalog = self.pool.registry._catalog(self.actor.project_id)
        unavailable = set()
        for task in snapshot['tasks']:
            if task['state'] != 'queued':
                continue
            role = task['contract']['role_id']
            bindings = [b for b in snapshot['bindings'] if b['role_id'] == role]
            if any(f"{b['provider_id']}/{b['model_id']}" not in self.pool.adapters or not catalog.get(f"{b['provider_id']}/{b['model_id']}")
                   or not catalog[f"{b['provider_id']}/{b['model_id']}"].ready for b in bindings):
                unavailable.add(role)
        if not unavailable:
            return None
        if any(deployment['preferences'][role]['mode'] == 'fixed' for role in unavailable):
            raise ModelNotStarted('FIXED_MODEL_UNAVAILABLE', 'a fixed role cannot be silently substituted')
        definition = self.runtime._compiled(run_id).spec.model_dump(mode='json')
        definition['version'] += 1
        definition['tasks'] = [task['contract'] for task in snapshot['tasks']]
        mission = self.missions.view(self.actor, run_id)
        proposed = self.plans.propose(self.actor, run_id, definition=definition, assignments=mission['assignments'],
                                     preferences=deployment['preferences'], reason='Unavailable preferred route; select only a pre-authorized capable alternative',
                                     expected_goal_version=mission['version'], expected_plan_revision=snapshot['run']['task_set_revision'],
                                     control_epoch=epoch, command_id=_id('route-proposal'))
        if not proposed['impact']['affected']:
            raise ModelNotStarted('MODEL_ROUTE_UNAVAILABLE', 'no different installed route can satisfy the declared preference')
        proposal = self.plans._row(self.actor, proposed['proposal_id'])
        if any(f"{b['provider_id']}/{b['model_id']}" not in self.pool.adapters for b in json.loads(proposal['bindings_json']) if b['kind'] == 'model'):
            raise ModelNotStarted('MODEL_ROUTE_UNAVAILABLE', 'the selected alternative lacks an installed adapter')
        return self.plans.apply(self.actor, proposed['proposal_id'], approved=False, internal=True, control_epoch=epoch, command_id=_id('route-apply'))

    @runtime_locked
    def _planning_context(self, run_id):
        snapshot = self.runtime.snapshot(self.actor, run_id)
        mission = self.missions.view(self.actor, run_id)
        disputes = self.disputes.inbox(self.actor, run_id)
        inputs = []
        known = set()
        for task in snapshot['tasks']:
            for slot in task['inputs']:
                if slot['version_id'] and slot['version_id'] not in known:
                    artifact = self.runtime.artifact(self.actor, slot['version_id'])
                    if artifact['valid_for_consumption']:
                        inputs.append(artifact)
                        known.add(slot['version_id'])
        for dispute in disputes:
            known.update(e['evidence_id'] for e in dispute['evidence'] if e['verified_by'])
        tasks = []
        for task in snapshot['tasks']:
            public_results = [json.loads(r[0]) for r in self.db.execute('SELECT m.response_json FROM team_v2_model_turns m JOIN team_v2_attempts a USING(attempt_id) WHERE a.task_id=? AND m.response_json IS NOT NULL ORDER BY m.rowid DESC LIMIT 2', (task['task_id'],))]
            tasks.append(dict(task_id=task['task_id'], task_key=task['task_key'], revision=task['revision'],
                              state=task['state'], block_reason=task['block_reason'], contract=task['contract'], public_results=public_results))
        return dict(mission=mission, definition=self.runtime._compiled(run_id).spec.model_dump(mode='json'),
                    tasks=tasks, disputes=disputes, input_evidence=inputs, supplied_evidence_refs=sorted(known),
                    root_reviews=[dict(accepted=bool(r['accepted']), scores=json.loads(r['scores_json']), goal_version=r['goal_version'], plan_revision=r['plan_revision'])
                                  for r in self.db.execute('SELECT * FROM team_v2_goal_reviews WHERE team_run_id=? ORDER BY rowid DESC LIMIT 3', (run_id,))],
                    response_schema=CoordinationDecision.model_json_schema(), authority='host_contract_only')

    async def _planning_reply(self, run_id, epoch, context, *, schema_type=CoordinationDecision, ignore_intervention=None, manual=False, before_reserve=None):
        deployment = self.pool.registry.get(run_id)
        if not deployment or not deployment.get('coordinator'):
            raise ModelNotStarted('COORDINATOR_ROUTE_REQUIRED', 'select a host-installed planning model and budget; no implicit fallback')
        selected = deployment['coordinator']['selection']['selected']
        route = self.pool.registry._catalog(self.actor.project_id).get(selected)
        adapter = self.pool.adapters.get(selected)
        if not route or not route.ready or not adapter or 'coordinator' not in route.purposes:
            raise ModelNotStarted('COORDINATOR_ROUTE_UNAVAILABLE', 'the selected coordinator route is not available')
        if len(canonical_json(context).encode()) > route.context_limit_bytes:
            raise ModelNotStarted('CONTEXT_LIMIT', 'planning evidence exceeds the exact model context grant')
        call_id = _id('coord-call')
        captured = {}
        def reserve():
            mission, run = self._guard(run_id, epoch, ignore_intervention=ignore_intervention, manual=manual)
            spent = self.db.execute('SELECT COALESCE(SUM(COALESCE(usage_tokens,reserved_tokens)),0) FROM team_v2_coord_model_calls WHERE team_run_id=?', (run_id,)).fetchone()[0]
            budget = self.db.execute('SELECT * FROM team_v2_budgets WHERE project_id=? AND budget_ref=?', (self.actor.project_id, run['budget_ref'])).fetchone()
            # Keep current task allocations available; planning never consumes
            # the producer's reserved grant just because no worker is running.
            unfinished = sum(max(0, t['token_limit'] - t['spent_tokens'] - t['held_tokens']) for t in self.runtime.snapshot(self.actor, run_id)['tasks'] if t['state'] != 'completed')
            remaining = min(deployment['coordinator_budget'] - spent,
                            budget['limit_tokens'] - budget['used_tokens'] - budget['reserved_tokens'] - unfinished)
            if remaining < 1:
                raise ModelNotStarted('COORDINATION_BUDGET_EXHAUSTED', 'planning and repair remain inside the original shared budget')
            active = self.db.execute("SELECT COUNT(*) FROM team_v2_coord_model_calls WHERE route_id=? AND state='running'", (selected,)).fetchone()[0]
            for attempt in self.db.execute("SELECT a.* FROM team_v2_attempts a WHERE a.state='running' OR EXISTS(SELECT 1 FROM team_v2_model_turns m WHERE m.attempt_id=a.attempt_id AND m.state='running')"):
                task = self.runtime._task(attempt['task_id'])
                active += int(any(b.principal_ref == attempt['principal_ref'] and f'{b.provider_id}/{b.model_id}' == selected
                                  for b in self.runtime._binding(task, json.loads(task['contract_json'])['role_id'])))
            if active >= route.max_instances:
                raise ModelNotStarted('WORKER_BUSY', 'the shared route is at its physical instance limit')
            if before_reserve:
                before_reserve()
            self.db.execute('INSERT INTO team_v2_coord_model_calls VALUES(?,?,?,?,?,?,\'running\',?,NULL,?,NULL,?,NULL)',
                            (call_id, run_id, selected, epoch, mission['version'], run['task_set_revision'], remaining, _digest(context), now_iso()))
            self.db.execute('UPDATE team_v2_budgets SET reserved_tokens=reserved_tokens+? WHERE project_id=? AND budget_ref=?',
                            (remaining, self.actor.project_id, run['budget_ref']))
            captured.update(reserved=remaining, budget_ref=run['budget_ref'], goal=mission['version'], plan=run['task_set_revision'])
            return dict(team_id=self.runtime._run(run_id)['team_id'], call_id=call_id, route_id=selected, reserved_tokens=remaining)
        self.runtime.repo._write(self.actor, 'team_coord_model_started', call_id + '-start', dict(run_id=run_id, route_id=selected, request_hash=_digest(context)),
                                 lambda: self.runtime._owner(self.actor, run_id, epoch=epoch), reserve)
        usage, decision, error = None, None, None
        try:
            async with asyncio.timeout(120):
                reply = await adapter.complete(context, max_tokens=captured['reserved'],
                                               effort=deployment['coordinator']['preference'].get('effort'), purpose='coordinator')
            if not isinstance(reply, ModelReply) or (reply.usage_tokens is not None and (type(reply.usage_tokens) is not int or reply.usage_tokens < 0)):
                raise TeamStateError('INVALID_ADAPTER_USAGE', 'invalid coordinator telemetry')
            usage = reply.usage_tokens
            decision = schema_type.model_validate_json(reply.text)
            self._guard(run_id, epoch, goal=captured['goal'], plan=captured['plan'], ignore_intervention=ignore_intervention, manual=manual)
        except BaseException as exc:
            error = exc
            if isinstance(exc, ModelNotStarted):
                usage = 0
        def settle():
            # Late telemetry may be reconciled without granting a stale model
            # response any execution authority.
            if usage is not None:
                self.db.execute('UPDATE team_v2_budgets SET reserved_tokens=reserved_tokens-?,used_tokens=used_tokens+? WHERE project_id=? AND budget_ref=?',
                                (captured['reserved'], usage, self.actor.project_id, captured['budget_ref']))
            public = decision.model_dump(mode='json') if decision and not error else dict(error=error.code if isinstance(error, TeamStateError) else type(error).__name__ if error else 'usage_unknown')
            self.db.execute('UPDATE team_v2_coord_model_calls SET state=?,usage_tokens=?,response_json=?,finished_at=? WHERE call_id=?',
                            ('failed' if error else 'completed', usage, canonical_json(public), now_iso(), call_id))
            return dict(team_id=self.runtime._run(run_id)['team_id'], call_id=call_id, usage_tokens=usage, result=public)
        self.runtime.repo._write(self.actor, 'team_coord_model_finished', call_id + '-finish', dict(call_id=call_id, usage=usage),
                                 lambda: self.runtime._owner(self.actor, run_id), settle)
        if error:
            raise error
        if usage is None or usage > captured['reserved']:
            raise TeamStateError('COORDINATOR_USAGE_UNRESOLVED', 'known in-budget usage is required before applying advice')
        return decision

    @runtime_locked
    def _apply_advice(self, run_id, epoch, context, decision):
        mission, run = self._guard(run_id, epoch)
        if not set(decision.evidence_refs) <= set(context['supplied_evidence_refs']):
            raise TeamStateError('EVIDENCE_NOT_ACCESSIBLE', 'the model referenced evidence not supplied by this host')
        if decision.action == 'propose_plan':
            proposed = self.plans.propose(self.actor, run_id, definition=decision.definition,
                       assignments=[a.model_dump() for a in decision.assignments], reason=decision.reason, preferences=decision.preferences,
                       expected_goal_version=mission['version'], expected_plan_revision=run['task_set_revision'], control_epoch=epoch, command_id=_id('auto-proposal'))
            if not self.missions.policy(run_id).internal_replan or proposed['impact']['requires_user_decision']:
                return dict(state='proposal_pending', proposal=proposed, user_action_required=proposed['impact']['requires_user_decision'])
            if not proposed['impact']['affected']:
                return dict(state='no_progress', reason='A plan with no affected work does not reset the recovery loop', proposal=proposed)
            return self.plans.apply(self.actor, proposed['proposal_id'], approved=False, internal=True, control_epoch=epoch, command_id=_id('auto-apply'))
        if decision.action == 'answer_internal':
            task = self.runtime.task(self.actor, decision.task_id)
            if task['team_run_id'] != run_id or task['state'] != 'blocked' or task['block_reason'] != 'internal_question':
                raise TeamStateError('INTERNAL_QUESTION_NOT_CURRENT', decision.task_id)
            if task['held_tokens'] or self.runtime._attempt_limit_reached(self.runtime._task(task['task_id'])):
                raise TeamStateError('REPLAN_REQUIRED', 'remaining budget or attempt bounds require a checked revised approach')
            advice_id = _id('advice')
            def save():
                current = self.runtime._task(task['task_id'])
                self._guard(run_id, epoch)
                if current['revision'] != task['revision'] or current['state_version'] != task['state_version']:
                    raise TeamStateError('STATE_VERSION_CONFLICT', task['task_id'])
                self.db.execute('INSERT INTO team_v2_coord_advice VALUES(?,?,?,?,?,?,?)',
                                (advice_id, run_id, task['task_id'], task['revision'], mission['version'],
                                 canonical_json(dict(text=decision.answer, evidence_refs=list(decision.evidence_refs), source='coordinator_advice', authority='none')), now_iso()))
                return dict(team_id=task['team_id'], advice_id=advice_id, task_id=task['task_id'], revision=task['revision'])
            self.runtime.repo._write(self.actor, 'team_internal_answer_recorded', advice_id, dict(advice=decision.model_dump()),
                                     lambda: self.runtime._owner(self.actor, run_id, epoch=epoch), save)
            return self.runtime.resume(self.actor, task['task_id'], expected_version=task['state_version'], control_epoch=epoch, command_id=advice_id + '-resume')
        if decision.action == 'decision_required':
            dispute = self.disputes.get(self.actor, decision.dispute_id)
            if dispute['team_run_id'] != run_id:
                raise TeamStateError('DISPUTE_NOT_ACCESSIBLE', decision.dispute_id)
            return self.disputes.resolve(self.actor, decision.dispute_id, outcome='decision_required', evidence_ids=list(decision.evidence_refs),
                       explanation=decision.reason, conflicting_constraints=list(decision.conflicting_constraints), choices=list(decision.choices),
                       expected_version=dispute['version'], control_epoch=epoch, command_id=_id('coord-decision'))
        return dict(state='waiting', reason=decision.reason, user_action_required=False)

    async def _root(self, run_id, epoch):
        policy = self.missions.policy(run_id)
        handler = self.validators.goals.get(policy.root_validator) if policy.automatic_verification else None
        if not handler:
            return dict(state='verifying', reason='ROOT_REVIEW_REQUIRED', execution_complete=False)
        snapshot = self.runtime.snapshot(self.actor, run_id)
        mission = self.missions.view(self.actor, run_id)
        before_hash = self.missions.state_hash(run_id)
        artifacts = [self.runtime.artifact(self.actor, a['version_id']) for t in snapshot['tasks'] for h in t['handoffs']
                     if h['revision'] == t['revision'] and h['state'] == 'published' for a in h['packet']['artifacts']]
        verdict = VerificationResult.model_validate(await self.validators.call(handler, dict(stage='goal', mission=mission, snapshot=snapshot, artifacts=artifacts)))
        self._guard(run_id, epoch)
        if self.missions.state_hash(run_id) != before_hash:
            raise TeamStateError('GOAL_REVISION_MISMATCH', 'root evidence changed while the independent check was running')
        self.missions.review_goal(self.actor, run_id, scores=[s.model_dump() for s in verdict.scores], accepted=verdict.accepted,
                                   expected_goal_version=mission['version'], expected_plan_revision=snapshot['run']['task_set_revision'],
                                   control_epoch=epoch, command_id=_id('auto-goal-review'))
        if not verdict.accepted:
            return dict(state='verifying', reason='ROOT_ACCEPTANCE_FAILED', detail=verdict.detail, execution_complete=False)
        return self.runtime.finalize(self.actor, run_id, expected_version=self.runtime._runtime_run(run_id)['state_version'],
                                     control_epoch=epoch, evidence=verdict.detail, command_id=_id('auto-finalize'))

    async def step(self, run_id, *, control_epoch, command_id):
        claim = self._claim_turn(run_id, control_epoch, command_id)
        with self.runtime.lock:
            previous = self.db.execute('SELECT state,result_json FROM team_v2_coordinator_turns WHERE turn_id=?', (claim['turn_id'],)).fetchone()
        if previous['state'] != 'running':
            return json.loads(previous['result_json'])
        def consume():
            self._guard(run_id, control_epoch)
            changed = self.db.execute('UPDATE team_v2_coordinator_turns SET decision_json=? WHERE turn_id=? AND state=\'running\' AND decision_json IS NULL',
                                      (canonical_json(dict(execution_claim=_id('coord-owner'))), claim['turn_id'])).rowcount
            if changed != 1:
                raise TeamStateError('COORDINATOR_BUSY', 'this exact command is already executing')
            return dict(team_id=claim['team_id'], turn_id=claim['turn_id'], execution_claimed=True)
        self.runtime.repo._write(self.actor, 'team_coordinator_claimed', _id('coord-consume'), dict(turn_id=claim['turn_id']),
                                 lambda: self.runtime._owner(self.actor, run_id, epoch=control_epoch), consume)
        result = dict(state='waiting', actions=[])
        try:
            async with asyncio.timeout(self.max_step_seconds):
                with self.runtime.lock:
                    initial_plan_needed = self.missions.policy(run_id).plan_before_execution and claim['plan_revision'] == 1 and not self.db.execute(
                            'SELECT 1 FROM team_v2_attempts a JOIN team_v2_tasks t USING(task_id) WHERE t.team_run_id=? LIMIT 1', (run_id,)).fetchone()
                    changed_goal_needs_plan = claim['goal_version'] > 1 and not self.db.execute(
                            "SELECT 1 FROM team_v2_plan_proposals WHERE team_run_id=? AND goal_version=? AND state='applied' LIMIT 1", (run_id, claim['goal_version'])).fetchone()
                if initial_plan_needed or changed_goal_needs_plan:
                    context = dict(self._planning_context(run_id), planning_stage='initial_goal_decomposition')
                    decision = await self._planning_reply(run_id, control_epoch, context)
                    result['coordination'] = self._apply_advice(run_id, control_epoch, context, decision)
                    return result
                self.runtime.reap_expired(self.actor, run_id, command_id=claim['turn_id'] + '-reap')
                result['actions'].extend(await self._reviews(run_id, control_epoch))
                result['actions'].extend(await self._receipts(run_id, control_epoch))
                result['actions'].extend(self._recover_mechanical(run_id, control_epoch))
                self._guard(run_id, control_epoch)
                reroute = self._recover_routes(run_id, control_epoch)
                if reroute:
                    result['actions'].append(reroute)
                result['execution'] = await self.pool.run_ready(self.actor, run_id)
                self._guard(run_id, control_epoch)
                result['actions'].extend(await self._reviews(run_id, control_epoch))
                result['actions'].extend(await self._receipts(run_id, control_epoch))
                result['actions'].extend(self._recover_mechanical(run_id, control_epoch))
                snapshot = self.runtime.snapshot(self.actor, run_id)
                result['state'] = snapshot['run']['state']
                if snapshot['run']['state'] == 'verifying':
                    result['root'] = await self._root(run_id, control_epoch)
                    result['state'] = result['root']['state']
                    if result['root'].get('reason') == 'ROOT_ACCEPTANCE_FAILED' and self.missions.policy(run_id).internal_replan:
                        context = self._planning_context(run_id)
                        decision = await self._planning_reply(run_id, control_epoch, context)
                        result['coordination'] = self._apply_advice(run_id, control_epoch, context, decision)
                        if result['coordination'].get('state') == 'applied':
                            result['root']['reason'] = None
                elif not any(t['state'] in {'queued', 'running', 'verifying'} for t in snapshot['tasks']):
                    blocked = [t for t in snapshot['tasks'] if t['state'] in {'blocked', 'expired'}]
                    if blocked and not any(t['held_tokens'] or t['block_reason'] in {'effect_unknown', 'user_paused'} for t in blocked):
                        context = self._planning_context(run_id)
                        decision = await self._planning_reply(run_id, control_epoch, context)
                        result['coordination'] = self._apply_advice(run_id, control_epoch, context, decision)
                    elif blocked:
                        result['reason'] = 'RECONCILIATION_OR_EXPLICIT_RESUME_REQUIRED'
                    elif any(t['state'] == 'waiting_inputs' for t in snapshot['tasks']):
                        result['reason'] = 'WAITING_FOR_INPUT_OR_COMPATIBILITY_RECEIPT'
                elif any(t['state'] == 'verifying' for t in snapshot['tasks']) and not result['execution']['attempts']:
                    result['reason'] = 'INDEPENDENT_VERIFICATION_REQUIRED'
                result['completed'] = self.runtime._runtime_run(run_id)['state'] == 'completed'
                if not result['completed'] and (result['execution']['unavailable'] or result['execution']['attempts']) and all(
                        attempt.get('state') == 'not_started' for attempt in result['execution']['attempts']):
                    result.setdefault('reason', result['execution']['attempts'][0]['reason'] if result['execution']['attempts'] else 'MODEL_ROUTE_UNAVAILABLE')
        except Exception as exc:
            result.update(state=self.runtime._runtime_run(run_id)['state'], completed=False,
                          reason=exc.code if isinstance(exc, TeamStateError) else type(exc).__name__)
        finally:
            self._finish_turn(claim, result)
        return result

    async def run_until_idle(self, run_id, *, control_epoch, max_steps=16):
        if type(max_steps) is not int or not 1 <= max_steps <= 64:
            raise ValueError('max_steps must be 1..64')
        results = []
        stagnant = 0
        for _ in range(max_steps):
            self._guard(run_id, control_epoch)
            before = self.missions.state_hash(run_id)
            result = await self.step(run_id, control_epoch=control_epoch, command_id=_id('coordinator-step'))
            results.append(result)
            if result.get('completed') or result.get('reason') or result.get('root', {}).get('reason'):
                break
            after = self.missions.state_hash(run_id)
            stagnant = stagnant + 1 if before == after else 0
            if stagnant >= self.missions.policy(run_id).stagnation_rounds:
                result.update(reason='NO_PROGRESS_REPLAN_REQUIRED', completed=False)
                break
            if result.get('coordination', {}).get('state') in {'proposal_pending', 'waiting', 'decision_required', 'no_progress'}:
                break
        final = self.runtime.snapshot(self.actor, run_id)['run']
        return dict(team_run_id=run_id, state=final['state'], completed=final['state'] == 'completed', steps=results,
                    bounded_pause=final['state'] != 'completed', budget_exhausted=any('BUDGET' in str(r.get('reason', '')) for r in results))

    def recover_turn(self, run_id, *, turn_id, evidence, control_epoch, command_id):
        evidence = _text(evidence)
        def action():
            self.runtime._owner(self.actor, run_id, epoch=control_epoch)
            turn = self.db.execute('SELECT * FROM team_v2_coordinator_turns WHERE turn_id=? AND team_run_id=?', (turn_id, run_id)).fetchone()
            if not turn or turn['state'] != 'running':
                raise TeamStateError('COORDINATOR_NOT_RUNNING', turn_id)
            age = self.runtime.clock() - datetime.fromisoformat(turn['created_at'].replace('Z', '+00:00')).timestamp()
            if age < self.max_step_seconds:
                raise TeamStateError('COORDINATOR_STILL_LIVE', 'wait for the bounded turn or explicitly fence/cancel the run')
            self.db.execute("UPDATE team_v2_coordinator_turns SET state='recovered',result_json=?,finished_at=? WHERE turn_id=?",
                            (canonical_json(dict(evidence=evidence, completion_inferred=False)), now_iso(), turn_id))
            self.db.execute("UPDATE team_v2_coord_model_calls SET state='interrupted' WHERE team_run_id=? AND state='running'", (run_id,))
            return dict(team_id=self.runtime._run(run_id)['team_id'], turn_id=turn_id, state='recovered', usage_reconciled=False)
        self.actor.require('team.recover')
        return self.runtime.repo._write(self.actor, 'team_coordinator_recovered', command_id, dict(run_id=run_id, turn_id=turn_id, evidence=evidence, epoch=control_epoch),
                                        lambda: self.runtime._owner(self.actor, run_id, epoch=control_epoch), action)

    def resolve_usage(self, run_id, *, call_id, usage_tokens, evidence, control_epoch, command_id):
        evidence = _text(evidence)
        if type(usage_tokens) is not int or usage_tokens < 0:
            raise ValueError('usage_tokens must be nonnegative')
        def action():
            row = self.db.execute('SELECT * FROM team_v2_coord_model_calls WHERE call_id=? AND team_run_id=?', (call_id, run_id)).fetchone()
            if not row or row['state'] == 'running' or row['usage_tokens'] is not None:
                raise TeamStateError('USAGE_NOT_RECONCILABLE', call_id)
            budget_ref = self.runtime._runtime_run(run_id)['budget_ref']
            self.db.execute('UPDATE team_v2_budgets SET reserved_tokens=reserved_tokens-?,used_tokens=used_tokens+? WHERE project_id=? AND budget_ref=?',
                            (row['reserved_tokens'], usage_tokens, self.actor.project_id, budget_ref))
            self.db.execute("UPDATE team_v2_coord_model_calls SET usage_tokens=?,state='reconciled',response_json=? WHERE call_id=?", (usage_tokens, canonical_json(dict(evidence=evidence)), call_id))
            return dict(team_id=self.runtime._run(run_id)['team_id'], call_id=call_id, usage_tokens=usage_tokens, state='reconciled')
        self.actor.require('team.recover')
        return self.runtime.repo._write(self.actor, 'team_coord_usage_reconciled', command_id, dict(run_id=run_id, call_id=call_id, usage_tokens=usage_tokens, evidence=evidence, epoch=control_epoch),
                                        lambda: self.runtime._owner(self.actor, run_id, epoch=control_epoch), action)
