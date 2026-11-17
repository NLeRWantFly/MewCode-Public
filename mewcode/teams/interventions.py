"""Versioned human interventions with explicit receipt and effect semantics."""
from __future__ import annotations

import json
import re

from mewcode.mcp.coord_protocol import canonical_json, now_iso
from .orchestration_contracts import Intervention, MissionContract, Responsibility
from .responsibility import MissionService
from .spec import TaskTemplate
from .store_v2 import TeamStateError, _text


def dependency_closure(runtime, run_id, task_id=None):
    """Include waiting/unmaterialized dependants, not only consumed artifacts."""
    compiled = runtime._compiled(run_id)
    ids = {r['task_key']: r['task_id'] for r in runtime.db.execute(
        'SELECT task_key,task_id FROM team_v2_tasks WHERE team_run_id=?', (run_id,))}
    if task_id is None:
        return [ids[key] for key in compiled.task_order if key in ids]
    start = runtime._task(task_id)
    if start['team_run_id'] != run_id or start['task_key'] not in compiled.task_order:
        raise TeamStateError('TASK_NOT_ACCESSIBLE', task_id)
    seen = {start['task_key']}
    for key in compiled.task_order:
        if key in seen:
            seen.update(e.target.task_key for e in compiled.spec.connections
                        if e.source.task_key == key and e.target.task_key is not None)
    return [ids[key] for key in compiled.task_order if key in seen]


def validate_task_refinement(old, new):
    """A local intervention may narrow work, never silently weaken its checks."""
    if (new.task_key != old.task_key or new.inputs != old.inputs or new.outputs != old.outputs
            or new.role_id != old.role_id or new.reviewer_role != old.reviewer_role
            or new.required != old.required or new.token_budget > old.token_budget
            or not set(new.input_scope) <= set(old.input_scope)
            or not set(new.required_actions) <= set(old.required_actions)
            or not set(old.forbidden_changes) <= set(new.forbidden_changes)
            or new.verification != old.verification):
        raise TeamStateError('NEW_PLAN_REQUIRED', 'topology, roles and authority require a checked plan proposal')
    criteria = {c.criterion_id: c for c in new.acceptance}
    if any(c.criterion_id not in criteria or criteria[c.criterion_id].description != c.description
           or criteria[c.criterion_id].minimum_score < c.minimum_score for c in old.acceptance):
        raise TeamStateError('ACCEPTANCE_WEAKENED', 'change the root goal explicitly before changing success criteria')


class InterventionService:
    def __init__(self, runtime):
        self.runtime, self.db = runtime, runtime.db
        self.missions = MissionService(runtime)

    @staticmethod
    def infer_intent(message):
        text = message.strip()
        if re.fullmatch(r'(暂停|暂停任务|pause)[。.!！]?', text, re.IGNORECASE):
            return 'pause'
        if re.fullmatch(r'(取消任务|取消本次运行|cancel run)[。.!！]?', text, re.IGNORECASE):
            return 'cancel'
        if re.match(r'^(补充资料|补充信息|information)\s*[:：]', text, re.IGNORECASE):
            return 'information'
        if re.search(r'(进度|进展|状态|谁负责|status|progress)', text, re.IGNORECASE) or text.endswith(('?', '？')):
            return 'question'
        # Unknown free text is queued for interpretation, never silently applied.
        return 'auto'

    def _row(self, actor, intervention_id, *, epoch=None):
        row = self.db.execute('SELECT * FROM team_v2_interventions WHERE intervention_id=?', (intervention_id,)).fetchone()
        if not row:
            raise TeamStateError('INTERVENTION_NOT_ACCESSIBLE', intervention_id)
        self.runtime._owner(actor, row['team_run_id'], epoch=epoch)
        return row

    def _versions(self, run_id, request):
        mission = self.missions._row(run_id)
        run = json.loads(self.runtime._run(run_id)['run_json'])
        if mission['version'] != request.expected_goal_version or run['task_set_revision'] != request.expected_plan_revision:
            raise TeamStateError('INTERVENTION_STALE', 'goal or plan changed; inspect the current impact before applying')
        if request.task_id:
            task = self.runtime._task(request.task_id)
            if task['team_run_id'] != run_id or request.task_revision != task['revision']:
                raise TeamStateError('INTERVENTION_STALE', 'target task revision changed')

    def _receipt(self, row):
        return dict(intervention_id=row['intervention_id'], intent=row['intent'], state=row['state'],
                    receipt=json.loads(row['receipt_json']), task_id=row['task_id'],
                    request=json.loads(row['request_json']),
                    deliveries=[dict(r) for r in self.db.execute('SELECT attempt_id,task_id,revision,created_at FROM team_v2_intervention_deliveries WHERE intervention_id=? ORDER BY rowid DESC LIMIT 32', (row['intervention_id'],))],
                    created_at=row['created_at'], updated_at=row['updated_at'])

    def list(self, actor, run_id):
        with self.runtime.lock:
            self.runtime._owner(actor, run_id)
            return [self._receipt(row) for row in self.db.execute(
                'SELECT * FROM team_v2_interventions WHERE team_run_id=? ORDER BY created_at,rowid', (run_id,))]

    def _status(self, actor, run_id):
        snapshot = self.runtime.snapshot(actor, run_id)
        blocked = [dict(task_id=t['task_id'], task_key=t['task_key'], reason=t['block_reason'])
                   for t in snapshot['tasks'] if t['state'] in {'blocked', 'expired'}]
        return dict(owner_ref=snapshot['team']['owner_ref'], state=snapshot['run']['state'],
                    completed=sum(t['state'] == 'completed' for t in snapshot['tasks']),
                    tasks=len(snapshot['tasks']), blockers=blocked,
                    text='当前运行：' + snapshot['run']['state'] + '；已验收 ' +
                         str(sum(t['state'] == 'completed' for t in snapshot['tasks'])) + '/' + str(len(snapshot['tasks'])) + ' 项。')

    def submit(self, actor, run_id, *, request, control_epoch, command_id):
        from .runtime import _id
        request = Intervention.model_validate(request)
        def action():
            team = self.runtime._owner(actor, run_id, epoch=control_epoch)
            self._versions(run_id, request)
            intent = self.infer_intent(request.message) if request.intent == 'auto' else request.intent
            state = 'needs_clarification' if intent == 'auto' else 'pending'
            receipt = dict(received=True, understood=intent != 'auto', effective=False, execution_started=False)
            if intent == 'question':
                state = 'answered'
                receipt.update(answer=self._status(actor, run_id), effect='read_only')
            elif intent == 'information':
                state = 'available'
                receipt.update(effect='available_to_next_attempt', note='补充材料仅作为资料，不更改目标、权限或当前执行版本。')
            elif intent == 'auto':
                receipt.update(note='尚未确定这条消息是补充资料还是变更请求；未改变执行。')
            else:
                if intent in {'change_task', 'pause'} and request.task_id:
                    affected = dependency_closure(self.runtime, run_id, request.task_id)
                else:
                    affected = dependency_closure(self.runtime, run_id)
                receipt.update(affected_task_ids=affected, note='待生效；影响范围已列出，旧成果会保留。')
            intervention_id, now = _id('intervention'), now_iso()
            self.db.execute('INSERT INTO team_v2_interventions VALUES(?,?,?,?,?,?,?,?,?,?)',
                            (intervention_id, run_id, request.task_id, actor.principal_ref, intent, state,
                             request.model_dump_json(), canonical_json(receipt), now, now))
            result = dict(team_id=team['team_id'], team_run_id=run_id, intervention_id=intervention_id,
                          state=state, intent=intent, receipt=receipt)
            if request.confirmed and intent in {'change_goal', 'change_task', 'pause', 'cancel'}:
                result = self._apply(actor, self.db.execute('SELECT * FROM team_v2_interventions WHERE intervention_id=?', (intervention_id,)).fetchone(),
                                     request, control_epoch)
            return result
        return self.runtime.repo._write(actor, 'team_intervention_received', command_id,
                                        dict(run_id=run_id, request=request.model_dump(), epoch=control_epoch),
                                        lambda: self.runtime._owner(actor, run_id, epoch=control_epoch), action)

    def _new_revisions(self, affected, *, new_contract=None, first_id=None):
        # Work is revised in dependency order. Old versions remain immutable.
        for task_id in affected:
            task = self.runtime._task(task_id)
            if task['block_reason'] == 'effect_unknown' and not task['held_tokens']:
                raise TeamStateError('NEEDS_RECONCILIATION', 'resolve unknown external effects before a new intervention')
            overrides = {}
            for slot in self.runtime._inputs(task_id, task['revision']):
                if slot['source_task_id'] in affected:
                    overrides[slot['port_id']] = None
            self.runtime._new_revision(task, overrides, new_contract if task_id == first_id else None)

    def clarify(self, actor, intervention_id, *, request, explanation, control_epoch, command_id):
        request = Intervention.model_validate(request)
        explanation = _text(explanation)
        if request.confirmed:
            raise TeamStateError('APPROVAL_REQUIRED', 'clarifying intent cannot also authorize execution')
        def action():
            row = self._row(actor, intervention_id, epoch=control_epoch)
            if row['state'] not in {'needs_clarification', 'interpreting'}:
                raise TeamStateError('INTERVENTION_NOT_APPLICABLE', row['state'])
            original = Intervention.model_validate_json(row['request_json'])
            if request.message != original.message or request.expected_goal_version != original.expected_goal_version or request.expected_plan_revision != original.expected_plan_revision:
                raise TeamStateError('INTERVENTION_REWRITTEN', 'preserve the original user message and its pinned goal/plan')
            self._versions(row['team_run_id'], request)
            state = 'needs_clarification' if request.intent == 'auto' else 'pending'
            receipt = dict(received=True, understood=request.intent != 'auto', effective=False, execution_started=False,
                           interpretation=explanation, interpretation_is_proposal=True)
            if request.intent == 'question':
                state = 'answered'
                receipt.update(answer=self._status(actor, row['team_run_id']), effect='read_only')
            elif request.intent == 'information':
                state = 'available'
                receipt.update(effect='available_to_next_attempt', note='原始消息作为资料送入后续尝试，不改变权限或当前版本。')
            elif request.intent != 'auto':
                affected = dependency_closure(self.runtime, row['team_run_id'], request.task_id if request.intent in {'change_task', 'pause'} else None)
                receipt.update(affected_task_ids=affected, note='意图已形成结构化提案，等待你确认具体影响后生效。')
                if request.intent == 'change_task':
                    if not request.task_id or not request.contract:
                        raise TeamStateError('TASK_CHANGE_REQUIRED', 'an exact contract and task must be supplied')
                    validate_task_refinement(TaskTemplate.model_validate_json(self.runtime._task(request.task_id)['contract_json']), TaskTemplate.model_validate(request.contract))
                if request.intent == 'change_goal' and request.mission is None:
                    raise TeamStateError('GOAL_CHANGE_REQUIRED', 'an exact root goal proposal is required')
            self.db.execute('UPDATE team_v2_interventions SET intent=?,state=?,task_id=?,request_json=?,receipt_json=?,updated_at=? WHERE intervention_id=?',
                            (request.intent, state, request.task_id, request.model_dump_json(), canonical_json(receipt), now_iso(), intervention_id))
            return dict(team_id=self.runtime._run(row['team_run_id'])['team_id'], **self._receipt(self._row(actor, intervention_id)))
        return self.runtime.repo._write(actor, 'team_intervention_clarified', command_id,
                                        dict(intervention_id=intervention_id, request=request.model_dump(), explanation=explanation, epoch=control_epoch),
                                        lambda: self._row(actor, intervention_id, epoch=control_epoch), action)

    def _apply(self, actor, row, request, epoch):
        self._versions(row['team_run_id'], request)
        team = self.runtime._editable(actor, row['team_run_id'], epoch)
        run_id, intent = row['team_run_id'], row['intent']
        affected = dependency_closure(self.runtime, run_id, request.task_id if intent in {'change_task', 'pause'} else None)
        if intent == 'change_task':
            if not request.task_id or request.contract is None:
                raise TeamStateError('TASK_CHANGE_REQUIRED', 'provide the exact target task and revised contract')
            old = TaskTemplate.model_validate_json(self.runtime._task(request.task_id)['contract_json'])
            new = TaskTemplate.model_validate(request.contract)
            validate_task_refinement(old, new)
            self._new_revisions(affected, new_contract=new, first_id=request.task_id)
            self.runtime._refresh_run(run_id)
        elif intent == 'change_goal':
            if request.mission is None:
                raise TeamStateError('GOAL_CHANGE_REQUIRED', 'provide the revised objective, hard constraints and success criteria')
            mission = self.missions._row(run_id)
            new_version = mission['version'] + 1
            # Changing the clause set requires an explicit complete new mapping;
            # the host never silently drops responsibility for a new criterion.
            assignments = request.assignments if request.assignments is not None else [json.loads(r[0]) for r in self.db.execute(
                'SELECT assignment_json FROM team_v2_responsibilities WHERE team_run_id=?', (run_id,))]
            checked = self.missions.validate_assignments(self.runtime._compiled(run_id), request.mission, assignments)
            self.db.execute('INSERT INTO team_v2_mission_revisions VALUES(?,?,?,?,?,?)',
                            (run_id, new_version, request.mission.model_dump_json(), actor.principal_ref, request.message, now_iso()))
            self.db.execute('UPDATE team_v2_missions SET version=?,contract_json=?,state_version=state_version+1,updated_at=? WHERE team_run_id=?',
                            (new_version, request.mission.model_dump_json(), now_iso(), run_id))
            self.missions._save_assignments(run_id, new_version, checked)
            run = json.loads(self.runtime._run(run_id)['run_json'])
            run['objective'] = request.mission.objective
            self.db.execute('UPDATE team_v2_runs SET run_json=? WHERE team_run_id=?', (canonical_json(run), run_id))
            self._new_revisions(affected)
            if self.db.execute('SELECT 1 FROM team_v2_runtime_runs WHERE team_run_id=?', (run_id,)).fetchone():
                self.runtime._refresh_run(run_id)
        elif intent == 'pause':
            for task_id in affected:
                task = self.runtime._task(task_id)
                if task['state'] in {'completed', 'cancelled', 'failed'}:
                    continue
                if task['state'] == 'running':
                    self.runtime._invalidate_attempt(task, 'effect_unknown')
                else:
                    self.runtime._state(task_id, 'blocked', 'user_paused')
            self.runtime._refresh_run(run_id)
        elif intent == 'cancel':
            for task_id in affected:
                task = self.runtime._task(task_id)
                if task['state'] not in {'completed', 'cancelled', 'failed'}:
                    self.runtime._invalidate_attempt(task, 'cancelled')
                    self.runtime._state(task_id, 'cancelled', request.message)
            self.db.execute('UPDATE team_v2_runtime_runs SET export_json=NULL WHERE team_run_id=?', (run_id,))
            self.runtime._run_state(run_id, 'cancelled')
        else:
            raise TeamStateError('INTERVENTION_NOT_APPLICABLE', intent)
        receipt = dict(received=True, understood=True, effective=True, affected_task_ids=affected,
                       goal_version=self.missions._row(run_id)['version'], execution_started=False,
                       effect='versioned_change' if intent.startswith('change_') else intent,
                       note='已生效。旧执行结果不能提交到新修订；未决用量或副作用仍需对账。')
        self.db.execute("UPDATE team_v2_interventions SET state='applied',receipt_json=?,updated_at=? WHERE intervention_id=?",
                        (canonical_json(receipt), now_iso(), row['intervention_id']))
        return dict(team_id=team['team_id'], team_run_id=run_id, intervention_id=row['intervention_id'],
                    intent=intent, state='applied', receipt=receipt)

    def apply(self, actor, intervention_id, *, approved, control_epoch, command_id):
        if approved is not True:
            raise TeamStateError('APPROVAL_REQUIRED', 'confirm the exact intervention and impact')
        def action():
            row = self._row(actor, intervention_id, epoch=control_epoch)
            if row['state'] == 'applied':
                return dict(team_id=self.runtime._run(row['team_run_id'])['team_id'], **self._receipt(row))
            if row['state'] != 'pending':
                raise TeamStateError('INTERVENTION_NOT_APPLICABLE', row['state'])
            return self._apply(actor, row, Intervention.model_validate_json(row['request_json']), control_epoch)
        return self.runtime.repo._write(actor, 'team_intervention_applied', command_id,
                                        dict(intervention_id=intervention_id, approved=approved, epoch=control_epoch),
                                        lambda: self._row(actor, intervention_id, epoch=control_epoch), action)

    def dismiss(self, actor, intervention_id, *, reason, control_epoch, command_id):
        reason = _text(reason)
        def action():
            row = self._row(actor, intervention_id, epoch=control_epoch)
            if row['state'] in {'applied', 'answered', 'dismissed'}:
                raise TeamStateError('INTERVENTION_NOT_APPLICABLE', row['state'])
            receipt = json.loads(row['receipt_json'])
            receipt.update(effective=False, reason=reason)
            self.db.execute("UPDATE team_v2_interventions SET state='dismissed',receipt_json=?,updated_at=? WHERE intervention_id=?",
                            (canonical_json(receipt), now_iso(), intervention_id))
            return dict(team_id=self.runtime._run(row['team_run_id'])['team_id'], intervention_id=intervention_id,
                        state='dismissed', receipt=receipt)
        return self.runtime.repo._write(actor, 'team_intervention_dismissed', command_id,
                                        dict(intervention_id=intervention_id, reason=reason, epoch=control_epoch),
                                        lambda: self._row(actor, intervention_id, epoch=control_epoch), action)

    def information_for_task(self, actor, task_id):
        with self.runtime.lock:
            task = self.runtime._task_access(actor, task_id)
            values = []
            for row in self.db.execute("SELECT * FROM team_v2_interventions WHERE team_run_id=? AND intent='information' AND state='available' AND (task_id=? OR task_id IS NULL) ORDER BY created_at,rowid",
                                       (task['team_run_id'], task_id)):
                request = Intervention.model_validate_json(row['request_json'])
                if request.expected_goal_version == self.missions._row(task['team_run_id'])['version'] and (
                        not request.task_id or request.task_revision == task['revision']):
                    values.append(dict(intervention_id=row['intervention_id'], text=request.message, source='user_information',
                                       trust='data_only', goal_version=request.expected_goal_version))
            return values
