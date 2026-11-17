"""Optional, explicitly invoked model interpretation without control authority."""
from __future__ import annotations

import json
from typing import Literal

from mewcode.mcp.coord_protocol import canonical_json, now_iso
from .interventions import InterventionService
from .orchestration_contracts import Intervention
from .runtime import _id
from .spec import ContractModel, Text
from .store_v2 import TeamStateError


class IntentInterpretation(ContractModel):
    intent: Literal['question', 'information', 'change_goal', 'change_task', 'pause', 'cancel', 'auto']
    explanation: Text
    task_id: Text | None = None
    new_objective: Text | None = None
    needs_contract_editor: bool = False


class InterventionInterpreter:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.runtime, self.actor = coordinator.runtime, coordinator.actor
        self.service = InterventionService(self.runtime)

    async def interpret(self, run_id, *, intervention_id, control_epoch, command_id):
        with self.runtime.lock:
            row = self.service._row(self.actor, intervention_id, epoch=control_epoch)
            if row['team_run_id'] != run_id:
                raise TeamStateError('INTERVENTION_NOT_ACCESSIBLE', intervention_id)
            if row['state'] != 'needs_clarification':
                # Repeated clicks never spend another model budget after a
                # completed interpretation or while one is already running.
                if row['state'] == 'interpreting':
                    raise TeamStateError('INTERPRETATION_BUSY', 'the same message is already being interpreted')
                return dict(team_id=self.runtime._run(run_id)['team_id'], **self.service._receipt(row), execution_started=False)
            original = Intervention.model_validate_json(row['request_json'])
            self.service._versions(run_id, original)
            mission = self.coordinator.missions.view(self.actor, run_id)
            tasks = self.runtime.snapshot(self.actor, run_id)['tasks']
            context = dict(operation='interpret_user_message_without_applying_changes', message=original.message,
                           selected_task=original.task_id, goal=mission['contract'], goal_version=mission['version'],
                           tasks=[dict(task_id=t['task_id'], task_key=t['task_key'], revision=t['revision'], objective=t['contract']['objective']) for t in tasks],
                           instructions='Classify the original user message. Negated actions are not authorization. Do not invent missing intent. '
                           'For an objective change, propose only the requested new objective; no other contract fields can change here. '
                           'If criteria, permissions, budget, or hard constraints must change, set needs_contract_editor=true and intent=auto. '
                           'A question returns host facts, information remains the ORIGINAL message as data, and control changes always need user confirmation.',
                           response_schema=IntentInterpretation.model_json_schema())
        def claim():
            current = self.service._row(self.actor, intervention_id, epoch=control_epoch)
            if current['state'] != 'needs_clarification' or current['request_json'] != row['request_json']:
                raise TeamStateError('INTERPRETATION_BUSY', 'message state changed')
            self.runtime.db.execute("UPDATE team_v2_interventions SET state='interpreting',updated_at=? WHERE intervention_id=?", (now_iso(), intervention_id))
        try:
            interpretation = await self.coordinator._planning_reply(run_id, control_epoch, context, schema_type=IntentInterpretation,
                                      ignore_intervention=intervention_id, manual=True, before_reserve=claim)
            intent = 'auto' if interpretation.needs_contract_editor else interpretation.intent
            target = original.task_id or interpretation.task_id
            task = next((t for t in tasks if t['task_id'] == target), None)
            if target and task is None:
                raise TeamStateError('TASK_NOT_ACCESSIBLE', 'interpretation referred to an unsupplied task')
            values = original.model_dump(mode='json')
            values.update(intent=intent, task_id=target, task_revision=task['revision'] if task else None, confirmed=False)
            if intent == 'change_task':
                if not task or not interpretation.new_objective:
                    raise TeamStateError('TASK_CHANGE_REQUIRED', 'an exact task and new objective are required')
                values['contract'] = dict(task['contract'], objective=interpretation.new_objective)
            elif intent == 'change_goal':
                if not interpretation.new_objective:
                    raise TeamStateError('GOAL_CHANGE_REQUIRED', 'a concrete goal proposal is required')
                values['mission'] = dict(mission['contract'], objective=interpretation.new_objective)
            result = self.service.clarify(self.actor, intervention_id, request=values, explanation=interpretation.explanation,
                                           control_epoch=control_epoch, command_id=command_id)
            return dict(result, model_interpretation=True, control_change_applied=False)
        except BaseException:
            def restore():
                current = self.runtime.db.execute('SELECT * FROM team_v2_interventions WHERE intervention_id=?', (intervention_id,)).fetchone()
                if current and current['state'] == 'interpreting':
                    receipt = json.loads(current['receipt_json'])
                    receipt.update(note='解释未能安全完成，原始请求保留，执行没有改变。')
                    self.runtime.db.execute("UPDATE team_v2_interventions SET state='needs_clarification',receipt_json=?,updated_at=? WHERE intervention_id=?",
                                            (canonical_json(receipt), now_iso(), intervention_id))
                return dict(team_id=self.runtime._run(run_id)['team_id'], intervention_id=intervention_id, control_change_applied=False)
            self.runtime.repo._write(self.actor, 'team_interpretation_interrupted', _id('interpret-error'),
                                     dict(intervention_id=intervention_id), lambda: self.actor.require('team.manage'), restore)
            raise
