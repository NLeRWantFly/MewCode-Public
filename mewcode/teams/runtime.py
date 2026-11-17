"""Transactional team task, handoff, and recovery state machine.

The existing tasks table remains the authoritative current task state. Its v2
extension records immutable revisions, exact input bindings, and evidence.
This module never calls a model or writes a user workspace.
"""
from __future__ import annotations

import base64
import json
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta

from .runtime_protocol import ArtifactContent, HandoffSubmission, LeaseToken, ReviewScore, byte_hash
from .runtime_schema import STATEMENTS
from .orchestration_schema import STATEMENTS as ORCHESTRATION_STATEMENTS
from .runtime_lock import runtime_locked
from .spec import RuntimeBinding, TaskTemplate, TeamRun
from .spec_compiler import validate_compiled_snapshot, validate_runtime_bindings
from .store_v2 import TeamActor, TeamStateError, _digest, _text
from mewcode.mcp.coord_protocol import canonical_json, now_iso


def _id(prefix):
    return prefix + "-" + uuid.uuid4().hex


class TeamRuntime:
    lease_seconds = 45
    max_attempts = 2
    max_attempt_seconds = 130

    def __init__(self, repository, *, read_only=False):
        self.repo = repository
        self.db = repository._conn
        self.lock = repository._lock
        self.clock = time.time
        if read_only:
            versions = self.db.execute("SELECT version FROM team_v2_runtime_schema").fetchall()
            if len(versions) != 1 or versions[0][0] != 1:
                raise TeamStateError("SCHEMA_VERSION_UNSUPPORTED", "team runtime")
            return
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute(STATEMENTS[0])
                versions = self.db.execute("SELECT version FROM team_v2_runtime_schema").fetchall()
                if versions and (len(versions) != 1 or versions[0][0] != 1):
                    raise TeamStateError("SCHEMA_VERSION_UNSUPPORTED", "team runtime")
                for statement in (*STATEMENTS[1:], *ORCHESTRATION_STATEMENTS):
                    self.db.execute(statement)
                if not versions:
                    self.db.execute("INSERT INTO team_v2_runtime_schema VALUES(1)")
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

    @runtime_locked
    def _run(self, run_id):
        row = self.db.execute("SELECT * FROM team_v2_runs WHERE team_run_id=?", (run_id,)).fetchone()
        if row is None:
            raise TeamStateError("RUN_NOT_ACCESSIBLE", run_id)
        return row

    @runtime_locked
    def _owner(self, actor, run_id, *, epoch=None):
        row = self._run(run_id)
        team = self.repo._team(actor, row["team_id"], owner=True)
        if epoch is not None and (type(epoch) is not int or team["control_epoch"] != epoch):
            raise TeamStateError("STALE_CONTROL_EPOCH", run_id)
        return team

    @runtime_locked
    def _compiled(self, run_id):
        row = self._run(run_id)
        run = TeamRun.model_validate_json(row['run_json'])
        revision = self.db.execute('SELECT compiled_json FROM team_v2_plan_revisions WHERE team_run_id=? AND revision=?', (run_id, run.task_set_revision)).fetchone()
        compiled = validate_compiled_snapshot(json.loads(revision[0] if revision else row['compiled_json']))
        if compiled.resolved_spec_hash != run.resolved_spec_hash:
            raise TeamStateError('SNAPSHOT_CORRUPT', 'active plan hash mismatch')
        return compiled

    def amend_plan(self, actor, run_id, *, compiled, expected_version, control_epoch, approved, reason, command_id):
        from .plan_amendment import amend_plan
        return amend_plan(self, actor, run_id, compiled=compiled, expected_version=expected_version,
                           control_epoch=control_epoch, approved=approved, reason=reason, command_id=command_id)

    @runtime_locked
    def _editable(self, actor, run_id, epoch):
        team = self._owner(actor, run_id, epoch=epoch)
        if team['lifecycle'] != 'active' or json.loads(self._run(run_id)['run_json'])['state'] in {'cancelled', 'failed'}:
            raise TeamStateError('RUN_NOT_ACTIVE', run_id)
        return team

    @runtime_locked
    def _runtime_run(self, run_id):
        row = self.db.execute("SELECT * FROM team_v2_runtime_runs WHERE team_run_id=?", (run_id,)).fetchone()
        if row is None:
            raise TeamStateError("RUN_NOT_ACTIVE", run_id)
        return row

    @runtime_locked
    def _task(self, task_id):
        row = self.db.execute("""SELECT v.*,t.state,t.deadline,r.contract_json,r.input_hash
            FROM team_v2_tasks v JOIN tasks t USING(task_id)
            JOIN team_v2_task_revisions r ON r.task_id=v.task_id AND r.revision=v.revision
            WHERE v.task_id=?""", (task_id,)).fetchone()
        if row is None:
            raise TeamStateError("TASK_NOT_ACCESSIBLE", task_id)
        return row

    @runtime_locked
    def _binding(self, task, role):
        bindings = json.loads(self._runtime_run(task["team_run_id"])["bindings_json"])
        return [RuntimeBinding.model_validate(binding) for binding in bindings if binding["role_id"] == role]

    @runtime_locked
    def _task_access(self, actor, task_id, permission="team.read", producer=False, reviewer=False):
        actor.require(permission)
        task = self._task(task_id)
        team = self.repo._team(actor, task["team_id"])
        contract = TaskTemplate.model_validate_json(task["contract_json"])
        if producer:
            allowed = {binding.principal_ref for binding in self._binding(task, contract.role_id)}
        elif reviewer:
            allowed = {binding.principal_ref for binding in self._binding(task, contract.reviewer_role)}
        else:
            active_roles = {(row['role_id'], row['principal_ref']) for row in self.db.execute("""SELECT m.role_id,m.principal_ref FROM team_v2_memberships m
                LEFT JOIN team_v2_member_states s USING(membership_id) WHERE m.team_run_id=? AND COALESCE(s.state,'active')!='left'""", (task['team_run_id'],)).fetchall()}
            allowed = {binding.principal_ref for role in (contract.role_id, contract.reviewer_role)
                       for binding in self._binding(task, role) if (role, binding.principal_ref) in active_roles} | {team["owner_ref"]}
        if actor.principal_ref not in allowed:
            raise TeamStateError("TASK_NOT_ACCESSIBLE", task_id)
        if producer or reviewer:
            role_id = contract.role_id if producer else contract.reviewer_role
            available = self.db.execute("""SELECT 1 FROM team_v2_memberships m LEFT JOIN team_v2_member_states s USING(membership_id)
                WHERE m.team_run_id=? AND m.role_id=? AND m.principal_ref=? AND COALESCE(s.state,'active')!='left'""",
                (task['team_run_id'], role_id, actor.principal_ref)).fetchone()
            if not available:
                raise TeamStateError('MEMBER_UNAVAILABLE', role_id)
        return task

    def set_member_state(self, actor, membership_id, *, state, reason, expected_version, control_epoch, command_id):
        reason = _text(reason)
        if state not in {'active', 'idle', 'left'}:
            raise ValueError('membership state must be active, idle or left')
        def load():
            member = self.db.execute('SELECT * FROM team_v2_memberships WHERE membership_id=?', (membership_id,)).fetchone()
            if member is None:
                raise TeamStateError('MEMBER_NOT_ACCESSIBLE', membership_id)
            return member
        def authorize():
            self._owner(actor, load()['team_run_id'], epoch=control_epoch)
        def action():
            member = load()
            team = self.repo._team(actor, member['team_id'], owner=True)
            self.repo._expected(team, expected_version, control_epoch)
            if team['lifecycle'] == 'closed':
                raise TeamStateError('TEAM_NOT_ACTIVE', member['team_id'])
            self.db.execute('INSERT INTO team_v2_member_states VALUES(?,?,?,?) ON CONFLICT(membership_id) DO UPDATE SET state=excluded.state,reason=excluded.reason,updated_at=excluded.updated_at',
                            (membership_id, state, reason, now_iso()))
            if state == 'left':
                for row in self.db.execute('SELECT task_id FROM team_v2_tasks WHERE team_run_id=?', (member['team_run_id'],)).fetchall():
                    task = self._task(row['task_id'])
                    contract = TaskTemplate.model_validate_json(task['contract_json'])
                    if contract.role_id != member['role_id']:
                        continue
                    if task['state'] == 'running':
                        self._invalidate_attempt(task, 'effect_unknown')
                    elif task['state'] == 'queued':
                        self._state(task['task_id'], 'blocked', 'member_left')
                if self.db.execute('SELECT 1 FROM team_v2_runtime_runs WHERE team_run_id=?', (member['team_run_id'],)).fetchone():
                    self._refresh_run(member['team_run_id'])
            self.db.execute('UPDATE team_v2_instances SET state_version=state_version+1 WHERE team_id=?', (member['team_id'],))
            return dict(team_id=member['team_id'], team_run_id=member['team_run_id'], membership_id=membership_id,
                        state=state, business_acceptance=False, reason=reason)
        return self.repo._write(actor, 'team_member_state_changed', command_id,
                                dict(membership_id=membership_id, state=state, reason=reason, expected_version=expected_version, control_epoch=control_epoch), authorize, action)

    @runtime_locked
    def _inputs(self, task_id, revision):
        return self.db.execute("SELECT * FROM team_v2_inputs WHERE task_id=? AND revision=? ORDER BY port_id",
                               (task_id, revision)).fetchall()

    @runtime_locked
    def _input_hash(self, task_id, revision):
        return _digest({"inputs": [{key: row[key] for key in ("port_id", "schema_ref", "version_id")}
                                   for row in self._inputs(task_id, revision)]})

    def _state(self, task_id, state, reason=None):
        task = self._task(task_id)
        self.db.execute("UPDATE tasks SET state=?,updated_at=? WHERE task_id=?", (state, now_iso(), task_id))
        self.db.execute("UPDATE team_v2_task_revisions SET state=? WHERE task_id=? AND revision=?",
                        (state, task_id, task["revision"]))
        self.db.execute("UPDATE team_v2_tasks SET state_version=state_version+1,block_reason=? WHERE task_id=?",
                        (reason, task_id))

    def _run_state(self, run_id, state):
        self.db.execute("UPDATE team_v2_runtime_runs SET state=?,state_version=state_version+1 WHERE team_run_id=?",
                        (state, run_id))
        value = TeamRun.model_validate_json(self._run(run_id)["run_json"])
        value = value.model_copy(update={"state": state, "state_version": value.state_version + 1})
        self.db.execute("UPDATE team_v2_runs SET run_json=? WHERE team_run_id=?", (value.model_dump_json(), run_id))

    @runtime_locked
    def _ready(self, task):
        for slot in self._inputs(task["task_id"], task["revision"]):
            if slot["version_id"] is None:
                if slot["required"]:
                    return False
                continue
            if slot["receipt"] != "accepted":
                return False
            self._artifact(slot["version_id"], published=True)
        return True

    @runtime_locked
    def _artifact(self, version_id, *, published=False, _visiting=None, _checked=None):
        row = self.db.execute("SELECT * FROM team_v2_artifacts WHERE version_id=?", (version_id,)).fetchone()
        if row is None or (published and row["state"] != "published"):
            raise TeamStateError("ARTIFACT_UNAVAILABLE", version_id)
        if byte_hash(bytes(row["content"])) != row["content_hash"]:
            raise TeamStateError("ARTIFACT_CORRUPT", version_id)
        if published and row['producer_task_id']:
            visiting = set() if _visiting is None else _visiting
            checked = set() if _checked is None else _checked
            if version_id in checked:
                return row
            if version_id in visiting or len(visiting) >= 256:
                raise TeamStateError('PROVENANCE_INVALID', version_id)
            visiting.add(version_id)
            packet = self.db.execute("""SELECT h.revision,h.packet_hash,h.packet_json,h.input_hash FROM team_v2_handoffs h,
                json_each(h.packet_json,'$.artifacts') a WHERE h.task_id=? AND h.state='published'
                AND json_extract(a.value,'$.version_id')=?""", (row['producer_task_id'], version_id)).fetchone()
            if packet is None or _digest(json.loads(packet['packet_json'])) != packet['packet_hash']:
                raise TeamStateError('PROVENANCE_INVALID', version_id)
            if self._input_hash(row['producer_task_id'], packet['revision']) != packet['input_hash']:
                raise TeamStateError('PROVENANCE_INVALID', version_id)
            for slot in self._inputs(row['producer_task_id'], packet['revision']):
                if slot['version_id']:
                    self._artifact(slot['version_id'], published=True, _visiting=visiting, _checked=checked)
            visiting.remove(version_id)
            checked.add(version_id)
        return row

    @runtime_locked
    def _artifact_for_run(self, version_id, run_id):
        artifact = self._artifact(version_id, published=True)
        target = self._run(run_id)
        project = self.db.execute("SELECT project_id FROM team_v2_instances WHERE team_id=?", (target["team_id"],)).fetchone()[0]
        if artifact["project_id"] != project:
            raise TeamStateError("ARTIFACT_NOT_ACCESSIBLE", version_id)
        if artifact["team_run_id"] != run_id and self.db.execute(
                "SELECT 1 FROM team_v2_artifact_grants WHERE version_id=? AND recipient_run_id=?",
                (version_id, run_id)).fetchone() is None:
            raise TeamStateError("ARTIFACT_NOT_ACCESSIBLE", "explicit export grant required")
        return artifact

    def _insert_artifact(self, actor, run_id, content, *, task_id=None, state="staged"):
        content = ArtifactContent.model_validate(content.model_dump() if isinstance(content, ArtifactContent) else content)
        body = content.content()
        version_id = _id("av")
        team_id = self._run(run_id)["team_id"]
        self.db.execute("INSERT INTO team_v2_artifacts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (version_id, actor.project_id, team_id, run_id, task_id, content.port_id,
                         content.schema_ref, content.media_type, body, byte_hash(body), state, now_iso()))
        return {"version_id": version_id, "port_id": content.port_id, "schema_ref": content.schema_ref,
                "media_type": content.media_type, "content_hash": byte_hash(body), "size": len(body)}

    def register_input(self, actor, run_id, content, *, command_id, control_epoch):
        content = ArtifactContent.model_validate(content.model_dump() if isinstance(content, ArtifactContent) else content)
        payload = {"run_id": run_id, "content": content.model_dump(), "epoch": control_epoch}
        def action():
            self._editable(actor, run_id, control_epoch)
            port = next((p for p in self._compiled(run_id).spec.inputs if p.port_id == content.port_id), None)
            if port is None or port.schema_ref != content.schema_ref:
                raise TeamStateError("SCHEMA_MISMATCH", content.port_id)
            return {"team_id": self._run(run_id)["team_id"],
                    **self._insert_artifact(actor, run_id, content, state="published")}
        return self.repo._write(actor, "team_input_registered", command_id, payload,
                                lambda: self._owner(actor, run_id, epoch=control_epoch), action)

    def grant_export(self, actor, version_id, recipient_run_id, *, command_id, control_epoch):
        def authorize():
            row = self._artifact(version_id, published=True)
            self._owner(actor, row["team_run_id"], epoch=control_epoch)
        def action():
            artifact = self._artifact(version_id, published=True)
            target = self._run(recipient_run_id)
            project = self.db.execute("SELECT project_id FROM team_v2_instances WHERE team_id=?", (target["team_id"],)).fetchone()[0]
            if project != actor.project_id:
                raise TeamStateError("CROSS_PROJECT_DENIED", recipient_run_id)
            self.db.execute("INSERT OR IGNORE INTO team_v2_artifact_grants VALUES(?,?,?)",
                            (version_id, recipient_run_id, actor.principal_ref))
            return {"team_id": artifact["team_id"], "version_id": version_id, "recipient_run_id": recipient_run_id}
        return self.repo._write(actor, "team_export_granted", command_id,
                                {"version_id": version_id, "recipient_run_id": recipient_run_id, "epoch": control_epoch}, authorize, action)

    def activate(self, actor, run_id, *, bindings, inputs, budget_limit, expected_version,
                 control_epoch, command_id):
        bindings = tuple(RuntimeBinding.model_validate(b.model_dump() if isinstance(b, RuntimeBinding) else b) for b in bindings)
        payload = dict(run_id=run_id, bindings=[b.model_dump() for b in bindings], inputs=inputs,
                       budget_limit=budget_limit, expected_version=expected_version, epoch=control_epoch)
        def action():
            team = self._owner(actor, run_id, epoch=control_epoch)
            if team["lifecycle"] != "active":
                raise TeamStateError("TEAM_NOT_ACTIVE", run_id)
            run = TeamRun.model_validate_json(self._run(run_id)["run_json"])
            if run.state != "draft" or run.state_version != expected_version:
                raise TeamStateError("STATE_VERSION_CONFLICT", run_id)
            compiled = self._compiled(run_id)
            validate_runtime_bindings(compiled, bindings, team_id=team["team_id"], team_run_id=run_id)
            if type(budget_limit) is not int or not 0 < budget_limit <= compiled.effective_policy.token_budget:
                raise TeamStateError("BUDGET_EXCEEDED", "activation requires an explicit bounded grant")
            if sum(task.token_budget for task in compiled.spec.tasks) > budget_limit:
                raise TeamStateError("BUDGET_EXCEEDED", "task allocation exceeds run grant")
            from .deployment import DeploymentRegistry, legacy_binding_check
            if not DeploymentRegistry(self).validate_bindings(actor, run_id, compiled, bindings):
                legacy_binding_check(actor, bindings, compiled.spec.tasks)
            for task in compiled.spec.tasks:
                if task.token_budget < 1:
                    raise TeamStateError("BUDGET_EXCEEDED", "each model task needs a positive allocation")
            ports = {p.port_id: p for p in compiled.spec.inputs}
            if set(inputs) - ports.keys() or any(p.required and p.port_id not in inputs for p in ports.values()):
                raise TeamStateError("UNBOUND_INPUT", "team input ports")
            for port_id, version in inputs.items():
                if self._artifact_for_run(version, run_id)["schema_ref"] != ports[port_id].schema_ref:
                    raise TeamStateError("SCHEMA_MISMATCH", port_id)
            budget = self.db.execute("SELECT * FROM team_v2_budgets WHERE project_id=? AND budget_ref=?",
                                     (actor.project_id, run.budget_ref)).fetchone()
            if budget and budget["limit_tokens"] != budget_limit:
                raise TeamStateError("BUDGET_GRANT_CONFLICT", run.budget_ref)
            self.db.execute("INSERT OR IGNORE INTO team_v2_budgets(project_id,budget_ref,limit_tokens) VALUES(?,?,?)",
                            (actor.project_id, run.budget_ref, budget_limit))
            self.db.execute("INSERT INTO team_v2_runtime_runs(team_run_id,state,bindings_json,budget_ref,input_json) VALUES(?,?,?,?,?)",
                            (run_id, "running", canonical_json([b.model_dump() for b in bindings]), run.budget_ref, canonical_json(inputs)))
            for binding in bindings:
                previous = self.db.execute("SELECT * FROM team_v2_memberships WHERE team_id=? AND team_run_id=? AND role_id=? AND principal_ref=?",
                                            (team["team_id"], run_id, binding.role_id, binding.principal_ref)).fetchone()
                if not previous:
                    alias = binding.role_id if binding.role_id != "lead" else "role-lead"
                    if self.db.execute("SELECT 1 FROM team_v2_memberships WHERE team_id=? AND team_run_id=? AND alias=?",
                                       (team["team_id"], run_id, alias)).fetchone():
                        alias = binding.binding_id
                    self.db.execute("INSERT INTO team_v2_memberships VALUES(?,?,?,?,?,?)",
                                    (_id("member"), team["team_id"], run_id, alias, binding.role_id, binding.principal_ref))
            ids = {task.task_key: _id("task") for task in compiled.spec.tasks}
            deadline = (datetime.now(UTC) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
            for task in compiled.spec.tasks:
                task_id = ids[task.task_key]
                fields = dict(task_id=task_id, kind="execution", title=task.task_key, description=task.objective,
                              creator_id=actor.principal_ref, target_agent_id=next(b.principal_ref for b in bindings if b.role_id == task.role_id), state="waiting_inputs",
                              allowed_tools_json="[]", allowed_skills_json="[]", allowed_paths_json="[]", forbidden_paths_json="[]",
                              write_mode="read_only", capabilities_hash=compiled.resolved_spec_hash,
                              acceptance_criteria_json=canonical_json([c.model_dump() for c in task.acceptance]),
                              max_turns=1, deadline=deadline, last_progress_at=now_iso(),
                              metadata_json=canonical_json({"team_v2": True, "team_id": team["team_id"], "team_run_id": run_id}),
                              created_at=now_iso(), updated_at=now_iso())
                self.db.execute(f"INSERT INTO tasks({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", tuple(fields.values()))
                self.db.execute("INSERT INTO team_v2_tasks(task_id,team_id,team_run_id,task_key,token_limit) VALUES(?,?,?,?,?)",
                                (task_id, team["team_id"], run_id, task.task_key, task.token_budget))
                self.db.execute("INSERT INTO team_v2_task_revisions VALUES(?,1,1,1,?,?,?,?)",
                                (task_id, task.model_dump_json(), "", "waiting_inputs", now_iso()))
                for port in task.inputs:
                    edge = next((edge for edge in compiled.spec.connections if edge.target.task_key == task.task_key
                                 and edge.target.port_id == port.port_id), None)
                    version = inputs.get(edge.source.port_id) if edge and edge.source.task_key is None else None
                    self.db.execute("""INSERT INTO team_v2_inputs(task_id,revision,port_id,schema_ref,required,
                        source_task_id,source_port,version_id,receipt,reason) VALUES(?,1,?,?,?,?,?,?,?,?)""",
                                    (task_id, port.port_id, port.schema_ref, int(port.required),
                                     ids[edge.source.task_key] if edge and edge.source.task_key else None,
                                     edge.source.port_id if edge else None, version,
                                     "accepted" if version else "pending", "explicit host input binding" if version else None))
                self.db.execute("UPDATE team_v2_task_revisions SET input_hash=? WHERE task_id=? AND revision=1",
                                (self._input_hash(task_id, 1), task_id))
                if self._ready(self._task(task_id)):
                    self._state(task_id, "queued")
            self._run_state(run_id, "running")
            return {"team_id": team["team_id"], "team_run_id": run_id, "task_ids": ids, "state": "running"}
        return self.repo._write(actor, "team_run_activated", command_id, payload,
                                lambda: self._owner(actor, run_id, epoch=control_epoch), action)

    def _task_view(self, task):
        return {key: task[key] for key in ("task_id", "task_key", "team_id", "team_run_id", "state", "revision",
                                           "contract_version", "input_revision", "state_version", "fence",
                                           "spent_tokens", "held_tokens", "token_limit", "attempts", "block_reason")}

    def task(self, actor, task_id):
        with self.lock:
            row = self._task_access(actor, task_id)
            value = self._task_view(row)
            value.update(contract=json.loads(row["contract_json"]), input_hash=row["input_hash"],
                         inputs=[dict(slot) for slot in self._inputs(task_id, row["revision"])],
                         question=json.loads(row["question_json"]) if row["question_json"] else None)
            value["handoffs"] = [dict(item) for item in self.db.execute(
                "SELECT handoff_id,revision,packet_json,packet_hash,state,input_hash FROM team_v2_handoffs WHERE task_id=? ORDER BY created_at",
                (task_id,)).fetchall()]
            for handoff in value["handoffs"]:
                handoff["packet"] = json.loads(handoff.pop("packet_json"))
            return value

    def accept_input(self, actor, task_id, port_id, version_id, *, revision, accepted, reason,
                     command_id, control_epoch):
        reason = _text(reason)
        if type(accepted) is not bool:
            raise ValueError("accepted must be a boolean")
        def authorize():
            task = self._task_access(actor, task_id)
            self._owner(actor, task["team_run_id"], epoch=control_epoch)
        def action():
            task = self._task(task_id)
            self._editable(actor, task['team_run_id'], control_epoch)
            if task["revision"] != revision or task["state"] not in {"waiting_inputs", "queued", "blocked"}:
                raise TeamStateError("INPUT_REVISION_MISMATCH", task_id)
            slot = self.db.execute("SELECT * FROM team_v2_inputs WHERE task_id=? AND revision=? AND port_id=?",
                                   (task_id, revision, port_id)).fetchone()
            if slot is None or slot["version_id"] != version_id:
                raise TeamStateError("INPUT_REVISION_MISMATCH", port_id)
            artifact = self._artifact_for_run(version_id, task["team_run_id"])
            if artifact["schema_ref"] != slot["schema_ref"]:
                raise TeamStateError("SCHEMA_MISMATCH", port_id)
            existing = self.db.execute("SELECT * FROM team_v2_receipts WHERE task_id=? AND revision=? AND port_id=? AND version_id=?",
                                       (task_id, revision, port_id, version_id)).fetchone()
            if existing and (bool(existing["accepted"]) != accepted or existing["reason"] != reason):
                raise TeamStateError("RECEIPT_CONFLICT", "revise inputs to reconsider an immutable receipt")
            if not existing:
                self.db.execute("INSERT INTO team_v2_receipts VALUES(?,?,?,?,?,?,?,?,?)",
                                (_id("receipt"), task_id, revision, port_id, version_id, actor.principal_ref, int(accepted), reason, now_iso()))
            self.db.execute("UPDATE team_v2_inputs SET receipt=?,reason=? WHERE task_id=? AND revision=? AND port_id=?",
                            ("accepted" if accepted else "rejected", reason, task_id, revision, port_id))
            if task['state'] == 'blocked' and task['block_reason'] not in {None, 'input_rejected'}:
                # A business receipt cannot double as recovery approval for
                # unknown external effects, rejected review or lost leases.
                self._state(task_id, 'blocked', task['block_reason'])
            else:
                self._state(task_id, "queued" if self._ready(task) else "waiting_inputs", None if accepted else "input_rejected")
            return self._task_view(self._task(task_id))
        return self.repo._write(actor, "team_input_receipted", command_id,
                                dict(task_id=task_id, port=port_id, version=version_id, revision=revision,
                                     accepted=accepted, reason=reason, epoch=control_epoch), authorize, action)

    def claim(self, actor, task_id, *, worker_session_id, expected_version, command_id):
        worker_session_id = _text(worker_session_id)
        def authorize():
            self._task_access(actor, task_id, "team.execute", producer=True)
        def action():
            task = self._task(task_id)
            team = self.repo._team(actor, task["team_id"])
            if task["state_version"] != expected_version or task["state"] != "queued":
                raise TeamStateError("STATE_VERSION_CONFLICT", task_id)
            if team["lifecycle"] != "active" or self._runtime_run(task["team_run_id"])["state"] not in {"running", "waiting"}:
                raise TeamStateError("RUN_NOT_ACTIVE", task_id)
            if task["deadline"] < now_iso():
                raise TeamStateError("DEADLINE_EXPIRED", task_id)
            if not self._ready(task):
                raise TeamStateError("INPUT_NOT_ACCEPTED", task_id)
            if self._attempt_limit_reached(task):
                raise TeamStateError("ATTEMPT_LIMIT", task_id)
            from .deployment import DeploymentRegistry
            DeploymentRegistry(self).check_claim(actor, task)
            runtime = self._runtime_run(task["team_run_id"])
            budget = self.db.execute("SELECT * FROM team_v2_budgets WHERE project_id=? AND budget_ref=?",
                                     (actor.project_id, runtime["budget_ref"])).fetchone()
            reserve = task["token_limit"] - task["spent_tokens"] - task["held_tokens"]
            if reserve < 1 or budget["limit_tokens"] - budget["used_tokens"] - budget["reserved_tokens"] < reserve:
                raise TeamStateError("BUDGET_EXHAUSTED", task_id)
            token = LeaseToken(task_id=task_id, attempt_id=_id("attempt"), revision=task["revision"],
                               fence=task["fence"] + 1, control_epoch=team["control_epoch"],
                               worker_session_id=worker_session_id, lease_token=secrets.token_urlsafe(32))
            self.db.execute("INSERT INTO team_v2_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (token.attempt_id, task_id, token.revision, token.fence, token.control_epoch,
                             actor.principal_ref, worker_session_id, byte_hash(token.lease_token.encode()),
                             self.clock() + self.lease_seconds, "running", reserve, None, task["input_hash"], now_iso()))
            self.db.execute("UPDATE team_v2_budgets SET reserved_tokens=reserved_tokens+? WHERE project_id=? AND budget_ref=?",
                            (reserve, actor.project_id, runtime["budget_ref"]))
            self.db.execute("UPDATE team_v2_tasks SET fence=?,held_tokens=held_tokens+?,attempts=attempts+1 WHERE task_id=?",
                            (token.fence, reserve, task_id))
            self._state(task_id, "running")
            self._run_state(task["team_run_id"], "running")
            return {"team_id": task["team_id"], "token": token.model_dump(), "reserved_tokens": reserve,
                    "lease_seconds": self.lease_seconds, "task": self._task_view(self._task(task_id))}
        return self.repo._write(actor, "team_attempt_claimed", command_id,
                                dict(task_id=task_id, worker_session_id=worker_session_id, expected_version=expected_version), authorize, action)

    @runtime_locked
    def _attempt_limit_reached(self, task):
        from .responsibility import MissionService
        missions = MissionService(self)
        if missions._row(task['team_run_id'], required=False):
            count = self.db.execute('SELECT COUNT(*) FROM team_v2_attempts WHERE task_id=? AND revision=?', (task['task_id'], task['revision'])).fetchone()[0]
            return count >= missions.policy(task['team_run_id']).max_attempts_per_revision
        return task['attempts'] >= self.max_attempts

    @runtime_locked
    def _lease(self, actor, token):
        task = self._task_access(actor, token.task_id, "team.execute", producer=True)
        attempt = self.db.execute("SELECT * FROM team_v2_attempts WHERE attempt_id=?", (token.attempt_id,)).fetchone()
        team = self.repo._team(actor, task["team_id"])
        if (attempt is None or attempt["task_id"] != token.task_id or attempt["principal_ref"] != actor.principal_ref
                or attempt["worker_session_id"] != token.worker_session_id
                or not secrets.compare_digest(attempt["token_hash"], byte_hash(token.lease_token.encode()))
                or task["revision"] != token.revision or task["fence"] != token.fence
                or team["control_epoch"] != token.control_epoch or attempt["state"] != "running"
                or task["state"] != "running" or team["lifecycle"] != "active"):
            raise TeamStateError("STALE_FENCE", token.task_id)
        if attempt["lease_until"] <= self.clock():
            raise TeamStateError("LEASE_EXPIRED", token.task_id)
        if self.clock() - datetime.fromisoformat(attempt['created_at'].replace('Z', '+00:00')).timestamp() >= self.max_attempt_seconds:
            raise TeamStateError('PROGRESS_TIMEOUT', 'heartbeats do not extend the bounded model turn')
        if task["input_hash"] != attempt["input_hash"] or not self._ready(task):
            raise TeamStateError("INPUT_REVISION_MISMATCH", token.task_id)
        return task, attempt

    def heartbeat(self, actor, token, *, command_id):
        token = LeaseToken.model_validate(token.model_dump() if isinstance(token, LeaseToken) else token)
        def action():
            task, _ = self._lease(actor, token)
            self.db.execute("UPDATE team_v2_attempts SET lease_until=? WHERE attempt_id=?",
                            (self.clock() + self.lease_seconds, token.attempt_id))
            return {"team_id": task["team_id"], "task_id": token.task_id, "lease_until": self.clock() + self.lease_seconds}
        return self.repo._write(actor, "team_attempt_heartbeat", command_id, token.model_dump(),
                                lambda: self._lease(actor, token), action)

    def _settle(self, actor, task, attempt, usage_tokens):
        if usage_tokens is None:
            return
        if type(usage_tokens) is not int or usage_tokens < 0:
            raise ValueError("usage_tokens must be a nonnegative integer or null")
        if attempt["usage_tokens"] is not None:
            if attempt["usage_tokens"] != usage_tokens:
                raise TeamStateError("USAGE_CONFLICT", attempt["attempt_id"])
            return
        budget_ref = self._runtime_run(task["team_run_id"])["budget_ref"]
        self.db.execute("UPDATE team_v2_budgets SET reserved_tokens=reserved_tokens-?,used_tokens=used_tokens+? WHERE project_id=? AND budget_ref=?",
                        (attempt["reserved_tokens"], usage_tokens, actor.project_id, budget_ref))
        self.db.execute("UPDATE team_v2_tasks SET held_tokens=held_tokens-?,spent_tokens=spent_tokens+? WHERE task_id=?",
                        (attempt["reserved_tokens"], usage_tokens, task["task_id"]))
        self.db.execute("UPDATE team_v2_attempts SET usage_tokens=? WHERE attempt_id=?", (usage_tokens, attempt["attempt_id"]))

    def submit(self, actor, token, submission, *, usage_tokens, command_id):
        token = LeaseToken.model_validate(token.model_dump() if isinstance(token, LeaseToken) else token)
        submission = HandoffSubmission.model_validate(submission.model_dump() if isinstance(submission, HandoffSubmission) else submission)
        def authorize():
            self._task_access(actor, token.task_id, "team.execute", producer=True)
        def action():
            task, attempt = self._lease(actor, token)
            contract = TaskTemplate.model_validate_json(task["contract_json"])
            ports = {p.port_id: p for p in contract.outputs}
            supplied = {a.port_id for a in submission.artifacts}
            if supplied - ports.keys() or any(p.required and p.port_id not in supplied for p in ports.values()):
                raise TeamStateError("OUTPUT_CONTRACT_MISMATCH", token.task_id)
            for content in submission.artifacts:
                if content.schema_ref != ports[content.port_id].schema_ref:
                    raise TeamStateError("SCHEMA_MISMATCH", content.port_id)
            artifacts = [self._insert_artifact(actor, task["team_run_id"], a, task_id=task["task_id"]) for a in submission.artifacts]
            packet = dict(summary=submission.summary, artifacts=artifacts, evidence=[e.model_dump() for e in submission.evidence],
                          task_id=token.task_id, attempt_id=token.attempt_id, revision=token.revision,
                          contract_version=task["contract_version"], input_hash=task["input_hash"])
            handoff_id = _id("handoff")
            self.db.execute("INSERT INTO team_v2_handoffs VALUES(?,?,?,?,?,?,?,?,?)",
                            (handoff_id, token.task_id, token.revision, token.attempt_id, canonical_json(packet),
                             _digest(packet), "submitted", task["input_hash"], now_iso()))
            self._settle(actor, task, attempt, usage_tokens)
            self.db.execute("UPDATE team_v2_attempts SET state='submitted' WHERE attempt_id=?", (token.attempt_id,))
            self._state(token.task_id, "verifying")
            return {"team_id": task["team_id"], "task_id": token.task_id, "handoff_id": handoff_id,
                    "packet_hash": _digest(packet), "state": "verifying", "artifacts": artifacts}
        return self.repo._write(actor, "team_handoff_submitted", command_id,
                                dict(token=token.model_dump(), submission=submission.model_dump(), usage_tokens=usage_tokens), authorize, action)

    def review(self, actor, handoff_id, *, accepted, scores, expected_revision, control_epoch, command_id):
        scores = tuple(ReviewScore.model_validate(s.model_dump() if isinstance(s, ReviewScore) else s) for s in scores)
        if type(accepted) is not bool:
            raise ValueError("accepted must be a boolean")
        def load():
            row = self.db.execute("SELECT * FROM team_v2_handoffs WHERE handoff_id=?", (handoff_id,)).fetchone()
            if row is None:
                raise TeamStateError("HANDOFF_NOT_ACCESSIBLE", handoff_id)
            return row
        def authorize():
            handoff = load()
            task = self._task_access(actor, handoff["task_id"], "team.review", reviewer=True)
            self._owner(actor, task["team_run_id"], epoch=control_epoch)
        def action():
            handoff = load()
            task = self._task(handoff["task_id"])
            self._editable(actor, task['team_run_id'], control_epoch)
            attempt = self.db.execute('SELECT * FROM team_v2_attempts WHERE attempt_id=?', (handoff['attempt_id'],)).fetchone()
            if (task["revision"] != expected_revision or handoff["revision"] != expected_revision
                    or handoff["state"] != "submitted" or task["state"] != "verifying"
                    or not attempt or attempt['state'] != 'submitted' or attempt['fence'] != task['fence']
                    or task["input_hash"] != handoff["input_hash"] or not self._ready(task)):
                raise TeamStateError("REVIEW_VERSION_MISMATCH", handoff_id)
            packet = json.loads(handoff["packet_json"])
            if _digest(packet) != handoff["packet_hash"]:
                raise TeamStateError("HANDOFF_CORRUPT", handoff_id)
            contract = TaskTemplate.model_validate_json(task["contract_json"])
            criteria = {criterion.criterion_id: criterion for criterion in contract.acceptance}
            if accepted and task['spent_tokens'] > task['token_limit']:
                raise TeamStateError('BUDGET_OVERRUN', 'actual usage exceeded the task authorization; it cannot be silently accepted')
            if len({s.criterion_id for s in scores}) != len(scores) or {s.criterion_id for s in scores} != criteria.keys():
                raise TeamStateError("REVIEW_CRITERIA_MISMATCH", handoff_id)
            if accepted and any(score.score < criteria[score.criterion_id].minimum_score for score in scores):
                raise TeamStateError("ACCEPTANCE_FAILED", handoff_id)
            for version in packet["artifacts"]:
                if self._artifact(version["version_id"])['state'] != 'staged':
                    raise TeamStateError('ARTIFACT_UNAVAILABLE', version['version_id'])
            receipt = dict(handoff_id=handoff_id, packet_hash=handoff["packet_hash"], input_hash=task["input_hash"],
                           contract_version=task["contract_version"], criteria=[c.model_dump() for c in contract.acceptance],
                           scores=[s.model_dump() for s in scores], accepted=accepted)
            review_id = _id("review")
            self.db.execute("INSERT INTO team_v2_reviews VALUES(?,?,?,?,?,?,?)",
                            (review_id, handoff_id, actor.principal_ref, int(accepted), canonical_json(receipt), _digest(receipt), now_iso()))
            self.db.execute("UPDATE team_v2_handoffs SET state=? WHERE handoff_id=?", ("published" if accepted else "rejected", handoff_id))
            for version in packet["artifacts"]:
                self.db.execute("UPDATE team_v2_artifacts SET state=? WHERE version_id=?",
                                ("published" if accepted else "rejected", version["version_id"]))
                if accepted:
                    self._offer_output(task, version)
            self._state(task["task_id"], "completed" if accepted else "blocked", None if accepted else "review_rejected")
            self._refresh_run(task["team_run_id"])
            return {"team_id": task["team_id"], "task_id": task["task_id"], "handoff_id": handoff_id,
                    "review_id": review_id, "review_hash": _digest(receipt), "accepted": accepted}
        return self.repo._write(actor, "team_handoff_reviewed", command_id,
                                dict(handoff_id=handoff_id, accepted=accepted, scores=[s.model_dump() for s in scores],
                                     expected_revision=expected_revision, epoch=control_epoch), authorize, action)

    def _offer_output(self, producer, version):
        slots = self.db.execute("""SELECT i.* FROM team_v2_inputs i JOIN team_v2_tasks t
            ON t.task_id=i.task_id AND t.revision=i.revision
            WHERE i.source_task_id=? AND i.source_port=? AND i.version_id IS NULL""",
                               (producer["task_id"], version["port_id"])).fetchall()
        for slot in slots:
            self.db.execute("UPDATE team_v2_inputs SET version_id=?,receipt='pending',reason=NULL WHERE task_id=? AND revision=? AND port_id=?",
                            (version["version_id"], slot["task_id"], slot["revision"], slot["port_id"]))
            self.db.execute("UPDATE team_v2_task_revisions SET input_hash=? WHERE task_id=? AND revision=?",
                            (self._input_hash(slot["task_id"], slot["revision"]), slot["task_id"], slot["revision"]))

    def _refresh_run(self, run_id):
        tasks = self.db.execute("SELECT task_id FROM team_v2_tasks WHERE team_run_id=?", (run_id,)).fetchall()
        active_keys = set(self._compiled(run_id).task_order)
        rows = [self._task(row[0]) for row in tasks if self._task(row[0])['task_key'] in active_keys]
        required = [task for task in rows if TaskTemplate.model_validate_json(task["contract_json"]).required]
        if required and all(task["state"] == "completed" for task in required):
            self._run_state(run_id, "verifying")
        elif any(task["state"] in {"queued", "running", "verifying"} for task in rows):
            self._run_state(run_id, "running")
        else:
            self._run_state(run_id, "waiting")

    def artifact(self, actor, version_id, *, include_content=True):
        actor.require("team.read")
        with self.lock:
            row = self._artifact(version_id)
            if row["project_id"] != actor.project_id:
                raise TeamStateError("ARTIFACT_NOT_ACCESSIBLE", version_id)
            team = self.db.execute("SELECT owner_ref FROM team_v2_instances WHERE team_id=?", (row["team_id"],)).fetchone()
            allowed = team["owner_ref"] == actor.principal_ref
            if row["producer_task_id"]:
                try:
                    self._task_access(actor, row["producer_task_id"])
                    allowed = True
                except TeamStateError:
                    pass
            for slot in self.db.execute("""SELECT i.task_id FROM team_v2_inputs i JOIN team_v2_tasks t
                ON t.task_id=i.task_id AND t.revision=i.revision WHERE i.version_id=?""", (version_id,)).fetchall():
                try:
                    self._task_access(actor, slot["task_id"])
                    allowed = True
                    break
                except TeamStateError:
                    pass
            if not allowed:
                raise TeamStateError("ARTIFACT_NOT_ACCESSIBLE", version_id)
            value = {key: row[key] for key in ("version_id", "port_id", "schema_ref", "media_type", "content_hash", "state")}
            value["size"] = len(row["content"])
            try:
                self._artifact(version_id, published=True)
                value['valid_for_consumption'] = True
            except TeamStateError as exc:
                value['valid_for_consumption'] = False
                value['consumption_error'] = exc.code
            if include_content:
                value["content_b64"] = base64.b64encode(row["content"]).decode("ascii")
            return value

    def block_attempt(self, actor, token, *, reason_code, detail, usage_tokens=None,
                      needs_answer=False, command_id):
        token = LeaseToken.model_validate(token.model_dump() if isinstance(token, LeaseToken) else token)
        detail, reason_code = _text(detail), _text(reason_code)
        def action():
            task, attempt = self._lease(actor, token)
            self._settle(actor, task, attempt, usage_tokens)
            self.db.execute("UPDATE team_v2_attempts SET state='blocked' WHERE attempt_id=?", (token.attempt_id,))
            question = None
            if needs_answer:
                question = dict(question_id=_id("question"), reason_code=reason_code, detail=detail,
                                revision=task["revision"], state="pending")
                self.db.execute("INSERT INTO team_v2_questions VALUES(?,?,?,?,?,?,?)",
                                (question["question_id"], task["task_id"], task["revision"], canonical_json(question), "pending", None, now_iso()))
                self.db.execute("UPDATE team_v2_tasks SET question_json=? WHERE task_id=?",
                                (canonical_json(question), task["task_id"]))
            self._state(task["task_id"], "blocked", reason_code)
            self._refresh_run(task["team_run_id"])
            return {"team_id": task["team_id"], "task_id": task["task_id"], "state": "blocked", "question": question,
                    "reason": reason_code, "usage_unknown": usage_tokens is None}
        return self.repo._write(actor, "team_attempt_blocked", command_id,
                                dict(token=token.model_dump(), reason=reason_code, detail=detail, usage_tokens=usage_tokens,
                                     needs_answer=needs_answer),
                                lambda: self._task_access(actor, token.task_id, "team.execute", producer=True), action)

    def reap_expired(self, actor, run_id, *, command_id):
        def authorize():
            self.repo._run(actor, self._run(run_id)["team_id"], run_id)
            if "team.manage" not in actor.permissions and "team.execute" not in actor.permissions:
                raise TeamStateError("PERMISSION_REQUIRED", "reap")
        def action():
            candidates = self.db.execute("""SELECT a.* FROM team_v2_attempts a JOIN team_v2_tasks t USING(task_id)
                WHERE t.team_run_id=? AND a.state='running'""", (run_id,)).fetchall()
            rows = [row for row in candidates if row['lease_until'] <= self.clock()
                    or self.clock() - datetime.fromisoformat(row['created_at'].replace('Z', '+00:00')).timestamp() >= self.max_attempt_seconds]
            for attempt in rows:
                self.db.execute("UPDATE team_v2_attempts SET state='expired' WHERE attempt_id=?", (attempt["attempt_id"],))
                self.db.execute("UPDATE team_v2_tasks SET fence=fence+1 WHERE task_id=?", (attempt["task_id"],))
                self._state(attempt["task_id"], "expired", "effect_unknown")
            if rows:
                self._refresh_run(run_id)
            return {"team_id": self._run(run_id)["team_id"], "expired_attempts": [r["attempt_id"] for r in rows]}
        return self.repo._write(actor, "team_attempts_reaped", command_id, dict(run_id=run_id), authorize, action)

    def resolve_usage(self, actor, attempt_id, *, usage_tokens, evidence, command_id, control_epoch):
        evidence = _text(evidence)
        def load():
            row = self.db.execute("SELECT * FROM team_v2_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None:
                raise TeamStateError("ATTEMPT_NOT_ACCESSIBLE", attempt_id)
            return row
        def authorize():
            actor.require("team.recover")
            self._owner(actor, self._task(load()["task_id"])["team_run_id"], epoch=control_epoch)
        def action():
            attempt = load()
            task = self._task(attempt["task_id"])
            if attempt["state"] == "running":
                raise TeamStateError("ATTEMPT_STILL_RUNNING", attempt_id)
            self._settle(actor, task, attempt, usage_tokens)
            self.db.execute("UPDATE team_v2_model_turns SET state='reconciled',finished_at=COALESCE(finished_at,?) WHERE attempt_id=? AND state='running'",
                            (now_iso(), attempt_id))
            return {"team_id": task["team_id"], "attempt_id": attempt_id, "usage_tokens": usage_tokens, "evidence": evidence}
        if type(usage_tokens) is not int or usage_tokens < 0:
            raise ValueError("resolved usage must be a nonnegative integer")
        return self.repo._write(actor, "team_usage_reconciled", command_id,
                                dict(attempt_id=attempt_id, usage_tokens=usage_tokens, evidence=evidence, epoch=control_epoch), authorize, action)

    def resume(self, actor, task_id, *, expected_version, control_epoch, command_id,
               question_id=None, answer=None, effect_evidence=None):
        def authorize():
            self._owner(actor, self._task(task_id)["team_run_id"], epoch=control_epoch)
        def action():
            task = self._task(task_id)
            self._editable(actor, task['team_run_id'], control_epoch)
            if task["state_version"] != expected_version or task["state"] not in {"blocked", "expired"}:
                raise TeamStateError("STATE_VERSION_CONFLICT", task_id)
            if task["held_tokens"]:
                raise TeamStateError("USAGE_UNKNOWN", "reconcile the outstanding attempt first")
            if self._attempt_limit_reached(task):
                raise TeamStateError("ATTEMPT_LIMIT", task_id)
            if task["block_reason"] == "effect_unknown" and not effect_evidence:
                raise TeamStateError("NEEDS_RECONCILIATION", "confirm external effects before retrying")
            if task["question_json"]:
                question = json.loads(task["question_json"])
                if question.get("state") == "pending":
                    if question["question_id"] != question_id or not answer or not str(answer).strip():
                        raise TeamStateError("QUESTION_REQUIRED", question["question_id"])
                    question.update(state="answered", answer=_text(answer))
                    self.db.execute("UPDATE team_v2_questions SET state='answered',answer=? WHERE question_id=? AND revision=?",
                                    (answer, question_id, task["revision"]))
                    self.db.execute("UPDATE team_v2_tasks SET question_json=? WHERE task_id=?", (canonical_json(question), task_id))
            if task["spent_tokens"] >= task["token_limit"]:
                raise TeamStateError("BUDGET_EXHAUSTED", task_id)
            self._state(task_id, "queued" if self._ready(task) else "waiting_inputs")
            self._refresh_run(task["team_run_id"])
            return self._task_view(self._task(task_id))
        return self.repo._write(actor, "team_task_resumed", command_id,
                                dict(task_id=task_id, expected_version=expected_version, epoch=control_epoch,
                                     question_id=question_id, answer=answer, effect_evidence=effect_evidence), authorize, action)

    def _invalidate_attempt(self, task, reason):
        self.db.execute("UPDATE team_v2_attempts SET state='cancelled' WHERE task_id=? AND state='running'", (task["task_id"],))
        self.db.execute("UPDATE team_v2_tasks SET fence=fence+1 WHERE task_id=?", (task["task_id"],))
        self._state(task["task_id"], "blocked", reason)

    def _new_revision(self, task, overrides, contract=None):
        old_contract = TaskTemplate.model_validate_json(task["contract_json"])
        contract = contract or old_contract
        if task["state"] == "running":
            self._invalidate_attempt(task, "effect_unknown")
        revision = task["revision"] + 1
        contract_version = task["contract_version"] + int(contract != old_contract)
        slots = self._inputs(task["task_id"], task["revision"])
        if set(overrides) - {slot["port_id"] for slot in slots}:
            raise TeamStateError("UNKNOWN_PORT", task["task_id"])
        self.db.execute("INSERT INTO team_v2_task_revisions VALUES(?,?,?,?,?,?,?,?)",
                        (task["task_id"], revision, contract_version, task["input_revision"] + 1,
                         contract.model_dump_json(), "", "waiting_inputs", now_iso()))
        for slot in slots:
            version = overrides.get(slot["port_id"], slot["version_id"])
            if version:
                artifact = self._artifact_for_run(version, task["team_run_id"])
                if artifact["schema_ref"] != slot["schema_ref"]:
                    raise TeamStateError("SCHEMA_MISMATCH", slot["port_id"])
            self.db.execute("INSERT INTO team_v2_inputs VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (task["task_id"], revision, slot["port_id"], slot["schema_ref"], slot["required"],
                             slot["source_task_id"], slot["source_port"], version,
                             "pending" if slot["port_id"] in overrides else slot["receipt"],
                             None if slot["port_id"] in overrides else slot["reason"]))
        self.db.execute("UPDATE team_v2_task_revisions SET input_hash=? WHERE task_id=? AND revision=?",
                        (self._input_hash(task["task_id"], revision), task["task_id"], revision))
        self.db.execute("""UPDATE team_v2_tasks SET revision=?,contract_version=?,input_revision=input_revision+1,
            fence=fence+1,question_json=NULL,token_limit=MIN(token_limit,?) WHERE task_id=?""",
                        (revision, contract_version, contract.token_budget, task["task_id"]))
        self.db.execute("UPDATE team_v2_questions SET state='superseded' WHERE task_id=? AND state='pending'", (task["task_id"],))
        current = self._task(task["task_id"])
        state = "blocked" if current["held_tokens"] else ("queued" if self._ready(current) else "waiting_inputs")
        self._state(task["task_id"], state, "effect_unknown" if current["held_tokens"] else None)
        self.db.execute("UPDATE team_v2_runtime_runs SET export_json=NULL WHERE team_run_id=?", (task["team_run_id"],))
        self._run_state(task["team_run_id"], "running" if state == "queued" else "waiting")

    def impact(self, actor, task_id):
        with self.lock:
            task = self._task_access(actor, task_id)
            self._owner(actor, task["team_run_id"])
            return self._impact(task_id)

    def _impact(self, task_id, *, cross_runs=False):
        root_run = self._task(task_id)["team_run_id"]
        seen, pending = set(), [task_id]
        while pending:
            current = pending.pop(0)
            if current in seen:
                continue
            seen.add(current)
            consumers = self.db.execute("""SELECT DISTINCT i.task_id,t.team_run_id FROM team_v2_inputs i JOIN team_v2_tasks t
                ON t.task_id=i.task_id AND t.revision=i.revision JOIN team_v2_artifacts a ON a.version_id=i.version_id
                WHERE a.producer_task_id=?""", (current,)).fetchall()
            pending.extend(row[0] for row in consumers if row[0] not in seen and (cross_runs or row[1] == root_run))
        return sorted(seen)

    def revise(self, actor, task_id, *, inputs, expected_version, control_epoch, reason,
               approved, command_id, contract=None, effect_evidence=None):
        reason = _text(reason)
        if approved is not True:
            raise TeamStateError("APPROVAL_REQUIRED", "explicit revision activation required")
        contract = TaskTemplate.model_validate(contract) if contract is not None else None
        def action():
            task = self._task(task_id)
            self._editable(actor, task['team_run_id'], control_epoch)
            if task["state_version"] != expected_version:
                raise TeamStateError("STATE_VERSION_CONFLICT", task_id)
            if task['block_reason'] == 'effect_unknown' and not task['held_tokens'] and not effect_evidence:
                raise TeamStateError('NEEDS_RECONCILIATION', 'a new revision cannot bypass external-effect reconciliation')
            if self._runtime_run(task["team_run_id"])["state"] == "cancelled":
                raise TeamStateError("RUN_NOT_ACTIVE", task_id)
            old = TaskTemplate.model_validate_json(task["contract_json"])
            if contract:
                if (contract.task_key != old.task_key or contract.inputs != old.inputs or contract.outputs != old.outputs
                        or contract.role_id != old.role_id or contract.reviewer_role != old.reviewer_role
                        or contract.required != old.required or contract.token_budget > old.token_budget
                        or not set(contract.input_scope) <= set(old.input_scope)
                        or not set(contract.required_actions) <= set(old.required_actions)
                        or not set(old.forbidden_changes) <= set(contract.forbidden_changes)
                        or contract.verification != old.verification):
                    raise TeamStateError("NEW_PLAN_REQUIRED", "changing authority, topology or verification requires a new TeamSpec")
            affected = self._impact(task_id)
            self._new_revision(task, inputs, contract)
            for consumer_id in affected:
                if consumer_id == task_id:
                    continue
                consumer = self._task(consumer_id)
                replacements = {}
                for slot in self._inputs(consumer_id, consumer["revision"]):
                    if slot["version_id"] and self._artifact(slot["version_id"])["producer_task_id"] in affected:
                        replacements[slot["port_id"]] = None
                self._new_revision(consumer, replacements)
            return {"team_id": task["team_id"], "task_id": task_id, "affected_task_ids": affected,
                    "revision": self._task(task_id)["revision"], "reason": reason}
        return self.repo._write(actor, "team_revision_activated", command_id,
                                dict(task_id=task_id, inputs=inputs, expected_version=expected_version, epoch=control_epoch,
                                     reason=reason, approved=approved, contract=contract.model_dump() if contract else None,
                                     effect_evidence=effect_evidence),
                                lambda: self._owner(actor, self._task(task_id)["team_run_id"], epoch=control_epoch), action)

    def revoke_artifact(self, actor, version_id, *, reason, control_epoch, command_id):
        reason = _text(reason)
        def action():
            artifact = self._artifact(version_id)
            consumers = [row[0] for row in self.db.execute("""SELECT DISTINCT i.task_id FROM team_v2_inputs i
                JOIN team_v2_tasks t ON t.task_id=i.task_id AND t.revision=i.revision WHERE i.version_id=?""", (version_id,)).fetchall()]
            affected = set()
            for task_id in consumers:
                affected.update(self._impact(task_id, cross_runs=True))
            # Break bindings before revoking the old blob, so unrelated valid
            # input checks do not accidentally roll the safety invalidation back.
            for task_id in sorted(affected):
                task = self._task(task_id)
                overrides = {}
                for slot in self._inputs(task_id, task["revision"]):
                    if slot["version_id"] and (slot["version_id"] == version_id or self._artifact(slot["version_id"])["producer_task_id"] in affected):
                        overrides[slot["port_id"]] = None
                self._new_revision(task, overrides)
            self.db.execute("UPDATE team_v2_artifacts SET state='revoked' WHERE version_id=?", (version_id,))
            if artifact["producer_task_id"]:
                producer = self._task(artifact["producer_task_id"])
                current_packets = self.db.execute('SELECT packet_json FROM team_v2_handoffs WHERE task_id=? AND revision=?',
                                                  (producer['task_id'], producer['revision'])).fetchall()
                if any(version_id in {a['version_id'] for a in json.loads(p[0])['artifacts']} for p in current_packets):
                    # Keep the former terminal revision as history. A revoked
                    # old version must not invalidate a newer valid revision.
                    self._new_revision(producer, {})
                    self._state(producer['task_id'], 'blocked', 'artifact_revoked')
                    self._run_state(producer['team_run_id'], 'waiting')
            # A passthrough export can reference this version without any task
            # input edge. Invalidate those manifests too, including closed runs.
            for row in self.db.execute('SELECT team_run_id,export_json FROM team_v2_runtime_runs WHERE export_json IS NOT NULL').fetchall():
                valid = True
                for exported in json.loads(row['export_json'])['exports'].values():
                    try:
                        self._artifact(exported, published=True)
                    except TeamStateError:
                        valid = False
                        break
                if not valid:
                    self.db.execute('UPDATE team_v2_runtime_runs SET export_json=NULL WHERE team_run_id=?', (row['team_run_id'],))
                    self._run_state(row['team_run_id'], 'failed')
            return {"team_id": artifact["team_id"], "version_id": version_id, "affected_task_ids": sorted(affected), "reason": reason}
        return self.repo._write(actor, "team_artifact_revoked", command_id,
                                dict(version_id=version_id, reason=reason, epoch=control_epoch),
                                lambda: self._owner(actor, self._artifact(version_id)["team_run_id"], epoch=control_epoch), action)

    def suspend(self, actor, team_id, *, expected_version, control_epoch, command_id):
        def action():
            team = self.repo._team(actor, team_id, owner=True)
            self.repo._expected(team, expected_version, control_epoch)
            if team["lifecycle"] not in {"active", "suspended"}:
                raise TeamStateError("TEAM_NOT_ACTIVE", team_id)
            self.db.execute("UPDATE team_v2_instances SET lifecycle='suspended',control_epoch=control_epoch+1,state_version=state_version+1 WHERE team_id=?", (team_id,))
            for row in self.db.execute("SELECT task_id FROM team_v2_tasks WHERE team_id=?", (team_id,)).fetchall():
                task = self._task(row[0])
                if task["state"] == "running":
                    self._invalidate_attempt(task, "effect_unknown")
            return dict(self.db.execute("SELECT * FROM team_v2_instances WHERE team_id=?", (team_id,)).fetchone())
        return self.repo._write(actor, "team_suspended", command_id,
                                dict(team_id=team_id, expected_version=expected_version, epoch=control_epoch),
                                lambda: self.repo._team(actor, team_id, owner=True), action)

    def take_over(self, actor, team_id, *, expected_version, control_epoch, reason, command_id):
        # The adapter must attest team.recover and its project scope. No worker
        # command or self-asserted role can manufacture this host capability.
        reason = _text(reason)
        def authorize():
            actor.require("team.recover")
            actor.require("team.manage")
            if self.db.execute("SELECT 1 FROM team_v2_instances WHERE team_id=? AND project_id=?",
                               (team_id, actor.project_id)).fetchone() is None:
                raise TeamStateError("TEAM_NOT_ACCESSIBLE", team_id)
        def action():
            team = self.db.execute("SELECT * FROM team_v2_instances WHERE team_id=?", (team_id,)).fetchone()
            self.repo._expected(team, expected_version, control_epoch)
            if team["lifecycle"] != "suspended":
                raise TeamStateError("TEAM_NOT_SUSPENDED", team_id)
            self.db.execute("UPDATE team_v2_instances SET owner_ref=?,lifecycle='active',control_epoch=control_epoch+1,state_version=state_version+1 WHERE team_id=?",
                            (actor.principal_ref, team_id))
            for row in self.db.execute("SELECT r.* FROM team_v2_runtime_runs r JOIN team_v2_runs t USING(team_run_id) WHERE t.team_id=?", (team_id,)).fetchall():
                bindings = json.loads(row["bindings_json"])
                for binding in bindings:
                    if binding["kind"] in {"host", "human"} and binding["principal_ref"] == team["owner_ref"]:
                        binding["principal_ref"] = actor.principal_ref
                self.db.execute("UPDATE team_v2_runtime_runs SET bindings_json=? WHERE team_run_id=?", (canonical_json(bindings), row["team_run_id"]))
                self.db.execute("UPDATE team_v2_memberships SET principal_ref=? WHERE team_run_id=? AND principal_ref=?",
                                (actor.principal_ref, row["team_run_id"], team["owner_ref"]))
            return dict(self.db.execute("SELECT * FROM team_v2_instances WHERE team_id=?", (team_id,)).fetchone())
        return self.repo._write(actor, "team_control_taken_over", command_id,
                                dict(team_id=team_id, expected_version=expected_version, epoch=control_epoch, reason=reason), authorize, action)

    def cancel_run(self, actor, run_id, *, control_epoch, reason, command_id):
        reason = _text(reason)
        def action():
            for row in self.db.execute("SELECT task_id FROM team_v2_tasks WHERE team_run_id=?", (run_id,)).fetchall():
                task = self._task(row[0])
                if task["state"] not in {"completed", "failed", "cancelled"}:
                    self._invalidate_attempt(task, "cancelled")
                    self._state(row[0], "cancelled", reason)
            self.db.execute("UPDATE team_v2_runtime_runs SET export_json=NULL WHERE team_run_id=?", (run_id,))
            self._run_state(run_id, "cancelled")
            return {"team_id": self._run(run_id)["team_id"], "team_run_id": run_id, "state": "cancelled"}
        return self.repo._write(actor, "team_run_cancelled", command_id,
                                dict(run_id=run_id, epoch=control_epoch, reason=reason),
                                lambda: self._owner(actor, run_id, epoch=control_epoch), action)

    def finalize(self, actor, run_id, *, expected_version, control_epoch, evidence, command_id):
        evidence = _text(evidence)
        def authorize():
            actor.require("team.review")
            self._owner(actor, run_id, epoch=control_epoch)
        def action():
            runtime = self._runtime_run(run_id)
            from .responsibility import MissionService
            MissionService(self).require_acceptance(run_id)
            budget = self.db.execute('SELECT * FROM team_v2_budgets WHERE project_id=? AND budget_ref=?', (actor.project_id, runtime['budget_ref'])).fetchone()
            if budget['used_tokens'] > budget['limit_tokens']:
                raise TeamStateError('BUDGET_OVERRUN', 'shared budget exceeded its explicit authorization')
            if runtime["state"] != "verifying" or runtime["state_version"] != expected_version:
                raise TeamStateError("STATE_VERSION_CONFLICT", run_id)
            compiled = self._compiled(run_id)
            tasks = {row["task_key"]: self._task(row["task_id"]) for row in self.db.execute(
                "SELECT task_key,task_id FROM team_v2_tasks WHERE team_run_id=?", (run_id,)).fetchall()}
            if any(task["held_tokens"] or task["question_json"] and json.loads(task["question_json"]).get("state") == "pending"
                   for task in tasks.values()):
                raise TeamStateError("NEEDS_RECONCILIATION", "outstanding usage or questions")
            if any(task["state"] not in {"completed", "cancelled", "failed"} for task in tasks.values()):
                raise TeamStateError("TASKS_NOT_TERMINAL", run_id)
            if any(tasks[key]["state"] != "completed" for key in compiled.required_task_keys):
                raise TeamStateError("ACCEPTANCE_FAILED", run_id)
            if self.db.execute("SELECT 1 FROM team_v2_publications WHERE team_run_id=? AND state NOT IN ('committed','rolled_back')", (run_id,)).fetchone():
                raise TeamStateError("NEEDS_RECONCILIATION", "publication unresolved")
            if self.db.execute("SELECT 1 FROM team_v2_tool_calls c JOIN team_v2_tasks t USING(task_id) WHERE t.team_run_id=? AND c.state IN ('running','effect_unknown') LIMIT 1", (run_id,)).fetchone():
                raise TeamStateError('NEEDS_RECONCILIATION', 'a tool outcome is unresolved')
            if self.db.execute("SELECT 1 FROM team_v2_coord_model_calls WHERE team_run_id=? AND usage_tokens IS NULL LIMIT 1", (run_id,)).fetchone():
                raise TeamStateError('NEEDS_RECONCILIATION', 'coordinator model usage is unresolved')
            exports = {}
            for port in compiled.spec.outputs:
                edge = next((edge for edge in compiled.spec.connections if edge.target.task_key is None and edge.target.port_id == port.port_id), None)
                version = None
                if edge and edge.source.task_key is None:
                    version = json.loads(runtime["input_json"]).get(edge.source.port_id)
                elif edge:
                    task = tasks[edge.source.task_key]
                    handoff = self.db.execute("SELECT * FROM team_v2_handoffs WHERE task_id=? AND revision=? AND state='published'",
                                              (task["task_id"], task["revision"])).fetchone()
                    if handoff:
                        packet = json.loads(handoff["packet_json"])
                        if _digest(packet) != handoff["packet_hash"]:
                            raise TeamStateError("HANDOFF_CORRUPT", handoff["handoff_id"])
                        version = next((a["version_id"] for a in packet["artifacts"] if a["port_id"] == edge.source.port_id), None)
                if not version:
                    if port.required:
                        raise TeamStateError("OUTPUT_CONTRACT_MISMATCH", port.port_id)
                    continue
                self._artifact(version, published=True)
                exports[port.port_id] = version
            manifest = dict(team_run_id=run_id, task_set_revision=runtime["task_set_revision"], exports=exports,
                            task_revisions={key: task["revision"] for key, task in tasks.items()}, evidence=evidence)
            self.db.execute("UPDATE team_v2_runtime_runs SET export_json=? WHERE team_run_id=?", (canonical_json(manifest), run_id))
            self._run_state(run_id, "completed")
            return {"team_id": self._run(run_id)["team_id"], "team_run_id": run_id, "state": "completed",
                    "manifest": manifest, "manifest_hash": _digest(manifest)}
        return self.repo._write(actor, "team_run_finalized", command_id,
                                dict(run_id=run_id, expected_version=expected_version, epoch=control_epoch, evidence=evidence), authorize, action)

    def close_team(self, actor, team_id, *, expected_version, control_epoch, command_id):
        def action():
            team = self.repo._team(actor, team_id, owner=True)
            self.repo._expected(team, expected_version, control_epoch)
            rows = self.db.execute("SELECT r.state FROM team_v2_runtime_runs r JOIN team_v2_runs t USING(team_run_id) WHERE t.team_id=?", (team_id,)).fetchall()
            if any(row[0] not in {"completed", "failed", "cancelled"} for row in rows):
                raise TeamStateError("TEAM_STILL_RUNNING", team_id)
            if self.db.execute("SELECT 1 FROM team_v2_tasks WHERE team_id=? AND held_tokens>0", (team_id,)).fetchone():
                raise TeamStateError("USAGE_UNKNOWN", team_id)
            if self.db.execute("SELECT 1 FROM team_v2_publications WHERE team_id=? AND state NOT IN ('committed','rolled_back')", (team_id,)).fetchone():
                raise TeamStateError("NEEDS_RECONCILIATION", team_id)
            self.db.execute("UPDATE team_v2_instances SET lifecycle='closed',state_version=state_version+1,control_epoch=control_epoch+1 WHERE team_id=?", (team_id,))
            return dict(self.db.execute("SELECT * FROM team_v2_instances WHERE team_id=?", (team_id,)).fetchone())
        return self.repo._write(actor, "team_closed", command_id,
                                dict(team_id=team_id, expected_version=expected_version, epoch=control_epoch),
                                lambda: self.repo._team(actor, team_id, owner=True), action)

    def send_message(self, actor, run_id, *, recipient, body, command_id):
        body = _text(body)
        team_id = self._run(run_id)["team_id"]
        def authorize():
            self.repo._run(actor, team_id, run_id)
        def action():
            if recipient == "*":
                targets = {row[0] for row in self.db.execute("SELECT m.principal_ref FROM team_v2_memberships m LEFT JOIN team_v2_member_states s USING(membership_id) WHERE m.team_run_id=? AND COALESCE(s.state,'active')!='left'", (run_id,)).fetchall()}
                targets.add(self.repo._team(actor, team_id)["owner_ref"])
                targets.discard(actor.principal_ref)
            else:
                targets = {self.repo.resolve_member(actor, team_id, run_id, recipient)}
            messages = []
            for target in sorted(targets):
                message_id = _id("message")
                self.db.execute("INSERT INTO team_v2_messages VALUES(?,?,?,?,?,?,?,NULL)",
                                (message_id, team_id, run_id, actor.principal_ref, target, body, now_iso()))
                messages.append(message_id)
            return {"team_id": team_id, "message_ids": messages, "execution_started": False}
        return self.repo._write(actor, "team_message_sent", command_id,
                                dict(run_id=run_id, recipient=recipient, body=body), authorize, action)

    def messages(self, actor, run_id):
        with self.lock:
            self.repo._run(actor, self._run(run_id)["team_id"], run_id)
            return [dict(row) for row in self.db.execute("SELECT * FROM team_v2_messages WHERE team_run_id=? AND recipient_ref=? ORDER BY created_at",
                                                        (run_id, actor.principal_ref)).fetchall()]

    def ack_message(self, actor, message_id, *, command_id):
        def authorize():
            actor.require("team.read")
            row = self.db.execute("SELECT * FROM team_v2_messages WHERE message_id=? AND recipient_ref=?", (message_id, actor.principal_ref)).fetchone()
            if row is None:
                raise TeamStateError("MESSAGE_NOT_ACCESSIBLE", message_id)
            self.repo._run(actor, row["team_id"], row["team_run_id"])
        def action():
            self.db.execute("UPDATE team_v2_messages SET acked_at=COALESCE(acked_at,?) WHERE message_id=?", (now_iso(), message_id))
            row = self.db.execute("SELECT team_id FROM team_v2_messages WHERE message_id=?", (message_id,)).fetchone()
            return {"team_id": row[0], "message_id": message_id, "acked": True, "business_acceptance": False}
        return self.repo._write(actor, "team_message_acked", command_id, dict(message_id=message_id), authorize, action)

    def snapshot(self, actor, run_id):
        from .deployment import DeploymentRegistry
        with self.lock:
            run_row = self._run(run_id)
            team = self.repo._team(actor, run_row["team_id"])
            self.repo._run(actor, team["team_id"], run_id)
            runtime = self.db.execute("SELECT * FROM team_v2_runtime_runs WHERE team_run_id=?", (run_id,)).fetchone()
            run = json.loads(run_row["run_json"])
            if runtime:
                run.update(state=runtime["state"], state_version=runtime["state_version"], control_epoch=team["control_epoch"])
            tasks, historical_tasks = [], []
            active_keys = set(self._compiled(run_id).task_order)
            for row in self.db.execute("SELECT task_id FROM team_v2_tasks WHERE team_run_id=? ORDER BY task_key", (run_id,)).fetchall():
                try:
                    task = self.task(actor, row[0])
                    (tasks if task['task_key'] in active_keys else historical_tasks).append(task)
                except TeamStateError:
                    continue
            budget = self.db.execute("SELECT * FROM team_v2_budgets WHERE project_id=? AND budget_ref=?",
                                     (actor.project_id, run["budget_ref"])).fetchone()
            bindings = json.loads(runtime["bindings_json"]) if runtime else []
            attempts = [dict(row) for row in self.db.execute("""SELECT a.attempt_id,a.task_id,a.state,a.usage_tokens,a.reserved_tokens,
                a.worker_session_id,a.lease_until,a.fence FROM team_v2_attempts a JOIN team_v2_tasks t USING(task_id)
                WHERE t.team_run_id=? ORDER BY a.created_at""", (run_id,)).fetchall()]
            visible = {task["task_id"] for task in tasks}
            owner_view = team['owner_ref'] == actor.principal_ref
            if not owner_view:
                run = {key: run[key] for key in ('team_id', 'team_run_id', 'state', 'state_version', 'task_set_revision', 'control_epoch')}
                roles = {role for task in tasks for role in (task['contract']['role_id'], task['contract']['reviewer_role'])}
                bindings = [binding for binding in bindings if binding['role_id'] in roles]
            publications = [dict(row) for row in self.db.execute("SELECT publication_id,state,task_id,created_at FROM team_v2_publications WHERE team_run_id=?", (run_id,)).fetchall()]
            members = [dict(row) for row in self.db.execute("SELECT m.*,COALESCE(s.state,'active') AS state,s.reason FROM team_v2_memberships m LEFT JOIN team_v2_member_states s USING(membership_id) WHERE m.team_run_id=?", (run_id,)).fetchall()]
            return {"team": dict(team), "run": run, "bindings": bindings, "tasks": tasks,
                    'members': members if owner_view else [m for m in members if m['principal_ref'] == actor.principal_ref],
                    "attempts": [row for row in attempts if row["task_id"] in visible],
                    "budget": dict(budget) if budget and team["owner_ref"] == actor.principal_ref else None,
                    "publications": publications if team["owner_ref"] == actor.principal_ref else [],
                    "export": json.loads(runtime["export_json"]) if owner_view and runtime and runtime["export_json"] else None,
                    "execution_slots": DeploymentRegistry(self).capacity(run_id), "historical_tasks": historical_tasks}
