from __future__ import annotations

import hashlib
import sqlite3
import threading
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


VALID_MEMORY_TYPES = {"user", "feedback", "project", "reference"}


@dataclass(frozen=True)
class StoredMemory:
    id: str
    name: str
    description: str
    type: str
    content: str
    source: str
    created_at: str
    updated_at: str

    @property
    def updated_at_ms(self) -> int:
        try:
            return int(datetime.fromisoformat(self.updated_at).timestamp() * 1000)
        except ValueError:
            return 0


class SQLiteMemoryStore:
    """Transactional storage for one memory scope."""

    def __init__(self, path: Path, scope: str) -> None:
        if scope not in {"user", "project"}:
            raise ValueError(f"Invalid memory scope: {scope}")
        self.path = path
        self.scope = scope
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _initialize(self) -> None:
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL DEFAULT '',
                    type TEXT NOT NULL CHECK (
                        type IN ('user', 'feedback', 'project', 'reference')
                    ),
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_memories_type
                    ON memories(type);
                CREATE INDEX IF NOT EXISTS idx_memories_updated_at
                    ON memories(updated_at DESC);

                CREATE TABLE IF NOT EXISTS memory_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            try:
                conn.executescript(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                        name, description, content,
                        content='memories', content_rowid='rowid'
                    );

                    CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                        INSERT INTO memory_fts(rowid, name, description, content)
                        VALUES (new.rowid, new.name, new.description, new.content);
                    END;
                    CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                        INSERT INTO memory_fts(memory_fts, rowid, name, description, content)
                        VALUES ('delete', old.rowid, old.name, old.description, old.content);
                    END;
                    CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                        INSERT INTO memory_fts(memory_fts, rowid, name, description, content)
                        VALUES ('delete', old.rowid, old.name, old.description, old.content);
                        INSERT INTO memory_fts(rowid, name, description, content)
                        VALUES (new.rowid, new.name, new.description, new.content);
                    END;
                    """
                )
                conn.execute("INSERT INTO memory_fts(memory_fts) VALUES ('rebuild')")
            except sqlite3.OperationalError:
                # Some Python SQLite builds omit FTS5. LIKE search remains available.
                pass

    @staticmethod
    def _row_to_memory(row: sqlite3.Row) -> StoredMemory:
        return StoredMemory(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            type=row["type"],
            content=row["content"],
            source=row["source"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _validate(name: str, content: str, memory_type: str) -> tuple[str, str]:
        name = name.strip()
        content = content.strip()
        if not name:
            raise ValueError("Memory name cannot be empty")
        if not content:
            raise ValueError("Memory content cannot be empty")
        if memory_type not in VALID_MEMORY_TYPES:
            raise ValueError(f"Invalid memory type: {memory_type}")
        return name, content

    def add(
        self,
        name: str,
        content: str,
        description: str = "",
        memory_type: str = "reference",
        source: str = "manual",
    ) -> StoredMemory:
        name, content = self._validate(name, content, memory_type)
        now = datetime.now(timezone.utc).isoformat()
        memory_id = uuid.uuid4().hex
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self._lock, closing(self._connect()) as conn, conn:
            try:
                conn.execute(
                    """
                    INSERT INTO memories (
                        id, name, description, type, content, content_hash,
                        source, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        name,
                        description.strip(),
                        memory_type,
                        content,
                        content_hash,
                        source,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"Memory already exists: {name}") from exc
        memory = self.get(name)
        assert memory is not None
        return memory

    def upsert(
        self,
        name: str,
        content: str,
        description: str = "",
        memory_type: str = "reference",
        source: str = "manual",
    ) -> StoredMemory:
        name, content = self._validate(name, content, memory_type)
        now = datetime.now(timezone.utc).isoformat()
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO memories (
                    id, name, description, type, content, content_hash,
                    source, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    description = excluded.description,
                    type = excluded.type,
                    content = excluded.content,
                    content_hash = excluded.content_hash,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    uuid.uuid4().hex,
                    name,
                    description.strip(),
                    memory_type,
                    content,
                    content_hash,
                    source,
                    now,
                    now,
                ),
            )
        memory = self.get(name)
        assert memory is not None
        return memory

    def update(
        self,
        name: str,
        content: str,
        description: str | None = None,
        memory_type: str | None = None,
    ) -> StoredMemory:
        existing = self.get(name)
        if existing is None:
            raise ValueError(f"Memory not found: {name}")
        return self.upsert(
            name=name,
            content=content,
            description=existing.description if description is None else description,
            memory_type=existing.type if memory_type is None else memory_type,
            source=existing.source,
        )

    def get(self, name_or_id: str) -> StoredMemory | None:
        with self._lock, closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE name = ? OR id = ? LIMIT 1",
                (name_or_id, name_or_id),
            ).fetchone()
        return self._row_to_memory(row) if row is not None else None

    def list(self, limit: int = 200) -> list[StoredMemory]:
        with self._lock, closing(self._connect()) as conn, conn:
            rows = conn.execute(
                "SELECT * FROM memories ORDER BY updated_at DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return [self._row_to_memory(row) for row in rows]

    def search(self, query: str, limit: int = 50) -> list[StoredMemory]:
        query = query.strip()
        if not query:
            return self.list(limit)

        rows: list[sqlite3.Row] = []
        with self._lock, closing(self._connect()) as conn, conn:
            try:
                rows = conn.execute(
                    """
                    SELECT memories.*
                    FROM memory_fts
                    JOIN memories ON memories.rowid = memory_fts.rowid
                    WHERE memory_fts MATCH ?
                    ORDER BY bm25(memory_fts), memories.updated_at DESC
                    LIMIT ?
                    """,
                    (f'"{query.replace(chr(34), chr(34) * 2)}"', max(1, limit)),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []

            if not rows:
                pattern = f"%{query}%"
                rows = conn.execute(
                    """
                    SELECT * FROM memories
                    WHERE name LIKE ? OR description LIKE ? OR content LIKE ?
                    ORDER BY updated_at DESC LIMIT ?
                    """,
                    (pattern, pattern, pattern, max(1, limit)),
                ).fetchall()
        return [self._row_to_memory(row) for row in rows]

    def delete(self, name_or_id: str) -> bool:
        with self._lock, closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "DELETE FROM memories WHERE name = ? OR id = ?",
                (name_or_id, name_or_id),
            )
        return cursor.rowcount > 0

    def clear(self) -> int:
        with self._lock, closing(self._connect()) as conn, conn:
            cursor = conn.execute("DELETE FROM memories")
        return cursor.rowcount

    def get_metadata(self, key: str) -> str | None:
        with self._lock, closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT value FROM memory_metadata WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row is not None else None

    def set_metadata(self, key: str, value: str) -> None:
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO memory_metadata(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
