"""Host-only durable native transaction lifecycle, independent of child output.

An unfinished publication fences the workspace across application restarts.
Startup never guesses that an expired PID/lease means cleanup or commit.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from mewcode.sandbox.windows import NativeBoundaryUnavailable

CURRENT_TRANSACTION: ContextVar[str] = ContextVar("native_transaction", default="")
TERMINAL = frozenset({"published", "no_changes", "rejected", "rolled_back"})
STATES = TERMINAL | {"preparing", "executing", "native_done", "verified", "publishing",
                     "recovery_required", "cleanup_unknown"}
MAX_RECORD_BYTES = 2 * 1024 * 1024


class NativeTransactionBusy(NativeBoundaryUnavailable):
    """An exclusive host claim is held; this is not evidence of a crash."""


def default_root() -> Path:
    from mewcode.runtime_paths import user_data_root
    return user_data_root() / "native"


def workspace_key(workspace: Path) -> str:
    return os.path.normcase(str(workspace.resolve(strict=True)))


class TransactionRegistry:
    def __init__(self, root: Path | None = None):
        self.root = root if root is not None else default_root()
        self.path = self.root / "transactions.sqlite3"

    @contextmanager
    def _db(self):
        from mewcode.sandbox.lpac_runtime import checked_path
        self.root.mkdir(parents=True, exist_ok=True)
        checked_path(self.root)
        for suffix in ("", "-journal", "-wal", "-shm"):
            path = Path(str(self.path) + suffix)
            if os.path.lexists(path):
                checked_path(path, file=True)
        db = sqlite3.connect(self.path, timeout=2)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE IF NOT EXISTS native_transactions "
                       "(id TEXT PRIMARY KEY, workspace TEXT NOT NULL, state TEXT NOT NULL, "
                       "updated REAL NOT NULL, record TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS native_workspace ON native_transactions(workspace)")
            yield db
        except sqlite3.Error as exc:
            raise NativeBoundaryUnavailable("Native transaction ledger is unavailable") from exc
        finally:
            db.close()

    @staticmethod
    def _encode(record):
        raw = json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False)
        if len(raw.encode("utf-8")) > MAX_RECORD_BYTES:
            raise NativeBoundaryUnavailable("Native transaction record exceeds bound")
        return raw

    def begin(self, workspace: Path, *, task_id: str, policy_revision: str,
              independent_command: str | None = None) -> dict:
        if not task_id or len(task_id) > 256:
            raise NativeBoundaryUnavailable("A host task identity is required")
        key, identity = workspace_key(workspace), uuid.uuid4().hex
        record = {"id": identity, "workspace": str(workspace), "task_id": task_id,
                  "policy_revision": policy_revision, "state": "preparing",
                  "created": time.time(), "pid": os.getpid()}
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            pending = db.execute("SELECT state,record FROM native_transactions WHERE workspace=?", (key,)).fetchall()
            dependencies = []
            for state, raw in pending:
                if state in TERMINAL:
                    continue
                from .independent_execution import private_cleanup_dependency
                dependency = (private_cleanup_dependency(json.loads(raw), workspace, independent_command)
                              if independent_command else None)
                if dependency is None:
                    raise NativeBoundaryUnavailable("Workspace has an unsettled native transaction; reconcile it first")
                dependencies.append(dependency)
            if independent_command:
                record.update(execution_lane="offline_discard", deferred_cleanup=dependencies)
            db.execute("INSERT INTO native_transactions VALUES(?,?,?,?,?)",
                       (identity, key, "preparing", time.time(), self._encode(record)))
            db.commit()
        return record

    def update(self, identity: str, state: str, **fields) -> dict:
        if state not in STATES or {"id", "workspace", "task_id", "policy_revision", "state"} & fields.keys():
            raise ValueError("Invalid native transaction update")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,record FROM native_transactions WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise NativeBoundaryUnavailable("Native transaction not registered")
            if row[0] in TERMINAL and state != row[0]:
                raise NativeBoundaryUnavailable("A settled native transaction cannot be reopened")
            record = json.loads(row[1])
            record.update(fields, state=state)
            db.execute("UPDATE native_transactions SET state=?,updated=?,record=? WHERE id=?",
                       (state, time.time(), self._encode(record), identity))
            db.commit()
        return record

    def pending(self, workspace: Path | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        from mewcode.sandbox.lpac_runtime import checked_path
        checked = checked_path(self.path, file=True)
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = Path(str(checked) + suffix)
            if os.path.lexists(sidecar):
                checked_path(sidecar, file=True)
        try:
            # Diagnostics must work without creating a ledger, schema or grants.
            with closing(sqlite3.connect(checked.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
                query = "SELECT state,record FROM native_transactions WHERE state NOT IN (?,?,?,?)"
                args = tuple(sorted(TERMINAL))
                if workspace is not None:
                    query += " AND workspace=?"
                    args += (workspace_key(workspace),)
                query += " ORDER BY updated LIMIT 4097"
                rows = db.execute(query, args).fetchall()
        except sqlite3.Error as exc:
            raise NativeBoundaryUnavailable("Native transaction ledger is unavailable") from exc
        if len(rows) > 4096:
            raise NativeBoundaryUnavailable("Native lifecycle scan exceeds bound")
        if any(len(raw.encode("utf-8")) > MAX_RECORD_BYTES for _, raw in rows):
            raise NativeBoundaryUnavailable("Native transaction record exceeds bound")
        return [json.loads(raw) for state, raw in rows if state not in TERMINAL]

    def get(self, identity: str) -> dict:
        with self._db() as db:
            row = db.execute("SELECT record FROM native_transactions WHERE id=?", (identity,)).fetchone()
        if row is None or len(row[0].encode("utf-8")) > MAX_RECORD_BYTES:
            raise NativeBoundaryUnavailable("Native transaction record missing or oversized")
        return json.loads(row[0])

    @contextmanager
    def claim(self, identity: str):
        """Nonblocking OS claim shared by active execution and recovery."""
        if not re.fullmatch(r"[0-9a-f]{32}", identity):
            raise NativeBoundaryUnavailable("Invalid transaction claim identity")
        from mewcode.sandbox.lpac_runtime import checked_path
        from mewcode.sandbox.publication_io import PinnedPath
        from mewcode.sandbox.staged_workspace import _write_new
        directory = self.root / "claims"
        directory.mkdir(exist_ok=True)
        checked_path(directory)
        path = directory / (identity + ".lock")
        try:
            _write_new(path, b"host lifecycle claim")
        except FileExistsError:
            pass
        try:
            pinned = PinnedPath(path, exclusive=True)
        except OSError as error:
            if getattr(error, "winerror", None) in {32, 33}:
                raise NativeTransactionBusy("Native transaction is owned by an active host operation") from error
            raise
        with pinned:
            yield

    def startup_inventory(self) -> dict:
        # No mutations/recovery without restored current authority. A different
        # app process may still be executing; do not classify by PID guesses.
        pending = self.pending()
        return {"pending": len(pending), "transactions": [
            {"id": r["id"], "task_id": r["task_id"], "state": r["state"],
             "cleanup_confirmed": r.get("native_receipt", {}).get("cleanup_confirmed")}
            for r in pending]}


def private_candidate_only(record: dict) -> bool:
    """Host-recorded discard runs cannot publish or hold source metadata grants."""
    if (record.get("publication_mode") != "discard"
            or record.get("metadata_broker_required") is not False
            or record.get("state") not in {"preparing", "executing", "native_done", "cleanup_unknown"}
            or not record.get("transaction")):
        return False
    from mewcode.sandbox.lpac_runtime import checked_path
    try:
        transaction = checked_path(Path(record["transaction"]))
        source = Path(record["workspace"]).resolve(strict=True)
        return (not transaction.is_relative_to(source) and not source.is_relative_to(transaction)
                and not os.path.lexists(transaction / "publication"))
    except (OSError, ValueError, RuntimeError, KeyError):
        return False


def _workspace_block(workspace: Path, *, commands: bool, independent_command: str | None = None) -> str:
    registry = TransactionRegistry()
    if not registry.path.exists():
        return ""
    try:
        records = [r for r in registry.pending(workspace) if r["id"] != CURRENT_TRANSACTION.get()]
        if any(not private_candidate_only(r) for r in records):
            return "上次执行的项目文件尚未恢复完成，暂不能写入或运行命令。请在沙箱设置中检查恢复结果。"
        if commands and records and independent_command:
            from .independent_execution import private_cleanup_dependency
            records = [r for r in records if private_cleanup_dependency(r, workspace, independent_command) is None]
        if commands and records:
            return "上次隔离命令尚未确认结束，暂不能运行新命令；可以继续查看和编辑项目文件。"
    except (OSError, ValueError, sqlite3.Error, NativeBoundaryUnavailable):
        return "无法读取上次执行状态，暂不能写入或运行命令。"
    return ""


def workspace_write_block(workspace: Path) -> str:
    return _workspace_block(workspace, commands=False)


def workspace_command_block(workspace: Path, *, independent_command: str | None = None) -> str:
    return _workspace_block(workspace, commands=True, independent_command=independent_command)
