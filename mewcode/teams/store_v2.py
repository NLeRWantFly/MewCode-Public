"""Opt-in team repository owned by the existing CoordinationStore.

All writes share its connection, lock, transactions, and audit log. This module
does not instantiate another database, background scheduler, or model client.
Actor contexts are supplied by trusted host adapters, never parsed from model
tool arguments. Persistent execution/handoff tables are added separately.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from pydantic import TypeAdapter

from mewcode.mcp.coord_protocol import CoordinationError, canonical_json, now_iso
from .spec import CompiledTeam, Identifier, SpecRef, TeamRun, TeamSpec, Text
from .spec_compiler import validate_compiled_snapshot

if TYPE_CHECKING:
    from mewcode.mcp.coord_store import CoordinationStore


class TeamStateError(CoordinationError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class TeamActor:
    """Host-attested principal and project scope, not a self-asserted role."""

    principal_ref: str
    project_id: str
    permissions: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "principal_ref", _text(self.principal_ref))
        object.__setattr__(self, "project_id", _text(self.project_id))
        permissions = frozenset(self.permissions)
        if not permissions <= {"team.read", "team.plan", "team.manage", "team.execute", "team.review", "team.recover"}:
            raise TeamStateError("INVALID_PERMISSION", "unknown team permission")
        object.__setattr__(self, "permissions", permissions)

    def require(self, permission: str) -> None:
        _text(self.principal_ref)
        _text(self.project_id)
        if permission not in self.permissions:
            raise TeamStateError("PERMISSION_REQUIRED", permission)


def _text(value: str) -> str:
    return TypeAdapter(Text).validate_python(value)


def _digest(value: dict) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _event_view(value):
    if isinstance(value, dict):
        return {key: _event_view(item) for key, item in value.items()
                if key not in {"lease_token", "content_b64", "compiled"}}
    if isinstance(value, (list, tuple)):
        return [_event_view(item) for item in value]
    return value


class TeamStoreV2:
    def __init__(self, store: CoordinationStore, *, read_only: bool = False) -> None:
        self._store = store
        self._conn = store._conn
        self._lock = store._lock
        self.read_only = read_only
        if read_only:
            versions = self._conn.execute("SELECT version FROM team_v2_schema").fetchall()
            if len(versions) != 1 or versions[0][0] != 1:
                raise TeamStateError("SCHEMA_VERSION_UNSUPPORTED", "team repository")
        else:
            self._init_schema()
        self._runtime = None

    @property
    def runtime(self):
        with self._lock:
            if self._runtime is None:
                from .runtime import TeamRuntime
                self._runtime = TeamRuntime(self, read_only=self.read_only)
            return self._runtime

    def _init_schema(self) -> None:
        # Explicit opt-in is checked by CoordinationStore before construction.
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("CREATE TABLE IF NOT EXISTS team_v2_schema (version INTEGER NOT NULL)")
                versions = self._conn.execute("SELECT version FROM team_v2_schema").fetchall()
                if versions and (len(versions) != 1 or versions[0][0] != 1):
                    raise TeamStateError("SCHEMA_VERSION_UNSUPPORTED", "team repository")
                statements = (
                    """CREATE TABLE IF NOT EXISTS team_v2_specs (
                        project_id TEXT NOT NULL, spec_id TEXT NOT NULL, version INTEGER NOT NULL,
                        spec_json TEXT NOT NULL, spec_hash TEXT NOT NULL, created_by TEXT NOT NULL,
                        created_at TEXT NOT NULL, PRIMARY KEY(project_id,spec_id,version))""",
                    """CREATE TABLE IF NOT EXISTS team_v2_instances (
                        team_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL,
                        spec_id TEXT NOT NULL, spec_version INTEGER NOT NULL, owner_ref TEXT NOT NULL,
                        lifecycle TEXT NOT NULL CHECK(lifecycle IN ('active','suspended','closing','closed')),
                        state_version INTEGER NOT NULL, control_epoch INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(project_id,spec_id,spec_version)
                        REFERENCES team_v2_specs(project_id,spec_id,version))""",
                    """CREATE TABLE IF NOT EXISTS team_v2_runs (
                        team_run_id TEXT PRIMARY KEY, team_id TEXT NOT NULL,
                        run_json TEXT NOT NULL, compiled_json TEXT NOT NULL, created_at TEXT NOT NULL,
                        UNIQUE(team_id,team_run_id),
                        FOREIGN KEY(team_id) REFERENCES team_v2_instances(team_id))""",
                    """CREATE TABLE IF NOT EXISTS team_v2_memberships (
                        membership_id TEXT PRIMARY KEY, team_id TEXT NOT NULL,
                        team_run_id TEXT NOT NULL, alias TEXT NOT NULL COLLATE NOCASE,
                        role_id TEXT NOT NULL, principal_ref TEXT NOT NULL,
                        UNIQUE(team_id,team_run_id,alias),
                        UNIQUE(team_id,team_run_id,role_id,principal_ref),
                        FOREIGN KEY(team_id,team_run_id) REFERENCES team_v2_runs(team_id,team_run_id))""",
                    """CREATE TABLE IF NOT EXISTS team_v2_commands (
                        project_id TEXT NOT NULL, actor_ref TEXT NOT NULL, command_id TEXT NOT NULL,
                        payload_hash TEXT NOT NULL, result_json TEXT NOT NULL,
                        PRIMARY KEY(project_id,actor_ref,command_id))""",
                    """CREATE TABLE IF NOT EXISTS team_v2_member_states (
                        membership_id TEXT PRIMARY KEY REFERENCES team_v2_memberships(membership_id),
                        state TEXT NOT NULL, reason TEXT NOT NULL, updated_at TEXT NOT NULL)""",
                    """CREATE TABLE IF NOT EXISTS team_v2_events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                        project_id TEXT NOT NULL, team_id TEXT, event_type TEXT NOT NULL,
                        actor_ref TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
                )
                for statement in statements:
                    self._conn.execute(statement)
                if not versions:
                    self._conn.execute("INSERT INTO team_v2_schema(version) VALUES(1)")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _write(self, actor: TeamActor, operation: str, command_id: str, payload: dict,
               authorize: Callable[[], None], action: Callable[[], dict]) -> dict:
        command_id = _text(command_id)
        digest = _digest({"operation": operation, "payload": payload})
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                authorize()
                previous = self._conn.execute(
                    "SELECT payload_hash,result_json FROM team_v2_commands WHERE project_id=? AND actor_ref=? AND command_id=?",
                    (actor.project_id, actor.principal_ref, command_id)).fetchone()
                if previous:
                    if previous["payload_hash"] != digest:
                        raise TeamStateError("IDEMPOTENCY_CONFLICT", command_id)
                    result = json.loads(previous["result_json"])
                else:
                    result = action()
                    encoded = canonical_json(result)
                    self._conn.execute("INSERT INTO team_v2_commands VALUES(?,?,?,?,?)",
                                       (actor.project_id, actor.principal_ref, command_id, digest, encoded))
                    self._conn.execute(
                        "INSERT INTO team_v2_events(event_id,project_id,team_id,event_type,actor_ref,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
                        ("te-" + uuid.uuid4().hex, actor.project_id, result.get("team_id"), operation,
                         actor.principal_ref, canonical_json(_event_view(result)), now_iso()))
                    self._store._audit(operation, actor_id=actor.principal_ref,
                                       team_id=result.get("team_id"), command_id=command_id)
                self._conn.commit()
                if not previous:
                    from mewcode.orchestration.workbench_events import changed
                    changed('team', db_path=str(self._store.db_path), project_id=actor.project_id)
                return result
            except Exception:
                self._conn.rollback()
                raise

    def _team(self, actor: TeamActor, team_id: str, *, owner: bool = False) -> sqlite3.Row:
        actor.require("team.manage" if owner else "team.read")
        row = self._conn.execute("SELECT * FROM team_v2_instances WHERE team_id=? AND project_id=?",
                                 (team_id, actor.project_id)).fetchone()
        # Do not disclose whether an out-of-scope ID exists.
        if row is None:
            raise TeamStateError("TEAM_NOT_ACCESSIBLE", team_id)
        if row["owner_ref"] != actor.principal_ref:
            member = None if owner else self._conn.execute(
                "SELECT 1 FROM team_v2_memberships m LEFT JOIN team_v2_member_states s USING(membership_id) WHERE m.team_id=? AND m.principal_ref=? AND COALESCE(s.state,'active')!='left' LIMIT 1",
                (team_id, actor.principal_ref)).fetchone()
            if member is None:
                raise TeamStateError("TEAM_NOT_ACCESSIBLE", team_id)
        return row

    def _owned_active(self, actor: TeamActor, team_id: str) -> None:
        row = self._team(actor, team_id, owner=True)
        if row["lifecycle"] != "active":
            raise TeamStateError("TEAM_NOT_ACTIVE", team_id)

    @staticmethod
    def _expected(row: sqlite3.Row, version: int, epoch: int) -> None:
        if type(version) is not int or type(epoch) is not int:
            raise TeamStateError("STATE_VERSION_CONFLICT", "integer versions required")
        if row["control_epoch"] != epoch:
            raise TeamStateError("STALE_CONTROL_EPOCH", "team control changed")
        if row["state_version"] != version:
            raise TeamStateError("STATE_VERSION_CONFLICT", "team state changed")

    def register_spec(self, actor: TeamActor, spec: TeamSpec, *, command_id: str) -> dict:
        spec = TeamSpec.model_validate(spec.model_dump())
        body = spec.model_dump(mode="json")
        digest = _digest(body)

        def action() -> dict:
            previous = self._conn.execute(
                "SELECT spec_hash FROM team_v2_specs WHERE project_id=? AND spec_id=? AND version=?",
                (actor.project_id, spec.spec_id, spec.version)).fetchone()
            if previous and previous["spec_hash"] != digest:
                raise TeamStateError("SPEC_VERSION_CONFLICT", spec.spec_id)
            if not previous:
                self._conn.execute("INSERT INTO team_v2_specs VALUES(?,?,?,?,?,?,?)",
                                   (actor.project_id, spec.spec_id, spec.version, canonical_json(body),
                                    digest, actor.principal_ref, now_iso()))
            return {"spec_id": spec.spec_id, "version": spec.version, "spec_hash": digest}

        return self._write(actor, "team_spec_registered", command_id, body,
                           lambda: actor.require("team.plan"), action)

    def spec_catalog(self, actor: TeamActor) -> tuple[TeamSpec, ...]:
        actor.require("team.plan")
        with self._lock:
            rows = self._conn.execute(
                "SELECT spec_json,spec_hash FROM team_v2_specs WHERE project_id=? ORDER BY spec_id,version",
                (actor.project_id,)).fetchall()
            result = []
            for row in rows:
                body = json.loads(row["spec_json"])
                if _digest(body) != row["spec_hash"]:
                    raise TeamStateError("SNAPSHOT_CORRUPT", "spec hash mismatch")
                result.append(TeamSpec.model_validate(body))
            return tuple(result)

    def create_team(self, actor: TeamActor, ref: SpecRef, *, name: str, command_id: str) -> dict:
        name = _text(name)
        ref = SpecRef.model_validate(ref.model_dump())

        def action() -> dict:
            exists = self._conn.execute(
                "SELECT 1 FROM team_v2_specs WHERE project_id=? AND spec_id=? AND version=?",
                (actor.project_id, ref.spec_id, ref.version)).fetchone()
            if not exists:
                raise TeamStateError("UNKNOWN_SPEC", ref.spec_id)
            team_id = "team-" + uuid.uuid4().hex
            self._conn.execute("INSERT INTO team_v2_instances VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (team_id, actor.project_id, name, ref.spec_id, ref.version,
                                actor.principal_ref, "active", 1, 1, now_iso()))
            return dict(self._conn.execute("SELECT * FROM team_v2_instances WHERE team_id=?", (team_id,)).fetchone())

        return self._write(actor, "team_created", command_id, {"ref": ref.model_dump(), "name": name},
                           lambda: actor.require("team.manage"), action)

    def get_team(self, actor: TeamActor, team_id: str) -> dict:
        with self._lock:
            return dict(self._team(actor, team_id))

    def save_draft_run(self, actor: TeamActor, team_id: str, compiled: CompiledTeam, *,
                       objective: str, budget_ref: str, expected_version: int,
                       control_epoch: int, command_id: str) -> dict:
        """Internal host API; TeamService supplies freshly compiled snapshots."""
        compiled = validate_compiled_snapshot(compiled)
        objective, budget_ref = _text(objective), _text(budget_ref)
        payload = {"team_id": team_id, "compiled": compiled.model_dump(mode="json"),
                   "objective": objective, "budget_ref": budget_ref,
                   "expected_version": expected_version, "control_epoch": control_epoch}

        def action() -> dict:
            team = self._team(actor, team_id, owner=True)
            self._expected(team, expected_version, control_epoch)
            if (compiled.spec.spec_id, compiled.spec.version) != (team["spec_id"], team["spec_version"]):
                raise TeamStateError("SPEC_VERSION_CONFLICT", "run differs from team definition")
            run = TeamRun(team_run_id="tr-" + uuid.uuid4().hex, team_id=team_id,
                          project_id=actor.project_id, resolved_spec_hash=compiled.resolved_spec_hash,
                          objective=objective, budget_ref=budget_ref, control_epoch=control_epoch)
            self._conn.execute("INSERT INTO team_v2_runs VALUES(?,?,?,?,?)",
                               (run.team_run_id, team_id, run.model_dump_json(),
                                compiled.model_dump_json(), now_iso()))
            self._conn.execute("UPDATE team_v2_instances SET state_version=state_version+1 WHERE team_id=?", (team_id,))
            return {**run.model_dump(mode="json"), "team_state_version": expected_version + 1}

        return self._write(actor, "team_run_planned", command_id, payload,
                           lambda: self._owned_active(actor, team_id), action)

    def _run(self, actor: TeamActor, team_id: str, team_run_id: str) -> sqlite3.Row:
        team = self._team(actor, team_id)
        if team["owner_ref"] != actor.principal_ref and self._conn.execute(
                "SELECT 1 FROM team_v2_memberships m LEFT JOIN team_v2_member_states s USING(membership_id) WHERE m.team_id=? AND m.team_run_id=? AND m.principal_ref=? AND COALESCE(s.state,'active')!='left'",
                (team_id, team_run_id, actor.principal_ref)).fetchone() is None:
            raise TeamStateError("RUN_NOT_ACCESSIBLE", team_run_id)
        row = self._conn.execute("SELECT * FROM team_v2_runs WHERE team_id=? AND team_run_id=?",
                                 (team_id, team_run_id)).fetchone()
        if row is None:
            raise TeamStateError("RUN_NOT_ACCESSIBLE", team_run_id)
        return row

    def get_run(self, actor: TeamActor, team_id: str, team_run_id: str) -> dict:
        with self._lock:
            row = self._run(actor, team_id, team_run_id)
            run = TeamRun.model_validate_json(row["run_json"])
            payload = row['compiled_json']
            if self._conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='team_v2_plan_revisions'").fetchone():
                revision = self._conn.execute('SELECT compiled_json FROM team_v2_plan_revisions WHERE team_run_id=? AND revision=?', (team_run_id, run.task_set_revision)).fetchone()
                if revision:
                    payload = revision[0]
            compiled = validate_compiled_snapshot(json.loads(payload))
            if (run.resolved_spec_hash != compiled.resolved_spec_hash
                    or run.team_id != team_id or run.team_run_id != team_run_id
                    or run.project_id != actor.project_id):
                raise TeamStateError("SNAPSHOT_CORRUPT", "run snapshot mismatch")
            if self._team(actor, team_id)["owner_ref"] == actor.principal_ref:
                return {"run": run.model_dump(mode="json"), "compiled": compiled.model_dump(mode="json")}
            # Membership grants its own contracts, not every other role's
            # objectives, source grant names, and private planning material.
            roles = {row[0] for row in self._conn.execute(
                "SELECT m.role_id FROM team_v2_memberships m LEFT JOIN team_v2_member_states s USING(membership_id) WHERE m.team_id=? AND m.team_run_id=? AND m.principal_ref=? AND COALESCE(s.state,'active')!='left'",
                (team_id, team_run_id, actor.principal_ref)).fetchall()}
            return {"run": {key: getattr(run, key) for key in (
                "team_id", "team_run_id", "state", "task_set_revision", "control_epoch")},
                "tasks": [task.model_dump(mode="json") for task in compiled.spec.tasks
                          if task.role_id in roles or task.reviewer_role in roles]}

    def add_member(self, actor: TeamActor, team_id: str, team_run_id: str, *,
                   alias: str, role_id: str, principal_ref: str, expected_version: int,
                   control_epoch: int, command_id: str) -> dict:
        alias = TypeAdapter(Identifier).validate_python(alias)
        role_id = TypeAdapter(Identifier).validate_python(role_id)
        principal_ref = _text(principal_ref)
        if alias.casefold() == "lead":
            raise TeamStateError("RESERVED_ALIAS", "lead resolves to this team's owner")
        payload = dict(team_id=team_id, team_run_id=team_run_id, alias=alias, role_id=role_id,
                       principal_ref=principal_ref, expected_version=expected_version, control_epoch=control_epoch)

        def action() -> dict:
            team = self._team(actor, team_id, owner=True)
            self._expected(team, expected_version, control_epoch)
            run_row = self._run(actor, team_id, team_run_id)
            compiled = validate_compiled_snapshot(json.loads(run_row["compiled_json"]))
            role = next((role for role in compiled.spec.roles if role.role_id == role_id), None)
            if role is None:
                raise TeamStateError("INVALID_ROLE", role_id)
            if self._conn.execute(
                    "SELECT 1 FROM team_v2_memberships WHERE team_id=? AND team_run_id=? AND alias=?",
                    (team_id, team_run_id, alias)).fetchone():
                raise TeamStateError("ALIAS_CONFLICT", alias)
            members = self._conn.execute(
                "SELECT role_id,principal_ref FROM team_v2_memberships WHERE team_id=? AND team_run_id=?",
                (team_id, team_run_id)).fetchall()
            if sum(member["role_id"] == role_id for member in members) >= role.max_members:
                raise TeamStateError("MEMBER_LIMIT", role_id)
            for member in members:
                if member["principal_ref"] == principal_ref:
                    if member["role_id"] == role_id:
                        raise TeamStateError("DUPLICATE_MEMBER", role_id)
                    other = next(role for role in compiled.spec.roles if role.role_id == member["role_id"])
                    pairs = {(task.role_id, task.reviewer_role) for task in compiled.spec.tasks}
                    if (other.role_id in role.independent_from or role_id in other.independent_from
                            or (compiled.effective_policy.self_acceptance_forbidden and (
                                (role_id, other.role_id) in pairs or (other.role_id, role_id) in pairs))):
                        raise TeamStateError("SELF_ACCEPTANCE", role_id)
            membership_id = "member-" + uuid.uuid4().hex
            self._conn.execute("INSERT INTO team_v2_memberships VALUES(?,?,?,?,?,?)",
                               (membership_id, team_id, team_run_id, alias, role_id, principal_ref))
            self._conn.execute("UPDATE team_v2_instances SET state_version=state_version+1 WHERE team_id=?", (team_id,))
            return {"membership_id": membership_id, "team_id": team_id, "team_run_id": team_run_id,
                    "alias": alias, "role_id": role_id, "principal_ref": principal_ref,
                    "team_state_version": expected_version + 1}

        return self._write(actor, "team_member_added", command_id, payload,
                           lambda: self._owned_active(actor, team_id), action)

    def resolve_member(self, actor: TeamActor, team_id: str, team_run_id: str, alias: str) -> str:
        alias = TypeAdapter(Identifier).validate_python(alias)
        with self._lock:
            self._run(actor, team_id, team_run_id)
            team = self._team(actor, team_id)
            if alias.casefold() == "lead":
                return str(team["owner_ref"])
            member = self._conn.execute(
                "SELECT m.principal_ref FROM team_v2_memberships m LEFT JOIN team_v2_member_states s USING(membership_id) WHERE m.team_id=? AND m.team_run_id=? AND m.alias=? AND COALESCE(s.state,'active')!='left'",
                (team_id, team_run_id, alias)).fetchone()
            if member is None:
                raise TeamStateError("MEMBER_NOT_FOUND", alias)
            return str(member["principal_ref"])
