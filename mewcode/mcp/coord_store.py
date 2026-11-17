"""SQLite-backed state for the local Codex/Grok coordination broker."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from .coord_protocol import (
    LEASE_SECONDS,
    NO_PROGRESS_LIMIT,
    PROTOCOL_VERSION,
    ROLE_DEFINITIONS,
    TERMINAL_STATES,
    CoordinationError,
    MessageEnvelope,
    TaskKind,
    TaskState,
    capabilities_hash,
    capability_names,
    canonical_json,
    now_iso,
    normalize_capabilities,
    parse_iso,
    validate_task_limits,
    validate_write_mode,
)


def _json(value: Any) -> str:
    return canonical_json(value)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


class CoordinationStore:
    """Transactional coordination state.

    The broker is intentionally the only writer for this state.  It stores no
    shell/file permissions and never executes a task; those are constraints
    handed to the executor by the caller.
    """

    def __init__(self, db_path: str | Path, capabilities_dir: str | Path | None = None,
                 *, enable_team_v2: bool = False) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.capabilities_dir = Path(capabilities_dir or self.db_path.parent / "capabilities")
        self.capabilities_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._init_schema()
        self._closed = False
        self._teams_v2 = None
        if enable_team_v2:
            from mewcode.teams.store_v2 import TeamStoreV2
            self._teams_v2 = TeamStoreV2(self)

    @property
    def teams_v2(self):
        if self._teams_v2 is None:
            raise CoordinationError("TEAM_V2_DISABLED: explicit opt-in is required")
        return self._teams_v2

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __enter__(self) -> "CoordinationStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id TEXT PRIMARY KEY,
                    model_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    capabilities_hash TEXT,
                    capabilities_stale INTEGER NOT NULL DEFAULT 1,
                    lease_until TEXT,
                    last_heartbeat TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS capabilities (
                    agent_id TEXT PRIMARY KEY REFERENCES agents(agent_id) ON DELETE CASCADE,
                    manifest_json TEXT NOT NULL,
                    capabilities_hash TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    stale INTEGER NOT NULL DEFAULT 0,
                    target TEXT,
                    changed INTEGER NOT NULL DEFAULT 0,
                    requires_resync INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    parent_task_id TEXT REFERENCES tasks(task_id),
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    creator_id TEXT NOT NULL,
                    target_agent_id TEXT,
                    assignee_id TEXT,
                    state TEXT NOT NULL,
                    allowed_tools_json TEXT NOT NULL,
                    allowed_skills_json TEXT NOT NULL,
                    allowed_paths_json TEXT NOT NULL,
                    forbidden_paths_json TEXT NOT NULL,
                    write_mode TEXT NOT NULL,
                    capabilities_hash TEXT NOT NULL,
                    acceptance_criteria_json TEXT NOT NULL,
                    max_turns INTEGER NOT NULL,
                    deadline TEXT NOT NULL,
                    lease_until TEXT,
                    last_progress_at TEXT NOT NULL,
                    no_progress_cycles INTEGER NOT NULL DEFAULT 0,
                    execution_turns INTEGER NOT NULL DEFAULT 0,
                    retries INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT,
                    evidence_json TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tasks_state_idx ON tasks(state, created_at);
                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    sender_id TEXT NOT NULL,
                    recipient_id TEXT,
                    kind TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    correlation_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    acked_at TEXT,
                    acked_by TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS messages_task_sequence_idx
                    ON messages(task_id, sequence);
                CREATE TABLE IF NOT EXISTS heartbeats (
                    heartbeat_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    task_id TEXT,
                    progress TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    actor_id TEXT,
                    task_id TEXT,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def _audit(self, event_type: str, actor_id: str | None = None, task_id: str | None = None, **details: Any) -> None:
        self._conn.execute(
            "INSERT INTO audit_events(event_id,event_type,actor_id,task_id,details_json,created_at) VALUES(?,?,?,?,?,?)",
            (_new_id("evt"), event_type, actor_id, task_id, _json(details), now_iso()),
        )

    def _agent_row(self, agent_id: str) -> sqlite3.Row:
        row = self._conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        if row is None:
            raise CoordinationError(f"unknown agent: {agent_id}")
        return row

    def _task_row(self, task_id: str) -> sqlite3.Row:
        row = self._conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise CoordinationError(f"unknown task: {task_id}")
        if _loads(row["metadata_json"], {}).get("team_v2"):
            raise CoordinationError("PROTOCOL_VERSION_MISMATCH: use the v2 team runtime")
        return row

    def register_agent(
        self,
        *,
        agent_id: str,
        model_id: str,
        role: str,
        session_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        expected = ROLE_DEFINITIONS.get(role)
        if expected is None:
            raise CoordinationError(f"unknown agent role: {role}")
        if agent_id != expected["agent_id"] or model_id != expected["model_id"]:
            raise CoordinationError("agent_id/model_id do not match the fixed role definition")
        if not session_id.strip():
            raise CoordinationError("session_id is required")
        now = now_iso()
        with self._lock, self._conn:
            existing = self._conn.execute("SELECT capabilities_hash FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
            existing_full = self._conn.execute("SELECT model_id, role, session_id, metadata_json FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
            metadata_json = _json(metadata or {})
            self._conn.execute(
                """INSERT INTO agents(agent_id,model_id,role,session_id,capabilities_hash,capabilities_stale,lease_until,last_heartbeat,metadata_json)
                   VALUES(?,?,?,?,NULL,1,NULL,?,?)
                   ON CONFLICT(agent_id) DO UPDATE SET model_id=excluded.model_id, role=excluded.role,
                     session_id=excluded.session_id, last_heartbeat=excluded.last_heartbeat, metadata_json=excluded.metadata_json""",
                (agent_id, model_id, role, session_id, now, metadata_json),
            )
            if (
                existing_full is None
                or existing_full["model_id"] != model_id
                or existing_full["role"] != role
                or existing_full["session_id"] != session_id
                or existing_full["metadata_json"] != metadata_json
            ):
                self._audit("agent_registered", agent_id, metadata=metadata or {}, replaced=existing is not None)
        return self.get_agent(agent_id)

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._agent_row(agent_id)
            return dict(row)

    def sync_capabilities(self, *, agent_id: str, manifest: dict[str, Any], source: str | None = None) -> dict[str, Any]:
        normalized = normalize_capabilities(manifest)
        if normalized["agent_id"] != agent_id:
            raise CoordinationError("capabilities.agent_id must match the registered agent")
        with self._lock, self._conn:
            agent = self._agent_row(agent_id)
            if normalized["model_id"] != agent["model_id"] or normalized["role"] != agent["role"]:
                raise CoordinationError("capabilities model/role do not match registered agent")
            digest = capabilities_hash(normalized)
            old_hash = agent["capabilities_hash"]
            changed = old_hash is not None and old_hash != digest
            generated_at = now_iso()
            src = source or normalized.get("source", "runtime")
            if old_hash == digest:
                existing_snapshot = self._conn.execute(
                    "SELECT manifest_json, generated_at, source, capabilities_hash, stale, target, changed, requires_resync FROM capabilities WHERE agent_id=?",
                    (agent_id,),
                ).fetchone()
                self._conn.execute(
                    "UPDATE agents SET last_heartbeat=? WHERE agent_id=?",
                    (generated_at, agent_id),
                )
                if existing_snapshot is not None:
                    value = _loads(existing_snapshot["manifest_json"], {})
                    return {
                        **value,
                        "generated_at": existing_snapshot["generated_at"],
                        "source": existing_snapshot["source"],
                        "capabilities_hash": existing_snapshot["capabilities_hash"],
                        "target": existing_snapshot["target"],
                        "changed": bool(existing_snapshot["changed"]),
                        "requires_resync": bool(existing_snapshot["requires_resync"]),
                        "stale": bool(existing_snapshot["stale"]),
                    }
            self._conn.execute(
                """INSERT INTO capabilities(agent_id,manifest_json,capabilities_hash,generated_at,source,stale,target,changed,requires_resync)
                   VALUES(?,?,?,?,?,0,?,?,?)
                   ON CONFLICT(agent_id) DO UPDATE SET manifest_json=excluded.manifest_json,
                     capabilities_hash=excluded.capabilities_hash, generated_at=excluded.generated_at,
                     source=excluded.source, stale=0, target=excluded.target, changed=excluded.changed,
                     requires_resync=excluded.requires_resync""",
                (agent_id, _json(normalized), digest, generated_at, src, "capabilities" if changed else None, int(changed), int(changed)),
            )
            self._conn.execute(
                "UPDATE agents SET capabilities_hash=?, capabilities_stale=0, last_heartbeat=? WHERE agent_id=?",
                (digest, generated_at, agent_id),
            )
            self._audit("capabilities_synced", agent_id, capabilities_hash=digest, changed=changed)
            snapshot = {
                **normalized,
                "generated_at": generated_at,
                "source": src,
                "capabilities_hash": digest,
                "target": "capabilities" if changed else None,
                "changed": changed,
                "requires_resync": changed,
                "stale": False,
            }
            self.capabilities_dir.mkdir(parents=True, exist_ok=True)
            (self.capabilities_dir / f"{agent_id}.json").write_text(_json(snapshot) + "\n", encoding="utf-8")
            return snapshot

    def get_capabilities(self, agent_id: str | None = None) -> list[dict[str, Any]] | dict[str, Any]:
        with self._lock:
            if agent_id:
                row = self._conn.execute("SELECT * FROM capabilities WHERE agent_id=?", (agent_id,)).fetchone()
                if row is None:
                    raise CoordinationError(f"no capabilities for agent: {agent_id}")
                value = _loads(row["manifest_json"], {})
                return {**value, "generated_at": row["generated_at"], "source": row["source"], "capabilities_hash": row["capabilities_hash"], "stale": bool(row["stale"]), "target": row["target"], "changed": bool(row["changed"]), "requires_resync": bool(row["requires_resync"])}
            rows = self._conn.execute("SELECT * FROM capabilities ORDER BY agent_id").fetchall()
            return [self.get_capabilities(row["agent_id"]) for row in rows]

    def create_task(
        self,
        *,
        creator_id: str,
        title: str,
        description: str,
        target_agent_id: str,
        capabilities_hash_value: str,
        allowed_tools: list[str],
        allowed_skills: list[str],
        allowed_paths: list[str],
        forbidden_paths: list[str],
        write_mode: str,
        acceptance_criteria: list[str] | dict[str, Any],
        max_turns: int,
        deadline: str,
        kind: str = TaskKind.EXECUTION.value,
        parent_task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if metadata and metadata.get("team_v2"):
            raise CoordinationError("PROTOCOL_VERSION_MISMATCH: legacy creation cannot create v2 tasks")
        if not title.strip() or not description.strip():
            raise CoordinationError("title and description are required")
        creator = self._agent_row(creator_id)
        if creator["role"] not in {"planner", "verifier"}:
            raise CoordinationError("only planner or verifier can create tasks")
        target = self._agent_row(target_agent_id)
        if target["role"] != "executor":
            raise CoordinationError("tasks must target the executor role")
        if kind not in {item.value for item in TaskKind}:
            raise CoordinationError("unknown task kind")
        if kind == TaskKind.VERIFICATION.value and creator["role"] != "verifier":
            raise CoordinationError("only verifier can create verification tasks")
        if kind == TaskKind.VERIFICATION.value and not parent_task_id:
            raise CoordinationError("verification tasks require parent_task_id")
        if parent_task_id:
            self._task_row(parent_task_id)
        for name, value in (("allowed_tools", allowed_tools), ("allowed_skills", allowed_skills), ("allowed_paths", allowed_paths), ("forbidden_paths", forbidden_paths)):
            if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
                raise CoordinationError(f"{name} must be a list of non-empty strings")
        if set(allowed_paths) & set(forbidden_paths):
            raise CoordinationError("allowed_paths and forbidden_paths overlap")
        mode = validate_write_mode(write_mode)
        max_turns = validate_task_limits(max_turns)
        if not deadline.strip():
            raise CoordinationError("deadline is required")
        try:
            deadline_at = parse_iso(deadline)
        except ValueError as exc:
            raise CoordinationError("deadline must be an ISO timestamp") from exc
        if deadline_at <= parse_iso(now_iso()):
            raise CoordinationError("deadline must be in the future")
        if not isinstance(acceptance_criteria, (list, dict)) or not acceptance_criteria:
            raise CoordinationError("acceptance_criteria is required")
        if not capabilities_hash_value:
            raise CoordinationError("capabilities_hash is required")
        task_id = _new_id("task")
        now = now_iso()
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM capabilities WHERE agent_id=? AND capabilities_hash=? AND stale=0", (target_agent_id, capabilities_hash_value)).fetchone()
            if row is None:
                raise CoordinationError("target capabilities_hash is not current")
            target_metadata = _loads(target["metadata_json"], {})
            if target_metadata.get("runner") == "mewcode-coord-worker":
                readiness = str(target_metadata.get("readiness", "unknown"))
                if readiness != "ready":
                    reason = str(target_metadata.get("readiness_reason", "")).strip()
                    suffix = f": {reason}" if reason else ""
                    raise CoordinationError(f"target executor is not ready ({readiness}){suffix}")
            self._conn.execute(
                """INSERT INTO tasks(task_id,parent_task_id,kind,title,description,creator_id,target_agent_id,assignee_id,state,
                   allowed_tools_json,allowed_skills_json,allowed_paths_json,forbidden_paths_json,write_mode,capabilities_hash,
                   acceptance_criteria_json,max_turns,deadline,lease_until,last_progress_at,no_progress_cycles,execution_turns,
                   retries,result_json,evidence_json,metadata_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,0,0,0,NULL,NULL,?,?,?)""",
                (task_id, parent_task_id, kind, title, description, creator_id, target_agent_id, None, TaskState.QUEUED.value,
                 _json(allowed_tools), _json(allowed_skills), _json(allowed_paths), _json(forbidden_paths), mode.value, capabilities_hash_value,
                 _json(acceptance_criteria), max_turns, deadline, now, _json(metadata or {}), now, now),
            )
            self._audit("task_created", creator_id, task_id, target_agent_id=target_agent_id, kind=kind)
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._task_row(task_id)
            value = dict(row)
            for field in ("allowed_tools_json", "allowed_skills_json", "allowed_paths_json", "forbidden_paths_json", "acceptance_criteria_json", "result_json", "evidence_json", "metadata_json"):
                value[field.removesuffix("_json")] = _loads(value.pop(field), None if field in {"result_json", "evidence_json"} else {})
            return value

    def list_tasks(
        self,
        *,
        states: set[str] | None = None,
        target_agent_id: str | None = None,
        limit: int = 20,
        before_deadline: bool = False,
    ) -> list[dict[str, Any]]:
        """List a bounded task snapshot for an external coordinator worker.

        ``before_deadline`` is for executors polling queued work.  It filters
        stale queued rows in SQL before applying the limit, so old abandoned
        tasks cannot starve newer work from the worker's bounded poll.
        """
        limit = max(1, min(int(limit), 100))
        with self._lock:
            sql = "SELECT task_id FROM tasks WHERE COALESCE(json_extract(metadata_json,'$.team_v2'),0)=0"
            params: list[Any] = []
            if states:
                values = sorted(str(item) for item in states)
                sql += f" AND state IN ({','.join('?' for _ in values)})"
                params.extend(values)
            if target_agent_id:
                sql += " AND target_agent_id=?"
                params.append(target_agent_id)
            if before_deadline:
                sql += " AND deadline>?"
                params.append(now_iso())
            sql += " ORDER BY created_at LIMIT ?"
            params.append(limit)
            return [self.get_task(row["task_id"]) for row in self._conn.execute(sql, params).fetchall()]

    def _reap_expired(self, now: str) -> None:
        rows = self._conn.execute("SELECT task_id,assignee_id,state FROM tasks WHERE COALESCE(json_extract(metadata_json,'$.team_v2'),0)=0 AND lease_until IS NOT NULL AND lease_until < ? AND state IN (?,?)", (now, TaskState.CLAIMED.value, TaskState.RUNNING.value)).fetchall()
        for row in rows:
            self._conn.execute("UPDATE tasks SET state=?, lease_until=NULL, updated_at=? WHERE task_id=?", (TaskState.EXPIRED.value, now, row["task_id"]))
            self._audit("task_expired", row["assignee_id"], row["task_id"])

    def claim_task(self, *, task_id: str, agent_id: str) -> dict[str, Any]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                now = now_iso()
                self._reap_expired(now)
                agent = self._agent_row(agent_id)
                if agent["role"] != "executor":
                    raise CoordinationError("only executor can claim tasks")
                if bool(agent["capabilities_stale"]) or not agent["capabilities_hash"]:
                    raise CoordinationError("agent capabilities must be synchronized before claiming")
                task = self._task_row(task_id)
                if task["target_agent_id"] != agent_id:
                    raise CoordinationError("task is assigned to a different target agent")
                if task["state"] not in {TaskState.QUEUED.value, TaskState.EXPIRED.value}:
                    raise CoordinationError(f"task cannot be claimed from state {task['state']}")
                if parse_iso(now) > parse_iso(task["deadline"]):
                    self._conn.execute("UPDATE tasks SET state=?,updated_at=? WHERE task_id=?", (TaskState.EXPIRED.value, now, task_id))
                    self._audit("task_deadline_expired", agent_id, task_id)
                    raise CoordinationError("task deadline has passed")
                if task["capabilities_hash"] != agent["capabilities_hash"]:
                    raise CoordinationError("capabilities_hash mismatch; resync before claiming")
                manifest_row = self._conn.execute("SELECT manifest_json FROM capabilities WHERE agent_id=? AND stale=0", (agent_id,)).fetchone()
                manifest = _loads(manifest_row[0] if manifest_row else None, {})
                if not set(_loads(task["allowed_tools_json"], [])) <= capability_names(manifest, "tools"):
                    raise CoordinationError("task requires tools not present in the current capability snapshot")
                if not set(_loads(task["allowed_skills_json"], [])) <= capability_names(manifest, "skills"):
                    raise CoordinationError("task requires skills not present in the current capability snapshot")
                lease_until = (parse_iso(now) + timedelta(seconds=LEASE_SECONDS)).isoformat().replace("+00:00", "Z")
                self._conn.execute("UPDATE tasks SET state=?,assignee_id=?,lease_until=?,updated_at=? WHERE task_id=?", (TaskState.CLAIMED.value, agent_id, lease_until, now, task_id))
                self._audit("task_claimed", agent_id, task_id, lease_until=lease_until)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return self.get_task(task_id)

    def heartbeat(self, *, agent_id: str, task_id: str | None = None, progress: str | None = None, progress_made: bool = False) -> dict[str, Any]:
        with self._lock, self._conn:
            now = now_iso()
            agent = self._agent_row(agent_id)
            self._conn.execute("UPDATE agents SET last_heartbeat=?, lease_until=? WHERE agent_id=?", (now, (parse_iso(now) + timedelta(seconds=LEASE_SECONDS)).isoformat().replace("+00:00", "Z"), agent_id))
            task_value = None
            if task_id:
                task = self._task_row(task_id)
                if task["assignee_id"] != agent_id:
                    raise CoordinationError("heartbeat agent does not own task")
                if task["state"] in TERMINAL_STATES:
                    raise CoordinationError("cannot heartbeat a terminal task")
                last_progress = parse_iso(task["last_progress_at"])
                if progress_made:
                    last_progress = parse_iso(now)
                    cycles = 0
                else:
                    cycles = int(task["no_progress_cycles"])
                    if parse_iso(now) - last_progress >= timedelta(seconds=LEASE_SECONDS * NO_PROGRESS_LIMIT):
                        self._conn.execute("UPDATE tasks SET state=?,lease_until=NULL,updated_at=? WHERE task_id=?", (TaskState.BLOCKED.value, now, task_id))
                        self._audit("task_blocked_no_progress", agent_id, task_id)
                        raise CoordinationError("task blocked after two lease periods without progress")
                lease_until = (parse_iso(now) + timedelta(seconds=LEASE_SECONDS)).isoformat().replace("+00:00", "Z")
                state = TaskState.RUNNING.value if task["state"] == TaskState.CLAIMED.value else task["state"]
                self._conn.execute("UPDATE tasks SET state=?,lease_until=?,last_progress_at=?,no_progress_cycles=?,updated_at=? WHERE task_id=?", (state, lease_until, last_progress.isoformat().replace("+00:00", "Z"), cycles, now, task_id))
                self._conn.execute("INSERT INTO heartbeats(heartbeat_id,agent_id,task_id,progress,created_at) VALUES(?,?,?,?,?)", (_new_id("hb"), agent_id, task_id, progress, now))
                self._audit("heartbeat", agent_id, task_id, progress=progress, progress_made=progress_made)
                task_value = self.get_task(task_id)
            else:
                self._conn.execute("INSERT INTO heartbeats(heartbeat_id,agent_id,task_id,progress,created_at) VALUES(?,?,?,?,?)", (_new_id("hb"), agent_id, None, progress, now))
                self._audit("heartbeat", agent_id, progress=progress)
            return {"agent_id": agent_id, "task": task_value, "heartbeat_at": now, "lease_seconds": LEASE_SECONDS}

    def post_message(self, *, task_id: str, sender_id: str, recipient_id: str | None, kind: str, payload: dict[str, Any], message_id: str | None = None, correlation_id: str | None = None) -> dict[str, Any]:
        self._task_row(task_id)
        self._agent_row(sender_id)
        if recipient_id:
            self._agent_row(recipient_id)
        mid = message_id or _new_id("msg")
        with self._lock, self._conn:
            existing = self._conn.execute("SELECT * FROM messages WHERE message_id=?", (mid,)).fetchone()
            if existing:
                return self._message_dict(existing, duplicate=True)
            seq = int(self._conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM messages WHERE task_id=?", (task_id,)).fetchone()[0])
            created = now_iso()
            self._conn.execute("INSERT INTO messages(message_id,task_id,sender_id,recipient_id,kind,sequence,correlation_id,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (mid, task_id, sender_id, recipient_id, kind, seq, correlation_id, _json(payload), created))
            self._audit("message_posted", sender_id, task_id, message_id=mid, kind=kind)
            row = self._conn.execute("SELECT * FROM messages WHERE message_id=?", (mid,)).fetchone()
            return self._message_dict(row)

    def _message_dict(self, row: sqlite3.Row, duplicate: bool = False) -> dict[str, Any]:
        envelope = MessageEnvelope(PROTOCOL_VERSION, row["message_id"], row["task_id"], row["sender_id"], row["recipient_id"], row["kind"], row["sequence"], row["correlation_id"], _loads(row["payload_json"], {}), row["created_at"])
        value = envelope.as_dict()
        value.update({"acked": row["acked_at"] is not None, "acked_at": row["acked_at"], "acked_by": row["acked_by"]})
        if duplicate:
            value["duplicate"] = True
        return value

    def poll_messages(self, *, recipient_id: str, task_id: str | None = None, include_acked: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        self._agent_row(recipient_id)
        limit = max(1, min(int(limit), 200))
        with self._lock:
            sql = "SELECT * FROM messages WHERE (recipient_id=? OR recipient_id IS NULL)"
            params: list[Any] = [recipient_id]
            if task_id:
                sql += " AND task_id=?"
                params.append(task_id)
            if not include_acked:
                sql += " AND acked_at IS NULL"
            sql += " ORDER BY created_at, sequence LIMIT ?"
            params.append(limit)
            return [self._message_dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def ack_message(self, *, message_id: str, agent_id: str) -> dict[str, Any]:
        self._agent_row(agent_id)
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()
            if row is None:
                raise CoordinationError(f"unknown message: {message_id}")
            if row["recipient_id"] not in {None, agent_id}:
                raise CoordinationError("message is addressed to another agent")
            if row["acked_at"] is None:
                self._conn.execute("UPDATE messages SET acked_at=?,acked_by=? WHERE message_id=?", (now_iso(), agent_id, message_id))
                self._audit("message_acked", agent_id, row["task_id"], message_id=message_id)
                row = self._conn.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()
            result = self._message_dict(row)
            result["idempotent"] = True
            return result

    def update_task(self, *, task_id: str, actor_id: str, state: str | None = None, progress: str | None = None, execution_turns: int | None = None, retries: int | None = None, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        actor = self._agent_row(actor_id)
        with self._lock, self._conn:
            task = self._task_row(task_id)
            if task["assignee_id"] not in {None, actor_id} and actor["role"] != "verifier":
                raise CoordinationError("actor does not own task")
            updates: dict[str, Any] = {"updated_at": now_iso()}
            if state:
                try:
                    desired = TaskState(state)
                except ValueError as exc:
                    raise CoordinationError(f"unknown task state: {state}") from exc
                allowed = {
                    TaskState.QUEUED: {TaskState.CLAIMED, TaskState.EXPIRED},
                    TaskState.CLAIMED: {TaskState.RUNNING, TaskState.BLOCKED, TaskState.FAILED, TaskState.EXPIRED},
                    TaskState.RUNNING: {TaskState.VERIFYING, TaskState.BLOCKED, TaskState.FAILED, TaskState.EXPIRED},
                    TaskState.VERIFYING: {TaskState.BLOCKED, TaskState.FAILED},
                }
                current = TaskState(task["state"])
                if desired != current and desired not in allowed.get(current, set()):
                    raise CoordinationError(f"invalid state transition {current.value} -> {desired.value}")
                updates["state"] = desired.value
            if progress is not None:
                updates["last_progress_at"] = now_iso()
            if execution_turns is not None:
                if execution_turns < 0 or execution_turns > int(task["max_turns"]):
                    raise CoordinationError("execution_turns exceeds max_turns")
                updates["execution_turns"] = execution_turns
            if retries is not None:
                if retries < 0 or retries > 1:
                    raise CoordinationError("retries exceeds the single retry limit")
                updates["retries"] = retries
            if evidence is not None:
                updates["evidence_json"] = _json(evidence)
            assignments = ",".join(f"{key}=?" for key in updates)
            self._conn.execute(f"UPDATE tasks SET {assignments} WHERE task_id=?", (*updates.values(), task_id))
            self._audit("task_updated", actor_id, task_id, state=state, progress=progress)
        return self.get_task(task_id)

    def submit_result(self, *, task_id: str, agent_id: str, result: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
        if not result or not evidence:
            raise CoordinationError("result and evidence are required")
        actor = self._agent_row(agent_id)
        if actor["role"] != "executor":
            raise CoordinationError("only executor can submit results")
        with self._lock, self._conn:
            task = self._task_row(task_id)
            if task["assignee_id"] != agent_id or task["state"] not in {TaskState.CLAIMED.value, TaskState.RUNNING.value}:
                raise CoordinationError("executor does not own a runnable task")
            now = now_iso()
            self._conn.execute("UPDATE tasks SET state=?,result_json=?,evidence_json=?,lease_until=NULL,updated_at=? WHERE task_id=?", (TaskState.VERIFYING.value, _json(result), _json(evidence), now, task_id))
            self._audit("result_submitted", agent_id, task_id)
        return self.get_task(task_id)

    def close_task(self, *, task_id: str, verifier_id: str, accepted: bool, evidence: dict[str, Any] | None = None, reason: str | None = None) -> dict[str, Any]:
        verifier = self._agent_row(verifier_id)
        if verifier["role"] != "verifier":
            raise CoordinationError("only verifier can close tasks")
        with self._lock, self._conn:
            task = self._task_row(task_id)
            if task["state"] != TaskState.VERIFYING.value:
                raise CoordinationError("only verifying tasks can be closed")
            if accepted and not evidence:
                raise CoordinationError("accepted closure requires verification evidence")
            child_rows = self._conn.execute("SELECT state FROM tasks WHERE parent_task_id=? AND kind=?", (task_id, TaskKind.VERIFICATION.value)).fetchall()
            if accepted and child_rows and not any(row["state"] == TaskState.COMPLETED.value for row in child_rows):
                raise CoordinationError("verification child task is not completed")
            now = now_iso()
            state = TaskState.COMPLETED.value if accepted else TaskState.FAILED.value
            self._conn.execute("UPDATE tasks SET state=?,evidence_json=?,metadata_json=json_set(metadata_json,'$.close_reason',?),updated_at=? WHERE task_id=?", (state, _json(evidence or {}), reason, now, task_id))
            self._audit("task_closed", verifier_id, task_id, accepted=accepted, reason=reason)
        return self.get_task(task_id)
