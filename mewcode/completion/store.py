from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class PreferenceStats:
    accepted_count: int = 0
    rejected_count: int = 0


@dataclass(frozen=True)
class AliasMatch:
    type_name: str
    observations: int


class CompletionPreferenceStore:
    """Aggregate-only SQLite storage for completion learning.

    The schema never stores source text, prompts, file paths, or user-provided
    identifiers. One database represents one scope (user or project).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS completion_preferences (
                    language TEXT NOT NULL,
                    context_key TEXT NOT NULL,
                    candidate_key TEXT NOT NULL,
                    accepted_count INTEGER NOT NULL DEFAULT 0
                        CHECK (accepted_count >= 0),
                    rejected_count INTEGER NOT NULL DEFAULT 0
                        CHECK (rejected_count >= 0),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (language, context_key, candidate_key)
                );

                CREATE INDEX IF NOT EXISTS idx_completion_preferences_context
                    ON completion_preferences(language, context_key);

                CREATE TABLE IF NOT EXISTS completion_aliases (
                    language TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    type_name TEXT NOT NULL,
                    observations INTEGER NOT NULL DEFAULT 1
                        CHECK (observations >= 1),
                    last_evidence_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (language, alias, type_name)
                );

                CREATE INDEX IF NOT EXISTS idx_completion_alias_lookup
                    ON completion_aliases(language, alias, observations DESC);
                """
            )

    def get_preferences(
        self,
        language: str,
        context_key: str,
        candidate_keys: list[str],
    ) -> dict[str, PreferenceStats]:
        if not candidate_keys:
            return {}
        unique_keys = list(dict.fromkeys(candidate_keys))[:100]
        placeholders = ",".join("?" for _ in unique_keys)
        query = (
            "SELECT candidate_key, accepted_count, rejected_count "
            "FROM completion_preferences "
            "WHERE language = ? AND context_key = ? "
            f"AND candidate_key IN ({placeholders})"
        )
        parameters = [language, context_key, *unique_keys]
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(query, parameters).fetchall()
        return {
            row["candidate_key"]: PreferenceStats(
                accepted_count=row["accepted_count"],
                rejected_count=row["rejected_count"],
            )
            for row in rows
        }

    def record_feedback(
        self,
        language: str,
        context_key: str,
        candidate_key: str,
        accepted: bool,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        accepted_increment = 1 if accepted else 0
        rejected_increment = 0 if accepted else 1
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO completion_preferences (
                    language, context_key, candidate_key,
                    accepted_count, rejected_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(language, context_key, candidate_key) DO UPDATE SET
                    accepted_count = accepted_count + excluded.accepted_count,
                    rejected_count = rejected_count + excluded.rejected_count,
                    updated_at = excluded.updated_at
                """,
                (
                    language,
                    context_key,
                    candidate_key,
                    accepted_increment,
                    rejected_increment,
                    now,
                ),
            )

    def observe_alias(
        self,
        language: str,
        alias: str,
        type_name: str,
        evidence_hash: str,
    ) -> None:
        if not evidence_hash:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO completion_aliases (
                    language, alias, type_name, observations,
                    last_evidence_hash, updated_at
                ) VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(language, alias, type_name) DO UPDATE SET
                    observations = completion_aliases.observations + 1,
                    last_evidence_hash = excluded.last_evidence_hash,
                    updated_at = excluded.updated_at
                WHERE completion_aliases.last_evidence_hash
                    <> excluded.last_evidence_hash
                """,
                (language, alias, type_name, evidence_hash, now),
            )

    def resolve_alias(
        self,
        language: str,
        alias: str,
        limit: int = 5,
    ) -> list[AliasMatch]:
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT type_name, observations
                FROM completion_aliases
                WHERE language = ? AND alias = ?
                ORDER BY observations DESC, updated_at DESC
                LIMIT ?
                """,
                (language, alias, max(1, min(limit, 20))),
            ).fetchall()
        return [
            AliasMatch(
                type_name=row["type_name"],
                observations=row["observations"],
            )
            for row in rows
        ]
