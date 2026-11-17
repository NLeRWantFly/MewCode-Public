"""Explicit append-only plan revisions using the existing task authority.

Existing roles, input grants, task contracts and target edges cannot change in
this command. Those changes require their own revision workflow or a new team.
"""
from __future__ import annotations

import json
from datetime import datetime, UTC, timedelta

from mewcode.mcp.coord_protocol import now_iso, canonical_json
from .runtime import _id
from .spec import RuntimeBinding, TaskTemplate, TeamRun
from .spec_compiler import validate_compiled_snapshot, validate_runtime_bindings
from .store_v2 import TeamStateError, _text, _digest


def amend_plan(runtime, actor, run_id, *, compiled, expected_version, control_epoch, approved, reason, command_id):
    compiled = validate_compiled_snapshot(compiled)
    reason = _text(reason)
    if approved is not True:
        raise TeamStateError('APPROVAL_REQUIRED', 'new tasks require explicit host plan approval')

    def action():
        team = runtime._editable(actor, run_id, control_epoch)
        current = runtime._runtime_run(run_id)
        if current['state_version'] != expected_version:
            raise TeamStateError('STATE_VERSION_CONFLICT', run_id)
        original = runtime._compiled(run_id)
        old = original.spec
        new = compiled.spec
        if new.spec_id != old.spec_id or new.version <= old.version:
            raise TeamStateError('SPEC_VERSION_CONFLICT', 'an amendment requires a newer version of the same team definition')
        if new.roles != old.roles or new.inputs != old.inputs:
            raise TeamStateError('NEW_TEAM_REQUIRED', 'append-only amendments preserve roles and input grants')
        before, after = original.effective_policy, compiled.effective_policy
        if (not set(after.allowed_actions) <= set(before.allowed_actions)
                or not set(after.allowed_resources) <= set(before.allowed_resources)
                or not set(after.allowed_models) <= set(before.allowed_models)
                or after.token_budget > before.token_budget or after.max_tasks > before.max_tasks
                or after.max_inheritance_depth > before.max_inheritance_depth
                or before.self_acceptance_forbidden and not after.self_acceptance_forbidden):
            raise TeamStateError('PERMISSION_REQUIRED', 'amendments cannot expand deployment authority or budget')
        rows = runtime.db.execute('SELECT task_id,task_key FROM team_v2_tasks WHERE team_run_id=?', (run_id,)).fetchall()
        existing = {row['task_key']: runtime._task(row['task_id']) for row in rows}
        declared = {task.task_key: task for task in new.tasks}
        for key, task in existing.items():
            if key not in declared or declared[key] != TaskTemplate.model_validate_json(task['contract_json']):
                raise TeamStateError('TASK_REVISION_REQUIRED', 'existing contracts must be retained exactly')
        added = [task for task in new.tasks if task.task_key not in existing]
        if not added:
            raise TeamStateError('NO_NEW_TASKS', run_id)
        old_outputs = {port.port_id: port for port in old.outputs}
        if any(next((p for p in new.outputs if p.port_id == key), None) != port for key, port in old_outputs.items()):
            raise TeamStateError('OUTPUT_CONTRACT_MISMATCH', 'existing required exports cannot change')
        old_edges = {edge.target: edge for edge in old.connections}
        new_edges = {edge.target: edge for edge in new.connections}
        if any(new_edges.get(target) != edge for target, edge in old_edges.items()):
            raise TeamStateError('INPUT_REVISION_MISMATCH', 'existing edge bindings cannot change in append-only amendments')
        if any(edge.target.task_key in existing and edge.target not in old_edges for edge in new.connections):
            raise TeamStateError('INPUT_REVISION_MISMATCH', 'existing input slots cannot gain new sources')
        bindings = tuple(RuntimeBinding.model_validate(value) for value in json.loads(current['bindings_json']))
        validate_runtime_bindings(compiled, bindings, team_id=team['team_id'], team_run_id=run_id)
        for task in added:
            producer = [b for b in bindings if b.role_id == task.role_id]
            reviewer = [b for b in bindings if b.role_id == task.reviewer_role]
            if (any(b.kind != 'model' or b.provider_id != 'grok-build' or b.model_id != 'grok-4.6' or b.principal_ref != 'grok-executor-01' for b in producer)
                    or any(b.kind not in {'host', 'human'} or b.principal_ref != actor.principal_ref for b in reviewer)):
                raise TeamStateError('EXECUTION_POLICY_MISMATCH', 'new tasks retain the fixed Grok / host deployment policy')
        budget = runtime.db.execute('SELECT * FROM team_v2_budgets WHERE project_id=? AND budget_ref=?', (actor.project_id, current['budget_ref'])).fetchone()
        if (any(task.token_budget < 1 for task in added)
                or sum(task.token_budget for task in new.tasks) > budget['limit_tokens']
                or budget['used_tokens'] + budget['reserved_tokens'] + sum(task.token_budget for task in added) > budget['limit_tokens']):
            raise TeamStateError('BUDGET_EXCEEDED', 'new tasks must fit the existing explicit shared grant')
        revision = current['task_set_revision'] + 1
        spec_body = new.model_dump(mode='json')
        digest = _digest(spec_body)
        registered = runtime.db.execute('SELECT spec_hash FROM team_v2_specs WHERE project_id=? AND spec_id=? AND version=?', (actor.project_id, new.spec_id, new.version)).fetchone()
        if registered and registered[0] != digest:
            raise TeamStateError('SPEC_VERSION_CONFLICT', new.spec_id)
        if not registered:
            runtime.db.execute('INSERT INTO team_v2_specs VALUES(?,?,?,?,?,?,?)', (actor.project_id, new.spec_id, new.version, canonical_json(spec_body), digest, actor.principal_ref, now_iso()))
        runtime.db.execute('INSERT OR IGNORE INTO team_v2_plan_revisions VALUES(?,?,?,?,?)',
                           (run_id, current['task_set_revision'], original.model_dump_json(), 'Original frozen plan', now_iso()))
        runtime.db.execute('INSERT INTO team_v2_plan_revisions VALUES(?,?,?,?,?)', (run_id, revision, compiled.model_dump_json(), reason, now_iso()))
        ids = {key: task['task_id'] for key, task in existing.items()}
        ids.update({task.task_key: _id('task') for task in added})
        roots = json.loads(current['input_json'])
        for task in added:
            _append_task(runtime, actor, team, run_id, compiled, task, ids, roots, existing)
        run = TeamRun.model_validate_json(runtime._run(run_id)['run_json'])
        run = run.model_copy(update={'task_set_revision': revision, 'resolved_spec_hash': compiled.resolved_spec_hash})
        runtime.db.execute('UPDATE team_v2_runs SET run_json=? WHERE team_run_id=?', (run.model_dump_json(), run_id))
        runtime.db.execute('UPDATE team_v2_runtime_runs SET task_set_revision=?,export_json=NULL WHERE team_run_id=?', (revision, run_id))
        runtime._refresh_run(run_id)
        return dict(team_id=team['team_id'], team_run_id=run_id, task_set_revision=revision,
                    added_task_ids={task.task_key: ids[task.task_key] for task in added}, resolved_spec_hash=compiled.resolved_spec_hash)

    return runtime.repo._write(actor, 'team_plan_amended', command_id,
                                dict(run_id=run_id, compiled=compiled.model_dump(mode='json'), expected_version=expected_version,
                                     control_epoch=control_epoch, approved=approved, reason=reason),
                                lambda: runtime._editable(actor, run_id, control_epoch), action)


def _append_task(runtime, actor, team, run_id, compiled, task, ids, roots, existing):
    task_id = ids[task.task_key]
    deadline = (datetime.now(UTC) + timedelta(days=1)).isoformat().replace('+00:00', 'Z')
    values = dict(task_id=task_id, kind='execution', title=task.task_key, description=task.objective,
                  creator_id=actor.principal_ref, target_agent_id='grok-executor-01', state='waiting_inputs',
                  allowed_tools_json='[]', allowed_skills_json='[]', allowed_paths_json='[]', forbidden_paths_json='[]',
                  write_mode='read_only', capabilities_hash=compiled.resolved_spec_hash,
                  acceptance_criteria_json=canonical_json([c.model_dump() for c in task.acceptance]), max_turns=1,
                  deadline=deadline, last_progress_at=now_iso(), metadata_json=canonical_json(dict(team_v2=True, team_id=team['team_id'], team_run_id=run_id)), created_at=now_iso(), updated_at=now_iso())
    runtime.db.execute(f"INSERT INTO tasks({','.join(values)}) VALUES({','.join('?' for _ in values)})", tuple(values.values()))
    runtime.db.execute('INSERT INTO team_v2_tasks(task_id,team_id,team_run_id,task_key,token_limit) VALUES(?,?,?,?,?)', (task_id, team['team_id'], run_id, task.task_key, task.token_budget))
    runtime.db.execute('INSERT INTO team_v2_task_revisions VALUES(?,1,1,1,?,?,?,?)', (task_id, task.model_dump_json(), '', 'waiting_inputs', now_iso()))
    for port in task.inputs:
        edge = next((e for e in compiled.spec.connections if e.target.task_key == task.task_key and e.target.port_id == port.port_id), None)
        version, receipt = None, 'pending'
        if edge and edge.source.task_key is None:
            version = roots.get(edge.source.port_id)
            receipt = 'accepted' if version else 'pending'
        elif edge and edge.source.task_key in existing:
            producer = existing[edge.source.task_key]
            if producer['state'] == 'completed':
                packet = runtime.db.execute("SELECT packet_json FROM team_v2_handoffs WHERE task_id=? AND revision=? AND state='published'", (producer['task_id'], producer['revision'])).fetchone()
                if packet:
                    version = next((a['version_id'] for a in json.loads(packet[0])['artifacts'] if a['port_id'] == edge.source.port_id), None)
        runtime.db.execute('INSERT INTO team_v2_inputs VALUES(?,1,?,?,?,?,?,?,?,?)',
                            (task_id, port.port_id, port.schema_ref, int(port.required), ids[edge.source.task_key] if edge and edge.source.task_key else None,
                             edge.source.port_id if edge else None, version, receipt, 'Explicit amended-plan root binding' if receipt == 'accepted' else None))
    runtime.db.execute('UPDATE team_v2_task_revisions SET input_hash=? WHERE task_id=? AND revision=1', (runtime._input_hash(task_id, 1), task_id))
    if runtime._ready(runtime._task(task_id)):
        runtime._state(task_id, 'queued')
